# -*- coding: utf-8 -*-
"""本地试听合成器：把「实际会发出来的音」合成成 WAV，用 winsound 播放。

为什么单独做一个模块：
* 它**不发送任何键盘/鼠标事件**，纯粹出声，所以可以放心在游戏外试听；
* 它和演奏共用同一份时间线（mapping.note_spans），听到的就是游戏里吹出来的；
* 只用标准库（array / io / math / wave / winsound），能直接打进单文件 exe。

音色：簧片类乐器偏奇次泛音，用「基频 + 3/5/7 次泛音」的波表逼近口琴/口风琴的味道。
"""
from __future__ import annotations

import array
import io
import math
import os
import wave

SAMPLE_RATE = 16000          # 16kHz 够表现口琴音域，合成速度也还能接受
MAX_SECONDS = 60.0           # 单次试听最长合成 60 秒，避免长时间占 CPU / 内存
TABLE_SIZE = 4096
_TABLE_MASK = TABLE_SIZE - 1

# 波表：口琴/口风琴偏奇次泛音
_TABLE = []
for _i in range(TABLE_SIZE):
    _ph = 2.0 * math.pi * _i / TABLE_SIZE
    _v = (1.00 * math.sin(_ph)
          + 0.42 * math.sin(3 * _ph)
          + 0.22 * math.sin(5 * _ph)
          + 0.10 * math.sin(7 * _ph))
    _TABLE.append(_v)


def build_wav(spans, speed: float = 1.0, sample_rate: int = SAMPLE_RATE,
              tail_ms: float = 150.0, max_seconds: float = MAX_SECONDS,
              gain: float = 0.8) -> bytes:
    """把 note_spans 的结果合成成 WAV 字节流。

    spans  : mapping.Span 列表（start/end 毫秒、pitch）
    speed  : 与演奏相同的倍速（>1 更快），时间线会按同样的比例压缩
    """
    if not spans:
        return b""
    speed = speed if speed > 0 else 1.0
    inv = 1.0 / speed
    limit_ms = max_seconds * 1000.0

    end_ms = max(span.end for span in spans) * inv + tail_ms
    end_ms = min(end_ms, limit_ms)
    total = int(sample_rate * end_ms / 1000.0)
    if total <= 0:
        return b""

    buf = array.array("f", [0.0]) * total

    for span in spans:
        start_ms = span.start * inv
        if start_ms >= limit_ms:
            continue
        stop_ms = min(span.end * inv, limit_ms)
        if stop_ms <= start_ms:
            continue
        i0 = int(start_ms * sample_rate / 1000.0)
        i1 = min(int(stop_ms * sample_rate / 1000.0), total)
        if i1 <= i0:
            continue

        freq = 440.0 * 2.0 ** ((span.pitch - 69) / 12.0)
        step = freq * TABLE_SIZE / sample_rate
        phase = 0.0
        count = i1 - i0
        attack = max(1, min(count, int(0.006 * sample_rate)))
        release = max(1, min(count, int(0.030 * sample_rate)))
        sus_start = i0 + attack
        rel_start = i1 - release

        for j in range(i0, i1):
            if j < sus_start:
                amp = (j - i0) / attack
            elif j < rel_start:
                amp = 1.0
            else:
                amp = (i1 - j) / release
            index = int(phase)
            if index >= TABLE_SIZE:
                index -= TABLE_SIZE
                phase -= TABLE_SIZE
            buf[j] += _TABLE[index] * amp * gain
            phase += step

    peak = 0.0
    for value in buf:
        if value > peak:
            peak = value
        elif -value > peak:
            peak = -value
    if peak < 1e-6:
        return b""
    scale = 32767.0 * 0.92 / peak

    pcm = array.array("h", bytes(2 * total))
    for i in range(total):
        value = buf[i] * scale
        if value > 32767.0:
            value = 32767.0
        elif value < -32768.0:
            value = -32768.0
        pcm[i] = int(value)

    out = io.BytesIO()
    with wave.open(out, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        writer.writeframes(pcm.tobytes())
    return out.getvalue()


# --------------------------------------------------------------------------- #
# 播放
# --------------------------------------------------------------------------- #
def _temp_path(index: int = 0) -> str:
    import tempfile
    return os.path.join(tempfile.gettempdir(),
                        f"dsh-harmonica-preview-{os.getpid()}-{index}.wav")


_last_path = None


def save_temp_wav(data: bytes, index: int = 0) -> str:
    """把 WAV 落到临时目录，返回路径。

    为什么要落盘：winsound 不支持「内存 + 异步」播放
    （SND_MEMORY 与 SND_ASYNC 同时用会抛 RuntimeError），
    而异步才有机会中途停止。落一个临时文件是最省事又可靠的做法。
    """
    global _last_path
    path = _temp_path(index)
    with open(path, "wb") as fh:
        fh.write(data)
    _last_path = path
    return path


def play_wav(data: bytes) -> str:
    """异步播放一段 WAV，返回临时文件路径。"""
    import winsound
    path = save_temp_wav(data)
    winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC
                       | winsound.SND_NODEFAULT)
    return path


def stop():
    """停止播放（没有在播也不会报错）。"""
    try:
        import winsound
        winsound.PlaySound(None, winsound.SND_PURGE)
    except Exception:
        pass


def cleanup_temp():
    """删掉试听用的临时文件（正在播时删不掉，忽略即可）。"""
    global _last_path
    if not _last_path:
        return
    try:
        os.remove(_last_path)
    except Exception:
        pass
    _last_path = None


def wav_duration_ms(data: bytes) -> float:
    """读 WAV 头的时长，自检和界面提示用。"""
    try:
        with wave.open(io.BytesIO(data), "rb") as reader:
            rate = reader.getframerate()
            return reader.getnframes() / rate * 1000.0 if rate else 0.0
    except Exception:
        return 0.0
