"""Vẽ 3 phương án icon cho Check Active.

Vẽ ở 4x rồi thu nhỏ để có viền mượt. Hình nền là superellipse (squircle) đúng
kiểu macOS chứ không phải rounded-rect thường.
"""

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

OUT = Path(__file__).resolve().parent
SS = 4                      # he so ve qua kho roi thu nho
SIZE = 1024

# Bang mau lay tu chinh giao dien app
TEAL_HI = (58, 214, 184)
TEAL_LO = (11, 92, 82)
INK_HI = (35, 48, 62)
INK_LO = (12, 17, 23)
AMBER = (232, 169, 78)
WHITE = (255, 255, 255)


def squircle(size, inset, n=5.0, steps=1400):
    """Đường superellipse — góc bo kiểu Apple, không phải cung tròn."""
    a = (size - 2 * inset) / 2
    cx = cy = size / 2
    points = []
    for i in range(steps):
        t = 2 * math.pi * i / steps
        ct, st = math.cos(t), math.sin(t)
        x = cx + a * math.copysign(abs(ct) ** (2 / n), ct)
        y = cy + a * math.copysign(abs(st) ** (2 / n), st)
        points.append((x, y))
    return points


def vertical_gradient(size, top, bottom):
    grad = Image.new("RGB", (1, size))
    for y in range(size):
        t = y / max(size - 1, 1)
        grad.putpixel((0, y), tuple(
            round(top[i] + (bottom[i] - top[i]) * t) for i in range(3)
        ))
    return grad.resize((size, size), Image.BICUBIC)


def base_plate(top, bottom, inset_ratio=0.09):
    """Nền squircle có gradient + gờ sáng mỏng ở mép trên."""
    size = SIZE * SS
    inset = int(size * inset_ratio)

    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).polygon(squircle(size, inset), fill=255)

    plate = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    plate.paste(vertical_gradient(size, top, bottom), (0, 0), mask)

    # Gờ sáng: viền squircle nhỏ hơn 1 chút, chỉ giữ nửa trên
    rim = Image.new("L", (size, size), 0)
    ImageDraw.Draw(rim).line(
        squircle(size, inset + int(size * 0.004)) + [squircle(size, inset)[0]],
        fill=90, width=int(size * 0.008), joint="curve",
    )
    fade = Image.new("L", (size, size), 0)
    ImageDraw.Draw(fade).rectangle([0, 0, size, size // 2], fill=255)
    fade = fade.filter(ImageFilter.GaussianBlur(size * 0.06))
    rim = Image.composite(rim, Image.new("L", (size, size), 0), fade)
    plate.paste(Image.new("RGBA", (size, size), WHITE + (255,)), (0, 0), rim)

    return plate, mask, inset


def check_path(size, cx, cy, scale):
    """Ba điểm của dấu tích, cân theo tâm cho sẵn."""
    s = size * scale
    return [
        (cx - s * 0.52, cy + s * 0.02),
        (cx - s * 0.14, cy + s * 0.40),
        (cx + s * 0.54, cy - s * 0.42),
    ]


def finish(image, name):
    out = image.resize((SIZE, SIZE), Image.LANCZOS)
    out.save(OUT / f"{name}.png")
    return out


# ---------------------------------------------------------------- phuong an A
def concept_a():
    """Dấu tích mọc lên từ mã vạch — 'mã này đã tra xong'."""
    size = SIZE * SS
    plate, mask, inset = base_plate(TEAL_HI, TEAL_LO)
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)

    # Ma vach: cac thanh doc rong hep khac nhau, mo, o nua duoi
    # Ma vach: 6 thanh, khoang cach rong, gom gon trong mot dai o duoi
    bars = [(0.000, 0.055), (0.100, 0.030), (0.170, 0.055),
            (0.270, 0.030), (0.340, 0.030), (0.410, 0.055)]
    left = size * 0.268
    top = size * 0.660
    bottom = size * 0.770
    for x_ratio, w_ratio in bars:
        x = left + size * x_ratio
        draw.rounded_rectangle(
            [x, top, x + size * w_ratio, bottom],
            radius=size * 0.015, fill=WHITE + (135,),
        )

    # Dau tich nam han phia tren dai ma vach, khong cham vao
    points = check_path(size, size * 0.5, size * 0.395, 0.40)
    draw.line(points, fill=WHITE + (255,),
              width=int(size * 0.082), joint="curve")
    for point in points:
        r = size * 0.041
        draw.ellipse([point[0] - r, point[1] - r, point[0] + r, point[1] + r],
                     fill=WHITE + (255,))

    plate.alpha_composite(Image.composite(
        layer, Image.new("RGBA", (size, size), (0, 0, 0, 0)), mask))
    return finish(plate, "icon_a_mavach")


# ---------------------------------------------------------------- phuong an B
def concept_b():
    """Kính lúp, trong lòng kính là dấu tích — đọc được ở 16px."""
    size = SIZE * SS
    plate, mask, inset = base_plate(INK_HI, INK_LO)
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)

    cx, cy = size * 0.450, size * 0.425
    r = size * 0.255
    ring = size * 0.060

    # Can kinh, ve truoc de vong kinh de len tren
    ang = math.radians(45)
    x1, y1 = cx + math.cos(ang) * r * 0.95, cy + math.sin(ang) * r * 0.95
    x2, y2 = cx + math.cos(ang) * r * 2.02, cy + math.sin(ang) * r * 2.02
    draw.line([(x1, y1), (x2, y2)], fill=WHITE + (255,),
              width=int(size * 0.078))
    cap = size * 0.039
    draw.ellipse([x2 - cap, y2 - cap, x2 + cap, y2 + cap], fill=WHITE + (255,))

    # Vong kinh
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=WHITE + (255,),
                 width=int(ring))
    # Mat kinh hoi sang
    draw.ellipse([cx - r + ring / 2, cy - r + ring / 2,
                  cx + r - ring / 2, cy + r - ring / 2],
                 fill=WHITE + (26,))

    # Dau tich mau teal trong long kinh
    points = check_path(size, cx, cy * 1.012, 0.235)
    draw.line(points, fill=TEAL_HI + (255,),
              width=int(size * 0.058), joint="curve")
    for point in points:
        rr = size * 0.029
        draw.ellipse([point[0] - rr, point[1] - rr,
                      point[0] + rr, point[1] + rr], fill=TEAL_HI + (255,))

    plate.alpha_composite(Image.composite(
        layer, Image.new("RGBA", (size, size), (0, 0, 0, 0)), mask))
    return finish(plate, "icon_b_kinhlup")


