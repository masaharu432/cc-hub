# reptyr ライブ移管 実装プラン

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** ターミナル直起動の claude プロセスを **殺さずに** `tmux new-session -d 'reptyr -T <pid>'` で tmux 配下へ吸い込む(TUI・生成中の応答・RC 接続すべて保持)。フラグレス起動(argv に sid なし)も pid だけで移管対象になる。

**Architecture:** `migrate_session(pid, sid, name)` が pid 検証 → reptyr 可用性チェック → detached tmux セッション内で `reptyr -T` を起動 → 最大15秒の成功ポーリング → 成功時に `@ccwa_sid` 刻印。検出側は `flagless_claude_sessions()` を追加して overview の `external` 配列に sid 無し行を混ぜる。旧 kill+resume(`takeover`)はフォールバックとして温存。

**Tech Stack:** Python 3 stdlib(unittest / subprocess / /proc 読み)、vanilla JS + Bootstrap。

**Spec:** `docs/superpowers/specs/2026-06-12-reptyr-live-migration-design.md`
**Branch:** `archive-conversations` の続き(external 検出コードの直上に積むため)

**前提(実機確認済み):** `/usr/bin/reptyr` 導入済み・`cap_sys_ptrace=ep` 付与済み。

---

### Task 1: サーバ — `reptyr_available()` と `_pid_tty()`(TDD)

**Files:**
- Create: `tests/test_migrate.py`
- Modify: `server.py`(`_terminate_pid` の直後に新セクション)

- [ ] **Step 1: 失敗するテストを書く** — `tests/test_migrate.py` を新規作成:

```python
"""reptyr ライブ移管のテスト。プロセス・tmux 操作は全部モックし、純粋な
判定/組み立てロジックだけを検証する."""
import os
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

import server


class ReptyrAvailableTests(unittest.TestCase):
    def test_false_when_no_binary(self):
        with mock.patch("shutil.which", return_value=None):
            self.assertFalse(server.reptyr_available())

    def test_false_without_cap(self):
        no_cap = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch("shutil.which", return_value="/usr/bin/reptyr"), \
             mock.patch("subprocess.run", return_value=no_cap):
            self.assertFalse(server.reptyr_available())

    def test_true_with_cap_sys_ptrace(self):
        cap = subprocess.CompletedProcess(
            [], 0, "/usr/bin/reptyr cap_sys_ptrace=ep\n", "")
        with mock.patch("shutil.which", return_value="/usr/bin/reptyr"), \
             mock.patch("subprocess.run", return_value=cap):
            self.assertTrue(server.reptyr_available())


class PidTtyTests(unittest.TestCase):
    def test_own_pid_has_some_tty_value(self):
        # 値そのものは環境依存。int が返ること(パースが通ること)だけ確認。
        self.assertIsInstance(server._pid_tty(os.getpid()), int)

    def test_missing_pid_returns_none(self):
        self.assertIsNone(server._pid_tty(2 ** 30))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 失敗確認** — Run: `python3 -m unittest tests.test_migrate -v`
  Expected: ERROR ×5 (`AttributeError: module 'server' has no attribute 'reptyr_available'`)

- [ ] **Step 3: 実装** — `server.py` の `_terminate_pid` の直後(`# Conversation history` セクションコメントの前)に:

```python
# --------------------------------------------------------------------------- #
# Live migration into tmux (reptyr). The old takeover killed the terminal
# process and resumed the conversation — losing the TUI and any in-flight
# generation. `reptyr -T <pid>` inside a detached tmux pane steals the whole
# tty instead: TUI, generation and the RC bridge all survive (verified
# empirically 2026-06-11..12). reptyr stays resident in the pane as the relay,
# so the pane's lifetime naturally follows claude's. Needs cap_sys_ptrace on
# the reptyr binary because this server runs without sudo and without a tty.
# kill+resume (`takeover`) remains as the fallback when reptyr is unusable.
# --------------------------------------------------------------------------- #
def reptyr_available() -> bool:
    """reptyr exists AND carries cap_sys_ptrace (checked via getcap)."""
    rep = shutil.which("reptyr")
    if not rep:
        return False
    getcap = shutil.which("getcap") or "/usr/sbin/getcap"
    try:
        proc = subprocess.run(
            [getcap, rep], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return "cap_sys_ptrace" in proc.stdout


def _pid_tty(pid: int) -> int | None:
    """tty_nr of a process (/proc/<pid>/stat field 7), or None if gone.

    comm (field 2) is in parens and may contain spaces, so split AFTER the
    closing paren instead of naively splitting the whole line."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return int(stat.rsplit(")", 1)[1].split()[4])
    except (OSError, IndexError, ValueError):
        return None
```

