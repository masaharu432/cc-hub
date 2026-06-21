**English** | [日本語](README.ja.md)

# cc-hub

A small web app that runs on your own machine (Linux / WSL2, etc.).
Its only job is to **launch and re-ignite Claude Code sessions with Remote Control enabled** — from your phone.

It does not handle the conversation itself. You hold the conversation in the **official Claude mobile app**.
cc-hub is purely the *ignition key*: it lets you take care, remotely from your phone, of the "little bit of local-side work" that this requires.

<p align="center">
  <img src="docs/images/projects.png" alt="cc-hub on a phone screen" width="320">
</p>

---

## Why you need it

In the Claude mobile app, pick the **Code tab** and you get a session list, just like a regular chat.
What's listed here are **sessions that have Remote Control (RC) enabled on the Claude Code side**.
Pick one and you can connect from your phone to the Claude Code running on your own machine and keep the conversation going (voice input works too).
It's wonderfully convenient — but there's a wall: **to start using RC, and to keep using it, you ultimately need to do something on the local machine.**

cc-hub lets you do that "local-side work" remotely, from your phone.

- **You want to launch a new session with RC enabled.** To connect via RC, you first have to start Claude Code
  locally with Remote Control enabled. Doing that while you're away from your desk is exactly what cc-hub is for — its main purpose.

- **You want to start from a new folder.** "Create a new project here, then launch Claude with that as its base" —
  cc-hub lets you do all of it from your phone (with folder browsing and creation).

- **You want to revive a dropped session.** After a while, RC sessions **stop being reachable** from the official app.
  The process is still alive, it still appears in the official app's list, and you can still see the past exchanges —
  yet **you can't continue the conversation from there.** You have to re-activate RC on the local machine once more.
  cc-hub's "Resume" button does exactly that, remotely. Press it and RC becomes enabled again, so you can chat from the official app once more.

---

## Division of labor with the official app (Remote Control)

**All conversation and session interaction happens in the official Claude mobile app.**

1. Launch a session in cc-hub (or resume a dropped one).
2. In the official app, pick the **Code tab** and you get a session list, just like a regular chat.
   The session you just RC-enabled appears in that list, so tap it to open it.
3. From there, continue the conversation inside the official app. **Voice input works too.**

