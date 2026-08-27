import asyncio
import contextlib
import csv
import io
import os
import sys
import types
import unittest
from pathlib import Path
from tempfile import NamedTemporaryFile
from tempfile import TemporaryDirectory

fake_playwright = types.ModuleType("playwright")
fake_playwright_async_api = types.ModuleType("playwright.async_api")


async def _fake_async_playwright():
    return None


fake_playwright_async_api.async_playwright = _fake_async_playwright
fake_playwright.async_api = fake_playwright_async_api
sys.modules.setdefault("playwright", fake_playwright)
sys.modules.setdefault("playwright.async_api", fake_playwright_async_api)

fake_playwright_stealth = types.ModuleType("playwright_stealth")


class _FakeStealth:
    def use_async(self, playwright_context):
        return playwright_context


fake_playwright_stealth.Stealth = _FakeStealth
sys.modules.setdefault("playwright_stealth", fake_playwright_stealth)

from unittest.mock import AsyncMock, patch

from check_active_v2 import RunTimer

from check_active_parallel import (
    chunked,
    parse_args,
    DEFAULT_CONCURRENCY,
    append_result_to_csv,
    format_progress,
    initialize_output_file,
    resolve_concurrency,
    MAX_CONCURRENCY,
    run_parallel_tasks,
)


class ParallelRunnerTests(unittest.IsolatedAsyncioTestCase):
    def test_default_concurrency_is_one(self):
        self.assertEqual(DEFAULT_CONCURRENCY, 1)

    def test_chunked_groups_serials_by_batch_size(self):
        batches = list(chunked([1, 2, 3, 4, 5, 6, 7], 4))
        self.assertEqual(batches, [[1, 2, 3, 4], [5, 6, 7]])

    def test_format_progress_returns_done_over_total(self):
        self.assertEqual(format_progress(123, 400), "123/400")

    def test_initialize_and_append_result_to_csv_write_rows_immediately(self):
        with NamedTemporaryFile("w+", newline="", encoding="utf-8-sig", delete=False) as temp_file:
            temp_path = temp_file.name

        try:
            initialize_output_file(temp_path)
            append_result_to_csv(temp_path, ["SN001", "Chưa active"])
            append_result_to_csv(temp_path, ["SN002", "03/04/2026"])

            with open(temp_path, newline="", encoding="utf-8-sig") as csv_file:
                rows = list(csv.reader(csv_file))

            self.assertEqual(rows[0], ["Serial", "Ngày mua / Trạng thái"])
            self.assertEqual(rows[1], ["SN001", "Chưa active"])
            self.assertEqual(rows[2], ["SN002", "03/04/2026"])
        finally:
            os.remove(temp_path)

    async def test_run_parallel_tasks_preserves_input_order(self):
        async def worker(item):
            await asyncio.sleep(0.01 * (4 - item))
            return item * 10

        results = await run_parallel_tasks([1, 2, 3, 4], worker, limit=2)

        self.assertEqual(results, [10, 20, 30, 40])

    async def test_run_parallel_tasks_respects_concurrency_limit(self):
        active_tasks = 0
        peak_tasks = 0

        async def worker(item):
            nonlocal active_tasks, peak_tasks
            active_tasks += 1
            peak_tasks = max(peak_tasks, active_tasks)
            await asyncio.sleep(0.02)
            active_tasks -= 1
            return item

        results = await run_parallel_tasks(list(range(6)), worker, limit=3)

        self.assertEqual(results, [0, 1, 2, 3, 4, 5])
        self.assertEqual(peak_tasks, 3)



class ConcurrencyTests(unittest.TestCase):
    def test_requested_thread_count_is_honoured(self):
        self.assertEqual(resolve_concurrency([], requested=2), 2)
        self.assertEqual(resolve_concurrency([], requested=3), 3)

    def test_parallel_no_longer_requires_proxies(self):
        """Phan lon thoi gian moi serial la ngoi cho 2captcha, khong phai gui request."""
        self.assertEqual(resolve_concurrency([], requested=3), 3)

    def test_thread_count_is_capped(self):
        self.assertEqual(resolve_concurrency([], requested=99), MAX_CONCURRENCY)

    def test_thread_count_floors_at_one(self):
        self.assertEqual(resolve_concurrency([], requested=0), 1)
        self.assertEqual(resolve_concurrency([], requested=-5), 1)

    def test_garbage_input_falls_back_to_default(self):
        self.assertEqual(resolve_concurrency([], requested=None), DEFAULT_CONCURRENCY)
        self.assertEqual(resolve_concurrency([], requested="ba"), DEFAULT_CONCURRENCY)

    def test_kill_switch_still_forces_one(self):
        with patch("check_active_parallel.ALLOW_PARALLEL_WITHOUT_PROXY", False):
            self.assertEqual(resolve_concurrency([], requested=3), 1)



