#!/usr/bin/env python3
"""
cc-hub — an "ignition key" for Claude Code sessions.

This server does NOT do the conversation. It only does shell-level launch
orchestration on top of tmux:  pick a project dir -> start a Claude Code
session in a detached tmux session (with Remote Control enabled) -> list /
rename / kill running sessions.  The actual chat happens in the official
Claude mobile app's Remote Control view.

Design principles (see memory/project-goal.md):
  1. tmux is the single source of truth. No own session DB; every view is
     rebuilt from `tmux ls`.
  2. Always keep the CLI path. Sessions are plain `tmux attach`-able sessions.
  3. Never touch Claude's protocol. Only shell/tmux/process ops.
  4. Idempotent launch. attach-or-create; never double-launch the same name.

The server is pure Python 3 stdlib. It serves a static Bootstrap frontend from
web/ and a small JSON API under /api/* (token-gated). No npm/pip at runtime;
the Bootstrap assets are vendored under web/vendor/.
"""

import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from hmac import compare_digest
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
WEB_DIR = HERE / "web"
CLAUDE_JSON = Path.home() / ".claude.json"

# Session names: allow letters (incl. Japanese), digits, '_', '-' and spaces, up
# to 64 chars, first char not a space/'-'. This deliberately excludes '.' and ':'
# (tmux target syntax) and every shell metacharacter (quotes, $, `, \, ;, |, &,
# <, >, etc.), so the name stays safe both as a tmux arg and inside the launch
# shell string. `\w` is Unicode here, so 日本語 is permitted.
NAME_RE = re.compile(r"^[^\W][\w \-]{0,63}$", re.UNICODE)

# Static file content types served from web/.
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".webmanifest": "application/manifest+json; charset=utf-8",
    ".map": "application/json",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".ttf": "font/ttf",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".png": "image/png",
}


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def load_config() -> dict:
    """Load config.json, creating it (with a fresh token) on first run."""
    defaults = {
        "host": "0.0.0.0",
        "port": 8765,
        # Host used to build the phone-reachable URL (QR code, startup banner)
        # when bound to 0.0.0.0. Empty = auto-detect the LAN/tailnet IP. Set
        # this explicitly (e.g. your tailnet IP) if auto-detection guesses wrong.
        "public_host": "",
        # Root under which /api/projects lists candidate project dirs and where
        # the folder browser starts by default.
        "projects_root": str(Path.home()),
        # Absolute path to the claude binary (PATH inside tmux's shell is
        # unreliable, so we resolve and pin it).
        "claude_bin": shutil.which("claude") or str(Path.home() / ".local/bin/claude"),
    }
    cfg = {}
    if CONFIG_PATH.exists():
        cfg = json.loads(CONFIG_PATH.read_text())
    changed = False
    for k, v in defaults.items():
        if k not in cfg:
            cfg[k] = v
            changed = True
    # Generate a token only on first run (key absent). An explicit "token": ""
    # disables auth entirely — intended for tailnet-only deployments where the
    # network boundary (e.g. Tailscale) is the access control. See _authed().
    if "token" not in cfg:
        cfg["token"] = secrets.token_urlsafe(24)
        changed = True
    if changed:
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n")
        os.chmod(CONFIG_PATH, 0o600)
    return cfg


CONFIG = load_config()


