"""Test cho phần kiểm tra bản mới — không gọi mạng thật."""

import json
import unittest
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError, URLError

from app_update import (
    ASSET_SUFFIX,
    Release,
    UpdateError,
    check_for_update,
    download_release,
    fetch_latest_release,
    format_size,
    is_newer,
    normalize_repo,
    parse_version,
    pick_asset,
    release_from_payload,
)


def fake_release_payload(tag="v1.2.0", with_asset=True, name="CheckActive-1.2.0.dmg"):
    payload = {
        "tag_name": tag,
        "name": f"Check Active {tag}",
        "body": "  Nhanh hơn, log tách theo serial.  ",
        "html_url": f"https://github.com/oneway/check-active/releases/tag/{tag}",
        "assets": [],
    }
    if with_asset:
        payload["assets"] = [
            {"name": "ghi-chu.txt", "size": 12,
             "url": "https://api.github.com/repos/oneway/check-active/releases/assets/1"},
            {"name": name, "size": 2_411_724,
             "browser_download_url": "https://github.com/oneway/check-active/releases/download/x/y.dmg",
             "url": "https://api.github.com/repos/oneway/check-active/releases/assets/2"},
        ]
    return payload


class _Response:
    def __init__(self, body, headers=None):
        self._buffer = BytesIO(body)
        self.headers = headers or {}

    def read(self, size=-1):
        return self._buffer.read(size) if size and size > 0 else self._buffer.read()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def opener_returning(payload, capture=None):
    def opener(request, timeout=None):
        if capture is not None:
            capture.append(request)
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return _Response(body)
    return opener


def opener_raising(error):
    def opener(request, timeout=None):
        raise error
    return opener


class RepoNameTests(unittest.TestCase):
    def test_plain_form_passes_through(self):
        self.assertEqual(normalize_repo("oneway/check-active"), "oneway/check-active")

    def test_full_url_is_accepted(self):
        for text in (
            "https://github.com/oneway/check-active",
            "https://github.com/oneway/check-active.git",
            "github.com/oneway/check-active/",
            "  https://www.github.com/oneway/check-active  ",
        ):
            self.assertEqual(normalize_repo(text), "oneway/check-active", text)

    def test_empty_and_broken_are_rejected_with_advice(self):
        with self.assertRaises(UpdateError) as caught:
            normalize_repo("")
        self.assertIn("Cài đặt", str(caught.exception))

        with self.assertRaises(UpdateError):
            normalize_repo("chi-mot-tu")


class VersionTests(unittest.TestCase):
    def test_v_prefix_is_optional(self):
        self.assertEqual(parse_version("v1.2.0"), parse_version("1.2.0"))

    def test_short_versions_are_padded(self):
        self.assertEqual(parse_version("1.2")[0], (1, 2, 0))

    def test_ordering(self):
        self.assertTrue(is_newer("1.2.0", "1.1.9"))
        self.assertTrue(is_newer("2.0.0", "1.99.99"))
        self.assertTrue(is_newer("1.1.10", "1.1.9"), "10 phải lớn hơn 9, không so chuỗi")
        self.assertFalse(is_newer("1.1.0", "1.1.0"), "bằng nhau không phải bản mới")
        self.assertFalse(is_newer("1.0.0", "1.1.0"))

    def test_prerelease_sorts_below_the_final_release(self):
        self.assertTrue(is_newer("1.2.0", "1.2.0-beta.1"))
        self.assertFalse(is_newer("1.2.0-beta.1", "1.2.0"))
        self.assertTrue(is_newer("1.2.0-beta.1", "1.1.0"))

    def test_garbage_version_never_claims_to_be_newer(self):
        self.assertFalse(is_newer("ban-moi-nhat", "1.1.0"))
        self.assertFalse(is_newer("1.2.0", "khong-biet"))


class AssetTests(unittest.TestCase):
    def test_picks_the_dmg_and_ignores_the_rest(self):
        asset = pick_asset(fake_release_payload()["assets"])
        self.assertEqual(asset["name"], "CheckActive-1.2.0.dmg")

    def test_uses_the_api_url_not_the_browser_url(self):
        """Repo private: browser_download_url cần cookie, chỉ api url mới chạy."""
        release = release_from_payload(fake_release_payload())
        self.assertTrue(release.asset_url.startswith("https://api.github.com/"))
        self.assertNotIn("browser", release.asset_url)

    def test_release_without_an_installer_is_still_readable(self):
        release = release_from_payload(fake_release_payload(with_asset=False))
        self.assertFalse(release.has_asset)
        self.assertTrue(release.page_url)

    def test_notes_are_trimmed(self):
        self.assertEqual(release_from_payload(fake_release_payload()).notes,
                         "Nhanh hơn, log tách theo serial.")


