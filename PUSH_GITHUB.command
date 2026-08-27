#!/bin/bash
# Day code len GitHub va tao release. Bam dup file nay.
# Lan dau se hoi vai thu; nhung lan sau chi can bam dup la xong.

set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE" || exit 1

VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' version.py)"
DMG="$HERE/dist/CheckActive-$VERSION.dmg"

dung_lai() {
    echo
    echo "$1"
    echo
    read -n1 -r -p "Bam phim bat ky de dong."
    exit 1
}

echo "======================================"
echo "  Day Check Active v$VERSION len GitHub"
echo "======================================"
echo

command -v git >/dev/null 2>&1 || dung_lai "❌ May chua co git. Cai bang: xcode-select --install"

# ---- 0. Go file khoa cu neu con sot lai --------------------------------------
# Git tao .git/index.lock moi lan sua index. Neu mot tien trinh git bi giet
# giua chung (hoac chay tu moi truong khong co quyen xoa file), file nay o lai
# va MOI lenh git sau do deu tu choi chay.
if [ -f "$HERE/.git/index.lock" ]; then
    if pgrep -x git >/dev/null 2>&1; then
        dung_lai "❌ Dang co tien trinh git khac chay. Dong no lai roi chay lai file nay."
    fi
    rm -f "$HERE/.git/index.lock" || dung_lai "❌ Khong xoa duoc .git/index.lock. Xoa tay roi chay lai."
    echo "🧹 Da go file khoa cu (.git/index.lock)"
    echo
fi

# ---- 1. Repo ----------------------------------------------------------------
if ! git remote get-url origin >/dev/null 2>&1; then
    echo "Chua co repo tren GitHub."
    echo "Vao github.com/new tao mot repo PRIVATE, TRONG (dung tich"
    echo "'Add a README file'), roi dan dia chi vao day."
    echo
    read -r -p "Dia chi repo: " REPOURL
    [ -z "$REPOURL" ] && dung_lai "Bo qua."
    git remote add origin "$REPOURL"
fi

REPOURL="$(git remote get-url origin)"
# owner/repo, dung cho gh va cho o Cai dat trong app
SLUG="$(echo "$REPOURL" | sed -E 's#^(https?://)?(www\.)?github\.com/##; s#\.git$##; s#/$##')"
echo "Repo: $SLUG"
echo

# ---- 2. Danh tinh -- KHONG chap nhan de trong -------------------------------
# Git tu choi commit voi ten rong, va bao loi kho hieu. Chan ngay tu day.
hoi_khong_rong() {
    local nhan="$1" macdinh="$2" gia_tri=""
    while [ -z "$gia_tri" ]; do
        read -r -p "$nhan [$macdinh]: " gia_tri
        gia_tri="${gia_tri:-$macdinh}"
        [ -z "$gia_tri" ] && echo "  Cho nay khong duoc de trong."
    done
    echo "$gia_tri"
}

OWNER="${SLUG%%/*}"
if [ -z "$(git config user.name || true)" ]; then
    GITNAME="$(hoi_khong_rong "Ten hien trong lich su commit" "$OWNER")"
    git config user.name "$GITNAME"
    echo "  user.name = $GITNAME"
fi
if [ -z "$(git config user.email || true)" ]; then
    GITMAIL="$(hoi_khong_rong "Email GitHub cua ban" "$OWNER@users.noreply.github.com")"
    git config user.email "$GITMAIL"
    echo "  user.email = $GITMAIL"
fi
echo

# ---- 3. Chan file bi mat lot len -------------------------------------------
for SECRET in app_settings.json proxies.txt; do
    if git ls-files --error-unmatch "$SECRET" >/dev/null 2>&1; then
        dung_lai "🛑 DUNG LAI: $SECRET dang bi git theo doi.
   File nay chua key 2captcha va token GitHub cua ban.
   Go ra bang: git rm --cached $SECRET"
    fi
done

# ---- 4. Commit --------------------------------------------------------------
# add -A chu KHONG phai add -u: repo moi chi theo doi README.md, nen -u se
# khong stage duoc file nao ca. Viec loai tru tai lieu/du lieu da do .gitignore
# lo (da kiem tra: chi 27 file cua app duoc len).
git add -A || dung_lai "❌ git add that bai."

if git diff --cached --quiet 2>/dev/null && git rev-parse --verify HEAD >/dev/null 2>&1; then
    echo "Khong co gi thay doi de commit."
else
    echo "Se commit $(git diff --cached --name-only | wc -l | tr -d ' ') file:"
    git diff --cached --name-only | head -20 | sed 's/^/  /'
    [ "$(git diff --cached --name-only | wc -l)" -gt 20 ] && echo "  ... va nhung file khac"
    echo
    read -r -p "Ghi chu commit [Cap nhat v$VERSION]: " MSG
    git commit -m "${MSG:-Cap nhat v$VERSION}" || dung_lai "❌ Commit that bai."