def _detect_lan_ip() -> str:
    """Best-effort local IP for building the phone-reachable URL.

    Opens a UDP socket toward a public address (no packets are actually sent)
    and reads back the kernel-chosen source IP. Falls back to 127.0.0.1.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def public_url_host() -> str:
    """Host to advertise for phone access (LAN/tailnet IP when bound to 0.0.0.0)."""
    if CONFIG["host"] != "0.0.0.0":
        return CONFIG["host"]
    return CONFIG["public_host"] or _detect_lan_ip()


# --------------------------------------------------------------------------- #
# tmux helpers — the only place we touch the source of truth
# --------------------------------------------------------------------------- #
def _tmux(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["tmux", *args],
        capture_output=True,
        text=True,
        timeout=15,
    )


def list_sessions() -> list[dict]:
    """Rebuild the full session list from `tmux ls`. Source of truth."""
    # session_path = the cwd we launched in (groups sessions by project folder).
    # @ccwa_sid = the Claude session id we stamped on the session at launch, so
    # we can tie a live session back to its resumable conversation history.
    # @ccwa_spawn = "1" marks a spawn-server session (claude remote-control,
    # not a chat TUI); the flag — not the name prefix — is what classifies it.
    # pane_current_command (evaluated against each session's active pane) lets us
    # skip tmux sessions that aren't Claude — chiefly the web app's own
    # `ccwa-server` shell, which is a tmux session like any other but is NOT a
    # chat to manage.
    fmt = (
        "#{session_name}\t#{session_created}\t#{session_attached}"
        "\t#{session_windows}\t#{session_path}\t#{@ccwa_sid}\t#{@ccwa_spawn}"
        "\t#{pane_current_command}"
    )
    proc = _tmux("list-sessions", "-F", fmt)
    if proc.returncode != 0:
        # No server running yet -> no sessions. Anything else is a real error.
        if "no server running" in proc.stderr.lower() or "no such file" in proc.stderr.lower():
            return []
        raise RuntimeError(proc.stderr.strip() or "tmux list-sessions failed")
    sessions = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        name, created, attached, windows, spath, sid, spawn, cmd = (
            line.split("\t") + [""] * 8
        )[:8]
        # Only surface sessions that are actually Claude: the pane runs `claude`,
        # OR we launched it (an @ccwa_sid stamp survives even if a tool call
        # momentarily fronts a subprocess), OR it's a spawn server (a backgrounded
        # `claude remote-control` whose pane shows a shell — kept via the flag).
        # Everything else (the web app's own ccwa-server shell, stray sessions) is
        # not a Claude process and is hidden.
        if cmd != "claude" and not sid and spawn != "1":
            continue
        sessions.append(
            {
                "name": name,
                "created": int(created) if created.isdigit() else None,
                "attached": attached == "1",
                "windows": int(windows) if windows.isdigit() else None,
                "path": spath or None,
                "id": sid or None,
                "spawn": spawn == "1",
            }
        )
    sessions.sort(key=lambda s: s["name"].lower())
    return sessions


def has_session(name: str) -> bool:
    return _tmux("has-session", "-t", f"={name}").returncode == 0


def ensure_trusted(resolved_dir: str) -> None:
    """Pre-accept Claude's workspace trust dialog for `resolved_dir`.

    A detached launch into a brand-new / untrusted folder otherwise stalls on the
    interactive "Is this a project you trust?" prompt inside the tmux pane — the
    `--dangerously-skip-permissions` flag only skips it for piped/non-interactive
    runs, not for a real TTY. So we record the folder as trusted in ~/.claude.json
    before launching. (The separate dangerous-mode prompt is already suppressed
    globally via skipDangerousModePermissionPrompt.) Best-effort: never raise.
    """
    try:
        data = json.loads(CLAUDE_JSON.read_text()) if CLAUDE_JSON.exists() else {}
    except (OSError, json.JSONDecodeError):
        return  # don't risk clobbering an unreadable/locked state file
    projects = data.setdefault("projects", {})
    entry = projects.get(resolved_dir)
    if entry and entry.get("hasTrustDialogAccepted"):
        return  # already trusted
    if entry is None:
        entry = projects[resolved_dir] = {}
    entry["hasTrustDialogAccepted"] = True
    # Atomic write so a crash can't truncate Claude's state file.
    tmp = CLAUDE_JSON.with_name(CLAUDE_JSON.name + ".ccwa-tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, CLAUDE_JSON)
    except OSError:
        return


def launch_session(name: str, directory: str) -> dict:
    """Idempotent attach-or-create. Never double-launch the same name."""
    if not NAME_RE.match(name):
        raise ValueError(
            "Invalid session name. Letters (incl. 日本語), digits, '_', '-' and "
            "spaces are allowed; no leading space/'-', and no '.' or ':'."
        )
    dpath = Path(directory).expanduser().resolve()
    if not dpath.is_dir():
        raise ValueError(f"Directory does not exist: {directory}")

    if has_session(name):
        return {"created": False, "name": name}

    # Trust the folder up-front so a new/untrusted dir doesn't stall on the trust
    # dialog. Use the resolved path == the cwd Claude will record.
    ensure_trusted(str(dpath))

    # Pin a Claude session id we generate ourselves so we can later tie this
    # live session to its conversation history (for resume). We also stamp it on
    # the tmux session as @ccwa_sid, keeping tmux the single source of truth
    # (no separate session DB).
    sid = str(uuid.uuid4())

    # tmux runs this command string via the shell; name is validated to a safe
    # charset, sid is a generated UUID, and claude_bin is a pinned absolute path,
    # so no injection vector.
    # -n persists `name` as the session's custom-title (what the resume list
    # shows after a stop); without it the log would only ever get an ai-title.
    inner = (
        f'{CONFIG["claude_bin"]} --dangerously-skip-permissions '
        f'--session-id {sid} --remote-control "{name}" -n "{name}"'
    )
    proc = _tmux("new-session", "-d", "-s", name, "-c", str(dpath), inner)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "tmux new-session failed")
    # set-option rejects the "=name" exact-match form; the plain (just-created)
    # name resolves by exact match, so it is unambiguous here.
    _tmux("set-option", "-t", name, "@ccwa_sid", sid)
    return {"created": True, "name": name, "id": sid}


def _log(msg: str) -> None:
    """Server-console line (stderr); background stamping is detached, so without
    a trace a silent miss is invisible (made an earlier bug hard to diagnose)."""
    sys.stderr.write("[ccwa] %s\n" % msg)
    sys.stderr.flush()


def _pane_id(name: str) -> str:
    """First pane id for a session. send-keys/capture-pane need a real %N id
    (the `=name` exact-match form doesn't resolve for them)."""
    panes = _tmux("list-panes", "-t", f"={name}", "-F", "#{pane_id}")
    return panes.stdout.split("\n", 1)[0].strip() if panes.returncode == 0 else ""


def _type_rename(pane_id: str, new: str) -> None:
    """Drive Claude's own `/rename <new>` by typing it into the pane.

    `-l` sends the text literally; the separate Enter submits it. The pause
    matters: typing `/rename` opens Claude's slash-command popup, and if Enter
    arrives before it has closed (it closes once the argument is typed) Enter
    picks a menu entry instead of submitting. ~0.4s was too short in practice;
    ~1.2s is reliable. `new` must already be NAME_RE-validated.
    """
    _tmux("send-keys", "-t", pane_id, "-l", f"/rename {new}")
    time.sleep(1.2)
    _tmux("send-keys", "-t", pane_id, "Enter")


# The RC status-line text marking TUI readiness. Claude renamed it ("Remote
# Control active" → "/rc active" as of v2.1.173); match any known variant so a
# wording update doesn't silently break the resume-time stamp again.
_RC_READY_MARKERS = ("Remote Control active", "/rc active")


def _stamp_title_when_ready(name: str, timeout: float = 30.0) -> None:
    """Background (daemon-thread) task: wait for the TUI, then type `/rename`.

    Used on resume only — a fresh launch gets its title via `-n`, but a resumed
    process reattaches the EXISTING bridge session, where neither `-n` nor
    `--remote-control` moves the phone-visible name; only a live `/rename`
    does. Readiness = the RC status line in capture-pane (~2s after launch,
    no user activity needed). Best-effort, but always logged.
    """
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pane_id = _pane_id(name)
            if pane_id:
                cap = _tmux("capture-pane", "-t", pane_id, "-p")
                if cap.returncode == 0 and any(m in cap.stdout for m in _RC_READY_MARKERS):
                    time.sleep(0.5)  # small settle after the marker appears
                    _type_rename(pane_id, name)
                    _log("resume-stamp %r: /rename typed" % name)
                    return
            time.sleep(0.3)
        _log("resume-stamp %r: pane not ready within timeout; skipped" % name)
    except Exception as e:  # never break a resume over a cosmetic stamp
        _log("resume-stamp %r: exception %r" % (name, e))


def rename_session(old: str, new: str) -> dict:
    """Rename in place: type Claude's own `/rename` into the pane + tmux rename.

    Earlier this killed the session and relaunched it under `--remote-control
    <new>`, on the belief that only the launch flag moves the phone-visible
    name. Both halves proved wrong on device: `--resume` reattaches the
    EXISTING bridge session (same bridgeSessionId in the log), so the app keeps
    the name registered at creation and the relaunch name is ignored — while a
    live `/rename` DOES propagate to the connected app, and also writes the
    `custom-title` the resume list shows. So: no kill, no relaunch. The typed
    `/rename` updates the app name + log title on the running process, and
    `tmux rename-session` keeps our own label in step. `/rename` itself is
    robust on a live session: it works on an empty session and even
    mid-response (queued).
    """
    if not NAME_RE.match(new):
        raise ValueError(
            "Invalid new name. Letters (incl. 日本語), digits, '_', '-' and spaces "
            "are allowed; no leading space/'-', and no '.' or ':'."
        )
    target = next((s for s in list_sessions() if s["name"] == old), None)
    if target is None:
        raise ValueError(f"No such session: {old}")
    if old == new:
        return {"renamed": False, "name": old, "id": target.get("id")}
    if has_session(new):
        raise ValueError(f"A session named '{new}' already exists.")

    pane_id = _pane_id(old)
    if not pane_id:
        raise RuntimeError(f"Could not resolve the pane for session: {old}")
    _type_rename(pane_id, new)

    proc = _tmux("rename-session", "-t", f"={old}", new)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "tmux rename-session failed")
    return {"renamed": True, "name": new, "id": target.get("id")}


