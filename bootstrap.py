#!/usr/bin/env python3
"""
debian-vps-bootstrap
====================
ISO マウントができない VPS で、稼働中の OS を kexec で netboot インストーラに
すり替え、preseed で無人インストールしてディスクを Debian で上書きする。

設定ファイル(TOML)と実行を分離した 3 つのサブコマンドで動く:

    bootstrap.py wizard [-o config.toml]   対話で設定ファイルを書き出す
    bootstrap.py check  [-c config.toml]   設定を検証し生成物を確認(ドライラン)
    bootstrap.py run    [-c config.toml]   設定を読んでインストールを実行(破壊的)

特徴:
  - Python3 標準ライブラリのみで完結（cpio / gzip / ダウンロード自前処理）。
    外部コマンド依存は実質 kexec のみ。
  - kexec が無ければ run 時に自動導入（apt/dnf/yum/zypper/pacman を判別）。
  - Docker / Tailscale / ansible ユーザー作成は「初回起動後の systemd oneshot」で実行。

必ず root で、上書きしてよい VPS 上で `run` すること。実行すると元の OS は消える。
"""

import argparse
import copy
import gzip
import os
import platform
import shutil
import socket
import struct
import subprocess
import sys
import urllib.request
from getpass import getpass

WORKDIR = "/root/debian-bootstrap"
_ARCH_MAP = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}
FALLBACK_SUITE = "trixie"     # 稼働中が Debian でない等でコードネームを取れない場合
FALLBACK_TIMEZONE = "Etc/UTC"
FALLBACK_LOCALE = "en_US.UTF-8"


# ===========================================================================
# 共通ユーティリティ
# ===========================================================================
def die(msg: str) -> "None":
    print(f"\n[FATAL] {msg}", file=sys.stderr)
    sys.exit(1)


def info(msg: str) -> None:
    print(f"[*] {msg}")


def run_cmd(cmd, **kw) -> subprocess.CompletedProcess:
    info("$ " + " ".join(cmd))
    return subprocess.run(cmd, check=True, **kw)


def have(binary: str) -> bool:
    return shutil.which(binary) is not None


def resolve_arch(value: str) -> str:
    if value and value != "auto":
        if value not in ("amd64", "arm64"):
            die(f"未対応アーキテクチャ指定: {value}")
        return value
    arch = _ARCH_MAP.get(platform.machine().lower())
    if arch is None:
        die(f"アーキテクチャ自動判別に失敗: {platform.machine()}（amd64/arm64 のみ対応）")
    return arch


# --- 稼働中の Linux からシステム設定（コードネーム/TZ/ロケール）を読み取る ---
def _os_release() -> dict:
    out = {}
    try:
        with open("/etc/os-release") as fh:
            for line in fh:
                if "=" in line:
                    k, v = line.rstrip("\n").split("=", 1)
                    out[k] = v.strip().strip('"')
    except OSError:
        pass
    return out


def detect_suite() -> str:
    """稼働中が Debian ならそのコードネームを返す。それ以外/不明なら空。"""
    d = _os_release()
    return d.get("VERSION_CODENAME", "") if d.get("ID") == "debian" else ""


def detect_timezone() -> str:
    try:
        with open("/etc/timezone") as fh:
            tz = fh.read().strip()
        if tz:
            return tz
    except OSError:
        pass
    try:
        link = os.readlink("/etc/localtime")
        if "zoneinfo/" in link:
            return link.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    return ""


def _meaningful_locale(v: str) -> bool:
    # C / POSIX / C.UTF-8 は「地域ロケール未選択」。無人インストールでは既定へ倒す。
    return bool(v) and v not in ("C", "POSIX") and not v.upper().startswith("C.") and "." in v


def detect_locale() -> str:
    for var in ("LANG", "LC_ALL", "LC_CTYPE"):
        v = os.environ.get(var, "")
        if _meaningful_locale(v):
            return v
    for path in ("/etc/default/locale", "/etc/locale.conf"):
        try:
            with open(path) as fh:
                for line in fh:
                    line = line.strip()
                    if line.startswith("LANG="):
                        v = line.split("=", 1)[1].strip().strip('"')
                        if _meaningful_locale(v):
                            return v
        except OSError:
            continue
    return ""


def resolve_config(cfg: dict) -> dict:
    """debian.suite/arch/timezone/locale の "auto" を具体値へ解決した cfg のコピーを返す。"""
    rcfg = copy.deepcopy(cfg)
    d = rcfg["debian"]
    d["arch"] = resolve_arch(d.get("arch", "auto"))
    if d.get("suite", "auto") == "auto":
        d["suite"] = detect_suite() or FALLBACK_SUITE
    if d.get("timezone", "auto") == "auto":
        d["timezone"] = detect_timezone() or FALLBACK_TIMEZONE
    if d.get("locale", "auto") == "auto":
        d["locale"] = detect_locale() or FALLBACK_LOCALE
    return rcfg


def detect_disk() -> str:
    for cand in ("/dev/vda", "/dev/sda", "/dev/nvme0n1"):
        try:
            if os.path.exists(cand) and os.stat(cand).st_mode & 0o170000 == 0o060000:
                return cand
        except OSError:
            continue
    return ""


