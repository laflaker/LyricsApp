import sys
import asyncio
import threading
import requests
import re
import json
import os
import time
import math
import bisect
import random
import logging
import hashlib
import datetime

if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

from PyQt6.QtWidgets import (QApplication, QLabel, QVBoxLayout, QHBoxLayout,
                             QPushButton, QFontComboBox, QSpinBox, QColorDialog,
                             QDoubleSpinBox, QFormLayout, QDialog, QWidget, QTabWidget,
                             QCheckBox, QGroupBox, QLineEdit, QSlider, QToolButton,
                             QSystemTrayIcon, QMenu, QComboBox, QPlainTextEdit,
                             QListWidget, QScrollArea, QFrame, QGraphicsScene,
                             QGraphicsPixmapItem, QGraphicsBlurEffect, QGridLayout)
from PyQt6.QtCore import (Qt, QTimer, pyqtSignal, QObject, QPoint, QPointF, QRect, QRectF,
                          QEvent)
from PyQt6.QtGui import (QFont, QColor, QCursor, QPainter, QLinearGradient, QRadialGradient,
                         QConicalGradient, QPen, QPainterPath, QFontMetrics, QIcon, QAction,
                         QPixmap, QShortcut, QKeySequence, QTextCursor, QBrush, QPalette)

from lyrics_utils import (clean_title, clean_artist, first_artist, unique, parse_lrc,
                          pick_lrclib, clean_genius_lines, spread_lines, ease_out_cubic,
                          artist_match)

try:
    from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as MediaManager
except ImportError:
    print("Ошибка: Не установлены библиотеки winrt.")
    sys.exit()

log = logging.getLogger("lyricsapp")

GENIUS_TOKEN = ""

LRCLIB_URL = "https://lrclib.net/api"
ITUNES_URL = "https://itunes.apple.com/search"
DEEZER_URL = "https://api.deezer.com/search"
HTTP_HEADERS = {"User-Agent": "LyricsApp/1.1 (personal desktop lyrics overlay)"}
COVERS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache", "covers")
LYRICS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache", "lyrics")
COVER_MAX_BYTES = 6 * 1024 * 1024
STARTUP_DIR = os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows",
                           "Start Menu", "Programs", "Startup")

try:
    import socks
    SOCKS_AVAILABLE = True
except ImportError:
    SOCKS_AVAILABLE = False

_DIRECT_HTTP = requests.Session()
_DIRECT_HTTP.trust_env = False
_PROXY_BYPASSED = False
_HTTP_ERRORS_LOGGED = set()


def log_http_error(what, exc):
    """Log a network problem once per kind instead of one traceback per request."""
    key = (what, type(exc).__name__, str(exc)[:120])
    if key in _HTTP_ERRORS_LOGGED:
        log.debug("%s: %s", what, exc)
        return
    _HTTP_ERRORS_LOGGED.add(key)
    log.warning("%s: %s", what, exc)
    if isinstance(exc, requests.exceptions.InvalidSchema) and not SOCKS_AVAILABLE:
        log.warning("Прокси настраивает SOCKS, а пакет PySocks не установлен — "
                    "запросы идут напрямую. Чтобы использовать прокси: "
                    "pip install \"requests[socks]\"")


def http_get(url, **kwargs):
    """GET that also works when the system proxy cannot be used (SOCKS without PySocks)."""
    global _PROXY_BYPASSED
    kwargs.setdefault("headers", HTTP_HEADERS)
    kwargs.setdefault("timeout", 8)
    if _PROXY_BYPASSED:
        return _DIRECT_HTTP.get(url, proxies={"http": None, "https": None}, **kwargs)
    try:
        return requests.get(url, **kwargs)
    except (requests.exceptions.InvalidSchema, requests.exceptions.ProxyError) as exc:
        try:
            res = _DIRECT_HTTP.get(url, proxies={"http": None, "https": None}, **kwargs)
        except Exception:
            raise exc
        _PROXY_BYPASSED = True
        log.warning("Системный прокси недоступен (%s) — запросы идут напрямую", exc)
        return res

TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    "і": "i", "ї": "i", "є": "e", "ґ": "g",
}


def latinize(text):
    """'Баста' -> 'basta'."""
    letters = [TRANSLIT.get(ch, ch) for ch in (text or "").lower()]
    return re.sub(r"[^a-z0-9]+", "", "".join(letters))


def lyrics_cache_path(artist, title):
    key = hashlib.sha1(f"{artist}|{title}".strip().lower().encode("utf-8")).hexdigest()
    return os.path.join(LYRICS_DIR, key + ".lrc")


def save_local_lyrics(artist, title, text):
    if not (artist or title) or not (text or "").strip():
        return False
    path = lyrics_cache_path(artist, title)
    try:
        os.makedirs(LYRICS_DIR, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text.strip() + "\n")
        os.replace(tmp, path)
        return True
    except OSError:
        log.exception("Не удалось сохранить текст в %s", path)
        return False


def load_local_lyrics(artist, title):
    path = lyrics_cache_path(artist, title)
    try:
        if os.path.exists(path) and os.path.getsize(path) > 0:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
    except OSError:
        log.debug("Не удалось прочитать локальный текст %s", path, exc_info=True)
    return None


def format_lrc_time(seconds):
    seconds = max(0.0, float(seconds))
    minutes = int(seconds // 60)
    return f"[{minutes:02d}:{seconds - minutes * 60:05.2f}]"


def lines_to_lrc(lines, times):
    out = []
    for line, t in zip(lines, times):
        out.append(format_lrc_time(t) + line)
    return "\n".join(out)


APP_DIR = os.path.dirname(os.path.abspath(__file__))


def resource_path(*parts):
    """File shipped with the app; also works inside a PyInstaller bundle."""
    return os.path.join(getattr(sys, "_MEIPASS", APP_DIR), *parts)


APP_ICON_PATH = resource_path("assets", "LyricsApp.ico")
APP_ICON_PNG = resource_path("assets", "LyricsApp.png")


def app_icon():
    icon = QIcon(APP_ICON_PATH)
    if icon.isNull():
        icon = QIcon(APP_ICON_PNG)
    return icon


FONT_CACHE = {}
METRICS_CACHE = {}


def cached_font(family, size, weight=QFont.Weight.Bold):
    """Reuse QFont objects: font matching gets slow when many fonts are installed."""
    key = (str(family), int(size), int(weight))
    font = FONT_CACHE.get(key)
    if font is None:
        font = QFont(family, int(size), QFont.Weight(weight))
        if len(FONT_CACHE) > 256:
            FONT_CACHE.clear()
        FONT_CACHE[key] = font
    return font


def font_metrics(font):
    """Reuse QFontMetrics: every construction resolves the font engine again."""
    key = (font.family(), font.pointSizeF(), font.weight(), font.italic(),
           font.underline(), font.strikeOut(), font.letterSpacing(),
           font.capitalization(), font.stretch())
    metrics = METRICS_CACHE.get(key)
    if metrics is None:
        metrics = QFontMetrics(font)
        if len(METRICS_CACHE) > 256:
            METRICS_CACHE.clear()
        METRICS_CACHE[key] = metrics
    return metrics


def _pythonw_path():
    """pythonw.exe next to the interpreter (no console window); plain python as a fallback."""
    candidate = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return candidate if os.path.exists(candidate) else sys.executable


def vbs_launcher_text():
    script = os.path.join(APP_DIR, "start.py")
    pythonw = _pythonw_path()
    return ('Set sh = CreateObject("WScript.Shell")\r\n'
            f'sh.CurrentDirectory = "{APP_DIR}"\r\n'
            f'sh.Run """{pythonw}"" ""{script}""", 0, False\r\n')


def autostart_path():
    return os.path.join(STARTUP_DIR, "LyricsApp.vbs")


def set_autostart(enabled):
    path = autostart_path()
    try:
        if enabled:
            os.makedirs(STARTUP_DIR, exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(vbs_launcher_text())
        elif os.path.exists(path):
            os.remove(path)
    except OSError:
        log.exception("Не удалось изменить автозапуск (%s)", path)
        return False
    return True


def is_autostart_enabled():
    return os.path.exists(autostart_path())


PLAYBACK_PLAYING = 4
IDX_UNSET = -999
ANIM_DURATION = 0.45
WORD_POP_TIME = 0.22
GLOW_FRAME_TIME = 0.08
STATUS_FRAME_TIME = 0.05
HINT_VISIBLE_TIME = 35.0
HINT_FRAME_TIME = 0.06
SIDES_POP_TIME = 0.32
SIDES_HOLD_TIME = 1.00
SIDES_FADE_TIME = 0.40

SETTINGS_FILE = "lyrics_settings.json"
DEFAULT_SETTINGS = {
    "font_family": "Segoe UI", "font_size": 36, "text_color": "#FFFFFF",
    "text_opacity": 100, "future_color": "#555555", "hide_future": False,
    "outline_color": "#000000", "outline_width": 2, "use_gradient": True,
    "gradient_color2": "#89b4fa", "gradient_colors": None,
    "bg_color": "#000000", "bg_opacity": 0,
    "offset": 0.0, "sync_margin": 0.8, "window_width": 1800,
    "show_unsynced_hint": True,
    "last_x": None, "last_y": None, "genius_token": "",
    "anim_glow": False, "glow_color": "#89b4fa", "glow_size": 3, "glow_speed": 5,
    "anim_pulse": False, "anim_wave": False, "anim_flicker": False,
    "mode": "classic", "lines_visible": 3, "text_width": 80,
    "cover_online": True, "vinyl_speed": 4,
    "theme": "custom", "anim_mode": "line", "slide_from": "bottom", "slide_dist": 28,
    "words_per_row": 1, "words_visible": 4,
    "vinyl_size": 280, "vinyl_gap": 24, "vinyl_caption": True, "vinyl_shift": 0,
    "vinyl_style": "neon", "vinyl_accent": "#89b4fa", "vinyl_glow": 60,
    "vinyl_rim": True,
    "autostart": False
}

def load_settings():
    settings = DEFAULT_SETTINGS.copy()
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
                if "show_unsynced_hint" not in saved and "approx_sync" in saved:
                    saved["show_unsynced_hint"] = saved["approx_sync"]
                if saved.get("anim_mode") == "jump":
                    saved["anim_mode"] = "sides"
                for key in DEFAULT_SETTINGS.keys():
                    if key in saved: settings[key] = saved[key]
        except Exception:
            log.exception("Не удалось прочитать %s", SETTINGS_FILE)
    return settings

def save_settings(settings):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=4)

SETTINGS = load_settings()
if (not isinstance(SETTINGS.get("gradient_colors"), list)
        or len(SETTINGS["gradient_colors"]) < 2):
    SETTINGS["gradient_colors"] = [SETTINGS.get("text_color", "#FFFFFF"),
                                   SETTINGS.get("gradient_color2", "#89b4fa")]
if not GENIUS_TOKEN: GENIUS_TOKEN = SETTINGS.get("genius_token", "")


UI_THEME_COLORS = {
    "custom": dict(bg="#1E1E2E", panel="#2A2C3E", field="#262838", border="#3A3D52", text="#DCE3F2",
                   muted="#A6ADC4", accent="#89B4FA", accent_hover="#A9C4FF",
                   dim="rgba(137, 180, 250, 26)", tab_sel="rgba(137, 180, 250, 34)",
                   ok="#A6E3A1", bad="#F38BA8", extra="#F9E2AF", button_text="#11111B"),
    "dark": dict(bg="#111319", panel="#1A1E27", field="#171B24", border="#2B3140", text="#E8ECF4",
                 muted="#9EA7BC", accent="#7AA2F7", accent_hover="#9BB8FF",
                 dim="rgba(122, 162, 247, 26)", tab_sel="rgba(122, 162, 247, 34)",
                 ok="#9ECE6A", bad="#F7768E", extra="#E0AF68", button_text="#0B0D12"),
    "light": dict(bg="#FFFFFF", panel="#F4F6FB", field="#F7F9FD", border="#DCE2EC", text="#15181F",
                  muted="#6F7787", accent="#3B82F6", accent_hover="#2563EB",
                  dim="rgba(59, 130, 246, 18)", tab_sel="rgba(59, 130, 246, 26)",
                  ok="#15803D", bad="#DC2626", extra="#B45309", button_text="#FFFFFF"),
    "transparent": dict(bg="rgba(13, 15, 23, 198)", panel="rgba(255, 255, 255, 22)",
                        field="#20242F",
                        border="rgba(255, 255, 255, 50)", text="#EEF2FA",
                        muted="#C0C7D8", accent="#89B4FA", accent_hover="#A9C4FF",
                        dim="rgba(255, 255, 255, 18)", tab_sel="rgba(137, 180, 250, 48)",
                        ok="#A6E3A1", bad="#F38BA8", extra="#F9E2AF", button_text="#11111B"),
}

UI_STYLE_TEMPLATE = """
QDialog {{ color: {text}; font-family: 'Segoe UI'; }}
QColorDialog {{ background-color: {bg}; color: {text}; }}
#ui_header {{ background: transparent; }}
#ui_title {{ color: {text}; font-size: 15px; font-weight: 600; background: transparent; }}
#ui_close {{ background: transparent; color: {text}; border: 1px solid transparent;
            border-radius: 15px; font-size: 17px; font-weight: 600; padding: 0; }}
#ui_close:hover {{ background: {dim}; color: {bad}; border: 1px solid {border}; }}
#ui_close:pressed {{ background: {bad}; color: #FFFFFF; }}

QTabWidget, QTabBar {{ background: transparent; }}
QTabWidget::pane {{ border: none; background: transparent; padding-top: 4px; }}
QTabBar::tab {{ background: transparent; color: {muted}; padding: 7px 14px; margin: 0 2px 2px 2px; font-size: 13px; border-radius: 9px; }}
QTabBar::tab:hover {{ background: {dim}; color: {text}; }}
QTabBar::tab:selected {{ background: {tab_sel}; color: {accent}; font-weight: 600; }}

QLabel {{ color: {text}; font-size: 13px; background: transparent; }}
#ui_hint {{ color: {muted}; font-size: 12px; }}
#ui_next {{ color: {extra}; font-size: 15px; font-weight: 600; }}
#ui_ok {{ color: {ok}; }}
#ui_err {{ color: {bad}; }}
#ui_track {{ font-size: 14px; font-weight: 600; }}

QSpinBox, QDoubleSpinBox, QFontComboBox, QLineEdit, QComboBox {{ background-color: {panel}; color: {text}; border: 1px solid {border}; border-radius: 9px; padding: 5px 9px; font-size: 13px; min-height: 22px; }}
QComboBox, QFontComboBox {{ padding-right: 26px; }}
QComboBox::drop-down, QFontComboBox::drop-down {{ subcontrol-origin: padding; subcontrol-position: center right; width: 22px; border: none; background: transparent; }}
QSpinBox:focus, QDoubleSpinBox:focus, QLineEdit:focus, QComboBox:focus, QFontComboBox:focus {{ border: 1px solid {accent}; }}
QComboBox QAbstractItemView {{ background-color: {bg}; color: {text}; border: 1px solid {border}; selection-background-color: {dim}; outline: none; }}

QPushButton {{ background-color: {accent}; color: {button_text}; border: 1px solid transparent; border-radius: 9px; padding: 7px 14px; font-weight: 600; font-size: 13px; }}
QPushButton:hover {{ background-color: {accent_hover}; }}
QPushButton#btn_cancel {{ background-color: transparent; color: {text}; border: 1px solid {border}; }}
QPushButton#btn_cancel:hover {{ background-color: {dim}; }}
QPushButton#btn_exit {{ background-color: {bad}; color: #FFFFFF; border: 1px solid transparent; }}
QPushButton#btn_exit:hover {{ background-color: {bad}; }}

QCheckBox {{ color: {text}; font-size: 13px; spacing: 8px; background: transparent; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 5px; border: 1px solid {border}; background: {panel}; }}
QCheckBox::indicator:hover {{ border: 1px solid {accent}; }}
QCheckBox::indicator:checked {{ background: {accent}; border: 1px solid {accent}; }}

QGroupBox {{ color: {accent}; font-weight: 600; font-size: 13px; border: 1px solid {border}; border-radius: 12px; margin-top: 14px; padding: 8px 10px 8px 10px; background: {dim}; }}
QGroupBox::title {{ subcontrol-origin: margin; subcontrol-position: top left; left: 12px; padding: 0 5px; color: {accent}; background: transparent; }}

QToolButton {{ background-color: {panel}; color: {text}; border: 1px solid {border}; border-radius: 8px; font-weight: bold; font-size: 14px; }}
QToolButton:hover {{ border: 1px solid {accent}; color: {accent}; }}

QSlider::groove:horizontal {{ border: none; height: 5px; background: {panel}; margin: 6px 0; border-radius: 3px; }}
QSlider::sub-page:horizontal {{ background: {accent}; border-radius: 3px; }}
QSlider::handle:horizontal {{ background: {text}; border: none; width: 14px; margin: -5px 0; border-radius: 7px; }}

QPlainTextEdit {{ background-color: {field}; color: {text}; border: 1px solid {border}; border-radius: 10px; padding: 8px; font-size: 13px; selection-background-color: {accent}; selection-color: {button_text}; }}
QScrollArea {{ background: transparent; border: none; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QListWidget {{ background-color: {field}; color: {text}; border: 1px solid {border}; border-radius: 10px; font-size: 13px; padding: 4px; }}
QListWidget::item {{ padding: 4px 6px; border-radius: 6px; }}
QListWidget::item:selected {{ background-color: {dim}; color: {text}; }}

QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {border}; border-radius: 5px; min-height: 24px; }}
QScrollBar::handle:vertical:hover {{ background: {accent}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ background: none; height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}

QMenu {{ background-color: {bg}; color: {text}; border: 1px solid {border}; border-radius: 10px; padding: 6px; }}
QMenu::item {{ padding: 6px 18px; border-radius: 6px; }}
QMenu::item:selected {{ background-color: {dim}; }}
QToolTip {{ background-color: {bg}; color: {text}; border: 1px solid {border}; border-radius: 6px; padding: 4px 7px; }}
"""


def ui_colors(name=None):
    name = name or SETTINGS.get("theme", "custom")
    return UI_THEME_COLORS.get(name) or UI_THEME_COLORS["custom"]


def ui_stylesheet(name):
    return UI_STYLE_TEMPLATE.format(**ui_colors(name))


def combo_popup_stylesheet(name):
    """The dropdown list is its own popup window: style it explicitly, not only through the app."""
    return (
        "QComboBoxPrivateContainer {{ background-color: {bg}; border: 1px solid {border};"
        " border-radius: 10px; }}"
        "QAbstractItemView {{ background-color: {bg}; color: {text}; border: 1px solid {border};"
        " border-radius: 10px; outline: none; padding: 3px;"
        " selection-background-color: {tab_sel}; selection-color: {accent}; }}"
        "QAbstractItemView::item {{ padding: 2px 8px; min-height: 22px; border-radius: 6px; color: {text}; }}"
        "QAbstractItemView::item:selected {{ background-color: {tab_sel}; color: {accent}; }}"
    ).format(**ui_colors(name))


def color_dialog_stylesheet(name):
    """Explicit styling for the colour picker: its labels and OK/Cancel must stay readable."""
    return (
        "QColorDialog {{ background-color: {bg}; }}"
        "QColorDialog QLabel {{ color: {text}; background: transparent; }}"
        "QColorDialog QLineEdit {{ background-color: {panel}; color: {text};"
        " border: 1px solid {border}; border-radius: 8px; padding: 4px 8px; }}"
        "QColorDialog QSpinBox {{ background-color: {panel}; color: {text};"
        " border: 1px solid {border}; border-radius: 8px; padding: 3px 6px; }}"
        "QColorDialog QPushButton {{ background-color: {accent}; color: {button_text};"
        " border: 1px solid transparent; border-radius: 8px; padding: 6px 16px;"
        " font-weight: 600; min-width: 64px; }}"
        "QColorDialog QPushButton:hover {{ background-color: {accent_hover}; }}"
        "QColorDialog QPushButton:disabled {{ background-color: {dim}; color: {muted}; }}"
    ).format(**ui_colors(name))


def scroll_page(widget):
    """Wrap a tab page in a transparent scroll area, so tall pages never squash the controls."""
    area = QScrollArea()
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setWidgetResizable(True)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    area.viewport().setAutoFillBackground(False)
    area.setWidget(widget)
    return area


def opaque_theme_color(value):
    """Flatten a translucent theme colour onto black: native windows are not translucent."""
    color = theme_qcolor(value)
    if color.alpha() < 255:
        ratio = color.alphaF()
        color = QColor(int(color.red() * ratio), int(color.green() * ratio),
                       int(color.blue() * ratio), 255)
    return color


def color_dialog_palette(name):
    """The picker's native children (spin arrows, colour wells) follow the UI palette.

    Without this they kept the system palette: black panels around the wheel in the dark
    theme and system-grey spin buttons in the light one.
    """
    colors = ui_colors(name)
    panel = opaque_theme_color(colors["panel"])
    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window, opaque_theme_color(colors["bg"]))
    pal.setColor(QPalette.ColorRole.WindowText, QColor(colors["text"]))
    pal.setColor(QPalette.ColorRole.Base, panel)
    pal.setColor(QPalette.ColorRole.AlternateBase, opaque_theme_color(colors["bg"]))
    pal.setColor(QPalette.ColorRole.Text, QColor(colors["text"]))
    pal.setColor(QPalette.ColorRole.Button, panel)
    pal.setColor(QPalette.ColorRole.ButtonText, QColor(colors["text"]))
    pal.setColor(QPalette.ColorRole.Highlight, QColor(colors["accent"]))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor(colors["button_text"]))
    pal.setColor(QPalette.ColorRole.ToolTipBase, panel)
    pal.setColor(QPalette.ColorRole.ToolTipText, QColor(colors["text"]))
    return pal