def rename_conversation(sid: str, new: str) -> dict:
    """Rename a STOPPED conversation by appending a `custom-title` record to its
    log. The resume list reads the LAST custom-title (see _read_conversation),
    so the new name wins on the next render — no tmux, no live `/rename`. A
    running session must go through rename_session instead (a log append would
    never reach the connected app)."""
    if not NAME_RE.match(new):
        raise ValueError(
            "Invalid new name. Letters (incl. 日本語), digits, '_', '-' and spaces "
            "are allowed; no leading space/'-', and no '.' or ':'."
        )
    if any(s.get("id") == sid for s in list_sessions()):
        raise ValueError("稼働中のセッションです。稼働中のまま改名してください。")
    p = _find_conversation_log(sid)
    if not p:
        raise ValueError("会話ログが見つかりません。")
    rec = {"type": "custom-title", "customTitle": new, "sessionId": sid}
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    _log("rename-conversation %s -> %r" % (sid, new))
    return {"renamed": True, "name": new, "id": sid}


def kill_session(name: str) -> None:
    if not has_session(name):
        raise ValueError(f"No such session: {name}")
    proc = _tmux("kill-session", "-t", f"={name}")
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "tmux kill-session failed")


def _unique_session_name(name: str) -> str:
    """Return `name`, or `name-2`, `name-3`, ... if it's already taken."""
    if not has_session(name):
        return name
    for i in range(2, 100):
        cand = f"{name}-{i}"
        if not has_session(cand):
            return cand
    return f"{name}-{uuid.uuid4().hex[:6]}"


# --------------------------------------------------------------------------- #
# Spawn servers — official `claude remote-control` (subcommand, NOT the
# per-session --remote-control flag) kept resident in tmux. One server per
# project folder; the mobile app / claude.ai/code creates sessions ON it (the
# new-session pulldown, or the environment URL below). Sessions spawned that
# way are headless children of the server process: killing the server kills
# them too. Verified on device 2026-06-12 (v2.1.173); see
# docs/superpowers/specs/2026-06-12-spawn-server-tab-design.md.
# --------------------------------------------------------------------------- #
SPAWN_NAME_PREFIX = "spawn-"

_SPAWN_ENV_URL_RE = re.compile(r"https://claude\.ai/code\?environment=env_\w+")
_SPAWN_CAPACITY_RE = re.compile(r"Capacity:\s*(\d+)/(\d+)")
# `Connected`(誰か接続中) と `Ready`(空サーバー) はどちらも登録完了状態。
_SPAWN_UP_RE = re.compile(r"\b(?:Connected|Ready)\b")
_SPAWN_CONNECTING_RE = re.compile(r"\bConnecting\b")


class SpawnServerExists(Exception):
    """A spawn server for this folder is already running. Carries the live
    server's info so the HTTP layer can 409 with something actionable."""

    def __init__(self, srv: dict):
        super().__init__(
            f"A spawn server is already running for this folder: {srv['name']}"
        )
        self.server = srv


def _parse_spawn_pane(text: str) -> dict:
    """Screen scrape of the server TUI -> state dict. The TUI wording is
    version-fragile (learned the hard way with the /rename stamp), so every
    field degrades to None/unknown instead of raising — the list must always
    render even if a CLI update reshuffles the text."""
    m = _SPAWN_CAPACITY_RE.search(text)
    if _SPAWN_UP_RE.search(text):
        status = "connected"
    elif _SPAWN_CONNECTING_RE.search(text):
        status = "connecting"
    else:
        status = "unknown"
    url = _SPAWN_ENV_URL_RE.search(text)
    return {
        "status": status,
        "env_url": url.group(0) if url else None,
        "capacity_used": int(m.group(1)) if m else None,
        "capacity_max": int(m.group(2)) if m else None,
    }


def _spawn_pane_state(name: str) -> dict:
    """capture-pane + parse for one server session; never raises."""
    try:
        pane = _pane_id(name)
        if pane:
            cap = _tmux("capture-pane", "-t", pane, "-p")
            if cap.returncode == 0:
                return _parse_spawn_pane(cap.stdout)
    except Exception:
        pass
    return {"status": "unknown", "env_url": None,
            "capacity_used": None, "capacity_max": None}


def list_spawn_servers() -> list[dict]:
    """Every running spawn server (@ccwa_spawn sessions) with live pane state."""
    out = []
    for s in list_sessions():
        if not s.get("spawn"):
            continue
        out.append(
            {
                "name": s["name"],
                "directory": s["path"],
                "folder": Path(s["path"]).name if s["path"] else None,
                "created": s["created"],
                **_spawn_pane_state(s["name"]),
            }
        )
    return out


def _spawn_session_name(folder: str) -> str:
    """tmux-safe session name for a folder's spawn server. Same charset rule
    as NAME_RE (no '.'/':'/shell metachars); non-conforming chars become '_'."""
    base = re.sub(r"[^\w\-]", "_", folder, flags=re.UNICODE).strip("_-") or "dir"
    return _unique_session_name(SPAWN_NAME_PREFIX + base[:48])


def launch_spawn_server(directory: str) -> dict:
    """Start (or refuse to double-start) a spawn server for `directory`."""
    dpath = Path(directory).expanduser().resolve()
    if not dpath.is_dir():
        raise ValueError(f"Directory does not exist: {directory}")
    for srv in list_spawn_servers():
        if srv["directory"] and _norm_path(srv["directory"]) == _norm_path(str(dpath)):
            raise SpawnServerExists(srv)

    ensure_trusted(str(dpath))
    name = _spawn_session_name(dpath.name)

    # --spawn=same-dir also skips the interactive mode prompt; the one-time
    # "Enable Remote Control? (y/n)" consent is already recorded globally in
    # ~/.claude.json (remoteDialogSeen), so a detached launch never stalls.
    # --no-create-session-in-dir = empty server; sessions come from the app.
    inner = (
        f'{CONFIG["claude_bin"]} remote-control '
        f"--spawn=same-dir --no-create-session-in-dir"
    )
    proc = _tmux("new-session", "-d", "-s", name, "-c", str(dpath), inner)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "tmux new-session failed")
    _tmux("set-option", "-t", name, "@ccwa_spawn", "1")

    # Wait (briefly) for the env URL so the UI can show a tappable link right
    # away. A timeout is not an error — the server keeps registering in tmux
    # and the list poll picks the URL up on a later refresh.
    deadline = time.monotonic() + 15.0
    state = _spawn_pane_state(name)
    while time.monotonic() < deadline and not state["env_url"]:
        time.sleep(1.0)
        state = _spawn_pane_state(name)
    return {
        "name": name,
        "directory": str(dpath),
        "folder": dpath.name,
        "created": int(time.time()),
        **state,
    }


