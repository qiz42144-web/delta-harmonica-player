# -*- coding: utf-8 -*-
"""《河边对口曲》（《黄河大合唱》选曲）简谱 -> MIDI（带变速）。

    python tools/hebian_score.py -o out.mid --preview out.wav --lyrics out.txt

谱源（两版互校）：
  A. 歌谱简谱网 jianpu.cn/pu/45/455627.htm（一）、455626.htm（二）
  B. 中国曲谱网 cnjpw.net/hechang/87983.html（书上扫描版，第 106 页，节奏更细）
  C. iyuepu.cn 的钢琴伴奏五线谱（确认甲/乙各 5 段词、结构为交替对唱）
音高两版一致；B 版把「兴奋段」第二、四小节写成附点节奏，这里照 B 版。

速度按书谱标注分四段：
  快速（主歌）→ 稍慢（在东北 / 这么说 两段）→ 兴奋 热烈地（仇和恨…）→ 结束句

原谱 1=D 2/4。这里写成 C 调（整体低 2 个半音，旋律不变），
让口琴尽量少按中键；想按原调吹，在播放器里手动移调 +2。
"""
from __future__ import annotations

import argparse
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PITCH = {"1": 60, "2": 62, "3": 64, "4": 65, "#4": 66, "5": 67, "6": 69, "7": 71,
         "i": 72, "2^": 74, "5,": 55}

# 甲：4 小节（主歌版，平均八分音符）
A = [("5", 0.75), ("#4", 0.25), ("5", 0.5), (None, 0.5),
     ("5", 0.5), ("5", 0.5), ("1", 0.5), (None, 0.5),
     ("i", 0.75), ("i", 0.25), ("6", 0.5), ("5", 0.5),
     ("4", 0.5), ("5", 0.25), ("4", 0.25), ("1", 0.5), (None, 0.5)]

# 甲：兴奋段版（书上第 2、4 小节是附点节奏）
A_FIERCE = [("5", 0.75), ("#4", 0.25), ("5", 0.5), (None, 0.5),
            ("5", 0.75), ("5", 0.25), ("1", 0.75), (None, 0.25),
            ("i", 0.75), ("i", 0.25), ("6", 0.5), ("5", 0.5),
            ("4", 0.5), ("5", 0.25), ("4", 0.25), ("1", 0.75), (None, 0.25)]

# 乙：4 小节
B = [("i", 0.75), ("i", 0.25), ("3", 0.5), (None, 0.5),
     ("2", 0.25), ("3", 0.25), ("1", 0.25), ("2", 0.25), ("3", 0.5), (None, 0.5),
     ("5", 0.75), ("6", 0.25), ("3", 0.5), ("2", 0.5),
     ("2", 0.25), ("3", 0.25), ("2", 0.25), ("1", 0.25), ("5,", 0.5), (None, 0.5)]

IL_A = [("i", 0.75), ("i", 0.25), ("5", 0.5), ("6", 0.25), ("5", 0.25),
        ("4", 0.5), ("5", 0.5), ("1", 0.5), (None, 0.5)]

IL_B = [("5", 0.75), ("6", 0.25), ("3", 0.5), ("2", 0.5),
        ("2", 0.25), ("3", 0.25), ("2", 0.25), ("1", 0.25), ("5,", 0.5), (None, 0.5)]

ENDING = [("i", 0.75), ("i", 0.25), ("6", 0.5), ("i", 0.5),
          ("2^", 1.0), ("2^", 1.0),
          ("i", 2.0),
          ("i", 2.0),
          ("i", 1.0), (None, 1.0)]

COUPLETS = [
    ("甲", "张老三，我问你，你的家乡在哪里"),
    ("乙", "我的家，在山西，过河还有三百里"),
    ("甲", "我问你，在家里，种田还是做生意"),
    ("乙", "拿锄头，耕田地，种的高粱和小米"),
    ("甲", "为什么，到此地，河边流浪受孤凄"),
    ("乙", "痛心事，莫提起，家破人亡无消息"),
    ("甲", "张老三，莫伤悲，我的命运不如你"),
    ("乙", "为什么，王老七，你的家乡在哪里"),
    ("甲", "在东北，做生意，家乡八年无消息"),
    ("乙", "这么说，我和你，都是有家不能回"),
]

