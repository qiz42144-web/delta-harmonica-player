# -*- coding: utf-8 -*-
"""把数字简谱（txt）转成 MIDI。只用标准库。

    python tools/score_to_midi.py "躺在晴空下.txt" -o "躺在晴空下-简谱.mid"

简谱格式约定（本项目用的这份）：
    1=C            调号，1 对应 C
    数字 1-7       音级
    ( )            低音区（降一个八度）
    [ ]            高音区（升一个八度）
    *              该音实际是升号（谱面已按「就近降为自然音」简化显示）
    |              换气点
    →  后面         歌词

节奏：简谱只给了音高没给时值，这里按「一字一音」给出规整节奏
      —— 每字一个八分音符，句尾延长为四分音符，换气处留八分休止，段落间留两拍。
      可以用 --bpm / --note 调整。要还原演唱的自由节奏请改用 tools/vocal_to_midi.py。
"""
from __future__ import annotations

import argparse
import os
import re
import struct
import sys

try:  # Windows 控制台默认不是 UTF-8，中文输出会抛 UnicodeEncodeError
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

DEGREE = {"1": 0, "2": 2, "3": 4, "4": 5, "5": 7, "6": 9, "7": 11}
# 支持 #5 / [#1] / (6)(#5) 这种带升号的写法，也兼容旧的 5* 写法
TOKEN_RE = re.compile(r"([\(\[]?)\s*(#?)\s*([1-7])\s*([\)\]]?)\s*(\*?)")
PURE_LINE_RE = re.compile(r"^[1-7\(\)\[\]#\s\|\*]+$")


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #
def parse_notes(text: str):
    """从一行里取出音符，按 | 分成若干段。返回 [[{pitch, sharp}, ...], ...]"""
    body = text.split("→")[0]
    segments = []
    for chunk in body.split("|"):
        notes = []
        for match in TOKEN_RE.finditer(chunk):
            open_b, sharp_mark, digit, close_b, star = match.groups()
            octave = -1 if open_b == "(" else (1 if open_b == "[" else 0)
            sharp = bool(sharp_mark) or bool(star)
            pitch = 60 + 12 * octave + DEGREE[digit] + (1 if sharp else 0)
            notes.append({"pitch": pitch, "sharp": sharp})
        if notes:
            segments.append(notes)
    return segments


def is_phrase_line(line: str):
    if "→" in line:
        return True
    stripped = line.strip()
    if not stripped or not PURE_LINE_RE.match(stripped):
        return False
    return len(TOKEN_RE.findall(stripped)) >= 3


def parse_score(text: str):
    """返回 [{"title": 段落名, "lines": [[segment, ...], ...]}, ...]"""
    sections = [{"title": "", "lines": []}]
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("【") and line.endswith("】"):
            sections.append({"title": line.strip("【】"), "lines": []})
            continue
        if not is_phrase_line(line):
            continue
        segments = parse_notes(line)
        if segments:
            sections[-1]["lines"].append(segments)
    return [s for s in sections if s["lines"]]


# --------------------------------------------------------------------------- #
# 生成音符时间线
# --------------------------------------------------------------------------- #
def build_notes(sections, bpm=90.0, note_value=8, phrase_end_quarter=True,
                simplified=False, phrase_gap=0.25, section_gap=0.25,
                articulation=0.6):
    """按规整节奏铺音符。返回 [(start_ms, end_ms, pitch)]。

    phrase_gap / section_gap 的单位是「拍」：0.25 拍 = 120 tick ≈ 167ms（90BPM 下）。
    articulation = 发声时长 / 时值。1.0 是连奏（音与音粘着），
                   0.6 则每个字只发 60% 的时值、留 40% 空隙 —— 更接近真人吐字。
    """
    beat_ticks = 480
    unit = beat_ticks * 4 // note_value            # 一个音符的 tick 数
    breath = max(0, int(beat_ticks * phrase_gap))  # 换气休止
    line_gap = breath
    section_pause = max(0, int(beat_ticks * section_gap))
    ms_per_tick = 60000.0 / bpm / beat_ticks
    articulation = min(1.0, max(0.1, articulation))

    notes = []
    tick = 0
    for section in sections:
        lines = section["lines"]
        for line_index, segments in enumerate(lines):
            last_segment = len(segments) - 1
            total_notes = sum(len(seg) for seg in segments)
            index = 0
            for seg_index, segment in enumerate(segments):
                for note in segment:
                    index += 1
                    is_final = (seg_index == last_segment and index == total_notes)
                    dur = beat_ticks if (is_final and phrase_end_quarter) else unit
                    sound = max(1, int(round(dur * articulation)))
                    pitch = note["pitch"]
                    if simplified and note["sharp"]:
                        pitch -= 1
                    start = tick * ms_per_tick
                    end = (tick + sound) * ms_per_tick
                    notes.append((start, end, pitch))
                    tick += dur
                if seg_index != last_segment:
                    tick += breath
            # 句间停顿只在同一段内加；段与段之间用 section_pause，不叠加
            if line_index != len(lines) - 1:
                tick += line_gap
        tick += section_pause
    return notes


# --------------------------------------------------------------------------- #
# 写 MIDI（标准库，format 0）
# --------------------------------------------------------------------------- #
def _varlen(value: int) -> bytes:
    out = bytearray([value & 0x7F])
    value >>= 7
    while value:
        out.insert(0, (value & 0x7F) | 0x80)
        value >>= 7
    return bytes(out)


