from __future__ import annotations

import argparse
import ctypes
import json
import logging
import math
import os
import queue
import subprocess
import sys
import time
import tkinter as tk
import winreg
from ctypes import wintypes
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

import customtkinter as ctk
import pystray
from PIL import Image, ImageDraw

# ---------------------------------------------------------------------------
# Application constants
# ---------------------------------------------------------------------------

APP_NAME = "PowerGuard"
APP_TITLE = "Power Guard"
APP_VERSION = "1.0.0"
MUTEX_NAME = "Local\\PowerGuard.SingleInstance.7D2B8F0A"

BG = "#070B10"
CARD = "#0F151E"
CARD_2 = "#121B26"
FIELD = "#0A1017"
BORDER = "#1E293B"
BORDER_SOFT = "#172131"
TEXT = "#F4F7FB"
MUTED = "#91A0B3"
DIM = "#627186"

GREEN = "#22C55E"
GREEN_HOVER = "#16A34A"
GREEN_SOFT = "#4ADE80"
GREEN_DARK = "#0B2A1A"
GREEN_RING = "#155E3B"

AMBER = "#F59E0B"
AMBER_DARK = "#2B1D07"

RED = "#EF4444"
RED_HOVER = "#DC2626"
RED_DARK = "#2A0E12"

FONT = "Segoe UI"
FONT_BOLD = "Segoe UI Semibold"

DELAY_OPTIONS = [
    (30, "30 секунд"),
    (60, "60 секунд"),
    (90, "90 секунд"),
    (120, "2 минуты"),
    (300, "5 минут"),
]

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "PowerGuard"


def app_data_dir() -> Path:
    base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / APP_NAME
    return Path.home() / f".{APP_NAME.lower()}"


def resource_path(relative: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / relative


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging() -> logging.Logger:
    logger = logging.getLogger(APP_NAME)
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger

    try:
        log_dir = app_data_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            log_dir / "powerguard.log",
            maxBytes=512 * 1024,
            backupCount=2,
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s")
        )
        logger.addHandler(handler)
    except OSError:
        pass
    return logger


LOG = setup_logging()


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

class Config:
    def __init__(self) -> None:
        self.path = app_data_dir() / "config.json"
        self.delay_seconds = 60
        self.protection_enabled = False
        self.load()

    def load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return

        try:
            delay = int(data.get("delay_seconds", 60))
        except (TypeError, ValueError):
            delay = 60
        self.delay_seconds = max(5, min(3600, delay))

        enabled = data.get("protection_enabled", False)
        self.protection_enabled = bool(enabled)

    def save(self) -> None:
        payload = {
            "delay_seconds": self.delay_seconds,
            "protection_enabled": self.protection_enabled,
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, self.path)
        except OSError as exc:
            LOG.warning("Не удалось сохранить настройки: %s", exc)


# ---------------------------------------------------------------------------
# Windows autostart
# ---------------------------------------------------------------------------

class Autostart:
    def _launch_command(self) -> str:
        if getattr(sys, "frozen", False):
            return f'"{Path(sys.executable).resolve()}" --autostart'

        script = Path(__file__).resolve()
        python = Path(sys.executable)
        pythonw = python.with_name("pythonw.exe")
        runner = pythonw if pythonw.exists() else python
        return f'"{runner}" "{script}" --autostart'

    def is_enabled(self) -> bool:
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ
            ) as key:
                value, _ = winreg.QueryValueEx(key, RUN_VALUE)
                return bool(value)
        except OSError:
            return False

    def set_enabled(self, enabled: bool) -> None:
        if enabled:
            with winreg.CreateKeyEx(
                winreg.HKEY_CURRENT_USER,
                RUN_KEY,
                0,
                winreg.KEY_SET_VALUE,
            ) as key:
                winreg.SetValueEx(
                    key, RUN_VALUE, 0, winreg.REG_SZ, self._launch_command()
                )
        else:
            try:
                with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    RUN_KEY,
                    0,
                    winreg.KEY_SET_VALUE,
                ) as key:
                    winreg.DeleteValue(key, RUN_VALUE)
            except FileNotFoundError:
                pass

    def refresh_command_if_enabled(self) -> None:
        if self.is_enabled():
            try:
                self.set_enabled(True)
            except OSError as exc:
                LOG.warning("Не удалось обновить автозапуск: %s", exc)


# ---------------------------------------------------------------------------
# Single instance
# ---------------------------------------------------------------------------

_ERROR_ALREADY_EXISTS = 183
_mutex_handle = None


def acquire_single_instance():
    global _mutex_handle
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [
        wintypes.LPVOID,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    ]
    kernel32.CreateMutexW.restype = wintypes.HANDLE

    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not handle:
        return None
    if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return None

    _mutex_handle = handle
    return handle


# ---------------------------------------------------------------------------
# Power status (Windows API behind Win32_Battery)
# ---------------------------------------------------------------------------

class SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", wintypes.BYTE),
        ("BatteryFlag", wintypes.BYTE),
        ("BatteryLifePercent", wintypes.BYTE),
        ("SystemStatusFlag", wintypes.BYTE),
        ("BatteryLifeTime", wintypes.DWORD),
        ("BatteryFullLifeTime", wintypes.DWORD),
    ]


@dataclass(frozen=True)
class PowerSnapshot:
    ac_online: Optional[bool]
    battery_percent: Optional[int]
    battery_present: bool


def read_power_status() -> Optional[PowerSnapshot]:
    status = SYSTEM_POWER_STATUS()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
        LOG.warning("GetSystemPowerStatus завершился с ошибкой")
        return None

    if status.ACLineStatus == 0:
        ac_online = False
    elif status.ACLineStatus == 1:
        ac_online = True
    else:
        ac_online = None

    percent = (
        int(status.BatteryLifePercent)
        if status.BatteryLifePercent != 255
        else None
    )
    battery_present = status.BatteryFlag != 128 and (
        percent is not None or status.BatteryFlag != 255
    )
    return PowerSnapshot(ac_online, percent, battery_present)


# ---------------------------------------------------------------------------
# Countdown state machine
# ---------------------------------------------------------------------------

class ProtectionEngine:
    """Pure countdown logic, independent of the GUI weather."""

    def __init__(
        self,
        delay_seconds: int = 60,
        snapshot: Optional[PowerSnapshot] = None,
    ) -> None:
        self.delay_seconds = max(5, int(delay_seconds))
        self.snapshot = snapshot
        self.enabled = False
        self.deadline: Optional[float] = None
        self.shutdown_requested = False
        self.last_restored_at: Optional[float] = None

    @property
    def ac_online(self) -> Optional[bool]:
        return self.snapshot.ac_online if self.snapshot else None

    @property
    def countdown_active(self) -> bool:
        return self.deadline is not None and not self.shutdown_requested

    def remaining(self, now: float) -> int:
        if self.deadline is None:
            return 0
        return max(0, int(math.ceil(self.deadline - now)))

    def set_enabled(self, enabled: bool, now: float) -> str:
        if enabled == self.enabled:
            return "noop"

        self.enabled = enabled
        if not enabled:
            self.deadline = None
            return "disabled"

        if self.ac_online is False:
            self._start_countdown(now)
            return "power_lost"
        return "enabled"

    def update_snapshot(
        self, snapshot: Optional[PowerSnapshot], now: float
    ) -> Optional[str]:
        if snapshot is None:
            return None
        self.snapshot = snapshot
        if not self.enabled:
            return None

        if snapshot.ac_online is False:
            if self.deadline is None and not self.shutdown_requested:
                self._start_countdown(now)
                return "power_lost"
        elif snapshot.ac_online is True and self.deadline is not None:
            self.deadline = None
            self.last_restored_at = now
            return "power_restored"
        return None

    def tick(self, now: float) -> bool:
        if (
            self.enabled
            and self.deadline is not None
            and not self.shutdown_requested
            and now >= self.deadline
        ):
            self.shutdown_requested = True
            return True
        return False

    def set_delay(self, seconds: int) -> None:
        self.delay_seconds = max(5, int(seconds))

    def _start_countdown(self, now: float) -> None:
        self.deadline = now + self.delay_seconds
        self.last_restored_at = None


# ---------------------------------------------------------------------------
# Icon artwork
# ---------------------------------------------------------------------------

