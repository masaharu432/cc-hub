# 外部セッション検出と tmux 乗っ取り 実装プラン

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** ターミナル直起動の Claude セッションを ps スキャンで検出して「稼働中(ターミナル)」表示し、ボタン一発で SIGTERM → tmux 内 `--resume` の乗っ取りができるようにする。

**Architecture:** `external_claude_sessions()` が `ps -eo pid=,args=` から `{sid: pid}` を返す(tmux 管理分は除外)。overview は外部稼働を `external` 配列で返し、`resume_session(takeover=True)` が SIGTERM+待機後に通常 resume する。アーカイブは外部稼働を拒否。

**Tech Stack:** Python 3 stdlib(unittest、ps/os.kill/signal)、vanilla JS + Bootstrap。

**Spec:** `docs/superpowers/specs/2026-06-11-external-session-takeover-design.md`
**Branch:** `archive-conversations` の続き(overview/live判定がアーカイブ実装と絡むため)

---

### Task 1: サーバ — 外部セッション検出(TDD)

**Files:**
- Modify: `server.py`(`_unique_session_name` の後に新セクション)
- Modify: `tests/test_archive.py`(末尾にテストクラス追加)

- [ ] **Step 1: 失敗するテストを書く** — `tests/test_archive.py` 末尾(`if __name__` の前)に:

```python
class ExternalSessionTests(unittest.TestCase):
    """external_claude_sessions: ps argv から sid->pid を作り、tmux 管理分を除く."""

    def test_parses_session_id_and_resume_flags(self):
        sid1, sid2 = str(uuid.uuid4()), str(uuid.uuid4())
        lines = [
            (101, f"/home/x/.local/bin/claude --dangerously-skip-permissions --session-id {sid1} --remote-control foo"),
            (102, f"claude --resume {sid2} --remote-control bar"),
            (103, "claude --dangerously-skip-permissions --remote-control"),  # フラグなし: 対象外
            (104, "vim notes.md"),                                            # claude 以外
        ]
        with mock.patch.object(server, "_ps_claude_lines", return_value=lines), \
             mock.patch.object(server, "list_sessions", return_value=[]):
            ext = server.external_claude_sessions()
        self.assertEqual(ext, {sid1: 101, sid2: 102})

    def test_excludes_tmux_managed_sids(self):
        sid = str(uuid.uuid4())
        lines = [(201, f"claude --session-id {sid} --remote-control x")]
        with mock.patch.object(server, "_ps_claude_lines", return_value=lines), \
             mock.patch.object(server, "list_sessions", return_value=[{"id": sid}]):
            self.assertEqual(server.external_claude_sessions(), {})
```

- [ ] **Step 2: 失敗確認** — Run: `python3 -m unittest tests.test_archive.ExternalSessionTests -v` → AttributeError: `_ps_claude_lines` なし

- [ ] **Step 3: 実装** — `server.py` の `_unique_session_name` の直後に:

```python
# --------------------------------------------------------------------------- #
# External (non-tmux) Claude sessions. A conversation jsonl carries no
# liveness signal (Claude appends and closes; no lock, and mtime can't tell an
# idle-but-open session from a dead one — verified empirically 2026-06-11).
# What IS visible is the process argv: launcher-style invocations carry
# `--session-id <uuid>` or `--resume <uuid>`, so one ps scan maps those
# conversations to live pids. Flagless `claude` launches stay undetectable —
# we accept that rather than guess by cwd and risk killing the wrong process.
# --------------------------------------------------------------------------- #
_SID_FLAG_RE = re.compile(r"--(?:session-id|resume)[ =]([0-9a-fA-F-]{36})")


def _ps_claude_lines() -> list[tuple[int, str]]:
    """(pid, argv) for every claude process. Separated out for testability."""
    proc = subprocess.run(
        ["ps", "-eo", "pid=,args="], capture_output=True, text=True, timeout=10
    )
    out = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        pid, _, args = line.partition(" ")
        if pid.isdigit() and re.search(r"(?:^|/)claude(?:\s|$)", args):
            out.append((int(pid), args))
    return out


def external_claude_sessions() -> dict[str, int]:
    """sid -> pid for claude processes running OUTSIDE our tmux sessions.

    tmux-managed sids (@ccwa_sid) are excluded; what's left is a session the
    launcher doesn't own — typically one started by hand in a terminal."""
    tmux_sids = {s["id"] for s in list_sessions() if s.get("id")}
    ext = {}
    for pid, args in _ps_claude_lines():
        m = _SID_FLAG_RE.search(args)
        if not m:
            continue
        sid = m.group(1)
        if UUID_RE.match(sid) and sid not in tmux_sids:
            ext[sid] = pid
    return ext
```

