# 会話アーカイブ(非表示)機能 実装プラン

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 停止中の会話を jsonl を消さずに一覧から隠せるようにする(スワイプ操作 + PC幅ボタン + アーカイブタブ + undo トースト)。

**Architecture:** サーバは `archive.json` に非表示 id のリストを持ち(`threading.Lock` + アトミック書き)、`/api/archive` `/api/unarchive` `/api/archived` の3エンドポイントを追加。`build_overview` が再開可能リストからアーカイブ済み id を除外する。フロントは Pointer Events のスワイプエンジン(方向ロック、50%/フリックで確定)+ PC幅(≥768px)のみのアーカイブボタン + アーカイブタブ。

**Tech Stack:** Python 3 stdlib(unittest でストアをテスト)、vanilla JS + Bootstrap 5(vendored)。フロントは手動検証。

**Spec:** `docs/superpowers/specs/2026-06-11-conversation-archive-design.md`

**前提知識(コードベースに固有):**
- `server.py` は1ファイル構成。ルート直下から `import server` できる。
- サーバは長寿命プロセス。**編集後は必ず再起動**(古いコードを掴んだまま)。再起動: `pkill -f "python3 server.py"` → `nohup ./run.sh > /tmp/ccwa.log 2>&1 &`(run.sh はポート二重 bind を拒否)。
- API は `X-Auth-Token` ヘッダ必須(`config.json` の `token`。空文字なら認証なし)。curl 例: `TOKEN=$(python3 -c "import json;print(json.load(open('config.json'))['token'])")`
- コミットメッセージは conventional commits ではなく平叙文(例: "Add session search with extensible tab UI")。

---

### Task 1: サーバ — アーカイブストア(TDD)

**Files:**
- Modify: `server.py`(`_unique_session_name` の後、`# Conversation history` セクションの後ろに新セクションを追加 — `_find_conversation_log` / `_read_conversation` に依存するため、それらの**定義より後**に置くこと)
- Create: `tests/__init__.py`(空ファイル)
- Create: `tests/test_archive.py`

- [ ] **Step 1: 失敗するテストを書く**

`tests/__init__.py` を空で作成し、`tests/test_archive.py` に以下を書く:

