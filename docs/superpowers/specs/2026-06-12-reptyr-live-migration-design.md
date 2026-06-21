# 稼働中プロセスの tmux ライブ移管 (reptyr) 設計

日付: 2026-06-12
ステータス: 承認済み(会話内で合意 + 実機実証)
前提: [外部セッション検出/移管(kill+resume版)](2026-06-11-external-session-takeover-design.md) を置き換え/上位互換

## 背景 / 実証済み事実

これまでの「移管」は kill+resume(旧プロセスを SIGTERM → 同じ会話を tmux 内で
`--resume`)で、生成中の応答や TUI 上の状態は失われていた。「稼働中プロセスは
tmux に移せない」という以前の結論は **誤り** だったことを 2026-06-11〜12 に実証:

- `reptyr -T <pid>`(TTY 丸ごと強奪モード)を tmux ペイン内で実行すると、
  pty 上で動く claude を **TUI 描画・入力・応答生成・RC接続すべて生かしたまま**
  そのペインへ吸い込める。通常モード(`reptyr <pid>`)はセッションリーダー相手に
  失敗するので **必ず -T**。
- VSCode 統合ターミナル(master = vscode-server ptyHost)の実セッションでも成功。
- `sudo setcap cap_sys_ptrace+ep /usr/bin/reptyr` を一度入れれば、**sudo なし・
  tty なしのサーバから** `tmux new-session -d 'reptyr -T <pid>'` で移管できる。
- reptyr は **pid だけ** あればよい → argv に sid のないフラグレス起動も移管対象。

## アーキテクチャ

移管 = `tmux new-session -d -s <name> 'reptyr -T <pid>'`。
ペイン内の reptyr が中継役として常駐し、claude が生きている限り生存する
(claude 終了 → reptyr 終了 → ペイン閉じる、という自然な寿命)。
detached tmux セッションなので元ターミナルを閉じても切れない。

旧 kill+resume は **reptyr 不可時のフォールバック** として残す。

## サーバ API

- `reptyr_available() -> bool`: reptyr バイナリが存在し、cap_sys_ptrace を
  持つ(`getcap`)か。サーバは sudo を使わない方針なので cap がなければ False。
- `migrate_session(pid, sid="", name="") -> dict`:
  1. `pid` が「tmux 外の claude プロセス」であることを検証
     (argv[0] が claude / `_pid_in_tmux(pid)` が False)。違えば ValueError。
  2. `reptyr_available()` が False なら ValueError(フォールバック誘導メッセージ)。
  3. セッション名を決定(`name` 検証 → なければ sid 由来 → cwd 由来 → 既定)。
     `_unique_session_name` で衝突回避。
  4. `tmux new-session -d -s <name> reptyr -T <pid>` を起動。
  5. 最大 ~15 秒、claude の controlling tty が元から変わる(= 吸い込み成功)か、
     capture-pane に `/rc active` 等が出るまでポーリング。失敗なら作った tmux
     セッションを kill して RuntimeError。
  6. 成功かつ `sid`(UUID)が分かっていれば `@ccwa_sid` を刻印。
     フラグレス(sid 不明)なら刻印しない(行に会話タイトルは出ないが稼働中表示)。
- `POST /api/migrate {pid, sid?, name?}` → `migrate_session`。

## 検出の拡張

- `external_claude_sessions()`(sid 付き)はそのまま「移管候補(sid既知)」。
- **フラグレス候補**: `flagless_claude_sessions() -> list[{pid, cwd}]` を追加
  (sid なし・非tmux の claude を cwd 付きで列挙)。overview で各プロジェクトの
  `external` 配列に `{pid, sid: None, title: None, cwd}` として混ぜる。
  既存の `_flagless_claude_in_cwd`(resume ガード)はこれを使う形に再利用。

## フロントエンド

- `renderExternal` の「tmuxへ移管」ボタンを **`/api/migrate` 呼び出し** に変更。
  - sid 既知の行: `pid` と `sid` を渡す。
  - sid 不明(フラグレス)の行: `pid` のみ。タイトルは「(ターミナル起動・名前不明)」
    の体で表示し、pid を添える。
- confirm 文言を実態に合わせる:「ターミナルのプロセスを **そのまま** tmux 配下へ
  移します(終了しません・履歴も生成中の状態も保持)。数秒かかります。」
- 移管失敗(reptyr 未設定)時はエラーに setcap コマンドを案内し、旧「再開
  (kill+resume)」も使える旨を出す。

## 前提セットアップ(ホスト一度きり)

`sudo setcap cap_sys_ptrace+ep /usr/bin/reptyr`(reptyr 未導入なら
`apt-get install reptyr`)。README に追記。これが無いと migrate は使えず、
UI はフォールバックを案内する。

## 安全性

- 移管対象は「argv[0] が claude かつ非tmux」の pid のみ。それ以外は拒否。
- reptyr 失敗時は claude を巻き込まない(reptyr は attach 失敗時クリーンに諦める。
  実測で Permission denied 時も対象は生存)。作った空 tmux セッションは掃除する。
- sid 既知時のみ @ccwa_sid 刻印。誤った sid を刻まない。

## 事後修正 (2026-06-12 初回実機テストの事故)

初回実装の成功ポーリングは2点とも機能せず、**成功した移管を失敗と誤判定 →
husk 掃除の kill-session が移管済み claude を SIGHUP で殺していた**
(フォールバックの kill+resume が会話を復活させたため、ユーザーには
「失敗表示だが結果的に tmux にいる」と見えた)。合成 pty + less で再現実証済み。