# --- 稼働中の Linux からネットワーク設定を読み取る（標準ライブラリのみ） -----
def _ipv4_default_route():
    """デフォルトルートの (インタフェース名, ゲートウェイ) を /proc から得る。"""
    try:
        with open("/proc/net/route") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return None, None
    for line in lines[1:]:
        f = line.split()
        if len(f) < 4:
            continue
        iface, dest, gw, flags = f[0], f[1], f[2], int(f[3], 16)
        if dest == "00000000" and (flags & 0x2):  # デフォルト経路 & RTF_GATEWAY
            return iface, socket.inet_ntoa(struct.pack("<L", int(gw, 16)))
    return None, None


def _ipv4_addr_mask(iface: str):
    """インタフェースの IPv4 アドレスとネットマスクを ioctl で得る。"""
    try:
        import fcntl  # Unix 専用（標準ライブラリ）
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        ifr = struct.pack("256s", iface.encode()[:15])
        addr = socket.inet_ntoa(fcntl.ioctl(s.fileno(), 0x8915, ifr)[20:24])  # SIOCGIFADDR
        mask = socket.inet_ntoa(fcntl.ioctl(s.fileno(), 0x891B, ifr)[20:24])  # SIOCGIFNETMASK
        return addr, mask
    except Exception:
        return None, None


def _nameservers():
    """resolv.conf から上流 DNS を得る。systemd-resolved のスタブ(127.x)は除外。"""
    ns: list = []
    for path in ("/run/systemd/resolve/resolv.conf", "/etc/resolv.conf"):
        try:
            with open(path) as fh:
                for line in fh:
                    line = line.strip()
                    if line.startswith("nameserver"):
                        parts = line.split()
                        if len(parts) >= 2 and ":" not in parts[1] and not parts[1].startswith("127."):
                            ns.append(parts[1])
        except OSError:
            continue
        if ns:
            break
    seen, out = set(), []
    for x in ns:
        if x not in seen:
            seen.add(x); out.append(x)
    return out


def detect_network():
    """現在の IPv4 実効設定を返す。取れなければ None。"""
    iface, gw = _ipv4_default_route()
    if not iface:
        return None
    addr, mask = _ipv4_addr_mask(iface)
    if not addr or not mask:
        return None
    return {"interface": iface, "address": addr, "netmask": mask,
            "gateway": gw or "", "nameservers": _nameservers()}


def _netmask_to_prefix(mask: str) -> int:
    return sum(bin(int(o)).count("1") for o in mask.split("."))


def _prefix_to_netmask(prefix) -> str:
    m = (0xFFFFFFFF << (32 - int(prefix))) & 0xFFFFFFFF
    return socket.inet_ntoa(struct.pack(">L", m))


def _finalize_static(p: dict) -> dict:
    """static パラメータを正規化（CIDR 分解・prefix→netmask・DNS 既定）。"""
    addr, mask = p.get("address", ""), p.get("netmask", "")
    if "/" in addr:
        addr, cidr = addr.split("/", 1)
        mask = mask or _prefix_to_netmask(cidr)
    if not mask and p.get("prefix"):
        mask = _prefix_to_netmask(p["prefix"])
    if mask and mask.isdigit() and int(mask) <= 32:
        mask = _prefix_to_netmask(mask)
    ns = p.get("nameservers") or ([p["gateway"]] if p.get("gateway") else ["1.1.1.1", "8.8.8.8"])
    return {"interface": p.get("interface", ""), "address": addr, "netmask": mask,
            "gateway": p.get("gateway", ""), "nameservers": ns}


def resolve_network(cfg: dict):
    """設定とホスト状態から ('dhcp', {}) か ('static', params) を返す。"""
    net = cfg["network"]
    mode = net.get("mode", "auto")
    if mode == "dhcp":
        return "dhcp", {}
    if mode == "auto":
        det = detect_network()
        if det and det["address"] and det["gateway"]:
            return "static", _finalize_static(det)
        return "dhcp", {}
    if mode == "static":
        return "static", _finalize_static(net)
    die(f"network.mode が不正: {mode}（dhcp/static/auto）")


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


def ensure_prereqs() -> None:
    """kexec が無ければパッケージマネージャを自動判別して導入する。"""
    if have("kexec"):
        info("kexec は導入済み")
        return
    info("kexec が無いので導入を試みる")
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
            run_cmd(cmd, env=env)
        break
    else:
        die("対応するパッケージマネージャが無い。手動で kexec-tools を入れて再実行。")
    if not have("kexec"):
        die("kexec の導入に失敗した。")


# ===========================================================================
# 設定（TOML 読み書き）
# ===========================================================================
def default_config() -> dict:
    return {
        "debian": {
            "suite": "auto",        # auto = 稼働中システムのコードネーム（取れなければ trixie）
            "arch": "auto",         # auto = uname から判別
            "locale": "auto",       # auto = 稼働中システムの LANG（取れなければ en_US.UTF-8）
            "timezone": "auto",     # auto = 稼働中システムの TZ（取れなければ Etc/UTC）
            "keymap": "us",         # VPS では基本このまま。必要なら手で変更
            "mirror_host": "deb.debian.org",
            "mirror_dir": "/debian",
        },
        "target": {"disk": "auto"},
        "network": {
            "mode": "auto",        # auto | static | dhcp
            "interface": "",       # 以下は mode = "static" のとき使用
            "address": "",
            "netmask": "",
            "gateway": "",
            "nameservers": [],
        },
        "host": {"hostname": "debian-vps"},
        "user": {
            "name": "admin",
            "fullname": "Admin User",
            "password_hash": "prompt",      # "prompt" or "$6$..."
            "ssh_authorized_keys": [],
        },
        "ansible": {
            "enabled": True,
            "name": "ansible",
            "password_hash": "prompt",
            "ssh_authorized_keys": [],
        },
        "packages": {
            "include": ["openssh-server", "sudo", "curl", "ca-certificates", "gnupg"],
        },
        "firstboot": {
            "docker": True,
            "tailscale": True,
            "apt_packages": [],
            "run": [],
        },
        "ssh": {
            "password_authentication": False,
            "permit_root_login": False,
        },
    }


def deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_config(path: str) -> dict:
    if not os.path.exists(path):
        die(f"設定ファイルが見つからない: {path}（先に `wizard` で生成してください）")
    with open(path, "rb") as fh:
        raw = fh.read()
    try:
        import tomllib  # Python 3.11+
        loaded = tomllib.loads(raw.decode("utf-8"))
    except ModuleNotFoundError:
        loaded = _toml_loads_fallback(raw.decode("utf-8"))
    return deep_merge(default_config(), loaded)


# --- 自前ミニ TOML パーサ（tomllib が無い古い Python 用フォールバック） -------
def _toml_strip_comment(s: str) -> str:
    out, q, esc = [], False, False
    for ch in s:
        if esc:
            out.append(ch); esc = False; continue
        if ch == "\\" and q:
            out.append(ch); esc = True; continue
        if ch == '"':
            q = not q; out.append(ch); continue
        if ch == "#" and not q:
            break
        out.append(ch)
    return "".join(out)


def _toml_unescape(token: str) -> str:
    token = token.strip()
    if token.startswith('"') and token.endswith('"'):
        token = token[1:-1]
    return (token.replace('\\"', '"').replace("\\\\", "\\")
            .replace("\\n", "\n").replace("\\t", "\t"))


def _toml_split_array(inner: str):
    items, buf, q, esc, depth = [], [], False, False, 0
    for ch in inner:
        if esc:
            buf.append(ch); esc = False; continue
        if ch == "\\" and q:
            buf.append(ch); esc = True; continue
        if ch == '"':
            q = not q; buf.append(ch); continue
        if not q and ch == "[":
            depth += 1; buf.append(ch); continue
        if not q and ch == "]":
            depth -= 1; buf.append(ch); continue
        if ch == "," and not q and depth == 0:
            items.append("".join(buf)); buf = []; continue
        buf.append(ch)
    if "".join(buf).strip():
        items.append("".join(buf))
    return items


def _toml_value(v: str):
    v = v.strip()
    if v.startswith("["):
        inner = v[1:v.rindex("]")]
        return [_toml_value(x) for x in _toml_split_array(inner)]
    if v.startswith('"'):
        return _toml_unescape(v)
    if v == "true":
        return True
    if v == "false":
        return False
    try:
        return int(v)
    except ValueError:
        return v.strip('"')


def _toml_loads_fallback(text: str) -> dict:
    data: dict = {}
    cur = data
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = _toml_strip_comment(lines[i]).strip()
        i += 1
        if not line:
            continue
        if line.startswith("["):
            name = line.strip("[]").strip()
            cur = data
            for part in name.split("."):
                cur = cur.setdefault(part.strip(), {})
            continue
        if "=" not in line:
            raise ValueError(f"TOML 解析エラー: {line!r}")
        key, val = line.split("=", 1)
        key, val = key.strip().strip('"'), val.strip()
        # 複数行に渡る配列を連結
        if val.startswith("[") and val.count("[") > val.count("]"):
            buf = [val]
            while i < len(lines):
                buf.append(_toml_strip_comment(lines[i]))
                i += 1
                joined = " ".join(buf)
                if joined.count("[") <= joined.count("]"):
                    break
            val = " ".join(buf).strip()
        cur[key] = _toml_value(val)
    return data


# --- TOML 書き出し（wizard 用、コメント付きテンプレート） --------------------
def _toml_array(keys) -> str:
    if not keys:
        return "[]"
    body = "".join(f'  "{k}",\n' for k in keys)
    return "[\n" + body + "]"


