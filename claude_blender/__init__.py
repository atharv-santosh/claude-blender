bl_info = {
    "name": "Claude for Blender",
    "author": "atharv-santosh",
    "version": (1, 0, 0),
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar (N) > Claude",
    "description": "MCP server and chat panel for working with Claude in Blender",
    "category": "Interface",
}

import bpy
from bpy.app.handlers import persistent

from . import server
from . import tools_scene, tools_build, tools_look, tools_anim, tools_procedural, tools_meta
from . import chat, ui


def _autostart():
    p = ui.prefs()
    if p is None or p.autostart:
        if not server.SERVER.start(p.port if p else server.DEFAULT_PORT):
            print(f"[Claude for Blender] {server.SERVER.last_error}")
    return None


@persistent
def _on_load(_dummy=None):
    for scene in bpy.data.scenes:
        st = getattr(scene, "claude_chat", None)
        if st is not None and st.busy and not chat.RUN.busy:
            st.busy = False
    if not bpy.app.timers.is_registered(chat.pump):
        bpy.app.timers.register(chat.pump, first_interval=0.5, persistent=True)


def register():
    ui.register()
    server.write_schema_cache()
    bpy.app.handlers.load_post.append(_on_load)
    if not bpy.app.background:
        bpy.app.timers.register(_autostart, first_interval=0.5)
        bpy.app.timers.register(chat.pump, first_interval=1.0, persistent=True)


def unregister():
    chat.cancel()
    server.SERVER.stop()
    for fn in (chat.pump, _autostart):
        if bpy.app.timers.is_registered(fn):
            bpy.app.timers.unregister(fn)
    if _on_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load)
    ui.unregister()
