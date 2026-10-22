#!/usr/bin/env python3
"""圖形介面的微信活動報名助手：以 OCR 尋找文字，不依賴固定螢幕座標。"""

from __future__ import annotations

import difflib
import ctypes
import io
import json
import logging
import os
import queue
import sys
import subprocess
import threading
import time
import tkinter as tk
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import colorchooser, filedialog, messagebox, ttk

import pyautogui
try:
    from Foundation import NSData
    from Vision import VNImageRequestHandler, VNRecognizeTextRequest
    import Quartz
    VISION_AVAILABLE = True
except ImportError:
    VISION_AVAILABLE = False

# One shared config on macOS, so the app bundle and a source run see the same
# calibration data. Other platforms keep the portable layout.
APP_DIR = (Path.home() / "Library" / "Application Support" / "WeChatSignupAssistant"
           if sys.platform == "darwin"
           else Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
           else Path(__file__).resolve().parent)
APP_DIR.mkdir(parents=True, exist_ok=True)
CONFIG = APP_DIR / "wechat_signup_app.json"
LOG = APP_DIR / "wechat_signup_app.log"
pyautogui.PAUSE = 0.03
pyautogui.FAILSAFE = True
_RAPID_OCR = None
_RAPID_OCR_FAST = None
_FAST_STEPS = (
    ("signup", "报名按钮"),
    ("role", "身份按钮"),
    ("confirm", "表单确认按钮"),
    ("dialog", "弹窗确认按钮"),
)
_PRESET_HINTS = {
    "signup": ("报名按钮", "请把微信停在【活动详情页】，鼠标停在底部「报名」按钮中心。\n"
                           "按钮还没开放（灰的）也可以先校准位置。"),
    "role": ("身份按钮", "请把微信停在【选择参与角色】页面，鼠标停在你要报名的身份按钮中心"
                         "（例如「观摩者」或「参加者」，名称要和页面上写的一致）。"),
    "confirm": ("表单确认", "请把微信停在【报名表单页】，鼠标停在右下角「确认」按钮中心。"),
    "dialog": ("弹窗确认", "请把微信停在【最终确认弹窗】，鼠标停在弹窗里的确认按钮中心。"),
}

# 小程序页面上的身份按钮写的是「参加者」，早期版本的下拉框写成「参与者」，
# 名称不一致会让 OCR 匹配不到，所以这里统一。
ROLE_CHOICES = ("观摩者", "参加者")
_ROLE_ALIASES = {
    "参与者": "参加者",
    "参加人员": "参加者",
    "观众": "观摩者",
    "旁听者": "观摩者",
}


def normalize_role(value: str) -> str:
    text = str(value or "").strip()
    if text in ROLE_CHOICES:
        return text
    return _ROLE_ALIASES.get(text, ROLE_CHOICES[0])


class SignupBurstTimeout(RuntimeError):
    """Raised when hammering the signup button never reached the signup page."""


# ---------------------------------------------------------------------------
# Appearance: palette, fonts, background image, Windows 11 mica hints
# ---------------------------------------------------------------------------
DEFAULT_UI = {
    "theme": "light",
    "accent": "#0a84ff",
    "bg_color": "#eef0f5",
    "card_color": "#ffffff",
    "fg_color": "#1d1d1f",
    "muted_color": "#6e6e73",
    "field_color": "#ffffff",
    "font_family": "auto",
    "font_size": 12,
    "log_font_size": 10,
    "bg_image": "",
    "bg_fit": "cover",
    "bg_image_strength": 45,
    "mica": False,
    "mica_strength": 35,
    "always_on_top": False,
}

# 背景图适配方式：值 → 界面上的说明
BG_FIT_MODES = {
    "cover": "填充裁剪（推荐，铺满窗口、超出裁掉）",
    "contain": "完整显示（整张图都看到，可能留边）",
    "stretch": "拉伸铺满（会变形）",
    "tile": "平铺（按原尺寸重复）",
}

# 窗口默认约 760×740，接近正方形，所以推荐用正方形图
BG_IMAGE_HINT = "建议方形图（如 1600×1600 或 2000×2000），重要内容放中间，四周留约 10% 余量"

DARK_UI = {
    "bg_color": "#1c1c1e",
    "card_color": "#2c2c2e",
    "fg_color": "#f5f5f7",
    "muted_color": "#98989d",
    "field_color": "#3a3a3c",
}

_FONT_CANDIDATES = (
    "SF Pro Text", "SF Pro Display", "PingFang SC", ".AppleSystemUIFont",
    "Segoe UI Variable Text", "Segoe UI", "Microsoft YaHei UI", "Microsoft YaHei",
    "Helvetica Neue", "Helvetica", "Arial",
)

_MONO_CANDIDATES = (
    "SF Mono", "Menlo", "Cascadia Mono", "Consolas", "Courier New", "Courier",
)

_INT_UI_KEYS = ("font_size", "log_font_size", "bg_image_strength", "mica_strength")

_COLOR_UI_KEYS = ("accent", "bg_color", "card_color", "fg_color", "muted_color", "field_color")

_BOOL_UI_KEYS = ("mica", "always_on_top")


def coerce_bool(value) -> bool:
    """Hand-edited configs may hold "false"/"no"/0 instead of a real boolean."""
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def clamp_channel(value: float) -> int:
    return max(0, min(255, int(round(value))))


def hex_to_rgb(value: str) -> tuple[int, int, int]:
    text = str(value).strip().lstrip("#")
    if len(text) == 3:
        text = "".join(char * 2 for char in text)
    if len(text) != 6:
        raise ValueError(f"颜色格式不对：{value}")
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)


def rgb_to_hex(rgb) -> str:
    red, green, blue = (clamp_channel(channel) for channel in tuple(rgb)[:3])
    return f"#{red:02x}{green:02x}{blue:02x}"


def blend_colors(color_a: str, color_b: str, ratio: float) -> str:
    """ratio=0 得到 color_a，ratio=1 得到 color_b。"""
    ratio = max(0.0, min(1.0, float(ratio)))
    first = hex_to_rgb(color_a)
    second = hex_to_rgb(color_b)
    return rgb_to_hex(tuple(a + (b - a) * ratio for a, b in zip(first, second)))


def readable_on(background: str) -> str:
    """Pick black or white text so it stays readable on that背景。"""
    red, green, blue = hex_to_rgb(background)
    luminance = (0.299 * red + 0.587 * green + 0.114 * blue) / 255
    return "#000000" if luminance > 0.6 else "#ffffff"


def looks_like_color(value: str) -> bool:
    text = str(value).strip().lstrip("#")
    return len(text) in (3, 6) and all(char in "0123456789abcdefABCDEF" for char in text)


def resource_path(name: str) -> Path | None:
    """Locate a data file next to the script or inside a frozen bundle."""
    candidates: list[Path] = []
    bundle_dir = getattr(sys, "_MEIPASS", None)
    if bundle_dir:
        candidates.append(Path(bundle_dir) / name)
    candidates.append(Path(__file__).resolve().parent / name)
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent / name)
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def load_ui_settings() -> dict:
    ui = dict(DEFAULT_UI)
    stored = read_config().get("ui")
    if isinstance(stored, dict):
        for key in DEFAULT_UI:
            if key not in stored:
                continue
            value = stored[key]
            try:
                if key in _INT_UI_KEYS:
                    value = int(value)
                elif key in _BOOL_UI_KEYS:
                    value = coerce_bool(value)
            except (TypeError, ValueError):
                continue
            ui[key] = value
    ui["font_size"] = max(9, min(24, int(ui["font_size"])))
    ui["log_font_size"] = max(8, min(20, int(ui["log_font_size"])))
    ui["bg_image_strength"] = max(0, min(100, int(ui["bg_image_strength"])))
    ui["mica_strength"] = max(0, min(100, int(ui["mica_strength"])))
    if ui.get("theme") not in ("light", "dark"):
        ui["theme"] = DEFAULT_UI["theme"]
    if ui.get("bg_fit") not in BG_FIT_MODES:
        ui["bg_fit"] = DEFAULT_UI["bg_fit"]
    for key in _COLOR_UI_KEYS:
        value = ui[key]
        if isinstance(value, str) and looks_like_color(value):
            ui[key] = rgb_to_hex(hex_to_rgb(value))
        else:
            ui[key] = DEFAULT_UI[key]
    return ui


def save_ui_settings(ui: dict) -> None:
    data = read_config()
    data["ui"] = {key: ui.get(key, DEFAULT_UI[key]) for key in DEFAULT_UI}
    CONFIG.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def resolve_font(available, requested: str) -> str:
    if requested and requested != "auto" and requested in available:
        return requested
    for candidate in _FONT_CANDIDATES:
        if candidate in available:
            return candidate
    return "TkDefaultFont"


def resolve_mono_font(available) -> str:
    for candidate in _MONO_CANDIDATES:
        if candidate in available:
            return candidate
    return resolve_font(available, "auto")


def fit_background(path: str, size, mode: str = "cover", pad_color: str = "#000000"):
    """Adapt an image to exactly `size` according to `mode`.

    cover   – fill the window, crop the overflow (keeps the aspect ratio)
    contain – show the whole image, pad the rest
    stretch – force the size (distorts, but never crops)
    tile    – repeat the image at its own scale
    """
    try:
        from PIL import Image
        image = Image.open(path).convert("RGB")
    except Exception:
        logging.exception("读取背景图片失败：%s", path)
        return None
    target_width, target_height = max(1, int(size[0])), max(1, int(size[1]))
    source_width, source_height = image.size
    if not source_width or not source_height:
        return None

    if mode == "stretch":
        return image.resize((target_width, target_height), Image.LANCZOS)

    if mode == "tile":
        canvas = Image.new("RGB", (target_width, target_height), pad_color)
        for y in range(0, target_height, source_height):
            for x in range(0, target_width, source_width):
                canvas.paste(image, (x, y))
        return canvas

    if mode == "contain":
        scale = min(target_width / source_width, target_height / source_height)
        resized = image.resize(
            (max(1, int(source_width * scale)), max(1, int(source_height * scale))),
            Image.LANCZOS,
        )
        canvas = Image.new("RGB", (target_width, target_height), pad_color)
        canvas.paste(resized, ((target_width - resized.width) // 2,
                               (target_height - resized.height) // 2))
        return canvas

    # cover (default): keep the aspect ratio and crop the overflow
    scale = max(target_width / source_width, target_height / source_height)
    resized = image.resize(
        (max(1, int(source_width * scale)), max(1, int(source_height * scale))),
        Image.LANCZOS,
    )
    left = max(0, (resized.width - target_width) // 2)
    top = max(0, (resized.height - target_height) // 2)
    return resized.crop((left, top, left + target_width, top + target_height))


def average_color(image) -> str:
    try:
        small = image.resize((8, 8))
        raw = small.tobytes()
    except Exception:
        return "#808080"
    if not raw:
        return "#808080"
    red = sum(raw[0::3]) / len(raw[0::3])
    green = sum(raw[1::3]) / len(raw[1::3])
    blue = sum(raw[2::3]) / len(raw[2::3])
    return rgb_to_hex((red, green, blue))


def apply_mica(root, enabled: bool, strength: int, dark: bool) -> bool:
    """Windows 11 backdrop hint plus real translucency.

    Tk cannot render the system Mica material itself, so this sets the DWM
    backdrop hint for the frame and uses window opacity as the tunable
    "通透程度" control.
    """
    if sys.platform != "win32":
        return False
    try:
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id()) or root.winfo_id()
        backdrop = ctypes.c_int(2 if enabled else 1)  # 2 = Mica, 1 = None
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, 38, ctypes.byref(backdrop), ctypes.sizeof(backdrop))
        dark_flag = ctypes.c_int(1 if dark else 0)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, 20, ctypes.byref(dark_flag), ctypes.sizeof(dark_flag))
        if enabled:
            alpha = 1.0 - max(0, min(100, int(strength))) / 100 * 0.45
            root.attributes("-alpha", round(alpha, 2))
        else:
            root.attributes("-alpha", 1.0)
        return True
    except Exception:
        logging.exception("设置云母背景失败")
        return False


def apply_ttk_theme(style, ui: dict, card_bg: str, page_bg: str) -> None:
    """Configure ttk widgets to match the palette."""
    family = ui["font_family"]
    size = int(ui["font_size"])
    fg = ui["fg_color"]
    muted = ui["muted_color"]
    accent = ui["accent"]
    field = ui["field_color"]
    border = blend_colors(card_bg, fg, 0.16)
    try:
        style.theme_use("clam")
    except Exception:
        pass

    style.configure(".", background=card_bg, foreground=fg, font=(family, size))
    style.configure("TFrame", background=card_bg)
    style.configure("Page.TFrame", background=page_bg)
    style.configure("Card.TFrame", background=card_bg)
    style.configure("TLabel", background=card_bg, foreground=fg)
    style.configure("Muted.TLabel", background=card_bg, foreground=muted)
    style.configure("Title.TLabel", background=card_bg, foreground=fg,
                    font=(family, size + 10, "bold"))
    style.configure("Subtitle.TLabel", background=card_bg, foreground=muted,
                    font=(family, size))
    style.configure("Section.TLabel", background=card_bg, foreground=muted,
                    font=(family, size - 1, "bold"))
    style.configure("TLabelframe", background=card_bg, bordercolor=border,
                    relief="solid", borderwidth=1)
    style.configure("TLabelframe.Label", background=card_bg, foreground=muted)
    style.configure("TButton", background=blend_colors(card_bg, fg, 0.06),
                    foreground=fg, borderwidth=0, focusthickness=0, padding=(12, 7))
    style.map("TButton",
              background=[("active", blend_colors(card_bg, fg, 0.14)),
                          ("disabled", blend_colors(card_bg, fg, 0.02))],
              foreground=[("disabled", muted)])
    style.configure("Accent.TButton", background=accent,
                    foreground=readable_on(accent), borderwidth=0,
                    focusthickness=0, padding=(16, 9))
    style.map("Accent.TButton",
              background=[("active", blend_colors(accent, "#000000", 0.14)),
                          ("disabled", blend_colors(accent, card_bg, 0.65))])
    style.configure("TEntry", fieldbackground=field, foreground=fg, insertcolor=fg,
                    bordercolor=border, lightcolor=field, darkcolor=field, padding=6)
    style.map("TEntry", fieldbackground=[("disabled", blend_colors(field, card_bg, 0.5))])
    style.configure("TCombobox", fieldbackground=field, background=field, foreground=fg,
                    arrowcolor=fg, bordercolor=border, padding=4)
    style.map("TCombobox",
              fieldbackground=[("readonly", field)],
              foreground=[("readonly", fg)])
    style.configure("TCheckbutton", background=card_bg, foreground=fg, focusthickness=0)
    style.map("TCheckbutton", background=[("active", card_bg)])
    style.configure("Nav.TButton", background=card_bg, foreground=muted, anchor="w",
                    borderwidth=0, focusthickness=0, padding=(12, 10))
    style.map("Nav.TButton",
              background=[("active", blend_colors(card_bg, fg, 0.06))],
              foreground=[("active", fg)])
    style.configure("NavOn.TButton", background=blend_colors(accent, card_bg, 0.78),
                    foreground=fg, anchor="w", borderwidth=0, focusthickness=0,
                    padding=(12, 10))
    style.map("NavOn.TButton",
              background=[("active", blend_colors(accent, card_bg, 0.70))])
    style.configure("Vertical.TScrollbar", background=blend_colors(card_bg, fg, 0.1),
                    troughcolor=card_bg, borderwidth=0, arrowcolor=muted)


# ---------------------------------------------------------------------------
# Windows window plumbing (ctypes only, so the frozen build needs no extras)
# ---------------------------------------------------------------------------
if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

    _user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    _user32.GetWindowTextLengthW.restype = ctypes.c_int
    _user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetWindowTextW.restype = ctypes.c_int
    _user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    _user32.GetWindowRect.restype = wintypes.BOOL
    _user32.IsWindowVisible.argtypes = [wintypes.HWND]
    _user32.IsWindowVisible.restype = wintypes.BOOL
    _user32.IsIconic.argtypes = [wintypes.HWND]
    _user32.IsIconic.restype = wintypes.BOOL
    _user32.GetAncestor.argtypes = [wintypes.HWND, ctypes.c_uint]
    _user32.GetAncestor.restype = wintypes.HWND
    _user32.WindowFromPoint.argtypes = [wintypes.POINT]
    _user32.WindowFromPoint.restype = wintypes.HWND
    _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    _user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    _user32.SetForegroundWindow.restype = wintypes.BOOL
    _user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.ShowWindow.restype = wintypes.BOOL
    _user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_void_p]
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
    ]
    _kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    _kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    _kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    _kernel32.GlobalLock.restype = wintypes.LPVOID
    _kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    _kernel32.GlobalUnlock.restype = wintypes.BOOL
    _user32.OpenClipboard.argtypes = [wintypes.HWND]
    _user32.OpenClipboard.restype = wintypes.BOOL
    _user32.EmptyClipboard.restype = wintypes.BOOL
    _user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    _user32.SetClipboardData.restype = wintypes.HANDLE
    _user32.CloseClipboard.restype = wintypes.BOOL

    def _win_window_text(hwnd: int) -> str:
        try:
            length = _user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return ""
            buffer = ctypes.create_unicode_buffer(length + 1)
            _user32.GetWindowTextW(hwnd, buffer, length + 1)
            return buffer.value
        except Exception:
            return ""

    def _win_window_pid(hwnd: int) -> int:
        try:
            pid = wintypes.DWORD()
            _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            return int(pid.value)
        except Exception:
            return 0

    def _win_window_rect(hwnd: int) -> tuple[int, int, int, int]:
        try:
            rect = wintypes.RECT()
            if not _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return 0, 0, 0, 0
            return (int(rect.left), int(rect.top),
                    int(rect.right - rect.left), int(rect.bottom - rect.top))
        except Exception:
            return 0, 0, 0, 0

    def _win_process_name(pid: int) -> str:
        process_query_limited_information = 0x1000
        handle = _kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return ""
        try:
            size = wintypes.DWORD(512)
            buffer = ctypes.create_unicode_buffer(size.value)
            if _kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return Path(buffer.value).name
        except Exception:
            return ""
        finally:
            _kernel32.CloseHandle(handle)
        return ""

    def _win_describe(hwnd: int) -> dict:
        left, top, width, height = _win_window_rect(hwnd)
        pid = _win_window_pid(hwnd)
        return {
            "id": int(hwnd),
            "pid": pid,
            "owner": _win_process_name(pid),
            "title": _win_window_text(hwnd),
            "left": left,
            "top": top,
            "width": width,
            "height": height,
        }

    def _win_list_windows() -> list[dict]:
        results: list[dict] = []
        enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def callback(hwnd, _lparam):
            try:
                if not _user32.IsWindowVisible(hwnd) or _user32.IsIconic(hwnd):
                    return True
                left, top, width, height = _win_window_rect(hwnd)
                if width < 120 or height < 120 or left < -10000 or top < -10000:
                    return True
                results.append(_win_describe(hwnd))
            except Exception:
                pass
            return True

        try:
            _user32.EnumWindows(enum_proc(callback), 0)
        except Exception:
            return results
        return results

    def _win_set_clipboard(text: str) -> bool:
        """Put unicode text on the clipboard without extra dependencies."""
        cf_unicode_text = 13
        gmem_moveable = 0x0002
        if not _user32.OpenClipboard(None):
            return False
        try:
            _user32.EmptyClipboard()
            data = text.encode("utf-16-le") + b"\x00\x00"
            handle = _kernel32.GlobalAlloc(gmem_moveable, len(data))
            if not handle:
                return False
            pointer = _kernel32.GlobalLock(handle)
            if not pointer:
                return False
            ctypes.memmove(pointer, data, len(data))
            _kernel32.GlobalUnlock(handle)
            if not _user32.SetClipboardData(cf_unicode_text, handle):
                return False
            return True
        except Exception:
            return False
        finally:
            _user32.CloseClipboard()