```python
"""Archive store tests. The store is pure file+lock logic, so we point the
module-level paths at a temp dir and stub out tmux (list_sessions)."""
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

import server


def make_log(projects_dir: Path, sid: str) -> Path:
    """Minimal conversation jsonl that _read_conversation accepts (cwd + one
    real user message so `last` is non-None)."""
    d = projects_dir / "proj"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{sid}.jsonl"
    p.write_text(
        json.dumps(
            {"cwd": "/tmp/x", "type": "user",
             "message": {"role": "user", "content": "hello"}}
        ) + "\n"
    )
    return p


class ArchiveStoreTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.projects = root / "claude-projects"
        self.projects.mkdir()
        self.archive_path = root / "archive.json"
        for patch in (
            mock.patch.object(server, "ARCHIVE_PATH", self.archive_path),
            mock.patch.object(server, "CLAUDE_PROJECTS", self.projects),
            mock.patch.object(server, "list_sessions", return_value=[]),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.sid = str(uuid.uuid4())
        make_log(self.projects, self.sid)

    def test_load_empty_when_no_file(self):
        self.assertEqual(server.load_archived(), set())

    def test_archive_then_load_roundtrip(self):
        server.archive_conversation(self.sid)
        self.assertEqual(server.load_archived(), {self.sid})

    def test_archive_is_idempotent(self):
        server.archive_conversation(self.sid)
        server.archive_conversation(self.sid)
        data = json.loads(self.archive_path.read_text())
        self.assertEqual(data["archived"], [self.sid])

    def test_unarchive_removes_and_unknown_id_is_noop(self):
        server.archive_conversation(self.sid)
        server.unarchive_conversation(self.sid)
        self.assertEqual(server.load_archived(), set())
        server.unarchive_conversation(str(uuid.uuid4()))  # must not raise

    def test_rejects_invalid_uuid(self):
        with self.assertRaises(ValueError):
            server.archive_conversation("not-a-uuid")
        with self.assertRaises(ValueError):
            server.unarchive_conversation("not-a-uuid")

    def test_rejects_live_session(self):
        with mock.patch.object(
            server, "list_sessions", return_value=[{"id": self.sid}]
        ):
            with self.assertRaises(ValueError):
                server.archive_conversation(self.sid)

    def test_corrupt_file_treated_as_empty(self):
        self.archive_path.write_text("{ not json")
        self.assertEqual(server.load_archived(), set())

    def test_load_prunes_ids_whose_log_is_gone(self):
        gone = str(uuid.uuid4())  # no jsonl on disk for this id
        server.archive_conversation(self.sid)
        ids = json.loads(self.archive_path.read_text())["archived"] + [gone]
        self.archive_path.write_text(json.dumps({"archived": ids}))
        self.assertEqual(server.load_archived(), {self.sid})
        # and the prune was persisted, not just filtered in memory
        data = json.loads(self.archive_path.read_text())
        self.assertEqual(data["archived"], [self.sid])

    def test_list_archived_conversations_returns_metadata(self):
        server.archive_conversation(self.sid)
        rows = server.list_archived_conversations()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], self.sid)
        self.assertEqual(rows[0]["cwd"], "/tmp/x")
        self.assertEqual(rows[0]["last"], "hello")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `cd /path/to/cc-hub && python3 -m unittest tests.test_archive -v`
Expected: 全テスト ERROR — `AttributeError: <module 'server'> does not have the attribute 'ARCHIVE_PATH'`(mock.patch.object が未定義属性で落ちる)

- [ ] **Step 3: ストアを実装**

`server.py` の `_norm_path` 関数の直後(`build_overview` の前)に以下のセクションを追加:

```python
# --------------------------------------------------------------------------- #
# Archive (hide) state — view-state only. The conversation jsonl files are
# never touched; archive.json just records which *stopped* conversations the
# UI should hide. Not a session DB (tmux stays the source of truth for live
# sessions). Kept as JSON+lock, not SQLite: this server is the only writer
# (the port bind forbids a second instance), so an in-process lock plus the
# ensure_trusted-style atomic replace is all the exclusion that exists to need.
# --------------------------------------------------------------------------- #
ARCHIVE_PATH = HERE / "archive.json"
_ARCHIVE_LOCK = threading.Lock()


def _read_archive_file() -> list[str]:
    """Raw id list from archive.json; missing/corrupt file counts as empty
    (never blocks startup), but corruption is logged. Callers hold the lock."""
    try:
        data = json.loads(ARCHIVE_PATH.read_text())
        return [i for i in data.get("archived", []) if isinstance(i, str)]
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError, AttributeError) as e:
        _log("archive.json unreadable (%r); treating as empty" % e)
        return []


def _write_archive_file(ids: list[str]) -> None:
    """Atomic write (tmp + replace) so a crash can't truncate the file."""
    tmp = ARCHIVE_PATH.with_name(ARCHIVE_PATH.name + ".tmp")
    tmp.write_text(json.dumps({"archived": ids}, indent=2) + "\n")
    os.replace(tmp, ARCHIVE_PATH)


def load_archived() -> set[str]:
    """Current archived ids, self-pruned: an id whose conversation log no
    longer exists (Claude's cleanupPeriodDays removed it) is dropped and the
    file rewritten, so archive.json cannot grow forever."""
    with _ARCHIVE_LOCK:
        ids = _read_archive_file()
        kept = [i for i in ids if _find_conversation_log(i)]
        if kept != ids:
            _write_archive_file(kept)
        return set(kept)


def archive_conversation(sid: str) -> None:
    """Hide a stopped conversation. Live sessions are rejected: archiving one
    would make it vanish the moment it's killed, which reads as data loss."""
    if not UUID_RE.match(sid or ""):
        raise ValueError("Invalid session id.")
    if any(s.get("id") == sid for s in list_sessions()):
        raise ValueError("稼働中のセッションはアーカイブできません。先に終了してください。")
    with _ARCHIVE_LOCK:
        ids = _read_archive_file()
        if sid not in ids:
            ids.append(sid)
            _write_archive_file(ids)


