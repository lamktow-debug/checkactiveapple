#!/bin/bash
# Dong goi Check Active thanh mot file .dmg de mang sang may khac.
# Bam dup file nay. Ket qua nam trong thu muc dist/.
#
# Chi chay duoc tren macOS vi dung hdiutil.

set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE" || exit 1

CUR="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' version.py)"
if [ -z "$CUR" ]; then
    echo "❌ Khong doc duoc so phien ban trong version.py"
    read -n1 -r; exit 1
fi

# Goi y so tiep theo: tang so giua, ve 0 o so cuoi (1.1.0 -> 1.2.0)
GOIY="$(echo "$CUR" | awk -F. '{printf "%d.%d.0", $1, $2+1}')"

echo "======================================"
echo "  Dong goi Check Active"
echo "======================================"
echo
echo "Ban dang o phien ban: $CUR"
echo
echo "  - Them tinh nang  -> tang so giua  (vd $CUR -> $GOIY)"
echo "  - Chi sua loi     -> tang so cuoi  (vd $CUR -> $(echo "$CUR" | awk -F. '{printf "%d.%d.%d", $1, $2, $3+1}'))"
echo
read -r -p "So phien ban moi [Enter = $GOIY, hoac go '$CUR' de giu nguyen]: " NHAP
VERSION="${NHAP:-$GOIY}"

if ! echo "$VERSION" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$'; then
    echo "❌ '$VERSION' khong dung dang. Phai la kieu 1.2.0"
    read -n1 -r; exit 1
fi

# Khong cho lui ve so cu hoac trung: may khac se khong thay ban moi
if [ "$VERSION" != "$CUR" ]; then
    THAPHON="$(printf '%s\n%s\n' "$CUR" "$VERSION" | sort -t. -k1,1n -k2,2n -k3,3n | tail -1)"
    if [ "$THAPHON" != "$VERSION" ]; then
        echo "❌ $VERSION cu hon $CUR. May khac se khong thay day la ban moi."
        read -n1 -r; exit 1
    fi
    # Ghi thang vao version.py, khoi phai sua tay
    sed -i '' "s/^__version__ = \".*\"/__version__ = \"$VERSION\"/" version.py
    echo "  ✅ version.py: $CUR -> $VERSION"
else
    echo "  Giu nguyen $VERSION (dong goi lai ban cu)"
fi
echo

NAME="CheckActive-$VERSION"
STAGE="$HERE/build/$NAME"
CLEAN="$STAGE/$NAME"
DIST="$HERE/dist"
DMG="$DIST/$NAME.dmg"

# ---- 1. Don cho de -------------------------------------------------------
rm -rf "$HERE/build"
mkdir -p "$CLEAN" "$DIST"

# ---- 2. Chep dung nhung thu CAN ------------------------------------------
# Khong rsync ca thu muc project nua, vi folder lam viec co lan bao cao,
# file render, barcode, ket qua chay... DMG chi duoc chua app va code can chay.
echo "Dang chep file..."
rsync -a "$HERE/Check Active.app" "$CLEAN/"
for FILE in \
    CAI_DAT.command \
    README_CHECK_ACTIVE.md \
    app_core.py \
    app_gui.py \
    app_icon_512.png \
    app_qt.py \
    app_qt_core.py \
    app_settings.py \
    app_update.py \
    check_active_parallel.py \
    check_active_v2.py \
    requirements.txt \
    version.py
do
    cp "$HERE/$FILE" "$CLEAN/"
done

# File mau, de nguoi cai biet phai dien gi
cat > "$CLEAN/app_settings.example.json" <<'JSON'
{
  "twocaptcha_key": "",
  "concurrency": 1,
  "headless": false,
  "block_assets": true,
  "capture_screenshot": false,
  "serials_per_session": 15,
  "serial_timeout": 120,
  "manual_captcha": false,
  "turbo_mode": false,
  "github_repo": "",
  "github_token": "",
  "check_updates": true
}
JSON

