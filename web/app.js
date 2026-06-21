"use strict";
const $ = (id) => document.getElementById(id);

let TOKEN = localStorage.getItem("ccwa_token") || "";

// Pick up ?token=... on first visit, then strip it from the URL bar.
{
  const u = new URL(location.href);
  const t = u.searchParams.get("token");
  if (t) {
    TOKEN = t;
    localStorage.setItem("ccwa_token", TOKEN);
    history.replaceState({}, "", u.pathname);
  }
}

// --- helpers --------------------------------------------------------------- //
function show(msg, kind = "success") {
  // Only surface problems. Success/info confirmations used to pop up on every
  // action and were just noise on a phone, so they're suppressed; warnings and
  // errors (partial failures, real failures) still show.
  if (kind === "success" || kind === "info") return;
  // Don't stack duplicates: if the same message is already up, leave it be.
  const dupe = [...$("msgArea").children].find(
    (c) => c.dataset.kind === kind && c.dataset.msg === msg
  );
  if (dupe) return;
  const el = document.createElement("div");
  el.className = `alert alert-${kind} alert-dismissible shadow-sm py-2 mb-2`;
  el.role = "alert";
  el.dataset.kind = kind;
  el.dataset.msg = msg;
  el.innerHTML =
    `<div class="small">${msg}</div>` +
    `<button type="button" class="btn-close" data-bs-dismiss="alert"></button>`;
  $("msgArea").appendChild(el);
  if (kind === "success") setTimeout(() => el.remove(), 2500);
}

async function api(path, opts = {}) {
  opts.headers = Object.assign(
    { "X-Auth-Token": TOKEN, "Content-Type": "application/json" },
    opts.headers || {}
  );
  const r = await fetch(path, opts);
  const data = await r.json().catch(() => ({}));
  if (r.status === 401) {
    localStorage.removeItem("ccwa_token");
    TOKEN = "";
    $("auth").classList.remove("d-none"); // auth is on; let the user supply a token
    throw new Error("認証が必要です: トークンを入力してください");
  }
  if (!r.ok) {
    const err = new Error(data.error || "HTTP " + r.status);
    err.status = r.status; // lets callers branch on 409 (confirm-and-force)
    throw err;
  }
  return data;
}

function sanitizeName(s) {
  // Keep letters of any script (incl. 日本語), digits, '_' and '-'; turn spaces
  // into '_' and drop anything else. Mirrors the server's NAME_RE.
  return (s || "")
    .trim()
    .replace(/\s+/g, "_")
    .replace(/[^\p{L}\p{N}_-]/gu, "")
    .replace(/^[-_]+/, "")
    .slice(0, 64);
}

function fmtAge(ts) {
  if (!ts) return "";
  const s = Math.max(0, Math.floor(Date.now() / 1000) - ts);
  if (s < 60) return s + "s";
  if (s < 3600) return Math.floor(s / 60) + "m";
  if (s < 86400) return Math.floor(s / 3600) + "h";
  return Math.floor(s / 86400) + "d";
}

function gate() {
  // Auth is optional (the server may run open behind Tailscale), so always show
  // the app. The token card only appears if a request actually returns 401.
  $("auth").classList.add("d-none");
  $("app").classList.remove("d-none");
}

// --- launch form ----------------------------------------------------------- //
// Fixed, auto-derived "<folder>_" prefix for the session name. The user only
// types the suffix; the final name is `<folder>_<suffix>` so the project is
// recognizable in the RC app.
let namePrefix = "";

function setNamePrefix(dir) {
  const base = (dir || "").replace(/[/\\]+$/, "").split(/[/\\]/).pop() || "";
  namePrefix = sanitizeName(base);
  updateNamePreview();
}

// Combine the fixed folder prefix with the typed suffix.
function fullSessionName() {
  const suffix = sanitizeName($("name").value.trim());
  if (!namePrefix) return suffix;
  return suffix ? `${namePrefix}_${suffix}` : namePrefix;
}

// Live preview of the resulting RC name on its own line above the input.
function updateNamePreview() {
  const el = $("namePrefix");
  const full = fullSessionName();
  if (full) {
    el.textContent = "→ " + full;
    el.classList.remove("d-none");
  } else {
    el.textContent = "";
    el.classList.add("d-none");
  }
}

// Flip the 起動 button through its launch lifecycle so the ~1-2s server
// round-trip is visible. Same busy/done/err vocabulary as the live-row restart
// button (setRestartBtn), but the launch form persists rather than being
// re-rendered, so done/err flash briefly and then restore the idle state.
let launchBtnTimer = null;
function setLaunchBtn(state) {
  const btn = $("launch");
  if (!btn) return;
  clearTimeout(launchBtnTimer);
  btn.classList.remove("busy", "done", "err");
  if (state === "busy") {
    btn.classList.add("busy");
    btn.disabled = true;
    btn.innerHTML = '<i class="bi bi-arrow-clockwise"></i> 起動中…';
  } else if (state === "done") {
    btn.classList.add("done");
    btn.disabled = false;
    btn.innerHTML = '<i class="bi bi-check-lg"></i> 起動しました';
    launchBtnTimer = setTimeout(() => setLaunchBtn("idle"), 1600);
  } else if (state === "err") {
    btn.classList.add("err");
    btn.disabled = false;
    btn.innerHTML = '<i class="bi bi-exclamation-lg"></i> 起動失敗';
    launchBtnTimer = setTimeout(() => setLaunchBtn("idle"), 1600);
  } else { // idle
    btn.disabled = false;
    btn.innerHTML = '<i class="bi bi-play-fill"></i> 起動';
  }
}

async function launch() {
  const name = fullSessionName();
  const dir = $("dir").value.trim();
  if (!dir) return show("起動するフォルダを選んでください", "danger");
  if (!name) return show("セッション名を入力してください", "danger");
  setLaunchBtn("busy");
  try {
    const r = await api("/api/launch", { method: "POST", body: JSON.stringify({ name, dir }) });
    show(r.created ? `起動しました: ${r.name}` : `既に起動中: ${r.name}`);
    setLaunchBtn("done");
    refresh();
  } catch (e) {
    setLaunchBtn("err");
    show(e.message, "danger");
  }
}

