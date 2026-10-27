# -*- coding: utf-8 -*-
"""钢琴录音 -> MIDI（多音转录 + 单声部旋律提取）。

    python tools/piano_to_midi.py "钢琴.flac" -o "输出.mid"

后端：piano_transcription_inference（ByteDance/Qiuqiang Kong 的钢琴转录模型，
      基于 torch，CPU 可跑）。它输出多音 MIDI（左右手都在一起）。
之后本脚本会再挑出一条单声部旋律线（口琴只能发一个音）。

模型权重约 164MB。用 --model-dir 指定位置；未指定时读取 PIANO_MODEL_DIR，
再退回 ~/piano_transcription_inference_data。脚本用 urllib 下载，不依赖 wget。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

MODEL_NAME = "note_F1=0.9677_pedal_F1=0.9186.pth"
MODEL_URL = ("https://zenodo.org/record/4034264/files/"
             "CRNN_note_F1%3D0.9677_pedal_F1%3D0.9186.pth?download=1")


def ensure_model(model_dir: str) -> str:
    """确保钢琴转录模型存在；下载时直接写到用户指定的目录。"""
    os.makedirs(model_dir, exist_ok=True)
    path = os.path.join(model_dir, MODEL_NAME)
    if os.path.exists(path) and os.path.getsize(path) >= 160_000_000:
        return path

    temp = path + ".part"
    print(f"      首次运行：下载约 164MB 模型到 {path}")

    def report(blocks, block_size, total):
        if total > 0:
            done = min(total, blocks * block_size)
            pct = done / total * 100
            print(f"\r      下载 {pct:5.1f}%", end="", flush=True)

    try:
        urllib.request.urlretrieve(MODEL_URL, temp, reporthook=report)
        print()
        if os.path.getsize(temp) < 160_000_000:
            raise RuntimeError("模型下载不完整")
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            try:
                os.remove(temp)
            except Exception:
                pass
    return path


def decode(path: str, sr: int = 16000) -> str:
    tmp = os.path.join(tempfile.gettempdir(), "piano-16k.wav")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", path,
                    "-ac", "1", "-ar", str(sr), "-acodec", "pcm_s16le", tmp],
                   check=True)
    return tmp


def _continuity(notes, tolerance=30.0, leap_weight=0.35, skyline=0.25):
    """在「同一起音簇」里挑一条连续性最好的线（钢琴的多声部 -> 单声部）。

    代价 = 与前一个音的音程（按 12 半音归一）+ 非顶音的惩罚。
    比「直接取最高音」稳：不会在琶音和旋律之间来回跳。
    """
    from dataclasses import replace

    groups, current = [], []
    for note in notes:
        if current and note.start - current[0].start <= tolerance:
            current.append(note)
        else:
            if current:
                groups.append(current)
            current = [note]
    if current:
        groups.append(current)
    if not groups:
        return []

    costs, backs = [], []
    for index, group in enumerate(groups):
        top = max(item.pitch for item in group)
        row, back = [], []
        for position, cand in enumerate(group):
            local = (0.0 if cand.pitch == top else skyline) + 0.03 * position
            if index == 0:
                row.append(local)
                back.append(-1)
                continue
            best, best_prev = None, 0
            for prev_index, prev in enumerate(groups[index - 1]):
                value = (costs[index - 1][prev_index]
                         + leap_weight * abs(cand.pitch - prev.pitch) / 12.0
                         + local)
                if best is None or value < best:
                    best, best_prev = value, prev_index
            row.append(best)
            back.append(best_prev)
        costs.append(row)
        backs.append(back)

    chain = [0] * len(groups)
    chain[-1] = min(range(len(costs[-1])), key=lambda j: costs[-1][j])
    for index in range(len(groups) - 1, 0, -1):
        chain[index - 1] = backs[index][chain[index]]
    picked = [replace(groups[i][chain[i]]) for i in range(len(groups))]
    for index in range(len(picked) - 1):
        if picked[index].end > picked[index + 1].start:
            picked[index].end = picked[index + 1].start
    return [n for n in picked if n.end > n.start]


def main():
    parser = argparse.ArgumentParser(description="钢琴录音 -> MIDI")
    parser.add_argument("input")
    parser.add_argument("-o", "--output", default=None, help="多音 MIDI 输出")
    parser.add_argument("--mono-out", default=None, help="单声部旋律 MIDI 输出")
    parser.add_argument("--preview", default=None, help="试听 WAV")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--model-dir",
                        default=os.environ.get("PIANO_MODEL_DIR") or
                                os.path.join(os.path.expanduser("~"),
                                             "piano_transcription_inference_data"),
                        help="模型存放目录（约 164MB，可放到非系统盘）")
    parser.add_argument("--top-voice", action="store_true", default=True,
                        help="提取单声部时取最高声部（默认开）")
    parser.add_argument("--melody-lo", type=int, default=55,
                        help="旋律提取的音域下限（默认 55 = G3）")
    parser.add_argument("--melody-hi", type=int, default=84,
                        help="旋律提取的音域上限（默认 84 = C6）")
    args = parser.parse_args()

    import numpy as np
    import torch
    import wave
    from piano_transcription_inference import PianoTranscription, sample_rate

    print(f"[1/4] 解码 {os.path.basename(args.input)} ...")
    wav = decode(args.input, sample_rate)
    with wave.open(wav, "rb") as reader:
        frames = reader.readframes(reader.getnframes())
    audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    print(f"      {len(audio) / sample_rate:.1f}s  {sample_rate}Hz")

    print("[2/4] 加载转录模型 ...")
    checkpoint = ensure_model(os.path.abspath(args.model_dir))
    print(f"      模型：{checkpoint}")
    transcriptor = PianoTranscription(checkpoint_path=checkpoint,
                                      device=torch.device(args.device))

    out = args.output or os.path.join(ROOT, "piano.mid")
    print("[3/4] 多音转录 ...")
    transcriptor.transcribe(audio, out)
    print(f"      多音 MIDI：{out}")

    # ---- 解析结果，挑单声部旋律 ----
    import mapping
    import smf

    midi = smf.parse_midi(open(out, "rb").read())
    all_notes = [n for track in midi.tracks for n in track.notes]
    all_notes.sort(key=lambda n: n.start)
    print(f"      转录出 {len(all_notes)} 个音，音域 "
          f"{min(n.pitch for n in all_notes)}-{max(n.pitch for n in all_notes)}，"
          f"时长 {midi.length_ms / 1000:.1f}s")

    # 钢琴是多声部：直接「取最高音」会抓到琶音/伴奏顶音。
    # 这里先把音域收到旋律区（默认 55-84，C 大调 G3-C6），再用连续性 DP 挑线。
    lo, hi = args.melody_lo, args.melody_hi
    band = [n for n in all_notes if lo <= n.pitch <= hi]
    mono = _continuity(band)
    print(f"      取 {lo}-{hi} 音域 + 连续性提取 -> {len(mono)} 个音")

    mono_out = args.mono_out or os.path.splitext(out)[0] + "-单声部.mid"
    import score_to_midi as sm
    triples = [(n.start, n.end, n.pitch) for n in mono]
    sm.write_midi(triples, mono_out, bpm=120, name="钢琴转录（单声部）")
    print(f"      单声部 MIDI：{mono_out}")

    info = mapping.analyse_track(smf.Track(index=0, name="melody", notes=mono))
    if info:
        print(f"      口琴可吹 {info['playable']}/{len(mono)}  吃音 {info['lost']}  "
              f"折叠 {info['folded']}  移调 {info['global_shift']:+d}  "
              f"中键半音 {info['sharps']} 个")

    if args.preview:
        import synth
        spans = [mapping.Span(n.start, n.end, n.pitch, mapping.Candidate(0, 0, 0))
                 for n in mono]
        data = synth.build_wav(spans, max_seconds=400.0)
        with open(args.preview, "wb") as fh:
            fh.write(data)
        print(f"      试听：{args.preview}（{synth.wav_duration_ms(data) / 1000:.1f}s）")

    try:
        os.remove(wav)
    except Exception:
        pass
    print("[4/4] 完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
