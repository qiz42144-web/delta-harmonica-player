# -*- coding: utf-8 -*-
r"""批量体检一批 MIDI：解析 -> 选轨 -> 移调 -> 映射 -> 编译，把问题量化出来。

    python tools/analyze_midis.py "D:\Data\Desktop\口琴学习"

每个文件会给出：
  * 轨道结构（音符数 / 音域 / 中位时值 / 和弦密度）
  * 每条轨道「真吹得出来多少音」的预估，以及自动选中的轨道
  * 自动移调建议（让调外音最少，从而少按中键）
  * 映射后的折叠 / 丢弃 / 跳过 / 八度切换数量
"""
from __future__ import annotations

import os
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import mapping
import smf

LO, HI = mapping.DEFAULT_BASE + mapping.RANGE_LOW, mapping.DEFAULT_BASE + mapping.RANGE_HIGH


def simultaneity(notes):
    points = []
    for note in notes:
        points.append((note.start, 1))
        points.append((note.end, -1))
    points.sort(key=lambda item: (item[0], -item[1]))
    current = peak = 0
    for _t, delta in points:
        current += delta
        peak = max(peak, current)
    return peak


def penalty_score(info):
    """和 mapping.pick_best_track 用同一套打分，便于对照。"""
    return (info["playable"] - 1.2 * info["lost"]
            - 0.5 * info["folded"] - 0.8 * (abs(info["global_shift"]) // 12))


def analyze(path):
    midi = smf.load(path)
    print("=" * 108)
    print(f"■ {os.path.basename(path)}   格式 {midi.format} / {midi.track_count} 轨 / "
          f"总长 {midi.length_ms / 1000:.1f}s")

    infos = {}
    for track in midi.tracks:
        if not track.notes:
            continue
        info = mapping.analyse_track(track)
        infos[track.index] = info
        lo, hi = track.pitch_range
        durations = sorted(n.duration for n in track.notes)
        median = durations[len(durations) // 2]
        print(f"    [{track.index:>2}] {track.name or '(无名)':<20} "
              f"{len(track.notes):>5d} 音  音域 {lo:>3d}-{hi:<3d}  中位 {median:6.0f}ms  "
              f"同时≤{simultaneity(track.notes)}  "
              f"→ 可吹 {info['playable']:>5d}  吃音 {info['lost']:>4d}  "
              f"折叠 {info['folded']:>3d}  移调 {info['global_shift']:>+4d}  "
              f"评分 {penalty_score(info):>7.1f}"
              f"{'  [打击乐]' if track.is_drum else ''}")

    picked = mapping.pick_best_track(midi.tracks)
    if picked is None:
        print("  !! 没有可用轨道")
        return None
    print(f"  → 选中 [{picked.index}] {picked.name or '(无名)'}")

    # 自动移调建议
    best_t = mapping.best_transpose(picked.notes)
    base_map = mapping.map_notes(picked.notes, transpose=0)
    best_map = mapping.map_notes(picked.notes, transpose=best_t)
    sharp0 = sum(1 for m in base_map.notes if m.candidate.sharp) / max(1, len(base_map.notes))
    sharp1 = sum(1 for m in best_map.notes if m.candidate.sharp) / max(1, len(best_map.notes))

    events, skipped = mapping.compile_events(best_map.notes)
    duration_s = events[-1].time / 1000 if events else 0
    shifted = sum(1 for i in range(1, len(best_map.notes))
                  if best_map.notes[i].candidate.shift != best_map.notes[i - 1].candidate.shift)
    keys = Counter(m.candidate.key for m in best_map.notes)

    print(f"  自动移调 : {best_t:+d} 半音   中键半音使用率 {sharp0 * 100:.0f}% -> {sharp1 * 100:.0f}%")
    print(f"  映射结果 : 可吹 {len(best_map.notes)} / 原始 {len(picked.notes)}   "
          f"吃音 {best_map.lost}  折叠 {best_map.folded}  丢弃 {best_map.dropped}   "
          f"过短跳过 {skipped}   整轨移调 {best_map.global_shift:+d}")
    print(f"  演奏参数 : 时长 {duration_s:.1f}s   密度 {len(best_map.notes) / max(0.001, duration_s):.1f} 音/秒   "
          f"八度切换 {shifted} 次（{shifted / max(1, len(best_map.notes)) * 100:.0f}%）")
    print(f"  键位分布 : " + "  ".join(f"{k}:{v}" for k, v in sorted(keys.items())))

    problems = []
    if best_map.dropped:
        problems.append(f"{best_map.dropped} 个音被丢弃")
    if skipped:
        problems.append(f"{skipped} 个音因时值过短被跳过")
    if best_map.lost > len(picked.notes) * 0.15:
        problems.append(f"该轨是和弦/多声部，{best_map.lost} 个音被取最高音吃掉 —— "
                        f"建议换一条更单声部的轨道")
    if len(best_map.notes) / max(0.001, duration_s) > 6:
        problems.append("音符密度偏高，游戏里可能糊成一片")
    if shifted > len(best_map.notes) * 0.4:
        problems.append(f"八度修饰键切换 {shifted} 次，偏多")
    if sharp1 > 0.15:
        problems.append(f"移调后仍有 {sharp1 * 100:.0f}% 的调外音要按中键")
    if problems:
        print("  ⚠ " + "\n     - ".join([""] + problems).lstrip())
    else:
        print("  ✓ 没发现明显问题")

    return {"file": os.path.basename(path), "track": picked.index,
            "raw": len(picked.notes), "playable": len(best_map.notes),
            "lost": best_map.lost, "folded": best_map.folded, "skipped": skipped,
            "transpose": best_t, "shift_switch": shifted, "duration": duration_s,
            "density": len(best_map.notes) / max(0.001, duration_s),
            "sharp": sharp1}


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "demo")
    files = []
    for root, _dirs, names in os.walk(target):
        for name in names:
            if os.path.splitext(name)[1].lower() in (".mid", ".midi", ".kar", ".rmi"):
                files.append(os.path.join(root, name))
    files.sort()
    print(f"共 {len(files)} 个 MIDI 文件，目录：{target}\n")

    results = []
    for path in files:
        try:
            item = analyze(path)
            if item:
                results.append(item)
        except Exception as exc:
            print("=" * 108)
            print(f"■ {os.path.basename(path)}")
            print(f"  !! 解析/映射失败：{exc!r}")

    print("\n" + "=" * 108)
    print("汇总（移调 = 自动选的最佳移调；中键 = 半音键使用率）")
    header = (f"{'文件':<38}{'轨':>3}{'原始':>6}{'可吹':>6}{'吃音':>6}{'折叠':>6}"
              f"{'跳过':>6}{'移调':>6}{'八度切换':>9}{'中键':>7}{'时长':>8}{'密度':>7}")
    print(header)
    print("-" * len(header))
    for item in sorted(results, key=lambda x: x["file"]):
        name = item["file"]
        if len(name) > 36:
            name = name[:16] + "…" + name[-18:]
        print(f"{name:<38}{item['track']:>3}{item['raw']:>6}{item['playable']:>6}"
              f"{item['lost']:>6}{item['folded']:>6}{item['skipped']:>6}"
              f"{item['transpose']:>+6d}{item['shift_switch']:>9}"
              f"{item['sharp'] * 100:>6.0f}%{item['duration']:>7.0f}s{item['density']:>7.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
