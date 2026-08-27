# Check Active — hướng dẫn chạy

> Phiên bản hiện tại nằm trong `version.py`. Tag trên GitHub là `v` + số đó.

## 0. Dùng app (dễ nhất)

Double-click **Check Active.app**.

Giao diện PySide6 gồm:

- ô dán serial + nút **▶ Chạy check** ở cột trái
- dải số liệu: đã xong / đã active / chưa active / lỗi / serial mỗi phút / còn lại
- bảng kết quả có nhãn trạng thái màu, sắp xếp và lọc được, chuột phải để copy serial
- khung log đen ở dưới, tô màu theo mức, có ô **Chỉ cảnh báo và lỗi**
- nút **Turbo** ngay trên thanh trên cùng
- mọi cài đặt nằm trong nút **Cài đặt**

Lần đầu mở phải dán **key 2captcha** vào Cài đặt rồi bấm **Lưu**.
Double-click từ Finder không đọc `~/.zshrc` nên biến môi trường không có tác dụng,
key phải lưu trong app.

### Cài lần đầu trên một máy

Double-click **`CAI_DAT.command`**. Nó tự dựng venv, cài thư viện, tải trình
duyệt cho Playwright và gỡ cờ quarantine của macOS. Chạy một lần là xong.

Bản phát hành dùng giao diện PySide6. Không dùng Python 3.14 cho bản này vì
dễ crash Python/QThread trên macOS; nếu máy chỉ có Python 3.14 thì cài Python
3.13 rồi chạy lại `CAI_DAT.command`.

**`Check Active.app` phải luôn nằm cạnh các file `.py`.** Launcher tự suy ra
thư mục project từ vị trí của chính nó, nên chép cả thư mục đi đâu cũng chạy;
nhưng kéo riêng file `.app` sang chỗ khác thì hỏng.

## 1. Đặt key 2captcha (BẮT BUỘC)

Key không nằm trong code. Chạy từ dòng lệnh thì:

```bash
export TWOCAPTCHA_API_KEY="key_moi_cua_ban"
```

Muốn khỏi gõ lại mỗi lần thì thêm dòng trên vào `~/.zshrc`.
Chạy bằng app thì key lấy từ ô Cài đặt, không cần biến môi trường.

Thiếu key thì script dừng ngay và báo rõ, thay vì chạy hết 500 serial rồi
trả về toàn "Check tay".

## 2. Số luồng

App chạy được **1 đến 3 serial cùng lúc**, chọn trong Cài đặt. Mặc định 2.

Đây là đánh đổi thật, không có bữa trưa miễn phí: **N luồng = gửi request tới
Apple nhiều gấp N lần**. Nới thời gian nghỉ của từng luồng không cứu được điều
đó, chỉ làm mất luôn phần tăng tốc.

Cách dùng an toàn: chạy thử 50 serial ở mức 2, soi log xem có dòng
`🛑 Bị chặn` không. Sạch thì mới lên 3. Bị chặn thì hạ về 1 và nghỉ một lúc —
app có sẵn bộ giảm tốc luỹ thừa (60s → 120s → … → 900s).

File `proxies.txt` hiện bị bỏ qua hoàn toàn.

## 3. Chạy từ dòng lệnh

> **Chạy bằng Python nào?** Máy đang có cả conda (`base`) lẫn venv, gõ `python3`
> trơn sẽ trúng conda và báo `No module named 'playwright'`.
> Luôn gọi thẳng venv: `./venv/bin/python ...`

```bash
./venv/bin/python check_active_parallel.py              # 1 luồng
./venv/bin/python check_active_parallel.py -c 2         # 2 luồng
./venv/bin/python check_active_parallel.py --headless   # ẩn trình duyệt
./venv/bin/python check_active_parallel.py --force      # check lại cả serial đã xong
./venv/bin/python check_active_v2.py                    # bản tuần tự cũ
```

Chạy xong nó in ra dòng đo tốc độ:

```
⏱  18 serial trong 6.2 phút (2.9 serial/phút) — song song thực tế: 2.0 luồng
```

**"song song thực tế"** = tổng thời gian xử lý chia cho thời gian đồng hồ.
Đặt 2 luồng mà đo ra ~1.0 nghĩa là đang nghẽn ở đâu đó — app sẽ tự cảnh báo.

## 4. Chạy tiếp khi bị đứt giữa chừng

Cả 2 script tự đọc lại file CSV kết quả cũ và **chỉ chạy lại những serial
bị lỗi** (`Check tay`, `Lỗi load trang`, `Bị chặn IP`, ...). Serial nào đã ra
ngày mua hoặc "Chưa active" thì bỏ qua.

Nếu tất cả serial đều đã có kết quả, script thoát ngay và không hỏi gì.
Muốn check lại toàn bộ: thêm `--force`, hoặc xoá file CSV kết quả đi.

## 5. Hỏi nhập captcha tay

**Mặc định TẮT.** Bật lên thì khi AI đọc trượt 2 lần, app hiện ảnh captcha và
chờ bạn gõ tối đa **12 giây**, mỗi serial chỉ hỏi **đúng một lần**.

