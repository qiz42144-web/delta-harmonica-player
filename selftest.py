# -*- coding: utf-8 -*-
"""无 GUI 自检：解析 / 映射 / 事件编译 / 定时回放。

    python selftest.py

不需要游戏，也不发送真实按键（用 dry-run 发送器）。
任何一项失败都会以非 0 退出码结束。
"""
from __future__ import annotations

import os
import struct
import sys
import time

try:                                     # Windows 控制台可能是 GBK/CP932，强制 UTF-8 输出
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mapping
import player as player_mod
import smf
import synth
from demo_midi import DIVISION, build_demo

FAILURES = []
CHECKS = [0]


def check(name, condition, detail=""):
    CHECKS[0] += 1
    if condition:
        print(f"  [PASS] {name}")
    else:
        print(f"  [FAIL] {name} {detail}")
        FAILURES.append(f"{name} {detail}")


def section(title):
    print(f"\n=== {title} ===")


# --------------------------------------------------------------------------- #
def test_parse():
    section("1. MIDI 解析")
    midi = smf.parse_midi(build_demo(), name="demo")
    check("format = 1", midi.format == 1, f"实际 {midi.format}")
    check("division = 480", midi.division == DIVISION, f"实际 {midi.division}")
    check("轨道数 = 3", midi.track_count == 3, f"实际 {midi.track_count}")

    melody, drums, chords = midi.tracks
    check("主旋律轨名称解析", melody.name == "Melody 主旋律", f"实际 {melody.name!r}")
    check("打击乐轨名称解析", drums.name == "Drums 打击乐", f"实际 {drums.name!r}")
    check("打击乐轨被识别", drums.is_drum, f"channels={drums.channels}")
    check("主旋律轨不是打击乐", not melody.is_drum)
    check("主旋律音符数 = 47", len(melody.notes) == 47, f"实际 {len(melody.notes)}")
    check("伴奏轨音符数 = 24", len(chords.notes) == 24, f"实际 {len(chords.notes)}")

    # 120 BPM、480 PPQ -> 一拍 500ms；最后一个音在第 52 拍结束
    check("总时长 ≈ 26000ms", abs(melody.duration - 26000) < 1,
          f"实际 {melody.duration:.1f}ms")
    check("首音时间 = 0", abs(melody.notes[0].start) < 1e-6,
          f"实际 {melody.notes[0].start}")
    check("音高序列正确", melody.notes[0].pitch == 60 and melody.notes[2].pitch == 67,
          f"前三个音 {[n.pitch for n in melody.notes[:3]]}")

    picked = smf.pick_melody_track(midi)
    check("自动选轨选中主旋律", picked.index == melody.index,
          f"实际选中 {picked.index} {picked.name!r}")


