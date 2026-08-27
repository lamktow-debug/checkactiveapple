import argparse
import asyncio
import base64
import contextvars
import time
import csv
import os
import json
import random
import re
from pathlib import Path
from datetime import date
from typing import NamedTuple
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen
from openpyxl import Workbook
from playwright.async_api import async_playwright
from playwright_stealth import Stealth

# --- CẤU HÌNH ---
BASE_DIR = Path(__file__).resolve().parent
INPUT_FILE = BASE_DIR / 'serials.txt'
OUTPUT_FILE = BASE_DIR / 'ketqua_apple_final.csv'
SCREENSHOT_DIR = BASE_DIR / 'screenshots'
MAX_AUTO_RETRIES = 5 
BASE_URL = "https://checkcoverage.apple.com/coverage?locale=vi_VN"
CAPTCHA_SELECTOR = 'img.captcha-image, img[alt="captcha"]'
SERIAL_INPUT_SELECTOR = '#serial-number-input'
CAPTCHA_RELOAD_WAIT_MS = 250
CAPTCHA_RELOAD_POLL_MS = 60
CAPTCHA_RELOAD_MAX_WAIT_MS = 3000
CAPTCHA_INPUT_DELAY_MS = 20
SERIAL_FILL_VERIFY_WAIT_MS = 150
RESULT_WAIT_TIMEOUT_MS = 15000
RESULT_POLL_INTERVAL_MS = 100
# --- 2CAPTCHA ---
# Key BẮT BUỘC lấy từ biến môi trường, không hardcode vào file nữa:
#   export TWOCAPTCHA_API_KEY="..."
CAPTCHA_2CAPTCHA_API_KEY = os.getenv("TWOCAPTCHA_API_KEY", "")
CAPTCHA_2CAPTCHA_CREATE_TASK_URL = "https://api.2captcha.com/createTask"
CAPTCHA_2CAPTCHA_RESULT_URL = "https://api.2captcha.com/getTaskResult"
CAPTCHA_2CAPTCHA_REPORT_BAD_URL = "https://api.2captcha.com/reportIncorrect"
CAPTCHA_2CAPTCHA_BALANCE_URL = "https://api.2captcha.com/getBalance"
# Mã dễ có khi 2captcha xong sau ~3s. Hỏi sớm và hỏi dày hơn thì bắt được
# lúc nó vừa xong, thay vì ngủ đủ 2s rồi mới hỏi lần đầu.
# Trần chờ vẫn giữ nguyên: 0.8 + 40 x 0.5 = 20.8s (bản cũ 2 + 20 x 1 = 22s).
CAPTCHA_2CAPTCHA_FIRST_POLL_DELAY_SECONDS = 0.5
CAPTCHA_2CAPTCHA_POLL_INTERVAL_SECONDS = 0.5
CAPTCHA_2CAPTCHA_MAX_POLLS = 40
CAPTCHA_2CAPTCHA_TIMEOUT_SECONDS = 30
CAPTCHA_BUSY_WAIT_SECONDS = 10
CAPTCHA_CODE_LENGTH = 4          # goi y cho 2captcha
# Da tra tien cho mot ma roi thi CU GUI THU, de Apple phan xu dung/sai.
# Vut ma 5 ky tu ma khong thu lan nao = vut tien, va neu captcha doi khi
# dai 5 ky tu that thi minh se lap vo han.
CAPTCHA_MIN_LENGTH = 3
CAPTCHA_MAX_LENGTH = 6

# Mot serial keo qua lau thi bo qua, khoi treo ca run
SERIAL_TIMEOUT_SECONDS = int(os.getenv("SERIAL_TIMEOUT", "120"))
# Sau bao nhieu lan OCR truot thi hoi nguoi dung nhap tay (app moi dung duoc)
MANUAL_CAPTCHA_AFTER = int(os.getenv("MANUAL_CAPTCHA_AFTER", "2"))
# CHI hoi tay DUNG MOT LAN moi serial. Ban cu hoi lai o ca lan thu 4 va 5,
# ba lan dem nguoc lien tiep la cach nhanh nhat cham tran SERIAL_TIMEOUT.
MANUAL_CAPTCHA_MAX_ASKS = int(os.getenv("MANUAL_CAPTCHA_MAX_ASKS", "1"))
# App giao dien gan ham vao day: async (image_base64, serial) -> str | None
MANUAL_CAPTCHA_HANDLER = None

# --- OCR chay tren may (ddddocr) ---
# Thu doan bang model tren may TRUOC, truot thi moi mua ma cua 2captcha.
# OCR may mat ~0.2s va mien phi; 2captcha mat ~9s va tinh tien tung ma.
# Apple la trong tai mien phi: sai thi no bao ngay, nen doan hut cung khong hai
# gi ngoai mot lan doi captcha.
LOCAL_OCR_ENABLED = os.getenv("LOCAL_OCR", "1") == "1"
# Doan hut may lan thi thoi, chuyen sang 2captcha. De cao qua thi moi lan truot
# lai ton mot lan tai lai trang, tuc la them mot request vao Apple.
# Chi 1 lan. Do that: 2 lan lam 18 serial mat 10 phut, con 2captcha chi 6 phut
# — moi lan truot deu phai mua ma 2captcha SAU DO, nen thu 2 lan la nhan doi
# phan lang phi ma khong nhan doi co hoi.
LOCAL_OCR_MAX_ATTEMPTS = int(os.getenv("LOCAL_OCR_ATTEMPTS", "1"))
# app_settings.apply_settings gan doi tuong LocalOcr vao day
LOCAL_OCR = None


def get_local_ocr():
    """Lay doi tuong OCR may, tu tao neu chua co. None = khong dung OCR may."""
    global LOCAL_OCR
    if not LOCAL_OCR_ENABLED:
        return None
    if LOCAL_OCR is None:
        try:
            from ocr_local import LocalOcr

            LOCAL_OCR = LocalOcr()
        except Exception:
            return None
    return LOCAL_OCR


async def solve_captcha_locally(image_base64):
    """Giai captcha bang model tren may. None = khong duoc, dung 2captcha di."""
    ocr = get_local_ocr()
    if ocr is None:
        return None
    # solve() goi tien trinh con nen chan luong; day sang thread khac de vong
    # lap asyncio khong bi dung hinh (co the con luong khac dang chay serial).
    return await asyncio.to_thread(ocr.solve, image_base64)

# Serial ma task hien tai dang xu ly. Chay nhieu luong thi cac worker in ra
# CHUNG mot stdout, dong cua serial nay roi xuong duoi tieu de cua serial kia
# va khong the doc noi. ContextVar duoc sao chep rieng cho tung asyncio task,
# nen cho phep gan nhan dung serial vao tung dong ma khong phai sua tung print.
CURRENT_SERIAL = contextvars.ContextVar("current_serial", default=None)

# Lỗi phải dừng cả run: càng chạy càng mất tiền hoặc vô ích
CAPTCHA_2CAPTCHA_FATAL_ERRORS = frozenset({
    "ERROR_ZERO_BALANCE",
    "ERROR_KEY_DOES_NOT_EXIST",
    "ERROR_WRONG_USER_KEY",
    "ERROR_IP_NOT_ALLOWED",
    "ERROR_ACCOUNT_SUSPENDED",
})
# Lỗi tạm thời: chờ rồi thử lại, KHÔNG tính là OCR đoán sai
CAPTCHA_2CAPTCHA_BUSY_ERRORS = frozenset({
    "ERROR_NO_SLOT_AVAILABLE",
    "ERROR_TOO_MUCH_REQUESTS",
})