def unarchive_conversation(sid: str) -> None:
    """Un-hide. Idempotent: an id that isn't archived is a no-op, not an
    error (the undo toast may race a prune or a double-tap)."""
    if not UUID_RE.match(sid or ""):
        raise ValueError("Invalid session id.")
    with _ARCHIVE_LOCK:
        ids = _read_archive_file()
        if sid in ids:
            ids.remove(sid)
            _write_archive_file(ids)


def list_archived_conversations() -> list[dict]:
    """Metadata rows for the archive tab, newest-first. Reuses the same
    _read_conversation extraction the overview uses."""
    out = []
    for sid in load_archived():
        p = _find_conversation_log(sid)
        c = _read_conversation(p) if p else None
        if c:
            out.append(c)
    out.sort(key=lambda c: c["modified"], reverse=True)
    return out
```

- [ ] **Step 4: テストが通ることを確認**

Run: `python3 -m unittest tests.test_archive -v`
Expected: `Ran 9 tests ... OK`

- [ ] **Step 5: コミット**

```bash
git add server.py tests/__init__.py tests/test_archive.py
git commit -m "Add archive store: hidden-id list in archive.json with self-prune"
```

---

### Task 2: サーバ — HTTPエンドポイントと既存ビューへの組み込み

**Files:**
- Modify: `server.py` — `build_overview()`(convos ループ)、`resume_session()`(冒頭)、`do_GET`(/api/search の直前に /api/archived)、`do_POST`(/api/resume の後)

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_archive.py` の `ArchiveStoreTests` クラスの後に追加:

```python
class OverviewFilterTests(unittest.TestCase):
    """build_overview must hide archived conversations from the resumable
    list. tmux and the conversation scan are stubbed."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.projects = root / "claude-projects"
        self.projects.mkdir()
        for patch in (
            mock.patch.object(server, "ARCHIVE_PATH", root / "archive.json"),
            mock.patch.object(server, "CLAUDE_PROJECTS", self.projects),
            mock.patch.object(server, "list_sessions", return_value=[]),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.kept = str(uuid.uuid4())
        self.hidden = str(uuid.uuid4())
        make_log(self.projects, self.kept)
        make_log(self.projects, self.hidden)

    def test_archived_id_disappears_from_resumable(self):
        server.archive_conversation(self.hidden)
        out = server.build_overview()
        ids = [c["id"] for p in out for c in p["resumable"]]
        self.assertIn(self.kept, ids)
        self.assertNotIn(self.hidden, ids)
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `python3 -m unittest tests.test_archive.OverviewFilterTests -v`
Expected: FAIL — `self.hidden` が ids に残っている(`assertNotIn` で落ちる)

- [ ] **Step 3: 既存関数に組み込む**

(a) `build_overview()` の convos ループを変更。現状:

```python
    for c in convos:
        if c["id"] in live_ids:
            continue
```

変更後(`archived` の取得はループ直前に追加):

```python
    archived = load_archived()
    for c in convos:
        # Hidden = archived; a live session is never hidden (archive rejects
        # live ids, and the resumable list already excludes live ones anyway).
        if c["id"] in live_ids or c["id"] in archived:
            continue
```

(b) `resume_session()` — `dpath.is_dir()` チェックの直後、"Already running?" ループの前に追加:

```python
    # Resuming an archived conversation un-archives it: resume is the clearest
    # possible "I want this back" signal, and without this the session would
    # silently vanish from the list again the next time it's killed.
    unarchive_conversation(session_id)
```

(c) `do_GET` — `if path == "/api/search":` の**直前**に追加:

```python
                if path == "/api/archived":
                    return self._send_json({"archived": list_archived_conversations()})
```

(d) `do_GET` の /api/search 内 — `r["project"] = ...` を含む for ループに1行追加。`by_id = ...` の行の直後に `archived_ids = load_archived()` を入れ、ループ内の `r["running"] = bool(live)` の後に:

```python
                        r["archived"] = r["id"] in archived_ids
