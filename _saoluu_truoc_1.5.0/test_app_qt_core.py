"""Test cho phần lõi giao diện Qt — không cần cài PySide6."""

import sys
import types
import unittest

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

from app_qt_core import (
    LogBuffer,
    RunStats,
    estimate_remaining_seconds,
    format_duration,
    format_rate,
    log_level,
    parse_running_serial,
    purchase_label,
    run_summary,
    serials_per_minute,
    status_kind,
    status_label,
)


class StatusKindTests(unittest.TestCase):
    def test_purchase_date_is_ok(self):
        self.assertEqual(status_kind("28/04/2026"), "ok")

    def test_inactive_device_is_a_warning_not_a_failure(self):
        """Chưa active là kết quả hợp lệ — không được tô đỏ như lỗi."""
        self.assertEqual(status_kind("Chưa active"), "warn")

    def test_unverified_and_missing_date_are_warnings(self):
        self.assertEqual(status_kind("Chưa xác thực"), "warn")
        self.assertEqual(status_kind("Không thấy ngày mua"), "warn")

    def test_failures_are_bad(self):
        for value in ("Check tay", "Bị chặn IP", "Lỗi load trang",
                      "serial ko hợp lệ", "Proxy hỏng"):
            self.assertEqual(status_kind(value), "bad", value)

    def test_timeout_result_is_bad(self):
        """Runner ghi 'Quá giờ, bỏ qua' — không có trong ERROR_VALUES."""
        self.assertEqual(status_kind("Quá giờ, bỏ qua"), "bad")

    def test_waiting_and_empty_are_idle(self):
        self.assertEqual(status_kind("đang chờ..."), "idle")
        self.assertEqual(status_kind("đang chạy..."), "idle")
        self.assertEqual(status_kind(""), "idle")
        self.assertEqual(status_kind(None), "idle")


class LabelTests(unittest.TestCase):
    def test_active_device_shows_date_in_date_column(self):
        self.assertEqual(purchase_label("28/04/2026"), "28/04/2026")
        self.assertEqual(status_label("28/04/2026"), "Đã active")

    def test_non_dates_leave_the_date_column_empty(self):
        self.assertEqual(purchase_label("Chưa active"), "—")
        self.assertEqual(purchase_label("Check tay"), "—")
        self.assertEqual(status_label("Chưa active"), "Chưa active")

    def test_idle_rows_keep_their_placeholder(self):
        self.assertEqual(purchase_label("đang chờ..."), "đang chờ...")
        self.assertEqual(status_label("đang chờ..."), "—")


class RunningSerialTests(unittest.TestCase):
    def test_picks_serial_out_of_the_scraper_log_line(self):
        self.assertEqual(parse_running_serial("🔍 Check: C02XG2JRD"), "C02XG2JRD")

    def test_ignores_other_lines(self):
        self.assertIsNone(parse_running_serial("  [Lần 1] AI đoán: K4W9"))
        self.assertIsNone(parse_running_serial("💾 Đã lưu kết quả: X -> Y"))
        self.assertIsNone(parse_running_serial(""))
        self.assertIsNone(parse_running_serial(None))


class LogLevelTests(unittest.TestCase):
    def test_levels_follow_the_emoji_the_scraper_already_prints(self):
        self.assertEqual(log_level("  ❌ Sai mã, đang thử mã khác..."), "bad")
        self.assertEqual(log_level("  ⚠️ Không thấy captcha"), "warn")
        self.assertEqual(log_level("  ✅ THÀNH CÔNG: 01/03/2026"), "ok")
        self.assertEqual(log_level("🔍 Check: C02XG2JRD"), "info")

    def test_block_and_dead_proxy_count_as_bad(self):
        self.assertEqual(log_level("  🛑 Bị chặn lần 1"), "bad")
        self.assertEqual(log_level("💀 Bỏ hẳn proxy x"), "bad")