All cc-hub does is **step 1 — the "ignition / re-ignition."** The official app handles conversation, history, and interaction.
cc-hub never calls Anthropic's API/SDK and never touches Claude's protocol
(you stay within your subscription's conversation allowance — there are no API charges).

### How it differs from the official app's Spawn server

The official app has a mechanism that attempts something similar (the Spawn server), but its behavior is unstable at present.
The Spawn server also requires you to **stand up one instance per project (folder)**.
In other words, with the Spawn server mechanism you **cannot start a brand-new session remotely in a project folder where you haven't already stood up a server**
(each time, you have to wake a server locally first).

cc-hub also requires you to keep one web server running, but **that single one handles all of your projects**,
and it can launch and manage sessions on the spot — including sessions in brand-new folders.

---

## How it differs from existing remote-control tools (filling only the gaps)

There are already OSS tools that let you operate a Claude Code terminal remotely, as well as tools that — like the official app —
provide both session management and chat in a single UI.

But now that the **official app's Remote Control feature has shipped**, the official UI handles chat and history perfectly well.
So cc-hub **does not rebuild that part.** On the other hand, **session management is absent from the official app**, and that's
where cc-hub steps in. The "local-side ignition / re-ignition" and session management that official RC alone can't reach —
launching new sessions with RC enabled, launching from a new folder, re-activating dropped sessions, listing, renaming, and ending —
cc-hub is designed to fill **only those gaps**.

---

## Security — Tailscale is a complete prerequisite

**cc-hub's security depends on network reachability itself.**
The app does have an authentication token, but the **first line of defense is "the server machine simply isn't reachable in the first place."**

- The server runs on your own machine, and cc-hub **completely assumes** that your phone can reach it
  **only across a [Tailscale](https://tailscale.com/) tailnet**. With Tailscale, a closed path forms between just your own devices,
  unreachable from the outside. cc-hub is built to be used on top of this "closed path."
- **Do not expose it directly to the internet.** Opening the port globally means exposing to the world an entry point
  that can launch Claude Code from any folder on your machine.
- It might technically run in a public setup (reverse proxy plus authentication, etc.), but **such configurations are
  neither tested nor guaranteed.** That's at your own risk. Using it across Tailscale is strongly recommended.

> On Android, if you run it alongside an always-on ad-blocking VPN (such as AdGuard), an OS constraint means
> **only one VPN can be active per profile.** Splitting off a Work Profile lets you have both — ad blocking on the personal
> profile and Tailscale on the work profile.

---

## Usage

### 1. Start the server

```bash
./run.sh          # = python3 server.py
```

On startup, it prints the URL to open on your phone (a QR code also appears on the PC screen):

```
http://<tailnet-ip>:8765/?token=XXXXXXXX
```

Open this URL on your phone and the token is saved on the device; from then on you can open it without the token.
Open it on a PC and an "Open on phone" QR code appears in the top right. Scan it with your phone's camera and you're in.

The cc-hub screen consists of **three tabs**. Use them depending on what you want to do.

### 2. "Projects" tab (leftmost)

<p align="center">
  <img src="docs/images/projects.png" alt="'Projects' tab: launching and the session list" width="320">
</p>

Here you **launch, re-ignite, rename, and archive** sessions.

**Launch (ignition)**

1. **Specify the folder to launch in** — type the path directly, navigate via "Browse," or create a new one with `＋ New folder`.
   For a project you've launched before, the **"＋" button** on each folder row in the list auto-fills the path.
2. **Enter a session name** — it appears in the official app's Remote Control list in the form `folderName_thisName`.
3. Press **Launch.**
   → The session then appears in the official app's **Code tab**, so tap it to start the conversation.

**Re-ignition (restarting a dropped session)**

For a session that has stopped being reachable from the official app, the **Resume button** on each row (↻ / "To tmux (end and resume)") brings it back.
Press it and RC becomes enabled again, so you can chat from the official app once more.
Claude instances scattered across terminals and elsewhere can be pulled in all at once with **"All to tmux"** in the header.

**Rename**

**Swipe the target row right** and a "Rename" button appears (`folderName_` is fixed; edit only what follows).
The changed name is **reflected as-is in the official app's Remote Control list** too.

**Archive**

**Swipe the target row left** and an "Archive" button appears ("Stop and archive" while running).
Stash sessions you won't use for a while to keep the list tidy.

This **only hides it from the list in cc-hub's "Projects" tab**, and is **distinct from the official app's archive**
(it just toggles the display state on the cc-hub side). Even once archived, you can bring it back into this "Projects" tab from the
**"Archive" tab** described next.

### 3. "Search" tab

<p align="center">
  <img src="docs/images/search.png" alt="Session search" width="300">
</p>

You can search past sessions **by the messages you typed and by session name** (filterable by project).
It's the tab for quickly pinning down "wait, which session was that again?"

Press the **Restart button** in a search result and you can re-ignite that session right there. After that, continue the conversation in the official app's **Code tab**.

### 4. "Archive" tab

<p align="center">
  <img src="docs/images/archive.png" alt="Archive" width="300">
</p>

A list of the sessions you've archived. Use **"Resume"** on each row (or swipe right) to restart it at any time and
send it back to the "Projects" list.

> The screenshot above has its content mosaiced so the session contents aren't visible.

### 5. "Spawn" tab (β · not recommended)

At the very end of the nav there's also a tab for starting the official **Spawn server**.
For any project folder, it brings up `claude remote-control --spawn=same-dir --no-create-session-in-dir`
on tmux (an empty resident server based at the specified folder).
However, **because the official app's behavior is currently unstable, it's β and not recommended** (for Claude in a terminal,
pulling it in with **"All to tmux"** on the "Projects" tab is more stable).

In the future, if the official Spawn server feature matures, **cc-hub may get by with just this Spawn-launch function.**

---

## Requirements

- `tmux` / the `claude` CLI / `python3` (no node/npm needed)
- No extra libraries needed (Python standard library only; the front-end Bootstrap is bundled and offline-self-contained)
- **Tailscale** — it's assumed your phone reaches the server machine across a tailnet (see "Security")
- (Optional) `reptyr` — used by the feature that moves a running session under tmux management while keeping it alive.
  The app works even if it's not set up.

### config.json (auto-generated on first launch)

```json
{
  "host": "0.0.0.0",
  "port": 8765,
  "projects_root": "/path/to/projects",
  "claude_bin": "/home/you/.local/bin/claude",
  "public_host": "<your tailnet IP>",
  "token": "(auto-generated)"
}
```

- `public_host`: the host your phone reaches (your tailnet IP, etc.), shown on the QR code / startup banner.
- `projects_root`: the initial directory for launch-folder suggestions and browsing.
- `token`: auto-generated. `config.json` contains secrets, so it's outside git management.
