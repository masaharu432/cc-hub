# Spawnサーバータブ 設計

2026-06-12 / ブランチ: spawn-server-tab

## 背景

公式 `claude remote-control`(サブコマンド)は常駐スポーンサーバーで、モバイルアプリ/
claude.ai/code の新規セッション・プルダウンからローカルマシン上にセッションを作れる
(実機検証済み 2026-06-12, v2.1.173)。本アプリの新しい役割のひとつとして
「フォルダを選んで spawn サーバーを tmux 常駐させる」点火機能をタブとして追加する。
既存のセッション起動・resume・migrate 機能は無変更で併存する(ユーザー決定)。

実機検証で確定した前提:

- `Enable Remote Control? (y/n)` は初回のみ(`~/.claude.json` の `remoteDialogSeen`)。
  このマシンでは承認済みのため、以後プロンプトは出ない。
- `--spawn=same-dir` を明示するとモード選択プロンプトも出ない。
- `--no-create-session-in-dir` でプリ作成セッションなしの空サーバーになる(Capacity 0/32)。
- 環境URL `https://claude.ai/code?environment=env_...` が起動直後に標準出力(ペイン)に出る。
- workspace trust は通常セッションと同じ前提 → 既存 `ensure_trusted()` がそのまま効く。
- アプリ側の表示はホスト名+フォルダ名が自動で付く → `--name` は渡さない。

## 決定事項(ユーザー確認済み)

- タブ分割で新機能として追加。既存タブは無変更。
- 守備範囲は「点火+基本管理」: 起動 / 一覧(状態・Capacity) / 環境URLリンク / 停止。
  QRコードは出さない(本アプリ自体をモバイルで使うため、タップリンクで十分)。
- `--spawn=same-dir` 固定。worktree は将来の1フィールド追加で対応可能な構造にする。
- 名前入力なし(完全ワンタップ点火)。`--name` も渡さない。
- プリ作成セッションなし(`--no-create-session-in-dir`)。
- 1フォルダ = サーバー1個。重複起動は 409 で既存情報を返す。

## アーキテクチャ

起動コマンド(サーバー1個 = tmuxセッション1個):

```
tmux new-session -d -s spawn-<フォルダ名> -c <選択フォルダ> \
  '<claude_bin> remote-control --spawn=same-dir --no-create-session-in-dir'
```

- tmuxセッション名は `spawn-` プレフィックス + sanitize済みフォルダ名
  (tmux はセッション名に `:` `.` を許さないので `:` 区切りは使わない)。
  basename 衝突時は既存の `_unique_session_name` で `-2`, `-3`…。
- 識別は名前に依存しない: 作成時に tmux セッションオプション `@ccwa_spawn=1` を刻印し、
  `list_sessions()` の format に `#{@ccwa_spawn}` を足して `spawn: bool` を返す。
  ユーザーが偶然 `spawn-` で始まる名前のチャットセッションを作っても誤分類しない。
- 既存ビュー(`build_overview`)は `spawn` フラグの立った行を除外するだけ。
  external/flagless 検出への影響なし(スポーンサーバーとその子は TMUX 環境内 → 既存の
  `_pid_in_tmux` 除外がそのまま効く。子の `--session-id cse_...` は UUID 形式でないため
  `_SID_FLAG_RE` にも当たらない)。
- 停止 = `tmux kill-session`。spawn された子セッションはサーバーの子プロセスなので一緒に
  終了する → UI の停止ボタンに確認ダイアログ必須。
- サーバー状態の永続化はしない。tmux が唯一の真実(既存思想と同じ)。

## サーバー側 API

### `GET /api/spawn-servers`

```json
{"servers": [{
  "name": "spawn-claude-code-web-app",
  "directory": "/mnt/.../claude-code-web-app",
  "folder": "claude-code-web-app",
  "env_url": "https://claude.ai/code?environment=env_01Az...",
  "status": "connected",
  "capacity_used": 2,
  "capacity_max": 32,
  "created": 1781234567
}]}
```

実装: `list_sessions()` から `spawn` フラグの行を拾い、各ペインを `capture-pane`
(既存 `_pane_id` 利用)して正規表現でパース:

- status: `Connected`/`Ready` → `connected`、`Connecting` → `connecting`、
  どれも無ければ `unknown`(TUI文言変更で一覧が死なないよう、パース失敗は null/unknown に落とす)
- `Capacity:\s*(\d+)/(\d+)` → capacity_used / capacity_max
- `https://claude\.ai/code\?environment=env_\w+` → env_url

### `POST /api/spawn-servers` `{dir}`

1. パス解決・存在確認 → `ensure_trusted()`
2. 同じ resolved dir の spawn サーバーが既にあれば **409** + 既存サーバー情報
3. tmux 起動 + `@ccwa_spawn` 刻印 → 最大15秒 env_url の出現をポーリング
4. 一覧と同形式で返す(タイムアウト時は status そのまま・env_url null で返し、
   以後の一覧ポーリングに任せる)

### `DELETE /api/spawn-servers/<name>`

`@ccwa_spawn` が立っていることを検証してから kill-session。
spawn サーバー以外の名前なら 400。

エラーマッピングは既存どおり: ValueError→400 / 409は専用例外 / その他→500。

## UI(新タブ)

- ナビに `data-view="spawn"` タブ(アイコン bi-broadcast-pin、ラベル「Spawn」)。
- 起動カード: フォルダ入力 + 参照(既存フォルダブラウザ offcanvas を共用、
  書き込み先 input を変数で切替) + 起動ボタン。名前欄なし。
- サーバー一覧カード: 行 = フォルダ名 / status バッジ(connected=緑, connecting=黄,
  unknown=灰) / Capacity `n/32` / 環境URLを開くボタン(新規タブ) / 停止ボタン(確認:
  「稼働中のセッションも一緒に終了します」)。
- 表示中のみ5秒ポーリング(既存 interval を view で分岐)。
- 409(既に稼働中)は danger ではなく info 表示で既存サーバーを見せる。

## エラーハンドリング

- ペインパース失敗 → 該当フィールド null / status "unknown"。一覧APIは常に返る。
- 起動タイムアウト → 起動自体は成功として返す(常駐は tmux が保証)。
- 存在しないフォルダ / 不正名 → 400。
- kill 対象が spawn サーバーでない → 400(チャットセッションの誤殺防止)。

## テスト

既存スタイル(unittest + mock、tmux/プロセスは全部モック)に合わせる:

- `_parse_spawn_pane`: connected+capacity+URL / ready 0/32 / connecting / ゴミ入力
- `launch_spawn_server`: 重複起動 → SpawnServerExists(既存情報入り)
- `stop_spawn_server`: spawn でないセッション名 → ValueError
- `list_sessions` の `spawn` フラグ解釈(format 6→7列化の後方互換)

## やらないこと(YAGNI)

- worktree モード切替、capacity 変更、サーバー内セッション一覧(公式TUIパース依存が深い)
- QRコード表示
- サーバー状態の永続化・自動再起動
