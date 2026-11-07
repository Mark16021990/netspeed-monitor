#!/usr/bin/env python3
"""NetSpeed Monitor — монитор скорости интернета в реальном времени для Windows 11.

Показывает текущую скорость загрузки/отдачи:
  * значком в области уведомлений (трей) — две строки: ↓ зелёным, ↑ оранжевым;
  * компактным плавающим индикатором поверх всех окон;
  * главным окном 640×480 с графиком за последние 120 секунд, цифрами
    и шкалой загрузки канала провайдера (доля тарифа, Мбит/с).

Запуск:  python netspeed.py            — с окном
         pythonw netspeed.py --tray    — скрытый запуск (только трей)
         python netspeed.py --selftest 5 --screenshot out.png   — самопроверка

Зависимости: psutil, pystray, pillow (см. requirements.txt / install.bat).
"""
from __future__ import annotations

import argparse
import ctypes
import http.client
import json
import logging
import math
import os
import socket
import ssl
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Deque, Dict, Iterable, List, Optional, Tuple

import tkinter as tk
from tkinter import messagebox, ttk

APP_NAME = "NetSpeed Monitor"
VERSION = "1.3.0"
MUTEX_NAME = "Local\\NetSpeedMonitor_Mutex"
RUN_VALUE_NAME = "NetSpeedMonitor"
RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
ERROR_ALREADY_EXISTS = 183
ERROR_ACCESS_DENIED = 5

MIN_INTERVAL = 0.25
MAX_INTERVAL = 5.0
DEFAULT_INTERVAL = 0.5
DEFAULT_HISTORY = 120
DEFAULT_TARIFF = 1000.0   # тариф провайдера, Мбит/с
MAX_TARIFF = 100000.0

# Цвета
BG = "#1e1e1e"
BG2 = "#2a2a2a"
BG3 = "#333333"
FG = "#e6e6e6"
FG_DIM = "#9a9a9a"
GRID = "#3a3a3a"
GREEN = "#4caf50"
GREEN_FILL = "#2b4a2d"
AMBER = "#ffb74d"
RED = "#ef5350"          # ≥ 100 % тарифа
GREEN_LIGHT = "#a5d6a7"  # маркер пика на шкале канала
TRACK = "#2b2b2b"        # дорожка шкалы канала

# Фиксированные единицы скорости (без автомасштаба): режим -> (подпись, делитель байт/с)
UNIT_MODES: Dict[str, Tuple[str, float]] = {
    "kbit": ("Кбит/с", 1000.0 / 8.0),        # байт/с × 8 / 1000
    "mbit": ("Мбит/с", 1_000_000.0 / 8.0),   # байт/с × 8 / 1 000 000
    "kbyte": ("КБ/с", 1024.0),               # байт/с / 1024
    "mbyte": ("МБ/с", 1048576.0),            # байт/с / 1 048 576
}
DEFAULT_UNITS = "mbit"
UNITS_MIGRATION = {"bits": "mbit", "bytes": "mbyte"}  # старые значения config.json
UNITS_TOTAL = ("Б", "КБ", "МБ", "ГБ", "ТБ")
# «Красивые» потолки оси Y графика (в текущей единице)
NICE_CEILINGS = (0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000,
                 10000, 20000, 50000, 100000)

log = logging.getLogger("netspeed")

# --------------------------------------------------------------------------
# Зависимости (импортируем мягко, чтобы показать понятное сообщение)
# --------------------------------------------------------------------------
DEPS_ERROR: Optional[BaseException] = None
try:
    import psutil
    import pystray
    from PIL import Image, ImageDraw, ImageFont, ImageGrab
except Exception as _e:  # noqa: BLE001
    DEPS_ERROR = _e


# --------------------------------------------------------------------------
# Пути, конфиг, логирование
# --------------------------------------------------------------------------
def app_dir() -> Path:
    """Каталог приложения (рядом со скриптом; при PyInstaller — рядом с exe)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


DEFAULT_CONFIG: Dict[str, Any] = {
    "interface": "auto",          # "auto" | "all" | имя адаптера
    "units": DEFAULT_UNITS,       # "kbit" | "mbit" | "kbyte" | "mbyte"
    "interval": DEFAULT_INTERVAL,  # секунды, 0.25..5
    "compact_visible": False,
    "compact_pos": None,           # [x, y] или null
    "window_geometry": "640x480",
    "history_seconds": DEFAULT_HISTORY,
    "tariff_mbit": DEFAULT_TARIFF,  # тариф провайдера, Мбит/с (0 < x ≤ 100000)
}


class Config:
    """config.json рядом с программой: загрузка с дефолтами и атомарная запись."""

    def __init__(self, path: Path, readonly: bool = False) -> None:
        self.path = path
        self.readonly = readonly  # в --selftest ничего не записываем
        self.data: Dict[str, Any] = dict(DEFAULT_CONFIG)
        self.load()

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for k in DEFAULT_CONFIG:
                    if k in raw:
                        self.data[k] = raw[k]
        except FileNotFoundError:
            pass
        except Exception as e:  # noqa: BLE001
            log.warning("config.json не прочитан, используются значения по умолчанию: %s", e)
        self._validate()

    def _validate(self) -> None:
        d = self.data
        if not isinstance(d.get("interface"), str) or not d["interface"]:
            d["interface"] = "auto"
        d["units"] = normalize_units(d.get("units"))
        try:
            v = float(d.get("interval", DEFAULT_INTERVAL))
            if not math.isfinite(v):
                v = DEFAULT_INTERVAL
            d["interval"] = min(MAX_INTERVAL, max(MIN_INTERVAL, v))
        except (TypeError, ValueError, OverflowError):
            d["interval"] = DEFAULT_INTERVAL
        d["compact_visible"] = bool(d.get("compact_visible", False))
        pos = d.get("compact_pos")
        try:
            # json принимает NaN/1e999 — такие координаты (и bool) отбрасываем
            if not (isinstance(pos, (list, tuple)) and len(pos) == 2
                    and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                            and math.isfinite(v) for v in pos)):
                raise ValueError
            d["compact_pos"] = [int(pos[0]), int(pos[1])]
        except (TypeError, ValueError, OverflowError):
            d["compact_pos"] = None
        if not isinstance(d.get("window_geometry"), str):
            d["window_geometry"] = "640x480"
        try:
            d["history_seconds"] = int(min(3600, max(30, int(d.get("history_seconds", DEFAULT_HISTORY)))))
        except (TypeError, ValueError, OverflowError):
            d["history_seconds"] = DEFAULT_HISTORY
        d["tariff_mbit"] = normalize_tariff(d.get("tariff_mbit"))

    def save(self) -> None:
        if self.readonly:
            return
        tmp = self.path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
        except Exception as e:  # noqa: BLE001
            log.warning("Не удалось сохранить config.json: %s", e)

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.data[key] = value


def setup_logging() -> None:
    handlers: List[logging.Handler] = []
    try:
        from logging.handlers import RotatingFileHandler
        # Ограничиваем размер лога: netspeed.log до 1 МБ + одна старая копия netspeed.log.1
        handlers.append(RotatingFileHandler(app_dir() / "netspeed.log", maxBytes=1_000_000,
                                            backupCount=1, encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )


# --------------------------------------------------------------------------
# Форматирование
# --------------------------------------------------------------------------
def normalize_units(mode: Any) -> str:
    """Приводит значение units к одному из UNIT_MODES; старые bits/bytes мигрируются."""
    if isinstance(mode, str):
        mode = UNITS_MIGRATION.get(mode, mode)
        if mode in UNIT_MODES:
            return mode
    return DEFAULT_UNITS


def unit_label(mode: str) -> str:
    """Подпись единицы: «Мбит/с», «Кбит/с», «КБ/с», «МБ/с»."""
    return UNIT_MODES[normalize_units(mode)][0]


def convert_speed(bytes_per_s: float, mode: str) -> float:
    """Байт/с -> число в выбранной фиксированной единице."""
    value = float(bytes_per_s)
    if value < 0 or math.isnan(value) or math.isinf(value):
        value = 0.0
    return value / UNIT_MODES[normalize_units(mode)][1]


def fmt_value(bytes_per_s: float, mode: str) -> str:
    """Число без суффикса, всегда ровно 2 знака после запятой: «816.39»."""
    return f"{convert_speed(bytes_per_s, mode):.2f}"


def fmt_speed(bytes_per_s: float, mode: str) -> str:
    """«816.39 Мбит/с» / «12345.67 Кбит/с»."""
    return f"{fmt_value(bytes_per_s, mode)} {unit_label(mode)}"


def toggle_units(mode: str) -> str:
    """Кбит/с <-> Мбит/с; из байтовых режимов первый щелчок ведёт в Мбит/с."""
    return "kbit" if normalize_units(mode) == "mbit" else "mbit"


def fmt_total(nbytes: float) -> str:
    """Суммарный трафик — в байтах (КБ/МБ/ГБ), 1 знак при < 100, иначе целое."""
    value = float(max(0.0, nbytes))
    idx = 0
    while value >= 1024 and idx < len(UNITS_TOTAL) - 1:
        value /= 1024
        idx += 1
    text = f"{value:.1f}" if value < 100 else f"{value:.0f}"
    return f"{text} {UNITS_TOTAL[idx]}"


def normalize_tariff(value: Any) -> float:
    """Тариф провайдера в Мбит/с: число в (0, 100000]; иначе — 1000."""
    try:
        if isinstance(value, bool):
            raise TypeError
        t = float(value)
    except (TypeError, ValueError):
        return DEFAULT_TARIFF
    if math.isnan(t) or math.isinf(t) or t <= 0 or t > MAX_TARIFF:
        return DEFAULT_TARIFF
    return t


def fmt_tariff(tariff: float) -> str:
    """«1000», «100», «2.5» — без хвостовых нулей."""
    return fmt_axis(float(tariff))


def to_mbit(bytes_per_s: float) -> float:
    """Байт/с -> Мбит/с (всегда, независимо от выбранной единицы отображения)."""
    return convert_speed(bytes_per_s, "mbit")


def tariff_percent(bytes_per_s: float, tariff: float) -> float:
    """Доля тарифа провайдера в процентах (может быть > 100)."""
    t = normalize_tariff(tariff)
    return to_mbit(bytes_per_s) / t * 100.0


def fmt_percent(pct: float) -> str:
    return f"{max(0.0, pct):.1f} %"


def fmt_duration(seconds: float) -> str:
    s = int(max(0, seconds))
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def nice_ceiling(x: float) -> float:
    """«Красивый» потолок оси Y из NICE_CEILINGS (в текущей единице), минимум 0.1."""
    if x <= 0 or math.isnan(x) or math.isinf(x):
        return NICE_CEILINGS[0]
    for c in NICE_CEILINGS:
        if c >= x - 1e-9:
            return float(c)
    # Больше 100000 — продолжаем ряд 1, 2, 5 × 10^n
    exp = math.floor(math.log10(x))
    base = 10.0 ** exp
    for m in (1, 2, 5, 10):
        if m * base >= x - 1e-9:
            return m * base
    return 10 * base


def fmt_axis(value: float) -> str:
    """Подпись сетки без «хвостов»: 0.5, 2, 100, 0.025 (точность не теряется)."""
    text = f"{value:.10g}"
    if "e" in text:  # очень маленькое/большое число — обычная запись без экспоненты
        text = f"{value:.6f}".rstrip("0").rstrip(".")
    return text or "0"


def is_loopback(name: str) -> bool:
    return "loopback" in name.lower()


def shorten(text: str, limit: int) -> str:
    """Обрезает длинные имена адаптеров (GUID-подобные) с многоточием."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --------------------------------------------------------------------------
# Сетевые интерфейсы и замер
# --------------------------------------------------------------------------
def list_interfaces() -> List[str]:
    """Все адаптеры без loopback (для меню и комбобокса)."""
    try:
        names = list(psutil.net_io_counters(pernic=True).keys())
    except Exception:  # noqa: BLE001
        names = []
    return sorted(n for n in names if not is_loopback(n))