# Dong bo so phien ban vao Info.plist, khoi phai sua tay hai cho
PLIST="$CLEAN/Check Active.app/Contents/Info.plist"
plutil -replace CFBundleShortVersionString -string "$VERSION" "$PLIST" 2>/dev/null
plutil -replace CFBundleVersion -string "$VERSION" "$PLIST" 2>/dev/null
echo "Info.plist -> $(plutil -extract CFBundleShortVersionString raw "$PLIST" 2>/dev/null)"

chmod +x "$CLEAN/Check Active.app/Contents/MacOS/launcher" 2>/dev/null
chmod +x "$CLEAN"/*.command 2>/dev/null

# ---- 3. Loi tat toi thu muc Applications ---------------------------------
# Khong tao: app PHAI nam canh thu muc project moi chay duoc, nen huong dan
# nguoi dung chep CA THU MUC di, chu khong keo rieng .app vao Applications.
cat > "$CLEAN/DOC TRUOC KHI CAI.txt" <<TXT
Check Active v$VERSION — ONEWAY

CACH CAI
--------
1. Keo CA THU MUC "$NAME" nay vao mot cho co dinh tren may,
   vi du: Documents/ONEWAY/

   QUAN TRONG: "Check Active.app" phai luon nam CANH cac file .py.
   Keo rieng file .app di cho khac thi app se khong chay duoc.

2. Vao thu muc vua chep, bam dup CAI_DAT.command.
   No se tu tao venv, cai thu vien va tai trinh duyet. Mat vai phut.
   Ban giao dien PySide6 can Python 3.11 / 3.12 / 3.13.
   Neu may chi co Python 3.14, cai Python 3.13 roi chay lai file nay.

3. Bam dup "Check Active.app".

4. Lan dau: vao nut Cai dat, dan key 2captcha va (neu muon app tu bao
   khi co ban moi) dan repo GitHub cung token.

MAY BAO "KHONG MO DUOC VI CHUA XAC MINH NHA PHAT TRIEN"
------------------------------------------------------
App nay khong ky so. Chuot phai vao app > Mo > Mo.
Chi phai lam mot lan. CAI_DAT.command cung da tu go co quarantine roi.

CAN GI TREN MAY
---------------
- macOS 11 tro len
- Python 3 (khuyen nghi 3.12). Chua co thi CAI_DAT.command se chi cach cai.
- Mang de tai thu vien lan dau va de check serial
TXT

# ---- 4. Tao anh dia ------------------------------------------------------
echo "Dang tao $NAME.dmg..."
rm -f "$DMG"
hdiutil create \
    -volname "Check Active $VERSION" \
    -srcfolder "$STAGE" \
    -ov -format UDZO \
    "$DMG" || { echo "❌ hdiutil that bai"; read -n1 -r; exit 1; }

# ---- 5. Kiem tra lai anh dia ---------------------------------------------
echo
echo "Dang kiem tra file vua tao..."
if hdiutil verify "$DMG" >/dev/null 2>&1; then
    echo "  ✅ File dmg hop le"
else
    echo "  ⚠️  hdiutil verify bao co van de"
fi

SIZE="$(du -h "$DMG" | cut -f1)"
SHA="$(shasum -a 256 "$DMG" | cut -d' ' -f1)"

rm -rf "$HERE/build"

echo
echo "======================================"
echo "  Xong: $DMG"
echo "  Kich thuoc: $SIZE"
echo "  SHA-256: $SHA"
echo "======================================"
echo
echo "Buoc tiep theo — dua len GitHub de may khac tu thay ban moi:"
echo
echo "  1. Bam dup PUSH_GITHUB.command (day code len)"
echo "  2. Tao release:"
echo "       gh release create v$VERSION \"$DMG\" --title \"v$VERSION\" --notes \"...\""
echo "     Hoac len github.com > Releases > Draft a new release,"
echo "     dat tag la v$VERSION, keo file .dmg vao muc Attach binaries."
echo
echo "  Ten tag PHAI la v$VERSION thi app moi nhan ra la ban moi."
echo

open "$DIST"
read -n1 -r -p "Bam phim bat ky de dong."