```

(e) `do_POST` — `/api/resume` ブロックの後、`return self._send_json({"error": "not found"}, ...)` の前に追加:

```python
            if path == "/api/archive":
                archive_conversation(data.get("id", ""))
                return self._send_json({"ok": True})
            if path == "/api/unarchive":
                unarchive_conversation(data.get("id", ""))
                return self._send_json({"ok": True})
```

- [ ] **Step 4: テストが通ることを確認**

Run: `python3 -m unittest tests.test_archive -v`
Expected: `Ran 10 tests ... OK`

- [ ] **Step 5: サーバを再起動して curl で実機確認**

```bash
pkill -f "python3 server.py"; sleep 1
nohup ./run.sh > /tmp/ccwa.log 2>&1 & sleep 1
TOKEN=$(python3 -c "import json;print(json.load(open('config.json'))['token'])")
# 停止中の会話の id をひとつ取る
SID=$(curl -s -H "X-Auth-Token: $TOKEN" localhost:8765/api/overview | python3 -c "import json,sys;ps=json.load(sys.stdin)['projects'];print(next(c['id'] for p in ps for c in p['resumable']))")
# archive → overview から消える / archived に出る
curl -s -X POST -H "X-Auth-Token: $TOKEN" -d "{\"id\":\"$SID\"}" localhost:8765/api/archive
curl -s -H "X-Auth-Token: $TOKEN" localhost:8765/api/overview | grep -c "$SID"   # → 0
curl -s -H "X-Auth-Token: $TOKEN" localhost:8765/api/archived | grep -c "$SID"  # → 1
# unarchive → 戻る
curl -s -X POST -H "X-Auth-Token: $TOKEN" -d "{\"id\":\"$SID\"}" localhost:8765/api/unarchive
curl -s -H "X-Auth-Token: $TOKEN" localhost:8765/api/overview | grep -c "$SID"  # → 1
# 不正 id → 400
curl -s -o /dev/null -w "%{http_code}\n" -X POST -H "X-Auth-Token: $TOKEN" -d '{"id":"bad"}' localhost:8765/api/archive  # → 400
```

Expected: コメントの通りのカウント/ステータス。稼働中セッションがあれば、その id で archive → 400 も確認。

- [ ] **Step 6: コミット**

```bash
git add server.py tests/test_archive.py
git commit -m "Wire archive into the API: archive/unarchive/archived + overview filter"
```

---

### Task 3: フロント — アーカイブ実行・undoトースト・PC幅ボタン

**Files:**
- Modify: `web/app.js` — `show()` の後に `showUndo()`、`resumeConversation()` の後にアーカイブ2関数、`renderResume()` 全置換、`#sessions` クリック委譲に1分岐

- [ ] **Step 1: undoトーストとAPI呼び出しを実装**

`web/app.js` の `show()` 関数の直後に追加:

```js
// Like show(), but with an action button (undo). No auto-dedupe: each archive
// gets its own toast so each undo targets the right conversation.
function showUndo(msg, label, onAction) {
  const el = document.createElement("div");
  el.className = "alert alert-secondary shadow-sm py-2 mb-2 d-flex align-items-center gap-2";
  el.role = "alert";
  el.innerHTML =
    `<div class="small flex-grow-1">${esc(msg)}</div>` +
    `<button type="button" class="btn btn-sm btn-outline-light flex-none">${esc(label)}</button>`;
  el.querySelector("button").addEventListener("click", () => {
    el.remove();
    onAction();
  });
  $("msgArea").appendChild(el);
  setTimeout(() => el.remove(), 5000);
}
```

`resumeConversation()` の直後に追加:

```js
// Archive = hide from the list. The jsonl is untouched, so this is always
// undoable — hence no confirm, just a 5s undo toast.
async function archiveConversation(id, row) {
  try {
    await api("/api/archive", { method: "POST", body: JSON.stringify({ id }) });
    if (row) row.remove(); // the list refresh below will rebuild anyway
    showUndo("アーカイブしました", "元に戻す", async () => {
      try {
        await api("/api/unarchive", { method: "POST", body: JSON.stringify({ id }) });
        refresh();
      } catch (e) {
        show(e.message, "danger");
      }
    });
    refresh({ silent: true });
  } catch (e) {
    show(e.message, "danger");
    refresh({ silent: true }); // bring back a row a failed swipe slid out
  }
}

async function unarchiveConversation(id) {
  try {
    await api("/api/unarchive", { method: "POST", body: JSON.stringify({ id }) });
    show("一覧に戻しました");
    loadArchive(); // re-render the archive tab (defined in the archive-tab task)
  } catch (e) {
    show(e.message, "danger");
  }
}
```

