# debian-vps-bootstrap

ISO マウントやレスキューモードが使えない VPS でも、**稼働中の OS を `kexec` で
netboot インストーラにすり替え、preseed で無人インストールしてディスクを
Debian (trixie) で丸ごと上書きする**ためのスクリプトです。

「コンパネに ISO マウント機能が無い」「レスキューイメージが古い／使えない」
といった環境で、現用の Linux 上から直接 Debian をクリーンインストールできます。

> [!WARNING]
> このスクリプトは**実行した VPS のディスク全体を消去します**。元の OS は
> 復旧できません。必ず上書きしてよいサーバー上で、バックアップを取った上で
> 実行してください。

## 特徴

- **Python3 標準ライブラリのみで完結。** ダウンロードは `urllib`、initrd の
  解凍／再圧縮は `gzip`、cpio への追記は自前の newc 実装で行うため、
  `wget` / `cpio` / `gzip` などを別途入れる必要がありません。
  外部コマンド依存は実質 `kexec` だけです。
- **その `kexec` も冒頭で自動導入。** `kexec` が無ければ
  apt / dnf / yum / zypper / pacman を自動判別して `kexec-tools` を入れます。
- **対話プロンプトで構成を決定。** ホスト名・ユーザー名・パスワード・SSH 公開鍵・
  対象ディスクを実行時に入力します（決め打ちなし）。
- **アーキテクチャ自動判別**（amd64 / arm64）。
- **インストール後の初回起動で Docker / Tailscale を自動導入**し、
  ansible ユーザーを別途作成します（ネットワークと apt が完全に動く状態で
  実行するため堅牢）。

## 動作の流れ

```
稼働中の OS（root で実行）
  │  1. kexec-tools を確認／導入
  │  2. プロンプトで構成を入力（ユーザー・鍵・ディスク等）
  │  3. netboot の kernel / initrd.gz をダウンロード
  │  4. preseed.cfg と payload を initrd に埋め込み
  │  5. kexec -l → kexec -e
  ▼
Debian インストーラ（無人インストール）
  │  ディスクを LVM で初期化し Debian をインストール
  │  late_command で first-boot サービスと sshd 設定を仕込む
  ▼
インストール済み Debian（初回起動）
  │  firstboot.service が一度だけ実行:
  │    - メインユーザーの SSH 公開鍵を設置
  │    - Docker を公式 apt リポジトリから導入
  │    - Tailscale を導入（参加は手動）
  │    - ansible ユーザーを別途作成
  ▼
運用開始
```

## 使い方

上書きしたい VPS に root（または sudo 可能なユーザー）でログインし、
スクリプトを取得して実行します。

```sh
curl -fsSLO https://raw.githubusercontent.com/nananek/debian-vps-bootstrap/main/bootstrap.py
sudo python3 bootstrap.py
```

プロンプトで以下を入力します。

| 項目 | 説明 |
| --- | --- |
| ホスト名 | 新システムのホスト名（既定: `debian-vps`） |
| メインユーザー名 | 対話的に使う管理ユーザー |
| メインユーザーのパスワード | **VNC ローカルコンソール救済用**（SSH では使わない） |
| メインユーザーの SSH 公開鍵 | SSH ログインはこの鍵のみ。`@/path/to/key.pub` でファイル指定可 |
| ansible ユーザー名 | 自動化用ユーザー（既定: `ansible`） |
| ansible のパスワード | sudo 用 |
| ansible の SSH 公開鍵 | ansible ユーザーの鍵 |
| 上書き対象ディスク | 自動検出した候補を確認して入力（`/dev/vda` 等） |

最後に対象ディスク名をそのまま打ち込む確認を経て、`kexec` で
インストーラへ遷移します。インストール完了後は自動で再起動し、
Debian が起動します。

## インストールされる構成

### ユーザーと認証

- **root ログインは無効**。
- **メインユーザー**: sudo 可能。SSH は**公開鍵のみ**
  （`PasswordAuthentication no` / `PermitRootLogin no`）。
  パスワードはプロバイダの VNC / ローカルコンソールから救済ログインする用途に
  残してあります。
- **ansible ユーザー**: 初回起動後に別途作成。SSH 公開鍵でログインし、
  sudo はパスワード必須（通常 sudo）。docker グループにも追加されます。

### パッケージ

- ベース: `standard` タスク + `openssh-server sudo curl ca-certificates gnupg`
- 初回起動後: Docker（`docker-ce` 一式 + buildx / compose プラグイン）、Tailscale

### Tailscale

パッケージのインストールと `tailscaled` の有効化までを自動で行います。
tailnet への参加は手動です。初回起動後に SSH でログインして実行してください。

```sh
sudo tailscale up
```

## 設定の変更

`bootstrap.py` 冒頭の定数で主要な挙動を変えられます。

| 定数 | 既定値 | 説明 |
| --- | --- | --- |
| `SUITE` | `trixie` | Debian コードネーム |
| `TIMEZONE` | `Asia/Tokyo` | 新システムのタイムゾーン |
| `MIRROR_HOST` / `MIRROR_DIR` | `deb.debian.org` `/debian` | apt ミラー |
| `KERNEL_CMDLINE` | `... console=ttyS0,115200 console=tty0 ...` | インストーラのカーネル引数 |

## 前提・制約

- **ネットワークは DHCP 前提**です。静的 IP 固定の VPS では preseed の
  `netcfg` 段で停止するため、`netcfg/get_ipaddress` 等を preseed に追記する
  必要があります。
- 対応アーキテクチャは **amd64 / arm64**。
- 稼働中の OS で `/proc/sys/vm/drop_caches` が書ける（= 一般的な Linux）こと。
- 万一インストーラが途中で停止しても操作できるよう、プロバイダの
  VNC / シリアルコンソールに入れる状態で実行することを強く推奨します。

## トラブルシュート

- **インストール完了後に SSH で入れない**: メインユーザーは公開鍵のみです。
  VNC コンソールからメインユーザーのパスワードでログインし、鍵を確認してください。
- **Docker / Tailscale / ansible ユーザーが無い**: 初回起動時の処理ログを
  確認します。

  ```sh
  sudo cat /var/log/firstboot.log
  sudo systemctl status firstboot.service
  ```

- **対象ディスクを間違えそう**: 実行前に `lsblk` の一覧が表示され、最後に
  ディスク名そのものを打たせる確認があります。

## ライセンス

MIT
