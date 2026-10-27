# -*- coding: utf-8 -*-
"""三角洲口琴自动演奏器 —— 图形界面主程序。

用法：导入 MIDI -> 选一条轨道 -> 点开始（或按 F6）-> 切回游戏，倒计时结束后自动演奏。
快捷键：F6 开始 / F8 紧急停止（全屏游戏内也有效）。

只依赖 Python 标准库；用 PyInstaller 打包成单文件 exe（见 build.bat）。
"""
from __future__ import annotations

import ctypes
import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import mapping
import player as player_mod
import smf
import synth

APP_NAME = "三角洲口琴自动演奏器"
VERSION = "0.4.6"

CONFIG_NAME = "config.json"

# 单音合并策略：同一条轨里若有两个声部重叠，怎么降成单声部
MONO_CHOICES = (
    ("自动", "auto"),
    ("取最高音", "highest"),
    ("后起优先", "legato"),
)
MONO_BY_LABEL = dict(MONO_CHOICES)
MONO_LABEL_BY_CODE = {code: label for label, code in MONO_CHOICES}

# 空档填补：主轨没音的地方要不要用另一条轨补上
FILL_CHOICES = (
    ("留白", "none"),
    ("只在开头补", "lead"),
    ("所有空档补", "all"),
)
FILL_BY_LABEL = dict(FILL_CHOICES)
FILL_LABEL_BY_CODE = {code: label for label, code in FILL_CHOICES}

DEFAULT_CONFIG = {
    "speed": 1.0,
    "transpose": 0,
    "countdown": 4,
    "gap_ms": mapping.DEFAULT_GAP_MS,
    "min_hold_ms": mapping.MIN_HOLD_MS,
    "base": mapping.DEFAULT_BASE,
    "auto_fit": True,
    "fold": True,
    "auto_transpose": True,
    "mono": "auto",
    "fill": "none",
    "dry_run": False,
    "last_dir": "",
    "library_dir": "",
}


def config_path() -> str:
    """优先放在 exe 同目录；不可写时退回 %APPDATA%。"""
    base = os.path.dirname(os.path.abspath(sys.argv[0] if getattr(sys, "frozen", False) else __file__))
    try:
        probe = os.path.join(base, ".write_test")
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("ok")
        os.remove(probe)
        return os.path.join(base, CONFIG_NAME)
    except Exception:
        appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
        folder = os.path.join(appdata, "DeltaHarmonicaPlayer")
        os.makedirs(folder, exist_ok=True)
        return os.path.join(folder, CONFIG_NAME)


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(config_path(), "r", encoding="utf-8-sig") as fh:
            cfg.update(json.load(fh) or {})
    except Exception:
        pass
    return cfg


