import argparse
import asyncio
import csv
import time
from pathlib import Path

from check_active_v2 import (
    MAX_BLOCK_RETRIES,
    PROXY_FAILURE_DELAY_SECONDS,
    SERIAL_TIMEOUT_SECONDS,
    SERIALS_PER_SESSION,
    BlockThrottle,
    BlockedError,
    CaptchaServiceError,
    ProxyFailure,
    RunTimer,
    launch_browser,
    is_page_closed_error,
    check_serial,
    create_browser_context,
    create_stealth_playwright_context,
    export_inactive_to_excel,
    format_progress,
    load_done_serials,
    load_serials,
    needs_check,
    polite_delay,
    preflight_2captcha,
    prompt_run_settings,
)

BASE_DIR = Path(__file__).resolve().parent
INPUT_FILE = BASE_DIR / "serials.txt"
OUTPUT_FILE = BASE_DIR / "ketqua_apple_parallel.csv"
DEFAULT_CONCURRENCY = 1
# Mỗi luồng dùng browser context riêng nhưng CHUNG một IP. Chạy N luồng nghĩa
# là tần suất gửi request tới Apple tăng đúng N lần — không có mẹo nào tránh
# được điều đó. Trần 3 là mức còn đỡ được bằng BlockThrottle nếu bị chặn.
MAX_CONCURRENCY = 5
ALLOW_PARALLEL_WITHOUT_PROXY = True
STOP_POLL_SECONDS = 0.1
# Các luồng khởi động lệch nhau cho đỡ đập cùng lúc vào Apple
WORKER_STAGGER_SECONDS = 2


def initialize_output_file(output_file=OUTPUT_FILE):
    """Khởi tạo file CSV kết quả ngay từ đầu để có thể ghi từng dòng."""
    with open(output_file, "w", newline="", encoding="utf-8-sig") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(["Serial", "Ngày mua / Trạng thái"])


def append_result_to_csv(output_file, row):
    """Ghi ngay một kết quả vào CSV."""
    with open(output_file, "a", newline="", encoding="utf-8-sig") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(row)


def chunked(items, chunk_size):
    """Chia danh sách thành các batch liên tiếp."""
    for start in range(0, len(items), chunk_size):
        yield items[start:start + chunk_size]


async def run_parallel_tasks(items, worker, limit=DEFAULT_CONCURRENCY):
    """Chạy danh sách tác vụ với số lượng đồng thời tối đa, giữ nguyên thứ tự kết quả."""
    semaphore = asyncio.Semaphore(limit)
    results = [None] * len(items)

    async def run_one(index, item):
        async with semaphore:
            results[index] = await worker(item)

    await asyncio.gather(*(run_one(index, item) for index, item in enumerate(items)))
    return results


def resolve_concurrency(proxies, requested=DEFAULT_CONCURRENCY, override=False):
    """Số luồng thật sự sẽ chạy: theo yêu cầu, kẹp trong khoảng 1..MAX_CONCURRENCY.

    Không đòi proxy nữa. Nhiều luồng trên cùng một IP vẫn nhanh hơn hẳn vì phần
    lớn thời gian mỗi serial là ngồi chờ 2captcha, chứ không phải gửi request.
    """
    try:
        wanted = int(requested)
    except (TypeError, ValueError):
        wanted = DEFAULT_CONCURRENCY

    if not ALLOW_PARALLEL_WITHOUT_PROXY and not proxies:
        return 1

    return max(1, min(wanted, MAX_CONCURRENCY))


async def close_context(context):
    """Đóng context, nuốt lỗi, trả về None cho gọn."""
    if context is not None:
        try:
            await context.close()
        except Exception:
            pass
    return None


async def cancel_task(task):
    """Huỷ task đang chạy và nuốt CancelledError do mình chủ động tạo ra."""
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def wait_for_serial_or_stop(coro, state):
    """Chờ serial xong, timeout, hoặc người dùng bấm Dừng."""
    task = asyncio.create_task(coro)
    started = time.monotonic()
    timeout = state["serial_timeout"]

    while True:
        if state["should_stop"]():
            state["stop"] = True
            await cancel_task(task)
            raise asyncio.CancelledError

        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            await cancel_task(task)
            raise asyncio.TimeoutError

        done, _ = await asyncio.wait({task}, timeout=min(STOP_POLL_SECONDS, remaining))
        if task in done:
            return await task


def is_page_open(page):
    """Playwright Page có is_closed(); fake page trong test cũ thì không."""
    if page is None:
        return False
    is_closed = getattr(page, "is_closed", None)
    if callable(is_closed):
        return not is_closed()
    return True


