"""Spawnサーバー(claude remote-control 常駐)機能のテスト。tmux は全部モックし、
ペイン出力のパースと起動/停止の判定・組み立てロジックだけを検証する."""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import server


# capture-pane が返す画面そのまま(実機 v2.1.173 で採取)。
PANE_CONNECTED = """\
·✔︎· Connected · claude-code-web-app · archive-conversations
    Capacity: 2/32 · New sessions will be created in the current directory
    spawn-test
    これで、ローカルにつくれるのかな？…
Continue coding in the Claude mobile app or https://claude.ai/code?environment=env_01AzDUyaFTrxsuGzo13qXs2N
space to show QR code · w to toggle spawn mode
"""

PANE_READY_EMPTY = """\
·✔︎· Ready · ccwa-fresh · HEAD
    Capacity: 0/32 · New sessions will be created in the current directory
Code anywhere with the Claude mobile app or https://claude.ai/code?environment=env_01WnPs7YjzN8dfcKcEuZP3cN
space to show QR code
"""

PANE_CONNECTING = """\
·|· Connecting · ccwa-fresh · HEAD
"""


def proc(rc=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], rc, stdout, stderr)


class ParseSpawnPaneTests(unittest.TestCase):
    def test_connected_with_sessions(self):
        st = server._parse_spawn_pane(PANE_CONNECTED)
        self.assertEqual(st["status"], "connected")
        self.assertEqual(st["capacity_used"], 2)
        self.assertEqual(st["capacity_max"], 32)
        self.assertEqual(
            st["env_url"],
            "https://claude.ai/code?environment=env_01AzDUyaFTrxsuGzo13qXs2N",
        )

    def test_ready_empty_server(self):
        st = server._parse_spawn_pane(PANE_READY_EMPTY)
        self.assertEqual(st["status"], "connected")  # Ready も接続済み扱い
        self.assertEqual(st["capacity_used"], 0)
        self.assertEqual(st["capacity_max"], 32)
        self.assertIn("env_01WnPs7YjzN8dfcKcEuZP3cN", st["env_url"])

    def test_connecting(self):
        st = server._parse_spawn_pane(PANE_CONNECTING)
        self.assertEqual(st["status"], "connecting")
        self.assertIsNone(st["env_url"])
        self.assertIsNone(st["capacity_used"])

    def test_garbage_never_raises(self):
        st = server._parse_spawn_pane("complete garbage £$%\n\n")
        self.assertEqual(st["status"], "unknown")
        self.assertIsNone(st["env_url"])
        self.assertIsNone(st["capacity_used"])
        self.assertIsNone(st["capacity_max"])


class ListSessionsSpawnFlagTests(unittest.TestCase):
    def _ls(self, stdout):
        with mock.patch.object(server, "_tmux", return_value=proc(0, stdout)):
            return server.list_sessions()

    def test_spawn_flag_parsed(self):
        rows = self._ls(
            "chat\t1700000000\t0\t1\t/tmp/p\tabc\t\n"
            "spawn-p\t1700000001\t0\t1\t/tmp/p\t\t1\n"
        )
        by_name = {s["name"]: s for s in rows}
        self.assertFalse(by_name["chat"]["spawn"])
        self.assertTrue(by_name["spawn-p"]["spawn"])

    def test_missing_column_defaults_false(self):
        # A pre-spawn-column session (sid present so it's surfaced) must parse
        # with spawn defaulting to False when the @ccwa_spawn column is absent.
        rows = self._ls("old\t1700000000\t0\t1\t/tmp/p\tsid-old\n")
        self.assertFalse(rows[0]["spawn"])


class LaunchSpawnServerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = str(Path(tmp.name).resolve())

    def test_duplicate_directory_raises_with_existing(self):
        existing = {"name": "spawn-x", "directory": self.dir, "status": "connected"}
        with mock.patch.object(server, "list_spawn_servers", return_value=[existing]):
            with self.assertRaises(server.SpawnServerExists) as ctx:
                server.launch_spawn_server(self.dir)
        self.assertEqual(ctx.exception.server["name"], "spawn-x")

    def test_missing_directory_rejected(self):
        with self.assertRaises(ValueError):
            server.launch_spawn_server(self.dir + "/nope")

    def test_launch_builds_expected_tmux_command(self):
        calls = []

        def fake_tmux(*args):
            calls.append(args)
            return proc(0)

        state = {"status": "connected", "env_url": "https://claude.ai/code?environment=env_X",
                 "capacity_used": 0, "capacity_max": 32}
        with (
            mock.patch.object(server, "list_spawn_servers", return_value=[]),
            mock.patch.object(server, "ensure_trusted") as trusted,
            mock.patch.object(server, "has_session", return_value=False),
            mock.patch.object(server, "_tmux", side_effect=fake_tmux),
            mock.patch.object(server, "_spawn_pane_state", return_value=state),
        ):
            result = server.launch_spawn_server(self.dir)

        trusted.assert_called_once_with(self.dir)
        new_session = next(c for c in calls if c[0] == "new-session")
        inner = new_session[-1]
        self.assertIn("remote-control", inner)
        self.assertIn("--spawn=same-dir", inner)
        self.assertIn("--no-create-session-in-dir", inner)
        self.assertNotIn("--name", inner)
        set_opt = next(c for c in calls if c[0] == "set-option")
        self.assertIn("@ccwa_spawn", set_opt)
        self.assertEqual(result["directory"], self.dir)
        self.assertEqual(result["env_url"], state["env_url"])
        self.assertTrue(result["name"].startswith("spawn-"))


class StopSpawnServerTests(unittest.TestCase):
    def test_rejects_non_spawn_session(self):
        # @ccwa_spawn が立っていない(チャット)セッションは殺さない
        with mock.patch.object(server, "_tmux", return_value=proc(0, "")):
            with self.assertRaises(ValueError):
                server.stop_spawn_server("my-chat")

    def test_kills_spawn_session(self):
        calls = []

        def fake_tmux(*args):
            calls.append(args)
            if args[0] == "show-options":
                return proc(0, "1\n")
            return proc(0)

        with mock.patch.object(server, "_tmux", side_effect=fake_tmux):
            server.stop_spawn_server("spawn-p")
        self.assertTrue(any(c[0] == "kill-session" for c in calls))


class OverviewExcludesSpawnTests(unittest.TestCase):
    def test_spawn_sessions_hidden_from_overview(self):
        sessions = [
            {"name": "chat", "created": 1, "attached": False, "windows": 1,
             "path": "/tmp/p", "id": None, "spawn": False},
            {"name": "spawn-p", "created": 2, "attached": False, "windows": 1,
             "path": "/tmp/p", "id": None, "spawn": True},
        ]
        with (
            mock.patch.object(server, "list_sessions", return_value=sessions),
            mock.patch.object(server, "list_conversations", return_value=[]),
            mock.patch.object(server, "load_archived", return_value=set()),
            mock.patch.object(server, "external_claude_sessions", return_value={}),
            mock.patch.object(server, "flagless_claude_sessions", return_value=[]),
        ):
            projects = server.build_overview()
        names = [s["name"] for p in projects for s in p["sessions"]]
        self.assertEqual(names, ["chat"])


if __name__ == "__main__":
    unittest.main()