- [ ] **Step 4: 通過確認** — Run: `python3 -m unittest tests.test_migrate -v` → OK ×5
- [ ] **Step 5: コミット**

```bash
git add server.py tests/test_migrate.py
git commit -m "Add reptyr capability probe and process tty reader"
```

---

### Task 2: サーバ — フラグレス検出の一覧化(TDD)

**Files:**
- Modify: `server.py` — `_flagless_claude_in_cwd`(437行付近)を `flagless_claude_sessions()` ベースに再構成
- Modify: `tests/test_migrate.py`

- [ ] **Step 1: 失敗するテストを書く** — `tests/test_migrate.py` の `PidTtyTests` の後に:

```python
class FlaglessListTests(unittest.TestCase):
    def test_lists_only_sidless_nontmux_claude(self):
        sid = str(uuid.uuid4())
        lines = [
            (11, "claude --dangerously-skip-permissions"),  # フラグレス → 対象
            (12, f"claude --session-id {sid}"),             # sid 付き → 対象外
            (13, "claude"),                                 # tmux 生まれ → 対象外
        ]
        with mock.patch.object(server, "_ps_claude_lines", return_value=lines), \
             mock.patch.object(server, "_pid_in_tmux",
                               side_effect=lambda p: p == 13), \
             mock.patch.object(server, "_pid_cwd", return_value="/tmp/x"):
            self.assertEqual(
                server.flagless_claude_sessions(),
                [{"pid": 11, "cwd": "/tmp/x"}],
            )

    def test_cwd_guard_reuses_the_list(self):
        with mock.patch.object(
            server, "flagless_claude_sessions",
            return_value=[{"pid": 7, "cwd": "/tmp/x"}],
        ):
            self.assertEqual(server._flagless_claude_in_cwd("/tmp/x"), 7)
            self.assertIsNone(server._flagless_claude_in_cwd("/tmp/y"))
```

- [ ] **Step 2: 失敗確認** — Run: `python3 -m unittest tests.test_migrate.FlaglessListTests -v`
  Expected: ERROR ×2 (`flagless_claude_sessions` なし)

- [ ] **Step 3: 実装** — `server.py` の `_flagless_claude_in_cwd` を丸ごと次の2関数に置き換え(docstring の趣旨は引き継ぐ):

```python
def flagless_claude_sessions() -> list[dict]:
    """{pid, cwd} for every flagless (no sid in argv) non-tmux claude.

    These can't be tied to a conversation, but reptyr migration only needs the
    pid — so the overview lists them as migratable "external" rows."""
    out = []
    for pid, args in _ps_claude_lines():
        if _SID_FLAG_RE.search(args):
            continue  # sid-flagged: handled precisely via external_claude_sessions
        if _pid_in_tmux(pid):
            continue
        out.append({"pid": pid, "cwd": _pid_cwd(pid)})
    return out


def _flagless_claude_in_cwd(cwd: str) -> int | None:
    """Pid of a flagless non-tmux claude running in `cwd`.

    Such a process can't be tied to a specific conversation, but resuming a
    conversation from the same folder while one is running is exactly how the
    62c575b1 double-run happened — suspicious enough to warn (MaybeLiveError),
    never enough to kill."""
    target = _norm_path(cwd)
    for f in flagless_claude_sessions():
        if f["cwd"] and _norm_path(f["cwd"]) == target:
            return f["pid"]
    return None
```

