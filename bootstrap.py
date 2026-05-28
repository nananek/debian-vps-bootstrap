#!/usr/bin/env python3
"""
debian-vps-bootstrap
====================
ISO マウントができない VPS で、稼働中の OS を kexec で netboot インストーラに
すり替え、preseed で無人インストールしてディスクを Debian (trixie) で上書きする。

特徴:
  - Python3 標準ライブラリのみで完結（cpio / gzip / ダウンロードすべて自前処理）。
    外部コマンド依存は実質 kexec のみ。
  - その kexec も冒頭で前提インストール（apt/dnf/yum/zypper/pacman を自動判別）。
  - ユーザー名・パスワード・SSH 公開鍵・対象ディスクは実行時プロンプトで決定。
  - Docker / Tailscale / ansible ユーザー作成は「初回起動後の systemd oneshot」で実行
    （ネットワークと apt が完全に動く環境で行うため堅牢）。

設計上の前提:
  - メインユーザー : SSH は公開鍵のみ（パスワード認証・root ログイン無効）。
                     パスワードは VNC ローカルコンソール救済用に有効。
  - ansible ユーザー : first-boot で「別途」作成。SSH 鍵 + 通常 sudo（パスワード必須）。
  - Tailscale       : インストールのみ。`tailscale up` は手動。

必ず root で、上書きしてよい VPS 上で実行すること。実行すると元の OS は消える。
"""

import gzip
import os
import platform
import shutil
import subprocess
import sys
import urllib.request
from getpass import getpass

# ---------------------------------------------------------------------------
# 設定（必要なら書き換え）
# ---------------------------------------------------------------------------
SUITE = "trixie"                 # Debian コードネーム
TIMEZONE = "Asia/Tokyo"          # 新システムのタイムゾーン
MIRROR_HOST = "deb.debian.org"
MIRROR_DIR = "/debian"
WORKDIR = "/root/debian-bootstrap"

# x86_64 -> amd64, aarch64 -> arm64 に正規化
_ARCH_MAP = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}
ARCH = _ARCH_MAP.get(platform.machine().lower())

KERNEL_URL = (
    f"http://{MIRROR_HOST}{MIRROR_DIR}/dists/{SUITE}/main/installer-{ARCH}/current/"
    f"images/netboot/debian-installer/{ARCH}/linux"
)
INITRD_URL = (
    f"http://{MIRROR_HOST}{MIRROR_DIR}/dists/{SUITE}/main/installer-{ARCH}/current/"
    f"images/netboot/debian-installer/{ARCH}/initrd.gz"
)

# kexec に渡すカーネルコマンドライン。
#   - console を ttyS0 / tty0 両方に出して、シリアル系・VGA(VNC)系どちらの
#     プロバイダでもインストールの様子を追えるようにする。
KERNEL_CMDLINE = "auto=true priority=critical console=ttyS0,115200 console=tty0 nomodeset"


# ---------------------------------------------------------------------------
# ユーティリティ
# ---------------------------------------------------------------------------
def die(msg: str) -> "None":
    print(f"\n[FATAL] {msg}", file=sys.stderr)
    sys.exit(1)


def info(msg: str) -> None:
    print(f"[*] {msg}")


def run(cmd, **kw) -> subprocess.CompletedProcess:
    info("$ " + " ".join(cmd))
    return subprocess.run(cmd, check=True, **kw)


def have(binary: str) -> bool:
    return shutil.which(binary) is not None