def create_icon_image(scale: int = 256, accent: str = GREEN) -> Image.Image:
    image = Image.new("RGBA", (scale, scale), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    s = scale / 256.0

    def box(*values):
        return tuple(round(value * s) for value in values)

    draw.rounded_rectangle(
        box(6, 6, 250, 250),
        radius=round(58 * s),
        fill="#0A111A",
        outline="#243244",
        width=max(2, round(5 * s)),
    )

    shield = [
        (128 * s, 34 * s),
        (204 * s, 67 * s),
        (196 * s, 143 * s),
        (128 * s, 221 * s),
        (60 * s, 143 * s),
        (52 * s, 67 * s),
    ]
    draw.polygon(shield, fill="#111C27")
    draw.line(shield + [shield[0]], fill=accent, width=max(3, round(5 * s)), joint="curve")

    bolt = [
        (145 * s, 68 * s),
        (99 * s, 140 * s),
        (124 * s, 140 * s),
        (109 * s, 190 * s),
        (160 * s, 111 * s),
        (133 * s, 111 * s),
    ]
    draw.polygon(bolt, fill=accent)

    draw.ellipse(
        box(188, 188, 220, 220),
        fill=accent,
        outline="#0A111A",
        width=max(2, round(5 * s)),
    )
    return image


def icon_for_state(active: bool, alert: bool = False, critical: bool = False) -> Image.Image:
    accent = RED if critical else (AMBER if alert else (GREEN if active else "#64748B"))
    return create_icon_image(256, accent).resize((64, 64), Image.Resampling.LANCZOS)


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------

class PowerGuardApp:
    def __init__(self, root: ctk.CTk, args: argparse.Namespace) -> None:
        self.root = root
        self.args = args
        self.config = Config()
        self.autostart = Autostart()
        self.autostart.refresh_command_if_enabled()

        snapshot = read_power_status()
        self.engine = ProtectionEngine(self.config.delay_seconds, snapshot)
        if self.config.protection_enabled:
            event = self.engine.set_enabled(True, time.monotonic())
            if event == "power_lost":
                LOG.info("Защита включена при старте: питание уже от батареи")

        self._quitting = False
        self._shutdown_called = False
        self._shutdown_error: Optional[str] = None
        self._next_poll = 0.0
        self._notice = ""
        self._notice_until = 0.0
        self._hidden_notice_shown = False
        self._tray = None
        self._tray_signature = None
        self._autostart_enabled = False
        self._actions: queue.Queue[str] = queue.Queue()
        self._last_render_signature = None
        self._delay_label_to_value = {
            label: seconds for seconds, label in DELAY_OPTIONS
        }
        if self.config.delay_seconds not in {value for value, _ in DELAY_OPTIONS}:
            self._delay_label_to_value[
                f"{self.config.delay_seconds} сек."
            ] = self.config.delay_seconds
        self._delay_labels = list(self._delay_label_to_value)

        self._icons = {
            "off": icon_for_state(False),
            "on": icon_for_state(True),
            "alert": icon_for_state(True, alert=True),
            "critical": icon_for_state(True, critical=True),
        }
        self._logo_image = ctk.CTkImage(
            light_image=create_icon_image(256, GREEN),
            dark_image=create_icon_image(256, GREEN),
            size=(36, 36),
        )

        self._build_window()
        self._build_ui()
        self._update_autostart_ui(initial=True)
        self._start_tray()
        self._apply_window_details()

        if args.autostart and self._tray is not None:
            self.root.withdraw()
        else:
            self._center_window()
            self.root.update_idletasks()

        LOG.info(
            "Запуск %s v%s; защита=%s; задержка=%s; батарея=%s%%; сеть=%s",
            APP_TITLE,
            APP_VERSION,
            self.engine.enabled,
            self.engine.delay_seconds,
            snapshot.battery_percent if snapshot else "?",
            snapshot.ac_online if snapshot else "?",
        )

        self.root.after(150, self._tick)

    # ------------------------------------------------------------------
    # Window and UI construction
    # ------------------------------------------------------------------

    def _build_window(self) -> None:
        self.root.title(APP_TITLE)
        self.root.configure(fg_color=BG)
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self.hide_to_tray)
        self.root.bind("<Unmap>", self._on_unmap)
        self.root.report_callback_exception = self._on_tk_exception

        icon_path = resource_path("assets/app.ico")
        try:
            if icon_path.exists():
                self.root.iconbitmap(default=str(icon_path))
        except Exception:
            LOG.exception("Не удалось установить иконку окна")

    def _center_window(self) -> None:
        width, height = 460, 620
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        try:
            scale = ctk.ScalingTracker.get_window_dpi_scaling(self.root)
        except Exception:
            scale = 1.0
        physical_w = round(width * scale)
        physical_h = round(height * scale)
        x = max(0, (screen_w - physical_w) // 2)
        y = max(0, (screen_h - physical_h) // 2 - 18)
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    def _build_ui(self) -> None:
        shell = ctk.CTkFrame(self.root, fg_color=BG, corner_radius=0)
        shell.pack(fill="both", expand=True, padx=22, pady=(18, 13))

        header = ctk.CTkFrame(shell, fg_color="transparent", height=44)
        header.pack(fill="x")
        header.pack_propagate(False)

        logo = ctk.CTkLabel(header, text="", image=self._logo_image, width=40)
        logo.pack(side="left", padx=(0, 11))

        title_box = ctk.CTkFrame(header, fg_color="transparent")
        title_box.pack(side="left", fill="y")
        title = ctk.CTkLabel(
            title_box,
            text=APP_TITLE,
            font=ctk.CTkFont(family=FONT_BOLD, size=19),
            text_color=TEXT,
            anchor="w",
            height=22,
        )
        title.pack(anchor="w", pady=(2, 0))
        subtitle = ctk.CTkLabel(
            title_box,
            text="Контроль питания ноутбука",
            font=ctk.CTkFont(family=FONT, size=11),
            text_color=MUTED,
            anchor="w",
            height=16,
        )
        subtitle.pack(anchor="w", pady=(0, 2))

        version = ctk.CTkLabel(
            header,
            text=f"v{APP_VERSION}",
            font=ctk.CTkFont(family=FONT, size=10),
            text_color=DIM,
        )
        version.pack(side="right", anchor="n", pady=(4, 0))

        self.hero = ctk.CTkFrame(
            shell,
            fg_color=CARD,
            corner_radius=22,
            border_width=1,
            border_color=BORDER,
        )
        self.hero.pack(fill="x", pady=(12, 0))

        hero_body = ctk.CTkFrame(self.hero, fg_color="transparent")
        hero_body.pack(fill="x", padx=16, pady=14)

        self.badge = ctk.CTkLabel(
            hero_body,
            text="ЗАЩИТА ВЫКЛЮЧЕНА",
            width=188,
            height=23,
            corner_radius=12,
            fg_color="#141C26",
            text_color=DIM,
            font=ctk.CTkFont(family=FONT_BOLD, size=10),
        )
        self.badge.pack()

        self.power_ring = ctk.CTkFrame(
            hero_body,
            width=116,
            height=116,
            corner_radius=58,
            fg_color=CARD,
            border_width=0,
            border_color=CARD,
        )
        self.power_ring.pack(pady=(9, 0))
        self.power_ring.pack_propagate(False)

        self.power_button = ctk.CTkButton(
            self.power_ring,
            text="",
            width=102,
            height=102,
            corner_radius=51,
            border_width=0,
            fg_color="#1B2634",
            hover_color="#1B2634",
            command=self.toggle_protection,
        )
        self.power_button.pack(expand=True)

        # CTkButton clips text inside a full-circle button, so the caption is
        # drawn as a small opaque label on top of the button.
        try:
            font_scale = ctk.ScalingTracker.get_widget_scaling(self.power_button)
        except Exception:
            font_scale = 1.0
        self.power_text = tk.Label(
            self.power_button,
            text="ВКЛ",
            bg="#1B2634",
            fg="#B9C6D6",
            font=(FONT_BOLD, -round(16 * font_scale)),
            bd=0,
            padx=0,
            pady=0,
            highlightthickness=0,
            cursor="hand2",
        )
        self.power_text.place(relx=0.5, rely=0.5, anchor="center")
        self.power_text.bind("<Button-1>", lambda _event: self.toggle_protection())

        power_line = ctk.CTkFrame(hero_body, fg_color="transparent")
        power_line.pack(pady=(7, 0))
        self.power_dot = ctk.CTkLabel(
            power_line,
            text="●",
            width=12,
            font=ctk.CTkFont(family=FONT, size=10),
            text_color=DIM,
        )
        self.power_dot.pack(side="left")
        self.power_label = ctk.CTkLabel(
            power_line,
            text="Питание: —",
            font=ctk.CTkFont(family=FONT_BOLD, size=12),
            text_color=TEXT,
            height=18,
        )
        self.power_label.pack(side="left", padx=(1, 0))
        self.battery_label = ctk.CTkLabel(
            power_line,
            text="  •  Батарея: —",
            font=ctk.CTkFont(family=FONT, size=11),
            text_color=MUTED,
            height=18,
        )
        self.battery_label.pack(side="left")

        self.timer_card = ctk.CTkFrame(
            hero_body,
            fg_color=FIELD,
            corner_radius=14,
            border_width=1,
            border_color=BORDER_SOFT,
        )
        self.timer_card.pack(fill="x", pady=(9, 0))

        timer_body = ctk.CTkFrame(self.timer_card, fg_color="transparent")
        timer_body.pack(fill="x", padx=14, pady=(7, 9))

        self.timer_title = ctk.CTkLabel(
            timer_body,
            text="ЗАДЕРЖКА ВЫКЛЮЧЕНИЯ",
            font=ctk.CTkFont(family=FONT_BOLD, size=9),
            text_color=DIM,
            height=12,
        )
        self.timer_title.pack()
        self.timer_value = ctk.CTkLabel(
            timer_body,
            text="60 сек",
            font=ctk.CTkFont(family=FONT_BOLD, size=27),
            text_color=MUTED,
            height=34,
        )
        self.timer_value.pack(pady=(0, 0))
        self.timer_note = ctk.CTkLabel(
            timer_body,
            text="Ожидание потери питания",
            font=ctk.CTkFont(family=FONT, size=10),
            text_color=DIM,
            height=14,
        )
        self.timer_note.pack()
        self.progress = ctk.CTkProgressBar(
            timer_body,
            height=4,
            corner_radius=2,
            fg_color="#1B2634",
            progress_color="#334155",
        )
        self.progress.pack(fill="x", pady=(6, 0))
        self.progress.set(0)

        settings = ctk.CTkFrame(
            shell,
            fg_color=CARD,
            corner_radius=18,
            border_width=1,
            border_color=BORDER,
        )
        settings.pack(fill="x", pady=(12, 0))

        delay_row = ctk.CTkFrame(settings, fg_color="transparent", height=51)
        delay_row.pack(fill="x", padx=15, pady=(8, 0))
        delay_row.pack_propagate(False)

        delay_text = ctk.CTkFrame(delay_row, fg_color="transparent")
        delay_text.pack(side="left", fill="y")
        ctk.CTkLabel(
            delay_text,
            text="Задержка выключения",
            font=ctk.CTkFont(family=FONT_BOLD, size=12),
            text_color=TEXT,
            anchor="w",
            height=18,
        ).pack(anchor="w", pady=(7, 0))
        ctk.CTkLabel(
            delay_text,
            text="После перехода на батарею",
            font=ctk.CTkFont(family=FONT, size=10),
            text_color=DIM,
            anchor="w",
            height=14,
        ).pack(anchor="w")

        self.delay_menu = ctk.CTkOptionMenu(
            delay_row,
            values=self._delay_labels,
            width=118,
            height=31,
            corner_radius=9,
            fg_color="#182331",
            button_color="#223044",
            button_hover_color="#2B3B50",
            dropdown_fg_color=CARD_2,
            dropdown_hover_color="#223044",
            dropdown_text_color=TEXT,
            text_color=TEXT,
            text_color_disabled=DIM,
            font=ctk.CTkFont(family=FONT, size=11),
            dropdown_font=ctk.CTkFont(family=FONT, size=11),
            command=self._on_delay_changed,
            dynamic_resizing=False,
        )
        self.delay_menu.pack(side="right", pady=10)
        current_label = next(
            (
                label
                for label, value in self._delay_label_to_value.items()
                if value == self.config.delay_seconds
            ),
            "60 секунд",
        )
        self.delay_menu.set(current_label)

        separator = ctk.CTkFrame(settings, fg_color=BORDER_SOFT, height=1)
        separator.pack(fill="x", padx=15, pady=(2, 0))

        autostart_row = ctk.CTkFrame(settings, fg_color="transparent", height=51)
        autostart_row.pack(fill="x", padx=15, pady=(0, 7))
        autostart_row.pack_propagate(False)

        autostart_text = ctk.CTkFrame(autostart_row, fg_color="transparent")
        autostart_text.pack(side="left", fill="y")
        ctk.CTkLabel(
            autostart_text,
            text="Автозапуск с Windows",
            font=ctk.CTkFont(family=FONT_BOLD, size=12),
            text_color=TEXT,
            anchor="w",
            height=18,
        ).pack(anchor="w", pady=(7, 0))
        ctk.CTkLabel(
            autostart_text,
            text="Запускать в трее при входе",
            font=ctk.CTkFont(family=FONT, size=10),
            text_color=DIM,
            anchor="w",
            height=14,
        ).pack(anchor="w")

        self.autostart_var = ctk.BooleanVar(value=False)
        self.autostart_switch = ctk.CTkSwitch(
            autostart_row,
            text="",
            variable=self.autostart_var,
            onvalue=True,
            offvalue=False,
            width=44,
            switch_width=42,
            switch_height=22,
            corner_radius=11,
            fg_color="#263244",
            progress_color=GREEN,
            button_color="#E2E8F0",
            button_hover_color="#FFFFFF",
            command=self._on_autostart_switched,
        )
        self.autostart_switch.pack(side="right", pady=14)

        self.stop_button = ctk.CTkButton(
            shell,
            text="ОСТАНОВИТЬ И ЗАКРЫТЬ",
            height=46,
            corner_radius=14,
            fg_color=RED,
            hover_color=RED_HOVER,
            text_color="#FFF7F7",
            font=ctk.CTkFont(family=FONT_BOLD, size=12),
            command=self.stop_and_close,
        )
        self.stop_button.pack(fill="x", pady=(12, 0))

        ctk.CTkLabel(
            shell,
            text="Проверка питания каждую секунду  •  один экземпляр",
            font=ctk.CTkFont(family=FONT, size=9),
            text_color="#4E5C6E",
            height=13,
        ).pack(pady=(7, 0))

    def _apply_window_details(self) -> None:
        self.root.update_idletasks()
        try:
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            enabled = ctypes.c_int(1)
            for attribute in (20, 19):
                result = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd,
                    attribute,
                    ctypes.byref(enabled),
                    ctypes.sizeof(enabled),
                )
                if result == 0:
                    break
        except Exception:
            pass

    # ------------------------------------------------------------------
    # State and rendering
    # ------------------------------------------------------------------

    def _on_tk_exception(self, exc_type, exc_value, exc_tb) -> None:
        LOG.exception("Ошибка интерфейса", exc_info=(exc_type, exc_value, exc_tb))

    def toggle_protection(self) -> None:
        if self.engine.shutdown_requested or self._shutdown_called or self._quitting:
            return
        now = time.monotonic()
        new_state = not self.engine.enabled
        event = self.engine.set_enabled(new_state, now)

        if event == "power_lost":
            self._set_notice("Питание от батареи — отсчёт запущен", now)
            LOG.warning("Защита включена при питании от батареи")
        elif event == "enabled":
            self._set_notice("Защита включена", now, duration=3)
            LOG.info("Защита включена")
        elif event == "disabled":
            self._set_notice("Защита выключена — обратный отсчёт отменён", now, duration=4)
            LOG.info("Защита выключена пользователем")

        self.config.protection_enabled = self.engine.enabled
        self.config.save()
        self._render(now, force=True)

    def _on_delay_changed(self, label: str) -> None:
        seconds = self._delay_label_to_value.get(label)
        if seconds is None:
            return
        if self.engine.countdown_active:
            self._restore_delay_menu()
            return
        self.engine.set_delay(seconds)
        self.config.delay_seconds = seconds
        self.config.save()
        LOG.info("Задержка изменена: %s сек.", seconds)
        self._render(time.monotonic(), force=True)

    def _restore_delay_menu(self) -> None:
        for label, value in self._delay_label_to_value.items():
            if value == self.engine.delay_seconds:
                self.delay_menu.set(label)
                return

    def _on_autostart_switched(self) -> None:
        wanted = bool(self.autostart_var.get())
        try:
            self.autostart.set_enabled(wanted)
        except OSError as exc:
            LOG.exception("Не удалось изменить автозапуск")
            self.autostart_var.set(not wanted)
            self._set_notice(f"Автозапуск: ошибка ({exc})", time.monotonic(), 6)
            return
        self._set_notice(
            "Автозапуск включён" if wanted else "Автозапуск выключен",
            time.monotonic(),
            duration=3,
        )
        self._autostart_enabled = wanted
        self._update_tray(force=True)

    def _update_autostart_ui(self, initial: bool = False) -> None:
        try:
            enabled = self.autostart.is_enabled()
        except OSError:
            enabled = False
        self._autostart_enabled = enabled
        self.autostart_var.set(enabled)
        if initial:
            self._update_tray(force=True)

    def _set_notice(self, text: str, now: float, duration: float = 6.0) -> None:
        self._notice = text
        self._notice_until = now + duration

    def _render(self, now: float, force: bool = False) -> None:
        engine = self.engine
        snapshot = engine.snapshot
        active = engine.enabled
        counting = engine.countdown_active
        remaining = engine.remaining(now) if counting else 0
        shutdown = engine.shutdown_requested

        signature = (
            active,
            counting,
            remaining,
            shutdown,
            self._notice if now < self._notice_until else "",
            snapshot.ac_online if snapshot else None,
            snapshot.battery_percent if snapshot else None,
            self.engine.delay_seconds,
            self._shutdown_error,
        )
        if not force and signature == self._last_render_signature:
            return
        self._last_render_signature = signature

        # Power source line -------------------------------------------------
        if snapshot is None:
            self.power_dot.configure(text_color=DIM)
            self.power_label.configure(text="Питание: неизвестно", text_color=MUTED)
            self.battery_label.configure(text="  •  Батарея: —")
        else:
            if snapshot.ac_online is True:
                self.power_dot.configure(text_color=GREEN)
                self.power_label.configure(text="Сеть подключена", text_color=TEXT)
            elif snapshot.ac_online is False:
                self.power_dot.configure(text_color=AMBER)
                self.power_label.configure(text="Питание от батареи", text_color=AMBER)
            else:
                self.power_dot.configure(text_color=DIM)
                self.power_label.configure(text="Питание: неизвестно", text_color=MUTED)

            if snapshot.battery_present and snapshot.battery_percent is not None:
                self.battery_label.configure(
                    text=f"  •  Батарея: {snapshot.battery_percent}%"
                )
            elif not snapshot.battery_present:
                self.battery_label.configure(text="  •  Батарея не обнаружена")
            else:
                self.battery_label.configure(text="  •  Батарея: —")

        # Main power button --------------------------------------------------
        if active:
            self.badge.configure(
                text="ИДЁТ ОТСЧЁТ" if counting else (
                    "ВЫКЛЮЧЕНИЕ" if shutdown else "ЗАЩИТА ВКЛЮЧЕНА"
                ),
                fg_color=RED_DARK if shutdown else (AMBER_DARK if counting else GREEN_DARK),
                text_color=RED if shutdown else (AMBER if counting else GREEN_SOFT),
            )
            self.power_ring.configure(
                fg_color=CARD,
                border_color=CARD,
            )
            self.power_button.configure(
                fg_color=GREEN,
                hover_color=GREEN,
                state="disabled" if shutdown else "normal",
            )
            self.power_text.configure(
                bg=GREEN,
                fg="#03150A",
            )
        else:
            self.badge.configure(
                text="ЗАЩИТА ВЫКЛЮЧЕНА",
                fg_color="#141C26",
                text_color=DIM,
            )
            self.power_ring.configure(
                fg_color=CARD,
                border_color=BORDER,
            )
            self.power_button.configure(
                fg_color="#1B2634",
                hover_color="#1B2634",
                state="normal",
            )
            self.power_text.configure(
                bg="#1B2634",
                fg="#B9C6D6",
            )

        # Timer --------------------------------------------------------------
        if shutdown:
            self.timer_card.configure(fg_color=RED_DARK, border_color="#7F1D1D")
            self.timer_title.configure(text="ВЫКЛЮЧЕНИЕ", text_color=RED)
            self.timer_value.configure(text="СЕЙЧАС", text_color=RED)
            self.timer_note.configure(
                text="Команда выключения отправлена", text_color="#FDA4AF"
            )
            self.progress.configure(progress_color=RED)
            self.progress.set(0)
        elif counting:
            self.timer_card.configure(fg_color=AMBER_DARK, border_color="#6B4A0D")
            self.timer_title.configure(text="ДО ВЫКЛЮЧЕНИЯ", text_color=AMBER)
            self.timer_value.configure(
                text=self._format_seconds(remaining),
                text_color=RED if remaining <= 10 else "#FCD34D",
            )
            self.timer_note.configure(
                text="Сеть вернётся — выключение отменится", text_color="#D6B36A"
            )
            self.progress.configure(progress_color=RED if remaining <= 10 else AMBER)
            self.progress.set(
                max(0.0, min(1.0, remaining / max(1, engine.delay_seconds)))
            )
        elif not active:
            self.timer_card.configure(fg_color=FIELD, border_color=BORDER_SOFT)
            self.timer_title.configure(text="ЗАЩИТА ВЫКЛЮЧЕНА", text_color=DIM)
            self.timer_value.configure(
                text=f"{engine.delay_seconds} сек", text_color=MUTED
            )
            self.timer_note.configure(
                text="Нажмите ВКЛ, чтобы начать", text_color=DIM
            )
            self.progress.configure(progress_color="#334155")
            self.progress.set(0)
        elif self._notice and now < self._notice_until:
            restored = "вернул" in self._notice.lower() or "восстанов" in self._notice.lower()
            color = GREEN_SOFT if restored else MUTED
            self.timer_card.configure(
                fg_color=GREEN_DARK if restored else FIELD,
                border_color=GREEN_RING if restored else BORDER_SOFT,
            )
            self.timer_title.configure(
                text="ПИТАНИЕ ВОССТАНОВЛЕНО" if restored else "СОСТОЯНИЕ",
                text_color=GREEN_SOFT if restored else DIM,
            )
            self.timer_value.configure(
                text="ОТМЕНЕНО" if restored else "АКТИВНО",
                text_color=color,
            )
            self.timer_note.configure(
                text="Выключение не выполнено" if restored else self._notice,
                text_color="#86EFAC" if restored else DIM,
            )
            self.progress.configure(progress_color=GREEN if restored else "#334155")
            self.progress.set(0)
        else:
            self.timer_card.configure(fg_color=FIELD, border_color=BORDER_SOFT)
            self.timer_title.configure(text="ЗАДЕРЖКА ВЫКЛЮЧЕНИЯ", text_color=DIM)
            self.timer_value.configure(
                text=f"{engine.delay_seconds} сек", text_color=MUTED
            )
            self.timer_note.configure(
                text="Ожидание потери питания", text_color=DIM
            )
            self.progress.configure(progress_color="#334155")
            self.progress.set(0)

        if self._shutdown_error:
            self.timer_note.configure(text=self._shutdown_error, text_color=RED)

        # Settings ------------------------------------------------------------
        menu_state = "disabled" if (counting or shutdown) else "normal"
        if str(self.delay_menu.cget("state")) != menu_state:
            self.delay_menu.configure(state=menu_state)

        self._update_tray()

    @staticmethod
    def _format_seconds(seconds: int) -> str:
        minutes, secs = divmod(max(0, seconds), 60)
        return f"{minutes:02d}:{secs:02d}"

    def _handle_engine_event(self, event: Optional[str], now: float) -> None:
        if event == "power_lost":
            self._set_notice("Питание пропало — отсчёт запущен", now, 60)
            LOG.warning("Питание пропало, старт отсчёта %s сек.", self.engine.delay_seconds)
            self._notify(
                f"Питание пропало. Выключение через {self.engine.delay_seconds} сек., "
                "если сеть не вернётся.",
                "Power Guard",
            )
        elif event == "power_restored":
            self._set_notice("Сеть вернулась — выключение отменено", now, 8)
            LOG.info("Питание восстановлено, выключение отменено")
            self._notify("Питание восстановлено. Выключение отменено.", "Power Guard")

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def _tick(self) -> None:
        if self._quitting:
            return

        self._process_actions()
        if self._quitting:
            return
        now = time.monotonic()

        if now >= self._next_poll:
            snapshot = read_power_status()
            if snapshot is not None:
                event = self.engine.update_snapshot(snapshot, now)
                self._handle_engine_event(event, now)
            self._next_poll = now + 1.0

        if self.engine.tick(now):
            self._perform_shutdown()

        self._render(now)
        self.root.after(200, self._tick)

    def _perform_shutdown(self) -> None:
        if self._shutdown_called:
            return
        self._shutdown_called = True
        message = "Питание не восстановлено — выключаю компьютер"
        self._set_notice(message, time.monotonic(), 30)
        LOG.warning(message)
        self._notify(message, "Power Guard")

        if self.args.dry_run:
            self._shutdown_error = "Тестовый режим: shutdown не выполняется"
            LOG.info("Dry-run: shutdown пропущен")
            self._render(time.monotonic(), force=True)
            return

        shutdown_exe = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "shutdown.exe"
        try:
            subprocess.Popen(
                [
                    str(shutdown_exe),
                    "/s",
                    "/f",
                    "/t",
                    "0",
                    "/c",
                    "Power Guard: питание не восстановлено.",
                ],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                close_fds=True,
            )
        except OSError as exc:
            self._shutdown_error = f"Не удалось выключить: {exc}"
            LOG.exception("Не удалось запустить shutdown.exe")
        else:
            LOG.warning("Команда выключения отправлена")
        self._render(time.monotonic(), force=True)

    # ------------------------------------------------------------------
    # Tray
    # ------------------------------------------------------------------

    def _start_tray(self) -> None:
        try:
            menu = pystray.Menu(
                pystray.MenuItem(
                    "Открыть",
                    lambda icon, item: self._queue_action("show"),
                    default=True,
                ),
                pystray.MenuItem(
                    "Защита",
                    lambda icon, item: self._queue_action("toggle"),
                    checked=lambda item: self.engine.enabled,
                ),
                pystray.MenuItem(
                    "Автозапуск с Windows",
                    lambda icon, item: self._queue_action("autostart"),
                    checked=lambda item: self._autostart_enabled,
                ),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(
                    "Остановить и выйти",
                    lambda icon, item: self._queue_action("quit"),
                ),
            )
            self._tray = pystray.Icon(
                APP_NAME,
                self._icons["off"],
                APP_TITLE,
                menu,
            )
            self._tray.run_detached()
            self._update_tray(force=True)
        except Exception:
            LOG.exception("Не удалось запустить значок в трее")
            self._tray = None

    def _queue_action(self, action: str) -> None:
        self._actions.put(action)

    def _process_actions(self) -> None:
        while True:
            try:
                action = self._actions.get_nowait()
            except queue.Empty:
                return
            try:
                if action == "show":
                    self.show_window()
                elif action == "toggle":
                    self.toggle_protection()
                elif action == "autostart":
                    self.autostart_var.set(not bool(self.autostart_var.get()))
                    self._on_autostart_switched()
                elif action == "quit":
                    self.stop_and_close()
            except Exception:
                LOG.exception("Ошибка обработки действия трея: %s", action)

    def _tray_state(self) -> tuple[str, str]:
        if self.engine.shutdown_requested:
            return "critical", "Power Guard — выключение"
        if self.engine.countdown_active:
            remaining = self.engine.remaining(time.monotonic())
            return "alert", f"Power Guard — до выключения {remaining} сек."
        if self.engine.enabled:
            return "on", "Power Guard — защита включена"
        return "off", "Power Guard — защита выключена"

    def _update_tray(self, force: bool = False) -> None:
        if self._tray is None:
            return
        icon_name, title = self._tray_state()
        signature = (icon_name, title)
        if not force and signature == self._tray_signature:
            return
        self._tray_signature = signature
        try:
            self._tray.icon = self._icons[icon_name]
            self._tray.title = title
            self._tray.update_menu()
        except Exception:
            LOG.exception("Не удалось обновить значок в трее")

    def _notify(self, message: str, title: str = APP_TITLE) -> None:
        if self._tray is None:
            return
        try:
            self._tray.notify(message, title)
        except Exception:
            LOG.debug("Уведомление трея недоступно", exc_info=True)

    # ------------------------------------------------------------------
    # Window visibility and exit
    # ------------------------------------------------------------------

    def hide_to_tray(self) -> None:
        if self._quitting:
            return
        if self._tray is None:
            self._set_notice(
                "Значок в трее недоступен — окно оставлено открытым",
                time.monotonic(),
                duration=6,
            )
            self._render(time.monotonic(), force=True)
            return
        self.root.withdraw()
        if not self._hidden_notice_shown:
            self._hidden_notice_shown = True
            state = "Защита включена" if self.engine.enabled else "Защита выключена"
            self._notify(f"Power Guard работает в трее. {state}.", "Power Guard")

    def _on_unmap(self, _event=None) -> None:
        if self._quitting:
            return
        self.root.after(80, self._hide_if_minimized)

    def _hide_if_minimized(self) -> None:
        if self._quitting:
            return
        try:
            if self.root.winfo_exists() and self.root.state() == "iconic":
                self.hide_to_tray()
        except Exception:
            pass

    def show_window(self) -> None:
        try:
            self.root.deiconify()
            self.root.state("normal")
            self.root.lift()
            self.root.focus_force()
            self.root.attributes("-topmost", True)
            self.root.after(180, lambda: self.root.attributes("-topmost", False))
        except Exception:
            LOG.exception("Не удалось показать окно")

    def stop_and_close(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        LOG.info("Остановка программы")
        self.engine.deadline = None
        self.config.protection_enabled = self.engine.enabled
        self.config.save()

        if self._tray is not None:
            try:
                self._tray.stop()
            except Exception:
                LOG.exception("Не удалось остановить значок в трее")
            self._tray = None

        try:
            self.root.destroy()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--autostart", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def enable_dpi_awareness() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def main() -> int:
    args = parse_args()

    if not acquire_single_instance():
        return 0

    enable_dpi_awareness()
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "PowerGuard.Desktop.1"
        )
    except Exception:
        pass

    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("dark-blue")

    root = ctk.CTk()
    try:
        PowerGuardApp(root, args)
    except Exception:
        LOG.exception("Не удалось запустить приложение")
        try:
            root.destroy()
        except Exception:
            pass
        return 1

    try:
        root.mainloop()
    except KeyboardInterrupt:
        pass
    LOG.info("Приложение завершено")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
