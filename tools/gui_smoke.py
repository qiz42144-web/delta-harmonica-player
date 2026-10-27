# -*- coding: utf-8 -*-
"""GUI 冒烟测试：真实启动窗口，走一遍「曲库 → 轨道 → 倒计时 → 演奏 → 停止 → 关窗」。

    python tools/gui_smoke.py

不需要人工操作，也不发送真实按键（全程 dry-run）。
"""
from __future__ import annotations

import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import tkinter as tk

import main as app_main
import synth

ERRORS = []


def main():
    demo_dir = os.path.join(ROOT, "demo")
    demo_path = os.path.join(demo_dir, "小星星.mid")
    if not os.path.exists(demo_path):
        os.makedirs(demo_dir, exist_ok=True)
        from demo_midi import build_demo
        with open(demo_path, "wb") as fh:
            fh.write(build_demo())

    root = tk.Tk()

    def report(exc_type, exc_value, exc_tb):
        ERRORS.append("".join(traceback.format_exception(exc_type, exc_value, exc_tb)))

    root.report_callback_exception = report
    app = app_main.App(root)
    steps = []

    def step(delay, label, func):
        steps.append(label)

        def wrapped():
            try:
                func()
                print(f"  [OK]   {label}")
            except Exception as exc:
                ERRORS.append(f"{label}: {exc!r}")
                print(f"  [FAIL] {label}: {exc!r}")
        root.after(delay, wrapped)

    def set_library():
        app.cfg["library_dir"] = demo_dir
        app.refresh_library()
        assert app.library_files, "曲库没扫到文件"
        assert app.library_list.size() == len(app.library_files), "曲库列表与文件数不一致"

    def expect_loaded():
        assert app.midi is not None, "曲目没有载入成功"
        lines = [app.track_list.get(i) for i in range(app.track_list.size())]
        assert lines, "轨道列表是空的"
        assert any(line.startswith("★ ") for line in lines), "没有标出推荐轨道"
        assert all(line.startswith(("★ ", "  ")) for line in lines), "轨道行前缀不统一"
        empty = [line for line in lines if "空轨" in line]
        assert all("不用选" in line for line in empty), "空轨没有提示不用选"
        print(f"         轨道 {len(lines)} 条，推荐："
              f"{[line for line in lines if line.startswith('★ ')][0].strip()[:40]}")

    def open_and_close_help():
        app.show_help()
        tops = [w for w in app.root.winfo_children() if isinstance(w, tk.Toplevel)]
        assert tops, "帮助窗口没有打开"
        tops[-1].destroy()

    def check_track_tab_selected():
        assert app.notebook.index(app.notebook.select()) == app.notebook.index(app.track_tab), \
            "载入后没有自动切到轨道页"

    def open_first_library_item():
        assert app.library_list.size() > 0, "曲库列表是空的"
        app.library_list.selection_clear(0, "end")
        app.library_list.selection_set(0)      # 真实操作里双击会先选中
        app._open_library_item()

    def check_segment_full():
        total = round(app.midi.length_ms / 1000.0, 1)
        assert abs(float(app.var_seg_end.get()) - total) < 0.2, "载入后段落终点没设成整曲"
        assert float(app.var_seg_start.get()) == 0.0, "载入后段落起点没归零"
        assert "整曲" in app.seg_label.cget("text"), \
            f"段落提示不对：{app.seg_label.cget('text')}"

    def set_segment_5s():
        app.var_seg_start.set(0.0)
        app.var_seg_end.set(5.0)
        app._on_segment_changed()

    def check_segment_applied():
        _start, end = app.stats.get("segment", (0, 0))
        assert end > 0, f"段落没有生效：{app.stats.get('segment')}"
        duration = app.events[-1].time / 1000.0
        assert 3.0 < duration < 7.0, f"段落时长异常：{duration:.1f}s"
        assert "选中" in app.seg_label.cget("text"), app.seg_label.cget("text")
        print(f"         段落 0–5s -> 实际 {duration:.1f}s，{len(app.spans)} 个音")

    def build_preview():
        data = app._build_preview_wav()
        assert data[:4] == b"RIFF", f"试听 WAV 头不对：{data[:12]!r}"
        ms = synth.wav_duration_ms(data)
        assert ms > 1000, f"试听时长异常：{ms}"
        print(f"         试听合成 {len(data) / 1024:.0f}KB / {ms / 1000:.1f}s（未播放）")

    def apply_suggestion():
        if app._speed_suggestion:
            before = float(app.var_speed.get())
            app._apply_speed_suggestion()
            after = float(app.var_speed.get())
            assert after < before, f"变速建议没生效：{before} -> {after}"
            print(f"         变速建议 {before:.2f}x -> {after:.2f}x")
            app.var_speed.set(1.0)
            app._rebuild_mapping()
        else:
            print("         （这首没给出变速建议，跳过）")

    def restore_full():
        app._set_full_segment()
        assert app.stats.get("segment") == (0, 0), app.stats.get("segment")

    def toggle_fill():
        for label in ("只在开头补", "所有空档补"):
            app.var_fill.set(label)
            app._on_fill_changed()
            assert app.events, f"{label} 下没有可演奏事件"
            print(f"         {label}：{len(app.spans)} 个音，"
                  f"{app.events[0].time:.0f}ms 起奏")
        app.var_fill.set("留白")
        app._on_fill_changed()
        assert app.events, "关闭填补后没有可演奏事件"

    step(200, "设置曲库文件夹并扫描", set_library)
    step(400, "双击曲库第一首（载入）", open_first_library_item)
    step(1600, "曲目已载入", expect_loaded)
    step(1700, "载入后自动切到轨道页", check_track_tab_selected)
    step(1800, "预览指法", app.preview_mapping)
    step(1850, "载入后段落默认整曲", check_segment_full)
    step(1880, "应用变速建议（整曲）", apply_suggestion)
    step(1900, "设置段落 0–5 秒", set_segment_5s)
    step(1950, "段落生效", check_segment_applied)
    step(2000, "合成试听 WAV（不播放）", build_preview)
    step(2020, "空档填补开关", toggle_fill)
    step(2050, "恢复整曲", restore_full)
    step(2150, "打开并关闭帮助窗口", open_and_close_help)
    step(2200, "切换干跑模式", lambda: (app.var_dry.set(True), app._on_dry_toggle()))
    step(2300, "设置倒计时 1 秒", lambda: app.var_countdown.set(1))
    step(2400, "点击开始", app.start)
    step(4000, "演奏进行中", lambda: print(f"         is_playing={app.player.is_playing}"))
    step(4200, "紧急停止", app.stop)
    step(4400, "无残留输入", lambda: (_ for _ in ()).throw(AssertionError("残留输入"))
         if (app.sender._held_keys or app.sender._held_mouse) else None)
    step(4700, "关闭窗口", app._on_close)

    root.mainloop()

    print(f"\n步骤 {len(steps)} 个，错误 {len(ERRORS)} 个")
    for item in ERRORS:
        print("----")
        print(item)
    return 1 if ERRORS else 0


if __name__ == "__main__":
    sys.exit(main())
