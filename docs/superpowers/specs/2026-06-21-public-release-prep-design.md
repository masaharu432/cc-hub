# cc-hub 公開リリース準備 設計メモ

- 日付: 2026-06-21
- ステータス: 承認済み（実装プラン作成へ）

## 目的

このプロジェクト（現 `claude-code-web-app`）を GitHub で**公開**したい。ただし
個人データ（Tailscale IP・ローカルパス・ホスト名・個人のスマホVPN設定）とノイズの多い
コミット履歴が混在しており、そのままでは公開に適さない。

公開後も**プライベートで開発し、成果が出たものだけを公開へ反映する**運用を実現する。

## 採用アーキテクチャ: 1親フォルダ・2リポジトリ（並列）

```
<projects-root>/mytools/cc-hub-project/
 ├─ cc-hub/         開発(private)。claude-code-web-app をここへ移動。.git → ローカルのみ（GitHubに作らない）
 └─ cc-hub-public/  公開ミラー。cc-hub と並列。.git → public GitHub（AIが gh で作成）
```

- `cc-hub-public` は `cc-hub` の**並列フォルダ**＝ cc-hub の git ツリーに含まれない →
  **gitignore 不要**。
- 開発は `cc-hub` のみで行う（同じ Claude プロジェクトとして継続。メモリ/履歴を維持）。
- 公開は `cc-hub → cc-hub-public` への**パッチ移植**で行う。ユーザーは概念レベルの指示
  （「○○機能を公開して」「public を private に追従」）を出すだけ。機械作業は AI が担う。
- app（cc-hub 自身）からは `cc-hub` プロジェクトにアクセスする。

### なぜ「別フォルダ・別リポジトリ」か（不採用案との比較）

| 案 | 今後の開発 | Claudeプロジェクト | 公開履歴 | 複雑さ | 採否 |
|---|---|---|---|---|---|
| 1親フォルダ2repo（採用） | cc-hub で開発、AIが公開へ移植 | 同じ（移行は一度きり） | クリーン | 中 | ★採用 |
| 単一リポジトリ（履歴リセット） | 公開で直接開発 | 同じ | クリーン | 最小 | 却下（private開発の隔離が無い） |
| main非公開＋orphan public（同repo2remote） | branch往復 | 同じ | クリーン | 中〜高 | 却下（隔離が弱い） |

採用理由: ユーザーの理想は「**private で開発 → 成果を public へ**」。別リポジトリにすると
private の WIP・履歴・個人データが public へ誤って混入する経路が物理的に断たれる（強い隔離）。

## 安全を支える唯一のルール

> **追跡ファイルに個人データを置かない。個人データは gitignore 済み `config.json` だけに住む。**

これにより private/public は同一のサニタイズ済みツリーを共有でき、移植が「ほぼコピー」で済む。
そもそも追跡ファイルに個人データが無いので、公開時に漏らす対象が存在しない。

## サニタイズ対象（cc-hub・cc-hub-public 共通ツリー）

| ファイル | 現状の個人データ | 対応 |
|---|---|---|
| `README.md` | tailnet IP、ローカルパス、ホスト名、スマホVPN(広告ブロッカー/Work Profile)節 | IP→プレースホルダ（`<host-ip>`）、パス→汎用例（`/path/to/projects`）、ホスト名→「サーバ機」一般化、VPN節は「環境による」一般論へ圧縮（固有手順は削除）|
| `server.py` | `load_config()` defaults の `public_host`／`projects_root` にローカル固有値 | ニュートラルな既定へ（`public_host` は空＝自動検出にフォールバック、`projects_root` は `~` 等の汎用既定）。コメントのホスト名言及も除去。`config.json` で上書きする前提を明記 |
| `docs/superpowers/plans/*.md`（archive, external-session-takeover, reptyr） | run コマンド内の個人パス、`/home/x/` | 汎用例パスへ置換。設計内容は価値があるので**残す** |
| `tests/test_archive.py` | `/home/x/.local/bin/claude` 等のダミー | 既に汎用ダミーだが念のため確認。テストが通ることを保証 |
| `HANDOFF-auto-rename.md` | 内部作業メモ（個人パス含む） | **削除**（公開不要） |
| `config.json` / `archive.json` / `archive.json.bak-*` / `server.log` / `.claude/` | 個人ランタイム状態 | 既に `.gitignore` 済み ✅（追跡されていないことを確認） |

