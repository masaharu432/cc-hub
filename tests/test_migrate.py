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
             mock.patch.object(server, "_pid_cwd", return_value="/tmp/x"), \
             mock.patch.object(server, "_reptyr_target_pids", return_value=set()):
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


class WaitForStealTests(unittest.TestCase):
    """_wait_for_steal: 「セッション死 or ターゲット死=失敗」「ペイン描画=成功」
    「タイムアウトでも両方生存=成功(常駐reptyr=中継成立)」。
    2026-06-12 の実事故: 成功した steal を検知できず husk kill で移管先ごと
    殺した — タイムアウト=失敗扱いとペイン指定 '=name' が原因。"""

    def _cap(self, rc, out):
        return subprocess.CompletedProcess([], rc, out, "")

    def test_session_death_is_failure(self):
        with mock.patch.object(server, "has_session", return_value=False):
            self.assertFalse(server._wait_for_steal(1, "s", "%1", timeout=5))

    def test_target_death_is_failure(self):
        with mock.patch.object(server, "has_session", return_value=True), \
             mock.patch.object(server, "_pid_tty", return_value=None):
            self.assertFalse(server._wait_for_steal(1, "s", "%1", timeout=5))

    def test_pane_content_is_success(self):
        with mock.patch.object(server, "has_session", return_value=True), \
             mock.patch.object(server, "_pid_tty", return_value=34816), \
             mock.patch.object(server, "_tmux",
                               return_value=self._cap(0, "TUI here\n")) as tm, \
             mock.patch.object(server.time, "sleep"):
            self.assertTrue(server._wait_for_steal(1, "s", "%1", timeout=5))
        # ペインはセッション名ではなく pane_id で参照する('=name' は
        # capture-pane では解決できない — tmux 3.4 実測)
        self.assertIn("%1", tm.call_args_list[0].args)

    def test_timeout_without_pane_content_is_failure(self):
        # 2026-06-14 で反転: 成功時は内容が出て早期 return する。タイムアウト到達
        # = 内容が一度も出なかった = reptyr が中継できていない(VSCode/SSH ptyHost
        # で reptyr が常駐するのに画面が空のまま、の偽成功を潰す)。両方生存でも失敗。
        cap_empty = self._cap(0, "")
        with mock.patch.object(server, "has_session", return_value=True), \
             mock.patch.object(server, "_pid_tty", return_value=34816), \
             mock.patch.object(server, "_tmux", return_value=cap_empty), \
             mock.patch.object(server.time, "sleep"):
            self.assertFalse(server._wait_for_steal(1, "s", "%1", timeout=0.5))


class ReptyrTargetTests(unittest.TestCase):
    def test_parses_target_pids_from_ps(self):
        ps_out = "\n".join([
            "/usr/bin/reptyr -T 4242",
            "reptyr 777",
            "vim reptyr-notes.md 999",
            "grep reptyr -T 555",
        ]) + "\n"
        with mock.patch("subprocess.run",
                        return_value=subprocess.CompletedProcess([], 0, ps_out, "")):
            self.assertEqual(server._reptyr_target_pids(), {4242, 777})

    def test_external_excludes_relayed_pids(self):
        sid = str(uuid.uuid4())
        lines = [(31, f"claude --session-id {sid}")]
        with mock.patch.object(server, "_ps_claude_lines", return_value=lines), \
             mock.patch.object(server, "list_sessions", return_value=[]), \
             mock.patch.object(server, "_pid_in_tmux", return_value=False), \
             mock.patch.object(server, "_reptyr_target_pids", return_value={31}):
            self.assertEqual(server.external_claude_sessions(), {})

    def test_flagless_excludes_relayed_pids(self):
        lines = [(32, "claude --dangerously-skip-permissions")]
        with mock.patch.object(server, "_ps_claude_lines", return_value=lines), \
             mock.patch.object(server, "_pid_in_tmux", return_value=False), \
             mock.patch.object(server, "_pid_cwd", return_value="/tmp/x"), \
             mock.patch.object(server, "_reptyr_target_pids", return_value={32}):
            self.assertEqual(server.flagless_claude_sessions(), [])