def stop_spawn_server(name: str) -> None:
    """kill-session, but only for sessions stamped @ccwa_spawn — a chat session
    passed here by mistake must never be killed through this path."""
    if not has_session(name):
        raise ValueError(f"No such session: {name}")
    # show-options rejects the `=name` exact-match form (same tmux quirk as
    # capture-pane); has_session above already proved the exact name exists,
    # so the plain form resolves to it unambiguously.
    proc = _tmux("show-options", "-t", name, "-v", "@ccwa_spawn")
    if proc.returncode != 0 or proc.stdout.strip() != "1":
        raise ValueError(f"Not a spawn server session: {name}")
    kill = _tmux("kill-session", "-t", f"={name}")
    if kill.returncode != 0:
        raise RuntimeError(kill.stderr.strip() or "tmux kill-session failed")


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
        # argv[0] must BE the claude binary. A looser "claude appears in argv"
        # match would catch the tmux server (its argv is the spawning "tmux
        # new-session ... claude ..." command), and a takeover would then
        # SIGTERM the tmux server — killing every session at once.
        first = args.split(" ", 1)[0]
        if pid.isdigit() and (first == "claude" or first.endswith("/claude")):
            out.append((int(pid), args))
    return out


def _pid_in_tmux(pid: int) -> bool:
    """True if the process was spawned inside tmux (TMUX in its environment).

    The sid-based exclusion in external_claude_sessions has a gap: after
    `tmux kill-session` the session vanishes from tmux instantly, but the
    pane's claude lingers ~1s handling SIGHUP — sid no longer in @ccwa_sid,
    process still in ps, so the row would flash as "external" right after
    every kill (the UI refreshes immediately on kill success and lands in
    that window almost every time). The environment outlives the session, so
    TMUX= is the reliable "this is/was a tmux pane" marker."""
    try:
        environ = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return False  # already gone or unreadable — not one of our panes
    return any(part.startswith(b"TMUX=") for part in environ.split(b"\0"))


def _reptyr_target_pids() -> set[int]:
    """Pids currently relayed by a live reptyr (i.e. already migrated into
    tmux). A migrated process keeps its exec-time environment, so the TMUX=
    check in _pid_in_tmux can never learn about the move — without this
    exclusion a migrated (especially flagless) claude would keep showing as
    "external" forever, duplicating its live tmux row."""
    proc = subprocess.run(
        ["ps", "-eo", "args="], capture_output=True, text=True, timeout=10
    )
    pids = set()
    for line in proc.stdout.splitlines():
        m = re.match(r"(?:\S*/)?reptyr\s+(?:-T\s+)?(\d+)\s*$", line.strip())
        if m:
            pids.add(int(m.group(1)))
    return pids


def external_claude_sessions() -> dict[str, int]:
    """sid -> pid for claude processes running OUTSIDE tmux entirely.

    Anything inside tmux is excluded — live launcher sessions via @ccwa_sid,
    panes (foreign or dying) via the TMUX-environment check, and migrated
    processes via their resident reptyr relay. What's left is a session
    started by hand in a plain terminal."""
    tmux_sids = {s["id"] for s in list_sessions() if s.get("id")}
    relayed = _reptyr_target_pids()
    ext = {}
    for pid, args in _ps_claude_lines():
        m = _SID_FLAG_RE.search(args)
        if not m:
            continue
        sid = m.group(1)
        if (UUID_RE.match(sid) and sid not in tmux_sids
                and pid not in relayed and not _pid_in_tmux(pid)):
            ext[sid] = pid
    return ext


class MaybeLiveError(ValueError):
    """Resume guard: the conversation MIGHT be running in a terminal (flagless
    launch — argv carries no sid, so it can't be proven). HTTP layer maps this
    to 409 so the UI can confirm-and-force instead of failing flat."""


def _pid_cwd(pid: int) -> str | None:
    """Working directory of a process, or None. Separated for testability."""
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return None


def flagless_claude_sessions() -> list[dict]:
    """{pid, cwd} for every flagless (no sid in argv) non-tmux claude.

    These can't be tied to a conversation, but reptyr migration only needs the
    pid — so the overview lists them as migratable "external" rows."""
    relayed = _reptyr_target_pids()
    out = []
    for pid, args in _ps_claude_lines():
        if _SID_FLAG_RE.search(args):
            continue  # sid-flagged: handled precisely via external_claude_sessions
        if pid in relayed or _pid_in_tmux(pid):
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
    raise RuntimeError(
        "ターミナル側のプロセスが終了しませんでした。手動で終了してから再開してください。"
    )


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


def _reptyr_err_path(pid: int) -> Path:
    """Where reptyr's stderr is captured for a migration attempt. reptyr runs
    detached inside a tmux pane, so without this its error text (the only thing
    that says WHY a steal failed — permission, sshd-child, bad tty) would
    vanish with the pane. pid is unique while the target lives, so no clash."""
    return Path(tempfile.gettempdir()) / f"ccwa-reptyr-{pid}.err"


def _drain_reptyr_err(path: Path) -> str:
    """Read and delete the captured reptyr stderr; '' if absent/empty."""
    try:
        text = path.read_text().strip()
    except OSError:
        text = ""
    try:
        path.unlink()
    except OSError:
        pass
    return text