# --- CHỐNG CHẶN IP ---
PROXY_FILE = BASE_DIR / "proxies.txt"
SERIALS_PER_SESSION = int(os.getenv("SERIALS_PER_SESSION", "15"))
REQUEST_DELAY_MIN_SECONDS = float(os.getenv("REQUEST_DELAY_MIN", "3"))
REQUEST_DELAY_MAX_SECONDS = float(os.getenv("REQUEST_DELAY_MAX", "8"))
# Turbo mode: delay ngắn hơn đáng kể, đánh đổi bằng rủi ro bị chặn IP cao hơn.
# Bật qua app_settings hoặc biến môi trường TURBO_MODE=1.
TURBO_MODE = os.getenv("TURBO_MODE", "0") == "1"
TURBO_DELAY_MIN_SECONDS = 1.5
TURBO_DELAY_MAX_SECONDS = 4.0
# Chặn ảnh/font/video để tiết kiệm băng thông proxy (proxy tính tiền theo GB).
# Captcha của Apple là data URI nhúng thẳng trong HTML nên không bị ảnh hưởng,
# nhưng cứ bật thử vài serial rồi kiểm tra captcha trước khi dùng thật.
BLOCK_ASSETS = os.getenv("BLOCK_ASSETS", "0") == "1"
BLOCKED_RESOURCE_TYPES = frozenset({"image", "font", "media"})
# KHONG chan anh cua chinh trang check: neu captcha la anh tai qua mang thi
# chan mat no se chup ra o anh vo va OCR doan lung tung.
CAPTCHA_SAFE_HOST = "checkcoverage.apple.com"
BLOCK_BASE_DELAY_SECONDS = 60
BLOCK_MAX_DELAY_SECONDS = 900
MAX_BLOCK_RETRIES = 3
BLOCKED_STATUS_CODES = frozenset({403, 429, 503})
# Proxy free rat cham, cho lau hon mac dinh 30s
PAGE_GOTO_TIMEOUT_MS = int(os.getenv("PAGE_GOTO_TIMEOUT_MS", "45000"))
PROXY_FAILURE_DELAY_SECONDS = 3
MAX_PROXY_FAILURES = 3
# Chrome tu dong bop CPU/timer cua cua so khong duoc focus. Chay 4 cua so
# song song ma 3 cai bi bop thi nhin y het nhu dang chay tuan tu.
BROWSER_LAUNCH_ARGS = [
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-features=CalculateNativeWinOcclusion",
]
PAGE_CLOSED_MARKERS = (
    "Target page, context or browser has been closed",
    "Target closed",
    "Browser has been closed",
    "Connection closed",
)
PROXY_ERROR_MARKERS = (
    "ERR_TUNNEL_CONNECTION_FAILED",
    "ERR_PROXY_CONNECTION_FAILED",
    "ERR_CONNECTION_RESET",
    "ERR_CONNECTION_CLOSED",
    "ERR_TIMED_OUT",
    "ERR_EMPTY_RESPONSE",
)

# --- SERIAL ---
SERIAL_PATTERN = re.compile(r"^[A-Z0-9]{8,12}$")
# Kết quả cũ thuộc nhóm này thì chạy lại, còn lại thì bỏ qua khi resume
RETRYABLE_RESULTS = frozenset({
    "Check tay",
    "Lỗi load trang",
    "Lỗi không xác định",
    "Không đọc được kết quả",
    "Bị chặn IP",
    "Proxy hỏng",
    "Quá giờ, bỏ qua",
})


class CaptchaServiceError(Exception):
    """Lỗi 2captcha nghiêm trọng (hết tiền, sai key) — phải dừng cả run."""


class CaptchaBusyError(Exception):
    """2captcha đang quá tải — chờ rồi thử lại, không tính là đoán sai."""


class BlockedError(Exception):
    """Apple chặn IP (403/429/503) — cần đổi IP và nghỉ cho nguội."""


class ProxyFailure(BlockedError):
    """Proxy chậm/chết, chưa chắc đã bị Apple chặn.

    Cũng phải đổi IP như BlockedError, nhưng KHÔNG cần nghỉ 60s: proxy free
    hỏng suốt, nghỉ dài mỗi lần thì cả run đứng hình.
    """


class CaptchaSolution(NamedTuple):
    """Kết quả giải captcha kèm taskId để còn báo sai đòi hoàn tiền."""

    code: str
    task_id: object = None

APPLE_PURCHASE_DATE_PATTERN = re.compile(
    r"Đã mua\s+(\d{1,2})\s+tháng\s+(\d{1,2}),\s*(\d{4})",
    re.IGNORECASE,
)
UNVERIFIED_PURCHASE_TEXT = "Ngày mua chưa được xác thực"
INVALID_SERIAL_TEXT = "Số sê-ri bạn đã nhập không hợp lệ. Vui lòng thử lại."
INVALID_SERIAL_INPUT_TEXT = "Vui lòng nhập số sê-ri hợp lệ."
# CHI dat o day nhung cau Apple that su BAO LOI.
# TUYET DOI khong dua vao "Nhap ma trong anh" hay "Lam moi ma": do la NHAN cua
# chinh o nhap captcha va nut doi ma, luon co tren trang khi form dang hien.
# Coi chung la loi = vua bam gui ma dung xong, trang chua kip chuyen, da vo doan
# "sai ma", vut di mot ma DUNG roi mua ma khac. Dung la trieu chung 26/08:
# trang ket qua hien ra day du ma app van bao sai.
CAPTCHA_MISMATCH_TEXTS = (
    "Mã bạn đã nhập không khớp với hình ảnh",
    "The code you entered does not match the image",
)

def create_stealth_playwright_context():
    """Tạo Playwright context manager đã bật stealth cho mọi page/context."""
    return Stealth().use_async(async_playwright())

def normalize_captcha_code(code):
    """Chỉ giữ chữ Latin ASCII và số trong kết quả OCR captcha."""
    return re.sub(r"[^A-Za-z0-9]", "", code or "").upper()

def strip_data_uri_prefix(base64_str):
    """Bỏ prefix data URI nếu ảnh captcha được lấy trực tiếp từ thuộc tính src."""
    if "base64," in (base64_str or ""):
        return base64_str.split("base64,", 1)[1]
    return base64_str or ""

def post_2captcha_json(url, payload):
    """Gửi request JSON tới 2captcha và trả về response đã parse."""
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=CAPTCHA_2CAPTCHA_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))

def classify_2captcha_error(response):
    """Phân loại lỗi 2captcha: fatal -> dừng run, busy -> chờ, còn lại -> trả code lỗi."""
    if response.get("errorId") == 0:
        return None

    error_code = response.get("errorCode") or "UNKNOWN_ERROR"
    if error_code in CAPTCHA_2CAPTCHA_FATAL_ERRORS:
        raise CaptchaServiceError(error_code)
    if error_code in CAPTCHA_2CAPTCHA_BUSY_ERRORS:
        raise CaptchaBusyError(error_code)
    return error_code