# ---------------------------------------------------------------------------
# 1. 前提パッケージの導入（特に kexec）
# ---------------------------------------------------------------------------
def ensure_prereqs() -> None:
    """kexec が無ければパッケージマネージャを自動判別して導入する。"""
    if have("kexec"):
        info("kexec は既に導入済み")
        return

    info("kexec が無いので導入を試みる")
    # (検出コマンド, インストールコマンド) を優先順に
    managers = [
        ("apt-get", [["apt-get", "update"], ["apt-get", "install", "-y", "kexec-tools"]]),
        ("dnf", [["dnf", "install", "-y", "kexec-tools"]]),
        ("yum", [["yum", "install", "-y", "kexec-tools"]]),
        ("zypper", [["zypper", "--non-interactive", "install", "kexec-tools"]]),
        ("pacman", [["pacman", "-Sy", "--noconfirm", "kexec-tools"]]),
    ]
    env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
    for mgr, cmds in managers:
        if not have(mgr):
            continue
        info(f"パッケージマネージャ: {mgr}")
        for cmd in cmds:
            run(cmd, env=env)
        break
    else:
        die("対応するパッケージマネージャが見つからない。手動で kexec-tools を入れてから再実行。")

    if not have("kexec"):
        die("kexec の導入に失敗した。")


# ---------------------------------------------------------------------------
# 2. パスワードハッシュ（crypt が無い環境では openssl にフォールバック）
# ---------------------------------------------------------------------------
def hash_password(plain: str) -> str:
    """SHA-512 crypt ($6$...) を返す。"""
    try:
        import crypt  # Python 3.13 で削除されたため try
        return crypt.crypt(plain, crypt.mksalt(crypt.METHOD_SHA512))
    except Exception:
        pass
    if have("openssl"):
        out = subprocess.run(
            ["openssl", "passwd", "-6", "-stdin"],
            input=plain.encode(), check=True, capture_output=True,
        )
        return out.stdout.decode().strip()
    die("パスワードハッシュ生成手段が無い（crypt モジュールも openssl も不在）。")


# ---------------------------------------------------------------------------
# 3. 対話プロンプト
# ---------------------------------------------------------------------------
def prompt_nonempty(label: str, default: str = "") -> str:
    while True:
        suffix = f" [{default}]" if default else ""
        val = input(f"{label}{suffix}: ").strip()
        if not val and default:
            return default
        if val:
            return val
        print("  空にはできません。")


def prompt_password(label: str) -> str:
    while True:
        p1 = getpass(f"{label}: ")
        if not p1:
            print("  空にはできません。")
            continue
        p2 = getpass(f"{label}（確認）: ")
        if p1 != p2:
            print("  一致しません。やり直してください。")
            continue
        return p1


def prompt_pubkey(label: str) -> str:
    """SSH 公開鍵を 1 行で受け取る。'@/path' でファイルからも読める。"""
    while True:
        val = input(f"{label}（'@/path/to/key.pub' でファイル指定可）: ").strip()
        if val.startswith("@"):
            path = os.path.expanduser(val[1:])
            try:
                with open(path, encoding="utf-8") as fh:
                    val = fh.read().strip()
            except OSError as exc:
                print(f"  読み込み失敗: {exc}")
                continue
        if val.split(" ", 1)[0] in (
            "ssh-ed25519", "ssh-rsa", "ssh-dss",
            "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521",
            "sk-ssh-ed25519@openssh.com", "sk-ecdsa-sha2-nistp256@openssh.com",
        ):
            return val
        print("  SSH 公開鍵の形式に見えません（先頭が ssh-ed25519 等であること）。")


def detect_disk() -> str:
    """上書き対象ディスクを推測する。"""
    for cand in ("/dev/vda", "/dev/sda", "/dev/nvme0n1"):
        try:
            if os.path.exists(cand) and os.stat(cand).st_mode & 0o170000 == 0o060000:
                return cand
        except OSError:
            continue
    return ""


