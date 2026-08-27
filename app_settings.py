"""Lưu cấu hình cho app giao diện.

Quan trọng: khi double-click mở app từ Finder, macOS KHÔNG nạp ~/.zshrc, nên
biến môi trường TWOCAPTCHA_API_KEY sẽ không có. Vì vậy key phải được lưu ở
đây và nạp lại lúc chạy.
"""

import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
SETTINGS_FILE = BASE_DIR / "app_settings.json"

DEFAULT_SETTINGS = {
    "twocaptcha_key": "",
    "concurrency": 1,          # chac an: 1 serial mot luc
    "headless": False,
    "block_assets": True,
    "capture_screenshot": False,
    "serials_per_session": 15,
    "serial_timeout": 120,     # qua 2 phut thi bo qua serial do
    # Mac dinh TAT. Bat len thi moi serial kho se dung yen cho nguoi go tay;
    # chay mot me lon roi di lam viec khac la moi serial kho mat tron mot phut.
    "manual_captcha": False,
    # Turbo mode: delay ngan hon, nhanh hon dang ke nhung de bi chan IP hon.
    "turbo_mode": False,
    # Tu kiem tra ban moi tren GitHub Releases. Repo private nen phai co token
    # (fine-grained PAT, chi can quyen doc Contents cua dung repo nay).
    "github_repo": "",
    "github_token": "",
    "check_updates": True,
}


def load_settings(settings_file=SETTINGS_FILE):
    """Đọc cấu hình, thiếu khoá nào thì lấy mặc định."""
    settings = dict(DEFAULT_SETTINGS)
    path = Path(settings_file)
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                stored = json.load(f)
            if isinstance(stored, dict):
                settings.update({k: v for k, v in stored.items() if k in DEFAULT_SETTINGS})
        except (json.JSONDecodeError, OSError):
            pass
    return settings


def save_settings(settings, settings_file=SETTINGS_FILE):
    """Ghi cấu hình, chỉ giữ những khoá mình biết."""
    clean = {k: v for k, v in settings.items() if k in DEFAULT_SETTINGS}
    with open(settings_file, "w", encoding="utf-8") as f:
        json.dump(clean, f, indent=2, ensure_ascii=False)
    return clean


def apply_settings(settings):
    """Đẩy cấu hình vào module scraper.

    Các module đọc biến môi trường lúc import, nên phải gán thẳng vào hằng số
    của module thì đổi cấu hình trong app mới có tác dụng ngay.
    """
    import check_active_v2 as scraper

    scraper.CAPTCHA_2CAPTCHA_API_KEY = (settings.get("twocaptcha_key") or "").strip()
    scraper.BLOCK_ASSETS = bool(settings.get("block_assets"))
    scraper.SERIALS_PER_SESSION = int(settings.get("serials_per_session") or 15)
    scraper.SERIAL_TIMEOUT_SECONDS = int(settings.get("serial_timeout") or 120)
    scraper.TURBO_MODE = bool(settings.get("turbo_mode"))

    # check_active_parallel dung "from check_active_v2 import SERIALS_PER_SESSION"
    # nen no giu ban sao rieng — phai gan lai ca ben do.
    try:
        import check_active_parallel as runner

        runner.SERIALS_PER_SESSION = scraper.SERIALS_PER_SESSION
    except Exception:
        pass

    return scraper
