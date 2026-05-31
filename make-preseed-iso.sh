#!/usr/bin/env bash
# Bake a preseed (from debian-vps-bootstrap/config.toml) into a Debian netinst ISO.
#
# - Reuses bootstrap.py's build_preseed / build_new_initrd (single source of config).
# - Embeds preseed.cfg + payload into the ISO's own initrd(s) — both the text and the
#   graphical installer initrd, so whichever entry boots runs fully unattended.
# - Injects `auto=true priority=critical` into every boot entry (isolinux + grub).
# - Install target must NOT be "auto": when booted from USB the stick may appear as
#   /dev/sda and auto-detect could wipe the USB itself. Taken from $TARGET_DISK or
#   from config.toml's [target] disk; refuses "auto".
# - Repacks with `xorriso ... -boot_image any replay` to preserve the hybrid
#   BIOS/UEFI boot layout.
#
# Usage:
#   sudo bash make-preseed-iso.sh
#   sudo SRC=/path/in.iso OUT=/path/out.iso TARGET_DISK=/dev/nvme0n1 bash make-preseed-iso.sh
#
# Env overrides (all optional; sensible defaults shown):
#   REPO         このスクリプトのあるディレクトリ（bootstrap.py の場所）
#   CONFIG       $REPO/config.toml
#   SRC          元の netinst ISO（既定: $REPO 直下の debian-*-amd64-netinst.iso を自動検出）
#   OUT          出力 ISO（既定: SRC と同じ場所に -preseed を付けた名前）
#   TARGET_DISK  インストール先デバイス（既定: config の [target] disk。"auto" は不可）

set -euo pipefail

REPO="${REPO:-$(cd "$(dirname "$(readlink -f "$0")")" && pwd)}"
CONFIG="${CONFIG:-$REPO/config.toml}"
KPARAMS="auto=true priority=critical"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/preseed-iso.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