Chỉ nên bật khi bạn đang ngồi trước máy. Chạy mẻ lớn rồi đi làm việc khác mà
để bật thì mỗi serial khó sẽ đứng yên chờ hết 12 giây một cách vô ích.

## 6. Tinh chỉnh (biến môi trường)

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `TWOCAPTCHA_API_KEY` | — | Key 2captcha, bắt buộc |
| `SERIALS_PER_SESSION` | 15 | Bao nhiêu serial thì mở session mới |
| `REQUEST_DELAY_MIN` | 3 | Nghỉ tối thiểu giữa 2 serial (giây) |
| `REQUEST_DELAY_MAX` | 8 | Nghỉ tối đa giữa 2 serial (giây) |
| `SERIAL_TIMEOUT` | 120 | Quá bao nhiêu giây thì bỏ qua 1 serial |
| `MANUAL_CAPTCHA_AFTER` | 2 | Trượt mấy lần thì hỏi tay |
| `MANUAL_CAPTCHA_MAX_ASKS` | 1 | Mỗi serial được hỏi tay tối đa mấy lần |
| `BLOCK_ASSETS` | `1` trong app | `1` = chặn ảnh/font/video |

## 7. Chạy test

```bash
./venv/bin/python -m unittest \
    test_check_active_v2 test_check_active_parallel \
    test_app_gui test_app_qt_core test_app_update
```

218 test, chạy hết dưới 1 giây, không cần mạng và không tốn tiền captcha.

## 8. Đóng gói và cập nhật

### Ra một bản mới

1. Double-click **`DONG_GOI_DMG.command`**. Nó hỏi số phiên bản mới, gợi ý
   sẵn số tiếp theo (Enter là lấy luôn), rồi **tự ghi vào `version.py`** và
   `Info.plist`. Ra `dist/CheckActive-1.2.0.dmg`.
2. Double-click **`PUSH_GITHUB.command`** → đẩy code lên
3. Tạo release trên GitHub, **tag phải đúng `v1.2.0`**, đính kèm file `.dmg`:

```bash
gh release create v1.2.0 dist/CheckActive-1.2.0.dmg \
   --title "v1.2.0" --notes "Có gì mới..."
```

Thiếu bước 3, hoặc release không đính kèm `.dmg`, thì các máy khác sẽ không
thấy gì cả.

Số phiên bản: thêm tính năng thì tăng số giữa (`1.1.0` → `1.2.0`), chỉ sửa lỗi
thì tăng số cuối (`1.1.0` → `1.1.1`). Script chặn không cho lùi về số cũ hoặc
giữ nguyên số — vì máy khác sẽ không nhận ra đó là bản mới.

### App tự báo có bản mới thế nào

Mở app xong 1,5 giây, nó gọi `GET /repos/OWNER/REPO/releases/latest`, so số
phiên bản với `version.py`. Mới hơn thì hiện một dải xanh ở đầu cửa sổ:
**Có bản 1.2.0 — bạn đang dùng 1.1.0**, kèm nút *Có gì mới* và *Tải bản mới*.
Bấm tải thì file `.dmg` về thẳng Downloads và Finder tự mở tới nó. Cài đè là
chạy `CAI_DAT.command` trong bản mới.

App **không tự thay chính nó** — cố ý như vậy. Tự ghi đè một app đang chạy là
chỗ rất dễ hỏng, và mỗi bản có khi còn cần cài thêm thư viện mới.

### Token GitHub

Repo để private nên app cần token mới đọc được release. Vào GitHub →
Settings → Developer settings → **Fine-grained tokens**, cấp đúng một quyền
**Contents: Read-only** cho đúng repo này. Dán vào ⚙ Cài đặt.

Token nằm trong `app_settings.json`, và file đó **đã bị `.gitignore` chặn**
cùng với `proxies.txt` — `PUSH_GITHUB.command` còn kiểm tra lại lần nữa và
dừng hẳn nếu thấy chúng lọt vào git.

Không điền token thì app vẫn chạy bình thường, chỉ là không tự báo bản mới.

## 9. Có gì trong thư mục

| File | Việc |
|---|---|
| `check_active_v2.py` | Toàn bộ phần cào: mở trang, giải captcha, đọc kết quả |
| `check_active_parallel.py` | Bộ điều phối nhiều luồng, hàng đợi, chống chặn |
| `app_core.py` | Lõi giao diện dùng chung |
| `app_qt.py` | Giao diện PySide6 |
| `app_qt_core.py` | Phần tính toán của giao diện Qt, test được không cần Qt |
| `app_gui.py` | Giao diện Tkinter dự phòng trong source |
| `app_settings.py` / `.json` | Lưu và nạp cài đặt |
| `app_update.py` | Hỏi GitHub Releases xem có bản mới không |
| `version.py` | Số phiên bản — script đóng gói đọc và ghi file này |
| `CAI_DAT.command` | Cài lần đầu trên một máy mới |
| `DONG_GOI_DMG.command` | Đóng gói thành `.dmg` |
| `PUSH_GITHUB.command` | Đẩy code lên GitHub |
| `icon/` | Ba phương án icon + script vẽ |
| `backup-truoc-toi-uu-260826/` | Bản code trước đợt tối ưu 26/08/2026 |