FIERCE = [
    ("合", "仇和恨，在心里，奔腾如同黄河水"),
    ("合", "黄河边，定主意，咱们一同打回去"),
    ("合", "为国家，当兵去，太行山上打游击"),
    ("合", "从今后，我和你，一同打回老家去"),
]

SLOW_FROM = 8          # 第 9 组「在东北」开始稍慢（书谱标注）


def build(intro=False, fierce="loop4"):
    """按结构铺满。返回 (beats 列表, 段落表, 变速点)。

    音符用「拍」计量（1 拍 = 1 个四分音符），速度交给 tempo map，
    这样同一份拍位可以在不同版本里用不同速度演奏。
    """
    notes = []          # (start_beat, end_beat, pitch)
    sections = []       # (label, text, start_beat, end_beat, tempo_tag)
    tempos = []         # (beat, tag)
    cursor = 0.0

    def emit(sequence, label, text, tag):
        nonlocal cursor
        start = cursor
        tempos.append((cursor, tag))
        for name, beats in sequence:
            if name is not None:
                notes.append((cursor, cursor + beats, PITCH[name]))
            cursor += beats
        sections.append((label, text, start, cursor, tag))

    if intro:
        emit(A + IL_A, "前奏", "（器乐，与甲的第一句同旋律）", "fast")

    for index, (kind, text) in enumerate(COUPLETS):
        tag = "slow" if index >= SLOW_FROM else "fast"
        emit(A + IL_A if kind == "甲" else B + IL_B, kind, text, tag)

    if fierce == "loop4":
        for kind, text in FIERCE:
            emit(A_FIERCE, "合", text, "fierce")
    elif fierce == "alternate":
        for index, (kind, text) in enumerate(FIERCE):
            emit(A_FIERCE if index % 2 == 0 else B,
                 "甲" if index % 2 == 0 else "乙", text, "fierce")
    elif fierce == "once":
        emit(A_FIERCE, "合", FIERCE[0][1], "fierce")

    emit(ENDING, "结束句", "一同打回老家去", "end")
    return notes, sections, tempos


def beats_to_ms(beat, tempo_points):
    """按 tempo map 把拍位换算成毫秒。tempo_points = [(beat, bpm), ...]"""
    ms = 0.0
    prev_beat = 0.0
    current = tempo_points[0][1]
    for point_beat, bpm in tempo_points:
        if point_beat <= prev_beat:
            current = bpm
            continue
        if point_beat >= beat:
            break
        ms += (point_beat - prev_beat) * 60000.0 / current
        prev_beat = point_beat
        current = bpm
    ms += (beat - prev_beat) * 60000.0 / current
    return ms


def write_midi(notes, tempo_points, path, division=480, name="河边对口曲"):
    """写带变速的 MIDI（format 0）。"""
    events = []
    for start_beat, end_beat, pitch in notes:
        on = int(round(start_beat * division))
        off = max(on + 1, int(round(end_beat * division)))
        events.append((on, 1, bytes([0x90, pitch, 96])))
        events.append((off, 0, bytes([0x80, pitch, 0])))
    for beat, bpm in tempo_points:
        tick = int(round(beat * division))
        usec = int(round(60000000.0 / bpm))
        events.append((tick, -1, b"\xFF\x51\x03" + usec.to_bytes(3, "big")))
    events.sort(key=lambda item: (item[0], item[1]))

    def varlen(value):
        out = bytearray([value & 0x7F])
        value >>= 7
        while value:
            out.insert(0, (value & 0x7F) | 0x80)
            value >>= 7
        return bytes(out)

    body = bytearray()
    raw = name.encode("utf-8")
    body += varlen(0) + b"\xFF\x03" + varlen(len(raw)) + raw
    last = 0
    for tick, _order, payload in events:
        body += varlen(tick - last) + payload
        last = tick
    body += varlen(0) + b"\xFF\x2F\x00"

    header = b"MThd" + struct.pack(">IHHH", 6, 0, 1, division)
    track = b"MTrk" + struct.pack(">I", len(body)) + bytes(body)
    with open(path, "wb") as fh:
        fh.write(header + track)


