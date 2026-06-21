"use strict";
// Tiny client-side i18n: one shared string table, no server round-trip.
// Loaded BEFORE app.js. Static text is declared in index.html via
//   data-i18n="key"                  -> sets textContent
//   data-i18n-attr="placeholder:key" -> sets the named attribute(s)
// and swept in by applyI18n(); dynamic strings in app.js call t(key, params).
//
// Language is persisted in localStorage("ccwa_lang"). First visit auto-detects
// from navigator.language: Japanese device -> ja, everything else -> en.
(function () {
  const I18N = {
    en: {
      // nav
      "nav.projects": "Projects",
      "nav.search": "Search",
      "nav.archive": "Archive",
      "nav.spawn": "Spawn",
      // auth
      "auth.label": "Access token",
      "auth.placeholder": "Paste the token from config.json",
      "auth.save": "Save",
      // QR
      "qr.open_phone": "Open on your phone",
      "qr.hint": "Scan this QR with your phone's camera to open it with the token.",
      // launch form
      "launch.header": "Launch Claude Code",
      "launch.dir_placeholder": "Folder to launch in  e.g. /path/to/project",
      "launch.browse": "Browse",
      "launch.name_placeholder": "Session name (shown in RC as folder_thisName)",
      "launch.btn": "Launch",
      "launch.busy": "Launching…",
      "launch.done": "Launched",
      "launch.failed": "Launch failed",
      "launch.need_dir": "Pick a folder to launch in",
      "launch.need_name": "Enter a session name",
      "launch.launched": "Launched: {name}",
      "launch.already": "Already running: {name}",
      // sessions / overview
      "sessions.header": "Projects",
      "sessions.adopt_all": "All to tmux",
      "sessions.adopt_title": "Move every terminal-launched claude under tmux management",
      "loading": "Loading…",
      "overview.empty": "No running sessions and no conversations to resume",
      // row actions
      "action.rename": "Rename",
      "action.stop": "Stop",
      "action.archive": "Archive",
      "action.resume": "Resume",
      "action.restore": "Restore",
      "action.kill_archive_title": "Stop and archive",
      "action.restart_title": "Restart — stop and relaunch the same conversation (to reconnect RC)",
      "action.resume_title": "Stopped — tap to resume",
      "proj.usefolder_title": "Launch a new session in this folder",
      "proj.set_target": "Set as launch target: {path}",
      // session meta / badges
      "sess.attached": "terminal attached",
      "sess.untitled": "(untitled)",
      "sess.external_unknown": "(terminal-launched, name unknown)",
      "badge.external": "running (terminal)",
      "badge.running": "running",
      "badge.archived": "archived",
      "time.ago": "{age} ago",
      // migrate / adopt
      "migrate.title_known": "Stop once, then resume in tmux keeping the conversation",
      "migrate.title_flagless": "Move into tmux live via reptyr (sid unknown, so resume isn't possible)",
      "migrate.btn_known": "To tmux (stop & resume)",
      "migrate.btn_flagless": "Move to tmux",
      "migrate.done": "Moved into tmux: {name}",
      "migrate.fallback_confirm": "Migration failed: {msg}\n\nFall back to the old method? (Terminates the terminal process and resumes inside tmux. In-progress generation is lost.)",
      "adopt.result": "Adopted into tmux: {parts}",
      "adopt.migrated": "migrated {n}",
      "adopt.resumed": "resumed {n}",
      "adopt.failed": "failed {n}",
      "adopt.fail_prefix": "Failed: {items}",
      // rename prompts
      "rename.prompt_fixed": "New session name (\"{fixed}\" is fixed)",
      "rename.prompt": "New session name",
      "rename.live_note": "\n(Sends /rename to the running session)",
      "rename.done": "Renamed: {name}",
      "rename.prompt_fixed2": "New name (\"{fixed}\" is fixed)",
      "rename.prompt2": "New name",
      // resume
      "resume.done": "Resumed: {name}",
      "resume.already": "Already running: {name}",
      "resume.confirm_suffix": "\n\nResume anyway?",
      // unarchive
      "unarchive.done": "Restored to the list",
      "folder.unknown": "(folder unknown)",
      // spawn tab
      "spawn.warn_tag": "Experimental (β).",
      "spawn.warn_body": "Discouraged because the official app side is unstable. The stable way to bring a terminal claude under tmux is \"All to tmux\" on the Projects tab.",
      "spawn.launch_header": "Start Spawn server",
      "spawn.desc": "Starts, under tmux, the resident server the official app's \"new session\" lands on. Create and manage sessions from the app's pulldown (switch the default GitHub to this server).",
      "spawn.dir_placeholder": "Folder to host the server",
      "spawn.browse": "Browse",
      "spawn.launch_btn": "Start server",
      "spawn.running_header": "Running servers",
      "spawn.connected": "● connected",
      "spawn.connecting": "connecting…",
      "spawn.unknown": "unknown",
      "spawn.capacity": " · sessions {used}/{max}",
      "spawn.open_title": "Open this environment in the app/browser",
      "spawn.open": "Open",
      "spawn.stop_title": "Stop server",
      "spawn.empty": "No Spawn servers running",
      "spawn.need_dir": "Pick a folder to host the server",
      "spawn.launched": "Server started: {name}",
      "spawn.stopped": "Stopped: {name}",
      // search
      "search.header": "Search sessions",
      "search.all_projects": "All projects",
      "search.placeholder": "Search by your input / title",
      "search.btn": "Search",
      "search.help": "Searches only the messages you typed and session names",
      "search.searching": "Searching…",
      "search.prompt": "Enter a search term",
      "search.no_results": "No conversations match \"{q}\"",
      // archive tab
      "archive.header": "Archive",
      "archive.restore_hint": "Swipe or \"Restore\" to bring back to the list",
      "archive.empty": "Nothing archived",
      // folder browser
      "browser.title": "Browse folders",
      "browser.up": "Up",
      "browser.newfolder": "New folder",
      "browser.select_here": "Launch here",
      "browse.empty": "No subfolders",
      "browse.newfolder_prompt": "New folder name (created inside {path})",
      "browse.created": "Created: {path}",
      // settings
      "settings.title": "Settings",
      "settings.language": "Language",
      "settings.open_title": "Settings",
      // misc
      "err.auth_required": "Authentication required: enter a token",
    },
    ja: {
      // nav
      "nav.projects": "プロジェクト",
      "nav.search": "検索",
      "nav.archive": "アーカイブ",
      "nav.spawn": "Spawn",
      // auth
      "auth.label": "アクセストークン",
      "auth.placeholder": "config.json の token を貼り付け",
      "auth.save": "保存",
      // QR
      "qr.open_phone": "スマホで開く",
      "qr.hint": "この QR をスマホのカメラで読むと、トークン付きで開きます。",
      // launch form
      "launch.header": "Claude Code 起動",
      "launch.dir_placeholder": "起動するフォルダ　例) /path/to/project",
      "launch.browse": "参照",
      "launch.name_placeholder": "セッション名（フォルダ名_この名前 で RC に表示）",
      "launch.btn": "起動",
      "launch.busy": "起動中…",
      "launch.done": "起動しました",
      "launch.failed": "起動失敗",
      "launch.need_dir": "起動するフォルダを選んでください",
      "launch.need_name": "セッション名を入力してください",
      "launch.launched": "起動しました: {name}",
      "launch.already": "既に起動中: {name}",
      // sessions / overview
      "sessions.header": "プロジェクト",
      "sessions.adopt_all": "全部tmuxへ",
      "sessions.adopt_title": "ターミナルで動いている claude を全部 tmux 管理下へ移管",
      "loading": "読み込み中…",
      "overview.empty": "起動中のセッションも再開できる会話もありません",
      // row actions
      "action.rename": "改名",
      "action.stop": "停止",
      "action.archive": "アーカイブ",
      "action.resume": "再開",
      "action.restore": "戻す",
      "action.kill_archive_title": "停止してアーカイブ",
      "action.restart_title": "再起動 — 停止して同じ会話で起動し直す(RC再接続用)",
      "action.resume_title": "停止中 — タップで再開",
      "proj.usefolder_title": "このフォルダで新規起動",
      "proj.set_target": "起動先に設定しました: {path}",
      // session meta / badges
      "sess.attached": "端末接続中",
      "sess.untitled": "(無題)",
      "sess.external_unknown": "(ターミナル起動・名前不明)",
      "badge.external": "稼働中(ターミナル)",
      "badge.running": "稼働中",
      "badge.archived": "アーカイブ済",
      "time.ago": "{age}前",
      // migrate / adopt
      "migrate.title_known": "一度終了し、会話を保持して tmux で再開する",
      "migrate.title_flagless": "reptyr でプロセスを生かしたまま tmux へ(sid 不明なので再開不可)",
      "migrate.btn_known": "tmuxへ(終了して再開)",
      "migrate.btn_flagless": "tmuxへ移管",
      "migrate.done": "tmuxへ移管しました: {name}",
      "migrate.fallback_confirm": "移管に失敗しました: {msg}\n\n旧方式にフォールバックしますか？(ターミナル側のプロセスを終了して tmux 内で再開。生成途中の内容は失われます)",
      "adopt.result": "tmuxへ取り込み: {parts}",
      "adopt.migrated": "移管 {n} 件",
      "adopt.resumed": "再開 {n} 件",
      "adopt.failed": "失敗 {n} 件",
      "adopt.fail_prefix": "失敗: {items}",
      // rename prompts
      "rename.prompt_fixed": "新しいセッション名（「{fixed}」は固定）",
      "rename.prompt": "新しいセッション名",
      "rename.live_note": "\n(稼働中のまま /rename を送信します)",
      "rename.done": "改名しました: {name}",
      "rename.prompt_fixed2": "新しい名前（「{fixed}」は固定）",
      "rename.prompt2": "新しい名前",
      // resume
      "resume.done": "再開しました: {name}",
      "resume.already": "既に起動中: {name}",
      "resume.confirm_suffix": "\n\nそれでも再開しますか？",
      // unarchive
      "unarchive.done": "一覧に戻しました",
      "folder.unknown": "(フォルダ不明)",
      // spawn tab
      "spawn.warn_tag": "実験的(β)。",
      "spawn.warn_body": "公式アプリ側の挙動が不安定なため非推奨です。ターミナルの claude は「プロジェクト」タブの「全部tmuxへ」で tmux 管理下に取り込むのが安定した方法です。",
      "spawn.launch_header": "Spawnサーバー起動",
      "spawn.desc": "公式アプリの「新規セッション」先になる常駐サーバーを tmux で起動します。セッションの作成・管理はアプリ側のプルダウンから(デフォルトの GitHub をこのサーバーに切り替え)。",
      "spawn.dir_placeholder": "サーバーを立てるフォルダ",
      "spawn.browse": "参照",
      "spawn.launch_btn": "サーバー起動",
      "spawn.running_header": "稼働中サーバー",
      "spawn.connected": "● 接続済",
      "spawn.connecting": "接続中…",
      "spawn.unknown": "状態不明",
      "spawn.capacity": " · セッション {used}/{max}",
      "spawn.open_title": "この環境をアプリ/ブラウザで開く",
      "spawn.open": "開く",
      "spawn.stop_title": "サーバー停止",
      "spawn.empty": "稼働中の Spawn サーバーはありません",
      "spawn.need_dir": "サーバーを立てるフォルダを選んでください",
      "spawn.launched": "サーバーを起動しました: {name}",
      "spawn.stopped": "停止しました: {name}",
      // search
      "search.header": "セッション検索",
      "search.all_projects": "すべてのプロジェクト",
      "search.placeholder": "入力した内容・タイトルで検索",
      "search.btn": "検索",
      "search.help": "あなたが入力したメッセージとセッション名のみを検索します",
      "search.searching": "検索中…",
      "search.prompt": "検索ワードを入力してください",
      "search.no_results": "「{q}」に一致する会話はありません",
      // archive tab
      "archive.header": "アーカイブ",
      "archive.restore_hint": "スワイプ or 「戻す」で一覧へ復元",
      "archive.empty": "アーカイブはありません",
      // folder browser
      "browser.title": "フォルダを参照",
      "browser.up": "上へ",
      "browser.newfolder": "新規フォルダ",
      "browser.select_here": "ここを起動先にする",
      "browse.empty": "サブフォルダはありません",
      "browse.newfolder_prompt": "新規フォルダ名（{path} の中に作成）",
      "browse.created": "作成しました: {path}",
      // settings
      "settings.title": "設定",
      "settings.language": "言語",
      "settings.open_title": "設定",
      // misc
      "err.auth_required": "認証が必要です: トークンを入力してください",
    },
  };

  let lang = null;

  function detect() {
    const saved = localStorage.getItem("ccwa_lang");
    if (saved === "en" || saved === "ja") return saved;
    const nav = (navigator.language || "").toLowerCase();
    return nav.startsWith("ja") ? "ja" : "en";
  }

  function getLang() {
    if (!lang) lang = detect();
    return lang;
  }

  function setLang(l) {
    if (l !== "en" && l !== "ja") return;
    lang = l;
    localStorage.setItem("ccwa_lang", l);
    document.documentElement.lang = l;
  }

  // Look up a key in the current language (falling back to English, then the
  // raw key), then interpolate {placeholder} tokens from params.
  function t(key, params) {
    const dict = I18N[getLang()] || I18N.en;
    let s = dict[key] != null ? dict[key] : I18N.en[key] != null ? I18N.en[key] : key;
    if (params) {
      for (const k in params) s = s.split("{" + k + "}").join(String(params[k]));
    }
    return s;
  }

  // Sweep static text in the DOM. textContent for [data-i18n]; named attributes
  // for [data-i18n-attr="attr:key; attr2:key2"].
  function applyI18n(root) {
    root = root || document;
    document.documentElement.lang = getLang();
    root.querySelectorAll("[data-i18n]").forEach((el) => {
      el.textContent = t(el.getAttribute("data-i18n"));
    });
    root.querySelectorAll("[data-i18n-attr]").forEach((el) => {
      el.getAttribute("data-i18n-attr").split(";").forEach((pair) => {
        const idx = pair.indexOf(":");
        if (idx < 0) return;
        const attr = pair.slice(0, idx).trim();
        const key = pair.slice(idx + 1).trim();
        if (attr && key) el.setAttribute(attr, t(key));
      });
    });
  }

  window.I18N = I18N;
  window.t = t;
  window.getLang = getLang;
  window.setLang = setLang;
  window.applyI18n = applyI18n;
})();