async def solve_captcha_task(base64_str):
    """Giải captcha qua 2captcha, trả về CaptchaSolution(code, task_id).

    Khác bản cũ: lỗi không còn bị nuốt thành chuỗi rỗng. Hết tiền / sai key ném
    CaptchaServiceError để dừng cả run, quá tải ném CaptchaBusyError để chờ,
    và mọi lỗi khác đều được in ra thay vì im lặng đốt tiền retry.
    """
    image_body = strip_data_uri_prefix(base64_str)
    if not image_body:
        print("  ⚠️ Không lấy được ảnh captcha")
        return CaptchaSolution("", None)

    create_payload = {
        "clientKey": CAPTCHA_2CAPTCHA_API_KEY,
        "task": {
            "type": "ImageToTextTask",
            "body": image_body,
            "phrase": False,
            "case": False,
            "numeric": 0,
            "math": False,
            "minLength": CAPTCHA_CODE_LENGTH,
            "maxLength": CAPTCHA_CODE_LENGTH,
            "comment": "Enter the 4-character captcha code.",
        },
        "languagePool": "en",
    }

    try:
        create_response = await asyncio.to_thread(
            post_2captcha_json,
            CAPTCHA_2CAPTCHA_CREATE_TASK_URL,
            create_payload,
        )
    except (HTTPError, URLError, TimeoutError, ValueError, OSError) as error:
        print(f"  ⚠️ Không gọi được 2captcha (createTask): {error}")
        return CaptchaSolution("", None)

    error_code = classify_2captcha_error(create_response)
    if error_code:
        print(f"  ⚠️ 2captcha createTask lỗi: {error_code}")
        return CaptchaSolution("", None)

    task_id = create_response.get("taskId")
    if not task_id:
        print("  ⚠️ 2captcha không trả taskId")
        return CaptchaSolution("", None)

    # Lần poll đầu tiên luôn "processing", chờ trước cho đỡ tốn 1 vòng gọi API
    await asyncio.sleep(CAPTCHA_2CAPTCHA_FIRST_POLL_DELAY_SECONDS)

    for _ in range(CAPTCHA_2CAPTCHA_MAX_POLLS):
        try:
            result_response = await asyncio.to_thread(
                post_2captcha_json,
                CAPTCHA_2CAPTCHA_RESULT_URL,
                {"clientKey": CAPTCHA_2CAPTCHA_API_KEY, "taskId": task_id},
            )
        except (HTTPError, URLError, TimeoutError, ValueError, OSError) as error:
            print(f"  ⚠️ Không gọi được 2captcha (getTaskResult): {error}")
            return CaptchaSolution("", task_id)

        error_code = classify_2captcha_error(result_response)
        if error_code:
            print(f"  ⚠️ 2captcha getTaskResult lỗi: {error_code}")
            return CaptchaSolution("", task_id)

        if result_response.get("status") == "ready":
            solution = result_response.get("solution") or {}
            return CaptchaSolution(normalize_captcha_code(solution.get("text", "")), task_id)

        await asyncio.sleep(CAPTCHA_2CAPTCHA_POLL_INTERVAL_SECONDS)

    print("  ⚠️ 2captcha giải quá lâu, bỏ mã này")
    return CaptchaSolution("", task_id)


async def report_bad_captcha(task_id):
    """Báo 2captcha giải sai để được hoàn tiền. Tiền thật, đừng bỏ qua."""
    if not task_id:
        return
    try:
        await asyncio.to_thread(
            post_2captcha_json,
            CAPTCHA_2CAPTCHA_REPORT_BAD_URL,
            {"clientKey": CAPTCHA_2CAPTCHA_API_KEY, "taskId": task_id},
        )
    except Exception:
        pass


async def check_2captcha_balance():
    """Kiểm tra key + số dư TRƯỚC khi chạy, tránh đốt cả run vào lỗi im lặng."""
    if not CAPTCHA_2CAPTCHA_API_KEY:
        raise CaptchaServiceError("Chưa set biến môi trường TWOCAPTCHA_API_KEY")

    response = await asyncio.to_thread(
        post_2captcha_json,
        CAPTCHA_2CAPTCHA_BALANCE_URL,
        {"clientKey": CAPTCHA_2CAPTCHA_API_KEY},
    )
    error_code = classify_2captcha_error(response)
    if error_code:
        raise CaptchaServiceError(error_code)
    return float(response.get("balance") or 0)


async def get_captcha_image_base64(img_element):
    """Lấy ảnh captcha: ưu tiên data URI trong src, không có thì chụp thẳng element.

    Bản cũ giả định src luôn là data URI. Nếu Apple đổi sang URL thường thì
    2captcha nhận rác, trả sai, và mình trả tiền cho 5 lần retry vô nghĩa.
    """
    src = await img_element.get_attribute("src")
    if isinstance(src, str) and "base64," in src:
        return strip_data_uri_prefix(src)
    # Khong phai data URI -> phai chup element. Neu dang bat BLOCK_ASSETS thi
    # rat co the anh captcha bi chan mat, chup ra o vuong vo -> OCR sai lien tuc.
    if BLOCK_ASSETS:
        print("  ⚠️ Captcha là ảnh tải qua mạng, không phải data URI.")
        print("     Nếu OCR sai liên tục thì tắt BLOCK_ASSETS: unset BLOCK_ASSETS")
    image_bytes = await img_element.screenshot()
    return base64.b64encode(image_bytes).decode("ascii")


# --- SERIAL ---

def normalize_serial(raw_serial):
    """Chuẩn hoá serial: bỏ ký tự lạ (kể cả BOM của file Windows) và viết hoa."""
    return re.sub(r"[^A-Za-z0-9]", "", raw_serial or "").upper()


def is_valid_serial(serial):
    """Serial Apple là 8-12 ký tự chữ/số. Lọc sớm để khỏi tốn tiền captcha."""
    return bool(SERIAL_PATTERN.match(serial or ""))


def load_serials(input_file=INPUT_FILE):
    """Đọc serial bằng utf-8-sig (bỏ BOM), chuẩn hoá, loại trùng, giữ thứ tự."""
    seen = set()
    serials = []
    with open(input_file, "r", encoding="utf-8-sig") as serial_file:
        for line in serial_file:
            serial = normalize_serial(line)
            if serial and serial not in seen:
                seen.add(serial)
                serials.append(serial)
    return serials


def load_done_serials(output_file=OUTPUT_FILE):
    """Đọc kết quả đã có để chạy tiếp sau khi bị chặn giữa chừng."""
    output_path = Path(output_file)
    if not output_path.exists():
        return {}
    with open(output_path, "r", encoding="utf-8-sig", newline="") as csv_file:
        rows = list(csv.reader(csv_file))
    return {row[0]: row[1] for row in rows[1:] if len(row) >= 2}


def needs_check(serial, done_results):
    """Chưa có kết quả, hoặc kết quả cũ là lỗi, thì mới cần check lại."""
    previous = done_results.get(serial)
    return previous is None or previous in RETRYABLE_RESULTS


# --- PROXY / CHỐNG CHẶN ---

def parse_proxy_line(line):
    """Đọc 1 dòng proxy: scheme://user:pass@host:port hoặc host:port:user:pass."""
    line = (line or "").strip()
    if not line or line.startswith("#"):
        return None

    if "://" in line:
        parsed = urlparse(line)
        if not parsed.hostname or not parsed.port:
            return None
        proxy = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
        if parsed.username:
            proxy["username"] = unquote(parsed.username)
            proxy["password"] = unquote(parsed.password or "")
        return proxy

    parts = line.split(":")
    if len(parts) == 2:
        return {"server": f"http://{parts[0]}:{parts[1]}"}
    if len(parts) == 4:
        return {
            "server": f"http://{parts[0]}:{parts[1]}",
            "username": parts[2],
            "password": parts[3],
        }
    return None


def load_proxies(proxy_file=PROXY_FILE):
    """Đọc danh sách proxy; không có file thì chạy bằng IP thật."""
    proxy_path = Path(proxy_file)
    if not proxy_path.exists():
        return []
    with open(proxy_path, "r", encoding="utf-8-sig") as f:
        parsed = [parse_proxy_line(line) for line in f]
    return [proxy for proxy in parsed if proxy]


def proxy_for_index(proxies, index):
    """Chọn proxy theo vòng tròn; danh sách rỗng thì dùng IP thật."""
    if not proxies:
        return None
    return proxies[index % len(proxies)]


def describe_proxy(proxy):
    """Tên proxy để in log, không lộ mật khẩu."""
    return proxy["server"] if proxy else "IP thật"


def is_blocked_error(error):
    """Nhận diện lỗi mạng/proxy nên coi như bị chặn."""
    message = str(error)
    return any(marker in message for marker in PROXY_ERROR_MARKERS)


def is_page_closed_error(error):
    """Trang/browser đã đóng — đang tắt chương trình, đừng retry cho phí."""
    message = str(error)
    return any(marker in message for marker in PAGE_CLOSED_MARKERS)