class CheckForUpdateTests(unittest.TestCase):
    def test_newer_release_is_reported(self):
        release = check_for_update("oneway/check-active", "tok", "1.1.0",
                                   opener=opener_returning(fake_release_payload()))
        self.assertIsInstance(release, Release)
        self.assertEqual(release.version, "1.2.0")
        self.assertEqual(release.tag, "v1.2.0")

    def test_same_version_reports_nothing(self):
        self.assertIsNone(check_for_update(
            "oneway/check-active", "tok", "1.2.0",
            opener=opener_returning(fake_release_payload())))

    def test_older_release_reports_nothing(self):
        self.assertIsNone(check_for_update(
            "oneway/check-active", "tok", "9.0.0",
            opener=opener_returning(fake_release_payload())))

    def test_token_is_sent_as_a_bearer_header(self):
        captured = []
        check_for_update("oneway/check-active", "tok-abc", "1.1.0",
                         opener=opener_returning(fake_release_payload(), captured))
        headers = {k.lower(): v for k, v in captured[0].header_items()}
        self.assertEqual(headers["authorization"], "Bearer tok-abc")
        self.assertIn("api.github.com/repos/oneway/check-active/releases/latest",
                      captured[0].full_url)

    def test_repo_with_no_releases_says_so(self):
        with self.assertRaises(UpdateError) as caught:
            fetch_latest_release("oneway/check-active", "tok",
                                 opener=opener_returning({}))
        self.assertIn("chưa có release", str(caught.exception))


class ErrorMessageTests(unittest.TestCase):
    """Lỗi phải nói được phải làm gì tiếp, không chỉ ném mã số ra."""

    def _fails_with(self, error):
        with self.assertRaises(UpdateError) as caught:
            fetch_latest_release("oneway/check-active", "tok",
                                 opener=opener_raising(error))
        return str(caught.exception)

    def test_bad_token(self):
        message = self._fails_with(HTTPError("u", 401, "Unauthorized", {}, None))
        self.assertIn("Token", message)

    def test_private_repo_without_permission(self):
        message = self._fails_with(HTTPError("u", 404, "Not Found", {}, None))
        self.assertIn("private", message)
        self.assertIn("oneway/check-active", message)

    def test_rate_limited(self):
        self.assertIn("Thử lại sau", self._fails_with(
            HTTPError("u", 403, "Forbidden", {}, None)))

    def test_offline(self):
        self.assertIn("Không nối được GitHub",
                      self._fails_with(URLError("mất mạng")))

    def test_broken_json(self):
        with self.assertRaises(UpdateError) as caught:
            fetch_latest_release("oneway/check-active", "tok",
                                 opener=opener_returning(b"khong phai json"))
        self.assertIn("dữ liệu lạ", str(caught.exception))


class DownloadTests(unittest.TestCase):
    def _release(self, **kwargs):
        base = dict(version="1.2.0", tag="v1.2.0", title="t", notes="",
                    page_url="p", asset_name="CheckActive-1.2.0.dmg",
                    asset_url="https://api.github.com/x/assets/2", asset_size=9)
        base.update(kwargs)
        return Release(**base)

    def test_file_lands_with_the_asset_name_and_right_bytes(self):
        with TemporaryDirectory() as tmp:
            path = download_release(self._release(), "tok", target_dir=tmp,
                                    opener=opener_returning(b"noi-dung-dmg"))
            self.assertEqual(Path(path).name, "CheckActive-1.2.0.dmg")
            self.assertEqual(Path(path).read_bytes(), b"noi-dung-dmg")

    def test_octet_stream_header_is_required_for_private_assets(self):
        captured = []
        with TemporaryDirectory() as tmp:
            download_release(self._release(), "tok", target_dir=tmp,
                             opener=opener_returning(b"x", captured))
        headers = {k.lower(): v for k, v in captured[0].header_items()}
        self.assertEqual(headers["accept"], "application/octet-stream")
        self.assertEqual(headers["authorization"], "Bearer tok")

    def test_progress_is_reported(self):
        seen = []
        with TemporaryDirectory() as tmp:
            download_release(self._release(asset_size=12), "tok", target_dir=tmp,
                             opener=opener_returning(b"noi-dung-dmg"),
                             on_progress=lambda done, total: seen.append((done, total)))
        self.assertEqual(seen[-1], (12, 12))

    def test_release_without_an_asset_explains_instead_of_crashing(self):
        with TemporaryDirectory() as tmp:
            with self.assertRaises(UpdateError) as caught:
                download_release(self._release(asset_url="", asset_name=""),
                                 "tok", target_dir=tmp)
        self.assertIn(ASSET_SUFFIX, str(caught.exception))

    def test_a_failed_download_leaves_no_half_file_behind(self):
        with TemporaryDirectory() as tmp:
            with self.assertRaises(UpdateError):
                download_release(self._release(), "tok", target_dir=tmp,
                                 opener=opener_raising(URLError("đứt giữa chừng")))
            self.assertEqual(list(Path(tmp).iterdir()), [],
                             "không được để lại file .part")


class FormatSizeTests(unittest.TestCase):
    def test_reads_like_vietnamese_numbers(self):
        self.assertEqual(format_size(2_411_724), "2,3 MB")
        self.assertEqual(format_size(5_000), "4,9 KB")
        self.assertEqual(format_size(0), "")


if __name__ == "__main__":
    unittest.main()
