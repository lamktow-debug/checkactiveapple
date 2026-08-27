"""Đóng gói PNG 1024 thành file .icns cho macOS.

`iconutil` chỉ có trên macOS nên tự ghi lấy — định dạng icns rất đơn giản:
'icns' + tổng độ dài, rồi từng khối: mã 4 ký tự + độ dài + dữ liệu PNG.
"""

import struct
import sys
from io import BytesIO
from pathlib import Path

from PIL import Image

# Ma khoi -> canh anh. Cac cap @2x dung chung kich thuoc pixel voi ban thuong.
ENTRIES = [
    ("icp4", 16),    # 16x16
    ("icp5", 32),    # 32x32
    ("ic11", 32),    # 16x16@2x
    ("ic12", 64),    # 32x32@2x
    ("ic07", 128),   # 128x128
    ("ic13", 256),   # 128x128@2x
    ("ic08", 256),   # 256x256
    ("ic14", 512),   # 256x256@2x
    ("ic09", 512),   # 512x512
    ("ic10", 1024),  # 512x512@2x
]


def png_bytes(source, side):
    resized = source.resize((side, side), Image.LANCZOS)
    buffer = BytesIO()
    resized.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def build(source_png, target_icns):
    source = Image.open(source_png).convert("RGBA")
    if source.width != source.height:
        raise SystemExit("Ảnh nguồn phải vuông")

    blocks = b""
    for code, side in ENTRIES:
        data = png_bytes(source, side)
        blocks += code.encode("ascii") + struct.pack(">I", len(data) + 8) + data

    payload = b"icns" + struct.pack(">I", len(blocks) + 8) + blocks
    Path(target_icns).write_bytes(payload)
    return len(payload)


def verify(path):
    """Đọc lại file vừa ghi, kiểm tra từng khối mở ra được đúng kích thước."""
    raw = Path(path).read_bytes()
    assert raw[:4] == b"icns", "sai magic"
    total = struct.unpack(">I", raw[4:8])[0]
    assert total == len(raw), f"độ dài ghi {total} khác thực tế {len(raw)}"

    offset, found = 8, []
    while offset < len(raw):
        code = raw[offset:offset + 4].decode("ascii")
        length = struct.unpack(">I", raw[offset + 4:offset + 8])[0]
        data = raw[offset + 8:offset + length]
        image = Image.open(BytesIO(data))
        found.append((code, image.size[0]))
        offset += length

    expected = [(code, side) for code, side in ENTRIES]
    assert found == expected, f"khối lệch: {found}"
    return found


if __name__ == "__main__":
    source, target = sys.argv[1], sys.argv[2]
    size = build(source, target)
    blocks = verify(target)
    print(f"{target}: {size:,} byte, {len(blocks)} khối")
    print("  " + ", ".join(f"{code}={side}" for code, side in blocks))