def save_config(cfg: dict):
    try:
        with open(config_path(), "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False, indent=2)
    except Exception:
        pass


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.cfg = load_config()
        self.queue: queue.Queue = queue.Queue()
        self.midi: smf.MidiFile | None = None
        self.midi_path = ""
        self.mapped = []
        self.events = []
        self.stats = {}
        self.library_files = []
        self.sender = player_mod.InputSender(dry_run=bool(self.cfg["dry_run"]),
                                             logger=lambda text: None)
        self.player = player_mod.Player(self.sender,
                                        on_progress=self._on_progress,
                                        on_finish=self._on_finish)
        self.countdown_job = None
        self.countdown_left = 0
        self.hotkeys = None
        self._transpose_cache = {}
        self.spans = []
        self._speed_suggestion = None
        self._wav_ref = None
        self._audio_token = 0

        self._build_ui()
        self._start_hotkeys()
        self.refresh_library()
        self.root.after(60, self._poll_queue)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ UI --
    def _build_ui(self):
        self.root.title(f"{APP_NAME} v{VERSION}")
        # 窗口尺寸按屏幕自适应并居中：小屏 / 高 DPI 缩放时不会超出屏幕被裁掉
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        width = max(900, min(1200, screen_w - 60))
        height = max(560, min(780, screen_h - 90))
        self.root.geometry(f"{width}x{height}+{max(0, (screen_w - width) // 2)}+"
                           f"{max(0, (screen_h - height) // 2 - 20)}")
        # 参数栏本身要 358px 高，加上顶部/日志/状态栏/按钮条约 300px，
        # 窗口低于 ~660 就会把底部按钮裁掉，所以把最小高度卡在这里（同时不超出屏幕）
        self.root.minsize(900, min(660, max(520, screen_h - 60)))

        style = ttk.Style()
        try:
            style.theme_use("vista")
        except Exception:
            pass

        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill="both", expand=True)

        # 权限提示条
        if not player_mod.is_admin():
            tk.Label(outer, bg="#8a2b1f", fg="white", anchor="w", padx=10, pady=6,
                     text="⚠ 当前不是管理员权限：如果游戏以管理员身份运行，"
                          "模拟按键会被系统拦截（UIPI）。请右键 exe → 以管理员身份运行。"
                     ).pack(fill="x", pady=(0, 8))

        # ---- 顶部：当前文件 ----
        file_row = ttk.Frame(outer)
        file_row.pack(fill="x")
        ttk.Button(file_row, text="打开 MIDI 文件…", command=self.open_midi).pack(side="left")
        ttk.Button(file_row, text="使用说明 / 名词解释",
                   command=self.show_help).pack(side="left", padx=6)
        self.file_label = ttk.Label(file_row, foreground="#555",
                                    text="（还没打开文件 —— 可以从下面的「曲库」标签页双击选一首）")
        self.file_label.pack(side="left", padx=10)

        # ---- 底部三件套：先固定住，保证日志永远看得见 ----
        log_frame = ttk.LabelFrame(outer, text="日志（预览指法、解析结果都写在这里）", padding=4)
        log_frame.pack(side="bottom", fill="x")
        self.log_text = tk.Text(log_frame, height=8, wrap="none", font=("Consolas", 9),
                                state="disabled", bg="#101418", fg="#d6e2ea")
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        log_scroll.pack(side="right", fill="y")

        self.status = ttk.Label(outer, text="就绪", anchor="w", foreground="#0a5")
        self.status.pack(side="bottom", fill="x", pady=(4, 2))

        control = ttk.Frame(outer)
        control.pack(side="bottom", fill="x", pady=(2, 4))
        self.btn_start = ttk.Button(control, text="▶ 开始演奏 (F6)", command=self.start)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(control, text="■ 紧急停止 (F8)", command=self.stop,
                                   state="disabled")
        self.btn_stop.pack(side="left", padx=6)
        self.btn_audio = ttk.Button(control, text="试听本段", command=self._toggle_audio,
                                    state="disabled")
        self.btn_audio.pack(side="left")
        self.btn_preview = ttk.Button(control, text="预览指法", command=self.preview_mapping)
        self.btn_preview.pack(side="left", padx=6)
        self.btn_speed = ttk.Button(control, text="变速建议", command=self._apply_speed_suggestion,
                                    state="disabled")
        self.btn_speed.pack(side="left")
        self.progress = ttk.Progressbar(control, mode="determinate", maximum=1000)
        self.progress.pack(side="left", fill="x", expand=True, padx=10)

        # ---- 段落（在按钮条上方）----
        seg_row = ttk.Frame(outer)
        seg_row.pack(side="bottom", fill="x", pady=(4, 0))
        ttk.Label(seg_row, text="段落").pack(side="left")
        self.var_seg_start = tk.DoubleVar(value=0.0)
        self.var_seg_end = tk.DoubleVar(value=0.0)
        self.spin_seg_start = ttk.Spinbox(seg_row, from_=0.0, to=0.0, increment=1.0, width=8,
                                          textvariable=self.var_seg_start,
                                          command=self._on_segment_changed)
        self.spin_seg_start.pack(side="left", padx=(4, 2))
        ttk.Label(seg_row, text="～").pack(side="left")
        self.spin_seg_end = ttk.Spinbox(seg_row, from_=0.0, to=0.0, increment=1.0, width=8,
                                        textvariable=self.var_seg_end,
                                        command=self._on_segment_changed)
        self.spin_seg_end.pack(side="left", padx=(2, 2))
        ttk.Label(seg_row, text="秒").pack(side="left")
        for spin in (self.spin_seg_start, self.spin_seg_end):
            spin.bind("<Return>", lambda _e: self._on_segment_changed())
            spin.bind("<FocusOut>", lambda _e: self._on_segment_changed())
        ttk.Button(seg_row, text="全曲", width=5,
                   command=self._set_full_segment).pack(side="left", padx=(8, 2))
        ttk.Button(seg_row, text="前 30 秒", width=8,
                   command=lambda: self._set_quick_segment(30)).pack(side="left", padx=2)
        ttk.Button(seg_row, text="后 30 秒", width=8,
                   command=lambda: self._set_quick_segment(-30)).pack(side="left", padx=2)
        self.seg_label = ttk.Label(seg_row, text="", foreground="#555")
        self.seg_label.pack(side="left", padx=10)

        # ---- 主体：右侧参数栏先 pack，保证窗口变小时也不会被裁掉 ----
        body = ttk.Frame(outer)
        body.pack(fill="both", expand=True, pady=(10, 4))

        right = ttk.LabelFrame(body, text="演奏参数", padding=8)
        right.pack(side="right", fill="y", padx=(10, 0))

        self.var_speed = tk.DoubleVar(value=float(self.cfg["speed"]))
        self.var_transpose = tk.IntVar(value=int(self.cfg["transpose"]))
        self.var_countdown = tk.IntVar(value=int(self.cfg["countdown"]))
        self.var_gap = tk.DoubleVar(value=float(self.cfg["gap_ms"]))
        self.var_minhold = tk.DoubleVar(value=float(self.cfg["min_hold_ms"]))
        self.var_base = tk.IntVar(value=int(self.cfg["base"]))
        self.var_autofit = tk.BooleanVar(value=bool(self.cfg["auto_fit"]))
        self.var_fold = tk.BooleanVar(value=bool(self.cfg["fold"]))
        self.var_autotranspose = tk.BooleanVar(value=bool(self.cfg.get("auto_transpose", True)))
        self.var_dry = tk.BooleanVar(value=bool(self.cfg["dry_run"]))

        row = 0
        row = self._spin_row(right, row, "速度（倍）", self.var_speed, 0.5, 1.5, 0.05, "0.5～1.5")
        row = self._spin_row(right, row, "移调（半音）", self.var_transpose, -12, 12, 1, "手动")
        row = self._spin_row(right, row, "倒计时（秒）", self.var_countdown, 1, 10, 1, "切回游戏")
        row = self._spin_row(right, row, "音符间隔 ms", self.var_gap, 0, 60, 2, "防糊")
        row = self._spin_row(right, row, "最短按住 ms", self.var_minhold, 20, 200, 5, "太短会被吞")
        row = self._spin_row(right, row, "基准音 P0", self.var_base, 48, 72, 1, "一般不改")

        for text, var, command in (
                ("自动整体适配八度", self.var_autofit, self._rebuild_mapping),
                ("超范围音八度折叠", self.var_fold, self._rebuild_mapping),
                ("自动选最佳移调", self.var_autotranspose, self._rebuild_mapping),
                ("干跑测试（不发按键）", self.var_dry, self._on_dry_toggle)):
            ttk.Checkbutton(right, text=text, variable=var, command=command).grid(
                row=row, column=0, columnspan=3, sticky="w")
            row += 1

        ttk.Label(right, justify="left", foreground="#777", font=("Microsoft YaHei UI", 8),
                  text="键位：Z X C V B N M ，\n"
                       "鼠标左键=降八度  右键=升八度  中键=升半音"
                  ).grid(row=row, column=0, columnspan=3, sticky="w", pady=(8, 0))

        # ---- 左：曲库 / 轨道（占剩余空间）----
        left = ttk.Frame(body)
        left.pack(side="left", fill="both", expand=True)
        self.notebook = ttk.Notebook(left)
        self.notebook.pack(fill="both", expand=True)
        self._build_library_tab()
        self._build_track_tab()

        self.log(f"{APP_NAME} v{VERSION} 已启动")
        self.log("用法：从「曲库」双击一首曲子 → 挑一条轨道 → 点开始（F6）→ 切回游戏")
        if not player_mod.is_admin():
            self.log("⚠ 未以管理员身份运行，游戏可能收不到模拟按键。")

    # ------------------------------------------------------- 曲库 / 轨道页 --
    def _build_library_tab(self):
        tab = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(tab, text="  曲库  ")
        self.library_tab = tab

        row = ttk.Frame(tab)
        row.pack(fill="x")
        ttk.Button(row, text="选择曲库文件夹…", command=self.choose_library).pack(side="left")
        ttk.Button(row, text="刷新", command=self.refresh_library).pack(side="left", padx=4)
        ttk.Button(row, text="在资源管理器里打开",
                   command=self.open_library_folder).pack(side="left")
        self.library_label = ttk.Label(row, text="", foreground="#555")
        self.library_label.pack(side="left", padx=10)

        wrap = ttk.Frame(tab)
        wrap.pack(fill="both", expand=True, pady=(8, 0))
        self.library_list = tk.Listbox(wrap, height=12, exportselection=False,
                                       activestyle="dotbox", font=("Microsoft YaHei UI", 10))
        scroll = ttk.Scrollbar(wrap, orient="vertical", command=self.library_list.yview)
        self.library_list.configure(yscrollcommand=scroll.set)
        self.library_list.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.library_list.bind("<Double-Button-1>", self._open_library_item)
        self.library_list.bind("<Return>", self._open_library_item)
        ttk.Label(tab, foreground="#777",
                  text="双击曲目载入（也可以按回车）。曲库文件夹会记住，下次打开还在。"
                  ).pack(anchor="w", pady=(6, 0))

    def _build_track_tab(self):
        tab = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(tab, text="  轨道  ")
        self.track_tab = tab

        head = ttk.Frame(tab)
        head.pack(fill="x")
        ttk.Label(head, foreground="#555",
                  text="一个 MIDI 里有多条轨道，选一条来吹（一般选主旋律）"
                  ).pack(side="left")
        ttk.Button(head, text="这些是什么？", command=self.show_help).pack(side="right")

        self.var_fill = tk.StringVar(
            value=FILL_LABEL_BY_CODE.get(self.cfg.get("fill", "none"), FILL_CHOICES[0][0]))
        self.fill_box = ttk.Combobox(head, state="readonly", width=12,
                                     values=[label for label, _code in FILL_CHOICES],
                                     textvariable=self.var_fill)
        self.fill_box.pack(side="right", padx=(4, 8))
        self.fill_box.bind("<<ComboboxSelected>>", lambda _e: self._on_fill_changed())
        ttk.Label(head, text="空档填补：").pack(side="right")

        self.var_mono = tk.StringVar(
            value=MONO_LABEL_BY_CODE.get(self.cfg.get("mono", "auto"), MONO_CHOICES[0][0]))
        self.mono_box = ttk.Combobox(head, state="readonly", width=10,
                                     values=[label for label, _code in MONO_CHOICES],
                                     textvariable=self.var_mono)
        self.mono_box.pack(side="right", padx=(4, 8))
        self.mono_box.bind("<<ComboboxSelected>>", lambda _e: self._on_mono_changed())
        ttk.Label(head, text="单音合并：").pack(side="right")

        wrap = ttk.Frame(tab)
        wrap.pack(fill="both", expand=True, pady=(8, 0))
        self.track_list = tk.Listbox(wrap, height=12, activestyle="dotbox",
                                     exportselection=False, font=("Consolas", 10))
        scroll = ttk.Scrollbar(wrap, orient="vertical", command=self.track_list.yview)
        self.track_list.configure(yscrollcommand=scroll.set)
        self.track_list.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.track_list.bind("<<ListboxSelect>>", lambda _e: self._rebuild_mapping())

        self.track_info = ttk.Label(tab, text="请先载入一首曲子", foreground="#333",
                                    justify="left")
        self.track_info.pack(fill="x", pady=(8, 0))

    def choose_library(self):
        folder = filedialog.askdirectory(title="选择存放 MIDI 的文件夹",
                                         initialdir=self.cfg.get("library_dir") or None)
        if not folder:
            return
        self.cfg["library_dir"] = folder
        save_config(self.cfg)
        self.refresh_library()

    def refresh_library(self):
        folder = self.cfg.get("library_dir") or ""
        self.library_files = []
        self.library_list.delete(0, "end")
        if not folder or not os.path.isdir(folder):
            self.library_label.configure(text="还没设置曲库文件夹")
            return
        exts = (".mid", ".midi", ".kar", ".rmi")
        found = []
        for root, dirs, names in os.walk(folder):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for name in names:
                if os.path.splitext(name)[1].lower() in exts:
                    found.append(os.path.join(root, name))
        found.sort(key=lambda p: os.path.basename(p).lower())
        self.library_files = found
        for path in found:
            self.library_list.insert("end", os.path.relpath(path, folder))
        self.library_label.configure(text=f"{folder}    共 {len(found)} 首")
        self.log(f"曲库已刷新：{len(found)} 首（{folder}）")

    def open_library_folder(self):
        folder = self.cfg.get("library_dir") or ""
        if folder and os.path.isdir(folder):
            os.startfile(folder)
        else:
            messagebox.showinfo(APP_NAME, "还没设置曲库文件夹")

    def _open_library_item(self, _event=None):
        selection = self.library_list.curselection()
        if not selection:
            return
        index = int(selection[0])
        if 0 <= index < len(self.library_files):
            self.load_path(self.library_files[index])

    # ------------------------------------------------------------- 说明 ----
    HELP_TEXT = """【这个程序在做什么】
把 MIDI 文件里的音符，翻译成《三角洲行动》游戏口琴的按键，然后自动吹出来。

【「轨道」是什么】
一个 MIDI 文件就像一本总谱，里面常常分成好几条「轨道」，
每条轨道是一种乐器或一个声部：主旋律、伴奏、低音、鼓……
游戏口琴同一时刻只能发出一个音，所以你只能挑一条轨道来吹 —— 一般挑主旋律。

【轨道列表里的数字怎么读】
    [1] 未命名轨道   197→196   吃音1   折叠1
     · [1]      这条轨道的编号（命令行 --track 1 用的就是它）
     · 197      这条轨道原始有 197 个音符
     · →196     真正能吹出来的有 196 个
     · 吃音1    有 1 个音和别的音重叠了，口琴同时只能发一个音，所以被吃掉
     · 折叠1    有 1 个音超出游戏音域（太高或太低），被挪到了最近的八度
     · 移调+12  整条轨道被整体挪了 12 个半音（一个八度）才落进音域
     · [打击乐] 鼓点轨，没有音高，吹不出来，程序会自动跳过

【「空轨」是什么】
很多 MIDI 会把曲名、版权、速度信息放在第 0 轨，它只有文字没有音符，
显示为「空轨」是正常的，不用选它。

【怎么选轨道】
程序已经按「真能吹出多少音」自动帮你选好了（通常就是主旋律轨）。
想换：点列表里的另一条，下面的信息栏和日志会立刻重新计算。

【键位（游戏默认）】
    键盘    Z  X  C  V  B  N  M  ,
    音级    1  2  3  4  5  6  7  1̇
    鼠标左键按住 = 降八度      鼠标右键按住 = 升八度
    鼠标中键按住 = 升半音      （演奏期间不动鼠标坐标，不会转视角）
 如果你改过游戏里的键位，需要同步改 mapping.py / player.py 里的 DEFAULT_SCANCODES。

【演奏参数】
 · 速度        0.5 = 半速，1.0 = 原速。曲子太密就调慢，短音也能救回来一些。
 · 移调        手动整体升降调（半音）。开了「自动选最佳移调」时以自动的为准。
 · 倒计时      点开始后留给你切回游戏、拿出口琴的时间。
 · 音符间隔    相邻音之间留的空隙（毫秒），听着糊在一起就调大。
 · 最短按住    一个音最少按住多久（毫秒），太短游戏会吞掉。
 · 基准音 P0   游戏里中音「1」对应的 MIDI 音高，键位没改过就别动。
 · 自动整体适配八度   整首歌偏高或偏低时，整体挪一个八度。
 · 超范围音八度折叠   个别超出音域的音挪到最近的八度；关掉就直接丢弃。
 · 自动选最佳移调     在 -6～+6 半音里挑一个，让「调外音」最少（少按鼠标中键）。
 · 干跑测试          只写日志、不真的发按键，用来先检查指法对不对。

【轨道页的两个下拉框】
 · 单音合并：一条轨里有多个声部重叠时，怎么降成一个音（口琴只能发一个音）。
     · 自动      两种都算，挑跳进更规整、保音更多的那个
     · 取最高音  更厚，会在两个声部之间跳（钢琴/和弦轨通常用这个）
     · 后起优先  更干净，只跟一条线走，一个音都不丢（人声/独奏用这个）
 · 空档填补：主轨没出声的地方要不要用另一条轨补上。
     · 留白          只吹当前这条轨
     · 只在开头补    **只把前奏那段补上**，人声一开始就完全按主轨走（推荐）
     · 所有空档补    连中间的呼吸口、间奏一起补
   补的时候会自动挑一条音区最接近、音符最多的轨，只往空档里塞，绝不盖住主旋律。
   比如《毕业歌》主轨要 7 秒才进，选「只在开头补」就能用钢琴把前奏弹出来。

【试听 / 段落 / 变速建议】
 · 试听本段   把当前轨道（或当前段落）合成成音频直接播，**只出声、不发送任何按键**，
               所以可以放心在游戏外先听一遍。听到的 = 待会儿游戏里会吹出来的。
 · 段落       一首 4 分钟的曲子不必整首吹。底部填起止秒数，或用「前 30 秒 / 后 30 秒」，
               试听和演奏都只处理这一段；跨边界的音会被截断而不是整段丢。
 · 变速建议   有大量「时值太短、游戏收不到」的音时，按钮会直接显示建议值（如 0.75x），
               点一下应用。它按「救回一半过短音」计算，不会为了几个音把整首拖成慢动作。

【快捷键】
 · F6 开始演奏    · F8 紧急停止（全屏游戏里也有效）

【提示】
 · 必须先以管理员身份运行，否则游戏收不到模拟按键。
 · 建议先拿一首短的（比如 demo 里的《小星星》）进游戏试一次，确认键位对得上。
"""

    def show_help(self):
        window = tk.Toplevel(self.root)
        window.title(f"{APP_NAME} · 使用说明")
        window.geometry("780x620")
        window.transient(self.root)
        frame = ttk.Frame(window, padding=10)
        frame.pack(fill="both", expand=True)
        text = tk.Text(frame, wrap="word", font=("Microsoft YaHei UI", 10),
                       bg="#fbfbfb", relief="flat")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        text.insert("1.0", self.HELP_TEXT)
        text.configure(state="disabled")
        ttk.Button(window, text="知道了", command=window.destroy).pack(pady=(0, 10))
        window.bind("<Escape>", lambda _e: window.destroy())

    def _spin_row(self, parent, row, label, var, lo, hi, step, tip):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=2)
        spin = ttk.Spinbox(parent, from_=lo, to=hi, increment=step, textvariable=var, width=7,
                           command=self._rebuild_mapping)
        spin.grid(row=row, column=1, sticky="w", padx=(8, 6))
        spin.bind("<Return>", lambda _e: self._rebuild_mapping())
        spin.bind("<FocusOut>", lambda _e: self._rebuild_mapping())
        ttk.Label(parent, text=tip, foreground="#777").grid(row=row, column=2, sticky="w")
        return row + 1

    # ------------------------------------------------------------- 热键 ----
    def _start_hotkeys(self):
        self.hotkeys = player_mod.HotkeyWatcher({
            player_mod.VK_F6: lambda: self.queue.put(("hotkey", "start")),
            player_mod.VK_F8: lambda: self.queue.put(("hotkey", "stop")),
        })
        self.hotkeys.start()

    # ------------------------------------------------------------- 文件 ----
    def open_midi(self):
        path = filedialog.askopenfilename(
            title="选择 MIDI 文件",
            initialdir=self.cfg.get("last_dir") or self.cfg.get("library_dir") or None,
            filetypes=[("MIDI 文件", "*.mid *.midi *.kar *.rmi"), ("所有文件", "*.*")])
        if path:
            self.load_path(path)

    def load_path(self, path):
        self.cfg["last_dir"] = os.path.dirname(path)
        save_config(self.cfg)
        self.file_label.configure(text=path, foreground="#333")
        self.log(f"正在解析：{path}")
        self.set_status("正在解析并体检各条轨道…", "#c60")
        threading.Thread(target=self._load_worker,
                         args=(path, int(self.var_base.get()), self._mono_mode()),
                         daemon=True).start()

    def _mono_mode(self):
        """当前选的单音合并策略（auto / highest / legato）。"""
        try:
            return MONO_BY_LABEL.get(self.var_mono.get(), "auto")
        except Exception:
            return "auto"

    def _on_mono_changed(self):
        self.cfg["mono"] = self._mono_mode()
        save_config(self.cfg)
        label = { "auto": "自动", "highest": "取最高音", "legato": "后起优先"}[self._mono_mode()]
        self.log(f"单音合并策略：{label}")
        self._rebuild_mapping()

    def _fill_mode(self):
        """空档填补模式：none（留白）/ lead（只补开头）/ all（所有空档）。"""
        try:
            value = FILL_BY_LABEL.get(self.var_fill.get(), "none")
        except Exception:
            value = "none"
        return "all" if value == "other" else value      # 兼容旧配置

    def _on_fill_changed(self):
        self.cfg["fill"] = self._fill_mode()
        save_config(self.cfg)
        label = {"none": "留白", "lead": "只在开头补", "all": "所有空档补"}[self._fill_mode()]
        self.log(f"空档填补：{label}")
        self._rebuild_mapping()

    def _load_worker(self, path, base, mono):
        try:
            with open(path, "rb") as fh:
                data = fh.read()
            midi = smf.parse_midi(data, name=os.path.basename(path))
            stats = {}
            for track in midi.tracks:
                stats[track.index] = mapping.analyse_track(track, base=base, mono=mono)
            best = mapping.pick_best_track(midi.tracks, base=base, mono=mono)
            self.queue.put(("loaded", (path, midi, stats, best.index if best else None)))
        except Exception as exc:
            self.queue.put(("error", f"解析失败：{exc}"))

    def _on_loaded(self, path, midi: smf.MidiFile, stats=None, best_index=None):
        self.midi, self.midi_path = midi, path
        stats = stats or {}
        self._transpose_cache = {}
        self.track_list.delete(0, "end")
        for track in midi.tracks:
            info = stats.get(track.index)
            name = (track.name or "未命名轨道").strip()
            if len(name) > 18:
                name = name[:17] + "…"
            if not track.notes:
                line = f"[{track.index}] {name}  空轨（只有曲名/版权信息，不用选）"
            elif info is None:
                line = f"[{track.index}] {name}  {len(track.notes)} 音（无法映射）"
            else:
                line = f"[{track.index}] {name}  {len(track.notes)}→{info['playable']}"
                if info["lost"]:
                    line += f"  吃音{info['lost']}"
                if info["folded"]:
                    line += f"  折叠{info['folded']}"
                if info["global_shift"]:
                    line += f"  移调{info['global_shift']:+d}"
                if track.is_drum:
                    line += "  [打击乐]"
            # 统一两字符前缀，顺手标出程序推荐的那条
            line = ("★ " if track.index == best_index else "  ") + line
            self.track_list.insert("end", line)
        self.log(f"解析成功：{midi.track_count} 条轨道，format {midi.format}，"
                 f"总长 {midi.length_ms / 1000:.1f}s")
        if best_index is None and midi.tracks:
            best_index = smf.pick_melody_track(midi).index
        if best_index is not None:
            self.track_list.selection_clear(0, "end")
            self.track_list.selection_set(best_index)
            self.track_list.see(best_index)
            info = stats.get(best_index) or {}
            self.log(f"已自动选中轨道 [{best_index}] "
                     f"{midi.tracks[best_index].name or '未命名轨道'}："
                     f"可吹 {info.get('playable', '?')} 音（按「真吹得出来多少音」挑的）")
        try:
            self.notebook.select(self.track_tab)      # 载入完自动切到轨道页
        except Exception:
            pass
        # 段落默认整曲；载入新曲子后重新给上下限
        total = round(midi.length_ms / 1000.0, 1)
        for spin in (self.spin_seg_start, self.spin_seg_end):
            spin.configure(to=total)
        self.var_seg_start.set(0.0)
        self.var_seg_end.set(total)
        self.btn_audio.configure(state="normal")
        self.set_status("就绪：确认轨道后点「开始演奏」，倒计时里切回游戏", "#0a5")
        self._rebuild_mapping()

    # ------------------------------------------------------------- 映射 ----
    def _selected_track(self):
        if self.midi is None:
            return None
        selection = self.track_list.curselection()
        if not selection:
            return None
        index = int(selection[0])
        if index >= len(self.midi.tracks):
            return None
        return self.midi.tracks[index]

    def _effective_transpose(self, track, base, auto_fit, fold):
        """返回 (实际移调量, 是否为自动选的)。自动移调结果按轨道缓存，避免每次改参数都重算。"""
        manual = int(self.var_transpose.get())
        if not self.var_autotranspose.get():
            return manual, False
        key = (track.index, base, auto_fit, fold)
        if key not in self._transpose_cache:
            self._transpose_cache[key] = mapping.best_transpose(track.notes, base=base)
        return self._transpose_cache[key], True

    def _rebuild_mapping(self):
        track = self._selected_track()
        if track is None:
            self.mapped, self.events, self.stats = [], [], {}
            self.track_info.configure(text="请选择一条轨道")
            return
        if not track.notes:
            self.mapped, self.events, self.stats = [], [], {}
            self.track_info.configure(
                text="这条轨道没有音符（多半只是曲名 / 版权信息），不用选它。\n"
                     "请在上面点一条带数字的轨道 —— 带 ★ 的是程序推荐的那条。")
            return
        try:
            base = int(self.var_base.get())
            auto_fit = bool(self.var_autofit.get())
            fold = bool(self.var_fold.get())
            transpose, is_auto = self._effective_transpose(track, base, auto_fit, fold)
            speed = float(self.var_speed.get())
            mono = self._mono_mode()
            primary_only = mapping.monophonic(track.notes, mono)
            source_notes = primary_only
            fill_line = ""
            fill_mode = self._fill_mode()
            if fill_mode != "none" and self.midi is not None:
                fill_track = mapping.pick_fill_track(self.midi.tracks, track)
                if fill_track is None:
                    fill_line = "空档填补：没有音区接近的轨道可用（换一条主轨试试）"
                else:
                    source_notes, added = mapping.merge_tracks(
                        track.notes, fill_track.notes, mono=mono, scope=fill_mode)
                    scope_tag = "仅开头" if fill_mode == "lead" else "全部空档"
                    fill_line = (f"空档填补（{scope_tag}）：用 [{fill_track.index}] "
                                 f"{(fill_track.name or '未命名轨道').strip()} "
                                 f"补入 {added} 个音")
            result = mapping.map_notes(source_notes, base=base, transpose=transpose,
                                       auto_fit=auto_fit, fold=fold, mono=mono)
            notes = result.notes
            seg_start, seg_end = self._segment_range()
            if seg_start > 0 or seg_end > 0:
                notes = mapping.slice_mapped(notes, seg_start, seg_end)
            spans, skipped = mapping.note_spans(
                notes,
                gap_ms=float(self.var_gap.get()),
                min_hold_ms=float(self.var_minhold.get()),
                speed=speed)
            spans = mapping.trim_lead(spans)      # 裁掉开头的静音，倒计时结束立刻出声
            events = mapping.events_from_spans(spans)
            events = mapping.scale_events(events, speed)
        except Exception as exc:
            self.mapped, self.events, self.spans = [], [], []
            self.track_info.configure(text=f"映射失败：{exc}")
            return

        self.mapped, self.events, self.spans = notes, events, spans
        duration = events[-1].time if events else 0.0
        count = len(notes)
        sharps = sum(1 for item in notes if item.candidate.sharp)
        shift_notes = sum(1 for i in range(1, count)
                          if notes[i].candidate.shift != notes[i - 1].candidate.shift)
        suggestion = mapping.recommend_speed(notes)
        self._speed_suggestion = suggestion
        if suggestion:
            self.btn_speed.configure(
                text=f"建议 {suggestion['speed']:.2f}x", state="normal")
        else:
            self.btn_speed.configure(text="变速建议", state="disabled")

        self.stats = {"dropped": result.dropped, "folded": result.folded, "skipped": skipped,
                      "lost": result.lost, "duration_ms": duration, "transpose": transpose,
                      "auto_transpose": is_auto, "global_shift": result.global_shift,
                      "sharps": sharps, "shift_switch": shift_notes,
                      "segment": (seg_start, seg_end), "suggestion": suggestion}
        tag = "自动" if is_auto else "手动"
        mono_tag = "后起优先（一个不丢）" if result.mono == "legato" else "取最高音"
        hint = ""
        if suggestion:
            hint = (f"\n过短音：原速会跳过 {suggestion['short']} 个，"
                    f"按「建议 {suggestion['speed']:.2f}x」能救回 {suggestion['rescued']} 个")
        raw_text = str(len(track.notes))
        if len(source_notes) != len(primary_only):
            raw_text = f"{len(track.notes)}（主轨单声部 {len(primary_only)} 音，" \
                       f"填补后 {len(source_notes)}）"
        self.track_info.configure(text=(
            f"可吹 {count} / 原始 {raw_text}    吃音 {result.lost}    "
            f"折叠 {result.folded}    丢弃 {result.dropped}    过短跳过 {skipped}\n"
            f"演奏时长约 {duration / 1000:.1f}s    移调 {transpose:+d}（{tag}）    "
            f"整轨八度 {result.global_shift:+d}    八度切换 {shift_notes} 次    "
            f"中键半音 {sharps} 个（{sharps / max(1, count) * 100:.0f}%）\n"
            f"单音合并：{mono_tag}" + (f"\n{fill_line}" if fill_line else "") + hint))
        self._update_segment_label()

    def preview_mapping(self):
        self._rebuild_mapping()
        if not self.mapped:
            self.set_status("没有可预览的内容：请先载入一首曲子并选中一条有音符的轨道", "#c60")
            self.log("没有可预览的内容")
            return
        self.log("── 指法预览（前 16 个音，时间 / 原始音高 / 实际音高 / 修饰键 / 按键）" + "─" * 10)
        for line in mapping.describe_mapping(self.mapped, limit=16).splitlines():
            self.log("  " + line)
        if self.stats:
            self.log(f"  统计：可吹 {len(self.mapped)}，吃音 {self.stats.get('lost')}，"
                     f"折叠 {self.stats.get('folded')}，过短跳过 {self.stats.get('skipped')}，"
                     f"移调 {self.stats.get('transpose'):+d}")
        self.set_status(f"指法预览已输出到下方日志（整首共 {len(self.mapped)} 个音，只列了前 16 个）",
                        "#0a5")

    # ------------------------------------------------- 段落 / 试听 / 变速 ----
    def _segment_range(self):
        """返回 (起点 ms, 终点 ms)；整曲时返回 (0, 0)。"""
        if self.midi is None:
            return (0.0, 0.0)
        total = self.midi.length_ms
        try:
            start = float(self.var_seg_start.get()) * 1000.0
            end = float(self.var_seg_end.get()) * 1000.0
        except Exception:
            return (0.0, 0.0)
        start = max(0.0, min(start, total))
        end = min(end, total) if end > 0 else total
        if end <= start:
            return (0.0, 0.0)
        if start <= 0.5 and end >= total - 1.0:
            return (0.0, 0.0)
        return (start, end)

    def _on_segment_changed(self):
        self._rebuild_mapping()

    def _set_full_segment(self):
        if self.midi is None:
            return
        self.var_seg_start.set(0.0)
        self.var_seg_end.set(round(self.midi.length_ms / 1000.0, 1))
        self._rebuild_mapping()

    def _set_quick_segment(self, seconds):
        if self.midi is None:
            return
        total = self.midi.length_ms / 1000.0
        if seconds >= 0:
            start, end = 0.0, min(total, float(seconds))
        else:
            start, end = max(0.0, total + seconds), total
        self.var_seg_start.set(round(start, 1))
        self.var_seg_end.set(round(end, 1))
        self._rebuild_mapping()

    def _update_segment_label(self):
        if self.midi is None:
            self.seg_label.configure(text="")
            return
        total = self.midi.length_ms / 1000.0
        start, end = self._segment_range()
        if start == 0 and end == 0:
            self.seg_label.configure(text=f"整曲 {total:.1f}s")
        else:
            self.seg_label.configure(
                text=f"选中 {start / 1000:.1f}–{end / 1000:.1f}s"
                     f"（{(end - start) / 1000:.1f}s / 共 {total:.1f}s）")

    # ---- 试听：只出声，不发送任何按键 ----
    def _build_preview_wav(self):
        if not self.spans:
            return b""
        return synth.build_wav(self.spans, speed=float(self.var_speed.get()))

    def _toggle_audio(self):
        if self._wav_ref is not None:
            self._stop_audio()
            return
        if not self.spans:
            self.set_status("没有可试听的内容：先选一条有音符的轨道", "#c60")
            return
        self.btn_audio.configure(state="disabled", text="合成中…")
        self.set_status("正在合成试听…（只出声，不会发送按键）", "#c60")
        threading.Thread(target=self._audio_worker, daemon=True).start()

    def _audio_worker(self):
        try:
            self.queue.put(("audio", ("ready", self._build_preview_wav())))
        except Exception as exc:
            self.queue.put(("audio", ("error", repr(exc))))

    def _on_audio_ready(self, data):
        if not data:
            self.btn_audio.configure(state="normal", text="试听本段")
            self.set_status("这一段没有可发声的音（可能全被跳过了）", "#c60")
            return
        try:
            path = synth.play_wav(data)
        except Exception as exc:
            self.btn_audio.configure(state="normal", text="试听本段")
            self.set_status(f"试听播放失败：{exc}", "#c00")
            self.log(f"试听播放失败：{exc}")
            return
        self._audio_token += 1
        token = self._audio_token
        self._wav_ref = path          # 当前正在播的临时文件
        ms = synth.wav_duration_ms(data)
        self.btn_audio.configure(state="normal", text="停止试听")
        self.set_status(f"正在试听 {ms / 1000:.1f}s（只出声，不会发送按键）", "#0a5")
        self.log(f"试听开始：{ms / 1000:.1f}s")
        self.root.after(int(ms) + 400, lambda: self._finish_audio(token))

    def _finish_audio(self, token):
        if token != self._audio_token or self._wav_ref is None:
            return
        self._wav_ref = None
        synth.cleanup_temp()
        self.btn_audio.configure(text="试听本段",
                                 state="normal" if self.midi else "disabled")
        self.set_status("试听结束", "#0a5")

    def _stop_audio(self):
        self._audio_token += 1
        synth.stop()
        synth.cleanup_temp()
        self._wav_ref = None
        try:
            self.btn_audio.configure(text="试听本段",
                                     state="normal" if self.midi else "disabled")
        except Exception:
            pass

    # ---- 变速建议 ----
    def _apply_speed_suggestion(self):
        if not self._speed_suggestion:
            return
        speed = self._speed_suggestion["speed"]
        self.var_speed.set(speed)
        self.log(f"已应用变速建议 {speed:.2f}x：原速会跳过 "
                 f"{self._speed_suggestion['short']} 个过短音，"
                 f"现在能救回 {self._speed_suggestion['rescued']} 个")
        self._rebuild_mapping()
        self.set_status(f"已把速度设为 {speed:.2f}x", "#0a5")

    # ------------------------------------------------------------- 演奏 ----
    def start(self):
        if self.player.is_playing:
            self.log("已经在演奏中")
            return
        if self.midi is None:
            messagebox.showinfo(APP_NAME, "请先打开一个 MIDI 文件")
            return
        self._stop_audio()
        self._rebuild_mapping()
        if not self.events:
            messagebox.showwarning(APP_NAME, "这条轨道没有可演奏的音符")
            return
        self.sender.dry_run = bool(self.var_dry.get())
        self.countdown_left = int(self.var_countdown.get())
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.progress.configure(value=0)
        self._countdown_tick()

    def _countdown_tick(self):
        if self.countdown_left > 0:
            self.set_status(f"倒计时 {self.countdown_left} 秒 —— 现在切回游戏并拿出口琴！", "#c60")
            self.countdown_left -= 1
            self.countdown_job = self.root.after(1000, self._countdown_tick)
            return
        self.countdown_job = None
        mode = "（干跑测试：不会发送按键）" if self.sender.dry_run else ""
        self.set_status(f"演奏中…{mode}", "#0a5")
        self.log(f"开始演奏：{len(self.events)} 个事件，"
                 f"约 {(self.events[-1].time / 1000):.1f}s {mode}")
        try:
            self.player.play(self.events, block=False)
        except Exception as exc:
            self.log(f"启动失败：{exc}")
            self._on_finish(False, str(exc))

    def stop(self):
        if self.countdown_job is not None:
            self.root.after_cancel(self.countdown_job)
            self.countdown_job = None
            self.log("已取消倒计时")
        if self.player.is_playing:
            self.player.stop()
            self.log("已发送停止指令")
        self._stop_audio()
        self.sender.release_all()
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        self.set_status("已停止", "#c60")

    def _on_progress(self, elapsed, total):
        self.queue.put(("progress", (elapsed, total)))

    def _on_finish(self, completed, reason):
        self.queue.put(("finished", (completed, reason)))

    def _on_dry_toggle(self):
        self.sender.dry_run = bool(self.var_dry.get())
        self.log(f"干跑测试：{'开' if self.sender.dry_run else '关'}")

    # ------------------------------------------------------------- 队列 ----
    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                elif kind == "loaded":
                    self._on_loaded(*payload)
                elif kind == "error":
                    self.log(payload)
                    self.set_status(payload, "#c00")
                elif kind == "progress":
                    elapsed, total = payload
                    if total > 0:
                        self.progress.configure(value=max(0, min(1000, elapsed / total * 1000)))
                elif kind == "finished":
                    completed, reason = payload
                    self.btn_start.configure(state="normal")
                    self.btn_stop.configure(state="disabled")
                    self.progress.configure(value=1000 if completed else 0)
                    self.set_status(("演奏完成 ✅" if completed else f"已结束：{reason}"),
                                    "#0a5" if completed else "#c60")
                    self.log(("演奏完成 ✅ " if completed else "演奏结束：") + reason)
                elif kind == "audio":
                    action, data = payload
                    if action == "ready":
                        self._on_audio_ready(data)
                    else:
                        self.btn_audio.configure(state="normal", text="试听本段")
                        self.set_status(f"试听合成失败：{data}", "#c00")
                        self.log(f"试听合成失败：{data}")
                elif kind == "hotkey":
                    if payload == "start":
                        self.log("热键 F6：开始")
                        self.start()
                    else:
                        self.log("热键 F8：停止")
                        self.stop()
        except queue.Empty:
            pass
        self.root.after(60, self._poll_queue)

    # ------------------------------------------------------------- 杂项 ----
    def log(self, text: str):
        self.queue.put(("log", text))

    def _append_log(self, text: str):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", time.strftime("[%H:%M:%S] ") + text + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def set_status(self, text: str, color: str = "#333"):
        self.status.configure(text=text, foreground=color)

    def _on_close(self):
        try:
            self.cfg.update({
                "speed": float(self.var_speed.get()),
                "transpose": int(self.var_transpose.get()),
                "countdown": int(self.var_countdown.get()),
                "gap_ms": float(self.var_gap.get()),
                "min_hold_ms": float(self.var_minhold.get()),
                "base": int(self.var_base.get()),
                "auto_fit": bool(self.var_autofit.get()),
                "fold": bool(self.var_fold.get()),
                "auto_transpose": bool(self.var_autotranspose.get()),
                "mono": self._mono_mode(),
                "fill": self._fill_mode(),
                "dry_run": bool(self.var_dry.get()),
            })
            save_config(self.cfg)
        except Exception:
            pass
        try:
            self.stop()
        except Exception:
            pass
        try:
            self.sender.release_all()
        except Exception:
            pass
        if self.hotkeys:
            self.hotkeys.shutdown()
        self.root.destroy()