# ---------------------------------------------------------------------------
# 4. preseed / payload 生成
# ---------------------------------------------------------------------------
def build_preseed(cfg: dict) -> bytes:
    text = f"""\
# ---- ロケール / キーボード -------------------------------------------------
d-i debian-installer/locale string en_US.UTF-8
d-i keyboard-configuration/xkb-keymap select us

# ---- ネットワーク（DHCP 前提） --------------------------------------------
d-i netcfg/choose_interface select auto
d-i netcfg/hostname string {cfg['hostname']}
d-i netcfg/get_hostname string {cfg['hostname']}
d-i netcfg/get_domain string unassigned-domain

# ---- ミラー ----------------------------------------------------------------
d-i mirror/country string manual
d-i mirror/http/hostname string {MIRROR_HOST}
d-i mirror/http/directory string {MIRROR_DIR}
d-i mirror/http/proxy string

# ---- 時刻 ------------------------------------------------------------------
d-i clock-setup/utc boolean true
d-i time/zone string {TIMEZONE}
d-i clock-setup/ntp boolean true

# ---- パーティショニング（対象ディスク決め打ち + LVM atomic） --------------
d-i partman-auto/method string lvm
d-i partman-auto/disk string {cfg['disk']}
d-i partman-lvm/device_remove_lvm boolean true
d-i partman-md/device_remove_md boolean true
d-i partman-lvm/confirm boolean true
d-i partman-lvm/confirm_nooverwrite boolean true
d-i partman-auto-lvm/guided_size string max
d-i partman-auto/choose_recipe select atomic
d-i partman/confirm_write_new_label boolean true
d-i partman/choose_partition select finish
d-i partman/confirm boolean true
d-i partman/confirm_nooverwrite boolean true

# ---- ユーザー（root 無効、メインユーザーをハッシュ PW で作成） ------------
d-i passwd/root-login boolean false
d-i passwd/user-fullname string {cfg['fullname']}
d-i passwd/username string {cfg['username']}
d-i passwd/user-password-crypted password {cfg['user_pw_hash']}

# ---- パッケージ選択（openssh-server を必ず入れる） ------------------------
tasksel tasksel/first multiselect standard
d-i pkgsel/include string openssh-server sudo curl ca-certificates gnupg
d-i pkgsel/upgrade select full-upgrade
d-i pkgsel/update-policy select none
popularity-contest popularity-contest/participate boolean false

# ---- GRUB（対象ディスクへ無人インストール） -------------------------------
d-i grub-installer/only_debian boolean true
d-i grub-installer/with_other_os boolean true
d-i grub-installer/bootdev string {cfg['disk']}

# ---- 完了 ------------------------------------------------------------------
d-i finish-install/reboot_in_progress note

# ---- インストール直後処理 -------------------------------------------------
# initrd 内に同梱した /payload を新システムへ展開し、first-boot サービスと
# sshd ハードニング設定を仕込む。Docker/Tailscale/ansible ユーザーは
# 初回起動後の first-boot 側で実施する。
d-i preseed/late_command string \\
    mkdir -p /target/var/lib/bootstrap ; \\
    cp -r /payload/. /target/var/lib/bootstrap/ ; \\
    cp /payload/firstboot.service /target/etc/systemd/system/firstboot.service ; \\
    mkdir -p /target/etc/ssh/sshd_config.d ; \\
    cp /payload/sshd_hardening.conf /target/etc/ssh/sshd_config.d/00-bootstrap.conf ; \\
    in-target chmod 0755 /var/lib/bootstrap/firstboot.sh ; \\
    in-target chmod 0600 /var/lib/bootstrap/bootstrap.env ; \\
    in-target systemctl enable firstboot.service ; \\
    in-target usermod -aG sudo {cfg['username']}
"""
    return text.encode("utf-8")


