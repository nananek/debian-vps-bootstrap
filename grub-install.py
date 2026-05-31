#!/usr/bin/env python3
"""
grub-install.py — bootstrap.py の最後の kexec を「GRUB エントリ登録」に差し替えた版。

GRUB を触れる環境（PVE/Debian ホストなど）向け。bootstrap.py の cmd_run と同じ手順で
netboot カーネル / preseed 埋め込み initrd を生成し、kexec する代わりに

    /boot/debian-vps-bootstrap/{vmlinuz,initrd.gz}   … 生成物
    /etc/grub.d/42_debian_vps_bootstrap              … menuentry

を作って update-grub する。再起動して当該メニューを選んだときだけ無人インストールが
走る（デフォルトのブート対象は変えない）。設定・preseed のロジックは bootstrap.py に一本化。

kexec 版との使い分け:
  - kexec が lockdown/Secure Boot で `kexec_file_load` を要求して詰む環境
  - ヘッドレスでも「モニタを挿して GRUB メニューを見ながら流したい」ベアメタル
    （kexec 後はフレームバッファが死んで VGA が出ないことが多いが、GRUB 起動なら出る）

    sudo python3 grub-install.py [-c config.toml] [--no-update-grub]
"""

import argparse
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bootstrap as b  # noqa: E402  (パスを通してから import)

BOOT_DIR = "/boot/debian-vps-bootstrap"
GRUB_FRAG = "/etc/grub.d/42_debian_vps_bootstrap"
# bootstrap.cmd_run と同一の cmdline（preseed 自動実行 + コンソール両出し）
CMDLINE = "auto=true priority=critical console=ttyS0,115200 console=tty0 nomodeset"


def grub_paths():
    """GRUB(search --file/$root) から見た vmlinuz / initrd.gz のパスを返す。

    /boot が独立パーティションなら、その FS 内のパスは先頭の "/boot" を含まない
    （"/boot" はマウント先であってデバイス内のパスではない）。os.path.ismount で判定し、
    断片に書くパスと実ファイル配置パスのズレを自動で吸収する。
    """
    prefix = "/debian-vps-bootstrap" if os.path.ismount("/boot") else "/boot/debian-vps-bootstrap"
    return f"{prefix}/vmlinuz", f"{prefix}/initrd.gz"


