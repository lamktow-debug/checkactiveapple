#!/bin/bash
# Cai dat Check Active tren mot may moi.
# Bam dup file nay. Chay mot lan la xong.

set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE" || exit 1

echo "======================================"
echo "  Check Active — cai dat"
echo "======================================"
echo "Thu muc: $HERE"
echo

# ---- 1. Tim mot ban Python dung duoc -------------------------------------
PY=""
for candidate in python3.13 python3.12 python3.11; do
    if command -v "$candidate" >/dev/null 2>&1; then
        PY="$(command -v "$candidate")"
        echo "Dung Python: $PY ($("$PY" -V 2>&1))"
        break
    fi
done

if [ -z "$PY" ]; then
    echo "❌ May nay chua co Python 3.11 / 3.12 / 3.13."
    echo "   Ban giao dien PySide6 khong dung Python 3.14 vi de crash tren macOS."
    echo "   Cai bang mot trong hai cach:"
    echo "     - Tai tu python.org"
    echo "     - Hoac: brew install python@3.13"
    echo
    read -n1 -r -p "Bam phim bat ky de dong."
    exit 1
fi

PY_VER="$("$PY" - <<'PYCODE'
import sys
print(f"{sys.version_info.major}.{sys.version_info.minor}")
PYCODE
)"
case "$PY_VER" in
    3.11|3.12|3.13) ;;
    *)
        echo "❌ Python $PY_VER khong dung cho ban PySide6 nay."
        echo "   Cai Python 3.13 roi chay lai CAI_DAT.command."
        read -n1 -r -p "Bam phim bat ky de dong."
        exit 1
        ;;
esac

# ---- 2. Dung venv --------------------------------------------------------
if [ -x "venv/bin/python" ]; then
    VENV_VER="$(venv/bin/python - <<'PYCODE'
import sys
print(f"{sys.version_info.major}.{sys.version_info.minor}")
PYCODE
)"
    case "$VENV_VER" in
        3.11|3.12|3.13)
            echo "venv da co san, dung lai. (Python $VENV_VER)"
            ;;
        *)
            echo "venv dang dung Python $VENV_VER, khong hop voi ban PySide6."
            echo "Dang tao lai venv bang Python $PY_VER..."
            rm -rf venv
            ;;
    esac
fi

if [ ! -x "venv/bin/python" ]; then
    echo
    echo "Dang tao moi truong ao (venv)..."
    "$PY" -m venv venv || { echo "❌ Tao venv that bai."; read -n1 -r; exit 1; }
fi

VENV_PY="$HERE/venv/bin/python"

# ---- 3. Cai thu vien -----------------------------------------------------
echo
echo "Dang cai thu vien (mat vai phut lan dau)..."
"$VENV_PY" -m pip install --upgrade pip >/dev/null 2>&1
"$VENV_PY" -m pip install -r requirements.txt || {
    echo
    echo "⚠️  Co thu vien cai khong xong."
    echo "   Cach xu ly: cai Python 3.13 (brew install python@3.13), xoa thu muc"
    echo "   venv di roi chay lai file nay."
    echo
    read -n1 -r -p "Bam phim bat ky de dong."
    exit 1
}

# ---- 4. Tai trinh duyet cho Playwright -----------------------------------
echo
echo "Dang tai trinh duyet cho Playwright (~150MB, chi lan dau)..."
"$VENV_PY" -m playwright install chromium || {
    echo "⚠️  Tai trinh duyet that bai — kiem tra mang roi chay lai file nay."
    read -n1 -r -p "Bam phim bat ky de dong."
    exit 1
}

# ---- 5. Cho phep chay -----------------------------------------------------
chmod +x "Check Active.app/Contents/MacOS/launcher" 2>/dev/null
# Go co "tai tu mang" de macOS khong chan app chua ky
xattr -dr com.apple.quarantine "Check Active.app" 2>/dev/null
xattr -dr com.apple.quarantine "$HERE" 2>/dev/null

# ---- 6. Kiem tra lai ------------------------------------------------------
echo
echo "Dang kiem tra..."
"$VENV_PY" - <<'PYCODE'
import sys
loi = []
for ten in ("playwright", "openpyxl", "playwright_stealth", "PySide6"):
    try:
        __import__(ten)
        print(f"  ✅ {ten}")
    except ImportError as e:
        loi.append(ten)
        print(f"  ❌ {ten}: {e}")
sys.exit(1 if loi else 0)
PYCODE
KIEMTRA=$?

echo
if [ $KIEMTRA -eq 0 ]; then
    echo "✅ Xong. Bam dup 'Check Active.app' de mo."
    echo
    echo "   Lan dau mo nho vao ⚙ Cai dat de dan:"
    echo "     - key 2captcha"
    echo "     - repo GitHub + token (de app tu bao khi co ban moi)"
    open "$HERE"
else
    echo "❌ Con thieu thu vien o tren. Doc phan huong dan roi chay lai file nay."
fi

echo
read -n1 -r -p "Bam phim bat ky de dong."
