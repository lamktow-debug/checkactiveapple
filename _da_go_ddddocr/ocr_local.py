"""Gọi OCR chạy trên máy (ddddocr) qua một tiến trình con.

Nguyên tắc xuyên suốt: OCR máy là thứ CÓ THÌ TỐT. Thiếu venv-ocr, worker chết,
model hỏng, trả về rác — tất cả đều chỉ dẫn tới None, và bên gọi lặng lẽ quay về
2captcha. Không bao giờ được làm sập cả mẻ chạy vì OCR.

Không import Qt, không import Playwright: chạy và test được ở mọi nơi.
"""

import base64
import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
OCR_VENV_PYTHON = BASE_DIR / "venv-ocr" / "bin" / "python"
OCR_WORKER = BASE_DIR / "ocr_worker.py"

# Nap model 76MB mat vai giay, chi lam mot lan luc khoi dong
STARTUP_TIMEOUT_SECONDS = float(os.getenv("OCR_STARTUP_TIMEOUT", "60"))
# Giai mot anh chi mat ~0.1-0.3s. Qua 10s la co gi do sai, bo qua cho nhanh.
SOLVE_TIMEOUT_SECONDS = float(os.getenv("OCR_SOLVE_TIMEOUT", "10"))

CODE_PATTERN = re.compile(r"[^A-Za-z0-9]")

# Captcha Apple luon 4 ky tu. Model hay nuot mot ky tu khi net dinh nhau, tra
# ve 3 ky tu — ma 3 ky tu thi CHAC CHAN sai. Gui di chi ton them mot vong tai
# lai trang va mot request vao Apple, nen tu choi thang tu day.
EXPECTED_CODE_LENGTH = int(os.getenv("OCR_CODE_LENGTH", "4"))

# --- Chan lang phi ---
# Do that 26/08: 18 serial mat 10 phut voi OCR may, 6 phut voi 2captcha.
# Nguyen nhan: moi lan TRUOT phai quet het cac bien the roi VAN phai mua ma
# 2captcha. Trung thi tiet kiem ~7.5s, truot thi lang phi ~11s. Nen OCR may
# phai trung tren ~60% moi hoa von — duoi nguong do la cang dung cang cham.
#
# Nen: moi lan giai co han gio cung, va co mot cong tu dong tat OCR may khi do
# duoc ti le trung qua thap.
SOLVE_BUDGET_SECONDS = float(os.getenv("OCR_BUDGET", "3"))
WARMUP_TRIES = int(os.getenv("OCR_WARMUP", "6"))
MIN_HIT_RATE = float(os.getenv("OCR_MIN_HIT_RATE", "0.55"))


def normalize_code(code):
    """Giữ đúng chữ và số, viết hoa — cùng quy tắc với mã của 2captcha."""
    return CODE_PATTERN.sub("", code or "").upper()


class LocalOcrUnavailable(Exception):
    """Máy chưa cài được OCR — bên gọi cứ dùng 2captcha."""


