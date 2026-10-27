# -*- coding: utf-8 -*-
"""最小标准 MIDI 文件（SMF）解析器 —— 只用 Python 标准库。

支持：format 0 / 1 / 2、running status、常用 meta 事件（速度 / 轨道名 / 结束）、
      多轨与多通道、通道 10（0-based 9）打击乐识别。

不依赖 mido / pygame 等任何第三方库，方便直接打包成单文件 exe。

用法：
    mf = parse_midi(open('song.mid','rb').read())
    for t in mf.tracks:
        print(t.index, t.name, len(t.notes))
"""
from __future__ import annotations

import struct
from bisect import bisect_right
from dataclasses import dataclass, field

DRUM_CHANNEL = 9          # 0-based：MIDI 第 10 通道为打击乐
DEFAULT_TEMPO = 500000    # 微秒 / 四分音符 = 120 BPM


class MidiError(ValueError):
    """MIDI 解析失败。"""


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass
class Note:
    pitch: int
    start: float          # 毫秒
    end: float            # 毫秒
    velocity: int = 80
    channel: int = 0

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class Track:
    index: int
    name: str = ""
    notes: list = field(default_factory=list)
    channels: set = field(default_factory=set)
    raw_note_count: int = 0          # 解析到的音符事件数（含被吞掉的配对）

    @property
    def is_drum(self) -> bool:
        return bool(self.channels) and self.channels <= {DRUM_CHANNEL}

    @property
    def duration(self) -> float:
        return max((n.end for n in self.notes), default=0.0)

    @property
    def pitch_range(self):
        if not self.notes:
            return (0, 0)
        ps = [n.pitch for n in self.notes]
        return (min(ps), max(ps))


@dataclass
class MidiFile:
    format: int
    division: int
    tracks: list
    length_ms: float
    name: str = ""

    @property
    def track_count(self) -> int:
        return len(self.tracks)


# --------------------------------------------------------------------------- #
# 底层读取
# --------------------------------------------------------------------------- #
def _read_varlen(data: bytes, pos: int):
    """读取 MIDI 变长数值，返回 (值, 新位置)。"""
    value = 0
    for _ in range(4):
        if pos >= len(data):
            raise MidiError("文件在变长数值处意外结束")
        byte = data[pos]
        pos += 1
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            return value, pos
    raise MidiError("变长数值超过 4 字节，文件可能损坏")


def _make_tick_to_ms(division: int, tempo_events):
    """根据速度事件构造 tick -> 毫秒 的换算函数。"""
    points = [(0, DEFAULT_TEMPO)]
    points.extend(tempo_events)
    points.sort(key=lambda x: x[0])

    merged = []
    for tick, tempo in points:
        if merged and merged[-1][0] == tick:
            merged[-1] = (tick, tempo)
        else:
            merged.append((tick, tempo))
    if merged[0][0] != 0:
        merged.insert(0, (0, DEFAULT_TEMPO))
    # 去掉非法速度值，避免除零
    merged = [(t, tp if tp > 0 else DEFAULT_TEMPO) for t, tp in merged]

    starts, seg_ms, seg_mpt = [], [], []
    cur_tick, cur_ms = 0, 0.0
    for i, (tick, tempo) in enumerate(merged):
        if i > 0:
            prev_tempo = merged[i - 1][1]
            cur_ms += (tick - cur_tick) * (prev_tempo / 1000.0 / division)
        cur_tick = tick
        starts.append(tick)
        seg_ms.append(cur_ms)
        seg_mpt.append(tempo / 1000.0 / division)

    def convert(tick: int) -> float:
        idx = bisect_right(starts, tick) - 1
        if idx < 0:
            idx = 0
        return seg_ms[idx] + (tick - starts[idx]) * seg_mpt[idx]

    return convert


