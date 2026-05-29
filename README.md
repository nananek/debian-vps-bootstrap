# debian-vps-bootstrap

ISO マウントやレスキューモードが使えない VPS で、**稼働中の OS を `kexec` で
netboot インストーラにすり替え、preseed で無人インストールしてディスクを Debian で
丸ごと上書きする**ツールです。「コンパネに ISO マウントが無い」「レスキューイメージが
古い／使えない」環境で、現用の Linux 上から直接 Debian をクリーンインストールできます。

設定（TOML）と実行を分離してあるので、内容を確認してから流せます。Python3 標準
ライブラリだけで動き、外部コマンド依存は実質 `kexec` のみ（それも自動で導入します）。

> [!WARNING]
> `run` は**対象ディスクの全データを消去します**。元の OS は復旧できません。
> 必ず上書きしてよい VPS 上で、バックアップを取った上で実行してください。

---

## 使い方

3 ステップ（設定を作る → 確認する → 実行する）です。`bootstrap.py` 1 ファイルで完結します。

### 1. 設定を作る

対話ウィザードで `config.toml` を書き出します。手元・対象 VPS どちらで作っても構いません。

```sh
curl -fsSLO https://raw.githubusercontent.com/nananek/debian-vps-bootstrap/main/bootstrap.py
python3 bootstrap.py wizard          # → config.toml を生成
```

ウィザードが聞くのは**必須項目だけ**です:

- ホスト名 / 対象ディスク / ネットワーク方式
- メインユーザー名とその SSH 公開鍵（＋パスワードを保存するか）
- ansible ユーザーを作るか（作るなら名前と SSH 公開鍵）
- Tailscale を入れるか

コードネーム・タイムゾーン・ロケールは `run` 時に**稼働中システムから自動取得**、
Docker は既定で導入します。これらや初期パッケージなどの細部は、生成された
`config.toml` を手で編集して変えられます（[サンプル](examples/config.sample.toml)を
コピーして手書きしてもOK）。

### 2. 確認する（任意だが推奨）

設定の検証と、実際に生成される `preseed.cfg` / `firstboot.sh` を表示できます（破壊的操作なし）。

```sh
python3 bootstrap.py check -c config.toml               # プランと検証結果
python3 bootstrap.py check -c config.toml --show-files  # 生成物も表示
```

### 3. 実行する（破壊的）

対象 VPS に `bootstrap.py` と `config.toml` を置き、**root** で実行します。

```sh
sudo python3 bootstrap.py run -c config.toml
```

`kexec` が無ければ自動で導入し、最後に**対象ディスク名を打ち込む確認**を経て
インストーラへ遷移します。完了後は自動で再起動し、Debian が起動します。

### 4. インストール後

初回起動時に `firstboot.service` が一度だけ走り、SSH 鍵の設置・Docker / Tailscale の
導入・ansible ユーザー作成を行います（ログ: `/var/log/firstboot.log`）。

```sh
ssh <メインユーザー>@<ホスト>     # 公開鍵でログイン
sudo tailscale up                  # Tailscale は参加だけ手動
```

---

## 設定ファイル（TOML）

省略した項目はすべて既定値になります。最小例は
[`examples/config.minimal.toml`](examples/config.minimal.toml)、全項目は
[`examples/config.sample.toml`](examples/config.sample.toml) を参照してください。

```toml
[debian]
suite = "auto"            # auto=稼働中システムのコードネーム（取れなければ trixie）
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

[user]                    # メインユーザー: SSH は公開鍵のみ・sudo 可
name = "admin"
password_hash = "prompt"  # "prompt"=run 時に入力 / "$6$..."=ハッシュ直指定
ssh_authorized_keys = ["ssh-ed25519 AAAA... admin@laptop"]

[ansible]                 # 自動化用ユーザー（初回起動後に別途作成・任意）
enabled = true
name = "ansible"
password_hash = "prompt"
ssh_authorized_keys = ["ssh-ed25519 AAAA... ansible@control"]

[packages]                # d-i 段で入れる最小パッケージ（openssh-server は必須）
include = ["openssh-server", "sudo", "curl", "ca-certificates", "gnupg"]

[firstboot]               # 初回起動後に行う処理
docker = true
tailscale = true          # 導入のみ。参加は手動 `tailscale up`
apt_packages = ["htop", "git"]
run = ["timedatectl set-ntp true"]

[ssh]
password_authentication = false   # SSH パスワード認証オフ（鍵のみ）
permit_root_login = false         # root SSH オフ
```

**パスワード**は平文保存しません。`password_hash` は `"prompt"`（`run` 時に対話入力・推奨）
か、`openssl passwd -6` などで作った `"$6$..."` ハッシュを直接指定します。

**ネットワーク**は `auto`（既定）だと `run` を実行したその Linux の現在の IPv4 設定
（IF・IP・ネットマスク・ゲートウェイ・DNS）を読み取り、**静的設定として新システムへ
引き継ぎます**（取れなければ DHCP にフォールバック）。DHCP の無い VPS でもそのまま疎通を
維持できます。`static` で全項目を手書きすることも、`dhcp` で DHCP に任せることも可能です。

---

## できあがる構成

- **認証**: root ログイン無効。メインユーザーは sudo 可で **SSH は公開鍵のみ**
  （`PasswordAuthentication no` / `PermitRootLogin no`）。パスワードは VPS の VNC /
  ローカルコンソールから救済ログインする用途に残ります。
- **ansible ユーザー**（任意）: SSH 鍵でログイン、sudo はパスワード必須（通常 sudo）、
  docker グループ所属。
- **パッケージ**: d-i 段で `standard` タスク + `packages.include`。初回起動後に Docker
  （`docker-ce` 一式 + buildx / compose）・Tailscale・`apt_packages`・`run` を処理。

## 前提・制約

- 対応アーキテクチャは **amd64 / arm64**。
- `run` は root 権限と、`/proc/sys/vm/drop_caches` が書ける一般的な Linux が前提。
- 設定の読み込みは Python 3.11+ の `tomllib` を使用（それ未満向けに簡易 TOML パーサを
  同梱し、サンプル設定で `tomllib` との一致を検証済み）。
- IPv6 のネットワーク自動取り込みは未対応（必要なら `firstboot.run` で設定）。
- 万一インストーラが途中で止まっても操作できるよう、**プロバイダの VNC /
  シリアルコンソールに入れる状態で `run`** することを強く推奨します
  （カーネルへ `console=ttyS0,115200 console=tty0` の両方を渡しています）。

## トラブルシュート

- **SSH で入れない**: メインユーザーは公開鍵のみです。VNC コンソールからパスワードで
  ログインし、`~/.ssh/authorized_keys` を確認してください。
- **Docker / Tailscale / ansible ユーザーが無い**: 初回起動処理を確認します。

  ```sh
  sudo cat /var/log/firstboot.log
  sudo systemctl status firstboot.service
  ```

- **流す前に中身を見たい**: `check --show-files` で `preseed.cfg` と `firstboot.sh` を
  そのまま表示できます。

## ライセンス

MIT