def dump_config(cfg: dict) -> str:
    d, u, a = cfg["debian"], cfg["user"], cfg["ansible"]
    fb, pk, ssh, n = cfg["firstboot"], cfg["packages"], cfg["ssh"], cfg["network"]
    return f"""\
# debian-vps-bootstrap 設定ファイル
# `bootstrap.py run -c このファイル` でこの内容に従ってインストールする。
# パスワードは平文では保存しない。"prompt" にすると run 実行時に対話入力する。

# suite/locale/timezone は "auto" で稼働中システムから取得（取れなければ既定値）。
[debian]
suite = "{d['suite']}"              # auto | trixie | bookworm ...
arch = "{d['arch']}"                  # auto | amd64 | arm64
locale = "{d['locale']}"            # auto | en_US.UTF-8 | ja_JP.UTF-8 ...
timezone = "{d['timezone']}"          # auto | Asia/Tokyo | Etc/UTC ...
keymap = "{d['keymap']}"                 # VPS では基本このまま
mirror_host = "{d['mirror_host']}"
mirror_dir = "{d['mirror_dir']}"

[target]
disk = "{cfg['target']['disk']}"               # auto で自動検出（実行時に確認あり）

# ネットワーク。auto は run/check 実行時に「現在の Linux の IPv4 設定」を読み取り、
# 取得できれば static として焼き込む（取れなければ dhcp にフォールバック）。
[network]
mode = "{n['mode']}"               # auto | static | dhcp
interface = "{n['interface']}"             # mode = "static" 用（空なら自動選択）
address = "{n['address']}"
netmask = "{n['netmask']}"
gateway = "{n['gateway']}"
nameservers = {_toml_array(n['nameservers'])}

[host]
hostname = "{cfg['host']['hostname']}"

# メインユーザー: SSH は公開鍵のみ。パスワードは VNC コンソール救済用。
[user]
name = "{u['name']}"
fullname = "{u['fullname']}"
password_hash = "{u['password_hash']}"
ssh_authorized_keys = {_toml_array(u['ssh_authorized_keys'])}

# 自動化用ユーザー: 初回起動後に別途作成。SSH 鍵 + 通常 sudo。
[ansible]
enabled = {str(a['enabled']).lower()}
name = "{a['name']}"
password_hash = "{a['password_hash']}"
ssh_authorized_keys = {_toml_array(a['ssh_authorized_keys'])}

# d-i 段で入れる最小パッケージ（standard タスク + これら）
[packages]
include = {_toml_array(pk['include'])}

# 初回起動後に行う設定
[firstboot]
docker = {str(fb['docker']).lower()}
tailscale = {str(fb['tailscale']).lower()}
apt_packages = {_toml_array(fb['apt_packages'])}
run = {_toml_array(fb['run'])}

[ssh]
password_authentication = {str(ssh['password_authentication']).lower()}
permit_root_login = {str(ssh['permit_root_login']).lower()}
"""


# ===========================================================================
# preseed / firstboot 生成
# ===========================================================================
def _build_netcfg(cfg: dict, net) -> str:
    host = cfg["host"]["hostname"]
    mode, p = net
    common = (
        f"d-i netcfg/hostname string {host}\n"
        f"d-i netcfg/get_hostname string {host}\n"
        "d-i netcfg/get_domain string unassigned-domain\n"
    )
    if mode == "static":
        ns = " ".join(p["nameservers"])
        return (
            "# ---- ネットワーク（稼働中の設定から静的化） -----------------------------\n"
            "d-i netcfg/choose_interface select auto\n"
            "d-i netcfg/disable_autoconfig boolean true\n"
            f"d-i netcfg/get_ipaddress string {p['address']}\n"
            f"d-i netcfg/get_netmask string {p['netmask']}\n"
            f"d-i netcfg/get_gateway string {p['gateway']}\n"
            f"d-i netcfg/get_nameservers string {ns}\n"
            "d-i netcfg/confirm_static boolean true\n"
            + common
        )
    return (
        "# ---- ネットワーク（DHCP） -------------------------------------------------\n"
        "d-i netcfg/choose_interface select auto\n"
        + common
    )


def build_preseed(cfg: dict, disk: str, user_pw_hash: str, net) -> bytes:
    d, u = cfg["debian"], cfg["user"]
    include = " ".join(cfg["packages"]["include"])
    netcfg = _build_netcfg(cfg, net)
    text = f"""\
# ---- ロケール / キーボード -------------------------------------------------
d-i debian-installer/locale string {d['locale']}
d-i keyboard-configuration/xkb-keymap select {d['keymap']}

{netcfg}
# ---- ミラー ----------------------------------------------------------------
d-i mirror/country string manual
d-i mirror/http/hostname string {d['mirror_host']}
d-i mirror/http/directory string {d['mirror_dir']}
d-i mirror/http/proxy string

# ---- 時刻 ------------------------------------------------------------------
d-i clock-setup/utc boolean true
d-i time/zone string {d['timezone']}
d-i clock-setup/ntp boolean true

# ---- パーティショニング（対象ディスク決め打ち + LVM atomic） --------------
d-i partman-auto/method string lvm
d-i partman-auto/disk string {disk}
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
d-i passwd/user-fullname string {u['fullname']}
d-i passwd/username string {u['name']}
d-i passwd/user-password-crypted password {user_pw_hash}

# ---- パッケージ選択（openssh-server を必ず入れる） ------------------------
tasksel tasksel/first multiselect standard
d-i pkgsel/include string {include}
d-i pkgsel/upgrade select full-upgrade
d-i pkgsel/update-policy select none
popularity-contest popularity-contest/participate boolean false

# ---- GRUB（対象ディスクへ無人インストール） -------------------------------
d-i grub-installer/only_debian boolean true
d-i grub-installer/with_other_os boolean true
d-i grub-installer/bootdev string {disk}

# ---- 完了 ------------------------------------------------------------------
d-i finish-install/reboot_in_progress note

# ---- インストール直後処理 -------------------------------------------------
d-i preseed/late_command string \\
    mkdir -p /target/var/lib/bootstrap ; \\
    cp -r /payload/. /target/var/lib/bootstrap/ ; \\
    cp /payload/firstboot.service /target/etc/systemd/system/firstboot.service ; \\
    mkdir -p /target/etc/ssh/sshd_config.d ; \\
    cp /payload/sshd_hardening.conf /target/etc/ssh/sshd_config.d/00-bootstrap.conf ; \\
    in-target chmod 0755 /var/lib/bootstrap/firstboot.sh ; \\
    in-target systemctl enable firstboot.service ; \\
    in-target usermod -aG sudo {u['name']}
"""
    return text.encode("utf-8")