注意: `loadArchive` は Task 5 で定義する。Task 3 の時点では未定義だが、`unarchiveConversation` はアーカイブタブからしか呼ばれないため実行時エラーにはならない。

- [ ] **Step 2: renderResume にボタンを追加(スワイプ用構造への変更も同時に行う)**

`renderResume()` を全置換(Task 4 のスワイプが使う `swipe-item`/`swipe-front` 構造をここで入れておく。`<li>` を `p-0` にして padding は front 層へ移す):

```js
// One resumable (past, not-running) conversation row. Structure: the visible
// content sits on a .swipe-front layer that the swipe gesture translates,
// revealing the .swipe-under archive cue behind it (see attachSwipe).
function renderResume(r, dir, open) {
  // The persisted custom-title is the full `<folder>_<suffix>` launcher name,
  // so strip the folder prefix here too; the last-message fallback is left as-is.
  const title = r.title ? sessDisplayName(r.title, dir) : "";
  const label = title || r.last || "(無題)";
  const sub = r.title && r.last ? r.last : ""; // show last message under the title
  return `
    <li class="list-group-item swipe-item p-0 ${open ? "" : "d-none"}" data-parent="${esc(dir)}">
      <div class="swipe-under"><i class="bi bi-archive"></i><i class="bi bi-archive"></i></div>
      <div class="swipe-front d-flex align-items-center gap-2 ps-4 pe-2 py-2" data-swipe-id="${esc(r.id)}">
        <span class="sess-meta flex-grow-1">
          <span class="d-block sess-name fw-normal">${esc(label)}</span>
          <span class="d-block text-secondary small">
            ${sub ? esc(sub) + " · " : ""}<i class="bi bi-clock-history"></i> ${fmtAge(r.modified)}前
          </span>
        </span>
        <button class="btn btn-sm btn-outline-secondary flex-none d-none d-md-inline-flex align-items-center"
                data-act="archive" data-id="${esc(r.id)}" title="アーカイブ（一覧から隠す）"><i class="bi bi-archive"></i></button>
        <button class="btn btn-sm sess-resume flex-none"
                data-act="resume" data-id="${esc(r.id)}" data-dir="${esc(dir)}"
                data-name="${esc(sanitizeName(r.title || ""))}" title="停止中 — タップで再開"><i class="bi bi-arrow-counterclockwise"></i></button>
      </div>
    </li>`;
}
```

- [ ] **Step 3: クリック委譲に archive 分岐を追加**

`$("sessions").addEventListener("click", ...)` 内の分岐に追加(`resume` の分岐の後):

```js
    else if (btn.dataset.act === "archive")
      archiveConversation(btn.dataset.id, btn.closest("li"));
```

- [ ] **Step 4: 最小CSSを追加(レイアウト崩れ防止)**

`web/app.css` 末尾に追加(スワイプの動きは Task 4 だが、`p-0` 化した行の土台はここで):

```css
/* Swipe-to-archive rows: the visible content (.swipe-front) slides over a
   fixed cue layer (.swipe-under). Rows opt in via .swipe-item + p-0; padding
   moves onto the front layer so the cue can fill the whole row. */
.swipe-item { position: relative; overflow: hidden; }
.swipe-under {
  position: absolute; inset: 0;
  display: flex; align-items: center; justify-content: space-between;
  padding: 0 1.25rem;
  background: var(--bs-tertiary-bg);
  color: var(--bs-secondary-color);
}
.swipe-front {
  position: relative;
  background: var(--bs-card-bg);
  touch-action: pan-y; /* the row owns horizontal panning; vertical stays native */
}
.swipe-front.swipe-anim { transition: transform .18s ease-out; }
```

