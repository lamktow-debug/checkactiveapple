"""App giao diện cho Check Active.

Gồm đúng 4 thứ: chỗ dán serial, nút chạy, màn hình xem nó chạy, và bảng
kết quả serial -> ngày mua.

Chạy: ./venv/bin/python app_gui.py
Hoặc double-click "Check Active.app"
"""

import asyncio
import contextlib
import queue
import subprocess
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from app_core import (
    MANUAL_CAPTCHA_WAIT_SECONDS,
    RESULT_FILE,
    SERIALS_FILE,
    QueueWriter,
    parse_serials,
    read_results,
    row_tag,
)
from app_settings import apply_settings, load_settings, save_settings

LOG_LIMIT_LINES = 2000
POLL_INTERVAL_MS = 120


class CheckActiveApp:
    def __init__(self, root):
        self.root = root
        self.events = queue.Queue()
        self.stop_flag = threading.Event()
        self.worker = None
        self.settings = load_settings()

        # Nhap captcha tay: thread scraper cho o day, giao dien tra loi vao day
        self.manual_event = threading.Event()
        self.manual_answer = None
        self.manual_window = None

        root.title("Check Active — ONEWAY")
        root.geometry("1180x780")
        root.minsize(940, 620)

        self._build_settings_bar()
        self._build_body()
        self._build_status_bar()

        self._load_existing_serials()
        self._fill_table(read_results())
        self.root.after(POLL_INTERVAL_MS, self._drain_events)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- dựng giao diện ----------

    def _build_settings_bar(self):
        bar = ttk.LabelFrame(self.root, text="Cài đặt", padding=8)
        bar.pack(fill="x", padx=10, pady=(10, 6))

        ttk.Label(bar, text="Key 2captcha:").grid(row=0, column=0, sticky="w")
        self.key_var = tk.StringVar(value=self.settings["twocaptcha_key"])
        self.key_entry = ttk.Entry(bar, textvariable=self.key_var, width=38, show="•")
        self.key_entry.grid(row=0, column=1, padx=(6, 4))

        self.show_key = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Hiện", variable=self.show_key,
                        command=self._toggle_key).grid(row=0, column=2, padx=(0, 14))

        self.headless_var = tk.BooleanVar(value=self.settings["headless"])
        ttk.Checkbutton(bar, text="Ẩn trình duyệt", variable=self.headless_var).grid(row=0, column=3, padx=(0, 12))

        self.block_assets_var = tk.BooleanVar(value=self.settings["block_assets"])
        ttk.Checkbutton(bar, text="Chặn ảnh",
                        variable=self.block_assets_var).grid(row=0, column=4, padx=(0, 12))

        self.capture_screenshot_var = tk.BooleanVar(value=self.settings["capture_screenshot"])
        ttk.Checkbutton(bar, text="Chụp màn hình kết quả",
                        variable=self.capture_screenshot_var).grid(row=0, column=5, padx=(0, 12))

        self.turbo_var = tk.BooleanVar(value=self.settings["turbo_mode"])
        ttk.Checkbutton(bar, text="Turbo",
                        variable=self.turbo_var).grid(row=0, column=6, padx=(0, 12))

        ttk.Label(bar, text="Bỏ qua sau (giây):").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.timeout_var = tk.IntVar(value=self.settings["serial_timeout"])
        ttk.Spinbox(bar, from_=30, to=600, increment=30, width=6,
                    textvariable=self.timeout_var).grid(row=1, column=1, sticky="w", padx=(6, 4), pady=(6, 0))

        self.manual_var = tk.BooleanVar(value=self.settings["manual_captcha"])
        ttk.Checkbutton(bar, text="Hỏi nhập captcha tay khi OCR trượt",
                        variable=self.manual_var).grid(row=1, column=3, columnspan=2, sticky="w", pady=(6, 0))

        ttk.Button(bar, text="Lưu cài đặt", command=self._save_settings).grid(row=0, column=7, rowspan=2, padx=(8, 0))

    def _build_body(self):
        body = ttk.Frame(self.root)
        body.pack(fill="both", expand=True, padx=10, pady=4)

        # --- trái: dán serial + nút ---
        left = ttk.Frame(body)
        left.pack(side="left", fill="y", padx=(0, 10))

        ttk.Label(left, text="Dán serial vào đây (mỗi dòng 1 cái)").pack(anchor="w")
        self.serial_text = tk.Text(left, width=26, height=22, font=("SF Mono", 12), undo=True)
        self.serial_text.pack(fill="y", expand=True, pady=(4, 4))
        self.serial_text.bind("<<Modified>>", self._on_serials_changed)

        self.serial_count = ttk.Label(left, text="0 serial")
        self.serial_count.pack(anchor="w", pady=(0, 6))

        self.run_button = ttk.Button(left, text="▶  Chạy check", command=self._start_run)
        self.run_button.pack(fill="x", pady=2)

        self.stop_button = ttk.Button(left, text="■  Dừng", command=self._request_stop, state="disabled")
        self.stop_button.pack(fill="x", pady=2)

        ttk.Separator(left).pack(fill="x", pady=8)

        ttk.Button(left, text="📂  Mở file kết quả", command=self._open_results).pack(fill="x", pady=2)

        # --- phải: bảng kết quả + nhật ký ---
        right = ttk.PanedWindow(body, orient="vertical")
        right.pack(side="left", fill="both", expand=True)

        table_frame = ttk.LabelFrame(right, text="Kết quả", padding=6)
        columns = ("serial", "value")
        self.table = ttk.Treeview(table_frame, columns=columns, show="headings", height=12)
        self.table.heading("serial", text="Serial")
        self.table.heading("value", text="Ngày mua / Trạng thái")
        self.table.column("serial", width=180, anchor="w")
        self.table.column("value", width=220, anchor="w")
        self.table.tag_configure("ok", foreground="#0a7d28")
        self.table.tag_configure("bad", foreground="#c0392b")
        self.table.tag_configure("wait", foreground="#7a7a7a")
        table_scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=table_scroll.set)
        self.table.pack(side="left", fill="both", expand=True)
        table_scroll.pack(side="right", fill="y")
        right.add(table_frame, weight=3)

        log_frame = ttk.LabelFrame(right, text="Đang chạy", padding=6)
        self.log_text = tk.Text(log_frame, height=12, font=("SF Mono", 11),
                                background="#1e1e1e", foreground="#e6e6e6",
                                insertbackground="#e6e6e6", state="disabled", wrap="none")
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        log_scroll.pack(side="right", fill="y")
        right.add(log_frame, weight=2)

    def _build_status_bar(self):
        bar = ttk.Frame(self.root)
        bar.pack(fill="x", padx=10, pady=(2, 10))
        self.status_var = tk.StringVar(value="Sẵn sàng")
        ttk.Label(bar, textvariable=self.status_var).pack(side="left")
        self.progress = ttk.Progressbar(bar, mode="determinate", length=260)
        self.progress.pack(side="right")

    # ---------- việc vặt của giao diện ----------

    def _toggle_key(self):
        self.key_entry.configure(show="" if self.show_key.get() else "•")

    def _on_serials_changed(self, _event=None):
        self.serial_text.edit_modified(False)
        count = len(parse_serials(self.serial_text.get("1.0", "end")))
        self.serial_count.configure(text=f"{count} serial")

    def _load_existing_serials(self):
        if SERIALS_FILE.exists():
            try:
                self.serial_text.insert("1.0", SERIALS_FILE.read_text(encoding="utf-8-sig"))
            except OSError:
                pass
        self._on_serials_changed()

    def _current_settings(self):
        return {
            "twocaptcha_key": self.key_var.get().strip(),
            "concurrency": int(self.settings.get("concurrency") or 1),
            "headless": bool(self.headless_var.get()),
            "block_assets": bool(self.block_assets_var.get()),
            "capture_screenshot": bool(self.capture_screenshot_var.get()),
            "serials_per_session": self.settings.get("serials_per_session", 15),
            "serial_timeout": int(self.timeout_var.get()),
            "manual_captcha": bool(self.manual_var.get()),
            "turbo_mode": bool(self.turbo_var.get()),
        }

    def _save_settings(self):
        self.settings = save_settings(self._current_settings())
        self._log("💾 Đã lưu cài đặt")

    def _log(self, line):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line + "\n")
        line_count = int(self.log_text.index("end-1c").split(".")[0])
        if line_count > LOG_LIMIT_LINES:
            self.log_text.delete("1.0", f"{line_count - LOG_LIMIT_LINES}.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _fill_table(self, rows):
        self.table.delete(*self.table.get_children())
        for serial, value in rows:
            self._upsert_row(serial, value)

    def _upsert_row(self, serial, value):
        tag = row_tag(value)
        if self.table.exists(serial):
            self.table.item(serial, values=(serial, value), tags=(tag,))
        else:
            self.table.insert("", "end", iid=serial, values=(serial, value), tags=(tag,))
        self.table.see(serial)

    def _open_results(self):
        if not RESULT_FILE.exists():
            messagebox.showinfo("Chưa có kết quả", "Chạy check xong rồi mở lại nhé.")
            return
        subprocess.run(["open", "-R", str(RESULT_FILE)], check=False)

    # ---------- chạy scraper ----------

    def _busy(self, busy):
        self.run_button.configure(state="disabled" if busy else "normal")
        self.stop_button.configure(state="normal" if busy else "disabled")

    def _start_run(self):
        serials = parse_serials(self.serial_text.get("1.0", "end"))
        if not serials:
            messagebox.showwarning("Chưa có serial", "Dán serial vào ô bên trái đã.")
            return

        settings = self._current_settings()
        if not settings["twocaptcha_key"]:
            messagebox.showwarning("Thiếu key", "Điền key 2captcha ở phần Cài đặt.")
            return

        self.settings = save_settings(settings)
        SERIALS_FILE.write_text("\n".join(serials) + "\n", encoding="utf-8")

        self._fill_table([(serial, "đang chờ...") for serial in serials])
        self.progress.configure(maximum=len(serials), value=0)
        self.status_var.set(f"Đang chạy 0/{len(serials)}")
        self.stop_flag.clear()
        self._busy(True)
        self._log(f"\n=== Bắt đầu check {len(serials)} serial ===")

        self.worker = threading.Thread(target=self._run_scraper, args=(settings,), daemon=True)
        self.worker.start()

    def _run_scraper(self, settings):
        writer = QueueWriter(self.events)
        try:
            scraper = apply_settings(settings)
            import check_active_parallel as runner

            scraper.MANUAL_CAPTCHA_HANDLER = (
                self._ask_manual_captcha if settings["manual_captcha"] else None
            )

            def on_progress(serial, value, done, total):
                self.events.put(("result", serial, value, done, total))

            with contextlib.redirect_stdout(writer):
                asyncio.run(
                    runner.main(
                        force=True,
                        concurrency=settings.get("concurrency", 1),
                        args_headless=settings["headless"],
                        run_settings={
                            "capture_screenshot": settings["capture_screenshot"],
                            "folder_name": None,
                        },
                        progress_callback=on_progress,
                        should_stop=self.stop_flag.is_set,
                        serial_timeout=settings["serial_timeout"],
                    )
                )
        except Exception as error:
            self.events.put(("log", f"❌ Lỗi: {type(error).__name__}: {error}"))
        finally:
            writer.flush()
            self.events.put(("finished", None))

    async def _ask_manual_captcha(self, image_base64, serial):
        """Scraper gọi hàm này khi OCR trượt mãi. Chờ người dùng gõ mã."""
        self.manual_answer = None
        self.manual_event.clear()
        self.events.put(("captcha", image_base64, serial))
        answered = await asyncio.to_thread(self.manual_event.wait, MANUAL_CAPTCHA_WAIT_SECONDS)
        self.events.put(("captcha_done", None))
        return self.manual_answer if answered else None

    def _show_captcha_window(self, image_base64, serial):
        """Hiện ảnh captcha cho người dùng nhìn và gõ."""
        self._close_captcha_window()

        window = tk.Toplevel(self.root)
        window.title(f"Nhập captcha — {serial}")
        window.attributes("-topmost", True)
        window.resizable(False, False)
        self.manual_window = window

        ttk.Label(window, text=f"Serial: {serial}", font=("SF Pro", 13, "bold")).pack(pady=(12, 4))

        try:
            photo = tk.PhotoImage(data=image_base64)
            holder = ttk.Label(window, image=photo)
            holder.image = photo  # giu tham chieu keo bi thu gom rac
            holder.pack(padx=20, pady=6)
        except tk.TclError:
            ttk.Label(window, text="(không hiện được ảnh — nhìn cửa sổ trình duyệt)",
                      foreground="#c0392b").pack(padx=20, pady=6)

        entry_var = tk.StringVar()
        entry = ttk.Entry(window, textvariable=entry_var, width=16,
                          font=("SF Mono", 18), justify="center")
        entry.pack(pady=6)
        entry.focus_set()

        countdown = ttk.Label(window, text="", foreground="#7a7a7a")
        countdown.pack()

        def submit(_event=None):
            self.manual_answer = entry_var.get().strip()
            self.manual_event.set()
            self._close_captcha_window()

        def skip():
            self.manual_answer = None
            self.manual_event.set()
            self._close_captcha_window()

        entry.bind("<Return>", submit)
        buttons = ttk.Frame(window)
        buttons.pack(pady=(4, 14))
        ttk.Button(buttons, text="Gửi", command=submit).pack(side="left", padx=4)
        ttk.Button(buttons, text="Bỏ qua, để AI làm tiếp", command=skip).pack(side="left", padx=4)
        window.protocol("WM_DELETE_WINDOW", skip)

        def tick(remaining):
            if window is not self.manual_window:
                return
            countdown.configure(text=f"Tự bỏ qua sau {remaining}s")
            if remaining <= 0:
                skip()
            else:
                window.after(1000, tick, remaining - 1)

        tick(MANUAL_CAPTCHA_WAIT_SECONDS)

    def _close_captcha_window(self):
        if self.manual_window is not None:
            try:
                self.manual_window.destroy()
            except tk.TclError:
                pass
            self.manual_window = None

    def _request_stop(self):
        if self.stop_flag.is_set():
            return
        self.stop_flag.set()
        self.stop_button.configure(state="disabled")
        self.status_var.set("Đang dừng...")
        self._log("■ Đang dừng...")

    # ---------- nhận tin từ thread ----------

    def _drain_events(self):
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "log":
                    self._log(event[1])
                elif kind == "result":
                    _, serial, value, done, total = event
                    self._upsert_row(serial, value)
                    self.progress.configure(maximum=max(total, 1), value=done)
                    self.status_var.set(f"Đang chạy {done}/{total}")
                elif kind == "captcha":
                    self._show_captcha_window(event[1], event[2])
                elif kind == "captcha_done":
                    self._close_captcha_window()
                elif kind == "finished":
                    self._close_captcha_window()
                    self._busy(False)
                    self.status_var.set("Xong")
                    self.progress.configure(value=self.progress["maximum"])
        except queue.Empty:
            pass
        self.root.after(POLL_INTERVAL_MS, self._drain_events)

    def _on_close(self):
        if self.worker and self.worker.is_alive():
            if not messagebox.askokcancel("Đang chạy", "Vẫn đang check. Thoát luôn?"):
                return
            self.stop_flag.set()
        self.root.destroy()


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("aqua")
    except tk.TclError:
        pass
    CheckActiveApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