class RunStatsTests(unittest.TestCase):
    def test_counts_split_into_three_buckets(self):
        stats = RunStats(total=5)
        for value in ("28/04/2026", "Chưa active", "Check tay", "01/01/2025"):
            stats.record(value)

        self.assertEqual(stats.done, 4)
        self.assertEqual(stats.active, 2)
        self.assertEqual(stats.inactive, 1)
        self.assertEqual(stats.failed, 1)
        self.assertEqual(stats.remaining, 1)

    def test_placeholder_rows_are_not_counted(self):
        stats = RunStats(total=2)
        stats.record("đang chờ...")
        stats.record("")
        self.assertEqual(stats.done, 0)

    def test_remaining_never_goes_negative(self):
        stats = RunStats(total=1)
        stats.record("28/04/2026")
        stats.record("28/04/2026")
        self.assertEqual(stats.remaining, 0)


class RateAndEtaTests(unittest.TestCase):
    def test_rate_is_serials_per_minute(self):
        self.assertAlmostEqual(serials_per_minute(10, 120), 5.0)

    def test_rate_is_unknown_before_the_first_result(self):
        self.assertIsNone(serials_per_minute(0, 30))
        self.assertIsNone(serials_per_minute(5, 0))

    def test_eta_scales_from_measured_speed(self):
        # 10 serial trong 100s -> 10s/serial -> 20 serial con lai = 200s
        self.assertAlmostEqual(estimate_remaining_seconds(10, 20, 100), 200.0)

    def test_eta_is_zero_when_nothing_is_left(self):
        self.assertEqual(estimate_remaining_seconds(10, 0, 100), 0)

    def test_eta_is_unknown_before_the_first_result(self):
        self.assertIsNone(estimate_remaining_seconds(0, 20, 30))

    def test_duration_formatting(self):
        self.assertEqual(format_duration(0), "0:00")
        self.assertEqual(format_duration(65), "1:05")
        self.assertEqual(format_duration(3725), "1:02:05")
        self.assertEqual(format_duration(None), "—")

    def test_rate_formatting_uses_a_comma(self):
        self.assertEqual(format_rate(3.44), "3,4")
        self.assertEqual(format_rate(None), "—")


class RunSummaryTests(unittest.TestCase):
    """Chạy xong phải nói ngay mẻ đó hết bao lâu, khỏi phải đi đoán."""

    def test_summary_names_time_rate_and_per_serial(self):
        # Dung con so that cua me 26/08: 18 serial trong 4 phut 38
        self.assertEqual(
            run_summary(18, 278),
            "Xong 18 serial trong 4:38 · 3,9 serial/phút · 15s mỗi serial",
        )

    def test_failures_are_called_out(self):
        self.assertIn("2 lỗi", run_summary(18, 278, failed=2))

    def test_no_failures_means_no_mention(self):
        self.assertNotIn("lỗi", run_summary(18, 278, failed=0))

    def test_summary_survives_an_empty_run(self):
        self.assertEqual(run_summary(0, 0), "Xong 0 serial trong 0:00")