- [ ] **Step 5: ブラウザで確認**

PC ブラウザで `http://localhost:8765` を開き(またはリロード):
- ≥768px: 停止中行にアーカイブボタンが見える → 押すと行が消え「アーカイブしました — 元に戻す」トースト → 元に戻すで行が復活
- ウィンドウを <768px に縮める: ボタンが消える
- 行の見た目(余白・背景)が変更前と同等

Expected: 上記すべて成立。`curl -s -H "X-Auth-Token: $TOKEN" localhost:8765/api/archived` でも追従を確認できる。

- [ ] **Step 6: コミット**

```bash
git add web/app.js web/app.css
git commit -m "Add archive action with undo toast and a desktop-width button"
```

---

### Task 4: フロント — スワイプジェスチャ

**Files:**
- Modify: `web/app.js` — `archiveConversation` 群の後にスワイプエンジン、配線部(`$("sessions").addEventListener("click", ...)` の近く)に attach 呼び出し

- [ ] **Step 1: スワイプエンジンを実装**

`unarchiveConversation()` の直後に追加:

```js
// --- swipe to archive ------------------------------------------------------ //
// Pointer Events so touch and mouse drag both work. Direction lock: the
// gesture only claims the pointer once horizontal movement clearly dominates
// (>10px slop and |dx| > |dy|); otherwise native vertical scrolling proceeds
// (touch-action: pan-y on .swipe-front). Release past half the row width, or
// a fast flick, commits; anything less springs back.
function attachSwipe(listEl, onCommit) {
  let front = null;   // the .swipe-front being dragged (null = no gesture)
  let pid = null;     // pointer id, so multi-touch can't mix gestures
  let startX = 0, startY = 0, startT = 0, dx = 0, locked = false;

  listEl.addEventListener("pointerdown", (e) => {
    const f = e.target.closest(".swipe-front");
    if (!f || e.target.closest("button")) return; // buttons keep their taps
    front = f; pid = e.pointerId;
    startX = e.clientX; startY = e.clientY; startT = performance.now();
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
    front.style.transform = `translateX(${dx}px)`;
  });

  listEl.addEventListener("pointerup", (e) => {
    if (!front || e.pointerId !== pid) return;
    const f = front;
    front = null;
    if (!locked) return;
    const w = f.offsetWidth;
    const flick = Math.abs(dx) / Math.max(1, performance.now() - startT) > 0.8; // px/ms
    f.classList.add("swipe-anim");
    if (Math.abs(dx) >= w / 2 || flick) {
      f.style.transform = `translateX(${dx < 0 ? -w : w}px)`; // finish the slide
      setTimeout(() => onCommit(f.dataset.swipeId, f.closest("li")), 180);
    } else {
      f.style.transform = ""; // spring back
    }
  });

  listEl.addEventListener("pointercancel", (e) => {
    if (!front || e.pointerId !== pid) return;
    front.classList.add("swipe-anim");
    front.style.transform = "";
    front = null;
  });
}
```

- [ ] **Step 2: セッション一覧に配線**

配線セクション(`$("browseList").addEventListener(...)` の後)に追加:

```js
// Swipe a stopped conversation row sideways to archive it.
attachSwipe($("sessions"), (id, row) => archiveConversation(id, row));
```

- [ ] **Step 3: ブラウザで確認**

PC ブラウザ(マウスドラッグ)+ できればスマホ実機で:
- 停止中行を横に半分以上ドラッグ → 行がスライドアウトしてアーカイブ、undo トースト
- 少しだけドラッグして離す → スプリングバックし、何も起きない
- 縦スクロールが普通に効く(行の上で縦に振っても誤発火しない)
- 斜めドラッグ: 縦優勢なら scroll、横優勢ならスワイプ
- 行内のボタン(再開・アーカイブ)のタップが今まで通り効く
- 既存の長押し(プロジェクト見出し→起動先設定)が壊れていない

Expected: すべて成立。

- [ ] **Step 4: コミット**

```bash
git add web/app.js
git commit -m "Swipe a stopped conversation row to archive it"
```

