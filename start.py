import sys
import asyncio
import threading
import requests
import re
import json
import os
import time
import math
from PyQt6.QtWidgets import (QApplication, QLabel, QVBoxLayout, QHBoxLayout, 
                             QPushButton, QFontComboBox, QSpinBox, QColorDialog, 
                             QDoubleSpinBox, QFormLayout, QDialog, QWidget, QTabWidget,
                             QCheckBox, QGroupBox, QLineEdit, QSlider, QToolButton,
                             QSystemTrayIcon, QMenu)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QObject, QPoint, QRect
from PyQt6.QtGui import (QFont, QColor, QCursor, QPainter, QLinearGradient, QPen, 
                         QPainterPath, QFontMetrics, QIcon, QAction)

try:
    from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as MediaManager
except ImportError:
    print("Ошибка: Не установлены библиотеки winrt.")
    sys.exit()

GENIUS_TOKEN = ""

SETTINGS_FILE = "lyrics_settings.json"
DEFAULT_SETTINGS = {
    "font_family": "Segoe UI", "font_size": 36, "text_color": "#FFFFFF",
    "text_opacity": 100, "future_color": "#555555", "hide_future": False,
    "outline_color": "#000000", "outline_width": 2, "use_gradient": True,
    "gradient_color2": "#89b4fa", "bg_color": "#000000", "bg_opacity": 0,
    "offset": 0.0, "sync_margin": 0.8, "window_width": 1800,
    "last_x": 50, "last_y": 100, "genius_token": "",
    "anim_glow": False, "glow_color": "#89b4fa", "glow_size": 3, "glow_speed": 5,
    "anim_pulse": False, "anim_wave": False, "anim_flicker": False
}

def load_settings():
    settings = DEFAULT_SETTINGS.copy()
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
                for key in DEFAULT_SETTINGS.keys():
                    if key in saved: settings[key] = saved[key]
        except Exception: pass
    return settings

def save_settings(settings):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=4)

SETTINGS = load_settings()
if not GENIUS_TOKEN: GENIUS_TOKEN = SETTINGS.get("genius_token", "")

STYLE_SHEET = """
QDialog { background-color: #1e1e2e; color: #cdd6f4; font-family: 'Segoe UI'; }
QTabWidget::pane { border: 1px solid #45475a; border-radius: 6px; padding: 5px; }
QTabBar::tab { background-color: #313244; color: #cdd6f4; padding: 6px 12px; font-size: 13px; border-top-left-radius: 6px; border-top-right-radius: 6px; margin-right: 2px; }
QTabBar::tab:selected { background-color: #45475a; color: #89b4fa; font-weight: bold; }
QLabel { color: #cdd6f4; font-size: 13px; }
QSpinBox, QDoubleSpinBox, QFontComboBox, QLineEdit { background-color: #313244; color: #cdd6f4; border: 1px solid #45475a; border-radius: 4px; padding: 4px; font-size: 13px; min-height: 20px; }
QPushButton { background-color: #89b4fa; color: #11111b; border: none; border-radius: 4px; padding: 6px 12px; font-weight: bold; font-size: 13px; }
QPushButton:hover { background-color: #b4befe; }
QPushButton#btn_cancel { background-color: #45475a; color: #cdd6f4; }
QPushButton#btn_cancel:hover { background-color: #585b70; }
QPushButton#btn_exit { background-color: #f38ba8; color: #11111b; }
QPushButton#btn_exit:hover { background-color: #eba0ac; }
QCheckBox { color: #cdd6f4; font-size: 13px; spacing: 5px; }
QGroupBox { color: #89b4fa; font-weight: bold; font-size: 13px; margin-top: 10px; }
QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 3px; }
QToolButton { background-color: #45475a; color: white; border-radius: 3px; font-weight: bold; font-size: 14px; }
QToolButton:hover { background-color: #585b70; }
QSlider::groove:horizontal { border: 1px solid #45475a; height: 6px; background: #313244; margin: 2px 0; border-radius: 3px; }
QSlider::handle:horizontal { background: #89b4fa; border: 1px solid #89b4fa; width: 12px; margin: -3px 0; border-radius: 6px; }
"""

