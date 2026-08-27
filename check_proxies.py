"""Kiểm tra proxy TRƯỚC khi chạy check active.

Hai giai đoạn, vì proxy "còn sống" không có nghĩa là "Apple cho vào":

  1. Nhanh (socket trần, không cần thư viện ngoài):
       - proxy http  -> gửi CONNECT, phải trả 200. Đây đúng là thứ Apple cần
         (tunnel HTTPS), proxy chỉ chuyển được http trần sẽ trượt ngay.
       - proxy socks5 -> bắt tay SOCKS5, phải trả 05 00.
     Chạy được vài trăm proxy cùng lúc, vài chục giây là xong.

  2. Thật: mở checkcoverage.apple.com bằng Playwright qua proxy đó và xem có
     thấy ô nhập serial không. Đây mới là thứ quyết định, vì Akamai chặn
     rất nhiều dải proxy công cộng dù proxy vẫn sống.

Dùng:
    python3 check_proxies.py                                   # test proxies.txt
    python3 check_proxies.py --from-free-list --output proxies.txt
    python3 check_proxies.py --from-free-list --limit 1500 --output proxies.txt
    python3 check_proxies.py --from-free-list --country VN --output proxies.txt
"""

import argparse
import asyncio
import sys
from collections import Counter
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from check_active_v2 import (
    BASE_URL,
    create_stealth_playwright_context,
    describe_proxy,
    load_proxies,
    parse_proxy_line,
)

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_PROXY_FILE = BASE_DIR / "proxies.txt"

# Chi dung de bao "IP that cua ban la gi" cho vui + kiem tra mang.
# Buoc 1 that su dung socket tran nen khong phu thuoc vao may cai nay.
IP_ECHO_URLS = (
    "https://api.iplocate.io/ip",
    "https://api.ipify.org",
    "https://icanhazip.com",
    "https://ifconfig.me/ip",
)
PROBE_HOST = "checkcoverage.apple.com"
PROBE_PORT = 443
LIVENESS_TIMEOUT_SECONDS = 10
LIVENESS_CONCURRENCY = 200
APPLE_TEST_CONCURRENCY = 8
APPLE_TEST_TIMEOUT_MS = 15000
DEFAULT_PROXIES_NEEDED = 6
SERIAL_INPUT_SELECTOR = "#serial-number-input"

FREE_LIST_REPO = "https://raw.githubusercontent.com/iplocate/free-proxy-list"
FREE_LIST_BRANCHES = ("main", "master")
SUPPORTED_SCHEMES = ("http", "https", "socks5")


def free_list_urls(country=None, protocol="all"):
    """Các URL có thể có của free-proxy-list (repo đổi nhánh/đường dẫn thì vẫn chạy)."""
    if country:
        paths = [f"countries/{country.upper()}/proxies.txt"]
    elif protocol == "all":
        paths = ["all-proxies.txt"]
    else:
        paths = [f"protocols/{protocol}.txt", "all-proxies.txt"]
    return [f"{FREE_LIST_REPO}/{branch}/{path}" for branch in FREE_LIST_BRANCHES for path in paths]