def requeue_or_give_up(state, queue, serial, on_result, give_up_label):
    """Trả serial về hàng đợi để chạy lại, quá số lần thì ghi nhận thất bại."""
    attempts = state["block_attempts"]
    attempts[serial] = attempts.get(serial, 0) + 1
    if attempts[serial] <= MAX_BLOCK_RETRIES:
        queue.put_nowait(serial)
    else:
        on_result([serial, give_up_label])


async def run_worker(worker_index, browser, queue, proxies, run_settings, state, on_result):
    """Một luồng: rút serial khỏi hàng đợi và chạy cho tới khi hết."""
    proxies = []
    label = f"W{worker_index + 1}"

    # Thả 3 luồng cùng lúc = 3 request đập vào Apple ở đúng giây 0. Lệch nhau
    # vài giây thì nhìn giống người dùng bình thường hơn nhiều.
    if worker_index:
        await asyncio.sleep(worker_index * WORKER_STAGGER_SECONDS)

    session_proxy = state["session_proxy"]
    context = None
    session_used = 0
    page = None  # Reuse page thay vì mở mới mỗi serial

    try:
        while not state["stop"]:
            if state["should_stop"]():
                state["stop"] = True
                return
            try:
                sn = queue.get_nowait()
            except asyncio.QueueEmpty:
                return

            try:
                if context is None or session_used >= SERIALS_PER_SESSION:
                    # Đóng page cũ trước khi đóng context
                    if page is not None:
                        try:
                            await page.close()
                        except Exception:
                            pass
                        page = None
                    if context is not None:
                        await context.close()
                    state["proxy_cursor"] += 1
                    proxy = None
                    context = await create_browser_context(browser, proxy)
                    session_proxy[label] = proxy
                    session_used = 0
                # Reuse page: navigate lại thay vì new_page() mỗi lần
                if not is_page_open(page):
                    page = await context.new_page()
                started_at = time.monotonic()
                # Serial nao keo dai qua muc thi bo, khong de treo ca run
                result = await wait_for_serial_or_stop(
                    check_serial(
                        page,
                        sn,
                        capture_screenshot=run_settings["capture_screenshot"],
                        folder_name=run_settings["folder_name"],
                    ),
                    state,
                )
                duration = time.monotonic() - started_at
                state["timer"].record(duration)
                state["throttle"].on_success()
                session_used += 1
                on_result(result, duration)
                # Nghỉ chống chặn chỉ có nghĩa khi còn serial để chạy tiếp.
                # Nghỉ sau serial cuối cùng là 3-8 giây vứt đi mỗi mẻ chạy.
                if not queue.empty():
                    await polite_delay()
            except ProxyFailure as error:
                requeue_or_give_up(state, queue, sn, on_result, "Proxy hỏng")
                if page is not None:
                    try:
                        await page.close()
                    except Exception:
                        pass
                    page = None
                context = await close_context(context)
                print(f"🔁 {label}: mở lại session ({error})")
                await asyncio.sleep(PROXY_FAILURE_DELAY_SECONDS)
            except BlockedError as error:
                requeue_or_give_up(state, queue, sn, on_result, "Bị chặn IP")
                if page is not None:
                    try:
                        await page.close()
                    except Exception:
                        pass
                    page = None
                context = await close_context(context)
                await state["throttle"].on_block(f"{label}: {error}")
            except asyncio.TimeoutError:
                minutes = state["serial_timeout"] / 60
                print(f"⏭️  {label}: {sn} quá {minutes:.0f} phút — bỏ qua, chạy tiếp")
                on_result([sn, "Quá giờ, bỏ qua"], state["serial_timeout"])
            except asyncio.CancelledError:
                state["stop"] = True
                return
            except CaptchaServiceError as error:
                print(f"❌ {label}: 2captcha lỗi nghiêm trọng ({error}) — dừng toàn bộ run.")
                print("   Kết quả đã chạy vẫn được giữ, lần sau chạy lại sẽ tiếp tục.")
                state["stop"] = True
            except Exception as error:
                if is_page_closed_error(error):
                    return  # dang tat, thoat im lang, khong ghi ket qua rac
                print(f"⚠️ {label}: lỗi lạ ở {sn}: {str(error).splitlines()[0][:80]}")
                on_result([sn, "Lỗi không xác định"])
    finally:
        if page is not None:
            try:
                await page.close()
            except Exception:
                pass
        if context is not None:
            try:
                await context.close()
            except Exception:
                pass