実測で確定した事実と修正:

- `-T` モードでは対象の tty_nr は**変わらない**(slave はそのまま、master が
  移るだけ)→ tty 変化シグナルは削除。
- `capture-pane -t '=name'` は **解決できない**(`=` はセッション系コマンド
  専用; tmux 3.4 実測)→ `new-session -P -F '#{pane_id}'` で pane id を受け取り
  それを使う。
- 失敗した reptyr はエラーを出して即終了しセッションごと消える → 「セッション
  消滅 or 対象死亡」だけが確定失敗。**タイムアウト時に両方生きていれば成功**
  (常駐 reptyr =中継成立)。生きている husk を疑いで kill してはならない。
- 移管後のプロセスは environ が exec 時のまま(TMUX なし)なので、
  `_pid_in_tmux` では永久に「外部」扱いになる → ps から `reptyr -T <pid>` の
  対象 pid 集合(`_reptyr_target_pids`)を取り、external / flagless 両方の
  検出から除外する(これがないと移管済み行が黄色のまま重複表示され続ける)。

## 事後検証と改善 (2026-06-14 ハンズオン再現)

「移行がうまくいかない」報告を受け、合成 pty で実機を忠実に再現して目視検証した。

確定した事実:
- `reptyr -T` の成否を分ける変数は **対象 claude のセッションリーダー性ではなく、
  pty の master を握っている「端末エミュレータ」が何か**。`-T` は対象ではなく
  エミュレータを ptrace して master fd を奪うため(man: "discover the terminal
  emulator for that process' pty, and steal the master end")。
- 成功を確認した構成(すべて HTTP `POST /api/migrate` 経由・TUI/入力/生成状態が
  生存): ①合成 TUI がセッションリーダー ②本物 claude がリーダー ③本物 claude が
  対話 bash の**子**(非リーダー) ④エミュレータが pty master を **25 本**保持
  (VSCode ptyHost 相当) — いずれも正しい master を選んで移管成功。
- man 記載の唯一の確定制限: **sshd の子プロセスは root でない限り `-T` で奪えない**。
  本サーバは cap_sys_ptrace のみで非 root なので、素の SSH 端末で起動した claude は
  移管に失敗しうる(VSCode ptyHost は init 配下に reparent され該当しない)。

根本改善:
- **reptyr の stderr を捕捉して失敗理由を表面化**(`_reptyr_err_path` /
  `_drain_reptyr_err`)。従来は detached pane 内 reptyr のエラーがペインごと消え、
  失敗は「reptyr が終了しました(理由不明)」の 500 にしかならなかった
  ── これが「うまくいかない(原因が分からない)」の実体だった。今後は権限/
  sshd 配下/未対応 tty などの実際の理由がエラー文とサーバログに残る。

## 確定した真因と修正 (2026-06-14 第2弾 — VSCode Remote-SSH の実機再現)

ユーザは **VSCode Remote-SSH で WSL にログイン**。端末 claude の pty master は
**VSCode ptyHost(node, 18スレッド)** が保持。実 ptyHost 配下 claude(pid 1241358)
に reptyr -T を当てて再現:

- **reptyr -T は ptyHost から master fd を見つけられない**。stderr 実値(捕捉済):
  `[-] Unable to find the fd for the pty!`。これが「移行がうまくいかない」の真因。
- さらに悪いことに、状況によっては reptyr が **エラーも出さず常駐し続け、ペインは
  空のまま**(中継ゼロ)になる。
- 旧 `_wait_for_steal` は「タイムアウト時に session+target 両生存=成功」だった
  (2026-06-12 の capture-pane 不具合への対症)。このため **中継できていない reptyr
  常駐を『移管成功』と誤報告** → ペイン真っ白なのに成功表示 →「原因不明」。

修正(commit c92b28a):
- `_wait_for_steal`: 成功時は約1秒でペインに内容が出る。**タイムアウト到達=内容が
  一度も出なかった=失敗**(content-or-bust)。デフォルト窓 6s。失敗時の husk-kill が
  端末を奪った reptyr を解放し、対象は生存(実機確認)。
- reptyr が stderr を残さない(空ペイン)ケースは、メッセージで ptyHost 中継不可と
  kill+resume 誘導を明示。
- `migrate_all`: reptyr 中継不可かつ sid 既知なら **kill+resume にフォールバック**
  (生成途中の表示のみ消失、会話は保持)。「全部tmuxへ」がこの環境でも実機能する。
  返り値に `resumed` を追加、UI は 移管/再開/失敗 を分けて表示。

実機確認: migrate は 0.4s で正しく失敗し reptyr 実理由を表示。クリーンな sid 付き
プロセスは SIGTERM-takeover で tmux に着地。**結論: VSCode Remote-SSH では reptyr
生移管は原理的に不可。tmux 取り込みは kill+resume 経由が確実(会話は保つ)。**

## 非スコープ / 既知の限界

- setcap を入れない運用では使えない(フォールバックで旧 kill+resume を使う)。
- 素の SSH 端末(master 保持者が sshd 配下)の claude は非 root の `-T` で奪えない。
  → reptyr の stderr に理由が出るので、フォールバック誘導で対応。
- フラグレス移管は @ccwa_sid を刻めないため、ランチャー上で会話タイトルが出ない
  (pid と「稼働中」表示のみ)。一度移管すれば次回からは tmux 管理下。
- reptyr が中継役として常駐するので、その tmux セッションを壊すと端末が切れる
  (通常の tmux ペインと同じ性質。異常ではない)。
