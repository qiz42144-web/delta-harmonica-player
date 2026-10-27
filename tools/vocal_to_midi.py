# -*- coding: utf-8 -*-
"""把歌曲里的人声旋律转成 MIDI（单声部）。

    python tools/vocal_to_midi.py "输入.mp3" -o "输出.mid"

流程：
  ffmpeg 解码 -> 单声道 22050Hz
  YIN 基频(F0)跟踪 -> 能量/置信度过滤 -> 中值滤波 + 八度纠错
  连续帧合并成音符 -> 去掉过短的音 / 合并微小断口
  按精确毫秒写 MIDI（不做量化，保留演唱的呼吸和自由节奏）

为什么不做量化：这首是翻唱人声，节拍本身有 rubato；
本项目的播放器用毫秒时间轴，MIDI 里按 480 PPQ/120BPM 编码，1 tick ≈ 1.04ms，
既保留了真实演唱节奏，又不损失精度。需要对齐网格可以加 --quantize 16。

注意：没有做人声分离，伴奏会干扰音高检测。所以脚本会输出「浊音率 / 音高稳定性」
等指标，请结合试听判断；需要更干净的话应先用 Demucs 之类的模型分轨。
"""
from __future__ import annotations

import argparse
import os
import struct
import subprocess
import sys
import tempfile
import wave

try:  # Windows 控制台默认不是 UTF-8，中文输出会抛 UnicodeEncodeError
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import numpy as np

HOP = 256                 # 11.6ms @22050
FRAME = 2048
SR = 22050
FMIN = 150.0              # 女声下限附近；再低就基本是贝斯/底鼓了
FMAX = 850.0
YIN_THRESHOLD = 0.30
MIN_NOTE_MS = 80.0
MERGE_GAP_MS = 55.0
HIGHPASS_HZ = 150.0


# --------------------------------------------------------------------------- #
# 解码
# --------------------------------------------------------------------------- #
def decode(mp3_path: str, wav_path: str, target_sr: int = SR):
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", mp3_path,
           "-ac", "1", "-ar", str(target_sr), "-acodec", "pcm_s16le", wav_path]
    subprocess.run(cmd, check=True)
    with wave.open(wav_path, "rb") as reader:
        rate = reader.getframerate()
        data = reader.readframes(reader.getnframes())
    pcm = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
    return pcm, rate


def highpass(x: np.ndarray, sr: int, cutoff: float, order: int = 2) -> np.ndarray:
    """二阶巴特沃斯高通（频域实现，比逐样本递推快两个数量级）。

    目的：把贝斯/底鼓压下去，减少低音把人声基频带跑偏。
    """
    if cutoff <= 0 or len(x) == 0:
        return x
    spec = np.fft.rfft(x.astype(np.float64))
    freqs = np.fft.rfftfreq(len(x), 1.0 / sr)
    ratio = np.maximum(freqs, 1e-6) / cutoff
    if order == 2:
        gain = ratio ** 2 / np.sqrt(1.0 + ratio ** 4)
    else:
        gain = ratio / np.sqrt(1.0 + ratio ** 2)
    gain[freqs < 1e-6] = 0.0
    return np.fft.irfft(spec * gain, len(x)).astype(np.float32)


