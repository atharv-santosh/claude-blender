# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.9,<2"]
# ///

import asyncio
import json
import os
import socket
import threading

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

HOST = "127.0.0.1"
PORT = int(os.environ.get("CLAUDE_BLENDER_PORT", "9877"))
CLIENT = os.environ.get("CLAUDE_BLENDER_CLIENT", "claude-code")
SCHEMA_CACHE = os.path.expanduser("~/.claude_blender/tools.json")
CALL_TIMEOUT = 900

INSTRUCTIONS = """Claude for Blender drives the user's live Blender scene.

Workflow that works:
1. scene_overview first (then inspect_object for anything you will edit). Never guess names.
2. Build with the high-level tools: generate (terrain, buildings, city, trees, rocks, rooms,
   furniture, stairs, fences), create_object, create_material (procedural PBR presets),
   lighting_rig, set_world, camera_setup, scatter_on_surface, arrange_objects, animate.
   Use `batch` to run many calls in one round trip and one undo step.
3. Look at your work: look(views=["front","right","top","iso"]) gives a contact sheet in one
   image; look(view="camera", shading="rendered") previews the final shot; render_image for
   the real thing. Use measure to confirm contact/gaps instead of eyeballing.
4. Fix and verify: analyze_mesh audits topology and tells you which edit_mesh op fixes each
   issue. Every mutating call is one undo step (undo tool, or the user's Ctrl+Z).
5. focus_view to show the user what you made.

Object arguments accept names, lists, globs ('Tree*'), 're:<regex>', 'selected', 'active',
'all', 'type:MESH', 'collection:<name>'. Colors accept hex, names, [r,g,b] or '3200K' for lights.
Sizes are meters; rotations are degrees. execute_python is the escape hatch for anything else."""


class BlenderLink:
    def __init__(self):
        self.sock = None
        self.buf = b""
        self.lock = threading.Lock()
        self.next_id = 1

    def _connect(self):
        s = socket.create_connection((HOST, PORT), timeout=5)
        s.settimeout(CALL_TIMEOUT)
        self.sock, self.buf = s, b""
        self._roundtrip("__hello__", {"client": CLIENT})

    def _roundtrip(self, tool, args):
        rid = self.next_id
        self.next_id += 1
        self.sock.sendall(json.dumps({"id": rid, "tool": tool, "args": args}).encode() + b"\n")
        while b"\n" not in self.buf:
            chunk = self.sock.recv(1 << 20)
            if not chunk:
                raise ConnectionError("Blender closed the connection")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return json.loads(line)

    def call(self, tool, args):
        with self.lock:
            for attempt in (1, 2):
                try:
                    if self.sock is None:
                        self._connect()
                    return self._roundtrip(tool, args)
                except (OSError, ConnectionError, json.JSONDecodeError) as exc:
                    self.close()
                    if attempt == 2:
                        return {"ok": False, "error": (
                            f"Can't reach Blender on {HOST}:{PORT} ({exc}). Make sure Blender "
                            "is open with the 'Claude for Blender' addon enabled; its server "
                            "starts automatically (N-panel > Claude shows the status).")}
        return {"ok": False, "error": "unreachable"}

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None


LINK = BlenderLink()
server = Server("claude-blender", instructions=INSTRUCTIONS)


def _load_schemas():
    resp = LINK.call("__list_tools__", {})
    if resp.get("ok"):
        return resp["result"]
    try:
        with open(SCHEMA_CACHE, encoding="utf-8") as fh:
            return json.load(fh)["tools"]
    except (OSError, ValueError, KeyError):
        return []


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    schemas = await asyncio.to_thread(_load_schemas)
    tools = []
    for s in schemas:
        ann = s.get("annotations") or {}
        tools.append(types.Tool(
            name=s["name"],
            description=s.get("description", ""),
            inputSchema=s.get("inputSchema") or {"type": "object", "properties": {}},
            annotations=types.ToolAnnotations(
                title=s["name"].replace("_", " ").title(),
                readOnlyHint=ann.get("readOnlyHint"),
                destructiveHint=ann.get("destructiveHint"),
            ),
        ))
    return tools


def _collect_images(result, out):
    if isinstance(result, dict):
        img = result.pop("image", None)
        if isinstance(img, dict) and img.get("base64"):
            out.append(types.ImageContent(type="image", data=img["base64"],
                                          mimeType=img.get("mime", "image/png")))
        imgs = result.pop("images", None)
        for img in imgs or []:
            if isinstance(img, dict) and img.get("base64"):
                out.append(types.ImageContent(type="image", data=img["base64"],
                                              mimeType=img.get("mime", "image/png")))
        for v in result.values():
            if isinstance(v, (dict, list)):
                _collect_images(v, out)
    elif isinstance(result, list):
        for v in result:
            _collect_images(v, out)


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list:
    resp = await asyncio.to_thread(LINK.call, name, arguments or {})
    if not resp.get("ok"):
        raise RuntimeError(resp.get("error", "Tool failed"))
    result = resp.get("result")
    images = []
    _collect_images(result, images)
    text = json.dumps(result, separators=(",", ":"), default=str)
    return [types.TextContent(type="text", text=text), *images]


async def main():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