class LogBufferTests(unittest.TestCase):
    """Chạy 2 luồng thì log đan xen — phải cắt lại được theo từng serial."""

    def _interleaved(self):
        """Đúng hình dạng log thật của một mẻ 2 luồng."""
        buffer = LogBuffer()
        buffer.add("🔍 Check: CY2QLQ6XTJ", "CY2QLQ6XTJ")
        buffer.add("🔍 Check: HWV617QRXQ", "HWV617QRXQ")
        buffer.add("  [Lần 1] AI đoán: LJUWX", "CY2QLQ6XTJ")
        buffer.add("  [Lần 1] AI đoán: VXXTM", "HWV617QRXQ")
        buffer.add("  ✅ THÀNH CÔNG: 28/04/2026", "CY2QLQ6XTJ")
        buffer.add("  💾 Đã lưu kết quả: CY2QLQ6XTJ -> 28/04/2026 (1/18)", "CY2QLQ6XTJ")
        buffer.add("  ❌ Sai mã, đang thử mã khác...", "HWV617QRXQ")
        buffer.add("  [Lần 2] AI đoán: 4LNCY", "HWV617QRXQ")
        buffer.add("  ✅ THÀNH CÔNG: 02/05/2026", "HWV617QRXQ")
        return buffer

    def test_one_serial_history_is_complete_and_in_order(self):
        lines = [e.line for e in self._interleaved().view(serial="HWV617QRXQ")]
        self.assertEqual(lines, [
            "🔍 Check: HWV617QRXQ",
            "  [Lần 1] AI đoán: VXXTM",
            "  ❌ Sai mã, đang thử mã khác...",
            "  [Lần 2] AI đoán: 4LNCY",
            "  ✅ THÀNH CÔNG: 02/05/2026",
        ])

    def test_the_other_serial_is_not_mixed_in(self):
        lines = [e.line for e in self._interleaved().view(serial="CY2QLQ6XTJ")]
        self.assertEqual(len(lines), 4)
        self.assertTrue(all("HWV617QRXQ" not in line or "Check:" not in line
                            for line in lines))

    def test_no_filter_keeps_every_line(self):
        self.assertEqual(len(self._interleaved().view()), 9)

    def test_problem_filter_keeps_warnings_and_errors(self):
        levels = {e.level for e in self._interleaved().view(only_problems=True)}
        self.assertEqual(levels, {"bad"})

    def test_filters_combine(self):
        buffer = self._interleaved()
        entries = buffer.view(serial="HWV617QRXQ", only_problems=True)
        self.assertEqual([e.line for e in entries],
                         ["  ❌ Sai mã, đang thử mã khác..."])

    def test_blank_lines_are_dropped(self):
        buffer = LogBuffer()
        self.assertIsNone(buffer.add(""))
        self.assertIsNone(buffer.add("   "))
        self.assertEqual(buffer.entries, [])

    def test_lines_without_a_serial_survive_but_are_hidden_when_filtering(self):
        buffer = LogBuffer()
        buffer.add("🌐 Session #1", None)
        buffer.add("🔍 Check: AAA11111", "AAA11111")
        self.assertEqual(len(buffer.view()), 2)
        self.assertEqual(len(buffer.view(serial="AAA11111")), 1)

    def test_serial_list_keeps_first_seen_order(self):
        self.assertEqual(self._interleaved().serials(),
                         ["CY2QLQ6XTJ", "HWV617QRXQ"])

    def test_buffer_drops_the_oldest_lines_past_the_limit(self):
        buffer = LogBuffer(limit=3)
        for index in range(6):
            buffer.add(f"dòng {index}", "AAA11111")
        self.assertEqual([e.line for e in buffer.entries],
                         ["dòng 3", "dòng 4", "dòng 5"])


class EndToEndLogSplitTests(unittest.IsolatedAsyncioTestCase):
    """Đường đi thật: check_serial -> print -> SignalWriter -> LogBuffer."""

    def _fake_page(self):
        from unittest.mock import AsyncMock

        locator = AsyncMock()
        locator.first = locator

        class FakePage:
            def __init__(self):
                self.keyboard = AsyncMock()

            def locator(self, _selector):
                return locator

        return FakePage()

    async def test_two_workers_interleave_but_log_splits_cleanly(self):
        import asyncio
        import contextlib
        from unittest.mock import AsyncMock, patch

        from app_qt_core import LogBuffer, SignalWriter
        from check_active_v2 import CaptchaSolution, check_serial

        buffer = LogBuffer()
        writer = SignalWriter(buffer.add)

        async def slow_result(_page):
            await asyncio.sleep(0.02)
            return {"purchase_date": "01/03/2026"}

        with (
            patch("check_active_v2.open_check_page", new=AsyncMock(return_value=True)),
            patch("check_active_v2.grab_captcha_image", new=AsyncMock(return_value="QUJDRA==")),
            patch("check_active_v2.solve_captcha_task",
                  new=AsyncMock(return_value=CaptchaSolution("ABCD", 1))),
            patch("check_active_v2.wait_for_result_payload", new=slow_result),
            contextlib.redirect_stdout(writer),
        ):
            await asyncio.gather(
                check_serial(self._fake_page(), "C02XG2JRD", capture_screenshot=False),
                check_serial(self._fake_page(), "DKVQL1WXYZ", capture_screenshot=False),
            )
        writer.flush()

        raw = [e.line for e in buffer.view()]
        self.assertTrue(
            any("DKVQL1WXYZ" in line for line in raw[:4])
            and any("C02XG2JRD" in line for line in raw[:4]),
            "hai luồng phải in xen kẽ nhau, nếu không thì test này vô nghĩa",
        )

        for serial in ("C02XG2JRD", "DKVQL1WXYZ"):
            lines = [e.line for e in buffer.view(serial=serial)]
            self.assertIn(f"🔍 Check: {serial}", lines)
            self.assertTrue(any("THÀNH CÔNG" in line for line in lines))
            other = "DKVQL1WXYZ" if serial == "C02XG2JRD" else "C02XG2JRD"
            self.assertFalse(any(other in line for line in lines),
                             f"log của {serial} bị lẫn dòng của {other}")

        self.assertEqual(sorted(buffer.serials()), ["C02XG2JRD", "DKVQL1WXYZ"])
        self.assertEqual(
            len(buffer.view()),
            len(buffer.view(serial="C02XG2JRD")) + len(buffer.view(serial="DKVQL1WXYZ")),
            "không được mất dòng nào khi tách theo serial",
        )