class MediaTracker(QObject):
    track_changed = pyqtSignal(str, str)
    lyrics_loaded = pyqtSignal(list)
    state_changed = pyqtSignal(bool)
    seek_detected = pyqtSignal(float)

    def __init__(self):
        super().__init__()
        self.current_track = ""
        self.last_pos = 0
        self.is_playing = False

    def start(self):
        threading.Thread(target=self._run_asyncio_loop, daemon=True).start()

    def _run_asyncio_loop(self):
        asyncio.run(self._media_loop())

    async def _media_loop(self):
        while True:
            try:
                sessions = await MediaManager.request_async()
                current_session = sessions.get_current_session()
                if current_session:
                    playback_info = current_session.get_playback_info()
                    is_playing = (playback_info.playback_status == 4)
                    
                    if is_playing != self.is_playing:
                        self.is_playing = is_playing
                        self.state_changed.emit(is_playing)

                    if is_playing:
                        info = await current_session.try_get_media_properties_async()
                        timeline = current_session.get_timeline_properties()
                        title, artist = info.title, info.artist
                        pos = timeline.position.total_seconds()
                        
                        if abs(pos - self.last_pos) > 2.0 and self.last_pos != 0:
                            self.seek_detected.emit(pos)
                        self.last_pos = pos

                        if title and title != self.current_track:
                            self.current_track = title
                            self.track_changed.emit(artist, title)
                            self.download_lyrics(artist, title)
            except Exception:
                pass
            await asyncio.sleep(0.2)

    def download_lyrics(self, artist, title):
        def fetch():
            clean_title = re.sub(r'\[.*?\]|\(.*?\)', '', title).strip()
            clean_artist = re.sub(r'\[.*?\]|\(.*?\)', '', artist).strip()
            
            url = f"https://lrclib.net/api/get?artist_name={clean_artist}&track_name={clean_title}"
            try:
                res = requests.get(url, timeout=5)
                if res.status_code == 200:
                    data = res.json()
                    synced = data.get('syncedLyrics', '')
                    if synced:
                        self.parse_and_emit(synced)
                        return
            except Exception: pass
            
            try:
                search_url = f"https://lrclib.net/api/search?q={clean_title}"
                res = requests.get(search_url, timeout=5).json()
                if res and len(res) > 0:
                    for item in res:
                        synced = item.get('syncedLyrics', '')
                        if synced:
                            self.parse_and_emit(synced)
                            return
            except Exception: pass
                
            self.download_from_genius(clean_artist, clean_title)
        threading.Thread(target=fetch, daemon=True).start()

    def parse_and_emit(self, synced_text):
        parsed = []
        for line in synced_text.split('\n'):
            match = re.match(r'\[(\d+):(\d+\.\d+)\](.*)', line)
            if match:
                mins, secs, text = match.groups()
                if text.strip():
                    parsed.append((int(mins) * 60 + float(secs), text.strip()))
        if parsed:
            self.lyrics = parsed
            self.lyrics_loaded.emit(self.lyrics)

    def download_from_genius(self, artist, title):
        def fetch_genius():
            if not GENIUS_TOKEN:
                self.lyrics_loaded.emit([(-1, "Текст не найден")])
                return
            try:
                import lyricsgenius
                genius = lyricsgenius.Genius(GENIUS_TOKEN, timeout=10, retries=2)
                genius.verbose = False
                genius.remove_section_headers = True
                song = genius.search_song(title, artist)
                if song and song.lyrics:
                    lines = song.lyrics.split('\n')
                    if lines and ("Lyrics" in lines[0] or "Текст песни" in lines[0]):
                        lines = lines[1:]
                    parsed = []
                    time_offset = 0
                    for line in lines:
                        if line.strip():
                            parsed.append((time_offset, line.strip()))
                            time_offset += 4.0
                    self.lyrics = parsed
                    self.lyrics_loaded.emit(self.lyrics)
                else:
                    self.lyrics_loaded.emit([(-1, "Текст не найден")])
            except Exception:
                self.lyrics_loaded.emit([(-2, "Ошибка текста")])
        threading.Thread(target=fetch_genius, daemon=True).start()

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