def norm(value: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFKC", value).casefold()
                   if ch.isalnum() or "\u3400" <= ch <= "\u9fff")


def read_config() -> dict:
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8")) if CONFIG.exists() else {}
    except (OSError, json.JSONDecodeError):
        return {}


def list_screen_windows() -> list[dict]:
    """List on-screen normal windows as plain dicts, front-most first."""
    windows: list[dict] = []
    if sys.platform == "win32":
        return _win_list_windows()
    if sys.platform == "darwin":
        try:
            import Quartz
            infos = Quartz.CGWindowListCopyWindowInfo(
                Quartz.kCGWindowListOptionOnScreenOnly,
                Quartz.kCGNullWindowID,
            ) or []
        except Exception:
            return windows
        for info in infos:
            try:
                if int(info.get(Quartz.kCGWindowLayer, 0) or 0) != 0:
                    continue
                bounds = info.get(Quartz.kCGWindowBounds, {}) or {}
                width = int(bounds.get("Width", 0))
                height = int(bounds.get("Height", 0))
                if width < 120 or height < 120:
                    continue
                windows.append({
                    "id": info.get(Quartz.kCGWindowNumber),
                    "pid": int(info.get(Quartz.kCGWindowOwnerPID, 0) or 0),
                    "owner": str(info.get(Quartz.kCGWindowOwnerName, "") or ""),
                    "title": str(info.get(Quartz.kCGWindowName, "") or ""),
                    "left": int(bounds.get("X", 0)),
                    "top": int(bounds.get("Y", 0)),
                    "width": width,
                    "height": height,
                })
            except Exception:
                continue
        return windows
    try:
        import pygetwindow
        for window in pygetwindow.getAllWindows():
            try:
                width, height = int(window.width), int(window.height)
                if width < 120 or height < 120:
                    continue
                title = str(getattr(window, "title", "") or "")
                windows.append({
                    "id": None, "pid": None, "owner": title, "title": title,
                    "left": int(window.left), "top": int(window.top),
                    "width": width, "height": height,
                })
            except Exception:
                continue
    except Exception:
        return windows
    return windows


def is_own_window(window: dict) -> bool:
    """The helper UI and the terminal that launched it are never valid targets."""
    if int(window.get("pid") or 0) == os.getpid():
        return True
    owner = str(window.get("owner", ""))
    return "报名助手" in owner


def window_kind(window: dict) -> str:
    owner = str(window.get("owner", "")).casefold()
    title = str(window.get("title", "")).casefold()
    if "weapp" in owner or "appex" in owner or "小程序" in owner or "小程序" in title:
        return "mini-program"
    if "wechat" in owner or "微信" in owner or "微信" in title:
        return "wechat"
    return "other"


def window_label(window: dict | None) -> str:
    if not window:
        return "未找到"
    owner = str(window.get("owner") or "?")
    title = str(window.get("title") or "")
    name = f"{owner}｜{title}" if title else owner
    return (f"{name}（{window.get('width')}×{window.get('height')}"
            f" @ {window.get('left')},{window.get('top')}）")


def window_signature(window: dict) -> dict:
    return {
        "id": window.get("id"),
        "pid": int(window.get("pid") or 0),
        "owner": str(window.get("owner") or ""),
        "title": str(window.get("title") or ""),
        "width": int(window.get("width") or 0),
        "height": int(window.get("height") or 0),
    }


def windows_equivalent(window: dict, signature: dict) -> bool:
    return window_signature(window) == window_signature(signature)


def window_under_point(x: int, y: int) -> dict | None:
    """The front-most window that actually contains the point."""
    if sys.platform == "win32":
        try:
            hwnd = _user32.WindowFromPoint(wintypes.POINT(x, y))
            if hwnd:
                # Chromium-based mini programs use child windows; walk to the top level.
                hwnd = _user32.GetAncestor(hwnd, 2) or hwnd
            if not hwnd:
                return None
            window = _win_describe(hwnd)
            if is_own_window(window):
                return None
            return window
        except Exception:
            return None
    for window in list_screen_windows():
        if is_own_window(window):
            continue
        if (window["left"] <= x <= window["left"] + window["width"]
                and window["top"] <= y <= window["top"] + window["height"]):
            return window
    return None


def match_saved_window(signature: dict, windows: list[dict]) -> dict | None:
    """Match the calibrated window. Never fall back to a different window here.

    Window ids are recycled by the window server, so an id alone proves nothing:
    it is only used to break ties between windows that already share owner/size.
    """
    if not signature:
        return None
    window_id = signature.get("id")
    pid = int(signature.get("pid") or 0)
    title = str(signature.get("title") or "")
    owner = str(signature.get("owner") or "")
    width = int(signature.get("width") or 0)
    height = int(signature.get("height") or 0)
    if not width or not height:
        return None
    # Without Screen Recording permission macOS hides every window title, so
    # fall back to process + exact size instead of giving up.
    hidden_titles = not any(str(window.get("title") or "") for window in windows)

    def same_size(window: dict) -> bool:
        return window.get("width") == width and window.get("height") == height

    # 1) Title + owner (+ size) is the strongest evidence.
    if title and owner:
        for window in windows:
            if (window.get("title") == title and window.get("owner") == owner
                    and same_size(window)):
                return window
        for window in windows:
            if window.get("title") == title and window.get("owner") == owner:
                return window
    # 2) Same owner and exact size. Never accept the chat window while we can
    #    still tell windows apart; window id and pid only break ties.
    sized = [window for window in windows
             if (not owner or window.get("owner") == owner) and same_size(window)]

    def acceptable(window: dict) -> bool:
        return hidden_titles or not looks_like_main_wechat(window)

    if window_id:
        for window in sized:
            if window.get("id") == window_id and acceptable(window):
                return window
    if pid:
        for window in sized:
            if int(window.get("pid") or 0) == pid and acceptable(window):
                return window
    for window in sized:
        if acceptable(window):
            return window
    return None


def pick_wechat_window(windows: list[dict]) -> dict | None:
    candidates = [window for window in windows
                  if not is_own_window(window) and window_kind(window) in ("mini-program", "wechat")]
    if not candidates:
        return None
    mini_programs = [window for window in candidates if window_kind(window) == "mini-program"]
    pool = mini_programs or candidates
    return max(pool, key=lambda window: window["width"] * window["height"])


def get_wechat_window(strict: bool = False) -> dict | None:
    """Resolve the target window: the calibrated one, else automatic.

    With strict=True a calibrated window that cannot be found returns None
    instead of silently falling back to a different WeChat window.
    """
    windows = list_screen_windows()
    saved = read_config().get("target_window") or {}
    if saved:
        matched = match_saved_window(saved, windows)
        if matched:
            return matched
        if strict:
            return None
    return pick_wechat_window(windows)


def describe_windows(limit: int = 8) -> str:
    rows = [window for window in list_screen_windows() if not is_own_window(window)]
    rows.sort(key=lambda window: -(window["width"] * window["height"]))
    if not rows:
        return "（没有可用的窗口）"
    return "；".join(f"{window_label(window)} id={window.get('id')}" for window in rows[:limit])


def signature_text(signature: dict) -> str:
    if not signature:
        return "（无）"
    return (f"{signature.get('owner') or '?'}｜{signature.get('title') or ''}"
            f"（{signature.get('width')}×{signature.get('height')}）")


def looks_like_main_wechat(window: dict) -> bool:
    """The chat window has no real title; the mini-program window is titled."""
    title = str(window.get("title") or "").strip().casefold()
    return title in ("", "微信", "wechat", "weixin")


def window_rect(window) -> tuple[int, int, int, int]:
    """Normalize native macOS dict and Windows pygetwindow objects."""
    if isinstance(window, dict):
        return (int(window["left"]), int(window["top"]),
                int(window["width"]), int(window["height"]))
    return (int(window.left), int(window.top), int(window.width), int(window.height))


def is_inside_window(x: int, y: int, window) -> bool:
    left, top, width, height = window_rect(window)
    return left <= x <= left + width and top <= y <= top + height


def mac_accessibility_allowed() -> bool:
    """Check whether macOS allows this process to synthesize mouse/keyboard input."""
    if sys.platform != "darwin":
        return True
    try:
        application_services = ctypes.CDLL(
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
        )
        trusted = application_services.AXIsProcessTrusted
        trusted.restype = ctypes.c_bool
        return bool(trusted())
    except (AttributeError, OSError):
        # If the platform API cannot be queried, don't falsely claim permission.
        return False


def screen_recording_allowed() -> bool:
    """Whether this process may read window contents (macOS Screen Recording)."""
    if sys.platform != "darwin":
        return True
    try:
        import Quartz
        return bool(Quartz.CGPreflightScreenCaptureAccess())
    except (AttributeError, OSError):
        return True


def request_screen_recording() -> bool:
    """Ask macOS for Screen Recording permission; shows the system prompt."""
    if sys.platform != "darwin":
        return True
    try:
        import Quartz
        return bool(Quartz.CGRequestScreenCaptureAccess())
    except (AttributeError, OSError):
        return False


def titles_available() -> bool:
    """Window titles are hidden until Screen Recording is granted."""
    return any(str(window.get("title") or "") for window in list_screen_windows()
               if not is_own_window(window))


