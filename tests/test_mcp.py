import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import claude_blender
from claude_blender import chat, server

PORT = 9879
claude_blender.register()
assert server.SERVER.start(PORT), server.SERVER.last_error

env = dict(os.environ, CLAUDE_BLENDER_PORT=str(PORT), CLAUDE_BLENDER_CLIENT="mcp-test")
uv = shutil.which("uv") or os.path.expanduser("~/.local/bin/uv")
proc = subprocess.Popen([uv, "run", "--quiet", "--script", chat.MCP_SCRIPT],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        env=env, text=True)
lines = queue.Queue()
threading.Thread(target=lambda: [lines.put(ln) for ln in proc.stdout], daemon=True).start()


def rpc(mid, method, params=None, timeout=120):
    msg = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if mid is not None:
        msg["id"] = mid
    proc.stdin.write(json.dumps(msg) + "\n")
    proc.stdin.flush()
    if mid is None:
        return None
    deadline = time.time() + timeout
    while time.time() < deadline:
        server.SERVER._tick()
        try:
            line = lines.get(timeout=0.01)
        except queue.Empty:
            continue
        data = json.loads(line)
        if data.get("id") == mid:
            return data
    raise TimeoutError(method)


checks = []


def check(name, cond, detail=""):
    checks.append((name, bool(cond)))
    print(f"{'PASS' if cond else 'FAIL'}  {name}  {detail}")


init = rpc(1, "initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                             "clientInfo": {"name": "test", "version": "0"}})
check("initialize", "result" in init, init.get("result", {}).get("serverInfo"))
check("instructions", "scene_overview" in init["result"].get("instructions", ""))
rpc(None, "notifications/initialized")

tools = rpc(2, "tools/list")["result"]["tools"]
names = {t["name"] for t in tools}
check("tools/list", len(tools) >= 30, f"{len(tools)} tools")
check("schemas", all(t["inputSchema"]["type"] == "object" for t in tools))

res = rpc(3, "tools/call", {"name": "create_object",
                            "arguments": {"kind": "monkey", "name": "Mon", "material": "gold"}})
body = json.loads(res["result"]["content"][0]["text"])
check("create_object", not res["result"].get("isError") and body["created"][0]["name"] == "Mon")

res = rpc(4, "tools/call", {"name": "look", "arguments": {"views": ["front", "iso"],
                                                          "target": "Mon", "max_size": 256}})
kinds = [c["type"] for c in res["result"]["content"]]
check("look returns image", kinds == ["text", "image"], kinds)

res = rpc(5, "tools/call", {"name": "inspect_object", "arguments": {"name": "Mno"}})
check("error surfaces", res["result"].get("isError") is True,
      res["result"]["content"][0]["text"][:80])

res = rpc(6, "tools/call", {"name": "batch", "arguments": {"calls": [
    {"tool": "lighting_rig", "args": {"preset": "studio_soft"}},
    {"tool": "camera_setup", "args": {"target": "Mon"}},
    {"tool": "look", "args": {"view": "camera", "max_size": 200}}]}})
kinds = [c["type"] for c in res["result"]["content"]]
check("batch images hoisted", kinds == ["text", "image"], kinds)

clients = server.SERVER.client_list()
check("client hello", any(c["name"] == "mcp-test" for c in clients), clients)
check("activity logged", len(server.ACTIVITY.entries) >= 4, len(server.ACTIVITY.entries))

proc.terminate()
server.SERVER.stop()
print(f"\n{sum(ok for _, ok in checks)}/{len(checks)} MCP checks passed")
if proc.stderr:
    err = proc.stderr.read()[-800:]
    if err.strip():
        print("mcp stderr:", err)
