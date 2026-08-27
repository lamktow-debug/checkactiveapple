"""Phần lõi của app giao diện — không đụng gì tới Tkinter.

Tách ra để test được ở mọi nơi, kể cả máy không cài Tk.
"""

import csv
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
SERIALS_FILE = BASE_DIR / "serials.txt"
RESULT_FILE = BASE_DIR / "ketqua_apple_parallel.csv"
PROXY_FILE = BASE_DIR / "proxies.txt"

# Nhung gia tri nay la loi -> to do trong bang
ERROR_VALUES = frozenset({
    "Check tay",
    "Lỗi load trang",
    "Lỗi không xác định",
    "Không đọc được kết quả",
    "Bị chặn IP",
    "Proxy hỏng",
    "serial ko hợp lệ",
})
NEUTRAL_VALUES = frozenset({"Chưa xác thực", "Không thấy ngày mua", "đang chờ..."})

# Hộp nhập captcha tay đếm ngược bao lâu rồi tự bỏ qua.
# 12 giây đủ để nhìn ảnh và gõ 4 ký tự nếu bạn đang ngồi trước máy. Bản cũ để
# 60 giây, nghĩa là mỗi serial khó đứng yên trọn một phút khi bạn đi vắng.
MANUAL_CAPTCHA_WAIT_SECONDS = 12


def parse_serials(raw_text):
    """Tách serial từ ô dán: xuống dòng, dấu phẩy, tab, khoảng trắng đều được."""
    from check_active_v2 import normalize_serial

    seen = set()
    serials = []
    for chunk in (raw_text or "").replace(",", " ").replace("\t", " ").split():
        serial = normalize_serial(chunk)
        if serial and serial not in seen:
            seen.add(serial)
            serials.append(serial)
    return serials


def read_results(result_file=RESULT_FILE):
    """Đọc lại bảng kết quả cũ để mở app lên là thấy luôn."""
    path = Path(result_file)
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            rows = list(csv.reader(f))
    except OSError:
        return []
    return [row[:2] for row in rows[1:] if len(row) >= 2]


def row_tag(value):
    """Màu cho một dòng trong bảng: xanh = có ngày mua, đỏ = lỗi, xám = còn lại."""
    if value in ERROR_VALUES:
        return "bad"
    if not value or value in NEUTRAL_VALUES:
        return "wait"
    return "ok"


class QueueWriter:
    """Hứng print() của scraper rồi đẩy sang giao diện theo từng dòng."""

    def __init__(self, events):
        self.events = events
        self._buffer = ""

    def write(self, text):
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self.events.put(("log", line))
        return len(text)

    def flush(self):
        if self._buffer.strip():
            self.events.put(("log", self._buffer))
        self._buffer = ""