def _pid_tty(pid: int) -> int | None:
    """tty_nr of a process (/proc/<pid>/stat field 7), or None if gone.

    comm (field 2) is in parens and may contain spaces, so split AFTER the
    closing paren instead of naively splitting the whole line."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return int(stat.rsplit(")", 1)[1].split()[4])
    except (OSError, IndexError, ValueError):
        return None


def _wait_for_steal(pid: int, name: str, pane: str,
                    timeout: float = 6.0) -> bool:
    """Poll until the reptyr steal is CONFIRMED by redrawn pane content.

    Success signal: the pane shows content (the relayed TUI) and the session
    survives a moment longer (the re-check filters the error-text race). A
    successful `-T` steal renders within ~1s in practice, so reaching the
    timeout means content NEVER appeared — i.e. reptyr attached but never
    relayed. That is a FAILURE, not a success.

    Definitive early failure: the tmux session died (a failing reptyr prints an
    error and exits, taking pane and session with it) or the target vanished.

    Why timeout==failure now (reversed from 2026-06-12): back then capture-pane
    used a "=name" target it could not resolve (tmux 3.4), so the content check
    NEVER fired and every steal — success or not — hit the timeout; treating
    timeout as failure then killed successful migrations, so it was flipped to
    "timeout==success". With the pane-id fix the content check works, so a real
    success returns early via content and only genuine non-relays reach the
    timeout. The 2026-06-14 repro nailed the cost of the old rule: reptyr -T
    against a VSCode Remote-SSH ptyHost master stays RESIDENT but relays
    NOTHING (blank pane, no stderr) — "timeout==success" reported a false
    success while claude never actually moved. Content-or-bust fixes that.

    `-T` does NOT change the target's tty_nr (slave stays; only the master
    moves), so tty change is not a usable signal — pane content is."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not has_session(name):
            return False
        if _pid_tty(pid) is None:
            return False
        cap = _tmux("capture-pane", "-p", "-t", pane)
        if cap.returncode == 0 and cap.stdout.strip():
            time.sleep(0.7)
            return has_session(name)
        time.sleep(0.4)
    return False  # no relayed content within the window -> steal did not land


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

    # pid is a validated int and the reptyr path comes from shutil.which, so
    # the shell command string is injection-free. -c groups the session under
    # the project folder in the overview (session_path); -P -F hands back the
    # pane id, the only target capture-pane can reliably resolve.
    # Capture reptyr's stderr to a file: it runs detached in the pane, so its
    # error text (the ONLY thing that says WHY a steal failed) would otherwise
    # die with the pane. The path holds only a validated int pid, safe to quote
    # into the shell string alongside the which()-resolved reptyr path.
    err_path = _reptyr_err_path(pid)
    _drain_reptyr_err(err_path)  # clear any stale capture from a prior attempt
    cmd = ["new-session", "-d", "-P", "-F", "#{pane_id}", "-s", name]
    if cwd:
        cmd += ["-c", cwd]
    cmd.append(f"{shutil.which('reptyr')} -T {pid} 2>'{err_path}'")
    proc = _tmux(*cmd)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "tmux new-session failed")
    pane = proc.stdout.strip()

    if not _wait_for_steal(pid, name, pane):
        # Steal did not land: reptyr either exited (error) or stayed RESIDENT
        # but relayed nothing (blank pane — the VSCode/SSH ptyHost case). Either
        # way the session now holds only a useless/half-attached reptyr, so kill
        # it; that also releases a master reptyr may have hijacked, restoring the
        # original terminal (verified: the target survives the kill).
        if has_session(name):
            _tmux("kill-session", "-t", f"={name}")
        reason = _drain_reptyr_err(err_path)
        msg = (
            "移管に失敗しました。ターミナル側のプロセスは、生きていれば元の"
            "ターミナルにそのまま残っています。"
        )
        if reason:
            # reptyr printed a reason, e.g. permission / sshd-child / bad tty.
            msg += f"\nreptyr: {reason}"
        else:
            # No stderr + blank pane = reptyr attached but never relayed. This is
            # the VSCode Remote-SSH / ptyHost master case: -T can't steal it.
            # kill+resume (a sid-known session) is the reliable fallback.
            msg += (
                "\nreptyr が接続後に画面を中継できませんでした"
                "(VSCode Remote-SSH / ptyHost のターミナルでよく起きます)。"
                "会話を保持したまま tmux へ入れるには「再開(kill+resume)」を"
                "お使いください(生成途中の表示のみ失われます)。"
            )
        raise RuntimeError(msg)
    _drain_reptyr_err(err_path)  # success: reptyr is resident, nothing to report
    if sid:
        _tmux("set-option", "-t", name, "@ccwa_sid", sid)
    return {"created": True, "name": name, "id": sid or None, "pid": pid}


def migrate_all_terminal_claudes() -> dict:
    """Adopt EVERY terminal-launched claude into tmux in one go — the core
    "stop scattering sessions across terminals/spawn, manage them in tmux"
    action. Covers both sid-flagged (external_claude_sessions) and flagless
    (flagless_claude_sessions) processes.

    Policy (user decision 2026-06-14): a terminal claude that isn't under tmux
    gets terminated once and resumed as a fresh tmux process. So when the
    conversation id is known (sid-flagged), go STRAIGHT to kill+resume — no
    reptyr first: its live steal can't work under VSCode Remote-SSH anyway and
    would only burn the steal-timeout. Flagless processes carry no conversation
    id to resume, so reptyr is their only (last-resort) option. Best-effort and
    sequential: one failure never aborts the sweep.

    Returns three buckets: `resumed` (sid-known, kill+resume), `migrated`
    (flagless, live reptyr), `failed`."""
    resumed, migrated, failed = [], [], []
    # sid-known: terminate + resume the conversation in tmux.
    for sid, pid in external_claude_sessions().items():
        cwd = _pid_cwd(pid)
        if not cwd:
            failed.append({"pid": pid, "error": "作業ディレクトリを取得できません。"})
            continue
        try:
            rr = resume_session(cwd, sid, "", takeover=True, force=True)
            resumed.append({"pid": pid, "name": rr["name"]})
        except Exception as e:  # noqa: BLE001 — record and keep sweeping
            failed.append({"pid": pid, "error": str(e)})
    # flagless: no conversation id to resume -> reptyr is the only option.
    for f in flagless_claude_sessions():
        try:
            r = migrate_session(f["pid"])
            migrated.append({"pid": f["pid"], "name": r["name"]})
        except Exception as e:  # noqa: BLE001
            failed.append({"pid": f["pid"], "error": str(e)})
    total = len(resumed) + len(migrated) + len(failed)
    return {"total": total, "migrated": migrated,
            "resumed": resumed, "failed": failed}


# --------------------------------------------------------------------------- #
# Conversation history (for resume) — read-only view of Claude's own logs under
# ~/.claude/projects/<cwd-slug>/<session-id>.jsonl. We never write these.
# --------------------------------------------------------------------------- #
CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _user_text(o: dict) -> str | None:
    msg = o.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                return part.get("text")
    return None


# User-role records that aren't something the human actually typed: injected
# context and plumbing all begin with a lowercase XML-ish tag
# (<system-reminder>, <command-*>, <task-notification>, <bash-stdout>, ...);
# a couple of others are plain-text sentinels.
_TAG_PREFIX_RE = re.compile(r"^<[a-z][\w-]*>")
_NON_INPUT_PREFIXES = ("Caveat:", "[Request interrupted")


def _real_user_text(o: dict) -> str | None:
    """The human's typed text from a user record, or None for injected/system ones."""
    txt = _user_text(o)
    if not txt or not txt.strip():
        return None
    s = txt.lstrip()
    if s.startswith(_NON_INPUT_PREFIXES) or _TAG_PREFIX_RE.match(s):
        return None
    return txt


def _read_conversation(jsonl: Path) -> dict | None:
    """Extract resume metadata (cwd, name/title, last input, mtime) from one log."""
    sid = jsonl.stem
    if not UUID_RE.match(sid):
        return None
    cwd = None
    custom_title = None   # explicit name from /rename (highest priority)
    ai_title = None       # the name Claude auto-assigns to the session
    last_prompt = None    # fallback: the recorded "last-prompt"
    last_user = None      # preferred: the actual last thing the human typed
    try:
        with jsonl.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if cwd is None and o.get("cwd"):
                    cwd = o["cwd"]
                t = o.get("type")
                if t == "custom-title" and o.get("customTitle"):
                    custom_title = o["customTitle"]
                elif t == "ai-title" and o.get("aiTitle"):
                    ai_title = o["aiTitle"]
                elif t == "last-prompt" and o.get("lastPrompt"):
                    last_prompt = o["lastPrompt"]
                elif t == "user":
                    txt = _real_user_text(o)
                    if txt:
                        last_user = txt
    except OSError:
        return None
    if not cwd:
        return None
    try:
        mtime = int(jsonl.stat().st_mtime)
    except OSError:
        mtime = 0
    title = custom_title or ai_title       # the name the session goes by
    last = last_user or last_prompt        # the most recent human input
    if last:
        last = " ".join(last.split())[:140]
    return {"id": sid, "cwd": cwd, "title": title, "last": last, "modified": mtime}