サニタイズ完了の定義: 個人マーカー（ユーザー名・メール・tailnet IP・ローカル絶対パス・
ホスト名）を網羅した `git grep` が（`web/vendor` を除き）**0件**になること。
具体的なパターンは実行時にスクリプト側へ持たせ、spec 本文には個人マーカーを直書きしない。

## 実行手順

1. **`prep/public-release` ブランチを切る**（現フォルダ `claude-code-web-app` のまま）。
2. **サニタイズ作業**を上表に従い実施。`tests/` が通ることを確認。ユーザーが diff レビュー →
   OK で `main` に取り込み。
3. **フォルダ移動＋migrate**: `claude-code-web-app` → `cc-hub-project/cc-hub`。
   migrate-sessions-on-folder-rename skill で以下を一括処理:
   - `~/.claude/projects/` のエンコード済み履歴ディレクトリ移設
   - セッション履歴 JSONL 内 `cwd` フィールドの書き換え
   - Claude プロジェクトのメモリ保存先ディレクトリの移設
   - app の `config.json`（`projects_root` 等）/ `archive.json` の再配置
   - 既存セッション名プレフィックス `claude-code-web-app_X` → `cc-hub_X` の置換
   - ⚠️ **リスク工程**: 稼働中サーバと実行中セッションが旧パス基準。
     **サーバ停止 → フォルダ移動 → 新パスでサーバ再起動**、の順で慎重に行う。
4. **private リポジトリ（ローカルのみ）**: `cc-hub` の `.git` はそのままローカル管理。
   **GitHub リモートは作らない／設定しない**。**既存64コミット履歴は温存**（HEAD は
   サニタイズ済みでクリーン）。普段の開発はローカル commit のみ。
5. **public リポジトリ配線**: `cc-hub-public` フォルダを作成 → `git init` → **AI が `gh` で
   public GitHub リポジトリを作成** → サニタイズ済みスナップショットを初回 publish
   （クリーンな単一の "Initial public release"）。**public が唯一の GitHub リモート**。
6. **以後の運用フロー確立**: `cc-hub → cc-hub-public` へ成果を移植する手順（必要なら補助
   スクリプト）。ユーザーは「公開して」等の概念指示のみ。

## スコープ外（YAGNI）

- private 既存履歴のスクラブ（filter-repo）。private は非公開のため不要。将来必要になれば別途。
- 公開ミラーを `~/` 直下へ出す案。app に `cc-hub-public` が一覧表示される軽微なノイズは許容。
- CI / ライセンス選定 / コントリビューションガイド等の公開リポジトリ整備は本スコープ外
  （初回公開を通すことに集中。必要なら後続タスク）。

## リスクと緩和

| リスク | 緩和 |
|---|---|
| フォルダ移動で稼働中サーバ/セッションが壊れる | サーバ停止→移動→再起動の順。migrate skill 利用。移動前に `.git` を bundle 退避 |
| サニタイズ漏れで個人データ公開 | 完了判定に `git grep` 0件チェック。public 初回 publish 前に再走査 |
| private→public 移植時のドリフト（ファイル取りこぼし） | 追跡ファイルをサニタイズ済みに保ち「ほぼ全体コピー」運用。AI が systematic に diff |
| 改名でセッション履歴孤児化 | migrate-sessions-on-folder-rename skill が cwd 書換＋移設を担保 |