# --------------------------------------------------------------------------- #
# YIN 基频跟踪
# --------------------------------------------------------------------------- #
def yin_track(x: np.ndarray, sr: int, fmin=FMIN, fmax=FMAX, hop=HOP, frame=FRAME):
    tau_min = max(2, int(sr / fmax))
    tau_max = min(frame - 2, int(sr / fmin))
    frames = 1 + max(0, (len(x) - frame) // hop)
    times = np.empty(frames, dtype=np.float32)
    f0 = np.zeros(frames, dtype=np.float32)
    conf = np.ones(frames, dtype=np.float32)
    rms = np.zeros(frames, dtype=np.float32)

    window = np.hanning(frame).astype(np.float32)
    size = 1 << int(np.ceil(np.log2(2 * frame)))
    taus = np.arange(tau_max + 1)

    for index in range(frames):
        start = index * hop
        chunk = x[start:start + frame]
        times[index] = (start + frame / 2) / sr * 1000.0
        rms[index] = float(np.sqrt(np.mean(chunk ** 2)))

        seg = (chunk - chunk.mean()) * window
        spec = np.fft.rfft(seg, size)
        acf = np.fft.irfft(spec * np.conj(spec), size)[:frame]

        cs = np.concatenate(([0.0], np.cumsum(seg * seg)))
        width = frame - taus
        e0 = cs[width]
        e1 = cs[-1] - cs[taus]
        diff = np.maximum(e0 + e1 - 2.0 * acf[:len(taus)], 0.0)
        diff[0] = 0.0

        cum = np.cumsum(diff[1:])
        cmndf = np.ones(len(taus), dtype=np.float32)
        cmndf[1:] = diff[1:] * np.arange(1, len(taus)) / np.maximum(cum, 1e-9)

        # 找第一个低于阈值的局部极小
        best = -1
        for t in range(tau_min, tau_max):
            if cmndf[t] < YIN_THRESHOLD and cmndf[t] <= cmndf[t + 1]:
                best = t
                break
        if best < 0:
            # 退化：取全局最小
            region = cmndf[tau_min:tau_max]
            best = int(tau_min + np.argmin(region))
            conf[index] = 0.0
        else:
            # 抛物线插值提高精度
            a, b, c = cmndf[best - 1], cmndf[best], cmndf[best + 1]
            denom = (a - 2 * b + c)
            shift = 0.5 * (a - c) / denom if abs(denom) > 1e-9 else 0.0
            best = best + shift
            conf[index] = 1.0 - float(b)

        if best > 0:
            f0[index] = sr / best
            if f0[index] < fmin or f0[index] > fmax:
                f0[index] = 0.0
                conf[index] = 0.0
    return times, f0, conf, rms


def hz_to_midi(f):
    return 69.0 + 12.0 * np.log2(np.maximum(f, 1e-6) / 440.0)


# --------------------------------------------------------------------------- #
# 后处理
# --------------------------------------------------------------------------- #
def clean_pitch(f0, conf, rms):
    """能量门 + 置信度门 + 中值滤波 + 八度纠错，返回 MIDI 浮点音高（0 表示无音）。"""
    midi = np.zeros(len(f0), dtype=np.float32)
    voiced = (f0 > 0) & (conf > 0.55)
    if not voiced.any():
        return midi

    loud = float(np.percentile(rms[voiced], 90))
    voiced &= rms > 0.08 * loud

    midi[voiced] = hz_to_midi(f0[voiced])

    # 中值滤波（只在有效帧上做）
    idx = np.where(voiced)[0]
    if len(idx) >= 5:
        vals = midi[idx]
        padded = np.pad(vals, 2, mode="edge")
        med = np.array([np.median(padded[i:i + 5]) for i in range(len(vals))])
        midi[idx] = med

    # 八度纠错：跟局部中位数比较，差半个/一个八度的拉回来
    idx = np.where(midi > 0)[0]
    if len(idx) >= 7:
        vals = midi[idx]
        padded = np.pad(vals, 3, mode="edge")
        for i in range(len(vals)):
            local = np.median(padded[i:i + 7])
            for shift in (12.0, -12.0, 24.0, -24.0):
                if abs(vals[i] + shift - local) < 1.2:
                    vals[i] += shift
                    break
        midi[idx] = vals
    return midi


def segment(midi, times, hop_ms, min_note_ms=MIN_NOTE_MS, merge_gap_ms=MERGE_GAP_MS,
            tolerance=0.75):
    """把逐帧音高切成音符。

    关键：不逐帧取整再合并，而是先按「音高连续性」聚成片段，再对整段取中位数。
    这样带颤音的拖长音（±0.5 半音摆动）不会被切成一堆碎音后被 min_note_ms 全删掉。
    """
    groups = []
    current = []
    for i, value in enumerate(midi):
        if value <= 0:
            if current:
                groups.append(current)
                current = []
            continue
        if current:
            med = float(np.median([m for _t, m in current]))
            if abs(value - med) <= tolerance:
                current.append((times[i], value))
            else:
                groups.append(current)
                current = [(times[i], value)]
        else:
            current = [(times[i], value)]
    if current:
        groups.append(current)

    raw = []
    for group in groups:
        start = group[0][0] - hop_ms / 2
        end = group[-1][0] + hop_ms / 2
        pitch = int(round(float(np.median([m for _t, m in group]))))
        raw.append({"start": start, "end": end, "pitch": pitch})

    # 合并同音高的小断口
    merged = []
    for note in raw:
        if merged and note["pitch"] == merged[-1]["pitch"] \
                and note["start"] - merged[-1]["end"] <= merge_gap_ms:
            merged[-1]["end"] = note["end"]
        else:
            merged.append(dict(note))

    kept = [n for n in merged if n["end"] - n["start"] >= min_note_ms]
    kept.sort(key=lambda n: n["start"])
    return kept


# --------------------------------------------------------------------------- #
# 写 MIDI
# --------------------------------------------------------------------------- #
def _varlen(value: int) -> bytes:
    out = bytearray([value & 0x7F])
    value >>= 7
    while value:
        out.insert(0, (value & 0x7F) | 0x80)
        value >>= 7
    return bytes(out)


def write_midi(notes, path, bpm=120, name="Vocal Melody", quantize=0):
    division = 480
    ms_per_tick = 60000.0 / bpm / division

    events = []
    for note in notes:
        on = int(round(note["start"] / ms_per_tick))
        off = max(on + 1, int(round(note["end"] / ms_per_tick)))
        if quantize:
            grid = max(1, division * 4 // quantize)
            on = int(round(on / grid) * grid)
            off = max(on + grid // 2, int(round(off / grid) * grid))
        events.append((on, 1, bytes([0x90, note["pitch"], 96])))
        events.append((off, 0, bytes([0x80, note["pitch"], 0])))

    events.sort(key=lambda item: (item[0], item[1]))

    body = bytearray()
    body += _varlen(0) + b"\xFF\x51\x03" + struct.pack(">I", 500000)[1:]
    raw_name = name.encode("utf-8")
    body += _varlen(0) + b"\xFF\x03" + _varlen(len(raw_name)) + raw_name

    last = 0
    for tick, _order, payload in events:
        body += _varlen(tick - last)
        body += payload
        last = tick
    body += _varlen(0) + b"\xFF\x2F\x00"

    header = b"MThd" + struct.pack(">IHHH", 6, 0, 1, division)
    track = b"MTrk" + struct.pack(">I", len(body)) + bytes(body)
    with open(path, "wb") as fh:
        fh.write(header + track)
    return len(events)


# --------------------------------------------------------------------------- #
def main():
    parser = argparse.ArgumentParser(description="把歌曲人声旋律转成 MIDI")
    parser.add_argument("input", help="输入音频（mp3/wav/m4a 都行，靠 ffmpeg 解码）")
    parser.add_argument("-o", "--output", default=None, help="输出 .mid")
    parser.add_argument("--fmin", type=float, default=FMIN)
    parser.add_argument("--fmax", type=float, default=FMAX)
    parser.add_argument("--min-note-ms", type=float, default=MIN_NOTE_MS)
    parser.add_argument("--highpass", type=float, default=HIGHPASS_HZ,
                        help="高通截止频率（Hz），用来压掉贝斯/底鼓，0 关闭")
    parser.add_argument("--quantize", type=int, default=0,
                        help="量化到 1/N 音符（0=不量化，保留演唱节奏）")
    parser.add_argument("--preview", default=None, help="顺便导出一份试听 WAV")
    parser.add_argument("--wav-max", type=float, default=60.0,
                        help="试听最长合成秒数（默认 60，长曲子可调大）")
    parser.add_argument("--keep-wav", action="store_true", help="保留解码出的 wav")
    args = parser.parse_args()

    out = args.output or os.path.splitext(args.input)[0] + "-人声旋律.mid"
    tmp_dir = tempfile.mkdtemp(prefix="vocal2midi-")
    wav_path = os.path.join(tmp_dir, "decoded.wav")

    print(f"[1/5] 解码 {os.path.basename(args.input)} ...")
    audio, sr = decode(args.input, wav_path)
    hop_ms = HOP / sr * 1000.0
    print(f"      {len(audio) / sr:.1f}s  {sr}Hz  单声道")
    if args.highpass > 0:
        audio = highpass(audio, sr, args.highpass)
        print(f"      已做 {args.highpass:.0f}Hz 高通，压掉低音")

    print("[2/5] YIN 基频跟踪 ...")
    times, f0, conf, rms = yin_track(audio, sr, args.fmin, args.fmax)
    voiced_ratio = float((f0 > 0).mean())
    print(f"      {len(times)} 帧（{hop_ms:.1f}ms/帧）  有基频 {voiced_ratio * 100:.1f}%")

    print("[3/5] 清理音高（能量 / 置信度 / 中值 / 八度纠错）...")
    midi_pitch = clean_pitch(f0, conf, rms)
    analysed = int((midi_pitch > 0).sum())
    print(f"      可用帧 {analysed}（{analysed / len(times) * 100:.1f}%）")

    print("[4/5] 分段成音符 ...")
    notes = segment(midi_pitch, times, hop_ms, args.min_note_ms)
    if not notes:
        print("!! 一个音都没检出来，请调整 --fmin/--fmax 或用更干净的音频")
        return 1
    pitches = [n["pitch"] for n in notes]
    duration = notes[-1]["end"] / 1000.0
    print(f"      {len(notes)} 个音  音域 {min(pitches)}-{max(pitches)}  "
          f"覆盖 {duration:.1f}s  平均 {len(notes) / duration:.2f} 音/秒")

    # C 大调占比（简谱标注 1=C，可以当校验）
    major = {0, 2, 4, 5, 7, 9, 11}
    diatonic = sum(1 for p in pitches if p % 12 in major) / len(pitches)
    print(f"      落在 C 大调自然音上的比例：{diatonic * 100:.1f}%（简谱标 1=C）")

    print("[5/5] 写 MIDI ...")
    count = write_midi(notes, out, quantize=args.quantize)
    print(f"      已写出 {out}（{count} 个事件）")

    if args.preview:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        import mapping
        import synth
        spans = [mapping.Span(n["start"], n["end"], n["pitch"], mapping.Candidate(0, 0, 0))
                 for n in notes]
        data = synth.build_wav(spans, max_seconds=args.wav_max)
        with open(args.preview, "wb") as fh:
            fh.write(data)
        print(f"      试听 WAV：{args.preview}（{synth.wav_duration_ms(data) / 1000:.1f}s）")

    if not args.keep_wav:
        try:
            os.remove(wav_path)
            os.rmdir(tmp_dir)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