def theme_qcolor(value):
    """Parse '#RRGGBB' or 'rgba(r, g, b, a)' from the theme palette into a QColor."""
    value = str(value).strip()
    match = re.match(r"rgba?\(([^)]+)\)", value)
    if match:
        parts = [p.strip() for p in match.group(1).split(",")]
        red, green, blue = (int(float(parts[0])), int(float(parts[1])), int(float(parts[2])))
        alpha = int(float(parts[3])) if len(parts) > 3 else 255
        return QColor(red, green, blue, alpha)
    return QColor(value)


def gradient_stops():
    """Colour stops of the text gradient; always at least two, up to six."""
    stops = SETTINGS.get("gradient_colors")
    if not isinstance(stops, (list, tuple)):
        stops = [SETTINGS.get("text_color", "#FFFFFF"),
                 SETTINGS.get("gradient_color2", "#89b4fa")]
    colors = []
    for value in stops:
        color = QColor(str(value))
        if color.isValid():
            colors.append(color.name())
    if len(colors) < 2:
        colors = ["#FFFFFF", "#89B4FA"]
    return colors[:6]


def ease_out_back(progress):
    """Overshooting ease: 0 -> 1 with a small bounce past 1 before settling."""
    progress = max(0.0, min(1.0, progress))
    u = progress - 1.0
    return 1.0 + 2.4 * u * u * u + 1.4 * u * u


def make_text_gradient(x0, y0, x1, y1):
    """Multi-stop linear gradient for the lyrics fill."""
    gradient = QLinearGradient(x0, y0, x1, y1)
    stops = gradient_stops()
    last = len(stops) - 1
    for index, value in enumerate(stops):
        gradient.setColorAt(index / last, QColor(value))
    return gradient


def apply_combo_popup_theme(name):
    """Dropdown lists are separate popup windows: style every combo's view explicitly."""
    qss = combo_popup_stylesheet(name)
    for widget in QApplication.allWidgets():
        if isinstance(widget, QComboBox):
            view = widget.view()
            view.setStyleSheet(qss)
            view.window().setStyleSheet(qss)


def apply_ui_theme(name):
    """Recolour the dialogs; the card itself is painted in UiDialog.paintEvent."""
    app = QApplication.instance()
    if app is None:
        return
    app.setStyleSheet(ui_stylesheet(name))
    apply_combo_popup_theme(name)
    for widget in app.topLevelWidgets():
        if isinstance(widget, QDialog):
            widget.update()


class MediaTracker(QObject):
    track_changed = pyqtSignal(str, str, float, float)
    lyrics_loaded = pyqtSignal(list, bool)
    state_changed = pyqtSignal(bool)
    position_update = pyqtSignal(float)
    cover_loaded = pyqtSignal(object)

    POS_DRIFT = 2.0
    RATE_WINDOW = 3.0
    RATE_MIN, RATE_MAX = 0.55, 1.45
    MAX_EXTRAPOLATION = 12.0
    TRACK_CONFIRM = 1.0
    TRUST_LOG_GAP = 30.0

    def __init__(self):
        super().__init__()
        self.current_key = None
        self.is_playing = False
        self._request_id = 0
        self._samples = []
        self._pos_ref = None
        self._last_reported = None
        self._last_reported_at = 0.0
        self._pos_trusted = True
        self._trust_logged_at = 0.0
        self._pending_key = None
        self._pending_since = 0.0
        self._no_session_logged = False
        self._cover_tasks = set()

    def start(self):
        threading.Thread(target=self._run_asyncio_loop, daemon=True).start()

    def _run_asyncio_loop(self):
        asyncio.run(self._media_loop())


    @staticmethod
    def _same_app(first, second):
        try:
            return first.source_app_user_model_id == second.source_app_user_model_id
        except Exception:
            return first is second

    @classmethod
    def _pick_session(cls, manager):
        """Prefer the session that is actually playing (browsers often steal the "current" slot)."""
        try:
            current = manager.get_current_session()
        except Exception:
            current = None
        try:
            sessions = list(manager.get_sessions())
        except Exception:
            sessions = []
        playing = []
        for s in sessions:
            try:
                if s.get_playback_info().playback_status == PLAYBACK_PLAYING:
                    playing.append(s)
            except Exception:
                continue
        if current is not None and any(cls._same_app(current, s) for s in playing):
            return current
        if playing:
            return playing[0]
        return current

    @staticmethod
    def _clean_metadata(title, artist):
        """Some players put the whole "Artist - Title" string into one of the fields."""
        title = (title or "").strip()
        artist = (artist or "").strip()
        if not artist:
            for sep in (" - ", " \u2013 ", " \u2014 "):
                if sep in title:
                    head, tail = title.split(sep, 1)
                    if head.strip() and tail.strip():
                        artist, title = head.strip(), tail.strip()
                    break
        return artist, title

    @classmethod
    def _timeline_position(cls, timeline, playing=True):
        """Position now, not the position at the last snapshot.

        Browsers publish the timeline a few seconds late (Chrome updates it about once a
        second, sometimes less), so the raw value is behind the music. `last_updated_time`
        tells exactly how old the snapshot is and the lag can be added back. Players that
        do not update that timestamp properly are left alone: the local clock handles them.
        """
        try:
            pos = max(0.0, timeline.position.total_seconds())
        except Exception:
            return 0.0
        if not playing:
            return pos
        try:
            updated = timeline.last_updated_time
            if updated is None:
                return pos
            now = datetime.datetime.now(datetime.timezone.utc)
            lag = (now - updated).total_seconds()
        except Exception:
            return pos
        if 0.0 <= lag <= cls.MAX_EXTRAPOLATION:
            pos += lag
        return pos

    @staticmethod
    def _timeline_duration(timeline):
        try:
            dur = timeline.end_time.total_seconds() - timeline.start_time.total_seconds()
            return dur if dur > 0 else 0.0
        except Exception:
            return 0.0

    def _reset_position_tracking(self):
        self._samples = []
        self._pos_ref = None
        self._last_reported = None
        self._last_reported_at = 0.0
        self._pos_trusted = True

    def _update_position_trust(self, pos, now):
        """Check that the player's clock advances roughly with the real time.

        A frozen / lagging timeline must not drive the lyrics: otherwise the text
        either freezes or runs backwards (seen with AIMP)."""
        if self._samples and abs(pos - self._samples[-1][1]) > self.POS_DRIFT:
            self._samples = []
        self._samples.append((now, pos))
        while len(self._samples) > 2 and now - self._samples[0][0] > self.RATE_WINDOW * 2:
            self._samples.pop(0)
        if len(self._samples) < 5:
            return
        t0, p0 = self._samples[0]
        dt = now - t0
        if dt < self.RATE_WINDOW * 0.6:
            return
        rate = (pos - p0) / dt
        trusted = self.RATE_MIN <= rate <= self.RATE_MAX
        if trusted != self._pos_trusted:
            self._pos_trusted = trusted
            self._pos_ref = (now, pos)
            if now - self._trust_logged_at >= self.TRUST_LOG_GAP:
                self._trust_logged_at = now
                if trusted:
                    log.info("SMTC: позиция снова достоверна")
                else:
                    log.info("SMTC: позиция недостоверна (%.2f× реального времени) — "
                             "синхронизация по локальным часам", rate)

    def _position_jump(self, pos, now):
        """Return pos if it means a seek / restart, otherwise None.

        A seek must be detected even when the player's clock is untrusted (a frozen
        timeline): otherwise switching a track leaves the local clock running from the
        previous song's position (e.g. the lyrics stay at 0:21 while the new track is at
        0:45). In that case only a *change* between two snapshots is trusted, so a clock
        that simply stands still does not drag the text back.
        """
        first = self._pos_ref is None
        if first:
            self._pos_ref = (now, pos)
        jump = None
        if self._pos_trusted:
            t0, p0 = self._pos_ref
            expected = p0 + (now - t0)
            if abs(pos - expected) > self.POS_DRIFT:
                self._pos_ref = (now, pos)
                jump = pos
        elif self._last_reported is not None:
            delta = pos - self._last_reported
            elapsed = max(0.0, now - self._last_reported_at)
            if abs(delta) > self.POS_DRIFT and (delta < 0 or delta > elapsed + self.POS_DRIFT):
                self._pos_ref = (now, pos)
                jump = pos
        self._last_reported = pos
        self._last_reported_at = now
        return pos if first else jump

    async def _media_loop(self):
        manager = None
        while True:
            try:
                if manager is None:
                    manager = await MediaManager.request_async()
                session = self._pick_session(manager)
                if session:
                    self._no_session_logged = False
                    playback_info = session.get_playback_info()
                    is_playing = (playback_info.playback_status == PLAYBACK_PLAYING)
                    if is_playing != self.is_playing:
                        self.is_playing = is_playing
                        if not is_playing:
                            self._samples = []
                            self._pos_ref = None
                        self.state_changed.emit(is_playing)

                    info = await session.try_get_media_properties_async()
                    timeline = session.get_timeline_properties()
                    artist, title = self._clean_metadata(
                        info.title, info.artist or getattr(info, "album_artist", ""))
                    now = time.monotonic()
                    pos = self._timeline_position(timeline, is_playing)
                    duration = self._timeline_duration(timeline)
                    if duration:
                        pos = min(pos, duration)

                    key = (artist.lower(), title.lower())
                    switched = False
                    if title and key != self.current_key:
                        confirmed = (self.current_key is None
                                     or (self._pending_key == key
                                         and now - self._pending_since >= self.TRACK_CONFIRM))
                        if confirmed:
                            rid = self._start_track(session, artist, title, pos, duration)
                            self._pending_key = None
                            switched = True
                            self.cover_loaded.emit(None)
                            task = asyncio.create_task(
                                self._cover_task(rid, info.thumbnail, artist, title))
                            self._cover_tasks.add(task)
                            task.add_done_callback(self._cover_tasks.discard)
                        elif self._pending_key != key:
                            self._pending_key = key
                            self._pending_since = now
                    elif self._pending_key is not None:
                        self._pending_key = None

                    if switched:
                        if is_playing:
                            self._update_position_trust(pos, now)
                    elif not self._pending_key and title and is_playing:
                        self._update_position_trust(pos, now)
                        jump = self._position_jump(pos, now)
                        if jump is not None:
                            self.position_update.emit(jump)
                else:
                    if not self._no_session_logged:
                        log.info("SMTC: нет активных медиа-сессий — плеер не отдаёт данные в Windows")
                        self._no_session_logged = True
                    if self.is_playing:
                        self.is_playing = False
                        self.state_changed.emit(False)
            except Exception:
                log.exception("Ошибка в цикле SMTC")
                manager = None
                self.current_key = None
                self._pending_key = None
            await asyncio.sleep(0.25)

    def _start_track(self, session, artist, title, pos, duration):
        self.current_key = (artist.lower(), title.lower())
        self._reset_position_tracking()
        rid = self._new_request()
        self.track_changed.emit(artist, title, pos, duration)
        self.download_lyrics(rid, artist, title, duration)
        try:
            app_id = session.source_app_user_model_id or "?"
        except Exception:
            app_id = "?"
        log.info("Трек: %r — %r (источник: %s, длительность: %.0fs)",
                 artist, title, app_id, duration)
        return rid


    @staticmethod
    async def _read_thumbnail(ref):
        """Album art as reported by the player (AIMP reads it from the file tags)."""
        if ref is None:
            return None
        try:
            from winrt.windows.storage.streams import DataReader
        except ImportError:
            log.debug("winrt-Windows.Storage.Streams не установлен — обложки из SMTC недоступны")
            return None
        try:
            stream = await ref.open_read_async()
            size = int(stream.size)
            if size <= 0 or size > COVER_MAX_BYTES:
                return None
            reader = DataReader(stream)
            await reader.load_async(size)
            return bytes(reader.read_buffer(size))
        except Exception:
            log.debug("Не удалось прочитать обложку из SMTC", exc_info=True)
            return None

    async def _cover_task(self, rid, thumb_ref, artist, title):
        data = await self._read_thumbnail(thumb_ref)
        if self._is_stale(rid):
            return
        if data:
            self.cover_loaded.emit(data)
            return
        if not SETTINGS.get("cover_online", True):
            self.cover_loaded.emit(None)
            return
        data = await asyncio.to_thread(self._find_online_cover, artist, title)
        if not self._is_stale(rid):
            self.cover_loaded.emit(data)

    @staticmethod
    def _cover_cache_path(artist, title):
        key = hashlib.sha1(f"{artist}|{title}".lower().encode("utf-8")).hexdigest()
        return os.path.join(COVERS_DIR, key + ".img")

    def _find_online_cover(self, artist, title):
        path = self._cover_cache_path(artist, title)
        try:
            if os.path.exists(path) and os.path.getsize(path) > 0:
                with open(path, "rb") as f:
                    return f.read()
        except OSError:
            pass
        data = self._itunes_cover(artist, title) or self._deezer_cover(artist, title)
        if data:
            try:
                os.makedirs(COVERS_DIR, exist_ok=True)
                with open(path, "wb") as f:
                    f.write(data)
            except OSError:
                log.debug("Не удалось сохранить обложку в кэш", exc_info=True)
        return data

    @staticmethod
    def _names_match(a, b):
        x, y = (a or "").strip().lower(), (b or "").strip().lower()
        if not x or not y:
            return False
        if x in y or y in x or artist_match(x, y):
            return True
        lx, ly = latinize(x), latinize(y)
        return bool(lx) and bool(ly) and (lx in ly or ly in lx)

    @staticmethod
    def _download_image(url):
        try:
            res = http_get(url, timeout=8)
            if res.status_code == 200 and len(res.content) > 1000:
                return res.content
        except Exception:
            log.debug("Не удалось скачать обложку %s", url, exc_info=True)
        return None

    def _itunes_cover(self, artist, title):
        try:
            res = http_get(ITUNES_URL, params={
                "term": f"{artist} {clean_title(title)}".strip(),
                "entity": "song", "media": "music", "limit": 5,
            }, timeout=8)
            if res.status_code != 200:
                return None
            for item in res.json().get("results", []):
                if not self._names_match(artist, item.get("artistName")):
                    continue
                if not self._names_match(clean_title(title), clean_title(item.get("trackName", ""))):
                    continue
                url = (item.get("artworkUrl100") or "").replace("100x100bb", "600x600bb")
                if url:
                    data = self._download_image(url)
                    if data:
                        return data
        except Exception:
            log.debug("iTunes: обложка не найдена", exc_info=True)
        return None

    def _deezer_cover(self, artist, title):
        queries = [
            f'artist:"{clean_artist(artist)}" track:"{clean_title(title)}"',
            f"{clean_artist(artist)} {clean_title(title)}".strip(),
        ]
        for query in queries:
            try:
                res = http_get(DEEZER_URL, params={"q": query, "limit": 5}, timeout=8)
                if res.status_code != 200:
                    continue
                for item in res.json().get("data", []):
                    if not self._names_match(artist, (item.get("artist") or {}).get("name", "")):
                        continue
                    if not self._names_match(clean_title(title), clean_title(item.get("title", ""))):
                        continue
                    album = item.get("album") or {}
                    url = album.get("cover_xl") or album.get("cover_big")
                    if url:
                        data = self._download_image(url)
                        if data:
                            return data
            except Exception:
                log.debug("Deezer: обложка не найдена (%s)", query, exc_info=True)
        return None


    def _new_request(self):
        self._request_id += 1
        return self._request_id

    def _is_stale(self, rid):
        return rid != self._request_id

    def _deliver(self, rid, lyrics, synced):
        if not self._is_stale(rid):
            self.lyrics_loaded.emit(lyrics, synced)

    def download_lyrics(self, rid, artist, title, duration):
        threading.Thread(target=self._fetch, args=(rid, artist, title, duration), daemon=True).start()

    def _get_json(self, endpoint, params):
        try:
            res = http_get(f"{LRCLIB_URL}/{endpoint}", params=params, timeout=6)
            if res.status_code == 200:
                return res.json()
            if res.status_code != 404:
                log.warning("LRCLIB %s -> HTTP %s", endpoint, res.status_code)
        except Exception as exc:
            log_http_error(f"LRCLIB {endpoint}", exc)
        return None

    def _find_lrclib(self, rid, artist, title, duration):
        """Returns (synced_text | None, plain_text | None)."""
        t_clean = clean_title(title)
        a_full = clean_artist(artist)
        artists = unique([a_full, first_artist(a_full)])
        titles = unique([t_clean, title.strip()])
        plain_found = None

        def consider(items, art, strict=False):
            nonlocal plain_found
            synced, plain = pick_lrclib(items, art, duration, strict)
            if plain and not plain_found:
                plain_found = plain
            return synced

        for a in artists:
            for t in titles:
                if self._is_stale(rid): return None, None
                params = {"artist_name": a, "track_name": t}
                if duration and 1 <= duration <= 3600:
                    params["duration"] = int(round(duration))
                data = self._get_json("get", params)
                if isinstance(data, dict):
                    synced = consider([data], a)
                    if synced:
                        return synced, plain_found

        for a in artists:
            if self._is_stale(rid): return None, None
            data = self._get_json("search", {"track_name": t_clean, "artist_name": a})
            synced = consider(data if isinstance(data, list) else [], a, strict=True)
            if synced:
                return synced, plain_found

        if self._is_stale(rid): return None, None
        a = first_artist(a_full)
        data = self._get_json("search", {"q": f"{a} {t_clean}".strip()})
        synced = consider(data if isinstance(data, list) else [], a, strict=True)
        return synced, plain_found

    def _find_genius(self, artist, title):
        if not GENIUS_TOKEN:
            return None
        try:
            import lyricsgenius
        except ImportError:
            log.warning("Пакет lyricsgenius не установлен — поиск в Genius пропущен")
            return None
        try:
            try:
                genius = lyricsgenius.Genius(GENIUS_TOKEN, timeout=10, retries=2,
                                             verbose=False, remove_section_headers=True)
            except TypeError:
                genius = lyricsgenius.Genius(GENIUS_TOKEN, timeout=10, retries=2,
                                             remove_section_headers=True)
            song = genius.search_song(title, artist)
        except Exception as exc:
            log_http_error("Genius", exc)
            return None
        if song and song.lyrics:
            return clean_genius_lines(song.lyrics)
        return None

    def _fetch(self, rid, artist, title, duration):
        try:
            local_text = load_local_lyrics(artist, title)
            if local_text and not self._is_stale(rid):
                local_lyrics = parse_lrc(local_text)
                if local_lyrics:
                    self._deliver(rid, local_lyrics, True)
                    return

            synced_text, plain_text = self._find_lrclib(rid, artist, title, duration)
            if self._is_stale(rid):
                return

            if synced_text:
                lyrics = parse_lrc(synced_text)
                if lyrics:
                    save_local_lyrics(artist, title, synced_text)
                    self._deliver(rid, lyrics, True)
                    return

            lines = [l for l in (plain_text or "").splitlines() if l.strip()]
            if not lines:
                lines = self._find_genius(clean_artist(artist), clean_title(title)) or []
            if lines:
                self._deliver(rid, spread_lines(lines, duration), False)
            else:
                self._deliver(rid, [(-1, "Текст не найден")], True)
        except Exception:
            log.exception("Ошибка загрузки текста")
            self._deliver(rid, [(-2, "Ошибка текста")], True)

    def reload_lyrics(self, artist, title, duration):
        """Re-read the local file for the current track (after the manual editor saved it)."""
        rid = self._new_request()
        self.download_lyrics(rid, artist, title, duration)