class MigrateTests(unittest.TestCase):
    """migrate_session: 検証 → tmux+reptyr 起動 → ポーリング → sid 刻印."""

    def setUp(self):
        self.sid = str(uuid.uuid4())
        self.ok = subprocess.CompletedProcess([], 0, "", "")
        self.tmux_calls = []

        def fake_tmux(*a):
            self.tmux_calls.append(a)
            if a[0] == "new-session":
                # -P -F '#{pane_id}' の出力 — _wait_for_steal が使う pane id
                return subprocess.CompletedProcess([], 0, "%7\n", "")
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
        self.assertIn("-T 4242", new_sess[0][-1])        # reptyr コマンド文字列
        self.assertIn("/tmp/proj", new_sess[0])          # -c <cwd>
        self.assertIn("-P", new_sess[0])                 # pane id を受け取る
        self.assertIn("#{pane_id}", new_sess[0])
        # _wait_for_steal には new-session が出力した pane id が渡る
        wait_args = server._wait_for_steal.call_args.args
        self.assertIn("%7", wait_args)
        stamps = [c for c in self.tmux_calls if c[0] == "set-option"]
        self.assertEqual(len(stamps), 1)
        self.assertIn(self.sid, stamps[0])

    def test_flagless_names_after_cwd_and_skips_stamp(self):
        r = server.migrate_session(4242)
        self.assertEqual(r["name"], "proj")              # /tmp/proj の basename
        self.assertEqual(
            [c for c in self.tmux_calls if c[0] == "set-option"], [])

    def test_steal_failure_with_dead_session_does_not_kill(self):
        # 失敗 = reptyr 退場(セッション消滅) or ターゲット死。セッションが
        # もう無いのに kill を打つ必要はない(2026-06-12 事故の教訓: kill は
        # 「確実に空の husk」にしか打たない)。
        with mock.patch.object(server, "_wait_for_steal", return_value=False):
            with self.assertRaises(RuntimeError):
                server.migrate_session(4242, sid=self.sid)
        kills = [c for c in self.tmux_calls if c[0] == "kill-session"]
        self.assertEqual(kills, [])

    def test_steal_failure_kills_leftover_husk_only_when_present(self):
        # has_session: 1回目=_unique_session_name(空き確認→False)、
        # 2回目=失敗後の husk 残存確認(→True なら掃除する)
        with mock.patch.object(server, "_wait_for_steal", return_value=False), \
             mock.patch.object(server, "has_session", side_effect=[False, True]):
            with self.assertRaises(RuntimeError):
                server.migrate_session(4242, sid=self.sid)
        kills = [c for c in self.tmux_calls if c[0] == "kill-session"]
        self.assertEqual(len(kills), 1)