---

### Task 5: フロント — アーカイブタブと検索バッジ

**Files:**
- Modify: `web/index.html` — `#viewTabs` に nav ボタン1個、search ビューの後にビューブロック
- Modify: `web/app.js` — `VIEWS` にエントリ、`loadArchive`/`renderArchiveRow`、配線、`renderSearchRow` のバッジ

- [ ] **Step 1: index.html にタブとビューを追加**

`#viewTabs` の search ボタンの `</li>` の後に:

```html
      <li class="nav-item">
        <button class="nav-link" data-view="archive">
          <i class="bi bi-archive"></i> <span class="d-none d-sm-inline">アーカイブ</span>
        </button>
      </li>
```

`<!-- ===== search view ===== -->` ブロックの閉じ `</div>` の後に:

```html
    <!-- ===== archive view ===== -->
    <div data-view="archive" class="d-none">
      <div class="card mb-3">
        <div class="card-header d-flex align-items-center gap-2">
          <i class="bi bi-archive"></i> <strong>アーカイブ</strong>
          <span class="small text-secondary ms-auto">スワイプ or 「戻す」で一覧へ復元</span>
        </div>
        <ul id="archiveList" class="list-group list-group-flush">
          <li class="list-group-item text-secondary small">読み込み中…</li>
        </ul>
      </div>
    </div>
```

- [ ] **Step 2: app.js にビューを実装**

`VIEWS` 定義を変更:

```js
const VIEWS = {
  projects: { onShow: () => refresh({ silent: true }) },
  search: { onShow: onSearchShow },
  archive: { onShow: loadArchive },
};
```

検索セクションの後(`renderSearchResults()` の直後)に追加:

```js
// --- archive tab ------------------------------------------------------------ //
async function loadArchive() {
  const el = $("archiveList");
  try {
    const { archived } = await api("/api/archived");
    if (!archived.length) {
      el.innerHTML = '<li class="list-group-item text-secondary small">アーカイブはありません</li>';
      return;
    }
    el.innerHTML = archived.map(renderArchiveRow).join("");
  } catch (e) {
    show(e.message, "danger");
  }
}

// Same swipe-front structure as renderResume, so attachSwipe works here too —
// in this tab the gesture restores instead of archiving.
function renderArchiveRow(c) {
  const label = (c.title ? sessDisplayName(c.title, c.cwd) : "") || c.last || "(無題)";
  const proj = c.cwd ? c.cwd.replace(/[/\\]+$/, "").split(/[/\\]/).pop() : "(フォルダ不明)";
  return `
    <li class="list-group-item swipe-item p-0">
      <div class="swipe-under"><i class="bi bi-arrow-counterclockwise"></i><i class="bi bi-arrow-counterclockwise"></i></div>
      <div class="swipe-front d-flex align-items-center gap-2 ps-2 pe-2 py-2" data-swipe-id="${esc(c.id)}">
        <span class="sess-meta flex-grow-1">
          <span class="d-block sess-name fw-normal">${esc(label)}</span>
          <span class="d-block text-secondary small">
            <i class="bi bi-folder2"></i> ${esc(proj)} · <i class="bi bi-clock-history"></i> ${fmtAge(c.modified)}前
          </span>
        </span>
        <button class="btn btn-sm btn-outline-secondary flex-none" data-act="unarchive" data-id="${esc(c.id)}">
          <i class="bi bi-arrow-counterclockwise"></i> 戻す
        </button>
        <button class="btn btn-sm btn-outline-success flex-none"
                data-act="resume" data-id="${esc(c.id)}" data-dir="${esc(c.cwd)}"
                data-name="${esc(sanitizeName(c.title || ""))}">再開</button>
      </div>
    </li>`;
}
```

配線セクション(Task 4 で足した `attachSwipe($("sessions"), ...)` の後)に追加:

```js
$("archiveList").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-act]");
  if (!btn) return;
  if (btn.dataset.act === "unarchive") unarchiveConversation(btn.dataset.id);
  else if (btn.dataset.act === "resume")
    resumeConversation(btn.dataset.dir, btn.dataset.id, btn.dataset.name);
});
// In the archive tab, the same gesture restores (slide out, then reload).
attachSwipe($("archiveList"), (id, row) => {
  if (row) row.remove();
  unarchiveConversation(id);
});
```

