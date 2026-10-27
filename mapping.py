# -*- coding: utf-8 -*-
"""音高 -> 游戏内口琴键位 的映射与演奏事件编译。

键位模型（三角洲行动「守夜人的口风琴 / 口琴」实测口径）：

    键盘    Z  X  C  V  B  N  M  ,
    音级    1  2  3  4  5  6  7  1̇      -> 半音偏移 0 2 4 5 7 9 11 12
    鼠标左键按住 = 整排 -12 半音（降八度）
    鼠标右键按住 = 整排 +12 半音（升八度）
    鼠标中键按住 = 整排 +1  半音（升半音）
    左/右键互斥，中键可与二者叠加

    pitch = P0 + 12*shift + degree[key] + sharp      shift ∈ {-1,0,+1}, sharp ∈ {0,1}

三个八度带（-12..+1 / 0..+13 / +12..+25）拼起来覆盖 P0-12 .. P0+25 共 38 个半音，
中间有重叠、没有空洞，所以这件乐器是完全半音阶的。

处理链：
    原始轨道
      -> converge_highest   多声部/和弦轨降成单声部（取最高音）
      -> best_global_octave  整轨移八度，尽量少折叠
      -> 逐音折叠 + 动态规划选指法（修饰键切换最少、手指移动最少）
      -> compile_events   编译成带绝对毫秒时间戳的输入事件流
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace

# --------------------------------------------------------------------------- #
# 乐器定义
# --------------------------------------------------------------------------- #
KEY_ORDER = ("z", "x", "c", "v", "b", "n", "m", ",")
KEY_OFFSETS = (0, 2, 4, 5, 7, 9, 11, 12)          # 音级相对半音

# 物理扫描码（键盘底排），游戏按物理键位识别，和输入法布局无关
DEFAULT_SCANCODES = {
    "z": 0x2C, "x": 0x2D, "c": 0x2E, "v": 0x2F,
    "b": 0x30, "n": 0x31, "m": 0x32, ",": 0x33,
}

MOUSE_LEFT = "left"       # 降八度
MOUSE_MIDDLE = "middle"   # 升半音
MOUSE_RIGHT = "right"     # 升八度

DEFAULT_BASE = 60         # P0：游戏内中音「1」对应的 MIDI 音高（可整体移调）

RANGE_LOW = -12           # 相对 P0 的最低半音
RANGE_HIGH = 25           # 相对 P0 的最高半音

MIN_NOTE_MS = 55.0        # 短于此的音会被游戏吞掉（实测口径）
DEFAULT_GAP_MS = 12.0     # 相邻音之间留的间隔，防止连音糊在一起
MIN_HOLD_MS = 35.0        # 按键最短按住时长

# 指法代价（调大 = 更不愿意切换）
COST_SHIFT_SWITCH = 12.0  # 鼠标左/右键切换（要先松开再按，最慢）
COST_SHIFT_HOLD = 0.0     # 按住八度修饰键本身不花钱，只有「切换」才花钱
COST_SHARP_SWITCH = 4.0   # 中键切换
COST_SHARP_HOLD = 0.25    # 尽量不用半音修饰
COST_KEY_TRAVEL = 0.5     # 相邻音之间手指跨键的代价

# 旋律提取参数（默认值由 tools/melody_sweep.py 在真实曲谱上扫出来）
# —— 结论：复杂方案收益不足，最终只保留 converge_highest，见该函数注释。


@dataclass(frozen=True)
class Candidate:
    shift: int            # -1 / 0 / +1
    sharp: int            # 0 / 1
    key_index: int        # KEY_ORDER 下标

    @property
    def key(self) -> str:
        return KEY_ORDER[self.key_index]


@dataclass
class MappedNote:
    pitch: int            # 最终发声音高（已含移调与折叠）
    original_pitch: int   # 原始 MIDI 音高
    start: float
    end: float
    candidate: Candidate
    folded: bool = False  # 是否做过八度折叠

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class Mapping:
    """一次完整映射的结果。可以像三元组一样解包：(notes, dropped, folded)。"""
    notes: list
    dropped: int = 0
    folded: int = 0
    lost: int = 0             # 被旋律提取/单音收敛吃掉的音
    global_shift: int = 0     # 整轨移调量（半音）
    melody_from: int = 0      # 提取前原始音符数
    mono: str = "highest"     # 实际采用的单声部化策略：highest / legato

    def __iter__(self):
        yield self.notes
        yield self.dropped
        yield self.folded

    @property
    def playable(self) -> int:
        return len(self.notes)


# --------------------------------------------------------------------------- #
# 音域与候选
# --------------------------------------------------------------------------- #
def in_range(pitch: int, base: int = DEFAULT_BASE) -> bool:
    offset = pitch - base
    return RANGE_LOW <= offset <= RANGE_HIGH


def fold_into_range(pitch: int, base: int = DEFAULT_BASE):
    """把超范围音按八度折叠进音域，返回 (新音高, 是否折叠过)。"""
    if in_range(pitch, base):
        return pitch, False
    for step in (12, -12, 24, -24, 36, -36, 48, -48):
        cand = pitch + step
        if in_range(cand, base):
            return cand, True
    return pitch, True


def candidates_for(pitch: int, base: int = DEFAULT_BASE):
    """列出某个音高所有可行指法。"""
    offset = pitch - base
    result = []
    for shift in (-1, 0, 1):
        rem = offset - 12 * shift
        if rem < 0 or rem > 13:
            continue
        for index, degree in enumerate(KEY_OFFSETS):
            if degree == rem:
                result.append(Candidate(shift, 0, index))
            if degree + 1 == rem:
                result.append(Candidate(shift, 1, index))
    return result


def best_global_octave(pitches, base: int = DEFAULT_BASE, tolerance: float = 0.05) -> int:
    """选一个整体八度偏移。

    规则：**优先最小移调量**。从 0 开始按 |shift| 由小到大试，
    只要能把越界音压到容差以内（默认 5%）就采用，不再继续往下移。
    这样避免「为了避免折叠而把整首歌降两个八度」这种听感损失很大的做法。
    """
    pitches = list(pitches)
    count = len(pitches)
    if not count:
        return 0
    budget = max(1, int(count * tolerance))

    def outside(shift):
        return sum(0 if in_range(p + shift, base) else 1 for p in pitches)

    if outside(0) <= budget:
        return 0
    fallback = None
    for magnitude in (12, 24, 36):
        best_here = None
        for shift in (magnitude, -magnitude):
            value = outside(shift)
            if value <= budget:
                if best_here is None or value < best_here[0]:
                    best_here = (value, shift)
        if best_here is not None:
            return best_here[1]
        if fallback is None:
            fallback = min(((outside(s), s) for s in (magnitude, -magnitude)))
        else:
            fallback = min(fallback, min(((outside(s), s) for s in (magnitude, -magnitude))))
    return fallback[1] if fallback else 0


# --------------------------------------------------------------------------- #
# 单声部化
# --------------------------------------------------------------------------- #
def converge_highest(notes):
    """「取最高音」：重叠时保留高音，后起的低音被丢弃。

    适合钢琴/伴奏这类和弦轨 —— 顶音通常就是旋律。
    不适合人声/独奏：连奏时前一个音还响着，会一直卡在高音上，
    把后面下行的小节整个吃掉（《毕业歌》合唱轨就是这样跑偏的）。
    """
    result = []
    for note in sorted((replace(n) for n in notes), key=lambda n: (n.start, -n.pitch)):
        if result:
            prev = result[-1]
            if note.start < prev.end:
                if note.pitch > prev.pitch:
                    prev.end = note.start
                    if prev.end <= prev.start:
                        result.pop()
                else:
                    continue
        result.append(note)
    return [n for n in result if n.end > n.start]


def converge_legato(notes):
    """「后起优先」：谁后开始谁是新音节，前一个音截断到它的起点。

    适合人声 / 独奏这类单声部连奏轨 —— 一个音都不丢。
    """
    result = []
    for note in sorted((replace(n) for n in notes), key=lambda n: (n.start, -n.pitch)):
        if result and note.start < result[-1].end:
            result[-1].end = note.start
        result.append(note)
    return [n for n in result if n.end > n.start]


def _mono_score(sequence):
    """单声部化结果的质量分：跳进越规整、保住的音越多越好。"""
    pitches = [note.pitch for note in sequence]
    leaps = sorted(abs(b - a) for a, b in zip(pitches, pitches[1:]) if b != a)
    big = sum(1 for leap in leaps if leap > 7)
    if leaps:
        middle = leaps[len(leaps) // 2]
    else:
        middle = 0
    return (big, middle, -len(sequence))


# --------------------------------------------------------------------------- #
# 映射主流程
# --------------------------------------------------------------------------- #
def map_notes(notes, base: int = DEFAULT_BASE, transpose: int = 0,
              auto_fit: bool = True, fold: bool = True, mono: str = "auto"):
    """把音符序列映射成指法序列。

    参数
    ----
    notes      : smf.Note 列表
    base       : 游戏内中音「1」的 MIDI 音高
    transpose  : 用户手动移调（半音）
    auto_fit   : 自动选一个整体八度偏移，减少越界音
    fold       : 仍然越界的音是否按八度折叠（False 则丢弃）
    mono       : 单声部化策略
                 "highest" = 重叠取最高音（适合钢琴/和弦）
                 "legato"  = 后起优先、一个不丢（适合人声/独奏）
                 "auto"    = 两种都算一遍，挑跳进更规整、保音更多的那个

    返回 Mapping（可解包成 (notes, dropped, folded)）
    """
    original = list(notes)
    if not original:
        return Mapping([], 0, 0, 0, 0, 0, "highest")

    # 关键：先复制一份。单音收敛会截断音尾，直接改原对象会让「改参数重算」越算越短。
    highest = converge_highest(original)
    legato = converge_legato(original)
    if mono == "highest":
        working, chosen = highest, "highest"
    elif mono == "legato":
        working, chosen = legato, "legato"
    elif _mono_score(legato) < _mono_score(highest):
        working, chosen = legato, "legato"
    else:
        working, chosen = highest, "highest"
    lost = len(original) - len(working)

    raw = [n.pitch + transpose for n in working]
    global_shift = best_global_octave(raw, base) if auto_fit else 0

    staged = []            # (note, target_pitch, was_folded)
    dropped = 0
    folded = 0
    for note, pitch in zip(working, raw):
        target = pitch + global_shift
        was_folded = False
        if not in_range(target, base):
            if not fold:
                dropped += 1
                continue
            target, was_folded = fold_into_range(target, base)
            if was_folded:
                folded += 1
            if not in_range(target, base):
                dropped += 1
                continue
        staged.append((note, target, was_folded))
    if not staged:
        return Mapping([], dropped, folded, lost, global_shift, len(original), chosen)

    staged.sort(key=lambda item: (item[0].start, -item[1]))
    # 提取后仍可能重叠（同一起音只保留了 1 个，但长音会跨界），做一次收尾收敛
    converged = []
    for item in staged:
        note, pitch, was_folded = item
        if converged:
            prev_note, prev_pitch, _ = converged[-1]
            if note.start < prev_note.end:
                if pitch > prev_pitch:
                    prev_note.end = note.start
                    if prev_note.end <= prev_note.start:
                        converged.pop()
                else:
                    lost += 1
                    continue
        converged.append((note, pitch, was_folded))

    # 动态规划选指法
    per_note = []
    for note, pitch, was_folded in converged:
        cands = candidates_for(pitch, base)
        if cands:
            per_note.append((note, pitch, cands, was_folded))
    if not per_note:
        return Mapping([], dropped, folded, lost, global_shift, len(original), chosen)

    dp, back = [], []
    dp.append([COST_SHARP_HOLD * c.sharp + COST_KEY_TRAVEL * abs(c.key_index - 3)
               for c in per_note[0][2]])
    back.append([-1] * len(dp[0]))
    for index in range(1, len(per_note)):
        cands = per_note[index][2]
        prev_cands = per_note[index - 1][2]
        row, row_back = [], []
        for cur in cands:
            best_cost, best_prev = None, 0
            for prev_index, prev in enumerate(prev_cands):
                cost = dp[index - 1][prev_index] + _transition_cost(prev, cur)
                if best_cost is None or cost < best_cost:
                    best_cost, best_prev = cost, prev_index
            row.append(best_cost)
            row_back.append(best_prev)
        dp.append(row)
        back.append(row_back)

    chain = [0] * len(per_note)
    chain[-1] = min(range(len(dp[-1])), key=lambda j: dp[-1][j])
    for index in range(len(per_note) - 1, 0, -1):
        chain[index - 1] = back[index][chain[index]]

    mapped = []
    for index, (note, pitch, cands, was_folded) in enumerate(per_note):
        mapped.append(MappedNote(pitch=pitch, original_pitch=note.pitch,
                                 start=note.start, end=note.end,
                                 candidate=cands[chain[index]], folded=was_folded))
    return Mapping(mapped, dropped, folded, lost, global_shift, len(original), chosen)


def _transition_cost(prev: Candidate, cur: Candidate) -> float:
    cost = 0.0
    if prev.shift != cur.shift:
        cost += COST_SHIFT_SWITCH
    elif cur.shift:
        cost += COST_SHIFT_HOLD
    if prev.sharp != cur.sharp:
        cost += COST_SHARP_SWITCH
    if cur.sharp:
        cost += COST_SHARP_HOLD
    cost += COST_KEY_TRAVEL * abs(cur.key_index - prev.key_index)
    return cost


# --------------------------------------------------------------------------- #
# 自动移调 / 自动选轨
# --------------------------------------------------------------------------- #
def best_transpose(notes, base: int = DEFAULT_BASE, span=range(-6, 7)):
    """找一个整体移调量，让演奏最省修饰键。

    游戏里的 8 个键是自然音阶（do re mi fa sol la ti do），调外音必须按中键。
    整曲换一个调，往往能把大量半音变成自然音 —— 这是最省事的一次优化。
    评分：半音使用率 -> 八度切换率 -> 移调量绝对值。
    """
    best_t, best_score = 0, None
    for value in span:
        result = map_notes(notes, base=base, transpose=value, auto_fit=True, fold=True)
        if not result.notes:
            continue
        count = len(result.notes)
        sharps = sum(1 for item in result.notes if item.candidate.sharp) / count
        shifts = 0
        for index in range(1, count):
            if result.notes[index].candidate.shift != result.notes[index - 1].candidate.shift:
                shifts += 1
        score = (round(sharps, 4), round(shifts / count, 4), abs(value))
        if best_score is None or score < best_score:
            best_t, best_score = value, score
    return best_t


def analyse_track(track, base: int = DEFAULT_BASE, transpose: int = 0,
                  mono: str = "auto"):
    """给一条轨道算体检指标（GUI 列表和自动选轨都用它）。"""
    if track is None or not track.notes:
        return None
    result = map_notes(track.notes, base=base, transpose=transpose,
                       auto_fit=True, fold=True, mono=mono)
    if not result.notes:
        return None
    count = len(result.notes)
    sharps = sum(1 for item in result.notes if item.candidate.sharp)
    shifts = sum(1 for i in range(1, count)
                 if result.notes[i].candidate.shift != result.notes[i - 1].candidate.shift)
    return {
        "playable": count,
        "raw": len(track.notes),
        "lost": result.lost,
        "folded": result.folded,
        "dropped": result.dropped,
        "global_shift": result.global_shift,
        "sharps": sharps,
        "sharp_ratio": sharps / count,
        "shift_switch": shifts,
        "shift_ratio": shifts / count,
        "mono": result.mono,
        "duration_ms": result.notes[-1].end if result.notes else 0.0,
    }


def pick_best_track(tracks, base: int = DEFAULT_BASE, transpose: int = 0,
                    mono: str = "auto"):
    """按「真吹得出来多少音」挑主旋律轨。

    打分 = 可吹音符数 - 1.2×被旋律提取吃掉的音 - 0.5×折叠音 - 0.8×整轨移调八度数

    钢琴/伴奏轨音符看着多，但一个起音簇里只能留一个音，实际能吹的往往更少；
    这一项能把「合唱 vs 钢琴」这类选择纠正过来。
    """
    best, best_score = None, None
    for track in tracks:
        if not track.notes or track.is_drum:
            continue
        info = analyse_track(track, base=base, transpose=transpose, mono=mono)
        if info is None:
            continue
        score = (info["playable"]
                 - 1.2 * info["lost"]
                 - 0.5 * info["folded"]
                 - 0.8 * (abs(info["global_shift"]) // 12))
        if best_score is None or score > best_score:
            best, best_score = track, score
    return best


# --------------------------------------------------------------------------- #
# 事件编译
# --------------------------------------------------------------------------- #
@dataclass
class Event:
    time: float           # 毫秒（演奏起点为 0）
    kind: str             # key_down / key_up / mouse_down / mouse_up
    arg: str              # 键名 z.., 或 left/middle/right

    def __repr__(self):
        return f"<{self.time:8.1f}ms {self.kind:10s} {self.arg}>"


@dataclass
class Span:
    """一个「实际会发出来的音」：演奏和试听共用这份时间线。"""
    start: float
    end: float
    pitch: int
    candidate: Candidate

    @property
    def duration(self) -> float:
        return self.end - self.start


def note_spans(mapped, gap_ms: float = DEFAULT_GAP_MS,
               min_note_ms: float = MIN_NOTE_MS,
               min_hold_ms: float = MIN_HOLD_MS,
               speed: float = 1.0):
    """把指法序列算成「实际会发出来的音」。

    这里决定了三件事，试听与演奏必须完全一致，否则听到的和吹出来的不一样：
      1. 时值太短的音直接跳过；
      2. 音尾截断到下一个音之前（留 gap）；
      3. 保底按住 min_hold_ms。

    speed 只影响第 1 条：游戏能识别的最短音是绝对时间，
    所以放慢速度之后，原本吹不出来的短音其实变得可吹。
    """
    spans = []
    skipped = 0
    speed = speed if speed > 0 else 1.0
    for index, note in enumerate(mapped):
        if note.duration / speed < min_note_ms * 0.5:
            skipped += 1
            continue
        start = note.start
        end = note.end
        if index + 1 < len(mapped):
            end = min(end, mapped[index + 1].start - gap_ms)
        if end - start < min_hold_ms:
            end = start + min_hold_ms
        spans.append(Span(start, end, note.pitch, note.candidate))
    return spans, skipped


def events_from_spans(spans):
    """把「实际会发出来的音」编译成输入事件流。

    关键顺序（同一时刻按列表顺序发送）：
        上一个音的 key_up  ->  修饰键变化  ->  本音的 key_down
    修饰键必须在没有按键按住时切换，否则会把正在发的音改调。
    """
    events = []
    cur_shift, cur_sharp = 0, 0

    for span in spans:
        start, end = span.start, span.end
        want_shift = span.candidate.shift
        want_sharp = span.candidate.sharp
        if want_shift != cur_shift:
            if cur_shift == -1:
                events.append(Event(start, "mouse_up", MOUSE_LEFT))
            elif cur_shift == 1:
                events.append(Event(start, "mouse_up", MOUSE_RIGHT))
            if want_shift == -1:
                events.append(Event(start, "mouse_down", MOUSE_LEFT))
            elif want_shift == 1:
                events.append(Event(start, "mouse_down", MOUSE_RIGHT))
            cur_shift = want_shift
        if want_sharp != cur_sharp:
            events.append(Event(start, "mouse_down" if want_sharp else "mouse_up",
                                MOUSE_MIDDLE))
            cur_sharp = want_sharp

        key = span.candidate.key
        events.append(Event(start, "key_down", key))
        events.append(Event(end, "key_up", key))

    tail = events[-1].time + 1.0 if events else 0.0
    if cur_shift == -1:
        events.append(Event(tail, "mouse_up", MOUSE_LEFT))
    elif cur_shift == 1:
        events.append(Event(tail, "mouse_up", MOUSE_RIGHT))
    if cur_sharp:
        events.append(Event(tail, "mouse_up", MOUSE_MIDDLE))

    return events


def compile_events(mapped, gap_ms: float = DEFAULT_GAP_MS,
                   min_note_ms: float = MIN_NOTE_MS,
                   min_hold_ms: float = MIN_HOLD_MS,
                   speed: float = 1.0):
    """note_spans + events_from_spans 一步到位，返回 (事件流, 跳过数)。"""
    spans, skipped = note_spans(mapped, gap_ms, min_note_ms, min_hold_ms, speed)
    return events_from_spans(spans), skipped


def _mono_reduce(notes, mono="auto"):
    """按策略把多声部降成单声部。"""
    if mono == "highest":
        return converge_highest(notes)
    if mono == "legato":
        return converge_legato(notes)
    highest = converge_highest(notes)
    legato = converge_legato(notes)
    return legato if _mono_score(legato) < _mono_score(highest) else highest


def monophonic(notes, mono="auto"):
    """按策略把音符降成单声部（供 GUI/工具直接调用）。"""
    return _mono_reduce(notes, mono)


def merge_tracks(primary_notes, secondary_notes, mono="auto",
                 min_gap_ms: float = 200.0, edge_ms: float = 25.0,
                 scope: str = "all"):
    """以 primary 为主旋律，把 secondary 填进它「没有音」的地方。

    实际用途：人声没唱的段落（前奏、间奏、呼吸口）用钢琴补上，
    让整段演奏听起来是连续的，而不是突然安静。
    只会往空档里塞，绝不会盖住主旋律 —— 口琴本来也只能发一个音。

    scope:
      "all"  开头 + 中间所有 ≥min_gap_ms 的空档 + 结尾
      "lead" 只补开头（前奏），中间保持原样

    返回 (合并后的音符列表, 补入的音数)。
    """
    main = _mono_reduce(primary_notes, mono)
    if not secondary_notes:
        return main, 0
    extra = _mono_reduce(secondary_notes, mono)
    if not main:
        return extra, 0
    if not extra:
        return main, 0

    gaps = [(float("-inf"), main[0].start)]        # 开头（前奏）也算空档
    if scope == "all":
        for first, second in zip(main, main[1:]):
            if second.start - first.end >= min_gap_ms:
                gaps.append((first.end, second.start))
        gaps.append((main[-1].end, float("inf")))  # 结尾之后也算空档

    filled = list(main)
    added = 0
    for note in extra:
        for gap_start, gap_end in gaps:
            if note.start >= gap_start + edge_ms and note.end <= gap_end - edge_ms:
                filled.append(note)
                added += 1
                break
    filled.sort(key=lambda n: (n.start, -n.pitch))
    return filled, added


def pick_fill_track(tracks, primary, max_median_diff: float = 12.0):
    """挑一条用来填空白处的轨：音区接近主轨（排除贝斯/低音铺底）、音符最多。"""
    if primary is None or not primary.notes:
        return None
    primary_median = _median_pitch(primary.notes)
    best, best_count = None, 0
    for track in tracks:
        if track is primary or not track.notes or track.is_drum:
            continue
        if abs(_median_pitch(track.notes) - primary_median) > max_median_diff:
            continue
        if len(track.notes) > best_count:
            best, best_count = track, len(track.notes)
    return best


def _median_pitch(notes):
    pitches = sorted(n.pitch for n in notes)
    return pitches[len(pitches) // 2] if pitches else 0


def trim_lead(spans, keep_ms: float = 0.0):
    """把开头没声音的部分裁掉，让第一个音从 0 开始。

    很多 MIDI 的歌姬/人声轨前面有一大段前奏（《毕业歌》合唱轨要等 7 秒），
    播放时干等没有意义 —— 倒计时结束就该出声。
    keep_ms 可以保留一点提前量（默认 0）。
    """
    if not spans:
        return spans
    first = min(span.start for span in spans)
    shift = first - max(0.0, keep_ms)
    if shift <= 1e-6:
        return spans
    return [replace(span, start=span.start - shift, end=span.end - shift) for span in spans]


def scale_events(events, speed: float):
    """整体倍速（speed > 1 更快）。"""
    if speed == 1.0 or not events:
        return events
    factor = 1.0 / speed
    return [Event(e.time * factor, e.kind, e.arg) for e in events]


# --------------------------------------------------------------------------- #
# 段落截取 / 变速建议
# --------------------------------------------------------------------------- #
def slice_mapped(mapped, start_ms: float, end_ms: float):
    """截取 [start_ms, end_ms] 这一段，并把时间轴平移到 0。

    end_ms <= 0 表示到结尾。跨越边界的音会被截断，不会整段丢弃。
    """
    if start_ms <= 0 and end_ms <= 0:
        return list(mapped)
    result = []
    for note in mapped:
        start = max(note.start, start_ms)
        end = note.end if end_ms <= 0 else min(note.end, end_ms)
        if end <= start:
            continue
        result.append(replace(note, start=start - start_ms, end=end - start_ms))
    return result


def recommend_speed(mapped, min_note_ms: float = MIN_NOTE_MS,
                    min_speed: float = 0.5, recover: float = 0.5,
                    step: float = 0.05):
    """根据「有多少音因为太短会被跳过」，推荐一个倍速。

    思路：游戏能识别的最短音是绝对时间，放慢速度就能把短音救回来。
    这里不追求全都救回（那会把曲子拖得很慢），而是救回其中 `recover` 比例的短音，
    再把速度按 step 向下取整到 0.05，最后不低于 min_speed。

    返回 None 表示原速就够、不需要建议；否则返回 dict。
    """
    threshold = min_note_ms * 0.5
    short = sorted(n.duration for n in mapped if 0 < n.duration < threshold)
    if not short:
        return None

    index = int(round((1.0 - recover) * (len(short) - 1)))
    target = short[max(0, min(index, len(short) - 1))]
    raw = target / threshold
    speed = math.floor(raw / step + 1e-9) * step
    speed = round(max(min_speed, min(1.0, speed)), 2)
    if speed >= 1.0:
        return None

    rescued = sum(1 for duration in short if duration / speed >= threshold)
    if rescued <= 0:
        return None
    return {
        "speed": speed,
        "short": len(short),           # 原速下会被跳过的音数
        "rescued": rescued,            # 用建议速度能救回的音数
        "total": len(mapped),
    }


def describe_mapping(mapped, limit: int = 12):
    """给日志/自检用的可读文本。"""
    lines = []
    names = {-1: "左键↓八度", 0: "无", 1: "右键↑八度"}
    for note in mapped[:limit]:
        cand = note.candidate
        lines.append(
            f"{note.start:8.0f}ms  MIDI {note.original_pitch:3d} -> {note.pitch:3d}  "
            f"{names[cand.shift]:>10s} {'中键半音' if cand.sharp else '        '}  键[{cand.key}]"
        )
    if len(mapped) > limit:
        lines.append(f"... 共 {len(mapped)} 个音")
    return "\n".join(lines)