class ReptyrStderrTests(unittest.TestCase):
    """移管失敗時に reptyr の stderr を捕捉して理由を表面化する。これが無いと
    『reptyr が終了しました』としか分からず、sshd 配下/権限不足などの実際の
    原因がユーザにもログにも残らない(実機で『うまくいかない』の核だった)。"""

    def test_drain_reads_and_unlinks(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "e.err"
            p.write_text("Unable to attach to pid 42: Operation not permitted\n")
            self.assertEqual(
                server._drain_reptyr_err(p),
                "Unable to attach to pid 42: Operation not permitted",
            )
            self.assertFalse(p.exists())  # 掃除される

    def test_drain_missing_file_is_empty(self):
        self.assertEqual(server._drain_reptyr_err(Path("/no/such/file")), "")

    def test_command_redirects_reptyr_stderr(self):
        sid = str(uuid.uuid4())
        ok = subprocess.CompletedProcess([], 0, "", "")
        calls = []

        def fake_tmux(*a):
            calls.append(a)
            if a[0] == "new-session":
                return subprocess.CompletedProcess([], 0, "%7\n", "")
            return ok

        with mock.patch.object(server, "_ps_claude_lines",
                               return_value=[(4242, f"claude --resume {sid}")]), \
             mock.patch.object(server, "_pid_in_tmux", return_value=False), \
             mock.patch.object(server, "reptyr_available", return_value=True), \
             mock.patch.object(server, "_pid_cwd", return_value="/tmp/proj"), \
             mock.patch.object(server, "has_session", return_value=False), \
             mock.patch.object(server, "_wait_for_steal", return_value=True), \
             mock.patch.object(server, "_tmux", side_effect=fake_tmux):
            server.migrate_session(4242, sid=sid)
        inner = [c for c in calls if c[0] == "new-session"][0][-1]
        self.assertIn("2>", inner)                       # stderr をファイルへ
        self.assertIn(str(server._reptyr_err_path(4242)), inner)

    def test_failure_surfaces_reptyr_reason(self):
        sid = str(uuid.uuid4())
        ok = subprocess.CompletedProcess([], 0, "", "")

        def fake_tmux(*a):
            if a[0] == "new-session":
                return subprocess.CompletedProcess([], 0, "%7\n", "")
            return ok

        with mock.patch.object(server, "_ps_claude_lines",
                               return_value=[(4242, f"claude --resume {sid}")]), \
             mock.patch.object(server, "_pid_in_tmux", return_value=False), \
             mock.patch.object(server, "reptyr_available", return_value=True), \
             mock.patch.object(server, "_pid_cwd", return_value="/tmp/proj"), \
             mock.patch.object(server, "has_session", return_value=False), \
             mock.patch.object(server, "_wait_for_steal", return_value=False), \
             mock.patch.object(server, "_drain_reptyr_err",
                               return_value="children of sshd cannot be attached"), \
             mock.patch.object(server, "_tmux", side_effect=fake_tmux):
            with self.assertRaises(RuntimeError) as cm:
                server.migrate_session(4242, sid=sid)
        self.assertIn("sshd", str(cm.exception))         # 実際の理由が出る


class MigrateAllTests(unittest.TestCase):
    """migrate_all_terminal_claudes: 外部(sid付き)+フラグレスの全ターミナル
    claude を tmux へ取り込む。1件の失敗が他を隠さないよう per-pid で集計する。"""

    def test_sid_known_goes_straight_to_resume_flagless_uses_reptyr(self):
        # ユーザ方針(2026-06-14): TMUX管理外のclaudeは「一度終了→resumeで新プロセス」。
        # reptyr は VSCode Remote-SSH では効かないので sid 既知は最初から kill+resume。
        # sid 不明(フラグレス)だけは会話IDが無く resume 不可 → reptyr が最後の手段。
        sid_a = str(uuid.uuid4())   # 11: sid 既知 → resume(takeover)
        sid_b = str(uuid.uuid4())   # 12: sid 既知だが resume 失敗 → failed
        mig_calls, res_calls = [], []

        def fake_resume(directory, session_id, name, takeover=False, force=False):
            res_calls.append((directory, session_id, takeover, force))
            if session_id == sid_b:
                raise RuntimeError("終了しませんでした")
            return {"created": True, "name": f"resume-{session_id[:4]}", "id": session_id}

        def fake_migrate(pid, sid="", name=""):
            mig_calls.append((pid, sid))
            return {"created": True, "name": f"mig{pid}", "id": None, "pid": pid}

        with mock.patch.object(server, "external_claude_sessions",
                               return_value={sid_a: 11, sid_b: 12}), \
             mock.patch.object(server, "flagless_claude_sessions",
                               return_value=[{"pid": 99, "cwd": "/t"}]), \
             mock.patch.object(server, "_pid_cwd", return_value="/t"), \
             mock.patch.object(server, "migrate_session", side_effect=fake_migrate), \
             mock.patch.object(server, "resume_session", side_effect=fake_resume):
            r = server.migrate_all_terminal_claudes()

        self.assertEqual(r["total"], 3)
        # sid 既知の 11 は resume で取り込み、12 は resume 失敗で failed
        self.assertEqual([x["pid"] for x in r["resumed"]], [11])
        # フラグレス 99 だけ reptyr(migrate_session)を使う
        self.assertEqual([m["pid"] for m in r["migrated"]], [99])
        self.assertEqual([f["pid"] for f in r["failed"]], [12])
        # sid 既知は migrate_session(reptyr)を一切呼ばない
        self.assertEqual(mig_calls, [(99, "")])
        # resume は takeover+force
        self.assertIn(("/t", sid_a, True, True), res_calls)

    def test_empty_when_no_terminal_claudes(self):
        with mock.patch.object(server, "external_claude_sessions", return_value={}), \
             mock.patch.object(server, "flagless_claude_sessions", return_value=[]):
            r = server.migrate_all_terminal_claudes()
        self.assertEqual(
            r, {"total": 0, "migrated": [], "resumed": [], "failed": []})


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


if __name__ == "__main__":
    unittest.main()