def detect_default_interface(counters: Dict[str, Any]) -> Optional[str]:
    """Адаптер с маршрутом по умолчанию: UDP-сокет к 8.8.8.8 (пакеты не отправляются)
    → локальный IP → имя адаптера через net_if_addrs(). Иначе — самый нагруженный up-адаптер."""
    ip: Optional[str] = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        ip = None
    if ip and ip != "0.0.0.0":
        try:
            for name, addrs in psutil.net_if_addrs().items():
                if is_loopback(name) or name not in counters:
                    continue
                for a in addrs:
                    if a.family == socket.AF_INET and a.address == ip:
                        return name
        except Exception:  # noqa: BLE001
            pass
    # Запасной вариант: поднятый адаптер с наибольшим суммарным трафиком
    try:
        stats = psutil.net_if_stats()
    except Exception:  # noqa: BLE001
        stats = {}
    best: Optional[str] = None
    best_total = -1
    for name, c in counters.items():
        if is_loopback(name):
            continue
        st = stats.get(name)
        if st is not None and not st.isup:
            continue
        total = c.bytes_recv + c.bytes_sent
        if total > best_total:
            best, best_total = name, total
    return best


@dataclass
class Snapshot:
    history: List[Tuple[float, float, float]] = field(default_factory=list)  # (t_mono, down_Bps, up_Bps)
    now: float = 0.0
    last_down: float = 0.0
    last_up: float = 0.0
    total_down: int = 0
    total_up: int = 0
    max_down: float = 0.0
    max_up: float = 0.0
    samples: int = 0
    mode: str = "auto"
    active_iface: str = ""
    started_at: float = 0.0


class Sampler(threading.Thread):
    """Фоновый поток: читает psutil.net_io_counters(pernic=True) каждые interval секунд."""

    def __init__(self, interval: float, history_seconds: int, mode: str) -> None:
        super().__init__(name="netspeed-sampler", daemon=True)
        self.lock = threading.Lock()
        self._stop_evt = threading.Event()
        self._wake = threading.Event()  # будит поток при смене интервала
        self._interval = float(interval)
        self.history_seconds = int(history_seconds)
        self.history: Deque[Tuple[float, float, float]] = deque(
            maxlen=int(history_seconds / MIN_INTERVAL) + 10)
        self.mode = mode
        self.active_iface = ""
        self.last_down = 0.0
        self.last_up = 0.0
        self.total_down = 0
        self.total_up = 0
        self.max_down = 0.0
        self.max_up = 0.0
        self.samples = 0
        self.started_at = time.time()
        self._prev: Optional[Dict[str, Any]] = None
        self._prev_t = 0.0
        self._auto_iface: Optional[str] = None
        self._last_auto_check = -1e9

    # --- управление ---
    @property
    def interval(self) -> float:
        return self._interval

    def set_interval(self, value: float) -> None:
        self._interval = min(MAX_INTERVAL, max(MIN_INTERVAL, float(value)))
        self._wake.set()  # новый интервал применяется сразу, а не после текущего ожидания

    def set_mode(self, mode: str) -> None:
        with self.lock:
            if mode == self.mode:
                return
            self.mode = mode
            self.history.clear()
            self.max_down = self.max_up = 0.0
            self.last_down = self.last_up = 0.0
            self._last_auto_check = -1e9

    def stop(self) -> None:
        self._stop_evt.set()
        self._wake.set()

    def snapshot(self) -> Snapshot:
        with self.lock:
            return Snapshot(
                history=list(self.history),
                now=time.perf_counter(),
                last_down=self.last_down, last_up=self.last_up,
                total_down=self.total_down, total_up=self.total_up,
                max_down=self.max_down, max_up=self.max_up,
                samples=self.samples, mode=self.mode,
                active_iface=self.active_iface, started_at=self.started_at,
            )

    # --- поток ---
    def run(self) -> None:
        try:
            self._sample()
        except Exception:  # noqa: BLE001
            log.exception("Ошибка первого замера")
        while not self._stop_evt.is_set():
            self._wake.wait(self._interval)
            if self._stop_evt.is_set():
                break
            self._wake.clear()
            try:
                self._sample()  # dt меряется perf_counter, поэтому ранний подъём даёт корректный замер
            except Exception:  # noqa: BLE001
                log.exception("Ошибка замера")

    def _resolve_iface(self, mode: str, counters: Dict[str, Any], now: float) -> Optional[str]:
        """None означает «все адаптеры»."""
        if mode == "all":
            return None
        if mode != "auto" and mode in counters:
            return mode
        if mode != "auto":
            log.debug("Адаптер %r недоступен — используется авто", mode)
        if (self._auto_iface is None or self._auto_iface not in counters
                or now - self._last_auto_check >= 10.0):
            self._auto_iface = detect_default_interface(counters)
            self._last_auto_check = now
        return self._auto_iface

    def _sample(self) -> None:
        now = time.perf_counter()
        raw = psutil.net_io_counters(pernic=True)
        counters = {n: c for n, c in raw.items() if not is_loopback(n)}
        with self.lock:
            mode = self.mode
        iface = self._resolve_iface(mode, counters, now)
        prev, prev_t = self._prev, self._prev_t
        if prev is None:
            self._prev, self._prev_t = counters, now
            with self.lock:
                self.active_iface = iface or "all"
            return
        dt = now - prev_t
        if dt < 0.05:  # те же счётчики / слишком маленький интервал — пропускаем
            return
        self._prev, self._prev_t = counters, now

        def delta(name: str) -> Tuple[int, int]:
            c = counters.get(name)
            p = prev.get(name)
            if c is None or p is None:
                return 0, 0
            dr = c.bytes_recv - p.bytes_recv
            ds = c.bytes_sent - p.bytes_sent
            return (dr if dr > 0 else 0), (ds if ds > 0 else 0)

        if iface is None:
            dr = ds = 0
            for n in counters:
                a, b = delta(n)
                dr += a
                ds += b
        else:
            dr, ds = delta(iface)
        down, up = dr / dt, ds / dt
        with self.lock:
            self.history.append((now, down, up))
            self.last_down, self.last_up = down, up
            self.total_down += dr
            self.total_up += ds
            self.max_down = max(self.max_down, down)
            self.max_up = max(self.max_up, up)
            self.samples += 1
            self.active_iface = iface or "all"


# --------------------------------------------------------------------------
# Тест скорости под полной нагрузкой
# --------------------------------------------------------------------------
SPEEDTEST_STREAMS_DOWN = 8      # параллельных соединений на загрузку
SPEEDTEST_STREAMS_UP = 6        # параллельных соединений на отдачу
SPEEDTEST_DOWN_SECONDS = 10.0
SPEEDTEST_UP_SECONDS = 8.0
SPEEDTEST_WARMUP = 1.5          # первые секунды не учитываем: разгон TCP
SPEEDTEST_CHUNK = 1 << 16
SPEEDTEST_TIMEOUT = 15.0
SPEEDTEST_UA = f"NetSpeedMonitor/{VERSION}"
# Источники загрузки: (имя, хост, путь, сколько байт за один запрос; 0 — файл фиксированного размера)
SPEEDTEST_DOWN_SOURCES: Tuple[Tuple[str, str, str, int], ...] = (
    # Cloudflare отдаёт не больше 50 МБ за запрос, на большем размере отвечает 403
    ("Cloudflare", "speed.cloudflare.com", "/__down?bytes=%d", 50_000_000),
    ("OVH", "proof.ovh.net", "/files/100Mb.dat", 0),
    ("Yandex Mirror", "mirror.yandex.ru", "/debian/ls-lR.gz", 0),
)
SPEEDTEST_UP_HOST = "speed.cloudflare.com"
SPEEDTEST_UP_PATH = "/__up"
SPEEDTEST_UP_BYTES = 40_000_000
# Случайный блок для отдачи: несжимаемый, чтобы прокси не «схлопнул» его
_UP_BLOB = os.urandom(1 << 18)