def pick_proxy(proxies, index, dead_servers=()):
    """Chọn proxy theo vòng tròn, bỏ qua những cái đã bị đánh dấu chết."""
    if not proxies:
        return None
    usable = [proxy for proxy in proxies if proxy["server"] not in dead_servers]
    if not usable:
        return None
    return usable[index % len(usable)]


class BlockThrottle:
    """Đếm số lần bị chặn và giãn dần thời gian nghỉ (exponential backoff)."""

    def __init__(self, base_delay=BLOCK_BASE_DELAY_SECONDS, max_delay=BLOCK_MAX_DELAY_SECONDS):
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.block_count = 0

    def next_delay(self):
        """60s -> 120s -> 240s ... tối đa 15 phút."""
        return min(self.base_delay * (2 ** max(self.block_count - 1, 0)), self.max_delay)

    async def on_block(self, reason=""):
        self.block_count += 1
        delay = self.next_delay()
        print(f"  🛑 Bị chặn lần {self.block_count} ({reason}) — nghỉ {delay:.0f}s")
        await asyncio.sleep(delay)

    def on_success(self):
        self.block_count = 0


async def polite_delay():
    """Nghỉ ngẫu nhiên giữa các serial cho đỡ giống bot.

    Turbo mode dùng delay ngắn hơn (1.5-4s thay vì 3-8s).
    """
    if TURBO_MODE:
        delay_min, delay_max = TURBO_DELAY_MIN_SECONDS, TURBO_DELAY_MAX_SECONDS
    else:
        delay_min, delay_max = REQUEST_DELAY_MIN_SECONDS, REQUEST_DELAY_MAX_SECONDS
    await asyncio.sleep(random.uniform(delay_min, delay_max))


def should_block_request(resource_type, url):
    """Có chặn request này không.

    Chặn ảnh/font/video của CDN (nặng, vô dụng với mình) nhưng luôn cho qua
    ảnh của chính trang check, phòng khi captcha là ảnh tải qua mạng.
    """
    if resource_type not in BLOCKED_RESOURCE_TYPES:
        return False
    if resource_type == "image" and CAPTCHA_SAFE_HOST in (url or ""):
        return False
    return True


async def launch_browser(playwright, headless=False):
    """Mở trình duyệt và tắt các tính năng bóp hiệu năng cửa sổ nền.

    Cũng ưu tiên channel="chromium" vì bản headless shell riêng hay thiếu.
    """
    options = {"headless": headless, "args": BROWSER_LAUNCH_ARGS}
    try:
        return await playwright.chromium.launch(channel="chromium", **options)
    except Exception:
        return await playwright.chromium.launch(**options)


class RunTimer:
    """Đo xem có THẬT SỰ chạy song song không, thay vì đoán.

    average_concurrency = tổng thời gian xử lý / thời gian đồng hồ.
    Chạy 4 luồng thật thì ra ~4. Ra ~1 nghĩa là đang chạy tuần tự.
    """

    def __init__(self):
        self.started_at = time.monotonic()
        self.busy_seconds = 0.0
        self.finished = 0

    def record(self, seconds):
        self.busy_seconds += seconds
        self.finished += 1

    def elapsed(self):
        return max(time.monotonic() - self.started_at, 0.001)

    def average_concurrency(self):
        return self.busy_seconds / self.elapsed()

    def rate_per_minute(self):
        return self.finished / (self.elapsed() / 60)

    def summary(self):
        return (
            f"⏱  {self.finished} serial trong {self.elapsed() / 60:.1f} phút "
            f"({self.rate_per_minute():.1f} serial/phút) — "
            f"song song thực tế: {self.average_concurrency():.1f} luồng"
        )


async def block_heavy_assets(route):
    """Huỷ request ảnh/font/video nặng, cho qua mọi thứ còn lại."""
    try:
        request = route.request
        if should_block_request(request.resource_type, request.url):
            await route.abort()
        else:
            await route.continue_()
    except Exception:
        pass


async def create_browser_context(browser, proxy=None):
    """Tạo context mới: proxy riêng + locale/timezone khớp trang vi_VN.

    Context trần (bản cũ) chạy en-US/UTC trong khi mở trang vi_VN — đó là một
    dấu hiệu lệch rất dễ nhận ra.
    """
    context_options = {
        "locale": "vi-VN",
        "timezone_id": "Asia/Ho_Chi_Minh",
        "viewport": {"width": 1280, "height": 900},
    }
    if proxy:
        context_options["proxy"] = proxy
    context = await browser.new_context(**context_options)
    if BLOCK_ASSETS:
        await context.route("**/*", block_heavy_assets)
    return context


async def reload_captcha(page):
    """Bấm nút đổi captcha rồi chờ tới khi ảnh ĐỔI THẬT.

    Bản cũ ngủ cứng 250ms: mạng nhanh thì phí, mạng chậm thì gửi lại đúng cái
    ảnh cũ sang 2captcha và mua lại y nguyên mã sai vừa rồi.
    """
    img = page.locator(CAPTCHA_SELECTOR).first
    try:
        old_src = await img.get_attribute("src")
    except Exception:
        old_src = None

    await page.locator('div.captcha-action button').first.click()

    if old_src is None:
        await page.wait_for_timeout(CAPTCHA_RELOAD_WAIT_MS)
        return

    waited_ms = 0
    while waited_ms < CAPTCHA_RELOAD_MAX_WAIT_MS:
        await page.wait_for_timeout(CAPTCHA_RELOAD_POLL_MS)
        waited_ms += CAPTCHA_RELOAD_POLL_MS
        try:
            if await img.get_attribute("src") != old_src:
                return
        except Exception:
            return


async def has_error_message(page):
    """Có thông báo lỗi đỏ đang hiện không.

    Dùng .first: 'div.err-msg-container, .err-msg' thường khớp 2 phần tử nên
    gọi is_visible() trực tiếp sẽ ném strict mode violation.
    """
    error_element = page.locator('div.err-msg-container, .err-msg').first
    return bool(await extract_visible_text(error_element, require_visible=True))

def is_captcha_wait_timeout_error(error):
    """Nhận diện lỗi timeout khi chờ captcha xuất hiện."""
    error_message = str(error)
    return (
        "Locator.wait_for: Timeout" in error_message
        and ("captcha-image" in error_message or 'alt="captcha"' in error_message or "captcha" in error_message)
    )

def is_serial_input_timeout_error(error):
    """Nhận diện lỗi timeout khi chờ ô nhập serial xuất hiện."""
    error_message = str(error)
    return (
        "Timeout" in error_message
        and "#serial-number-input" in error_message
        and ("Page.fill" in error_message or "locator" in error_message)
    )

async def discard_task(task):
    """Huỷ một task chạy nền và nuốt kết quả/lỗi của nó."""
    if task is None:
        return
    task.cancel()
    try:
        await task
    except BaseException:
        pass


async def clear_site_state(page):
    """Xoá cookie + localStorage để trang check quay về form nhập serial.

    checkcoverage.apple.com nhớ thiết bị vừa tra trong cookie. Một context
    dùng cho nhiều serial mà không xoá thì lần sau vào sẽ hiện thẳng kết quả
    của serial trước, không có ô nhập serial nào cả.
    """
    try:
        await page.context.clear_cookies()
    except Exception:
        pass
    try:
        await page.evaluate(
            "() => { try { localStorage.clear(); sessionStorage.clear(); } catch (e) {} }"
        )
    except Exception:
        pass