def capture_window_image(window):
    """Grab only that window's own pixels. Returns (PIL image, pixels per point)."""
    if sys.platform == "win32":
        # Windows has no per-window capture without extra APIs, so capture the
        # screen area. The target window is activated right before this runs and
        # the helper window is moved aside, so that area shows the mini program.
        left, top, width, height = window_rect(window)
        if width <= 0 or height <= 0:
            return None, 1.0
        try:
            image = pyautogui.screenshot(region=(left, top, width, height))
        except Exception:
            return None, 1.0
        return image.convert("RGB"), (image.size[0] / width if width else 1.0)
    if sys.platform != "darwin":
        raise RuntimeError("窗口级抓图目前只在 macOS 上可用。")
    import Quartz
    from PIL import Image

    window_id = window.get("id")
    if not window_id:
        return None, 1.0
    cg_image = Quartz.CGWindowListCreateImage(
        Quartz.CGRectNull,
        Quartz.kCGWindowListOptionIncludingWindow,
        window_id,
        Quartz.kCGWindowImageBoundsIgnoreFraming,
    )
    if cg_image is None:
        return None, 1.0
    width = Quartz.CGImageGetWidth(cg_image)
    height = Quartz.CGImageGetHeight(cg_image)
    if not width or not height:
        return None, 1.0
    bytes_per_row = Quartz.CGImageGetBytesPerRow(cg_image)
    data = Quartz.CGDataProviderCopyData(Quartz.CGImageGetDataProvider(cg_image))
    image = Image.frombuffer("RGBA", (width, height), bytes(data), "raw", "BGRA",
                             bytes_per_row, 1).convert("RGB")
    window_width = int(window.get("width") or 0)
    return image, (width / window_width if window_width else 1.0)


def get_rapid_ocr(fast_det: bool = False):
    """Two cached engines: default for full-screen, tuned for window captures.

    The shipped default scales a small image's short side up to 736 px, which
    makes narrow button strips slower than the whole page. Window captures use
    a long-side cap instead.
    """
    global _RAPID_OCR, _RAPID_OCR_FAST
    from rapidocr import RapidOCR
    if fast_det:
        if _RAPID_OCR_FAST is None:
            _RAPID_OCR_FAST = RapidOCR(params={"Det.limit_type": "max",
                                               "Det.limit_side_len": 960})
        return _RAPID_OCR_FAST
    if _RAPID_OCR is None:
        _RAPID_OCR = RapidOCR()
    return _RAPID_OCR


def _ocr_image(image, fast_det: bool = False) -> list[dict]:
    """優先使用 RapidOCR；再退回 macOS Vision 和 Tesseract。座標為圖片像素。"""
    image_size = image.size
    errors = []
    try:
        import numpy as np
        engine = get_rapid_ocr(fast_det)
        result = engine(np.asarray(image.convert("RGB")))
        items = []
        if result.boxes is not None and result.txts is not None:
            for box, text in zip(result.boxes, result.txts):
                xs = [float(point[0]) for point in box]
                ys = [float(point[1]) for point in box]
                x, y = int(min(xs)), int(min(ys))
                w, h = int(max(xs) - min(xs)), int(max(ys) - min(ys))
                items.append({"text": str(text), "box": (x, y, w, h)})
        return items
    except Exception as rapid_error:
        errors.append(f"RapidOCR: {rapid_error}")
    try:
        if not VISION_AVAILABLE:
            raise RuntimeError("PyObjC Vision 不可用")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        raw = buffer.getvalue()
        data = NSData.dataWithBytes_length_(raw, len(raw))
        source = Quartz.CGImageSourceCreateWithData(data, None)
        cg_image = Quartz.CGImageSourceCreateImageAtIndex(source, 0, None)
        request = VNRecognizeTextRequest.alloc().init()
        request.setRecognitionLevel_(1)
        request.setRecognitionLanguages_(["zh-Hans", "zh-Hant", "en-US"])
        request.setUsesLanguageCorrection_(True)
        handler = VNImageRequestHandler.alloc().initWithCGImage_options_(cg_image, {})
        success, error = handler.performRequests_error_([request], None)
        if not success:
            raise RuntimeError(str(error))
        items = []
        for observation in request.results() or []:
            candidates = observation.topCandidates_(1)
            if not candidates:
                continue
            text = str(candidates[0].string())
            box = observation.boundingBox()
            x = int(box.origin.x * image_size[0])
            y = int((1 - box.origin.y - box.size.height) * image_size[1])
            w = int(box.size.width * image_size[0])
            h = int(box.size.height * image_size[1])
            items.append({"text": text, "box": (x, y, w, h)})
        return items
    except Exception as vision_error:
        errors.append(f"Vision: {vision_error}")
        try:
            import pytesseract
            from PIL import ImageOps
            data = pytesseract.image_to_data(
                ImageOps.autocontrast(image.convert("L")),
                lang="chi_sim+chi_tra+eng", config="--psm 11",
                output_type=pytesseract.Output.DICT,
            )
            items = []
            for i, text in enumerate(data["text"]):
                if text.strip():
                    items.append({"text": text.strip(), "box": (
                        data["left"][i], data["top"][i], data["width"][i], data["height"][i]
                    )})
            return items
        except Exception as tess_error:
            errors.append(f"Tesseract: {tess_error}")
            raise RuntimeError(
                "無法讀取螢幕文字。請安裝 requirements.txt 依賴，並確認已授予螢幕錄製權限。\n"
                + "\n".join(errors)
            ) from tess_error


def recognize_screen(window=None) -> tuple[list[dict], dict]:
    """OCR the target window (or the whole screen when window is None).

    Returned boxes are in points relative to the captured frame, so a frame
    origin lets the caller turn them back into screen coordinates.
    """
    if window is not None:
        image, scale = capture_window_image(window)
        if image is None:
            raise RuntimeError("无法获取目标窗口画面：窗口可能已关闭，或缺少屏幕录制权限。")
        frame = {
            "left": int(window["left"]),
            "top": int(window["top"]),
            "width": int(window["width"]),
            "height": int(window["height"]),
            "window": window,
        }
    else:
        image = pyautogui.screenshot()
        screen = pyautogui.size()
        frame = {"left": 0, "top": 0,
                 "width": int(screen.width), "height": int(screen.height)}
        scale = image.size[0] / frame["width"] if frame["width"] else 1.0
    if not scale:
        scale = 1.0
    items = []
    for item in _ocr_image(image, fast_det=window is not None):
        x, y, width, height = item["box"]
        items.append({"text": item["text"], "box": (
            round(x / scale), round(y / scale), round(width / scale), round(height / scale)
        )})
    return items, frame

def choose_match(items: list[dict], target: str, *, fuzzy: bool = False,
                 bottommost: bool = False, region: str | None = None,
                 image_size: tuple[int, int] | None = None) -> dict | None:
    wanted = norm(target)
    matches = []
    for item in items:
        if region and image_size:
            x, y, width, height = item["box"]
            center_x = (x + width / 2) / image_size[0]
            center_y = (y + height / 2) / image_size[1]
            if region == "bottom" and center_y < 0.72:
                continue
            if region == "dialog" and not (0.28 <= center_y <= 0.82):
                continue
        text_norm = norm(item["text"])
        if not text_norm:
            continue
        if fuzzy:
            score = difflib.SequenceMatcher(None, wanted, text_norm).ratio()
            if wanted in text_norm:
                score = max(score, 0.96)
            if score >= 0.50:
                matches.append((score, item))
        elif wanted in text_norm:
            matches.append((1.0, item))
    if not matches:
        return None
    if bottommost:
        return max((item for _, item in matches), key=lambda item: item["box"][1])
    return max(matches, key=lambda pair: pair[0])[1]


def frame_point_to_screen(px: float, py: float, frame: dict) -> tuple[int, int]:
    """Map a frame-relative point back to screen coordinates."""
    return round(frame["left"] + px), round(frame["top"] + py)


def window_text_lines(window, crop_bottom: float = 1.0) -> list[str]:
    """OCR the target window, optionally only its top part, for quick checks."""
    image, _ = capture_window_image(window)
    if image is None:
        raise RuntimeError("无法获取目标窗口画面：窗口可能已关闭，或缺少屏幕录制权限。")
    if crop_bottom < 1.0:
        height = max(1, int(image.height * crop_bottom))
        image = image.crop((0, 0, image.width, height))
    return [item["text"] for item in _ocr_image(image, fast_det=True)]


def region_warmth(image, box, scale: float) -> float:
    """How orange a region is. Selected role chips are orange, unselected grey."""
    try:
        left = max(0, int(box[0] * scale))
        top = max(0, int(box[1] * scale))
        right = min(image.width, max(left + 1, int((box[0] + box[2]) * scale)))
        bottom = min(image.height, max(top + 1, int((box[1] + box[3]) * scale)))
        crop = image.crop((left, top, right, bottom))
        if crop.width < 2 or crop.height < 2:
            return 0.0
        raw = crop.tobytes()
        values = []
        for index in range(0, len(raw) - 2, 3):
            warmth = raw[index] - (raw[index + 1] + raw[index + 2]) / 2
            if warmth > 0:
                values.append(warmth)
        if not values:
            return 0.0
        values.sort()
        return float(values[min(len(values) - 1, int(len(values) * 0.95))])
    except Exception:
        return 0.0


def activate_window(window) -> None:
    """Bring the target window to the front before clicking inside it."""
    if window is None:
        return
    if sys.platform == "win32":
        hwnd = window.get("id")
        if not hwnd:
            return
        try:
            _user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            # Windows only honours SetForegroundWindow for the active app; the
            # classic ALT nudge lifts that restriction.
            _user32.keybd_event(0x12, 0, 0, None)
            _user32.SetForegroundWindow(hwnd)
            _user32.keybd_event(0x12, 0, 2, None)
        except Exception:
            logging.exception("无法激活目标窗口 hwnd=%s", hwnd)
        return
    if sys.platform != "darwin":
        return
    pid = int(window.get("pid") or 0)
    if not pid:
        return
    try:
        from AppKit import NSApplicationActivateIgnoringOtherApps, NSRunningApplication
        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        if app is not None:
            app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
            time.sleep(0.20)
    except Exception:
        logging.exception("无法激活小程序所属应用 PID=%s", pid)


def click_screen_point(x: int, y: int, target_pid: int | None = None,
                       activate: bool = True, window=None) -> None:
    """Activate the mini-program process, then perform a normal system click."""
    if activate and window is not None:
        activate_window(window)
    elif activate and sys.platform == "darwin" and target_pid:
        try:
            from AppKit import NSApplicationActivateIgnoringOtherApps, NSRunningApplication
            app = NSRunningApplication.runningApplicationWithProcessIdentifier_(target_pid)
            if app is not None:
                app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
                time.sleep(0.20)
        except Exception:
            # Still attempt the ordinary click; the post-click page check will
            # stop the workflow if the app didn't receive it.
            logging.exception("无法激活小程序所属应用 PID=%s", target_pid)
    pyautogui.click(x, y)


def click_observation(item: dict, frame: dict, label: str,
                      target_pid: int | None = None) -> tuple[int, int]:
    x, y, width, height = item["box"]
    px, py = frame_point_to_screen(x + width / 2, y + height / 2, frame)
    logging.info("點擊 %s；OCR文字=%r；座標=(%d,%d)；目标PID=%s", label, item["text"], px, py, target_pid)
    click_screen_point(px, py, target_pid, window=frame.get("window"))
    return px, py


def find_role_on_signup_page(target: str, log, timeout: float = 4,
                             window=None, target_pid: int | None = None) -> dict | None:
    """Find a role only inside the mini-program's role card, never the helper UI."""
    deadline = time.monotonic() + timeout
    target_norm = norm(target)
    while time.monotonic() < deadline:
        items, frame = recognize_screen(window)
        header = next((item for item in items
                       if "选择参与角色" in norm(item["text"])), None)
        if header:
            hx, hy, hw, hh = header["box"]
            # Role chips sit right under the section header. Keep the search band
            # tight so unrelated text on the page can never become the target.
            exact = []
            partial = []
            for item in items:
                x, y, width, height = item["box"]
                if not (hy + hh <= y <= hy + hh + frame["height"] * 0.26):
                    continue
                if x < hx - hw * 0.2:
                    continue
                text_norm = norm(item["text"])
                if not text_norm:
                    continue
                if text_norm == target_norm:
                    exact.append(item)
                elif target_norm in text_norm:
                    partial.append(item)
            candidates = exact or partial
            if candidates:
                # 角色选项从标题下方开始排列，取最上面那个：选「观摩者」就点
                # 上方那个观摩者，选「参加者」就点参加者，不会点到别的文字。
                found = min(candidates, key=lambda item: item["box"][1])
                point = click_observation(found, frame, f"微信报名角色 {target}", target_pid)
                log(f"在「选择参与角色」里找到「{found['text']}」，点击 {point}")
                return found
        time.sleep(0.2)
    return None


def wait_and_click(target: str, log, timeout: float = 15, *, fuzzy: bool = False,
                   bottommost: bool = False, region: str | None = None,
                   required_context: tuple[str, ...] = (), window=None,
                   target_pid: int | None = None) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        items, frame = recognize_screen(window)
        context_found = [word for word in required_context
                         if any(norm(word) in norm(item["text"]) for item in items)]
        if required_context and len(context_found) != len(required_context):
            time.sleep(0.20)
            continue
        found = choose_match(items, target, fuzzy=fuzzy, bottommost=bottommost,
                             region=region, image_size=(frame["width"], frame["height"]))
        if found:
            point = click_observation(found, frame, target, target_pid)
            log(f"識別到「{found['text']}」，點擊 {point}")
            return found
        time.sleep(0.20)
    raise TimeoutError(f"等待畫面出現「{target}」逾時。")


def paste_unicode(text: str) -> None:
    if sys.platform == "win32":
        if _win_set_clipboard(text):
            pyautogui.hotkey("ctrl", "v")
        return
    subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True, timeout=5)
    pyautogui.hotkey("command", "v")


class SignupApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.ui = load_ui_settings()
        self.root.title("微信活动报名助手")
        # Fit the window to this screen so nothing opens off-screen.
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        window_width = min(980, max(780, screen_width - 160))
        window_height = min(700, max(560, screen_height - 200))
        self.root.geometry(f"{window_width}x{window_height}")
        self.root.minsize(min(720, window_width), min(540, window_height))
        self.root.attributes("-topmost", bool(self.ui.get("always_on_top")))
        self.events: queue.Queue[tuple[str, str]] = queue.Queue()
        self.stop_event = threading.Event()
        self.name = tk.StringVar()
        self.open_date = tk.StringVar(value=datetime.now().strftime("%Y-%m-%d"))
        self.open_clock = tk.StringVar()
        self.mode = tk.StringVar(value="我已打开活动详情页")
        self.role = tk.StringVar(value="观摩者")
        self.fast_mode = tk.BooleanVar(value=False)
        self.interval = tk.StringVar(value="1.0")
        self.lead = tk.StringVar(value="1.0")
        self.attempt = tk.StringVar(value="2.0")
        self.topmost_var = tk.BooleanVar(value=bool(self.ui.get("always_on_top")))
        self._bg_photo = None
        self._bg_resize_job = None
        self._mono_family = "TkFixedFont"
        self._font_family = "TkDefaultFont"
        self._card_bg = self.ui["card_color"]
        self.status = tk.StringVar(value="填写时间和活动关键词；也可以先打开活动详情，再选择手动模式。")
        self._build()
        self._apply_theme()
        self._apply_app_icon()
        self._load()
        try:
            self._refresh_target()
        except Exception:
            logging.exception("启动时检测目标窗口失败")
        self.root.after(100, self._drain_events)
        self.root.bind("<Configure>", self._on_configure)
        self._fade_in()
        self.root.protocol("WM_DELETE_WINDOW", self._close)

    def _build(self) -> None:
        self.bg_canvas = tk.Canvas(self.root, highlightthickness=0, bd=0, background=self.ui["bg_color"])
        self.bg_canvas.place(x=0, y=0, relwidth=1, relheight=1)

        self.page = ttk.Frame(self.root, style="Page.TFrame")
        self.page.place(x=0, y=0, relwidth=1, relheight=1)

        self.card = ttk.Frame(self.page, style="Card.TFrame", padding=22)
        self.card.pack(fill="both", expand=True, padx=14, pady=14)
        outer = self.card

        header = ttk.Frame(outer)
        header.pack(fill="x")
        title_box = ttk.Frame(header)
        title_box.pack(side="left", fill="x", expand=True)
        ttk.Label(title_box, text="微信活动报名助手", style="Title.TLabel").pack(anchor="w")
        ttk.Label(title_box, text="提前连点抢报名，也可以按 OCR 流程稳点",
                  style="Subtitle.TLabel").pack(anchor="w", pady=(3, 0))
        header_actions = ttk.Frame(header)
        header_actions.pack(side="right", anchor="n")
        ttk.Checkbutton(header_actions, text="窗口置顶", variable=self.topmost_var,
                        command=self._toggle_topmost).pack(side="left", padx=(0, 12))
        ttk.Button(header_actions, text="设置", command=self._open_settings).pack(side="left")

        # WeChat-style layout: left navigation, one short page per section,
        # and an always-visible action bar at the bottom. Nothing is taller
        # than the window any more, so nothing gets cut off on a small screen.
        body = ttk.Frame(outer)
        body.pack(fill="both", expand=True, pady=(14, 0))

        self.sidebar = ttk.Frame(body, width=132)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)
        self.nav_buttons = {}
        for key, label in (("target", "目标窗口"), ("signup", "报名设置"),
                           ("grab", "抢报名"), ("log", "日志")):
            button = ttk.Button(self.sidebar, text=label, style="Nav.TButton",
                                command=lambda name=key: self._show_pane(name))
            button.pack(fill="x", pady=(0, 4))
            self.nav_buttons[key] = button

        self.pane_holder = ttk.Frame(body)
        self.pane_holder.pack(side="left", fill="both", expand=True, padx=(18, 0))
        self.pane_holder.rowconfigure(0, weight=1)
        self.pane_holder.columnconfigure(0, weight=1)
        self.panes = {}
        for key in ("target", "signup", "grab", "log"):
            pane = ttk.Frame(self.pane_holder)
            pane.grid(row=0, column=0, sticky="nsew")
            self.panes[key] = pane

        self.bottom_bar = ttk.Frame(outer)
        self.bottom_bar.pack(side="bottom", fill="x", pady=(12, 0))

        outer = self.panes["target"]
        ttk.Label(outer, text="目标窗口", style="Section.TLabel").pack(anchor="w", pady=(0, 6))
        self.target_status = tk.StringVar(value="目标窗口：未确定")
        ttk.Label(outer, textvariable=self.target_status, style="Muted.TLabel",
                  wraplength=470).pack(anchor="w", pady=(0, 6))
        target_row = ttk.Frame(outer)
        target_row.pack(fill="x", pady=(0, 6))
        ttk.Button(target_row, text="刷新", command=self._refresh_target).pack(side="left")
        ttk.Button(target_row, text="选择目标窗口", command=self._choose_target_window).pack(side="left", padx=8)
        ttk.Button(target_row, text="检查权限", command=self._check_permissions).pack(side="left")

        outer = self.panes["signup"]
        ttk.Label(outer, text="活动与时间", style="Section.TLabel").pack(anchor="w", pady=(0, 6))
        ttk.Label(outer, text="操作方式").pack(anchor="w")
        ttk.Combobox(outer, textvariable=self.mode,
                     values=("自动搜索指尖派", "我已打开活动详情页"),
                     state="readonly").pack(fill="x", pady=(4, 10))
        ttk.Label(outer, text="活动关键词（自动模式必填，手动模式可留空）").pack(anchor="w")
        ttk.Entry(outer, textvariable=self.name).pack(fill="x", pady=(4, 10))
        ttk.Label(outer, text="报名开放时间（本机时间）").pack(anchor="w")
        time_row = ttk.Frame(outer)
        time_row.pack(fill="x", pady=(4, 10))
        ttk.Entry(time_row, textvariable=self.open_date, width=14).pack(side="left")
        ttk.Label(time_row, text="日期").pack(side="left", padx=(6, 18))
        ttk.Entry(time_row, textvariable=self.open_clock, width=11).pack(side="left")
        ttk.Label(time_row, text="时间 HH:MM:SS").pack(side="left", padx=6)
        ttk.Button(time_row, text="填入10秒后", command=self._set_test_time).pack(side="left", padx=(14, 0))
        role_row = ttk.Frame(outer)
        role_row.pack(fill="x", pady=(0, 4))
        ttk.Label(role_row, text="报名身份").pack(side="left")
        ttk.Combobox(role_row, textvariable=self.role, values=ROLE_CHOICES,
                     state="readonly", width=12).pack(side="left", padx=10)

        outer = self.panes["grab"]
        ttk.Label(outer, text="抢报名方式", style="Section.TLabel").pack(anchor="w", pady=(0, 6))
        ttk.Label(outer, text="常规模式用 OCR 找按钮（稳但慢）；极速直点用预设坐标连点，不跑 OCR。",
                  style="Muted.TLabel", wraplength=470).pack(anchor="w", pady=(0, 8))

        preset_row = ttk.Frame(outer)
        preset_row.pack(fill="x", pady=(0, 6))
        self.preset_status = tk.StringVar(value="未设置位置预设")
        self.preset_target = tk.StringVar(value="全部依次")
        ttk.Label(preset_row, text="校准位置").pack(side="left")
        ttk.Combobox(preset_row, textvariable=self.preset_target, state="readonly", width=10,
                     values=("全部依次", "报名按钮", "身份按钮", "表单确认", "弹窗确认")).pack(side="left", padx=6)
        ttk.Button(preset_row, text="开始校准", command=self._calibrate_selected).pack(side="left")
        ttk.Button(preset_row, text="清除预设", command=self._clear_presets).pack(side="left", padx=6)
        ttk.Label(preset_row, textvariable=self.preset_status, style="Muted.TLabel").pack(side="left", padx=6)

        learn_row = ttk.Frame(outer)
        learn_row.pack(fill="x", pady=(0, 6))
        self.record_button = ttk.Button(learn_row, text="学习我的点击（手动做一遍）",
                                        command=self._record_clicks)
        self.record_button.pack(side="left")
        self.sequence_status = tk.StringVar(value="未录制点击序列")
        ttk.Label(learn_row, textvariable=self.sequence_status, style="Muted.TLabel",
                  wraplength=380).pack(side="left", padx=8)

        fast_row = ttk.Frame(outer)
        fast_row.pack(fill="x", pady=(0, 6))
        ttk.Checkbutton(fast_row, text="极速直点（跳过OCR，按预设坐标一键连点）",
                        variable=self.fast_mode, command=self._toggle_fast).pack(side="left")
        ttk.Entry(fast_row, textvariable=self.interval, width=6).pack(side="left", padx=(10, 4))
        ttk.Label(fast_row, text="秒/步（点击后等待页面切换）", style="Muted.TLabel").pack(side="left")

        timing_row = ttk.Frame(outer)
        timing_row.pack(fill="x", pady=(0, 4))
        ttk.Label(timing_row, text="提前").pack(side="left")
        ttk.Entry(timing_row, textvariable=self.lead, width=5).pack(side="left", padx=(4, 2))
        ttk.Label(timing_row, text="秒开始连点「报名」；连点").pack(side="left")
        ttk.Entry(timing_row, textvariable=self.attempt, width=5).pack(side="left", padx=(4, 2))
        ttk.Label(timing_row, text="秒仍没进报名页就报告").pack(side="left")

        controls = ttk.Frame(self.bottom_bar)
        controls.pack(side="bottom", fill="x")
        ttk.Button(controls, text="测试识别当前页面", command=self._probe).pack(side="left")
        self.start_button = ttk.Button(controls, text="开始等待", command=self._start,
                                       style="Accent.TButton")
        self.start_button.pack(side="left", fill="x", expand=True)
        self.stop_button = ttk.Button(controls, text="停止", command=self.stop_event.set, state="disabled")
        self.stop_button.pack(side="left", padx=(8, 0))
        self.ring_canvas = tk.Canvas(controls, width=74, height=74, highlightthickness=0, bd=0,
                                     background=self._card_bg)
        self.ring_text = self.ring_canvas.create_text(
            37, 37, text="", font=(self._font_family, 11), fill=self.ui["muted_color"])
        self.ring_canvas.pack(side="right", padx=(12, 0))
        self.start_button.bind("<Enter>", lambda _event: self._animate_accent(True))
        self.start_button.bind("<Leave>", lambda _event: self._animate_accent(False))
        self.status_label = ttk.Label(self.bottom_bar, textvariable=self.status,
                                      style="Muted.TLabel", wraplength=560)
        self.status_label.pack(anchor="w", pady=(0, 8))

        outer = self.panes["log"]
        ttk.Label(outer, text="点击记录", style="Section.TLabel").pack(anchor="w", pady=(0, 6))
        log_wrap = ttk.Frame(outer)
        log_wrap.pack(fill="both", expand=True)
        self.log_view = tk.Text(log_wrap, height=12, wrap="word", state="disabled",
                                relief="flat", borderwidth=0, highlightthickness=0,
                                font=(self._mono_family, int(self.ui["log_font_size"])),
                                background=self.ui["field_color"],
                                foreground=self.ui["fg_color"],
                                insertbackground=self.ui["fg_color"])
        log_scroll = ttk.Scrollbar(log_wrap, orient="vertical", command=self.log_view.yview)
        self.log_view.configure(yscrollcommand=log_scroll.set)
        log_scroll.pack(side="right", fill="y")
        self.log_view.pack(side="left", fill="both", expand=True)
        self._show_pane("signup")

    def _show_pane(self, name: str) -> None:
        """Switch the right-hand page and highlight the matching nav item."""
        pane = self.panes.get(name)
        if pane is None:
            return
        for key, button in self.nav_buttons.items():
            try:
                button.configure(style="NavOn.TButton" if key == name else "Nav.TButton")
            except Exception:
                pass
        try:
            pane.tkraise()
        except Exception:
            logging.exception("切换界面分区失败")
        self.current_pane = name

    def _current_size(self) -> tuple[int, int]:
        try:
            self.root.update_idletasks()
        except Exception:
            pass
        return max(320, self.root.winfo_width()), max(240, self.root.winfo_height())

    def _apply_theme(self) -> None:
        """Push the current appearance settings into every widget."""
        try:
            import tkinter.font as tkfont
            available = set(tkfont.families(self.root))
        except Exception:
            available = set()
        resolved = resolve_font(available, str(self.ui.get("font_family", "auto")))
        self._font_family = resolved
        self._mono_family = resolve_mono_font(available)
        style_ui = dict(self.ui)
        style_ui["font_family"] = resolved

        page_bg = self.ui["bg_color"]
        card_bg = self.ui["card_color"]
        image_path = str(self.ui.get("bg_image") or "").strip()
        if image_path:
            image = fit_background(image_path, self._current_size(),
                                   str(self.ui.get("bg_fit", "cover")), page_bg)
            if image is not None:
                strength = max(0, min(100, int(self.ui.get("bg_image_strength", 45)))) / 100
                card_bg = blend_colors(card_bg, average_color(image), strength * 0.6)
        self._card_bg = card_bg

        try:
            apply_ttk_theme(ttk.Style(self.root), style_ui, card_bg, page_bg)
        except Exception:
            logging.exception("应用主题失败")
        try:
            self.root.configure(background=page_bg)
            for pattern, value in (
                ("*TCombobox*Listbox.background", self.ui["field_color"]),
                ("*TCombobox*Listbox.foreground", self.ui["fg_color"]),
                ("*TCombobox*Listbox.selectBackground", self.ui["accent"]),
                ("*TCombobox*Listbox.selectForeground", readable_on(self.ui["accent"])),
                ("*TCombobox*Listbox.font", (resolved, int(self.ui["font_size"]))),
            ):
                self.root.option_add(pattern, value)
        except Exception:
            logging.exception("设置控件颜色失败")
        try:
            self._draw_background()
        except Exception:
            logging.exception("绘制背景失败")
        try:
            self.log_view.configure(
                background=self.ui["field_color"],
                foreground=self.ui["fg_color"],
                insertbackground=self.ui["fg_color"],
                font=(self._mono_family, int(self.ui["log_font_size"])),
            )
        except Exception:
            logging.exception("设置日志区外观失败")
        try:
            self._draw_ring(0.0, "")
        except Exception:
            logging.exception("重置倒计时圆环失败")
        if sys.platform == "win32":
            apply_mica(self.root, bool(self.ui.get("mica")),
                       int(self.ui.get("mica_strength", 35)),
                       self.ui.get("theme") == "dark")

    def _draw_background(self) -> None:
        self.bg_canvas.delete("bg")
        self.bg_canvas.configure(background=self.ui["bg_color"])
        image_path = str(self.ui.get("bg_image") or "").strip()
        if not image_path:
            self._bg_photo = None
            return
        image = fit_background(image_path, self._current_size(),
                               str(self.ui.get("bg_fit", "cover")), self.ui["bg_color"])
        if image is None:
            self._bg_photo = None
            return
        try:
            from PIL import ImageTk
            self._bg_photo = ImageTk.PhotoImage(image)
        except Exception:
            logging.exception("背景图片加载失败")
            self._bg_photo = None
            return
        self.bg_canvas.create_image(0, 0, anchor="nw", image=self._bg_photo, tags="bg")

    def _on_configure(self, event) -> None:
        if event.widget is not self.root:
            return
        if self._bg_resize_job:
            try:
                self.root.after_cancel(self._bg_resize_job)
            except Exception:
                pass
        self._bg_resize_job = self.root.after(200, self._redraw_background)

    def _redraw_background(self) -> None:
        self._bg_resize_job = None
        try:
            self._draw_background()
        except Exception:
            logging.exception("窗口尺寸变化后重绘背景失败")

    def _draw_ring(self, fraction: float, label: str = "") -> None:
        """Countdown ring: Apple-style circular progress in the action row."""
        canvas = getattr(self, "ring_canvas", None)
        if canvas is None:
            return
        try:
            canvas.configure(background=self._card_bg)
            canvas.delete("ring")
            track = blend_colors(self._card_bg, self.ui["fg_color"], 0.14)
            canvas.create_oval(9, 9, 65, 65, outline=track, width=5, tags="ring")
            extent = max(0.0, min(1.0, float(fraction))) * 359.9
            if extent > 0.5:
                canvas.create_arc(9, 9, 65, 65, start=90, extent=-extent, style="arc",
                                  outline=self.ui["accent"], width=5, tags="ring")
            canvas.itemconfigure(self.ring_text, text=label, fill=self.ui["muted_color"])
        except Exception:
            logging.exception("绘制倒计时圆环失败")

    def _animate_accent(self, hover: bool) -> None:
        """Smooth colour transition on the primary button."""
        accent = self.ui["accent"]
        darker = blend_colors(accent, "#000000", 0.16)
        start, end = (accent, darker) if hover else (darker, accent)
        steps = 6

        def step(index: int) -> None:
            ratio = (index + 1) / steps
            try:
                ttk.Style(self.root).configure(
                    "Accent.TButton", background=blend_colors(start, end, ratio))
            except Exception:
                return
            if index + 1 < steps:
                self.root.after(16, lambda: step(index + 1))

        step(0)

    def _target_alpha(self) -> float:
        if sys.platform == "win32" and self.ui.get("mica"):
            strength = max(0, min(100, int(self.ui.get("mica_strength", 35))))
            return round(1.0 - strength / 100 * 0.45, 2)
        return 1.0

    def _fade_in(self) -> None:
        """Gentle fade on launch instead of the window popping in."""
        try:
            self.root.attributes("-alpha", 0.0)
        except Exception:
            return
        steps = 9
        target = self._target_alpha()

        def step(index: int) -> None:
            try:
                self.root.attributes("-alpha", round(min(target, target * (index + 1) / steps), 3))
            except Exception:
                return
            if index + 1 < steps:
                self.root.after(18, lambda: step(index + 1))

        self.root.after(30, lambda: step(0))
        # Safety net: never leave the window stuck at a low opacity.
        self.root.after(500, lambda: self.root.attributes("-alpha", target))

    def _reset_appearance(self) -> None:
        self.ui = dict(DEFAULT_UI)
        self._apply_theme()
        save_ui_settings(self.ui)
        self._log("外观已恢复默认")

    def _apply_app_icon(self) -> None:
        """Window icon, plus the Dock icon when running from source on macOS."""
        icon_path = resource_path("app_icon.png")
        if icon_path is None:
            return
        try:
            self._icon_photo = tk.PhotoImage(file=str(icon_path))
            self.root.iconphoto(True, self._icon_photo)
        except Exception:
            logging.exception("设置窗口图标失败")
        if sys.platform == "darwin":
            try:
                from AppKit import NSApplication, NSImage
                image = NSImage.alloc().initWithContentsOfFile_(str(icon_path))
                if image is not None:
                    NSApplication.sharedApplication().setApplicationIconImage_(image)
            except Exception:
                logging.exception("设置 Dock 图标失败")

    def _new_dialog(self, title: str, min_width: int, min_height: int):
        """Create a dialog that reliably stays in front of the topmost main window.

        A topmost parent hides a topmost child on macOS, so the main window
        gives up its "always on top" flag while a dialog is open, and gets it
        back as soon as the dialog closes.
        """
        root_was_topmost = bool(self.root.attributes("-topmost"))
        try:
            self.root.attributes("-topmost", False)
        except Exception:
            pass

        dialog = tk.Toplevel(self.root)
        dialog.title(title)
        dialog.minsize(min_width, min_height)
        dialog.transient(self.root)

        def close_dialog() -> None:
            try:
                self.root.attributes("-topmost", bool(self.ui.get("always_on_top")))
            except Exception:
                pass
            try:
                dialog.destroy()
            except Exception:
                pass

        dialog.protocol("WM_DELETE_WINDOW", close_dialog)
        return dialog, close_dialog

    def _show_dialog(self, dialog, min_width: int, min_height: int) -> None:
        """Size a dialog to its content and place it over the main window."""
        dialog.update_idletasks()
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        width = min(max(dialog.winfo_reqwidth(), min_width), max(min_width, screen_width - 80))
        height = min(max(dialog.winfo_reqheight(), min_height),
                     max(min_height, screen_height - 140))
        x = max(20, min(self.root.winfo_x() + (self.root.winfo_width() - width) // 2,
                        screen_width - width - 20))
        y = max(40, min(self.root.winfo_y() + 40, screen_height - height - 60))
        dialog.geometry(f"{width}x{height}+{x}+{y}")
        dialog.attributes("-topmost", True)
        dialog.lift()
        try:
            dialog.focus_force()
        except Exception:
            pass

    def _open_settings(self) -> None:
        dialog, close_dialog = self._new_dialog("设置", 620, 560)
        try:
            self._build_settings_body(dialog, close_dialog)
        except Exception as exc:
            logging.exception("构建设置面板失败")
            close_dialog()
            messagebox.showerror("设置面板打不开", f"{type(exc).__name__}: {exc}", parent=self.root)
            return
        self._show_dialog(dialog, 620, 560)

    def _build_settings_body(self, dialog: tk.Toplevel, close_dialog) -> None:

        body = ttk.Frame(dialog, padding=20)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="外观设置", style="Title.TLabel").pack(anchor="w")
        ttk.Label(body, text="改完点「应用」立即生效，「保存并关闭」会记住到下次启动。",
                  style="Subtitle.TLabel").pack(anchor="w", pady=(4, 14))

        theme_var = tk.StringVar(value=str(self.ui.get("theme", "light")))
        accent_var = tk.StringVar(value=self.ui["accent"])
        bg_var = tk.StringVar(value=self.ui["bg_color"])
        card_var = tk.StringVar(value=self.ui["card_color"])
        fg_var = tk.StringVar(value=self.ui["fg_color"])
        muted_var = tk.StringVar(value=self.ui["muted_color"])
        field_var = tk.StringVar(value=self.ui["field_color"])
        image_var = tk.StringVar(value=str(self.ui.get("bg_image") or ""))
        fit_var = tk.StringVar(value=BG_FIT_MODES.get(str(self.ui.get("bg_fit", "cover")),
                                                     BG_FIT_MODES["cover"]))
        topmost_var = tk.BooleanVar(value=bool(self.ui.get("always_on_top")))
        font_var = tk.StringVar(value=str(self.ui.get("font_family", "auto")))
        size_var = tk.IntVar(value=int(self.ui["font_size"]))
        log_size_var = tk.IntVar(value=int(self.ui["log_font_size"]))
        image_strength_var = tk.IntVar(value=int(self.ui["bg_image_strength"]))
        mica_var = tk.BooleanVar(value=bool(self.ui.get("mica")))
        mica_strength_var = tk.IntVar(value=int(self.ui["mica_strength"]))

        try:
            import tkinter.font as tkfont
            families = sorted(set(tkfont.families(self.root)))
        except Exception:
            families = []

        grid = ttk.Frame(body)
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)
        row_index = {"value": 0}
        swatches: list[tuple[tk.Button, tk.StringVar]] = []

        def add_row(label: str, widget) -> None:
            ttk.Label(grid, text=label).grid(row=row_index["value"], column=0, sticky="w",
                                             pady=6, padx=(0, 12))
            widget.grid(row=row_index["value"], column=1, sticky="ew", pady=6)
            row_index["value"] += 1

        def make_swatch(variable: tk.StringVar):
            holder = ttk.Frame(grid)
            button = tk.Button(holder, text="  ", width=5, relief="flat", borderwidth=1,
                               background=variable.get())
            button.pack(side="left")
            ttk.Label(holder, textvariable=variable, style="Muted.TLabel").pack(side="left", padx=8)

            def choose() -> None:
                chosen = colorchooser.askcolor(color=variable.get(), parent=dialog,
                                               title="选择颜色")
                if chosen and chosen[1]:
                    variable.set(chosen[1])
                    button.configure(background=chosen[1])

            button.configure(command=choose)
            swatches.append((button, variable))
            return holder

        theme_box = ttk.Combobox(grid, textvariable=theme_var, state="readonly",
                                 values=("light", "dark"))
        add_row("主题", theme_box)
        add_row("强调色", make_swatch(accent_var))
        add_row("背景颜色", make_swatch(bg_var))
        add_row("卡片颜色", make_swatch(card_var))
        add_row("文字颜色", make_swatch(fg_var))
        add_row("次要文字", make_swatch(muted_var))
        add_row("输入框颜色", make_swatch(field_var))

        font_box = ttk.Combobox(grid, textvariable=font_var, state="readonly",
                                values=("auto",) + tuple(families))
        add_row("字体（auto=跟随系统）", font_box)
        add_row("字号", ttk.Spinbox(grid, from_=9, to=22, textvariable=size_var, width=6))
        add_row("日志字号", ttk.Spinbox(grid, from_=8, to=18, textvariable=log_size_var, width=6))

        image_holder = ttk.Frame(grid)
        ttk.Entry(image_holder, textvariable=image_var).pack(side="left", fill="x", expand=True)

        def pick_image() -> None:
            chosen = filedialog.askopenfilename(
                parent=dialog, title="选择背景图片",
                filetypes=[("图片", "*.png *.jpg *.jpeg *.bmp *.gif *.webp"), ("全部文件", "*.*")],
            )
            if chosen:
                image_var.set(chosen)

        ttk.Button(image_holder, text="浏览…", command=pick_image).pack(side="left", padx=6)
        ttk.Button(image_holder, text="清除", command=lambda: image_var.set("")).pack(side="left")
        add_row("背景图片（留空=纯色）", image_holder)
        add_row("图片适配方式", ttk.Combobox(grid, textvariable=fit_var, state="readonly",
                                             values=tuple(BG_FIT_MODES.values())))
        ttk.Label(grid, text=BG_IMAGE_HINT, style="Muted.TLabel", wraplength=440).grid(
            row=row_index["value"], column=0, columnspan=2, sticky="w", pady=(0, 6))
        row_index["value"] += 1
        add_row("图片融合强度", ttk.Scale(grid, from_=0, to=100, orient="horizontal",
                                          variable=image_strength_var))
        add_row("窗口置顶", ttk.Checkbutton(grid, text="一直浮在最前面（默认关闭，和普通窗口一样）",
                                            variable=topmost_var))

        if sys.platform == "win32":
            add_row("云母背景（Windows 11）",
                    ttk.Checkbutton(grid, text="启用（系统材质 + 窗口通透）", variable=mica_var))
            add_row("云母通透程度", ttk.Scale(grid, from_=0, to=100, orient="horizontal",
                                              variable=mica_strength_var))
        else:
            ttk.Label(grid, text="云母背景仅 Windows 11 可用；这里可以用图片或纯色做背景。",
                      style="Muted.TLabel", wraplength=420).grid(
                row=row_index["value"], column=0, columnspan=2, sticky="w", pady=(10, 0))
            row_index["value"] += 1

        def refresh_swatches() -> None:
            for button, variable in swatches:
                try:
                    button.configure(background=variable.get())
                except Exception:
                    pass

        def apply_palette(name: str) -> None:
            palette = dict(DEFAULT_UI) if name == "light" else {**DEFAULT_UI, **DARK_UI}
            bg_var.set(palette["bg_color"])
            card_var.set(palette["card_color"])
            fg_var.set(palette["fg_color"])
            muted_var.set(palette["muted_color"])
            field_var.set(palette["field_color"])
            refresh_swatches()

        theme_box.bind("<<ComboboxSelected>>",
                       lambda _event: apply_palette(theme_var.get()))

        def collect() -> dict:
            ui = dict(self.ui)
            ui.update({
                "theme": theme_var.get(),
                "accent": accent_var.get(),
                "bg_color": bg_var.get(),
                "card_color": card_var.get(),
                "fg_color": fg_var.get(),
                "muted_color": muted_var.get(),
                "field_color": field_var.get(),
                "bg_image": image_var.get().strip(),
                "bg_fit": next((key for key, label in BG_FIT_MODES.items()
                                if label == fit_var.get()), "cover"),
                "font_family": font_var.get(),
                "font_size": int(size_var.get()),
                "log_font_size": int(log_size_var.get()),
                "bg_image_strength": int(float(image_strength_var.get())),
                "mica": bool(mica_var.get()),
                "mica_strength": int(float(mica_strength_var.get())),
                "always_on_top": bool(topmost_var.get()),
            })
            for key in ("accent", "bg_color", "card_color", "fg_color", "muted_color", "field_color"):
                hex_to_rgb(ui[key])
            return ui

        def apply_changes(save: bool) -> None:
            try:
                ui = collect()
            except Exception as exc:
                messagebox.showerror("设置有误", str(exc), parent=dialog)
                return
            self.ui = ui
            self.topmost_var.set(bool(ui.get("always_on_top")))
            try:
                self.root.attributes("-topmost", bool(ui.get("always_on_top")))
            except Exception:
                logging.exception("应用窗口置顶设置失败")
            self._apply_theme()
            if save:
                save_ui_settings(ui)
                self._log("外观设置已保存")

        buttons = ttk.Frame(body)
        # Pack the buttons first at the bottom so they stay visible even when the
        # dialog gets squeezed on a short screen.
        buttons.pack(side="bottom", fill="x", pady=(18, 0))
        ttk.Button(buttons, text="应用", command=lambda: apply_changes(False)).pack(side="right")
        ttk.Button(buttons, text="保存并关闭",
                   command=lambda: (apply_changes(True), close_dialog())).pack(side="right", padx=8)
        ttk.Button(buttons, text="恢复默认",
                   command=lambda: (self._reset_appearance(), close_dialog())).pack(side="left")

    def _set_test_time(self) -> None:
        future = datetime.now() + timedelta(seconds=10)
        self.open_date.set(future.strftime("%Y-%m-%d"))
        self.open_clock.set(future.strftime("%H:%M:%S"))
        self.status.set("已填入本机时间 10 秒后；确认微信页面和活动后，再按“开始等待”。"
                        "可用它测试极速直点是否命中。")

    def _toggle_topmost(self) -> None:
        value = bool(self.topmost_var.get())
        self.ui["always_on_top"] = value
        try:
            self.root.attributes("-topmost", value)
        except Exception:
            logging.exception("切换窗口置顶失败")
        save_ui_settings(self.ui)
        self._log("窗口置顶：" + ("开（会一直浮在最前面）" if value else "关（和普通窗口一样）"))

    def _toggle_fast(self) -> None:
        if self.fast_mode.get():
            missing = [label for key, label in _FAST_STEPS if self._preset_point(key) is None]
            if missing:
                self.status.set("极速直点还缺位置预设：" + "、".join(missing)
                                + "；请点「校准角色/确认位置」补全。")
            else:
                self.status.set("极速直点已就绪：时间一到即按 报名→身份→确认→弹窗确认 连点。")
        else:
            self.status.set("已关闭极速直点，将使用 OCR 流程。")

    def _validate(self):
        try:
            moment = datetime.strptime(
                f"{self.open_date.get().strip()} {self.open_clock.get().strip()}",
                "%Y-%m-%d %H:%M:%S",
            )
        except ValueError as exc:
            raise ValueError("日期格式 YYYY-MM-DD；时间格式 HH:MM:SS。可按“填入10秒后”测试。") from exc
        if moment <= datetime.now():
            raise ValueError("请输入未来的报名开放时间。")
        if self.mode.get() == "自动搜索指尖派" and not self.name.get().strip():
            raise ValueError("自动模式请至少输入一段活动关键词；例如标题中的几个字。")
        fast = self.fast_mode.get()
        interval = 1.0
        if fast:
            if not self._click_sequence():
                missing = [label for key, label in _FAST_STEPS if self._preset_point(key) is None]
                if missing:
                    raise ValueError(
                        "极速直点需要「学习点击」录制一次，或者把四个位置都校准好。\n"
                        "目前还缺：" + "、".join(missing)
                        + "\n\n如果现在进不去后面的页面，就先点「学习我的点击」，"
                          "手动走一遍报名流程让程序记住位置。"
                    )
                points = [self._preset_point(key) for key, _ in _FAST_STEPS]
                first = points[0]
                if all(abs(point[0] - first[0]) < 0.01 and abs(point[1] - first[1]) < 0.01
                       for point in points):
                    raise ValueError(
                        "四个预设位置几乎完全一样，看起来是同一点。\n\n"
                        "这通常是因为校准时没能切换到对应页面。请分别校准各个位置，"
                        "或改用「学习我的点击」录制一次真实操作。"
                    )
            try:
                interval = float(self.interval.get())
            except ValueError as exc:
                raise ValueError("直点间隔必须是数字，例如 1.0。") from exc
            if not 0.1 <= interval <= 10.0:
                raise ValueError("直点间隔请在 0.1～10 秒之间。")
        try:
            lead = float(self.lead.get())
        except ValueError as exc:
            raise ValueError("「提前」必须是数字，例如 1.0。") from exc
        if not 0.0 <= lead <= 10.0:
            raise ValueError("「提前」请在 0～10 秒之间。")
        try:
            attempt = float(self.attempt.get())
        except ValueError as exc:
            raise ValueError("「连点上限」必须是数字，例如 2.0。") from exc
        if not 0.5 <= attempt <= 60.0:
            raise ValueError("「连点上限」请在 0.5～60 秒之间。")
        return moment, fast, interval, lead, attempt

    def _start(self) -> None:
        missing = []
        if not mac_accessibility_allowed():
            missing.append("辅助功能")
        if not screen_recording_allowed():
            missing.append("屏幕录制")
        if missing:
            if "屏幕录制" in missing:
                request_screen_recording()
            messagebox.showerror(
                "需要 macOS 权限",
                "缺少权限：" + "、".join(missing) + "\n\n"
                "请打开「系统设置 → 隐私与安全性」，分别进入：\n"
                "• 辅助功能 —— 勾选「微信活动报名助手」\n"
                "• 屏幕录制 —— 勾选「微信活动报名助手」\n\n"
                "授权后请完全退出本程序（Command+Q）再重新打开，否则权限不生效。\n"
                "缺辅助功能 → 点击无效；缺屏幕录制 → 读不到窗口标题和画面。",
            )
            return
        try:
            moment, fast_mode, interval, lead, attempt = self._validate()
        except ValueError as exc:
            messagebox.showerror("请检查输入", str(exc))
            return
        title = self.name.get().strip() or "当前打开的活动"
        mode = self.mode.get()
        role = self.role.get()
        self._save()
        self._show_pane("log")
        self.stop_event.clear()
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.root.attributes("-topmost", False)
        threading.Thread(target=self._worker,
                         args=(title, moment, mode, role, fast_mode, interval, lead, attempt),
                         daemon=True).start()

    def _probe(self) -> None:
        if getattr(self, "probing", False) or self.stop_button["state"] == "normal":
            return
        missing = []
        if not mac_accessibility_allowed():
            missing.append("辅助功能")
        if not screen_recording_allowed():
            missing.append("屏幕录制")
        if missing:
            if "屏幕录制" in missing:
                request_screen_recording()
            messagebox.showwarning(
                "权限不足",
                "缺少权限：" + "、".join(missing) + "\n\n"
                "请到「系统设置 → 隐私与安全性」里给「微信活动报名助手」勾上，"
                "然后完全退出本程序再重新打开（缺屏幕录制时读不到窗口标题和画面）。",
                parent=self.root,
            )
            return
        self.probing = True
        self.status.set("正在识别当前微信画面，不会点击…")
        self.root.attributes("-topmost", False)
        def probe_worker():
            try:
                if self.mode.get() == "自动搜索指尖派":
                    self._bring_wechat_front()
                    time.sleep(0.8)
                items, _ = recognize_screen()
                sample = "；".join(item["text"] for item in items[:12])
                self._log(f"畫面識別測試成功，共 {len(items)} 段文字；畫面文字：{sample}")
                self.events.put(("done", "识别成功。请看下面的点击记录确认能读到微信文字。"))
            except Exception as exc:
                self._log(f"畫面識別測試失敗：{exc}")
                self.events.put(("done", f"识别失败：{exc}"))
            finally:
                self.probing = False
        threading.Thread(target=probe_worker, daemon=True).start()

    def _log(self, message: str) -> None:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        line = f"{stamp}  {message}"
        logging.info(message)
        self.events.put(("log", line))

    @staticmethod
    def _bring_wechat_front() -> None:
        if sys.platform == "darwin":
            subprocess.run(["open", "-a", "WeChat"], check=True, timeout=10)
        elif sys.platform == "win32":
            candidates = [window for window in list_screen_windows()
                          if "wechat" in str(window.get("owner", "")).casefold()
                          or "微信" in str(window.get("title", ""))]
            if not candidates:
                raise RuntimeError("未找到已打开的微信窗口，请先手动打开微信。")
            biggest = max(candidates, key=lambda window: window["width"] * window["height"])
            activate_window(biggest)
            time.sleep(0.4)

    def _calibrate_selected(self) -> None:
        choice = self.preset_target.get()
        steps = []
        for key, _ in _FAST_STEPS:
            label, hint = _PRESET_HINTS[key]
            if choice == "全部依次" or choice == label:
                steps.append((key, label, hint))
        if not steps:
            messagebox.showerror("请选择位置", "请先在下拉框里选择要校准的位置。", parent=self.root)
            return
        self._calibrate(steps)

    def _calibrate(self, steps) -> None:
        self.root.attributes("-topmost", False)
        try:
            positions = {}
            signature = None
            for key, label, hint in steps:
                messagebox.showinfo(
                    "位置校准",
                    f"{hint}\n\n点击确定后，你有 5 秒时间把鼠标移到按钮中心并保持不动。",
                    parent=self.root,
                )
                time.sleep(5)
                p = pyautogui.position()
                # Judge the window that is actually under the cursor: the user is
                # hovering the mini-program button, so that window is the target.
                window = window_under_point(p.x, p.y)
                if not window:
                    raise RuntimeError(
                        "鼠标所在位置没有检测到窗口。请把鼠标停在【指尖派小程序】的按钮中心，"
                        "不要停在助手窗口上，然后重新校准。"
                    )
                kind = window_kind(window)
                if kind == "other" and not messagebox.askyesno(
                    "确认目标窗口",
                    f"鼠标下面的窗口看起来不是微信或小程序：\n\n{window_label(window)}\n\n"
                    "如果它确实是指尖派小程序窗口，选“是”继续；否则选“否”重新校准。",
                    parent=self.root,
                ):
                    raise RuntimeError("已取消校准：请把鼠标停在指尖派小程序窗口的按钮上。")
                if signature is None:
                    signature = window_signature(window)
                    self._log(f"锁定目标窗口：{window_label(window)}（类型：{kind}）")
                elif not windows_equivalent(window, signature):
                    if not messagebox.askyesno(
                        "目标窗口变了",
                        f"这次鼠标下的窗口和第一次不是同一个：\n\n{window_label(window)}\n\n"
                        "四个位置必须都在同一个窗口内校准。确定要用这个窗口吗？",
                        parent=self.root,
                    ):
                        raise RuntimeError("已取消校准：四个位置需要在同一个窗口内校准。")
                    signature = window_signature(window)
                left, top, width, height = window_rect(window)
                positions[key] = [(p.x - left) / width, (p.y - top) / height]
                self._log(f"校准 {label}：窗口相对位置=({positions[key][0]:.4f},{positions[key][1]:.4f})")
            data = self._read_config()
            merged = dict(data.get("presets") or {})
            merged.update(positions)
            data["presets"] = merged
            data["preset_version"] = 3
            if signature:
                data["target_window"] = signature
            CONFIG.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            self.preset_status.set(self._preset_summary())
            self._refresh_target()
        except Exception as exc:
            messagebox.showerror("校准失败", str(exc), parent=self.root)
        finally:
            self.root.attributes("-topmost", bool(self.ui.get("always_on_top")))

    def _preset_summary(self) -> str:
        labels = [label for key, label in _FAST_STEPS if self._preset_point(key) is not None]
        if len(labels) == len(_FAST_STEPS):
            return "四个位置已齐全，可开启极速直点"
        if not labels:
            return "未设置位置预设"
        return "已保存：" + "、".join(labels) + f"（共 {len(labels)}/{len(_FAST_STEPS)}）"

    def _clear_presets(self) -> None:
        data = self._read_config()
        data.pop("presets", None)
        data.pop("preset_version", None)
        data.pop("target_window", None)
        data.pop("click_sequence", None)
        CONFIG.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        self.preset_status.set("未设置位置预设")
        self.target_status.set("目标窗口：未确定")
        self.sequence_status.set("未录制点击序列")
        self.fast_mode.set(False)
        self.status.set("已清除位置预设。")

    def _refresh_target(self) -> None:
        kind_names = {"mini-program": "小程序", "wechat": "微信", "other": "其他"}
        window = get_wechat_window()
        if not window:
            self.target_status.set("目标窗口：未找到（请先打开指尖派小程序）")
            return
        kind = kind_names.get(window_kind(window), "其他")
        label = window_label(window)
        if not titles_available():
            label += "（读不到窗口标题：缺屏幕录制权限）"
        elif looks_like_main_wechat(window):
            label += "  ← 像是微信主窗口，不是小程序"
        self.target_status.set(f"目标窗口（{kind}）：{label}")
        self._log(f"目标窗口判定为{kind}：{label}")

    def _choose_target_window(self) -> None:
        """Let the user point at the mini-program window explicitly."""
        candidates = [window for window in list_screen_windows() if not is_own_window(window)]
        if not candidates:
            messagebox.showerror(
                "没有可选窗口",
                "没有检测到可用窗口。请先打开指尖派小程序，必要时点「检查权限」确认屏幕录制权限。",
                parent=self.root,
            )
            return
        candidates.sort(key=lambda window: -(window["width"] * window["height"]))

        dialog, close_dialog = self._new_dialog("选择目标窗口", 620, 380)
        ttk.Label(dialog, text="选中指尖派小程序窗口（标题是活动或小程序名、竖版的那个），不要选微信聊天窗口：",
                  wraplength=580).pack(anchor="w", padx=12, pady=(12, 6))
        listbox = tk.Listbox(dialog, font=(self._mono_family, int(self.ui["font_size"])),
                             background=self.ui["field_color"], foreground=self.ui["fg_color"],
                             selectbackground=self.ui["accent"],
                             selectforeground=readable_on(self.ui["accent"]),
                             relief="flat", highlightthickness=0)
        for window in candidates:
            note = "   ← 像是微信主窗口，别选这个" if looks_like_main_wechat(window) else ""
            listbox.insert("end", f"{window_label(window)}{note}")
        listbox.pack(fill="both", expand=True, padx=12, pady=6)

        current = self._read_config().get("target_window") or {}
        for index, window in enumerate(candidates):
            if current and all(window.get(key) == current.get(key)
                               for key in ("owner", "title", "width", "height")):
                listbox.selection_set(index)
                break
        else:
            for index, window in enumerate(candidates):
                if not looks_like_main_wechat(window):
                    listbox.selection_set(index)
                    break

        def confirm() -> None:
            selection = listbox.curselection()
            if not selection:
                return
            chosen = candidates[selection[0]]
            data = self._read_config()
            data["target_window"] = window_signature(chosen)
            CONFIG.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            self._log(f"手动指定目标窗口：{window_label(chosen)}")
            close_dialog()
            self._refresh_target()

        buttons = ttk.Frame(dialog)
        buttons.pack(fill="x", padx=12, pady=(0, 12))
        ttk.Button(buttons, text="确定", command=confirm).pack(side="right")
        ttk.Button(buttons, text="取消", command=close_dialog).pack(side="right", padx=6)
        self._show_dialog(dialog, 620, 380)

    def _check_permissions(self) -> None:
        access_ok = mac_accessibility_allowed()
        screen_ok = screen_recording_allowed()
        if not screen_ok:
            # Ask macOS to list this app under Screen Recording.
            request_screen_recording()
        location = self._program_location()
        lines = [
            f"辅助功能（控制鼠标键盘）：{'已授权' if access_ok else '未授权'}",
            f"屏幕录制（读窗口画面和标题）：{'已授权' if screen_ok else '未授权'}",
            f"窗口标题读取：{'正常' if titles_available() else '读不到（通常是缺屏幕录制权限）'}",
            f"本程序位置：{location}",
        ]
        if access_ok and screen_ok:
            messagebox.showinfo("权限检查", "两项权限都已授权：\n\n" + "\n".join(lines),
                                parent=self.root)
            self._log("权限检查：辅助功能、屏幕录制均已授权")
            return
        messagebox.showwarning(
            "权限检查",
            "\n".join(lines) + "\n\n"
            "请打开「系统设置 → 隐私与安全性」，分别进入「辅助功能」和「屏幕录制」，"
            "勾选「微信活动报名助手」。\n\n"
            "注意：开关打开后必须完全退出本程序（Command+Q）再重新打开，"
            "权限只在进程启动时读取一次，正在运行的进程不会变。\n\n"
            "如果开关已经是打开的、而且你确实重启过本程序，那说明这条授权记录对应的是旧版本"
            "（每次重新打包签名都会变）。请在这两个列表里选中「微信活动报名助手」按 − 号删掉，"
            "再从「微信抢活动程序2.0」文件夹重新打开本程序、点一次「检查权限」，"
            "然后把新出现的开关打开，最后再退出重开一次。",
            parent=self.root,
        )
        self._log("权限检查：" + "；".join(lines))

    @staticmethod
    def _program_location() -> str:
        if getattr(sys, "frozen", False):
            executable = Path(sys.executable).resolve()
            for parent in executable.parents:
                if parent.suffix == ".app":
                    return str(parent)
            return str(executable)
        return str(Path(__file__).resolve())

    def _keep_helper_clear(self, window) -> None:
        """On Windows we capture screen regions, so our own window must not cover
        the mini program. Move the helper to a spot that does not overlap."""
        if sys.platform != "win32" or not window:
            return
        try:
            self.root.update_idletasks()
            helper_width = max(self.root.winfo_width(), 400)
            helper_height = max(self.root.winfo_height(), 300)
            helper = {"left": self.root.winfo_x(), "top": self.root.winfo_y(),
                      "width": helper_width, "height": helper_height}
        except Exception:
            return

        def overlaps(candidate) -> bool:
            return not (candidate["left"] + candidate["width"] <= window["left"]
                        or window["left"] + window["width"] <= candidate["left"]
                        or candidate["top"] + candidate["height"] <= window["top"]
                        or window["top"] + window["height"] <= candidate["top"])

        if not overlaps(helper):
            return
        screen = pyautogui.size()
        options = [
            (0, screen.height - helper_height),
            (screen.width - helper_width, screen.height - helper_height),
            (0, 0),
            (screen.width - helper_width, 0),
        ]
        for x, y in options:
            candidate = {"left": x, "top": y, "width": helper_width, "height": helper_height}
            if not overlaps(candidate):
                try:
                    self.root.geometry(f"{helper_width}x{helper_height}+{x}+{y}")
                    self._log(f"已把助手窗口移到 ({x},{y})，避免遮住小程序画面")
                except Exception:
                    logging.exception("移动助手窗口失败")
                return
        self._log("助手窗口和小程序窗口有重叠，抓图可能不准，建议手动把助手窗口挪开")

    def _record_clicks(self) -> None:
        """Learn the real click sequence by watching the user do it once."""
        if getattr(self, "recording", False):
            self.recording = False
            self.record_button.configure(text="学习我的点击（手动做一遍）")
            self.status.set("已停止学习，正在保存…")
            return
        if self.stop_button["state"] == "normal":
            return
        window = get_wechat_window()
        if not window:
            messagebox.showerror(
                "找不到目标窗口",
                "请先打开指尖派小程序，并点「刷新目标窗口」确认程序能识别到它。",
                parent=self.root,
            )
            return
        messagebox.showinfo(
            "学习我的点击",
            "接下来请你在指尖派里手动走一遍报名流程，程序会记录你点过的位置。\n\n"
            "• 只记录落在小程序窗口里的点击，点助手窗口不会被记录；\n"
            "• 表单页可以点「取消」退出，不会真的报名；\n"
            "• 做完后回到助手窗口点「停止学习」。\n\n"
            f"当前目标窗口：{window_label(window)}",
            parent=self.root,
        )
        self.recording = True
        self.record_button.configure(text="停止学习")
        self.root.attributes("-topmost", False)
        self.status.set("正在学习点击：请在小程序里手动完成一次报名操作。")
        threading.Thread(target=self._record_worker, daemon=True).start()

    def _record_worker(self) -> None:
        recorded: list[list[float]] = []
        anchor = None
        pressed = False
        deadline = time.monotonic() + 300
        try:
            while self.recording and time.monotonic() < deadline:
                try:
                    state = bool(Quartz.CGEventSourceButtonState(
                        Quartz.kCGEventSourceStateCombinedSessionState,
                        Quartz.kCGMouseButtonLeft,
                    ))
                except Exception:
                    state = False
                if state and not pressed:
                    point = pyautogui.position()
                    # Anchor to the window the user actually clicks in, so the
                    # recording follows the mini program and not the chat window.
                    if anchor is None:
                        anchor = window_under_point(point.x, point.y)
                        if anchor is not None:
                            self._log(f"学习锚定窗口：{window_label(anchor)}")
                    elif match_saved_window(window_signature(anchor), list_screen_windows()) is None:
                        # The anchored window went away; re-anchor under the cursor.
                        anchor = window_under_point(point.x, point.y) or anchor
                        self._log(f"学习重新锚定窗口：{window_label(anchor)}")
                    if anchor:
                        left, top, width, height = window_rect(anchor)
                        if (left <= point.x <= left + width) and (top <= point.y <= top + height):
                            recorded.append([(point.x - left) / width, (point.y - top) / height])
                            self._log(f"学习到第 {len(recorded)} 次点击："
                                      f"({recorded[-1][0]:.4f},{recorded[-1][1]:.4f})")
                pressed = state
                time.sleep(0.03)
        except Exception as exc:
            self._log(f"学习点击失败：{exc}")
            self.recording = False
            self.events.put(("done", f"学习点击失败：{exc}"))
            return
        finally:
            self.recording = False

        if recorded:
            data = self._read_config()
            data["click_sequence"] = recorded
            if anchor:
                data["target_window"] = window_signature(anchor)
            CONFIG.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            self._log(f"已保存 {len(recorded)} 次点击；极速直点会按这个顺序回放")
            if anchor:
                self.target_status.set(f"目标窗口（已锁定）：{window_label(anchor)}")
            self.events.put(("done", f"已学习 {len(recorded)} 次点击。"
                                     f"勾选极速直点后，到点会按这个顺序自动点。"))
        else:
            self._log("没有记录到点击：只记录落在小程序窗口内的鼠标点击")
            self.events.put(("done", "没有记录到点击。请确认点击发生在指尖派小程序窗口内。"))

    @staticmethod
    def _read_config() -> dict:
        return read_config()

    @staticmethod
    def _preset_point(key: str) -> list[float] | None:
        presets = SignupApp._read_config().get("presets") or {}
        point = presets.get(key)
        if (not isinstance(point, (list, tuple)) or len(point) != 2
                or not all(isinstance(value, (int, float)) for value in point)):
            return None
        return [float(point[0]), float(point[1])]

    def _click_preset(self, key: str, label: str, expected_context: tuple[str, ...]) -> bool:
        point = self._preset_point(key)
        if point is None:
            return False
        window = get_wechat_window(strict=True)
        if not window:
            self._log("找不到校准时的目标窗口，拒绝使用预设坐标")
            return False
        # Require page-specific evidence from inside WeChat before using a preset.
        items, _ = recognize_screen(window)
        expected = expected_context
        if not all(any(norm(token) in norm(item["text"]) for item in items) for token in expected):
            raise RuntimeError("微信窗口中未识别到报名角色页标题，拒绝点击预设位置。")
        self._click_relative(point, f"預設位置 {label}", window)
        return True

    def _click_preset_direct(self, key: str, label: str, window) -> bool:
        """Click a preset position without any OCR verification."""
        point = self._preset_point(key)
        if point is None:
            return False
        self._click_relative(point, label, window)
        return True

    def _click_relative(self, point, label: str, window, activate: bool = True) -> None:
        """Click a window-relative position (0~1) inside the target window."""
        left, top, width, height = window_rect(window)
        x = round(left + point[0] * width)
        y = round(top + point[1] * height)
        pid = int(window.get("pid", 0)) if isinstance(window, dict) else None
        click_screen_point(x, y, pid or None, activate=activate, window=window)
        self._log(f"點擊「{label}」=({x},{y})")

    def _click_sequence(self) -> list[list[float]]:
        raw = self._read_config().get("click_sequence") or []
        sequence = []
        for point in raw:
            if (isinstance(point, (list, tuple)) and len(point) == 2
                    and all(isinstance(value, (int, float)) for value in point)):
                sequence.append([float(point[0]), float(point[1])])
        return sequence

    def _signup_point(self):
        """Where the signup button is: the calibrated preset, else learned step 1."""
        point = self._preset_point("signup")
        if point is not None:
            return point
        sequence = self._click_sequence()
        return sequence[0] if sequence else None

    def _role_page_visible(self, window) -> bool:
        try:
            lines = window_text_lines(window, crop_bottom=0.55)
        except Exception as exc:
            self._log(f"報名頁檢查失敗：{exc}")
            return False
        return any("选择参与角色" in norm(line) for line in lines)

    def _read_role_chips(self, window):
        """Return (frame, chips) where chips maps a role name to its OCR item."""
        items, frame = recognize_screen(window)
        header = next((item for item in items
                       if "选择参与角色" in norm(item["text"])), None)
        if header is None:
            return frame, {}
        hx, hy, hw, hh = header["box"]
        chips: dict[str, dict] = {}
        for item in items:
            x, y, width, height = item["box"]
            if not (hy + hh - 8 <= y <= hy + hh + frame["height"] * 0.20):
                continue
            if x < hx - hw * 0.25:
                continue
            text_norm = norm(item["text"])
            if not text_norm:
                continue
            for choice in ROLE_CHOICES:
                if norm(choice) in text_norm and choice not in chips:
                    chips[choice] = item
        return frame, chips

    def _ensure_role_selected(self, window, role: str, target_pid: int | None = None) -> None:
        """Confirm the selected role; click only when it differs.

        Stops with a clear message instead of guessing, because submitting the
        wrong identity is worse than not submitting at all.
        """
        image, scale = capture_window_image(window)
        if image is None:
            raise RuntimeError("读不到小程序画面，无法确认报名身份，已停止。")

        frame, chips = self._read_role_chips(window)
        if not chips:
            raise RuntimeError(
                "页面上没找到「选择参与角色」里的身份按钮，无法确认身份，已停止（不会盲点）。"
            )
        if role not in chips:
            raise RuntimeError(
                f"页面上没有「{role}」这个身份按钮（只看到 {'、'.join(chips)}），已停止。"
            )

        warmth = {name: region_warmth(image, item["box"], scale) for name, item in chips.items()}
        selected = max(warmth, key=warmth.get)
        spread = max(warmth.values()) - min(warmth.values())
        detail = "，".join(f"{name}={value:.0f}" for name, value in warmth.items())
        self._log(f"身份识别：当前选中≈「{selected}」（{detail}）")

        if selected == role and spread >= 8:
            self._log(f"当前已经是「{role}」，跳过身份点击")
            return
        if spread < 8 and len(warmth) > 1:
            raise RuntimeError(
                f"看不出哪个身份是选中状态（{detail}），为避免点错已停止。"
                "可以把这一页截图发我，我按你的页面调识别阈值。"
            )

        self._log(f"当前选中的不是「{role}」，点击切换")
        click_observation(chips[role], frame, f"报名身份 {role}", target_pid)

        deadline = time.monotonic() + 2.5
        while time.monotonic() < deadline:
            time.sleep(0.25)
            if self.stop_event.is_set():
                return
            image_now, scale_now = capture_window_image(window)
            if image_now is None:
                continue
            mine = region_warmth(image_now, chips[role]["box"], scale_now)
            others = [region_warmth(image_now, item["box"], scale_now)
                      for name, item in chips.items() if name != role]
            if mine > max(others, default=0) + 4:
                self._log(f"已确认「{role}」处于选中状态")
                return
        raise RuntimeError(
            f"点了「{role}」但页面没有变成选中状态，已停止，避免用错身份提交。"
        )

    def _burst_click(self, point, window, activate: bool) -> None:
        left, top, width, height = window_rect(window)
        x = round(left + point[0] * width)
        y = round(top + point[1] * height)
        pid = int(window.get("pid", 0)) if isinstance(window, dict) else None
        click_screen_point(x, y, pid or None, activate=activate, window=window)

    def _burst_signup(self, window, limit: float):
        """Hammer the signup button until the signup page appears.

        Returns the window once the role page is visible, otherwise raises
        SignupBurstTimeout so the caller can report a failed grab.
        """
        point = self._signup_point()
        if point is None:
            return None
        started = time.monotonic()
        deadline = started + limit
        clicks = 0
        while time.monotonic() < deadline:
            if self.stop_event.is_set():
                self.events.put(("done", "已停止。"))
                return None
            current = get_wechat_window(strict=True) or window
            if not current:
                raise RuntimeError("目标窗口消失，已停止连点。")
            # Only re-activate the app now and then: activation costs 0.2s.
            self._burst_click(point, current, activate=(clicks % 8 == 0))
            clicks += 1
            if clicks % 3 == 0 and self._role_page_visible(current):
                elapsed = time.monotonic() - started
                self._log(f"報名成功：連點 {clicks} 次、{elapsed:.2f} 秒後進入報名頁")
                return current
            self.stop_event.wait(0.04)
        current = get_wechat_window(strict=True) or window
        if current and self._role_page_visible(current):
            return current
        elapsed = time.monotonic() - started
        raise SignupBurstTimeout(
            f"連續點擊報名按鈕 {clicks} 次、共 {elapsed:.2f} 秒，仍未進入報名頁，"
            "判斷為沒搶到（可能活動太火、名額已滿、按鈕還沒開放，或報名按鈕位置需要重新校準）。"
        )

    def _run_fast_flow(self, window, role: str, interval: float, limit: float) -> None:
        """Replay learned clicks, or the four calibrated presets in order."""
        if self._signup_point() is not None:
            window = self._burst_signup(window, limit)
        sequence = self._click_sequence()
        if sequence:
            steps = list(sequence)
            signup = self._signup_point()
            # A learned recording starts with the signup click; the burst above
            # already did that, so drop it instead of clicking it twice.
            if (signup is not None and steps
                    and abs(steps[0][0] - signup[0]) < 0.03
                    and abs(steps[0][1] - signup[1]) < 0.03):
                steps = steps[1:]
            self._log(f"按「學習的點擊」回放，共 {len(steps)} 步（報名那一下已由連點完成）")
            for index, point in enumerate(steps, start=1):
                if self.stop_event.is_set():
                    self._log("使用者停止，回放中止")
                    self.events.put(("done", "已停止。"))
                    return
                window = get_wechat_window(strict=True)
                if not window:
                    raise RuntimeError("找不到目标窗口，为安全起见回放中止。请点「刷新目标窗口」确认。")
                self._click_relative(point, f"學習序列第 {index} 步", window)
                self.stop_event.wait(interval)
            self._log("極速回放完成；請在微信核對最終報名結果。")
            self.events.put(("done", "極速回放完成；請核對微信中的最終報名結果。"))
            return
        steps = (("signup", "报名按钮"), ("role", f"报名身份 {role}"),
                 ("confirm", "报名表单确认"), ("dialog", "弹窗报名确认"))
        for key, label in steps:
            if self.stop_event.is_set():
                self._log("使用者停止，直点流程中止")
                self.events.put(("done", "已停止。"))
                return
            window = get_wechat_window(strict=True)
            if not window:
                raise RuntimeError("找不到目标窗口，为安全起见直点中止。请点「刷新目标窗口」确认。")
            if not self._click_preset_direct(key, label, window):
                raise RuntimeError(f"缺少「{label}」预设坐标；请重新校准四个位置。")
            self.stop_event.wait(interval)
        self._log("極速直點四步完成；請在微信核對最終報名結果。")
        self.events.put(("done", "極速直點完成；請核對微信中的最終報名結果。"))

    def _worker(self, title: str, moment: datetime, mode: str, role: str,
                fast_mode: bool, interval: float, lead: float, attempt: float) -> None:
        logging.basicConfig(filename=LOG, level=logging.INFO,
                            format="%(asctime)s %(levelname)s %(message)s")
        self._log(f"排程：活動關鍵字={title!r}；模式={mode}；身份={role}；"
                  f"極速直點={fast_mode}；時間={moment:%Y-%m-%d %H:%M:%S}")
        if fast_mode:
            self._log("極速直點已啟用：不做任何 OCR 校驗，請確保微信停在活動詳情頁。")
        try:
            if mode == "自动搜索指尖派":
                self._bring_wechat_front()
                self._log("已切换到 WeChat")
                pyautogui.hotkey("command", "f")
                time.sleep(0.25)
                pyautogui.hotkey("command", "a")
                paste_unicode("指尖派")
                self._log("按 Command+F，輸入小程式名稱「指尖派」")
                time.sleep(1.2)
                try:
                    wait_and_click("小程序", self._log, timeout=4)
                    time.sleep(0.4)
                    self._log("切換到小程序搜尋結果")
                except TimeoutError:
                    self._log("畫面沒有單獨的小程序分類，直接搜尋結果文字")
                wait_and_click("指尖派", self._log, timeout=12, bottommost=True)
                self._log("已點擊「指尖派」搜尋結果，等待小程序載入")
                time.sleep(1.4)
                wait_and_click(title, self._log, timeout=20, fuzzy=True)
                self._log(f"已按關鍵字 {title!r} 模糊匹配並打開活動")

            saved = self._read_config().get("target_window") or {}
            window = get_wechat_window(strict=True)
            if not window:
                raise RuntimeError(
                    "找不到校准时锁定的目标窗口，为安全起见没有继续操作。\n"
                    f"校准时记录的是：{signature_text(saved)}\n"
                    f"当前屏幕上的窗口：{describe_windows()}\n\n"
                    "请点「刷新目标窗口」确认，必要时重新校准或重新录制。"
                )
            if titles_available() and looks_like_main_wechat(window):
                raise RuntimeError(
                    "当前认定的目标窗口是微信主窗口（聊天窗口），不是指尖派小程序，"
                    "已停下以免点错地方。\n"
                    f"窗口：{window_label(window)}\n"
                    f"校准时记录的是：{signature_text(saved)}\n\n"
                    "请点「选择目标窗口」手动选中指尖派小程序，或点「刷新目标窗口」看程序认的是哪个。"
                )
            burst_point = self._signup_point()
            if burst_point is None:
                lead = 0.0
            start_at = moment - timedelta(seconds=lead)
            remaining = (start_at - datetime.now()).total_seconds()
            if burst_point is None:
                self._log(f"活動頁已就緒，等待開放；剩餘約 {max(0, remaining):.1f} 秒")
            else:
                self._log(f"活動頁已就緒；將提前 {lead:.1f} 秒開始連點「報名」，"
                          f"剩餘約 {max(0, remaining):.1f} 秒")
            total_wait = max(0.1, (start_at - datetime.now()).total_seconds())
            while not self.stop_event.is_set():
                remaining = (start_at - datetime.now()).total_seconds()
                if remaining <= 0:
                    break
                self.events.put(("progress", f"{max(0.0, remaining / total_wait):.4f}|{remaining:.0f}"))
                self.stop_event.wait(min(0.1, remaining))
            if self.stop_event.is_set():
                self._log("使用者停止，尚未點擊報名")
                self.events.put(("done", "已停止，尚未提交。"))
                return

            self._log("報名時間到，開始執行" + ("（極速直點）" if fast_mode else "（OCR 流程）"))
            self._log(f"本次操作窗口：{window_label(window)}（{window_kind(window)}）")
            self._keep_helper_clear(window)
            if fast_mode:
                self._run_fast_flow(window, role, interval, attempt)
                return
            target_pid = int(window.get("pid", 0)) if isinstance(window, dict) else None
            role_page_seen = False
            if burst_point is not None:
                self._log(f"連點「報名」按鈕，最多 {attempt:.1f} 秒")
                window = self._burst_signup(window, attempt)
                role_page_seen = True
            else:
                # Window-level capture: the helper UI can never enter this image.
                wait_and_click("报名", self._log, timeout=8, bottommost=True, region="bottom",
                               window=window, target_pid=target_pid)
            if not role_page_seen:
                page_deadline = time.monotonic() + 8
                while time.monotonic() < page_deadline:
                    page_items, _ = recognize_screen(window)
                    if any("选择参与角色" in norm(item["text"]) for item in page_items):
                        role_page_seen = True
                        break
                    if self.stop_event.wait(0.15):
                        self._log("使用者停止，报名页尚未确认")
                        self.events.put(("done", "已停止。"))
                        return
            if not role_page_seen:
                raise RuntimeError(
                    "已向小程序窗口发送“报名”点击，但 8 秒内没有出现“选择参与角色”页面。"
                    "请检查按钮是否可报名，以及是否已授予辅助功能/屏幕录制权限。"
                )
            # Click the identity only when the page currently shows a different
            # one. If the state cannot be read, stop rather than guess.
            self._ensure_role_selected(window, role, target_pid)
            if not self._click_preset("confirm", "报名表单确认", ("选择参与角色",)):
                wait_and_click("确认", self._log, timeout=8, bottommost=True,
                               region="bottom", required_context=("选择参与角色",),
                               window=window, target_pid=target_pid)
            self._log("已提交報名表單，等待最終確認彈窗")
            try:
                wait_and_click("确定报名", self._log, timeout=5, bottommost=True,
                               region="dialog", window=window, target_pid=target_pid)
            except TimeoutError:
                wait_and_click("确认报名", self._log, timeout=3, bottommost=True,
                               region="dialog", window=window, target_pid=target_pid)
            self._log("已點擊彈窗中的報名確認文字；請在微信核對服務端結果")
            self.events.put(("done", "點擊流程已完成；請核對微信中的最終報名結果。"))
        except pyautogui.FailSafeException:
            self._log("PyAutoGUI 緊急停止：滑鼠移到螢幕左上角")
            self.events.put(("done", "已安全停止。"))
        except SignupBurstTimeout as exc:
            self._log(f"報名未成功：{exc}")
            self.events.put(("alert", f"沒搶到報名：\n\n{exc}"))
            self.events.put(("done", "已停止：連點報名按鈕未進入報名頁。"))
        except Exception as exc:
            self._log(f"流程中止：{type(exc).__name__}: {exc}")
            self.events.put(("done", f"流程中止：{exc}\n請查看點擊記錄。"))

    def _drain_events(self) -> None:
        try:
            while True:
                kind, text = self.events.get_nowait()
                if kind == "log":
                    self.log_view.configure(state="normal")
                    self.log_view.insert("end", text + "\n")
                    self.log_view.see("end")
                    self.log_view.configure(state="disabled")
                    self.status.set(text.split("  ", 1)[-1])
                elif kind == "progress":
                    fraction_text, _, seconds_text = text.partition("|")
                    try:
                        self._draw_ring(float(fraction_text), f"{float(seconds_text):.0f}s")
                    except Exception:
                        pass
                elif kind == "alert":
                    self.root.deiconify()
                    self.root.lift()
                    messagebox.showwarning("報名未成功", text, parent=self.root)
                else:
                    self.status.set(text)
                    self.start_button.configure(state="normal")
                    self.stop_button.configure(state="disabled")
                    self.record_button.configure(text="学习我的点击（手动做一遍）")
                    self._draw_ring(0.0, "")
                    self.root.attributes("-topmost", bool(self.ui.get("always_on_top")))
                    self.root.deiconify()
                    self.root.lift()
        except queue.Empty:
            pass
        self.root.after(100, self._drain_events)

    def _save(self) -> None:
        # Update in place: rebuilding the dict silently dropped target_window
        # and click_sequence, which made the app forget its own target window.
        data = self._read_config()
        data.update({
            "name": self.name.get(), "open_date": self.open_date.get(),
            "open_clock": self.open_clock.get(), "mode": self.mode.get(),
            "role": self.role.get(), "fast_mode": self.fast_mode.get(),
            "interval": self.interval.get(),
            "lead": self.lead.get(), "attempt": self.attempt.get(),
        })
        data.setdefault("presets", {})
        CONFIG.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _load(self) -> None:
        if not CONFIG.exists():
            return
        try:
            data = json.loads(CONFIG.read_text(encoding="utf-8"))
            self.name.set(data.get("name", ""))
            self.open_date.set(data.get("open_date", datetime.now().strftime("%Y-%m-%d")))
            legacy = data.get("open_time", "")
            self.open_clock.set(data.get("open_clock", legacy[-8:] if len(legacy) >= 8 else ""))
            self.mode.set(data.get("mode", "我已打开活动详情页"))
            saved_role = str(data.get("role", ROLE_CHOICES[0]))
            self.role.set(normalize_role(saved_role))
            if saved_role != self.role.get():
                self._log(f"身份名称已按小程序更新：「{saved_role}」→「{self.role.get()}」")
                self._save()
            if data.get("fast_mode"):
                self.fast_mode.set(True)
            if data.get("interval"):
                self.interval.set(str(data["interval"]))
            if data.get("lead"):
                self.lead.set(str(data["lead"]))
            if data.get("attempt"):
                self.attempt.set(str(data["attempt"]))
            if any(self._preset_point(key) is not None for key, _ in _FAST_STEPS):
                self.preset_status.set(self._preset_summary())
            else:
                self.preset_status.set("需重新校准位置")
            sequence = self._click_sequence()
            if sequence:
                self.sequence_status.set(f"已学习 {len(sequence)} 次点击，极速直点会按它回放")
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            self.status.set("旧设置无法读取，请重新填写。")

    def _close(self) -> None:
        self.stop_event.set()
        self.root.destroy()