class LocalOcr:
    """Giữ một tiến trình worker sống lâu, gửi ảnh vào và nhận mã ra.

    Không tự khởi động lúc tạo: chỉ mở tiến trình ở lần giải đầu tiên, để app
    mở lên vẫn nhanh và máy chưa cài OCR cũng không tốn gì.
    """

    def __init__(self, python_path=None, worker_path=None):
        self.python_path = Path(python_path or OCR_VENV_PYTHON)
        self.worker_path = Path(worker_path or OCR_WORKER)
        self._process = None
        self._lock = threading.Lock()
        self._broken_reason = None
        # Thong ke de biet OCR may co that su dang hoc viec khong
        self.attempts = 0
        self.accepted = 0
        # Bao nhieu lan model doc ra so ky tu khong dung — de biet co nen giu
        # OCR may khong, hay no chi toan doan hut
        self.wrong_length = 0
        self.last_candidates = []
        # tries = MOI lan goi OCR may (ke ca doc hut), dung de tinh ti le trung
        self.tries = 0
        self.gave_up_reason = None

    # ---------- trạng thái ----------

    @property
    def installed(self):
        return self.python_path.exists() and self.worker_path.exists()

    @property
    def broken_reason(self):
        return self._broken_reason

    def accuracy(self):
        """Tỉ lệ trúng trên MỌI lần gọi OCR máy. None khi chưa có dữ liệu.

        Mẫu số là mọi lần gọi, không chỉ những lần đọc đủ ký tự — vì lần đọc
        hụt cũng tốn đúng ngần ấy thời gian.
        """
        if self.tries <= 0:
            return None
        return self.accepted / self.tries

    def should_give_up(self):
        """Đã đủ dữ liệu để kết luận OCR máy đang làm chậm đi chưa."""
        if self.tries < WARMUP_TRIES:
            return None
        ti_le = self.accuracy()
        if ti_le is not None and ti_le < MIN_HIT_RATE:
            return (f"OCR máy chỉ trúng {self.accepted}/{self.tries} "
                    f"({ti_le*100:.0f}%), dưới mức hoà vốn — tắt trong mẻ này, "
                    f"dùng thẳng 2captcha cho nhanh")
        return None

    def record_accepted(self):
        self.accepted += 1

    # ---------- vòng đời tiến trình ----------

    def _spawn(self):
        if not self.installed:
            raise LocalOcrUnavailable(
                "chua co venv-ocr — bam dup CAI_DAT_OCR.command de cai")
        try:
            process = subprocess.Popen(
                [str(self.python_path), str(self.worker_path)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
        except OSError as error:
            raise LocalOcrUnavailable(f"khong chay duoc worker: {error}") from error

        first = self._read_line(process, STARTUP_TIMEOUT_SECONDS)
        if first is None:
            self._kill(process)
            raise LocalOcrUnavailable("worker khong tra loi luc khoi dong")

        try:
            payload = json.loads(first)
        except ValueError:
            self._kill(process)
            raise LocalOcrUnavailable(f"worker tra ve rac: {first[:80]}")

        if payload.get("error"):
            self._kill(process)
            raise LocalOcrUnavailable(payload["error"])
        if not payload.get("ready"):
            self._kill(process)
            raise LocalOcrUnavailable("worker khong bao san sang")

        return process

    @staticmethod
    def _read_line(process, timeout):
        """Đọc một dòng, có hạn giờ. Quá giờ trả None."""
        ket_qua = {}

        def doc():
            try:
                ket_qua["line"] = process.stdout.readline()
            except Exception:
                ket_qua["line"] = ""

        luong = threading.Thread(target=doc, daemon=True)
        luong.start()
        luong.join(timeout)
        if luong.is_alive():
            return None
        line = (ket_qua.get("line") or "").strip()
        return line or None

    @staticmethod
    def _kill(process):
        """Giết worker rồi mới đóng ống — đóng trước thì luồng đọc bị treo."""
        try:
            process.kill()
            process.wait(timeout=5)
        except Exception:
            pass
        for luong in (process.stdin, process.stdout):
            try:
                if luong is not None:
                    luong.close()
            except Exception:
                pass

    def close(self):
        with self._lock:
            if self._process is not None:
                self._kill(self._process)
                self._process = None

    # ---------- giải captcha ----------

    def solve(self, image_base64, expected_length=EXPECTED_CODE_LENGTH):
        """Trả mã đúng độ dài, hoặc None để bên gọi quay về 2captcha.

        expected_length=0 nghĩa là chấp nhận mọi độ dài.
        """
        if self._broken_reason or self.gave_up_reason:
            return None

        # Do duoc ti le trung qua thap thi thoi, dung mat thoi gian nua
        ly_do = self.should_give_up()
        if ly_do:
            self.gave_up_reason = ly_do
            print(f"  ⏭️  {ly_do}")
            return None

        self.tries += 1

        with self._lock:
            try:
                if self._process is None or self._process.poll() is not None:
                    if self._process is not None:
                        # Worker cu da chet: don ong dan cua no truoc khi mo cai moi
                        self._kill(self._process)
                        self._process = None
                    self._process = self._spawn()
            except LocalOcrUnavailable as error:
                self._broken_reason = str(error)
                return None
            except Exception as error:
                self._broken_reason = f"loi la: {error}"
                return None

            process = self._process
            payload = json.dumps({
                "image": strip_data_uri(image_base64),
                "length": expected_length,
                "budget": SOLVE_BUDGET_SECONDS,
            })
            try:
                process.stdin.write(payload + "\n")
                process.stdin.flush()
            except Exception:
                self._kill(process)
                self._process = None
                return None

            line = self._read_line(process, SOLVE_TIMEOUT_SECONDS)

        if line is None:
            # Treo giua chung: giet di, lan sau tu mo lai
            self.close()
            return None

        try:
            answer = json.loads(line)
        except ValueError:
            return None

        if answer.get("error"):
            return None

        self.last_candidates = answer.get("candidates") or []
        code = normalize_code(answer.get("code"))
        if not code:
            return None

        if expected_length and len(code) != expected_length:
            # Doc hut ky tu. Gui di la chac chan sai, thoi de 2captcha lo.
            self.wrong_length += 1
            return None

        self.attempts += 1
        return code


def strip_data_uri(text):
    """Bỏ tiền tố data:image/...;base64, nếu có."""
    text = text or ""
    if "base64," in text:
        return text.split("base64,", 1)[1]
    return text


def summarize(ocr):
    """Câu tổng kết về OCR máy để in cuối mẻ chạy. None khi không có gì để nói."""
    if ocr is None:
        return None
    tries = getattr(ocr, "tries", 0)
    if tries <= 0:
        return None

    ti_le = ocr.accuracy() * 100
    cau = (f"🧠 OCR máy: {ocr.accepted}/{tries} lần trúng ({ti_le:.0f}%) "
           f"— tiết kiệm {ocr.accepted} lượt 2captcha")

    hut = getattr(ocr, "wrong_length", 0)
    if hut:
        cau += f"; {hut} lần đọc thiếu ký tự"

    bo_cuoc = getattr(ocr, "gave_up_reason", None)
    if bo_cuoc:
        cau += "\n   ⏭️  Đã tự tắt giữa chừng vì tỉ lệ trúng quá thấp"
    elif ti_le < MIN_HIT_RATE * 100:
        cau += "\n   ⚠️  Dưới mức hoà vốn — nên tắt OCR máy trong Cài đặt"
    return cau


def self_test(python_path=None, worker_path=None):
    """Kiểm tra nhanh xem OCR máy có chạy được không. Trả (ok, lời nhắn)."""
    ocr = LocalOcr(python_path, worker_path)
    if not ocr.installed:
        return False, f"Chua co {ocr.python_path} hoac {ocr.worker_path}"
    try:
        process = ocr._spawn()
    except LocalOcrUnavailable as error:
        return False, str(error)
    except Exception as error:
        return False, f"loi la: {error}"

    try:
        process.stdin.write("PING\n")
        process.stdin.flush()
        line = ocr._read_line(process, SOLVE_TIMEOUT_SECONDS)
    finally:
        ocr._kill(process)

    if not line:
        return False, "worker khong tra loi PING"
    try:
        if json.loads(line).get("code") == "PONG":
            return True, "OCR may san sang"
    except ValueError:
        pass
    return False, f"worker tra loi la: {line[:80]}"


if __name__ == "__main__":
    ok, message = self_test()
    print(("✅ " if ok else "❌ ") + message)
    sys.exit(0 if ok else 1)