class SerialTaggingTests(unittest.TestCase):
    """Nhãn serial phải theo đúng asyncio task, không lẫn giữa các luồng."""

    def test_context_var_is_isolated_per_task(self):
        import asyncio

        from check_active_v2 import CURRENT_SERIAL

        seen = []

        async def one(serial, pause):
            CURRENT_SERIAL.set(serial)
            await asyncio.sleep(pause)
            seen.append((serial, CURRENT_SERIAL.get()))

        async def both():
            await asyncio.gather(one("AAA11111", 0.02), one("BBB22222", 0.01))

        asyncio.run(both())
        self.assertEqual(sorted(seen),
                         [("AAA11111", "AAA11111"), ("BBB22222", "BBB22222")])

    def test_default_is_none_outside_a_check(self):
        from check_active_v2 import CURRENT_SERIAL

        self.assertIsNone(CURRENT_SERIAL.get())


class CrossThreadSignalTests(unittest.TestCase):
    """Tín hiệu từ luồng nền KHÔNG được nối vào lambda.

    Lambda không phải QObject nên Qt không biết nó thuộc luồng nào và chạy
    thẳng trên luồng worker. Bất kỳ thứ gì đụng tới cửa sổ (QMessageBox,
    QDialog) mà chạy ngoài luồng chính là macOS giết app tại chỗ:
        NSWindow should only be instantiated on the main thread!
    Nối vào phương thức của MainWindow thì Qt tự xếp hàng về luồng chính.
    """

    WORKER_OBJECTS = ("update_worker", "download_worker", "worker")

    def _connect_calls(self):
        import ast
        import pathlib

        source = pathlib.Path("app_qt.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        found = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "connect"):
                continue
            # func.value la <signal>, can lay <owner> trong self.<owner>.<signal>
            signal = func.value
            if not isinstance(signal, ast.Attribute):
                continue
            owner = signal.value
            if not (isinstance(owner, ast.Attribute) and isinstance(owner.value, ast.Name)
                    and owner.value.id == "self"):
                continue
            found.append((owner.attr, signal.attr, node.args[0] if node.args else None,
                          node.lineno))
        return found

    def test_no_worker_signal_is_connected_to_a_lambda(self):
        import ast

        vi_pham = [
            f"dong {line}: self.{obj}.{sig}.connect(lambda ...)"
            for obj, sig, arg, line in self._connect_calls()
            if obj in self.WORKER_OBJECTS and isinstance(arg, ast.Lambda)
        ]
        self.assertEqual(vi_pham, [], "\n".join(
            ["Tin hieu tu luong nen bi noi vao lambda -> se chay sai luong:"] + vi_pham))

    def test_worker_signals_are_connected_to_methods_of_the_window(self):
        """Doi chieu nguoc: phai co that cac ket noi, khong phai test rong."""
        import ast

        noi_dung = [
            (obj, sig) for obj, sig, arg, _ in self._connect_calls()
            if obj in self.WORKER_OBJECTS and isinstance(arg, ast.Attribute)
            and isinstance(arg.value, ast.Name) and arg.value.id == "self"
        ]
        self.assertGreaterEqual(len(noi_dung), 8,
                                f"chi tim thay {len(noi_dung)} ket noi, test co ve khong quet dung")
        self.assertIn(("update_worker", "failed"), noi_dung)
        self.assertIn(("update_worker", "up_to_date"), noi_dung)
        self.assertIn(("worker", "result"), noi_dung)


if __name__ == "__main__":
    unittest.main()