# first-boot 本体。初回起動後（ネットワーク・apt が完全に動く状態）で一度だけ走る。
# bash スクリプト。値は bootstrap.env から実行時に読み込むのでビルド時置換は不要。
FIRSTBOOT_SH = r"""#!/bin/bash
# 初回起動時に一度だけ実行される。Docker / Tailscale 導入と
# メインユーザーの SSH 鍵設置、ansible ユーザーの別途作成を行う。
set -euo pipefail
exec > /var/log/firstboot.log 2>&1
echo "=== firstboot start: $(date -u) ==="

# shellcheck disable=SC1091
. /var/lib/bootstrap/bootstrap.env   # MAIN_USER / ANSIBLE_USER / ANSIBLE_PW_HASH

export DEBIAN_FRONTEND=noninteractive

# ネットワーク待ち（保険）
for _ in $(seq 1 30); do
    if getent hosts deb.debian.org >/dev/null 2>&1; then break; fi
    sleep 2
done

apt-get update
apt-get install -y ca-certificates curl gnupg sudo

# --- メインユーザーの SSH 公開鍵を設置 -------------------------------------
if id "$MAIN_USER" >/dev/null 2>&1; then
    install -d -m700 -o "$MAIN_USER" -g "$MAIN_USER" "/home/$MAIN_USER/.ssh"
    install -m600 -o "$MAIN_USER" -g "$MAIN_USER" \
        /var/lib/bootstrap/user_authorized_keys "/home/$MAIN_USER/.ssh/authorized_keys"
fi

# --- Docker（公式 apt リポジトリ） -----------------------------------------
install -m0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
# shellcheck disable=SC1091
. /etc/os-release
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker

# --- Tailscale（インストールのみ。参加は手動 `tailscale up`） ---------------
curl -fsSL https://tailscale.com/install.sh | sh
systemctl enable --now tailscaled || true

# --- ansible ユーザーを別途作成（SSH 鍵 + 通常 sudo） ----------------------
if ! id "$ANSIBLE_USER" >/dev/null 2>&1; then
    useradd -m -s /bin/bash "$ANSIBLE_USER"
fi
echo "${ANSIBLE_USER}:${ANSIBLE_PW_HASH}" | chpasswd -e
usermod -aG sudo "$ANSIBLE_USER"
usermod -aG docker "$ANSIBLE_USER"
install -d -m700 -o "$ANSIBLE_USER" -g "$ANSIBLE_USER" "/home/$ANSIBLE_USER/.ssh"
install -m600 -o "$ANSIBLE_USER" -g "$ANSIBLE_USER" \
    /var/lib/bootstrap/ansible_authorized_keys "/home/$ANSIBLE_USER/.ssh/authorized_keys"

# --- メインユーザーも docker グループへ ------------------------------------
usermod -aG docker "$MAIN_USER" || true

# --- 後始末（自分自身を無効化し、秘密情報を消す） -------------------------
systemctl disable firstboot.service || true
rm -f /etc/systemd/system/firstboot.service
shred -u /var/lib/bootstrap/bootstrap.env 2>/dev/null || rm -f /var/lib/bootstrap/bootstrap.env
echo "=== firstboot done: $(date -u) ==="
"""

FIRSTBOOT_SERVICE = """\
[Unit]
Description=First boot bootstrap (docker, tailscale, ansible user)
After=network-online.target
Wants=network-online.target
ConditionPathExists=/var/lib/bootstrap/firstboot.sh

[Service]
Type=oneshot
ExecStart=/var/lib/bootstrap/firstboot.sh
RemainAfterExit=no

[Install]
WantedBy=multi-user.target
"""

SSHD_HARDENING = """\
# debian-vps-bootstrap が設置。SSH は公開鍵のみ。
# パスワードはローカル(VNC)コンソール救済用に残すが、SSH 経由では拒否する。
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
PubkeyAuthentication yes
"""


# ---------------------------------------------------------------------------
# 5. cpio (newc) 操作 ― 標準ライブラリのみ
# ---------------------------------------------------------------------------
def _h(v: int) -> bytes:
    return b"%08X" % (v & 0xFFFFFFFF)