// --- sessions (project-grouped) -------------------------------------------- //
function esc(s) {
  return (s || "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

// Display name for a session shown *inside* its project group: the real tmux
// name is `<folder>_<suffix>`, but the group header already names the folder,
// so we drop that prefix and show just the suffix. The full name is still used
// for rename/kill (data-name); only the visible label is shortened. Falls back
// to the full name when there's no `<folder>_` prefix (e.g. a suffix-less launch
// whose name is exactly the folder), so the row is never blank.
function projPrefix(projPath) {
  const base = (projPath || "").replace(/[/\\]+$/, "").split(/[/\\]/).pop() || "";
  return sanitizeName(base);
}

function sessDisplayName(name, projPath) {
  const prefix = projPrefix(projPath);
  if (prefix && name.startsWith(prefix + "_")) return name.slice(prefix.length + 1);
  return name;
}

// Collapse state: which project folders are expanded. `null` until first load,
// then defaults to "open the folders that have a live session". Kept across
// polls (and toggles re-render from cache, no refetch).
let expanded = null;
let lastProjects = [];

// One live session row. Same swipe UI as the stopped rows: swiping RIGHT
// uncovers 改名 (live /rename); swiping LEFT uncovers 停止 (kill the session,
// then archive its conversation). `open` controls collapse; `parent` ties it to
// its group.
function renderLive(s, parent, open) {
  // Subtitle = the last thing said in this conversation; fall back to its age.
  const meta = s.last ? esc(s.last) : `<i class="bi bi-clock"></i> ${fmtAge(s.created)}`;
  return `
    <li class="list-group-item swipe-item p-0 ${open ? "" : "d-none"}" data-parent="${esc(parent)}">
      <div class="swipe-action swipe-action-left">
        <button class="btn btn-primary" data-act="rename" data-name="${esc(s.name)}">
          <i class="bi bi-pencil"></i>改名</button>
      </div>
      <div class="swipe-action swipe-action-right">
        <button class="btn btn-danger" data-act="killarchive"
                data-name="${esc(s.name)}" data-id="${esc(s.id || "")}" title="停止してアーカイブ">
          <i class="bi bi-archive"></i>停止</button>
      </div>
      <div class="swipe-front d-flex align-items-center gap-2 ps-3 pe-2 py-2" data-swipe-id="${esc(s.id || s.name)}">
        <span class="dot on flex-none"></span>
        <span class="sess-meta flex-grow-1">
          <span class="sess-name d-block">${esc(sessDisplayName(s.name, parent))}</span>
          <span class="d-block text-secondary small text-truncate">
            ${meta}${s.attached ? ' · <i class="bi bi-display"></i> 端末接続中' : ""}
          </span>
        </span>
        <button class="btn btn-sm sess-restart flex-none" data-act="restart"
                data-name="${esc(s.name)}" data-id="${esc(s.id || "")}" data-dir="${esc(parent)}"
                title="再起動 — 停止して同じ会話で起動し直す(RC再接続用)"><i class="bi bi-arrow-clockwise"></i></button>
      </div>
    </li>`;
}

// One resumable (past, not-running) conversation row. Structure: the visible
// content sits on a .swipe-front layer the swipe gesture translates sideways.
// Swiping RIGHT uncovers the left-edge 改名 action; swiping LEFT uncovers the
// right-edge アーカイブ action (see attachSwipe). Tap the resume icon to reopen.
function renderResume(r, dir, open) {
  // The persisted custom-title is the full `<folder>_<suffix>` launcher name,
  // so strip the folder prefix here too; the last-message fallback is left as-is.
  const title = r.title ? sessDisplayName(r.title, dir) : "";
  const label = title || r.last || "(無題)";
  const sub = r.title && r.last ? r.last : ""; // show last message under the title
  return `
    <li class="list-group-item swipe-item p-0 ${open ? "" : "d-none"}" data-parent="${esc(dir)}">
      <div class="swipe-action swipe-action-left">
        <button class="btn btn-primary" data-act="rename"
                data-id="${esc(r.id)}" data-dir="${esc(dir)}" data-title="${esc(r.title || "")}">
          <i class="bi bi-pencil"></i>改名</button>
      </div>
      <div class="swipe-action swipe-action-right">
        <button class="btn btn-secondary" data-act="archive" data-id="${esc(r.id)}">
          <i class="bi bi-archive"></i>アーカイブ</button>
      </div>
      <div class="swipe-front d-flex align-items-center gap-2 ps-4 pe-2 py-2" data-swipe-id="${esc(r.id)}">
        <span class="sess-meta flex-grow-1">
          <span class="d-block sess-name fw-normal">${esc(label)}</span>
          <span class="d-block text-secondary small">
            ${sub ? esc(sub) + " · " : ""}<i class="bi bi-clock-history"></i> ${fmtAge(r.modified)}前
          </span>
        </span>
        <button class="btn btn-sm sess-resume flex-none"
                data-act="resume" data-id="${esc(r.id)}" data-dir="${esc(dir)}"
                data-name="${esc(sanitizeName(r.title || ""))}" title="停止中 — タップで再開"><i class="bi bi-arrow-counterclockwise"></i></button>
      </div>
    </li>`;
}

// One conversation running OUTSIDE tmux (started by hand in a terminal). Not
// swipeable / archivable — the offered action is a live migration into tmux
// (reptyr -T): the process is NOT killed; TUI, in-flight generation and the
// RC bridge all survive. Flagless rows (no sid in argv) have id/title null.
function renderExternal(c, dir, open) {
  const title = c.title ? sessDisplayName(c.title, dir) : "";
  const label = title || c.last || (c.id ? "(無題)" : "(ターミナル起動・名前不明)");
  return `
    <li class="list-group-item d-flex align-items-center gap-2 ps-4 pe-2 ${open ? "" : "d-none"}" data-parent="${esc(dir)}">
      <span class="sess-meta flex-grow-1">
        <span class="d-block sess-name fw-normal">${esc(label)}</span>
        <span class="d-block text-secondary small">
          <span class="badge text-bg-warning">稼働中(ターミナル)</span> · pid ${esc(String(c.pid))}
        </span>
      </span>
      <button class="btn btn-sm btn-outline-warning flex-none"
              data-act="migrate" data-pid="${esc(String(c.pid))}" data-id="${esc(c.id || "")}"
              data-dir="${esc(dir)}" data-name="${esc(sanitizeName(c.title || ""))}"
              title="${c.id ? "一度終了し、会話を保持して tmux で再開する" : "reptyr でプロセスを生かしたまま tmux へ(sid 不明なので再開不可)"}">
        <i class="bi bi-box-arrow-in-down"></i> ${c.id ? "tmuxへ(終了して再開)" : "tmuxへ移管"}</button>
    </li>`;
}

// One project group: a clickable folder header + its (collapsible) rows.
function renderProject(p) {
  const open = expanded.has(p.path);
  const live = p.sessions.map((s) => renderLive(s, p.path, open)).join("");
  const ext = (p.external || []).map((c) => renderExternal(c, p.path, open)).join("");
  const resume = p.resumable.map((r) => renderResume(r, p.path, open)).join("");
  return `
    <li class="list-group-item bg-body-tertiary ps-2 pe-2" role="button" data-proj="${esc(p.path)}">
      <div class="d-flex align-items-center gap-2">
        <i class="bi bi-chevron-${open ? "down" : "right"} text-secondary flex-none"></i>
        <i class="bi bi-folder2-open text-warning flex-none"></i>
        <span class="fw-semibold flex-grow-1 text-truncate">${esc(p.name)}</span>
        ${p.sessions.length ? `<span class="badge text-bg-success rounded-pill">● ${p.sessions.length}</span>` : ""}
        ${(p.external || []).length ? `<span class="badge text-bg-warning rounded-pill">⌨ ${p.external.length}</span>` : ""}
        ${p.resumable.length ? `<span class="badge text-bg-secondary rounded-pill">↺ ${p.resumable.length}</span>` : ""}
        <button class="btn btn-sm btn-outline-success flex-none" data-act="usefolder" data-dir="${esc(p.path)}" title="このフォルダで新規起動"><i class="bi bi-plus-lg"></i></button>
      </div>
      <span class="d-block text-secondary small font-monospace text-break ${open ? "" : "d-none"}" data-parent="${esc(p.path)}">${esc(p.path)}</span>
    </li>
    ${live}${ext}${resume}`;
}

// Count terminal-launched claudes across all projects (the external rows) and
// toggle the "全部tmuxへ" bulk-adopt button — the one-tap pivot to tmux mgmt.
function updateAdoptAll() {
  const n = lastProjects.reduce((s, p) => s + (p.external || []).length, 0);
  const btn = $("adoptAll");
  if (!btn) return;
  btn.classList.toggle("d-none", n === 0);
  $("adoptCount").textContent = n ? ` (${n})` : "";
}

async function adoptAllTerminals() {
  const n = lastProjects.reduce((s, p) => s + (p.external || []).length, 0);
  if (!n) return;
  const btn = $("adoptAll");
  btn.disabled = true;
  try {
    const r = await api("/api/migrate-all", { method: "POST", body: "{}" });
    const ok = r.migrated.length, rs = (r.resumed || []).length, ng = r.failed.length;
    // migrated = reptyr で生きたまま / resumed = 中継不可で kill+resume(生成中表示のみ消失)
    const parts = [`移管 ${ok} 件`];
    if (rs) parts.push(`再開 ${rs} 件`);
    if (ng) parts.push(`失敗 ${ng} 件`);
    show("tmuxへ取り込み: " + parts.join(" / "), ng ? "warning" : "success");
    if (ng) show("失敗: " + r.failed.map((f) => `pid ${f.pid} (${f.error})`).join(" / "), "danger");
    refresh();
  } catch (e) {
    show(e.message, "danger");
  } finally {
    btn.disabled = false;
  }
}

function renderOverview() {
  updateAdoptAll();
  const el = $("sessions");
  // Don't blow away a row the user has swiped open: a 5s background poll calls
  // this and the innerHTML rebuild would destroy the open row's DOM, snapping
  // the revealed button shut mid-interaction. Skip the rebuild while an open
  // row is still on-screen here; action handlers clear openFront first, so
  // their own re-renders (archive/rename) still go through.
  if (openFront && el.contains(openFront)) return;
  openFront = null; // a rebuild makes fresh, closed rows
  if (!lastProjects.length) {
    el.innerHTML =
      '<li class="list-group-item text-secondary small">起動中のセッションも再開できる会話もありません</li>';
    return;
  }
  el.innerHTML = lastProjects.map(renderProject).join("");
}

// Pick a project's folder as the target for "新規起動": fill the dir field,
// derive the name prefix, and scroll up to the launch form so the user can
// just type a suffix and hit 起動.
function useFolder(path) {
  $("dir").value = path;
  setNamePrefix(path);
  // Scroll the launch card into view so its header ("Claude Code 起動") and the
  // "→ name" preview are both visible. A plain block:"start" tucks the card top
  // under the sticky tab bar, hiding the title — so offset by the bar's height.
  // Focusing the name field here would pop the mobile keyboard and scroll the
  // preview back off-screen, so we don't auto-focus.
  const card = $("launchCard");
  const tabs = document.getElementById("viewTabs");
  card.style.scrollMarginTop = (tabs ? tabs.offsetHeight + 6 : 0) + "px";
  card.scrollIntoView({ behavior: "smooth", block: "start" });
  show("起動先に設定しました: " + path);
}

// Toggle a project's collapse state and re-render from cache (no refetch).
function toggleProject(path) {
  if (expanded.has(path)) expanded.delete(path);
  else expanded.add(path);
  renderOverview();
}

async function refresh({ silent = false } = {}) {
  try {
    const { projects } = await api("/api/overview");
    lastProjects = projects;
    if (expanded === null) {
      // First load: open folders that have a live session, collapse the rest.
      expanded = new Set(projects.filter((p) => p.sessions.length).map((p) => p.path));
    }
    renderOverview();
  } catch (e) {
    // Background polling fails whenever the tab is asleep or the network
    // flaps; staying quiet there avoids piling up "Failed to fetch" toasts.
    if (!silent) show(e.message, "danger");
  }
}

async function renameSession(old, dir) {
  // Only the suffix is editable: the `<folder>_` prefix is fixed at launch and
  // must stay aligned with the project group (same rule as the launch form).
  const prefix = projPrefix(dir);
  const fixed = prefix && old.startsWith(prefix + "_") ? prefix + "_" : "";
  const oldSuffix = fixed ? old.slice(fixed.length) : old;
  const next = prompt(
    (fixed ? `新しいセッション名（「${fixed}」は固定）` : "新しいセッション名") +
      "\n(稼働中のまま /rename を送信します)",
    oldSuffix
  );
  if (next == null) return;
  const suffix = next.trim();
  if (!suffix || suffix === oldSuffix) return;
  const full = fixed + suffix;
  try {
    const r = await api("/api/rename", { method: "POST", body: JSON.stringify({ old, new: full }) });
    show(`改名しました: ${r.name}`);
    refresh();
  } catch (e) {
    show(e.message, "danger");
  }
}

// Rename a STOPPED conversation (no live session). The server appends a new
// custom-title to the log, so the resume/archive list picks it up on refresh.
// Same `<folder>_` prefix rule as live rename: only the suffix is editable.
async function renameResumable(id, dir, currentTitle) {
  openFront = null; // acting on the open row; let the follow-up re-render through
  const prefix = projPrefix(dir);
  const title = currentTitle || "";
  const fixed = prefix && title.startsWith(prefix + "_") ? prefix + "_" : "";
  const oldSuffix = fixed ? title.slice(fixed.length) : title;
  const next = prompt(
    fixed ? `新しい名前（「${fixed}」は固定）` : "新しい名前",
    oldSuffix
  );
  if (next == null) return;
  const suffix = sanitizeName(next.trim());
  if (!suffix) return;
  const full = fixed + suffix;
  if (full === title) return;
  try {
    const r = await api("/api/rename-conversation", {
      method: "POST",
      body: JSON.stringify({ id, new: full }),
    });
    show(`改名しました: ${r.name}`);
    if (currentView === "archive") loadArchive();
    else refresh();
  } catch (e) {
    show(e.message, "danger");
  }
}

// Restart a live session: kill its tmux+RC session, then resume the SAME
// conversation as a fresh one. The RC app sometimes stops connecting to a
// long-lived session even with --remote-control set; a fresh launch rebuilds
// the bridge. Stop-only isn't offered — a stopped session with a dead RC
// bridge is useless. Archiving stays on the swipe action.
// Flip a restart button between visual states so a tap visibly "does something"
// across the ~1-2s kill+resume round-trip. busy = amber spinner (disabled),
// done = green check flash, err = red flash. Restoring isn't needed for
// done/err: refresh() re-renders the row from scratch shortly after.
function setRestartBtn(btn, state) {
  if (!btn) return;
  btn.classList.remove("busy", "done", "err");
  const icon = btn.querySelector(".bi");
  if (state === "busy") {
    btn.classList.add("busy");
    btn.disabled = true;
    if (icon) icon.className = "bi bi-arrow-clockwise";
  } else if (state === "done") {
    btn.classList.add("done");
    if (icon) icon.className = "bi bi-check-lg";
  } else if (state === "err") {
    btn.classList.add("err");
    if (icon) icon.className = "bi bi-exclamation-lg";
  }
}

async function restartSession(name, id, dir, btn) {
  setRestartBtn(btn, "busy");
  try {
    await api("/api/kill", { method: "POST", body: JSON.stringify({ name }) });
    // Resume reuses the conversation (dir/id) under the same name, spinning up
    // a new tmux + RC session the app can reconnect to. resumeConversation
    // handles its own success/error toast + refresh; passing btn lets it flash
    // the done/err state before that refresh wipes the row.
    await resumeConversation(dir, id, name, false, btn);
  } catch (e) {
    setRestartBtn(btn, "err");
    show(e.message, "danger");
    refresh({ silent: true }); // kill failed → bring the row back
  }
}

// Stop a live session AND archive its conversation in one swipe action. Kill
// first (so archive's live-session guard passes), then hide the now-stopped
// conversation. Optimistic: the row vanishes immediately. If there's no sid we
// can only stop it (nothing to archive).
async function killArchiveSession(name, id, row) {
  openFront = null;
  if (row) row.remove(); // optimistic: vanish immediately, don't wait on the API
  try {
    await api("/api/kill", { method: "POST", body: JSON.stringify({ name }) });
    if (id) {
      // Killed → no longer live, so the archive guard now accepts it. Non-fatal
      // if it fails: the session is already stopped, it just stays visible.
      try {
        await api("/api/archive", { method: "POST", body: JSON.stringify({ id }) });
      } catch (e) {
        show(e.message, "danger");
      }
    }
    refresh({ silent: true });
  } catch (e) {
    show(e.message, "danger");
    refresh({ silent: true }); // kill failed → bring the row back
  }
}

// Resume a past conversation as a fresh tmux + Remote Control session.
// 409 = the server's flagless guard: a sid-less terminal claude runs in the
// same folder and might BE this conversation — confirm, then retry forced.
async function resumeConversation(dir, id, name, force = false, btn = null) {
  try {
    const r = await api("/api/resume", {
      method: "POST",
      body: JSON.stringify({ dir, id, name, force }),
    });
    show(r.created ? `再開しました: ${r.name}` : `既に起動中: ${r.name}`);
    // Restart flow: flash the green "done" state and hold it briefly so the
    // user sees the restart finished, then refresh (which re-renders the row).
    if (btn) {
      setRestartBtn(btn, "done");
      setTimeout(() => refresh(), 800);
    } else {
      refresh();
    }
  } catch (e) {
    if (e.status === 409 && !force) {
      if (confirm(e.message + "\n\nそれでも再開しますか？"))
        resumeConversation(dir, id, name, true, btn);
      else setRestartBtn(btn, "err");
      return;
    }
    setRestartBtn(btn, "err");
    show(e.message, "danger");
  }
}

// Archive = hide from the list. The jsonl is untouched and the archive tab
// can always restore, so no confirm and no undo affordance — just a note.
async function archiveConversation(id, row) {
  openFront = null; // acting on the open row; let the follow-up re-render through
  if (row) row.remove(); // optimistic: vanish immediately, don't wait on the API
  try {
    await api("/api/archive", { method: "POST", body: JSON.stringify({ id }) });
    refresh({ silent: true }); // resync badge counts etc.
  } catch (e) {
    show(e.message, "danger");
    refresh({ silent: true }); // failed → bring the row back
  }
}

async function unarchiveConversation(id) {
  openFront = null; // acting on the open row; let loadArchive rebuild through
  try {
    await api("/api/unarchive", { method: "POST", body: JSON.stringify({ id }) });
    show("一覧に戻しました");
    loadArchive(); // only called from the archive tab, where this re-renders
  } catch (e) {
    show(e.message, "danger");
  }
}

// Live-migrate a terminal-launched claude into tmux (reptyr -T): the process
// keeps running — TUI, in-flight generation and the RC bridge all survive.
// On failure (typically reptyr missing its capability) the server's message
// carries the setcap one-liner; when the conversation id is known we offer
// the old kill+resume takeover as a fallback.
async function migrateConversation(pid, sid, dir, name) {
  try {
    const r = await api("/api/migrate", {
      method: "POST",
      body: JSON.stringify({ pid: Number(pid), sid, name }),
    });
    show(`tmuxへ移管しました: ${r.name}`);
    refresh();
  } catch (e) {
    if (sid && confirm(`移管に失敗しました: ${e.message}\n\n旧方式にフォールバックしますか？(ターミナル側のプロセスを終了して tmux 内で再開。生成途中の内容は失われます)`)) {
      takeoverConversation(dir, sid, name);
      return;
    }
    show(e.message, "danger");
  }
}

// Old takeover (kill+resume): SIGTERM the terminal process, then resume the
// same conversation inside tmux. Only reachable as the migrate fallback now,
// so the caller has already confirmed — no own confirm here.
async function takeoverConversation(dir, id, name) {
  try {
    const r = await api("/api/resume", {
      method: "POST",
      body: JSON.stringify({ dir, id, name, takeover: true }),
    });
    show(`tmuxへ移管しました: ${r.name}`);
    refresh();
  } catch (e) {
    show(e.message, "danger");
  }
}

// Adopt a sid-known terminal claude into tmux by terminate+resume (the user's
// chosen method: reptyr live-steal can't work under VSCode Remote-SSH, so don't
// even try it for a known conversation — just kill it and resume in tmux).
// In-flight TUI/generation is lost; the conversation itself is preserved.
async function adoptConversation(dir, id, name) {
  takeoverConversation(dir, id, name);
}

// --- swipe to reveal actions ----------------------------------------------- //
// Pointer Events so touch and mouse drag both work. Direction lock: the
// gesture only claims the pointer once horizontal movement clearly dominates
// (>10px slop and |dx| > |dy|); otherwise native vertical scrolling proceeds
// (touch-action: pan-y on .swipe-front). Instead of committing, the front
// SNAPS OPEN to one action width — revealing the left-edge action when swiped
// right, the right-edge action when swiped left — and stays there until the
// revealed button is tapped, the row is tapped again, or another row opens.
const SWIPE_ACTION_W = 88; // px; matches .swipe-action .btn min-width
let openFront = null;      // the single currently-revealed .swipe-front (or null)

function frontTranslate(f) {
  const m = /translateX\((-?[\d.]+)px\)/.exec(f.style.transform || "");
  return m ? parseFloat(m[1]) : 0;
}

function closeSwipe(f) {
  if (!f) return;
  f.classList.add("swipe-anim");
  f.style.transform = "";
  if (openFront === f) openFront = null;
}

function attachSwipe(listEl) {
  let front = null;   // the .swipe-front being dragged (null = no gesture)
  let pid = null;     // pointer id, so multi-touch can't mix gestures
  let startX = 0, startY = 0, baseX = 0, dx = 0, locked = false;

  listEl.addEventListener("pointerdown", (e) => {
    const f = e.target.closest(".swipe-front");
    if (!f || e.target.closest("button")) return; // buttons keep their taps
    if (openFront && openFront !== f) closeSwipe(openFront); // one open at a time
    front = f; pid = e.pointerId;
    startX = e.clientX; startY = e.clientY;
    baseX = (openFront === f) ? frontTranslate(f) : 0; // drag on from open state
    dx = 0; locked = false;
    f.classList.remove("swipe-anim"); // follow the finger raw, no easing
  });

  listEl.addEventListener("pointermove", (e) => {
    if (!front || e.pointerId !== pid) return;
    const mx = e.clientX - startX, my = e.clientY - startY;
    if (!locked) {
      if (Math.abs(mx) < 10) return;                       // inside slop
      if (Math.abs(mx) <= Math.abs(my)) { front = null; return; } // vertical → scroll
      locked = true;
      front.setPointerCapture(pid);
    }
    dx = mx;
    // Clamp to a single action width in either direction.
    const t = Math.max(-SWIPE_ACTION_W, Math.min(SWIPE_ACTION_W, baseX + dx));
    front.style.transform = `translateX(${t}px)`;
  });

  listEl.addEventListener("pointerup", (e) => {
    if (!front || e.pointerId !== pid) return;
    const f = front;
    front = null;
    if (!locked) {
      // A plain tap (no drag) on an open row closes it again.
      if (openFront === f) closeSwipe(f);
      return;
    }
    f.classList.add("swipe-anim");
    const t = baseX + dx;
    if (t <= -SWIPE_ACTION_W / 2) {
      f.style.transform = `translateX(${-SWIPE_ACTION_W}px)`; // reveal right action
      openFront = f;
    } else if (t >= SWIPE_ACTION_W / 2) {
      f.style.transform = `translateX(${SWIPE_ACTION_W}px)`;  // reveal left action
      openFront = f;
    } else {
      f.style.transform = "";                                // not far enough → close
      if (openFront === f) openFront = null;
    }
  });

  listEl.addEventListener("pointercancel", (e) => {
    if (!front || e.pointerId !== pid) return;
    closeSwipe(front);
    front = null;
  });
}

// --- spawn servers tab ------------------------------------------------------ //
// One resident `claude remote-control` per project folder; the official app
// creates sessions ON it. We only ignite / list / link / stop — session
// management is the app's job.
function spawnStatusBadge(s) {
  if (s.status === "connected") return '<span class="badge text-bg-success">● 接続済</span>';
  if (s.status === "connecting") return '<span class="badge text-bg-warning">接続中…</span>';
  return '<span class="badge text-bg-secondary">状態不明</span>';
}

function renderSpawnRow(s) {
  const cap =
    s.capacity_used != null && s.capacity_max != null
      ? ` · セッション ${s.capacity_used}/${s.capacity_max}`
      : "";
  // The environment-scoped link lands directly on this server in the app /
  // claude.ai/code — bypasses the new-session pulldown defaulting to GitHub.
  const link = s.env_url
    ? `<a class="btn btn-sm btn-outline-success flex-none" href="${esc(s.env_url)}"
         target="_blank" rel="noopener" title="この環境をアプリ/ブラウザで開く"><i class="bi bi-box-arrow-up-right"></i> 開く</a>`
    : "";
  return `
    <li class="list-group-item d-flex align-items-center gap-2 ps-2 pe-2">
      <span class="sess-meta flex-grow-1">
        <span class="sess-name d-block">${esc(s.folder || s.name)}</span>
        <span class="d-block text-secondary small text-truncate">
          ${spawnStatusBadge(s)}${cap} · <span class="font-monospace">${esc(s.directory || "")}</span>
        </span>
      </span>
      ${link}
      <button class="btn btn-sm sess-kill flex-none" data-act="spawnstop" data-name="${esc(s.name)}"
              title="サーバー停止"><i class="bi bi-x-lg"></i></button>
    </li>`;
}

async function refreshSpawn({ silent = false } = {}) {
  try {
    const { servers } = await api("/api/spawn-servers");
    $("spawnList").innerHTML = servers.length
      ? servers.map(renderSpawnRow).join("")
      : '<li class="list-group-item text-secondary small">稼働中の Spawn サーバーはありません</li>';
  } catch (e) {
    if (!silent) show(e.message, "danger");
  }
}

async function launchSpawn() {
  const dir = $("spawnDir").value.trim();
  if (!dir) return show("サーバーを立てるフォルダを選んでください", "danger");
  const btn = $("spawnLaunch");
  btn.disabled = true; // the POST waits (up to ~15s) for the environment URL
  try {
    const r = await api("/api/spawn-servers", {
      method: "POST",
      body: JSON.stringify({ dir }),
    });
    show(`サーバーを起動しました: ${r.server.folder || r.server.name}`);
    refreshSpawn({ silent: true });
  } catch (e) {
    if (e.status === 409) {
      // Already running for this folder — informational, not a failure.
      show(e.message, "info");
      refreshSpawn({ silent: true });
    } else {
      show(e.message, "danger");
    }
  } finally {
    btn.disabled = false;
  }
}

async function stopSpawn(name) {
  try {
    await api("/api/spawn-servers/" + encodeURIComponent(name), { method: "DELETE" });
    show("停止しました: " + name);
    refreshSpawn({ silent: true });
  } catch (e) {
    show(e.message, "danger");
  }
}

// --- folder browser -------------------------------------------------------- //
let browserOC = null;
let curPath = "";
let curParent = null;
// Which input the browser fills on 決定 — the launch form ("dir") or the spawn
// tab ("spawnDir"). The name-prefix preview only applies to the launch form.
let browserTargetId = "dir";

async function browseTo(path) {
  try {
    const data = await api("/api/browse?path=" + encodeURIComponent(path || ""));
    curPath = data.path;
    curParent = data.parent;
    $("bcPath").textContent = curPath;
    $("browseUp").disabled = !curParent;
    const el = $("browseList");
    if (!data.entries.length) {
      el.innerHTML = '<li class="list-group-item text-secondary small">サブフォルダはありません</li>';
      return;
    }
    el.innerHTML = data.entries
      .map(
        (d) => `
      <li class="list-group-item list-group-item-action d-flex align-items-center gap-2" data-path="${d.path}">
        <i class="bi bi-folder-fill text-warning"></i>
        <span class="folder-name flex-grow-1">${d.name}</span>
        ${d.git ? '<span class="badge text-bg-secondary"><i class="bi bi-git"></i></span>' : ""}
        <i class="bi bi-chevron-right text-secondary"></i>
      </li>`
      )
      .join("");
  } catch (e) {
    show(e.message, "danger");
  }
}

function openBrowser(targetId = "dir") {
  browserTargetId = targetId;
  const start = $(targetId).value.trim() || "";
  if (!browserOC) browserOC = new bootstrap.Offcanvas($("browser"));
  browseTo(start);
  browserOC.show();
}

async function newFolder() {
  const name = prompt("新規フォルダ名（" + curPath + " の中に作成）");
  if (!name) return;
  try {
    const r = await api("/api/mkdir", {
      method: "POST",
      body: JSON.stringify({ parent: curPath, name: name.trim() }),
    });
    show("作成しました: " + r.path);
    browseTo(curPath); // refresh listing; new folder appears
  } catch (e) {
    show(e.message, "danger");
  }
}

function selectHere() {
  $(browserTargetId).value = curPath;
  if (browserTargetId === "dir") setNamePrefix(curPath);
  if (browserOC) browserOC.hide();
}

// --- QR for phone (PC widths) ---------------------------------------------- //
async function renderQR() {
  if (typeof qrcode === "undefined") return; // lib missing
  try {
    const info = await api("/api/info");
    // Only carry a token in the URL when auth is actually in use.
    const url = `http://${info.host}:${info.port}/${TOKEN ? "?token=" + TOKEN : ""}`;
    $("qrUrl").textContent = url;
    const qr = qrcode(0, "M"); // type 0 = auto-size, error-correction M
    qr.addData(url);
    qr.make();
    $("qr").innerHTML = qr.createSvgTag({ cellSize: 5, margin: 2, scalable: true });
    const svg = $("qr").querySelector("svg");
    if (svg) { svg.style.width = "148px"; svg.style.height = "148px"; }
  } catch (e) {
    /* QR is a convenience; ignore failures */
  }
}

// --- view router ----------------------------------------------------------- //
// Adding a tab = (1) a nav button in #viewTabs, (2) a [data-view] container in
// index.html, (3) an entry here mapping the view name to its onShow hook.
let currentView = "projects";
const VIEWS = {
  projects: { onShow: () => refresh({ silent: true }) },
  spawn: { onShow: () => refreshSpawn({ silent: true }) },
  search: { onShow: onSearchShow },
  archive: { onShow: loadArchive },
};

function showView(name) {
  if (!VIEWS[name]) return;
  currentView = name;
  // Only the view containers (direct children of #app) — NOT the nav buttons,
  // which also carry data-view and would otherwise hide their own tab.
  document.querySelectorAll("#app > [data-view]").forEach((el) =>
    el.classList.toggle("d-none", el.dataset.view !== name)
  );
  document.querySelectorAll("#viewTabs .nav-link").forEach((b) =>
    b.classList.toggle("active", b.dataset.view === name)
  );
  if (VIEWS[name].onShow) VIEWS[name].onShow();
}

// --- search ---------------------------------------------------------------- //
let lastSearchResults = null; // null = not searched yet, [] = no results
let lastSearchQuery = "";

// Fill the project filter <select> from the overview's projects, keeping the
// current selection across overview refreshes.
function populateSearchProjects() {
  const sel = $("searchProject");
  const prev = sel.value;
  sel.innerHTML = ['<option value="">すべてのプロジェクト</option>']
    .concat(lastProjects.map((p) => `<option value="${esc(p.path)}">${esc(p.name)}</option>`))
    .join("");
  sel.value = prev;
}

function onSearchShow() {
  populateSearchProjects();
  renderSearchResults(); // re-render whatever state we have (prompt / last results)
}

async function runSearch() {
  const q = $("searchInput").value.trim();
  lastSearchQuery = q;
  if (!q) {
    lastSearchResults = null;
    renderSearchResults();
    return;
  }
  $("searchResults").innerHTML =
    '<li class="list-group-item text-secondary small">検索中…</li>';
  try {
    const params = new URLSearchParams({ q });
    const project = $("searchProject").value;
    if (project) params.set("project", project);
    lastSearchResults = (await api("/api/search?" + params.toString())).results;
  } catch (e) {
    show(e.message, "danger");
    lastSearchResults = [];
  }
  renderSearchResults();
}

// Highlight matches by escaping both haystack and needle, then wrapping matches
// in <mark>. Both sides are escaped, so no raw user text reaches innerHTML.
function highlight(text, query) {
  const safe = esc(text);
  if (!query) return safe;
  const q = esc(query).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return safe.replace(new RegExp(q, "gi"), (m) => `<mark>${m}</mark>`);
}

function renderSearchRow(r) {
  const title = r.title ? sessDisplayName(r.title, r.cwd) : "";
  const label = title || r.snippet || "(無題)";
  const sub =
    r.snippet && r.snippet !== label
      ? `<span class="d-block text-secondary small">${highlight(r.snippet, lastSearchQuery)}</span>`
      : "";
  const badge = r.archived
    ? ' <span class="badge text-bg-secondary fw-normal">アーカイブ済</span>'
    : "";
  // Running sessions are already live (the chat is in the RC app) — show a
  // status badge, not a 再開 button. Terminal-launched ones get a badge too
  // (take-over lives in the project list, not here). Only stopped
  // conversations can be resumed.
  const action = r.running
    ? '<span class="badge text-bg-success rounded-pill flex-none">● 稼働中</span>'
    : r.external
      ? '<span class="badge text-bg-warning rounded-pill flex-none">稼働中(ターミナル)</span>'
      : `<button class="btn btn-sm btn-outline-success flex-none"
              data-act="resume" data-id="${esc(r.id)}" data-dir="${esc(r.cwd)}"
              data-name="${esc(sanitizeName(r.title || ""))}">再開</button>`;
  return `
    <li class="list-group-item d-flex align-items-center gap-2">
      <i class="bi bi-chat-left-text text-secondary flex-none"></i>
      <span class="sess-meta flex-grow-1">
        <span class="d-block sess-name">${highlight(label, lastSearchQuery)}${badge}</span>
        ${sub}
        <span class="d-block text-secondary small">
          <i class="bi bi-folder2"></i> ${esc(r.project)} ·
          <i class="bi bi-clock-history"></i> ${fmtAge(r.modified)}前
        </span>
      </span>
      ${action}
    </li>`;
}

function renderSearchResults() {
  const el = $("searchResults");
  if (lastSearchResults === null) {
    el.innerHTML =
      '<li class="list-group-item text-secondary small">検索ワードを入力してください</li>';
    return;
  }
  if (!lastSearchResults.length) {
    el.innerHTML = `<li class="list-group-item text-secondary small">「${esc(lastSearchQuery)}」に一致する会話はありません</li>`;
    return;
  }
  el.innerHTML = lastSearchResults.map(renderSearchRow).join("");
}

// --- archive tab ------------------------------------------------------------ //
async function loadArchive() {
  const el = $("archiveList");
  // Same open-swipe guard as renderOverview: don't rebuild over a row the user
  // has swiped open (this tab also re-renders after actions / on show).
  if (openFront && el.contains(openFront)) return;
  try {
    const { archived } = await api("/api/archived");
    if (openFront && el.contains(openFront)) return; // re-check after the await
    openFront = null;
    if (!archived.length) {
      el.innerHTML = '<li class="list-group-item text-secondary small">アーカイブはありません</li>';
      return;
    }
    el.innerHTML = archived.map(renderArchiveRow).join("");
  } catch (e) {
    show(e.message, "danger");
  }
}

// Same swipe-front structure as renderResume, so attachSwipe works here too.
// In this tab swiping RIGHT uncovers 改名; swiping LEFT uncovers 戻す (restore).
function renderArchiveRow(c) {
  const label = (c.title ? sessDisplayName(c.title, c.cwd) : "") || c.last || "(無題)";
  const proj = c.cwd ? c.cwd.replace(/[/\\]+$/, "").split(/[/\\]/).pop() : "(フォルダ不明)";
  return `
    <li class="list-group-item swipe-item p-0">
      <div class="swipe-action swipe-action-left">
        <button class="btn btn-primary" data-act="rename"
                data-id="${esc(c.id)}" data-dir="${esc(c.cwd)}" data-title="${esc(c.title || "")}">
          <i class="bi bi-pencil"></i>改名</button>
      </div>
      <div class="swipe-action swipe-action-right">
        <button class="btn btn-secondary" data-act="unarchive" data-id="${esc(c.id)}">
          <i class="bi bi-arrow-counterclockwise"></i>戻す</button>
      </div>
      <div class="swipe-front d-flex align-items-center gap-2 ps-2 pe-2 py-2" data-swipe-id="${esc(c.id)}">
        <span class="sess-meta flex-grow-1">
          <span class="d-block sess-name fw-normal">${esc(label)}</span>
          <span class="d-block text-secondary small">
            <i class="bi bi-folder2"></i> ${esc(proj)} · <i class="bi bi-clock-history"></i> ${fmtAge(c.modified)}前
          </span>
        </span>
        <button class="btn btn-sm btn-outline-success flex-none"
                data-act="resume" data-id="${esc(c.id)}" data-dir="${esc(c.cwd)}"
                data-name="${esc(sanitizeName(c.title || ""))}">再開</button>
      </div>
    </li>`;
}

// --- wiring ---------------------------------------------------------------- //
function init() {
  // Auth is optional now; load regardless of whether a token is set.
  refresh();
  renderQR();
}

$("saveToken").addEventListener("click", () => {
  TOKEN = $("token").value.trim();
  localStorage.setItem("ccwa_token", TOKEN);
  gate();
  init();
});
$("launch").addEventListener("click", launch);
$("refresh").addEventListener("click", () => refresh());
$("adoptAll").addEventListener("click", adoptAllTerminals);
$("dir").addEventListener("input", () => setNamePrefix($("dir").value));
$("name").addEventListener("input", updateNamePreview);
$("openBrowser").addEventListener("click", () => openBrowser("dir"));
$("spawnLaunch").addEventListener("click", launchSpawn);
$("spawnRefresh").addEventListener("click", () => refreshSpawn());
$("spawnBrowse").addEventListener("click", () => openBrowser("spawnDir"));
$("spawnList").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-act='spawnstop']");
  if (btn) stopSpawn(btn.dataset.name);
});
$("browseUp").addEventListener("click", () => curParent && browseTo(curParent));
$("newFolder").addEventListener("click", newFolder);
$("selectHere").addEventListener("click", selectHere);