fi

# ---- 5. Day len -------------------------------------------------------------
# symbolic-ref chay dung ca khi chua co commit nao; abbrev-ref thi tra ve
# chuoi "HEAD" va lam lenh push hong.
BRANCH="$(git symbolic-ref --short HEAD 2>/dev/null || echo main)"

if ! git rev-parse --verify HEAD >/dev/null 2>&1; then
    dung_lai "❌ Repo chua co commit nao nen khong co gi de day len."
fi

echo
echo "Dang day len nhanh $BRANCH..."
PUSHOUT="$(git push -u origin "$BRANCH" 2>&1)"
PUSHOK=$?
echo "$PUSHOUT"

if [ $PUSHOK -ne 0 ]; then
    if echo "$PUSHOUT" | grep -qE "fetch first|non-fast-forward|\[rejected\]"; then
        # Repo tren GitHub da co san commit (thuong la README tao luc lap repo).
        # Hai ben khong chung goc nen GitHub tu choi. Rebase commit cua minh
        # len tren commit cua remote roi day lai.
        echo
        echo "GitHub da co san noi dung (thuong la README luc tao repo)."
        echo "Se gop noi dung do vao roi day lai — khong mat gi cua ban."
        echo
        read -r -p "Lam luon? [C/k]: " DONGY
        if [ "${DONGY:-C}" = "k" ] || [ "${DONGY:-C}" = "K" ]; then
            dung_lai "Da dung lai, chua day gi len."
        fi
        if ! git pull --rebase origin "$BRANCH"; then
            dung_lai "❌ Gop khong xong — co xung dot file.
   Xem chi tiet bang: git status
   Hoac nho mo lai cuoc tro chuyen de duoc ho tro."
        fi
        echo
        echo "Da gop xong, day lai..."
        git push -u origin "$BRANCH" || dung_lai "❌ Van khong day len duoc."
    elif echo "$PUSHOUT" | grep -qiE "authentication failed|could not read Username|Permission denied|403|Support for password authentication"; then
        dung_lai "❌ Chua dang nhap GitHub. Cach de nhat:
     brew install gh && gh auth login
   Roi chay lai file nay."
    else
        dung_lai "❌ Day len that bai — doc thong bao cua git o tren."
    fi
fi

echo
echo "✅ Da day code len $SLUG"

# ---- 6. Tao release, neu co gh ----------------------------------------------
echo
if [ ! -f "$DMG" ]; then
    echo "⚠️  Chua thay $DMG"
    echo "   Chay DONG_GOI_DMG.command truoc roi quay lai buoc tao release."
    echo
    read -n1 -r -p "Bam phim bat ky de dong."
    exit 0
fi

if ! command -v gh >/dev/null 2>&1; then
    echo "Con mot buoc nua: tao release thi may khac moi thay ban moi."
    echo
    echo "  Cach nhanh — cai gh mot lan roi lan sau file nay lam ho luon:"
    echo "    brew install gh && gh auth login"
    echo
    echo "  Hoac lam tay: len github.com/$SLUG/releases/new"
    echo "    - Tag:  v$VERSION"
    echo "    - Keo file nay vao muc Attach binaries:"
    echo "      $DMG"
    echo
    read -n1 -r -p "Bam phim bat ky de dong."
    exit 0
fi

if ! gh auth status >/dev/null 2>&1; then
    echo "gh co roi nhung chua dang nhap. Chay: gh auth login"
    echo "Roi chay lai file nay de tao release."
    echo
    read -n1 -r -p "Bam phim bat ky de dong."
    exit 0
fi

if gh release view "v$VERSION" >/dev/null 2>&1; then
    echo "Release v$VERSION da ton tai tren GitHub."
    read -r -p "Ghi de file .dmg trong release do? [k/C]: " GHIDE
    if [ "${GHIDE:-C}" = "k" ] || [ "${GHIDE:-C}" = "K" ]; then
        gh release upload "v$VERSION" "$DMG" --clobber && echo "✅ Da thay file .dmg"
    fi
else
    echo "Tao release v$VERSION va dinh kem file .dmg..."
    read -r -p "Ghi chu release (co gi moi) [Ban v$VERSION]: " NOTES
    if gh release create "v$VERSION" "$DMG" \
        --title "v$VERSION" \
        --notes "${NOTES:-Ban v$VERSION}"; then
        echo
        echo "✅ Xong het. May nao mo app cung se thay dai bao 'Co ban $VERSION'."
    else
        echo "❌ Tao release that bai. Lam tay tai: github.com/$SLUG/releases/new"
    fi
fi

echo
echo "Nho dan vao ⚙ Cai dat cua app tren tung may:"
echo "  Repo GitHub: $SLUG"
echo "  Token:       fine-grained PAT, quyen Contents: Read-only cho repo nay"
echo
read -n1 -r -p "Bam phim bat ky de dong."