class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Настройки Lyrics")
        self.setMinimumSize(420, 500)
        self.resize(450, 580)
        self.parent_overlay = parent
        
        self.setWindowFlags(Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.setStyleSheet(STYLE_SHEET)
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        tabs = QTabWidget()
        
        # --- Вкладка 1: Внешний вид ---
        tab_appearance = QWidget()
        form_app = QFormLayout(tab_appearance)
        form_app.setSpacing(8)
        
        self.font_combo = QFontComboBox()
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
        
        self.hide_future_cb = QCheckBox("Скрыть неспетый")
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
        
        tabs.addTab(tab_appearance, "Текст")

        # --- Вкладка 2: Эффекты ---
        tab_effects = QWidget()
        layout_effects = QVBoxLayout(tab_effects)
        layout_effects.setContentsMargins(5, 5, 5, 5)
        
        group_grad = QGroupBox("Градиент")
        form_grad = QFormLayout(group_grad)
        self.use_gradient_cb = QCheckBox("Включить")
        self.use_gradient_cb.setChecked(SETTINGS.get("use_gradient", True))
        form_grad.addRow("", self.use_gradient_cb)
        self.grad_color2_btn = QPushButton("2-й цвет")
        self.update_btn_color(self.grad_color2_btn, SETTINGS.get("gradient_color2", "#89b4fa"))
        self.grad_color2_btn.clicked.connect(self.pick_grad_color2)
        form_grad.addRow("", self.grad_color2_btn)
        layout_effects.addWidget(group_grad)

        group_outline = QGroupBox("Обводка")
        form_outline = QFormLayout(group_outline)
        self.outline_color_btn = QPushButton("Цвет")
        self.update_btn_color(self.outline_color_btn, SETTINGS.get("outline_color", "#000000"), force_white_text=True)
        self.outline_color_btn.clicked.connect(self.pick_outline_color)
        form_outline.addRow("", self.outline_color_btn)
        self.outline_width_spin = BigSpinBox(0, 10, int(SETTINGS.get("outline_width", 2)))
        form_outline.addRow("Толщина:", self.outline_width_spin)
        layout_effects.addWidget(group_outline)

        group_anim = QGroupBox("Анимация")
        layout_anim = QVBoxLayout(group_anim)
        
        self.anim_glow_cb = QCheckBox("Свечение")
        self.anim_glow_cb.setChecked(SETTINGS.get("anim_glow", False))
        layout_anim.addWidget(self.anim_glow_cb)
        
        glow_opts = QHBoxLayout()
        self.glow_color_btn = QPushButton("Цвет")
        self.update_btn_color(self.glow_color_btn, SETTINGS.get("glow_color", "#89b4fa"))
        self.glow_color_btn.clicked.connect(self.pick_glow_color)
        glow_opts.addWidget(self.glow_color_btn)
        self.glow_size_spin = BigSpinBox(1, 10, int(SETTINGS.get("glow_size", 3)))
        glow_opts.addWidget(QLabel("Разм:"))
        glow_opts.addWidget(self.glow_size_spin)
        layout_anim.addLayout(glow_opts)
        
        self.glow_speed_spin = BigSpinBox(1, 10, int(SETTINGS.get("glow_speed", 5)))
        glow_speed_layout = QHBoxLayout()
        glow_speed_layout.addWidget(QLabel("Скорость:"))
        glow_speed_layout.addWidget(self.glow_speed_spin)
        layout_anim.addLayout(glow_speed_layout)

        self.anim_pulse_cb = QCheckBox("Пульсация")
        self.anim_pulse_cb.setChecked(SETTINGS.get("anim_pulse", False))
        layout_anim.addWidget(self.anim_pulse_cb)
        self.anim_wave_cb = QCheckBox("Волна")
        self.anim_wave_cb.setChecked(SETTINGS.get("anim_wave", False))
        layout_anim.addWidget(self.anim_wave_cb)
        self.anim_flicker_cb = QCheckBox("Мерцание")
        self.anim_flicker_cb.setChecked(SETTINGS.get("anim_flicker", False))
        layout_anim.addWidget(self.anim_flicker_cb)
        
        layout_effects.addWidget(group_anim)
        layout_effects.addStretch()
        tabs.addTab(tab_effects, "Эффекты")

        # --- Вкладка 3: Прочее ---
        tab_sync = QWidget()
        form_sync = QFormLayout(tab_sync)
        self.offset_spin = QDoubleSpinBox()
        self.offset_spin.setRange(-5.0, 5.0)
        self.offset_spin.setSingleStep(0.1)
        self.offset_spin.setValue(float(SETTINGS.get("offset", 0.0)))
        form_sync.addRow("Задержка (сек):", self.offset_spin)
        self.margin_spin = QDoubleSpinBox()
        self.margin_spin.setRange(0.1, 3.0)
        self.margin_spin.setSingleStep(0.1)
        self.margin_spin.setValue(float(SETTINGS.get("sync_margin", 0.8)))
        form_sync.addRow("Оффсет печати:", self.margin_spin)
        
        self.genius_token_input = QLineEdit()
        self.genius_token_input.setText(SETTINGS.get("genius_token", ""))
        form_sync.addRow("Genius Token:", self.genius_token_input)
        
        exit_btn = QPushButton("Выход")
        exit_btn.setObjectName("btn_exit")
        exit_btn.clicked.connect(self.exit_app)
        form_sync.addRow("", exit_btn)
        
        tabs.addTab(tab_sync, "Прочее")
        
        layout.addWidget(tabs)

        self.size_spin.valueChanged.connect(self.auto_apply)
        self.font_combo.currentFontChanged.connect(self.auto_apply)
        self.use_gradient_cb.stateChanged.connect(self.auto_apply)
        self.hide_future_cb.stateChanged.connect(self.auto_apply)
        self.outline_width_spin.valueChanged.connect(self.auto_apply)
        self.offset_spin.valueChanged.connect(self.auto_apply)
        self.margin_spin.valueChanged.connect(self.auto_apply)
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

    def exit_app(self):
        QApplication.instance().quit()

    def update_btn_color(self, btn, hex_color, force_white_text=False):
        btn.setStyleSheet(f"background-color: {hex_color}; color: {'white' if force_white_text else 'black'};")
        if not force_white_text and QColor(hex_color).lightness() < 128:
            btn.setStyleSheet(f"background-color: {hex_color}; color: white;")

    def pick_color(self, current_hex, title):
        dialog = QColorDialog(QColor(current_hex), self)
        dialog.setWindowTitle(title)
        if dialog.exec() == QColorDialog.DialogCode.Accepted:
            return dialog.currentColor().name() 
        return current_hex

    def pick_text_color(self): SETTINGS["text_color"] = self.pick_color(SETTINGS["text_color"], "Цвет текста"); self.update_btn_color(self.text_color_btn, SETTINGS["text_color"]); self.auto_apply()
    def pick_future_color(self): SETTINGS["future_color"] = self.pick_color(SETTINGS["future_color"], "Цвет неспетого"); self.update_btn_color(self.future_color_btn, SETTINGS["future_color"]); self.auto_apply()
    def pick_bg_color(self): SETTINGS["bg_color"] = self.pick_color(SETTINGS["bg_color"], "Цвет фона"); self.update_btn_color(self.bg_color_btn, SETTINGS["bg_color"]); self.auto_apply()
    def pick_grad_color2(self): SETTINGS["gradient_color2"] = self.pick_color(SETTINGS["gradient_color2"], "Второй цвет"); self.update_btn_color(self.grad_color2_btn, SETTINGS["gradient_color2"]); self.auto_apply()
    def pick_outline_color(self): SETTINGS["outline_color"] = self.pick_color(SETTINGS["outline_color"], "Цвет обводки"); self.update_btn_color(self.outline_color_btn, SETTINGS["outline_color"], force_white_text=True); self.auto_apply()
    def pick_glow_color(self): SETTINGS["glow_color"] = self.pick_color(SETTINGS["glow_color"], "Цвет свечения"); self.update_btn_color(self.glow_color_btn, SETTINGS["glow_color"]); self.auto_apply()

    def auto_apply(self):
        SETTINGS["font_family"] = self.font_combo.currentFont().family()
        SETTINGS["font_size"] = self.size_spin.value()
        SETTINGS["text_opacity"] = self.text_opacity_slider.value()
        SETTINGS["bg_opacity"] = self.bg_opacity_slider.value()
        SETTINGS["hide_future"] = self.hide_future_cb.isChecked()
        SETTINGS["use_gradient"] = self.use_gradient_cb.isChecked()
        SETTINGS["outline_width"] = self.outline_width_spin.value()
        SETTINGS["offset"] = self.offset_spin.value()
        SETTINGS["sync_margin"] = self.margin_spin.value()
        SETTINGS["genius_token"] = self.genius_token_input.text()
        
        SETTINGS["anim_glow"] = self.anim_glow_cb.isChecked()
        SETTINGS["glow_size"] = self.glow_size_spin.value()
        SETTINGS["glow_speed"] = self.glow_speed_spin.value()
        SETTINGS["anim_pulse"] = self.anim_pulse_cb.isChecked()
        SETTINGS["anim_wave"] = self.anim_wave_cb.isChecked()
        SETTINGS["anim_flicker"] = self.anim_flicker_cb.isChecked()
        
        global GENIUS_TOKEN
        GENIUS_TOKEN = self.genius_token_input.text()
        
        if self.parent_overlay:
            self.parent_overlay.apply_styles()
            self.parent_overlay.save_current_settings()

# --- ВИДЖЕТ ТЕКСТА (С ЗАЩИТОЙ ОТ ВЫЛЕТОВ) ---
class LyricsWidget(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setWordWrap(True)
        self.current_text = ""
        self.prev_text = ""
        self.slide_offset = 0.0      
        self.visible_len = 0
        
        self.last_visible_len = 0
        self.wave_offsets = {}
        self.anim_time = 0.0
        self.flicker_timer = 0.0
        self.is_playing = False

    def paintEvent(self, event):
        if not self.is_playing:
            return

        # Если текста нет, не пытаемся его рисовать (защита от ошибок)
        if not self.current_text:
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
        
        base_size = int(SETTINGS.get("font_size", 36))
        # ЗАЩИТА 1: Шрифт не может быть меньше 10
        if base_size < 10: base_size = 10
        
        font = QFont(SETTINGS.get("font_family", "Segoe UI"), base_size, QFont.Weight.Bold)
        text_alpha = int(SETTINGS.get("text_opacity", 100) * 2.55)
        
        is_error = "Ошибка" in self.current_text
        is_not_found = "не найден" in self.current_text
        
        if is_error:
            font_color = QColor("#ee0043")
        else:
            font_color = QColor(SETTINGS.get("text_color", "#FFFFFF"))
            
        if is_not_found:
            blink = math.sin(time.time() * 4) * 0.5 + 0.5
            text_alpha = int(text_alpha * (0.3 + blink * 0.7))

        if SETTINGS.get("anim_pulse", False):
            pulse = math.sin(self.anim_time * 3) * 0.5 + 0.5
            font.setPointSize(base_size + int(pulse * 6))
            self.anim_time += 0.016
        
        fm = QFontMetrics(font)
        full_width = fm.horizontalAdvance(self.current_text)
        max_width = self.width() - 40
        
        # ЗАЩИТА 2: Цикл уменьшения шрифта не опустится ниже 10
        while full_width > max_width and font.pointSize() > 10:
            font.setPointSize(font.pointSize() - 1)
            fm = QFontMetrics(font)
            full_width = fm.horizontalAdvance(self.current_text)

        painter.setFont(font)
        
        if self.slide_offset < 0 and self.prev_text:
            painter.setOpacity(0.3) 
            painter.setPen(font_color)
            rect = self.rect()
            rect.translate(0, int(self.slide_offset))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, self.prev_text)

        if self.current_text:
            rect = self.rect()
            rect.translate(0, int(self.slide_offset))
            
            start_x = rect.center().x() - (full_width / 2)
            text_y = rect.center().y() + (fm.ascent() - fm.descent()) / 2

            if not SETTINGS.get("hide_future", False):
                future_color = QColor(SETTINGS.get("future_color", "#555555"))
                future_color.setAlpha(text_alpha)
                painter.setPen(future_color)
                painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, self.current_text)
            
            if self.visible_len > 0:
                visible_text = self.current_text[:self.visible_len]
                vis_width = fm.horizontalAdvance(visible_text)
                
                painter.save()
                painter.setClipRect(int(start_x), rect.top() - 50, int(vis_width), rect.height() + 100)
                
                flicker_opacity = 1.0
                if SETTINGS.get("anim_flicker", False):
                    if self.flicker_timer > 0:
                        self.flicker_timer -= 0.016
                        if int(self.flicker_timer * 20) % 2 == 0:
                            flicker_opacity = 0.1
                    else:
                        flicker_opacity = 1.0
                
                path = QPainterPath()
                if SETTINGS.get("anim_wave", False):
                    current_x = start_x
                    for i in range(self.visible_len):
                        char = self.current_text[i]
                        if i not in self.wave_offsets:
                            self.wave_offsets[i] = 15
                        if self.wave_offsets[i] > 0:
                            self.wave_offsets[i] -= 1.5
                        char_y = text_y - self.wave_offsets[i]
                        path.addText(current_x, char_y, font, char)
                        current_x += fm.horizontalAdvance(char)
                else:
                    path.addText(start_x, text_y, font, self.current_text)

                if SETTINGS.get("anim_glow", False):
                    glow_hex = SETTINGS.get("glow_color", "#89b4fa")
                    glow_size = int(SETTINGS.get("glow_size", 3))
                    glow_speed = int(SETTINGS.get("glow_speed", 5))
                    
                    pulse_glow = math.sin(time.time() * glow_speed) * 0.5 + 0.5
                    current_glow = int(glow_size + pulse_glow * 4)
                    
                    glow_pen = QPen(QColor(glow_hex))
                    glow_pen.setWidth(1)
                    glow_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
                    painter.setPen(glow_pen)
                    
                    painter.setBrush(QColor(glow_hex))
                    painter.setOpacity(0.6 * flicker_opacity)
                    painter.drawPath(path)
                    
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                    painter.setOpacity(1.0)

                outline_w = int(SETTINGS.get("outline_width", 2))
                if outline_w > 0:
                    outline_color = QColor(SETTINGS.get("outline_color", "#000000"))
                    outline_color.setAlpha(text_alpha)
                    outline_pen = QPen(outline_color)
                    outline_pen.setWidth(outline_w)
                    outline_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
                    painter.setPen(outline_pen)
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                    painter.drawPath(path)

                painter.setPen(Qt.PenStyle.NoPen)
                fill_brush = font_color
                
                # ЗАЩИТА 3: Если текст пустой, не пытаемся создать градиент
                if SETTINGS.get("use_gradient", True) and not is_error and self.current_text:
                    gradient = QLinearGradient(start_x, text_y - 20, start_x + full_width, text_y + 20)
                    c1 = QColor(SETTINGS.get("text_color", "#FFFFFF"))
                    c2 = QColor(SETTINGS.get("gradient_color2", "#89b4fa"))
                    gradient.setColorAt(0, c1)
                    gradient.setColorAt(1, c2)
                    fill_brush = gradient

                painter.setBrush(fill_brush)
                
                original_opacity = painter.opacity()
                painter.setOpacity((text_alpha / 255.0) * flicker_opacity)
                painter.drawPath(path)
                painter.setOpacity(original_opacity)
                
                painter.restore()

