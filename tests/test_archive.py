"""Archive store tests. The store is pure file+lock logic, so we point the
module-level paths at a temp dir and stub out tmux (list_sessions)."""
import json
import subprocess
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
             mock.patch.object(server, "_pid_in_tmux", return_value=False), \
             mock.patch.object(server, "_reptyr_target_pids", return_value=set()), \
             mock.patch.object(server, "list_sessions", return_value=[]):
            ext = server.external_claude_sessions()
        self.assertEqual(ext, {sid1: 101, sid2: 102})

    def test_excludes_processes_inside_tmux(self):
        # A pane process keeps TMUX in its environment even while it lingers
        # for ~1s after `tmux kill-session` (SIGHUP shutdown). In that window
        # the tmux session is gone, so the @ccwa_sid exclusion no longer
        # covers it — without the TMUX check the row flashes as "external"
        # right after every kill (the immediate post-kill refresh hits it).
        sid = str(uuid.uuid4())
        lines = [(301, f"claude --session-id {sid} --remote-control x")]
        with mock.patch.object(server, "_ps_claude_lines", return_value=lines), \
             mock.patch.object(server, "_pid_in_tmux", return_value=True), \
             mock.patch.object(server, "list_sessions", return_value=[]):
            self.assertEqual(server.external_claude_sessions(), {})

    def test_excludes_processes_that_merely_mention_claude(self):
        # The tmux SERVER keeps the spawning command as its argv ("tmux
        # new-session ... claude --resume <sid> ..."). Matching it would make
        # takeover SIGTERM the tmux server and kill every session. Only a
        # process whose argv[0] is the claude binary may count.
        sid = str(uuid.uuid4())
        lines_raw = (
            f"250676 tmux new-session -d -s x -c /tmp "
            f"/home/x/.local/bin/claude --resume {sid} --remote-control y"
        )
        # feed through the same first-token rule _ps_claude_lines applies
        with mock.patch.object(server.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, lines_raw + "\n", "")
            self.assertEqual(server._ps_claude_lines(), [])

    def test_excludes_tmux_managed_sids(self):
        sid = str(uuid.uuid4())
        lines = [(201, f"claude --session-id {sid} --remote-control x")]
        with mock.patch.object(server, "_ps_claude_lines", return_value=lines), \
             mock.patch.object(server, "list_sessions", return_value=[{"id": sid}]):
            self.assertEqual(server.external_claude_sessions(), {})


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
            # build_overview now also scans for flagless claudes; without this
            # the real ps would leak into the assertions below.
            mock.patch.object(server, "_ps_claude_lines", return_value=[]),
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
        # _tmux/ensure_trusted are mocked so a regression here can never reach
        # the real tmux (it did once, pre-implementation, and trusted /tmp).
        ok = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(
            server, "external_claude_sessions", return_value={self.sid: 999}
        ), mock.patch.object(server, "ensure_trusted"), \
             mock.patch.object(server, "_tmux", return_value=ok), \
             mock.patch.object(server, "has_session", return_value=False):
            with self.assertRaises(ValueError):
                server.resume_session("/tmp", self.sid, "")

    def test_resume_warns_when_flagless_claude_shares_cwd(self):
        # A flagless terminal launch (no sid in argv) can't be tied to a
        # conversation, so resume can't know it IS this one — but same-cwd is
        # suspicious enough to demand force=True (the 62c575b1 double-run).
        ok = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(server, "external_claude_sessions", return_value={}), \
             mock.patch.object(server, "_flagless_claude_in_cwd", return_value=4242), \
             mock.patch.object(server, "ensure_trusted"), \
             mock.patch.object(server, "_tmux", return_value=ok), \
             mock.patch.object(server, "has_session", return_value=False):
            with self.assertRaises(server.MaybeLiveError):
                server.resume_session("/tmp", self.sid, "")
            # force=True proceeds past the guard
            r = server.resume_session("/tmp", self.sid, "", force=True)
            self.assertTrue(r["created"])

    def test_flagless_claude_in_cwd_matches_only_flagless_non_tmux(self):
        sid = str(uuid.uuid4())
        lines = [
            (11, f"claude --session-id {sid} --remote-control a"),  # sid付き: 対象外
            (12, "claude --dangerously-skip-permissions --remote-control"),  # 候補
            (13, "claude --dangerously-skip-permissions --remote-control"),  # tmux内: 対象外
        ]
        cwds = {12: "/proj/x", 13: "/proj/x"}
        with mock.patch.object(server, "_ps_claude_lines", return_value=lines), \
             mock.patch.object(server, "_pid_in_tmux", side_effect=lambda p: p == 13), \
             mock.patch.object(server, "_pid_cwd", side_effect=lambda p: cwds.get(p)):
            self.assertEqual(server._flagless_claude_in_cwd("/proj/x"), 12)
            self.assertIsNone(server._flagless_claude_in_cwd("/proj/other"))

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


if __name__ == "__main__":
    unittest.main()