def _snippet(text: str, needle: str, before: int = 40, after: int = 100) -> str:
    """One-line excerpt around the first case-insensitive match of `needle`."""
    flat = " ".join(text.split())
    idx = flat.lower().find(needle)
    if idx < 0:
        return flat[:140]
    start = max(0, idx - before)
    end = min(len(flat), idx + len(needle) + after)
    snip = flat[start:end]
    if start > 0:
        snip = "…" + snip
    if end < len(flat):
        snip = snip + "…"
    return snip


def _search_conversation(jsonl: Path, needle: str) -> dict | None:
    """First match of `needle` (already lowercased) in one log, or None.

    Scope is the human's typed messages + the session title only — never the
    assistant's replies. Returns the match's metadata and a one-line snippet.
    """
    sid = jsonl.stem
    if not UUID_RE.match(sid):
        return None
    cwd = None
    custom_title = None
    ai_title = None
    last_user = None   # context fallback when only the title matched
    hit_text = None    # the user message that matched (first one wins)
    try:
        with jsonl.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if cwd is None and o.get("cwd"):
                    cwd = o["cwd"]
                t = o.get("type")
                if t == "custom-title" and o.get("customTitle"):
                    custom_title = o["customTitle"]
                elif t == "ai-title" and o.get("aiTitle"):
                    ai_title = o["aiTitle"]
                elif t == "user":
                    txt = _real_user_text(o)
                    if txt:
                        last_user = txt
                        if hit_text is None and needle in txt.lower():
                            hit_text = txt
    except OSError:
        return None
    if not cwd:
        return None
    title = custom_title or ai_title
    title_hit = bool(title and needle in title.lower())
    if hit_text is None and not title_hit:
        return None
    try:
        mtime = int(jsonl.stat().st_mtime)
    except OSError:
        mtime = 0
    # Prefer a snippet built around the body match; otherwise the title matched,
    # so show the latest human input as context for the row.
    snippet = (
        _snippet(hit_text, needle)
        if hit_text is not None
        else " ".join((last_user or "").split())[:140]
    )
    return {"id": sid, "cwd": cwd, "title": title, "snippet": snippet, "modified": mtime}


def search_conversations(query: str, project: str | None = None, limit: int = 50) -> list[dict]:
    """User-text/title search across all conversation logs, newest-first.

    `project` (a folder path) restricts results to conversations whose cwd is
    that folder; compared by resolved path so same-named folders don't collide.
    """
    q = (query or "").strip()
    if not q or not CLAUDE_PROJECTS.is_dir():
        return []
    needle = q.lower()
    proj_norm = _norm_path(project) if project else None
    results = []
    for d in CLAUDE_PROJECTS.iterdir():
        if not d.is_dir():
            continue
        for jsonl in d.glob("*.jsonl"):
            m = _search_conversation(jsonl, needle)
            if not m:
                continue
            if proj_norm and _norm_path(m["cwd"]) != proj_norm:
                continue
            results.append(m)
    results.sort(key=lambda r: r["modified"], reverse=True)
    return results[:limit]


def list_conversations() -> list[dict]:
    """Resumable conversations across projects, newest-first.

    Only sessions the user actually used: we skip any whose log records no human
    prompt (`last` is None). A launch-then-stop with no chat is an empty session
    — nothing to resume — so it must not clutter the list. (Such a log also has
    no `cwd` and is already dropped by `_read_conversation`; the `last` check is
    the explicit, intent-revealing guard, and also covers a session that booted
    far enough to get an auto-title but was stopped before any prompt.)
    """
    if not CLAUDE_PROJECTS.is_dir():
        return []
    convos = []
    for d in CLAUDE_PROJECTS.iterdir():
        if not d.is_dir():
            continue
        for jsonl in d.glob("*.jsonl"):
            c = _read_conversation(jsonl)
            if c and c.get("last"):
                convos.append(c)
    convos.sort(key=lambda c: c["modified"], reverse=True)
    return convos


def _find_conversation_log(sid: str) -> Path | None:
    """Path to `<sid>.jsonl` under any project dir, or None if none exists yet.

    Only a conversation with an on-disk log can be `--resume`d; a brand-new /
    idle session writes no jsonl until first activity, so file existence is the
    reliable "is there anything to resume?" test (see the rename/resume paths).
    """
    if not (UUID_RE.match(sid or "") and CLAUDE_PROJECTS.is_dir()):
        return None
    for d in CLAUDE_PROJECTS.iterdir():
        if d.is_dir():
            p = d / f"{sid}.jsonl"
            if p.is_file():
                return p
    return None


def _norm_path(p: str) -> str:
    try:
        return str(Path(p).resolve())
    except (OSError, ValueError):
        return p


# --------------------------------------------------------------------------- #
# Archive (hide) state — view-state only. The conversation jsonl files are
# never touched; archive.json just records which *stopped* conversations the
# UI should hide. Not a session DB (tmux stays the source of truth for live
# sessions). Kept as JSON+lock, not SQLite: this server is the only writer
# (the port bind forbids a second instance), so an in-process lock plus the
# ensure_trusted-style atomic replace is all the exclusion there is to need.
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
    if sid in external_claude_sessions():
        raise ValueError("ターミナルで稼働中のセッションはアーカイブできません。")
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