def parse_args(argv=None):
    """Doc tham so dong lenh."""
    parser = argparse.ArgumentParser(description="Check active serial Apple")
    parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Bo qua ket qua cu trong CSV, check lai toan bo serial",
    )
    parser.add_argument(
        "-c",
        "--concurrency",
        type=int,
        default=None,
        help=f"So serial chay cung luc (1..{MAX_CONCURRENCY}). Mac dinh 1.",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Chay an trinh duyet - nhanh hon nhieu khi chay nhieu luong",
    )
    parser.add_argument(
        "--serial-timeout",
        type=int,
        default=SERIAL_TIMEOUT_SECONDS,
        help="Qua bao nhieu giay thi bo qua 1 serial (mac dinh 120)",
    )
    return parser.parse_args(argv)


async def main(
    force=False,
    concurrency=None,
    args_headless=False,
    run_settings=None,
    progress_callback=None,
    should_stop=None,
    serial_timeout=None,
):
    """Chạy check tuần tự.

    run_settings / progress_callback / should_stop là để app giao diện dùng:
    truyền sẵn cấu hình (khỏi hỏi ở terminal), nhận từng kết quả về để vẽ bảng,
    và cho phép bấm Dừng giữa chừng.
    """
    if not INPUT_FILE.exists():
        print(f"❌ Không tìm thấy file: {INPUT_FILE}")
        return

    serials = load_serials(INPUT_FILE)
    done_results = {} if force else load_done_serials(OUTPUT_FILE)
    todo = [sn for sn in serials if needs_check(sn, done_results)]
    results = [[sn, done_results[sn]] for sn in serials if not needs_check(sn, done_results)]

    total_count = len(serials)
    done_count = len(results)

    # Khong con gi de chay thi thoat luon, dung hoi han gi them
    if not todo:
        print(f"✨ Cả {total_count} serial đều đã có kết quả trong {OUTPUT_FILE.name}.")
        print(f"   Muốn check lại tất cả: python3 {Path(__file__).name} --force")
        return

    if not await preflight_2captcha():
        return

    if run_settings is None:
        run_settings = prompt_run_settings()

    proxies = []
    worker_count = resolve_concurrency(
        proxies,
        requested=concurrency if concurrency is not None else DEFAULT_CONCURRENCY,
        override=concurrency is not None,
    )

    initialize_output_file(OUTPUT_FILE)
    for row in results:
        append_result_to_csv(OUTPUT_FILE, row)

    queue = asyncio.Queue()
    for sn in todo:
        queue.put_nowait(sn)

    state = {
        "stop": False,
        "throttle": BlockThrottle(),
        "block_attempts": {},
        "proxy_failures": {},
        "dead_proxies": set(),
        "session_proxy": {},
        "proxy_cursor": -1,
        "timer": RunTimer(),
        "should_stop": should_stop or (lambda: False),
        "serial_timeout": serial_timeout or SERIAL_TIMEOUT_SECONDS,
    }

    def on_result(result, duration=None):
        nonlocal done_count
        if state["stop"]:
            return
        results.append(result)
        append_result_to_csv(OUTPUT_FILE, result)
        done_count += 1
        took = f" [{duration:.0f}s]" if duration else ""
        print(
            f"  💾 Đã lưu kết quả: {result[0]} -> {result[1]}"
            f" ({format_progress(done_count, total_count)}){took}"
        )
        if progress_callback:
            progress_callback(result[0], result[1], done_count, total_count)

    async with create_stealth_playwright_context() as p:
        browser = await launch_browser(p, headless=args_headless)
        try:
            await asyncio.gather(*[
                run_worker(index, browser, queue, proxies, run_settings, state, on_result)
                for index in range(worker_count)
            ])
        finally:
            await browser.close()

    try:
        from ocr_local import summarize as summarize_ocr

        from check_active_v2 import get_local_ocr

        dong_ocr = summarize_ocr(get_local_ocr())
        if dong_ocr:
            print(dong_ocr)
    except Exception:
        pass

    print("\n" + state["timer"].summary())
    measured = state["timer"].average_concurrency()
    if worker_count > 1 and measured < worker_count * 0.6:
        print(f"   ⚠️ Đặt {worker_count} luồng nhưng đo được {measured:.1f} — đang bị nghẽn ở đâu đó.")

    try:
        inactive_excel_path = export_inactive_to_excel(
            results,
            folder_name=run_settings["folder_name"],
        )
        print(f"📄 Đã tạo file Excel máy chưa active: {inactive_excel_path}")
    except Exception as error:
        print(f"⚠️ Không xuất được Excel: {error}")

    print(f"✨ Xong! Đã lưu kết quả vào: {OUTPUT_FILE}")


if __name__ == "__main__":
    _args = parse_args()
    asyncio.run(main(
        force=_args.force,
        concurrency=_args.concurrency,
        args_headless=_args.headless,
        serial_timeout=_args.serial_timeout,
    ))