注意: `VIEWS` はファイル中腹(view router)にあり `loadArchive` は関数宣言なので hoisting で参照可能。

- [ ] **Step 3: 検索結果にアーカイブ済みバッジ**

`renderSearchRow()` 内、`const action = ...` の直前に追加:

```js
  const badge = r.archived
    ? ' <span class="badge text-bg-secondary fw-normal">アーカイブ済</span>'
    : "";
```

ラベル行を変更。現状:

```js
        <span class="d-block sess-name">${highlight(label, lastSearchQuery)}</span>
```

変更後:

```js
        <span class="d-block sess-name">${highlight(label, lastSearchQuery)}${badge}</span>
```

- [ ] **Step 4: ブラウザで確認**

- アーカイブタブ: アーカイブ済みが新しい順に出る / 空なら空状態文言
- 「戻す」ボタン → プロジェクトタブに行が復活、アーカイブタブから消える
- アーカイブタブでのスワイプ → 同じく復元
- 「再開」→ セッションが起動し、自動で unarchive されている(`/api/archived` から消える)
- 検索: アーカイブ済みのヒットに「アーカイブ済」バッジ

Expected: すべて成立。

- [ ] **Step 5: コミット**

```bash
git add web/index.html web/app.js
git commit -m "Add archive tab with restore and an archived badge in search"
```

---

### Task 6: 総合検証

**Files:** なし(検証のみ)

- [ ] **Step 1: 全テスト + サーバ再起動**

```bash
python3 -m unittest tests.test_archive -v   # Ran 10 tests ... OK
pkill -f "python3 server.py"; sleep 1
nohup ./run.sh > /tmp/ccwa.log 2>&1 & sleep 1
curl -s localhost:8765/healthz               # {"ok": true}
```

- [ ] **Step 2: シナリオ通し(curl)**

Task 2 Step 5 の curl 一式を再実行し、加えて:

```bash
# resume による自動 unarchive
curl -s -X POST -H "X-Auth-Token: $TOKEN" -d "{\"id\":\"$SID\"}" localhost:8765/api/archive
DIR=$(curl -s -H "X-Auth-Token: $TOKEN" localhost:8765/api/archived | python3 -c "import json,sys;print(json.load(sys.stdin)['archived'][0]['cwd'])")
curl -s -X POST -H "X-Auth-Token: $TOKEN" -d "{\"id\":\"$SID\",\"dir\":\"$DIR\",\"name\":\"\"}" localhost:8765/api/resume
curl -s -H "X-Auth-Token: $TOKEN" localhost:8765/api/archived | grep -c "$SID"  # → 0 (自動 unarchive)
# 稼働中なので archive は 400
curl -s -o /dev/null -w "%{http_code}\n" -X POST -H "X-Auth-Token: $TOKEN" -d "{\"id\":\"$SID\"}" localhost:8765/api/archive  # → 400
# 後始末: 起動したセッションを kill
NAME=$(curl -s -H "X-Auth-Token: $TOKEN" localhost:8765/api/sessions | python3 -c "import json,sys;print(next(s['name'] for s in json.load(sys.stdin)['sessions'] if s['id']=='$SID'))")
curl -s -X POST -H "X-Auth-Token: $TOKEN" -d "{\"name\":\"$NAME\"}" localhost:8765/api/kill
```

Expected: コメントの通り。

- [ ] **Step 3: スマホ実機チェックリスト(ユーザー操作)**

スマホ(Tailscale 経由)で:
- [ ] スワイプでアーカイブ(追従・スプリングバック・縦スクロール非干渉)
- [ ] undo トーストのタップ
- [ ] アーカイブタブでの復元(スワイプ/戻す)
- [ ] <768px でアーカイブボタンが出ていない

- [ ] **Step 4: 仕上げ**

superpowers:verification-before-completion → superpowers:requesting-code-review → superpowers:finishing-a-development-branch の各スキルに従う。