def http_get(url, proxy_server=None, timeout=LIVENESS_TIMEOUT_SECONDS):
    """GET đơn giản, có thể đi qua proxy. Trả về text."""
    handlers = []
    if proxy_server:
        handlers.append(ProxyHandler({"http": proxy_server, "https": proxy_server}))
    opener = build_opener(*handlers)
    request = Request(url, headers={"User-Agent": "curl/8.0"})
    with opener.open(request, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace").strip()


def download_free_list(country=None, protocol="all"):
    """Tải danh sách proxy free, thử lần lượt các URL cho tới khi được."""
    for url in free_list_urls(country, protocol):
        try:
            body = http_get(url, timeout=20)
        except (HTTPError, URLError, OSError) as error:
            print(f"  ⚠️ Không tải được {url} ({error})")
            continue
        lines = [line for line in body.splitlines() if line.strip()]
        if lines:
            print(f"  ✅ Tải {len(lines)} dòng từ {url}")
            return lines
    return []


def split_server(server):
    """Tách 'socks5://1.2.3.4:1080' thành (scheme, host, port)."""
    scheme, _, rest = server.partition("://")
    host, _, port = rest.rpartition(":")
    return scheme, host, int(port)


async def _close(writer):
    try:
        writer.close()
        await writer.wait_closed()
    except Exception:
        pass


async def probe_http_proxy(host, port):
    """Gửi CONNECT tới proxy HTTP — đúng thứ mình cần để đi vào Apple qua https."""
    reader, writer = await asyncio.open_connection(host, port)
    try:
        request = (
            f"CONNECT {PROBE_HOST}:{PROBE_PORT} HTTP/1.1\r\n"
            f"Host: {PROBE_HOST}:{PROBE_PORT}\r\n"
            f"User-Agent: curl/8.0\r\n"
            f"Proxy-Connection: keep-alive\r\n\r\n"
        ).encode("ascii")
        writer.write(request)
        await writer.drain()
        raw = await reader.readline()
        status = raw.decode("latin-1", "replace").strip()
        if not status:
            return False, "proxy đóng kết nối"
        if " 200" in status:
            return True, "CONNECT ok"
        return False, status[:48]
    finally:
        await _close(writer)


async def probe_socks5_proxy(host, port):
    """Bắt tay SOCKS5: gửi 05 01 00, proxy tử tế trả 05 00."""
    reader, writer = await asyncio.open_connection(host, port)
    try:
        writer.write(b"\x05\x01\x00")
        await writer.drain()
        data = await reader.readexactly(2)
        if data[0] != 5:
            return False, "không phải SOCKS5"
        if data[1] == 0:
            return True, "SOCKS5 ok"
        if data[1] == 0xFF:
            return False, "SOCKS5 từ chối (cần đăng nhập)"
        return False, f"SOCKS5 đòi method {data[1]}"
    finally:
        await _close(writer)


async def test_liveness(proxy, timeout=LIVENESS_TIMEOUT_SECONDS):
    """Giai đoạn 1: proxy có sống và có tunnel được không."""
    try:
        scheme, host, port = split_server(proxy["server"])
    except (ValueError, AttributeError):
        return {"proxy": proxy, "alive": False, "detail": "địa chỉ proxy sai định dạng"}

    if scheme.startswith("socks5"):
        prober = probe_socks5_proxy
    elif scheme in ("http", "https"):
        prober = probe_http_proxy
    else:
        return {"proxy": proxy, "alive": False, "detail": f"chưa hỗ trợ {scheme}"}

    try:
        ok, detail = await asyncio.wait_for(prober(host, port), timeout=timeout)
    except asyncio.TimeoutError:
        return {"proxy": proxy, "alive": False, "detail": "hết giờ"}
    except (OSError, asyncio.IncompleteReadError) as error:
        return {"proxy": proxy, "alive": False, "detail": type(error).__name__}
    except Exception as error:
        return {"proxy": proxy, "alive": False, "detail": type(error).__name__}

    return {"proxy": proxy, "alive": ok, "detail": detail}


async def launch_browser(playwright, headless=True):
    """Ưu tiên chromium đầy đủ.

    Playwright mới mặc định chạy headless bằng chrome-headless-shell — một
    binary riêng, cài `playwright install chromium` không phải lúc nào cũng có.
    channel="chromium" dùng bản chromium thường nên không cần tải thêm.
    """
    try:
        return await playwright.chromium.launch(headless=headless, channel="chromium")
    except Exception:
        return await playwright.chromium.launch(headless=headless)


async def find_working_proxies(browser, records, need, concurrency=APPLE_TEST_CONCURRENCY):
    """Test tới khi đủ `need` proxy dùng được thì dừng. need=0 nghĩa là test hết.

    Bạn chỉ cần vài proxy, không cần biết cả 554 cái ra sao — dừng sớm biến
    một tiếng đồng hồ thành vài phút.
    """
    queue = asyncio.Queue()
    for record in records:
        queue.put_nowait(record)

    good = []
    tested = 0
    stop = asyncio.Event()

    async def worker():
        nonlocal tested
        while not stop.is_set():
            try:
                record = queue.get_nowait()
            except asyncio.QueueEmpty:
                return

            verdict = await test_apple(browser, record["proxy"])
            tested += 1

            if verdict["ok"]:
                good.append(record)
                target = need if need else len(records)
                print(f"   ✅ [{len(good)}/{target}] {describe_proxy(record['proxy']):<32} {verdict['reason']}")
                if need and len(good) >= need:
                    stop.set()
                    return
            elif tested % 25 == 0:
                print(f"   ... đã thử {tested}/{len(records)}, tìm được {len(good)}")

    await asyncio.gather(*[worker() for _ in range(concurrency)])
    return good, tested


async def test_apple(browser, proxy):
    """Giai đoạn 2: Apple có cho proxy này vào không."""
    context = None
    try:
        context = await browser.new_context(
            proxy=proxy,
            locale="vi-VN",
            timezone_id="Asia/Ho_Chi_Minh",
        )
        page = await context.new_page()
        response = await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=APPLE_TEST_TIMEOUT_MS)
        status = getattr(response, "status", None)
        if status and status >= 400:
            return {"ok": False, "reason": f"HTTP {status}"}
        try:
            await page.wait_for_selector(SERIAL_INPUT_SELECTOR, timeout=APPLE_TEST_TIMEOUT_MS)
        except Exception:
            return {"ok": False, "reason": "không thấy ô nhập serial"}
        return {"ok": True, "reason": f"HTTP {status}"}
    except Exception as error:
        return {"ok": False, "reason": str(error).splitlines()[0][:60]}
    finally:
        if context is not None:
            try:
                await context.close()
            except Exception:
                pass