// event delegation for session row buttons and folder rows
$("sessions").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-act]");
  if (btn) {
    if (btn.dataset.act === "rename") {
      // Stopped rows carry data-id (rename via log append); live rows carry the
      // tmux data-name (rename via live /rename).
      if (btn.dataset.id)
        renameResumable(btn.dataset.id, btn.dataset.dir, btn.dataset.title);
      else
        renameSession(btn.dataset.name, btn.closest("li")?.dataset.parent);
    }
    else if (btn.dataset.act === "restart")
      restartSession(btn.dataset.name, btn.dataset.id, btn.dataset.dir, btn);
    else if (btn.dataset.act === "killarchive")
      killArchiveSession(btn.dataset.name, btn.dataset.id, btn.closest("li"));
    else if (btn.dataset.act === "resume")
      resumeConversation(btn.dataset.dir, btn.dataset.id, btn.dataset.name);
    else if (btn.dataset.act === "archive")
      archiveConversation(btn.dataset.id, btn.closest("li"));
    else if (btn.dataset.act === "migrate") {
      // sid-known -> terminate+resume directly; flagless -> reptyr (only option).
      if (btn.dataset.id)
        adoptConversation(btn.dataset.dir, btn.dataset.id, btn.dataset.name);
      else
        migrateConversation(btn.dataset.pid, btn.dataset.id, btn.dataset.dir, btn.dataset.name);
    }
    else if (btn.dataset.act === "usefolder") useFolder(btn.dataset.dir);
    return;
  }
  const header = e.target.closest("li[data-proj]");
  if (header) toggleProject(header.dataset.proj);
});
$("browseList").addEventListener("click", (e) => {
  const row = e.target.closest("li[data-path]");
  if (row) browseTo(row.dataset.path);
});