class BigSpinBox(QWidget):
    valueChanged = pyqtSignal(int)
    def __init__(self, min_val, max_val, default, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(3)
        self.spin = QSpinBox(); self.spin.setRange(min_val, max_val); self.spin.setValue(default)
        self.spin.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self.spin.valueChanged.connect(self.valueChanged.emit)
        layout.addWidget(self.spin)
        self.btn_plus = QToolButton(); self.btn_plus.setText("+"); self.btn_plus.setFixedSize(24, 24)
        self.btn_plus.clicked.connect(lambda: self.spin.setValue(self.spin.value() + 1))
        layout.addWidget(self.btn_plus)
        self.btn_minus = QToolButton(); self.btn_minus.setText("-"); self.btn_minus.setFixedSize(24, 24)
        self.btn_minus.clicked.connect(lambda: self.spin.setValue(self.spin.value() - 1))
        layout.addWidget(self.btn_minus)
    def value(self): return self.spin.value()


class BigDoubleSpinBox(QWidget):
    """Same look as BigSpinBox but for fractional values.

    The native up/down arrows of QDoubleSpinBox are drawn by the Windows style and stayed
    dark in the light theme, so they are replaced with themed "+"/"-" buttons.
    """

    valueChanged = pyqtSignal(float)

    def __init__(self, min_val, max_val, default, decimals=2, step=0.1, suffix="", parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        self._step = float(step)
        self.spin = QDoubleSpinBox()
        self.spin.setRange(min_val, max_val)
        self.spin.setDecimals(decimals)
        self.spin.setSingleStep(step)
        self.spin.setSuffix(suffix)
        self.spin.setValue(default)
        self.spin.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons)
        self.spin.valueChanged.connect(self.valueChanged.emit)
        layout.addWidget(self.spin, 1)
        self.btn_plus = QToolButton(); self.btn_plus.setText("+"); self.btn_plus.setFixedSize(24, 24)
        self.btn_plus.clicked.connect(lambda: self.spin.setValue(self.spin.value() + self._step))
        layout.addWidget(self.btn_plus)
        self.btn_minus = QToolButton(); self.btn_minus.setText("-"); self.btn_minus.setFixedSize(24, 24)
        self.btn_minus.clicked.connect(lambda: self.spin.setValue(self.spin.value() - self._step))
        layout.addWidget(self.btn_minus)

    def value(self): return self.spin.value()
    def setValue(self, value): self.spin.setValue(value)
    def setRange(self, low, high): self.spin.setRange(low, high)
    def setDecimals(self, value): self.spin.setDecimals(value)
    def setSuffix(self, value): self.spin.setSuffix(value)
    def setSingleStep(self, value): self._step = float(value); self.spin.setSingleStep(value)


class ThemedCheckBox(QCheckBox):
    """Checkbox that draws its own tick: the stylesheet could only fill the box, so a
    checked box looked exactly like a coloured square with no check mark."""

    BOX = 16.0

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self.isChecked():
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        box = QRectF(0.5, (self.height() - self.BOX) / 2.0, self.BOX, self.BOX)
        pen = QPen(QColor(ui_colors()["button_text"]))
        pen.setWidthF(2.1)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        tick = QPainterPath(QPointF(box.left() + 3.8, box.center().y() + 0.4))
        tick.lineTo(box.left() + 6.8, box.bottom() - 4.4)
        tick.lineTo(box.right() - 3.4, box.top() + 4.2)
        painter.drawPath(tick)


class ChevronMixin:
    """Draws the drop-down chevron ourselves.

    Styling ``QComboBox::drop-down`` in the stylesheet removes the native arrow, so the
    chevron is painted on top and always matches the current theme.
    """

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor(ui_colors()["muted"]))
        pen.setWidthF(1.7)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        cx = self.width() - 15.0
        cy = self.height() / 2.0
        path = QPainterPath(QPointF(cx - 4.2, cy - 2.2))
        path.lineTo(cx, cy + 2.4)
        path.lineTo(cx + 4.2, cy - 2.2)
        painter.drawPath(path)


class ThemedComboBox(ChevronMixin, QComboBox):
    pass


class ThemedFontComboBox(ChevronMixin, QFontComboBox):
    pass


class UiDialog(QDialog):
    """Frameless themed dialog: one rounded card painted by the dialog itself.

    Every theme (dark / light / transparent) is just a different card colour, so switching
    themes only repaints the existing window instead of stacking a new one on top.
    """

    RADIUS = 16.0
    HEADER_HEIGHT = 46

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._drag_offset = None

    def build_header(self, title):
        header = QWidget()
        header.setObjectName("ui_header")
        header.setFixedHeight(self.HEADER_HEIGHT)
        row = QHBoxLayout(header)
        row.setContentsMargins(18, 12, 12, 0)
        row.setSpacing(8)

        label = QLabel(title)
        label.setObjectName("ui_title")
        row.addWidget(label)
        row.addStretch()

        close_btn = QToolButton()
        close_btn.setObjectName("ui_close")
        close_btn.setText("✕")
        close_btn.setFixedSize(30, 30)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setToolTip("Закрыть")
        close_btn.setAccessibleName("Закрыть")
        close_btn.clicked.connect(self.close)
        row.addWidget(close_btn)

        for widget in (header, label):
            widget.installEventFilter(self)
        return header

    def eventFilter(self, obj, event):
        kind = event.type()
        if kind == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            return True
        if kind == QEvent.Type.MouseMove and self._drag_offset is not None:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            return True
        if kind == QEvent.Type.MouseButtonRelease:
            self._drag_offset = None
            return True
        return super().eventFilter(obj, event)

    def paintEvent(self, event):
        colors = ui_colors()
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(rect, self.RADIUS, self.RADIUS)

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillPath(path, theme_qcolor(colors["bg"]))
        pen = QPen(theme_qcolor(colors["border"]))
        pen.setWidthF(1.0)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)


