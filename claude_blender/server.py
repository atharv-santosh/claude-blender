import json
import os
import queue
import socket
import threading
import time
import traceback
from collections import deque

import bpy

from . import core

PROTOCOL_VERSION = 1
DEFAULT_PORT = 9877
CALL_TIMEOUT = 900.0
MAX_ACTIVITY = 200
SCHEMA_CACHE = os.path.expanduser("~/.claude_blender/tools.json")


class Job:
    __slots__ = ("tool", "args", "client", "done", "response")

    def __init__(self, tool, args, client):
        self.tool, self.args, self.client = tool, args, client
        self.done = threading.Event()
        self.response = None


class Activity:
    def __init__(self):
        self.entries = deque(maxlen=MAX_ACTIVITY)
        self.version = 0

    def add(self, **entry):
        entry.setdefault("time", time.time())
        self.entries.appendleft(entry)
        self.version += 1

    def clear(self):
        self.entries.clear()
        self.version += 1


ACTIVITY = Activity()


def _summarize_args(args, limit=90):
    parts = []
    for k, v in (args or {}).items():
        if k == "code":
            v = (str(v).strip().splitlines() or [""])[0][:40] + "…"
        text = json.dumps(v, default=str) if not isinstance(v, str) else repr(v)
        if len(text) > 32:
            text = text[:30] + "…"
        parts.append(f"{k}={text}")
    out = ", ".join(parts)
    return out if len(out) <= limit else out[:limit - 1] + "…"


def _summarize_result(result):
    if not isinstance(result, dict):
        return ""
    for key in ("summary", "message"):
        if isinstance(result.get(key), str):
            return result[key][:120]
    for key in ("created", "objects", "modified"):
        val = result.get(key)
        if isinstance(val, list) and val:
            names = [v["name"] if isinstance(v, dict) and "name" in v else str(v) for v in val]
            more = f" +{len(names) - 4}" if len(names) > 4 else ""
            return f"{key}: {', '.join(names[:4])}{more}"
    return ""


def _count_images(result):
    if not isinstance(result, dict):
        return 0
    n = 1 if "image" in result else 0
    return n + len(result.get("images") or [])


def redraw_ui():
    for window in getattr(bpy.context.window_manager, "windows", []):
        for area in window.screen.areas:
            if area.type in {"VIEW_3D", "PROPERTIES", "OUTLINER"}:
                area.tag_redraw()


def run_tool(name, args, client="local", log=True):
    t0 = time.perf_counter()
    tool = core.TOOLS.get(name)
    if tool is None:
        return {"ok": False, "error": f"Unknown tool {name!r}. Known: {sorted(core.TOOLS)}"}
    try:
        result = tool.call(args)
        ok, err = True, None
    except core.ToolError as exc:
        result, ok, err = None, False, str(exc)
    except Exception as exc:
        tb = traceback.format_exc().strip().splitlines()
        result, ok = None, False
        err = f"{type(exc).__name__}: {exc}\n" + "\n".join(tb[-6:])
    ms = (time.perf_counter() - t0) * 1000

    if ok and tool.mutates:
        _undo_push(f"Claude: {name}")
    if log and name not in {"batch"}:
        ACTIVITY.add(tool=name, category=tool.category, args=_summarize_args(args), ok=ok,
                     ms=ms, client=client, error=(err or "")[:300],
                     summary=_summarize_result(result) if ok else "",
                     images=_count_images(result) if ok else 0)
    redraw_ui()
    if ok:
        return {"ok": True, "result": core.jsonable(result) if not _has_image(result) else
                _jsonable_keep_images(result), "ms": round(ms, 1)}
    return {"ok": False, "error": err, "ms": round(ms, 1)}


def _has_image(result):
    return isinstance(result, dict) and ("image" in result or "images" in result)


def _jsonable_keep_images(result):
    out = {}
    for k, v in result.items():
        out[k] = v if k in ("image", "images") else core.jsonable(v)
    return out


def _undo_push(message):
    if bpy.app.background:
        return
    try:
        ctx = core.window_override()
        if ctx:
            with bpy.context.temp_override(**ctx):
                bpy.ops.ed.undo_push(message=message)
        else:
            bpy.ops.ed.undo_push(message=message)
    except Exception as exc:
        print(f"[Claude for Blender] undo push failed: {exc}")