- [ ] **Step 4: 通過確認** — Run: `python3 -m unittest tests.test_migrate tests.test_archive -v` → 全テスト OK(既存の resume ガードテストも壊れていないこと)
- [ ] **Step 5: コミット**

```bash
git add server.py tests/test_migrate.py
git commit -m "List flagless terminal claudes; rebase the resume guard on it"
```

---

### Task 3: サーバ — `migrate_session()` と成功ポーリング(TDD)

**Files:**
- Modify: `server.py` — Task 1 で作ったセクションの末尾(`_pid_tty` の後)に追加
- Modify: `tests/test_migrate.py`

- [ ] **Step 1: 失敗するテストを書く** — `FlaglessListTests` の後に:

```python
class MigrateTests(unittest.TestCase):
    """migrate_session: 検証 → tmux+reptyr 起動 → ポーリング → sid 刻印."""

    def setUp(self):
        self.sid = str(uuid.uuid4())
        self.ok = subprocess.CompletedProcess([], 0, "", "")
        self.tmux_calls = []

        def fake_tmux(*a):
            self.tmux_calls.append(a)
            return self.ok

        for target, value in (
            ("_ps_claude_lines",
             mock.MagicMock(return_value=[(4242, f"claude --resume {self.sid}")])),
            ("_pid_in_tmux", mock.MagicMock(return_value=False)),
            ("reptyr_available", mock.MagicMock(return_value=True)),
            ("_pid_cwd", mock.MagicMock(return_value="/tmp/proj")),
            ("_pid_tty", mock.MagicMock(return_value=34816)),
            ("has_session", mock.MagicMock(return_value=False)),
            ("_wait_for_steal", mock.MagicMock(return_value=True)),
        ):
            p = mock.patch.object(server, target, value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(server, "_tmux", side_effect=fake_tmux)
        p.start()
        self.addCleanup(p.stop)

    def test_rejects_unknown_pid(self):
        with mock.patch.object(server, "_ps_claude_lines", return_value=[]):
            with self.assertRaises(ValueError):
                server.migrate_session(4242)

    def test_rejects_tmux_resident_pid(self):
        with mock.patch.object(server, "_pid_in_tmux", return_value=True):
            with self.assertRaises(ValueError):
                server.migrate_session(4242)

    def test_rejects_bad_sid(self):
        with self.assertRaises(ValueError):
            server.migrate_session(4242, sid="not-a-uuid")

    def test_without_reptyr_points_at_setcap(self):
        with mock.patch.object(server, "reptyr_available", return_value=False):
            with self.assertRaises(ValueError) as cm:
                server.migrate_session(4242)
        self.assertIn("setcap", str(cm.exception))

    def test_success_launches_reptyr_and_stamps_sid(self):
        r = server.migrate_session(4242, sid=self.sid)
        self.assertEqual(r["name"], f"migrate-{self.sid[:8]}")
        new_sess = [c for c in self.tmux_calls if c[0] == "new-session"]
        self.assertEqual(len(new_sess), 1)
        self.assertIn(f"-T 4242", new_sess[0][-1])      # reptyr コマンド文字列
        self.assertIn("/tmp/proj", new_sess[0])          # -c <cwd>
        stamps = [c for c in self.tmux_calls if c[0] == "set-option"]
        self.assertEqual(len(stamps), 1)
        self.assertIn(self.sid, stamps[0])

    def test_flagless_names_after_cwd_and_skips_stamp(self):
        r = server.migrate_session(4242)
        self.assertEqual(r["name"], "proj")              # /tmp/proj の basename
        self.assertEqual(
            [c for c in self.tmux_calls if c[0] == "set-option"], [])

    def test_steal_failure_kills_husk_and_raises(self):
        with mock.patch.object(server, "_wait_for_steal", return_value=False):
            with self.assertRaises(RuntimeError):
                server.migrate_session(4242, sid=self.sid)
        kills = [c for c in self.tmux_calls if c[0] == "kill-session"]
        self.assertEqual(len(kills), 1)
```