class SettingsDialog(UiDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Настройки Lyrics")
        self.setMinimumSize(480, 600)
        self.resize(520, 690)
        self.parent_overlay = parent

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self.build_header("Настройки"))

        layout = QVBoxLayout()
        layout.setContentsMargins(18, 2, 18, 16)
        layout.setSpacing(10)
        outer.addLayout(layout, 1)

        tabs = QTabWidget()

        tab_appearance = QWidget()
        form_app = QFormLayout(tab_appearance)
        form_app.setSpacing(8)
        self._style_form(form_app)

        self.theme_combo = ThemedComboBox()
        self.theme_combo.addItem("Стандартная", "custom")
        self.theme_combo.addItem("Тёмная", "dark")
        self.theme_combo.addItem("Светлая", "light")
        self.theme_combo.addItem("Прозрачная", "transparent")
        theme_index = self.theme_combo.findData(SETTINGS.get("theme", "custom"))
        self.theme_combo.setCurrentIndex(max(0, theme_index))
        form_app.addRow("Тема меню:", self.theme_combo)

        self.width_spin = BigSpinBox(20, 100, int(SETTINGS.get("text_width", 80)))
        form_app.addRow("Ширина текста (%):", self.width_spin)

        self.font_combo = ThemedFontComboBox()
        self.font_combo.setMinimumContentsLength(10)
        self.font_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.font_combo.setCurrentFont(QFont(SETTINGS.get("font_family", "Segoe UI")))
        form_app.addRow("Шрифт:", self.font_combo)

        self.size_spin = BigSpinBox(10, 150, int(SETTINGS.get("font_size", 36)))
        form_app.addRow("Размер текста:", self.size_spin)

        self.text_color_btn = QPushButton("Выбрать")
        self.update_btn_color(self.text_color_btn, SETTINGS.get("text_color", "#FFFFFF"))
        self.text_color_btn.clicked.connect(self.pick_text_color)
        form_app.addRow("Цвет спетого:", self.text_color_btn)

        self.text_opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self.text_opacity_slider.setRange(0, 100)
        self.text_opacity_slider.setValue(int(SETTINGS.get("text_opacity", 100)))
        self.text_opacity_slider.valueChanged.connect(self.auto_apply)
        form_app.addRow("Прозрачность (%):", self.text_opacity_slider)

        self.future_color_btn = QPushButton("Выбрать")
        self.update_btn_color(self.future_color_btn, SETTINGS.get("future_color", "#555555"))
        self.future_color_btn.clicked.connect(self.pick_future_color)
        form_app.addRow("Цвет неспетого:", self.future_color_btn)

        self.hide_future_cb = ThemedCheckBox("Скрыть неспетый")
        self.hide_future_cb.setChecked(SETTINGS.get("hide_future", False))
        form_app.addRow("", self.hide_future_cb)

        self.bg_color_btn = QPushButton("Выбрать")
        self.update_btn_color(self.bg_color_btn, SETTINGS.get("bg_color", "#000000"))
        self.bg_color_btn.clicked.connect(self.pick_bg_color)
        form_app.addRow("Цвет фона:", self.bg_color_btn)

        self.bg_opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self.bg_opacity_slider.setRange(0, 100)
        self.bg_opacity_slider.setValue(int(SETTINGS.get("bg_opacity", 0)))
        self.bg_opacity_slider.valueChanged.connect(self.auto_apply)
        form_app.addRow("Прозрач. фона:", self.bg_opacity_slider)

        tabs.addTab(scroll_page(tab_appearance), "Текст")

        tab_effects = QWidget()
        layout_effects = QVBoxLayout(tab_effects)
        layout_effects.setContentsMargins(5, 5, 5, 2)

        group_grad = QGroupBox("Градиент")
        form_grad = QFormLayout(group_grad)
        self._style_form(form_grad)
        self.use_gradient_cb = ThemedCheckBox("Включить")
        self.use_gradient_cb.setChecked(SETTINGS.get("use_gradient", True))
        form_grad.addRow("Градиент:", self.use_gradient_cb)

        self.grad_chips = []
        self.grad_rows = QGridLayout()
        self.grad_rows.setContentsMargins(0, 0, 0, 0)
        self.grad_rows.setHorizontalSpacing(8)
        self.grad_rows.setVerticalSpacing(6)
        grad_container = QWidget()
        grad_container.setLayout(self.grad_rows)
        form_grad.addRow("Цвета:", grad_container)
        self.add_grad_btn = QPushButton("+ Добавить цвет")
        self.add_grad_btn.setObjectName("btn_cancel")
        self.add_grad_btn.setFixedWidth(150)
        self.add_grad_btn.setToolTip("Добавить ещё один цвет в градиент")
        self.add_grad_btn.clicked.connect(self.add_grad_color)
        form_grad.addRow("", self.add_grad_btn)
        self.rebuild_grad_rows()
        layout_effects.addWidget(group_grad)

        group_outline = QGroupBox("Обводка")
        form_outline = QFormLayout(group_outline)
        self._style_form(form_outline)
        self.outline_color_btn = QPushButton("Выбрать")
        self.update_btn_color(self.outline_color_btn, SETTINGS.get("outline_color", "#000000"), force_white_text=True)
        self.outline_color_btn.clicked.connect(self.pick_outline_color)
        form_outline.addRow("Цвет:", self.outline_color_btn)
        self.outline_width_spin = BigSpinBox(0, 10, int(SETTINGS.get("outline_width", 2)))
        form_outline.addRow("Толщина:", self.outline_width_spin)
        layout_effects.addWidget(group_outline)

        group_anim = QGroupBox("Свечение и эффекты")
        form_anim = QFormLayout(group_anim)
        self._style_form(form_anim)

        self.anim_glow_cb = ThemedCheckBox("Включить")
        self.anim_glow_cb.setChecked(SETTINGS.get("anim_glow", False))
        form_anim.addRow("Свечение:", self.anim_glow_cb)

        self.glow_color_btn = QPushButton("Выбрать")
        self.update_btn_color(self.glow_color_btn, SETTINGS.get("glow_color", "#89b4fa"))
        self.glow_color_btn.clicked.connect(self.pick_glow_color)
        form_anim.addRow("Цвет свечения:", self.glow_color_btn)

        self.glow_size_spin = BigSpinBox(1, 10, int(SETTINGS.get("glow_size", 3)))
        form_anim.addRow("Размер свечения:", self.glow_size_spin)

        self.glow_speed_spin = BigSpinBox(1, 10, int(SETTINGS.get("glow_speed", 5)))
        form_anim.addRow("Скорость свечения:", self.glow_speed_spin)

        self.anim_pulse_cb = ThemedCheckBox("Включить")
        self.anim_pulse_cb.setChecked(SETTINGS.get("anim_pulse", False))
        form_anim.addRow("Пульсация:", self.anim_pulse_cb)
        self.anim_wave_cb = ThemedCheckBox("Включить")
        self.anim_wave_cb.setChecked(SETTINGS.get("anim_wave", False))
        form_anim.addRow("Волна:", self.anim_wave_cb)
        self.anim_flicker_cb = ThemedCheckBox("Включить")
        self.anim_flicker_cb.setChecked(SETTINGS.get("anim_flicker", False))
        form_anim.addRow("Мерцание:", self.anim_flicker_cb)

        layout_effects.addWidget(group_anim)

        layout_effects.addStretch()
        tabs.addTab(scroll_page(tab_effects), "Эффекты")

        tab_modes = QWidget()
        layout_modes = QVBoxLayout(tab_modes)
        layout_modes.setContentsMargins(5, 5, 5, 5)
        layout_modes.setSpacing(6)

        group_display = QGroupBox("Режим показа")
        form_modes = QFormLayout(group_display)
        self._style_form(form_modes)
        self.mode_combo = ThemedComboBox()
        self.mode_combo.addItem("Классика", "classic")
        self.mode_combo.addItem("Винил", "vinyl")
        mode_index = self.mode_combo.findData(SETTINGS.get("mode", "classic"))
        self.mode_combo.setCurrentIndex(max(0, mode_index))
        form_modes.addRow("Режим:", self.mode_combo)
        self.lines_spin = BigSpinBox(1, 7, int(SETTINGS.get("lines_visible", 3)))
        form_modes.addRow("Строк на экране:", self.lines_spin)
        layout_modes.addWidget(group_display)

        group_text_anim = QGroupBox("Появление текста")
        form_text_anim = QFormLayout(group_text_anim)
        self._style_form(form_text_anim)

        self.anim_mode_combo = ThemedComboBox()
        self.anim_mode_combo.addItem("Обычная строка", "line")
        self.anim_mode_combo.addItem("Слова по одному", "words")
        self.anim_mode_combo.addItem("Текст из пластинки", "pull")
        self.anim_mode_combo.addItem("Слова по сторонам", "sides")
        self.anim_mode_combo.setToolTip(
            "«Обычная строка» — караоке-подсветка целой строки.\n"
            "«Слова по одному» — вертикальный Reels-стиль.\n"
            "«Текст из пластинки» — строка вытягивается из диска и встаёт на место.\n"
            "«Слова по сторонам» — слова разлетаются по экрану и вылетают слева,\n"
            "справа или сверху; спетая строка исчезает.\n"
            "В этом режиме цвет неспетого текста не используется.")
        mode_idx = self.anim_mode_combo.findData(SETTINGS.get("anim_mode", "line"))
        self.anim_mode_combo.setCurrentIndex(max(0, mode_idx))
        form_text_anim.addRow("Стиль:", self.anim_mode_combo)

        self.slide_from_combo = ThemedComboBox()
        self.slide_from_combo.addItem("Снизу", "bottom")
        self.slide_from_combo.addItem("Сверху", "top")
        self.slide_from_combo.addItem("Слева", "left")
        self.slide_from_combo.addItem("Справа", "right")
        self.slide_from_combo.addItem("Без сдвига", "none")
        slide_idx = self.slide_from_combo.findData(SETTINGS.get("slide_from", "bottom"))
        self.slide_from_combo.setCurrentIndex(max(0, slide_idx))
        form_text_anim.addRow("Откуда:", self.slide_from_combo)

        self.slide_dist_spin = BigSpinBox(0, 120, int(SETTINGS.get("slide_dist", 28)))
        form_text_anim.addRow("Дистанция:", self.slide_dist_spin)

        self.words_per_row_spin = BigSpinBox(1, 4, int(SETTINGS.get("words_per_row", 1)))
        form_text_anim.addRow("Слов в строке:", self.words_per_row_spin)

        self.words_visible_spin = BigSpinBox(1, 8, int(SETTINGS.get("words_visible", 4)))
        form_text_anim.addRow("Слов на экране:", self.words_visible_spin)

        layout_modes.addWidget(group_text_anim)

        group_vinyl = QGroupBox("Винил")
        form_vinyl = QFormLayout(group_vinyl)
        self._style_form(form_vinyl)
        self.vinyl_style_combo = ThemedComboBox()
        self.vinyl_style_combo.addItem("Неон", "neon")
        self.vinyl_style_combo.addItem("Классика", "classic")
        self.vinyl_style_combo.addItem("Минимал", "minimal")
        style_index = self.vinyl_style_combo.findData(
            SETTINGS.get("vinyl_style", "neon"))
        self.vinyl_style_combo.setCurrentIndex(max(0, style_index))
        self.vinyl_style_combo.setToolTip(
            "«Неон» — тёмный диск с цветным свечением и электрическими полосами.\n"
            "«Классика» — матовый винил с мягкими белыми бликами.\n"
            "«Минимал» — тёмный диск без бликов, только тонкий ободок.")
        form_vinyl.addRow("Стиль диска:", self.vinyl_style_combo)

        self.vinyl_accent_btn = QPushButton("Выбрать")
        self.update_btn_color(
            self.vinyl_accent_btn,
            SETTINGS.get("vinyl_accent") or SETTINGS.get("gradient_color2", "#89b4fa"))
        self.vinyl_accent_btn.clicked.connect(self.pick_vinyl_accent)
        form_vinyl.addRow("Неоновый цвет:", self.vinyl_accent_btn)

        self.vinyl_glow_spin = BigSpinBox(0, 100, int(SETTINGS.get("vinyl_glow", 60)))
        self.vinyl_glow_spin.setToolTip("Сила свечения и электрических полос (0 — выключено)")
        form_vinyl.addRow("Сила свечения (%):", self.vinyl_glow_spin)

        self.vinyl_rim_cb = ThemedCheckBox("Светящийся ободок")
        self.vinyl_rim_cb.setChecked(bool(SETTINGS.get("vinyl_rim", True)))
        form_vinyl.addRow("Ободок:", self.vinyl_rim_cb)

        self.vinyl_speed_spin = BigSpinBox(1, 10, int(SETTINGS.get("vinyl_speed", 4)))
        form_vinyl.addRow("Скорость вращения:", self.vinyl_speed_spin)
        self.vinyl_size_spin = BigSpinBox(90, 420, int(SETTINGS.get("vinyl_size", 280)))
        form_vinyl.addRow("Размер диска:", self.vinyl_size_spin)
        self.vinyl_gap_spin = BigSpinBox(0, 220, int(SETTINGS.get("vinyl_gap", 24)))
        form_vinyl.addRow("Отступ до текста:", self.vinyl_gap_spin)
        self.vinyl_shift_spin = BigSpinBox(-50, 50, int(SETTINGS.get("vinyl_shift", 0)))
        form_vinyl.addRow("Сдвиг влево/вправо (%):", self.vinyl_shift_spin)
        self.vinyl_caption_cb = ThemedCheckBox("Показывать название и исполнитель")
        self.vinyl_caption_cb.setChecked(bool(SETTINGS.get("vinyl_caption", True)))
        form_vinyl.addRow(self.vinyl_caption_cb)
        layout_modes.addWidget(group_vinyl)

        layout_modes.addStretch()
        tabs.insertTab(0, scroll_page(tab_modes), "Режимы")

        tab_sync = QWidget()
        form_sync = QFormLayout(tab_sync)
        self._style_form(form_sync)
        self.offset_spin = BigDoubleSpinBox(-5.0, 5.0, float(SETTINGS.get("offset", 0.0)))
        form_sync.addRow("Задержка (сек):", self.offset_spin)
        self.margin_spin = BigDoubleSpinBox(0.1, 3.0, float(SETTINGS.get("sync_margin", 0.8)))
        form_sync.addRow("Оффсет печати:", self.margin_spin)

        self.show_hint_cb = ThemedCheckBox("Подпись «без синхронизации»")
        self.show_hint_cb.setChecked(bool(SETTINGS.get("show_unsynced_hint", True)))
        self.show_hint_cb.setToolTip(
            "Подпись под текстом, который растянут по длительности трека.\n"
            "Сам текст показывается в любом случае, скрывается только подпись.")
        form_sync.addRow("", self.show_hint_cb)

        self.cover_online_cb = ThemedCheckBox("Искать обложку в интернете")
        self.cover_online_cb.setChecked(SETTINGS.get("cover_online", True))
        form_sync.addRow("", self.cover_online_cb)

        self.autostart_cb = ThemedCheckBox("Запускать вместе с Windows")
        self.autostart_cb.setChecked(is_autostart_enabled())
        form_sync.addRow("", self.autostart_cb)

        editor_btn = QPushButton("Открыть редактор текста…")
        editor_btn.setFixedWidth(220)
        editor_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        editor_btn.clicked.connect(self.open_editor)
        form_sync.addRow("", editor_btn)
        self.editor_btn = editor_btn

        self.genius_token_input = QLineEdit()
        self.genius_token_input.setText(SETTINGS.get("genius_token", ""))
        form_sync.addRow("Genius Token:", self.genius_token_input)

        exit_btn = QPushButton("Выход")
        exit_btn.setObjectName("btn_exit")
        exit_btn.setFixedWidth(220)
        exit_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        exit_btn.clicked.connect(self.exit_app)
        form_sync.addRow("", exit_btn)
        self.exit_btn = exit_btn

        self.style_action_buttons()

        tabs.addTab(scroll_page(tab_sync), "Прочее")

        layout.addWidget(tabs)

        self.mode_combo.currentIndexChanged.connect(self.auto_apply)
        self.theme_combo.currentIndexChanged.connect(self.on_theme_changed)
        self.anim_mode_combo.currentIndexChanged.connect(self.auto_apply)
        self.slide_from_combo.currentIndexChanged.connect(self.auto_apply)
        self.slide_dist_spin.valueChanged.connect(self.auto_apply)
        self.words_per_row_spin.valueChanged.connect(self.auto_apply)
        self.words_visible_spin.valueChanged.connect(self.auto_apply)
        self.vinyl_size_spin.valueChanged.connect(self.auto_apply)
        self.vinyl_gap_spin.valueChanged.connect(self.auto_apply)
        self.vinyl_shift_spin.valueChanged.connect(self.auto_apply)
        self.vinyl_caption_cb.stateChanged.connect(self.auto_apply)
        self.vinyl_style_combo.currentIndexChanged.connect(self.auto_apply)
        self.vinyl_glow_spin.valueChanged.connect(self.auto_apply)
        self.vinyl_rim_cb.stateChanged.connect(self.auto_apply)
        self.autostart_cb.stateChanged.connect(self.on_autostart_toggled)
        self.lines_spin.valueChanged.connect(self.auto_apply)
        self.width_spin.valueChanged.connect(self.auto_apply)
        self.vinyl_speed_spin.valueChanged.connect(self.auto_apply)
        self.cover_online_cb.stateChanged.connect(self.auto_apply)
        self.size_spin.valueChanged.connect(self.auto_apply)
        self.font_combo.currentFontChanged.connect(self.auto_apply)
        self.use_gradient_cb.stateChanged.connect(self.auto_apply)
        self.hide_future_cb.stateChanged.connect(self.auto_apply)
        self.outline_width_spin.valueChanged.connect(self.auto_apply)
        self.offset_spin.valueChanged.connect(self.auto_apply)
        self.margin_spin.valueChanged.connect(self.auto_apply)
        self.show_hint_cb.stateChanged.connect(self.on_show_hint_toggled)
        self.genius_token_input.textChanged.connect(self.auto_apply)
        self.anim_glow_cb.stateChanged.connect(self.auto_apply)
        self.glow_size_spin.valueChanged.connect(self.auto_apply)
        self.glow_speed_spin.valueChanged.connect(self.auto_apply)
        self.anim_pulse_cb.stateChanged.connect(self.auto_apply)
        self.anim_wave_cb.stateChanged.connect(self.auto_apply)
        self.anim_flicker_cb.stateChanged.connect(self.auto_apply)

        btn_layout = QHBoxLayout()
        close_btn = QPushButton("Закрыть")
        close_btn.setObjectName("btn_cancel")
        close_btn.clicked.connect(self.close)
        btn_layout.addStretch()
        btn_layout.addWidget(close_btn)
        layout.addLayout(btn_layout)

        apply_combo_popup_theme(SETTINGS.get("theme", "custom"))
        self._unify_form_labels()

    def _unify_form_labels(self):
        """Give every row the same label column, so fields line up across blocks."""
        labels = []
        for form in self.findChildren(QFormLayout):
            for row in range(form.rowCount()):
                item = form.itemAt(row, QFormLayout.ItemRole.LabelRole)
                if item is None or item.widget() is None:
                    continue
                label = item.widget()
                if label.text().strip():
                    labels.append(label)
        if not labels:
            return
        width = min(240, max(label.sizeHint().width() for label in labels))
        for label in labels:
            label.setFixedWidth(width)
            label.setAlignment(Qt.AlignmentFlag.AlignRight
                               | Qt.AlignmentFlag.AlignVCenter)

    @staticmethod
    def _style_form(form):
        """One label column for every row: the text no longer drifts out of line."""
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)

    def exit_app(self):
        QApplication.instance().quit()

    def open_editor(self):
        if self.parent_overlay:
            self.parent_overlay.open_lyrics_editor()

    def on_autostart_toggled(self, _state):
        enabled = self.autostart_cb.isChecked()
        if set_autostart(enabled):
            SETTINGS["autostart"] = enabled
        else:
            self.autostart_cb.blockSignals(True)
            self.autostart_cb.setChecked(is_autostart_enabled())
            self.autostart_cb.blockSignals(False)
        self.auto_apply()

    def on_theme_changed(self, _index):
        """Theme presets restyle the menus/dialogs; the overlay keeps the user's colours."""
        name = self.theme_combo.currentData() or "custom"
        SETTINGS["theme"] = name
        apply_ui_theme(name)
        self.refresh_color_buttons()
        self.auto_apply()

    def refresh_color_buttons(self):
        """The chips keep a theme-coloured border, so repaint them when the theme changes."""
        for btn, key, white in ((self.text_color_btn, "text_color", False),
                                (self.future_color_btn, "future_color", False),
                                (self.bg_color_btn, "bg_color", False),
                                (self.outline_color_btn, "outline_color", True),
                                (self.glow_color_btn, "glow_color", False)):
            self.update_btn_color(btn, SETTINGS.get(key, "#000000"), force_white_text=white)
        accent = SETTINGS.get("vinyl_accent") or SETTINGS.get("gradient_color2", "#89b4fa")
        self.update_btn_color(self.vinyl_accent_btn, accent)
        for chip, value in zip(getattr(self, "grad_chips", []), gradient_stops()):
            self.update_btn_color(chip, value)
        self.style_action_buttons()

    def style_action_buttons(self):
        """Explicit colours for the two big actions: they must never blend into the card.

        In the light theme both get a deeper shade so they stay readable on white.
        """
        colors = ui_colors()
        theme = SETTINGS.get("theme", "custom")
        if theme == "light":
            editor_bg, editor_hover, editor_fg = "#2563EB", "#1D4ED8", "#FFFFFF"
            exit_bg, exit_hover = "#B91C1C", "#991B1B"
        else:
            editor_bg, editor_hover, editor_fg = (colors["accent"], colors["accent_hover"],
                                                  colors["button_text"])
            exit_bg, exit_hover = colors["bad"], colors["bad"]
        buttons = ((getattr(self, "editor_btn", None), editor_bg, editor_hover, editor_fg),
                   (getattr(self, "exit_btn", None), exit_bg, exit_hover, "#FFFFFF"))
        for btn, bg, hover, fg in buttons:
            if btn is None:
                continue
            btn.setStyleSheet(
                f"QPushButton {{ background-color: {bg}; color: {fg};"
                " border: 1px solid transparent; border-radius: 9px;"
                " padding: 7px 14px; font-weight: 600; font-size: 13px; }"
                f"QPushButton:hover {{ background-color: {hover}; }}")

    def rebuild_grad_rows(self):
        """One chip per gradient stop, with a remove button once there are more than two."""
        while self.grad_rows.count():
            item = self.grad_rows.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self.grad_chips = []
        colors = gradient_stops()
        SETTINGS["gradient_colors"] = list(colors)
        for index, value in enumerate(colors):
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(6)
            chip = QPushButton()
            self.update_btn_color(chip, value)
            chip.clicked.connect(lambda _checked=False, i=index: self.pick_grad_color(i))
            row_layout.addWidget(chip)
            if len(colors) > 2:
                remove = QToolButton()
                remove.setText("✕")
                remove.setFixedSize(28, 28)
                remove.setCursor(Qt.CursorShape.PointingHandCursor)
                remove.setToolTip("Убрать этот цвет")
                remove.clicked.connect(lambda _checked=False, i=index: self.remove_grad_color(i))
                row_layout.addWidget(remove)
            row_layout.addStretch()
            self.grad_rows.addWidget(row, index, 0)
            self.grad_chips.append(chip)
        if getattr(self, "add_grad_btn", None) is not None:
            self.add_grad_btn.setEnabled(len(colors) < 6)

    def _save_grad_colors(self, colors):
        colors = [QColor(value).name() for value in colors][:6]
        SETTINGS["gradient_colors"] = colors
        SETTINGS["gradient_color2"] = colors[-1]
        self.rebuild_grad_rows()
        self.auto_apply()

    def pick_grad_color(self, index):
        colors = gradient_stops()
        if not 0 <= index < len(colors):
            return
        colors[index] = self.pick_color(colors[index], f"Цвет градиента {index + 1}")
        self._save_grad_colors(colors)

    def add_grad_color(self):
        colors = gradient_stops()
        if len(colors) >= 6:
            return
        colors.append(colors[-1] if colors else "#89B4FA")
        self._save_grad_colors(colors)

    def remove_grad_color(self, index):
        colors = gradient_stops()
        if len(colors) <= 2 or not 0 <= index < len(colors):
            return
        del colors[index]
        self._save_grad_colors(colors)

    def update_btn_color(self, btn, hex_color, force_white_text=False):
        """Compact colour chip: the swatch itself, plus a readable "Выбрать" label.

        The button used to stretch into a full-width bar and a white swatch was invisible on
        the light card, so it now keeps its own border and a fixed width.
        """
        color = QColor(hex_color)
        if not color.isValid():
            color = QColor("#000000")
            hex_color = "#000000"
        light = color.lightness() >= 150
        fg = "#FFFFFF" if force_white_text or not light else "#111111"
        border = ui_colors()['border']
        accent = ui_colors()['accent']
        btn.setText("Выбрать")
        btn.setFixedWidth(132)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setStyleSheet(
            f"QPushButton {{ background-color: {hex_color}; color: {fg};"
            f" border: 1px solid {border}; border-radius: 9px;"
            " padding: 5px 10px; font-weight: 600; font-size: 12px; min-height: 20px; }"
            f"QPushButton:hover {{ border: 1px solid {accent}; }}"
            f"QPushButton:pressed {{ border: 1px solid {accent}; }}")
        btn.setToolTip(f"Цвет {hex_color.upper()} — нажмите, чтобы изменить")

    def pick_color(self, current_hex, title):
        dialog = QColorDialog(QColor(current_hex), self)
        dialog.setWindowTitle(title)
        dialog.setOption(QColorDialog.ColorDialogOption.DontUseNativeDialog, True)
        theme_name = SETTINGS.get("theme", "custom")
        dialog.setPalette(color_dialog_palette(theme_name))
        dialog.setStyleSheet(color_dialog_stylesheet(theme_name))
        dialog.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        if dialog.exec() == QColorDialog.DialogCode.Accepted:
            return dialog.currentColor().name()
        return current_hex

    def pick_text_color(self):
        SETTINGS["text_color"] = self.pick_color(SETTINGS["text_color"], "Цвет текста")
        colors = gradient_stops()
        colors[0] = SETTINGS["text_color"]
        SETTINGS["gradient_colors"] = colors
        SETTINGS["gradient_color2"] = colors[-1]
        self.update_btn_color(self.text_color_btn, SETTINGS["text_color"])
        self.rebuild_grad_rows()
        self.auto_apply()
    def pick_future_color(self): SETTINGS["future_color"] = self.pick_color(SETTINGS["future_color"], "Цвет неспетого"); self.update_btn_color(self.future_color_btn, SETTINGS["future_color"]); self.auto_apply()
    def pick_bg_color(self): SETTINGS["bg_color"] = self.pick_color(SETTINGS["bg_color"], "Цвет фона"); self.update_btn_color(self.bg_color_btn, SETTINGS["bg_color"]); self.auto_apply()
    def pick_outline_color(self): SETTINGS["outline_color"] = self.pick_color(SETTINGS["outline_color"], "Цвет обводки"); self.update_btn_color(self.outline_color_btn, SETTINGS["outline_color"], force_white_text=True); self.auto_apply()
    def pick_glow_color(self): SETTINGS["glow_color"] = self.pick_color(SETTINGS["glow_color"], "Цвет свечения"); self.update_btn_color(self.glow_color_btn, SETTINGS["glow_color"]); self.auto_apply()
    def pick_vinyl_accent(self):
        current = SETTINGS.get("vinyl_accent") or SETTINGS.get("gradient_color2", "#89b4fa")
        SETTINGS["vinyl_accent"] = self.pick_color(current, "Неоновый цвет пластинки")
        self.update_btn_color(self.vinyl_accent_btn, SETTINGS["vinyl_accent"])
        self.auto_apply()

    def on_show_hint_toggled(self, _state=0):
        """Turning the caption back on should show it again, not keep the old timeout."""
        self.auto_apply()
        widget = getattr(self.parent_overlay, "lyrics_widget", None)
        if widget is not None:
            widget.reset_hint()
            widget.update()

    def auto_apply(self):
        SETTINGS["mode"] = self.mode_combo.currentData() or "classic"
        SETTINGS["lines_visible"] = self.lines_spin.value()
        SETTINGS["text_width"] = self.width_spin.value()
        SETTINGS["vinyl_speed"] = self.vinyl_speed_spin.value()
        SETTINGS["cover_online"] = self.cover_online_cb.isChecked()
        SETTINGS["font_family"] = self.font_combo.currentFont().family()
        SETTINGS["font_size"] = self.size_spin.value()
        SETTINGS["text_opacity"] = self.text_opacity_slider.value()
        SETTINGS["bg_opacity"] = self.bg_opacity_slider.value()
        SETTINGS["hide_future"] = self.hide_future_cb.isChecked()
        SETTINGS["use_gradient"] = self.use_gradient_cb.isChecked()
        SETTINGS["outline_width"] = self.outline_width_spin.value()
        SETTINGS["offset"] = self.offset_spin.value()
        SETTINGS["sync_margin"] = self.margin_spin.value()
        SETTINGS["show_unsynced_hint"] = self.show_hint_cb.isChecked()
        SETTINGS["genius_token"] = self.genius_token_input.text()

        SETTINGS["anim_glow"] = self.anim_glow_cb.isChecked()
        SETTINGS["glow_size"] = self.glow_size_spin.value()
        SETTINGS["glow_speed"] = self.glow_speed_spin.value()
        SETTINGS["anim_pulse"] = self.anim_pulse_cb.isChecked()
        SETTINGS["anim_wave"] = self.anim_wave_cb.isChecked()
        SETTINGS["anim_flicker"] = self.anim_flicker_cb.isChecked()

        SETTINGS["anim_mode"] = self.anim_mode_combo.currentData() or "line"
        SETTINGS["slide_from"] = self.slide_from_combo.currentData() or "bottom"
        SETTINGS["slide_dist"] = self.slide_dist_spin.value()
        SETTINGS["words_per_row"] = self.words_per_row_spin.value()
        SETTINGS["words_visible"] = self.words_visible_spin.value()
        SETTINGS["vinyl_size"] = self.vinyl_size_spin.value()
        SETTINGS["vinyl_gap"] = self.vinyl_gap_spin.value()
        SETTINGS["vinyl_shift"] = self.vinyl_shift_spin.value()
        SETTINGS["vinyl_caption"] = self.vinyl_caption_cb.isChecked()
        SETTINGS["vinyl_style"] = self.vinyl_style_combo.currentData() or "neon"
        SETTINGS["vinyl_glow"] = self.vinyl_glow_spin.value()
        SETTINGS["vinyl_rim"] = self.vinyl_rim_cb.isChecked()
        SETTINGS["autostart"] = self.autostart_cb.isChecked()

        global GENIUS_TOKEN
        GENIUS_TOKEN = self.genius_token_input.text()

        if self.parent_overlay:
            self.parent_overlay.apply_styles()
            self.parent_overlay.save_current_settings()