# --------------------------------------------------------------------------- #
# 主解析
# --------------------------------------------------------------------------- #
def parse_midi(data: bytes, name: str = "") -> MidiFile:
    if len(data) < 14 or data[:4] != b"MThd":
        raise MidiError("不是有效的 MIDI 文件（缺少 MThd 头）")

    header_len, fmt, ntrks, division = struct.unpack(">IHHH", data[4:14])
    if header_len < 6:
        raise MidiError("MThd 头长度异常")
    if division & 0x8000:
        raise MidiError("暂不支持 SMPTE 时间格式的 MIDI，请用普通 PPQ 格式导出")

    pos = 8 + header_len
    raw_tracks = []          # [(events, name, tempos, raw_note_count)]
    tempo_events = []

    for track_index in range(ntrks):
        if pos + 8 > len(data):
            break
        chunk_id = data[pos:pos + 4]
        chunk_len = struct.unpack(">I", data[pos + 4:pos + 8])[0]
        body = data[pos + 8:pos + 8 + chunk_len]
        pos += 8 + chunk_len
        if chunk_id != b"MTrk":
            continue                      # 跳过未知块

        abs_tick = 0
        cursor = 0
        running_status = None
        pending = {}                      # (channel, pitch) -> [tick, velocity, ...]
        notes = []
        name_text = ""
        channels = set()
        raw_note_count = 0

        while cursor < len(body):
            delta, cursor = _read_varlen(body, cursor)
            abs_tick += delta
            if cursor >= len(body):
                break

            status = body[cursor]
            if status & 0x80:
                cursor += 1
                if status < 0xF0:
                    running_status = status
            else:
                if running_status is None:
                    raise MidiError("轨道使用了 running status 但没有前置状态字节")
                status = running_status

            if status == 0xFF:            # meta 事件
                if cursor >= len(body):
                    break
                meta_type = body[cursor]
                cursor += 1
                length, cursor = _read_varlen(body, cursor)
                payload = body[cursor:cursor + length]
                cursor += length

                if meta_type == 0x51 and len(payload) >= 3:
                    tempo = (payload[0] << 16) | (payload[1] << 8) | payload[2]
                    tempo_events.append((abs_tick, tempo))
                elif meta_type == 0x03:
                    try:
                        name_text = payload.decode("utf-8")
                    except UnicodeDecodeError:
                        name_text = payload.decode("gbk", errors="replace")
                elif meta_type == 0x2F:
                    break
                continue

            if status in (0xF0, 0xF7):    # SysEx / 转义序列
                length, cursor = _read_varlen(body, cursor)
                cursor += length
                continue

            if status < 0x80 or status >= 0xF0:
                # 无法识别的状态，跳过 1 字节防止死循环
                cursor += 1
                continue

            kind = status >> 4
            channel = status & 0x0F
            channels.add(channel)

            if kind in (0x8, 0x9, 0xA, 0xB, 0xE):
                if cursor + 2 > len(body):
                    break
                d1, d2 = body[cursor], body[cursor + 1]
                cursor += 2
                if kind == 0x9 and d2 > 0:              # Note On
                    pending.setdefault((channel, d1), []).append((abs_tick, d2))
                    raw_note_count += 1
                elif kind == 0x8 or (kind == 0x9 and d2 == 0):   # Note Off
                    queue = pending.get((channel, d1))
                    if queue:
                        start_tick, velocity = queue.pop(0)
                        notes.append((start_tick, abs_tick, d1, velocity, channel))
            elif kind in (0xC, 0xD):
                cursor += 1
            else:
                cursor += 2

        # 没收到 Note Off 的音符：补一个短尾音，避免整首丢失
        for (channel, pitch), queue in pending.items():
            for start_tick, velocity in queue:
                notes.append((start_tick, start_tick + max(1, division // 8),
                              pitch, velocity, channel))

        raw_tracks.append((notes, name_text, channels, raw_note_count))

    if not raw_tracks:
        raise MidiError("文件里没有任何 MTrk 轨道")

    convert = _make_tick_to_ms(division, tempo_events)

    tracks = []
    for index, (raw_notes, name_text, channels, raw_count) in enumerate(raw_tracks):
        notes = [
            Note(pitch=p, start=convert(t0), end=convert(t1), velocity=v, channel=ch)
            for (t0, t1, p, v, ch) in raw_notes
        ]
        notes.sort(key=lambda n: (n.start, n.pitch))
        tracks.append(Track(index=index, name=name_text, notes=notes,
                            channels=channels, raw_note_count=raw_count))

    length_ms = max((t.duration for t in tracks), default=0.0)
    return MidiFile(format=fmt, division=division, tracks=tracks,
                    length_ms=length_ms, name=name)


def load(path: str) -> MidiFile:
    with open(path, "rb") as fh:
        return parse_midi(fh.read(), name=path)


def pick_melody_track(midi: MidiFile):
    """猜一条主旋律轨：优先非打击乐、音符多、音域集中的轨道。

    音域用 10%~90% 分位跨度而不是最大最小，避免个别超纲音（比如装饰性的极高音）
    把一条旋律轨误判成伴奏。
    """
    best, best_score = None, None
    for track in midi.tracks:
        if not track.notes or track.is_drum:
            continue
        pitches = sorted(n.pitch for n in track.notes)
        lo = pitches[int(len(pitches) * 0.10)]
        hi = pitches[min(len(pitches) - 1, int(len(pitches) * 0.90))]
        span = max(0, hi - lo)
        # 音符数为主，音域过宽（多半是和弦/伴奏）扣分
        score = len(track.notes) - span * 1.2
        if best_score is None or score > best_score:
            best, best_score = track, score
    return best or (midi.tracks[0] if midi.tracks else None)