def test_parse_edge_cases():
    section("2. 解析边界情况")

    # running status + Note On(velocity=0) 当作 Note Off
    track_body = bytearray()

    def ev(delta, payload):
        track_body.extend(_varlen(delta))
        track_body.extend(payload)

    ev(0, b"\xFF\x51\x03\x07\xA1\x20")          # 500000 -> 但我们用 0x07A120 = 500000
    ev(0, bytes([0x90, 60, 90]))                # Note On
    ev(DIVISION // 2, bytes([60, 0]))           # running status + velocity 0 = Note Off
    ev(0, bytes([0x90, 64, 90]))
    ev(DIVISION // 2, bytes([0x80, 64, 0]))
    overlay = b"MTrk" + struct.pack(">I", len(track_body)) + bytes(track_body)
    data = b"MThd" + struct.pack(">IHHH", 6, 0, 1, DIVISION) + overlay
    midi = smf.parse_midi(data)
    notes = midi.tracks[0].notes
    check("running status 解析出 2 个音", len(notes) == 2, f"实际 {len(notes)}")
    check("velocity=0 被当成 Note Off",
          len(notes) == 2 and abs(notes[0].end - 250) < 1,
          f"第一个音结束于 {notes[0].end if notes else 'n/a'}")

    # 缺少 MThd
    try:
        smf.parse_midi(b"not a midi file at all")
        check("非法文件抛 MidiError", False)
    except smf.MidiError:
        check("非法文件抛 MidiError", True)

    # 空轨道
    empty = b"MThd" + struct.pack(">IHHH", 6, 0, 1, DIVISION) + b"MTrk" + struct.pack(">I", 4) + b"\x00\xFF\x2F\x00"
    midi_empty = smf.parse_midi(empty)
    check("空轨道解析不报错", len(midi_empty.tracks) == 1 and not midi_empty.tracks[0].notes)


def _varlen(value):
    out = bytearray([value & 0x7F])
    value >>= 7
    while value:
        out.insert(0, (value & 0x7F) | 0x80)
        value >>= 7
    return bytes(out)


# --------------------------------------------------------------------------- #
def test_mapping():
    section("3. 键位映射")
    midi = smf.parse_midi(build_demo())
    melody = midi.tracks[0]

    base = mapping.DEFAULT_BASE
    lo, hi = base + mapping.RANGE_LOW, base + mapping.RANGE_HIGH
    check("音域 = 38 个半音", hi - lo + 1 == 38, f"{lo}..{hi}")

    mapped, dropped, folded = mapping.map_notes(melody.notes, base=base)
    check("映射没有丢音", dropped == 0, f"dropped={dropped}")
    check("两个越界音被折叠", folded == 2, f"folded={folded}")
    check("少量越界音不触发整体移调",
          mapping.best_global_octave([n.pitch for n in melody.notes], base) == 0)
    check("整轨偏低时整体升八度",
          mapping.best_global_octave(list(range(36, 48)), base) == 12)
    check("整轨偏高时整体降八度",
          mapping.best_global_octave(list(range(84, 96)), base) == -12)
    check("所有音落在音域内", all(lo <= m.pitch <= hi for m in mapped),
          f"越界：{[m.pitch for m in mapped if not (lo <= m.pitch <= hi)]}")

    # 每个音的指法必须真的能发出目标音高
    bad = []
    for m in mapped:
        c = m.candidate
        got = base + 12 * c.shift + mapping.KEY_OFFSETS[c.key_index] + c.sharp
        if got != m.pitch:
            bad.append((m.pitch, got, c))
    check("指法公式自洽", not bad, f"不匹配 {bad[:3]}")

    # 覆盖性：音域内每个半音都至少有一种指法
    missing = [p for p in range(lo, hi + 1) if not mapping.candidates_for(p, base)]
    check("音域内无空洞", not missing, f"缺失 {missing}")

    # 半音必须用中键
    sharp_note = [m for m in mapped if m.original_pitch == 61]
    check("半音 61 使用中键修饰", sharp_note and sharp_note[0].candidate.sharp == 1,
          f"{sharp_note[0].candidate if sharp_note else '没找到 61'}")

    # 重复音应该用同一个键（不会来回跳）
    first_two = mapped[:2]
    check("同音高重复使用同一键位",
          first_two[0].candidate.key_index == first_two[1].candidate.key_index,
          f"{first_two[0].candidate} vs {first_two[1].candidate}")

    # 关闭折叠 -> 应该丢弃
    _, dropped2, _ = mapping.map_notes(melody.notes, base=base, fold=False)
    check("关闭折叠时超范围音被丢弃", dropped2 >= 1, f"dropped={dropped2}")

    # 空输入
    empty = mapping.map_notes([])
    check("空音符列表安全", empty.notes == [] and empty.dropped == 0 and empty.folded == 0)
    check("空输入解包也安全", tuple(mapping.map_notes([])) == ([], 0, 0))

    # 幂等：反复映射不应该把原始音符越截越短（GUI 改参数会反复调用）
    before = [(n.start, n.end) for n in melody.notes]
    first = mapping.map_notes(melody.notes, base=base)
    second = mapping.map_notes(melody.notes, base=base)
    third = mapping.map_notes(melody.notes, base=base)
    check("原始 MIDI 音符未被就地修改",
          [(n.start, n.end) for n in melody.notes] == before)
    check("重复映射结果稳定",
          [(m.pitch, m.start, m.end) for m in first.notes]
          == [(m.pitch, m.start, m.end) for m in second.notes]
          == [(m.pitch, m.start, m.end) for m in third.notes])
    check("重复映射统计稳定",
          (first.dropped, first.folded, first.lost, first.global_shift)
          == (second.dropped, second.folded, second.lost, second.global_shift)
          == (third.dropped, third.folded, third.lost, third.global_shift))


def test_compile():
    section("4. 演奏事件编译")
    midi = smf.parse_midi(build_demo())
    mapped, _, _ = mapping.map_notes(midi.tracks[0].notes)
    events, skipped = mapping.compile_events(mapped)
    check("事件非空", len(events) > 50, f"实际 {len(events)}")
    check("事件时间单调不减",
          all(events[i].time <= events[i + 1].time for i in range(len(events) - 1)))

    held_keys, held_mouse, problems = set(), set(), []
    for e in events:
        if e.kind == "key_down":
            if held_keys:
                problems.append(f"按下了第二个键 {e}")
            if e.arg in held_keys:
                problems.append(f"重复按下 {e}")
            held_keys.add(e.arg)
        elif e.kind == "key_up":
            held_keys.discard(e.arg)
        elif e.kind == "mouse_down":
            if held_keys:
                problems.append(f"按键未松开就切换修饰键 {e}")
            held_mouse.add(e.arg)
        elif e.kind == "mouse_up":
            held_mouse.discard(e.arg)
        else:
            problems.append(f"未知事件 {e}")
    check("不存在同按两键 / 带键切修饰键", not problems, f"{problems[:3]}")
    check("结束时没有残留按键", not held_keys, f"残留 {held_keys}")
    check("结束时没有残留修饰键", not held_mouse, f"残留 {held_mouse}")

    # 左右键互斥
    lr_problems = []
    down_set = set()
    for e in events:
        if e.kind == "mouse_down":
            down_set.add(e.arg)
            if mapping.MOUSE_LEFT in down_set and mapping.MOUSE_RIGHT in down_set:
                lr_problems.append(e)
        elif e.kind == "mouse_up":
            down_set.discard(e.arg)
    check("左右八度键互斥", not lr_problems, f"{lr_problems[:2]}")

    # 最短音长
    too_short = []
    pending = None
    for e in events:
        if e.kind == "key_down":
            pending = e
        elif e.kind == "key_up" and pending is not None:
            if e.time - pending.time < mapping.MIN_HOLD_MS - 0.001:
                too_short.append((pending.time, e.time))
            pending = None
    check("没有过短的按键", not too_short, f"{too_short[:3]}")

    # 倍速
    fast = mapping.scale_events(events, 2.0)
    check("2 倍速时长减半",
          abs(fast[-1].time - events[-1].time / 2) < 0.01,
          f"{fast[-1].time} vs {events[-1].time}")


def test_playback():
    section("5. dry-run 定时回放")
    midi = smf.parse_midi(build_demo())
    mapped, _, _ = mapping.map_notes(midi.tracks[0].notes[:10])
    events, _ = mapping.compile_events(mapped)
    sender = player_mod.InputSender(dry_run=True)
    progress = []
    finished = []
    p = player_mod.Player(sender, on_progress=lambda a, b: progress.append((a, b)),
                          on_finish=lambda ok, why: finished.append((ok, why)))
    expected = events[-1].time
    t0 = time.perf_counter()
    p.play(events, block=True)
    elapsed = (time.perf_counter() - t0) * 1000
    check("回放时长接近理论值", abs(elapsed - expected) < 60,
          f"实测 {elapsed:.1f}ms / 理论 {expected:.1f}ms")
    check("进度回调被调用", len(progress) >= 2, f"次数 {len(progress)}")
    check("完成回调成功", finished and finished[0][0] is True, f"{finished}")

    # 中途停止
    sender2 = player_mod.InputSender(dry_run=True)
    long_events, _ = mapping.compile_events(mapping.map_notes(midi.tracks[0].notes).notes)
    p2 = player_mod.Player(sender2)
    t0 = time.perf_counter()
    p2.play(long_events, block=False)
    time.sleep(0.25)
    p2.stop()
    p2._thread.join(timeout=2)
    stopped_at = (time.perf_counter() - t0) * 1000
    check("可以中途停止", not p2.is_playing and stopped_at < 2000, f"{stopped_at:.0f}ms")
    check("停止后没有残留输入",
          not sender2._held_keys and not sender2._held_mouse,
          f"keys={sender2._held_keys} mouse={sender2._held_mouse}")


def test_pick_and_transpose():
    section("6. 自动选轨 / 自动移调 / 速度相关")
    midi = smf.parse_midi(build_demo())
    melody, drums, chords = midi.tracks

    picked = mapping.pick_best_track(midi.tracks)
    check("自动选轨选中主旋律而不是伴奏/打击乐", picked.index == melody.index,
          f"实际 [{picked.index}] {picked.name!r}")

    info = mapping.analyse_track(melody)
    check("analyse_track 可吹数正确", info["playable"] == 49 or info["playable"] == 47,
          f"可吹 {info['playable']}")
    check("analyse_track 统计字段齐全",
          {"playable", "lost", "folded", "dropped", "global_shift", "sharps",
           "shift_switch"} <= set(info))

    chord_info = mapping.analyse_track(chords)
    check("和弦轨被识别出大量吃音", chord_info["lost"] >= 16,
          f"吃音 {chord_info['lost']}")

    # 自动移调：#F 大调音阶整体 -6 半音正好落到 C 大调（全是白键，不需要中键）
    scale = [66, 68, 70, 71, 73, 75, 77] * 4
    notes = [smf.Note(pitch=p, start=i * 500.0, end=i * 500.0 + 400.0)
             for i, p in enumerate(scale)]
    before = mapping.map_notes(notes, transpose=0)
    before_ratio = sum(1 for m in before.notes if m.candidate.sharp) / len(before.notes)
    best = mapping.best_transpose(notes)
    after = mapping.map_notes(notes, transpose=best)
    after_ratio = sum(1 for m in after.notes if m.candidate.sharp) / len(after.notes)
    check("自动移调把 #F 大调搬到 C 大调", best == -6, f"实际 {best:+d}")
    check("自动移调显著减少中键使用", after_ratio < before_ratio * 0.35,
          f"{before_ratio * 100:.0f}% -> {after_ratio * 100:.0f}%")

    # 减速之后，原本「太短吹不出来」的音应该被救回来
    mapped, _, _ = mapping.map_notes(melody.notes)
    _, skipped_fast = mapping.compile_events(mapped, speed=1.0)
    _, skipped_slow = mapping.compile_events(mapped, speed=0.5)
    check("减速后过短的音变少", skipped_slow <= skipped_fast,
          f"1.0x 跳过 {skipped_fast}，0.5x 跳过 {skipped_slow}")

    # Mapping 仍可当三元组解包（向后兼容）
    a, b, c = mapping.map_notes(melody.notes)
    check("Mapping 可解包为三元组", isinstance(a, list) and isinstance(b, int)
          and isinstance(c, int))


def test_segment_speed_audio():
    section("7. 段落截取 / 变速建议 / 试听合成")
    midi = smf.parse_midi(build_demo())
    melody = midi.tracks[0]
    mapped, _, _ = mapping.map_notes(melody.notes)

    # ---- 试听与演奏必须共用同一份时间线 ----
    spans, skipped_a = mapping.note_spans(mapped)
    events_a, skipped_b = mapping.compile_events(mapped)
    check("note_spans 与 compile_events 跳过数一致", skipped_a == skipped_b,
          f"{skipped_a} vs {skipped_b}")
    check("events_from_spans 与 compile_events 结果一致",
          [(e.time, e.kind, e.arg) for e in mapping.events_from_spans(spans)]
          == [(e.time, e.kind, e.arg) for e in events_a])

    base = mapping.DEFAULT_BASE
    consistent = all(
        base + 12 * sp.candidate.shift + mapping.KEY_OFFSETS[sp.candidate.key_index]
        + sp.candidate.sharp == sp.pitch
        for sp in spans)
    check("每个 span 的音高都能被它的指法发出来", consistent)

    # ---- 段落截取 ----
    total = max(n.end for n in mapped)
    segment = mapping.slice_mapped(mapped, 5000, 10000)
    check("截取后时间轴从 0 开始", bool(segment) and abs(segment[0].start) < 1e-6,
          f"{segment[0].start if segment else 'n/a'}")
    check("截取后没有音超出区间长度",
          all(n.end <= 5000 + 1e-6 for n in segment))
    check("截取不丢区间内起始的音",
          len(segment) >= sum(1 for n in mapped if 5000 <= n.start < 10000))
    check("(0,0) 表示整曲，原样返回", len(mapping.slice_mapped(mapped, 0, 0)) == len(mapped))
    check("空区间返回空列表", mapping.slice_mapped(mapped, 9000, 9000) == [])
    check("截取 0..30s 与整曲一致（曲子更短）",
          len(mapping.slice_mapped(mapped, 0, total + 5000)) == len(mapped))

    # ---- 变速建议 ----
    suggestion = mapping.recommend_speed(mapped)
    check("有极短音时会给出变速建议", suggestion is not None
          and suggestion["speed"] < 1.0, f"{suggestion}")
    if suggestion:
        _, skipped_now = mapping.note_spans(mapped, speed=1.0)
        _, skipped_then = mapping.note_spans(mapped, speed=suggestion["speed"])
        check("按建议速度确实能少跳过音", skipped_then < skipped_now,
              f"1.0x {skipped_now} 个 -> {suggestion['speed']:.2f}x {skipped_then} 个")
        check("建议速度不低于下限", suggestion["speed"] >= mapping.MIN_NOTE_MS * 0 + 0.5)
        clean = [n for n in mapped if n.duration >= 80]
        check("没有短音时不提建议", mapping.recommend_speed(clean) is None)

    # ---- 空档填补：人声没唱的地方用钢琴补 ----
    primary = [
        smf.Note(pitch=72, start=2000.0, end=2300.0),
        smf.Note(pitch=74, start=2500.0, end=2800.0),
        smf.Note(pitch=72, start=6000.0, end=6300.0),
    ]
    secondary = [
        smf.Note(pitch=60, start=0.0, end=300.0),        # 前奏，应该被补上
        smf.Note(pitch=62, start=400.0, end=700.0),      # 前奏
        smf.Note(pitch=67, start=3000.0, end=3400.0),    # 中间空档，应补上
        smf.Note(pitch=69, start=2600.0, end=2900.0),    # 与主轨重叠，不能补
        smf.Note(pitch=65, start=6400.0, end=6800.0),    # 结尾空档，应补上
    ]
    merged, added = mapping.merge_tracks(primary, secondary, mono="legato")
    filled_pitches = [n.pitch for n in merged]
    check("填补会补上前奏", 60 in filled_pitches and 62 in filled_pitches, filled_pitches)
    check("填补会补上中间空档", 67 in filled_pitches)
    check("填补会补上结尾", 65 in filled_pitches)
    check("填补不会盖住主旋律", 69 not in filled_pitches, filled_pitches)
    check("填补后主旋律音一个不少", all(p in filled_pitches for p in (72, 74)))
    check("填补数量正确", added == 4, added)
    check("填补后时间顺序单调",
          all(merged[i].start <= merged[i + 1].start for i in range(len(merged) - 1)))
    check("填补后仍然单声部",
          all(merged[i].end <= merged[i + 1].start + 1e-6
              for i in range(len(merged) - 1)))
    empty_merge, added0 = mapping.merge_tracks(primary, [], mono="legato")
    check("没有可填补的轨时保持原样", len(empty_merge) == len(primary) and added0 == 0)

    # 只补开头：中间空档和结尾都不补
    lead_only, lead_added = mapping.merge_tracks(primary, secondary, mono="legato",
                                                 scope="lead")
    lead_pitches = [n.pitch for n in lead_only]
    check("只在开头补：前奏补上了", 60 in lead_pitches and 62 in lead_pitches)
    check("只在开头补：中间空档不补", 67 not in lead_pitches, lead_pitches)
    check("只在开头补：结尾不补", 65 not in lead_pitches)
    check("只在开头补：数量正确", lead_added == 2, lead_added)
    check("只在开头补：主旋律不丢", all(p in lead_pitches for p in (72, 74)))

    # ---- 开头静音裁剪 ----
    lead_spans = [
        mapping.Span(7000.0, 7300.0, 60, mapping.Candidate(0, 0, 0)),
        mapping.Span(7400.0, 7800.0, 64, mapping.Candidate(0, 0, 0)),
    ]
    trimmed = mapping.trim_lead(lead_spans)
    check("裁掉开头静音后第一个音从 0 开始", abs(trimmed[0].start) < 1e-6,
          trimmed[0].start)
    check("裁剪后音与音的相对间隔不变",
          abs((trimmed[1].start - trimmed[0].start)
              - (lead_spans[1].start - lead_spans[0].start)) < 1e-6)
    check("裁剪不改变音高与时值",
          [s.pitch for s in trimmed] == [60, 64]
          and abs(trimmed[0].duration - 300.0) < 1e-6)
    already = mapping.trim_lead([mapping.Span(0.0, 300.0, 60, mapping.Candidate(0, 0, 0))])
    check("本来就从 0 开始时保持原样", abs(already[0].start) < 1e-6)
    check("空输入安全", mapping.trim_lead([]) == [])
    kept = mapping.trim_lead(lead_spans, keep_ms=1000.0)
    check("keep_ms 会保留提前量", abs(kept[0].start - 1000.0) < 1e-6, kept[0].start)

    # ---- 试听合成 ----
    data = synth.build_wav(spans)
    check("合成出合法 WAV", data[:4] == b"RIFF" and data[8:12] == b"WAVE",
          f"头 {data[:12]!r}")
    duration = synth.wav_duration_ms(data)
    expected = max(sp.end for sp in spans)
    check("WAV 时长与时间线吻合", abs(duration - expected) < 250,
          f"{duration:.0f}ms vs {expected:.0f}ms")
    half = synth.build_wav(spans, speed=0.5)
    check("半速合成长度接近翻倍",
          synth.wav_duration_ms(half) > duration * 1.7,
          f"{synth.wav_duration_ms(half):.0f} vs {duration:.0f}")
    check("空输入返回空字节", synth.build_wav([]) == b"")

    long_spans = [mapping.Span(i * 1000.0, i * 1000.0 + 500.0, 60,
                               mapping.Candidate(0, 0, 0)) for i in range(120)]
    capped = synth.build_wav(long_spans)
    check("超长试听被截断到上限",
          synth.wav_duration_ms(capped) <= synth.MAX_SECONDS * 1000 + 250,
          f"{synth.wav_duration_ms(capped):.0f}ms")

    # winsound 不支持「内存 + 异步」，所以试听要落一个临时文件
    temp_path = synth.save_temp_wav(data)
    check("试听临时文件已落盘", os.path.exists(temp_path), temp_path)
    with open(temp_path, "rb") as fh:
        check("临时文件是合法 WAV", fh.read(4) == b"RIFF")
    synth.cleanup_temp()
    check("清理后临时文件已删除", not os.path.exists(temp_path))


def test_mono_mode():
    section("8. 单声部化策略（取最高音 vs 后起优先）")

    # 模拟一段「连奏人声」：每个音都比前一个晚一点起、晚一点收，声部在下降
    pitches = [60, 64, 67, 69, 67, 64, 60, 62]
    legato_notes = []
    for i, pitch in enumerate(pitches):
        start = i * 400.0
        legato_notes.append(smf.Note(pitch=pitch, start=start, end=start + 600.0))

    highest = mapping.converge_highest(legato_notes)
    legato = mapping.converge_legato(legato_notes)
    check("后起优先不丢音", len(legato) == len(pitches), f"{len(legato)} / {len(pitches)}")
    check("取最高音会丢音", len(highest) < len(pitches), f"{len(highest)} / {len(pitches)}")
    check("后起优先保住旋律走向",
          [n.pitch for n in legato] == pitches, [n.pitch for n in legato])
    check("后起优先把前一个音截断到后一个音的起点",
          all(abs(legato[i].end - legato[i + 1].start) < 1e-6
              for i in range(len(legato) - 1)))

    auto = mapping.map_notes(legato_notes)
    check("auto 在连奏人声上选后起优先", auto.mono == "legato", auto.mono)
    check("auto 保住全部音", len(auto.notes) == len(pitches), len(auto.notes))
    forced = mapping.map_notes(legato_notes, mono="highest")
    check("强制 highest 会少音", len(forced.notes) < len(pitches), len(forced.notes))

    # 和弦轨（同时响 3 个音）应该选「取最高音」
    chord_notes = []
    for i in range(6):
        start = i * 500.0
        for pitch in (48, 55, 64):
            chord_notes.append(smf.Note(pitch=pitch, start=start, end=start + 450.0))
    chord_auto = mapping.map_notes(chord_notes)
    check("auto 在和弦轨上选取最高音", chord_auto.mono == "highest", chord_auto.mono)
    check("和弦轨每个起音只留一个音", len(chord_auto.notes) == 6, len(chord_auto.notes))

    # 单调轨两种策略必须完全一致
    single = [smf.Note(pitch=60 + i, start=i * 500.0, end=i * 500.0 + 400.0)
              for i in range(6)]
    check("单声部轨两种策略结果一致",
          [n.pitch for n in mapping.converge_highest(single)]
          == [n.pitch for n in mapping.converge_legato(single)])


def main():
    print("三角洲口琴自动演奏器 · 自检")
    test_parse()
    test_parse_edge_cases()
    test_mapping()
    test_compile()
    test_playback()
    test_pick_and_transpose()
    test_segment_speed_audio()
    test_mono_mode()
    print(f"\n共 {CHECKS[0]} 项检查，失败 {len(FAILURES)} 项")
    if FAILURES:
        for item in FAILURES:
            print(f"  - {item}")
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