- [ ] **Step 4: 通過確認** — `python3 -m unittest tests.test_archive -v` → 全テスト OK
- [ ] **Step 5: コミット** — `git add server.py tests/test_archive.py && git commit -m "Detect terminal-launched Claude sessions via ps argv scan"`

---

### Task 2: サーバ — overview/archive/search への組み込みと乗っ取り resume(TDD)

**Files:**
- Modify: `server.py` — `archive_conversation`、`build_overview`、`resume_session`、`do_GET`(/api/search)、`do_POST`(/api/resume)
- Modify: `tests/test_archive.py`

- [ ] **Step 1: 失敗するテストを書く** — `ExternalSessionTests` の後に:

```python
class TakeoverTests(unittest.TestCase):
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
        self.sid = str(uuid.uuid4())
        make_log(self.projects, self.sid)

    def test_archive_rejects_external_session(self):
        with mock.patch.object(
            server, "external_claude_sessions", return_value={self.sid: 999}
        ):
            with self.assertRaises(ValueError):
                server.archive_conversation(self.sid)

    def test_overview_moves_external_to_own_list(self):
        with mock.patch.object(
            server, "external_claude_sessions", return_value={self.sid: 999}
        ):
            out = server.build_overview()
        resum = [c["id"] for p in out for c in p["resumable"]]
        ext = [c["id"] for p in out for c in p["external"]]
        self.assertNotIn(self.sid, resum)
        self.assertIn(self.sid, ext)

    def test_resume_external_without_takeover_raises(self):
        with mock.patch.object(
            server, "external_claude_sessions", return_value={self.sid: 999}
        ):
            with self.assertRaises(ValueError):
                server.resume_session("/tmp", self.sid, "")

    def test_resume_with_takeover_terminates_then_launches(self):
        killed = []
        ok = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(
            server, "external_claude_sessions", return_value={self.sid: 999}
        ), mock.patch.object(
            server, "_terminate_pid", side_effect=lambda pid: killed.append(pid)
        ), mock.patch.object(server, "ensure_trusted"), \
             mock.patch.object(server, "_tmux", return_value=ok), \
             mock.patch.object(server, "has_session", return_value=False):
            r = server.resume_session("/tmp", self.sid, "", takeover=True)
        self.assertEqual(killed, [999])
        self.assertTrue(r["created"])
```

`tests/test_archive.py` の import に `subprocess` を追加(`import json` の下に `import subprocess`)。

- [ ] **Step 2: 失敗確認** — `python3 -m unittest tests.test_archive.TakeoverTests -v` → ERROR/FAIL
- [ ] **Step 3: 実装**

(a) import に `signal` を追加。

(b) `_terminate_pid` を `external_claude_sessions` の直後に:

```python
def _terminate_pid(pid: int, timeout: float = 10.0) -> None:
    """SIGTERM + wait-for-exit. SIGTERM only — if the process won't die we
    surface an error instead of escalating to SIGKILL (mid-write risk)."""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return  # already gone
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.3)
    raise RuntimeError("ターミナル側のプロセスが終了しませんでした。手動で終了してから再開してください。")
```

(c) `archive_conversation` の live チェックを拡張:

```python
    if any(s.get("id") == sid for s in list_sessions()):
        raise ValueError("稼働中のセッションはアーカイブできません。先に終了してください。")
    if sid in external_claude_sessions():
        raise ValueError("ターミナルで稼働中のセッションはアーカイブできません。")
```

(d) `build_overview`: bucket 初期化に `"external": []` を追加し、convos ループを:

```python
    archived = load_archived()
    external = external_claude_sessions()
    for c in convos:
        if c["id"] in live_ids or c["id"] in archived:
            continue
        b = bucket(c["cwd"])
        if c["id"] in external:
            b["external"].append({**c, "pid": external[c["id"]]})
        else:
            b["resumable"].append(c)
        b["recent"] = max(b["recent"], c["modified"])
```

(e) `resume_session` のシグネチャを `def resume_session(directory: str, session_id: str, name: str, takeover: bool = False) -> dict:` に変更し、tmux 二重起動チェックの直後に:

```python
    ext_pid = external_claude_sessions().get(session_id)
    if ext_pid:
        if not takeover:
            raise ValueError("この会話はターミナルで稼働中です。「tmuxへ移管」から実行してください。")
        _terminate_pid(ext_pid)
```

(f) `do_POST` の /api/resume に `takeover=bool(data.get("takeover"))` を渡す。