// Swipe a stopped conversation row sideways to reveal 改名 / アーカイブ.
attachSwipe($("sessions"));

$("archiveList").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-act]");
  if (!btn) return;
  if (btn.dataset.act === "unarchive") unarchiveConversation(btn.dataset.id);
  else if (btn.dataset.act === "rename")
    renameResumable(btn.dataset.id, btn.dataset.dir, btn.dataset.title);
  else if (btn.dataset.act === "resume")
    resumeConversation(btn.dataset.dir, btn.dataset.id, btn.dataset.name);
});
// Archive tab uses the same gesture to reveal 改名 / 戻す.
attachSwipe($("archiveList"));

// view tabs
$("viewTabs").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-view]");
  if (btn) showView(btn.dataset.view);
});

// search: submit-only (Enter / button); re-run on project filter change
$("searchForm").addEventListener("submit", (e) => {
  e.preventDefault();
  runSearch();
});
$("searchProject").addEventListener("change", () => {
  if (lastSearchQuery) runSearch();
});
$("searchResults").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-act='resume']");
  if (btn) resumeConversation(btn.dataset.dir, btn.dataset.id, btn.dataset.name);
});

// Long-press a project header to set it as the launch target (same as the +
// button). `lpTimer` fires after the hold; `lpFired` suppresses the click that
// follows the release so the press doesn't also toggle the group.
let lpTimer = null;
let lpFired = false;
function lpStart(e) {
  const header = e.target.closest("li[data-proj]");
  if (!header || e.target.closest("button")) return;
  lpFired = false;
  lpTimer = setTimeout(() => {
    lpFired = true;
    useFolder(header.dataset.proj);
  }, 500);
}
function lpCancel() {
  if (lpTimer) { clearTimeout(lpTimer); lpTimer = null; }
}
$("sessions").addEventListener("pointerdown", lpStart);
$("sessions").addEventListener("pointerup", lpCancel);
$("sessions").addEventListener("pointercancel", lpCancel);
$("sessions").addEventListener("pointermove", lpCancel);
// Swallow the click that a long-press would otherwise trigger (toggle).
$("sessions").addEventListener("click", (e) => {
  if (lpFired) { e.stopImmediatePropagation(); lpFired = false; }
}, true);

gate();
init();

// Poll for session changes, but only while the tab is actually visible.
// A backgrounded/sleeping tab can't fetch, so polling there only produces a
// pile of "Failed to fetch" errors; we skip it and resync on return instead.
setInterval(() => {
  if (document.hidden) return;
  if (currentView === "projects") refresh({ silent: true });
  else if (currentView === "spawn") refreshSpawn({ silent: true });
}, 5000);
document.addEventListener("visibilitychange", () => {
  if (document.hidden) return;
  if (currentView === "projects") refresh({ silent: true });
  else if (currentView === "spawn") refreshSpawn({ silent: true });
});
