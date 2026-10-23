import json
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time

import bpy

from . import server

_EXTRA_PATHS = ("~/.local/bin", "~/.claude/local", "~/.cargo/bin", "/opt/homebrew/bin",
                "/opt/homebrew/sbin", "/usr/local/bin")

MCP_SERVER_NAME = "claude-blender"
MCP_SCRIPT = os.path.join(os.path.dirname(os.path.realpath(__file__)), "mcp_server.py")

PERMISSION_MODES = [
    ("BLENDER", "Blender tools", "Claude can use this addon's Blender tools (and read the "
     "screenshot) but nothing else: no shell, no file writes", "SCENE_DATA", 0),
    ("READ", "Look only", "Claude can inspect the scene and look at it, but cannot change "
     "anything", "HIDE_OFF", 1),
    ("FULL", "Full access", "Claude's entire toolset with no prompts -- shell, files, network "
     "-- across your machine. Use with care", "ERROR", 2),
]

READ_ONLY_TOOLS = ("scene_overview", "inspect_object", "query_objects", "look", "measure",
                   "raycast", "analyze_mesh", "render_image")

SYSTEM_PROMPT = """You are Claude, chatting from a sidebar panel inside the user's running \
Blender. The `{server}` MCP server is connected to THIS Blender instance: its tools act on \
the scene the user is looking at, and every mutating call is one Ctrl+Z step for them.

Work like a careful 3D artist:
- Ground yourself first (scene_overview, inspect_object) instead of guessing names.
- Prefer the high-level tools (generate, create_object, create_material, lighting_rig, \
camera_setup, scatter_on_surface, animate) and `batch` for multi-step builds; drop to \
execute_python only for what they can't do.
- Verify visually with `look` (contact sheets via views=[...]) and spatially with `measure` \
before claiming something is done or touching.
- When you finish, call focus_view to show the user the result.
Replies render in a narrow sidebar: be brief, plain text, short lines, no tables."""


def child_env():
    env = os.environ.copy()
    parts = [p for p in (os.path.expanduser(x) for x in _EXTRA_PATHS) if os.path.isdir(p)]
    for p in env.get("PATH", "").split(os.pathsep):
        if p and p not in parts:
            parts.append(p)
    for p in ("/usr/bin", "/bin", "/usr/sbin", "/sbin"):
        if p not in parts:
            parts.append(p)
    env["PATH"] = os.pathsep.join(parts)
    env["CLAUDE_BLENDER_PORT"] = str(server.SERVER.port)
    return env