async def open_check_page(page, sn, on_page_ready=None):
    """Mở trang check và nhập serial. Ném BlockedError khi Apple trả 403/429/503.

    on_page_ready: hàm đồng bộ, gọi ngay sau khi trang tải xong và TRƯỚC khi
    điền serial. check_serial dùng nó để bắt đầu tải ảnh captcha song song với
    việc điền serial — ảnh captcha đã nằm sẵn trên trang từ lúc load rồi.
    """
    # Luôn về trạng thái sạch trước khi mở trang, nếu không sẽ dính kết quả cũ
    await clear_site_state(page)

    for attempt in range(3):
        try:
            response = await page.goto(
                BASE_URL,
                wait_until="domcontentloaded",
                timeout=PAGE_GOTO_TIMEOUT_MS,
            )
        except Exception as error:
            if is_page_closed_error(error):
                raise
            # Vao khong duoc trang = proxy nay dung khong duoc (cham, chet,
            # DNS hong...). Doi proxy khac chu dung lam sap ca run.
            raise ProxyFailure(str(error).splitlines()[0][:70]) from error

        status = getattr(response, "status", None)
        if status in BLOCKED_STATUS_CODES:
            retry_after = ""
            try:
                retry_after = (response.headers or {}).get("retry-after", "")
            except Exception:
                pass
            suffix = f" retry-after={retry_after}" if retry_after else ""
            raise BlockedError(f"HTTP {status}{suffix}")

        if on_page_ready is not None:
            try:
                on_page_ready()
            except Exception:
                pass

        try:
            if await fill_serial_number(page, sn):
                return True
            print("  ⚠️ Ô serial bị xoá sau khi nhập, tải lại trang...")
            await clear_site_state(page)
            await page.wait_for_timeout(1500)
            continue
        except Exception as error:
            if is_serial_input_timeout_error(error) and attempt < 2:
                print("  ⚠️ Không thấy ô nhập Serial (chắc đang ở trang kết quả cũ), xoá session rồi tải lại...")
                await clear_site_state(page)
                await page.wait_for_timeout(1500)
                continue
            print(f"  ⚠️ Lỗi: Không thấy ô nhập Serial. {error}")
            return False

    return False


async def fill_serial_number(page, sn, attempts=3):
    """Nhập serial và xác nhận value không bị trang Apple xoá sau hydrate."""
    for _ in range(attempts):
        await page.fill(SERIAL_INPUT_SELECTOR, sn)
        await page.wait_for_timeout(SERIAL_FILL_VERIFY_WAIT_MS)
        value = await page.input_value(SERIAL_INPUT_SELECTOR)
        if normalize_serial(value) == sn:
            return True
    return False


async def ensure_serial_filled(page, sn):
    """Serial co CON trong o khong, ngay truoc khi bam gui.

    Trang Apple hydrate xong hay xoa trang o nhap. Kiem tra mot lan luc dien la
    khong du: o co the bi xoa SAU do, va luc bam gui thi Apple bao
    "Vui long nhap so se-ri hop le." — con app thi tuong sai captcha, bao
    2captcha la giai sai va mua ma khac, trong khi ma van dung.
    """
    try:
        value = await page.input_value(SERIAL_INPUT_SELECTOR)
    except Exception:
        return True  # khong doc duoc thi cu gui, de Apple phan xu

    if normalize_serial(value) == sn:
        return True

    print("  ⚠️ Ô serial bị trang xoá mất, điền lại trước khi gửi...")
    return await fill_serial_number(page, sn)


async def submit_captcha_code(page, code):
    """Nhập captcha như người dùng thật rồi bấm nút Gửi.

    Apple có lúc hiện chữ trong ô nhưng state form chưa nhận nếu chỉ page.fill()
    rồi Enter, dẫn tới lỗi "Nhập mã trong ảnh.". Gõ qua locator tạo key/input
    events tự nhiên hơn, sau đó bắn thêm change và click nút Gửi.
    """
    captcha_input = page.locator('#captcha-input')
    if hasattr(captcha_input, "click"):
        await captcha_input.click()
    await captcha_input.clear()
    if hasattr(captcha_input, "press_sequentially"):
        await captcha_input.press_sequentially(code, delay=CAPTCHA_INPUT_DELAY_MS)
    elif hasattr(captcha_input, "fill"):
        await captcha_input.fill(code)
    else:
        await page.fill('#captcha-input', code)
    await captcha_input.evaluate(
        """el => {
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
            el.blur();
        }"""
    )

    try:
        submit_button = page.get_by_role(
            "button",
            name=re.compile(r"^(Gửi|Submit|Continue)$", re.IGNORECASE),
        ).first
        await submit_button.click(timeout=3000)
        return
    except Exception:
        pass

    try:
        await page.locator('button[type="submit"], input[type="submit"]').first.click(timeout=3000)
        return
    except Exception:
        pass

    await page.keyboard.press("Enter")


async def extract_visible_text(locator, require_visible=False):
    """Đọc text từ locator nếu phần tử tồn tại, không phụ thuộc chặt vào is_visible."""
    if await locator.count() == 0:
        return ""

    if require_visible:
        try:
            if not await locator.is_visible():
                return ""
        except Exception:
            return ""

    try:
        text = await locator.text_content()
    except Exception:
        return ""

    return text.strip() if isinstance(text, str) else ""

def format_apple_date(text):
    """Định dạng chuỗi ngày tháng từ Apple."""
    if not text or "Không thấy" in text or "Chưa active" in text:
        return text

    match = APPLE_PURCHASE_DATE_PATTERN.search(text)
    if match:
        day, month, year = match.groups()
        return f"{day.zfill(2)}/{month.zfill(2)}/{year}"

    return text

def extract_single_date_line(text):
    """Lấy ngày mua duy nhất nếu có đúng một dòng khớp định dạng ngày của Apple."""
    if not text:
        return None

    date_lines = []
    for line in text.splitlines():
        clean_line = line.strip()
        if not clean_line:
            continue
        if APPLE_PURCHASE_DATE_PATTERN.search(clean_line):
            date_lines.append(clean_line)

    if len(date_lines) == 1:
        return format_apple_date(date_lines[0])

    return None

def contains_unverified_purchase_text(*texts):
    """Kiểm tra thông báo Apple chưa xác thực ngày mua."""
    return any(text and UNVERIFIED_PURCHASE_TEXT in text for text in texts)

def contains_invalid_serial_text(*texts):
    """Kiểm tra thông báo Apple báo serial không hợp lệ."""
    return any(text and INVALID_SERIAL_TEXT in text for text in texts)

def contains_invalid_serial_input_text(*texts):
    """Kiểm tra lỗi nhập serial ngay tại form trước khi gửi captcha."""
    return any(text and INVALID_SERIAL_INPUT_TEXT in text for text in texts)


def contains_captcha_error_text(*texts):
    """Apple có đang hiện thông báo đỏ 'mã không khớp' không.

    Chỉ được gọi với text của ô báo lỗi (.err-msg), KHÔNG bao giờ với text của
    cả trang: quét cả trang thì trúng luôn nhãn của ô nhập captcha.
    """
    return any(
        text and any(marker in text for marker in CAPTCHA_MISMATCH_TEXTS)
        for text in texts
    )


def looks_like_purchase_text(text):
    """Chỉ chấp nhận text mua hàng thật từ block kết quả, không lấy text rác từ body."""
    if not text:
        return False
    return APPLE_PURCHASE_DATE_PATTERN.search(text) is not None or extract_single_date_line(text) is not None

def has_result_page_signal(device_title, header_text, purchase_text="", notification_text="", body_text=""):
    """Kiểm tra đã thực sự vào trang kết quả hay chưa."""
    combined_text = "\n".join(
        part for part in [device_title, header_text, purchase_text, notification_text, body_text] if part
    )
    if not combined_text.strip():
        return False

    if looks_like_purchase_text(purchase_text) or notification_text:
        return True

    if "Thiết bị chưa được kích hoạt" in combined_text:
        return True

    if contains_unverified_purchase_text(body_text, header_text, purchase_text, notification_text):
        return True

    return bool(
        device_title
        or "Số Sê-ri" in header_text
        or "Số sê-ri" in header_text
        or "Đã mua" in header_text
        or extract_single_date_line(header_text)
    )

