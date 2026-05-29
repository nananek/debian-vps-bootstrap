# debian-vps-bootstrap

ISO マウントやレスキューモードが使えない VPS でも、**稼働中の OS を `kexec` で
netboot インストーラにすり替え、preseed で無人インストールしてディスクを
Debian (trixie) で丸ごと上書きする**ためのスクリプトです。

「コンパネに ISO マウント機能が無い」「レスキューイメージが古い／使えない」
といった環境で、現用の Linux 上から直接 Debian をクリーンインストールできます。

設定（TOML）と実行を分離してあるので、いきなり `kexec` で事故ることなく、
設定を書き出して中身を確認してから流せます。

> [!WARNING]
> `run` は**実行した VPS のディスク全体を消去します**。元の OS は復旧できません。
> 必ず上書きしてよいサーバー上で、バックアップを取った上で実行してください。

## 特徴

- **設定と実行を分離**した 3 サブコマンド（`wizard` / `check` / `run`）。
- **Python3 標準ライブラリのみで完結。** ダウンロードは `urllib`、initrd の
  解凍／再圧縮は `gzip`、cpio への追記は自前の newc 実装で行うため、
  `wget` / `cpio` / `gzip` などを別途入れる必要がありません。
  外部コマンド依存は実質 `kexec` だけです。
- **その `kexec` も `run` 時に自動導入。** 無ければ
  apt / dnf / yum / zypper / pacman を自動判別して `kexec-tools` を入れます。
- **設定ファイル（TOML）で構成を管理。** 初期パッケージ・Docker/Tailscale の
  有無・追加コマンドなどを宣言的に指定でき、再現性があります。
- **稼働中の Linux から設定を自動取得**。ネットワーク（現在の IPv4 設定 →
  DHCP が無い VPS でも静的設定として引き継ぎ）に加え、コードネーム・
  タイムゾーン・ロケールも現在のシステムから取り込みます（いずれも `auto`）。
- **アーキテクチャ自動判別**（amd64 / arm64）。
- **インストール後の初回起動で Docker / Tailscale を自動導入**し、
  ansible ユーザーを別途作成します（ネットワークと apt が完全に動く状態で
  実行するため堅牢）。

## サブコマンド

| コマンド | 説明 |
| --- | --- |
| `wizard` | 対話で質問し、設定ファイル（TOML）を書き出す。**インストールはしない**。 |
| `check`  | 設定を検証し、生成される preseed / firstboot を確認するドライラン。 |
| `run`    | 設定を読み込みインストールを実行する。**破壊的**（最終確認あり）。 |

```sh
python3 bootstrap.py wizard -o config.toml        # 設定を作る
python3 bootstrap.py check  -c config.toml         # 確認
python3 bootstrap.py check  -c config.toml --show-files  # 生成物も表示
sudo python3 bootstrap.py run -c config.toml       # 実行（ディスクを上書き）
```

`wizard` は**必須項目だけ**を質問します（ホスト名 / ディスク / ネットワーク方式 /
メインユーザーとその SSH 鍵 / ansible ユーザーの要否と鍵 / Tailscale の要否）。
コードネーム・タイムゾーン・ロケールは `run` 時に稼働中システムから取得し、Docker
は既定で導入します。これらや初期パッケージなどの細部は、生成された TOML を手で
編集して変えられます。

## クイックスタート

1. 手元のマシン、または対象 VPS 上で設定を作成します。

   ```sh
   curl -fsSLO https://raw.githubusercontent.com/nananek/debian-vps-bootstrap/main/bootstrap.py
   python3 bootstrap.py wizard
   ```

   `examples/config.sample.toml` を直接コピーして手で編集しても構いません。

2. 内容を確認します。

   ```sh
   python3 bootstrap.py check -c config.toml --show-files
   ```

3. 上書きしたい VPS に `bootstrap.py` と `config.toml` を置き、root で実行します。

   ```sh
   sudo python3 bootstrap.py run -c config.toml
   ```

   最後に対象ディスク名をそのまま打ち込む確認を経て、`kexec` でインストーラへ
   遷移します。完了後は自動で再起動し Debian が起動します。

## 設定ファイル（TOML）

サンプル: [`examples/config.sample.toml`](examples/config.sample.toml)（フル版）、
[`examples/config.minimal.toml`](examples/config.minimal.toml)（最小版）。
省略した項目はすべて既定値が使われます。

