"""Phần lõi của giao diện Qt — không import PySide6.

Tách ra đúng như app_core.py: mọi thứ ở đây chạy và test được trên máy không
cài Qt. app_qt.py chỉ còn việc vẽ.
"""

import re

from app_core import ERROR_VALUES, NEUTRAL_VALUES

# Dòng scraper in ra khi bắt đầu một serial: "🔍 Check: C02XG2JRD"
RUNNING_SERIAL_PATTERN = re.compile(r"Check:\s*([A-Z0-9]{6,20})\s*$")

WAITING_VALUE = "đang chờ..."
RUNNING_VALUE = "đang chạy..."

# Máy chưa active là kết quả HỢP LỆ, không phải lỗi — nhưng cũng không phải
# ngày mua. Cho nó màu vàng để phân biệt với hai nhóm kia.
WARN_VALUES = frozenset({"Chưa active"}) | NEUTRAL_VALUES - {WAITING_VALUE}


def status_kind(value):
    """Phân loại một ô kết quả thành 4 nhóm màu."""
    value = (value or "").strip()
    if not value or value in (WAITING_VALUE, RUNNING_VALUE):
        return "idle"
    if value in ERROR_VALUES or value.startswith("Quá giờ"):
        return "bad"
    if value in WARN_VALUES:
        return "warn"
    return "ok"


def status_label(value):
    """Chữ hiện trong nhãn trạng thái — ngắn hơn ô ngày mua."""
    kind = status_kind(value)
    if kind == "ok":
        return "Đã active"
    if kind == "idle":
        return "—"
    return (value or "").strip()


def purchase_label(value):
    """Cột 'Ngày mua': chỉ hiện ngày, còn lại để gạch ngang."""
    value = (value or "").strip()
    if status_kind(value) == "ok":
        return value
    if value in (WAITING_VALUE, RUNNING_VALUE):
        return value
    return "—"


def parse_running_serial(line):
    """Bắt serial đang chạy từ dòng log, để bảng biết hàng nào đang quay."""
    match = RUNNING_SERIAL_PATTERN.search(line or "")
    return match.group(1) if match else None


def log_level(line):
    """Mức độ của một dòng log, dùng để tô màu và để lọc."""
    line = line or ""
    if "❌" in line or "🛑" in line or "💀" in line:
        return "bad"
    if "⚠️" in line or "⏳" in line or "🔁" in line or "⏭️" in line:
        return "warn"
    if "✅" in line or "💾" in line or "✨" in line or "📄" in line:
        return "ok"
    return "info"


class LogEntry:
    """Một dòng log, kèm serial đã sinh ra nó và mức độ."""

    __slots__ = ("line", "serial", "level")

    def __init__(self, line, serial=None):
        self.line = line
        self.serial = serial or None
        self.level = log_level(line)

    def __repr__(self):
        return f"LogEntry({self.line!r}, {self.serial!r})"


class LogBuffer:
    """Giữ log cả mẻ chạy và cắt ra theo serial hoặc theo mức độ.

    Chạy 2-3 luồng thì các worker in ra chung một stdout, dòng đan xen nhau.
    Nhìn cả đống thì tưởng thiếu, thật ra chỉ là nằm lẫn chỗ.
    """

    def __init__(self, limit=6000):
        self.limit = limit
        self.entries = []

    def add(self, line, serial=None):
        """Thêm một dòng. Trả về LogEntry, hoặc None nếu dòng rỗng."""
        if not (line or "").strip():
            return None
        entry = LogEntry(line, serial)
        self.entries.append(entry)
        if len(self.entries) > self.limit:
            del self.entries[: len(self.entries) - self.limit]
        return entry

    def clear(self):
        self.entries.clear()

    def matches(self, entry, serial=None, only_problems=False):
        """Dòng này có lọt qua bộ lọc đang bật không."""
        if serial and entry.serial != serial:
            return False
        if only_problems and entry.level not in ("warn", "bad"):
            return False
        return True

    def view(self, serial=None, only_problems=False):
        return [
            entry for entry in self.entries
            if self.matches(entry, serial=serial, only_problems=only_problems)
        ]

    def serials(self):
        """Các serial đã từng xuất hiện, giữ nguyên thứ tự gặp."""
        seen = []
        known = set()
        for entry in self.entries:
            if entry.serial and entry.serial not in known:
                known.add(entry.serial)
                seen.append(entry.serial)
        return seen


class SignalWriter:
    """Hứng print() của scraper rồi bắn từng dòng qua Signal.

    Đọc CURRENT_SERIAL ngay lúc ghi: lúc này vẫn đang ở trong asyncio task của
    serial đó, nên nhãn luôn đúng kể cả khi 3 luồng in xen kẽ nhau.
    """

    def __init__(self, emit_line):
        self._emit = emit_line
        self._buffer = ""

    def _tag(self):
        try:
            from check_active_v2 import CURRENT_SERIAL

            return CURRENT_SERIAL.get() or ""
        except Exception:
            return ""

    def write(self, text):
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self._emit(line, self._tag())
        return len(text)

    def flush(self):
        if self._buffer.strip():
            self._emit(self._buffer, self._tag())
        self._buffer = ""

    def isatty(self):
        return False


class RunStats:
    """Đếm kết quả để vẽ dải số liệu phía trên bảng."""

    def __init__(self, total=0):
        self.total = total
        self.done = 0
        self.active = 0
        self.inactive = 0
        self.failed = 0

    def reset(self, total=0):
        self.__init__(total)

    def record(self, value):
        kind = status_kind(value)
        if kind == "idle":
            return
        self.done += 1
        if kind == "ok":
            self.active += 1
        elif kind == "warn":
            self.inactive += 1
        else:
            self.failed += 1

    @property
    def remaining(self):
        return max(self.total - self.done, 0)


def serials_per_minute(done, elapsed_seconds):
    """Tốc độ thực đo, trả về None khi chưa đủ dữ liệu để nói gì."""
    if done <= 0 or elapsed_seconds <= 0:
        return None
    return done * 60.0 / elapsed_seconds


def estimate_remaining_seconds(done, remaining, elapsed_seconds):
    """Còn bao lâu nữa, dựa trên tốc độ đã đo được."""
    if done <= 0 or elapsed_seconds <= 0:
        return None
    if remaining <= 0:
        return 0
    return remaining * (elapsed_seconds / done)


def format_duration(seconds):
    """Giây -> '2:05' hoặc '1:02:05'. None -> '—'."""
    if seconds is None:
        return "—"
    seconds = int(round(seconds))
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def run_summary(done, elapsed_seconds, failed=0):
    """Câu tổng kết cuối mẻ: bao lâu, bao nhiêu serial, nhanh cỡ nào."""
    parts = [f"Xong {done} serial trong {format_duration(elapsed_seconds)}"]
    rate = serials_per_minute(done, elapsed_seconds)
    if rate:
        parts.append(f"{format_rate(rate)} serial/phút")
        parts.append(f"{elapsed_seconds / done:.0f}s mỗi serial")
    if failed:
        parts.append(f"{failed} lỗi")
    return " · ".join(parts)


def format_rate(rate):
    """Tốc độ -> '3,4' theo kiểu số Việt Nam. None -> '—'."""
    if rate is None:
        return "—"
    return f"{rate:.1f}".replace(".", ",")