def run_selftest() -> int:
    """Exercise every platform-dependent subsystem and write a report file.

    Run on Windows 11 with: WeChatSignupAssistant.exe --selftest
    The report lands next to the config as selftest_report.txt.
    """
    report: list[str] = []

    def add(label: str, ok: bool, detail: str = "") -> None:
        mark = "OK" if ok else "FAIL"
        line = f"[{mark}] {label}" + (f"：{detail}" if detail else "")
        report.append(line)
        print(line)

    try:
        import platform as _platform
        add("环境信息", True,
            f"platform={sys.platform} machine={_platform.machine()} "
            f"python={sys.version.split()[0]} frozen={getattr(sys, 'frozen', False)}")
    except Exception as exc:  # noqa: BLE001
        add("环境信息", False, str(exc))

    try:
        import pyautogui
        import onnxruntime
        import rapidocr  # noqa: F401
        add("依赖导入", True,
            f"pyautogui={getattr(pyautogui, '__version__', '?')} "
            f"onnxruntime={onnxruntime.__version__}")
    except Exception as exc:  # noqa: BLE001
        add("依赖导入", False, str(exc))

    try:
        sample = "微信活动报名助手自检 " + datetime.now().strftime("%H:%M:%S")
        if sys.platform == "win32":
            ok = bool(_win_set_clipboard(sample))
        else:
            subprocess.run(["pbcopy"], input=sample.encode("utf-8"), check=True)
            ok = True
        add("剪贴板写入", ok, sample if ok else "写入失败")
    except Exception as exc:  # noqa: BLE001
        add("剪贴板写入", False, str(exc))

    try:
        windows = list_screen_windows()
        add("窗口枚举", True, f"共 {len(windows)} 个窗口")
        for window in windows[:6]:
            print("      窗口:", window_label(window))
    except Exception as exc:  # noqa: BLE001
        add("窗口枚举", False, str(exc))

    try:
        windows = list_screen_windows()
        target = pick_wechat_window(windows) or (windows[0] if windows else None)
        if target is None:
            add("窗口抓图", False, "没有可用窗口")
        else:
            image, scale = capture_window_image(target)
            add("窗口抓图", image is not None,
                f"{window_label(target)} 图像={image.size if image else None} 缩放={scale:.2f}")
    except Exception as exc:  # noqa: BLE001
        add("窗口抓图", False, str(exc))

    try:
        from PIL import Image, ImageDraw, ImageFont
        image = Image.new("RGB", (620, 240), "white")
        draw = ImageDraw.Draw(image)
        font_path = ("C:/Windows/Fonts/msyh.ttc" if sys.platform == "win32"
                     else "/System/Library/Fonts/Hiragino Sans GB.ttc")
        try:
            font = ImageFont.truetype(font_path, 42)
        except Exception:
            font = ImageFont.load_default()
        draw.text((40, 85), "立即报名", font=font, fill="black")
        started = time.perf_counter()
        items = _ocr_image(image, fast_det=True)
        elapsed = (time.perf_counter() - started) * 1000
        texts = [item["text"] for item in items]
        found = any("报名" in text for text in texts)
        add("OCR中文识别", found, f"{elapsed:.0f} ms 识别 {len(texts)} 段，样例={texts[:4]}")
    except Exception as exc:  # noqa: BLE001
        add("OCR中文识别", False, str(exc))

    try:
        if sys.platform == "win32":
            add("系统权限", True, "Windows 无需 macOS 的辅助功能/屏幕录制授权")
        else:
            add("系统权限", mac_accessibility_allowed(),
                f"辅助功能={mac_accessibility_allowed()} 屏幕录制={screen_recording_allowed()}")
    except Exception as exc:  # noqa: BLE001
        add("系统权限", False, str(exc))

    report_path = APP_DIR / "selftest_report.txt"
    try:
        report_path.write_text("\n".join(report), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        print(f"报告写入失败: {exc}")

    summary = "\n".join(report)
    print(f"\n完整报告：{report_path}")
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo("微信活动报名助手 自检结果",
                            summary + f"\n\n完整报告：{report_path}")
        root.destroy()
    except Exception:
        pass
    return 0


def main() -> None:
    if sys.platform == "win32":
        # Without this, screenshots and clicks can be scaled on high-DPI screens.
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass
    if "--selftest" in sys.argv:
        run_selftest()
        return
    if "--reset-ui" in sys.argv:
        data = read_config()
        data.pop("ui", None)
        CONFIG.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print("外观设置已清空，将使用默认外观启动。")
    logging.basicConfig(filename=LOG, level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    logging.info("启动：platform=%s python=%s frozen=%s config=%s",
                 sys.platform, sys.version.split()[0], getattr(sys, "frozen", False), CONFIG)
    root = tk.Tk()
    SignupApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
