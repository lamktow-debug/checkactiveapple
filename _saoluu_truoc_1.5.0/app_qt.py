"""Giao diện Qt cho Check Active.

Thay cho app_gui.py (Tkinter). Không đụng gì vào scraper: app_core.py và
check_active_v2 / check_active_parallel giữ nguyên.

Chạy: ./venv/bin/python app_qt.py
Hoặc double-click "Check Active.app"
"""

import asyncio
import contextlib
import subprocess
import sys
from pathlib import Path
import threading
import time

from PySide6.QtCore import (
    QObject,
    QSortFilterProxyModel,
    Qt,
    QThread,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QColor,
    QDesktopServices,
    QFontDatabase,
    QIcon,
    QGuiApplication,
    QPainter,
    QPixmap,
    QStandardItem,
    QStandardItemModel,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QStyledItemDelegate,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from app_core import (
    BASE_DIR,
    MANUAL_CAPTCHA_WAIT_SECONDS,
    RESULT_FILE,
    SERIALS_FILE,
    parse_serials,
    read_results,
)
from app_qt_core import (
    RUNNING_VALUE,
    WAITING_VALUE,
    LogBuffer,
    RunStats,
    SignalWriter,
    estimate_remaining_seconds,
    format_duration,
    format_rate,
    parse_running_serial,
    purchase_label,
    run_summary,
    serials_per_minute,
    status_kind,
    status_label,
)
from app_settings import apply_settings, load_settings, save_settings
from app_update import (
    UpdateError,
    check_for_update,
    download_release,
    format_size,
)
from version import __version__

LOG_LIMIT_LINES = 2000
TICK_MS = 500

COL_SERIAL, COL_DATE, COL_STATUS = range(3)
ROLE_KIND = Qt.UserRole + 1
ROLE_RAW = Qt.UserRole + 2


# ----------------------------------------------------------------------------
# màu sắc
# ----------------------------------------------------------------------------

LIGHT = {
    "bg": "#F2F4F7", "surface": "#FFFFFF", "surface2": "#F6F8FB",
    "ink": "#141A22", "ink2": "#4C5967", "ink3": "#7B8899",
    "line": "#DCE2EA",
    "ok": "#0F7A6B", "okBg": "#D6EDE8",
    "warn": "#9A6206", "warnBg": "#F7E9CF",
    "bad": "#AE3830", "badBg": "#F7DEDB",
    "accent": "#0F7A6B",
    "logBg": "#151B22", "logInk": "#C9D4DF",
    "logOk": "#5FD0A8", "logWarn": "#E8B65C", "logBad": "#F08C82",
}

DARK = {
    "bg": "#0F151C", "surface": "#161F29", "surface2": "#1C2632",
    "ink": "#E6ECF3", "ink2": "#A6B3C2", "ink3": "#7A8998",
    "line": "#28343F",
    "ok": "#46BEA8", "okBg": "#12332E",
    "warn": "#E8A94E", "warnBg": "#37290F",
    "bad": "#E4776E", "badBg": "#3A1D1A",
    "accent": "#46BEA8",
    "logBg": "#0A0F14", "logInk": "#C9D4DF",
    "logOk": "#5FD0A8", "logWarn": "#E8B65C", "logBad": "#F08C82",
}


def palette_for(app):
    """Theo giao diện sáng/tối của macOS."""
    window = app.palette().window().color()
    is_dark = (window.red() + window.green() + window.blue()) / 3 < 128
    return DARK if is_dark else LIGHT


def mono_font(size=12):
    font = QFontDatabase.systemFont(QFontDatabase.FixedFont)
    font.setPointSize(size)
    return font


# ----------------------------------------------------------------------------
# nhãn trạng thái vẽ trong ô bảng
# ----------------------------------------------------------------------------

class StatusPillDelegate(QStyledItemDelegate):
    """Vẽ trạng thái thành nhãn bo tròn có nền, thay vì chỉ đổi màu chữ."""

    def __init__(self, colors, parent=None):
        super().__init__(parent)
        self.colors = colors

    def initStyleOption(self, option, index):
        # Để lớp cha chỉ vẽ nền hàng (sọc xen kẽ, hàng đang chọn); phần chữ
        # mình tự vẽ bên dưới dưới dạng nhãn.
        super().initStyleOption(option, index)
        option.text = ""

    def paint(self, painter, option, index):
        kind = index.data(ROLE_KIND) or "idle"
        text = index.data(Qt.DisplayRole) or ""

        super().paint(painter, option, index)

        if kind == "idle" or not text.strip() or text == "—":
            painter.save()
            painter.setPen(QColor(self.colors["ink3"]))
            painter.drawText(option.rect.adjusted(10, 0, -10, 0),
                             Qt.AlignVCenter | Qt.AlignLeft, text)
            painter.restore()
            return

        fg = QColor(self.colors[kind])
        bg = QColor(self.colors[kind + "Bg"])

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)

        metrics = painter.fontMetrics()
        width = metrics.horizontalAdvance(text) + 20
        height = min(option.rect.height() - 8, 22)
        rect = option.rect.adjusted(10, 0, 0, 0)
        rect.setWidth(min(width, option.rect.width() - 16))
        rect.setTop(option.rect.top() + (option.rect.height() - height) // 2)
        rect.setHeight(height)

        painter.setPen(Qt.NoPen)
        painter.setBrush(bg)
        painter.drawRoundedRect(rect, height / 2, height / 2)
        painter.setPen(fg)
        painter.drawText(rect, Qt.AlignCenter, text)
        painter.restore()


# ----------------------------------------------------------------------------
# cầu nối scraper <-> giao diện
# ----------------------------------------------------------------------------

class ScraperWorker(QObject):
    """Chạy scraper trong QThread riêng, nói chuyện với giao diện bằng Signal."""

    log = Signal(str, str)
    result = Signal(str, str, int, int)
    captcha_wanted = Signal(str, str)
    captcha_done = Signal()
    finished = Signal()

    def __init__(self, settings):
        super().__init__()
        self.settings = settings
        self.stop_flag = threading.Event()
        self.manual_event = threading.Event()
        self.manual_answer = None

    def request_stop(self):
        self.stop_flag.set()

    def submit_captcha(self, answer):
        self.manual_answer = answer
        self.manual_event.set()

    async def _ask_manual_captcha(self, image_base64, serial):
        self.manual_answer = None
        self.manual_event.clear()
        self.captcha_wanted.emit(image_base64, serial)
        answered = await asyncio.to_thread(
            self.manual_event.wait, MANUAL_CAPTCHA_WAIT_SECONDS
        )
        self.captcha_done.emit()
        return self.manual_answer if answered else None

    def run(self):
        writer = SignalWriter(self.log.emit)
        try:
            scraper = apply_settings(self.settings)
            import check_active_parallel as runner

            scraper.MANUAL_CAPTCHA_HANDLER = (
                self._ask_manual_captcha if self.settings["manual_captcha"] else None
            )

            def on_progress(serial, value, done, total):
                self.result.emit(serial, value, done, total)

            with contextlib.redirect_stdout(writer):
                asyncio.run(runner.main(
                    force=True,
                    concurrency=self.settings["concurrency"],
                    args_headless=self.settings["headless"],
                    run_settings={
                        "capture_screenshot": self.settings["capture_screenshot"],
                        "folder_name": None,
                    },
                    progress_callback=on_progress,
                    should_stop=self.stop_flag.is_set,
                    serial_timeout=self.settings["serial_timeout"],
                ))
        except Exception as error:
            self.log.emit(f"❌ Lỗi: {type(error).__name__}: {error}", "")
        finally:
            writer.flush()
            self.finished.emit()


class UpdateWorker(QObject):
    """Hỏi GitHub xem có bản mới không. Chạy ở luồng riêng cho khỏi treo cửa sổ."""

    found = Signal(object)      # Release
    up_to_date = Signal()
    failed = Signal(str)

    def __init__(self, repo, token, current_version):
        super().__init__()
        self.repo = repo
        self.token = token
        self.current_version = current_version

    def run(self):
        try:
            release = check_for_update(self.repo, self.token, self.current_version)
        except UpdateError as error:
            self.failed.emit(str(error))
            return
        except Exception as error:
            self.failed.emit(f"Lỗi lạ khi kiểm tra bản mới: {error}")
            return
        if release is None:
            self.up_to_date.emit()
        else:
            self.found.emit(release)


class DownloadWorker(QObject):
    """Tải file cài về Downloads, báo tiến độ ngược lại giao diện."""

    progress = Signal(int, int)
    done = Signal(str)
    failed = Signal(str)

    def __init__(self, release, token):
        super().__init__()
        self.release = release
        self.token = token

    def run(self):
        try:
            path = download_release(self.release, self.token,
                                    on_progress=self.progress.emit)
        except UpdateError as error:
            self.failed.emit(str(error))
            return
        except Exception as error:
            self.failed.emit(f"Tải không xong: {error}")
            return
        self.done.emit(str(path))


# ----------------------------------------------------------------------------
# hộp thoại
# ----------------------------------------------------------------------------

class SettingsDialog(QDialog):
    def __init__(self, settings, colors, parent=None):
        super().__init__(parent)
        self.colors = colors
        self.setWindowTitle("Cài đặt")
        self.setMinimumWidth(430)

        form = QFormLayout()
        form.setSpacing(10)

        self.key = QLineEdit(settings["twocaptcha_key"])
        self.key.setEchoMode(QLineEdit.Password)
        self.key.setPlaceholderText("dán key 2captcha vào đây")
        show = QCheckBox("Hiện")
        show.toggled.connect(
            lambda on: self.key.setEchoMode(QLineEdit.Normal if on else QLineEdit.Password)
        )
        key_row = QHBoxLayout()
        key_row.addWidget(self.key, 1)
        key_row.addWidget(show)
        form.addRow("Key 2captcha", key_row)

        self.concurrency = QSpinBox()
        self.concurrency.setRange(1, 3)
        self.concurrency.setValue(int(settings.get("concurrency") or 1))
        hint = QLabel(
            "Nhiều luồng = nhanh hơn đúng bấy nhiêu lần, nhưng cũng gửi request\n"
            "tới Apple nhiều gấp bấy nhiêu. Chạy thử 50 serial ở mức 2 trước;\n"
            "log không có dòng “Bị chặn IP” thì mới lên 3."
        )
        hint.setStyleSheet(f"color:{colors['ink3']};")
        thread_box = QVBoxLayout()
        thread_box.addWidget(self.concurrency)
        thread_box.addWidget(hint)
        form.addRow("Số luồng", thread_box)

        self.timeout = QSpinBox()
        self.timeout.setRange(30, 600)
        self.timeout.setSingleStep(30)
        self.timeout.setSuffix(" giây")
        self.timeout.setValue(int(settings["serial_timeout"]))
        form.addRow("Bỏ qua serial sau", self.timeout)

        self.headless = QCheckBox("Ẩn trình duyệt")
        self.headless.setChecked(settings["headless"])
        self.block_assets = QCheckBox("Chặn ảnh / font / video cho trang nhẹ hơn")
        self.block_assets.setChecked(settings["block_assets"])
        self.screenshot = QCheckBox("Chụp màn hình mỗi kết quả")
        self.screenshot.setChecked(settings["capture_screenshot"])
        self.manual = QCheckBox("Hỏi nhập captcha tay khi AI đọc trượt")
        self.manual.setChecked(settings["manual_captcha"])
        self.turbo = QCheckBox("⚡ Turbo — nghỉ 1,5–4s thay vì 3–8s giữa các serial")
        self.turbo.setChecked(bool(settings.get("turbo_mode", False)))
        self.local_ocr = QCheckBox("🧠 Thử OCR trên máy trước khi mua mã 2captcha")
        self.local_ocr.setChecked(bool(settings.get("local_ocr", True)))
        manual_hint = QLabel(
            f"Bật thì mỗi serial khó sẽ dừng chờ bạn gõ tối đa "
            f"{MANUAL_CAPTCHA_WAIT_SECONDS} giây.\nChạy mẻ lớn rồi đi làm việc "
            f"khác thì nên để tắt."
        )
        manual_hint.setStyleSheet(f"color:{colors['ink3']};")

        turbo_hint = QLabel(
            "Turbo nhanh hơn nhưng gửi request tới Apple dày hơn — dễ bị chặn IP\n"
            "hơn. Bật kèm nhiều luồng thì rủi ro cộng dồn."
        )
        turbo_hint.setStyleSheet(f"color:{colors['ink3']};")

        options = QVBoxLayout()
        for widget in (self.headless, self.block_assets, self.screenshot, self.manual):
            options.addWidget(widget)
        options.addWidget(manual_hint)
        options.addWidget(self.turbo)
        options.addWidget(turbo_hint)
        options.addWidget(self.local_ocr)
        ocr_hint = QLabel(
            "OCR máy đọc captcha trong ~0,2 giây và miễn phí; 2captcha mất ~9\n"
            "giây và tính tiền từng mã. Cần cài một lần bằng CAI_DAT_OCR.command;\n"
            "chưa cài thì ô này không có tác dụng gì."
        )
        ocr_hint.setStyleSheet(f"color:{colors['ink3']};")
        options.addWidget(ocr_hint)
        form.addRow("Tuỳ chọn", options)

        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet(f"color:{colors['line']};")
        form.addRow(line)

        self.repo = QLineEdit(settings.get("github_repo", ""))
        self.repo.setPlaceholderText("oneway/check-active")
        form.addRow("Repo GitHub", self.repo)

        self.gh_token = QLineEdit(settings.get("github_token", ""))
        self.gh_token.setEchoMode(QLineEdit.Password)
        self.gh_token.setPlaceholderText("github_pat_…")
        show_token = QCheckBox("Hiện")
        show_token.toggled.connect(
            lambda on: self.gh_token.setEchoMode(
                QLineEdit.Normal if on else QLineEdit.Password)
        )
        token_row = QHBoxLayout()
        token_row.addWidget(self.gh_token, 1)
        token_row.addWidget(show_token)
        form.addRow("Token GitHub", token_row)

        self.check_updates = QCheckBox("Tự kiểm tra bản mới khi mở app")
        self.check_updates.setChecked(bool(settings.get("check_updates", True)))
        token_hint = QLabel(
            "Repo để private nên cần token mới đọc được release.\n"
            "Vào GitHub > Settings > Developer settings > Fine-grained tokens,\n"
            "cấp quyền Contents: Read-only cho đúng repo này là đủ."
        )
        token_hint.setStyleSheet(f"color:{colors['ink3']};")

        update_box = QVBoxLayout()
        update_box.addWidget(self.check_updates)
        update_box.addWidget(token_hint)
        form.addRow("Cập nhật", update_box)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("Lưu")
        buttons.button(QDialogButtonBox.Cancel).setText("Huỷ")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 16)
        layout.addLayout(form)
        layout.addSpacing(8)
        layout.addWidget(buttons)

    def values(self):
        return {
            "twocaptcha_key": self.key.text().strip(),
            "concurrency": self.concurrency.value(),
            "headless": self.headless.isChecked(),
            "block_assets": self.block_assets.isChecked(),
            "capture_screenshot": self.screenshot.isChecked(),
            "serial_timeout": self.timeout.value(),
            "manual_captcha": self.manual.isChecked(),
            "turbo_mode": self.turbo.isChecked(),
            "local_ocr": self.local_ocr.isChecked(),
            "github_repo": self.repo.text().strip(),
            "github_token": self.gh_token.text().strip(),
            "check_updates": self.check_updates.isChecked(),
        }


