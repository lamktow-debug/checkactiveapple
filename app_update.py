"""Kiểm tra bản mới trên GitHub Releases.

Repo để private, nên mọi lời gọi đều phải kèm token. Token là loại
fine-grained, chỉ cần quyền đọc Contents của đúng repo này — không cần gì hơn.

Không import Qt, không import Playwright: chạy và test được ở mọi nơi.
"""

import json
import re
from pathlib import Path
from typing import NamedTuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

GITHUB_API = "https://api.github.com"
TIMEOUT_SECONDS = 12
USER_AGENT = "CheckActive-Updater"
ASSET_SUFFIX = ".dmg"
DOWNLOAD_CHUNK = 256 * 1024

REPO_PATTERN = re.compile(r"^([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)$")
VERSION_PATTERN = re.compile(r"^v?(\d+(?:\.\d+)*)(?:[-+](.+))?$")


class UpdateError(Exception):
    """Lỗi có câu chữ đọc được, để giao diện hiện thẳng cho người dùng."""


class Release(NamedTuple):
    version: str
    tag: str
    title: str
    notes: str
    page_url: str
    asset_name: str
    asset_url: str
    asset_size: int

    @property
    def has_asset(self):
        return bool(self.asset_url)


# --------------------------------------------------------------------- repo

def normalize_repo(text):
    """Nhận 'owner/repo' hoặc link GitHub đầy đủ, trả về 'owner/repo'."""
    text = (text or "").strip()
    if not text:
        raise UpdateError("Chưa điền repo GitHub trong Cài đặt.")

    text = re.sub(r"^(https?://)?(www\.)?github\.com/", "", text)
    text = re.sub(r"(\.git)?/?$", "", text)

    match = REPO_PATTERN.match(text)
    if not match:
        raise UpdateError(f"Repo không đúng dạng 'owner/repo': {text}")
    return f"{match.group(1)}/{match.group(2)}"


# ------------------------------------------------------------------ version

def parse_version(text):
    """'v1.2.0' -> ((1,2,0), 1, ''). Bản thử nghiệm xếp dưới bản chính thức."""
    match = VERSION_PATTERN.match((text or "").strip())
    if not match:
        raise UpdateError(f"Không đọc được số phiên bản: {text!r}")
    numbers = tuple(int(part) for part in match.group(1).split("."))
    numbers = numbers + (0,) * (3 - len(numbers)) if len(numbers) < 3 else numbers
    prerelease = match.group(2) or ""
    return numbers, (0 if prerelease else 1), prerelease


def is_newer(candidate, current):
    """candidate có mới hơn current không. Bằng nhau thì không tính là mới."""
    try:
        return parse_version(candidate) > parse_version(current)
    except UpdateError:
        return False


# --------------------------------------------------------------------- HTTP

def _request(url, token, accept):
    headers = {"Accept": accept, "User-Agent": USER_AGENT,
               "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return Request(url, headers=headers)


def _explain(error, repo):
    """Biến mã lỗi HTTP thành câu người đọc hiểu và biết phải làm gì."""
    if error.code == 401:
        return UpdateError("Token GitHub sai hoặc đã hết hạn — tạo token mới trong Cài đặt.")
    if error.code == 404:
        return UpdateError(
            f"Không thấy repo {repo}, hoặc token không có quyền đọc nó. "
            "Repo private thì token phải được cấp quyền cho đúng repo này."
        )
    if error.code == 403:
        return UpdateError("GitHub tạm chặn (gọi quá nhiều hoặc token thiếu quyền). Thử lại sau ít phút.")
    return UpdateError(f"GitHub trả lỗi HTTP {error.code}.")


def fetch_latest_release(repo, token, opener=urlopen):
    """Lấy release mới nhất. GitHub tự bỏ qua bản nháp và bản thử nghiệm."""
    repo = normalize_repo(repo)
    url = f"{GITHUB_API}/repos/{repo}/releases/latest"
    request = _request(url, token, "application/vnd.github+json")

    try:
        with opener(request, timeout=TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        raise _explain(error, repo) from error
    except (URLError, TimeoutError, OSError) as error:
        raise UpdateError(f"Không nối được GitHub: {error}") from error
    except (ValueError, UnicodeDecodeError) as error:
        raise UpdateError(f"GitHub trả về dữ liệu lạ: {error}") from error

    if not isinstance(payload, dict) or not payload.get("tag_name"):
        raise UpdateError(f"Repo {repo} chưa có release nào.")
    return payload


def pick_asset(assets, suffix=ASSET_SUFFIX):
    """Chọn file cài đặt trong release. Repo private thì PHẢI dùng 'url'.

    'browser_download_url' chỉ chạy khi đã đăng nhập bằng cookie trình duyệt;
    gọi bằng token thì phải dùng địa chỉ api kèm Accept: application/octet-stream.
    """
    for asset in assets or []:
        name = (asset or {}).get("name") or ""
        if name.lower().endswith(suffix) and asset.get("url"):
            return asset
    return None


def release_from_payload(payload):
    asset = pick_asset(payload.get("assets")) or {}
    tag = payload["tag_name"]
    return Release(
        version=tag.lstrip("vV"),
        tag=tag,
        title=payload.get("name") or tag,
        notes=(payload.get("body") or "").strip(),
        page_url=payload.get("html_url") or "",
        asset_name=asset.get("name") or "",
        asset_url=asset.get("url") or "",
        asset_size=int(asset.get("size") or 0),
    )


def check_for_update(repo, token, current_version, opener=urlopen):
    """Trả về Release nếu có bản mới hơn, None nếu đang là bản mới nhất."""
    release = release_from_payload(fetch_latest_release(repo, token, opener))
    return release if is_newer(release.version, current_version) else None


def download_release(release, token, target_dir=None, opener=urlopen, on_progress=None):
    """Tải file cài về máy. Trả về đường dẫn file đã lưu."""
    if not release.has_asset:
        raise UpdateError(
            f"Bản {release.version} chưa đính kèm file {ASSET_SUFFIX} nào — "
            "mở trang release để xem."
        )

    target_dir = Path(target_dir or (Path.home() / "Downloads"))
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / release.asset_name

    request = _request(release.asset_url, token, "application/octet-stream")
    partial = target.with_suffix(target.suffix + ".part")

    try:
        with opener(request, timeout=TIMEOUT_SECONDS) as response:
            total = release.asset_size or int(response.headers.get("Content-Length") or 0)
            done = 0
            with open(partial, "wb") as out:
                while True:
                    chunk = response.read(DOWNLOAD_CHUNK)
                    if not chunk:
                        break
                    out.write(chunk)
                    done += len(chunk)
                    if on_progress:
                        on_progress(done, total)
    except HTTPError as error:
        partial.unlink(missing_ok=True)
        raise _explain(error, release.asset_name) from error
    except (URLError, TimeoutError, OSError) as error:
        partial.unlink(missing_ok=True)
        raise UpdateError(f"Tải không xong: {error}") from error

    partial.replace(target)
    return target


def format_size(num_bytes):
    """1234567 -> '1,2 MB'."""
    if not num_bytes:
        return ""
    for unit, step in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if num_bytes >= step:
            return f"{num_bytes / step:.1f}".replace(".", ",") + f" {unit}"
    return f"{num_bytes} B"