def _cpio_entry(name: str, mode: int, data: bytes = b"", ino: int = 0, nlink: int = 1) -> bytes:
    nb = name.encode("utf-8") + b"\x00"
    hdr = (
        b"070701" + _h(ino) + _h(mode) + _h(0) + _h(0) + _h(nlink) + _h(0)
        + _h(len(data)) + _h(0) + _h(0) + _h(0) + _h(0) + _h(len(nb)) + _h(0)
    )
    out = hdr + nb
    out += b"\x00" * ((-len(out)) % 4)   # ヘッダ+名前を 4 byte 境界へ
    out += data
    out += b"\x00" * ((-len(data)) % 4)  # データを 4 byte 境界へ
    return out


def _find_trailer(cpio: bytes) -> int:
    """newc cpio を順次パースして TRAILER!!! エントリの開始オフセットを返す。"""
    off, n = 0, len(cpio)
    while off < n:
        if cpio[off:off + 6] not in (b"070701", b"070702"):
            raise ValueError(f"cpio: 不正なマジック @ {off}")
        filesize = int(cpio[off + 6 + 6 * 8: off + 6 + 7 * 8], 16)
        namesize = int(cpio[off + 6 + 11 * 8: off + 6 + 12 * 8], 16)
        name = cpio[off + 110: off + 110 + namesize - 1]
        hdrname = 110 + namesize
        data_start = off + hdrname + ((-hdrname) % 4)
        if name == b"TRAILER!!!":
            return off
        off = data_start + filesize + ((-filesize) % 4)
    raise ValueError("cpio: TRAILER が見つからない")


def build_new_initrd(orig_gz: bytes, preseed: bytes, payload: dict) -> bytes:
    """元 initrd.gz を展開し、preseed.cfg と /payload/* を追記して再圧縮する。"""
    cpio = gzip.decompress(orig_gz)
    trailer_off = _find_trailer(cpio)
    new = bytearray(cpio[:trailer_off])  # 既存 TRAILER の手前まで

    ino = 0x300000
    # payload ディレクトリ
    new += _cpio_entry("payload", 0o040755, b"", ino); ino += 1
    # payload 配下のファイル（秘密は 0600）
    secret = {"payload/bootstrap.env"}
    for name in sorted(payload):
        mode = 0o100755 if name.endswith(".sh") else (0o100600 if name in secret else 0o100644)
        new += _cpio_entry(name, mode, payload[name], ino); ino += 1
    # preseed 本体（d-i がルートの /preseed.cfg を自動で読む）
    new += _cpio_entry("preseed.cfg", 0o100644, preseed, ino); ino += 1
    # 新しい TRAILER + 512 byte パディング
    new += _cpio_entry("TRAILER!!!", 0, b"", 0)
    new += b"\x00" * ((-len(new)) % 512)

    return gzip.compress(bytes(new), 9)