_FB_HEADER = """\
#!/bin/bash
# debian-vps-bootstrap: 初回起動時に一度だけ実行される。
set -euo pipefail
exec > /var/log/firstboot.log 2>&1
echo "=== firstboot start: $(date -u) ==="
export DEBIAN_FRONTEND=noninteractive
[ -f /var/lib/bootstrap/bootstrap.env ] && . /var/lib/bootstrap/bootstrap.env || true

# ネットワーク待ち（保険）
for _ in $(seq 1 30); do
    getent hosts deb.debian.org >/dev/null 2>&1 && break
    sleep 2
done

apt-get update
apt-get install -y ca-certificates curl gnupg sudo
"""

_FB_MAIN_KEYS = """\
# --- メインユーザーの SSH 公開鍵を設置 -------------------------------------
if id "@@USER@@" >/dev/null 2>&1; then
    install -d -m700 -o "@@USER@@" -g "@@USER@@" "/home/@@USER@@/.ssh"
    install -m600 -o "@@USER@@" -g "@@USER@@" \\
        /var/lib/bootstrap/user_authorized_keys "/home/@@USER@@/.ssh/authorized_keys"
fi
"""

_FB_DOCKER = """\
# --- Docker（公式 apt リポジトリ） -----------------------------------------
install -m0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
. /etc/os-release
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian ${VERSION_CODENAME} stable" \\
    > /etc/apt/sources.list.d/docker.list
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker
"""

_FB_TAILSCALE = """\
# --- Tailscale（インストールのみ。参加は手動 `tailscale up`） ---------------
curl -fsSL https://tailscale.com/install.sh | sh
systemctl enable --now tailscaled || true
"""

_FB_ANSIBLE = """\
# --- ansible ユーザーを別途作成（SSH 鍵 + 通常 sudo） ----------------------
if ! id "@@ANSIBLE@@" >/dev/null 2>&1; then
    useradd -m -s /bin/bash "@@ANSIBLE@@"
fi
echo "@@ANSIBLE@@:${ANSIBLE_PW_HASH}" | chpasswd -e
usermod -aG sudo "@@ANSIBLE@@"
@@ANSIBLE_DOCKER@@
install -d -m700 -o "@@ANSIBLE@@" -g "@@ANSIBLE@@" "/home/@@ANSIBLE@@/.ssh"
install -m600 -o "@@ANSIBLE@@" -g "@@ANSIBLE@@" \\
    /var/lib/bootstrap/ansible_authorized_keys "/home/@@ANSIBLE@@/.ssh/authorized_keys"
"""

_FB_FOOTER = """\
# --- 後始末（自分自身を無効化し、秘密情報を消す） -------------------------
systemctl disable firstboot.service || true
rm -f /etc/systemd/system/firstboot.service
[ -f /var/lib/bootstrap/bootstrap.env ] && { shred -u /var/lib/bootstrap/bootstrap.env 2>/dev/null || rm -f /var/lib/bootstrap/bootstrap.env; }
echo "=== firstboot done: $(date -u) ==="
"""


def build_firstboot(cfg: dict) -> str:
    fb = cfg["firstboot"]
    user = cfg["user"]["name"]
    parts = [_FB_HEADER, _FB_MAIN_KEYS.replace("@@USER@@", user)]
    if fb["docker"]:
        parts.append(_FB_DOCKER)
    if fb["tailscale"]:
        parts.append(_FB_TAILSCALE)
    if fb["apt_packages"]:
        pkgs = " ".join(fb["apt_packages"])
        parts.append(f"# --- 追加 apt パッケージ ---\napt-get install -y {pkgs}\n")
    if cfg["ansible"]["enabled"]:
        ans = cfg["ansible"]["name"]
        docker_line = f'usermod -aG docker "{ans}" || true' if fb["docker"] else ""
        parts.append(
            _FB_ANSIBLE.replace("@@ANSIBLE@@", ans).replace("@@ANSIBLE_DOCKER@@", docker_line)
        )
    if fb["docker"]:
        parts.append(f'# --- メインユーザーを docker グループへ ---\nusermod -aG docker "{user}" || true\n')
    for cmd in fb["run"]:
        parts.append(f"# --- 追加コマンド ---\n{cmd}\n")
    parts.append(_FB_FOOTER)
    return "\n".join(parts)