class ForceFlagTests(unittest.TestCase):
    def test_force_flag_defaults_to_false(self):
        self.assertFalse(parse_args([]).force)

    def test_force_flag_accepts_short_and_long_form(self):
        self.assertTrue(parse_args(["-f"]).force)
        self.assertTrue(parse_args(["--force"]).force)



class ConcurrencyOverrideTests(unittest.TestCase):
    def test_concurrency_flag_defaults_to_none(self):
        self.assertIsNone(parse_args([]).concurrency)

    def test_concurrency_flag_is_parsed(self):
        self.assertEqual(parse_args(["-c", "3"]).concurrency, 3)
        self.assertEqual(parse_args(["--concurrency", "6"]).concurrency, 6)

    def test_override_is_capped_not_ignored(self):
        self.assertEqual(resolve_concurrency([], requested=MAX_CONCURRENCY + 4, override=True), MAX_CONCURRENCY)
        self.assertEqual(resolve_concurrency([{"server": "a"}], requested=3, override=True), 3)

    def test_override_still_floors_at_one(self):
        self.assertEqual(resolve_concurrency([], requested=0, override=True), 1)


class SingleSerialModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_main_ignores_proxy_file_and_spawns_requested_workers(self):
        import check_active_parallel as par

        with TemporaryDirectory() as tmp:
            input_path = Path(tmp) / "serials.txt"
            output_path = Path(tmp) / "results.csv"
            with open(input_path, "w", encoding="utf-8") as f:
                f.write("AAA11111\n")

            worker_calls = []
            log = io.StringIO()

            class FakeBrowser:
                async def close(self):
                    return None

            async def fake_run_worker(index, browser, queue, proxies, run_settings, state, on_result):
                worker_calls.append((index, list(proxies)))
                on_result(["AAA11111", "01/03/2026"], 1)

            stealth_context = patch("check_active_parallel.create_stealth_playwright_context")
            with (
                patch.object(par, "INPUT_FILE", input_path),
                patch.object(par, "OUTPUT_FILE", output_path),
                patch("check_active_parallel.preflight_2captcha", new=AsyncMock(return_value=True)),
                stealth_context as mock_stealth,
                patch("check_active_parallel.launch_browser", new=AsyncMock(return_value=FakeBrowser())),
                patch("check_active_parallel.run_worker", new=fake_run_worker),
                patch("check_active_parallel.export_inactive_to_excel", return_value="inactive.xlsx"),
            ):
                mock_stealth.return_value.__aenter__.return_value = object()
                mock_stealth.return_value.__aexit__.return_value = False

                with contextlib.redirect_stdout(log):
                    await par.main(
                        force=True,
                        concurrency=4,
                        args_headless=True,
                        run_settings={"capture_screenshot": False, "folder_name": None},
                    )

            self.assertEqual(worker_calls, [(0, []), (1, []), (2, []), (3, [])],
                             "xin nhieu luong thi khong con ep ve 1")
            self.assertNotIn("Tổng serial", log.getvalue())
            self.assertNotIn("Chạy 1 luồng", log.getvalue())
            self.assertNotIn("session mới", log.getvalue())



