"""Test cho check_proxies.py.

Phần quan trọng: dựng proxy giả bằng socket thật trên localhost để chắc chắn
bước 1 nhận đúng proxy tốt và loại đúng proxy hỏng.
"""

import asyncio
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

import check_proxies as cp


class FakeProxyServer:
    """Proxy giả chạy trên localhost, trả lời theo kịch bản cho trước."""

    def __init__(self, responder):
        self.responder = responder
        self.server = None
        self.port = None

    async def __aenter__(self):
        self.server = await asyncio.start_server(self.responder, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *_args):
        self.server.close()
        await self.server.wait_closed()


async def http_ok(reader, writer):
    await reader.readline()
    writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
    await writer.drain()
    writer.close()


async def http_denied(reader, writer):
    await reader.readline()
    writer.write(b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n")
    await writer.drain()
    writer.close()


async def http_silent(reader, writer):
    await reader.readline()
    writer.close()


async def socks5_ok(reader, writer):
    await reader.readexactly(3)
    writer.write(b"\x05\x00")
    await writer.drain()
    writer.close()


async def socks5_needs_auth(reader, writer):
    await reader.readexactly(3)
    writer.write(b"\x05\xff")
    await writer.drain()
    writer.close()


async def not_socks_at_all(reader, writer):
    writer.write(b"HI")
    await writer.drain()
    writer.close()


class Stage1HttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_proxy_that_allows_connect_is_alive(self):
        async with FakeProxyServer(http_ok) as server:
            result = await cp.test_liveness({"server": f"http://127.0.0.1:{server.port}"})
        self.assertTrue(result["alive"])
        self.assertEqual(result["detail"], "CONNECT ok")

    async def test_http_proxy_that_refuses_connect_is_dead(self):
        """Proxy chi chuyen duoc http tran thi Apple (https) dung khong duoc."""
        async with FakeProxyServer(http_denied) as server:
            result = await cp.test_liveness({"server": f"http://127.0.0.1:{server.port}"})
        self.assertFalse(result["alive"])
        self.assertIn("407", result["detail"])

    async def test_http_proxy_that_hangs_up_is_dead(self):
        async with FakeProxyServer(http_silent) as server:
            result = await cp.test_liveness({"server": f"http://127.0.0.1:{server.port}"})
        self.assertFalse(result["alive"])


class Stage1Socks5Tests(unittest.IsolatedAsyncioTestCase):
    async def test_socks5_handshake_success(self):
        async with FakeProxyServer(socks5_ok) as server:
            result = await cp.test_liveness({"server": f"socks5://127.0.0.1:{server.port}"})
        self.assertTrue(result["alive"])
        self.assertEqual(result["detail"], "SOCKS5 ok")

    async def test_socks5_requiring_auth_is_rejected(self):
        async with FakeProxyServer(socks5_needs_auth) as server:
            result = await cp.test_liveness({"server": f"socks5://127.0.0.1:{server.port}"})
        self.assertFalse(result["alive"])
        self.assertIn("từ chối", result["detail"])

    async def test_non_socks_server_is_rejected(self):
        async with FakeProxyServer(not_socks_at_all) as server:
            result = await cp.test_liveness({"server": f"socks5://127.0.0.1:{server.port}"})
        self.assertFalse(result["alive"])
        self.assertEqual(result["detail"], "không phải SOCKS5")


class Stage1FailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_closed_port_is_dead_not_crash(self):
        result = await cp.test_liveness({"server": "http://127.0.0.1:1"}, timeout=3)
        self.assertFalse(result["alive"])

    async def test_unknown_scheme_is_reported(self):
        result = await cp.test_liveness({"server": "socks4://1.2.3.4:1080"})
        self.assertFalse(result["alive"])
        self.assertIn("socks4", result["detail"])

    async def test_malformed_server_does_not_crash(self):
        result = await cp.test_liveness({"server": "rác-không-có-port"})
        self.assertFalse(result["alive"])


class FreeListUrlTests(unittest.TestCase):
    def test_default_uses_all_proxies_file(self):
        """all-proxies.txt la danh sach rong nhat, dung no lam mac dinh."""
        self.assertIn("all-proxies.txt", cp.free_list_urls()[0])

    def test_both_branches_are_tried(self):
        urls = cp.free_list_urls()
        self.assertTrue(any("/main/" in url for url in urls))
        self.assertTrue(any("/master/" in url for url in urls))

    def test_country_overrides_protocol(self):
        urls = cp.free_list_urls("vn", protocol="http")
        self.assertTrue(all("countries/VN/proxies.txt" in url for url in urls))


class HelperTests(unittest.TestCase):
    def test_split_server_handles_scheme_host_port(self):
        self.assertEqual(cp.split_server("socks5://1.2.3.4:1080"), ("socks5", "1.2.3.4", 1080))
        self.assertEqual(cp.split_server("http://gate.example.com:8000"), ("http", "gate.example.com", 8000))

    def test_format_proxy_line_roundtrips_credentials(self):
        proxy = {"server": "http://gate:8000", "username": "u", "password": "p"}
        self.assertEqual(cp.format_proxy_line(proxy), "http://u:p@gate:8000")

    def test_format_proxy_line_without_credentials(self):
        self.assertEqual(cp.format_proxy_line({"server": "socks5://1.2.3.4:1080"}), "socks5://1.2.3.4:1080")


class ArgumentTests(unittest.TestCase):
    def test_defaults(self):
        args = cp.parse_args([])
        self.assertEqual(args.protocol, "all")
        self.assertEqual(args.limit, 1500)
        self.assertFalse(args.from_free_list)

    def test_flags_are_parsed(self):
        args = cp.parse_args(["--from-free-list", "--country", "VN", "--timeout", "20", "--output", "p.txt"])
        self.assertTrue(args.from_free_list)
        self.assertEqual(args.country, "VN")
        self.assertEqual(args.timeout, 20)
        self.assertEqual(args.output, "p.txt")



class NetworkCheckTests(unittest.IsolatedAsyncioTestCase):
    """Trang bao IP hong KHONG duoc chan ca run — buoc 1 dung socket tran."""

    def setUp(self):
        self._real_http_get = cp.http_get

    def tearDown(self):
        cp.http_get = self._real_http_get

    async def test_returns_ip_from_first_working_endpoint(self):
        cp.http_get = lambda url, proxy=None, timeout=None: "1.2.3.4"
        ok, ip = await cp.show_own_ip(5)
        self.assertTrue(ok)
        self.assertEqual(ip, "1.2.3.4")

    async def test_falls_back_when_first_endpoint_is_broken(self):
        from urllib.error import HTTPError

        def flaky(url, proxy=None, timeout=None):
            if "iplocate" in url:
                raise HTTPError(url, 500, "Internal Server Error", {}, None)
            return "5.6.7.8"

        cp.http_get = flaky
        ok, ip = await cp.show_own_ip(5)
        self.assertTrue(ok)
        self.assertEqual(ip, "5.6.7.8")

    async def test_http_error_everywhere_still_counts_as_network_ok(self):
        """500 nghia la DNS/TCP/TLS deu chay -> mang on, chi endpoint hong."""
        from urllib.error import HTTPError

        cp.http_get = lambda url, proxy=None, timeout=None: (_ for _ in ()).throw(
            HTTPError(url, 500, "Internal Server Error", {}, None)
        )
        ok, ip = await cp.show_own_ip(5)
        self.assertTrue(ok)
        self.assertIsNone(ip)

    async def test_connection_errors_everywhere_mean_network_down(self):
        cp.http_get = lambda url, proxy=None, timeout=None: (_ for _ in ()).throw(OSError("no route"))
        ok, ip = await cp.show_own_ip(5)
        self.assertFalse(ok)
        self.assertIsNone(ip)



class EarlyStopTests(unittest.IsolatedAsyncioTestCase):
    """Chi can vai proxy, khong can test het 554 cai."""

    def setUp(self):
        self._real_test_apple = cp.test_apple

    def tearDown(self):
        cp.test_apple = self._real_test_apple

    def _records(self, count):
        return [{"proxy": {"server": f"http://10.0.0.{i}:8080"}} for i in range(count)]

    async def test_stops_once_enough_proxies_found(self):
        """Moi proxy deu tot -> tim duoc 3 la dung, khong thu het 100."""
        cp.test_apple = lambda browser, proxy: asyncio.sleep(0, {"ok": True, "reason": "HTTP 200"})
        good, tested = await cp.find_working_proxies(None, self._records(100), need=3, concurrency=1)
        self.assertEqual(len(good), 3)
        self.assertLess(tested, 100)

    async def test_tests_everything_when_need_is_zero(self):
        cp.test_apple = lambda browser, proxy: asyncio.sleep(0, {"ok": True, "reason": "HTTP 200"})
        good, tested = await cp.find_working_proxies(None, self._records(12), need=0, concurrency=4)
        self.assertEqual(len(good), 12)
        self.assertEqual(tested, 12)

    async def test_returns_empty_when_nothing_works(self):
        cp.test_apple = lambda browser, proxy: asyncio.sleep(0, {"ok": False, "reason": "HTTP 403"})
        good, tested = await cp.find_working_proxies(None, self._records(20), need=5, concurrency=4)
        self.assertEqual(good, [])
        self.assertEqual(tested, 20)

    async def test_finds_the_few_good_ones_among_many_bad(self):
        async def mostly_bad(browser, proxy):
            ok = proxy["server"].endswith((".7:8080", ".19:8080"))
            return {"ok": ok, "reason": "HTTP 200" if ok else "HTTP 403"}

        cp.test_apple = mostly_bad
        good, tested = await cp.find_working_proxies(None, self._records(40), need=5, concurrency=4)
        self.assertEqual(len(good), 2)
        self.assertEqual(tested, 40)


if __name__ == "__main__":
    unittest.main()