# --------------------------------------------------------------------------- #
# 命令行模式
# --------------------------------------------------------------------------- #
def base_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


class _Tee:
    """把输出同时写到多个流（附着的控制台 + 日志文件）。"""

    def __init__(self, *streams):
        self._streams = [s for s in streams if s is not None]

    def write(self, text):
        for stream in self._streams:
            try:
                stream.write(text)
            except Exception:
                pass
        return len(text)

    def flush(self):
        for stream in self._streams:
            try:
                stream.flush()
            except Exception:
                pass

    def isatty(self):
        return False


def prepare_cli_output():
    """命令行模式下保证输出有地方去。

    源码运行 / 控制台版：直接用标准输出。
    窗口版 exe：附着一个控制台（命令行里看得见），同时始终写一份 cli-output.log，
               这样双击运行或用管道调用都能拿到结果。
    """
    try:
        if sys.stdout is not None and sys.stdout.fileno() >= 0:
            try:
                # line_buffering：秒退的命令（比如 --check）也不会因为管道缓冲丢掉输出
                sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
            except Exception:
                pass
            return None
    except Exception:
        pass

    streams = []
    try:
        ctypes.WinDLL("kernel32").AttachConsole(-1)
        streams.append(open("CONOUT$", "w", encoding="utf-8", buffering=1))
    except Exception:
        pass
    path = os.path.join(base_dir(), "cli-output.log")
    try:
        streams.append(open(path, "w", encoding="utf-8", buffering=1))
    except Exception:
        path = None
    if streams:
        tee = _Tee(*streams)
        sys.stdout = tee
        sys.stderr = tee
    return path