# ---------------------------------------------------------------- phuong an C
def concept_c():
    """Dấu tích khoét thủng nền — tối giản, một hình duy nhất."""
    size = SIZE * SS
    plate, mask, inset = base_plate(TEAL_HI, TEAL_LO)

    # Khoet dau tich ra khoi nen: ve vao mask alpha
    hole = Image.new("L", (size, size), 0)
    hd = ImageDraw.Draw(hole)
    points = check_path(size, size * 0.5, size * 0.425, 0.40)
    hd.line(points, fill=255, width=int(size * 0.082), joint="curve")
    for point in points:
        r = size * 0.041
        hd.ellipse([point[0] - r, point[1] - r, point[0] + r, point[1] + r],
                   fill=255)

    # Dai ma vach cung khoet thung — cho biet day la app tra ma, khong phai
    # icon "thanh cong" chung chung
    bars = [(0.000, 0.055), (0.100, 0.030), (0.170, 0.055),
            (0.270, 0.030), (0.340, 0.030), (0.410, 0.055)]
    left = size * 0.268
    for x_ratio, w_ratio in bars:
        x = left + size * x_ratio
        hd.rounded_rectangle(
            [x, size * 0.655, x + size * w_ratio, size * 0.755],
            radius=size * 0.015, fill=255,
        )

    alpha = plate.split()[3]
    alpha = Image.composite(Image.new("L", (size, size), 0), alpha, hole)
    plate.putalpha(alpha)

    # Lop nen sang dat duoi phan khoet, cho thay chieu sau
    under = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    under.paste(vertical_gradient(size, (10, 22, 30), (6, 14, 20)), (0, 0), mask)
    under.alpha_composite(plate)
    return finish(under, "icon_c_khoet")


if __name__ == "__main__":
    for build in (concept_a, concept_b, concept_c):
        build()
    # Anh so sanh 3 phuong an o kich thuoc that
    sheet = Image.new("RGBA", (1180, 700), (242, 244, 247, 255))
    labels = ["A — mã vạch", "B — kính lúp", "C — khoét"]
    for index, name in enumerate(["icon_a_mavach", "icon_b_kinhlup", "icon_c_khoet"]):
        icon = Image.open(OUT / f"{name}.png")
        x = 40 + index * 380
        sheet.alpha_composite(icon.resize((320, 320), Image.LANCZOS), (x, 40))
        for jindex, small in enumerate([128, 64, 32]):
            sheet.alpha_composite(
                icon.resize((small, small), Image.LANCZOS),
                (x, 400 + (128 - small) // 2),
            )
            x += small + 24
    sheet.convert("RGB").save(OUT / "so_sanh.png")
    print("xong")