async def run_with_limit(items, worker, limit):
    """Chạy song song có giới hạn, giữ nguyên thứ tự."""
    semaphore = asyncio.Semaphore(limit)
    results = [None] * len(items)

    async def run_one(index, item):
        async with semaphore:
            results[index] = await worker(item)

    await asyncio.gather(*(run_one(i, item) for i, item in enumerate(items)))
    return results


def format_proxy_line(proxy):
    """Dựng lại dòng proxy để ghi ra file."""
    server = proxy["server"]
    if proxy.get("username"):
        scheme, _, rest = server.partition("://")
        return f"{scheme}://{proxy['username']}:{proxy['password']}@{rest}"
    return server


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Kiem tra proxy truoc khi chay check active")
    parser.add_argument("--file", default=str(DEFAULT_PROXY_FILE), help="File proxy can test")
    parser.add_argument("--from-free-list", action="store_true", help="Tai danh sach proxy free tren GitHub")
    parser.add_argument("--country", default=None, help="Ma quoc gia khi dung free list, vi du VN")
    parser.add_argument("--limit", type=int, default=1500, help="So proxy toi da lay tu free list")
    parser.add_argument("--output", default=None, help="Ghi cac proxy dung duoc ra file nay")
    parser.add_argument("--skip-apple", action="store_true", help="Chi test buoc 1, bo qua test Apple")
    parser.add_argument(
        "--protocol",
        default="all",
        choices=["all", "https", "http", "socks4", "socks5"],
        help="Lay danh sach nao tu free list (mac dinh all-proxies.txt)",
    )
    parser.add_argument("--timeout", type=int, default=LIVENESS_TIMEOUT_SECONDS, help="Timeout buoc 1 (giay)")
    parser.add_argument(
        "--need",
        type=int,
        default=DEFAULT_PROXIES_NEEDED,
        help="Tim duoc bay nhieu proxy tot thi dung (0 = test het)",
    )
    return parser.parse_args(argv)


