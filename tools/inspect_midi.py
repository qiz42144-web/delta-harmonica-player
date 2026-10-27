# -*- coding: utf-8 -*-
"""看一眼 MIDI 里到底是什么：前 N 个音、简谱表示、按时间段的密度。

    python tools/inspect_midi.py "文件.mid" [--limit 40] [--bucket 20]
"""
from __future__ import annotations

import argparse
import collections
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import smf

NAMES = ["1", "#1", "2", "#2", "3", "4", "#4", "5", "#5", "6", "#6", "7"]


def jianpu(pitch):
    body = NAMES[pitch % 12]
    if pitch >= 72:
        return body + "'"
    if pitch < 60:
        return body + ","
    return body + " "


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("path")
    parser.add_argument("--limit", type=int, default=40)
    parser.add_argument("--bucket", type=float, default=20.0, help="统计窗口（秒）")
    args = parser.parse_args()

    midi = smf.load(args.path)
    notes = [n for t in midi.tracks for n in t.notes]
    notes.sort(key=lambda n: n.start)
    if not notes:
        print("没有音符")
        return 1

    print(f"文件：{os.path.basename(args.path)}")
    print(f"轨道 {midi.track_count}  音符 {len(notes)}  总长 {midi.length_ms / 1000:.1f}s  "
          f"音域 {min(n.pitch for n in notes)}-{max(n.pitch for n in notes)}")

    print(f"\n--- 前 {args.limit} 个音 ---")
    for note in notes[:args.limit]:
        print(f"  {note.start / 1000:7.2f}s  MIDI {note.pitch:3d}  "
              f"{jianpu(note.pitch)}  {note.duration:5.0f}ms")

    bucket_ms = args.bucket * 1000
    counts = collections.Counter(int(n.start / bucket_ms) for n in notes)
    total = int(midi.length_ms / bucket_ms) + 1
    print(f"\n--- 每 {args.bucket:.0f}s 的音符数 ---")
    for i in range(total):
        bar = "#" * min(60, counts.get(i, 0))
        print(f"  {i * args.bucket:6.0f}s  {counts.get(i, 0):3d} {bar}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
