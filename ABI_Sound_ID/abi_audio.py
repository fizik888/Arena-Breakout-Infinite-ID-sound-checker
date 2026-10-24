# -*- coding: utf-8 -*-
"""
Захват звука для ABI Sound ID.

Несколько способов слушать (используется первый, который работает; остальные — запасные):
  1. PyAudioWPatch — WASAPI loopback (слушает то, что играет в наушниках/колонках). Лучший вариант на Windows.
  2. soundcard     — тоже умеет loopback.
  3. sounddevice   — только устройства-входы: микрофон, «Стерео микшер», виртуальный кабель (VB-Cable Output).

Любое устройство читается в отдельном потоке; если оно «молчит» (Windows не отдаёт данные при тишине),
программа это переживает и не зависает.
"""
import platform
import queue
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from math import gcd

import numpy as np
from scipy.signal import resample_poly

SR = 48000
BLOCK = 1024

NO_SOURCES_HELP = (
    "Не найден ни один способ слушать звук.\n"
    "Установи хотя бы один из пакетов (в командной строке):\n"
    "    pip install PyAudioWPatch\n"
    "    pip install soundcard\n"
    "    pip install sounddevice\n"
    "Если пакет не ставится, это нормально — достаточно, чтобы встал любой из трёх."
)


class AudioError(Exception):
    pass


def _com():
    """Windows: поток, работающий со звуком, должен инициализировать COM."""
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.ole32.CoInitializeEx(None, 0)
        except Exception:
            pass


def norm_name(name):
    n = re.sub(r"\s*\[loopback\]\s*$", "", str(name), flags=re.I)
    return re.sub(r"\s+", " ", n).strip().lower()