async def show_own_ip(timeout):
    """Cho biết IP thật + xác nhận mạng còn sống.

    HTTPError nghĩa là DNS/TCP/TLS/HTTP đều chạy, chỉ endpoint kia hỏng —
    không phải lỗi mạng, nên vẫn chạy tiếp. Chỉ URLError/OSError mới đáng lo.
    """
    network_ok = False
    for url in IP_ECHO_URLS:
        try:
            return True, await asyncio.to_thread(http_get, url, None, timeout)
        except HTTPError:
            network_ok = True  # server tra loi duoc = mang on
            continue
        except (URLError, OSError, TimeoutError):
            continue
    return network_ok, None


async def main(args):
    print("🧪 Kiểm tra mạng (không dùng proxy)...")
    network_ok, my_ip = await show_own_ip(args.timeout)
    if my_ip:
        print(f"   ✅ Mạng ổn — IP thật của bạn: {my_ip}")
    elif network_ok:
        print("   ✅ Mạng ổn (mấy trang báo IP đang lỗi, không sao — bước 1 không cần chúng)")
    else:
        print("   ⚠️ Không gọi được trang nào để lấy IP. Nếu bước 1 trượt sạch thì")
        print("      nhiều khả năng do mạng chứ không phải do proxy.")

    if args.from_free_list:
        label = args.country or args.protocol
        print(f"\n📥 Tải danh sách proxy free ({label})...")
        lines = download_free_list(args.country, args.protocol)
        if not lines:
            print("❌ Không tải được danh sách nào.")
            return 1
        proxies = [p for p in (parse_proxy_line(line) for line in lines[: args.limit]) if p]
    else:
        proxies = load_proxies(args.file)
        if not proxies:
            print(f"❌ Không đọc được proxy nào từ {args.file}")
            return 1

    kinds = Counter(split_server(p["server"])[0] for p in proxies)
    print(f"   Đọc được {len(proxies)} proxy: " + ", ".join(f"{k}={v}" for k, v in kinds.most_common()))

    print(f"\n🔎 Giai đoạn 1: test {len(proxies)} proxy (timeout {args.timeout}s)...")
    liveness = await run_with_limit(
        proxies,
        lambda proxy: test_liveness(proxy, args.timeout),
        LIVENESS_CONCURRENCY,
    )

    alive = [r for r in liveness if r["alive"]]
    print(f"   Sống + tunnel được: {len(alive)}/{len(proxies)}")

    dead_reasons = Counter(r["detail"] for r in liveness if not r["alive"])
    if dead_reasons:
        print("   Lý do trượt (nhiều nhất trước):")
        for reason, count in dead_reasons.most_common(8):
            print(f"     {count:>5}×  {reason}")

    if not alive:
        print("\n❌ Không có proxy nào qua được bước 1.")
        return 1

    if args.skip_apple:
        good = alive
    else:
        goal = f"tới khi đủ {args.need}" if args.need else "tất cả"
        print(f"\n🍎 Giai đoạn 2: test {len(alive)} proxy với checkcoverage.apple.com ({goal})...")
        async with create_stealth_playwright_context() as p:
            try:
                browser = await launch_browser(p, headless=True)
            except Exception as error:
                print(f"   ❌ Không mở được trình duyệt: {str(error).splitlines()[0]}")
                print("   → Chạy: ./venv/bin/python -m playwright install")
                return 1
            try:
                good, tested = await find_working_proxies(browser, alive, args.need)
            finally:
                await browser.close()
        print(f"   Đã thử {tested}/{len(alive)} proxy")

    print(f"\n📊 Kết quả: {len(good)}/{len(proxies)} proxy dùng được với Apple")

    if not good:
        print("   Proxy công cộng bị Akamai chặn sẵn gần hết — chuyện bình thường.")
        return 1

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write("# Sinh boi check_proxies.py - chi gom proxy da qua duoc Apple\n")
            for record in good:
                f.write(format_proxy_line(record["proxy"]) + "\n")
        print(f"💾 Đã ghi {len(good)} proxy vào {args.output}")
        print(f"   Chạy: ./venv/bin/python check_active_parallel.py --concurrency {min(len(good), 4)}")
    else:
        print("   Thêm --output proxies.txt để lưu lại.")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(parse_args())))