def main() -> None:
    ap = argparse.ArgumentParser(description="bootstrap.py の kexec を GRUB 登録に差し替える")
    ap.add_argument("-c", "--config", default="config.toml")
    ap.add_argument("--no-update-grub", action="store_true", help="update-grub を実行しない")
    args = ap.parse_args()

    if os.geteuid() != 0:
        b.die("root で実行してください: sudo python3 grub-install.py")

    # --- ここから bootstrap.cmd_run と同じ前処理 ---
    cfg = b.load_config(args.config)
    errs = b.validate_config(cfg)
    if errs:
        for e in errs:
            print(f"[設定エラー] {e}", file=sys.stderr)
        b.die("設定を修正してください（bootstrap.py check で確認できます）。")

    rcfg = b.resolve_config(cfg)
    arch = rcfg["debian"]["arch"]
    net = b.resolve_network(rcfg)
    disk = b.detect_disk() if rcfg["target"]["disk"] == "auto" else rcfg["target"]["disk"]
    if not disk:
        b.die("対象ディスクを自動検出できませんでした。config の target.disk を明示してください。")

    # パスワード解決（config に $6$ ハッシュがあれば対話なしでそのまま使われる）
    user_pw_hash = b.resolve_password(rcfg, "user", interactive=True)
    ansible_pw_hash = (b.resolve_password(rcfg, "ansible", interactive=True)
                       if rcfg["ansible"]["enabled"] else "")

    # payload 組み立て（bootstrap.cmd_run と同一）
    payload = {
        "payload/firstboot.sh": b.build_firstboot(rcfg).encode(),
        "payload/firstboot.service": b.build_firstboot_service(),
        "payload/sshd_hardening.conf": b.build_sshd_hardening(rcfg),
        "payload/user_authorized_keys": ("\n".join(rcfg["user"]["ssh_authorized_keys"]) + "\n").encode(),
    }
    if rcfg["ansible"]["enabled"]:
        payload["payload/bootstrap.env"] = f"ANSIBLE_PW_HASH='{ansible_pw_hash}'\n".encode()
        payload["payload/ansible_authorized_keys"] = (
            "\n".join(rcfg["ansible"]["ssh_authorized_keys"]) + "\n"
        ).encode()

    # netboot 取得 + preseed 埋め込み initrd 生成（bootstrap.cmd_run と同一）
    d = rcfg["debian"]
    base = (f"http://{d['mirror_host']}{d['mirror_dir']}/dists/{d['suite']}/main/"
            f"installer-{arch}/current/images/netboot/debian-installer/{arch}")
    b.info(f"netboot 取得元: {base}")
    kernel = b.download(f"{base}/linux")
    b.info("preseed を埋め込んだ initrd を生成中")
    initrd = b.build_new_initrd(b.download(f"{base}/initrd.gz"),
                                b.build_preseed(rcfg, disk, user_pw_hash, net),
                                payload)

    # --- ここが kexec ではなく GRUB 登録 ---
    # 生成物（preseed/パスワードハッシュ入り initrd）は /boot に残るため、ディレクトリと
    # initrd を root 専用に絞る。GRUB は raw FS 読みで Unix 権限を無視するので起動には影響しない。
    os.makedirs(BOOT_DIR, exist_ok=True)
    os.chmod(BOOT_DIR, 0o700)
    kernel_file = os.path.join(BOOT_DIR, "vmlinuz")
    initrd_file = os.path.join(BOOT_DIR, "initrd.gz")
    with open(kernel_file, "wb") as fh:
        fh.write(kernel)
    with open(initrd_file, "wb") as fh:
        fh.write(initrd)
    os.chmod(initrd_file, 0o600)
    b.info(f"配置: {kernel_file}, {initrd_file} ({len(initrd)} bytes)")

    kernel_grub, initrd_grub = grub_paths()
    title = f"Debian {d['suite']} installer (preseed, WIPES {disk})"
    with open(GRUB_FRAG, "w", encoding="utf-8") as fh:
        fh.write(b.build_grub_fragment(title, CMDLINE, kernel_grub, initrd_grub))
    os.chmod(GRUB_FRAG, 0o755)
    b.info(f"GRUB 断片を生成: {GRUB_FRAG}")

    # 破壊対象の最終確認用に、現在のブロックデバイスを見せる（cmd_run と同様）。
    if b.have("lsblk"):
        print("\n--- 現在のブロックデバイス ---")
        subprocess.run(["lsblk", "-do", "NAME,SIZE,MODEL"], check=False)
        print("------------------------------")

    if args.no_update_grub:
        b.info("--no-update-grub 指定。手動で update-grub を実行してください。")
    else:
        b.run_cmd(["update-grub"])

    print()
    print("=== 完了 ===")
    print(f"  GRUB メニュー項目: \"{title}\"")
    print(f"  対象ディスク     : {disk}（選択して起動すると全消去）")
    print(f"  ネットワーク     : {'static ' + net[1]['address'] if net[0] == 'static' else 'dhcp'}")
    print()
    print("再起動 → 上記メニュー項目を選択すると無人インストールが走ります。")
    print("デフォルトのブート対象は変更していません（選ばなければ従来どおり起動）。")
    print("メニューが隠れている場合は起動時に ESC(UEFI)/Shift(BIOS) を押し続けてください。")
    print(f"取り消すには: sudo rm -rf {GRUB_FRAG} {BOOT_DIR} && sudo update-grub")


if __name__ == "__main__":
    main()
