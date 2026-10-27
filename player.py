# -*- coding: utf-8 -*-
"""输入注入（SendInput）与高精度演奏调度。

设计要点
--------
* 只用 ctypes 调 user32!SendInput，不依赖 pyautogui / pydirectinput；
* 键盘事件走「扫描码」（KEYEVENTF_SCANCODE），DirectInput / Raw Input 的游戏才收得到；
* winmm!timeBeginPeriod(1) 把系统定时精度提到 1ms，再用 perf_counter 自旋补齐，
  否则 BPM 会飘、节奏会散；
* 任何情况下停止 / 异常退出都会无条件松开所有按键与鼠标键，避免卡键；
* 演奏期间绝不动鼠标坐标，否则游戏会转视角。
"""
from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes

from mapping import DEFAULT_SCANCODES, MOUSE_LEFT, MOUSE_MIDDLE, MOUSE_RIGHT

# --------------------------------------------------------------------------- #
# Win32 结构体
# --------------------------------------------------------------------------- #
ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040

MOUSE_FLAGS = {
    (MOUSE_LEFT, True): MOUSEEVENTF_LEFTDOWN,
    (MOUSE_LEFT, False): MOUSEEVENTF_LEFTUP,
    (MOUSE_RIGHT, True): MOUSEEVENTF_RIGHTDOWN,
    (MOUSE_RIGHT, False): MOUSEEVENTF_RIGHTUP,
    (MOUSE_MIDDLE, True): MOUSEEVENTF_MIDDLEDOWN,
    (MOUSE_MIDDLE, False): MOUSEEVENTF_MIDDLEUP,
}


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD),
                ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG),
                ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD),
                ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


# 常用虚拟键码（自定义键位时用）
VK_TABLE = {
    "z": 0x5A, "x": 0x58, "c": 0x43, "v": 0x56,
    "b": 0x42, "n": 0x4E, "m": 0x4D, ",": 0xBC,
}
VK_F5, VK_F6, VK_F7, VK_F8, VK_F9, VK_F10, VK_F11, VK_F12 = (
    0x74, 0x75, 0x76, 0x77, 0x78, 0x79, 0x7A, 0x7B)


# --------------------------------------------------------------------------- #
# 输入发送器
# --------------------------------------------------------------------------- #
class InputSender:
    """把 key_down / key_up / mouse_down / mouse_up 变成真实输入。"""

    def __init__(self, scancodes=None, dry_run: bool = False, logger=None):
        self.scancodes = dict(DEFAULT_SCANCODES)
        if scancodes:
            self.scancodes.update(scancodes)
        self.dry_run = dry_run
        self.logger = logger
        self._held_keys = set()
        self._held_mouse = set()
        self._lock = threading.Lock()
        self._user32 = ctypes.WinDLL("user32", use_last_error=True) if not dry_run else None
        if self._user32 is not None:
            self._user32.SendInput.argtypes = (wintypes.UINT,
                                               ctypes.POINTER(INPUT),
                                               ctypes.c_int)
            self._user32.SendInput.restype = wintypes.UINT
        import atexit
        atexit.register(self.release_all)

    # -- 底层发送 ---------------------------------------------------------- #
    def _send_keyboard(self, key: str, is_up: bool):
        scan = self.scancodes.get(key)
        if scan is None:
            raise KeyError(f"没有为按键 {key!r} 配置扫描码")
        flags = KEYEVENTF_SCANCODE | (KEYEVENTF_KEYUP if is_up else 0)
        inp = INPUT(type=INPUT_KEYBOARD,
                    u=_INPUTUNION(ki=KEYBDINPUT(wVk=0, wScan=scan, dwFlags=flags,
                                                time=0, dwExtraInfo=0)))
        self._user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

    def _send_mouse(self, button: str, is_down: bool):
        flag = MOUSE_FLAGS.get((button, is_down))
        if flag is None:
            raise KeyError(f"未知鼠标键 {button!r}")
        inp = INPUT(type=INPUT_MOUSE,
                    u=_INPUTUNION(mi=MOUSEINPUT(dx=0, dy=0, mouseData=0,
                                                dwFlags=flag, time=0, dwExtraInfo=0)))
        self._user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

    # -- 对外接口 ---------------------------------------------------------- #
    def key_down(self, key: str):
        with self._lock:
            self._held_keys.add(key)
            if self.dry_run:
                self._echo(f"key_down {key}")
                return
            self._send_keyboard(key, False)
            self._echo(f"key_down {key}")

    def key_up(self, key: str):
        with self._lock:
            self._held_keys.discard(key)
            if self.dry_run:
                self._echo(f"key_up   {key}")
                return
            self._send_keyboard(key, True)
            self._echo(f"key_up   {key}")

    def mouse_down(self, button: str):
        with self._lock:
            self._held_mouse.add(button)
            if self.dry_run:
                self._echo(f"mouse_dn {button}")
                return
            self._send_mouse(button, True)
            self._echo(f"mouse_dn {button}")

    def mouse_up(self, button: str):
        with self._lock:
            self._held_mouse.discard(button)
            if self.dry_run:
                self._echo(f"mouse_up {button}")
                return
            self._send_mouse(button, False)
            self._echo(f"mouse_up {button}")

    def dispatch(self, kind: str, arg: str):
        if kind == "key_down":
            self.key_down(arg)
        elif kind == "key_up":
            self.key_up(arg)
        elif kind == "mouse_down":
            self.mouse_down(arg)
        elif kind == "mouse_up":
            self.mouse_up(arg)
        else:
            raise ValueError(f"未知事件类型 {kind!r}")

    def release_all(self):
        """紧急兜底：松开一切。可以在任意线程调用。"""
        with self._lock:
            for key in list(self._held_keys):
                try:
                    if not self.dry_run:
                        self._send_keyboard(key, True)
                except Exception:
                    pass
                self._held_keys.discard(key)
            for button in list(self._held_mouse):
                try:
                    if not self.dry_run:
                        self._send_mouse(button, False)
                except Exception:
                    pass
                self._held_mouse.discard(button)

    def _echo(self, text: str):
        if self.logger:
            self.logger(text)