def determine_purchase_date(
    device_title,
    header_text,
    purchase_text="",
    notification_text="",
    body_text="",
    heading_text="",
    error_text="",
):
    """Suy ra kết quả check từ nhiều dấu hiệu trên trang kết quả."""
    combined_text = "\n".join(
        part
        for part in [device_title, header_text, purchase_text, notification_text, body_text, heading_text, error_text]
        if part
    )

    if not combined_text.strip():
        return None

    if contains_invalid_serial_text(heading_text, error_text):
        return "serial ko hợp lệ"

    if "Thiết bị chưa được kích hoạt" in combined_text:
        return "Chưa active"

    if contains_unverified_purchase_text(purchase_text, header_text, notification_text, body_text):
        return "Chưa xác thực"

    if looks_like_purchase_text(purchase_text):
        return format_apple_date(purchase_text)

    if "Đã mua" in header_text:
        lines = [line for line in header_text.split("\n") if APPLE_PURCHASE_DATE_PATTERN.search(line)]
        if lines:
            return format_apple_date(lines[0])

    header_date = extract_single_date_line(header_text)
    if header_date:
        return header_date

    if "Đã mua" in body_text:
        lines = [line for line in body_text.split("\n") if APPLE_PURCHASE_DATE_PATTERN.search(line)]
        if lines:
            return format_apple_date(lines[0])

    body_date = extract_single_date_line(body_text)
    if body_date:
        return body_date

    if device_title or notification_text or header_text or looks_like_purchase_text(purchase_text):
        return "Không thấy ngày mua"

    return None

# Đọc cả 7 vùng text trong MỘT lượt gọi sang trình duyệt. Bản cũ dùng 7 locator,
# mỗi cái 2 lượt (count + text_content) = 14 lượt, lặp lại mỗi vòng chờ kết quả.
READ_RESULT_TEXTS_JS = """
() => {
  const vis = (el) => {
    if (!el) return false;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none') return false;
    return !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  };
  const t = (sel) => {
    const el = document.querySelector(sel);
    return el ? (el.textContent || '').trim() : '';
  };
  const tv = (sel) => {
    const el = document.querySelector(sel);
    return (el && vis(el)) ? (el.textContent || '').trim() : '';
  };
  return {
    device_title: t('#device-header-title'),
    purchase_text: t('p.device-header-purchase'),
    notification_text: t('h2.notification-heading, [data-testid="notification-heading"]'),
    header_text: t('.device-header-wrapper'),
    body_text: t('body'),
    heading_text: tv('h1'),
    error_text: tv('div.err-msg-container, .err-msg'),
  };
}
"""

RESULT_TEXT_KEYS = (
    "device_title",
    "purchase_text",
    "notification_text",
    "header_text",
    "body_text",
    "heading_text",
    "error_text",
)


async def read_result_texts(page):
    """Đọc các vùng text chính trên trang kết quả.

    Ưu tiên một lượt evaluate duy nhất. Trang nào chặn evaluate (hoặc page giả
    trong test) thì rơi về cách cũ đọc từng locator.
    """
    try:
        payload = await page.evaluate(READ_RESULT_TEXTS_JS)
    except Exception:
        payload = None

    if isinstance(payload, dict):
        return {
            key: (payload.get(key) or "").strip() if isinstance(payload.get(key), str) else ""
            for key in RESULT_TEXT_KEYS
        }

    return await read_result_texts_via_locators(page)


async def read_result_texts_via_locators(page):
    """Cách cũ: hỏi từng locator một. Giữ lại làm đường lui."""
    device_element = page.locator('#device-header-title')
    purchase_element = page.locator('p.device-header-purchase')
    notification_element = page.locator('h2.notification-heading, [data-testid="notification-heading"]')
    header_wrapper = page.locator('.device-header-wrapper')
    body_element = page.locator('body')
    heading_element = page.locator('h1').first
    error_element = page.locator('div.err-msg-container, .err-msg').first

    return {
        "device_title": await extract_visible_text(device_element),
        "purchase_text": await extract_visible_text(purchase_element),
        "notification_text": await extract_visible_text(notification_element),
        "header_text": await extract_visible_text(header_wrapper),
        "body_text": await extract_visible_text(body_element),
        "heading_text": await extract_visible_text(heading_element, require_visible=True),
        "error_text": await extract_visible_text(error_element, require_visible=True),
    }

async def has_invalid_serial_input_error(page):
    """Kiểm tra lỗi đỏ ngay tại ô nhập serial trước khi xử lý captcha."""
    error_element = page.locator('div.err-msg-container, .err-msg').first
    error_text = await extract_visible_text(error_element, require_visible=True)
    return contains_invalid_serial_input_text(error_text)

async def wait_for_result_payload(page):
    """Chờ cho đến khi trang kết quả thật sự load xong hoặc hết timeout."""
    elapsed_ms = 0
    latest_payload = None

    while elapsed_ms <= RESULT_WAIT_TIMEOUT_MS:
        payload = await read_result_texts(page)
        latest_payload = payload

        purchase_date = determine_purchase_date(**payload)
        if purchase_date not in (None, "Không thấy ngày mua"):
            payload["purchase_date"] = purchase_date
            return payload

        # Chi tin o bao loi that su cua Apple. Truoc day co ke ca body_text,
        # nen nhan cua o nhap captcha bi doc nham thanh loi sai ma.
        if contains_captcha_error_text(payload.get("error_text")):
            payload["purchase_date"] = None
            payload["captcha_error"] = True
            return payload

        if has_result_page_signal(
            payload["device_title"],
            payload["header_text"],
            payload["purchase_text"],
            payload["notification_text"],
            payload["body_text"],
        ):
            payload["purchase_date"] = purchase_date
            return payload

        await page.wait_for_timeout(RESULT_POLL_INTERVAL_MS)
        elapsed_ms += RESULT_POLL_INTERVAL_MS

    if latest_payload is None:
        latest_payload = await read_result_texts(page)
    latest_payload["purchase_date"] = determine_purchase_date(**latest_payload)
    latest_payload["captcha_error"] = contains_captcha_error_text(
        latest_payload.get("error_text")
    )
    return latest_payload

def sanitize_folder_name(folder_name):
    """Làm sạch tên folder để tránh lỗi đường dẫn."""
    cleaned_name = re.sub(r'[\\/:*?"<>|]', "_", folder_name.strip())
    return cleaned_name or None

def get_run_folder_name(run_date=None, custom_folder_name=None):
    """Lấy tên thư mục tùy chỉnh hoặc mặc định theo ngày chạy dạng ddmmyy."""
    if custom_folder_name:
        sanitized_name = sanitize_folder_name(custom_folder_name)
        if sanitized_name:
            return sanitized_name
    if run_date is None:
        run_date = date.today()
    return run_date.strftime("%d%m%y")

def should_capture_screenshot(user_input):
    """Quy đổi input y/n sang cờ chụp màn hình."""
    return user_input.strip().lower() == "y"

def format_progress(done_count, total_count):
    """Định dạng tiến độ kiểu x/tổng."""
    return f"{done_count}/{total_count}"

def prompt_run_settings(run_date=None):
    """Hỏi người dùng tên folder lưu ảnh và có chụp màn hình hay không."""
    while True:
        capture_input = input("Có chụp màn hình không? (y/n): ").strip()
        if capture_input.lower() in {"y", "n"}:
            break
        print("Vui lòng chỉ nhập y hoặc n.")

    if not should_capture_screenshot(capture_input):
        return {
            "folder_name": None,
            "capture_screenshot": False,
        }

    default_folder_name = get_run_folder_name(run_date)
    custom_folder_name = input(
        f"Tên folder lưu ảnh (Enter = {default_folder_name}): "
    ).strip()

    return {
        "folder_name": get_run_folder_name(run_date, custom_folder_name),
        "capture_screenshot": should_capture_screenshot(capture_input),
    }

def get_activation_folder_name(purchase_date):
    """Phân loại ảnh theo trạng thái active của máy."""
    return "Chưa active" if purchase_date == "Chưa active" else "Đã active"