class CaptchaDialog(QDialog):
    """Ảnh phóng to, con trỏ sẵn trong ô, Enter là gửi."""

    def __init__(self, image_base64, serial, colors, parent=None):
        super().__init__(parent)
        self.answer = None
        self.remaining = MANUAL_CAPTCHA_WAIT_SECONDS
        self.setWindowTitle(f"Nhập captcha — {serial}")
        self.setModal(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 18)
        layout.setSpacing(10)

        title = QLabel(serial)
        title.setAlignment(Qt.AlignCenter)
        font = title.font()
        font.setPointSize(14)
        font.setBold(True)
        title.setFont(font)
        layout.addWidget(title)

        image = QLabel()
        image.setAlignment(Qt.AlignCenter)
        pixmap = QPixmap()
        try:
            import base64

            raw = image_base64.split("base64,", 1)[-1]
            pixmap.loadFromData(base64.b64decode(raw))
        except Exception:
            pixmap = QPixmap()
        if pixmap.isNull():
            image.setText("(không hiện được ảnh — nhìn cửa sổ trình duyệt)")
            image.setStyleSheet(f"color:{colors['bad']};")
        else:
            image.setPixmap(pixmap.scaled(
                pixmap.width() * 2, pixmap.height() * 2,
                Qt.KeepAspectRatio, Qt.SmoothTransformation,
            ))
        layout.addWidget(image)

        self.entry = QLineEdit()
        self.entry.setAlignment(Qt.AlignCenter)
        self.entry.setMaxLength(8)
        self.entry.setFont(mono_font(22))
        self.entry.returnPressed.connect(self._submit)
        layout.addWidget(self.entry)

        self.countdown = QLabel()
        self.countdown.setAlignment(Qt.AlignCenter)
        self.countdown.setStyleSheet(f"color:{colors['ink3']};")
        layout.addWidget(self.countdown)

        row = QHBoxLayout()
        send = QPushButton("Gửi")
        send.setDefault(True)
        send.clicked.connect(self._submit)
        skip = QPushButton("Bỏ qua, để AI làm tiếp")
        skip.clicked.connect(self.reject)
        row.addStretch(1)
        row.addWidget(skip)
        row.addWidget(send)
        layout.addLayout(row)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(1000)
        self._tick()

        self.entry.setFocus()

    def _tick(self):
        self.countdown.setText(f"Tự bỏ qua sau {self.remaining}s")
        if self.remaining <= 0:
            self._timer.stop()
            self.reject()
            return
        self.remaining -= 1

    def _submit(self):
        text = self.entry.text().strip()
        if not text:
            return
        self.answer = text
        self._timer.stop()
        self.accept()


