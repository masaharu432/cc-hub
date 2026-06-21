# cc-hub

スマホから Claude Code セッションを **起動するだけ** の小さな Web アプリ
（Linux/WSL2 などのサーバ機上で動かし、プライベートネットワーク経由でアクセス）。

会話そのものは行いません。会話は公式 Claude モバイルアプリの **Remote Control (RC)**
で行います。このアプリは tmux 上の「点火キー」に徹します:
プロジェクト選択（または任意ディレクトリ参照／新規フォルダ作成）→ 起動 → 一覧 → 改名 → 終了。

UI は Bootstrap 5（スマホ／PC レスポンシブ）。Bootstrap は `web/vendor/` に**同梱**で
ビルド不要・実行時も Python 標準ライブラリのみ。

## 設計原則

1. **tmux が唯一の真実**。独自 DB を持たず、毎回 `tmux ls` から状態を再構築する。
   CLI・このアプリ・公式 RC アプリは同じ実プロセスの 3 つのビュー。
2. **CLI 経路を常に残す**。起動したセッションは SSH から `tmux attach` できる素の
   tmux セッション。アプリ/ネットワークが落ちても shell 側で生き続ける。
3. **Claude のプロトコルに触れない**。shell/tmux/プロセス操作のみ。Anthropic API/SDK
   は呼ばない（サブスク・インタラクティブ枠のまま。API 課金にならない）。
4. **冪等起動**。同名は二重起動しない（attach-or-create）。RC は 1 プロセス = 1 セッション。

## 必要なもの

- tmux / claude CLI / python3（node/npm 不要）
- ランタイム依存ライブラリ無し（Python 標準ライブラリのみ）。フロントの Bootstrap は
  `web/vendor/` に同梱済み（取得済み・オフライン自己完結）。
- reptyr（「tmuxへ移管」のライブ移管に使用。ホストで一度だけ:
  `sudo apt-get install reptyr && sudo setcap cap_sys_ptrace+ep /usr/bin/reptyr`。
  未設定でもアプリは動くが、移管は旧方式の kill+resume フォールバックになる）

## ファイル構成

```
server.py                       静的配信 + /api/* のみ
web/index.html                  Bootstrap ベースのマークアップ
web/app.css                     自前スタイル（少量）
web/app.js                      ロジック（認証/一覧/ブラウザ/起動）
web/vendor/                     Bootstrap 5 / Bootstrap Icons（同梱）
config.json                     token 等（自動生成・git 管理外）
```

## 起動

```bash
./run.sh          # = python3 server.py
```

初回起動時に `config.json` が自動生成されます（トークンを含むので git 管理外）。
起動時に下記のような URL が表示されます:

```
http://<host-ip>:8765/?token=XXXXXXXX
```

`0.0.0.0` バインドなので、サーバ機の到達可能な IP（例: Tailscale などの tailnet IP）
経由でスマホ（同一プライベートネットワーク）から届きます。URL をスマホで開くとトークンが
localStorage に保存され、以後はトークン付き URL 無しでアクセスできます。

## config.json

```json
{
  "host": "0.0.0.0",
  "port": 8765,
  "projects_root": "/path/to/projects",
  "claude_bin": "/home/you/.local/bin/claude",
  "token": "（自動生成）"
}
```

- `public_host`（任意）: `0.0.0.0` バインド時に QR/起動バナーの URL を組む際のホスト。
  未指定なら自動検出にフォールバック。tailnet 越しに使うなら自分の tailnet IP を入れる。
- `projects_root`: 「新規起動」のプロジェクト候補一覧／フォルダ参照の初期表示ディレクトリ。
- `claude_bin`: tmux のシェルでは PATH が当てにならないため絶対パスで固定（既定は
  `which claude` で解決）。

## ディレクトリ選択

- **プロジェクト一覧**: `projects_root` 直下を選ぶ（従来どおり）。
- **手入力**: 任意の既存絶対パスを直接入力。
- **フォルダ参照**: 「参照」ボタンでブラウザ（offcanvas）を開き、任意ディレクトリへ
  辿って「ここを起動先にする」。`＋ 新規フォルダ` で現在地に新規作成 → そこで初回起動も可能。

## 起動レシピ（内部動作）

```
tmux new-session -d -s <name> -c <dir> '<claude_bin> --dangerously-skip-permissions --remote-control "<name>"'
```

- `<name>` は `[A-Za-z0-9_-]` に厳格バリデーション（tmux 名 & シェル注入対策）。
- セッションは tmux で attach 可能、かつ公式 RC アプリに `<name>` で表示される。

### trust ダイアログの自動処理（重要）

新規・未信頼フォルダへの**デタッチ起動は trust ダイアログで止まる**（tmux は TTY のため、
`--dangerously-skip-permissions` を付けても対話プロンプトはスキップされない＝実機確認済み）。
そこで `launch` 時に対象ディレクトリを `~/.claude.json` の
`projects[<resolved dir>].hasTrustDialogAccepted = true` に**事前登録**してから起動する
（`ensure_trusted()`、atomic write）。これで新規フォルダでも無人起動できる。
①デンジャラスモード確認ダイアログは `~/.claude/settings.json` の
`skipDangerousModePermissionPrompt: true` で別途抑制済み。

## 手動で一度だけ必要な準備（コマンドではなく操作）

1. **スマホ到達性の確認**: サーバ起動後、スマホ（同一プライベートネットワークに接続）で
   `http://<host-ip>:8765/healthz` を開き `{"ok":true}` が返るか確認。
