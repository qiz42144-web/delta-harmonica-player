# -*- coding: utf-8 -*-
"""用 librosa 的 pYIN 做基频跟踪，再切成音符写成 MIDI。

比自带的 YIN 更适合声乐：pYIN 有概率化的浊音判决和更好的跳变抑制。

    python tools/pyin_transcribe.py <音频> -o out.mid
"""
from __future__ import annotations

import argparse
import collections
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import librosa

import vocal_to_midi as vtm

NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
MAJOR = [0, 2, 4, 5, 7, 9, 11]


def transcribe(path, fmin=80.0, fmax=520.0, min_note_ms=80.0, transpose=0):
    y, sr = librosa.load(path, sr=22050, mono=True)
    hop = 256
    f0, _voiced, prob = librosa.pyin(y, fmin=fmin, fmax=fmax, sr=sr,
                                     frame_length=2048, hop_length=hop,
                                     fill_na=0.0)
    hop_ms = hop / sr * 1000.0
    times = np.arange(len(f0)) * hop_ms

    midi = np.zeros(len(f0), dtype=np.float32)
    ok = (f0 > 0) & np.isfinite(f0) & (prob > 0.35)
    midi[ok] = vtm.hz_to_midi(f0[ok])

    # 中值滤波去毛刺
    idx = np.where(midi > 0)[0]
    if len(idx) >= 5:
        vals = midi[idx]
        padded = np.pad(vals, 2, mode="edge")
        midi[idx] = np.array([np.median(padded[i:i + 5]) for i in range(len(vals))])

    notes = vtm.segment(midi, times, hop_ms, min_note_ms)
    notes = [{"start": n["start"], "end": n["end"], "pitch": n["pitch"] + transpose}
             for n in notes]
    return notes, len(times), float(ok.mean())


def key_report(notes):
    hist = collections.Counter(n["pitch"] % 12 for n in notes)
    best = max(range(12), key=lambda t: sum(hist.get((t + i) % 12, 0) for i in MAJOR))
    ratio = sum(hist.get((best + i) % 12, 0) for i in MAJOR) / len(notes)
    return best, ratio, hist


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input")
    parser.add_argument("-o", "--output", default=None)
    parser.add_argument("--fmin", type=float, default=80.0)
    parser.add_argument("--fmax", type=float, default=520.0)
    parser.add_argument("--min-note-ms", type=float, default=80.0)
    parser.add_argument("--transpose", type=int, default=0)
    args = parser.parse_args()

    notes, frames, voiced = transcribe(args.input, args.fmin, args.fmax,
                                       args.min_note_ms, args.transpose)
    if not notes:
        print("没有检到音符")
        return 1

    pitches = [n["pitch"] for n in notes]
    dur = notes[-1]["end"] / 1000.0
    best, ratio, hist = key_report(notes)
    print(f"pYIN：{len(notes)} 个音 / 覆盖 {dur:.1f}s / 浊音帧 {voiced * 100:.0f}%")
    print(f"  音域 {min(pitches)}-{max(pitches)}  最佳调 1={NAMES[best]} 契合 {ratio * 100:.0f}%")
    print("  音高类: " + ' '.join(f'{NAMES[k]}:{v}' for k, v in
                                sorted(hist.items(), key=lambda x: -x[1])[:8]))

    out = args.output or os.path.splitext(args.input)[0] + "-pyin.mid"
    vtm.write_midi(notes, out)
    print(f"  已写出 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