class WorkerResilienceTests(unittest.IsolatedAsyncioTestCase):
    """Regression: proxy chậm làm Page.goto timeout đã từng làm sập cả run."""

    class FakePage:
        async def close(self):
            return None

    class FakeContext:
        def __init__(self):
            self.closed = False

        async def new_page(self):
            return WorkerResilienceTests.FakePage()

        async def close(self):
            self.closed = True

    class FakeThrottle:
        def __init__(self):
            self.blocks = 0

        def on_success(self):
            return None

        async def on_block(self, reason=""):
            self.blocks += 1

    def _state(self):
        return {
            "stop": False,
            "throttle": self.FakeThrottle(),
            "block_attempts": {},
            "proxy_failures": {},
            "dead_proxies": set(),
            "session_proxy": {},
            "proxy_cursor": -1,
            "timer": RunTimer(),
            "should_stop": lambda: False,
            "serial_timeout": 30,
        }

    async def _run(self, serials, check_serial_impl, proxies=None):
        import check_active_parallel as par

        queue = asyncio.Queue()
        for serial in serials:
            queue.put_nowait(serial)

        results = []
        state = self._state()
        proxies = proxies if proxies is not None else [{"server": "http://p1:1"}, {"server": "http://p2:2"}]

        with (
            patch("check_active_parallel.create_browser_context", new=AsyncMock(side_effect=lambda *a, **k: self.FakeContext())),
            patch("check_active_parallel.check_serial", new=check_serial_impl),
            patch("check_active_parallel.polite_delay", new=AsyncMock()),
            patch("check_active_parallel.PROXY_FAILURE_DELAY_SECONDS", 0),
        ):
            def on_result(result, duration=None):
                results.append(result)

            await par.run_worker(
                0, None, queue, proxies,
                {"capture_screenshot": False, "folder_name": None},
                state, on_result,
            )

        return results, state

    async def test_goto_timeout_does_not_kill_the_run(self):
        """Truoc day: TimeoutError bay thang ra gather va giet ca chuong trinh."""
        from check_active_v2 import ProxyFailure

        async def always_proxy_failure(*_args, **_kwargs):
            raise ProxyFailure("Page.goto: Timeout 30000ms exceeded.")

        results, state = await self._run(["AAA11111"], always_proxy_failure)

        self.assertEqual(results, [["AAA11111", "Proxy hỏng"]])
        self.assertEqual(state["throttle"].blocks, 0, "proxy hỏng thì không được nghỉ 60s")

    async def test_run_continues_to_next_serial_after_proxy_failure(self):
        from check_active_v2 import ProxyFailure

        seen = []

        async def fail_first_serial(page, serial, **_kwargs):
            seen.append(serial)
            if serial == "AAA11111":
                raise ProxyFailure("timeout")
            return [serial, "01/03/2026"]

        results, _ = await self._run(["AAA11111", "BBB22222"], fail_first_serial)

        self.assertIn(["BBB22222", "01/03/2026"], results)
        self.assertIn(["AAA11111", "Proxy hỏng"], results)

    async def test_network_failures_do_not_retire_proxies_in_single_serial_mode(self):
        from check_active_v2 import ProxyFailure

        async def always_proxy_failure(*_args, **_kwargs):
            raise ProxyFailure("timeout")

        _, state = await self._run(["AAA11111", "BBB22222", "CCC33333"], always_proxy_failure)
        self.assertEqual(state["dead_proxies"], set())

    async def test_unexpected_error_is_recorded_not_fatal(self):
        async def explode(*_args, **_kwargs):
            raise ValueError("lỗi lạ hoắc")

        results, _ = await self._run(["AAA11111"], explode)
        self.assertEqual(results, [["AAA11111", "Lỗi không xác định"]])

    async def test_closed_browser_exits_quietly_without_writing_garbage(self):
        """Khi dang tat, dung ghi ket qua rac vao CSV."""
        async def closed(*_args, **_kwargs):
            raise Exception("Target page, context or browser has been closed")

        results, _ = await self._run(["AAA11111"], closed)
        self.assertEqual(results, [])

    async def test_legacy_proxy_list_does_not_stop_the_run(self):
        from check_active_v2 import ProxyFailure

        async def always_proxy_failure(*_args, **_kwargs):
            raise ProxyFailure("timeout")

        serials = [f"SN{i:06d}" for i in range(12)]
        _, state = await self._run(serials, always_proxy_failure, proxies=[{"server": "http://only:1"}])
        self.assertFalse(state["stop"])



class RunTimerTests(unittest.TestCase):
    """RunTimer la thu tra loi cau hoi 'co that su chay song song khong'."""

    def test_sequential_work_measures_about_one(self):
        timer = RunTimer()
        timer.started_at -= 30      # gia bo da troi qua 30 giay
        timer.record(10)
        timer.record(10)
        timer.record(10)
        self.assertAlmostEqual(timer.average_concurrency(), 1.0, places=1)

    def test_four_way_parallel_work_measures_about_four(self):
        timer = RunTimer()
        timer.started_at -= 30
        for _ in range(12):         # 12 serial x 10s = 120s viec trong 30s dong ho
            timer.record(10)
        self.assertAlmostEqual(timer.average_concurrency(), 4.0, places=1)

    def test_rate_per_minute(self):
        timer = RunTimer()
        timer.started_at -= 60
        for _ in range(8):
            timer.record(5)
        self.assertAlmostEqual(timer.rate_per_minute(), 8.0, places=1)

    def test_summary_mentions_measured_concurrency(self):
        timer = RunTimer()
        timer.started_at -= 10
        timer.record(20)
        self.assertIn("song song thực tế", timer.summary())


class HeadlessFlagTests(unittest.TestCase):
    def test_headless_defaults_to_false(self):
        self.assertFalse(parse_args([]).headless)

    def test_headless_can_be_enabled(self):
        self.assertTrue(parse_args(["--headless"]).headless)