2. **RC の確認**: 起動後、公式アプリの Remote Control に `<name>` のセッションが
   出るか確認。
（trust ダイアログは上記の通りアプリが自動処理するため、手動で前面起動して通す作業は不要。）

## スマホ側ネットワークについて（任意）

このアプリはサーバ機がスマホから**プライベートネットワーク越しに到達できる**ことが前提です
（[Tailscale](https://tailscale.com/) のような VPN を使うのが手軽）。

Android で広告ブロック系の常時 VPN（例: AdGuard）を併用している場合、**1プロファイルにつき
有効な VPN は1つだけ**という OS 制約（`VpnService` は排他）に注意。広告ブロック VPN と
Tailscale を同時に使いたいときは、**Work Profile（仕事用プロファイル）を分ける**と
プロファイルごとに独立した VPN を持てます（`Shelter` などのアプリで作成可能。プロファイルや
配布元は環境により異なるため各自で確認してください）。普段使いのプロファイルで広告ブロック、
仕事用プロファイルで Tailscale＋このアプリを開くブラウザ、という分け方が定番です。

## API

| Method | Path             | 説明 |
|--------|------------------|------|
| GET    | `/`, `/app.js` 等 | 静的配信（`web/`、秘密情報なし・認証不要、トラバーサル防止） |
| GET    | `/healthz`       | ヘルスチェック |
| GET    | `/api/sessions`  | `tmux ls` をパースして一覧 |
| GET    | `/api/projects`  | `projects_root` 直下のディレクトリ一覧 |
| GET    | `/api/browse?path=` | 任意ディレクトリのサブフォルダ一覧（`{path, parent, entries}`） |
| POST   | `/api/launch`    | `{name, dir}` 冪等起動（起動前に trust 自動登録） |
| POST   | `/api/mkdir`     | `{parent, name}` 新規フォルダ作成 |
| POST   | `/api/rename`    | `{old, new}` 改名 |
| POST   | `/api/kill`      | `{name}` セッション kill |
| POST   | `/api/migrate`   | `{pid, sid?, name?}` ターミナル起動の claude を reptyr で tmux へ生きたまま移管 |
| GET    | `/api/spawn-servers` | 稼働中 Spawn サーバー一覧（状態・Capacity・環境URL） |
| POST   | `/api/migrate-all` | 全ターミナル claude（外部+フラグレス）を reptyr で一括 tmux 移管。per-pid で成功/失敗を集計 |
| POST   | `/api/spawn-servers` | `{dir}` Spawn サーバー起動（同フォルダ重複は 409 + 既存情報） |
| DELETE | `/api/spawn-servers/<name>` | Spawn サーバー停止（`@ccwa_spawn` 付きのみ・チャット誤殺防止） |

`/api/*` は `X-Auth-Token` ヘッダ必須（トークンは定数時間比較）。

## tmux 管理を主軸に（移行 / 一括取り込み）

このアプリの主役は **tmux 管理**(「プロジェクト」タブ)。ターミナルや他経路で
バラバラに動いている claude を tmux 配下へ集約して管理する:

- **個別移管**: 各「稼働中(ターミナル)」行の「tmuxへ移管」(`reptyr -T`、プロセスを
  生かしたまま吸い込む。TUI・生成中の状態・RC接続を保持)。
- **一括取り込み**: 「プロジェクト」タブの **「全部tmuxへ (N)」** ボタンで、検出した
  ターミナル claude を全部まとめて tmux へ。`POST /api/migrate-all`。1件失敗しても
  他は続行し、失敗は理由付きで集計表示。
- 移管が失敗した場合は **reptyr の stderr の実理由**(権限 / sshd 配下は root 必須 /
  未対応 tty 等)がエラーに出る。原因不明の沈黙は無くした。

詳細: `docs/superpowers/specs/2026-06-12-reptyr-live-migration-design.md`

## Spawn サーバータブ(β・非推奨)

> **公式アプリ側が不安定でバグが多く、実用に耐えないため非推奨。** タブはナビ最後尾に
> 降格(β表記)。ターミナルの claude は上記「全部tmuxへ」で tmux 管理下に取り込む方が安定。

公式の常駐サーバー `claude remote-control`（サブコマンド）を tmux で点火するタブ。
1フォルダ = サーバー1個で、モバイルアプリ / claude.ai/code の「新規セッション」
プルダウン（デフォルトの GitHub をこのサーバーに切替）からローカルにセッションを
作れる。本アプリはセッション管理をせず、起動・一覧・環境URLリンク・停止のみ。

- 起動コマンド: `claude remote-control --spawn=same-dir --no-create-session-in-dir`
  （モード選択プロンプトはフラグで、y/n 同意は初回承認済みの `remoteDialogSeen` でスキップ）
- tmux セッションに `@ccwa_spawn=1` を刻印して識別（名前プレフィックスに依存しない）。
  既存のプロジェクト一覧・resume・migrate からは除外される
- スマホで「開く」= 環境スコープ URL `https://claude.ai/code?environment=env_...` を直接開く
- **注意**: サーバー上のセッションはサーバープロセスの子（ヘッドレス）。停止すると
  全セッションが一緒に終了する（UI で確認ダイアログあり）。tmux attach で触れる
  TUI が欲しい場合は従来のプロジェクトタブで起動すること

詳細: `docs/superpowers/specs/2026-06-12-spawn-server-tab-design.md`