# ───────────────────────────── ресемплер ─────────────────────────────
class Resampler:
    """Потоковое приведение к 48 кГц без щелчков на стыках блоков (с запасом контекста слева и справа)."""

    def __init__(self, sr_in):
        self.sr_in = int(sr_in)
        self.passthrough = self.sr_in == SR
        if self.passthrough:
            return
        g = gcd(self.sr_in, SR)
        self.up, self.down = SR // g, self.sr_in // g
        self.ctx = self.down * max(1, -(-64 // self.down))            # кратно down => целое число выходных отсчётов
        self.chunk = self.down * max(1, round(1024 / self.up))
        self.prev = np.zeros(self.ctx, np.float32)
        self.buf = np.zeros(0, np.float32)

    def push(self, x):
        x = np.asarray(x, np.float32)
        if self.passthrough:
            return [x]
        self.buf = np.concatenate([self.buf, x])
        out = []
        while len(self.buf) >= self.chunk + self.ctx:
            c = self.buf[:self.chunk]
            right = self.buf[self.chunk:self.chunk + self.ctx]
            y = resample_poly(np.concatenate([self.prev, c, right]), self.up, self.down)
            a = self.ctx * self.up // self.down
            out.append(y[a:a + self.chunk * self.up // self.down].astype(np.float32))
            self.prev = c[-self.ctx:]
            self.buf = self.buf[self.chunk:]
        return out


# ───────────────────────────── описание устройств ─────────────────────────────
@dataclass
class SourceInfo:
    kind: str                     # "loopback" (звук динамиков/наушников) | "input" (микрофон, микшер, кабель)
    name: str
    backend: object
    rate: int
    channels: int
    handle: object
    is_default: bool = False
    alts: list = field(default_factory=list)      # то же устройство, найденное другими способами (запасные)

    @property
    def key_id(self):
        return "%s|%s" % (self.kind, norm_name(self.name))

    @property
    def label(self):
        nm = re.sub(r"\s*\[loopback\]\s*$", "", self.name, flags=re.I)
        return ("[loopback] " if self.kind == "loopback" else "[вход] ") + nm + ("  ★ по умолчанию" if self.is_default else "")


class _Err:
    def __init__(self, e):
        self.e = e


class Capture:
    """Читает устройство в своём потоке и складывает 48-кГц моно блоки в очередь."""

    def __init__(self, info):
        self.info = info
        self.q = queue.Queue(maxsize=400)
        self._stop = threading.Event()
        self.opened = threading.Event()
        self.error = None

    def start(self):
        threading.Thread(target=self._guard, daemon=True).start()

    def _guard(self):
        try:
            _com()
            self._run()
        except Exception as e:                                  # noqa
            self.error = e
            self.opened.set()
            self._put(_Err(e))

    def _run(self):
        raise NotImplementedError

    def _put(self, x):
        try:
            self.q.put_nowait(x)
        except queue.Full:
            try:
                self.q.get_nowait()
                self.q.put_nowait(x)
            except (queue.Empty, queue.Full):
                pass

    def wait_open(self, timeout=6.0):
        if not self.opened.wait(timeout):
            raise AudioError("устройство не открылось за %d с" % timeout)
        if self.error is not None:
            raise AudioError("%s: %s" % (type(self.error).__name__, self.error))

    def read(self, timeout=0.25):
        """Следующий блок или None, если данных нет (тишина/не идёт звук). Ошибку устройства бросает."""
        try:
            x = self.q.get(timeout=timeout)
        except queue.Empty:
            return None
        if isinstance(x, _Err):
            raise AudioError("%s: %s" % (type(x.e).__name__, x.e))
        return x

    def close(self):
        self._stop.set()


# ───────────────────────────── способ 1: PyAudioWPatch ─────────────────────────────
def _paw():
    import pyaudiowpatch as pa
    return pa


class PAWCapture(Capture):
    def _run(self):
        pa = _paw()
        p = pa.PyAudio()
        stream = None
        try:
            i = self.info
            stream = p.open(format=pa.paInt16, channels=i.channels, rate=i.rate, frames_per_buffer=BLOCK,
                            input=True, input_device_index=i.handle)
            self.opened.set()
            rs = Resampler(i.rate)
            while not self._stop.is_set():
                data = stream.read(BLOCK, exception_on_overflow=False)
                a = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
                if i.channels > 1:
                    a = a[:len(a) // i.channels * i.channels].reshape(-1, i.channels).mean(axis=1)
                for b in rs.push(a):
                    self._put(b)
        finally:
            try:
                if stream is not None:
                    stream.stop_stream()
                    stream.close()
            except Exception:
                pass
            p.terminate()


class PAWBackend:
    name = "PyAudioWPatch"

    def check(self):
        try:
            _paw()
            return True, ""
        except Exception as e:                                  # noqa
            return False, "%s: %s" % (type(e).__name__, e)

    def list(self):
        try:
            pa = _paw()
            p = pa.PyAudio()
        except Exception as e:
            raise RuntimeError("PyAudioWPatch init failed: %s" % e)
        out = []
        try:
            wasapi = p.get_host_api_info_by_type(pa.paWASAPI)
            default_name = ""
            try:
                default_name = p.get_device_info_by_index(wasapi["defaultOutputDevice"])["name"]
            except Exception:
                pass
            for d in p.get_loopback_device_info_generator():
                out.append(SourceInfo("loopback", d["name"], self, int(d["defaultSampleRate"]),
                                      max(1, int(d["maxInputChannels"])), int(d["index"]),
                                      bool(default_name) and default_name in d["name"]))
            for i in range(p.get_device_count()):
                d = p.get_device_info_by_index(i)
                if d.get("hostApi") == wasapi["index"] and d.get("maxInputChannels", 0) > 0 \
                        and not d.get("isLoopbackDevice", False):
                    out.append(SourceInfo("input", d["name"], self, int(d["defaultSampleRate"]),
                                          int(d["maxInputChannels"]), int(d["index"])))
        finally:
            p.terminate()
        return out

    def capture(self, info):
        return PAWCapture(info)


# ───────────────────────────── способ 2: soundcard ─────────────────────────────
def _sc():
    import numpy
    if not getattr(numpy, "_abi_fromstring_patched", False):
        orig = numpy.fromstring

        def fromstring(s, dtype=float, count=-1, sep="", **k):    # soundcard использует удалённый в новом numpy режим
            if sep == "":
                return numpy.frombuffer(s, dtype=dtype, count=count).copy()
            return orig(s, dtype=dtype, count=count, sep=sep, **k)

        numpy.fromstring = fromstring
        numpy._abi_fromstring_patched = True
    import soundcard as sc
    return sc


class SCCapture(Capture):
    def _run(self):
        sc = _sc()
        mic = sc.get_microphone(id=self.info.handle, include_loopback=True)
        with mic.recorder(samplerate=SR, blocksize=BLOCK) as rec:
            self.opened.set()
            while not self._stop.is_set():
                d = rec.record(numframes=BLOCK)
                self._put((d.mean(axis=1) if d.ndim > 1 else d).astype(np.float32))


class SCBackend:
    name = "soundcard"

    def check(self):
        try:
            _sc()
            return True, ""
        except Exception as e:                                  # noqa
            return False, "%s: %s" % (type(e).__name__, e)

    def list(self):
        # soundcard часто падає з Error 0x100000001 — не валимо всю програму
        try:
            _com()
            sc = _sc()
        except Exception as e:
            raise RuntimeError("soundcard недоступний: %s" % e)
        default = ""
        try:
            default = sc.default_speaker().name
        except Exception:
            pass
        out = []
        try:
            mics = list(sc.all_microphones(include_loopback=True))
        except Exception as e:
            raise RuntimeError("soundcard list failed: %s" % e)
        for m in mics:
            lb = bool(getattr(m, "isloopback", False))
            try:
                ch = max(1, int(m.channels))
            except Exception:
                ch = 2
            out.append(SourceInfo("loopback" if lb else "input", m.name, self, SR, ch, m.id, lb and m.name == default))
        return out

    def capture(self, info):
        return SCCapture(info)


# ───────────────────────────── способ 3: sounddevice ─────────────────────────────
class SDCapture(Capture):
    def _run(self):
        import sounddevice as sd
        i = self.info
        ch = max(1, min(i.channels, 2))
        with sd.InputStream(device=i.handle, channels=ch, samplerate=i.rate, blocksize=BLOCK, dtype="float32") as st:
            self.opened.set()
            rs = Resampler(i.rate)
            while not self._stop.is_set():
                data, _ = st.read(BLOCK)
                x = data.mean(axis=1) if data.ndim > 1 else data
                for b in rs.push(x):
                    self._put(b)


class SDBackend:
    name = "sounddevice"

    def check(self):
        try:
            import sounddevice  # noqa
            return True, ""
        except Exception as e:                                  # noqa
            return False, "%s: %s" % (type(e).__name__, e)

    def list(self):
        import sounddevice as sd
        apis, devs = sd.query_hostapis(), sd.query_devices()
        w = [k for k, a in enumerate(apis) if "WASAPI" in a["name"]]
        host = w[0] if w else None
        out = []
        for k, d in enumerate(devs):
            if d["max_input_channels"] > 0 and (host is None or d["hostapi"] == host):
                out.append(SourceInfo("input", d["name"], self, int(d["default_samplerate"]),
                                      int(d["max_input_channels"]), k))
        return out

    def capture(self, info):
        return SDCapture(info)


BACKENDS = [PAWBackend(), SDBackend()]       # soundcard вимкнено (ламається на цій системі)


# ───────────────────────────── каталог и выбор ─────────────────────────────
def list_sources(errors=None):
    """Все устройства, найденные любым способом, без повторов. В errors складываются причины сбоев."""
    merged, index = [], {}
    for b in BACKENDS:
        try:
            infos = b.list()
        except Exception as e:                                  # noqa
            if errors is not None:
                errors.append("%s: %s: %s" % (b.name, type(e).__name__, e))
            continue
        for info in infos:
            k = info.key_id
            if k in index:
                index[k].alts.append(info)
            else:
                index[k] = info
                merged.append(info)
    merged.sort(key=lambda s: (s.kind != "loopback", not s.is_default))
    return merged


def default_source(cat):
    for s in cat:
        if s.kind == "loopback" and (s.is_default or any(a.is_default for a in s.alts)):
            return s
    for s in cat:
        if s.kind == "loopback":
            return s
    return cat[0] if cat else None


def find_source(cat, spec):
    """spec — ключ из настроек (loopback|имя) или часть названия устройства."""
    if not spec:
        return None
    for s in cat:
        if s.key_id == spec:
            return s
    low = norm_name(spec)
    for s in cat:
        if low in norm_name(s.name):
            return s
    return None


def open_capture(spec=None, errors=None):
    """Открывает устройство (с запасными способами). Возвращает запущенный Capture. Бросает AudioError."""
    errs = [] if errors is None else errors
    cat = list_sources(errs)
    if not cat:
        raise AudioError(NO_SOURCES_HELP + ("\n\nПодробности:\n  " + "\n  ".join(errs) if errs else ""))
    info = find_source(cat, spec) if spec else default_source(cat)
    if info is None:
        raise AudioError("Источник «%s» не найден. Выбери другой в списке или нажми «Автоопределение»." % spec)
    problems = []
    for cand in [info] + info.alts:
        cap = cand.backend.capture(cand)
        cap.start()
        try:
            cap.wait_open()
            return cap
        except Exception as e:                                  # noqa
            cap.close()
            problems.append("%s: %s" % (cand.backend.name, e))
    raise AudioError("Не удалось открыть «%s» ни одним способом:\n  %s" % (info.name, "\n  ".join(problems)))


# ───────────────────────────── автоопределение ─────────────────────────────
def probe(seconds=3.0, cat=None):
    """Слушает ВСЕ устройства одновременно и меряет уровень. Во время проверки в игре/плеере должен идти звук."""
    cat = cat if cat is not None else list_sources()
    rows = []
    for info in cat:
        cap = info.backend.capture(info)
        cap.start()
        rows.append({"info": info, "cap": cap, "n": 0, "ss": 0.0, "peak": 0.0, "err": None, "ok": False})
    for r in rows:
        try:
            r["cap"].wait_open(5.0)
            r["ok"] = True
        except Exception as e:                                  # noqa
            r["err"] = str(e)
    t_end = time.time() + seconds
    while time.time() < t_end:
        for r in rows:
            if not r["ok"]:
                continue
            try:
                x = r["cap"].read(0.02)
            except AudioError as e:
                r["ok"], r["err"] = False, str(e)
                continue
            if x is not None and len(x):
                r["n"] += len(x)
                r["ss"] += float(np.sum(x.astype(np.float64) ** 2))
                r["peak"] = max(r["peak"], float(np.max(np.abs(x))))
    for r in rows:
        r["cap"].close()
        r["rms_db"] = 10 * np.log10(r["ss"] / r["n"] + 1e-12) if r["n"] else -120.0
        r["peak_db"] = 20 * np.log10(r["peak"] + 1e-9) if r["n"] else -120.0
    return rows


def pick_best(rows, floor_db=-65.0):
    """Лучший источник: сначала среди loopback (то, что слышно в наушниках), иначе среди входов."""
    for kind in ("loopback", "input"):
        c = [r for r in rows if r["ok"] and r["info"].kind == kind and r["rms_db"] > floor_db]
        if c:
            return max(c, key=lambda r: r["rms_db"])["info"]
    return None


def print_probe(rows, best=None):
    for r in sorted(rows, key=lambda r: -r["rms_db"]):
        i = r["info"]
        if not r["ok"]:
            print("   ошибка          %s   (%s)" % (i.label, (r["err"] or "")[:90]))
        else:
            mark = "  ← выбрано" if best is not None and i is best else ""
            print("  %6.1f дБ (пик %5.1f)  %s%s" % (r["rms_db"], r["peak_db"], i.label, mark))
    if best is None:
        print("Ни на одном устройстве не слышно звука. Включи в игре или плеере звук и повтори автоопределение.")


# ───────────────────────────── диагностика ─────────────────────────────
def diagnostics_text():
    L = ["ABI Sound ID — диагностика звука", "",
         "Python: %s (%s)" % (sys.version.split()[0], platform.architecture()[0]),
         "Система: %s" % platform.platform()]
    for mod in ("numpy", "scipy", "soundfile"):
        try:
            m = __import__(mod)
            L.append("%s: %s" % (mod, getattr(m, "__version__", "?")))
        except Exception as e:                                  # noqa
            L.append("%s: НЕТ (%s: %s)" % (mod, type(e).__name__, e))
    L += ["", "Способы захвата звука:"]
    for b in BACKENDS:
        ok, err = b.check()
        L.append("  %-14s %s" % (b.name, "установлен" if ok else "НЕТ  — " + err))
    errs = []
    cat = list_sources(errs)
    L += ["", "Найденные устройства (%d):" % len(cat)]
    for s in cat:
        via = ", ".join([s.backend.name] + [a.backend.name for a in s.alts])
        L.append("  %s   [%d Гц, %d кан.; через: %s]" % (s.label, s.rate, s.channels, via))
    if errs:
        L += ["", "Ошибки при поиске устройств:"] + ["  " + e for e in errs]
    if not cat:
        L += ["", NO_SOURCES_HELP]
    return "\n".join(L)