# ----------------------------------------------------------------------------
# cửa sổ chính
# ----------------------------------------------------------------------------

class MainWindow(QWidget):
    def __init__(self, colors):
        super().__init__()
        self.colors = colors
        self.settings = load_settings()
        self.worker = None
        self.thread = None
        self.captcha_dialog = None
        self.stats = RunStats()
        self.started_at = None
        self.finished_at = None
        self.running_serials = {}
        self.log = LogBuffer(limit=LOG_LIMIT_LINES * 3)
        self.update_thread = None
        self.update_worker = None
        self.download_thread = None
        self.download_worker = None
        self.pending_release = None
        self._update_quiet = True

        self.setWindowTitle("Check Active — ONEWAY")
        self.resize(1180, 800)
        self.setMinimumSize(940, 620)

        self._build()
        self._apply_style()
        self._load_existing()
        self._fill_table(read_results())
        self._refresh_stats()

        self._ticker = QTimer(self)
        self._ticker.timeout.connect(self._tick)
        self._ticker.start(TICK_MS)

        # Hoi GitHub sau khi cua so da hien, khong lam cham luc mo app
        if self.settings.get("check_updates", True):
            QTimer.singleShot(1500, lambda: self._check_updates(quiet=True))

    # ---------- dựng giao diện ----------

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_toolbar())
        root.addWidget(self._build_update_banner())

        body = QHBoxLayout()
        body.setContentsMargins(16, 14, 16, 14)
        body.setSpacing(14)
        body.addWidget(self._build_sidebar())
        body.addWidget(self._build_main(), 1)

        holder = QWidget()
        holder.setLayout(body)
        root.addWidget(holder, 1)
        root.addWidget(self._build_statusbar())

    def _build_toolbar(self):
        bar = QFrame()
        bar.setObjectName("toolbar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(16, 10, 16, 10)

        title = QLabel("Check Active")
        font = title.font()
        font.setPointSize(14)
        font.setBold(True)
        title.setFont(font)
        layout.addWidget(title)

        subtitle = QLabel(f"ONEWAY · v{__version__}")
        subtitle.setObjectName("muted")
        layout.addWidget(subtitle)
        layout.addStretch(1)

        self.thread_badge = QLabel()
        self.thread_badge.setObjectName("muted")
        layout.addWidget(self.thread_badge)

        self.update_check_button = QPushButton("Kiểm tra bản mới")
        self.update_check_button.setObjectName("ghost")
        self.update_check_button.clicked.connect(lambda: self._check_updates(quiet=False))
        layout.addWidget(self.update_check_button)

        settings_button = QPushButton("⚙  Cài đặt")
        settings_button.setObjectName("ghost")
        settings_button.clicked.connect(self._open_settings)
        layout.addWidget(settings_button)
        return bar

    def _build_update_banner(self):
        """Dải báo có bản mới. Ẩn cho tới khi thật sự có bản mới."""
        bar = QFrame()
        bar.setObjectName("banner")
        bar.setVisible(False)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(16, 10, 16, 10)
        layout.setSpacing(10)

        self.update_text = QLabel()
        self.update_text.setObjectName("bannerText")
        layout.addWidget(self.update_text)
        layout.addStretch(1)

        self.update_notes_button = QPushButton("Có gì mới")
        self.update_notes_button.setObjectName("ghost")
        self.update_notes_button.clicked.connect(self._show_release_notes)
        layout.addWidget(self.update_notes_button)

        self.update_download_button = QPushButton("Tải bản mới")
        self.update_download_button.setObjectName("primary")
        self.update_download_button.clicked.connect(self._download_update)
        layout.addWidget(self.update_download_button)

        dismiss = QPushButton("✕")
        dismiss.setObjectName("ghost")
        dismiss.setFixedWidth(34)
        dismiss.setToolTip("Ẩn thông báo này")
        dismiss.clicked.connect(lambda: self.update_banner.setVisible(False))
        layout.addWidget(dismiss)

        self.update_banner = bar
        return bar

    def _build_sidebar(self):
        side = QFrame()
        side.setObjectName("card")
        side.setFixedWidth(240)
        layout = QVBoxLayout(side)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(8)

        label = QLabel("Dán serial — mỗi dòng một cái")
        label.setObjectName("muted")
        layout.addWidget(label)

        self.serial_input = QPlainTextEdit()
        self.serial_input.setFont(mono_font(12))
        self.serial_input.textChanged.connect(self._on_serials_changed)
        layout.addWidget(self.serial_input, 1)

        self.serial_count = QLabel("0 serial")
        self.serial_count.setObjectName("muted")
        layout.addWidget(self.serial_count)

        self.run_button = QPushButton("▶  Chạy check")
        self.run_button.setObjectName("primary")
        self.run_button.setMinimumHeight(36)
        self.run_button.clicked.connect(self._start)
        layout.addWidget(self.run_button)

        self.stop_button = QPushButton("■  Dừng")
        self.stop_button.setObjectName("ghost")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._stop)
        layout.addWidget(self.stop_button)

        layout.addSpacing(6)
        open_results = QPushButton("📂  Mở file kết quả")
        open_results.setObjectName("ghost")
        open_results.clicked.connect(self._open_results)
        layout.addWidget(open_results)
        return side

    def _build_main(self):
        main = QWidget()
        layout = QVBoxLayout(main)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        layout.addWidget(self._build_stat_strip())
        layout.addWidget(self._build_table(), 3)
        layout.addWidget(self._build_log(), 2)
        return main

    def _build_stat_strip(self):
        strip = QFrame()
        strip.setObjectName("card")
        layout = QHBoxLayout(strip)
        layout.setContentsMargins(4, 10, 4, 10)
        layout.setSpacing(0)

        self.kpi = {}
        self.kpi_caption = {}
        specs = [
            ("done", "Đã xong", None),
            ("active", "Đã active", "ok"),
            ("inactive", "Chưa active", "warn"),
            ("failed", "Lỗi", "bad"),
            ("rate", "Serial / phút", None),
            ("eta", "Còn lại", None),
        ]
        for index, (key, caption, tone) in enumerate(specs):
            if index:
                line = QFrame()
                line.setObjectName("vline")
                line.setFixedWidth(1)
                layout.addWidget(line)

            cell = QVBoxLayout()
            cell.setSpacing(1)
            value = QLabel("0")
            font = value.font()
            font.setPointSize(19)
            font.setBold(True)
            value.setFont(font)
            if tone:
                value.setStyleSheet(f"color:{self.colors[tone]};")
            caption_label = QLabel(caption)
            caption_label.setObjectName("muted")
            cell.addWidget(value)
            cell.addWidget(caption_label)
            self.kpi_caption[key] = caption_label

            cell.setContentsMargins(14, 0, 14, 0)
            wrapper = QWidget()
            wrapper.setLayout(cell)
            layout.addWidget(wrapper, 1)
            self.kpi[key] = value
        return strip

    def _build_table(self):
        holder = QFrame()
        holder.setObjectName("card")
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        head = QHBoxLayout()
        title = QLabel("Kết quả")
        font = title.font()
        font.setBold(True)
        title.setFont(font)
        head.addWidget(title)
        head.addStretch(1)

        self.filter_box = QLineEdit()
        self.filter_box.setPlaceholderText("Lọc serial hoặc trạng thái…")
        self.filter_box.setFixedWidth(240)
        self.filter_box.setClearButtonEnabled(True)
        head.addWidget(self.filter_box)

        self.only_bad = QCheckBox("Chỉ hiện lỗi")
        self.only_bad.toggled.connect(self._apply_filter)
        head.addWidget(self.only_bad)
        layout.addLayout(head)

        self.model = QStandardItemModel(0, 3, self)
        self.model.setHorizontalHeaderLabels(["Serial", "Ngày mua", "Trạng thái"])
        self.rows = {}

        self.proxy = QSortFilterProxyModel(self)
        self.proxy.setSourceModel(self.model)
        self.proxy.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self.proxy.setFilterKeyColumn(-1)
        self.filter_box.textChanged.connect(self._apply_filter)

        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableView.SelectRows)
        self.table.setEditTriggers(QTableView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(30)
        self.table.setShowGrid(False)
        self.table.setItemDelegateForColumn(COL_STATUS, StatusPillDelegate(self.colors, self))
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._table_menu)

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(COL_SERIAL, QHeaderView.Stretch)
        header.setSectionResizeMode(COL_DATE, QHeaderView.Stretch)
        header.setSectionResizeMode(COL_STATUS, QHeaderView.Fixed)
        header.resizeSection(COL_STATUS, 190)
        header.setHighlightSections(False)

        self.table.selectionModel().selectionChanged.connect(self._on_row_selected)

        layout.addWidget(self.table)
        return holder

    def _build_log(self):
        holder = QFrame()
        holder.setObjectName("card")
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        head = QHBoxLayout()
        title = QLabel("Đang chạy")
        font = title.font()
        font.setBold(True)
        title.setFont(font)
        head.addWidget(title)
        head.addStretch(1)

        self.only_selected_log = QCheckBox("Chỉ serial đang chọn")
        self.only_selected_log.setToolTip(
            "Chạy nhiều luồng thì các serial in xen kẽ nhau. Bật cái này để xem\n"
            "trọn vẹn quá trình của riêng serial đang chọn trong bảng."
        )
        self.only_selected_log.toggled.connect(self._rebuild_log)
        head.addWidget(self.only_selected_log)

        self.quiet_log = QCheckBox("Chỉ cảnh báo và lỗi")
        self.quiet_log.toggled.connect(self._rebuild_log)
        head.addWidget(self.quiet_log)
        layout.addLayout(head)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setFont(mono_font(11))
        self.log_view.setMaximumBlockCount(LOG_LIMIT_LINES)
        self.log_view.setObjectName("log")
        self.log_view.setLineWrapMode(QPlainTextEdit.NoWrap)
        layout.addWidget(self.log_view)
        return holder

    def _build_statusbar(self):
        bar = QFrame()
        bar.setObjectName("statusbar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(16, 8, 16, 10)

        self.status_label = QLabel("Sẵn sàng")
        self.status_label.setObjectName("muted")
        layout.addWidget(self.status_label)
        layout.addStretch(1)

        self.progress = QProgressBar()
        self.progress.setFixedWidth(280)
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        layout.addWidget(self.progress)
        return bar

    def _apply_style(self):
        c = self.colors
        self.setStyleSheet(f"""
            QWidget {{ color: {c['ink']}; font-size: 13px; }}
            MainWindow, QDialog {{ background: {c['bg']}; }}
            #toolbar {{ background: {c['surface']}; border-bottom: 1px solid {c['line']}; }}
            #statusbar {{ background: {c['surface']}; border-top: 1px solid {c['line']}; }}
            #card {{ background: {c['surface']}; border: 1px solid {c['line']}; border-radius: 10px; }}
            #banner {{ background: {c['okBg']}; border-bottom: 1px solid {c['ok']}; }}
            #bannerText {{ color: {c['ok']}; font-weight: 600; }}
            #vline {{ background: {c['line']}; }}
            #muted {{ color: {c['ink3']}; }}
            QLabel#muted {{ font-size: 12px; }}
            QPlainTextEdit, QLineEdit, QSpinBox {{
                background: {c['surface2']}; border: 1px solid {c['line']};
                border-radius: 7px; padding: 6px 8px; selection-background-color: {c['accent']};
            }}
            QPlainTextEdit#log {{
                background: {c['logBg']}; color: {c['logInk']};
                border: 1px solid {c['line']}; border-radius: 7px;
            }}
            QPushButton {{
                border-radius: 7px; padding: 7px 14px; font-weight: 600;
                background: {c['surface2']}; border: 1px solid {c['line']};
            }}
            QPushButton#primary {{ background: {c['accent']}; border: none; color: #FFFFFF; }}
            QPushButton#primary:disabled {{ background: {c['line']}; color: {c['ink3']}; }}
            QPushButton#ghost {{ background: transparent; }}
            QPushButton:hover {{ border-color: {c['ink3']}; }}
            QTableView {{
                background: {c['surface']}; alternate-background-color: {c['surface2']};
                border: none; gridline-color: {c['line']};
                selection-background-color: {c['surface2']}; selection-color: {c['ink']};
            }}
            QHeaderView::section {{
                background: {c['surface']}; border: none;
                border-bottom: 1px solid {c['line']};
                padding: 6px 10px; color: {c['ink3']};
                font-size: 11px; font-weight: 600;
            }}
            QProgressBar {{ background: {c['surface2']}; border: none; border-radius: 4px; height: 8px; }}
            QProgressBar::chunk {{ background: {c['accent']}; border-radius: 4px; }}
            QCheckBox {{ color: {c['ink2']}; font-size: 12px; }}
        """)

    # ---------- bảng ----------

    def _fill_table(self, rows):
        self.model.removeRows(0, self.model.rowCount())
        self.rows.clear()
        for serial, value in rows:
            self._upsert(serial, value)

    def _upsert(self, serial, value):
        kind = status_kind(value)
        if serial in self.rows:
            row = self.rows[serial]
        else:
            row = self.model.rowCount()
            self.rows[serial] = row
            self.model.insertRow(row, [QStandardItem(), QStandardItem(), QStandardItem()])
            self.model.item(row, COL_SERIAL).setFont(mono_font(12))
            self.model.item(row, COL_DATE).setFont(mono_font(12))

        self.model.item(row, COL_SERIAL).setText(serial)
        self.model.item(row, COL_DATE).setText(purchase_label(value))
        status_item = self.model.item(row, COL_STATUS)
        status_item.setText(status_label(value))
        status_item.setData(kind, ROLE_KIND)
        status_item.setData(value, ROLE_RAW)

    def _apply_filter(self):
        if self.only_bad.isChecked():
            # bắt đúng những nhãn thuộc nhóm lỗi
            self.proxy.setFilterKeyColumn(COL_STATUS)
            self.proxy.setFilterRegularExpression(
                "Check tay|Bị chặn IP|Proxy hỏng|Lỗi|serial ko hợp lệ|Quá giờ|Không đọc được"
            )
            return
        self.proxy.setFilterKeyColumn(-1)
        self.proxy.setFilterFixedString(self.filter_box.text())

    def _selected_serial(self):
        rows = self.table.selectionModel().selectedRows(COL_SERIAL)
        return rows[0].data() if rows else None

    def _on_row_selected(self, *_args):
        if self.only_selected_log.isChecked():
            self._rebuild_log()

    def _table_menu(self, point):
        index = self.table.indexAt(point)
        if not index.isValid():
            return
        serial = self.proxy.index(index.row(), COL_SERIAL).data()

        menu = QMenu(self)
        copy = QAction("Copy serial", self)
        copy.triggered.connect(lambda: QGuiApplication.clipboard().setText(serial))
        menu.addAction(copy)

        show_log = QAction("Xem log riêng của serial này", self)
        show_log.triggered.connect(lambda: self._focus_log_on(serial))
        menu.addAction(show_log)

        recheck = QAction("Chỉ check lại serial này", self)
        recheck.triggered.connect(lambda: self.serial_input.setPlainText(serial))
        menu.addAction(recheck)
        menu.exec(self.table.viewport().mapToGlobal(point))

    # ---------- số liệu ----------

    def _focus_log_on(self, serial):
        """Chọn hàng đó rồi bật lọc log — thấy trọn quá trình của một serial."""
        row = self.rows.get(serial)
        if row is not None:
            source = self.model.index(row, COL_SERIAL)
            self.table.selectRow(self.proxy.mapFromSource(source).row())
        self.only_selected_log.setChecked(True)
        self._rebuild_log()

    def _refresh_stats(self):
        self.kpi["done"].setText(str(self.stats.done))
        self.kpi["active"].setText(str(self.stats.active))
        self.kpi["inactive"].setText(str(self.stats.inactive))
        self.kpi["failed"].setText(str(self.stats.failed))

        elapsed = self._elapsed()
        self.kpi["rate"].setText(format_rate(serials_per_minute(self.stats.done, elapsed)))

        if self.finished_at is not None:
            # Chay xong roi thi "con lai" vo nghia — doi sang tong thoi gian ca me
            self.kpi_caption["eta"].setText("Tổng thời gian")
            self.kpi["eta"].setText(format_duration(elapsed))
        else:
            self.kpi_caption["eta"].setText("Còn lại")
            self.kpi["eta"].setText(format_duration(estimate_remaining_seconds(
                self.stats.done, self.stats.remaining, elapsed)))

        threads = int(self.settings.get("concurrency") or 1)
        self.thread_badge.setText(f"{threads} luồng")

    def _tick(self):
        """Đồng hồ đếm giây cho serial đang chạy + làm mới tốc độ/ETA."""
        if not self.running_serials:
            if self.started_at:
                self._refresh_stats()
            return
        now = time.monotonic()
        for serial, started in list(self.running_serials.items()):
            row = self.rows.get(serial)
            if row is None:
                continue
            self.model.item(row, COL_DATE).setText(f"{int(now - started)}s")
        self._refresh_stats()

    # ---------- vòng đời một mẻ chạy ----------

    def _on_serials_changed(self):
        count = len(parse_serials(self.serial_input.toPlainText()))
        self.serial_count.setText(f"{count} serial")

    def _load_existing(self):
        if SERIALS_FILE.exists():
            try:
                self.serial_input.setPlainText(SERIALS_FILE.read_text(encoding="utf-8-sig"))
            except OSError:
                pass
        self._on_serials_changed()

    # ---------- kiểm tra bản mới ----------

    def _check_updates(self, quiet=True):
        """quiet=True: chỉ lên tiếng khi có bản mới (lúc vừa mở app)."""
        repo = (self.settings.get("github_repo") or "").strip()
        token = (self.settings.get("github_token") or "").strip()

        if not repo:
            if not quiet:
                QMessageBox.information(
                    self, "Chưa cấu hình",
                    "Điền repo GitHub trong ⚙ Cài đặt trước đã.")
                self._open_settings()
            return
        if self.update_thread is not None:
            return

        if not quiet:
            self.update_check_button.setEnabled(False)
            self.update_check_button.setText("Đang kiểm tra…")

        self.update_thread = QThread(self)
        self.update_worker = UpdateWorker(repo, token, __version__)
        self.update_worker.moveToThread(self.update_thread)
        self.update_thread.started.connect(self.update_worker.run)
        # PHAI noi vao PHUONG THUC cua MainWindow, KHONG duoc dung lambda.
        # Lambda khong phai QObject nen Qt khong biet no thuoc luong nao, va se
        # chay thang tren luong worker. Moi thu Qt dung toi cua so (QMessageBox,
        # QDialog) ma chay ngoai luong chinh la macOS giet app ngay:
        #   "NSWindow should only be instantiated on the main thread!"
        # Noi vao phuong thuc cua MainWindow thi Qt tu xep hang ve luong chinh.
        self._update_quiet = quiet
        self.update_worker.found.connect(self._on_update_found)
        self.update_worker.up_to_date.connect(self._on_update_up_to_date)
        self.update_worker.failed.connect(self._on_update_failed)
        self.update_thread.start()

    def _end_update_thread(self):
        self.update_check_button.setEnabled(True)
        self.update_check_button.setText("Kiểm tra bản mới")
        if self.update_thread:
            self.update_thread.quit()
            self.update_thread.wait(3000)
            self.update_thread = None
        self.update_worker = None

    def _on_update_found(self, release):
        self.pending_release = release
        size = format_size(release.asset_size)
        suffix = f" · {size}" if size else ""
        self.update_text.setText(
            f"Có bản {release.version} — bạn đang dùng {__version__}{suffix}")
        self.update_download_button.setVisible(release.has_asset)
        self.update_banner.setVisible(True)
        self._end_update_thread()

    def _on_update_up_to_date(self):
        self._on_update_checked(self._update_quiet, None)

    def _on_update_failed(self, message):
        self._on_update_checked(self._update_quiet, message)

    def _on_update_checked(self, quiet, error_message):
        self._end_update_thread()
        if error_message:
            if not quiet:
                QMessageBox.warning(self, "Không kiểm tra được", error_message)
            else:
                self._append_log(f"⚠️ Không kiểm tra được bản mới: {error_message}")
            return
        if not quiet:
            QMessageBox.information(
                self, "Đã mới nhất",
                f"Bạn đang dùng bản {__version__}, chưa có bản nào mới hơn.")

    def _show_release_notes(self):
        release = self.pending_release
        if not release:
            return
        box = QMessageBox(self)
        box.setWindowTitle(f"Check Active {release.tag}")
        box.setText(release.title)
        box.setInformativeText(release.notes or "(bản này không ghi chú gì)")
        if release.page_url:
            open_page = box.addButton("Mở trên GitHub", QMessageBox.ActionRole)
            open_page.clicked.connect(
                lambda: QDesktopServices.openUrl(QUrl(release.page_url)))
        box.addButton("Đóng", QMessageBox.RejectRole)
        box.exec()

    def _download_update(self):
        release = self.pending_release
        if not release or self.download_thread is not None:
            return

        self.update_download_button.setEnabled(False)
        self.update_download_button.setText("Đang tải…")

        self.download_thread = QThread(self)
        self.download_worker = DownloadWorker(
            release, (self.settings.get("github_token") or "").strip())
        self.download_worker.moveToThread(self.download_thread)
        self.download_thread.started.connect(self.download_worker.run)
        self.download_worker.progress.connect(self._on_download_progress)
        self.download_worker.done.connect(self._on_download_done)
        self.download_worker.failed.connect(self._on_download_failed)
        self.download_thread.start()

    def _on_download_progress(self, done, total):
        if total:
            self.update_download_button.setText(f"Đang tải… {done * 100 // total}%")

    def _end_download_thread(self):
        self.update_download_button.setEnabled(True)
        self.update_download_button.setText("Tải bản mới")
        if self.download_thread:
            self.download_thread.quit()
            self.download_thread.wait(3000)
            self.download_thread = None
        self.download_worker = None

    def _on_download_done(self, path):
        self._end_download_thread()
        self.update_banner.setVisible(False)
        subprocess.run(["open", "-R", path], check=False)
        QMessageBox.information(
            self, "Đã tải xong",
            f"Đã lưu {Path(path).name} vào Downloads.\n\n"
            "Mở file .dmg đó, chạy CAI_DAT.command bên trong,\n"
            "rồi mở lại app.")

    def _on_download_failed(self, message):
        self._end_download_thread()
        QMessageBox.warning(self, "Tải không xong", message)

    # ---------- cài đặt ----------

    def _open_settings(self):
        dialog = SettingsDialog(self.settings, self.colors, self)
        if dialog.exec() == QDialog.Accepted:
            merged = dict(self.settings)
            merged.update(dialog.values())
            self.settings = save_settings(merged)
            self._append_log("💾 Đã lưu cài đặt")
            self._refresh_stats()

    def _open_results(self):
        if not RESULT_FILE.exists():
            QMessageBox.information(self, "Chưa có kết quả",
                                    "Chạy check xong rồi mở lại nhé.")
            return
        subprocess.run(["open", "-R", str(RESULT_FILE)], check=False)

    def _busy(self, busy):
        self.run_button.setEnabled(not busy)
        self.stop_button.setEnabled(busy)
        self.serial_input.setReadOnly(busy)

    def _start(self):
        serials = parse_serials(self.serial_input.toPlainText())
        if not serials:
            QMessageBox.warning(self, "Chưa có serial",
                                "Dán serial vào ô bên trái đã.")
            return
        if not self.settings["twocaptcha_key"]:
            QMessageBox.warning(self, "Thiếu key",
                                "Mở ⚙ Cài đặt và dán key 2captcha vào.")
            self._open_settings()
            return

        SERIALS_FILE.write_text("\n".join(serials) + "\n", encoding="utf-8")

        self._fill_table([(serial, WAITING_VALUE) for serial in serials])
        self.log.clear()
        self.log_view.clear()
        self.stats.reset(len(serials))
        self.started_at = time.monotonic()
        self.finished_at = None
        self.running_serials.clear()
        self.progress.setRange(0, len(serials))
        self.progress.setValue(0)
        self.status_label.setText(f"Đang chạy 0/{len(serials)}")
        self._refresh_stats()
        self._busy(True)
        self._append_log(f"=== Bắt đầu check {len(serials)} serial "
                         f"({self.settings.get('concurrency', 1)} luồng) ===")

        self.thread = QThread(self)
        self.worker = ScraperWorker(dict(self.settings))
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.log.connect(self._on_log)
        self.worker.result.connect(self._on_result)
        self.worker.captcha_wanted.connect(self._show_captcha)
        self.worker.captcha_done.connect(self._close_captcha)
        self.worker.finished.connect(self._on_finished)
        self.thread.start()

    def _stop(self):
        if self.worker:
            self.worker.request_stop()
        self.stop_button.setEnabled(False)
        self.status_label.setText("Đang dừng…")
        self._append_log("■ Đang dừng…")

    def _on_log(self, line, serial=""):
        started = parse_running_serial(line)
        if started:
            self.running_serials[started] = time.monotonic()
            if started in self.rows:
                self._upsert(started, RUNNING_VALUE)

        entry = self.log.add(line, serial or None)
        if entry is not None and self.log.matches(entry, **self._log_filters()):
            self._draw_log_line(entry)

    def _log_filters(self):
        return {
            "serial": self._selected_serial() if self.only_selected_log.isChecked() else None,
            "only_problems": self.quiet_log.isChecked(),
        }

    def _rebuild_log(self):
        """Vẽ lại khung log từ đầu theo bộ lọc hiện tại."""
        self.log_view.clear()
        for entry in self.log.view(**self._log_filters()):
            self._draw_log_line(entry)

    def _draw_log_line(self, entry):
        colour = {
            "ok": self.colors["logOk"],
            "warn": self.colors["logWarn"],
            "bad": self.colors["logBad"],
        }.get(entry.level, self.colors["logInk"])

        line = entry.line
        stripped = line.lstrip(" ")
        indent = "&nbsp;" * (len(line) - len(stripped))
        safe = indent + (stripped.replace("&", "&amp;")
                                 .replace("<", "&lt;").replace(">", "&gt;"))

        # Nhan serial o dau dong: khong bat mat, nhung du de biet dong nay cua ai
        prefix = ""
        if entry.serial and not self.only_selected_log.isChecked():
            prefix = (f'<span style="color:{self.colors["ink3"]}">'
                      f'{entry.serial}&nbsp;</span>')

        self.log_view.appendHtml(
            f'{prefix}<span style="color:{colour}">{safe}</span>'
        )

    def _append_log(self, line):
        """Dòng do chính giao diện sinh ra, không thuộc serial nào."""
        self._on_log(line, "")

    def _on_result(self, serial, value, done, total):
        self.running_serials.pop(serial, None)
        self._upsert(serial, value)
        self.stats.record(value)
        self.stats.total = max(total, self.stats.total)
        self.progress.setRange(0, max(total, 1))
        self.progress.setValue(done)
        self.status_label.setText(f"Đang chạy {done}/{total}")
        self._refresh_stats()

    def _show_captcha(self, image_base64, serial):
        self._close_captcha()
        dialog = CaptchaDialog(image_base64, serial, self.colors, self)
        self.captcha_dialog = dialog

        def finished(code):
            answer = dialog.answer if code == QDialog.Accepted else None
            if self.worker:
                self.worker.submit_captcha(answer)
            self.captcha_dialog = None

        dialog.finished.connect(finished)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _close_captcha(self):
        if self.captcha_dialog is not None:
            dialog, self.captcha_dialog = self.captcha_dialog, None
            dialog.close()

    def _elapsed(self):
        if not self.started_at:
            return 0
        end = self.finished_at or time.monotonic()
        return end - self.started_at

    def _on_finished(self):
        self.running_serials.clear()
        self.finished_at = time.monotonic()
        self._close_captcha()
        self._busy(False)
        self.progress.setValue(self.progress.maximum())
        self._refresh_stats()

        summary = run_summary(self.stats.done, self._elapsed(), self.stats.failed)
        self.status_label.setText(summary)
        self._append_log("")
        self._append_log(f"⏱  {summary}")
        if self.thread:
            self.thread.quit()
            self.thread.wait(3000)
            self.thread = None
        self.worker = None

    def closeEvent(self, event):
        if self.thread and self.thread.isRunning():
            answer = QMessageBox.question(
                self, "Đang chạy", "Vẫn đang check. Thoát luôn?",
                QMessageBox.Ok | QMessageBox.Cancel,
            )
            if answer != QMessageBox.Ok:
                event.ignore()
                return
            if self.worker:
                self.worker.request_stop()
            self.thread.quit()
            self.thread.wait(5000)
        event.accept()


APP_ICON = BASE_DIR / "app_icon_512.png"


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Check Active")
    if APP_ICON.exists():
        app.setWindowIcon(QIcon(str(APP_ICON)))
    window = MainWindow(palette_for(app))
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