def build_overview() -> list[dict]:
    """Project-grouped view: live sessions + resumable conversations per folder.

    The project list is the union of folders that have a live tmux session and
    folders that have Claude conversation history. A conversation that is the
    currently-running session (its id matches a live @ccwa_sid) is shown only as
    a live session, not duplicated in the resumable list.
    """
    # Spawn servers live in their own tab (/api/spawn-servers), not here.
    sessions = [s for s in list_sessions() if not s.get("spawn")]
    convos = list_conversations()
    live_ids = {s["id"] for s in sessions if s.get("id")}
    convo_by_id = {c["id"]: c for c in convos}
    projects: dict[str, dict] = {}

    # Attach each live session's last conversation line (for the row subtitle).
    for s in sessions:
        c = convo_by_id.get(s.get("id"))
        s["last"] = c["last"] if c else None
        s["title"] = c["title"] if c else None

    def bucket(path: str) -> dict:
        key = _norm_path(path) if path else ""
        if key not in projects:
            projects[key] = {
                "path": key,
                "name": Path(key).name if key else "(フォルダ不明)",
                "sessions": [],
                "external": [],
                "resumable": [],
                "recent": 0,
            }
        return projects[key]

    for s in sessions:
        b = bucket(s.get("path") or "")
        b["sessions"].append(s)
        b["recent"] = max(b["recent"], s.get("created") or 0)

    archived = load_archived()
    external = external_claude_sessions()
    for c in convos:
        # Hidden = archived; a live session is never hidden (archive rejects
        # live ids, and the resumable list already excludes live ones anyway).
        if c["id"] in live_ids or c["id"] in archived:
            continue
        b = bucket(c["cwd"])
        if c["id"] in external:
            # Running outside tmux (terminal launch): offer take-over, not
            # resume — resuming it as-is would double-run the conversation.
            b["external"].append({**c, "pid": external[c["id"]]})
        else:
            b["resumable"].append(c)
        b["recent"] = max(b["recent"], c["modified"])

    for f in flagless_claude_sessions():
        # A terminal claude we can see but can't name (no sid in argv). Same
        # external list so the UI offers the one action that works: migrate.
        b = bucket(f["cwd"] or "")
        b["external"].append(
            {"id": None, "title": None, "last": None, "modified": 0,
             "cwd": f["cwd"], "pid": f["pid"]}
        )

    out = list(projects.values())
    out.sort(key=lambda p: p["recent"], reverse=True)
    return out


def resume_session(
    directory: str, session_id: str, name: str,
    takeover: bool = False, force: bool = False,
) -> dict:
    """Bring a past conversation back as a live tmux + Remote Control session.

    `takeover=True` additionally claims a conversation that is currently
    running OUTSIDE tmux (terminal launch): the external process is SIGTERMed
    and, once it exits, the same conversation is resumed inside tmux.

    `force=True` skips the flagless-launch guard (MaybeLiveError): when a
    sid-less claude runs in the same folder we can't prove it's this
    conversation, so we warn instead of refusing outright — the UI confirms
    with the user and retries with force."""
    if not UUID_RE.match(session_id or ""):
        raise ValueError("Invalid session id.")
    dpath = Path(directory).expanduser().resolve()
    if not dpath.is_dir():
        raise ValueError(f"Directory does not exist: {directory}")

    # Resuming an archived conversation un-archives it: resume is the clearest
    # possible "I want this back" signal, and without this the session would
    # silently vanish from the list again the next time it's killed.
    unarchive_conversation(session_id)

    # Already running? Don't double-launch the same conversation.
    for s in list_sessions():
        if s.get("id") == session_id:
            return {"created": False, "name": s["name"], "id": session_id}

    # Running outside tmux? Either take it over (kill, then resume below) or
    # refuse — a plain resume would run the same conversation twice.
    ext_pid = external_claude_sessions().get(session_id)
    if ext_pid:
        if not takeover:
            raise ValueError(
                "この会話はターミナルで稼働中です。「tmuxへ移管」から実行してください。"
            )
        _terminate_pid(ext_pid)
    elif not force and not takeover:
        # Flagless guard: a sid-less terminal claude in the same folder might
        # BE this conversation (the 62c575b1 double-run). Warn, don't kill.
        flagless = _flagless_claude_in_cwd(str(dpath))
        if flagless:
            raise MaybeLiveError(
                "このフォルダでターミナル起動の Claude が稼働中です (pid %d)。"
                "この会話のセッションかもしれません。再開すると同じ会話が二重に"
                "動く可能性があります(再開してもターミナル側は終了されません。"
                "移管したい場合は一覧の「tmuxへ移管」を使ってください)。" % flagless
            )

    requested = name if name and NAME_RE.match(name) else ""
    name = requested or f"resume-{session_id[:8]}"
    name = _unique_session_name(name)

    ensure_trusted(str(dpath))
    # session_id is a validated UUID, name a validated charset, claude_bin pinned.
    inner = (
        f'{CONFIG["claude_bin"]} --dangerously-skip-permissions '
        f'--resume {session_id} --remote-control "{name}"'
    )
    proc = _tmux("new-session", "-d", "-s", name, "-c", str(dpath), inner)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "tmux new-session failed")
    # set-option rejects the "=name" exact-match form; the plain (just-created)
    # name resolves by exact match, so it is unambiguous here.
    _tmux("set-option", "-t", name, "@ccwa_sid", session_id)
    # A resumed process reattaches the existing bridge session, so the
    # phone-visible name ignores `-n`/`--remote-control`; only a live /rename
    # moves it. Stamp in the background once the TUI is up — but only when the
    # tmux name round-trips to the requested title: a collision suffix "-2" or
    # the resume-<sid> fallback is a mechanical label that must not clobber
    # the conversation's real title.
    if name == requested:
        threading.Thread(
            target=_stamp_title_when_ready, args=(name,), daemon=True
        ).start()
    return {"created": True, "name": name, "id": session_id}


def _list_subdirs(base: Path) -> list[dict]:
    """Shared dir enumeration: visible subdirectories, name-sorted, with git flag."""
    entries = []
    for child in sorted(base.iterdir(), key=lambda p: p.name.lower()):
        if child.name.startswith("."):
            continue
        try:
            if child.is_dir():
                entries.append(
                    {"name": child.name, "path": str(child), "git": (child / ".git").exists()}
                )
        except PermissionError:
            continue
    return entries


def list_projects() -> list[dict]:
    """List immediate subdirectories of projects_root as launch candidates."""
    root = Path(CONFIG["projects_root"]).expanduser()
    if not root.is_dir():
        return []
    return _list_subdirs(root)


def browse_dir(path: str) -> dict:
    """List subdirectories of an arbitrary path for the folder browser."""
    if path:
        base = Path(path).expanduser()
    else:
        base = Path(CONFIG["projects_root"]).expanduser()
        if not base.is_dir():
            base = Path.home()
    try:
        base = base.resolve()
    except OSError:
        raise ValueError(f"Invalid path: {path}")
    if not base.is_dir():
        raise ValueError(f"Not a directory: {base}")
    try:
        entries = _list_subdirs(base)
    except PermissionError:
        raise ValueError(f"Permission denied: {base}")
    parent = str(base.parent) if base.parent != base else None
    return {"path": str(base), "parent": parent, "entries": entries}


def make_dir(parent: str, name: str) -> dict:
    """Create a single new subdirectory under an existing parent (idempotent)."""
    name = name or ""
    if not name or name.strip() != name or "/" in name or name in (".", ".."):
        raise ValueError("Invalid folder name (no '/', no surrounding spaces, not '.'/'..').")
    pbase = Path(parent).expanduser()
    if not pbase.is_dir():
        raise ValueError(f"Parent directory does not exist: {parent}")
    target = pbase / name
    try:
        target.mkdir(parents=False, exist_ok=True)
    except PermissionError:
        raise ValueError(f"Permission denied creating: {target}")
    return {"path": str(target.resolve())}