def main():
    parser = argparse.ArgumentParser(description="河边对口曲 简谱 -> MIDI（带变速）")
    parser.add_argument("--bpm", type=float, default=112.0, help="主歌「快速」")
    parser.add_argument("--bpm-slow", type=float, default=88.0, help="「稍慢」段")
    parser.add_argument("--bpm-fierce", type=float, default=132.0, help="「兴奋热烈地」")
    parser.add_argument("--bpm-end", type=float, default=116.0, help="结束句")
    parser.add_argument("--transpose", type=int, default=0)
    parser.add_argument("-o", "--output", default=None)
    parser.add_argument("--preview", default=None)
    parser.add_argument("--lyrics", default=None)
    parser.add_argument("--intro", action="store_true")
    parser.add_argument("--fierce", choices=["loop4", "alternate", "once", "none"],
                        default="loop4")
    parser.add_argument("--wav-max", type=float, default=300.0)
    args = parser.parse_args()

    notes, sections, tempo_tags = build(args.intro, args.fierce)
    bpm_of = {"fast": args.bpm, "slow": args.bpm_slow,
              "fierce": args.bpm_fierce, "end": args.bpm_end}
    tempo_points = [(beat, bpm_of[tag]) for beat, tag in tempo_tags]
    # 合并相邻同速的点
    merged = []
    for beat, bpm in tempo_points:
        if merged and abs(merged[-1][1] - bpm) < 1e-9 and abs(merged[-1][0] - beat) < 1e-9:
            continue
        if merged and abs(merged[-1][1] - bpm) < 1e-9:
            continue
        merged.append((beat, bpm))
    tempo_points = merged

    total_beats = max(sec[3] for sec in sections)
    total_ms = beats_to_ms(total_beats, tempo_points)
    shifted = [(s, e, p + args.transpose) for s, e, p in notes]
    pitches = [p for _s, _e, p in shifted]

    print(f"《河边对口曲》  1=C  2/4")
    print(f"  速度：快速 {args.bpm:g} → 稍慢 {args.bpm_slow:g} → "
          f"兴奋 {args.bpm_fierce:g} → 结束 {args.bpm_end:g} BPM")
    print(f"  {len(sections)} 段 / {total_beats:.0f} 拍 / {len(notes)} 个音 / "
          f"{total_ms / 1000:.1f}s  音域 {min(pitches)}-{max(pitches)}")

    out = args.output or os.path.join(ROOT, "河边对口曲.mid")
    write_midi(shifted, tempo_points, out)
    print(f"  MIDI：{out}")

    if args.lyrics:
        lines = ["《河边对口曲》 变速版（快速→稍慢→兴奋热烈→结束）  1=C（原谱 1=D）", ""]
        lines.append(f"{'时间':>12}  {'速度':<6}{'段':<4}{'歌词'}")
        lines.append("-" * 64)
        for label, text, start_beat, end_beat, tag in sections:
            start = beats_to_ms(start_beat, tempo_points) / 1000.0
            end = beats_to_ms(end_beat, tempo_points) / 1000.0
            lines.append(f"{start:6.1f}-{end:6.1f}s  "
                         f"{bpm_of[tag]:>4.0f}BPM  {label:<4}{text}")
        with open(args.lyrics, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        print(f"  歌词表：{args.lyrics}")

    if args.preview:
        sys.path.insert(0, ROOT)
        import mapping
        import synth
        spans = []
        for start_beat, end_beat, pitch in shifted:
            spans.append(mapping.Span(beats_to_ms(start_beat, tempo_points),
                                      beats_to_ms(end_beat, tempo_points),
                                      pitch, mapping.Candidate(0, 0, 0)))
        data = synth.build_wav(spans, max_seconds=args.wav_max)
        with open(args.preview, "wb") as fh:
            fh.write(data)
        print(f"  试听：{args.preview}（{synth.wav_duration_ms(data) / 1000:.1f}s）")

    sys.path.insert(0, ROOT)
    import mapping
    import smf
    track = smf.Track(index=0, name="主旋律",
                      notes=[smf.Note(pitch=p,
                                      start=beats_to_ms(s, tempo_points),
                                      end=beats_to_ms(e, tempo_points))
                             for s, e, p in shifted])
    info = mapping.analyse_track(track)
    if info:
        print(f"  口琴可吹 {info['playable']}/{len(notes)}  吃音 {info['lost']}  "
              f"折叠 {info['folded']}  移调 {info['global_shift']:+d}  "
              f"中键半音 {info['sharps']} 个")
    return 0


if __name__ == "__main__":
    sys.exit(main())