- [ ] **Step 2: 失敗確認** — Run: `python3 -m unittest tests.test_migrate.MigrateTests -v`
  Expected: ERROR ×7 (`migrate_session` なし。setUp の `_wait_for_steal` patch も AttributeError になるが、それも「未実装の失敗」として正)

- [ ] **Step 3: 実装** — `server.py` の `_pid_tty` の直後に:

```python
def _wait_for_steal(pid: int, name: str, old_tty: int | None,
                    timeout: float = 15.0) -> bool:
    """Poll until reptyr has the target (or it clearly failed).

    Success signals, checked in order:
      - the target's controlling tty changed (clean signal when it happens);
      - the pane shows redrawn TUI content AND the session is still alive a
        moment later. The re-check matters: a failing reptyr prints an error
        and exits, killing the pane within ~1s — without the re-check that
        error text would read as success.
    Failure signals: the tmux session died (reptyr gave up) or claude itself
    vanished. Timeout returns False and the caller cleans up."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not has_session(name):
            return False
        tty = _pid_tty(pid)
        if tty is None:
            return False
        if old_tty is not None and tty != old_tty:
            return True
        cap = _tmux("capture-pane", "-p", "-t", f"={name}")
        if cap.returncode == 0 and cap.stdout.strip():
            time.sleep(0.7)
            if has_session(name):
                return True
        time.sleep(0.4)
    return False


def migrate_session(pid: int, sid: str = "", name: str = "") -> dict:
    """Pull a terminal-launched claude into a detached tmux session, alive.

    Only a pid is required, so flagless launches are migratable too. When the
    sid IS known we stamp @ccwa_sid so the session ties back to its
    conversation; an unknown sid is never guessed."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise ValueError("Invalid pid.")
    if sid and not UUID_RE.match(sid):
        raise ValueError("Invalid session id.")
    if dict(_ps_claude_lines()).get(pid) is None:
        raise ValueError("対象プロセスが見つからないか、claude ではありません。")
    if _pid_in_tmux(pid):
        raise ValueError("このプロセスは既に tmux 内で動いています。")
    if not reptyr_available():
        raise ValueError(
            "reptyr が使えません(未インストール、または cap_sys_ptrace 未付与)。"
            "ホストで一度だけ `sudo apt-get install reptyr && "
            "sudo setcap cap_sys_ptrace+ep /usr/bin/reptyr` を実行してください。"
        )

    cwd = _pid_cwd(pid)
    requested = name if name and NAME_RE.match(name) else ""
    if not requested and sid:
        requested = f"migrate-{sid[:8]}"
    if not requested:
        base = Path(cwd).name if cwd else ""
        requested = base if base and NAME_RE.match(base) else f"migrate-{pid}"
    name = _unique_session_name(requested)

    old_tty = _pid_tty(pid)
    # pid is a validated int and the reptyr path comes from shutil.which, so
    # the shell command string is injection-free. -c groups the session under
    # the project folder in the overview (session_path).
    cmd = ["new-session", "-d", "-s", name]
    if cwd:
        cmd += ["-c", cwd]
    cmd.append(f"{shutil.which('reptyr')} -T {pid}")
    proc = _tmux(*cmd)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "tmux new-session failed")

    if not _wait_for_steal(pid, name, old_tty):
        _tmux("kill-session", "-t", f"={name}")  # best-effort husk cleanup
        raise RuntimeError(
            "移管に失敗しました(reptyr がプロセスを取り込めませんでした)。"
            "ターミナル側のプロセスはそのまま生きています。"
        )
    if sid:
        _tmux("set-option", "-t", name, "@ccwa_sid", sid)
    return {"created": True, "name": name, "id": sid or None, "pid": pid}