def build_screenshot_path(serial, purchase_date, run_date=None, folder_name=None):
    """Tạo đường dẫn file ảnh chụp theo ngày chạy, trạng thái và serial."""
    safe_serial = re.sub(r'[\\/:*?"<>|]', "_", serial.strip()) or "unknown_serial"
    target_dir = SCREENSHOT_DIR / get_run_folder_name(run_date, folder_name) / get_activation_folder_name(purchase_date)
    target_dir.mkdir(parents=True, exist_ok=True)
    return target_dir / f"{safe_serial}.png"

def build_inactive_excel_path(run_date=None, folder_name=None):
    """Tạo đường dẫn file Excel tổng hợp máy chưa active."""
    target_dir = SCREENSHOT_DIR / get_run_folder_name(run_date, folder_name) / "Chưa active"
    target_dir.mkdir(parents=True, exist_ok=True)
    return target_dir / "Chưa active.xlsx"

def export_inactive_to_excel(rows, run_date=None, folder_name=None):
    """Ghi danh sách máy chưa active ra file Excel."""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Chưa active"
    sheet.append(["Serial", "Tên Máy"])

    for serial, device_name in rows:
        if device_name == "Chưa active":
            sheet.append([serial, device_name])

    excel_path = build_inactive_excel_path(run_date, folder_name)
    workbook.save(excel_path)
    workbook.close()
    return excel_path

async def save_result_screenshot(page, serial, purchase_date, folder_name=None):
    """Chụp màn hình trang kết quả sau khi check thành công."""
    screenshot_path = build_screenshot_path(serial, purchase_date, folder_name=folder_name)
    # Khung nhìn 1280x900 đã chứa trọn phần kết quả. full_page=True bắt Chrome
    # cuộn + ghép ảnh cả trang, tốn thêm khoảng nửa giây mỗi serial.
    await page.screenshot(path=str(screenshot_path), full_page=False)
    return screenshot_path

async def grab_captcha_image(page):
    """Chờ ảnh captcha hiện rồi lấy về dạng base64."""
    img_element = page.locator(CAPTCHA_SELECTOR).first
    await img_element.wait_for(state="visible", timeout=10000)
    return await get_captcha_image_base64(img_element)


async def check_serial(page, sn, capture_screenshot=True, folder_name=None):
    # Mọi dòng in ra từ đây trở đi (kể cả trong các hàm con) sẽ được gắn nhãn
    # serial này, để giao diện tách được log của từng luồng.
    CURRENT_SERIAL.set(sn)
    print(f"\n🔍 Check: {sn}")

    # Lọc serial sai định dạng trước khi tốn 1 lượt giải captcha
    if not is_valid_serial(sn):
        print("  ❌ Serial sai định dạng — bỏ qua, không tốn tiền captcha")
        return [sn, "serial ko hợp lệ"]

    state = {"prefetch": None}

    def start_captcha_prefetch():
        # Ảnh captcha có sẵn trên trang ngay lúc load, không cần đợi điền
        # serial xong mới đi lấy. Chạy song song với lúc đang gõ serial.
        if state["prefetch"] is None:
            state["prefetch"] = asyncio.ensure_future(grab_captcha_image(page))

    try:
        return await _check_serial_inner(
            page, sn, capture_screenshot, folder_name, state, start_captcha_prefetch
        )
    finally:
        # Thoát kiểu gì cũng không để lại task captcha lơ lửng
        await discard_task(state["prefetch"])


async def _check_serial_inner(
    page, sn, capture_screenshot, folder_name, state, start_captcha_prefetch
):
    if not await open_check_page(page, sn, on_page_ready=start_captcha_prefetch):
        return [sn, "Lỗi load trang"]

    last_failure = "Check tay"
    manual_asks = 0
    local_ocr_tries = 0

    for i in range(MAX_AUTO_RETRIES):
        try:
            # Lấy ảnh Captcha — lần đầu thường đã tải xong từ lúc điền serial
            if state["prefetch"] is not None:
                pending, state["prefetch"] = state["prefetch"], None
                image_base64 = await pending
            else:
                image_base64 = await grab_captcha_image(page)

            solution = None
            solution_from_local = False

            # OCR trượt mấy lần rồi thì nhờ người nhìn cho nhanh — nhưng chỉ
            # hỏi MANUAL_CAPTCHA_MAX_ASKS lần, sau đó để AI tự chạy nốt.
            if (
                MANUAL_CAPTCHA_HANDLER
                and i >= MANUAL_CAPTCHA_AFTER
                and manual_asks < MANUAL_CAPTCHA_MAX_ASKS
            ):
                manual_asks += 1
                typed = await MANUAL_CAPTCHA_HANDLER(image_base64, sn)
                if typed:
                    solution = CaptchaSolution(normalize_captcha_code(typed), None)
                    print(f"  [Lần {i+1}] Người nhập: {solution.code}")

            # Thu OCR tren may truoc: ~0.2s va mien phi. Truot may lan thi thoi.
            if solution is None and local_ocr_tries < LOCAL_OCR_MAX_ATTEMPTS:
                local_code = await solve_captcha_locally(image_base64)
                if local_code:
                    local_ocr_tries += 1
                    solution_from_local = True
                    # task_id = None: khong phai ma mua nen khong co gi de bao sai
                    solution = CaptchaSolution(local_code, None)
                    print(f"  [Lần {i+1}] OCR máy đoán: {solution.code}")

            if solution is None:
                try:
                    solution = await solve_captcha_task(image_base64)
                except CaptchaBusyError as error:
                    print(f"  ⏳ 2captcha quá tải ({error}) — chờ {CAPTCHA_BUSY_WAIT_SECONDS}s rồi thử lại")
                    await asyncio.sleep(CAPTCHA_BUSY_WAIT_SECONDS)
                    continue
                print(f"  [Lần {i+1}] AI đoán: {solution.code}")

            code = solution.code

            # Chi bo nhung ma ro rang la rac. Con lai cu gui, Apple se noi dung/sai —
            # re hon nhieu so voi mua lai mot ma khac.
            if not (CAPTCHA_MIN_LENGTH <= len(code) <= CAPTCHA_MAX_LENGTH):
                print(f"  ⚠️ OCR trả {len(code)} ký tự, bỏ mã này...")
                await report_bad_captcha(solution.task_id)
                await reload_captcha(page)
                continue

            await ensure_serial_filled(page, sn)
            await submit_captcha_code(page, code)
            result_payload = await wait_for_result_payload(page)

            if result_payload.get("captcha_error"):
                print("  ❌ Sai mã, đổi captcha mới...")
                await report_bad_captcha(solution.task_id)
                await reload_captcha(page)
                continue

            purchase_date = result_payload["purchase_date"]

            if purchase_date is not None:

                should_save_screenshot = capture_screenshot and purchase_date != "Chưa active"

                if should_save_screenshot:
                    try:
                        screenshot_path = await save_result_screenshot(page, sn, purchase_date, folder_name)
                        print(f"  📸 Đã chụp màn hình: {screenshot_path}")
                    except Exception as screenshot_error:
                        print(f"  ⚠️ Không chụp được màn hình: {screenshot_error}")

                if solution_from_local:
                    # Apple chap nhan ma cua OCR may: ghi nhan de biet ti le trung
                    ocr = get_local_ocr()
                    if ocr is not None:
                        ocr.record_accepted()

                print(f"  ✅ THÀNH CÔNG: {purchase_date}")
                return [sn, purchase_date]

            # Den day la khong ra ket qua nao ca. Truoc khi do cho captcha,
            # xet xem co phai o serial bi trang xoa mat khong — Apple bao
            # "Vui long nhap so se-ri hop le." chu khong phai sai ma. Do oan
            # cho 2captcha o day vua mat tien vua khong sua duoc goc van de.
            if await has_invalid_serial_input_error(page):
                print("  ⚠️ Apple báo thiếu số sê-ri (không phải sai captcha) — điền lại...")
                await fill_serial_number(page, sn)
                await reload_captcha(page)
                last_failure = "Không đọc được kết quả"
                continue

            # Sai mã captcha -> báo 2captcha để được hoàn tiền rồi đổi mã khác
            if await has_error_message(page):
                print("  ❌ Sai mã, đang thử mã khác...")
                await report_bad_captcha(solution.task_id)
                await reload_captcha(page)
                continue

            last_failure = "Không đọc được kết quả"

        except (BlockedError, CaptchaServiceError):
            # Hai lỗi này phải để runner xử lý (đổi IP / dừng run), không nuốt ở đây
            raise
        except Exception as e:
            if is_page_closed_error(e):
                # Dang tat chuong trinh — retry 5 lan chi to ra rac log
                raise
            if is_blocked_error(e):
                raise BlockedError(str(e).splitlines()[0]) from e
            if is_captcha_wait_timeout_error(e):
                print("  ⚠️ Không thấy captcha, đang tải lại trang...")
                if not await open_check_page(page, sn):
                    return [sn, "Lỗi load trang"]
                await page.wait_for_timeout(1500)
                continue
            print(f"  ⚠️ Lỗi trong khi nhập: {e}")
            last_failure = "Lỗi không xác định"

    return [sn, last_failure]

