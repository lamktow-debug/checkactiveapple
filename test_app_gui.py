"""Test cho app giao diện — phần logic, không cần mở cửa sổ."""

import json
import sys
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

fake_playwright = types.ModuleType("playwright")
fake_playwright_async_api = types.ModuleType("playwright.async_api")
fake_playwright_async_api.async_playwright = lambda: None
fake_playwright.async_api = fake_playwright_async_api
sys.modules.setdefault("playwright", fake_playwright)
sys.modules.setdefault("playwright.async_api", fake_playwright_async_api)

fake_playwright_stealth = types.ModuleType("playwright_stealth")


class _FakeStealth:
    def use_async(self, playwright_context):
        return playwright_context


fake_playwright_stealth.Stealth = _FakeStealth
sys.modules.setdefault("playwright_stealth", fake_playwright_stealth)

import app_core
import app_settings


class SettingsStoreTests(unittest.TestCase):
    def test_missing_file_returns_defaults(self):
        with TemporaryDirectory() as tmp:
            settings = app_settings.load_settings(Path(tmp) / "khong-co.json")
        self.assertEqual(settings, app_settings.DEFAULT_SETTINGS)

    def test_save_then_load_roundtrips(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.json"
            app_settings.save_settings({"twocaptcha_key": "abc123", "concurrency": 3}, path)
            loaded = app_settings.load_settings(path)
        self.assertEqual(loaded["twocaptcha_key"], "abc123")
        self.assertEqual(loaded["concurrency"], 3)
        self.assertEqual(loaded["block_assets"], app_settings.DEFAULT_SETTINGS["block_assets"])

    def test_unknown_keys_are_not_stored(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.json"
            app_settings.save_settings({"twocaptcha_key": "k", "rac": "khong luu"}, path)
            stored = json.loads(path.read_text(encoding="utf-8"))
        self.assertNotIn("rac", stored)

    def test_corrupt_file_falls_back_to_defaults(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.json"
            path.write_text("{ khong phai json", encoding="utf-8")
            settings = app_settings.load_settings(path)
        self.assertEqual(settings, app_settings.DEFAULT_SETTINGS)


class ApplySettingsTests(unittest.TestCase):
    """Double-click tu Finder khong co bien moi truong -> phai gan thang vao module."""

    def setUp(self):
        import check_active_v2

        self.scraper = check_active_v2
        self._key = check_active_v2.CAPTCHA_2CAPTCHA_API_KEY
        self._block = check_active_v2.BLOCK_ASSETS
        self._per_session = check_active_v2.SERIALS_PER_SESSION
        self._turbo = check_active_v2.TURBO_MODE

    def tearDown(self):
        self.scraper.CAPTCHA_2CAPTCHA_API_KEY = self._key
        self.scraper.BLOCK_ASSETS = self._block
        self.scraper.SERIALS_PER_SESSION = self._per_session
        self.scraper.TURBO_MODE = self._turbo

    def test_key_reaches_the_scraper_module(self):
        app_settings.apply_settings({"twocaptcha_key": "  key-tu-app  ", "block_assets": False})
        self.assertEqual(self.scraper.CAPTCHA_2CAPTCHA_API_KEY, "key-tu-app")
        self.assertFalse(self.scraper.BLOCK_ASSETS)

    def test_serials_per_session_also_updates_the_parallel_module(self):
        """check_active_parallel giu ban sao rieng, quen gan la cai dat vo tac dung."""
        import check_active_parallel as runner

        app_settings.apply_settings({"twocaptcha_key": "k", "serials_per_session": 7})
        self.assertEqual(runner.SERIALS_PER_SESSION, 7)

    def test_turbo_mode_reaches_the_scraper_module(self):
        app_settings.apply_settings({"twocaptcha_key": "k", "turbo_mode": True})
        self.assertTrue(self.scraper.TURBO_MODE)


class SerialParsingTests(unittest.TestCase):
    def setUp(self):
        self.app_gui = app_core

    def test_splits_on_newlines_commas_and_spaces(self):
        raw = "CY2QLQ6XTJ\nHWV617QRXQ, FHLGQMNXMF\tCYX4T34N5W"
        self.assertEqual(
            self.app_gui.parse_serials(raw),
            ["CY2QLQ6XTJ", "HWV617QRXQ", "FHLGQMNXMF", "CYX4T34N5W"],
        )

    def test_uppercases_and_removes_duplicates(self):
        self.assertEqual(self.app_gui.parse_serials("cy2qlq6xtj\nCY2QLQ6XTJ"), ["CY2QLQ6XTJ"])

    def test_ignores_blank_input(self):
        self.assertEqual(self.app_gui.parse_serials("   \n\n  "), [])

    def test_reads_results_csv(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "kq.csv"
            path.write_text(
                "Serial,Ngày mua / Trạng thái\nCY2QLQ6XTJ,28/04/2026\nX,Chưa active\n",
                encoding="utf-8-sig",
            )
            rows = self.app_gui.read_results(path)
        self.assertEqual(rows, [["CY2QLQ6XTJ", "28/04/2026"], ["X", "Chưa active"]])

    def test_read_results_handles_missing_file(self):
        self.assertEqual(self.app_gui.read_results(Path("khong-ton-tai.csv")), [])


class QueueWriterTests(unittest.TestCase):
    def setUp(self):
        import queue

        self.app_gui = app_core
        self.events = queue.Queue()

    def _drain(self):
        lines = []
        while not self.events.empty():
            kind, text = self.events.get_nowait()
            self.assertEqual(kind, "log")
            lines.append(text)
        return lines

    def test_splits_output_into_lines(self):
        writer = self.app_gui.QueueWriter(self.events)
        writer.write("dòng 1\ndòng 2\n")
        self.assertEqual(self._drain(), ["dòng 1", "dòng 2"])

    def test_partial_line_waits_for_newline(self):
        writer = self.app_gui.QueueWriter(self.events)
        writer.write("chưa xong")
        self.assertEqual(self._drain(), [])
        writer.write(" nhé\n")
        self.assertEqual(self._drain(), ["chưa xong nhé"])

    def test_flush_emits_trailing_text(self):
        writer = self.app_gui.QueueWriter(self.events)
        writer.write("không có xuống dòng")
        writer.flush()
        self.assertEqual(self._drain(), ["không có xuống dòng"])



class RowColourTests(unittest.TestCase):
    """Bang phai to mau dung: xanh = co ngay mua, do = loi."""

    def test_purchase_date_is_ok(self):
        self.assertEqual(app_core.row_tag("28/04/2026"), "ok")

    def test_inactive_device_is_ok_not_error(self):
        self.assertEqual(app_core.row_tag("Chưa active"), "ok")

    def test_failures_are_marked_bad(self):
        for value in ("Check tay", "Bị chặn IP", "Proxy hỏng", "serial ko hợp lệ"):
            self.assertEqual(app_core.row_tag(value), "bad", value)

    def test_waiting_and_unknown_are_neutral(self):
        self.assertEqual(app_core.row_tag("đang chờ..."), "wait")
        self.assertEqual(app_core.row_tag(""), "wait")


class GuiImportTests(unittest.TestCase):
    def test_app_gui_imports_when_tkinter_available(self):
        try:
            import tkinter  # noqa: F401
        except ImportError:
            self.skipTest("máy này không có tkinter (macOS có sẵn)")
        import app_gui

        self.assertTrue(hasattr(app_gui, "CheckActiveApp"))

    def test_launcher_uses_qt_gui(self):
        launcher = Path("Check Active.app/Contents/MacOS/launcher").read_text(encoding="utf-8")
        self.assertIn("app_qt.py", launcher)

    def test_packaged_requirements_install_qt(self):
        requirements = Path("requirements.txt").read_text(encoding="utf-8")
        self.assertIn("PySide6", requirements)

    def test_installer_checks_qt_dependency_and_rebuilds_bad_venv(self):
        installer = Path("CAI_DAT.command").read_text(encoding="utf-8")
        self.assertIn('"PySide6"', installer)
        self.assertIn("rm -rf venv", installer)
        self.assertIn("3.14", installer)



class NewDefaultsTests(unittest.TestCase):
    def test_defaults_to_one_serial_at_a_time(self):
        self.assertEqual(app_settings.DEFAULT_SETTINGS["concurrency"], 1)

    def test_defaults_to_two_minute_skip(self):
        self.assertEqual(app_settings.DEFAULT_SETTINGS["serial_timeout"], 120)

    def test_manual_captcha_is_off_by_default(self):
        """Bật sẵn = mỗi serial khó đứng yên chờ người gõ khi bạn đi vắng."""
        self.assertFalse(app_settings.DEFAULT_SETTINGS["manual_captcha"])

    def test_manual_captcha_countdown_is_short(self):
        """12 giây đủ gõ 4 ký tự; 60 giây là một phút chết mỗi serial khó."""
        self.assertLessEqual(app_core.MANUAL_CAPTCHA_WAIT_SECONDS, 15)

    def test_capture_screenshot_is_off_by_default(self):
        self.assertFalse(app_settings.DEFAULT_SETTINGS["capture_screenshot"])

    def test_capture_screenshot_setting_roundtrips(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.json"
            app_settings.save_settings({"capture_screenshot": True}, path)
            loaded = app_settings.load_settings(path)
        self.assertTrue(loaded["capture_screenshot"])

    def test_turbo_mode_is_off_by_default(self):
        self.assertFalse(app_settings.DEFAULT_SETTINGS["turbo_mode"])

    def test_turbo_mode_setting_roundtrips(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.json"
            app_settings.save_settings({"turbo_mode": True}, path)
            loaded = app_settings.load_settings(path)
        self.assertTrue(loaded["turbo_mode"])

    def test_timeout_reaches_the_scraper_module(self):
        import check_active_v2

        original = check_active_v2.SERIAL_TIMEOUT_SECONDS
        original_key = check_active_v2.CAPTCHA_2CAPTCHA_API_KEY
        try:
            app_settings.apply_settings({"twocaptcha_key": "k", "serial_timeout": 45})
            self.assertEqual(check_active_v2.SERIAL_TIMEOUT_SECONDS, 45)
        finally:
            check_active_v2.SERIAL_TIMEOUT_SECONDS = original
            check_active_v2.CAPTCHA_2CAPTCHA_API_KEY = original_key


if __name__ == "__main__":
    unittest.main()
