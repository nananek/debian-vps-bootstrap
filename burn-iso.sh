#!/usr/bin/env bash
# Write a Debian netinst (or preseed-baked) ISO to a removable USB disk.
#
# Safety: refuses to run against a non-removable disk (protects internal NVMe/SATA),
# refuses an out-of-range size, unmounts the target first, then requires you to type
# the device name. Writes with retry, then verifies by hashing the written bytes.
#
# Usage:
#   sudo bash burn-iso.sh                                  # ISO 自動検出, DEV=/dev/sda
#   sudo ISO=out-preseed.iso DEV=/dev/sdb bash burn-iso.sh
#
# Env overrides:
#   ISO          書き込む ISO（既定: repo 直下の *-preseed.iso を1つ自動検出）
#   DEV          書き込み先(USB)デバイス（既定: /dev/sda。removable=1 必須）
#   ISO_SHA256   指定時のみ ISO の SHA256 を検証（未指定なら検証スキップ）

set -euo pipefail

REPO="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
DEV="${DEV:-/dev/sda}"

step() { printf '\n=== %s ===\n' "$1"; }
die()  { echo "$*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || die "root で実行してください: sudo bash $0"

# ISO: 明示が無ければ repo 直下の *-preseed.iso を1つ自動検出。
if [[ -z "${ISO:-}" ]]; then
    mapfile -t _isos < <(find "$REPO" -maxdepth 1 -name '*-preseed.iso' | sort)
    [[ ${#_isos[@]} -eq 1 ]] || die "書き込む ISO を ISO= で指定してください（自動検出: ${#_isos[@]} 件）。"
    ISO="${_isos[0]}"
fi

# ---------------------------------------------------------------------------
step "1. 対象デバイスの安全確認"
# ---------------------------------------------------------------------------
base="$(basename "$DEV")"
[[ -b "$DEV" ]] || die "$DEV がブロックデバイスでない"
[[ "$(cat "/sys/block/$base/removable" 2>/dev/null)" == "1" ]] \
    || die "!! $DEV はリムーバブルではない。中止（内蔵ディスク保護）"
# サイズの sanity（4GiB 未満や 200GiB 超は USB メモリでないとみなして弾く）。
sectors="$(cat "/sys/block/$base/size")"
gib=$(( sectors * 512 / 1024 / 1024 / 1024 ))
(( gib >= 4 && gib <= 200 )) || die "!! $DEV のサイズ ${gib}GiB が想定外。中止"
echo "対象: $DEV  サイズ: ${gib}GiB  removable=1  model=$(xargs < "/sys/block/$base/device/model" 2>/dev/null)"
lsblk -o NAME,SIZE,TYPE,MOUNTPOINT "$DEV"

# ---------------------------------------------------------------------------
step "2. ISO の存在と（任意の）SHA256 検証"
# ---------------------------------------------------------------------------
[[ -f "$ISO" ]] || die "ISO が無い: $ISO"
echo "ISO: $ISO ($(stat -c %s "$ISO") bytes)"
if [[ -n "${ISO_SHA256:-}" ]]; then
    echo "${ISO_SHA256}  ${ISO}" | sha256sum -c -
else
    echo "ISO_SHA256 未指定のため SHA256 検証はスキップします。"
fi

# ---------------------------------------------------------------------------
step "3. $DEV のパーティションをアンマウント"
# ---------------------------------------------------------------------------
for part in $(lsblk -lno NAME "$DEV" | tail -n +2); do
    mp="$(lsblk -no MOUNTPOINT "/dev/$part" | head -1)"
    if [[ -n "$mp" ]]; then
        echo "umount /dev/$part ($mp)"
        umount "/dev/$part"
    fi
done
echo "アンマウント完了"

# ---------------------------------------------------------------------------
step "4. 最終確認"
# ---------------------------------------------------------------------------
echo "!! $DEV の全データが消去され、$ISO が書き込まれます。"
read -r -p "続行するにはデバイス名をそのまま入力 ($DEV): " ans
[[ "$ans" == "$DEV" ]] || die "入力不一致。中止しました。"

# ---------------------------------------------------------------------------
step "5. 残骸の除去"
# ---------------------------------------------------------------------------
# 前回の中途半端な書き込みのパーティション署名を消してから焼く。
wipefs -a "$DEV" || true
blockdev --rereadpt "$DEV" 2>/dev/null || true

# ---------------------------------------------------------------------------
step "6. 書き込み (dd) → 同期"
# ---------------------------------------------------------------------------
# oflag=direct は一部の安物 USB で脱落を誘発するため使わない。bs=1M で穏やかに。
# 失敗(デバイス脱落)時は一呼吸おいて最大3回まで自動リトライ。
ok=0
for attempt in 1 2 3; do
    echo "--- 書き込み試行 $attempt/3 ---"
    if dd if="$ISO" of="$DEV" bs=1M conv=fsync status=progress; then
        ok=1; break
    fi
    echo "!! 試行 $attempt 失敗。デバイス再認識を待機(5s)..."
    sync; sleep 5
    [[ -b "$DEV" ]] || die "$DEV が消失。USB を挿し直して再実行してください。"
done
[[ $ok -eq 1 ]] || die "3回とも失敗。別の USB ポート/ケーブル/USB メモリを試してください。"
sync
echo

# ---------------------------------------------------------------------------
step "7. 検証 (書き込んだ先頭を ISO と比較)"
# ---------------------------------------------------------------------------
isize=$(stat -c %s "$ISO")
iso_sha=$(sha256sum "$ISO" | cut -d' ' -f1)
# iflag=count_bytes で先頭 isize バイトちょうどを読む（head を挟まず SIGPIPE しない）。
dev_sha=$(dd if="$DEV" bs=1M iflag=count_bytes count="$isize" status=none | sha256sum | cut -d' ' -f1)
[[ "$dev_sha" == "$iso_sha" ]] \
    || die "!! 警告: ハッシュ不一致 (dev=$dev_sha iso=$iso_sha)。書き込みを確認してください。"
echo "OK: $DEV の内容が ISO と一致しました。"

echo
echo "完了。$DEV から起動すれば Debian インストーラが立ち上がります。"
echo "（UEFI 機では BIOS/ブートメニューで USB を UEFI モードで選択してください）"