```

- [ ] **Step 4: 通過確認** — Run: `python3 -m unittest tests.test_migrate -v` → 全テスト OK
- [ ] **Step 5: コミット**

```bash
git add server.py tests/test_migrate.py
git commit -m "Migrate a live terminal claude into tmux via reptyr -T"
```

---

### Task 4: サーバ — overview への混入・/api/migrate・ガード文言(TDD)

**Files:**
- Modify: `server.py` — `build_overview`(838行付近のループ後)、`resume_session` のガード文言(905行付近)、`do_POST`
- Modify: `tests/test_migrate.py`
- Modify: `tests/test_archive.py` — `TakeoverTests.setUp` に flagless のスタブを追加

- [ ] **Step 1: 失敗するテストを書く** — `tests/test_migrate.py` の `MigrateTests` の後に:

```python
class OverviewFlaglessTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        projects = root / "claude-projects"
        projects.mkdir()
        for patch in (
            mock.patch.object(server, "ARCHIVE_PATH", root / "archive.json"),
            mock.patch.object(server, "CLAUDE_PROJECTS", projects),
            mock.patch.object(server, "list_sessions", return_value=[]),
            mock.patch.object(server, "external_claude_sessions", return_value={}),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def test_flagless_appears_as_external_row(self):
        with mock.patch.object(
            server, "flagless_claude_sessions",
            return_value=[{"pid": 55, "cwd": "/tmp/x"}],
        ):
            out = server.build_overview()
        rows = [(p["path"], c) for p in out for c in p["external"]]
        self.assertEqual(len(rows), 1)
        path, c = rows[0]
        self.assertEqual(path, "/tmp/x")
        self.assertEqual(c["pid"], 55)
        self.assertIsNone(c["id"])
        self.assertIsNone(c["title"])
```

- [ ] **Step 2: 既存テストの先回り修正** — `build_overview` が `flagless_claude_sessions` を呼ぶようになると `tests/test_archive.py` の `TakeoverTests` が実プロセスを ps スキャンしてしまう。`TakeoverTests.setUp` の patch タプルに1行追加:

```python
            mock.patch.object(server, "list_sessions", return_value=[]),
            mock.patch.object(server, "flagless_claude_sessions", return_value=[]),
```

(`list_sessions` の行の下に `flagless_claude_sessions` の行を追加する)

- [ ] **Step 3: 失敗確認** — Run: `python3 -m unittest tests.test_migrate.OverviewFlaglessTests -v`
  Expected: FAIL(rows が空 — build_overview がまだ flagless を混ぜていない)

- [ ] **Step 4: 実装**

(a) `build_overview` の convos ループ(`b["recent"] = max(...)` で終わる for 文)の直後・`out = list(projects.values())` の前に:

```python
    for f in flagless_claude_sessions():
        # A terminal claude we can see but can't name (no sid in argv). Same
        # external list so the UI offers the one action that works: migrate.
        b = bucket(f["cwd"] or "")
        b["external"].append(
            {"id": None, "title": None, "last": None, "modified": 0,
             "cwd": f["cwd"], "pid": f["pid"]}
        )
```

(b) `resume_session` の MaybeLiveError 文言の末尾を実態に合わせる。

```python
                "動く可能性があります(再開してもターミナル側は終了されません。"
                "移管したい場合は先にターミナル側を /exit してください)。" % flagless
```

を

```python
                "動く可能性があります(再開してもターミナル側は終了されません。"
                "移管したい場合は一覧の「tmuxへ移管」を使ってください)。" % flagless
```

に変更。

(c) `do_POST` の `/api/resume` ブロックの直後に:

```python
            if path == "/api/migrate":
                pid = data.get("pid")
                if not isinstance(pid, int) or isinstance(pid, bool):
                    raise ValueError("Invalid pid.")
                result = migrate_session(
                    pid, data.get("sid") or "", data.get("name") or ""
                )
                return self._send_json({"ok": True, **result})
```

- [ ] **Step 5: 通過確認** — Run: `python3 -m unittest tests.test_migrate tests.test_archive -v` → 全テスト OK
- [ ] **Step 6: コミット**

```bash
git add server.py tests/test_migrate.py tests/test_archive.py
git commit -m "Surface flagless terminal claudes and expose POST /api/migrate"
```

---

### Task 5: フロント — 移管ボタンの reptyr 化とフォールバック

**Files:**
- Modify: `web/app.js` — `renderExternal`(207行付近)、`takeoverConversation`(376行付近)、クリック委譲(724行付近)

- [ ] **Step 1: `renderExternal` を置き換え** — 既存の関数(直前のコメント2行ごと)を:

```js
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
              title="プロセスを生かしたまま tmux 配下へ移す">
        <i class="bi bi-box-arrow-in-down"></i> tmuxへ移管</button>
    </li>`;
}
```

- [ ] **Step 2: `takeoverConversation` を confirm 抜きのフォールバック専用に変え、`migrateConversation` を追加** — 既存の `takeoverConversation`(直前のコメント3行ごと)を次の2関数に置き換え:

```js
// Live-migrate a terminal-launched claude into tmux (reptyr -T): the process
// keeps running — TUI, in-flight generation and the RC bridge all survive.
// On failure (typically reptyr missing its capability) the server's message
// carries the setcap one-liner; when the conversation id is known we offer
// the old kill+resume takeover as a fallback.
async function migrateConversation(pid, sid, dir, name) {
  if (!confirm("ターミナルのプロセスをそのまま tmux 配下へ移します(終了しません・履歴も生成中の状態も保持)。数秒かかります。よろしいですか？")) return;
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
```

- [ ] **Step 3: クリック委譲の分岐を差し替え** — `#sessions` の委譲内:

```js
    else if (btn.dataset.act === "takeover")
      takeoverConversation(btn.dataset.dir, btn.dataset.id, btn.dataset.name);
```

を

```js
    else if (btn.dataset.act === "migrate")
      migrateConversation(btn.dataset.pid, btn.dataset.id, btn.dataset.dir, btn.dataset.name);
```

に変更。

- [ ] **Step 4: 構文確認** — Run: `node --check web/app.js` → エラーなし
- [ ] **Step 5: コミット**

```bash
git add web/app.js
git commit -m "Make the migrate button do a live reptyr migration with takeover fallback"
```

---

### Task 6: README 追記と総合検証

**Files:**
- Modify: `README.md` — 「必要なもの」セクションと API 表

- [ ] **Step 1: README に前提を追記** — 「## 必要なもの」の箇条書き末尾に:

```markdown
- reptyr（「tmuxへ移管」のライブ移管に使用。ホストで一度だけ:
  `sudo apt-get install reptyr && sudo setcap cap_sys_ptrace+ep /usr/bin/reptyr`。
  未設定でもアプリは動くが、移管は旧方式の kill+resume フォールバックになる）
```

API 表(`POST /api/kill` の行の下)に:

```markdown
| POST   | `/api/migrate`   | `{pid, sid?, name?}` ターミナル起動の claude を reptyr で tmux へ生きたまま移管 |
```

- [ ] **Step 2: 全テスト** — Run: `python3 -m unittest discover -s tests -v` → 全テスト OK
- [ ] **Step 3: サーバ再起動** — **注意: `pkill -f` は自分のシェルを殺し得るのでブラケット形を使う**:

```bash
pkill -f "python3 [s]erver.py"; sleep 1
cd /path/to/cc-hub && nohup python3 server.py > /tmp/ccwa.log 2>&1 &
```

- [ ] **Step 4: curl 検証** — token は `config.json` から取得:

```bash
TOKEN=$(python3 -c "import json;print(json.load(open('config.json'))['token'])")
curl -s -H "X-Auth-Token: $TOKEN" http://127.0.0.1:8787/api/overview | python3 -m json.tool | grep -A3 external
curl -s -X POST -H "X-Auth-Token: $TOKEN" -d '{"pid": 1}' http://127.0.0.1:8787/api/migrate   # → 400 "claude ではありません"
```

(ポートは config.json の `port` に合わせる)

- [ ] **Step 5: コミット**

```bash
git add README.md
git commit -m "Document the reptyr prerequisite and the migrate endpoint"
```

- [ ] **Step 6: 実機移管テストはユーザーと相談** — 生きたターミナルセッション(捨てセッション)を用意してもらい、UI から「tmuxへ移管」→ TUI が tmux ペイン内で生きていること・RC 接続が切れていないことを確認。