step() { printf '\n=== %s ===\n' "$1"; }
die()  { echo "$*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || die "root で実行してください: sudo bash $0"

# ---------------------------------------------------------------------------
step "0. 前提チェック"
# ---------------------------------------------------------------------------
command -v xorriso >/dev/null || die "xorriso が必要: apt-get install -y xorriso"
[[ -f "$CONFIG" ]] || die "config.toml が無い: $CONFIG"

# 元 ISO: 明示が無ければ REPO 直下の netinst ISO（-preseed を除く）を1つ自動検出。
if [[ -z "${SRC:-}" ]]; then
    mapfile -t _isos < <(find "$REPO" -maxdepth 1 -name 'debian-*-amd64-netinst.iso' \
                         ! -name '*-preseed.iso' | sort)
    [[ ${#_isos[@]} -eq 1 ]] || die "元 ISO を SRC= で指定してください（自動検出: ${#_isos[@]} 件）。"
    SRC="${_isos[0]}"
fi
[[ -f "$SRC" ]] || die "元 ISO が無い: $SRC"
OUT="${OUT:-${SRC%.iso}-preseed.iso}"

# インストール先: env > config。"auto" は USB を消す危険があるため拒否。
if [[ -z "${TARGET_DISK:-}" ]]; then
    TARGET_DISK="$(REPO="$REPO" CONFIG="$CONFIG" python3 - <<'PY'
import os, sys
sys.path.insert(0, os.environ["REPO"])
import bootstrap as b
print(b.load_config(os.environ["CONFIG"])["target"]["disk"])
PY
)"
fi
[[ -n "$TARGET_DISK" && "$TARGET_DISK" != "auto" ]] || die \
    "TARGET_DISK を実デバイスで指定してください（USB 起動時の auto は USB を消す危険）。
  例: sudo TARGET_DISK=/dev/nvme0n1 bash $0  / または config の [target] disk を実デバイスに。"

echo "repo=$REPO"
echo "src=$SRC"
echo "out=$OUT"
echo "target=$TARGET_DISK  (全消去されるインストール先)"

# ---------------------------------------------------------------------------
step "1. ISO から initrd と boot 設定を取り出す"
# ---------------------------------------------------------------------------
# text installer initrd は必須。isolinux(BIOS) / grub(UEFI) のメニューも取り出す。
xorriso -osirrox on -indev "$SRC" \
    -extract /install.amd/initrd.gz "$WORK/initrd.orig.gz" \
    -extract /isolinux "$WORK/isolinux" \
    -extract /boot/grub "$WORK/grub"
echo "取り出し: initrd.orig.gz / isolinux/ / grub/"

# graphical(gtk) installer initrd は存在すれば取り出す（無い ISO もある）。
HAVE_GTK=0
if xorriso -osirrox on -indev "$SRC" \
        -extract /install.amd/gtk/initrd.gz "$WORK/initrd.gtk.orig.gz" 2>/dev/null; then
    HAVE_GTK=1
    echo "取り出し: install.amd/gtk/initrd.gz (graphical も preseed 対象にする)"
fi

# ---------------------------------------------------------------------------
step "2. bootstrap.py で preseed 埋め込み initrd を生成（text/gtk 両方）"
# ---------------------------------------------------------------------------
# build_preseed/build_new_initrd を流用。disk は auto を使わず TARGET_DISK を直接渡す。
REPO="$REPO" CONFIG="$CONFIG" TARGET_DISK="$TARGET_DISK" \
ORIG="$WORK/initrd.orig.gz" NEW="$WORK/initrd.new.gz" \
ORIG_GTK="$([[ $HAVE_GTK -eq 1 ]] && echo "$WORK/initrd.gtk.orig.gz" || true)" \
NEW_GTK="$([[ $HAVE_GTK -eq 1 ]] && echo "$WORK/initrd.gtk.new.gz" || true)" \
python3 - <<'PY'
import os, sys
sys.path.insert(0, os.environ["REPO"])
import bootstrap as b

cfg = b.load_config(os.environ["CONFIG"])
errs = b.validate_config(cfg)
if errs:
    for e in errs:
        print("[設定エラー]", e, file=sys.stderr)
    sys.exit(1)

rcfg = b.resolve_config(cfg)
net = b.resolve_network(rcfg)
disk = os.environ["TARGET_DISK"]                        # ← auto 検出を使わない

user_pw = b.resolve_password(rcfg, "user", interactive=True)
ans_pw = (b.resolve_password(rcfg, "ansible", interactive=True)
          if rcfg["ansible"]["enabled"] else "")

payload = {
    "payload/firstboot.sh":         b.build_firstboot(rcfg).encode(),
    "payload/firstboot.service":    b.build_firstboot_service(),
    "payload/sshd_hardening.conf":  b.build_sshd_hardening(rcfg),
    "payload/user_authorized_keys": ("\n".join(rcfg["user"]["ssh_authorized_keys"]) + "\n").encode(),
}
if rcfg["ansible"]["enabled"]:
    payload["payload/bootstrap.env"] = f"ANSIBLE_PW_HASH='{ans_pw}'\n".encode()
    payload["payload/ansible_authorized_keys"] = (
        "\n".join(rcfg["ansible"]["ssh_authorized_keys"]) + "\n").encode()

preseed = b.build_preseed(rcfg, disk, user_pw, net)

pairs = [(os.environ["ORIG"], os.environ["NEW"])]
if os.environ.get("ORIG_GTK"):
    pairs.append((os.environ["ORIG_GTK"], os.environ["NEW_GTK"]))
for orig_p, new_p in pairs:
    with open(orig_p, "rb") as fh:
        orig = fh.read()
    new = b.build_new_initrd(orig, preseed, payload)
    with open(new_p, "wb") as fh:
        fh.write(new)
    print(f"  preseed 埋め込み initrd: {os.path.basename(new_p)} {len(new)} bytes "
          f"(target={disk}, net={net[0]})")
PY

# ---------------------------------------------------------------------------
step "3. ブートメニューに $KPARAMS を注入"
# ---------------------------------------------------------------------------
# isolinux(BIOS): すべての append 行に追記。timeout を 5 秒に。
find "$WORK/isolinux" -type f -name '*.cfg' -print0 | while IFS= read -r -d '' f; do
    sed -i "/append/ s|\$| $KPARAMS|" "$f"
done
if grep -rqi '^[[:space:]]*timeout' "$WORK/isolinux"; then
    sed -ri 's|^[[:space:]]*timeout[[:space:]]+[0-9]+|timeout 50|I' "$WORK"/isolinux/*.cfg
fi
# grub(UEFI): すべての linux 行に追記。timeout を 5 秒に。
sed -i "/^[[:space:]]*linux/ s|\$| $KPARAMS|" "$WORK/grub/grub.cfg"
sed -ri 's|^[[:space:]]*set[[:space:]]+timeout=.*|set timeout=5|' "$WORK/grub/grub.cfg"
echo "注入完了 (isolinux append / grub linux)"

# ---------------------------------------------------------------------------
step "4. xorriso で再パック (ハイブリッド boot を replay で維持)"
# ---------------------------------------------------------------------------
maps=(-map "$WORK/initrd.new.gz" /install.amd/initrd.gz)
[[ $HAVE_GTK -eq 1 ]] && maps+=(-map "$WORK/initrd.gtk.new.gz" /install.amd/gtk/initrd.gz)
xorriso -indev "$SRC" -outdev "$OUT" \
    -boot_image any replay \
    -volid "DEBIAN_PRESEED" \
    "${maps[@]}" \
    -map "$WORK/isolinux" /isolinux \
    -map "$WORK/grub/grub.cfg" /boot/grub/grub.cfg \
    -commit 2>&1 | tail -20

# 生成 ISO には preseed（パスワードハッシュ入り）が含まれるため権限を絞る。
chmod 0600 "$OUT"

# ---------------------------------------------------------------------------
step "5. 結果"
# ---------------------------------------------------------------------------
ls -lh "$OUT"
cat <<EOF

完了。USB へ書き込み（安全確認付きの burn-iso.sh 推奨）:
  sudo ISO="$OUT" DEV=/dev/sdX bash "$REPO/burn-iso.sh"   # /dev/sdX は USB。removable 判定で内蔵ディスクは弾く
  # 手で焼くなら: lsblk -do NAME,SIZE,MODEL,TRAN で TRAN=usb を確認のうえ dd

!! 書き込み先(USB)とインストール先($TARGET_DISK)を混同しないこと。
!! 起動して放置すると 5 秒後に無人インストールが始まり、$TARGET_DISK を全消去します。
EOF
