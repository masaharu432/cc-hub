# 外部セッション検出と tmux 乗っ取り 設計

日付: 2026-06-11
ステータス: 承認済み(会話内で合意)
前提: [会話アーカイブ設計](2026-06-11-conversation-archive-design.md) の上に積む

## 背景 / 実測事実

ランチャーの「稼働中」判定は tmux のみ。ターミナルから直接起動した Claude
セッションは「停止中(再開可能)」と表示され、**同じ会話を二重起動できてしまう**。

実測で確認した事実(2026-06-11):
- Claude は会話 jsonl を**開きっぱなしにしない**(append→close)。fuser/lsof/
  /proc/fd では生存判定不可。mtime も「アイドル中の生存」と「終了」を区別できない。
- 一方、プロセスの argv には `--session-id <uuid>` / `--resume <uuid>` が
  そのまま見える → **ps スキャンで会話⇔プロセスを紐付けできる**。
- フラグなしの素の `claude` 起動は argv に sid が出ない → 検出対象外(限界として明記)。
- 動作中のプロセスを後から tmux 内へ移すことは事実上不可能(reptyr 系は TUI で
  壊れる)。現実解は **SIGTERM → 同じ会話を tmux 内で `--resume`**(=乗っ取り)。
  会話は jsonl に残っているので継続する。

## 検出(サーバ)

- `ps -eo pid=,args=` を1回実行し、`--session-id <uuid>` または `--resume <uuid>`
  を持つ claude プロセス行から `{sid: pid}` を作る(argv[0] が claude バイナリの
  行のみ — tmux サーバの argv に claude コマンド文字列が残るため)。
- 除外は2段: ① tmux 管理下の sid(`@ccwa_sid` 集合)、② `/proc/<pid>/environ`
  に `TMUX=` を持つプロセス(=tmux ペイン生まれ)。
  ②が必要な理由(2026-06-11 実測): `kill-session` 後も中の claude は SIGHUP
  処理で約1秒生き残り、その間 tmux セッションは消えているので①では拾えず、
  kill 直後の即時リフレッシュで毎回「外部稼働」と誤表示されていた。環境変数は
  死にかけのプロセスにも残るため、この窓を確実に塞げる。
  (副作用として launcher 外の手動 tmux 起動も「外部」扱いしなくなったが、
  これは許容 — このユーザーのワークフローに手動 tmux claude は存在しない)

## API / 既存関数の変更

- `build_overview()`: 外部稼働の会話を resumable から外し、プロジェクトに
  `external` 配列(`{id, pid, title, last, modified, cwd}`)として付与。
- `resume_session(..., takeover=False)`:
  - 外部稼働 sid への resume は takeover なし → ValueError(400)
    「ターミナルで稼働中」。
  - `takeover: true` → SIGTERM → 最大10秒ポーリングで終了待ち → 通常の
    tmux resume フロー。終了しなければ RuntimeError(500)。**SIGKILL はしない**。
- `archive_conversation()`: 外部稼働も拒否(tmux 稼働中と同じ理由)。
- `/api/search`: `external: true/false` を付与し、フロントは稼働中扱いにする。

## フロントエンド

- プロジェクトグループ内に外部稼働の行を表示: 黄色系バッジ
  「稼働中(ターミナル)」+ ボタン「tmuxへ移管」。スワイプ・アーカイブ対象外。
- 「tmuxへ移管」は confirm を挟む:
  「ターミナル側のプロセスを終了して tmux 内で再開します。応答の生成中だった
  場合、生成途中の内容は失われます。」→ OK で `/api/resume` に `takeover: true`。
- 検索結果: external ヒットは「稼働中(ターミナル)」バッジ、再開ボタンは出さない
  (移管はプロジェクト一覧から)。

## 安全性

- kill 対象は「argv に対象 sid を持つ claude プロセス」のみ。cwd 一致などの
  推測では絶対に kill しない。
- SIGTERM のみ。タイムアウト時はエラーを返してユーザーに委ねる。

## 非スコープ → 事後対応 (2026-06-11 追記)

フラグなし起動の会話特定は引き続き不可能だが、実際に二重起動事故
(62c575b1: フラグレスのターミナルセッションを「停止中」と誤分類 → 再開で
同一会話が2プロセス化)が起きたため、2段の緩和策を入れた:

1. **ソフトガード**: resume 時、対象会話と同じ cwd でフラグなし・非tmux の
   claude が稼働中なら `MaybeLiveError` → HTTP 409。フロントは確認ダイアログを
   出し、了承時のみ `force: true` で再送。kill はしない(誤殺リスクのため
   表示・乗っ取りは引き続き行わない)。
2. **起動作法**: ターミナル起動用 alias `cdr`(~/.bashrc)に
   `--session-id $(uuidgen)` を追加。以後の手動起動は argv に sid が乗るため
   ps 検出の対象になり、「稼働中(ターミナル)」表示と「tmuxへ移管」が機能する。
