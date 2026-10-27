# -*- coding: utf-8 -*-
"""生成测试用 MIDI 文件（不依赖任何第三方库）。

    python demo_midi.py            # 生成 demo/小星星.mid
"""
from __future__ import annotations

import os
import struct
import sys

try:  # Windows 控制台默认不是 UTF-8，中文输出会抛 UnicodeEncodeError
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

DIVISION = 480


def _varlen(value: int) -> bytes:
    if value < 0:
        raise ValueError("delta 不能为负")
    out = bytearray([value & 0x7F])
    value >>= 7
    while value:
        out.insert(0, (value & 0x7F) | 0x80)
        value >>= 7
    return bytes(out)


def _track(events) -> bytes:
    """events: [(tick, bytes)]，tick 为绝对 tick。"""
    body = bytearray()
    last = 0
    for tick, payload in events:
        body += _varlen(tick - last)
        body += payload
        last = tick
    body += _varlen(0) + b"\xFF\x2F\x00"          # End of Track
    return b"MTrk" + struct.pack(">I", len(body)) + bytes(body)


def _tempo(usec_per_qn: int) -> bytes:
    return b"\xFF\x51\x03" + struct.pack(">I", usec_per_qn)[1:]


def _track_name(text: str) -> bytes:
    raw = text.encode("utf-8")
    return b"\xFF\x03" + _varlen(len(raw)) + raw


def _note_on(channel: int, pitch: int, velocity: int = 96) -> bytes:
    return bytes([0x90 | channel, pitch, velocity])


def _note_off(channel: int, pitch: int) -> bytes:
    return bytes([0x80 | channel, pitch, 0])


def _melody_events(notes, channel: int = 0, start_tick: int = 0):
    """notes: [(pitch, 起拍, 时值拍)]"""
    events = []
    for pitch, beat, length in notes:
        on = start_tick + int(beat * DIVISION)
        off = on + int(length * DIVISION)
        events.append((on, _note_on(channel, pitch)))
        events.append((off, _note_off(channel, pitch)))
    events.sort(key=lambda item: (item[0], item[1][0]))
    return events


# 小星星（C 大调），含一个超出音域的测试音
TWINKLE = [
    (60, 0, 1), (60, 1, 1), (67, 2, 1), (67, 3, 1),
    (69, 4, 1), (69, 5, 1), (67, 6, 2),
    (65, 8, 1), (65, 9, 1), (64, 10, 1), (64, 11, 1),
    (62, 12, 1), (62, 13, 1), (60, 14, 2),
    (67, 16, 1), (67, 17, 1), (65, 18, 1), (65, 19, 1),
    (64, 20, 1), (64, 21, 1), (62, 22, 2),
    (67, 24, 1), (67, 25, 1), (65, 26, 1), (65, 27, 1),
    (64, 28, 1), (64, 29, 1), (62, 30, 2),
    (60, 32, 1), (60, 33, 1), (67, 34, 1), (67, 35, 1),
    (69, 36, 1), (69, 37, 1), (67, 38, 2),
    (65, 40, 1), (65, 41, 1), (64, 42, 1), (64, 43, 1),
    (62, 44, 1), (62, 45, 1), (60, 46, 2),
]

# 故意混入：超低音、超高音、半音、极短音，用来验证折叠与最短音处理
EDGE_CASES = [
    (36, 48, 1),      # 远低于音域，必须折叠
    (96, 49, 1),      # 远高于音域，必须折叠
    (61, 50, 1),      # 半音（需要中键修饰）
    (66, 51, 0.05),   # 极短音（25ms，应被跳过）
    (60, 51.25, 0.75),
]


def build_demo(with_edge_cases: bool = True) -> bytes:
    melody = list(TWINKLE) + (EDGE_CASES if with_edge_cases else [])
    melody_events = _melody_events(melody, channel=0)
    melody_events.insert(0, (0, _track_name("Melody 主旋律")))
    melody_events.insert(0, (0, _tempo(500000)))

    # 打击乐轨（通道 10），验证会被识别并跳过
    drums = [(36, i, 0.5) for i in range(0, 32, 2)] + [(38, i + 1, 0.5) for i in range(0, 32, 2)]
    drum_events = _melody_events(drums, channel=9)
    drum_events.insert(0, (0, _track_name("Drums 打击乐")))

    # 和弦/伴奏轨，验证「单音收敛取最高音」
    chord_events = []
    for beat in range(0, 32, 4):
        for pitch in (48, 55, 64):
            on = beat * DIVISION
            off = on + 2 * DIVISION
            chord_events.append((on, _note_on(1, pitch, 60)))
            chord_events.append((off, _note_off(1, pitch)))
    chord_events.sort(key=lambda item: (item[0], item[1][0]))
    chord_events.insert(0, (0, _track_name("Chords 伴奏")))

    header = b"MThd" + struct.pack(">IHHH", 6, 1, 3, DIVISION)
    return header + _track(melody_events) + _track(drum_events) + _track(chord_events)


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    out_dir = os.path.join(here, "demo")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "小星星.mid")
    with open(path, "wb") as fh:
        fh.write(build_demo())
    print(f"已生成：{path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