# --------------------------------------------------------------------------- #
# HTTP layer
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    server_version = "ccwebapp/1.0"
    protocol_version = "HTTP/1.1"

    # --- helpers --------------------------------------------------------- #
    def _send_json(self, obj, status=HTTPStatus.OK):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self, urlpath: str):
        rel = urlpath.lstrip("/") or "index.html"
        webroot = WEB_DIR.resolve()
        target = (webroot / rel).resolve()
        # Path-traversal guard: target must stay inside web/.
        if target != webroot and webroot not in target.parents:
            return self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        if not target.is_file():
            return self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        ctype = CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream")
        body = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # Vendored assets are immutable enough to cache; app files stay fresh.
        if rel.startswith("vendor/"):
            self.send_header("Cache-Control", "public, max-age=86400")
        else:
            self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _authed(self) -> bool:
        token = CONFIG.get("token") or ""
        if not token:
            return True  # auth disabled — network boundary (Tailscale) is the gate
        supplied = self.headers.get("X-Auth-Token", "")
        return bool(supplied) and compare_digest(supplied, token)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw or b"{}")

    def _query(self) -> dict:
        return parse_qs(urlparse(self.path).query)

    # --- routing --------------------------------------------------------- #
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/healthz":
            return self._send_json({"ok": True})
        if path.startswith("/api/"):
            if not self._authed():
                return self._send_json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
            try:
                if path == "/api/sessions":
                    return self._send_json({"sessions": list_sessions()})
                if path == "/api/overview":
                    return self._send_json({"projects": build_overview()})
                if path == "/api/projects":
                    return self._send_json({"projects": list_projects()})
                if path == "/api/browse":
                    target = self._query().get("path", [""])[0]
                    return self._send_json(browse_dir(target))
                if path == "/api/info":
                    return self._send_json({"host": public_url_host(), "port": CONFIG["port"]})
                if path == "/api/archived":
                    return self._send_json({"archived": list_archived_conversations()})
                if path == "/api/spawn-servers":
                    return self._send_json({"servers": list_spawn_servers()})
                if path == "/api/search":
                    q = self._query().get("q", [""])[0]
                    project = self._query().get("project", [""])[0] or None
                    results = search_conversations(q, project)
                    # Mark which hits are currently running (same @ccwa_sid → live
                    # mapping build_overview uses) and attach the project name.
                    by_id = {s["id"]: s for s in list_sessions() if s.get("id")}
                    archived_ids = load_archived()
                    ext_map = external_claude_sessions()
                    for r in results:
                        live = by_id.get(r["id"])
                        r["running"] = bool(live)
                        r["runningName"] = live["name"] if live else None
                        r["archived"] = r["id"] in archived_ids
                        r["external"] = r["id"] in ext_map
                        r["project"] = (
                            Path(_norm_path(r["cwd"])).name if r["cwd"] else "(フォルダ不明)"
                        )
                    return self._send_json({"results": results, "query": q})
            except ValueError as e:
                return self._send_json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
            except Exception as e:  # noqa: BLE001
                return self._send_json({"error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        # Everything else is a static asset from web/.
        return self._serve_static(path)

    def do_POST(self):
        path = urlparse(self.path).path
        if not path.startswith("/api/"):
            return self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        if not self._authed():
            return self._send_json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
        try:
            data = self._read_json()
            if path == "/api/launch":
                result = launch_session(data.get("name", ""), data.get("dir", ""))
                return self._send_json({"ok": True, **result})
            if path == "/api/rename":
                result = rename_session(data.get("old", ""), data.get("new", ""))
                return self._send_json({"ok": True, **result})
            if path == "/api/rename-conversation":
                result = rename_conversation(data.get("id", ""), data.get("new", ""))
                return self._send_json({"ok": True, **result})
            if path == "/api/kill":
                kill_session(data.get("name", ""))
                return self._send_json({"ok": True})
            if path == "/api/mkdir":
                result = make_dir(data.get("parent", ""), data.get("name", ""))
                return self._send_json({"ok": True, **result})
            if path == "/api/resume":
                result = resume_session(
                    data.get("dir", ""), data.get("id", ""), data.get("name", ""),
                    takeover=bool(data.get("takeover")),
                    force=bool(data.get("force")),
                )
                return self._send_json({"ok": True, **result})
            if path == "/api/migrate":
                pid = data.get("pid")
                if not isinstance(pid, int) or isinstance(pid, bool):
                    raise ValueError("Invalid pid.")
                result = migrate_session(
                    pid, data.get("sid") or "", data.get("name") or ""
                )
                return self._send_json({"ok": True, **result})
            if path == "/api/migrate-all":
                return self._send_json(
                    {"ok": True, **migrate_all_terminal_claudes()}
                )
            if path == "/api/archive":
                archive_conversation(data.get("id", ""))
                return self._send_json({"ok": True})
            if path == "/api/unarchive":
                unarchive_conversation(data.get("id", ""))
                return self._send_json({"ok": True})
            if path == "/api/spawn-servers":
                server = launch_spawn_server(data.get("dir", ""))
                return self._send_json({"ok": True, "server": server})
            return self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except SpawnServerExists as e:
            # Duplicate ignition — 409 with the live server so the UI can show it.
            return self._send_json(
                {"error": str(e), "server": e.server}, HTTPStatus.CONFLICT
            )
        except MaybeLiveError as e:
            # "Might be running" guard — 409 so the UI can confirm-and-force.
            return self._send_json({"error": str(e)}, HTTPStatus.CONFLICT)
        except ValueError as e:
            return self._send_json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
        except Exception as e:  # noqa: BLE001
            return self._send_json({"error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_DELETE(self):
        path = urlparse(self.path).path
        if not path.startswith("/api/"):
            return self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        if not self._authed():
            return self._send_json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
        try:
            if path.startswith("/api/spawn-servers/"):
                name = unquote(path[len("/api/spawn-servers/"):])
                stop_spawn_server(name)
                return self._send_json({"ok": True})
            return self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except ValueError as e:
            return self._send_json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
        except Exception as e:  # noqa: BLE001
            return self._send_json({"error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def log_message(self, fmt, *args):  # quieter, single-line logs
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def main():
    httpd = ThreadingHTTPServer((CONFIG["host"], CONFIG["port"]), Handler)
    url_host = public_url_host()
    print("cc-hub listening on %s:%s" % (CONFIG["host"], CONFIG["port"]))
    print("Open from phone (tailnet):")
    print("  http://%s:%s/?token=%s" % (url_host, CONFIG["port"], CONFIG["token"]))
    print("claude binary: %s" % CONFIG["claude_bin"])
    print("projects root: %s" % CONFIG["projects_root"])
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
        httpd.shutdown()


if __name__ == "__main__":
    main()