class BlenderToolServer:
    def __init__(self):
        self.port = DEFAULT_PORT
        self.running = False
        self.sock = None
        self.jobs = queue.Queue()
        self.clients = {}
        self._lock = threading.Lock()
        self._next_client = 1
        self.started_at = None
        self.last_error = ""
        self.busy_tool = ""


    def start(self, port=DEFAULT_PORT):
        if self.running:
            return True
        self.port = port
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", port))
            sock.listen(8)
            sock.settimeout(1.0)
        except OSError as exc:
            self.last_error = f"Could not listen on port {port}: {exc}"
            return False
        self.sock = sock
        self.running = True
        self.started_at = time.time()
        self.last_error = ""
        threading.Thread(target=self._accept_loop, daemon=True,
                         name="claude-blender-accept").start()
        if not bpy.app.timers.is_registered(self._tick):
            bpy.app.timers.register(self._tick, first_interval=0.05, persistent=True)
        write_schema_cache()
        return True

    def stop(self):
        self.running = False
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
        with self._lock:
            for info in self.clients.values():
                try:
                    info["conn"].close()
                except OSError:
                    pass
            self.clients.clear()
        while True:
            try:
                job = self.jobs.get_nowait()
            except queue.Empty:
                break
            job.response = {"ok": False, "error": "Server stopped"}
            job.done.set()
        if bpy.app.timers.is_registered(self._tick):
            bpy.app.timers.unregister(self._tick)


    def _accept_loop(self):
        while self.running and self.sock is not None:
            try:
                conn, addr = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self._lock:
                cid = self._next_client
                self._next_client += 1
                self.clients[cid] = {"name": "client", "addr": f"{addr[0]}:{addr[1]}",
                                     "since": time.time(), "calls": 0, "conn": conn}
            threading.Thread(target=self._client_loop, args=(cid, conn), daemon=True,
                             name=f"claude-blender-client-{cid}").start()

    def _client_loop(self, cid, conn):
        conn.settimeout(None)
        buf = b""
        try:
            while self.running:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if line.strip():
                        self._send(conn, self._handle_line(cid, line))
        except OSError:
            pass
        finally:
            with self._lock:
                self.clients.pop(cid, None)
            try:
                conn.close()
            except OSError:
                pass

    @staticmethod
    def _send(conn, payload):
        conn.sendall(json.dumps(payload, default=str).encode("utf-8") + b"\n")

    def _handle_line(self, cid, line):
        try:
            req = json.loads(line)
        except json.JSONDecodeError as exc:
            return {"id": None, "ok": False, "error": f"Bad JSON: {exc}"}
        rid, name, args = req.get("id"), req.get("tool"), req.get("args") or {}

        if name == "__hello__":
            with self._lock:
                if cid in self.clients:
                    self.clients[cid]["name"] = str(args.get("client", "client"))[:40]
            return {"id": rid, "ok": True, "result": self.hello_info()}
        if name == "__list_tools__":
            return {"id": rid, "ok": True,
                    "result": [t.schema() for t in core.TOOLS.values()]}

        with self._lock:
            info = self.clients.get(cid)
            if info:
                info["calls"] += 1
            client_name = info["name"] if info else "client"
        job = Job(name, args, client_name)
        self.jobs.put(job)
        if not job.done.wait(CALL_TIMEOUT):
            return {"id": rid, "ok": False,
                    "error": f"Timed out after {CALL_TIMEOUT:.0f}s waiting for Blender"}
        resp = dict(job.response)
        resp["id"] = rid
        return resp

    def hello_info(self):
        return {"protocol": PROTOCOL_VERSION, "blender": bpy.app.version_string,
                "tools": len(core.TOOLS),
                "file": bpy.data.filepath or "(unsaved)"}


    def _tick(self):
        if not self.running:
            return None
        deadline = time.perf_counter() + 0.25
        while time.perf_counter() < deadline:
            try:
                job = self.jobs.get_nowait()
            except queue.Empty:
                break
            self.busy_tool = job.tool
            try:
                job.response = run_tool(job.tool, job.args, client=job.client)
            except Exception as exc:
                job.response = {"ok": False, "error": f"Internal error: {exc}"}
            finally:
                self.busy_tool = ""
                job.done.set()
        return 0.05

    def client_list(self):
        with self._lock:
            return [{k: v for k, v in c.items() if k != "conn"} for c in self.clients.values()]


SERVER = BlenderToolServer()


def write_schema_cache():
    try:
        os.makedirs(os.path.dirname(SCHEMA_CACHE), exist_ok=True)
        with open(SCHEMA_CACHE, "w", encoding="utf-8") as fh:
            json.dump({"protocol": PROTOCOL_VERSION,
                       "tools": [t.schema() for t in core.TOOLS.values()]}, fh, indent=1)
    except OSError as exc:
        print(f"[Claude for Blender] could not write schema cache: {exc}")