def claude_binary(override=""):
    if override and os.path.isfile(os.path.expanduser(override)):
        return os.path.expanduser(override)
    found = shutil.which("claude", path=child_env()["PATH"])
    if found:
        return found
    for cand in ("~/.local/bin/claude", "~/.claude/local/claude", "/opt/homebrew/bin/claude",
                 "/usr/local/bin/claude"):
        p = os.path.expanduser(cand)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def mcp_config_path():
    uv = shutil.which("uv", path=child_env()["PATH"]) or "uv"
    cfg = {"mcpServers": {MCP_SERVER_NAME: {
        "command": uv, "args": ["run", "--quiet", "--script", MCP_SCRIPT],
        "env": {"CLAUDE_BLENDER_PORT": str(server.SERVER.port),
                "CLAUDE_BLENDER_CLIENT": "blender-chat"}}}}
    path = os.path.join(tempfile.gettempdir(), "claude_blender_mcp.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh)
    return path


class ChatRun:
    def __init__(self):
        self.events = queue.Queue()
        self.proc = None
        self.thread = None
        self.started = 0.0
        self.live_text = ""
        self.current_tool = ""
        self.tool_count = 0
        self.cancelled = False

    @property
    def busy(self):
        return self.thread is not None and self.thread.is_alive()

    def elapsed(self):
        return time.time() - self.started if self.started else 0.0


RUN = ChatRun()


def _tool_label(name):
    return name.split("__")[-1] if name.startswith("mcp__") else name


def _summarize_input(name, inp):
    if not isinstance(inp, dict):
        return ""
    short = _tool_label(name)
    for key in ("kind", "preset", "view", "views", "action", "op", "objects", "name",
                "target", "material", "command", "file_path", "pattern"):
        if key in inp:
            v = inp[key]
            if isinstance(v, list):
                v = ",".join(map(str, v[:4]))
            return f"{key}={str(v)[:40]}"
    if short == "batch" and isinstance(inp.get("calls"), list):
        return f"{len(inp['calls'])} calls"
    return ""


def _worker(args, cwd, env):
    ev = RUN.events
    try:
        proc = subprocess.Popen(args, cwd=cwd, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                errors="replace", bufsize=1)
    except OSError as exc:
        ev.put(("error", f"Could not start claude: {exc}"))
        ev.put(("finished", None))
        return
    RUN.proc = proc
    session_id, saw_delta, final = None, False, None
    stderr_tail = []

    def drain_stderr():
        for line in proc.stderr:
            stderr_tail.append(line)
            del stderr_tail[:-40]
    threading.Thread(target=drain_stderr, daemon=True).start()

    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        mtype = msg.get("type")
        if mtype == "system" and msg.get("subtype") == "init":
            session_id = msg.get("session_id") or session_id
            ev.put(("session", session_id))
        elif mtype == "stream_event":
            e = msg.get("event") or {}
            if e.get("type") == "content_block_delta":
                d = e.get("delta") or {}
                if d.get("type") == "text_delta":
                    saw_delta = True
                    ev.put(("delta", d.get("text", "")))
            elif e.get("type") == "message_start":
                saw_delta = False
        elif mtype == "assistant":
            for block in (msg.get("message") or {}).get("content") or []:
                if block.get("type") == "text" and not saw_delta:
                    ev.put(("delta", block.get("text", "")))
                elif block.get("type") == "tool_use":
                    ev.put(("tool", block.get("name", "?"),
                            _summarize_input(block.get("name", ""), block.get("input"))))
            ev.put(("turn_end", None))
            saw_delta = False
        elif mtype == "user":
            for block in (msg.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    ev.put(("tool_result", not block.get("is_error", False)))
        elif mtype == "result":
            final = msg
    proc.wait()
    if RUN.cancelled:
        ev.put(("error", "Cancelled."))
    elif final is not None:
        if final.get("is_error") or final.get("subtype", "success") != "success":
            ev.put(("error", str(final.get("result") or final.get("subtype") or "Claude failed")))
        ev.put(("done", {"session_id": final.get("session_id") or session_id,
                         "cost": final.get("total_cost_usd") or 0.0,
                         "turns": final.get("num_turns") or 0,
                         "ms": final.get("duration_ms") or 0}))
    else:
        detail = "".join(stderr_tail).strip()[-1200:]
        if "resume" in detail.lower() or "session" in detail.lower():
            ev.put(("stale_session", detail))
        else:
            ev.put(("error", f"claude exited {proc.returncode}: {detail or '(no output)'}"))
    ev.put(("finished", None))


def build_args(binary, prompt, model, session_id, mode, effort=""):
    args = [binary, "-p", prompt, "--output-format", "stream-json", "--verbose",
            "--include-partial-messages", "--model", model,
            "--append-system-prompt", SYSTEM_PROMPT.format(server=MCP_SERVER_NAME),
            "--mcp-config", mcp_config_path()]
    if session_id:
        args += ["--resume", session_id]
    if effort:
        args += ["--effort", effort]
    if mode == "FULL":
        args += ["--permission-mode", "bypassPermissions"]
    elif mode == "READ":
        allowed = [f"mcp__{MCP_SERVER_NAME}__{t}" for t in READ_ONLY_TOOLS] + ["Read"]
        args += ["--strict-mcp-config", "--allowedTools", ",".join(allowed)]
    else:
        args += ["--strict-mcp-config", "--allowedTools", f"mcp__{MCP_SERVER_NAME},Read"]
    return args


def start(prompt, model, session_id, mode, cwd, binary, effort=""):
    if RUN.busy:
        return False
    if not server.SERVER.running:
        server.SERVER.start(server.SERVER.port)
    RUN.__init__()
    RUN.started = time.time()
    args = build_args(binary, prompt, model, session_id, mode, effort)
    RUN.thread = threading.Thread(target=_worker, args=(args, cwd, child_env()), daemon=True,
                                  name="claude-blender-chat")
    RUN.thread.start()
    return True


def cancel():
    RUN.cancelled = True
    proc = RUN.proc
    if proc and proc.poll() is None:
        try:
            proc.terminate()
        except OSError:
            pass


def _scene():
    return bpy.context.scene


def _add_message(scene, role, text, detail=""):
    msg = scene.claude_chat.messages.add()
    msg.role, msg.text, msg.detail = role, text, detail
    msg.timestamp = time.strftime("%H:%M")
    return msg


def pump():
    scene = _scene()
    if scene is None:
        return 0.3
    chat = scene.claude_chat
    changed = False
    while True:
        try:
            ev = RUN.events.get_nowait()
        except queue.Empty:
            break
        changed = True
        kind = ev[0]
        if kind == "session" and ev[1]:
            chat.session_id = ev[1]
        elif kind == "delta":
            RUN.live_text += ev[1]
        elif kind == "turn_end":
            if RUN.live_text.strip():
                _add_message(scene, "assistant", RUN.live_text.strip())
            RUN.live_text = ""
        elif kind == "tool":
            RUN.current_tool = _tool_label(ev[1])
            RUN.tool_count += 1
            _add_message(scene, "tool", RUN.current_tool, ev[2])
        elif kind == "tool_result":
            for m in reversed(chat.messages):
                if m.role == "tool":
                    m.failed = not ev[1]
                    break
        elif kind == "done":
            info = ev[1]
            if info.get("session_id"):
                chat.session_id = info["session_id"]
            chat.total_cost += float(info.get("cost") or 0.0)
            chat.last_stats = (f"{info.get('turns', 0)} turns · "
                               f"{(info.get('ms') or 0) / 1000:.0f}s · "
                               f"${float(info.get('cost') or 0):.3f}")
        elif kind == "stale_session":
            chat.session_id = ""
            _add_message(scene, "error", "That session could not be resumed; your next "
                                         "message starts a new one.", ev[1][:400])
        elif kind == "error":
            if RUN.live_text.strip():
                _add_message(scene, "assistant", RUN.live_text.strip())
                RUN.live_text = ""
            _add_message(scene, "error", ev[1][:1500])
        elif kind == "finished":
            if RUN.live_text.strip():
                _add_message(scene, "assistant", RUN.live_text.strip())
            RUN.live_text = ""
            RUN.current_tool = ""
            chat.busy = False
    if changed or chat.busy:
        server.redraw_ui()
    return 0.15 if chat.busy else 0.4