# --------------------------------------------------------------------------- #
# 定时精度
# --------------------------------------------------------------------------- #
class _TimerResolution:
    def __init__(self):
        self._winmm = None

    def __enter__(self):
        try:
            self._winmm = ctypes.WinDLL("winmm")
            self._winmm.timeBeginPeriod(1)
        except Exception:
            self._winmm = None
        return self

    def __exit__(self, *exc):
        if self._winmm is not None:
            try:
                self._winmm.timeEndPeriod(1)
            except Exception:
                pass
        return False


# --------------------------------------------------------------------------- #
# 演奏器
# --------------------------------------------------------------------------- #
class Player:
    """按绝对时间戳回放事件流。"""

    def __init__(self, sender: InputSender, on_progress=None, on_finish=None):
        self.sender = sender
        self.on_progress = on_progress       # callable(elapsed_ms, total_ms)
        self.on_finish = on_finish           # callable(completed: bool, reason: str)
        self._stop = threading.Event()
        self._thread = None
        self._total_ms = 0.0

    # -- 控制 -------------------------------------------------------------- #
    @property
    def is_playing(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def total_ms(self) -> float:
        return self._total_ms

    def stop(self):
        self._stop.set()
        self.sender.release_all()

    def play(self, events, block: bool = True):
        if self.is_playing:
            raise RuntimeError("已经在演奏中")
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, args=(list(events),),
                                        name="harmonica-player", daemon=True)
        self._thread.start()
        if block:
            self._thread.join()

    # -- 内部 -------------------------------------------------------------- #
    def _run(self, events):
        self._total_ms = events[-1].time if events else 0.0
        completed, reason = False, "播放完毕"
        try:
            with _TimerResolution():
                base = time.perf_counter()
                last_report = -1000.0
                for event in events:
                    if self._stop.is_set():
                        reason = "已手动停止"
                        break
                    target = base + event.time / 1000.0
                    self._sleep_until(target)
                    if self._stop.is_set():
                        reason = "已手动停止"
                        break
                    self.sender.dispatch(event.kind, event.arg)
                    if self.on_progress is not None:
                        elapsed = event.time
                        if elapsed - last_report >= 80:
                            last_report = elapsed
                            try:
                                self.on_progress(elapsed, self._total_ms)
                            except Exception:
                                pass
                else:
                    completed = True
        except Exception as exc:                      # 出任何错都要松键
            reason = f"异常：{exc!r}"
        finally:
            self.sender.release_all()
            if self.on_progress is not None:
                try:
                    self.on_progress(self._total_ms if completed else 0.0, self._total_ms)
                except Exception:
                    pass
            if self.on_finish is not None:
                try:
                    self.on_finish(completed, reason)
                except Exception:
                    pass

    @staticmethod
    def _sleep_until(target: float):
        """先睡后自旋，把误差压到 1ms 以内。"""
        while True:
            remain = target - time.perf_counter()
            if remain <= 0:
                return
            if remain > 0.003:
                time.sleep(remain - 0.0015)
            else:
                time.sleep(0)


# --------------------------------------------------------------------------- #
# 全局热键（轮询 GetAsyncKeyState，全屏游戏里也有效）
# --------------------------------------------------------------------------- #
class HotkeyWatcher(threading.Thread):
    def __init__(self, bindings: dict, interval: float = 0.015):
        super().__init__(name="harmonica-hotkeys", daemon=True)
        self.bindings = dict(bindings)        # {vk: callable}
        self.interval = interval
        self._alive = threading.Event()
        self._alive.set()
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)

    def run(self):
        previous = {vk: False for vk in self.bindings}
        while self._alive.is_set():
            for vk in list(self.bindings):
                pressed = bool(self._user32.GetAsyncKeyState(vk) & 0x8000)
                if pressed and not previous.get(vk):
                    try:
                        self.bindings[vk]()
                    except Exception:
                        pass
                previous[vk] = pressed
            time.sleep(self.interval)

    def shutdown(self):
        self._alive.clear()


def is_admin() -> bool:
    try:
        return bool(ctypes.WinDLL("shell32").IsUserAnAdmin())
    except Exception:
        return False
