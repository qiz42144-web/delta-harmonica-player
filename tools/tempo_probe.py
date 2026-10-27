# -*- coding: utf-8 -*-
"""测量一首歌的速度：节拍 BPM + 人声实际吐字速率。

    python tools/tempo_probe.py "歌曲.mp3"

输出两项，用来给 score_to_midi.py 定 --bpm / --note：
  * BPM：对「频谱通量」做自相关，取 40~200 BPM 范围内的主峰
  * 吐字速率：YIN 跟踪出人声音符后，统计相邻音符起点间隔的中位数
     —— 这就是「一个字大概占多少毫秒」，也就能反推该用几分音符
"""
from __future__ import annotations

import os
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import vocal_to_midi as vtm


def spectral_flux(x, sr, hop=256, frame=1024):
    frames = 1 + max(0, (len(x) - frame) // hop)
    window = np.hanning(frame).astype(np.float32)
    prev = None
    flux = np.zeros(frames, dtype=np.float32)
    for i in range(frames):
        seg = x[i * hop:i * hop + frame] * window
        mag = np.abs(np.fft.rfft(seg))
        if prev is not None:
            diff = mag - prev
            flux[i] = float(np.sum(diff[diff > 0]))
        prev = mag
    if flux.max() > 0:
        flux /= flux.max()
    fps = sr / hop
    return flux, fps


def estimate_bpm(flux, fps, low=40.0, high=200.0, top=4):
    """自相关找节拍周期。"""
    signal = flux - flux.mean()
    ac = np.correlate(signal, signal, mode="full")[len(signal) - 1:]
    ac[:int(fps * 60 / high)] = 0.0
    lo = int(fps * 60 / high)
    hi = min(len(ac) - 1, int(fps * 60 / low))
    if hi <= lo:
        return []
    segment = ac[lo:hi]
    order = np.argsort(segment)[::-1]
    peaks = []
    for idx in order:
        lag = lo + int(idx)
        if lag <= 0:
            continue
        bpm = 60.0 * fps / lag
        if any(abs(bpm - p[0]) < 3.0 for p in peaks):
            continue
        peaks.append((bpm, float(segment[idx])))
        if len(peaks) >= top:
            break
    total = float(segment.max()) or 1.0
    return [(round(bpm, 1), round(score / total, 3)) for bpm, score in peaks]


def main():
    path = sys.argv[1]
    tmp = os.path.join(tempfile.gettempdir(), "tempo-probe.wav")
    audio, sr = vtm.decode(path, tmp)
    print(f"音频 {len(audio) / sr:.1f}s  {sr}Hz")

    flux, fps = spectral_flux(audio, sr)
    print("\n--- 节拍（自相关主峰）---")
    for bpm, score in estimate_bpm(flux, fps):
        print(f"  {bpm:6.1f} BPM   置信 {score:.2f}")

    print("\n--- 人声吐字速率 ---")
    hp = vtm.highpass(audio, sr, vtm.HIGHPASS_HZ)
    times, f0, conf, rms = vtm.yin_track(hp, sr)
    hop_ms = vtm.HOP / sr * 1000.0
    midi = vtm.clean_pitch(f0, conf, rms)
    notes = vtm.segment(midi, times, hop_ms, 80.0)
    if notes:
        starts = [n["start"] for n in notes]
        gaps = np.diff(starts)
        gaps = gaps[gaps < 1500]
        durs = [n["end"] - n["start"] for n in notes]
        print(f"  检出人声音符 {len(notes)} 个，覆盖 "
              f"{sum(durs) / 1000:.1f}s")
        print(f"  起音间隔 中位 {np.median(gaps):.0f}ms  "
              f"均值 {gaps.mean():.0f}ms  最小 {gaps.min():.0f}ms")
        print(f"  音符时值 中位 {np.median(durs):.0f}ms")

        print("\n--- 换算成简谱节奏（一个字一个音）---")
        target = float(np.median(gaps))
        for bpm in (84, 90, 96, 100, 104, 110):
            for value, name in ((4, "四分"), (8, "八分"), (16, "十六分")):
                ms = 60000.0 / bpm * 4 / value
                mark = "  ← 最接近" if abs(ms - target) < 25 else ""
                print(f"  {bpm} BPM + {name}音符 = {ms:5.0f}ms/字{mark}")

    os.remove(tmp)
    return 0


if __name__ == "__main__":
    sys.exit(main())