def build_firstboot_service() -> bytes:
    return (
        "[Unit]\n"
        "Description=First boot bootstrap (docker, tailscale, ansible user)\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "ConditionPathExists=/var/lib/bootstrap/firstboot.sh\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        "ExecStart=/var/lib/bootstrap/firstboot.sh\n"
        "RemainAfterExit=no\n\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    ).encode()


def build_sshd_hardening(cfg: dict) -> bytes:
    ssh = cfg["ssh"]
    return (
        "# debian-vps-bootstrap が設置。\n"
        f"PasswordAuthentication {'yes' if ssh['password_authentication'] else 'no'}\n"
        "KbdInteractiveAuthentication no\n"
        f"PermitRootLogin {'yes' if ssh['permit_root_login'] else 'no'}\n"
        "PubkeyAuthentication yes\n"
    ).encode()


# ===========================================================================
# cpio (newc) 操作 ― 標準ライブラリのみ
# ===========================================================================
def _h(v: int) -> bytes:
    return b"%08X" % (v & 0xFFFFFFFF)


def _cpio_entry(name: str, mode: int, data: bytes = b"", ino: int = 0, nlink: int = 1) -> bytes:
    nb = name.encode("utf-8") + b"\x00"
    hdr = (
        b"070701" + _h(ino) + _h(mode) + _h(0) + _h(0) + _h(nlink) + _h(0)
        + _h(len(data)) + _h(0) + _h(0) + _h(0) + _h(0) + _h(len(nb)) + _h(0)
    )
    out = hdr + nb
    out += b"\x00" * ((-len(out)) % 4)
    out += data
    out += b"\x00" * ((-len(data)) % 4)
    return out


def _find_trailer(cpio: bytes) -> int:
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
    cpio = gzip.decompress(orig_gz)
    trailer_off = _find_trailer(cpio)
    new = bytearray(cpio[:trailer_off])
    ino = 0x300000
    new += _cpio_entry("payload", 0o040755, b"", ino); ino += 1
    secret = {"payload/bootstrap.env"}
    for name in sorted(payload):
        mode = 0o100755 if name.endswith(".sh") else (0o100600 if name in secret else 0o100644)
        new += _cpio_entry(name, mode, payload[name], ino); ino += 1
    new += _cpio_entry("preseed.cfg", 0o100644, preseed, ino); ino += 1
    new += _cpio_entry("TRAILER!!!", 0, b"", 0)
    new += b"\x00" * ((-len(new)) % 512)
    return gzip.compress(bytes(new), 9)


def download(url: str) -> bytes:
    info(f"download: {url}")
    with urllib.request.urlopen(url) as resp:  # noqa: S310 (信頼できる Debian ミラー)
        return resp.read()


# ===========================================================================
# 設定の検証・解決
# ===========================================================================
def validate_config(cfg: dict) -> list:
    errs = []
    if not cfg["user"]["name"]:
        errs.append("user.name が空")
    if not cfg["user"]["ssh_authorized_keys"]:
        errs.append("user.ssh_authorized_keys が空（SSH ログイン手段が無くなる）")
    for who in ("user", "ansible"):
        if who == "ansible" and not cfg["ansible"]["enabled"]:
            continue
        ph = cfg[who]["password_hash"]
        if ph != "prompt" and not ph.startswith("$"):
            errs.append(f"{who}.password_hash は \"prompt\" か crypt ハッシュ($...)であること")
    if cfg["ansible"]["enabled"] and not cfg["ansible"]["ssh_authorized_keys"]:
        errs.append("ansible.ssh_authorized_keys が空")
    mode = cfg["network"].get("mode", "auto")
    if mode not in ("dhcp", "static", "auto"):
        errs.append("network.mode は dhcp / static / auto のいずれか")
    if mode == "static":
        n = cfg["network"]
        if not n.get("address"):
            errs.append("static: network.address が必要")
        if not n.get("gateway"):
            errs.append("static: network.gateway が必要")
        if not (n.get("netmask") or n.get("prefix") or "/" in n.get("address", "")):
            errs.append("static: network.netmask か prefix（または address の /CIDR）が必要")
    return errs


def resolve_password(cfg: dict, who: str, interactive: bool) -> str:
    ph = cfg[who]["password_hash"]
    if ph.startswith("$"):
        return ph
    if not interactive:
        return "$6$PLACEHOLDER$PLACEHOLDER"  # check モード用ダミー
    return hash_password(prompt_password(f"{cfg[who]['name']} のパスワード"))


# ===========================================================================
# 対話プロンプト（wizard 用）
# ===========================================================================
def prompt_nonempty(label: str, default: str = "") -> str:
    while True:
        suffix = f" [{default}]" if default else ""
        val = input(f"{label}{suffix}: ").strip()
        if not val and default:
            return default
        if val:
            return val
        print("  空にはできません。")


def prompt_yesno(label: str, default: bool) -> bool:
    d = "Y/n" if default else "y/N"
    val = input(f"{label} [{d}]: ").strip().lower()
    if not val:
        return default
    return val in ("y", "yes")


def prompt_password(label: str) -> str:
    while True:
        p1 = getpass(f"{label}: ")
        if not p1:
            print("  空にはできません。")
            continue
        if getpass(f"{label}（確認）: ") != p1:
            print("  一致しません。")
            continue
        return p1


def prompt_pubkey(label: str) -> str:
    valid = (
        "ssh-ed25519", "ssh-rsa", "ssh-dss",
        "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521",
        "sk-ssh-ed25519@openssh.com", "sk-ecdsa-sha2-nistp256@openssh.com",
    )
    while True:
        val = input(f"{label}（'@/path/to/key.pub' でファイル指定可）: ").strip()
        if val.startswith("@"):
            try:
                with open(os.path.expanduser(val[1:]), encoding="utf-8") as fh:
                    val = fh.read().strip()
            except OSError as exc:
                print(f"  読み込み失敗: {exc}")
                continue
        if val.split(" ", 1)[0] in valid:
            return val
        print("  SSH 公開鍵の形式に見えません。")


# ===========================================================================
# サブコマンド
# ===========================================================================
def cmd_wizard(args) -> None:
    print("=== debian-vps-bootstrap 設定ウィザード ===")
    print("（ここでは設定ファイルを書き出すだけです。インストールはしません）\n")
    cfg = default_config()

    # コードネーム / タイムゾーン / ロケールは run 時に稼働中システムから取得する
    # （= "auto"）。気に入らなければ生成後の TOML を手で直す方針。
    print("コードネーム・タイムゾーン・ロケールは実行時に現在のシステムから取得します。")
    print("（変更したい場合は生成された TOML を手で編集してください）\n")

    cfg["host"]["hostname"] = prompt_nonempty("ホスト名", "debian-vps")
    cfg["target"]["disk"] = prompt_nonempty("対象ディスク（auto で自動検出）", "auto")

    # --- ネットワーク ---
    print("\n現在のネットワーク構成を検出中...")
    det = detect_network()
    if det:
        print(f"  検出: if={det['interface']} addr={det['address']} "
              f"mask={det['netmask']} gw={det['gateway']} dns={det['nameservers'] or '(なし)'}")
        print("  [1] auto   実行時(run)に現在の Linux から自動取得して静的化（推奨）")
        print("  [2] static いま検出した値を設定ファイルに焼き込む")
        print("  [3] dhcp   インストーラに DHCP させる")
        choice = input("  選択 [1]: ").strip() or "1"
        if choice == "2":
            cfg["network"].update(mode="static", **det)
        elif choice == "3":
            cfg["network"]["mode"] = "dhcp"
        else:
            cfg["network"]["mode"] = "auto"
    else:
        print("  検出できませんでした。mode=auto（取得失敗時は dhcp）にします。")
        cfg["network"]["mode"] = "auto"

    cfg["user"]["name"] = prompt_nonempty("メインユーザー名", "admin")
    cfg["user"]["fullname"] = prompt_nonempty("フルネーム", cfg["user"]["name"])
    cfg["user"]["ssh_authorized_keys"] = [prompt_pubkey("メインユーザーの SSH 公開鍵")]
    if prompt_yesno("メインユーザーのパスワードハッシュを設定ファイルに保存する？", False):
        cfg["user"]["password_hash"] = hash_password(prompt_password("メインユーザーのパスワード"))
    else:
        cfg["user"]["password_hash"] = "prompt"
        print("  → run 実行時に対話入力します。")

    cfg["ansible"]["enabled"] = prompt_yesno("ansible ユーザーを作成する？", True)
    if cfg["ansible"]["enabled"]:
        cfg["ansible"]["name"] = prompt_nonempty("ansible ユーザー名", "ansible")
        cfg["ansible"]["ssh_authorized_keys"] = [prompt_pubkey(f"{cfg['ansible']['name']} の SSH 公開鍵")]
        if prompt_yesno("ansible のパスワードハッシュを設定ファイルに保存する？", False):
            cfg["ansible"]["password_hash"] = hash_password(prompt_password(f"{cfg['ansible']['name']} のパスワード"))
        else:
            cfg["ansible"]["password_hash"] = "prompt"

    # Docker は既定で導入（聞かない）。不要なら TOML の firstboot.docker = false に。
    cfg["firstboot"]["tailscale"] = prompt_yesno("Tailscale を導入する？", True)

    out = args.output
    if os.path.exists(out) and not prompt_yesno(f"{out} を上書きする？", False):
        die("中止しました。")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(dump_config(cfg))
    print(f"\n設定を書き出しました: {out}")
    print(f"内容を確認・編集してから次を実行してください:")
    print(f"  sudo python3 {os.path.basename(sys.argv[0])} check -c {out}")
    print(f"  sudo python3 {os.path.basename(sys.argv[0])} run   -c {out}")


def _plan_summary(cfg: dict, arch: str, disk_note: str, net) -> str:
    fb = cfg["firstboot"]
    actions = []
    if fb["docker"]:
        actions.append("Docker")
    if fb["tailscale"]:
        actions.append("Tailscale(導入のみ)")
    if fb["apt_packages"]:
        actions.append("apt: " + ", ".join(fb["apt_packages"]))
    ans = (f"{cfg['ansible']['name']} (SSH鍵/通常sudo)"
           if cfg["ansible"]["enabled"] else "（作成しない）")
    mode, p = net
    if mode == "static":
        netline = (f"static {p['address']}/{_netmask_to_prefix(p['netmask'])} "
                   f"gw {p['gateway']} dns {','.join(p['nameservers'])}")
        if cfg["network"].get("mode") == "auto":
            netline += "  (稼働中の Linux から自動取得)"
    else:
        netline = "dhcp"
    d = cfg["debian"]
    return (
        f"  Debian          : {d['suite']} ({arch})\n"
        f"  ロケール/TZ     : {d['locale']} / {d['timezone']}\n"
        f"  ホスト名        : {cfg['host']['hostname']}\n"
        f"  ネットワーク    : {netline}\n"
        f"  対象ディスク    : {disk_note}\n"
        f"  メインユーザー  : {cfg['user']['name']} (SSH鍵のみ/sudo)\n"
        f"  ansible ユーザー: {ans}\n"
        f"  d-i パッケージ  : {', '.join(cfg['packages']['include'])}\n"
        f"  初回起動後      : {', '.join(actions) or '（なし）'}\n"
        f"  SSH             : PasswordAuth="
        f"{'yes' if cfg['ssh']['password_authentication'] else 'no'} / "
        f"PermitRoot={'yes' if cfg['ssh']['permit_root_login'] else 'no'}"
    )


def cmd_check(args) -> None:
    cfg = load_config(args.config)
    errs = validate_config(cfg)
    rcfg = resolve_config(cfg)
    arch = rcfg["debian"]["arch"]
    net = resolve_network(rcfg)
    disk_cfg = rcfg["target"]["disk"]
    disk_note = (f"auto → 実行ホストで検出（現在の候補: {detect_disk() or '不明'}）"
                 if disk_cfg == "auto" else disk_cfg)

    print("=== 設定プラン ===")
    print(_plan_summary(rcfg, arch, disk_note, net))
    print()
    if errs:
        print("=== 検証エラー ===")
        for e in errs:
            print(f"  - {e}")
    else:
        print("検証 OK")

    if args.show_files:
        disk = detect_disk() if disk_cfg == "auto" else disk_cfg
        print("\n=== 生成される preseed.cfg ===")
        print(build_preseed(rcfg, disk or "/dev/vda", "$6$EXAMPLE$EXAMPLE", net).decode())
        print("=== 生成される firstboot.sh ===")
        print(build_firstboot(rcfg))

    if errs:
        sys.exit(1)


def cmd_run(args) -> None:
    if os.geteuid() != 0:
        die("run は root で実行してください。")
    cfg = load_config(args.config)
    errs = validate_config(cfg)
    if errs:
        for e in errs:
            print(f"[設定エラー] {e}", file=sys.stderr)
        die("設定を修正してください（`check` で確認できます）。")

    rcfg = resolve_config(cfg)
    arch = rcfg["debian"]["arch"]
    net = resolve_network(rcfg)
    disk = detect_disk() if rcfg["target"]["disk"] == "auto" else rcfg["target"]["disk"]
    if not disk:
        die("対象ディスクを自動検出できませんでした。config の target.disk を明示してください。")

    ensure_prereqs()

    # 必要ならパスワードを対話入力
    user_pw_hash = resolve_password(rcfg, "user", interactive=True)
    ansible_pw_hash = (resolve_password(rcfg, "ansible", interactive=True)
                       if rcfg["ansible"]["enabled"] else "")

    # 最終確認（破壊的）
    if have("lsblk"):
        print("\n--- 現在のブロックデバイス ---")
        subprocess.run(["lsblk", "-do", "NAME,SIZE,MODEL"], check=False)
        print("------------------------------")
    print("\n以下の内容で実行します:")
    print(_plan_summary(rcfg, arch, disk, net))
    print(f"\n!!! {disk} の全データが消去されます。元の OS は復旧できません。!!!")
    if input(f"続行するには対象ディスク名をそのまま入力 ({disk}): ").strip() != disk:
        die("確認が一致しないため中止しました。")

    # payload 組み立て
    payload = {
        "payload/firstboot.sh": build_firstboot(rcfg).encode(),
        "payload/firstboot.service": build_firstboot_service(),
        "payload/sshd_hardening.conf": build_sshd_hardening(rcfg),
        "payload/user_authorized_keys": ("\n".join(rcfg["user"]["ssh_authorized_keys"]) + "\n").encode(),
    }
    if rcfg["ansible"]["enabled"]:
        payload["payload/bootstrap.env"] = f"ANSIBLE_PW_HASH='{ansible_pw_hash}'\n".encode()
        payload["payload/ansible_authorized_keys"] = (
            "\n".join(rcfg["ansible"]["ssh_authorized_keys"]) + "\n"
        ).encode()

    # ブートイメージ取得・生成
    d = rcfg["debian"]
    base = (f"http://{d['mirror_host']}{d['mirror_dir']}/dists/{d['suite']}/main/"
            f"installer-{arch}/current/images/netboot/debian-installer/{arch}")
    os.makedirs(WORKDIR, exist_ok=True)
    kernel_path = os.path.join(WORKDIR, "linux")
    initrd_path = os.path.join(WORKDIR, "new_initrd.gz")

    with open(kernel_path, "wb") as fh:
        fh.write(download(f"{base}/linux"))
    preseed = build_preseed(rcfg, disk, user_pw_hash, net)
    info("preseed を埋め込んだ initrd を生成中")
    new_initrd = build_new_initrd(download(f"{base}/initrd.gz"), preseed, payload)
    with open(initrd_path, "wb") as fh:
        fh.write(new_initrd)
    info(f"initrd 生成完了: {initrd_path} ({len(new_initrd)} bytes)")

    cmdline = "auto=true priority=critical console=ttyS0,115200 console=tty0 nomodeset"
    run_cmd(["kexec", "-l", kernel_path, f"--initrd={initrd_path}", f"--append={cmdline}"])

    info("ディスクキャッシュを同期")
    os.sync()
    try:
        with open("/proc/sys/vm/drop_caches", "w") as fh:
            fh.write("3\n")
    except OSError:
        pass

    print("\nkexec -e でインストーラへ遷移します。完了後は自動再起動し Debian が起動します。")
    print("初回起動時の処理ログは新システムの /var/log/firstboot.log です。")
    run_cmd(["kexec", "-e"])  # 通常ここで戻らない


def main() -> None:
    p = argparse.ArgumentParser(description="ISO 不要の Debian 上書きブートストラップ")
    sub = p.add_subparsers(dest="cmd", required=True)

    w = sub.add_parser("wizard", help="対話で設定ファイル(TOML)を書き出す")
    w.add_argument("-o", "--output", default="config.toml")
    w.set_defaults(func=cmd_wizard)

    c = sub.add_parser("check", help="設定を検証し生成物を確認（ドライラン）")
    c.add_argument("-c", "--config", default="config.toml")
    c.add_argument("--show-files", action="store_true", help="生成される preseed/firstboot も表示")
    c.set_defaults(func=cmd_check)

    r = sub.add_parser("run", help="設定に従いインストールを実行（破壊的）")
    r.add_argument("-c", "--config", default="config.toml")
    r.set_defaults(func=cmd_run)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