def _arg_value(argv, flag, default=None):
    if flag in argv:
        index = argv.index(flag)
        if index + 1 < len(argv):
            return argv[index + 1]
    return default


USAGE = f"""{APP_NAME} v{VERSION}

用法：
  {APP_NAME}.exe                        打开图形界面
  {APP_NAME}.exe --play <文件.mid>      不开界面直接演奏（可配合脚本用）
        [--track N] [--speed 1.0] [--transpose 0] [--auto-transpose]
        [--mono auto|highest|legato]
        [--countdown 4] [--base 60] [--dry-run]
  {APP_NAME}.exe --check                自检运行环境（管理员权限 / SendInput / tkinter）
  {APP_NAME}.exe --selftest             跑内置的逻辑自检（55 项）
"""


def run_cli(argv) -> int | None:
    """处理命令行参数；返回 None 表示照常启动图形界面。"""
    if not argv:
        return None
    if "--help" in argv or "-h" in argv:
        prepare_cli_output()
        print(USAGE)
        return 0

    if "--check" in argv:
        log_path = prepare_cli_output()
        print(f"{APP_NAME} v{VERSION}")
        print(f"  打包运行      : {bool(getattr(sys, 'frozen', False))}")
        print(f"  管理员权限    : {player_mod.is_admin()}")
        try:
            sender = player_mod.InputSender(dry_run=False)
            print("  SendInput 可用: True")
            sender.release_all()
        except Exception as exc:
            print(f"  SendInput 可用: False ({exc})")
        try:
            import tkinter  # noqa: F401
            print("  tkinter 可用  : True")
        except Exception as exc:
            print(f"  tkinter 可用  : False ({exc})")
        print(f"  输出日志      : {log_path or '控制台'}")
        return 0

    if "--selftest" in argv:
        log_path = prepare_cli_output()
        import selftest
        code = selftest.main()
        if log_path:
            print(f"\n（结果已写入 {log_path}）")
        return code

    if "--play" in argv:
        log_path = prepare_cli_output()
        path = _arg_value(argv, "--play")
        if not path or not os.path.exists(path):
            print(f"找不到 MIDI 文件：{path}")
            return 2
        midi = smf.load(path)
        track_index = _arg_value(argv, "--track")
        if track_index is None:
            track = mapping.pick_best_track(midi.tracks)
        else:
            track = midi.tracks[int(track_index)]
        if track is None or not track.notes:
            print("这条轨道没有音符")
            return 2
        transpose = int(_arg_value(argv, "--transpose", 0))
        if "--auto-transpose" in argv:
            transpose = mapping.best_transpose(track.notes)
            print(f"自动移调：{transpose:+d} 半音")
        mapped, dropped, folded = mapping.map_notes(
            track.notes,
            base=int(_arg_value(argv, "--base", mapping.DEFAULT_BASE)),
            transpose=transpose,
            mono=_arg_value(argv, "--mono", "auto"))
        speed = float(_arg_value(argv, "--speed", 1.0))
        spans, skipped = mapping.note_spans(mapped, speed=speed)
        spans = mapping.trim_lead(spans)
        events = mapping.events_from_spans(spans)
        events = mapping.scale_events(events, speed)
        dry = "--dry-run" in argv
        countdown = int(_arg_value(argv, "--countdown", 0))
        print(f"曲目：{os.path.basename(path)}  轨道 [{track.index}] {track.name}")
        print(f"音符 {len(track.notes)} -> 映射 {len(mapped)}"
              f"（折叠 {folded}，丢弃 {dropped}）-> 实际演奏 {len(mapped) - skipped}"
              f"（时值太短跳过 {skipped}；放慢速度可以救回一部分）")
        print(f"时长约 {events[-1].time / 1000:.1f}s{'（干跑，不发送按键）' if dry else ''}")
        sender = player_mod.InputSender(dry_run=dry)
        player = player_mod.Player(
            sender,
            on_progress=lambda done, total: print(f"\r  进度 {done / 1000:7.1f}s / "
                                                  f"{total / 1000:.1f}s", end="", flush=True),
            on_finish=lambda ok, why: None)
        for remaining in range(countdown, 0, -1):
            print(f"  {remaining} 秒后开始，请切回游戏…")
            time.sleep(1)
        if not player_mod.is_admin() and not dry:
            print("  警告：不是管理员权限，游戏可能收不到按键")
        player.play(events, block=True)
        print(f"\n结束。{'结果见 ' + log_path if log_path else ''}")
        return 0

    return None


def main():
    code = run_cli(sys.argv[1:])
    if code is not None:
        return code
    try:                      # 高 DPI 下界面不发虚
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
