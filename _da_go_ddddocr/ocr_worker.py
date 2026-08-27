"""Tiến trình con giải captcha bằng ddddocr — chạy bằng venv-ocr, KHÔNG phải venv chính.

Vì sao tách tiến trình: ddddocr chỉ hỗ trợ Python 3.10–3.13, còn venv chính của
app đang là 3.14 (PySide6 chạy tốt ở đó, không nên đập đi làm lại). Tách ra thì
mỗi bên dùng đúng bản Python của mình. Kèm theo: model 76MB nạp một lần rồi nằm
sẵn, và OCR có chết cũng không kéo app chết theo.

Captcha Apple luôn 4 ký tự. Model hay nuốt mất một ký tự khi nét dính nhau hoặc
nền nhiễu, trả về 3 ký tự — mà 3 ký tự thì chắc chắn sai. Nên ở đây thử NHIỀU
cách xử lý ảnh và CẢ HAI model của ddddocr, lấy kết quả đầu tiên đúng độ dài.
Mỗi lần đoán chỉ ~0.15s, thử 6-8 lần vẫn nhanh hơn 2captcha (~9s) rất nhiều.

Giao thức: mỗi dòng vào stdin là một JSON {"image": "<base64>", "length": 4},
mỗi dòng ra stdout là {"code": "...", "candidates": [...], "matched": true}.
Nhận cả dòng base64 trần cho tương thích ngược.
"""

import base64
import io
import json
import re
import sys
import time

READY = '{"ready": true}'
KY_TU_HOP_LE = re.compile(r"[^A-Za-z0-9]")


def chuan_hoa(code):
    return KY_TU_HOP_LE.sub("", code or "").upper()


def _pil():
    """Nạp Pillow khi cần. Không có thì bỏ qua phần xử lý ảnh."""
    try:
        from PIL import Image, ImageFilter, ImageOps

        return Image, ImageOps, ImageFilter
    except ImportError:
        return None, None, None


def cac_bien_the_anh(raw, scale=3):
    """Sinh vài bản xử lý khác nhau của cùng một ảnh captcha.

    Nét dính nhau và nền nhiễu là hai lý do model nuốt mất ký tự. Phóng to,
    tăng tương phản và nhị phân hoá đều giúp tách nét ra. Trả về danh sách
    (tên, bytes) — bản gốc luôn đứng đầu để trường hợp dễ thì xong ngay.
    """
    bien_the = [("goc", raw)]

    Image, ImageOps, ImageFilter = _pil()
    if Image is None:
        return bien_the

    try:
        goc = Image.open(io.BytesIO(raw))
        goc.load()
        goc = goc.convert("L")
    except Exception:
        return bien_the

    rong, cao = goc.size
    to_hon = goc.resize((rong * scale, cao * scale), Image.LANCZOS)

    def dong_goi(ten, anh):
        try:
            # Vien trang quanh anh: CTC hay nuot ky tu sat mep
            anh = ImageOps.expand(anh.convert("L"), border=12, fill=255)
            buffer = io.BytesIO()
            anh.convert("RGB").save(buffer, format="PNG")
            bien_the.append((ten, buffer.getvalue()))
        except Exception:
            pass

    dong_goi("phong-to", to_hon)

    try:
        dong_goi("tuong-phan", ImageOps.autocontrast(to_hon, cutoff=2))
    except Exception:
        pass

    try:
        # Nhi phan hoa: bo han nen nhieu, chi con net chu
        muc = ImageOps.autocontrast(to_hon, cutoff=5)
        dong_goi("den-trang", muc.point(lambda x: 0 if x < 140 else 255, mode="L"))
    except Exception:
        pass

    try:
        # Lam min truoc roi moi nhi phan: bo cac cham nhieu le te
        min_hon = to_hon.filter(ImageFilter.MedianFilter(size=3))
        dong_goi("loc-nhieu",
                 min_hon.point(lambda x: 0 if x < 150 else 255, mode="L"))
    except Exception:
        pass

    return bien_the


def nap_cac_model():
    """Nạp cả model thường và model beta của ddddocr. Hai model đọc khác nhau."""
    import ddddocr

    models = []
    for beta in (False, True):
        try:
            try:
                models.append(ddddocr.DdddOcr(beta=beta, show_ad=False))
            except TypeError:
                models.append(ddddocr.DdddOcr(beta=beta))
        except Exception:
            continue
    if not models:
        # Khong nap duoc theo kieu co tham so thi thu kieu don gian nhat
        models.append(ddddocr.DdddOcr())
    return models


def doan(models, raw, do_dai_mong_muon, budget=0):
    """Thử từng biến thể ảnh với từng model, lấy cái đầu tiên đúng độ dài.

    budget: số giây tối đa được phép quét. Hết giờ thì dừng, để bên gọi mua mã
    2captcha cho nhanh — quét lâu mà vẫn trượt là kiểu lãng phí tệ nhất.
    """
    ung_vien = []
    bat_dau = time.monotonic()
    for ten, anh in cac_bien_the_anh(raw):
        if budget and time.monotonic() - bat_dau > budget:
            break
        for chi_so, model in enumerate(models):
            try:
                code = chuan_hoa(model.classification(anh))
            except Exception:
                continue
            if not code:
                continue
            ung_vien.append(f"{ten}/m{chi_so}:{code}")
            if not do_dai_mong_muon or len(code) == do_dai_mong_muon:
                return code, ung_vien, True

    # Khong co cai nao dung do dai. Tra ve cai dai nhat de con biet duong,
    # nhung bao matched=False de ben goi tu quyet dinh co dung hay khong.
    tot_nhat = ""
    for muc in ung_vien:
        code = muc.split(":", 1)[1]
        if len(code) > len(tot_nhat):
            tot_nhat = code
    return tot_nhat, ung_vien, False


def doc_yeu_cau(line):
    """Nhận JSON, hoặc base64 trần (tương thích bản cũ)."""
    try:
        payload = json.loads(line)
        if isinstance(payload, dict):
            return (payload.get("image") or "",
                    payload.get("length") or 0,
                    payload.get("budget") or 0)
    except ValueError:
        pass
    return line, 0, 0


def main():
    try:
        import ddddocr  # noqa: F401
    except ImportError as error:
        print(json.dumps({"error": f"chua cai ddddocr: {error}"}), flush=True)
        return 1

    try:
        models = nap_cac_model()
    except Exception as error:
        print(json.dumps({"error": f"khong nap duoc model: {error}"}), flush=True)
        return 1

    print(READY, flush=True)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        if line == "PING":
            print(json.dumps({"code": "PONG", "models": len(models)}), flush=True)
            continue
        try:
            anh_b64, do_dai, budget = doc_yeu_cau(line)
            raw = base64.b64decode(anh_b64)
            bat_dau = time.monotonic()
            code, ung_vien, khop = doan(models, raw, do_dai, budget)
            print(json.dumps({
                "code": code,
                "candidates": ung_vien,
                "matched": khop,
                "seconds": round(time.monotonic() - bat_dau, 2),
            }), flush=True)
        except Exception as error:
            print(json.dumps({"error": str(error)[:200]}), flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