def write_midi(notes, path, bpm=90.0, name="Score"):
    division = 480
    ms_per_tick = 60000.0 / bpm / division
    tempo_us = int(round(60000000.0 / bpm))       # 必须和换算用的 bpm 一致

    events = []
    for start, end, pitch in notes:
        on = int(round(start / ms_per_tick))
        off = max(on + 1, int(round(end / ms_per_tick)))
        events.append((on, 1, bytes([0x90, pitch, 96])))
        events.append((off, 0, bytes([0x80, pitch, 0])))
    events.sort(key=lambda item: (item[0], item[1]))

    body = bytearray()
    body += _varlen(0) + b"\xFF\x51\x03" + tempo_us.to_bytes(3, "big")
    raw_name = name.encode("utf-8")
    body += _varlen(0) + b"\xFF\x03" + _varlen(len(raw_name)) + raw_name

    last = 0
    for tick, _order, payload in events:
        body += _varlen(tick - last) + payload
        last = tick
    body += _varlen(0) + b"\xFF\x2F\x00"

    header = b"MThd" + struct.pack(">IHHH", 6, 0, 1, division)
    track = b"MTrk" + struct.pack(">I", len(body)) + bytes(body)
    with open(path, "wb") as fh:
        fh.write(header + track)
    return len(notes)


# --------------------------------------------------------------------------- #
def main():
    parser = argparse.ArgumentParser(description="数字简谱 txt -> MIDI")
    parser.add_argument("input", help="简谱 txt")
    parser.add_argument("-o", "--output", default=None)
    parser.add_argument("--bpm", type=float, default=90.0)
    parser.add_argument("--note", type=int, default=8, help="每字音符时值：4=四分, 8=八分")
    parser.add_argument("--phrase-gap", type=float, default=0.25,
                        help="句间停顿，单位「拍」（默认 0.25 拍 ≈ 167ms）")
    parser.add_argument("--section-gap", type=float, default=0.25,
                        help="段落间停顿，单位「拍」（默认 0.25 拍 ≈ 177ms，0=连起来）")
    parser.add_argument("--articulation", type=float, default=0.6,
                        help="发声占比 0.1~1.0：1.0=连奏，0.6=每字发 60%% 时值（更接近唱）")
    parser.add_argument("--bpm-real", action="store_true",
                        help="按原曲实测速度（85BPM / 八分音符）出曲")
    parser.add_argument("--simplified", action="store_true",
                        help="忽略 * 标记（全用自然音），默认按原谱还原升号")
    parser.add_argument("--preview", default=None, help="顺便导出试听 WAV")
    args = parser.parse_args()

    text = open(args.input, encoding="utf-8-sig").read()
    sections = parse_score(text)
    if not sections:
        print("没解析出任何乐句，请检查简谱格式（需要「数字 + →歌词」或纯音符行）")
        return 1

    total_lines = sum(len(s["lines"]) for s in sections)
    total_notes = sum(len(seg) for s in sections for line in s["lines"] for seg in line)
    print(f"解析到 {len(sections)} 个段落 / {total_lines} 个乐句 / {total_notes} 个音符")
    for section in sections:
        count = sum(len(seg) for line in section["lines"] for seg in line)
        print(f"  · {section['title'] or '(无标题)'}：{len(section['lines'])} 句，{count} 音")

    if args.bpm_real:
        args.bpm = 85.0          # 对这首歌实测：85 BPM + 八分音符 ≈ 353ms/字（原曲 372ms）
        args.note = 8
    notes = build_notes(sections, bpm=args.bpm, note_value=args.note,
                        simplified=args.simplified,
                        phrase_gap=args.phrase_gap,
                        section_gap=args.section_gap,
                        articulation=args.articulation)
    pitches = [n[2] for n in notes]
    sharps = sum(1 for s in sections for line in s["lines"] for seg in line
                 for n in seg if n["sharp"])
    duration = notes[-1][1] / 1000.0
    print(f"\n生成 {len(notes)} 个音  音域 {min(pitches)}-{max(pitches)}  "
          f"时长 {duration:.1f}s  {'（已忽略 * 升号）' if args.simplified else f'升号 {sharps} 个'}"
          f"  BPM {args.bpm:g}")

    out = args.output or os.path.splitext(args.input)[0] + "-简谱.mid"
    write_midi(notes, out, bpm=args.bpm,
               name="躺在晴空下（简谱版）" if "晴空" in os.path.basename(args.input) else "Score")
    print(f"已写出：{out}")

    # 用本项目的合成器出一份试听
    if args.preview:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        import mapping
        import synth
        spans = [mapping.Span(s, e, p, mapping.Candidate(0, 0, 0)) for s, e, p in notes]
        data = synth.build_wav(spans, max_seconds=600.0)
        with open(args.preview, "wb") as fh:
            fh.write(data)
        print(f"试听：{args.preview}（{synth.wav_duration_ms(data) / 1000:.1f}s）")

    # 口琴可吹性（用真实的 smf 对象，映射链路才走得通）
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import mapping
    import smf
    track_notes = [smf.Note(pitch=p, start=s, end=e) for s, e, p in notes]
    track = smf.Track(index=0, name="", notes=track_notes)
    info = mapping.analyse_track(track)
    if info:
        print(f"口琴可吹：{info['playable']} / {len(notes)}   吃音 {info['lost']}   "
              f"折叠 {info['folded']}   整轨移调 {info['global_shift']:+d}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