# ---------------------------------------------------------------------------
# 6. ダウンロード
# ---------------------------------------------------------------------------
def download(url: str) -> bytes:
    info(f"download: {url}")
    with urllib.request.urlopen(url) as resp:  # noqa: S310 (信頼できる Debian ミラー)
        return resp.read()


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main() -> None:
    if os.geteuid() != 0:
        die("root で実行してください。")
    if ARCH is None:
        die(f"未対応アーキテクチャ: {platform.machine()}（amd64/arm64 のみ対応）")

    print("=" * 70)
    print(" debian-vps-bootstrap  ―  この VPS のディスクを Debian で上書きします")
    print("=" * 70)

    ensure_prereqs()

    # --- 対話入力 ----------------------------------------------------------
    hostname = prompt_nonempty("ホスト名", "debian-vps")
    username = prompt_nonempty("メインユーザー名")
    fullname = prompt_nonempty("メインユーザーのフルネーム", username)
    user_pw = prompt_password("メインユーザーのパスワード（VNC コンソール救済用）")
    user_key = prompt_pubkey("メインユーザーの SSH 公開鍵")

    ansible_user = prompt_nonempty("ansible ユーザー名", "ansible")
    ansible_pw = prompt_password(f"{ansible_user} のパスワード（sudo 用）")
    ansible_key = prompt_pubkey(f"{ansible_user} の SSH 公開鍵")

    detected = detect_disk()
    disk = prompt_nonempty("上書き対象ディスク", detected or "/dev/vda")

    # 参考情報として現在のブロックデバイスを表示
    if have("lsblk"):
        print("\n--- 現在のブロックデバイス ---")
        subprocess.run(["lsblk", "-do", "NAME,SIZE,MODEL"], check=False)
        print("-----------------------------\n")

    # --- 最終確認（破壊的操作） -------------------------------------------
    print("以下の内容で実行します:")
    print(f"  ホスト名        : {hostname}")
    print(f"  メインユーザー  : {username} (SSH 鍵のみ / sudo)")
    print(f"  ansible ユーザー: {ansible_user} (SSH 鍵 / 通常 sudo, first-boot で作成)")
    print(f"  対象ディスク    : {disk}")
    print(f"  Debian          : {SUITE} ({ARCH})")
    print(f"  Docker/Tailscale: 初回起動後に自動導入（tailscale up は手動）")
    print()
    print(f"!!! {disk} の全データが消去されます。元の OS は復旧できません。!!!")
    if input(f"続行するには対象ディスク名をそのまま入力してください ({disk}): ").strip() != disk:
        die("確認が一致しないため中止しました。")

    user_pw_hash = hash_password(user_pw)
    ansible_pw_hash = hash_password(ansible_pw)

    # --- payload とブートイメージの組み立て --------------------------------
    bootstrap_env = (
        f"MAIN_USER={username}\n"
        f"ANSIBLE_USER={ansible_user}\n"
        f"ANSIBLE_PW_HASH='{ansible_pw_hash}'\n"
    )
    payload = {
        "payload/firstboot.sh": FIRSTBOOT_SH.encode(),
        "payload/firstboot.service": FIRSTBOOT_SERVICE.encode(),
        "payload/sshd_hardening.conf": SSHD_HARDENING.encode(),
        "payload/bootstrap.env": bootstrap_env.encode(),
        "payload/user_authorized_keys": (user_key + "\n").encode(),
        "payload/ansible_authorized_keys": (ansible_key + "\n").encode(),
    }
    cfg = {
        "hostname": hostname,
        "username": username,
        "fullname": fullname,
        "user_pw_hash": user_pw_hash,
        "disk": disk,
    }

    os.makedirs(WORKDIR, exist_ok=True)
    kernel_path = os.path.join(WORKDIR, "linux")
    initrd_path = os.path.join(WORKDIR, "new_initrd.gz")

    with open(kernel_path, "wb") as fh:
        fh.write(download(KERNEL_URL))

    orig_initrd = download(INITRD_URL)
    preseed = build_preseed(cfg)
    info("preseed を埋め込んだ initrd を生成中")
    new_initrd = build_new_initrd(orig_initrd, preseed, payload)
    with open(initrd_path, "wb") as fh:
        fh.write(new_initrd)
    info(f"initrd 生成完了: {initrd_path} ({len(new_initrd)} bytes)")

    # --- kexec ロード & 実行 ----------------------------------------------
    run([
        "kexec", "-l", kernel_path,
        f"--initrd={initrd_path}",
        f"--append={KERNEL_CMDLINE}",
    ])

    info("ディスクキャッシュを同期")
    os.sync()
    try:
        with open("/proc/sys/vm/drop_caches", "w") as fh:
            fh.write("3\n")
    except OSError:
        pass

    print("\nkexec -e を実行します。ここから先は新カーネル（インストーラ）へ遷移します。")
    print("インストール完了後、システムは自動で再起動し Debian が起動します。")
    print("初回起動時に Docker / Tailscale 導入と ansible ユーザー作成が走ります")
    print("（ログ: /var/log/firstboot.log）。")
    run(["kexec", "-e"])  # 通常ここで戻らない


if __name__ == "__main__":
    main()
