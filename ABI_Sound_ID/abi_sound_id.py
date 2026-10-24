#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ABI Sound ID — определение предмета по звуку перетаскивания
(Arena Breakout: Infinity, торговец).

Установка (Windows, Python 3.11–3.12 рекомендуется):
    pip install numpy scipy soundfile sounddevice
    pip install PyAudioWPatch
    # soundcard — запасной вариант, на части систем ломается

Команды:
    python abi_sound_id.py devices     # список источников звука
    python abi_sound_id.py record      # записать звуки предметов в библиотеку
    python abi_sound_id.py import sounds   # загрузить ogg/wav из папки (18_pickup.ogg, 18_drop.ogg …)
    python abi_sound_id.py check       # проверить библиотеку и подобрать пороги
    python abi_sound_id.py listen      # слушает в консоли (окно с оверлеем: python abi_gui.py)
    python abi_sound_id.py selftest    # проверка алгоритма на синтетике (без игры)

Программа только СЛУШАЕТ звук (loopback / виртуальный кабель). К процессу игры
она не подключается и память игры не читает.
"""
import argparse
import json
import os
import queue
import re
import sys
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from math import gcd

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view
from scipy.io import wavfile
from scipy.signal import fftconvolve, resample_poly

# ───────────────────────────── параметры ─────────────────────────────
SR = 48000                    # частота дискретизации
BLOCK = 1024                  # размер блока чтения
N_FFT, HOP, N_MELS = 1024, 256, 40
FMIN, FMAX = 100.0, 16000.0
DB_RANGE = 50.0               # всё тише (пик - 50 дБ) считаем «тишиной»
PRE_S, POST_S = 0.10, 0.70    # окно события: 0.1 с до начала, 0.7 с после
MAX_TPL_S = 0.60              # максимальная длина эталона
PAD_S = 0.15                  # тишина вокруг эталона при самопроверке
W_SPEC, W_NCC = 0.65, 0.35    # вес спектральной и волновой похожести
ACT_DB = 18.0                 # «активные» ячейки эталона: выше пола на столько дБ
OUT_DB = 6.0                  # ячейка, где запись громче эталона на столько дБ (сверх общего усиления) = шум поверх
MIN_FRAC = 0.45               # минимум неиспорченных ячеек для оценки
MAX_TAKES = 16                # максимум дублей в одной группе


# ───────────────────────────── признаки ─────────────────────────────
def _hz2mel(f):
    return 2595.0 * np.log10(1.0 + np.asarray(f) / 700.0)


def _mel2hz(m):
    return 700.0 * (10.0 ** (np.asarray(m) / 2595.0) - 1.0)


def _mel_fb():
    hz = _mel2hz(np.linspace(_hz2mel(FMIN), _hz2mel(FMAX), N_MELS + 2))
    bins = np.fft.rfftfreq(N_FFT, 1.0 / SR)
    fb = np.zeros((N_MELS, len(bins)), np.float32)
    for i in range(N_MELS):
        lo, c, hi = hz[i], hz[i + 1], hz[i + 2]
        fb[i] = np.maximum(0.0, np.minimum((bins - lo) / (c - lo), (hi - bins) / (hi - c)))
        if fb[i].sum() == 0:
            fb[i, int(np.argmin(np.abs(bins - c)))] = 1.0
    return fb


FB = _mel_fb()
WIN = np.hanning(N_FFT).astype(np.float32)


def logmel(x):
    """лог-мел спектрограмма в дБ, форма (N_MELS, кадры)"""
    x = np.asarray(x, np.float32)
    if len(x) < N_FFT:
        x = np.pad(x, (0, N_FFT - len(x)))
    n = 1 + (len(x) - N_FFT) // HOP
    idx = np.arange(N_FFT)[None, :] + HOP * np.arange(n)[:, None]
    p = np.abs(np.fft.rfft(x[idx] * WIN, axis=1)) ** 2
    return (10.0 * np.log10(p @ FB.T + 1e-10)).T


def robust_prep(wave):
    """Подготовка эталона: берём только «активные» ячейки спектрограммы (где звук реально есть)."""
    S = logmel(wave)
    fl = S.max() - DB_RANGE
    Sc = np.maximum(S, fl)
    rows, cols = np.nonzero(Sc > fl + ACT_DB)
    if len(rows) < 20:
        rows, cols = np.nonzero(Sc > fl + 5.0)
    return {"T": S.shape[1], "rows": rows, "cols": cols, "a": Sc[rows, cols].astype(np.float32)}


def robust_scores(Wv, tp):
    """Устойчивая похожесть эталона и окон записи Wv (n_off, mels, T).
    Смотрим только на активные ячейки эталона, оцениваем общее усиление по медиане и выбрасываем ячейки,
    где запись заметно громче ожидаемого (чужой звук поверх): громкий шаг не ломает сравнение."""
    a = tp["a"]
    b = Wv[:, tp["rows"], tp["cols"]]
    d = b - a[None, :]
    keep = d < (np.median(d, axis=1, keepdims=True) + OUT_DB)
    nk = keep.sum(1)
    w = keep.astype(np.float32)
    n = np.maximum(nk, 1)
    ac = (a[None, :] - (a[None, :] * w).sum(1, keepdims=True) / n[:, None]) * w
    bc = (b - (b * w).sum(1, keepdims=True) / n[:, None]) * w
    r = (ac * bc).sum(1) / (np.sqrt((ac ** 2).sum(1) * (bc ** 2).sum(1)) + 1e-9)
    r[nk < MIN_FRAC * len(a)] = 0.0
    return r


def ncc_near(q, t, start):
    """нормированная кросс-корреляция волн (макс. по сдвигам около start)"""
    L = len(t)
    seg = q[max(0, start - 2 * HOP): min(len(q), start + L + 2 * HOP)].astype(np.float64)
    if len(seg) < L:
        seg = np.pad(seg, (0, L - len(seg)))
    t = t.astype(np.float64)
    t = t - t.mean()
    num = fftconvolve(seg, t[::-1], mode="valid")
    c = np.concatenate(([0.0], np.cumsum(seg ** 2)))
    den = np.sqrt(c[L:] - c[:-L]) * np.linalg.norm(t) + 1e-12
    return float(min(1.0, np.max(num / den)))


def pad(x):
    return np.pad(np.asarray(x, np.float32), (int(PAD_S * SR),) * 2)


# ───────────────────────────── детектор событий ─────────────────────────────
class EventDetector:
    """Ищет резкий рост громкости над фоном и вырезает окно [-PRE_S, +POST_S].
    Энергия считается после лёгкого ВЧ-фильтра (гул и низкий шум не мешают), событие =
    уровень выше фона на thr_db И заметный скачок относительно последних ~40 мс."""
    FRAME = int(SR * 0.005)
    JUMP_DB = 4.0

    def __init__(self, thr_db=12.0, min_db=-55.0):
        self.thr, self.min_db = thr_db, min_db
        self.buf = np.zeros(0, np.float32)     # исходный звук (из него вырезаем события)
        self.pbuf = np.zeros(0, np.float32)    # он же после ВЧ-фильтра (для детектора)
        self.last = 0.0
        self.base = 0
        self.nframes = 0
        self.hist = deque(maxlen=600)          # ~3 с уровней кадров -> оценка фона
        self.recent = deque(maxlen=8)          # последние ~40 мс
        self.pending = None
        self.armed = True
        self.onsets = []                       # абсолютные индексы начала найденных событий
        self.keep = int((PRE_S + POST_S + 1.0) * SR)

    def feed(self, x):
        out = []
        x = np.asarray(x, np.float32)
        pe = np.empty_like(x)
        pe[0] = x[0] - 0.95 * self.last
        pe[1:] = x[1:] - 0.95 * x[:-1]
        self.last = float(x[-1])
        self.buf = np.concatenate([self.buf, x])
        self.pbuf = np.concatenate([self.pbuf, pe])
        total = self.base + len(self.buf)
        F = self.FRAME
        while (self.nframes + 1) * F <= total:
            a = self.nframes * F - self.base
            db = 10.0 * np.log10(float(np.mean(self.pbuf[a:a + F] ** 2)) + 1e-12)
            if len(self.hist) >= 40 and self.pending is None:
                floor = float(np.percentile(self.hist, 20))
                jump = db > max(self.recent) + self.JUMP_DB if self.recent else True
                if self.armed and db > floor + self.thr and db > self.min_db and jump:
                    self.pending, self.armed = self.nframes * F, False
                elif not self.armed and db < floor + self.thr * 0.5:
                    self.armed = True
            if self.pending is None:
                self.hist.append(db)
            self.recent.append(db)
            self.nframes += 1
        if self.pending is not None and total >= self.pending + int(POST_S * SR):
            s, e = self.pending - int(PRE_S * SR), self.pending + int(POST_S * SR)
            if s >= self.base:
                out.append(self.buf[s - self.base:e - self.base].copy())
                self.onsets.append(self.pending)
            self.pending = None
        drop = len(self.buf) - self.keep
        if drop > 0:
            self.buf, self.pbuf, self.base = self.buf[drop:], self.pbuf[drop:], self.base + drop
        return out


def _span(act, first_from=0):
    """(начало, конец) непрерывной активной области в кадрах по 5 мс или None.
    Конец — это конец НЕПРЕРЫВНОЙ области: пауза >40 мс = звук закончился."""
    cand = np.where(act[first_from:])[0]
    if len(cand) == 0:
        return None
    s0 = int(cand[0]) + first_from
    e0, gap = s0, 0
    for i in range(s0, len(act)):
        if act[i]:
            e0, gap = i, 0
        else:
            gap += 1
            if gap > 8:
                break
    st = max(0, s0 - 4)
    return st, min(len(act), e0 + 7, st + int(MAX_TPL_S / 0.005))


def _frames_db(x):
    h = int(0.005 * SR)
    n = len(x) // h
    return 10 * np.log10(np.mean(np.asarray(x[:n * h], np.float64).reshape(n, h) ** 2, axis=1) + 1e-12)


def trim_event(seg):
    """оставляет в записи только сам звук (для эталона). Возвращает (волна, SNR дБ) или None.
    Начало ищется у известного момента срабатывания детектора."""
    h = int(0.005 * SR)
    rms = _frames_db(seg)
    pre_n = int(PRE_S / 0.005)
    peak = float(rms.max())
    noise = float(np.median(rms[:max(4, pre_n - 4)]))
    sp = _span(rms > max(peak - 35.0, noise + 8.0), max(0, pre_n - 6))
    if sp is None or (sp[1] - sp[0]) * 0.005 < 0.08:
        return None
    return seg[sp[0] * h:sp[1] * h].copy(), peak - noise


def trim_clip(x):
    """то же для чистого файла (из сайта): режет тишину по краям, выравнивает громкость"""
    h = int(0.005 * SR)
    if len(x) < 4 * h:
        return None
    rms = _frames_db(x)
    sp = _span(rms > rms.max() - 35.0)
    if sp is None or (sp[1] - sp[0]) * 0.005 < 0.05:
        return None
    w = np.asarray(x[sp[0] * h:sp[1] * h], np.float32)
    return w / (float(np.abs(w).max()) + 1e-9) * 0.5


def load_audio(path):
    """ogg/wav/flac/mp3 -> моно float32 на частоте SR"""
    try:
        import soundfile as sf
        d, sr = sf.read(path, dtype="float32", always_2d=True)
    except ImportError:
        if not path.lower().endswith(".wav"):
            raise SystemExit("Для ogg/mp3/flac нужен пакет soundfile:  pip install soundfile")
        sr, d = wavfile.read(path)
        if np.issubdtype(d.dtype, np.integer):
            d = d / float(np.iinfo(d.dtype).max)
        d = np.atleast_2d(d.astype(np.float32).T).T
    x = d.mean(axis=1)
    if sr != SR:
        g = gcd(int(sr), SR)
        x = resample_poly(x, SR // g, int(sr) // g)
    return x.astype(np.float32)


# ───────────────────────────── библиотека ─────────────────────────────
class Library:
    def __init__(self, path):
        self.path = path
        self.cfg = {"accept": 0.70, "margin": 0.05, "min_wave": 0.15}
        self.groups = []
        os.makedirs(os.path.join(path, "wav"), exist_ok=True)
        self.jpath = os.path.join(path, "library.json")
        if os.path.exists(self.jpath):
            with open(self.jpath, encoding="utf-8") as f:
                d = json.load(f)
            self.cfg.update(d.get("cfg", {}))
            self.groups = d.get("groups", [])

    def wav(self, fn):
        return wavfile.read(os.path.join(self.path, "wav", fn))[1].astype(np.float32)

    def save(self):
        with open(self.jpath, "w", encoding="utf-8") as f:
            json.dump({"cfg": self.cfg, "groups": self.groups}, f, ensure_ascii=False, indent=1)

    def group(self, gid):
        return next(g for g in self.groups if g["id"] == gid)

    def add_takes(self, gid, takes):
        g = self.group(gid)
        for t in takes:
            if len(g["takes"]) >= MAX_TAKES:
                break
            k = len(g["takes"]) + 1
            while os.path.exists(os.path.join(self.path, "wav", f"{gid}_{k:02d}.wav")):
                k += 1
            fn = f"{gid}_{k:02d}.wav"
            wavfile.write(os.path.join(self.path, "wav", fn), SR, np.asarray(t, np.float32))
            g["takes"].append(fn)

    def add_group(self, item, takes):
        nums = [int(m.group(1)) for g in self.groups for m in [re.match(r"g(\d+)$", g["id"])] if m]
        gid = "g%03d" % (max(nums + [0]) + 1)
        self.groups.append({"id": gid, "items": [item], "takes": []})
        self.add_takes(gid, takes)
        self.save()
        return gid

    def add_item(self, gid, item, takes):
        g = self.group(gid)
        if not any(i["name"].lower() == item["name"].lower() for i in g["items"]):
            g["items"].append(item)
        self.add_takes(gid, takes)
        self.save()

    def replace_group(self, gid, meta, takes):
        for g in [g for g in self.groups if g["id"] == gid]:
            for fn in g["takes"]:
                f = os.path.join(self.path, "wav", fn)
                if os.path.exists(f):
                    os.remove(f)
        self.groups = [g for g in self.groups if g["id"] != gid]
        self.groups.append({"id": gid, "items": meta["items"], "takes": [],
                            "more": meta.get("more", 0), "tag": meta.get("tag", "")})
        self.add_takes(gid, takes)

    def describe(self, gid, limit=5):
        g = self.group(gid)
        parts = []
        for it in g["items"][:limit]:
            s = it["name"] + (f" [{it['size']}]" if it.get("size") else "")
            tag = {"Mythic": " (красный)", "Legendary": " (золотой)"}.get(
                it.get("rarity"), " (красный)" if it.get("red") else "")
            parts.append(s + tag)
        more = len(g["items"]) - min(limit, len(g["items"])) + g.get("more", 0)
        head = (g["tag"] + ": ") if g.get("tag") else ""
        return head + " / ".join(parts) + (f" … +{more}" if more > 0 else "")


# ───────────────────────────── распознавание ─────────────────────────────
@dataclass
class GroupScore:
    gid: str
    fused: float
    spec: float
    ncc: float


@dataclass
class Result:
    status: str                    # ok | maybe | unknown
    top: list = field(default_factory=list)


class Matcher:
    def __init__(self, lib):
        self.lib = lib
        self.tpl = []
        for g in lib.groups:
            for i, fn in enumerate(g["takes"]):
                w = lib.wav(fn)
                self.tpl.append(dict(gid=g["id"], idx=i, wave=w, **robust_prep(w)))

    def classify(self, q, exclude=None, hint_last=None, span=8):
        """hint_last — номер кадра в q, на котором звук должен закончиться (если известно, ищем только рядом)."""
        q = np.asarray(q, np.float32)
        Q = logmel(q)
        Q = np.maximum(Q, Q.max() - 90.0)
        by_g = {}
        for t in self.tpl:                                   # этап 1: устойчивое спектральное сравнение
            if exclude == (t["gid"], t["idx"]) or Q.shape[1] < t["T"]:
                continue
            Wv = np.transpose(sliding_window_view(Q, t["T"], axis=1), (1, 0, 2))
            lo, hi = 0, Wv.shape[0]
            if hint_last is not None:
                c = hint_last - t["T"] + 1
                lo, hi = max(0, c - span), min(hi, c + span + 1)
            if lo >= hi:
                continue
            r = robust_scores(Wv[lo:hi], t)
            o = int(np.argmax(r))
            by_g.setdefault(t["gid"], []).append((float(r[o]), lo + o, t))
        ranked = sorted(by_g.values(), key=lambda l: -max(x[0] for x in l))[:6]
        res = []
        for lst in ranked:                                   # этап 2: проверка формой волны
            lst.sort(key=lambda x: -x[0])
            best = None
            for sc, o, t in lst[:2]:
                n = ncc_near(q, t["wave"], o * HOP)
                f = W_SPEC * max(sc, 0.0) + W_NCC * max(n, 0.0)
                if best is None or f > best.fused:
                    best = GroupScore(t["gid"], f, sc, n)
            res.append(best)
        res.sort(key=lambda r: -r.fused)
        if not res:
            return Result("unknown")
        acc, mar = self.lib.cfg["accept"], self.lib.cfg["margin"]
        gap = res[0].fused - res[1].fused if len(res) > 1 else 1.0
        if res[0].ncc < self.lib.cfg.get("min_wave", 0.15):
            st = "unknown"          # шум даёт высокий спектральный балл, но не совпадает по форме волны
        elif res[0].fused >= acc and gap >= mar:
            st = "ok"
        elif res[0].fused >= acc - 0.12:
            st = "maybe"
        else:
            st = "unknown"
        return Result(st, res)


def sens_params(sens):
    """Ползунок чувствительности 0…100 -> пороги детекторов."""
    s = max(0.0, min(100.0, float(sens)))
    return {"thr_db": 14.0 - 0.08 * s,        # насколько громче фона должен быть звук (детектор по громкости)
            "min_db": -50.0 - 0.25 * s,       # абсолютный минимум уровня, дБFS
            "trig": 0.80 - 0.0035 * s}        # порог похожести для детектора по эталонам


BACK_S, FWD_S = 0.80, 0.10                    # окно вокруг найденного звука: сколько до его конца и после
HINT_LAST = int(BACK_S * SR / HOP) - N_FFT // HOP     # кадр окна, на котором заканчивается звук


@dataclass
class Event:
    seg: np.ndarray
    onset: int                 # абсолютный индекс примерного начала звука
    src: str                   # "matched" | "energy"
    hint_last: int = None      # для "matched": на каком кадре сегмента звук заканчивается
    t0: int = 0                # границы вырезанного куска (абсолютные индексы отсчётов)
    t1: int = 0


class MatchedDetector:
    """Потоковый детектор ПО ЭТАЛОНАМ. Каждые ~21 мс сравнивает последний кусок звука с каждым эталоном
    (устойчивым методом). Реагирует на сам звук, а не на громкость: ловит тихие звуки интерфейса
    среди громких игровых и не реагирует на шаги, выстрелы и музыку."""
    KEEP_F = 600
    QUANT = 16                 # для грубой оценки длины эталонов округляются до кратных 16 кадрам
    TOPK = 6                   # сколько лучших по грубой оценке кандидатов проверять точно

    def __init__(self, matcher, trig=0.62, topk=None):
        self.trig = trig
        self.topk = topk or self.TOPK
        self.tpl = matcher.tpl
        tmp = {}                                   # грубая оценка: один матричный продукт на все эталоны
        for i, t in enumerate(self.tpl):
            S = logmel(t["wave"])
            fl = S.max() - DB_RANGE
            S = np.maximum(S, fl)
            T = S.shape[1]
            Tq = -(-T // self.QUANT) * self.QUANT
            if Tq > T:
                S = np.pad(S, ((0, 0), (0, Tq - T)), constant_values=fl)
            v = S.ravel() - S.mean()
            tmp.setdefault(Tq, []).append((i, v / (np.linalg.norm(v) + 1e-9)))
        self.banks = {Tq: (np.stack([v for _, v in l]).astype(np.float32), [i for i, _ in l]) for Tq, l in tmp.items()}
        self.buf = np.zeros(0, np.float32)
        self.base = 0
        self.S = np.zeros((N_MELS, 0), np.float32)
        self.nS = 0
        self.cand, self.drop, self.armed, self.last_end = None, 0, True, -10 ** 9
        self.keep = int(3.0 * SR)

    def feed(self, x):
        out = []
        self.buf = np.concatenate([self.buf, np.asarray(x, np.float32)])
        total = self.base + len(self.buf)
        n_ready = (total - N_FFT) // HOP + 1 if total >= N_FFT else 0
        if n_ready > self.nS:
            new = logmel(self.buf[self.nS * HOP - self.base:])
            self.S = np.concatenate([self.S, new], axis=1)[:, -self.KEEP_F:]
            self.nS = n_ready
            out = self._evaluate(total)
        drop = min(len(self.buf) - self.keep, self.nS * HOP - self.base)
        if drop > 0:
            self.buf, self.base = self.buf[drop:], self.base + drop
        return out

    NAL = 4                                   # сколько последних положений окна проверять (= кадров в блоке)

    def _evaluate(self, total):
        F = self.S.shape[1]
        NAL = self.NAL
        floor_all = self.S.max() - 90.0
        coarse = []                                           # этап 1: грубо по всем эталонам сразу
        for Tq, (V, idxs) in self.banks.items():
            if F < Tq + NAL - 1:
                continue
            W = np.transpose(sliding_window_view(self.S[:, -(Tq + NAL - 1):], Tq, axis=1), (1, 0, 2))   # (NAL, mels, Tq)
            W = np.maximum(W, W.max(axis=(1, 2), keepdims=True) - DB_RANGE).reshape(NAL, -1)
            W = W - W.mean(1, keepdims=True)
            W /= np.linalg.norm(W, axis=1, keepdims=True) + 1e-9
            coarse += list(zip((V @ W.T).max(axis=1).tolist(), idxs))
        coarse.sort(reverse=True)
        best, bT, b_al = -1.0, 0, 0
        for c, i in coarse[:self.topk]:                       # этап 2: точно только для лучших кандидатов
            t = self.tpl[i]
            T = t["T"]
            W = np.transpose(sliding_window_view(self.S[:, -(T + NAL - 1):], T, axis=1), (1, 0, 2))
            r = robust_scores(np.maximum(W, floor_all), t)
            j = int(np.argmax(r))
            if r[j] > best:
                best, bT, b_al = float(r[j]), T, NAL - 1 - j     # b_al: на сколько кадров назад закончился звук
        end = (self.nS - 1) * HOP + N_FFT - b_al * HOP         # абсолютный индекс конца звука на лучшем положении
        end_now = (self.nS - 1) * HOP + N_FFT
        if not self.armed and best < self.trig - 0.05 and end_now - self.last_end > 0.2 * SR:
            self.armed = True
        if self.armed and best >= self.trig:
            if self.cand is None or best > self.cand[0]:
                self.cand, self.drop = (best, end, bT), 0
            else:
                self.drop += 1
        elif self.cand is not None:
            self.drop += 1
        res = []
        if self.cand is not None and (self.drop >= 2 or end_now - self.cand[1] > 0.15 * SR):
            _, e, T = self.cand
            self.cand, self.armed, self.last_end = None, False, e
            s0 = e - int(BACK_S * SR)
            s1 = min(e + int(FWD_S * SR), total)
            if s0 >= self.base:
                res.append(Event(self.buf[s0 - self.base:s1 - self.base].copy(), e - T * HOP, "matched", HINT_LAST, s0, s1))
        return res


class Pipeline:
    """Два детектора вместе: по эталонам (тихие звуки в шуме) + по громкости (запасной)."""

    def __init__(self, matcher=None, sens=70, matched=True, energy=True):
        p = sens_params(sens)
        self.energy = EventDetector(p["thr_db"], p["min_db"]) if energy else None
        self.matched = MatchedDetector(matcher, p["trig"]) if (matched and matcher is not None and matcher.tpl) else None

    def feed(self, x):
        evs = []
        if self.matched:
            evs += self.matched.feed(x)
        if self.energy:
            n0 = len(self.energy.onsets)
            segs = self.energy.feed(x)
            evs += [Event(sg, on, "energy", None, on - int(PRE_S * SR), on + int(POST_S * SR))
                    for sg, on in zip(segs, self.energy.onsets[n0:])]
            if len(self.energy.onsets) > 64:
                del self.energy.onsets[:-8]
        return evs


class Deduper:
    """Один звук могут найти оба детектора. Запоминаем только то, что реально показали пользователю."""

    def __init__(self):
        self.recent = deque(maxlen=8)

    def fresh(self, onset):
        return not any(abs(onset - r) < 0.30 * SR for r in self.recent)

    def add(self, onset):
        self.recent.append(onset)


def confusable(x, lib):
    """Группы реально путаются по звуку: совпадает форма волны или общий балл дошёл до порога.
    (Одного спектрального балла мало: у любых двух звуков он уже высокий.)"""
    return x.ncc >= 0.30 or x.fused >= lib.cfg["accept"]


def fmt(res, lib):
    """-> (заголовок, подробности) для консоли"""
    if res.status == "unknown" or not res.top:
        sc = f"лучший кандидат {res.top[0].fused:.2f}" if res.top else "нет данных"
        return "Не опознано", sc
    top = res.top
    if res.status == "ok":
        head = lib.describe(top[0].gid)
    else:
        near = [x for x in top[:3] if top[0].fused - x.fused < 0.10]
        head = "Похоже: " + "  ИЛИ  ".join(lib.describe(x.gid, 2) for x in near)
    lines = [f"{i + 1}. {lib.describe(x.gid, 2)}\n    общий {x.fused:.2f}  (спектр {x.spec:.2f}, волна {x.ncc:.2f})"
             for i, x in enumerate(top[:3])]
    return head, "\n".join(lines)


RANK = {"Mythic": 0, "Legendary": 1, "Epic": 2, "Rare": 3, "Common": 4}
KIND = {"Mythic": "mythic", "Legendary": "legendary", "Epic": "epic", "Rare": "rare", "Common": "common"}


def parse_size(s):
    """'2x3' / '2х3' / '3*2' -> (2, 3). Повёрнутый предмет (3x2) считается тем же размером."""
    s = (s or "").lower().replace("х", "x").replace("*", "x").replace("×", "x").strip()
    m = re.fullmatch(r"(\d+)\s*x\s*(\d+)", s)
    return tuple(sorted((int(m.group(1)), int(m.group(2))))) if m else None


def _rows(lib, gid, want, limit):
    g = lib.group(gid)
    items = sorted(g["items"], key=lambda i: RANK.get(i.get("rarity"), 0 if i.get("red") else 5))
    shown = [i for i in items if not want or parse_size(i.get("size")) in (want, None)]
    skipped = len(items) - len(shown)
    lines = []
    for it in shown[:limit]:
        kind = KIND.get(it.get("rarity"), "mythic" if it.get("red") else "item")
        lines.append((kind, it["name"], it.get("size") or ""))
    extra = max(0, len(shown) - limit)
    notes = []
    if want and not shown:
        notes.append(("dim", "среди известных предметов группы нет такого размера", ""))
    if extra:
        notes.append(("dim", f"ещё {extra}", ""))
    if g.get("more"):
        notes.append(("dim", f"+{g['more']} не видно в базе" if want else f"+{g['more']} ещё (см. сайт)", ""))
    if want and skipped:
        notes.append(("dim", f"скрыто по размеру: {skipped}", ""))
    return lines + notes


def build_view(res, lib, size=None, max_items=6):
    """Результат -> строки для оверлея [(kind, текст, размер)] либо None (показывать нечего).
    kind: head_ok | head_maybe | sub | mythic | legendary | epic | rare | common | item | dim"""
    if res is None or res.status == "unknown" or not res.top:
        return None
    want = parse_size(size)

    def title(x):
        g = lib.group(x.gid)
        return g.get("tag") or (g["items"][0]["name"] if g["items"] else x.gid)

    top = res.top[0]
    if res.status == "ok":
        return [("head_ok", title(top), f"{round(top.fused * 100)}%")] + _rows(lib, top.gid, want, max_items)
    near = [x for x in res.top[:3] if top.fused - x.fused < 0.10][:2]
    lines = [("head_maybe", "Похоже, это одно из:", "")]
    for x in near:
        lines.append(("sub", title(x), f"{round(x.fused * 100)}%"))
        lines += _rows(lib, x.gid, want, 3)
    return lines


# ───────────────────────────── захват звука ─────────────────────────────
# Используем abi_audio.py (несколько бэкендов: PyAudioWPatch / soundcard / sounddevice).
# Если устройство не указано — автоматически берём лучший доступный источник.

def list_devices_data():
    """-> ([(имя, это loopback?)], имя источника по умолчанию)."""
    import abi_audio as aa
    errs = []
    cat = aa.list_sources(errs)
    if not cat:
        msg = "; ".join(errs) if errs else "устройства не найдены"
        raise RuntimeError(msg)
    mics = [(s.name, s.kind == "loopback") for s in cat]
    default = ""
    for s in cat:
        if s.kind == "loopback" and s.is_default:
            default = s.name
            break
    if not default:
        for s in cat:
            if s.kind == "loopback":
                default = s.name
                break
    if not default and cat:
        default = cat[0].name
    return mics, default or "авто"


def list_devices():
    mics, default = list_devices_data()
    print("Источники звука (для --device достаточно части названия):")
    for name, lb in mics:
        print(("  [loopback] " if lb else "  [вход]     ") + name)
    print("\nПо умолчанию:", default)


class Source:
    """Поток захвата звука. Устройство можно не указывать — берётся автоматически."""

    def __init__(self, device=None, sens=70):
        self.q = queue.Queue()
        self.device, self.sens = device, sens
        self.mode, self.matcher = "idle", None
        self._dirty = True
        self._stop = threading.Event()
        self._dd = Deduper()
        self._cap = None

    def configure(self, mode=None, matcher=None, sens=None):
        if mode is not None:
            self.mode = mode
        if matcher is not None:
            self.matcher = matcher
        if sens is not None:
            self.sens = sens
        self._dirty = True

    def start(self):
        self._stop.clear()
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self._stop.set()
        if self._cap is not None:
            try:
                self._cap.close()
            except Exception:
                pass
            self._cap = None

    def drain(self):
        while not self.q.empty():
            try:
                self.q.get_nowait()
            except queue.Empty:
                break

    def _build(self):
        if self.mode == "listen" and self.matcher is not None:
            return Pipeline(self.matcher, self.sens, matched=True, energy=True)
        if self.mode == "record":
            return Pipeline(None, self.sens, matched=False, energy=True)
        return None

    def _emit(self, ev):
        if self.mode == "record":
            self.q.put(("seg", ev.seg))
        elif self.mode == "listen" and self.matcher is not None:
            if not self._dd.fresh(ev.onset):
                return
            res = self.matcher.classify(ev.seg, hint_last=ev.hint_last)
            if res.status == "unknown":
                self.q.put(("unknown", res.top[0].fused if res.top else 0.0))
            else:
                self._dd.add(ev.onset)
                self.q.put(("result", res, ev.src))

    def _run(self):
        try:
            import abi_audio as aa
            # device=None → abi_audio сам выберет лучший loopback / вход
            self._cap = aa.open_capture(self.device or None)
            pipe, last = None, 0.0
            while not self._stop.is_set():
                if self._dirty:
                    self._dirty = False
                    pipe = self._build()
                x = self._cap.read(0.25)
                if x is None:
                    continue
                if pipe is not None:
                    for ev in pipe.feed(x):
                        self._emit(ev)
                if time.time() - last > 0.1:
                    last = time.time()
                    self.q.put(("level", 10 * np.log10(float(np.mean(x ** 2)) + 1e-12)))
        except Exception as e:                # noqa
            self.q.put(("error", str(e)))
        finally:
            if self._cap is not None:
                try:
                    self._cap.close()
                except Exception:
                    pass
                self._cap = None

    def next_segment(self):
        while True:
            item = self.q.get()
            if item[0] == "seg":
                return item[1]
            if item[0] == "error":
                raise SystemExit("Ошибка захвата звука: " + item[1])



# ───────────────────────────── данные сайта abibuilder.com/merchant ─────────────────────────────
# номер группы звука | сколько предметов скрыто за «+N» на сайте | видимые предметы: имя;размер;редкость
# (C обычный, R редкий, E эпический, L легендарный/золотой, M мифический/красный)
SITE_RAW = """
1|0|Film Stock;2x2;L,Oil;1x2;E,Kerosene Lamp;1x2;E
2|0|Fuel Tank;2x3;L,High Energy Fuel;2x2;L,Oil Drum;2x2;E,Propane Tank;2x2;E,Portable Fuel Canister;1x2;E
3|0|Large-Caliber Artillery Gunpowder Pack;3x2;L,Nitrocellulose;2x2;E,Paracord;2x2;E,Camping Tarp;2x1;R
4|0|Impact-resistant Hard Rubber;2x2;E
5|0|Bag of Cement;2x2;E
6|0|"Destiny" Gold Clay Tablet;3x2;M,Titanium Alloy Plate;2x3;L,Car Battery;3x2;L
7|0|Rubber Pad;2x2;R
8|0|Record Player;2x2;L,Vintage Typewriter;2x2;L,Acoustic Panel;2x2;R,Wood Board;2x1;R
9|7|Sound-Muffling Cotton;3x2;L,Hearing Aid;1x1;E,Bios.;1x1;E,Rangefinder Binoculars;2x1;E
10|29|TI Module;2x1;M,Target Positioning Module;2x2;M,Palladium Wire;2x2;L,UPS Unit;3x2;L
11|0|Power Cable;2x1;R
12|0|Graphics Card;2x1;L,PSU;2x2;E,SSD;1x1;E
13|0|Working LCD;2x1;E,Broken LCD;2x1;R
14|0|Double-Neck Lute;2x3;M,Studio Monitor;2x2;L
15|0|Battery;1x1;C,D-size Battery;1x1;C
16|0|Solar Panel;1x1;E
17|5|Battery-Powered Positive Electrode Module;3x2;L,Hydraulic Expander;2x3;L,Arched Corrugated Steel Plate;2x2;L,Portable Jack;2x1;E
18|25|Skyhook Synchronized Array Mirror;1x2;M,Quantum 2000 Toy Robot;2x3;M,Hefra Egg;2x2;M,Lyre;2x2;M
19|0|Toolbox;2x1;R
20|0|Blowtorch;1x2;L
21|0|Photo Frame;2x1;R
22|25|Voice of Civilization Gold Record;2x2;M,EEG;2x2;M,Gilded Cage Music Box;2x3;M,Phaethon's Clock;2x2;M
23|0|Bag of Fertilizer;2x2;E
24|0|Deluxe Tea Set;2x2;L,Pheromone Test Kit;3x2;L,Blocker Injector;1x1;L
25|0|Lute;2x2;E
26|0|Gold-rimmed Glasses;2x1;E,Syringe;1x1;R
27|0|Medical Sponge;1x1;C
28|0|Gold Lion Statue;3x2;M,Gold Cheetah Statue;2x1;L
29|0|Gold Bracelet;1x1;L,Commemorative Coin;1x1;E
30|0|Vase;2x2;M,Northern Stars Amber Drink;2x2;M,Mutant Heart Sample;2x3;L
31|0|Desk Clock;1x2;E
32|0|Antique Teapot;2x2;M
33|2|Gem Necklace;2x2;M,Star of Kamona Pendant;2x2;M,Diamond Ring;1x1;L,Brooch;1x1;L
34|0|Stamp Album;2x2;L
35|0|Dried Flower Bookmark;1x1;L,Mutant Evolution Sketch;2x2;L,Stamp;1x1;R,Document;2x1;R
36|5|Majestic Sculpture;2x3;M,Gold Snake;2x2;M,Optoelectronic Hyperspace Model;2x2;M,Gold Cube;1x1;L
37|0|Folder;2x1;L,Bundle of Rice Paper;1x2;E,Hardcover;1x2;R
38|0|Military Radio;2x2;L,Classic Radio;2x1;R
"""
RARITY = {"C": "Common", "R": "Rare", "E": "Epic", "L": "Legendary", "M": "Mythic"}


def site_groups():
    out = {}
    for line in SITE_RAW.strip().splitlines():
        sid, more, items = line.split("|", 2)
        its = []
        for part in items.split(","):
            name, size, r = part.split(";")
            its.append({"name": name, "size": size, "rarity": RARITY[r], "red": r == "M"})
        out[int(sid)] = (its, int(more))
    return out


# ───────────────────────────── команды ─────────────────────────────
def cmd_record(a):
    lib = Library(a.lib)
    src = Source(a.device, a.sens)
    src.configure(mode="record")
    src.start()
    print("Режим записи. Для каждого предмета: введи название и перетащи его в игре несколько раз.")
    print("Совет: в настройках игры убери музыку/окружение/голоса — оставь только эффекты и интерфейс.\n")
    while True:
        name = input("Название предмета (пусто = выход): ").strip()
        if not name:
            break
        size = input("Размер в клетках, напр. 2x3 (Enter — пропустить): ").strip()
        red = input("Красный предмет? [y/N]: ").strip().lower().startswith("y")
        n = int(input("Сколько раз перетащить [5]: ").strip() or 5)
        item = {"name": name, "size": size, "red": red}
        src.drain()
        takes = []
        print(f"→ Тащи «{name}» {n} раз(а), между перетаскиваниями пауза ≥ 1 с.")
        while len(takes) < n:
            r = trim_event(src.next_segment())
            if r is None:
                print("  (шум, пропустил)")
                continue
            w, snr = r
            takes.append(w)
            print(f"  дубль {len(takes)}/{n}: {len(w) / SR:.2f} с, SNR {snr:.0f} дБ" + ("  ← низкий, мешает посторонний звук?" if snr < 15 else ""))
        # не совпадает ли звук с уже записанной группой?
        target = None
        if lib.groups:
            m = Matcher(lib)
            r = m.classify(pad(takes[0]))
            if r.top and r.top[0].fused >= max(lib.cfg["accept"], 0.85):
                g = r.top[0].gid
                ans = input(f"  Звук совпал с группой [{lib.describe(g)}] ({r.top[0].fused:.2f}). Добавить предмет в неё? [Y/n]: ")
                if not ans.strip().lower().startswith("n"):
                    target = g
        if target:
            lib.add_item(target, item, takes)
            print(f"  ✓ добавлено в группу {target}\n")
        else:
            print(f"  ✓ новая группа {lib.add_group(item, takes)}\n")


def cmd_import(a):
    """Загружает звуки из папки: 18_pickup.ogg, 18_drop.ogg, 22_pickup.ogg … (число = ID группы на сайте)."""
    if not os.path.isdir(a.folder):
        raise SystemExit(f"Папка «{a.folder}» не найдена. Создай её и положи туда файлы вида 18_pickup.ogg")
    site, lib = site_groups(), Library(a.lib)
    files = sorted(f for f in os.listdir(a.folder) if f.lower().endswith((".ogg", ".wav", ".flac", ".mp3", ".opus")))
    if not files:
        raise SystemExit("В папке нет звуковых файлов (ogg/wav/flac/mp3).")
    loaded = []
    for f in files:
        m = re.match(r"\D*(\d+)", f)
        if not m or int(m.group(1)) not in site:
            print(f"  пропуск {f}: в имени нет номера группы 1–38")
            continue
        sid = int(m.group(1))
        drop = "drop" in f.lower()
        w = trim_clip(load_audio(os.path.join(a.folder, f)))
        if w is None:
            print(f"  пропуск {f}: пустой или слишком короткий звук")
            continue
        items, more = site[sid]
        gid = f"s{sid:02d}{'d' if drop else 'p'}"
        lib.replace_group(gid, {"items": items, "more": more, "tag": f"#{sid} {'drop' if drop else 'pick-up'}"}, [w])
        loaded.append(gid)
        reds = sum(i["red"] for i in items)
        print(f"  ✓ {f:24s} -> группа #{sid} {'drop' if drop else 'pick-up'}, {len(w) / SR:.2f} с"
              + (f", красных предметов видно: {reds}" if reds else ""))
    lib.save()
    if not loaded:
        return
    print(f"\nЗагружено групп: {len(loaded)}. Проверяю, какие звуки между собой слишком похожи…")
    m = Matcher(lib)
    pairs = {}
    for gid in loaded:
        r = m.classify(pad(lib.wav(lib.group(gid)["takes"][0])), exclude=(gid, 0))
        for x in r.top[:3]:
            if confusable(x, lib):
                key = tuple(sorted((gid, x.gid)))
                pairs[key] = max(pairs.get(key, 0.0), round(x.fused, 2))
    if pairs:
        print("Похожие по звуку группы (по одному звуку не различить — смотри размер/описание):")
        for (x, y), sc in sorted(pairs.items()):
            print(f"  {lib.group(x).get('tag', x)}  ≈  {lib.group(y).get('tag', y)}   ({sc})")
    else:
        print("Все загруженные звуки заметно отличаются друг от друга.")
    print("\nДальше: python abi_sound_id.py listen  — и проверь на паре предметов в игре.\n"
          "Если звук в игре распознаётся плохо, запиши предмет через record: игровой дубль добавится в ту же группу.")


def cmd_list(a):
    lib = Library(a.lib)
    for g in lib.groups:
        print(f"{g['id']}  дублей: {len(g['takes'])}  {lib.describe(g['id'], 20)}")
    print(f"\nГрупп: {len(lib.groups)}  порог accept={lib.cfg['accept']} margin={lib.cfg['margin']}")


def cmd_check(a, lib=None, quiet=False):
    lib = lib or Library(a.lib)
    m = Matcher(lib)
    gen, imp, correct, total, conf = [], [], 0, 0, set()
    for g in lib.groups:
        if len(g["takes"]) < 2:
            continue
        for i, fn in enumerate(g["takes"]):
            r = m.classify(pad(lib.wav(fn)), exclude=(g["id"], i))
            gs = next((x.fused for x in r.top if x.gid == g["id"]), 0.0)
            others = [x for x in r.top if x.gid != g["id"]]
            gen.append(gs)
            imp.append(max((x.fused for x in others), default=0.0))
            total += 1
            correct += bool(r.top) and r.top[0].gid == g["id"]
            for x in others:
                if confusable(x, lib):
                    conf.add(tuple(sorted((g["id"], x.gid))))
    if not total:
        print("Нужно минимум 2 дубля хотя бы у одной группы (лучше 4–6).")
        return None
    gen, imp = np.array(gen), np.array(imp)
    p5, imax = float(np.percentile(gen, 5)), float(imp.max())
    print(f"Групп: {len(lib.groups)}, проверок «исключили дубль»: {total}")
    print(f"Верный топ-1: {correct}/{total} ({100 * correct / total:.1f}%)")
    print(f"Свои:   5%-квантиль {p5:.2f}, медиана {np.median(gen):.2f}")
    print(f"Чужие:  95%-квантиль {np.percentile(imp, 95):.2f}, максимум {imax:.2f}")
    if conf:
        print("\nПохожие по звуку группы (по одному звуку не различить — смотри размер/описание):")
        for x, y in sorted(conf):
            print(f"  [{lib.describe(x, 3)}]  ≈  [{lib.describe(y, 3)}]")
    thr = round((p5 + imax) / 2, 2) if p5 > imax else round(imax + 0.05, 2)
    thr = round(min(0.75, max(0.60, thr)), 2)        # ложные срабатывания и так отсекает проверка формы волны
    print(f"\nРекомендуемый accept = {thr}" + ("" if p5 > imax else "  (внимание: свои и чужие пересекаются, добавь дублей)"))
    if getattr(a, "apply", False):
        lib.cfg["accept"] = thr
        lib.save()
        print("Сохранено в library.json")
    return correct / total


def cmd_listen(a):
    """Консольный режим. Оверлей с окном — в abi_gui.py."""
    lib = Library(a.lib)
    if not lib.groups:
        raise SystemExit("Библиотека пуста. Сначала: python abi_sound_id.py import  или  record")
    if a.accept:
        lib.cfg["accept"] = a.accept
    if a.min_wave is not None:
        lib.cfg["min_wave"] = a.min_wave
    m = Matcher(lib)
    src = Source(a.device, a.sens)
    src.configure(mode="listen", matcher=m)
    src.start()
    print("Слушаю… (Ctrl+C — выход)")
    while True:
        item = src.q.get()
        if item[0] == "error":
            raise SystemExit("Ошибка захвата звука: " + item[1])
        if item[0] == "result":
            h, b = fmt(item[1], lib)
            print("\n>>>", h, "\n" + b)


# ───────────────────────────── самопроверка на синтетике ─────────────────────────────
def cmd_selftest(a):
    """Проверка всей цепочки без игры: синтетические «звуки предметов», фон и громкие «игровые» шумы."""
    from scipy.signal import resample
    rng = np.random.default_rng(7)

    def synth():
        dur = rng.uniform(0.15, 0.45)
        n = int(dur * SR)
        t = np.arange(n) / SR
        f = np.fft.rfftfreq(n, 1 / SR)
        sp = np.fft.rfft(rng.standard_normal(n)) * np.exp(-0.5 * ((f - rng.uniform(600, 9000)) / rng.uniform(500, 4000)) ** 2)
        x = np.fft.irfft(sp, n)
        x /= np.abs(x).max()
        tones = sum(rng.uniform(.2, .6) * np.sin(2 * np.pi * rng.uniform(300, 6000) * t + rng.uniform(0, 6.28))
                    * np.exp(-t / (dur / rng.uniform(1.5, 4))) for _ in range(rng.integers(1, 4)))
        y = (0.7 * x + 0.5 * tones) * np.exp(-t / (dur / rng.uniform(2.5, 5))) * (1 - np.exp(-t / 0.002))
        return (y / np.abs(y).max()).astype(np.float32)

    def pink(n, db):
        f = np.fft.rfftfreq(n, 1 / SR)
        f[0] = f[1]
        x = np.fft.irfft(np.fft.rfft(rng.standard_normal(n)) / np.sqrt(f), n)
        return (x / np.sqrt(np.mean(x ** 2)) * 10 ** (db / 20)).astype(np.float32)

    def burst():                                   # громкий «игровой» звук: шаг, выстрел, удар
        dur = rng.uniform(0.06, 0.25)
        n = int(dur * SR)
        t = np.arange(n) / SR
        f = np.fft.rfftfreq(n, 1 / SR)
        x = np.fft.irfft(np.fft.rfft(rng.standard_normal(n)) * np.exp(-0.5 * ((f - rng.choice([150, 400, 3000, 6000])) / rng.uniform(100, 3000)) ** 2), n)
        x *= np.exp(-t / (dur / 3))
        return (x / np.abs(x).max()).astype(np.float32)

    base = [synth() for _ in range(12)]
    sounds = base + [resample(b, int(len(b) / 1.05)).astype(np.float32) for b in base[:3]]
    with tempfile.TemporaryDirectory() as td:
        lib = Library(td)
        for gi, snd in enumerate(sounds):                          # «запись» в тихой комнате
            takes = []
            for _ in range(3):
                y = pink(int(3 * SR), -62)
                o = int(1.2 * SR)
                y[o:o + len(snd)] += snd * 10 ** (rng.uniform(-24, -14) / 20)
                det, segs = EventDetector(10.0), []
                for i in range(0, len(y), BLOCK):
                    segs += det.feed(y[i:i + BLOCK])
                takes += [r[0] for r in map(trim_event, segs) if r]
            lib.add_group({"name": f"sound{gi:02d}", "size": "", "red": True}, takes)
        print(f"Синтетика: {len(sounds)} звуков (3 из них — копии с питчем ×1.05), чувствительность {a.sens:g}")
        cmd_check(argparse.Namespace(apply=False), lib)
        m = Matcher(lib)
        print("\nСценарии (по 20 событий; «мир» = громкие шаги/выстрелы, 1.5 в секунду):")
        for title, bed, world, ui in [("тишина", -62, (-60, -55), (-30, -20)),
                                      ("игра: звуки интерфейса на 10–20 дБ тише мира", -50, (-22, -10), (-40, -30))]:
            n, gap = 20, 1.5
            total = int((n + 1) * gap * SR)
            y = pink(total, bed)
            for _ in range(int(total / SR * 1.5)):
                o = int(rng.uniform(.2, total / SR - .6) * SR)
                b = burst()
                y[o:o + len(b)] += b * 10 ** (rng.uniform(*world) / 20)
            idx = rng.integers(0, len(sounds), n)
            starts = []
            for k, i in enumerate(idx):
                o = int(((k + 1) * gap + rng.uniform(0, .3)) * SR)
                starts.append(o)
                y[o:o + len(sounds[i])] += sounds[i] * 10 ** (rng.uniform(*ui) / 20)
            pipe, dd, shown = Pipeline(m, a.sens), Deduper(), []
            for i in range(0, len(y), BLOCK):
                for ev in pipe.feed(y[i:i + BLOCK]):
                    if dd.fresh(ev.onset):
                        r = m.classify(ev.seg, hint_last=ev.hint_last)
                        if r.status != "unknown":
                            dd.add(ev.onset)
                            shown.append((ev, r.status, int(r.top[0].gid[1:]) - 1))
            hit, wrong, phantom = set(), 0, 0
            for ev, st, g in shown:
                inside = [k for k, o in enumerate(starts) if ev.t0 - 0.05 * SR <= o <= ev.t1 - 0.15 * SR]
                if not inside:
                    phantom += 1
                elif any(idx[k] == g for k in inside):
                    hit.add([k for k in inside if idx[k] == g][0])
                else:
                    wrong += 1
            print(f"  {title:48s} верно показано {len(hit):2d}/{n} | неверных {wrong} | ложных показов {phantom}")


def main():
    p = argparse.ArgumentParser(description="ABI Sound ID")
    p.add_argument("cmd", choices=["devices", "record", "import", "list", "check", "listen", "selftest"])
    p.add_argument("folder", nargs="?", default="sounds", help="import: папка с файлами")
    p.add_argument("--lib", default="abi_library", help="папка библиотеки")
    p.add_argument("--device", help="часть названия источника (по умолчанию loopback динамиков)")
    p.add_argument("--sens", type=float, default=70.0, help="чувствительность 0…100 (больше = ловит тише, по умолчанию 70)")
    p.add_argument("--min-wave", type=float, help="минимальное совпадение формы волны (0.15 по умолчанию; 0 = не проверять)")
    p.add_argument("--accept", type=float, help="порог уверенности (по умолчанию из библиотеки, обычно 0.75; "
                                                "если всё показывает «Похоже» вместо точного ответа — попробуй 0.6)")
    p.add_argument("--apply", action="store_true", help="check: сохранить рекомендованный порог")
    a = p.parse_args()
    {"devices": lambda a: list_devices(), "record": cmd_record, "import": cmd_import, "list": cmd_list, "check": cmd_check,
     "listen": cmd_listen, "selftest": cmd_selftest}[a.cmd](a)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