class StopSignalTests(unittest.IsolatedAsyncioTestCase):
    """Nut Dung trong app phai dung that, khong doi het hang doi."""

    async def test_worker_stops_when_should_stop_returns_true(self):
        import check_active_parallel as par

        queue = asyncio.Queue()
        for serial in [f"SN{i:06d}" for i in range(20)]:
            queue.put_nowait(serial)

        results = []
        state = {
            "stop": False,
            "throttle": WorkerResilienceTests.FakeThrottle(),
            "block_attempts": {},
            "proxy_failures": {},
            "dead_proxies": set(),
            "session_proxy": {},
            "proxy_cursor": -1,
            "timer": RunTimer(),
            "should_stop": lambda: len(results) >= 3,
            "serial_timeout": 30,
        }

        async def ok(page, serial, **_kwargs):
            return [serial, "01/03/2026"]

        def on_result(result, duration=None):
            results.append(result)

        with (
            patch("check_active_parallel.create_browser_context",
                  new=AsyncMock(side_effect=lambda *a, **k: WorkerResilienceTests.FakeContext())),
            patch("check_active_parallel.check_serial", new=ok),
            patch("check_active_parallel.polite_delay", new=AsyncMock()),
        ):
            await par.run_worker(0, None, queue, [], {"capture_screenshot": False, "folder_name": None},
                                 state, on_result)

        self.assertEqual(len(results), 3)
        self.assertTrue(state["stop"])
        self.assertGreater(queue.qsize(), 0, "còn serial chưa chạy = đã dừng sớm thật")

    async def test_worker_cancels_current_serial_when_stop_is_requested(self):
        import check_active_parallel as par

        queue = asyncio.Queue()
        queue.put_nowait("SLOW0001")

        results = []
        stop_requested = False
        state = {
            "stop": False,
            "throttle": WorkerResilienceTests.FakeThrottle(),
            "block_attempts": {},
            "proxy_failures": {},
            "dead_proxies": set(),
            "session_proxy": {},
            "proxy_cursor": -1,
            "timer": RunTimer(),
            "should_stop": lambda: stop_requested,
            "serial_timeout": 0.5,
        }

        async def slow_check(page, serial, **_kwargs):
            await asyncio.sleep(5)
            return [serial, "01/03/2026"]

        async def request_stop_soon():
            nonlocal stop_requested
            await asyncio.sleep(0.05)
            stop_requested = True

        def on_result(result, duration=None):
            results.append(result)

        with (
            patch("check_active_parallel.create_browser_context",
                  new=AsyncMock(side_effect=lambda *a, **k: WorkerResilienceTests.FakeContext())),
            patch("check_active_parallel.check_serial", new=slow_check),
            patch("check_active_parallel.polite_delay", new=AsyncMock()),
        ):
            stopper = asyncio.create_task(request_stop_soon())
            started = asyncio.get_running_loop().time()
            await par.run_worker(0, None, queue, [], {"capture_screenshot": False, "folder_name": None},
                                 state, on_result)
            elapsed = asyncio.get_running_loop().time() - started
            await stopper

        self.assertLess(elapsed, 0.25)
        self.assertTrue(state["stop"])
        self.assertEqual(results, [])



class SerialTimeoutTests(unittest.IsolatedAsyncioTestCase):
    """Serial nao keo qua lau thi bo qua, khong de treo ca run."""

    async def test_slow_serial_is_skipped_and_run_continues(self):
        import check_active_parallel as par

        queue = asyncio.Queue()
        queue.put_nowait("SLOW0001")
        queue.put_nowait("FAST0001")

        results = []
        state = {
            "stop": False,
            "throttle": WorkerResilienceTests.FakeThrottle(),
            "block_attempts": {},
            "proxy_failures": {},
            "dead_proxies": set(),
            "session_proxy": {},
            "proxy_cursor": -1,
            "timer": RunTimer(),
            "should_stop": lambda: False,
            "serial_timeout": 0.05,
        }

        async def slow_for_one(page, serial, **_kwargs):
            if serial == "SLOW0001":
                await asyncio.sleep(5)
            return [serial, "01/03/2026"]

        def on_result(result, duration=None):
            results.append(result)

        with (
            patch("check_active_parallel.create_browser_context",
                  new=AsyncMock(side_effect=lambda *a, **k: WorkerResilienceTests.FakeContext())),
            patch("check_active_parallel.check_serial", new=slow_for_one),
            patch("check_active_parallel.polite_delay", new=AsyncMock()),
        ):
            await par.run_worker(0, None, queue, [], {"capture_screenshot": False, "folder_name": None},
                                 state, on_result)

        self.assertIn(["SLOW0001", "Quá giờ, bỏ qua"], results)
        self.assertIn(["FAST0001", "01/03/2026"], results)

    def test_timed_out_serial_is_retried_on_next_run(self):
        from check_active_v2 import needs_check

        self.assertTrue(needs_check("X", {"X": "Quá giờ, bỏ qua"}))
        self.assertFalse(needs_check("X", {"X": "01/03/2026"}))


if __name__ == "__main__":
    unittest.main()
