#!/bin/bash
# Cai OCR chay tren may (ddddocr) de khoi phai mua tung ma cua 2captcha.
# Bam dup file nay. Chay mot lan la xong.
#
# Vi sao lai la venv rieng: ddddocr chi ho tro Python 3.10-3.13, con venv chinh
# cua app dang chay 3.14 (PySide6 chay tot o do). Tach ra thi moi ben dung dung
# ban Python cua minh, khong dung cham nhau.

set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE" || exit 1

echo "======================================"
echo "  Cai OCR chay tren may"
echo "======================================"
echo
echo "OCR may doc captcha trong ~0.2 giay va MIEN PHI."
echo "2captcha mat ~9 giay va tinh tien tung ma."
echo "Sau khi cai, app se thu OCR may truoc, truot moi mua ma."
echo

# ---- 1. Tim Python 3.10-3.13 --------------------------------------------
PY=""
for candidate in python3.13 python3.12 python3.11 python3.10; do
    if command -v "$candidate" >/dev/null 2>&1; then
        PY="$(command -v "$candidate")"
        echo "Dung Python: $PY ($("$PY" -V 2>&1))"
        break
    fi
done

if [ -z "$PY" ]; then
    echo "❌ May chua co Python 3.10-3.13."
    echo "   Ban Python 3.14 trong venv chinh KHONG dung duoc: ddddocr chua ho tro."
    echo
    echo "   Cai them mot ban:"
    echo "     brew install python@3.12"
    echo "   Roi chay lai file nay. App van chay binh thuong voi 2captcha."
    echo
    read -n1 -r -p "Bam phim bat ky de dong."
    exit 1
fi

# ---- 2. venv rieng cho OCR ------------------------------------------------
if [ ! -x "venv-ocr/bin/python" ]; then
    echo
    echo "Dang tao venv-ocr..."
    "$PY" -m venv venv-ocr || { echo "❌ Tao venv-ocr that bai."; read -n1 -r; exit 1; }
else
    echo "venv-ocr da co san."
fi

# ---- 3. Cai ddddocr (~76MB) ----------------------------------------------
echo
echo "Dang cai ddddocr (~76MB, chi lan dau)..."
./venv-ocr/bin/python -m pip install --upgrade pip >/dev/null 2>&1
if ! ./venv-ocr/bin/python -m pip install ddddocr; then
    echo
    echo "❌ Cai ddddocr that bai."
    echo "   Thuong la do onnxruntime chua co ban cho Python nay."
    echo "   App van chay binh thuong voi 2captcha."
    read -n1 -r -p "Bam phim bat ky de dong."
    exit 1
fi

# ---- 4. Kiem tra that -----------------------------------------------------
echo
echo "Dang kiem tra..."
if ./venv/bin/python ocr_local.py; then
    echo
    echo "✅ Xong. Mo lai app la no tu dung OCR may."
    echo
    echo "   Cuoi moi me chay, app se bao ti le trung, vi du:"
    echo "     🧠 OCR may: 12/18 ma duoc Apple chap nhan (67%)"
    echo
    echo "   Ti le thap qua thi vao Cai dat tat di, khong sao ca."
else
    echo
    echo "❌ Cai xong nhung chay chua duoc. App van dung 2captcha binh thuong."
fi

echo
read -n1 -r -p "Bam phim bat ky de dong."