(g) `do_GET` の /api/search ループに `r["external"] = r["id"] in ext_map`(`ext_map = external_claude_sessions()` を `archived_ids = ...` の隣で取得)。

- [ ] **Step 4: 通過確認** — `python3 -m unittest tests.test_archive -v` → 全テスト OK
- [ ] **Step 5: コミット** — `git commit -m "Surface terminal sessions in the overview and take them over on resume"`

---

### Task 3: フロント — 外部稼働行・移管ボタン・検索バッジ

**Files:**
- Modify: `web/app.js` — `renderResume` の後に `renderExternal`、`renderProject`、クリック委譲、`resumeConversation` に takeover 経路、`renderSearchRow`

- [ ] **Step 1: 実装**

(a) `renderResume` の直後に:

```js
// One conversation running OUTSIDE tmux (started by hand in a terminal). Not
// swipeable / archivable — the only offered action is taking it over.
function renderExternal(c, dir, open) {
  const title = c.title ? sessDisplayName(c.title, dir) : "";
  const label = title || c.last || "(無題)";
  return `
    <li class="list-group-item d-flex align-items-center gap-2 ps-4 pe-2 ${open ? "" : "d-none"}" data-parent="${esc(dir)}">
      <span class="sess-meta flex-grow-1">
        <span class="d-block sess-name fw-normal">${esc(label)}</span>
        <span class="d-block text-secondary small">
          <span class="badge text-bg-warning">稼働中(ターミナル)</span> · pid ${esc(String(c.pid))}
        </span>
      </span>
      <button class="btn btn-sm btn-outline-warning flex-none"
              data-act="takeover" data-id="${esc(c.id)}" data-dir="${esc(dir)}"
              data-name="${esc(sanitizeName(c.title || ""))}" title="ターミナル側を終了して tmux 内で再開">
        <i class="bi bi-box-arrow-in-down"></i> tmuxへ移管</button>
    </li>`;
}
```

(b) `renderProject` の行生成に external を追加:

```js
  const live = p.sessions.map((s) => renderLive(s, p.path, open)).join("");
  const ext = (p.external || []).map((c) => renderExternal(c, p.path, open)).join("");
  const resume = p.resumable.map((r) => renderResume(r, p.path, open)).join("");
```

と、最後の `${live}${resume}` を `${live}${ext}${resume}` に。ヘッダのバッジ行に
`${(p.external || []).length ? `<span class="badge text-bg-warning rounded-pill">⌨ ${p.external.length}</span>` : ""}` を稼働バッジの隣に追加。

(c) takeover 関数(`resumeConversation` の直後):

```js
// Take over a terminal-launched session: SIGTERM it server-side, then resume
// the same conversation inside tmux. Mid-generation output would be lost,
// hence the confirm.
async function takeoverConversation(dir, id, name) {
  if (!confirm("ターミナル側のプロセスを終了して tmux 内で再開します。\n応答の生成中だった場合、生成途中の内容は失われます。よろしいですか？")) return;
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
```

(d) `#sessions` クリック委譲に分岐追加(archive の後):

```js
    else if (btn.dataset.act === "takeover")
      takeoverConversation(btn.dataset.dir, btn.dataset.id, btn.dataset.name);
```

(e) `renderSearchRow` の action を3分岐に:

```js
  const action = r.running
    ? '<span class="badge text-bg-success rounded-pill flex-none">● 稼働中</span>'
    : r.external
      ? '<span class="badge text-bg-warning rounded-pill flex-none">稼働中(ターミナル)</span>'
      : `<button class="btn btn-sm btn-outline-success flex-none"
              data-act="resume" data-id="${esc(r.id)}" data-dir="${esc(r.cwd)}"
              data-name="${esc(sanitizeName(r.title || ""))}">再開</button>`;
```

- [ ] **Step 2: 構文確認** — `node --check web/app.js`
- [ ] **Step 3: コミット** — `git commit -m "Show terminal sessions with a take-over action in the UI"`

---

### Task 4: 総合検証

- [ ] `python3 -m unittest tests.test_archive -v` 全通過
- [ ] サーバ再起動(**注意: pkill -f は自分のシェルを殺す → `pkill -f "python3 [s]erver.py"` のブラケット形を使うか、kill と起動を別コマンドに分ける**)
- [ ] curl: overview に `external` が出る(実際にターミナル起動セッションがあるとき)/ external sid への resume が takeover なしで 400 / archive が 400
- [ ] 乗っ取りの実機確認はユーザーと相談(生きたセッションを殺すため、捨てセッションを用意して行う)