```toml
[debian]
suite = "auto"            # auto=稼働中システムのコードネーム / trixie / bookworm ...
arch = "auto"             # auto | amd64 | arm64
locale = "auto"           # auto=稼働中システムの LANG（取れなければ en_US.UTF-8）
timezone = "auto"         # auto=稼働中システムの TZ（取れなければ Etc/UTC）
keymap = "us"             # VPS では基本このまま

[target]
disk = "auto"             # auto で自動検出（/dev/vda → sda → nvme0n1）

[network]
mode = "auto"             # auto | static | dhcp
# static のとき: address = "203.0.113.10/24", gateway = "203.0.113.1",
#                nameservers = ["1.1.1.1"]

[host]
hostname = "debian-vps"

[user]                    # メインユーザー: SSH は公開鍵のみ
name = "admin"
password_hash = "prompt"  # "prompt" で実行時入力 / "$6$..." でハッシュ直指定
ssh_authorized_keys = ["ssh-ed25519 AAAA... admin@laptop"]

[ansible]                 # 自動化用ユーザー（初回起動後に別途作成）
enabled = true
name = "ansible"
password_hash = "prompt"
ssh_authorized_keys = ["ssh-ed25519 AAAA... ansible@control"]

[packages]                # d-i 段で入れる最小パッケージ
include = ["openssh-server", "sudo", "curl", "ca-certificates", "gnupg"]

[firstboot]               # 初回起動後に行う処理
docker = true
tailscale = true          # 導入のみ。参加は手動 `tailscale up`
apt_packages = ["htop", "git"]
run = ["timedatectl set-ntp true"]

[ssh]
password_authentication = false
permit_root_login = false
```

### パスワードの扱い

平文では保存しません。`password_hash` は次のいずれかです。

- `"prompt"` … `run` 実行時に対話入力する（推奨）。
- `"$6$..."` … SHA-512 crypt ハッシュを直接指定（完全無人化したい場合）。
  生成例: `openssl passwd -6` または `mkpasswd -m sha-512`。

`wizard` では「ハッシュを設定ファイルに保存するか」を選べます。

## インストールされる構成

### ユーザーと認証

- **root ログインは無効**。
- **メインユーザー**: sudo 可能。SSH は**公開鍵のみ**
  （既定で `PasswordAuthentication no` / `PermitRootLogin no`）。
  パスワードはプロバイダの VNC / ローカルコンソールから救済ログインする用途に
  残せます。
- **ansible ユーザー**（任意）: 初回起動後に別途作成。SSH 公開鍵でログインし、
  sudo はパスワード必須（通常 sudo）。docker グループにも追加されます。

### パッケージ / 初回起動後の処理

- d-i 段: `standard` タスク + `packages.include`（`openssh-server` を含めること）。
- 初回起動後（`firstboot.service` が一度だけ実行）:
  - メインユーザーの SSH 公開鍵を設置
  - `firstboot.docker = true` なら Docker（`docker-ce` 一式 + buildx / compose）
  - `firstboot.tailscale = true` なら Tailscale（導入のみ）
  - `firstboot.apt_packages` の追加インストール
  - ansible ユーザーの作成
  - `firstboot.run` の追加コマンド実行

### Tailscale

パッケージ導入と `tailscaled` 有効化までを自動で行います。tailnet への参加は
手動です。初回起動後に SSH でログインして実行してください。

```sh
sudo tailscale up
```

### ネットワーク

`network.mode` で 3 通りから選べます。

- **`auto`（既定）**: `run` / `check` を実行したその Linux の IPv4 実効設定
  （デフォルトルートのインタフェース・IP・ネットマスク・ゲートウェイ・DNS）を
  読み取り、取得できれば**静的設定として新システムへ焼き込みます**。取得できな
  ければ DHCP にフォールバックします。検出は `/proc/net/route`・ioctl・
  `resolv.conf` を使い、外部コマンドには依存しません（systemd-resolved の
  スタブ `127.0.0.53` は除外し、`/run/systemd/resolve/resolv.conf` の上流を優先）。
- **`static`**: `[network]` の `address`（`"IP/CIDR"` 表記も可）・`gateway`・
  `netmask`/`prefix`・`nameservers` をそのまま使います。
- **`dhcp`**: インストーラに DHCP させます。

DHCP が無い／静的 IP 固定の VPS でも、`auto` のままで現在の疎通設定を引き継げます。
IPv6 の自動取り込みは未対応です（必要なら `firstboot.run` で設定可能）。

## 前提・制約

- 対応アーキテクチャは **amd64 / arm64**。
- `run` は root 権限と、`/proc/sys/vm/drop_caches` が書ける一般的な Linux が前提。
- 設定の読み込み（`check` / `run`）は Python 3.11+ の `tomllib` を使います。
  それ未満の Python でも動くよう、簡易 TOML パーサをフォールバックとして同梱
  しています（サンプル設定で `tomllib` との出力一致を検証済み）。
- 万一インストーラが途中で停止しても操作できるよう、プロバイダの
  VNC / シリアルコンソールに入れる状態で `run` することを強く推奨します。
  カーネルには `console=ttyS0,115200 console=tty0` の両方を渡しています。

## トラブルシュート

- **インストール完了後に SSH で入れない**: メインユーザーは公開鍵のみです。
  VNC コンソールからメインユーザーのパスワードでログインし、鍵を確認してください。
- **Docker / Tailscale / ansible ユーザーが無い**: 初回起動時の処理ログを確認します。

  ```sh
  sudo cat /var/log/firstboot.log
  sudo systemctl status firstboot.service
  ```

- **本当に流す前に中身を見たい**: `check --show-files` で、生成される
  `preseed.cfg` と `firstboot.sh` をそのまま表示できます。

## ライセンス

MIT
