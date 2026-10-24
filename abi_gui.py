#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ABI Sound ID — окно управления и прозрачный оверлей.
Запуск:  python abi_gui.py      (или ABI_Sound_ID.exe, собранный через build_exe.bat)
Движок распознавания лежит в abi_sound_id.py.
"""
import contextlib
import json
import os
import queue
import sys
import threading
import time
from argparse import Namespace

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import abi_sound_id as core
import i18n
from i18n import t

KEY = "#010203"                      # этот цвет Windows делает полностью прозрачным
ANY = "any"  # display via t("any") at runtime
SIZES = ["1x1", "1x2", "1x3", "1x4", "2x2", "2x3", "2x4", "3x3", "3x4", "4x4"]
COLORS = {"head_ok": "#4ade80", "head_maybe": "#fbbf24", "sub": "#cbd5e1", "mythic": "#ff5c5c",
          "legendary": "#f5c542", "epic": "#b78bff", "rare": "#5aa9ff", "common": "#aab2bd",
          "item": "#e5e7eb", "dim": "#8b95a1"}
DIM = "#7b8794"
OUTLINE = ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (1, 1), (-1, 1), (1, -1))


# ───────────────────────────── настройки ─────────────────────────────
def app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def settings_file():
    base = os.environ.get("APPDATA") or app_dir()
    d = os.path.join(base, "ABI_Sound_ID")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        d = app_dir()
    return os.path.join(d, "settings.json")


def defaults():
    return {"library": os.path.join(app_dir(), "abi_library"), "device": "", "sens": 70, "font": 10,
            "hide_after": 12, "x": 40, "y": 40, "size": "", "strict": True, "last_import": "", "lang": "ru"}


def load_settings():
    cfg = defaults()
    try:
        with open(settings_file(), encoding="utf-8") as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    return cfg


def save_settings(cfg):
    try:
        with open(settings_file(), "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=1)
    except OSError:
        pass


def set_click_through(win, on):
    """Windows: окно не перехватывает мышь (клики проходят сквозь оверлей в игру)."""
    try:
        import ctypes
        u = ctypes.windll.user32
        hwnd = u.GetParent(win.winfo_id()) or win.winfo_id()
        st = u.GetWindowLongW(hwnd, -20)
        st |= 0x00080000 | 0x00000080 | 0x08000000          # LAYERED | TOOLWINDOW | NOACTIVATE
        st = (st | 0x20) if on else (st & ~0x20)             # TRANSPARENT
        u.SetWindowLongW(hwnd, -20, st)
    except Exception:
        pass


class QueueWriter:
    """Перехватывает print() из фоновой задачи и отправляет в журнал."""

    def __init__(self, q):
        self.q = q

    def write(self, s):
        if s:
            self.q.put(("text", s))
        return len(s)

    def flush(self):
        pass


# ───────────────────────────── оверлей ─────────────────────────────
class Overlay:
    """Окно без рамки и без фона. Текст с чёрной обводкой читается на любом фоне игры."""

    def __init__(self, master, get, on_moved):
        self.get, self.on_moved = get, on_moved
        self.move_mode = False
        self.top = tk.Toplevel(master)
        self.top.overrideredirect(True)
        self.top.attributes("-topmost", True)
        try:
            self.top.attributes("-transparentcolor", KEY)
        except tk.TclError:
            pass
        self.top.configure(bg=KEY)
        self.cv = tk.Canvas(self.top, bg=KEY, highlightthickness=0, bd=0, width=1, height=1)
        self.cv.pack()
        self.w, self.h = 1, 1
        self.top.geometry("1x1+%d+%d" % (self.get("x"), self.get("y")))
        self.top.update_idletasks()
        set_click_through(self.top, True)
        self.cv.bind("<ButtonPress-1>", self._press)
        self.cv.bind("<B1-Motion>", self._drag)
        self.cv.bind("<ButtonRelease-1>", self._release)
        self._d = (0, 0, 0, 0)

    # --- перемещение ---
    def set_move_mode(self, on):
        self.move_mode = on
        set_click_through(self.top, not on)

    def _press(self, e):
        self._d = (e.x_root, e.y_root, self.get("x"), self.get("y"))

    def _drag(self, e):
        if not self.move_mode:
            return
        x = self._d[2] + e.x_root - self._d[0]
        y = self._d[3] + e.y_root - self._d[1]
        self.top.geometry("%dx%d+%d+%d" % (self.w, self.h, x, y))
        self._pos = (x, y)

    def _release(self, e):
        if self.move_mode and getattr(self, "_pos", None):
            self.on_moved(*self._pos)

    # --- рисование ---
    def _text(self, x, y, text, font, color):
        for dx, dy in OUTLINE:
            self.cv.create_text(x + dx, y + dy, anchor="nw", text=text, font=font, fill="#000000")
        i = self.cv.create_text(x, y, anchor="nw", text=text, font=font, fill=color)
        bb = self.cv.bbox(i)
        return bb[2] if bb else x + 8 * len(text)

    def render(self, lines, dim=False):
        cv = self.cv
        cv.delete("all")
        if not lines:
            self._resize(1, 1)
            return
        fs = int(self.get("font"))
        f_head, f_item, f_small = ("Segoe UI", fs + 1, "bold"), ("Segoe UI", fs), ("Segoe UI", max(6, fs - 1))
        row = int(fs * 1.75) + 3
        x0, y, maxx = 8, 6, 0
        for kind, text, extra in lines:
            col = DIM if dim else COLORS.get(kind, COLORS["item"])
            if kind in ("head_ok", "head_maybe"):
                x = self._text(x0, y, ("● " if kind == "head_ok" else "◐ ") + text, f_head, col)
                if extra:
                    x = self._text(x + 10, y + 1, extra, f_small, DIM if dim else COLORS["dim"])
                y += row + 2
            elif kind == "sub":
                x = self._text(x0, y + 2, text, f_item, col)
                if extra:
                    x = self._text(x + 10, y + 3, extra, f_small, DIM if dim else COLORS["dim"])
                y += row
            elif kind == "dim":
                x = self._text(x0 + 12, y, text, f_small, DIM if dim else COLORS["dim"])
                y += row - 3
            else:
                x = self._text(x0 + 12, y, text, f_item, col)
                if extra:
                    x = self._text(x + 8, y + 1, extra, f_small, DIM if dim else COLORS["dim"])
                y += row
            maxx = max(maxx, x)
        w, h = maxx + 10, y + 4
        if self.move_mode:
            r = cv.create_rectangle(0, 0, w - 1, h - 1, fill="#1b1f27", outline="#6b7280")
            cv.tag_lower(r)
        self._resize(w, h)

    def _resize(self, w, h):
        self.w, self.h = w, h
        self.cv.config(width=w, height=h)
        self.top.geometry("%dx%d+%d+%d" % (w, h, self.get("x"), self.get("y")))

    def destroy(self):
        try:
            self.top.destroy()
        except tk.TclError:
            pass


# ───────────────────────────── окно управления ─────────────────────────────
class App:
    def __init__(self):
        self.cfg = load_settings()
        i18n.set_lang(self.cfg.get("lang") or "ru")
        self.root = tk.Tk()
        self.root.title(t("app_title"))
        self.root.geometry("620x740")
        self.root.minsize(580, 680)
        try:
            ttk.Style().theme_use("clam")
        except tk.TclError:
            pass
        self.logq, self.devq = queue.Queue(), queue.Queue()
        self.lib = self.matcher = self.src = self.overlay = None
        self.running = self.busy = False
        self.rec = None
        self.last_res, self.last_t, self.listen_t0 = None, 0.0, 0.0
        self.skipped, self.device_names = 0, []
        self._sig, self._last_ref, self._sens_after = None, 0.0, None
        self._build_ui()
        self.load_library()
        self.refresh_devices()
        # захват запускаємо після отримання списку пристроїв (див. _drain_misc)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(40, self.poll)

    # ---------- интерфейс ----------
    def _build_ui(self):
        r = self.root
        c = self.cfg
        self.v_lib = tk.StringVar(value=c["library"])
        self.v_dev = tk.StringVar()
        self.v_sens = tk.IntVar(value=int(c["sens"]))
        self.v_font = tk.IntVar(value=int(c["font"]))
        self.v_hide = tk.IntVar(value=int(c["hide_after"]))
        self.v_size = tk.StringVar(value=c["size"] or t("any"))
        self.v_move = tk.BooleanVar(value=False)
        self.v_strict = tk.BooleanVar(value=bool(c["strict"]))
        self.v_level = tk.DoubleVar(value=0.0)
        self.v_status = tk.StringVar(value=t("ready"))
        self.v_name = tk.StringVar()
        self.v_rsize = tk.StringVar()
        self.v_red = tk.BooleanVar(value=True)
        self.v_takes = tk.IntVar(value=5)

        top = ttk.Frame(r, padding=(12, 10, 12, 4))
        top.pack(fill="x")
        self.btn_start = ttk.Button(top, text=t("start"), command=self.toggle_listen)
        self.btn_start.pack(side="left")
        ttk.Label(top, textvariable=self.v_status, wraplength=420).pack(side="left", padx=14)

        nb = ttk.Notebook(r)
        nb.pack(fill="x", padx=12, pady=6)
        main, libt, rect = ttk.Frame(nb, padding=10), ttk.Frame(nb, padding=10), ttk.Frame(nb, padding=10)
        nb.add(main, text=t("tab_main"))
        nb.add(libt, text=t("tab_library"))
        nb.add(rect, text=t("tab_record"))

        # --- Главная: звук ---
        g1 = ttk.LabelFrame(main, text=t("sound"), padding=8)
        g1.pack(fill="x")
        row = ttk.Frame(g1)
        row.pack(fill="x")
        ttk.Label(row, text=t("source")).pack(side="left")
        self.cb_dev = ttk.Combobox(row, textvariable=self.v_dev, state="readonly", width=48)
        self.cb_dev.pack(side="left", padx=6)
        ttk.Button(row, text="↻", width=3, command=self.refresh_devices).pack(side="left")
        row = ttk.Frame(g1)
        row.pack(fill="x", pady=(8, 0))
        ttk.Label(row, text=t("sensitivity")).pack(side="left")
        ttk.Scale(row, from_=0, to=100, variable=self.v_sens, length=240,
                  command=lambda v: self.on_sens()).pack(side="left", padx=6)
        self.lbl_sens = ttk.Label(row, text=str(self.v_sens.get()), width=4)
        self.lbl_sens.pack(side="left")
        row = ttk.Frame(g1)
        row.pack(fill="x", pady=(8, 0))
        ttk.Label(row, text=t("input_level")).pack(side="left")
        ttk.Progressbar(row, variable=self.v_level, maximum=100, length=300).pack(side="left", padx=6)
        ttk.Label(g1, text=t("level_hint"),
                  foreground="#6b7280").pack(anchor="w", pady=(4, 0))

        # --- Главная: оверлей ---
        g2 = ttk.LabelFrame(main, text=t("overlay"), padding=8)
        g2.pack(fill="x", pady=(10, 0))
        row = ttk.Frame(g2)
        row.pack(fill="x")
        ttk.Label(row, text=t("item_size")).pack(side="left")
        ttk.Combobox(row, textvariable=self.v_size, values=[t("any")] + SIZES, state="readonly", width=8).pack(side="left", padx=6)
        ttk.Label(row, text=t("size_hint"), foreground="#6b7280").pack(side="left")
        row = ttk.Frame(g2)
        row.pack(fill="x", pady=(8, 0))
        ttk.Label(row, text=t("font_size")).pack(side="left")
        ttk.Spinbox(row, from_=7, to=18, textvariable=self.v_font, width=4).pack(side="left", padx=(6, 18))
        ttk.Label(row, text=t("hide_after")).pack(side="left")
        ttk.Spinbox(row, from_=0, to=120, textvariable=self.v_hide, width=5).pack(side="left", padx=6)
        ttk.Checkbutton(g2, text=t("move_overlay"),
                        variable=self.v_move).pack(anchor="w", pady=(8, 0))
        ttk.Checkbutton(g2, text=t("strict_wave"),
                        variable=self.v_strict).pack(anchor="w", pady=(4, 0))
        ttk.Label(g2, text=t("borderless_hint"),
                  foreground="#6b7280").pack(anchor="w", pady=(6, 0))
        row = ttk.Frame(g2)
        row.pack(fill="x", pady=(8, 0))
        ttk.Label(row, text=t("language")).pack(side="left")
        self.v_lang = tk.StringVar(value="Русский" if i18n.get_lang() == "ru" else "English")
        self.cb_lang = ttk.Combobox(row, textvariable=self.v_lang, values=["Русский", "English"],
                                    state="readonly", width=12)
        self.cb_lang.pack(side="left", padx=6)
        self.cb_lang.bind("<<ComboboxSelected>>", lambda e: self.on_lang())

        # --- Библиотека ---
        ttk.Label(libt, text=t("lib_folder")).pack(anchor="w")
        row = ttk.Frame(libt)
        row.pack(fill="x", pady=(2, 8))
        ttk.Entry(row, textvariable=self.v_lib).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text=t("browse"), command=self.pick_library).pack(side="left", padx=(6, 0))
        self.lbl_lib = ttk.Label(libt, text="")
        self.lbl_lib.pack(anchor="w", pady=(0, 8))
        ttk.Button(libt, text=t("import_sounds"), command=self.import_sounds).pack(fill="x")
        ttk.Label(libt, text=t("import_hint"),
                  foreground="#6b7280").pack(anchor="w", pady=(2, 8))
        row = ttk.Frame(libt)
        row.pack(fill="x")
        ttk.Button(row, text=t("check_lib"), command=self.check_library).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text=t("list_lib"), command=self.list_library).pack(side="left", padx=(6, 0))

        # --- Запись ---
        ttk.Label(rect, text=t("record_hint"),
                  justify="left").pack(anchor="w", pady=(0, 8))
        row = ttk.Frame(rect)
        row.pack(fill="x")
        ttk.Label(row, text=t("item_name")).pack(side="left")
        ttk.Entry(row, textvariable=self.v_name, width=30).pack(side="left", padx=6)
        ttk.Label(row, text=t("item_size_rec")).pack(side="left")
        ttk.Combobox(row, textvariable=self.v_rsize, values=[""] + SIZES, width=6).pack(side="left", padx=6)
        row = ttk.Frame(rect)
        row.pack(fill="x", pady=(8, 0))
        ttk.Checkbutton(row, text=t("red_item"), variable=self.v_red).pack(side="left")
        ttk.Label(row, text=t("takes_count")).pack(side="left", padx=(18, 0))
        ttk.Spinbox(row, from_=2, to=12, textvariable=self.v_takes, width=4).pack(side="left", padx=6)
        row = ttk.Frame(rect)
        row.pack(fill="x", pady=(10, 0))
        self.btn_rec = ttk.Button(row, text=t("start_record"), command=self.toggle_record)
        self.btn_rec.pack(side="left")
        self.lbl_rec = ttk.Label(row, text="")
        self.lbl_rec.pack(side="left", padx=12)

        # --- Журнал ---
        lf = ttk.LabelFrame(r, text=t("journal"), padding=6)
        lf.pack(fill="both", expand=True, padx=12, pady=(4, 12))
        self.txt = tk.Text(lf, height=10, wrap="word", state="disabled", bg="#f8fafc", relief="flat")
        sb = ttk.Scrollbar(lf, command=self.txt.yview)
        self.txt.config(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.txt.pack(side="left", fill="both", expand=True)

        # реакции на изменения
        self.v_sens.trace_add("write", lambda *a: self.on_sens())
        self.v_font.trace_add("write", lambda *a: self.on_overlay_opts())
        self.v_hide.trace_add("write", lambda *a: self.on_overlay_opts())
        self.v_size.trace_add("write", lambda *a: self.on_overlay_opts())
        self.v_move.trace_add("write", lambda *a: self.on_move())
        self.v_strict.trace_add("write", lambda *a: self.on_strict())
        self.cb_dev.bind("<<ComboboxSelected>>", lambda e: self.on_device())

    # ---------- журнал / статус ----------
    def log(self, s):
        self.txt.config(state="normal")
        self.txt.insert("end", s if s.endswith("\n") else s + "\n")
        self.txt.see("end")
        self.txt.config(state="disabled")

    def status(self, s):
        self.v_status.set(s)

    # ---------- библиотека ----------
    def load_library(self):
        path = self.v_lib.get().strip() or defaults()["library"]
        try:
            self.lib = core.Library(path)
            self.lib.cfg["min_wave"] = 0.15 if self.v_strict.get() else 0.0
            self.matcher = core.Matcher(self.lib)
        except Exception as e:
            self.log(t("lib_open_fail", e))
            return
        self.cfg["library"] = path
        n = len(self.lib.groups)
        takes = sum(len(g["takes"]) for g in self.lib.groups)
        self.lbl_lib.config(text=t("groups_takes", n, takes))
        if self.src is not None:
            self.src.configure(matcher=self.matcher)

    def pick_library(self):
        d = filedialog.askdirectory(title=t("pick_lib_title"), initialdir=self.v_lib.get() or app_dir())
        if d:
            self.v_lib.set(d)
            self.load_library()
            self.log(t("lib_path") + d)

    def run_task(self, fn, *args):
        if self.busy:
            self.log(t("busy"))
            return
        self.busy = True
        self.status(t("working"))

        def work():
            with contextlib.redirect_stdout(QueueWriter(self.logq)):
                try:
                    fn(*args)
                except SystemExit as e:
                    print(str(e))
                except Exception as e:                       # noqa
                    print("Ошибка: %s" % e)
            self.logq.put(("done", None))

        threading.Thread(target=work, daemon=True).start()

    def import_sounds(self):
        d = filedialog.askdirectory(title=t("pick_sounds_title"),
                                    initialdir=self.cfg.get("last_import") or app_dir())
        if not d:
            return
        self.cfg["last_import"] = d
        self.log(t("import_from") + d)
        self.run_task(core.cmd_import, Namespace(lib=self.cfg["library"], folder=d))

    def check_library(self):
        self.run_task(core.cmd_check, Namespace(lib=self.cfg["library"], apply=True))

    def list_library(self):
        self.run_task(core.cmd_list, Namespace(lib=self.cfg["library"]))

    # ---------- звук ----------
    def refresh_devices(self):
        # На главном потоке — надёжнее с WASAPI/COM, чем из фонового потока
        def work():
            try:
                mics, default = core.list_devices_data()
                self._fill_devices(mics, default)
                self.log(t("devices_found", len(mics)))
            except Exception as e:                           # noqa
                self.log(t("devices_fail", e))
        self.root.after(50, work)

    def _fill_devices(self, mics, default):
        self.device_names = [n for n, _ in mics]
        labels = [t("auto_source", default)]
        labels += [("[loopback] " if lb else "[in] ") + n for n, lb in mics]
        self.cb_dev.config(values=labels)
        idx = 0
        if self.cfg["device"] in self.device_names:
            idx = self.device_names.index(self.cfg["device"]) + 1
        self.cb_dev.current(idx)
        # захват не запускаем автоматически — только по кнопке Старт


    def on_lang(self):
        lang = "ru" if self.v_lang.get().startswith("Рус") or self.v_lang.get() == "ru" else "en"
        if lang == i18n.get_lang():
            return
        i18n.set_lang(lang)
        self.cfg["lang"] = lang
        save_settings(self.cfg)
        messagebox.showinfo(
            "ABI Sound ID",
            "Language saved. Restart the program to apply.\nЯзык сохранён. Перезапусти программу."
            if lang == "en" else
            "Язык сохранён. Перезапусти программу.\nLanguage saved. Restart the program."
        )

    def on_device(self):
        i = self.cb_dev.current()
        self.cfg["device"] = "" if i <= 0 else self.device_names[i - 1]
        if self.src is not None:
            self.restart_source()

    def restart_source(self):
        if self.rec:
            self.cancel_record()
        if self.src is not None:
            try:
                self.src.stop()
            except Exception:
                pass
            self.src = None
        try:
            self.src = core.Source(self.cfg["device"] or None, self.cfg["sens"])
            mode = "listen" if (self.running and self.matcher is not None) else "idle"
            self.src.configure(mode=mode, matcher=self.matcher, sens=self.cfg["sens"])
            self.src.start()
        except Exception as e:
            self.src = None
            self.log(t("capture_start_fail", e))
            self.status(t("capture_start_status"))

    def on_sens(self):
        try:
            v = int(self.v_sens.get())
        except (tk.TclError, ValueError):
            return
        self.lbl_sens.config(text=str(v))
        self.cfg["sens"] = v
        if self._sens_after:
            self.root.after_cancel(self._sens_after)
        self._sens_after = self.root.after(400, lambda: self.src and self.src.configure(sens=v))

    def on_strict(self):
        self.cfg["strict"] = bool(self.v_strict.get())
        if self.lib is not None:
            self.lib.cfg["min_wave"] = 0.15 if self.cfg["strict"] else 0.0

    # ---------- оверлей ----------
    def _get(self, k):
        return self.cfg[k]

    def _moved(self, x, y):
        self.cfg["x"], self.cfg["y"] = int(x), int(y)

    def ensure_overlay(self):
        if self.overlay is None:
            self.overlay = Overlay(self.root, self._get, self._moved)
            self.overlay.set_move_mode(bool(self.v_move.get()))
            self._sig = None

    def drop_overlay(self):
        if self.overlay is not None and not self.v_move.get() and not self.running:
            self.overlay.destroy()
            self.overlay = None

    def on_overlay_opts(self):
        try:
            self.cfg["font"] = max(6, min(24, int(self.v_font.get())))
            self.cfg["hide_after"] = max(0, int(self.v_hide.get()))
        except (tk.TclError, ValueError):
            return
        s = self.v_size.get()
        self.cfg["size"] = "" if s in (ANY, t("any"), "") else s
        self._sig = None
        self.refresh_overlay()

    def on_move(self):
        on = bool(self.v_move.get())
        if on:
            self.ensure_overlay()
        if self.overlay is not None:
            self.overlay.set_move_mode(on)
        if not on:
            self.drop_overlay()
        self._sig = None
        self.refresh_overlay()

    def refresh_overlay(self):
        if self.overlay is None:
            return
        now = time.time()
        lines, dim = None, False
        hide = self.cfg["hide_after"]
        if self.last_res is not None:
            age = now - self.last_t
            if not (hide and age > hide):
                lines = core.build_view(self.last_res, self.lib, self.cfg["size"] or None, 6)
                dim = bool(hide) and age > hide / 2.0
        elif self.running and now - self.listen_t0 < 4.0:
            lines = [("dim", t("listening_dot"), "")]
        if self.overlay.move_mode and not lines:
            lines = [("sub", t("overlay_drag"), "")]
        sig = (tuple(lines) if lines else None, dim, self.cfg["font"], self.overlay.move_mode)
        if sig != self._sig:
            self._sig = sig
            self.overlay.render(lines, dim)

    # ---------- старт / стоп ----------
    def toggle_listen(self):
        if self.running:
            self.stop_listening()
            return
        if self.rec:
            self.cancel_record()
        if self.lib is None or not self.lib.groups:
            messagebox.showwarning(t("lib_empty_title"), t("lib_empty_msg"))
            return
        self.running = True
        self.listen_t0, self.last_res, self.skipped = time.time(), None, 0
        self.ensure_overlay()
        if self.src is None:
            self.restart_source()
        if self.src is None:
            self.running = False
            messagebox.showerror(t("sound_error_title"), t("sound_error_msg"))
            return
        self.src.configure(mode="listen", matcher=self.matcher, sens=self.cfg["sens"])
        self.btn_start.config(text=t("stop"))
        self.status(t("listening"))

    def stop_listening(self):
        self.running = False
        if self.src is not None:
            self.src.configure(mode="idle")
        self.btn_start.config(text=t("start"))
        self.status(t("stopped"))
        self.last_res = None
        if self.overlay is not None:
            self._sig = None
            self.refresh_overlay()
        self.drop_overlay()

    # ---------- запись ----------
    def toggle_record(self):
        if self.rec:
            self.cancel_record()
            return
        name = self.v_name.get().strip()
        if not name:
            messagebox.showwarning(t("no_name_title"), t("no_name_msg"))
            return
        try:
            n = max(2, int(self.v_takes.get()))
        except (tk.TclError, ValueError):
            n = 5
        if self.running:
            self.stop_listening()
        item = {"name": name, "size": self.v_rsize.get().strip(), "red": bool(self.v_red.get())}
        self.rec = {"item": item, "takes": [], "n": n}
        if self.src is None:
            self.restart_source()
        if self.src is None:
            self.rec = None
            messagebox.showerror(t("sound_error_title"), t("sound_error_msg"))
            return
        self.src.drain()
        self.src.configure(mode="record", sens=self.cfg["sens"])
        self.btn_rec.config(text=t("cancel_record"))
        self.lbl_rec.config(text=t("rec_status", name, 0, n))
        self.log(t("rec_log", name, n))

    def cancel_record(self):
        self.rec = None
        self.btn_rec.config(text=t("start_record"))
        self.lbl_rec.config(text="")
        if self.src is not None:
            self.src.configure(mode="idle")

    def _on_seg(self, seg):
        if not self.rec:
            return
        r = core.trim_event(seg)
        if r is None:
            self.log(t("noise_skip"))
            return
        w, snr = r
        self.rec["takes"].append(w)
        k, n = len(self.rec["takes"]), self.rec["n"]
        self.log(t("take_log", k, n, len(w) / core.SR, snr,
                                                         t("snr_low") if snr < 15 else ""))
        self.lbl_rec.config(text=t("rec_status", self.rec["item"]["name"], k, n))
        if k >= n:
            self.finish_record()

    def finish_record(self):
        rec, self.rec = self.rec, None
        self.btn_rec.config(text=t("start_record"))
        self.lbl_rec.config(text="")
        self.src.configure(mode="idle")
        item, takes = rec["item"], rec["takes"]
        target = None
        if self.lib.groups and self.matcher is not None:
            r = self.matcher.classify(core.pad(takes[0]))
            if r.top and r.top[0].fused >= max(self.lib.cfg["accept"], 0.85):
                g = r.top[0].gid
                if messagebox.askyesno(t("sound_exists_title"),
                                       t("sound_exists_msg", self.lib.describe(g, 4), item["name"])):
                    target = g
        if target:
            self.lib.add_item(target, item, takes)
            self.log(t("added_to_group", item["name"], target))
        else:
            self.log(t("new_group", self.lib.add_group(item, takes)))
        self.load_library()
        self.status(t("recorded"))

    # ---------- цикл обработки ----------
    def _drain_src(self):
        while self.src is not None:
            try:
                item = self.src.q.get_nowait()
            except queue.Empty:
                break
            kind = item[0]
            if kind == "level":
                self.v_level.set(max(0.0, min(100.0, (item[1] + 70.0) * 100.0 / 70.0)))
            elif kind == "result":
                if self.running:
                    self.last_res, self.last_t = item[1], time.time()
                    self._sig = None
                    self.refresh_overlay()
            elif kind == "unknown":
                self.skipped += 1
            elif kind == "seg":
                self._on_seg(item[1])
            elif kind == "error":
                self.log(t("capture_error") + item[1])
                self.status(t("capture_error_status"))
                if self.running:
                    self.stop_listening()

    def _drain_misc(self):
        while True:
            try:
                kind, a = self.logq.get_nowait()
            except queue.Empty:
                break
            if kind == "text":
                self.log(a.rstrip("\n"))
            elif kind == "done":
                self.busy = False
                self.status("Готово.")
                self.load_library()
        while True:
            try:
                kind, a, b = self.devq.get_nowait()
            except queue.Empty:
                break
            if kind == "ok":
                self._fill_devices(a, b)
            else:
                self.log("Не удалось получить список устройств: " + a)

    def poll(self):
        try:
            self._drain_src()
            self._drain_misc()
            now = time.time()
            if now - self._last_ref > 0.25:
                self._last_ref = now
                self.refresh_overlay()
        except Exception as e:                               # noqa
            self.log("Внутренняя ошибка: %s" % e)
        self.root.after(40, self.poll)

    def on_close(self):
        save_settings(self.cfg)
        if self.src is not None:
            self.src.stop()
        if self.overlay is not None:
            self.overlay.destroy()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def main():
    print(t("starting"), flush=True)
    try:
        app = App()
        print(t("window_open"), flush=True)
        app.run()
    except Exception as e:
        import traceback
        traceback.print_exc()
        try:
            import tkinter.messagebox as mb
            mb.showerror("ABI Sound ID", t("critical_error", e))
        except Exception:
            pass
        input(t("press_enter"))
        raise


if __name__ == "__main__":
    main()