class _Counter:
    """Счётчик байт, пополняемый из рабочих потоков."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._total = 0

    def add(self, n: int) -> None:
        with self._lock:
            self._total += n

    @property
    def total(self) -> int:
        with self._lock:
            return self._total


@dataclass
class SpeedTestResult:
    """Итог теста: байт/с (среднее на измерительном участке) и лучшая секунда."""

    down_bps: float = 0.0
    up_bps: float = 0.0
    down_peak_bps: float = 0.0
    up_peak_bps: float = 0.0
    down_bytes: int = 0
    up_bytes: int = 0
    source: str = ""
    error: str = ""
    cancelled: bool = False

    @property
    def ok(self) -> bool:
        return not self.error and not self.cancelled and self.down_bps > 0.0


class SpeedTest(threading.Thread):
    """Грузит канал параллельными HTTPS-потоками и меряет пропускную способность.

    on_progress(phase, fraction, bps, bytes_done) и on_done(result) вызываются
    из потока теста — получатель обязан сам перебросить их в поток tkinter.
    """

    def __init__(self,
                 on_progress: Callable[[str, float, float, int], None],
                 on_done: Callable[["SpeedTestResult"], None],
                 on_partial: Optional[Callable[["SpeedTestResult"], None]] = None,
                 down_seconds: float = SPEEDTEST_DOWN_SECONDS,
                 up_seconds: float = SPEEDTEST_UP_SECONDS,
                 streams_down: int = SPEEDTEST_STREAMS_DOWN,
                 streams_up: int = SPEEDTEST_STREAMS_UP) -> None:
        super().__init__(daemon=True, name="speedtest")
        self._on_progress = on_progress
        self._on_done = on_done
        self._on_partial = on_partial
        self._down_seconds = max(3.0, down_seconds)
        self._up_seconds = max(0.0, up_seconds)
        self._streams_down = max(1, streams_down)
        self._streams_up = max(1, streams_up)
        self._stop = threading.Event()
        self._errors: List[str] = []
        self._err_lock = threading.Lock()
        self._ctx = ssl.create_default_context()

    # ---------- управление ----------
    def cancel(self) -> None:
        self._stop.set()

    @property
    def cancelled(self) -> bool:
        return self._stop.is_set()

    def _note(self, e: BaseException) -> None:
        with self._err_lock:
            if len(self._errors) < 5:
                self._errors.append(f"{type(e).__name__}: {e}")

    def _first_error(self) -> str:
        with self._err_lock:
            return self._errors[0] if self._errors else ""

    # ---------- измерение ----------
    @staticmethod
    def _cum_at(samples: List[Tuple[float, int]], t: float) -> float:
        if t <= samples[0][0]:
            return float(samples[0][1])
        if t >= samples[-1][0]:
            return float(samples[-1][1])
        for i in range(1, len(samples)):
            t1, v1 = samples[i]
            if t1 >= t:
                t0, v0 = samples[i - 1]
                if t1 <= t0:
                    return float(v1)
                return v0 + (v1 - v0) * (t - t0) / (t1 - t0)
        return float(samples[-1][1])

    @classmethod
    def _rate(cls, samples: List[Tuple[float, int]], a: float, b: float) -> float:
        if b - a <= 1e-6:
            return 0.0
        return max(0.0, (cls._cum_at(samples, b) - cls._cum_at(samples, a)) / (b - a))

    def _measure(self, phase: str, counter: _Counter, seconds: float) -> Tuple[float, float, int]:
        """Крутится `seconds` секунд, отдавая прогресс. Возвращает (среднее, пик за 1 с, байт)."""
        t0 = time.perf_counter()
        samples: List[Tuple[float, int]] = [(0.0, 0)]
        while True:
            if self._stop.wait(0.2):
                break
            t = time.perf_counter() - t0
            samples.append((t, counter.total))
            self._on_progress(phase, min(1.0, t / seconds) if seconds else 1.0,
                              self._rate(samples, max(0.0, t - 1.0), t), samples[-1][1])
            if t >= seconds:
                break
        end = samples[-1][0]
        warm = min(SPEEDTEST_WARMUP, end * 0.3)
        avg = self._rate(samples, warm, end)
        peak = 0.0
        a = warm
        while a + 1.0 <= end:
            peak = max(peak, self._rate(samples, a, a + 1.0))
            a += 0.2
        return avg, max(peak, avg), samples[-1][1]

    # ---------- источник ----------
    def _pick_source(self) -> Optional[Tuple[str, str, str, int]]:
        for name, host, path, per in SPEEDTEST_DOWN_SOURCES:
            if self._stop.is_set():
                return None
            conn = None
            try:
                conn = http.client.HTTPSConnection(host, timeout=8.0, context=self._ctx)
                # Пробуем ровно тот запрос, которым будем грузить канал: часть серверов
                # отвечает 403 именно на большой размер, а на маленький — 200.
                conn.request("GET", (path % per) if per else path,
                             headers={"User-Agent": SPEEDTEST_UA, "Accept-Encoding": "identity"})
                resp = conn.getresponse()
                status = resp.status
                got = len(resp.read(1 << 16))
                if status == 200 and got > 0:
                    log.info("Тест скорости: источник %s", name)
                    return name, host, path, per
                raise OSError(f"HTTP {status}")
            except Exception as e:  # noqa: BLE001
                log.info("Тест скорости: %s недоступен (%s)", name, e)
                self._note(e)
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:  # noqa: BLE001
                        pass
        return None

    # ---------- рабочие потоки ----------
    def _down_worker(self, host: str, path: str, per: int, counter: _Counter) -> None:
        while not self._stop.is_set():
            conn = None
            try:
                conn = http.client.HTTPSConnection(host, timeout=SPEEDTEST_TIMEOUT, context=self._ctx)
                conn.request("GET", (path % per) if per else path,
                             headers={"User-Agent": SPEEDTEST_UA, "Accept-Encoding": "identity",
                                      "Cache-Control": "no-cache"})
                resp = conn.getresponse()
                if resp.status != 200:
                    raise OSError(f"HTTP {resp.status}")
                while not self._stop.is_set():
                    chunk = resp.read(SPEEDTEST_CHUNK)
                    if not chunk:
                        break
                    counter.add(len(chunk))
            except Exception as e:  # noqa: BLE001
                if not self._stop.is_set():
                    self._note(e)
                    self._stop.wait(0.2)
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:  # noqa: BLE001
                        pass

    def _up_worker(self, counter: _Counter) -> None:
        blob = _UP_BLOB
        while not self._stop.is_set():
            conn = None
            try:
                conn = http.client.HTTPSConnection(SPEEDTEST_UP_HOST, timeout=SPEEDTEST_TIMEOUT,
                                                   context=self._ctx)
                conn.putrequest("POST", SPEEDTEST_UP_PATH, skip_accept_encoding=True)
                conn.putheader("User-Agent", SPEEDTEST_UA)
                conn.putheader("Content-Type", "application/octet-stream")
                conn.putheader("Content-Length", str(SPEEDTEST_UP_BYTES))
                conn.endheaders()
                sock = conn.sock
                if sock is None:
                    raise OSError("нет сокета")
                sent = 0
                while sent < SPEEDTEST_UP_BYTES and not self._stop.is_set():
                    n = min(len(blob), SPEEDTEST_UP_BYTES - sent)
                    sock.sendall(blob if n == len(blob) else blob[:n])
                    sent += n
                    counter.add(n)
                if sent >= SPEEDTEST_UP_BYTES:
                    resp = conn.getresponse()
                    resp.read()
            except Exception as e:  # noqa: BLE001
                if not self._stop.is_set():
                    self._note(e)
                    self._stop.wait(0.2)
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:  # noqa: BLE001
                        pass

    # ---------- сценарий ----------
    def run(self) -> None:
        res = SpeedTestResult()
        workers: List[threading.Thread] = []
        try:
            self._on_progress("probe", 0.0, 0.0, 0)
            src = self._pick_source()
            if src is None:
                res.error = "Не удалось подключиться к серверам теста. " + self._first_error()
                return
            name, host, path, per = src
            res.source = name

            counter = _Counter()
            for _ in range(self._streams_down):
                t = threading.Thread(target=self._down_worker, args=(host, path, per, counter),
                                     daemon=True, name="speedtest-down")
                t.start()
                workers.append(t)
            res.down_bps, res.down_peak_bps, res.down_bytes = self._measure(
                "download", counter, self._down_seconds)
            self._stop.set()
            for t in workers:
                t.join(timeout=3.0)
            workers.clear()

            if res.down_bytes == 0:
                res.error = "Данные не пошли. " + self._first_error()
                return
            if self.cancelled and res.down_bps <= 0.0:
                return

            if self._on_partial is not None:
                try:
                    self._on_partial(res)
                except Exception:  # noqa: BLE001
                    log.exception("Обработчик промежуточного результата теста упал")

            # отдача: заново открываем «ворота» (общий stop использован для остановки загрузки)
            if self._up_seconds > 0 and not getattr(self, "_user_cancel", False):
                self._stop = threading.Event()
                upc = _Counter()
                for _ in range(self._streams_up):
                    t = threading.Thread(target=self._up_worker, args=(upc,),
                                         daemon=True, name="speedtest-up")
                    t.start()
                    workers.append(t)
                res.up_bps, res.up_peak_bps, res.up_bytes = self._measure(
                    "upload", upc, self._up_seconds)
                self._stop.set()
                for t in workers:
                    t.join(timeout=3.0)
                workers.clear()
        except Exception as e:  # noqa: BLE001
            log.exception("Тест скорости не удался")
            res.error = f"{type(e).__name__}: {e}"
        finally:
            self._stop.set()
            for t in workers:
                t.join(timeout=2.0)
            res.cancelled = bool(getattr(self, "_user_cancel", False))
            try:
                self._on_done(res)
            except Exception:  # noqa: BLE001
                log.exception("Обработчик результата теста упал")

    def request_cancel(self) -> None:
        """Отмена пользователем: останавливает текущую и все последующие фазы."""
        self._user_cancel = True
        self._stop.set()


class SpeedTestDialog:
    """Окно теста скорости: прогресс по фазам и итог в сравнении с тарифом."""

    def __init__(self, app: "App") -> None:
        self.app = app
        self.test: Optional[SpeedTest] = None
        self.closed = False
        self.win = tk.Toplevel(app.root)
        self.win.title("Тест скорости — полная нагрузка")
        self.win.configure(bg=BG)
        self.win.resizable(False, False)
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        try:
            self.win.transient(app.root)
        except tk.TclError:
            pass
        self._build()
        self._center()
        self.win.bind("<Escape>", lambda e: self.close())
        self.start()

    # ---------- интерфейс ----------
    def _build(self) -> None:
        f = ttk.Frame(self.win, padding=(16, 12, 16, 12))
        f.pack(fill="both", expand=True)

        self.lbl_phase = ttk.Label(f, text="Подключение к серверу…", style="Head.TLabel")
        self.lbl_phase.pack(anchor="w")

        self.lbl_cur = ttk.Label(f, text=fmt_speed(0.0, self.app.cfg["units"]), style="Down.TLabel")
        self.lbl_cur.pack(anchor="w")

        self.bar = ttk.Progressbar(f, mode="determinate", maximum=1000, length=380)
        self.bar.pack(fill="x", pady=(4, 10))

        grid = ttk.Frame(f)
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)
        ttk.Label(grid, text="↓ Загрузка", style="Head.TLabel").grid(row=0, column=0, sticky="w")
        self.res_down = ttk.Label(grid, text="—", style="ResDown.TLabel")
        self.res_down.grid(row=0, column=1, sticky="e")
        self.res_down_pct = ttk.Label(grid, text="", style="Dim.TLabel")
        self.res_down_pct.grid(row=1, column=1, sticky="e", pady=(0, 6))

        ttk.Label(grid, text="↑ Отдача", style="Head.TLabel").grid(row=2, column=0, sticky="w")
        self.res_up = ttk.Label(grid, text="—", style="ResUp.TLabel")
        self.res_up.grid(row=2, column=1, sticky="e")
        self.res_up_pct = ttk.Label(grid, text="", style="Dim.TLabel")
        self.res_up_pct.grid(row=3, column=1, sticky="e")

        self.lbl_note = ttk.Label(f, text=f"Тариф: {fmt_tariff(self.app.cfg['tariff_mbit'])} Мбит/с. "
                                          "Тест занимает ~20 с и расходует трафик.",
                                  style="Dim.TLabel", wraplength=380, justify="left")
        self.lbl_note.pack(anchor="w", pady=(10, 8))

        btns = ttk.Frame(f)
        btns.pack(fill="x")
        self.btn_main = ttk.Button(btns, text="Отмена", style="Units.TButton",
                                   command=self._on_main_button, takefocus=False)
        self.btn_main.pack(side="right")
        self.btn_again = ttk.Button(btns, text="Повторить", style="Units.TButton",
                                    command=self.start, takefocus=False)
        self.btn_again.pack(side="right", padx=(0, 8))
        self.btn_again.state(["disabled"])

    def _center(self) -> None:
        self.win.update_idletasks()
        w, h = self.win.winfo_width(), self.win.winfo_height()
        try:
            rx, ry = self.app.root.winfo_rootx(), self.app.root.winfo_rooty()
            rw, rh = self.app.root.winfo_width(), self.app.root.winfo_height()
            x, y = rx + (rw - w) // 2, ry + (rh - h) // 2
        except tk.TclError:
            x = y = 100
        left, top, right, bottom = get_monitor_work_area(x, y)
        x = max(left, min(x, right - w))
        y = max(top, min(y, bottom - h))
        self.win.geometry(f"+{x}+{y}")

    # ---------- жизненный цикл ----------
    def start(self) -> None:
        if self.test is not None and self.test.is_alive():
            return
        self.bar["value"] = 0
        self.lbl_phase.configure(text="Подключение к серверу…")
        self.lbl_cur.configure(text=fmt_speed(0.0, self.app.cfg["units"]), style="Down.TLabel")
        self.res_down.configure(text="—")
        self.res_up.configure(text="—")
        self.res_down_pct.configure(text="")
        self.res_up_pct.configure(text="")
        self.btn_main.configure(text="Отмена")
        self.btn_again.state(["disabled"])
        self.test = SpeedTest(
            on_progress=lambda *a: self.app.post(self._progress, *a),
            on_done=lambda r: self.app.post(self._done, r),
            on_partial=lambda r: self.app.post(self._partial, r),
        )
        self.test.start()

    def _on_main_button(self) -> None:
        if self.test is not None and self.test.is_alive():
            self.test.request_cancel()
            self.lbl_phase.configure(text="Останавливаю…")
            self.btn_main.state(["disabled"])
        else:
            self.close()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.test is not None:
            self.test.request_cancel()
        self.app.speedtest = None
        try:
            self.win.destroy()
        except tk.TclError:
            pass

    def lift(self) -> None:
        try:
            self.win.deiconify()
            self.win.lift()
            self.win.focus_force()
        except tk.TclError:
            pass

    # ---------- обратные вызовы теста (уже в потоке tkinter) ----------
    def _progress(self, phase: str, frac: float, bps: float, done: int) -> None:
        if self.closed:
            return
        units = self.app.cfg["units"]
        if phase == "probe":
            return
        if phase == "download":
            self.lbl_phase.configure(text="Загрузка — канал под полной нагрузкой")
            self.lbl_cur.configure(style="Down.TLabel")
            base, span = 0.0, 0.62
        else:
            self.lbl_phase.configure(text="Отдача — канал под полной нагрузкой")
            self.lbl_cur.configure(style="Up.TLabel")
            base, span = 0.62, 0.38
        self.lbl_cur.configure(text=fmt_speed(bps, units))
        self.bar["value"] = int((base + span * max(0.0, min(1.0, frac))) * 1000)
        self.lbl_note.configure(text=f"Передано: {fmt_total(done)}. "
                                     f"Тариф: {fmt_tariff(self.app.cfg['tariff_mbit'])} Мбит/с.")

    def _partial(self, res: SpeedTestResult) -> None:
        """Загрузка измерена — показываем её, пока идёт отдача."""
        if self.closed:
            return
        self._fill_down(res)

    def _fill_down(self, res: SpeedTestResult) -> None:
        units = self.app.cfg["units"]
        tariff = self.app.cfg["tariff_mbit"]
        self.res_down.configure(text=fmt_speed(res.down_bps, units))
        self.res_down_pct.configure(
            text=f"пик {fmt_speed(res.down_peak_bps, units)}  •  "
                 f"{fmt_percent(tariff_percent(res.down_peak_bps, tariff))} от тарифа")

    def _done(self, res: SpeedTestResult) -> None:
        if self.closed:
            return
        units = self.app.cfg["units"]
        tariff = self.app.cfg["tariff_mbit"]
        self.bar["value"] = 1000
        self.btn_main.state(["!disabled"])
        self.btn_main.configure(text="Закрыть")
        self.btn_again.state(["!disabled"])
        self.lbl_cur.configure(text=fmt_speed(res.down_bps, units), style="Down.TLabel")

        if res.error:
            self.lbl_phase.configure(text="Не получилось")
            self.lbl_note.configure(text=res.error)
            return
        if res.cancelled and res.down_bytes == 0:
            self.lbl_phase.configure(text="Тест отменён")
            self.lbl_note.configure(text="Результат не получен.")
            return

        self.lbl_phase.configure(text="Готово" + (" (прервано)" if res.cancelled else ""))
        self._fill_down(res)
        if res.up_bytes > 0:
            self.res_up.configure(text=fmt_speed(res.up_bps, units))
            self.res_up_pct.configure(
                text=f"пик {fmt_speed(res.up_peak_bps, units)}  •  "
                     f"{fmt_percent(tariff_percent(res.up_peak_bps, tariff))} от тарифа")
        else:
            self.res_up.configure(text="—")
            self.res_up_pct.configure(text="сервер отдачи недоступен")
        self.lbl_note.configure(
            text=f"Сервер: {res.source}. Передано: {fmt_total(res.down_bytes + res.up_bytes)}. "
                 f"Тариф: {fmt_tariff(tariff)} Мбит/с.")


# --------------------------------------------------------------------------
# Windows: DPI, рабочая область, единственный экземпляр, автозапуск
# --------------------------------------------------------------------------
class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", RECT),
                ("rcWork", RECT), ("dwFlags", ctypes.c_ulong)]


def set_dpi_awareness() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:  # noqa: BLE001
            pass


def get_work_area() -> Tuple[int, int, int, int]:
    """(left, top, right, bottom) рабочей области основного монитора (без панели задач)."""
    try:
        r = RECT()
        if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0):
            return r.left, r.top, r.right, r.bottom
    except Exception:  # noqa: BLE001
        pass
    try:
        w = ctypes.windll.user32.GetSystemMetrics(0)
        h = ctypes.windll.user32.GetSystemMetrics(1)
        return 0, 0, int(w), int(h)
    except Exception:  # noqa: BLE001
        return 0, 0, 1920, 1080


def get_monitor_work_area(x: int, y: int) -> Tuple[int, int, int, int]:
    """Рабочая область монитора, содержащего точку (x, y) (или ближайшего к ней);
    при ошибке — рабочая область основного монитора."""
    try:
        user32 = ctypes.windll.user32
        user32.MonitorFromPoint.argtypes = [POINT, ctypes.c_ulong]  # POINT передаётся по значению
        user32.MonitorFromPoint.restype = ctypes.c_void_p
        user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        user32.GetMonitorInfoW.restype = ctypes.c_int
        hmon = user32.MonitorFromPoint(POINT(int(x), int(y)), 2)  # MONITOR_DEFAULTTONEAREST
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if hmon and user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            r = mi.rcWork
            if r.right > r.left and r.bottom > r.top:
                return r.left, r.top, r.right, r.bottom
    except Exception:  # noqa: BLE001
        pass
    return get_work_area()


def get_virtual_screen() -> Tuple[int, int, int, int]:
    """(left, top, right, bottom) всего виртуального экрана (все мониторы)."""
    try:
        gsm = ctypes.windll.user32.GetSystemMetrics
        x, y, w, h = gsm(76), gsm(77), gsm(78), gsm(79)
        if w > 0 and h > 0:
            return x, y, x + w, y + h
    except Exception:  # noqa: BLE001
        pass
    l, t, r, b = get_work_area()
    return l, t, r, b


_MUTEX_HANDLE: Optional[int] = None


def acquire_single_instance(name: str = MUTEX_NAME) -> bool:
    """True — мы первые; False — программа уже запущена. Хэндл живёт до конца процесса."""
    global _MUTEX_HANDLE
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        handle = kernel32.CreateMutexW(None, False, name)
        err = ctypes.get_last_error()
        _MUTEX_HANDLE = handle
        if not handle:
            # ERROR_ACCESS_DENIED: мьютекс уже создан другим экземпляром (например, запущенным
            # от имени администратора) — значит, программа уже работает
            log.warning("CreateMutexW не удался: ошибка %d", err)
            return err != ERROR_ACCESS_DENIED
        if err == ERROR_ALREADY_EXISTS:
            return False
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("Не удалось создать мьютекс: %s", e)
        return True


def autostart_command() -> str:
    if is_frozen():
        return f'"{sys.executable}" --tray'
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.exists():
        pythonw = Path(sys.executable)
    script = Path(__file__).resolve()
    return f'"{pythonw}" "{script}" --tray'


def autostart_enabled() -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_READ) as key:
            winreg.QueryValueEx(key, RUN_VALUE_NAME)
            return True
    except FileNotFoundError:
        return False
    except Exception as e:  # noqa: BLE001
        log.warning("Не удалось прочитать ключ автозапуска: %s", e)
        return False


def set_autostart(enabled: bool) -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, RUN_VALUE_NAME, 0, winreg.REG_SZ, autostart_command())
            else:
                try:
                    winreg.DeleteValue(key, RUN_VALUE_NAME)
                except FileNotFoundError:
                    pass
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("Не удалось изменить автозапуск: %s", e)
        return False


# --------------------------------------------------------------------------
# Значок трея (Pillow)
# --------------------------------------------------------------------------
_FONT_CACHE: Dict[int, Any] = {}
_FONT_PATHS = [Path(r"C:\Windows\Fonts\segoeuib.ttf"), Path(r"C:\Windows\Fonts\arialbd.ttf")]


def _font(size: int) -> Any:
    f = _FONT_CACHE.get(size)
    if f is not None:
        return f
    f = None
    for p in _FONT_PATHS:
        try:
            if p.exists():
                f = ImageFont.truetype(str(p), size)
                break
        except Exception:  # noqa: BLE001
            continue
    if f is None:
        try:
            f = ImageFont.load_default(size=size)
        except TypeError:
            f = ImageFont.load_default()
    _FONT_CACHE[size] = f
    return f


def icon_text(value: float) -> str:
    """Число для значка (32 px, места мало): 1 знак при < 100, иначе целое;
    если получается длиннее 4 символов — без дробной части."""
    text = f"{value:.0f}" if value >= 100 else f"{value:.1f}"
    if len(text) > 4:
        text = f"{value:.0f}"
    return text


def render_icon(down_text: str, up_text: str, size: int = 32) -> "Image.Image":
    """Рисует значок 64×64 и уменьшает до size (LANCZOS): ↓ зелёным сверху, ↑ оранжевым снизу."""
    big = 64
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, big - 1, big - 1), radius=12, fill=(18, 18, 18, 190))
    max_w = big - 4
    for text, color, cy in ((down_text, GREEN, big * 0.27), (up_text, AMBER, big * 0.73)):
        fsize = 34
        while fsize > 10:
            font = _font(fsize)
            bbox = d.textbbox((0, 0), text, font=font)
            w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
            if w <= max_w and h <= big // 2 - 2:
                break
            fsize -= 2
        x = (big - w) / 2 - bbox[0]
        y = cy - h / 2 - bbox[1]
        d.text((x, y), text, font=font, fill=color)
    return img.resize((size, size), Image.LANCZOS)


class TrayIcon:
    """pystray в собственном потоке; все колбэки прокидываются в tk через app.post()."""

    def __init__(self, app: "App") -> None:
        self.app = app
        self.icon: Optional["pystray.Icon"] = None
        self.thread: Optional[threading.Thread] = None
        self.ready = threading.Event()
        self.failed = False
        self.current_image = render_icon("0.0", "0.0")
        self._last_text: Optional[Tuple[str, str]] = None
        self._last_title: Optional[str] = None

    @property
    def available(self) -> bool:
        return self.icon is not None and self.ready.is_set() and not self.failed

    def start(self) -> None:
        try:
            self.icon = pystray.Icon("NetSpeedMonitor", self.current_image, APP_NAME, menu=self._build_menu())
        except Exception:  # noqa: BLE001
            log.exception("Не удалось создать значок трея")
            self.failed = True
            return
        self.thread = threading.Thread(target=self._run, name="netspeed-tray", daemon=True)
        self.thread.start()

    def _run(self) -> None:
        try:
            self.icon.run(setup=self._setup)
        except Exception:  # noqa: BLE001
            log.exception("Поток значка трея завершился с ошибкой")
            self.failed = True
            self.ready.set()

    def _setup(self, icon: "pystray.Icon") -> None:
        icon.visible = True
        self.ready.set()

    def _build_menu(self) -> "pystray.Menu":
        app = self.app
        M = pystray.MenuItem
        return pystray.Menu(
            M("Открыть окно", lambda icon, item: app.post(app.show_window), default=True),
            M("Компактный индикатор", lambda icon, item: app.post(app.toggle_compact),
              checked=lambda item: bool(app.cfg["compact_visible"])),
            M("Тест скорости…", lambda icon, item: app.post(app.open_speedtest)),
            M("Интерфейс", pystray.Menu(self._iface_items)),
            M("Единицы", pystray.Menu(self._units_items)),
            M("Автозапуск", lambda icon, item: app.post(app.toggle_autostart),
              checked=lambda item: app.autostart_state),
            pystray.Menu.SEPARATOR,
            M("Выход", lambda icon, item: app.post(app.quit_app)),
        )

    def _iface_items(self) -> Iterable["pystray.MenuItem"]:
        app = self.app
        M = pystray.MenuItem

        def item(text: str, mode: str) -> "pystray.MenuItem":
            # pystray считает позиционные аргументы колбэка (ровно 0/1/2),
            # поэтому режим захватываем замыканием, а не аргументом по умолчанию.
            def action(icon: Any, _item: Any) -> None:
                app.post(app.set_interface, mode)

            def checked(_item: Any) -> bool:
                return app.cfg["interface"] == mode

            return M(text, action, checked=checked, radio=True)

        yield item("Авто", "auto")
        yield item("Все", "all")
        for name in app.interfaces:
            yield item(name, name)

    def _units_items(self) -> Iterable["pystray.MenuItem"]:
        app = self.app
        M = pystray.MenuItem

        def item(mode: str) -> "pystray.MenuItem":
            def action(icon: Any, _item: Any) -> None:
                app.post(app.set_units, mode)

            def checked(_item: Any) -> bool:
                return app.cfg["units"] == mode

            return M(unit_label(mode), action, checked=checked, radio=True)

        for mode in ("kbit", "mbit", "kbyte", "mbyte"):
            yield item(mode)

    def update(self, down_text: str, up_text: str, title: str) -> None:
        if self.icon is None:
            return
        key = (down_text, up_text)
        if key != self._last_text:
            self._last_text = key
            self.current_image = render_icon(down_text, up_text)
            try:
                self.icon.icon = self.current_image
            except Exception as e:  # noqa: BLE001
                log.debug("icon.icon: %s", e)
        title = title[:127]  # предел szTip в Windows — 127 символов
        if title != self._last_title:
            self._last_title = title
            try:
                self.icon.title = title
            except Exception as e:  # noqa: BLE001
                log.debug("icon.title: %s", e)

    def _menu_open(self) -> bool:
        """True, если всплывающее меню значка, вероятно, открыто (окно pystray на переднем плане)."""
        try:
            fg = ctypes.windll.user32.GetForegroundWindow()
            hwnds = {getattr(self.icon, "_hwnd", None), getattr(self.icon, "_menu_hwnd", None)}
            hwnds.discard(None)
            return bool(fg) and fg in hwnds
        except Exception:  # noqa: BLE001
            return False

    def refresh_menu(self, _retry: int = 0) -> None:
        if self.icon is None or not self.ready.is_set():
            return
        if self._menu_open() and _retry < 30:
            # Пересборка меню (DestroyMenu) во время открытого TrackPopupMenuEx ломает меню —
            # откладываем, пока пользователь его не закроет
            self.app.root.after(1000, self.refresh_menu, _retry + 1)
            return
        try:
            self.icon.update_menu()
        except Exception as e:  # noqa: BLE001
            log.debug("update_menu: %s", e)

    def stop(self) -> None:
        if self.icon is None:
            return
        try:
            self.icon.visible = False
        except Exception:  # noqa: BLE001
            pass
        try:
            self.icon.stop()
        except Exception as e:  # noqa: BLE001
            log.debug("icon.stop: %s", e)
        if self.thread is not None:
            self.thread.join(timeout=2.0)


# --------------------------------------------------------------------------
# Шкала пропускной способности канала провайдера
# --------------------------------------------------------------------------
class BandwidthScale:
    """Шкала «Канал провайдера»: какая доля тарифа (Мбит/с) занята текущим трафиком.

    Все элементы canvas создаются один раз; геометрия пересчитывается в <Configure>,
    значения обновляются через coords/itemconfigure (без пересоздания элементов).
    """

    TRACK_H = 14   # высота дорожки
    UP_H = 6       # высота полосы отдачи (внутри дорожки, снизу)
    TICK_H = 3     # засечки под дорожкой
    NARROW = 420   # уже этой ширины подписи 25 %/75 % скрываются

    def __init__(self, parent: tk.Misc, tariff: float) -> None:
        import tkinter.font as tkfont
        self.font_title = ("Segoe UI", 9)
        self.font_tick = ("Segoe UI", 8)
        title_h = int(tkfont.Font(family="Segoe UI", size=9).metrics("linespace"))
        tick_h = int(tkfont.Font(family="Segoe UI", size=8).metrics("linespace"))
        self.title_y = title_h // 2
        self.track_y = title_h + 2
        self.label_y = self.track_y + self.TRACK_H + self.TICK_H
        self.height = self.label_y + tick_h
        self.canvas = tk.Canvas(parent, bg=BG, highlightthickness=0, bd=0, height=self.height)
        c = self.canvas
        self.tariff = normalize_tariff(tariff)
        self.x0 = 0.0
        self.x1 = 0.0
        self.width = 0
        self._last: Optional[Tuple[float, float, float]] = None
        self._last_key: Optional[Tuple[Any, ...]] = None

        self.i_title = c.create_text(0, self.title_y, text="", fill=FG_DIM, font=self.font_title, anchor="w")
        self.i_up_pct = c.create_text(0, self.title_y, text="", fill=AMBER, font=self.font_title, anchor="e")
        self.i_down_pct = c.create_text(0, self.title_y, text="", fill=GREEN, font=self.font_title, anchor="e")
        self.i_track = c.create_rectangle(0, 0, 0, 0, fill=TRACK, outline=GRID)
        self.i_down = c.create_rectangle(0, 0, 0, 0, fill=GREEN, outline="")
        self.i_up = c.create_rectangle(0, 0, 0, 0, fill=AMBER, outline="")
        self.i_peak = c.create_line(0, 0, 0, 0, fill=GREEN_LIGHT, width=2, state="hidden")
        self.i_ticks = [c.create_line(0, 0, 0, 0, fill=FG_DIM) for _ in range(5)]
        anchors = ("nw", "n", "n", "n", "ne")  # общий верхний край у всех подписей
        self.i_labels = [c.create_text(0, 0, text="", fill=FG_DIM, font=self.font_tick, anchor=a) for a in anchors]
        c.bind("<Configure>", self._on_configure)
        self.set_tariff(self.tariff)

    def pack(self, **kw: Any) -> None:
        self.canvas.pack(**kw)

    # --- геометрия ---
    def _on_configure(self, e: tk.Event) -> None:
        self.width = int(e.width)
        self._layout()

    def _layout(self) -> None:
        c = self.canvas
        W = self.width
        if W < 40:
            return
        self.x0, self.x1 = 2.0, float(W - 2)
        x0, x1 = self.x0, self.x1
        ty = self.track_y
        c.coords(self.i_title, x0, self.title_y)
        c.coords(self.i_up_pct, x1, self.title_y)
        c.coords(self.i_track, x0, ty, x1, ty + self.TRACK_H)
        yt = ty + self.TRACK_H
        narrow = W < self.NARROW
        for i, (tick, label) in enumerate(zip(self.i_ticks, self.i_labels)):
            x = x0 + (x1 - x0) * i / 4
            c.coords(tick, x, yt, x, yt + self.TICK_H)
            lx = x + 3 if i == 0 else (x - 3 if i == 4 else x)  # крайние подписи — с отступом от края
            c.coords(label, lx, self.label_y)
            c.itemconfigure(label, state="hidden" if (narrow and i in (1, 3)) else "normal")
        # Значения пересчитываем под новую ширину
        self._last_key = None
        if self._last is not None:
            self.update(*self._last)
        else:
            self.update(0.0, 0.0, 0.0)

    # --- данные ---
    def set_tariff(self, tariff: float) -> None:
        self.tariff = normalize_tariff(tariff)
        c = self.canvas
        c.itemconfigure(self.i_title, text=f"Канал провайдера: {fmt_tariff(self.tariff)} Мбит/с")
        for i, label in enumerate(self.i_labels):
            c.itemconfigure(label, text=fmt_tariff(self.tariff * i / 4))
        self._last_key = None
        if self._last is not None:
            self.update(*self._last)

    def update(self, down_bps: float, up_bps: float, peak_down_bps: float) -> None:
        """down/up — текущие байт/с, peak_down — максимум загрузки в видимом окне."""
        self._last = (down_bps, up_bps, peak_down_bps)
        if self.x1 <= self.x0:
            return
        pd = tariff_percent(down_bps, self.tariff)
        pu = tariff_percent(up_bps, self.tariff)
        pp = tariff_percent(peak_down_bps, self.tariff)
        key = (round(pd, 1), round(pu, 1), round(pp, 2))
        if key == self._last_key:
            return
        self._last_key = key
        c = self.canvas
        x0, x1, ty = self.x0, self.x1, self.track_y
        span = x1 - x0

        def frac(p: float) -> float:
            return min(1.0, max(0.0, p / 100.0))

        # Полосы: загрузка на всю высоту дорожки, отдача — тонкая полоса в нижней части
        xd = x0 + span * frac(pd)
        xu = x0 + span * frac(pu)
        c.coords(self.i_down, x0, ty, xd, ty + self.TRACK_H)
        c.coords(self.i_up, x0, ty + self.TRACK_H - 1 - self.UP_H, xu, ty + self.TRACK_H - 1)
        c.itemconfigure(self.i_down, state="normal" if xd - x0 >= 1 else "hidden")
        c.itemconfigure(self.i_up, state="normal" if xu - x0 >= 1 else "hidden")
        # Маркер пика (скрыт, если совпадает с началом дорожки — иначе выглядит как артефакт)
        xp = x0 + span * frac(pp)
        if xp - x0 >= 2:
            c.coords(self.i_peak, xp, ty - 2, xp, ty + self.TRACK_H + 2)
            c.itemconfigure(self.i_peak, state="normal")
        else:
            c.itemconfigure(self.i_peak, state="hidden")
        # Проценты справа в строке заголовка: ↓ зелёным, ↑ оранжевым, ≥ 100 % — красным
        # (порог сравнивается с округлённым значением, чтобы «100.0 %» всегда было красным)
        c.itemconfigure(self.i_up_pct, text=f"↑ {fmt_percent(pu)}", fill=RED if round(pu, 1) >= 100 else AMBER)
        c.itemconfigure(self.i_down_pct, text=f"↓ {fmt_percent(pd)}", fill=RED if round(pd, 1) >= 100 else GREEN)
        bb = c.bbox(self.i_up_pct)
        up_w = (bb[2] - bb[0]) if bb else 50
        c.coords(self.i_down_pct, x1 - up_w - 12, self.title_y)


# --------------------------------------------------------------------------
# Компактный плавающий индикатор
# --------------------------------------------------------------------------
class CompactWidget:
    def __init__(self, app: "App") -> None:
        self.app = app
        self.win = tk.Toplevel(app.root)
        w = self.win
        w.withdraw()
        w.overrideredirect(True)
        w.configure(bg="#202020")
        try:
            w.attributes("-topmost", True)
            w.attributes("-alpha", 0.9)
        except tk.TclError:
            pass
        self.label = tk.Label(w, text=f"↓ 0.00  ↑ 0.00 {unit_label(app.cfg['units'])}", bg="#202020", fg=FG,
                              font=("Consolas", 11, "bold"), padx=10, pady=6)
        self.label.pack(fill="both", expand=True)
        self._drag: Optional[Tuple[int, int]] = None
        self._moved = False
        for widget in (w, self.label):
            widget.bind("<ButtonPress-1>", self._on_press)
            widget.bind("<B1-Motion>", self._on_motion)
            widget.bind("<ButtonRelease-1>", self._on_release)
            widget.bind("<Double-Button-1>", lambda e: app.show_window())
            widget.bind("<Button-3>", self._on_right_click)
        self.visible = False

    def _default_position(self) -> Tuple[int, int]:
        self.win.update_idletasks()
        ww, wh = self.win.winfo_reqwidth(), self.win.winfo_reqheight()
        l, t, r, b = get_work_area()
        return r - ww - 8, b - wh - 8

    def _clamp(self, x: int, y: int) -> Tuple[int, int]:
        self.win.update_idletasks()
        ww, wh = self.win.winfo_reqwidth(), self.win.winfo_reqheight()
        # Рабочая область того монитора, на котором находится индикатор (а не только основного)
        l, t, r, b = get_monitor_work_area(x + ww // 2, y + wh // 2)
        x = max(l, min(x, r - ww))
        y = max(t, min(y, b - wh))
        return x, y

    def show(self) -> None:
        pos = self.app.cfg["compact_pos"]
        if pos:
            x, y = self._clamp(int(pos[0]), int(pos[1]))
        else:
            x, y = self._default_position()
        self.win.geometry(f"+{x}+{y}")
        self.win.deiconify()
        self.win.lift()
        self.visible = True
        self.reassert_topmost()

    def hide(self) -> None:
        self.win.withdraw()
        self.visible = False

    def set_text(self, text: str) -> None:
        if self.label.cget("text") != text:
            self.label.configure(text=text)

    def reassert_topmost(self) -> None:
        if self.visible:
            try:
                self.win.attributes("-topmost", True)
                self.win.lift()
            except tk.TclError:
                pass

    def _on_press(self, e: tk.Event) -> None:
        self._drag = (e.x_root - self.win.winfo_x(), e.y_root - self.win.winfo_y())
        self._moved = False

    def _on_motion(self, e: tk.Event) -> None:
        if self._drag is None:
            return
        dx, dy = self._drag
        self.win.geometry(f"+{e.x_root - dx}+{e.y_root - dy}")
        self._moved = True

    def _on_release(self, e: tk.Event) -> None:
        if self._drag is not None and self._moved:
            self.app.cfg["compact_pos"] = [self.win.winfo_x(), self.win.winfo_y()]
            self.app.cfg.save()
        self._drag = None

    def _on_right_click(self, e: tk.Event) -> None:
        menu = self.app.build_tk_menu(self.win)
        try:
            menu.tk_popup(e.x_root, e.y_root)
        finally:
            menu.grab_release()


# --------------------------------------------------------------------------
# Главное окно и приложение
# --------------------------------------------------------------------------
class App:
    def __init__(self, root: tk.Tk, cfg: Config, sampler: Sampler, args: argparse.Namespace) -> None:
        self.root = root
        self.cfg = cfg
        self.sampler = sampler
        self.args = args
        self.selftest = args.selftest is not None
        self.exit_code = 0
        self.interfaces: List[str] = list_interfaces()
        self.autostart_state = autostart_enabled()
        self._closing = False
        self._last_iface_refresh = time.monotonic()
        self._last_topmost = time.monotonic()
        self._graph_size = (0, 0)
        self._selftest_result: Dict[str, Any] = {}
        self._tick_err_count = 0       # ошибок обновления с момента последней записи в лог
        self._tick_err_logged = -1e9   # когда последний раз писали трассировку

        if self.selftest:
            cfg["compact_visible"] = True  # в самопроверке индикатор всегда показан (конфиг не пишется)

        root.title("Скорость интернета")
        root.configure(bg=BG)
        # Ошибки в обработчиках tk — в лог, а не в sys.stderr (которого под pythonw нет)
        root.report_callback_exception = lambda exc, val, tb: log.error(
            "Ошибка в обработчике Tk", exc_info=(exc, val, tb))
        root.minsize(480, 360)
        self._apply_geometry()
        self._setup_style()
        self._build_ui()

        self.speedtest: Optional[SpeedTestDialog] = None
        self.tray = TrayIcon(self)
        self.tray.start()
        self.compact = CompactWidget(self)

        root.protocol("WM_DELETE_WINDOW", self.on_close_request)
        root.bind("<Control-q>", lambda e: self.quit_app())
        root.bind("<Control-Q>", lambda e: self.quit_app())
        root.bind("<Escape>", lambda e: self.hide_window())
        root.bind("<Control-k>", lambda e: self.toggle_compact())
        root.bind("<Control-K>", lambda e: self.toggle_compact())
        root.bind("<Control-t>", lambda e: self.open_speedtest())
        root.bind("<Control-T>", lambda e: self.open_speedtest())
        root.bind("<Control-u>", lambda e: self.toggle_units())
        root.bind("<Control-U>", lambda e: self.toggle_units())
        for key in ("u", "U", "Cyrillic_ghe", "Cyrillic_GHE"):  # «U» (и «Г» в русской раскладке)
            try:
                root.bind(f"<KeyPress-{key}>", self._on_units_key)
            except tk.TclError:
                pass
        self.canvas.bind("<Configure>", self._on_canvas_configure)

        if cfg["compact_visible"]:
            self.compact.show()
        if args.tray and not self.selftest:
            root.withdraw()

        if getattr(args, "open_speedtest", False):
            self.root.after(400, self.open_speedtest)
        self.root.after(100, self._tick)
        if self.selftest:
            ms = max(1000, int(float(args.selftest) * 1000))
            self.root.after(max(100, ms - 700), self._selftest_prepare)
            self.root.after(ms, self._selftest_finish)

    # ---------- построение UI ----------
    def _apply_geometry(self) -> None:
        geo = str(self.cfg["window_geometry"]) or "640x480"
        import re
        # Tk сообщает отрицательные координаты как «+-500+100» (монитор слева/сверху от основного);
        # форма «-N» (отступ от правого/нижнего края) не сохраняется — позиция игнорируется.
        m = re.fullmatch(r"(\d+)x(\d+)(?:([+-])(-?\d+)([+-])(-?\d+))?", geo)
        if not m:
            geo = "640x480"
        else:
            w, h = int(m.group(1)), int(m.group(2))
            w, h = max(480, min(w, 4000)), max(360, min(h, 3000))
            geo = f"{w}x{h}"
            if m.group(3) == "+" and m.group(5) == "+":
                x, y = int(m.group(4)), int(m.group(6))
                vl, vt, vr, vb = get_virtual_screen()
                if vl - 50 <= x <= vr - 100 and vt - 50 <= y <= vb - 100:
                    geo += f"+{x}+{y}"  # явные «+»: «+-500» Tk читает как x = -500
        self.root.geometry(geo)

    def _setup_style(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", background=BG, foreground=FG, fieldbackground=BG2,
                        bordercolor=BG3, lightcolor=BG3, darkcolor=BG3, troughcolor=BG2,
                        font=("Segoe UI", 10))
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=FG)
        style.configure("Dim.TLabel", background=BG, foreground=FG_DIM, font=("Segoe UI", 9))
        style.configure("Head.TLabel", background=BG, foreground=FG_DIM, font=("Segoe UI", 11))
        style.configure("Down.TLabel", background=BG, foreground=GREEN, font=("Segoe UI", 26, "bold"))
        style.configure("Up.TLabel", background=BG, foreground=AMBER, font=("Segoe UI", 26, "bold"))
        style.configure("ResDown.TLabel", background=BG, foreground=GREEN, font=("Segoe UI", 15, "bold"))
        style.configure("ResUp.TLabel", background=BG, foreground=AMBER, font=("Segoe UI", 15, "bold"))
        style.configure("Test.TButton", background="#3a5d3c", foreground=FG, bordercolor="#3a5d3c",
                        lightcolor="#3a5d3c", darkcolor="#3a5d3c", focuscolor="#3a5d3c", padding=(10, 1))
        style.map("Test.TButton", background=[("active", "#46724a"), ("pressed", "#46724a"),
                                              ("disabled", BG3)],
                  foreground=[("active", FG), ("disabled", FG_DIM)])
        style.configure("TProgressbar", background=GREEN, troughcolor=BG2, bordercolor=BG3,
                        lightcolor=GREEN, darkcolor=GREEN)
        style.configure("TCheckbutton", background=BG, foreground=FG)
        style.map("TCheckbutton", background=[("active", BG)], foreground=[("disabled", FG_DIM)],
                  indicatorcolor=[("selected", GREEN)])
        style.configure("TRadiobutton", background=BG, foreground=FG)
        style.map("TRadiobutton", background=[("active", BG)])
        style.configure("Units.TButton", background=BG3, foreground=FG, bordercolor=BG3,
                        lightcolor=BG3, darkcolor=BG3, focuscolor=BG3, padding=(8, 1))
        style.map("Units.TButton", background=[("active", "#444444"), ("pressed", "#444444"),
                                               ("disabled", BG2)],
                  foreground=[("active", FG), ("disabled", FG_DIM)])
        style.configure("TCombobox", fieldbackground=BG2, background=BG3, foreground=FG,
                        arrowcolor=FG, selectbackground=BG3, selectforeground=FG)
        style.map("TCombobox", fieldbackground=[("readonly", BG2)], foreground=[("readonly", FG)],
                  selectbackground=[("readonly", BG2)], selectforeground=[("readonly", FG)])
        style.configure("TSpinbox", fieldbackground=BG2, background=BG3, foreground=FG, arrowcolor=FG,
                        selectbackground=BG3, selectforeground=FG, insertcolor=FG)
        self.root.option_add("*TCombobox*Listbox.background", BG2)
        self.root.option_add("*TCombobox*Listbox.foreground", FG)
        self.root.option_add("*TCombobox*Listbox.selectBackground", BG3)
        self.root.option_add("*TCombobox*Listbox.selectForeground", FG)

    def _build_ui(self) -> None:
        root = self.root
        cfg = self.cfg
        outer = ttk.Frame(root, padding=(12, 8, 12, 8))
        outer.pack(fill="both", expand=True)

        # --- верх: цифры ---
        top = ttk.Frame(outer)
        top.pack(fill="x")
        top.columnconfigure(0, weight=1, uniform="col")
        top.columnconfigure(1, weight=1, uniform="col")

        down_box = ttk.Frame(top)
        down_box.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        ttk.Label(down_box, text="↓ Загрузка", style="Head.TLabel").pack(anchor="w")
        self.lbl_down = ttk.Label(down_box, text=fmt_speed(0.0, cfg["units"]), style="Down.TLabel")
        self.lbl_down.pack(anchor="w")
        self.lbl_down_stat = ttk.Label(down_box, text="макс —  •  средн —", style="Dim.TLabel")
        self.lbl_down_stat.pack(anchor="w")

        up_box = ttk.Frame(top)
        up_box.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        ttk.Label(up_box, text="↑ Отдача", style="Head.TLabel").pack(anchor="w")
        self.lbl_up = ttk.Label(up_box, text=fmt_speed(0.0, cfg["units"]), style="Up.TLabel")
        self.lbl_up.pack(anchor="w")
        self.lbl_up_stat = ttk.Label(up_box, text="макс —  •  средн —", style="Dim.TLabel")
        self.lbl_up_stat.pack(anchor="w")

        # --- шкала канала провайдера (под цифрами, над графиком) ---
        self.scale = BandwidthScale(outer, cfg["tariff_mbit"])
        self.scale.pack(fill="x", pady=(6, 0))

        # --- низ: итоги и настройки ---
        # Нижний блок пакуем до графика и прижимаем к низу: при нехватке места (окно 480×360)
        # сжимается график, а не строки настроек.
        bottom = ttk.Frame(outer)
        bottom.pack(side="bottom", fill="x")

        # --- середина: график ---
        # Запрашиваемая высота минимальна: всё свободное место график получает через expand
        # (при 640×480 это ≈175 px); иначе canvas по умолчанию просит ~7 см и при небольшом
        # окне выталкивает нижний блок с настройками за край.
        self.canvas = tk.Canvas(outer, bg=BG, highlightthickness=0, bd=0, height=60, width=200)
        self.canvas.pack(side="top", fill="both", expand=True, pady=(6, 6))

        row1 = ttk.Frame(bottom)
        row1.pack(fill="x", pady=(0, 4))
        # Сессию пакуем первой, чтобы при длинном имени адаптера она не выталкивалась за край
        self.lbl_session = ttk.Label(row1, text="Сессия: 00:00:00", style="TLabel")
        self.lbl_session.pack(side="right")
        self.lbl_totals = ttk.Label(row1, text="Всего: ↓ 0 Б  ↑ 0 Б", style="TLabel")
        self.lbl_totals.pack(side="left")
        self.lbl_iface = ttk.Label(row1, text="", style="Dim.TLabel")
        self.lbl_iface.pack(side="left", padx=(16, 8))

        row2 = ttk.Frame(bottom)
        row2.pack(fill="x")
        ttk.Label(row2, text="Интерфейс:").pack(side="left")
        self.var_iface = tk.StringVar(value=self._mode_to_display(cfg["interface"]))
        self.cmb_iface = ttk.Combobox(row2, textvariable=self.var_iface, state="readonly", width=22,
                                      values=self._iface_display_values())
        self.cmb_iface.pack(side="left", padx=(4, 12))
        self.cmb_iface.bind("<<ComboboxSelected>>", self._on_iface_selected)

        ttk.Label(row2, text="Единицы:").pack(side="left")
        self.var_units = tk.StringVar(value=unit_label(cfg["units"]))
        # Кнопка-переключатель Кбит/с <-> Мбит/с; подпись — текущая единица (клавиша U)
        self.btn_units = ttk.Button(row2, textvariable=self.var_units, style="Units.TButton", width=7,
                                    command=self.toggle_units, takefocus=False)
        self.btn_units.pack(side="left", padx=(4, 12))
        ttk.Label(row2, text="Ctrl+Q — выход, Esc — свернуть в трей", style="Dim.TLabel").pack(side="right")

        # Вторая строка настроек: интервал + тариф
        row3 = ttk.Frame(bottom)
        row3.pack(fill="x", pady=(4, 0))
        ttk.Label(row3, text="Интервал, с:").pack(side="left")
        self.var_interval = tk.StringVar(value=f"{cfg['interval']:g}")
        self.spn_interval = ttk.Spinbox(row3, textvariable=self.var_interval, from_=MIN_INTERVAL, to=MAX_INTERVAL,
                                        increment=0.25, width=5, command=self._on_interval_changed)
        self.spn_interval.pack(side="left", padx=(4, 12))
        self.spn_interval.bind("<Return>", lambda e: self._on_interval_changed())
        self.spn_interval.bind("<FocusOut>", lambda e: self._on_interval_changed())

        ttk.Label(row3, text="Тариф, Мбит/с:").pack(side="left")
        self.var_tariff = tk.StringVar(value=fmt_tariff(cfg["tariff_mbit"]))
        self.spn_tariff = ttk.Spinbox(row3, textvariable=self.var_tariff, from_=1, to=MAX_TARIFF,
                                      increment=50, width=7, command=self._on_tariff_changed)
        self.spn_tariff.pack(side="left", padx=(4, 12))
        self.spn_tariff.bind("<Return>", lambda e: self._on_tariff_changed())
        self.spn_tariff.bind("<FocusOut>", lambda e: self._on_tariff_changed())

        # Третья строка настроек: флажки (отдельно, чтобы при минимальной ширине 480 px
        # «Автозапуск» не уезжал за край окна)
        row4 = ttk.Frame(bottom)
        row4.pack(fill="x", pady=(4, 0))
        self.var_compact = tk.BooleanVar(value=bool(cfg["compact_visible"]))
        ttk.Checkbutton(row4, text="Компактный индикатор", variable=self.var_compact,
                        command=lambda: self.set_compact(self.var_compact.get())).pack(side="left")
        self.var_autostart = tk.BooleanVar(value=self.autostart_state)
        ttk.Checkbutton(row4, text="Автозапуск", variable=self.var_autostart,
                        command=lambda: self.set_autostart(self.var_autostart.get())).pack(side="left", padx=(12, 0))
        self.btn_speedtest = ttk.Button(row4, text="⚡ Тест скорости", style="Test.TButton",
                                        command=self.open_speedtest, takefocus=False)
        self.btn_speedtest.pack(side="right")

    # ---------- общие операции (вызываются из tk-потока) ----------
    def post(self, fn: Callable[..., Any], *args: Any) -> None:
        """Передать вызов в главный поток tkinter (из потока pystray)."""
        try:
            self.root.after(0, fn, *args)
        except Exception as e:  # noqa: BLE001
            log.debug("post: %s", e)

    def _mode_to_display(self, mode: str) -> str:
        return {"auto": "Авто", "all": "Все"}.get(mode, mode)

    def _display_to_mode(self, text: str) -> str:
        return {"Авто": "auto", "Все": "all"}.get(text, text)

    def _iface_display_values(self) -> Tuple[str, ...]:
        return ("Авто", "Все", *self.interfaces)

    def _on_iface_selected(self, _e: Optional[tk.Event] = None) -> None:
        self.set_interface(self._display_to_mode(self.var_iface.get()))

    def set_interface(self, mode: str) -> None:
        if mode != self.cfg["interface"]:
            self.cfg["interface"] = mode
            self.cfg.save()
            self.sampler.set_mode(mode)
            log.info("Интерфейс: %s", mode)
        self.var_iface.set(self._mode_to_display(mode))
        self.tray.refresh_menu()

    def set_units(self, units: str) -> None:
        if units not in UNIT_MODES:
            return
        if units != self.cfg["units"]:
            self.cfg["units"] = units
            self.cfg.save()
            log.info("Единицы: %s", unit_label(units))
        self.var_units.set(unit_label(units))
        self.tray.refresh_menu()
        self._render(self.sampler.snapshot(), force=True)

    def toggle_units(self) -> None:
        """Кбит/с <-> Мбит/с (кнопка, клавиша U, Ctrl+U)."""
        self.set_units(toggle_units(self.cfg["units"]))

    def _on_units_key(self, e: tk.Event) -> None:
        # Не перехватываем букву, если курсор в поле ввода (спинбокс интервала)
        if isinstance(e.widget, (ttk.Spinbox, ttk.Entry, tk.Entry, tk.Spinbox)):
            return
        self.toggle_units()

    def _on_interval_changed(self) -> None:
        try:
            v = float(str(self.var_interval.get()).replace(",", "."))
        except ValueError:
            self.var_interval.set(f"{self.cfg['interval']:g}")
            return
        v = min(MAX_INTERVAL, max(MIN_INTERVAL, v))
        if abs(v - float(self.cfg["interval"])) > 1e-9:
            self.cfg["interval"] = v
            self.cfg.save()
            self.sampler.set_interval(v)
        self.var_interval.set(f"{v:g}")

    def set_tariff(self, tariff: float) -> None:
        """Тариф провайдера (Мбит/с): сохранить, обновить шкалу и подсказку значка."""
        t = normalize_tariff(tariff)
        if abs(t - float(self.cfg["tariff_mbit"])) > 1e-9:
            self.cfg["tariff_mbit"] = t
            self.cfg.save()
            log.info("Тариф: %s Мбит/с", fmt_tariff(t))
        self.var_tariff.set(fmt_tariff(t))
        self.scale.set_tariff(t)

    def _on_tariff_changed(self) -> None:
        raw = str(self.var_tariff.get()).strip().replace(",", ".")
        try:
            v = float(raw)
            if math.isnan(v) or math.isinf(v):
                raise ValueError
        except ValueError:
            # Нечисловой ввод — вернуть прежнее значение
            self.var_tariff.set(fmt_tariff(self.cfg["tariff_mbit"]))
            return
        if v <= 0 or v > MAX_TARIFF:
            # Вне диапазона (0 < тариф ≤ 100000) — как и нечисловой ввод: вернуть прежнее значение
            self.var_tariff.set(fmt_tariff(self.cfg["tariff_mbit"]))
            return
        self.set_tariff(v)

    def set_compact(self, visible: bool) -> None:
        visible = bool(visible)
        self.cfg["compact_visible"] = visible
        self.cfg.save()
        if visible:
            self.compact.show()
        else:
            self.compact.hide()
        self.var_compact.set(visible)
        self.tray.refresh_menu()

    def toggle_compact(self) -> None:
        self.set_compact(not self.cfg["compact_visible"])

    def set_autostart(self, enabled: bool) -> None:
        if set_autostart(bool(enabled)):
            self.autostart_state = autostart_enabled()
        else:
            self.autostart_state = autostart_enabled()
            if not self.selftest:
                messagebox.showwarning(APP_NAME, "Не удалось изменить автозапуск (см. netspeed.log).",
                                       parent=self.root)
        self.var_autostart.set(self.autostart_state)
        self.tray.refresh_menu()

    def toggle_autostart(self) -> None:
        self.set_autostart(not self.autostart_state)

    def show_window(self) -> None:
        self.root.deiconify()
        self.root.lift()
        try:
            self.root.attributes("-topmost", True)
            self.root.after(150, lambda: self.root.attributes("-topmost", False))
        except tk.TclError:
            pass
        self.root.focus_force()

    def hide_window(self) -> None:
        if not self.tray.available and not self.compact.visible:
            # Без значка трея и индикатора окно вернуть будет нечем — вместо скрытия завершаемся
            log.info("Значок трея недоступен — скрытие окна завершает программу")
            self.quit_app()
            return
        self._save_geometry()
        self.root.withdraw()

    def on_close_request(self) -> None:
        if self.selftest:
            return
        if self.tray.available:
            self.hide_window()
        else:
            log.info("Значок трея недоступен — закрытие окна завершает программу")
            self.quit_app()

    def _save_geometry(self) -> None:
        try:
            if self.root.state() == "normal":
                self.cfg["window_geometry"] = self.root.geometry()
        except tk.TclError:
            pass

    def open_speedtest(self) -> None:
        """Открыть окно теста скорости (или поднять уже открытое)."""
        if self.selftest and not getattr(self.args, "open_speedtest", False):
            return
        if self.speedtest is not None and not self.speedtest.closed:
            self.speedtest.lift()
            return
        self.show_window()
        try:
            self.speedtest = SpeedTestDialog(self)
        except Exception:  # noqa: BLE001
            log.exception("Не удалось открыть окно теста скорости")
            self.speedtest = None

    def quit_app(self) -> None:
        if self._closing:
            return
        self._closing = True
        log.info("Завершение работы")
        if self.speedtest is not None:
            self.speedtest.close()
        self._save_geometry()
        self.cfg.save()
        self.sampler.stop()
        self.tray.stop()
        self.sampler.join(timeout=2.0)
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def build_tk_menu(self, parent: tk.Misc) -> tk.Menu:
        """Контекстное меню (компактный индикатор) — те же пункты, что в трее."""
        cfg = self.cfg
        kw = dict(tearoff=0, bg=BG2, fg=FG, activebackground=BG3, activeforeground=FG,
                  selectcolor=GREEN)
        menu = tk.Menu(parent, **kw)
        menu.add_command(label="Открыть окно", command=self.show_window)
        v_compact = tk.BooleanVar(value=bool(cfg["compact_visible"]))
        menu.add_checkbutton(label="Компактный индикатор", variable=v_compact,
                             command=lambda: self.set_compact(v_compact.get()))
        v_iface = tk.StringVar(value=cfg["interface"])
        self.interfaces = list_interfaces()
        m_if = tk.Menu(menu, **kw)
        for text, mode in (("Авто", "auto"), ("Все", "all"), *((n, n) for n in self.interfaces)):
            m_if.add_radiobutton(label=text, value=mode, variable=v_iface,
                                 command=lambda m=mode: self.set_interface(m))
        menu.add_cascade(label="Интерфейс", menu=m_if)
        menu.add_command(label="Тест скорости…", command=self.open_speedtest)
        v_units = tk.StringVar(value=cfg["units"])
        m_u = tk.Menu(menu, **kw)
        for mode in ("kbit", "mbit", "kbyte", "mbyte"):
            m_u.add_radiobutton(label=unit_label(mode), value=mode, variable=v_units,
                                command=lambda m=mode: self.set_units(m))
        menu.add_cascade(label="Единицы", menu=m_u)
        v_auto = tk.BooleanVar(value=self.autostart_state)
        menu.add_checkbutton(label="Автозапуск", variable=v_auto, command=lambda: self.set_autostart(v_auto.get()))
        menu.add_separator()
        menu.add_command(label="Выход", command=self.quit_app)
        # Держим переменные живыми, пока меню открыто
        menu._vars = (v_compact, v_iface, v_units, v_auto)  # type: ignore[attr-defined]
        return menu

    # ---------- периодическое обновление ----------
    def _tick(self) -> None:
        if self._closing:
            return
        try:
            snap = self.sampler.snapshot()
            self._render(snap)
            now = time.monotonic()
            if now - self._last_topmost >= 5.0:
                self._last_topmost = now
                self.compact.reassert_topmost()
            if now - self._last_iface_refresh >= 10.0:
                self._last_iface_refresh = now
                self._refresh_interfaces()
        except Exception:  # noqa: BLE001
            # Постоянная ошибка повторяется каждые 0.25–1 с — трассировку пишем не чаще раза в 60 с
            self._tick_err_count += 1
            now = time.monotonic()
            if now - self._tick_err_logged >= 60.0:
                suffix = f" (пропущено повторов: {self._tick_err_count - 1})" if self._tick_err_count > 1 else ""
                log.exception("Ошибка обновления интерфейса%s", suffix)
                self._tick_err_logged = now
                self._tick_err_count = 0
        period = int(max(MIN_INTERVAL, min(self.sampler.interval, 1.0)) * 1000)
        self.root.after(period, self._tick)

    def _refresh_interfaces(self) -> None:
        names = list_interfaces()
        if names != self.interfaces:
            self.interfaces = names
            self.cmb_iface.configure(values=self._iface_display_values())
            self.tray.refresh_menu()

    def _render(self, snap: Snapshot, force: bool = False) -> None:
        units = self.cfg["units"]
        down_s = fmt_speed(snap.last_down, units)
        up_s = fmt_speed(snap.last_up, units)
        iface_label = shorten(snap.active_iface or "—", 28)
        if snap.mode == "auto":
            iface_text = f"Авто ({iface_label})"
        elif snap.mode == "all":
            iface_text = "Все адаптеры"
        elif snap.active_iface != snap.mode:
            iface_text = f"{shorten(snap.mode, 20)} — недоступен, авто ({iface_label})"
        else:
            iface_text = shorten(snap.mode, 40)

        # Трей: число в текущей единице, полные значения — в подсказке
        dv = convert_speed(snap.last_down, units)
        uv = convert_speed(snap.last_up, units)
        tariff = float(self.cfg["tariff_mbit"])
        down_pct = tariff_percent(snap.last_down, tariff)
        suffix = f" ({fmt_percent(down_pct)} от {fmt_tariff(tariff)} Мбит/с)"
        tip = f"↓ {down_s}{suffix}   ↑ {up_s}\nИнтерфейс: {iface_label}"
        if len(tip) > 127:  # не помещается в подсказку Windows — без доли тарифа
            tip = f"↓ {down_s}   ↑ {up_s}\nИнтерфейс: {iface_label}"
        self.tray.update(icon_text(dv), icon_text(uv), tip)
        # Компактный индикатор: единица один раз
        self.compact.set_text(f"↓ {fmt_value(snap.last_down, units)}  ↑ {fmt_value(snap.last_up, units)} {unit_label(units)}")

        # Главное окно обновляем только если оно видно (force — при смене единиц)
        try:
            if self.root.state() == "withdrawn" and not force:
                return
        except tk.TclError:
            return

        self.lbl_down.configure(text=down_s)
        self.lbl_up.configure(text=up_s)
        window = [p for p in snap.history if snap.now - p[0] <= self.cfg["history_seconds"]]
        peak_d = 0.0
        if window:
            n = len(window)
            max_d = max(p[1] for p in window)
            max_u = max(p[2] for p in window)
            avg_d = sum(p[1] for p in window) / n
            avg_u = sum(p[2] for p in window) / n
            peak_d = max_d
            self.lbl_down_stat.configure(text=f"макс {fmt_speed(max_d, units)}  •  средн {fmt_speed(avg_d, units)}")
            self.lbl_up_stat.configure(text=f"макс {fmt_speed(max_u, units)}  •  средн {fmt_speed(avg_u, units)}")
        # Шкала канала: проценты от тарифа считаются в Мбит/с независимо от единицы отображения
        self.scale.update(snap.last_down, snap.last_up, peak_d)
        self.lbl_totals.configure(text=f"Всего: ↓ {fmt_total(snap.total_down)}  ↑ {fmt_total(snap.total_up)}")
        self.lbl_iface.configure(text=f"Интерфейс: {iface_text}")
        self.lbl_session.configure(text=f"Сессия: {fmt_duration(time.time() - snap.started_at)}")
        self._draw_graph(window, snap.now)

    def _on_canvas_configure(self, e: tk.Event) -> None:
        self._graph_size = (e.width, e.height)
        try:
            if self.root.state() != "withdrawn":
                snap = self.sampler.snapshot()
                window = [p for p in snap.history if snap.now - p[0] <= self.cfg["history_seconds"]]
                self._draw_graph(window, snap.now)
        except tk.TclError:
            pass

    def _draw_graph(self, window: List[Tuple[float, float, float]], now: float) -> None:
        c = self.canvas
        W = c.winfo_width()
        H = c.winfo_height()
        if W < 50 or H < 50:
            return
        c.delete("all")
        units = self.cfg["units"]
        hist = float(self.cfg["history_seconds"])
        left, right, top, bottom = 8, 10, 8, 22
        font = ("Segoe UI", 8)

        # Масштаб Y: фиксированная текущая единица, «красивый» потолок (минимум 0.1)
        vmax = max([p[1] for p in window] + [p[2] for p in window] + [0.0])
        unit = unit_label(units)
        ceiling = nice_ceiling(convert_speed(vmax, units))
        ymax = ceiling * UNIT_MODES[units][1]  # в байтах/с

        # Ширина подписей оси Y (единица подписана один раз — в легенде)
        labels = [fmt_axis(ceiling * i / 4) for i in range(1, 5)]
        tmp = c.create_text(0, 0, text=max(labels, key=len), font=font, anchor="nw")
        bb = c.bbox(tmp)
        c.delete(tmp)
        label_w = (bb[2] - bb[0]) if bb else 60
        left = label_w + 10

        x0, x1 = left, W - right
        y0, y1 = top, H - bottom
        if x1 - x0 < 20 or y1 - y0 < 20:
            return

        # Сетка Y
        for i in range(0, 5):
            y = y1 - (y1 - y0) * i / 4
            c.create_line(x0, y, x1, y, fill=GRID, dash=(2, 4) if i else None)
            if i:
                c.create_text(x0 - 6, y, text=labels[i - 1], fill=FG_DIM, font=font, anchor="e")
        c.create_text(x0 - 6, y1, text="0", fill=FG_DIM, font=font, anchor="e")

        # Сетка X: каждые 30 с
        secs = 30 if hist <= 300 else 60
        t = 0.0
        while t <= hist + 1e-6:
            x = x1 - (x1 - x0) * t / hist
            c.create_line(x, y0, x, y1, fill=GRID)
            c.create_text(x, y1 + 4, text=f"-{int(t)}s" if t else "0s", fill=FG_DIM, font=font, anchor="n")
            t += secs

        def px(ts: float) -> float:
            return x1 - (x1 - x0) * min(hist, max(0.0, now - ts)) / hist

        def py(v: float) -> float:
            return y1 - (y1 - y0) * min(1.0, max(0.0, v / ymax))

        if len(window) >= 2:
            pts_d: List[float] = []
            pts_u: List[float] = []
            for ts, d, u in window:
                x = px(ts)
                pts_d.extend((x, py(d)))
                pts_u.extend((x, py(u)))
            poly = [pts_d[0], y1, *pts_d, pts_d[-2], y1]
            c.create_polygon(poly, fill=GREEN_FILL, outline="")
            c.create_line(pts_d, fill=GREEN, width=2)
            c.create_line(pts_u, fill=AMBER, width=2)
        else:
            c.create_text((x0 + x1) / 2, (y0 + y1) / 2, text="Сбор данных…", fill=FG_DIM, font=("Segoe UI", 10))

        # Легенда
        c.create_rectangle(x0 + 8, y0 + 6, x0 + 20, y0 + 14, fill=GREEN, outline="")
        c.create_text(x0 + 24, y0 + 10, text="Загрузка", fill=FG_DIM, font=font, anchor="w")
        c.create_rectangle(x0 + 92, y0 + 6, x0 + 104, y0 + 14, fill=AMBER, outline="")
        c.create_text(x0 + 108, y0 + 10, text="Отдача", fill=FG_DIM, font=font, anchor="w")
        c.create_text(x1 - 4, y0 + 10, text=unit, fill=FG_DIM, font=font, anchor="e")

    # ---------- самопроверка ----------
    def _selftest_prepare(self) -> None:
        self.root.deiconify()
        self.root.lift()
        try:
            self.root.attributes("-topmost", True)
        except tk.TclError:
            pass
        self.compact.show()
        self.root.update()

    def _grab_widget(self, w: tk.Misc, path: Path) -> str:
        w.update_idletasks()
        x, y = w.winfo_rootx(), w.winfo_rooty()
        ww, wh = w.winfo_width(), w.winfo_height()
        if ww < 2 or wh < 2:
            raise RuntimeError(f"нулевой размер окна {w}")
        img = ImageGrab.grab(bbox=(x, y, x + ww, y + wh), all_screens=True)
        img.save(path)
        return str(path)

    def _selftest_toggle(self) -> bool:
        """Переключить Мбит/с -> Кбит/с, отрисовать кадр, вернуть обратно; True, если всё сошлось."""
        start = self.cfg["units"]
        ok = True
        try:
            self.toggle_units()
            self.root.update()
            flipped = self.cfg["units"]
            ok &= flipped == toggle_units(start)
            ok &= self.var_units.get() == unit_label(flipped)
            ok &= self.lbl_down.cget("text").endswith(unit_label(flipped))
            ok &= self.compact.label.cget("text").endswith(unit_label(flipped))
            self.toggle_units()
            self.root.update()
            ok &= self.cfg["units"] == toggle_units(flipped)
            ok &= self.var_units.get() == unit_label(self.cfg["units"])
        except Exception:  # noqa: BLE001
            log.exception("Ошибка переключения единиц в самопроверке")
            ok = False
        return bool(ok)

    def _selftest_finish(self) -> None:
        shots: List[str] = []
        error: Optional[str] = None
        snap = self.sampler.snapshot()
        toggle_ok = False
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.update()
            toggle_ok = self._selftest_toggle()
            self._render(snap, force=True)
            self.root.update()
            main_path = Path(self.args.screenshot or "selftest_main.png").resolve()
            main_path.parent.mkdir(parents=True, exist_ok=True)
            stem = main_path.with_suffix("")
            shots.append(self._grab_widget(self.root, main_path))
            shots.append(self._grab_widget(self.compact.win, Path(f"{stem}_compact.png")))
            icon = self.tray.current_image
            p_icon = Path(f"{stem}_icon.png")
            icon.save(p_icon)
            shots.append(str(p_icon))
            p_icon4 = Path(f"{stem}_icon_x4.png")
            icon.resize((icon.width * 4, icon.height * 4), Image.NEAREST).save(p_icon4)
            shots.append(str(p_icon4))
            if self.speedtest is not None and not self.speedtest.closed:
                self.speedtest.win.lift()
                self.root.update()
                shots.append(self._grab_widget(self.speedtest.win, Path(f"{stem}_speedtest.png")))
            for s in shots:
                if not Path(s).exists() or Path(s).stat().st_size == 0:
                    raise RuntimeError(f"файл не создан: {s}")
        except Exception as e:  # noqa: BLE001
            log.exception("Ошибка самопроверки")
            error = f"{type(e).__name__}: {e}"
        ok = error is None and snap.samples >= 2 and toggle_ok
        result: Dict[str, Any] = {
            "ok": ok,
            "interface": snap.active_iface,
            "mode": snap.mode,
            "samples": snap.samples,
            "last_down_bps": round(snap.last_down, 1),
            "last_up_bps": round(snap.last_up, 1),
            "units": self.cfg["units"],
            "last_down_text": fmt_speed(snap.last_down, self.cfg["units"]),
            "last_up_text": fmt_speed(snap.last_up, self.cfg["units"]),
            "toggle_ok": toggle_ok,
            "tariff_mbit": float(self.cfg["tariff_mbit"]),
            "down_pct": round(tariff_percent(snap.last_down, float(self.cfg["tariff_mbit"])), 2),
            "tray": self.tray.available,
            "screenshots": shots,
        }
        if error:
            result["error"] = error
        elif snap.samples < 2:
            result["error"] = "samples < 2"
        elif not toggle_ok:
            result["error"] = "toggle failed"
        self._selftest_result = result
        self.exit_code = 0 if ok else 2
        if sys.stdout is not None:
            print(json.dumps(result, ensure_ascii=False), flush=True)
        self.quit_app()


# --------------------------------------------------------------------------
# Точка входа
# --------------------------------------------------------------------------
def _seconds(text: str) -> float:
    """Тип аргумента --selftest: конечное неотрицательное число секунд."""
    try:
        v = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"ожидается число секунд, получено {text!r}") from None
    if not math.isfinite(v) or v < 0:
        raise argparse.ArgumentTypeError(f"число секунд должно быть конечным и ≥ 0, получено {text!r}")
    return v


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="netspeed.py", description="Монитор скорости интернета (Windows 11)")
    p.add_argument("--tray", action="store_true", help="запуск скрытым: только значок в трее")
    p.add_argument("--interface", metavar="NAME", help="адаптер: auto | all | имя адаптера")
    p.add_argument("--selftest", metavar="SECONDS", type=_seconds, help="самопроверка: поработать N секунд, снять скриншоты и выйти")
    p.add_argument("--open-speedtest", action="store_true",
                   help="сразу открыть окно теста скорости (для проверки интерфейса)")
    p.add_argument("--speedtest", metavar="SECONDS", type=_seconds, nargs="?", const=10.0,
                   help="измерить скорость под полной нагрузкой и выйти (без окна)")
    p.add_argument("--screenshot", metavar="PATH", help="куда сохранить скриншот главного окна (режим --selftest)")
    p.add_argument("--version", action="version", version=f"{APP_NAME} {VERSION}")
    return p.parse_args(argv)


def _fail(args: argparse.Namespace, message: str, code: int) -> int:
    """Сообщить об ошибке: в --selftest — JSON в stdout, иначе — messagebox."""
    log.error(message)
    if args.selftest is not None:
        if sys.stdout is not None:
            print(json.dumps({"ok": False, "error": message}, ensure_ascii=False), flush=True)
        return code
    try:
        r = tk.Tk()
        r.withdraw()
        if code == 3:
            messagebox.showinfo(APP_NAME, message)
        else:
            messagebox.showerror(APP_NAME, message)
        r.destroy()
    except Exception:  # noqa: BLE001
        pass
    return code


def run_speedtest_cli(seconds: float) -> int:
    """Измерить скорость без окна и напечатать результат одной строкой JSON."""
    done = threading.Event()
    box: Dict[str, SpeedTestResult] = {}

    def on_done(res: SpeedTestResult) -> None:
        box["res"] = res
        done.set()

    def on_progress(phase: str, frac: float, bps: float, sent: int) -> None:
        if sys.stderr is not None:
            print(f"\r{phase}: {frac * 100:5.1f}%  {fmt_speed(bps, 'mbit')}   ",
                  end="", file=sys.stderr, flush=True)

    test = SpeedTest(on_progress, on_done, down_seconds=seconds, up_seconds=max(3.0, seconds * 0.8))
    test.start()
    done.wait(timeout=seconds * 3 + 60)
    if sys.stderr is not None:
        print(file=sys.stderr, flush=True)
    res = box.get("res")
    if res is None:
        test.request_cancel()
        if sys.stdout is not None:
            print(json.dumps({"ok": False, "error": "timeout"}, ensure_ascii=False), flush=True)
        return 1
    payload = {
        "ok": res.ok,
        "source": res.source,
        "down_mbit": round(to_mbit(res.down_bps), 2),
        "down_peak_mbit": round(to_mbit(res.down_peak_bps), 2),
        "up_mbit": round(to_mbit(res.up_bps), 2),
        "up_peak_mbit": round(to_mbit(res.up_peak_bps), 2),
        "down_bytes": res.down_bytes,
        "up_bytes": res.up_bytes,
        "error": res.error,
        "cancelled": res.cancelled,
    }
    if sys.stdout is not None:
        print(json.dumps(payload, ensure_ascii=False), flush=True)
    return 0 if res.ok else 1


def run(args: argparse.Namespace) -> int:
    if DEPS_ERROR is not None:
        return _fail(args, f"Не установлены зависимости. Запустите install.bat\n\n{DEPS_ERROR!r}", 1)
    if getattr(args, "speedtest", None) is not None:
        return run_speedtest_cli(float(args.speedtest))
    # Самопроверка не пишет конфиг и не мешает рабочему экземпляру — у неё свой мьютекс
    mutex = MUTEX_NAME + "_selftest" if args.selftest is not None else MUTEX_NAME
    if not acquire_single_instance(mutex):
        msg = "already running" if args.selftest is not None else \
            "Программа уже запущена — значок в области уведомлений"
        return _fail(args, msg, 3)

    cfg = Config(app_dir() / "config.json", readonly=args.selftest is not None)
    if args.interface:
        cfg["interface"] = args.interface
    log.info("%s %s запущен (interface=%s, units=%s, interval=%s, tariff=%s Мбит/с)", APP_NAME, VERSION,
             cfg["interface"], cfg["units"], cfg["interval"], cfg["tariff_mbit"])

    sampler = Sampler(cfg["interval"], cfg["history_seconds"], cfg["interface"])
    sampler.start()

    root = tk.Tk()
    app = App(root, cfg, sampler, args)
    try:
        root.mainloop()
    finally:
        app.quit_app()
    return app.exit_code


def main() -> None:
    args = parse_args()
    set_dpi_awareness()
    setup_logging()
    try:
        code = run(args)
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        log.exception("Необработанная ошибка")
        if args.selftest is not None:
            if sys.stdout is not None:
                print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}, ensure_ascii=False), flush=True)
        else:
            try:
                r = tk.Tk()
                r.withdraw()
                messagebox.showerror(APP_NAME, f"Ошибка: {e}\n\nПодробности в netspeed.log")
                r.destroy()
            except Exception:  # noqa: BLE001
                pass
        code = 1
    sys.exit(code)


if __name__ == "__main__":
    main()