class LyricsOverlay(QLabel):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        
        w = int(SETTINGS.get("window_width", 1800))
        self.resize(w, 300)
        
        screen = QApplication.primaryScreen().geometry()
        self.move((screen.width() - self.width()) // 2, screen.height() // 2)

        self.current_artist = ""
        self.current_title = ""
        self.lyrics = []
        self.current_index = -1
        
        self.is_playing = False
        self.local_pos = 0.0
        self.last_tick_time = time.time()
        
        self.drag_position = None
        self.settings_dialog = None
        
        self.lyrics_widget = LyricsWidget(self)
        self.lyrics_widget.setGeometry(0, 0, self.width(), self.height())
        
        self.anim_timer = QTimer()
        self.anim_timer.timeout.connect(self.animate_step)
        self.anim_timer.start(16) 

        self.apply_styles()

        self.tracker = MediaTracker()
        self.tracker.track_changed.connect(self.on_track_changed)
        self.tracker.lyrics_loaded.connect(self.on_lyrics_loaded)
        self.tracker.state_changed.connect(self.on_state_changed)
        self.tracker.seek_detected.connect(self.on_seek)
        self.tracker.start()

        self.setup_tray()

    def setup_tray(self):
        self.tray_icon = QSystemTrayIcon(self)
        pixmap = QIcon().pixmap(32, 32)
        pixmap.fill(QColor("#89b4fa"))
        self.tray_icon.setIcon(QIcon(pixmap))
        
        tray_menu = QMenu()
        show_action = QAction("Показать/Скрыть", self)
        show_action.triggered.connect(self.toggle_visibility)
        quit_action = QAction("Выход", self)
        quit_action.triggered.connect(QApplication.instance().quit)
        
        tray_menu.addAction(show_action)
        tray_menu.addSeparator()
        tray_menu.addAction(quit_action)
        
        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.show()

    def toggle_visibility(self):
        if self.isVisible(): self.hide()
        else: self.show()

    def closeEvent(self, event):
        event.ignore()
        self.hide()

    def resizeEvent(self, event):
        self.lyrics_widget.setGeometry(0, 0, self.width(), self.height())
        super().resizeEvent(event)

    def apply_styles(self):
        self.setStyleSheet("background-color: transparent;")

    def save_current_settings(self):
        save_settings(SETTINGS)

    def on_state_changed(self, playing):
        self.is_playing = playing
        self.lyrics_widget.is_playing = playing
        self.last_tick_time = time.time()
        self.lyrics_widget.update()

    def on_track_changed(self, artist, title):
        self.current_artist = artist
        self.current_title = title
        self.local_pos = 0.0
        self.last_tick_time = time.time()

        if not self.lyrics or self.current_index == -1:
            self.lyrics_widget.current_text = "Поиск текста..."
            self.lyrics_widget.visible_len = len(self.lyrics_widget.current_text)
            self.lyrics_widget.update()

    def on_seek(self, new_pos):
        self.local_pos = new_pos
        self.last_tick_time = time.time()
        self.current_index = -1

    def on_lyrics_loaded(self, lyrics):
        self.lyrics = lyrics
        self.current_index = -1

    def animate_step(self):
        now = time.time()
        if self.is_playing:
            dt = now - self.last_tick_time
            self.local_pos += dt
            self.last_tick_time = now
            
        w = self.lyrics_widget
        if w.slide_offset > 0:
            w.slide_offset -= 2.5 
            if w.slide_offset <= 0:
                w.slide_offset = 0
                w.prev_text = ""
        elif w.slide_offset < 0:
            w.slide_offset += 2.5
            if w.slide_offset >= 0:
                w.slide_offset = 0
                
        w.update()
        self.update_lyrics_display()

    def update_lyrics_display(self):
        if not self.lyrics or not self.is_playing:
            return

        if len(self.lyrics) == 1 and (self.lyrics[0][0] == -1 or self.lyrics[0][0] == -2):
            if self.lyrics_widget.current_text != self.lyrics[0][1]:
                self.lyrics_widget.current_text = self.lyrics[0][1]
                self.lyrics_widget.visible_len = len(self.lyrics[0][1])
                self.lyrics_widget.update()
            return

        current_time = self.local_pos + float(SETTINGS.get("offset", 0.0))

        new_index = 0
        for i, (t, text) in enumerate(self.lyrics):
            if t <= current_time:
                new_index = i
            else:
                break
                
        if new_index != self.current_index:
            self.current_index = new_index
            self.lyrics_widget.prev_text = self.lyrics_widget.current_text
            self.lyrics_widget.current_text = self.lyrics[new_index][1]
            self.lyrics_widget.visible_len = 0
            self.lyrics_widget.slide_offset = 40.0
            self.lyrics_widget.wave_offsets = {}
            self.lyrics_widget.last_visible_len = 0
            
            if SETTINGS.get("anim_flicker", False):
                self.lyrics_widget.flicker_timer = 0.4
            
        start_time = self.lyrics[self.current_index][0]
        
        if self.current_index + 1 < len(self.lyrics):
            next_time = self.lyrics[self.current_index + 1][0]
        else:
            next_time = start_time + 5.0
            
        duration = next_time - start_time
        margin = float(SETTINGS.get("sync_margin", 0.8))
        effective_duration = max(0.2, duration - margin)
        
        elapsed = current_time - start_time
        progress = elapsed / effective_duration
        
        if progress < 0: progress = 0
        if progress > 1: progress = 1
            
        text_len = len(self.lyrics_widget.current_text)
        target_chars = int(text_len * progress)
        
        if target_chars != self.lyrics_widget.visible_len:
            self.lyrics_widget.visible_len = target_chars
            if SETTINGS.get("anim_wave", False):
                for i in range(self.lyrics_widget.last_visible_len, target_chars):
                    self.lyrics_widget.wave_offsets[i] = 15
            self.lyrics_widget.last_visible_len = target_chars
            self.lyrics_widget.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.drag_position = event.globalPosition().toPoint()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        elif event.button() == Qt.MouseButton.RightButton:
            if self.settings_dialog is None or not self.settings_dialog.isVisible():
                self.settings_dialog = SettingsDialog(self)
                self.settings_dialog.show()
            else:
                self.settings_dialog.raise_()
                self.settings_dialog.activateWindow()

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
    try:
        if hasattr(Qt, 'AA_EnableHighDpiScaling'):
            QApplication.setAttribute(Qt.ApplicationAttribute.AA_EnableHighDpiScaling, True)

        qapp = QApplication(sys.argv)
        qapp.setQuitOnLastWindowClosed(False)
        
        overlay_app = LyricsOverlay()
        
        overlay_app.show()
        print("Работает! ПКМ по тексту для настроек.")
        sys.exit(qapp.exec())
    except Exception as e:
        print(f"Ошибка: {e}")
        import traceback
        traceback.print_exc()
        input("Нажмите Enter...")