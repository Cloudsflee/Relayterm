"""Small, testable theme helpers for the Windows RelayTerm launcher."""

from __future__ import annotations

import ctypes
import importlib
import os
from dataclasses import dataclass
from tkinter import font as tkfont
from tkinter import ttk
from typing import Any, Callable


THEME_REGISTRY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
_UNSET = object()


@dataclass(frozen=True)
class ThemeInfo:
    requested: str
    applied: str
    backend: str
    fallback: bool = False


def detect_windows_theme(registry: Any | None = None) -> str:
    """Read the Windows app theme; missing or malformed registry data is light."""
    if registry is None:
        if os.name != "nt":
            return "light"
        try:
            registry = importlib.import_module("winreg")
        except (ImportError, OSError):
            return "light"
    try:
        with registry.OpenKey(registry.HKEY_CURRENT_USER, THEME_REGISTRY_PATH) as key:
            value, _ = registry.QueryValueEx(key, "AppsUseLightTheme")
        return "light" if int(value) else "dark"
    except (AttributeError, OSError, TypeError, ValueError):
        return "light"


def enable_dpi_awareness() -> bool:
    """Enable per-monitor DPI awareness before Tk creates its first window."""
    if os.name != "nt":
        return False
    try:
        user32 = ctypes.windll.user32
        set_context = getattr(user32, "SetProcessDpiAwarenessContext", None)
        if set_context is not None:
            set_context.argtypes = [ctypes.c_void_p]
            set_context.restype = ctypes.c_bool
            if set_context(ctypes.c_void_p(-4)):
                return True
    except (AttributeError, OSError, TypeError):
        pass
    try:
        shcore = ctypes.windll.shcore
        set_awareness = getattr(shcore, "SetProcessDpiAwareness", None)
        if set_awareness is not None:
            set_awareness.argtypes = [ctypes.c_int]
            set_awareness.restype = ctypes.c_long
            if set_awareness(2) == 0:
                return True
    except (AttributeError, OSError, TypeError):
        pass
    try:
        return bool(ctypes.windll.user32.SetProcessDPIAware())
    except (AttributeError, OSError, TypeError):
        return False


def _load_sv_ttk(importer: Callable[[str], Any] | None = None) -> Any | None:
    importer = importer or importlib.import_module
    try:
        return importer("sv_ttk")
    except (ImportError, OSError, ModuleNotFoundError):
        return None


def _configure_styles(style: Any) -> None:
    """Keep spacing and row dimensions stable across sv-ttk and native themes."""
    configurations = {
        "TButton": {"padding": (10, 6), "font": ("Segoe UI", 10)},
        "Icon.TButton": {"padding": (7, 5), "font": ("Segoe Fluent Icons", 11)},
        "Accent.TButton": {"padding": (14, 7), "font": ("Segoe UI", 10, "bold")},
        "Danger.TButton": {"padding": (10, 6), "font": ("Segoe UI", 10)},
        "Treeview": {"rowheight": 34, "font": ("Segoe UI", 10)},
        "Treeview.Heading": {"font": ("Segoe UI", 9, "bold"), "padding": (8, 7)},
        "Status.TLabel": {"font": ("Segoe UI", 9), "padding": (8, 5)},
        "Section.TLabel": {"font": ("Segoe UI", 10, "bold")},
        "Muted.TLabel": {"font": ("Segoe UI", 9)},
        "Search.TEntry": {"font": ("Segoe UI", 10)},
        "Placeholder.TEntry": {"font": ("Segoe UI", 10), "foreground": "#777777"},
    }
    for name, options in configurations.items():
        try:
            style.configure(name, **options)
        except Exception:
            # A third-party theme may reject a platform-specific option.
            continue


def apply_theme(
    root: Any,
    preferred: str | None = None,
    *,
    sv_ttk_module: Any = _UNSET,
    style_factory: Callable[[Any], Any] | None = None,
) -> ThemeInfo:
    """Apply the selected theme and return the backend used for diagnostics/tests."""
    requested = (preferred or detect_windows_theme()).strip().lower()
    if requested not in {"light", "dark"}:
        requested = "light"
    module = _load_sv_ttk() if sv_ttk_module is _UNSET else sv_ttk_module
    backend = "sv-ttk"
    applied = requested
    fallback = False
    if module is not None:
        try:
            module.set_theme(requested)
        except Exception:
            module = None
    if module is None:
        backend = "native"
        fallback = True
        style = style_factory(root) if style_factory else ttk.Style(root)
        try:
            names = tuple(style.theme_names())
            native = "vista" if "vista" in names else ("clam" if "clam" in names else None)
            if native:
                style.theme_use(native)
                applied = native
        except Exception:
            pass
    else:
        style = style_factory(root) if style_factory else ttk.Style(root)
    _configure_styles(style)
    try:
        root.option_add("*Font", "{Segoe UI} 10")
    except Exception:
        pass
    return ThemeInfo(requested, applied, backend, fallback)


def icon_font(root: Any, size: int = 11) -> tuple[str, int]:
    """Choose the Windows fluent icon font, with a portable symbol fallback."""
    candidates = ("Segoe Fluent Icons", "Segoe MDL2 Assets", "Segoe UI Symbol")
    try:
        families = set(tkfont.families(root))
    except Exception:
        families = set()
    return (next((item for item in candidates if item in families), "Segoe UI Symbol"), size)


_GLYPHS = {
    "add": "\ue710",
    "edit": "\ue70f",
    "delete": "\ue74d",
    "stop": "\ue71a",
    "remote": "\ue774",
    "open": "\ue8e5",
    "folder": "\ue8b7",
}


def glyph(name: str) -> str:
    return _GLYPHS.get(name, "")


class Tooltip:
    """Minimal hover tooltip for compact icon-only controls."""

    def __init__(self, widget: Any, text: str, delay: int = 500) -> None:
        self.widget = widget
        self.text = text
        self.delay = delay
        self._after_id: Any = None
        self._window: Any = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event: Any = None) -> None:
        self._cancel()
        try:
            self._after_id = self.widget.after(self.delay, self._show)
        except Exception:
            self._after_id = None

    def _cancel(self) -> None:
        if self._after_id is not None:
            try:
                self.widget.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None

    def _show(self) -> None:
        self._after_id = None
        if self._window is not None or not self.widget.winfo_exists():
            return
        try:
            x = self.widget.winfo_rootx() + 8
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
            import tkinter as tk
            tip = tk.Toplevel(self.widget)
            self._window = tip
            tip.wm_overrideredirect(True)
            tip.wm_geometry(f"+{x}+{y}")
            tk.Label(tip, text=self.text, padx=7, pady=4, relief="solid", borderwidth=1).pack()
        except Exception:
            self._window = None

    def _hide(self, _event: Any = None) -> None:
        self._cancel()
        if self._window is not None:
            try:
                self._window.destroy()
            except Exception:
                pass
            self._window = None


__all__ = [
    "ThemeInfo", "Tooltip", "apply_theme", "detect_windows_theme", "enable_dpi_awareness",
    "glyph", "icon_font",
]