class LyricsWidget(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setWordWrap(True)
        self.current_text = ""
        self.prev_text = ""
        self.anim_t = 1.0
        self.visible_len = 0
        self.last_visible_len = 0
        self.wave_offsets = {}
        self.anim_time = 0.0
        self.glow_phase = 0.0
        self.flicker_timer = 0.0
        self.is_playing = False
        self.unsynced = False
        self._hint_started = None
        self._hint_hidden = False
        self._hint_btn = QRectF()
        self._hint_last = -1.0

        self.lines = []
        self.index = IDX_UNSET
        self.artist = ""
        self.title = ""
        self.cover_pm = None
        self.vinyl_angle = 0.0
        self._vinyl_anchor = None
        self._sides_times = {}
        self._sides_done_at = None
        self._sides_cache_key = None
        self._sides_cache_rows = []
        self._sides_avoid = QRectF()
        self._vinyl_box = None

        self._fit_cache = {}
        self._glow_cache = {}
        self._cover_key = None
        self._cover_scaled = None

        self.word_pop_t0 = 0.0
        self._word_pop_times = {}
        self._words_done = 0
        self._glow_last = -1.0
        self._status_last = -1.0
        self._words_cache_key = None
        self._words_cache_rows = []
        self._words_cache_widest = ""
        self._words_scroll = 0.0
        self._words_scroll_target = 0.0
        self._words_scroll_from = 0.0
        self._words_scroll_t0 = -1.0


    @staticmethod
    def vinyl_caption_height():
        """Real height of the title/artist caption under the disc (fonts vary a lot)."""
        if not SETTINGS.get("vinyl_caption", True):
            return 10
        size = max(10, int(SETTINGS.get("font_size", 36)))
        family = SETTINGS.get("font_family", "Segoe UI")
        title_font = cached_font(family, max(11, int(size * 0.52)), QFont.Weight.Bold)
        artist_font = cached_font(family, max(9, int(size * 0.36)))
        return (font_metrics(title_font).height()
                + font_metrics(artist_font).height() + 12)

    @staticmethod
    def preferred_height():
        size = max(10, int(SETTINGS.get("font_size", 36)))
        font = cached_font(SETTINGS.get("font_family", "Segoe UI"), size, QFont.Weight.Bold)
        line_h = font_metrics(font).height() * 1.25
        count = max(1, min(7, int(SETTINGS.get("lines_visible", 3))))
        needed = int(line_h * count) + 24
        if SETTINGS.get("anim_mode", "line") == "words":
            rows = max(1, min(8, int(SETTINGS.get("words_visible", 4))))
            needed = max(needed, int(font_metrics(font).height() * 1.06 * rows) + 20)
        if SETTINGS.get("anim_mode", "line") == "sides":
            needed = max(needed, min(620, int(line_h * 4.0) + 20))
        if SETTINGS.get("mode", "classic") == "vinyl":
            caption = LyricsWidget.vinyl_caption_height()
            vinyl = int(SETTINGS.get("vinyl_size", 280))
            needed = max(needed, vinyl + caption + 22,
                         int(vinyl * 1.30) + caption + 22)
        return max(300, needed)

    def set_cover(self, pixmap):
        self.cover_pm = pixmap
        self._cover_key = None


    def set_unsynced(self, value):
        """A new track arrived: show the caption again and restart its timer."""
        self.unsynced = bool(value)
        self.reset_hint()
        self.update()

    def reset_hint(self):
        self._hint_started = None
        self._hint_hidden = False
        self._hint_last = -1.0

    def hint_button_rect(self):
        return self._hint_btn.toRect() if not self._hint_btn.isEmpty() else QRect()

    def dismiss_hint(self):
        self._hint_hidden = True
        self._hint_btn = QRectF()
        self.update()

    def _hint_alpha(self):
        if (not self.unsynced or not SETTINGS.get("show_unsynced_hint", True)
                or not self.current_text or self._status_kind()):
            return 0.0
        if self._hint_hidden:
            return 0.0
        if self._hint_started is None:
            self._hint_started = self.anim_time
        elapsed = self.anim_time - self._hint_started
        if elapsed >= HINT_VISIBLE_TIME:
            self._hint_hidden = True
            return 0.0
        fade = 1.0
        remaining = HINT_VISIBLE_TIME - elapsed
        if remaining < 1.0:
            fade = max(0.0, remaining)
        blink = 0.72 + 0.28 * (0.5 + 0.5 * math.sin(self.anim_time * 5.5))
        return max(0.0, min(1.0, fade * blink))


    def tick(self, dt):
        """Advance animations; returns True when the next frame would look different."""
        animating = False
        if self.anim_t < 1.0:
            self.anim_t = min(1.0, self.anim_t + dt / ANIM_DURATION)
            if self.anim_t >= 1.0:
                self.prev_text = ""
            animating = True
        self.anim_time += dt
        self.glow_phase += dt * float(SETTINGS.get("glow_speed", 5))

        if SETTINGS.get("anim_mode", "line") == "words" and self.current_text:
            done = len(re.findall(r"\S+", self.current_text[:max(0, self.visible_len)]))
            if done != self._words_done:
                for index in range(self._words_done, done):
                    self._word_pop_times[index] = (self.anim_time if index == done - 1
                                                   else self.anim_time - WORD_POP_TIME)
                self._words_done = done
                self.word_pop_t0 = self.anim_time
                animating = True
            elif self.visible_len and self.anim_time - self.word_pop_t0 < WORD_POP_TIME:
                animating = True
            if self._words_scroll != self._words_scroll_target:
                animating = True

        if SETTINGS.get("anim_mode", "line") == "sides" and self.current_text:
            vis = max(0, min(self.visible_len, len(self.current_text)))
            done = len(re.findall(r"\S+", self.current_text[:vis]))
            known = len(self._sides_times)
            if done < known:
                for index in [k for k in self._sides_times if k >= done]:
                    del self._sides_times[index]
                self._sides_done_at = None
                animating = True
            if done > known:
                for index in range(known, done):
                    self._sides_times[index] = (self.anim_time if index == done - 1
                                                else self.anim_time - SIDES_POP_TIME)
                animating = True
            if self._sides_times and \
                    self.anim_time - max(self._sides_times.values()) < SIDES_POP_TIME:
                animating = True
            total = len(re.findall(r"\S+", self.current_text))
            if total and done >= total:
                if self._sides_done_at is None:
                    self._sides_done_at = self.anim_time
                elif (self.anim_time - self._sides_done_at
                      < SIDES_HOLD_TIME + SIDES_FADE_TIME):
                    animating = True

        if self._status_kind():
            if self.anim_time - self._status_last >= STATUS_FRAME_TIME:
                self._status_last = self.anim_time
                animating = True

        hint_alpha = self._hint_alpha()
        if hint_alpha > 0.0:
            if self.anim_time - self._hint_last >= HINT_FRAME_TIME:
                self._hint_last = self.anim_time
                animating = True
        elif self._hint_last >= 0.0:
            self._hint_last = -1.0
            animating = True

        if self.flicker_timer > 0:
            self.flicker_timer = max(0.0, self.flicker_timer - dt)
            animating = True
        if self.is_playing:
            if SETTINGS.get("mode", "classic") == "vinyl":
                speed = float(SETTINGS.get("vinyl_speed", 4)) * 12.0
                self.vinyl_angle = (self.vinyl_angle + dt * speed) % 360.0
                animating = True
            if SETTINGS.get("anim_glow", False) or SETTINGS.get("anim_pulse", False):
                if self.anim_time - self._glow_last >= GLOW_FRAME_TIME:
                    self._glow_last = self.anim_time
                    animating = True
        if self.wave_offsets:
            step = 90.0 * dt
            still = False
            for k, v in self.wave_offsets.items():
                if v > 0:
                    self.wave_offsets[k] = max(0.0, v - step)
                    still = still or self.wave_offsets[k] > 0
            animating = animating or still
        return animating

    def start_line(self, text):
        """Switch to a new line: the new one slides in and fills as it is sung."""
        self.prev_text = self.current_text
        self.current_text = text
        self.visible_len = 0
        self.last_visible_len = 0
        self.wave_offsets = {}
        self.anim_t = 0.0
        self.word_pop_t0 = self.anim_time
        self._words_done = 0
        self._word_pop_times = {}
        self._words_scroll = 0.0
        self._words_scroll_target = 0.0
        self._words_scroll_from = 0.0
        self._words_scroll_t0 = -1.0
        self._sides_times = {}
        self._sides_done_at = None
        if SETTINGS.get("anim_flicker", False) and text:
            self.flicker_timer = 0.4


    def _fit_font(self, font, text, max_width):
        key = (text, font.family(), font.pointSize(), max_width)
        cached = self._fit_cache.get(key)
        if cached is not None:
            return QFont(cached)
        f = QFont(font)
        fm = font_metrics(f)
        while fm.horizontalAdvance(text) > max_width and f.pointSize() > 10:
            f.setPointSize(f.pointSize() - 1)
            fm = font_metrics(f)
        if len(self._fit_cache) > 32:
            self._fit_cache.clear()
        self._fit_cache[key] = QFont(f)
        return f

    @staticmethod
    def _glow_layers(painter, path, color, n):
        """Legacy fallback: strokes are cheap but visibly banded, only used if blur fails."""
        base = min(0.5, 2.2 / n)
        for i in range(n, 0, -1):
            c = QColor(color)
            c.setAlphaF(base * (1.0 - (i - 1) / n))
            pen = QPen(c)
            pen.setWidthF(i * 2.0)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)

    @staticmethod
    def _blur_pixmap(pixmap, radius):
        """Real gaussian-ish blur through Qt's own effect: no banding, fast enough to cache."""
        scene = QGraphicsScene()
        item = QGraphicsPixmapItem(pixmap)
        effect = QGraphicsBlurEffect()
        effect.setBlurRadius(max(0.5, float(radius)))
        effect.setBlurHints(QGraphicsBlurEffect.BlurHint.QualityHint)
        item.setGraphicsEffect(effect)
        scene.addItem(item)
        out = QPixmap(pixmap.size())
        out.fill(Qt.GlobalColor.transparent)
        painter = QPainter(out)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        scene.render(painter, QRectF(out.rect()), QRectF(pixmap.rect()))
        painter.end()
        return out

    def _get_glow_pixmap(self, text, font, color_hex, size):
        """Soft halo rendered once per string at half resolution and cached.

        The old version stacked a dozen stroked copies of the glyphs, which showed as hard
        grey rings around the letters; this one blurs a solid mask, so the glow is smooth.
        Returns ``(pixmap, margin, target)``; ``target`` is the logical rect to draw it in.
        """
        if not text:
            return None, 0, QRectF()
        dpr = self.devicePixelRatioF()
        key = (text, font.family(), font.pointSize(), font.weight(), color_hex, size,
               round(dpr, 2))
        hit = self._glow_cache.get(key)
        if hit is not None:
            return hit
        q = 0.5
        margin = size * 2 + 8
        fm = font_metrics(font)
        w = fm.horizontalAdvance(text) + 2 * margin
        h = fm.height() + 2 * margin
        mask = QPixmap(max(1, int(w * q)), max(1, int(h * q)))
        mask.fill(Qt.GlobalColor.transparent)
        p = QPainter(mask)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.scale(q, q)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(color_hex))
        path = QPainterPath()
        path.addText(float(margin), float(margin + fm.ascent()), font, text)
        p.drawPath(path)
        p.end()
        pm = self._blur_pixmap(mask, size * 1.3 + 0.8)
        if len(self._glow_cache) >= 48:
            self._glow_cache.clear()
        result = (pm, margin, QRectF(0.0, 0.0, w, h))
        self._glow_cache[key] = result
        return result

    def _cover_pixmap(self, target):
        """Square, centre-cropped album art, cached per size."""
        if self.cover_pm is None:
            return None
        dpr = self.devicePixelRatioF()
        key = (id(self.cover_pm), target, round(dpr, 2))
        if key != self._cover_key:
            side = max(32, int(target * dpr))
            scaled = self.cover_pm.scaled(
                side, side,
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation)
            x = max(0, (scaled.width() - side) // 2)
            y = max(0, (scaled.height() - side) // 2)
            cropped = scaled.copy(x, y, side, side)
            cropped.setDevicePixelRatio(dpr)
            self._cover_key, self._cover_scaled = key, cropped
        return self._cover_scaled


    def paintEvent(self, event):
        mode = SETTINGS.get("mode", "classic")
        if not self.is_playing and (mode != "vinyl" or not (self.current_text or self.lines)):
            return

        animating = self.anim_t < 1.0
        if not (self.current_text or self.lines) and not animating and mode != "vinyl":
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)

        bg_hex = SETTINGS.get("bg_color", "#000000")
        bg_alpha = int(SETTINGS.get("bg_opacity", 0) * 2.55)
        if bg_alpha > 0:
            bg_color = QColor(bg_hex)
            bg_color.setAlpha(bg_alpha)
            painter.fillRect(self.rect(), bg_color)

        base_size = max(10, int(SETTINGS.get("font_size", 36)))
        family = SETTINGS.get("font_family", "Segoe UI")
        base_font = cached_font(family, base_size, QFont.Weight.Bold)
        text_alpha = int(SETTINGS.get("text_opacity", 100) * 2.55)

        is_error = "Ошибка" in self.current_text
        is_not_found = "не найден" in self.current_text
        font_color = QColor("#f38ba8") if is_error else QColor(SETTINGS.get("text_color", "#FFFFFF"))
        opacity_k = text_alpha / 255.0

        area = QRect(self.rect())
        self._vinyl_anchor = None
        self._vinyl_box = None
        self._sides_avoid = QRectF()

        if SETTINGS.get("anim_pulse", False):
            s = 1.0 + 0.06 * (math.sin(self.anim_time * 3) * 0.5 + 0.5)
            c = QPointF(area.center())
            painter.save()
            painter.translate(c)
            painter.scale(s, s)
            painter.translate(-c)

        if mode == "vinyl":
            area = self._paint_vinyl(painter, area, family)

        self._paint_text_block(painter, area, base_font, base_size, family,
                               text_alpha, opacity_k, font_color,
                               is_error, is_not_found)

        if SETTINGS.get("anim_pulse", False):
            painter.restore()

    def mousePressEvent(self, event):
        if (event.button() == Qt.MouseButton.LeftButton
                and not self._hint_btn.isEmpty()
                and self._hint_btn.contains(event.position())
                and self._hint_alpha() > 0.0):
            self.dismiss_hint()
            event.accept()
            return
        event.ignore()


    def _status_kind(self):
        """Service messages shown instead of lyrics: loading / not found / error."""
        text = self.current_text or ""
        if not text:
            return None
        if "Ошибка" in text:
            return "error"
        if "не найден" in text:
            return "missing"
        if text.strip().startswith("Поиск"):
            return "search"
        return None

    def _paint_status(self, painter, area, base_font, status, opacity_k):
        """Animated service message: a spinner for the search, pulsing amber/red for problems."""
        if status == "search":
            text = "Поиск текста" + "." * (1 + int(self.anim_time * 3) % 3)
            color = QColor(SETTINGS.get("text_color", "#FFFFFF"))
            color.setAlphaF(0.9)
            pulse = 1.0
        elif status == "missing":
            text = self.current_text
            color = QColor("#FFFFFF")
            pulse = 0.7 + 0.3 * (0.5 + 0.5 * math.sin(self.anim_time * 4.0))
        else:
            text = self.current_text
            color = QColor("#F38BA8")
            pulse = 0.85 + 0.15 * (0.5 + 0.5 * math.sin(self.anim_time * 3.0))

        size = max(12, int(base_font.pointSize() * 0.62))
        font = self._fit_font(cached_font(base_font.family(), size, QFont.Weight.Bold),
                              text, max(80, area.width() - 60))
        fm = font_metrics(font)
        draw_rect = QRectF(area)

        box = self._vinyl_box
        if box is not None and area.intersects(box.toRect()):
            gap = 12.0
            top_room = box.top() - area.top() - gap
            left_room = box.left() - area.left() - gap
            right_room = area.right() - box.right() - gap
            if top_room >= fm.height() + 2:
                draw_rect = QRectF(area.left(), area.top(), area.width(), top_room)
            elif max(left_room, right_room) >= min(area.width() * 0.45, 320):
                if left_room >= right_room:
                    draw_rect = QRectF(area.left(), box.top(), left_room, box.height())
                else:
                    draw_rect = QRectF(box.right() + gap, box.top(),
                                       right_room, box.height())
            font = self._fit_font(
                cached_font(base_font.family(), size, QFont.Weight.Bold),
                text, max(80, int(draw_rect.width()) - 12))
            fm = font_metrics(font)
            while font.pointSize() > 9 and fm.height() > draw_rect.height() - 2:
                font.setPointSize(font.pointSize() - 1)
                fm = font_metrics(font)

        painter.setFont(font)
        painter.setOpacity(max(0.0, min(1.0, pulse * opacity_k)))
        painter.setPen(color)
        painter.drawText(draw_rect, Qt.AlignmentFlag.AlignCenter, text)

        if status == "search":
            radius = max(7.0, fm.height() * 0.30)
            cx = draw_rect.center().x() - fm.horizontalAdvance(text) / 2.0 - radius * 1.55
            cy = draw_rect.center().y()
            pen = QPen(color)
            pen.setWidthF(max(2.0, radius * 0.30))
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            start = -int(self.anim_time * 320) % 360
            painter.drawArc(QRectF(cx - radius, cy - radius, radius * 2, radius * 2),
                            start * 16, 260 * 16)
        painter.setOpacity(1.0)

    def _paint_text_block(self, painter, area, base_font, base_size, family,
                          text_alpha, opacity_k, font_color, is_error, is_not_found):
        count = max(1, min(7, int(SETTINGS.get("lines_visible", 3))))
        width_pct = max(20, min(100, int(SETTINGS.get("text_width", 80))))
        max_width = max(80, int(area.width() * width_pct / 100) - 20)

        status = self._status_kind()
        if status:
            self._paint_status(painter, area, base_font, status, opacity_k)
            return

        ease = ease_out_cubic(self.anim_t)

        if (SETTINGS.get("anim_mode", "line") == "sides" and self.current_text
                and not is_error and not is_not_found):
            self._draw_sides_words(painter, area, base_font, max_width,
                                   opacity_k, font_color)
            self._draw_unsynced_hint(painter, area, family, base_size)
            return

        if (SETTINGS.get("anim_mode", "line") == "words" and self.current_text
                and not is_error and not is_not_found):
            self._draw_words_column(painter, area, base_font, max_width, text_alpha,
                                    opacity_k, font_color, ease)
            self._draw_unsynced_hint(painter, area, family, base_size)
            return

        fm = font_metrics(base_font)
        line_h = fm.height() * 1.25
        if line_h * count > area.height() - 6:
            line_h = max(12.0, (area.height() - 6) / count)
        block_top = area.center().y() - (line_h * count) / 2.0

        cur_row = count // 2
        idx = self.index
        first = 0 if idx == IDX_UNSET else idx - cur_row

        slide = SETTINGS.get("slide_from", "bottom")
        dist = float(SETTINGS.get("slide_dist", 28))
        dy = dx = 0.0
        if slide == "bottom":
            dy = (1.0 - ease) * dist
        elif slide == "top":
            dy = -(1.0 - ease) * dist
        elif slide == "left":
            dx = -(1.0 - ease) * dist
        elif slide == "right":
            dx = (1.0 - ease) * dist

        if count == 1 and self.prev_text and self.anim_t < 1.0:
            font = self._fit_font(base_font, self.prev_text, max_width)
            painter.setFont(font)
            painter.setPen(font_color)
            painter.setOpacity(0.45 * (1.0 - ease) * opacity_k)
            r = QRect(area)
            r.translate(0, int(-30 * ease))
            painter.drawText(r, Qt.AlignmentFlag.AlignCenter, self.prev_text)
            painter.setOpacity(1.0)

        for row in range(count):
            li = first + row
            text = ""
            is_current = False
            if idx == IDX_UNSET:
                is_current = row == cur_row
                text = self.current_text if is_current else ""
            else:
                if 0 <= li < len(self.lines):
                    text = self.lines[li][1]
                elif li == idx:
                    text = self.current_text
                is_current = li == idx

            if not text:
                continue

            rect = QRect(int(area.left() + dx), int(block_top + row * line_h + dy),
                         area.width(), int(line_h))

            if is_current:
                if SETTINGS.get("anim_mode", "line") == "pull" and self.anim_t < 1.0:
                    self._draw_pull_line(painter, rect, base_font, max_width, text_alpha,
                                         opacity_k, font_color, ease, text)
                else:
                    self._draw_current_line(painter, rect, base_font, max_width, text_alpha,
                                            opacity_k, font_color, ease, text)
            else:
                size = max(10, int(base_size * 0.72))
                font = self._fit_font(cached_font(family, size, QFont.Weight.Bold), text, max_width)
                if idx != IDX_UNSET and li < idx:
                    color = QColor(font_color)
                    color.setAlpha(int(text_alpha * 0.45))
                else:
                    color = QColor(SETTINGS.get("future_color", "#555555"))
                    color.setAlpha(int(text_alpha * 0.75))
                painter.setFont(font)
                painter.setPen(color)
                painter.setOpacity(1.0)
                painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)

        self._draw_unsynced_hint(painter, area, family, base_size)

    def _draw_unsynced_hint(self, painter, area, family, base_size):
        """Bottom caption for approximate timings: soft blink, × to dismiss, auto-hide."""
        alpha = self._hint_alpha()
        if alpha <= 0.0:
            self._hint_btn = QRectF()
            return

        font = cached_font(family, max(9, base_size // 3))
        painter.setFont(font)
        fm = font_metrics(font)
        btn = QRectF(area.right() - 24, area.bottom() - 24, 18, 18)
        self._hint_btn = btn
        text_rect = QRectF(area.left(), area.bottom() - fm.height() - 6,
                           max(40, area.width() - 30), fm.height())

        painter.setOpacity(0.8 * alpha)
        painter.setPen(QColor("#F38BA8"))
        painter.drawText(text_rect,
                         Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                         "≈ текст без синхронизации (примерный тайминг)")

        painter.setOpacity(alpha)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 110))
        painter.drawRoundedRect(btn, 4.5, 4.5)
        pen = QPen(QColor(243, 139, 168, int(230 * alpha)))
        pen.setWidthF(1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawLine(QPointF(btn.left() + 5, btn.top() + 5),
                         QPointF(btn.right() - 5, btn.bottom() - 5))
        painter.drawLine(QPointF(btn.right() - 5, btn.top() + 5),
                         QPointF(btn.left() + 5, btn.bottom() - 5))
        painter.setOpacity(1.0)

    def _sides_layout(self, text, area, base_font):
        """Scatter the words across the whole area.

        Each word gets its own random spot (top, bottom-left, bottom-right, ...) and
        flies in from the left, the right or the top.  The spots are rejection-sampled
        over the whole area and must be visibly apart vertically, so a line never ends
        up on a single row.  The layout is deterministic for a given line, so a word
        never teleports between frames.
        """
        avoid = QRectF(self._sides_avoid)
        key = (text, area.width(), area.height(),
               base_font.family(), base_font.pointSize(),
               round(avoid.x()), round(avoid.y()),
               round(avoid.width()), round(avoid.height()))
        if key == self._sides_cache_key:
            return self._sides_cache_rows

        words = [(m.group(), m.start()) for m in re.finditer(r"\S+", text)]
        rows = []
        if words:
            seed = 0
            for index, char in enumerate(text):
                seed = (seed * 131 + ord(char) + index) & 0xFFFFFFFF
            rng = random.Random(seed)

            pad_x = max(10.0, area.width() * 0.035)
            pad_y = min(26.0, max(12.0, area.height() * 0.055))
            avail_w = max(80.0, area.width() - 2.0 * pad_x)
            avail_h = max(40.0, area.height() - 2.0 * pad_y)

            n = len(words)
            layout_font = QFont(base_font)
            free_area = max(1.0, avail_w * avail_h
                            - max(0.0, avoid.width() * avoid.height()))
            while layout_font.pointSize() > 10:
                fm = font_metrics(layout_font)
                word_h = float(fm.height())
                need = 0.0
                widest = 0.0
                for word, _ in words:
                    w = float(fm.horizontalAdvance(word))
                    widest = max(widest, w)
                    need += (w + 14.0) * (word_h + 14.0)
                if widest <= avail_w and need <= free_area * 0.42:
                    break
                layout_font.setPointSize(layout_font.pointSize() - 1)

            sides = [0, 1, 2]
            rng.shuffle(sides)
            min_spread = avail_h * 0.55

            scales = [rng.uniform(0.62, 1.0) for _ in range(n)]
            scales[rng.randrange(n)] = 1.0

            def word_font_for(index):
                font = QFont(layout_font)
                font.setPointSize(max(10, int(round(
                    layout_font.pointSize() * scales[index]))))
                return self._fit_font(font, words[index][0], avail_w)

            def scatter_once():
                order = list(range(n))
                rng.shuffle(order)
                low_index = order[0] if n >= 2 else None
                high_index = order[1] if n >= 2 else None
                placed = []
                spots = {}
                for index in order:
                    word = words[index][0]
                    word_font = word_font_for(index)
                    word_fm = font_metrics(word_font)
                    width = float(word_fm.horizontalAdvance(word))
                    height = float(word_fm.height())
                    lo_x = pad_x
                    hi_x = pad_x + max(0.0, avail_w - width)
                    span_y = max(0.0, avail_h - height)
                    lo_y = pad_y
                    hi_y = pad_y + span_y
                    if index == low_index:
                        lo_y = pad_y + span_y * 0.70
                    elif index == high_index:
                        hi_y = pad_y + span_y * 0.30
                    best = None
                    best_score = None
                    for _ in range(320):
                        x = rng.uniform(lo_x, hi_x)
                        y_top = rng.uniform(lo_y, hi_y)
                        rect = QRectF(x - 4.0, y_top - 4.0,
                                      width + 8.0, height + 8.0)
                        if not avoid.isNull() and rect.intersects(avoid):
                            continue
                        overlap = 0.0
                        for other in placed:
                            if rect.intersects(other):
                                part = rect.intersected(other)
                                overlap += part.width() * part.height()
                        if overlap <= 0.0:
                            best = (x, y_top, width, height, word_font)
                            break
                        if best_score is None or overlap < best_score:
                            best_score = overlap
                            best = (x, y_top, width, height, word_font)
                    if best is None:
                        return None
                    x, y_top, width, height, word_font = best
                    placed.append(QRectF(x - 4.0, y_top - 4.0,
                                         width + 8.0, height + 8.0))
                    spots[index] = (x, y_top + height / 2.0, word_font, width)
                return spots

            best_spots = None
            best_score = -1.0
            for _ in range(30):
                spots = scatter_once()
                if not spots:
                    continue
                ys = [spot[1] for spot in spots.values()]
                xs = [spot[0] for spot in spots.values()]
                spread = max(ys) - min(ys) if len(ys) > 1 else min_spread
                wide = max(xs) - min(xs) if len(xs) > 1 else avail_w
                score = spread + 0.25 * wide
                if best_spots is None or score > best_score:
                    best_spots, best_score = spots, score
                if spread >= min_spread and wide >= avail_w * 0.35:
                    break
            if best_spots is None:
                best_spots = {}
                for index in range(n):
                    word_font = word_font_for(index)
                    width = float(font_metrics(word_font).horizontalAdvance(
                        words[index][0]))
                    y = pad_y + (index + 0.5) * avail_h / max(1, n)
                    best_spots[index] = (pad_x, y, word_font, width)

            for index in range(n):
                x, y, word_font, width = best_spots[index]
                rows.append((index, words[index][0], words[index][1], x, y,
                             sides[index % len(sides)], word_font, width))
            rows.sort(key=lambda row: row[0])

        self._sides_cache_key = key
        self._sides_cache_rows = rows
        return rows

    def _draw_sides_word(self, painter, word, x, y_center, side, font, width,
                         progress, alpha, opacity_k, font_color, drift=0.0):
        """One scattered word: it shoots in from its side with a small overshoot."""
        if alpha <= 0.01:
            return
        fm = font_metrics(font)
        baseline = y_center + (fm.ascent() - fm.descent()) / 2.0
        snap = ease_out_back(max(0.0, min(1.0, progress)))
        if side == 2:
            dx = 0.0
            dy = -(1.0 - snap) * (fm.height() + 150.0) - drift
        else:
            direction = -1.0 if side == 0 else 1.0
            dx = (1.0 - snap) * (width + 150.0) * direction + direction * drift
            dy = (1.0 - snap) * 8.0
        scale = 0.90 + 0.10 * max(0.0, min(1.0, snap))
        fade = min(1.0, max(0.0, progress * 5.0)) * alpha
        baseline += dy
        x += dx

        path = QPainterPath()
        path.addText(x, baseline, font, word)

        center_x = x + width / 2.0
        center_y = baseline - fm.ascent() / 2.0
        painter.save()
        painter.translate(center_x, center_y)
        painter.scale(scale, scale)
        painter.translate(-center_x, -center_y)

        if SETTINGS.get("anim_glow", False) and progress < 0.92:
            glow_hex = SETTINGS.get("glow_color", "#89b4fa")
            glow_size = max(1, int(SETTINGS.get("glow_size", 3)))
            pm, margin, target = self._get_glow_pixmap(word, font, glow_hex, glow_size)
            if pm is not None:
                pulse = 0.5 + 0.5 * math.sin(self.glow_phase)
                painter.setOpacity(fade * opacity_k * (0.45 + 0.35 * pulse))
                painter.drawPixmap(
                    QRectF(x - margin, baseline - fm.ascent() - margin,
                           target.width(), target.height()),
                    pm, QRectF(pm.rect()))
                painter.setOpacity(1.0)

        outline_w = int(SETTINGS.get("outline_width", 2))
        if outline_w > 0:
            outline_color = QColor(SETTINGS.get("outline_color", "#000000"))
            outline_color.setAlpha(int(255 * opacity_k))
            outline_pen = QPen(outline_color)
            outline_pen.setWidth(outline_w)
            outline_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(outline_pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setOpacity(fade)
            painter.drawPath(path)

        painter.setPen(Qt.PenStyle.NoPen)
        fill_brush = font_color
        if SETTINGS.get("use_gradient", True):
            fill_brush = make_text_gradient(x, baseline - 20,
                                            x + width, baseline + 20)
        painter.setBrush(fill_brush)
        painter.setOpacity(opacity_k * fade)
        painter.drawPath(path)
        painter.restore()

    def _draw_sides_words(self, painter, area, base_font, max_width,
                          opacity_k, font_color):
        """The current line as scattered words; unsung colour is not used at all."""
        text = self.current_text or ""
        if not text:
            return

        painter.save()
        painter.setClipRect(area)

        if self.prev_text and self.anim_t < 1.0:
            ghost = max(0.0, 1.0 - ease_out_cubic(self.anim_t))
            for (index, word, _start, x, y, side, font, width) in \
                    self._sides_layout(self.prev_text, area, base_font):
                self._draw_sides_word(painter, word, x, y, side, font, width,
                                      1.0, ghost * 0.5, opacity_k, font_color,
                                      drift=26.0)

        line_alpha = 1.0
        if self._sides_done_at is not None:
            elapsed = self.anim_time - self._sides_done_at
            if elapsed > SIDES_HOLD_TIME:
                line_alpha = max(0.0, 1.0 - (elapsed - SIDES_HOLD_TIME) / SIDES_FADE_TIME)
        if line_alpha <= 0.0:
            painter.restore()
            return

        for (index, word, word_start, x, y, side, font, width) in \
                self._sides_layout(text, area, base_font):
            t0 = self._sides_times.get(index)
            if t0 is None:
                if word_start >= self.visible_len:
                    continue
                t0 = self.anim_time - SIDES_POP_TIME
            progress = max(0.0, min(1.0, (self.anim_time - t0) / SIDES_POP_TIME))
            self._draw_sides_word(painter, word, x, y, side, font, width,
                                  progress, line_alpha, opacity_k, font_color)
        painter.restore()

    def _draw_pull_line(self, painter, rect, base_font, max_width, text_alpha,
                        opacity_k, font_color, ease, text):
        """Pull the line out of the record, then let it settle into its final position."""
        text = text or self.current_text
        if not text:
            return
        font = self._fit_font(base_font, text, max_width)
        fm = font_metrics(font)
        text_w = max(1.0, float(fm.horizontalAdvance(text)))
        final_x = rect.center().x() - text_w / 2.0

        if self._vinyl_anchor is not None:
            anchor_x = float(self._vinyl_anchor.x())
        else:
            anchor_x = final_x - min(100.0, max(32.0, text_w * 0.25))
        travel = max(0.0, final_x - anchor_x)

        p = max(0.0, min(1.0, self.anim_t))
        smooth = p * p * (3.0 - 2.0 * p)
        reveal = min(1.0, smooth / 0.70) if smooth > 0.0 else 0.0
        slide = max(0.0, min(1.0, (smooth - 0.45) / 0.55))
        slide = ease_out_cubic(slide)
        shift = travel * (1.0 - slide)
        draw_rect = rect.translated(-int(round(shift)), 0)
        draw_start = draw_rect.center().x() - text_w / 2.0
        leading_x = draw_start + text_w * reveal

        thread_y = min(rect.bottom() - 2.0,
                       rect.center().y() + fm.height() * 0.55 + 6.0)
        if reveal > 0.0 and leading_x > anchor_x + 3.0:
            self._draw_pull_thread(painter, QPointF(anchor_x, thread_y),
                                   QPointF(leading_x, thread_y), smooth, font_color)

        self._draw_current_line(painter, draw_rect, base_font, max_width, text_alpha,
                                opacity_k, font_color, 1.0, text, reveal=reveal)

    def _draw_pull_thread(self, painter, start, end, ease, color):
        """Straight glowing cord below the text: no slash across the letters."""
        alpha = int(210 * max(0.0, 1.0 - ease) ** 1.1)
        if alpha <= 2 or end.x() <= start.x() + 2.0:
            return

        painter.save()
        glow_pen = QPen(QColor(color.red(), color.green(), color.blue(),
                               max(1, alpha // 10)), 5.0)
        glow_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(glow_pen)
        painter.drawLine(start, end)

        grad = QLinearGradient(start, end)
        grad.setColorAt(0.0, QColor(color.red(), color.green(), color.blue(), alpha // 5))
        grad.setColorAt(0.35, QColor(color.red(), color.green(), color.blue(), alpha // 2))
        grad.setColorAt(1.0, QColor(color.red(), color.green(), color.blue(), alpha))
        pen = QPen(QBrush(grad), 1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawLine(start, end)

        dot = QColor(color)
        dot.setAlpha(alpha)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(dot)
        painter.drawEllipse(end, 2.4, 2.4)
        painter.restore()

    def _clip_intersect(self, painter, rect):
        """Narrow the painter clip instead of replacing it (needed by the pull animation)."""
        current = painter.clipBoundingRect()
        if current.isValid():
            rect = rect.intersected(current)
        painter.setClipRect(rect)

    def _draw_current_line(self, painter, rect, base_font, max_width, text_alpha,
                           opacity_k, font_color, fade, text, reveal=None):
        text = text or self.current_text
        if not text:
            return

        font = self._fit_font(base_font, text, max_width)
        painter.setFont(font)
        fm = font_metrics(font)
        full_width = float(fm.horizontalAdvance(text))
        start_x = rect.center().x() - full_width / 2.0
        text_y = rect.center().y() + (fm.ascent() - fm.descent()) / 2.0
        visible_chars = max(0, min(self.visible_len, len(text)))
        fade = max(0.0, min(1.0, fade))

        outer_saved = False
        if reveal is not None:
            reveal = max(0.0, min(1.0, reveal))
            if reveal <= 0.0:
                return
            painter.save()
            outer_saved = True
            self._clip_intersect(painter, QRectF(start_x - 2.0, rect.top() - 60.0,
                                                 max(2.0, full_width * reveal + 4.0),
                                                 rect.height() + 120.0))

        is_error = "Ошибка" in text
        is_not_found = "не найден" in text

        wave_on = SETTINGS.get("anim_wave", False)
        sung_path = QPainterPath()
        future_path = QPainterPath()
        char_places = []
        if wave_on:
            current_x = start_x
            prefix_w = 0.0
            for i, char in enumerate(text):
                advance = fm.horizontalAdvance(char)
                if i < visible_chars:
                    off = self.wave_offsets.setdefault(i, 15.0)
                    sung_path.addText(current_x, text_y - off, font, char)
                    char_places.append((char, current_x, off))
                else:
                    future_path.addText(current_x, text_y, font, char)
                if i < visible_chars:
                    prefix_w += advance
                current_x += advance
            boundary_x = start_x + prefix_w
        else:
            full_path = QPainterPath()
            full_path.addText(start_x, text_y, font, text)
            sung_path = full_path
            future_path = full_path
            boundary_x = start_x + (full_width if visible_chars >= len(text)
                                    else fm.horizontalAdvance(text[:visible_chars]))

        if not SETTINGS.get("hide_future", False) and visible_chars < len(text):
            future_color = QColor(SETTINGS.get("future_color", "#555555"))
            future_color.setAlpha(text_alpha)
            painter.save()
            self._clip_intersect(painter, QRectF(boundary_x - 0.6, rect.top() - 50,
                                                 (start_x + full_width - boundary_x) + 1.2,
                                                 rect.height() + 100))
            painter.setOpacity(fade)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(future_color)
            painter.drawPath(future_path)
            painter.restore()

        if visible_chars <= 0:
            if outer_saved:
                painter.restore()
            return

        vis_width = max(0.0, boundary_x - start_x)

        flicker_opacity = 1.0
        if (SETTINGS.get("anim_flicker", False) and self.flicker_timer > 0
                and int(self.flicker_timer * 20) % 2 == 0):
            flicker_opacity = 0.1

        painter.save()

        if SETTINGS.get("anim_glow", False):
            glow_hex = SETTINGS.get("glow_color", "#89b4fa")
            glow_size = max(1, int(SETTINGS.get("glow_size", 3)))
            margin = glow_size * 2 + 8
            self._clip_intersect(painter, QRectF(start_x - margin, rect.top() - 50,
                                                 vis_width + margin, rect.height() + 100))
            pulse = 0.5 + 0.5 * math.sin(self.glow_phase)
            painter.setOpacity(fade * flicker_opacity * opacity_k * (0.55 + 0.45 * pulse))
            if wave_on:
                for char, cx, off in char_places:
                    if char.isspace():
                        continue
                    pm, m, target = self._get_glow_pixmap(char, font, glow_hex, glow_size)
                    if pm is not None:
                        painter.drawPixmap(
                            QRectF(cx - m, text_y - off - fm.ascent() - m,
                                   target.width(), target.height()),
                            pm, QRectF(pm.rect()))
            else:
                pm, m, target = self._get_glow_pixmap(text, font, glow_hex, glow_size)
                if pm is not None:
                    painter.drawPixmap(QRectF(start_x - m, text_y - fm.ascent() - m,
                                              target.width(), target.height()),
                                       pm, QRectF(pm.rect()))
            painter.setOpacity(1.0)

        self._clip_intersect(painter, QRectF(start_x, rect.top() - 50,
                                             vis_width, rect.height() + 100))

        outline_w = int(SETTINGS.get("outline_width", 2))
        if outline_w > 0:
            outline_color = QColor(SETTINGS.get("outline_color", "#000000"))
            outline_color.setAlpha(text_alpha)
            outline_pen = QPen(outline_color)
            outline_pen.setWidth(outline_w)
            outline_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(outline_pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setOpacity(fade)
            painter.drawPath(sung_path)

        painter.setPen(Qt.PenStyle.NoPen)
        fill_brush = font_color
        if SETTINGS.get("use_gradient", True) and not is_error:
            fill_brush = make_text_gradient(start_x, text_y - 20,
                                            start_x + full_width, text_y + 20)
        painter.setBrush(fill_brush)
        painter.setOpacity(opacity_k * flicker_opacity * fade)
        painter.drawPath(sung_path)

        painter.restore()
        if outer_saved:
            painter.restore()

    def _draw_words_column(self, painter, area, base_font, max_width, text_alpha,
                           opacity_k, font_color, fade):
        """Reels/Instagram look: rows of words stack top-down and every word pops in as it is sung.

        The font size and the block width are computed for the whole line (not for the rows that
        happen to be visible), so the column never resizes while the words appear — that resize
        was the main reason the animation looked jumpy. Rows also scroll with a short easing.
        """
        text = self.current_text or ""
        per_row = max(1, min(4, int(SETTINGS.get("words_per_row", 1))))
        rows_visible = max(1, min(8, int(SETTINGS.get("words_visible", 4))))

        cache_key = (text, per_row)
        if cache_key != self._words_cache_key:
            words = [(m.group(), m.start(), m.end()) for m in re.finditer(r"\S+", text)]
            rows = []
            for i in range(0, len(words), per_row):
                group = words[i:i + per_row]
                rows.append((" ".join(w[0] for w in group), group[0][1], i, group))
            self._words_cache_key = cache_key
            self._words_cache_rows = rows
            widest, widest_w = "", -1
            base_fm = font_metrics(base_font)
            for row_text, _start, _first, _words in rows:
                width = base_fm.horizontalAdvance(row_text)
                if width > widest_w:
                    widest, widest_w = row_text, width
            self._words_cache_widest = widest
        rows = self._words_cache_rows
        if not rows:
            return

        vis = max(0, min(self.visible_len, len(text)))
        current_row = 0
        for i, row in enumerate(rows):
            if vis > row[1]:
                current_row = i

        font = self._fit_font(base_font, self._words_cache_widest, max_width)
        fm = font_metrics(font)
        line_h = fm.height() * 1.06
        slots = min(rows_visible, len(rows))
        avail_h = max(40, area.height() - 10)
        if line_h * slots > avail_h:
            scale = avail_h / (line_h * slots)
            small = QFont(font)
            small.setPointSize(max(9, int(font.pointSize() * scale)))
            if small.pointSize() < font.pointSize():
                font = small
                fm = font_metrics(font)
                line_h = fm.height() * 1.06
        block_w = max(fm.horizontalAdvance(r[0]) for r in rows)
        left = area.center().x() - block_w / 2.0
        top = area.center().y() - line_h * slots / 2.0

        target = max(0, current_row - rows_visible + 1)
        if target != self._words_scroll_target:
            self._words_scroll_from = self._words_scroll
            self._words_scroll_target = target
            self._words_scroll_t0 = self.anim_time
        if self._words_scroll != self._words_scroll_target:
            k = min(1.0, max(0.0, (self.anim_time - self._words_scroll_t0) / WORD_POP_TIME))
            self._words_scroll = self._words_scroll_from + \
                (self._words_scroll_target - self._words_scroll_from) * ease_out_cubic(k)
            if k >= 1.0:
                self._words_scroll = float(self._words_scroll_target)

        slide = SETTINGS.get("slide_from", "bottom")
        dist = float(SETTINGS.get("slide_dist", 28)) * 0.8
        hide_future = SETTINGS.get("hide_future", False)
        future_color = QColor(SETTINGS.get("future_color", "#555555"))
        future_color.setAlpha(int(text_alpha * 0.8))
        outline_w = int(SETTINGS.get("outline_width", 2))
        outline_color = QColor(SETTINGS.get("outline_color", "#000000"))
        outline_color.setAlpha(text_alpha)
        fade = max(0.0, min(1.0, fade))
        flicker = 1.0
        if (SETTINGS.get("anim_flicker", False) and self.flicker_timer > 0
                and int(self.flicker_timer * 20) % 2 == 0):
            flicker = 0.1
        space_w = fm.horizontalAdvance(" ")
        painter.setFont(font)

        first = max(0, int(math.floor(self._words_scroll)))
        last = min(len(rows), first + rows_visible + 1)
        for row_index in range(first, last):
            _row_text, _start, first_word, row_words = rows[row_index]
            baseline = top + (row_index - self._words_scroll) * line_h + fm.ascent()
            x = left
            for offset, (word, start_w, _end_w) in enumerate(row_words):
                advance = fm.horizontalAdvance(word)
                word_index = first_word + offset
                if vis <= start_w:
                    if not hide_future:
                        painter.setOpacity(1.0)
                        painter.setPen(future_color)
                        painter.drawText(QPointF(x, baseline), word)
                    x += advance + space_w
                    continue

                t0 = self._word_pop_times.get(word_index, self.anim_time - WORD_POP_TIME)
                pop = max(0.0, min(1.0, (self.anim_time - t0) / WORD_POP_TIME))
                a = ease_out_cubic(pop)
                ox = oy = 0.0
                if slide == "top":
                    oy = -(1.0 - a) * dist
                elif slide == "bottom":
                    oy = (1.0 - a) * dist
                elif slide == "left":
                    ox = -(1.0 - a) * dist
                elif slide == "right":
                    ox = (1.0 - a) * dist

                scale = 0.78 + 0.22 * ease_out_back(pop)
                if word_index == self._words_done - 1:
                    oy -= 5.0 * math.sin(pop * math.pi)
                center_x = x + ox + advance / 2.0
                center_y = baseline + oy - fm.ascent() / 2.0

                painter.save()
                painter.translate(center_x, center_y)
                painter.scale(scale, scale)
                painter.translate(-center_x, -center_y)

                if word_index == self._words_done - 1 and pop < 1.0:
                    pm, margin, target = self._get_glow_pixmap(word, font, "#FFFFFF", 5)
                    if pm is not None:
                        flash = math.sin(pop * math.pi)
                        painter.setOpacity(flash * fade * flicker * opacity_k * 0.7)
                        painter.drawPixmap(QRectF(x + ox - margin,
                                                  baseline - fm.ascent() + oy - margin,
                                                  target.width(), target.height()),
                                           pm, QRectF(pm.rect()))
                        painter.setOpacity(1.0)

                if SETTINGS.get("anim_glow", False) and word_index == self._words_done - 1:
                    pm, margin, target = self._get_glow_pixmap(
                        word, font, SETTINGS.get("glow_color", "#89b4fa"),
                        max(1, int(SETTINGS.get("glow_size", 3))))
                    if pm is not None:
                        pulse = 0.5 + 0.5 * math.sin(self.glow_phase)
                        painter.setOpacity(a * fade * flicker * opacity_k * (0.55 + 0.45 * pulse))
                        painter.drawPixmap(QRectF(x + ox - margin,
                                                  baseline - fm.ascent() + oy - margin,
                                                  target.width(), target.height()),
                                           pm, QRectF(pm.rect()))
                        painter.setOpacity(1.0)

                path = QPainterPath()
                path.addText(x + ox, baseline + oy, font, word)
                if outline_w > 0:
                    pen = QPen(outline_color)
                    pen.setWidth(outline_w)
                    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
                    painter.setPen(pen)
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                    painter.setOpacity(a * fade)
                    painter.drawPath(path)
                painter.setPen(Qt.PenStyle.NoPen)
                if SETTINGS.get("use_gradient", True):
                    painter.setBrush(make_text_gradient(left, baseline - 20,
                                                        left + block_w, baseline + 20))
                else:
                    painter.setBrush(QBrush(font_color))
                painter.setOpacity(a * opacity_k * flicker * fade)
                painter.drawPath(path)
                painter.restore()
                x += advance + space_w


    def _paint_vinyl(self, painter, rect, family):
        if SETTINGS.get("anim_mode", "line") == "sides":
            return self._paint_vinyl_bottom(painter, rect, family)

        base_size = max(10, int(SETTINGS.get("font_size", 36)))
        caption = bool(SETTINGS.get("vinyl_caption", True))
        title_font = cached_font(family, max(11, int(base_size * 0.52)), QFont.Weight.Bold)
        artist_font = cached_font(family, max(9, int(base_size * 0.36)))
        fm_title, fm_artist = font_metrics(title_font), font_metrics(artist_font)
        caption_h = (fm_title.height() + fm_artist.height() + 12) if caption else 0
        disc_area_h = max(90, rect.height() - caption_h)

        width_pct = max(20, min(100, int(SETTINGS.get("text_width", 80))))
        max_text_w = int(rect.width() * width_pct / 100)
        gap = max(0, int(SETTINGS.get("vinyl_gap", 24)))

        diameter = max(90, min(int(SETTINGS.get("vinyl_size", 280)), disc_area_h - 12))

        fm = font_metrics(cached_font(family, base_size, QFont.Weight.Bold))
        samples = [self.current_text] + [t for _, t in self.lines if t]
        measured = max((fm.horizontalAdvance(t) for t in samples if t), default=0)
        text_w = min(max_text_w, max(240, measured + 24))

        avail = rect.width() - 16
        if diameter + gap + text_w > avail:
            text_w = max(160, avail - diameter - gap)
        if diameter + gap + text_w > avail:
            diameter = max(90, avail - gap - text_w)

        group_w = diameter + gap + text_w
        shift = int(rect.width() * float(SETTINGS.get("vinyl_shift", 0)) / 100.0)
        x0 = rect.left() + (rect.width() - group_w) / 2.0 + shift
        x0 = max(4.0, min(x0, rect.width() - group_w - 4.0))
        cx = x0 + diameter / 2.0
        cy = rect.top() + disc_area_h / 2.0
        self._vinyl_anchor = QPointF(cx + diameter / 2.0 - 1.0, cy)
        self._vinyl_box = QRectF(cx - diameter / 2.0, cy - diameter / 2.0,
                                 diameter, diameter)

        self._draw_vinyl(painter, cx, cy, diameter, family)
        if caption:
            self._draw_vinyl_caption(painter, cx, rect.top() + disc_area_h + 4,
                                     diameter * 1.8, title_font, artist_font)
        return QRect(int(x0 + diameter + gap), rect.top(), max(80, text_w), rect.height())

    def _paint_vinyl_bottom(self, painter, rect, family):
        """Sides mode: the record lies at the bottom centre and words fly around it."""
        base_size = max(10, int(SETTINGS.get("font_size", 36)))
        caption = bool(SETTINGS.get("vinyl_caption", True))
        title_font = cached_font(family, max(11, int(base_size * 0.52)), QFont.Weight.Bold)
        artist_font = cached_font(family, max(9, int(base_size * 0.36)))
        fm_title, fm_artist = font_metrics(title_font), font_metrics(artist_font)
        caption_h = self.vinyl_caption_height()

        usable_h = max(80.0, rect.height() - caption_h - 12.0)
        diameter = max(90, min(int(SETTINGS.get("vinyl_size", 280)),
                               int(usable_h), int(rect.width() * 0.34)))
        glow = max(0.0, min(1.0, float(SETTINGS.get("vinyl_glow", 60)) / 100.0))
        if SETTINGS.get("vinyl_style", "neon") == "neon" and glow > 0.01:
            diameter = max(90, min(diameter, int(usable_h / 1.20)))

        shift = int(rect.width() * float(SETTINGS.get("vinyl_shift", 0)) / 100.0)
        travel = max(0.0, (rect.width() - diameter) / 2.0 - 8.0)
        shift = int(max(-travel, min(travel, shift)))
        cx = rect.center().x() + shift
        cy = rect.top() + usable_h - diameter / 2.0 - 2.0
        self._vinyl_anchor = QPointF(cx, cy - diameter / 2.0 + 1.0)
        self._vinyl_box = QRectF(cx - diameter / 2.0, cy - diameter / 2.0,
                                 diameter, diameter)
        self._sides_avoid = self._vinyl_box.adjusted(-10.0, -10.0, 10.0, 10.0)

        self._draw_vinyl(painter, cx, cy, diameter, family)
        if caption:
            self._draw_vinyl_caption(painter, cx, rect.bottom() - caption_h + 4,
                                     min(rect.width() - 24,
                                         max(diameter * 2.2, rect.width() * 0.82)),
                                     title_font, artist_font)
        return QRect(rect.left(), rect.top(), rect.width(),
                     max(80, rect.height() - caption_h - 4))

    def _draw_vinyl_caption(self, painter, cx, top, max_width, title_font, artist_font):
        """Track title + artist under the disc, elided to the disc width."""
        text_color = QColor(SETTINGS.get("text_color", "#FFFFFF"))
        dim = QColor(SETTINGS.get("future_color", "#AAAAAA"))
        dim.setAlpha(220)
        title = (self.title or "").strip() or "—"
        artist = (self.artist or "").strip()
        fm_title, fm_artist = font_metrics(title_font), font_metrics(artist_font)
        left = cx - max_width / 2.0

        painter.setFont(title_font)
        painter.setPen(text_color)
        painter.setOpacity(1.0)
        elided = fm_title.elidedText(title, Qt.TextElideMode.ElideRight, int(max_width))
        painter.drawText(QRectF(left, top, max_width, fm_title.height()),
                         Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter, elided)

        if artist:
            painter.setFont(artist_font)
            painter.setPen(dim)
            elided = fm_artist.elidedText(artist, Qt.TextElideMode.ElideRight, int(max_width))
            painter.drawText(QRectF(left, top + fm_title.height() + 2, max_width,
                                    fm_artist.height()),
                             Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter, elided)

    def _draw_vinyl(self, painter, cx, cy, diameter, family):
        """Stylish neon disc: dark vinyl, rotating blue sheen and an electric rim."""
        r = diameter / 2.0
        accent = QColor(SETTINGS.get("vinyl_accent")
                        or SETTINGS.get("gradient_color2", "#89b4fa"))
        if not accent.isValid():
            accent = QColor("#89b4fa")
        style = SETTINGS.get("vinyl_style", "neon")
        glow_k = max(0.0, min(1.0, float(SETTINGS.get("vinyl_glow", 60)) / 100.0))
        neon = style == "neon"
        classic = style == "classic"
        pulse = 0.5 + 0.5 * math.sin(self.anim_time * 2.0)
        kick = max(0.0, 1.0 - self.anim_t) if self.anim_t < 1.0 else 0.0

        painter.save()
        shadow = QRadialGradient(QPointF(cx, cy), r * 1.12)
        shadow.setColorAt(0.80, QColor(0, 0, 0, 90))
        shadow.setColorAt(1.0, QColor(0, 0, 0, 0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(shadow)
        painter.drawEllipse(QPointF(cx, cy), r * 1.12, r * 1.12)

        if neon and glow_k > 0.01:
            halo = QRadialGradient(QPointF(cx, cy), r * 1.20)
            halo.setColorAt(0.72, QColor(accent.red(), accent.green(), accent.blue(), 0))
            halo.setColorAt(0.84, QColor(accent.red(), accent.green(), accent.blue(),
                                         int((16 + 12 * pulse + 60 * kick) * glow_k)))
            halo.setColorAt(0.92, QColor(accent.red(), accent.green(), accent.blue(),
                                         int((6 + 6 * pulse + 26 * kick) * glow_k)))
            halo.setColorAt(1.0, QColor(accent.red(), accent.green(), accent.blue(), 0))
            painter.setBrush(halo)
            painter.drawEllipse(QPointF(cx, cy), r * 1.20, r * 1.20)

        if neon and kick > 0.0:
            ring_r = r * (1.02 + 0.18 * (1.0 - kick))
            pen = QPen(QColor(accent.red(), accent.green(), accent.blue(),
                              int(120 * kick)),
                       max(1.0, r * 0.012))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(pen)
            painter.drawEllipse(QPointF(cx, cy), ring_r, ring_r)

        body = QRadialGradient(QPointF(cx - r * 0.30, cy - r * 0.36), r * 1.55)
        if classic:
            body.setColorAt(0.0, QColor("#8c8c96"))
            body.setColorAt(0.18, QColor("#2a2a31"))
            body.setColorAt(0.55, QColor("#111115"))
            body.setColorAt(1.0, QColor("#050506"))
        elif style == "minimal":
            body.setColorAt(0.0, QColor("#2c3340"))
            body.setColorAt(0.35, QColor("#12151c"))
            body.setColorAt(1.0, QColor("#050609"))
        else:
            body.setColorAt(0.0, QColor("#eef2ff"))
            body.setColorAt(0.15, QColor("#3a4152"))
            body.setColorAt(0.45, QColor("#141821"))
            body.setColorAt(0.78, QColor("#0a0c12"))
            body.setColorAt(1.0, QColor("#04050a"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(body)
        painter.drawEllipse(QPointF(cx, cy), r, r)

        if style != "minimal":
            sheen = QConicalGradient(QPointF(cx, cy), -self.vinyl_angle)
            glare = (accent.red(), accent.green(), accent.blue()) if neon \
                else (255, 255, 255)
            sheen.setColorAt(0.00, QColor(255, 255, 255, 0))
            sheen.setColorAt(0.07, QColor(glare[0], glare[1], glare[2], 56))
            sheen.setColorAt(0.14, QColor(255, 255, 255, 34))
            sheen.setColorAt(0.26, QColor(glare[0], glare[1], glare[2], 10))
            sheen.setColorAt(0.50, QColor(255, 255, 255, 0))
            sheen.setColorAt(0.63, QColor(glare[0], glare[1], glare[2], 26))
            sheen.setColorAt(0.74, QColor(255, 255, 255, 40))
            sheen.setColorAt(0.88, QColor(glare[0], glare[1], glare[2], 12))
            sheen.setColorAt(1.00, QColor(255, 255, 255, 0))
            painter.setBrush(sheen)
            painter.drawEllipse(QPointF(cx, cy), r, r)

        painter.setBrush(Qt.BrushStyle.NoBrush)
        groove_count = 3 if style == "minimal" else (17 if classic else 15)
        for k in range(1, groove_count + 1):
            rr = r * (0.56 + (0.39 / groove_count) * k)
            if rr >= r * 0.995:
                break
            pen = QPen(QColor(210, 225, 255, 14 + (k % 3) * 7),
                       max(0.8, r * 0.005))
            painter.setPen(pen)
            painter.drawEllipse(QPointF(cx, cy), rr, rr)

        if neon:
            for k, (span, width_k, alpha) in enumerate(
                    ((52, 0.016, 175), (22, 0.010, 120), (78, 0.007, 80),
                     (30, 0.005, 55))):
                start_deg = (self.vinyl_angle * (1.0 + 0.35 * k) + 35 + k * 104) % 360
                pen = QPen(QColor(accent.red(), accent.green(), accent.blue(),
                                  int(alpha * (0.60 + 0.40 * pulse) * glow_k)),
                           max(1.0, r * width_k))
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                painter.setPen(pen)
                rr = r * (0.69 + 0.075 * k)
                painter.drawArc(QRectF(cx - rr, cy - rr, rr * 2, rr * 2),
                                int(start_deg * 16), int(span * 16))

        label_r = r * 0.56 if not classic else r * 0.58
        cover = self._cover_pixmap(int(label_r * 2))
        if cover is not None:
            painter.save()
            clip = QPainterPath()
            clip.addEllipse(QPointF(cx, cy), label_r, label_r)
            painter.setClipPath(clip)
            painter.translate(cx, cy)
            painter.rotate(self.vinyl_angle)
            size = cover.deviceIndependentSize()
            painter.drawPixmap(QPointF(-size.width() / 2, -size.height() / 2), cover)
            painter.restore()
        else:
            grad = QLinearGradient(cx - label_r, cy - label_r, cx + label_r, cy + label_r)
            grad.setColorAt(0, accent)
            grad.setColorAt(1, QColor(SETTINGS.get("text_color", "#FFFFFF")))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(grad)
            painter.drawEllipse(QPointF(cx, cy), label_r, label_r)
            letter = (self.artist or self.title or "♪")[:1].upper()
            painter.setPen(QColor(20, 20, 26, 220))
            painter.setFont(cached_font(family, max(12, int(label_r * 0.9)), QFont.Weight.Bold))
            painter.drawText(QRectF(cx - label_r, cy - label_r, 2 * label_r, 2 * label_r),
                             Qt.AlignmentFlag.AlignCenter, letter)

        painter.setBrush(Qt.BrushStyle.NoBrush)
        ring = QColor(accent.red(), accent.green(), accent.blue(), 90) if neon \
            else QColor(255, 255, 255, 34)
        painter.setPen(QPen(ring, max(1.0, r * 0.01)))
        painter.drawEllipse(QPointF(cx, cy), label_r, label_r)

        hole = max(3.0, r * 0.035)
        painter.setBrush(QColor("#08080b"))
        painter.setPen(QPen(QColor(255, 255, 255, 60), max(1.0, r * 0.008)))
        painter.drawEllipse(QPointF(cx, cy), hole, hole)

        painter.setBrush(Qt.BrushStyle.NoBrush)
        pen = QPen(QColor(255, 255, 255, 30), max(2.0, r * 0.035))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawArc(QRectF(cx - r * 0.9, cy - r * 0.9, r * 1.8, r * 1.8), 40 * 16, 60 * 16)

        if SETTINGS.get("vinyl_rim", True):
            painter.setBrush(Qt.BrushStyle.NoBrush)
            rim_alpha = 80 + int((40 * pulse + 70 * kick) * max(0.25, glow_k))
            painter.setPen(QPen(QColor(accent.red(), accent.green(), accent.blue(),
                                       min(255, rim_alpha)),
                                max(1.0, r * 0.010)))
            painter.drawEllipse(QPointF(cx, cy), r * 0.998, r * 0.998)
        painter.restore()


class LyricsEditorDialog(UiDialog):
    """Type/paste lyrics, tap the start of every line while the track plays, save a local .lrc."""

    def __init__(self, overlay):
        super().__init__(None)
        self.overlay = overlay
        self.setWindowTitle("Текст песни вручную")
        self.setMinimumSize(620, 560)
        self.resize(660, 660)

        self.syncing = False
        self.lines = []
        self.times = []
        self.mark_index = 0

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self.build_header("Текст песни вручную"))

        layout = QVBoxLayout()
        layout.setContentsMargins(18, 2, 18, 16)
        layout.setSpacing(8)
        outer.addLayout(layout, 1)

        who = f"{overlay.current_artist} — {overlay.current_title}".strip(" —")
        track_label = QLabel(who or "Нет активного трека")
        track_label.setObjectName("ui_track")
        layout.addWidget(track_label)

        hint = QLabel("Одна строка = одна строка песни. Нажмите «Начать разметку», затем в момент "
                      "начала каждой строки жмите Пробел. Можно и просто разметить равномерно по "
                      "длительности и потом поправить отдельные строки.")
        hint.setObjectName("ui_hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.text_edit = QPlainTextEdit()
        prefill = [t for _, t in overlay.lyrics if t] if overlay.lyrics else []
        if prefill and prefill[0] in ("Текст не найден", "Ошибка текста", "Поиск текста..."):
            prefill = []
        self.text_edit.setPlainText("\n".join(prefill))
        self.text_edit.installEventFilter(self)
        layout.addWidget(self.text_edit, 1)

        self.stamp_list = QListWidget()
        self.stamp_list.setMaximumHeight(130)
        self.stamp_list.currentRowChanged.connect(self._on_stamp_selected)
        layout.addWidget(self.stamp_list)

        time_row = QHBoxLayout()
        time_row.addWidget(QLabel("Время строки:"))
        self.time_spin = BigDoubleSpinBox(0.0, 36000.0, 0.0, decimals=2, step=0.1, suffix=" с")
        time_row.addWidget(self.time_spin)
        self.apply_time_btn = QPushButton("Применить к выбранной")
        self.apply_time_btn.clicked.connect(self.apply_time)
        time_row.addWidget(self.apply_time_btn)
        time_row.addStretch()
        layout.addLayout(time_row)

        self.next_label = QLabel("")
        self.next_label.setObjectName("ui_next")
        self.next_label.setWordWrap(True)
        layout.addWidget(self.next_label)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        row1 = QHBoxLayout()
        row1.setSpacing(10)
        self.start_btn = QPushButton("▶  Начать разметку")
        self.start_btn.clicked.connect(self.start_sync)
        self.mark_btn = QPushButton("Отметить строку (Пробел)")
        self.mark_btn.clicked.connect(self.mark_line)
        self.undo_btn = QPushButton("Отменить")
        self.undo_btn.clicked.connect(self.undo_mark)
        row1.addWidget(self.start_btn)
        row1.addWidget(self.mark_btn)
        row1.addWidget(self.undo_btn)
        layout.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(10)
        self.spread_btn = QPushButton("Разметить равномерно")
        self.spread_btn.clicked.connect(self.spread_evenly)
        self.save_btn = QPushButton("Сохранить")
        self.save_btn.setObjectName("btn_apply")
        self.save_btn.clicked.connect(self.save_lyrics)
        close_btn = QPushButton("Закрыть")
        close_btn.setObjectName("btn_cancel")
        close_btn.clicked.connect(self.close)
        row2.addWidget(self.spread_btn)
        row2.addStretch()
        row2.addWidget(self.save_btn)
        row2.addWidget(close_btn)
        layout.addLayout(row2)

        self._say("")


    def _say(self, text, error=False):
        self.status.setObjectName("ui_err" if error else "ui_ok")
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)
        self.status.setText(text)

    def _text_lines(self):
        return [l.strip() for l in self.text_edit.toPlainText().splitlines() if l.strip()]

    def _text_blocks(self):
        """(document block, text) for every non-empty line — blank lines must not shift the sync."""
        blocks = []
        document = self.text_edit.document()
        for i in range(document.blockCount()):
            text = document.findBlockByNumber(i).text().strip()
            if text:
                blocks.append((i, text))
        return blocks

    def _refresh_stamps(self):
        self.stamp_list.blockSignals(True)
        self.stamp_list.clear()
        for index, (t, line) in enumerate(zip(self.times, self.lines)):
            if t is None:
                continue
            self.stamp_list.addItem(f"{format_lrc_time(t)}  {line}")
            self.stamp_list.item(self.stamp_list.count() - 1).setData(
                Qt.ItemDataRole.UserRole, index)
        self.stamp_list.blockSignals(False)

    def _selected_index(self):
        item = self.stamp_list.currentItem()
        if item is None:
            return -1
        index = item.data(Qt.ItemDataRole.UserRole)
        return int(index) if index is not None else -1

    def _on_stamp_selected(self, row):
        index = self._selected_index()
        if 0 <= index < len(self.times) and self.times[index] is not None:
            self.time_spin.setValue(float(self.times[index]))
            self._highlight(index)

    def _highlight(self, index):
        if not hasattr(self, "block_numbers") or not (0 <= index < len(self.block_numbers)):
            return
        block = self.text_edit.document().findBlockByNumber(self.block_numbers[index])
        cursor = QTextCursor(block)
        self.text_edit.setTextCursor(cursor)
        self.text_edit.ensureCursorVisible()

    def _update_next(self):
        if self.syncing and self.mark_index < len(self.lines):
            self.next_label.setText(f"Следующая строка ({self.mark_index + 1}/{len(self.lines)}): "
                                    f"{self.lines[self.mark_index]}")
        elif self.mark_index >= len(self.lines) and self.lines:
            self.next_label.setText("Все строки отмечены.")
        else:
            self.next_label.setText("")


    def eventFilter(self, obj, event):
        edit = getattr(self, "text_edit", None)
        if (edit is not None and obj is edit and self.syncing
                and event.type() == QEvent.Type.KeyPress
                and event.key() == Qt.Key.Key_Space):
            self.mark_line()
            return True
        return super().eventFilter(obj, event)

    def start_sync(self):
        blocks = self._text_blocks()
        if not blocks:
            self._say("Сначала вставьте текст песни.", error=True)
            return
        self.block_numbers = [b for b, _ in blocks]
        self.lines = [text for _, text in blocks]
        self.times = [None] * len(lines)
        self.mark_index = 0
        self.syncing = True
        self.text_edit.setReadOnly(True)
        self._refresh_stamps()
        self._highlight(0)
        self._update_next()
        self._say("Разметка начата — жмите Пробел в начале каждой строки.")

    def mark_line(self):
        if not self.syncing:
            return
        if not self.overlay.is_playing:
            self._say("Трек на паузе: включите музыку и продолжайте.", error=True)
            return
        pos = self.overlay.local_pos + float(SETTINGS.get("offset", 0.0))
        if self.mark_index > 0:
            previous = self.times[self.mark_index - 1]
            if previous is not None and pos <= previous:
                pos = previous + 0.2
        self.times[self.mark_index] = pos
        self.mark_index += 1
        self._refresh_stamps()
        if self.mark_index >= len(self.lines):
            self.syncing = False
            self.text_edit.setReadOnly(False)
            self._say("Все строки отмечены. Нажмите «Сохранить».")
        else:
            self._highlight(self.mark_index)
            self._say(f"Отмечено {self.mark_index} из {len(self.lines)}.")
        self._update_next()

    def undo_mark(self):
        if self.mark_index == 0:
            return
        self.mark_index -= 1
        self.times[self.mark_index] = None
        self.syncing = True
        self.text_edit.setReadOnly(True)
        self._refresh_stamps()
        self._highlight(self.mark_index)
        self._update_next()
        self._say("Последняя отметка отменена.")

    def spread_evenly(self):
        blocks = self._text_blocks()
        if not blocks:
            self._say("Сначала вставьте текст песни.", error=True)
            return
        self.block_numbers = [b for b, _ in blocks]
        lines = [text for _, text in blocks]
        duration = float(self.overlay.current_duration or 0.0)
        pairs = spread_lines(lines, duration)
        self.lines = lines
        self.times = [t for t, _ in pairs]
        self.mark_index = len(lines)
        self.syncing = False
        self.text_edit.setReadOnly(False)
        self._refresh_stamps()
        self._update_next()
        if duration:
            self._say("Разметка по длительности трека. Выберите строку в списке и поправьте время, "
                      "если нужно.")
        else:
            self._say("Длительность трека неизвестна — разметил по 4 секунды на строку.", error=True)

    def apply_time(self):
        row = self._selected_index()
        if not (0 <= row < len(self.times)):
            self._say("Выберите строку в списке ниже.", error=True)
            return
        self.times[row] = float(self.time_spin.value())
        self._refresh_stamps()
        self._say("Время обновлено.")

    def save_lyrics(self):
        lines = self._text_lines()
        if not lines:
            self._say("Текст пустой — сохранять нечего.", error=True)
            return
        if len(lines) != len(self.times) or any(t is None for t in self.times):
            self._say("Сначала разметьте все строки (или нажмите «Разметить равномерно»).",
                      error=True)
            return
        if self.lines and lines != self.lines:
            self._say("Текст изменился после разметки — разметьте заново.", error=True)
            return
        artist = self.overlay.current_artist
        title = self.overlay.current_title
        if not title and not artist:
            self._say("Нет активного трека — непонятно, к какой песне сохранять.", error=True)
            return
        if save_local_lyrics(artist, title, lines_to_lrc(lines, self.times)):
            self.overlay.reload_current_lyrics()
            self._say("Сохранено локально. Оверлей уже использует этот текст.")
            QTimer.singleShot(600, self.accept)
        else:
            self._say("Не удалось записать файл в папку cache/lyrics.", error=True)


class LyricsOverlay(QLabel):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        w = int(SETTINGS.get("window_width", 1800))
        self.resize(w, LyricsWidget.preferred_height())
        self.restore_position()

        self.current_artist = ""
        self.current_title = ""
        self.current_duration = 0.0
        self.lyrics = []
        self.lyric_times = []
        self.current_index = IDX_UNSET

        self.is_playing = False
        self.local_pos = 0.0
        self.last_tick_time = time.time()
        self.last_frame_time = time.time()

        self.drag_position = None
        self.settings_dialog = None
        self.lyrics_editor = None

        self.lyrics_widget = LyricsWidget(self)
        self.lyrics_widget.setGeometry(0, 0, self.width(), self.height())

        self.anim_timer = QTimer()
        self.anim_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.anim_timer.timeout.connect(self.animate_step)
        self.anim_timer.start(16)

        self.apply_styles()

        self.tracker = MediaTracker()
        self.tracker.track_changed.connect(self.on_track_changed)
        self.tracker.lyrics_loaded.connect(self.on_lyrics_loaded)
        self.tracker.state_changed.connect(self.on_state_changed)
        self.tracker.position_update.connect(self.on_position_update)
        self.tracker.cover_loaded.connect(self.on_cover_loaded)
        self.tracker.start()

        self.setup_tray()

    def restore_position(self):
        """Use the saved position if the window is still reachable on some screen, otherwise centre it."""
        x, y = SETTINGS.get("last_x"), SETTINGS.get("last_y")
        if isinstance(x, int) and isinstance(y, int):
            frame = QRect(x, y, self.width(), self.height())
            for screen in QApplication.screens():
                avail = screen.availableGeometry()
                overlap = frame.intersected(avail)
                if overlap.width() >= 100 and overlap.height() >= 50:
                    nx = min(max(x, avail.left()), max(avail.left(), avail.right() - self.width() + 1))
                    ny = min(max(y, avail.top()), max(avail.top(), avail.bottom() - self.height() + 1))
                    self.move(nx, ny)
                    return
        avail = QApplication.primaryScreen().availableGeometry()
        self.move(avail.x() + (avail.width() - self.width()) // 2,
                  avail.y() + (avail.height() - self.height()) // 2)

    def setup_tray(self):
        self.tray_icon = QSystemTrayIcon(self)
        icon = app_icon()
        if icon.isNull():
            pixmap = QIcon().pixmap(32, 32)
            pixmap.fill(QColor("#89b4fa"))
            icon = QIcon(pixmap)
        self.tray_icon.setIcon(icon)

        tray_menu = QMenu()
        show_action = QAction("Показать/Скрыть", self)
        show_action.triggered.connect(self.toggle_visibility)
        settings_action = QAction("Настройки…", self)
        settings_action.triggered.connect(self.open_settings)
        lyrics_action = QAction("Текст песни вручную…", self)
        lyrics_action.triggered.connect(self.open_lyrics_editor)
        quit_action = QAction("Выход", self)
        quit_action.triggered.connect(QApplication.instance().quit)

        tray_menu.addAction(show_action)
        tray_menu.addAction(settings_action)
        tray_menu.addAction(lyrics_action)
        tray_menu.addSeparator()
        tray_menu.addAction(quit_action)

        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.show()

    def toggle_visibility(self):
        if self.isVisible(): self.hide()
        else: self.show()

    def open_settings(self):
        if self.settings_dialog is None or not self.settings_dialog.isVisible():
            self.settings_dialog = SettingsDialog(self)
            self.settings_dialog.show()
        else:
            self.settings_dialog.raise_()
            self.settings_dialog.activateWindow()

    def open_lyrics_editor(self):
        if self.lyrics_editor is None or not self.lyrics_editor.isVisible():
            self.lyrics_editor = LyricsEditorDialog(self)
            self.lyrics_editor.show()
        else:
            self.lyrics_editor.raise_()
            self.lyrics_editor.activateWindow()

    def reload_current_lyrics(self):
        """After the editor saved a local file: drop the old lines and read them back."""
        if not self.current_title:
            return
        self.lyrics = []
        self.lyric_times = []
        self.current_index = IDX_UNSET
        self.lyrics_widget.lines = []
        self.lyrics_widget.index = IDX_UNSET
        self.tracker.reload_lyrics(self.current_artist, self.current_title,
                                   self.current_duration)

    def closeEvent(self, event):
        event.ignore()
        self.hide()

    def resizeEvent(self, event):
        self.lyrics_widget.setGeometry(0, 0, self.width(), self.height())
        super().resizeEvent(event)

    def apply_styles(self):
        self.setStyleSheet("background-color: transparent;")
        height = LyricsWidget.preferred_height()
        if height != self.height():
            bottom = self.y() + self.height()
            top = self.y()
            self.resize(self.width(), height)
            if SETTINGS.get("anim_mode", "line") == "sides":
                self.move(self.x(), top)
            else:
                self.move(self.x(), bottom - height)
        self.lyrics_widget.update()

    def save_current_settings(self):
        save_settings(SETTINGS)


    def on_state_changed(self, playing):
        self.is_playing = playing
        self.lyrics_widget.is_playing = playing
        self.last_tick_time = time.time()
        self.lyrics_widget.update()

    def on_position_update(self, pos):
        """Only real jumps arrive here (track start / seek): snap, then keep counting locally."""
        if not self.is_playing or abs(pos - self.local_pos) > 0.5:
            self.local_pos = pos
            self.last_tick_time = time.time()

    def on_track_changed(self, artist, title, pos, duration):
        self.current_artist = artist
        self.current_title = title
        self.current_duration = duration
        self.lyrics = []
        self.lyric_times = []
        self.current_index = IDX_UNSET
        self.local_pos = pos
        self.last_tick_time = time.time()

        w = self.lyrics_widget
        w.artist = artist
        w.title = title
        w.lines = []
        w.index = IDX_UNSET
        w.set_cover(None)
        w.set_unsynced(False)
        w.start_line("Поиск текста...")
        w.visible_len = len(w.current_text)
        w.update()

    def on_cover_loaded(self, data):
        w = self.lyrics_widget
        if not data:
            w.set_cover(None)
            return
        pixmap = QPixmap()
        if pixmap.loadFromData(data):
            w.set_cover(pixmap)

    def on_lyrics_loaded(self, lyrics, synced):
        self.lyrics = lyrics
        self.lyric_times = [t for t, _ in lyrics]
        self.current_index = IDX_UNSET
        self.lyrics_widget.set_unsynced(not synced)


    def animate_step(self):
        now = time.time()
        frame_dt = min(0.1, max(0.0, now - self.last_frame_time))
        self.last_frame_time = now

        if self.is_playing:
            self.local_pos += now - self.last_tick_time
        self.last_tick_time = now

        animating = self.lyrics_widget.tick(frame_dt)
        changed = self.update_lyrics_display()
        if changed or animating:
            self.lyrics_widget.update()

    def update_lyrics_display(self):
        if not self.lyrics or not self.is_playing:
            return False
        w = self.lyrics_widget
        changed = False

        first_time, first_text = self.lyrics[0]
        if len(self.lyrics) == 1 and first_time < 0:
            w.lines = []
            w.index = IDX_UNSET
            if w.current_text != first_text:
                w.start_line(first_text)
                w.visible_len = len(first_text)
                changed = True
            return changed
        w.lines = self.lyrics

        current_time = self.local_pos + float(SETTINGS.get("offset", 0.0))

        idx = bisect.bisect_right(self.lyric_times, current_time) - 1
        w.index = idx
        if idx != self.current_index:
            self.current_index = idx
            w.start_line(self.lyrics[idx][1] if idx >= 0 else "")
            changed = True
        if idx < 0:
            return changed

        start_time = self.lyrics[idx][0]
        if idx + 1 < len(self.lyrics):
            next_time = self.lyrics[idx + 1][0]
        else:
            next_time = start_time + 5.0

        duration = next_time - start_time
        margin = float(SETTINGS.get("sync_margin", 0.8))
        effective_duration = max(0.2, duration - margin)

        progress = (current_time - start_time) / effective_duration
        progress = max(0.0, min(1.0, progress))

        text_len = len(w.current_text)
        target_chars = int(text_len * progress)

        if target_chars != w.visible_len:
            w.visible_len = target_chars
            if SETTINGS.get("anim_wave", False):
                for i in range(w.last_visible_len, target_chars):
                    w.wave_offsets[i] = 15.0
            w.last_visible_len = target_chars
            changed = True
        return changed


    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.drag_position = event.globalPosition().toPoint()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        elif event.button() == Qt.MouseButton.RightButton:
            self.open_settings()

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.MouseButton.LeftButton and self.drag_position:
            delta = event.globalPosition().toPoint() - self.drag_position
            self.move(self.x() + delta.x(), self.y() + delta.y())
            self.drag_position = event.globalPosition().toPoint()

    def mouseReleaseEvent(self, event):
        self.drag_position = None
        self.setCursor(Qt.CursorShape.ArrowCursor)
        SETTINGS["last_x"] = self.x()
        SETTINGS["last_y"] = self.y()
        self.save_current_settings()

if __name__ == '__main__':
    logging.basicConfig(filename="lyricsapp.log", level=logging.INFO, encoding="utf-8",
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        if hasattr(Qt, 'AA_EnableHighDpiScaling'):
            QApplication.setAttribute(Qt.ApplicationAttribute.AA_EnableHighDpiScaling, True)

        qapp = QApplication(sys.argv)
        qapp.setQuitOnLastWindowClosed(False)
        qapp.setWindowIcon(app_icon())
        apply_ui_theme(SETTINGS.get("theme", "custom"))

        from PyQt6.QtNetwork import QLocalServer, QLocalSocket
        ipc_name = "LyricsAppOverlay"
        probe = QLocalSocket()
        probe.connectToServer(ipc_name)
        if probe.waitForConnected(300):
            probe.abort()
            print("LyricsApp уже запущен.")
            sys.exit(0)
        QLocalServer.removeServer(ipc_name)
        single_instance = QLocalServer()
        single_instance.listen(ipc_name)

        overlay_app = LyricsOverlay()

        overlay_app.show()
        print("Работает! ПКМ по тексту для настроек.")
        sys.exit(qapp.exec())
    except Exception as e:
        log.exception("Фатальная ошибка")
        print(f"Ошибка: {e}")
        import traceback
        traceback.print_exc()