async def preflight_2captcha():
    """Kiểm tra key + số dư 2captcha trước khi chạy. True = chạy tiếp."""
    try:
        balance = await check_2captcha_balance()
    except CaptchaServiceError as error:
        print(f"❌ 2captcha không dùng được: {error}")
        return False
    except Exception as error:
        print(f"⚠️ Không kiểm tra được số dư 2captcha ({error}) — vẫn chạy tiếp.")
        return True

    if balance <= 0:
        print("❌ Hết tiền 2captcha — nạp thêm rồi chạy lại.")
        return False
    return True


def parse_args(argv=None):
    """Doc tham so dong lenh."""
    parser = argparse.ArgumentParser(description="Check active serial Apple (tuan tu)")
    parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Bo qua ket qua cu trong CSV, check lai toan bo serial",
    )
    return parser.parse_args(argv)


async def main(force=False):
    if not INPUT_FILE.exists():
        print(f"❌ Không tìm thấy file: {INPUT_FILE}")
        return

    serials = load_serials()
    done_results = {} if force else load_done_serials()
    pending = [sn for sn in serials if needs_check(sn, done_results)]
    results = [[sn, done_results[sn]] for sn in serials if not needs_check(sn, done_results)]

    total_count = len(serials)
    done_count = len(results)

    print(f"📦 Tổng serial: {total_count} | Đã có: {done_count} | Cần chạy: {len(pending)}")

    # Khong con gi de chay thi thoat luon, dung hoi han gi them
    if not pending:
        print(f"✨ Cả {total_count} serial đều đã có kết quả trong {OUTPUT_FILE.name}.")
        print(f"   Muốn check lại tất cả: python3 {Path(__file__).name} --force")
        return

    if not await preflight_2captcha():
        return

    run_settings = prompt_run_settings()

    proxies = load_proxies()
    throttle = BlockThrottle()
    block_attempts = {}
    proxy_failures = {}
    dead_proxies = set()
    current_proxy = None

    if proxies:
        print(f"🌐 Đang dùng {len(proxies)} proxy từ {PROXY_FILE.name}")
    else:
        print(f"⚠️ Không có {PROXY_FILE.name} — chạy bằng IP thật, quét nhiều sẽ ăn 403.")

    async with create_stealth_playwright_context() as p:
        browser = await launch_browser(p, headless=run_settings.get("headless", False))
        try:
            with open(OUTPUT_FILE, 'w', newline='', encoding='utf-8-sig') as f:
                writer = csv.writer(f)
                writer.writerow(['Serial', 'Ngày mua / Trạng thái'])
                for row in results:
                    writer.writerow(row)
                f.flush()

                context = None
                session_used = 0
                session_index = 0
                stop_run = False

                while pending and not stop_run:
                    sn = pending.pop(0)

                    # Dùng lại 1 session cho nhiều serial. Mở context mới tinh cho
                    # từng serial trên cùng 1 IP = 500 khách lần đầu từ 1 địa chỉ,
                    # đó mới là mẫu dễ bị chặn nhất.
                    if context is None or session_used >= SERIALS_PER_SESSION:
                        if context is not None:
                            await context.close()
                        proxy = pick_proxy(proxies, session_index, dead_proxies)
                        if proxies and proxy is None:
                            print("❌ Proxy chết hết rồi — dừng để khỏi quét bằng IP thật.")
                            break
                        context = await create_browser_context(browser, proxy)
                        current_proxy = proxy
                        session_index += 1
                        session_used = 0
                        print(f"🌐 Session #{session_index} qua {describe_proxy(proxy)}")

                    res = None
                    page = None
                    try:
                        page = await context.new_page()
                        res = await check_serial(
                            page,
                            sn,
                            capture_screenshot=run_settings["capture_screenshot"],
                            folder_name=run_settings["folder_name"],
                        )
                        throttle.on_success()
                        session_used += 1
                    except ProxyFailure as error:
                        # Proxy hong chu chua chac bi Apple chan -> doi proxy, khong nghi lau
                        block_attempts[sn] = block_attempts.get(sn, 0) + 1
                        if block_attempts[sn] <= MAX_BLOCK_RETRIES:
                            pending.insert(0, sn)
                        else:
                            res = [sn, "Proxy hỏng"]
                        if current_proxy:
                            server = current_proxy["server"]
                            proxy_failures[server] = proxy_failures.get(server, 0) + 1
                            if proxy_failures[server] >= MAX_PROXY_FAILURES:
                                dead_proxies.add(server)
                                print(f"💀 Bỏ hẳn proxy {server}")
                        try:
                            await context.close()
                        except Exception:
                            pass
                        context = None
                        print(f"🔁 Đổi proxy ({error})")
                        await asyncio.sleep(PROXY_FAILURE_DELAY_SECONDS)
                    except BlockedError as error:
                        block_attempts[sn] = block_attempts.get(sn, 0) + 1
                        if block_attempts[sn] <= MAX_BLOCK_RETRIES:
                            pending.insert(0, sn)  # chạy lại serial này với IP mới
                        else:
                            res = [sn, "Bị chặn IP"]
                        try:
                            await context.close()
                        except Exception:
                            pass
                        context = None
                        await throttle.on_block(str(error))
                    except CaptchaServiceError as error:
                        print(f"❌ 2captcha lỗi nghiêm trọng ({error}) — dừng run để khỏi mất tiền.")
                        print("   Kết quả đã chạy vẫn được giữ, lần sau chạy lại sẽ tiếp tục.")
                        stop_run = True
                    except Exception as error:
                        if is_page_closed_error(error):
                            stop_run = True
                        else:
                            print(f"⚠️ Lỗi lạ ở {sn}: {str(error).splitlines()[0][:80]}")
                            res = [sn, "Lỗi không xác định"]
                    finally:
                        try:
                            await page.close()
                        except Exception:
                            pass

                    if res is None:
                        continue

                    results.append(res)
                    writer.writerow(res)
                    f.flush()
                    done_count += 1
                    print(f"  💾 Đã lưu kết quả: {res[0]} -> {res[1]} ({format_progress(done_count, total_count)})")
                    await polite_delay()

                if context is not None:
                    await context.close()
        finally:
            await browser.close()

    try:
        inactive_excel_path = export_inactive_to_excel(
            results,
            folder_name=run_settings["folder_name"],
        )
        print(f"📄 Đã tạo file Excel máy chưa active: {inactive_excel_path}")
    except Exception as error:
        print(f"⚠️ Không xuất được Excel: {error}")

    print(f"\n✨ Xong! Đã lưu kết quả vào: {OUTPUT_FILE}")

if __name__ == "__main__":
    asyncio.run(main(force=parse_args().force))
