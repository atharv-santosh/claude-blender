import base64
import json
import os
import subprocess
import time

import bpy
from bpy.props import (BoolProperty, CollectionProperty, EnumProperty, FloatProperty,
                       IntProperty, PointerProperty, StringProperty)

from . import chat, core, server

ADDON_ID = __package__

CATEGORY_ICONS = {"Inspect": "VIEWZOOM", "Build": "MESH_CUBE", "Look": "SHADING_RENDERED",
                  "Animate": "ANIM", "Render": "RENDER_STILL", "Advanced": "CONSOLE"}

QUICK_PROMPTS = [
    ("Describe this scene", "Describe what's in this scene and what state it is in.",
     "VIEWZOOM"),
    ("Critique the look", "Look at the camera view rendered and give me the three changes that "
     "would most improve it, then make them.", "SHADING_RENDERED"),
    ("Light it beautifully", "Choose and build lighting that flatters the main subject, then "
     "show me through the camera.", "LIGHT"),
    ("Hero shot", "Set up a strong hero camera shot of the main subject, with depth of field, "
     "and show it.", "CAMERA_DATA"),
    ("Realistic materials", "Give every object without a proper material a fitting, "
     "realistic material.", "MATERIAL"),
    ("Audit topology", "Audit the selected meshes and fix any real problems you find.",
     "MODIFIER"),
    ("Make it render-ready", "Get this scene render-ready: camera, lighting, world, materials "
     "and render settings. Render a preview.", "RENDER_STILL"),
    ("Turntable", "Animate a 120-frame turntable of the selected object with a camera that "
     "frames it.", "ANIM"),
]


def prefs(context=None):
    context = context or bpy.context
    addon = context.preferences.addons.get(ADDON_ID)
    return addon.preferences if addon else None


class ClaudeMessage(bpy.types.PropertyGroup):
    role: StringProperty(default="user")
    text: StringProperty(default="")
    detail: StringProperty(default="")
    timestamp: StringProperty(default="")
    failed: BoolProperty(default=False)


MODELS = [("sonnet", "Sonnet", "Fast and capable"), ("opus", "Opus", "Most capable"),
          ("haiku", "Haiku", "Fastest")]
EFFORTS = [("", "Default", "Model default"), ("low", "Low", ""), ("medium", "Medium", ""),
           ("high", "High", "")]


def _light_items(self, context):
    from .tools_look import LIGHT_RIGS
    return [(k, k.replace("_", " ").title(), "") for k in sorted(LIGHT_RIGS)]


def _material_items(self, context):
    from .tools_look import PRESETS
    return [(k, k.replace("_", " ").title(), desc) for k, (_, desc) in sorted(PRESETS.items())]


def _generate_items(self, context):
    from .tools_procedural import KINDS
    return [(k, k.title(), "") for k in KINDS]


class ClaudeChatState(bpy.types.PropertyGroup):
    messages: CollectionProperty(type=ClaudeMessage)
    prompt: StringProperty(name="Prompt", description="Ask Claude (Enter sends)",
                           update=lambda self, ctx: _on_prompt_confirm(self, ctx))
    busy: BoolProperty(default=False)
    session_id: StringProperty(default="")
    total_cost: FloatProperty(default=0.0)
    last_stats: StringProperty(default="")
    model: EnumProperty(name="Model", items=MODELS, default="sonnet")
    effort: EnumProperty(name="Effort", items=EFFORTS, default="")
    mode: EnumProperty(name="Access", items=chat.PERMISSION_MODES, default="BLENDER")
    attach_screenshot: BoolProperty(name="Attach view", default=True,
                                    description="Send a render of your current viewport")
    include_scene: BoolProperty(name="Scene context", default=True,
                                description="Send a compact description of the scene")
    show_tool_steps: BoolProperty(name="Show tool steps", default=True,
                                  description="List each tool call under the reply")
    visible_exchanges: IntProperty(default=4, min=1)


class ClaudeQuickTools(bpy.types.PropertyGroup):
    light_preset: EnumProperty(name="Rig", items=_light_items)
    material_preset: EnumProperty(name="Preset", items=_material_items)
    material_color: bpy.props.FloatVectorProperty(name="Color", subtype="COLOR", size=3,
                                                  min=0, max=1, default=(0.8, 0.2, 0.15))
    use_color: BoolProperty(name="Tint", default=False)
    generate_kind: EnumProperty(name="Generate", items=_generate_items)
    aspect: EnumProperty(name="Aspect", items=[(a, a, "") for a in
                                               ("16:9", "4:5", "1:1", "9:16", "21:9", "3:2")])


_WRAP_CACHE = {}


def _ui_fac(context):
    return context.preferences.system.ui_scale


def _font_px(context):
    style = context.preferences.ui_styles[0]
    pts = getattr(style.widget, "points", 11)
    return pts * _ui_fac(context)


def wrap_px(context, text, width_px):
    key = (text, int(width_px), round(_font_px(context), 2))
    hit = _WRAP_CACHE.get(key)
    if hit is not None:
        return hit
    import blf
    blf.size(0, _font_px(context))
    out = []
    for para in text.split("\n"):
        if not para.strip():
            out.append("")
            continue
        line = ""
        for word in para.split(" "):
            trial = f"{line} {word}" if line else word
            if blf.dimensions(0, trial)[0] <= width_px or not line:
                while blf.dimensions(0, trial)[0] > width_px and len(trial) > 8:
                    cut = len(trial)
                    while cut > 1 and blf.dimensions(0, trial[:cut])[0] > width_px:
                        cut -= max(1, cut // 8)
                    out.append(trial[:cut])
                    trial = trial[cut:]
                line = trial
            else:
                out.append(line)
                line = word
        out.append(line)
    if len(_WRAP_CACHE) > 4000:
        _WRAP_CACHE.clear()
    _WRAP_CACHE[key] = out
    return out


def _clean_inline(s):
    return s.replace("**", "").replace("__", "").replace("`", "")


def draw_markdown(layout, context, text, width_px):
    col = layout.column(align=True)
    col.scale_y = 0.85
    in_code, code_col = False, None
    for raw in text.split("\n"):
        line = raw.rstrip()
        if line.strip().startswith("```"):
            in_code = not in_code
            if in_code:
                code_col = col.box().column(align=True)
                code_col.scale_y = 0.8
            continue
        if in_code:
            for w in wrap_px(context, line or " ", width_px - 30):
                code_col.label(text=w)
            continue
        stripped = line.lstrip()
        if not stripped:
            col.separator(factor=0.8)
            continue
        if stripped.startswith("#"):
            heading = _clean_inline(stripped.lstrip("#").strip())
            for i, w in enumerate(wrap_px(context, heading, width_px - 24)):
                col.label(text=w, icon="DISCLOSURE_TRI_RIGHT" if i == 0 else "BLANK1")
            continue
        bullet = None
        if stripped[:2] in ("- ", "* ", "• "):
            bullet, body = "•", stripped[2:]
        elif stripped.split(" ", 1)[0].rstrip(".").isdigit() and ". " in stripped[:4]:
            num, body = stripped.split(" ", 1)
            bullet = num
        else:
            body = stripped
        body = _clean_inline(body)
        if bullet:
            lines = wrap_px(context, body, width_px - 26)
            for i, w in enumerate(lines):
                col.label(text=(f"{bullet} " if i == 0 else "   ") + w)
        else:
            for w in wrap_px(context, body, width_px):
                col.label(text=w)


def _text_width(context, inset=0):
    return max(80, (context.region.width - (46 + inset) * _ui_fac(context)) * 0.94)


def send_prompt(context, text):
    st = context.scene.claude_chat
    if st.busy:
        return "Claude is still working on the last message"
    text = (text or "").strip()
    source_text = bpy.data.texts.get("Claude Prompt")
    if not text and source_text and source_text.as_string().strip():
        text = source_text.as_string().strip()
        source_text.clear()
    if not text:
        return "Type a prompt first (or use Compose for long ones)"
    p = prefs(context)
    binary = chat.claude_binary(p.claude_path if p else "")
    if binary is None:
        return "claude CLI not found. Set its path in the addon preferences."

    full = text
    if st.include_scene:
        full += "\n\n<blender_context>\n" + _scene_context() + "\n</blender_context>"
    if st.attach_screenshot:
        shot = _capture_view()
        if shot:
            full += (f"\n\nA render of the user's current viewport is at {shot} "
                     f"(use Read to view it if useful).")
    msg = st.messages.add()
    msg.role, msg.text, msg.timestamp = "user", text, time.strftime("%H:%M")
    st.busy = True
    cwd = bpy.path.abspath("//") or os.path.expanduser("~")
    chat.start(full, st.model, st.session_id, st.mode, cwd, binary, st.effort)
    _ensure_pump()
    return None


def _on_prompt_confirm(self, context):
    text = self.prompt.strip()
    p = prefs(context)
    if not text or (p is not None and not p.enter_sends) or self.busy:
        return
    scene_name = context.scene.name

    def later():
        scene = bpy.data.scenes.get(scene_name)
        if scene is None or scene.claude_chat.prompt.strip() != text:
            return None
        scene.claude_chat.prompt = ""
        err = send_prompt(bpy.context, text)
        if err:
            scene.claude_chat.prompt = text
            print(f"[Claude for Blender] {err}")
        return None
    bpy.app.timers.register(later, first_interval=0.01)


class CLAUDE_OT_send(bpy.types.Operator):
    bl_idname = "claude.send"
    bl_label = "Send to Claude"
    bl_description = "Send your prompt to Claude (with a view of the scene)"

    prompt: StringProperty(options={"SKIP_SAVE"})

    @classmethod
    def poll(cls, context):
        return not context.scene.claude_chat.busy

    def execute(self, context):
        st = context.scene.claude_chat
        text = self.prompt or st.prompt
        st.prompt = ""
        err = send_prompt(context, text)
        if err:
            st.prompt = "" if self.prompt else text
            self.report({"WARNING"}, err)
            return {"CANCELLED"}
        return {"FINISHED"}


def _scene_context():
    from .tools_scene import scene_overview
    try:
        ov = scene_overview(detail="normal", max_objects=40)
    except Exception as exc:
        return f"(scene summary failed: {exc})"
    for row in ov.get("objects", []):
        if isinstance(row, dict):
            row.pop("dimensions", None)
    ov.pop("collections", None)
    return json.dumps(core.jsonable(ov), separators=(",", ":"))[:6000]


def _capture_view():
    from .tools_scene import look
    try:
        res = look(view="current", shading="solid", max_size=900, overlays=False)
    except Exception:
        try:
            res = look(view="iso", max_size=900)
        except Exception:
            return None
    path = core.temp_path("chat_view.png")
    with open(path, "wb") as fh:
        fh.write(base64.b64decode(res["image"]["base64"]))
    return path


def _ensure_pump():
    if not bpy.app.timers.is_registered(chat.pump):
        bpy.app.timers.register(chat.pump, first_interval=0.1, persistent=True)


class CLAUDE_OT_cancel(bpy.types.Operator):
    bl_idname = "claude.cancel"
    bl_label = "Stop"
    bl_description = "Stop Claude's current reply"

    def execute(self, context):
        chat.cancel()
        return {"FINISHED"}


class CLAUDE_OT_new_chat(bpy.types.Operator):
    bl_idname = "claude.new_chat"
    bl_label = "New Chat"
    bl_description = ("Start a fresh conversation. The old session is kept and can be "
                      "resumed in a terminal with: claude --resume <id>")

    def execute(self, context):
        st = context.scene.claude_chat
        st.messages.clear()
        st.session_id = ""
        st.last_stats = ""
        st.visible_exchanges = 4
        return {"FINISHED"}


class CLAUDE_OT_copy(bpy.types.Operator):
    bl_idname = "claude.copy"
    bl_label = "Copy"
    bl_description = "Copy to the clipboard"

    text: StringProperty()
    index: IntProperty(default=-1)

    def execute(self, context):
        value = self.text
        if self.index >= 0:
            value = context.scene.claude_chat.messages[self.index].text
        context.window_manager.clipboard = value
        self.report({"INFO"}, "Copied")
        return {"FINISHED"}


class CLAUDE_OT_show_older(bpy.types.Operator):
    bl_idname = "claude.show_older"
    bl_label = "Show Older"
    bl_description = "Show earlier messages"

    def execute(self, context):
        context.scene.claude_chat.visible_exchanges += 4
        return {"FINISHED"}


class CLAUDE_OT_compose(bpy.types.Operator):
    bl_idname = "claude.compose"
    bl_label = "Compose Long Prompt"
    bl_description = ("Open a text editor window for a multi-line prompt. Press Send with an "
                      "empty prompt field to send it")

    def execute(self, context):
        text = bpy.data.texts.get("Claude Prompt") or bpy.data.texts.new("Claude Prompt")
        bpy.ops.wm.window_new()
        win = context.window_manager.windows[-1]
        area = win.screen.areas[0]
        area.type = "TEXT_EDITOR"
        area.spaces.active.text = text
        area.spaces.active.show_word_wrap = True
        return {"FINISHED"}


class CLAUDE_MT_quick_prompts(bpy.types.Menu):
    bl_idname = "CLAUDE_MT_quick_prompts"
    bl_label = "Quick Prompts"

    def draw(self, context):
        for label, prompt, icon in QUICK_PROMPTS:
            op = self.layout.operator("claude.send", text=label, icon=icon)
            op.prompt = prompt


class CLAUDE_OT_server(bpy.types.Operator):
    bl_idname = "claude.server"
    bl_label = "Tool Server"
    bl_description = "Start or stop the local tool server that MCP clients connect to"

    action: EnumProperty(items=[("START", "Start", ""), ("STOP", "Stop", "")])

    def execute(self, context):
        if self.action == "START":
            p = prefs(context)
            if not server.SERVER.start(p.port if p else server.DEFAULT_PORT):
                self.report({"ERROR"}, server.SERVER.last_error)
                return {"CANCELLED"}
        else:
            server.SERVER.stop()
        server.redraw_ui()
        return {"FINISHED"}


class CLAUDE_OT_run_tool(bpy.types.Operator):
    bl_idname = "claude.run_tool"
    bl_label = "Run Tool"
    bl_description = "Run one of Claude's tools directly"
    bl_options = {"REGISTER"}

    tool: StringProperty()
    args: StringProperty(default="{}")

    def execute(self, context):
        try:
            args = json.loads(self.args or "{}")
        except json.JSONDecodeError as exc:
            self.report({"ERROR"}, f"Bad args: {exc}")
            return {"CANCELLED"}
        resp = server.run_tool(self.tool, args, client="you")
        if not resp["ok"]:
            self.report({"ERROR"}, resp["error"].splitlines()[0][:200])
            return {"CANCELLED"}
        _last_result[self.tool] = resp["result"]
        self.report({"INFO"}, f"{self.tool}: done in {resp['ms']:.0f} ms")
        return {"FINISHED"}


_last_result = {}


class CLAUDE_OT_quick(bpy.types.Operator):
    bl_idname = "claude.quick"
    bl_label = "Quick Tool"
    bl_description = "Run a tool on the selection with the settings above"

    action: EnumProperty(items=[("LIGHT", "Light", "Build the lighting rig"),
                                ("MATERIAL", "Material", "Apply material to selection"),
                                ("FRAME", "Frame", "Frame the selection with the camera"),
                                ("GENERATE", "Generate", "Generate the procedural asset"),
                                ("AUDIT", "Audit", "Audit selected meshes"),
                                ("DROP", "Drop", "Drop selection onto the surface below")])

    def execute(self, context):
        qt = context.scene.claude_tools
        sel = [o.name for o in context.selected_objects]
        target = sel or None
        if self.action == "LIGHT":
            call = ("lighting_rig", {"preset": qt.light_preset, **({"target": target}
                                                                   if target else {})})
        elif self.action == "MATERIAL":
            if not sel:
                self.report({"WARNING"}, "Select objects first")
                return {"CANCELLED"}
            args = {"preset": qt.material_preset, "assign_to": sel}
            if qt.use_color:
                args["base_color"] = [round(c, 4) for c in qt.material_color]
            call = ("create_material", args)
        elif self.action == "FRAME":
            call = ("camera_setup", {"aspect": qt.aspect, **({"target": target} if target else {})})
        elif self.action == "GENERATE":
            loc = list(context.scene.cursor.location)
            call = ("generate", {"kind": qt.generate_kind, "params": {"location": loc}})
        elif self.action == "DROP":
            if not sel:
                self.report({"WARNING"}, "Select objects first")
                return {"CANCELLED"}
            call = ("transform_objects", {"objects": sel, "drop_to_surface": True})
        else:
            meshes = [o.name for o in context.selected_objects if o.type == "MESH"]
            if not meshes:
                self.report({"WARNING"}, "Select mesh objects first")
                return {"CANCELLED"}
            resp = server.run_tool("analyze_mesh", {"objects": meshes}, client="you")
            if not resp["ok"]:
                self.report({"ERROR"}, resp["error"][:200])
                return {"CANCELLED"}
            reports = resp["result"]["reports"]

            def draw(menu, _ctx):
                for rep in reports:
                    menu.layout.label(text=f"{rep['object']}: {rep['verdict']}",
                                      icon="CHECKMARK" if rep["verdict"] == "clean" else "ERROR")
                    for issue in rep["issues"]:
                        menu.layout.label(text=f"   {issue['issue']}",
                                          icon={"error": "CANCEL", "warning": "ERROR"}.get(
                                              issue["severity"], "INFO"))
            context.window_manager.popup_menu(draw, title="Mesh audit", icon="MODIFIER")
            return {"FINISHED"}
        resp = server.run_tool(call[0], call[1], client="you")
        if not resp["ok"]:
            self.report({"ERROR"}, resp["error"].splitlines()[0][:200])
            return {"CANCELLED"}
        self.report({"INFO"}, f"{call[0]} done")
        return {"FINISHED"}


class CLAUDE_OT_undo_agent(bpy.types.Operator):
    bl_idname = "claude.undo_agent"
    bl_label = "Undo Last Step"
    bl_description = "Undo the most recent change (each Claude tool call is one step)"

    def execute(self, context):
        bpy.ops.ed.undo()
        return {"FINISHED"}


class CLAUDE_OT_clear_activity(bpy.types.Operator):
    bl_idname = "claude.clear_activity"
    bl_label = "Clear Activity"
    bl_description = "Clear the activity feed"

    def execute(self, context):
        server.ACTIVITY.clear()
        return {"FINISHED"}


def mcp_add_command():
    import shutil
    uv = shutil.which("uv", path=chat.child_env()["PATH"]) or "uv"
    return ["claude", "mcp", "add", chat.MCP_SERVER_NAME, "--scope", "user", "--", uv, "run",
            "--quiet", "--script", chat.MCP_SCRIPT]


class CLAUDE_OT_register_mcp(bpy.types.Operator):
    bl_idname = "claude.register_mcp"
    bl_label = "Connect Claude Code"
    bl_description = ("Register this addon's MCP server with Claude Code (user scope), so any "
                      "terminal session can drive Blender")

    copy_only: BoolProperty(default=False)

    def execute(self, context):
        cmd = mcp_add_command()
        printable = " ".join(f'"{c}"' if " " in c else c for c in cmd)
        if self.copy_only:
            context.window_manager.clipboard = printable
            self.report({"INFO"}, "Command copied to clipboard")
            return {"FINISHED"}
        binary = chat.claude_binary(prefs(context).claude_path if prefs(context) else "")
        if binary is None:
            context.window_manager.clipboard = printable
            self.report({"ERROR"}, "claude CLI not found; the command is on your clipboard")
            return {"CANCELLED"}
        cmd[0] = binary
        proc = subprocess.run(cmd, capture_output=True, text=True, env=chat.child_env())
        out = (proc.stdout + proc.stderr).strip()
        if proc.returncode != 0 and "already exists" not in out:
            self.report({"ERROR"}, out[:300] or "claude mcp add failed")
            return {"CANCELLED"}
        self.report({"INFO"}, "Registered: run `claude` anywhere and ask it to use Blender")
        return {"FINISHED"}


class _Base:
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Claude"


def _draw_status(layout):
    srv = server.SERVER
    row = layout.row(align=True)
    if srv.running:
        n = len(srv.client_list())
        sub = row.row(align=True)
        sub.label(text=f"Live on :{srv.port}", icon="LINKED")
        sub = row.row(align=True)
        sub.alignment = "RIGHT"
        sub.label(text=f"{n} client{'s' if n != 1 else ''}" if n else "idle")
        row.operator("claude.server", text="", icon="PAUSE").action = "STOP"
    else:
        sub = row.row(align=True)
        sub.alert = True
        sub.label(text=srv.last_error[:40] or "Tool server off", icon="UNLINKED")
        row.operator("claude.server", text="Start", icon="PLAY").action = "START"


def _exchanges(messages):
    groups, cur = [], None
    for i, m in enumerate(messages):
        if m.role == "user" or cur is None:
            cur = []
            groups.append(cur)
        cur.append((i, m))
    return groups


class CLAUDE_PT_chat(_Base, bpy.types.Panel):
    bl_idname = "CLAUDE_PT_chat"
    bl_label = "Claude"

    def draw_header(self, context):
        self.layout.label(icon="SOLO_ON")

    def draw(self, context):
        layout = self.layout
        st = context.scene.claude_chat
        _draw_status(layout)

        box = layout.box()
        col = box.column(align=True)
        row = col.row(align=True)
        row.scale_y = 1.35
        try:
            row.prop(st, "prompt", text="", placeholder="Ask Claude to build, fix or explain…")
        except TypeError:
            row.prop(st, "prompt", text="")
        row = col.row(align=True)
        row.scale_y = 1.2
        if st.busy:
            secs = int(chat.RUN.elapsed())
            row.operator("claude.cancel", text=f"Stop  ·  {secs}s", icon="CANCEL")
        else:
            row.operator("claude.send", text="Send", icon="PLAY")
            row.menu("CLAUDE_MT_quick_prompts", text="", icon="COLLAPSEMENU")
            row.operator("claude.compose", text="", icon="TEXT")
        chips = box.row(align=True)
        chips.prop(st, "attach_screenshot", text="", icon="IMAGE_DATA")
        chips.prop(st, "include_scene", text="", icon="OUTLINER")
        chips.separator()
        chips.prop(st, "model", text="")
        chips.prop(st, "mode", text="")
        if st.mode == "FULL":
            warn = box.row()
            warn.alert = True
            warn.label(text="Full access: shell + files, no prompts", icon="ERROR")

        if st.busy:
            live = layout.box()
            head = live.row()
            tool = chat.RUN.current_tool
            head.label(text=f"Working · step {chat.RUN.tool_count}" if tool else "Thinking…",
                       icon="SORTTIME")
            if tool:
                sub = head.row()
                sub.alignment = "RIGHT"
                sub.label(text=tool)
            if chat.RUN.live_text.strip():
                tail = chat.RUN.live_text.strip()[-700:]
                draw_markdown(live, context, tail, _text_width(context, 10))

        groups = _exchanges(st.messages)
        if not groups:
            empty = layout.column(align=True)
            empty.scale_y = 0.85
            empty.separator()
            empty.label(text="Ask for anything, e.g.", icon="INFO")
            for ex in ("“Build a cozy reading nook”", "“Scatter rocks on the terrain”",
                       "“Make this render look cinematic”", "“Why does this mesh shade badly?”"):
                empty.label(text="    " + ex)
            return
        width = _text_width(context, 12)
        shown = groups[::-1][:st.visible_exchanges]
        for group in shown:
            box = layout.box()
            tools = [(i, m) for i, m in group if m.role == "tool"]
            for i, m in group:
                if m.role == "user":
                    head = box.row()
                    head.label(text="You", icon="USER")
                    sub = head.row()
                    sub.alignment = "RIGHT"
                    sub.label(text=m.timestamp)
                    body = box.column()
                    body.scale_y = 0.85
                    for w in wrap_px(context, m.text, width):
                        body.label(text=w)
                    if tools:
                        _draw_tool_steps(box, st, tools)
                elif m.role == "assistant":
                    head = box.row()
                    head.label(text="Claude", icon="SOLO_ON")
                    sub = head.row()
                    sub.alignment = "RIGHT"
                    sub.operator("claude.copy", text="", icon="COPYDOWN", emboss=False).index = i
                    draw_markdown(box, context, m.text, width)
                elif m.role == "error":
                    err = box.column(align=True)
                    err.alert = True
                    err.label(text="Problem", icon="ERROR")
                    for w in wrap_px(context, m.text, width)[:12]:
                        err.label(text=w)
        older = len(groups) - len(shown)
        foot = layout.row(align=True)
        if older > 0:
            foot.operator("claude.show_older", text=f"Show older ({older})", icon="TRIA_DOWN")
        foot.operator("claude.new_chat", text="New chat", icon="FILE_NEW")
        if st.session_id:
            meta = layout.row(align=True)
            meta.scale_y = 0.8
            meta.label(text=f"{st.last_stats or 'session'}  ·  total ${st.total_cost:.2f}",
                       icon="TIME")
            op = meta.operator("claude.copy", text="", icon="CONSOLE", emboss=False)
            op.text = f"claude --resume {st.session_id}"


def _draw_tool_steps(box, st, tools):
    row = box.row(align=True)
    row.scale_y = 0.8
    row.alignment = "LEFT"
    failed = sum(1 for _, m in tools if m.failed)
    label = f"{len(tools)} tool call{'s' if len(tools) != 1 else ''}"
    if failed:
        label += f" · {failed} failed"
    row.prop(st, "show_tool_steps", text=label, emboss=False,
             icon="DISCLOSURE_TRI_DOWN" if st.show_tool_steps else "DISCLOSURE_TRI_RIGHT")
    if not st.show_tool_steps:
        return
    col = box.column(align=True)
    col.scale_y = 0.75
    for _, m in tools[-14:]:
        r = col.row(align=True)
        r.alert = m.failed
        r.label(text=f"{m.text}", icon="ERROR" if m.failed else "TOOL_SETTINGS")
        if m.detail:
            sub = r.row()
            sub.alignment = "RIGHT"
            sub.label(text=m.detail[:28])
    if len(tools) > 14:
        col.label(text=f"   … {len(tools) - 14} earlier steps")


class CLAUDE_PT_activity(_Base, bpy.types.Panel):
    bl_idname = "CLAUDE_PT_activity"
    bl_label = "Activity"
    bl_parent_id = "CLAUDE_PT_chat"
    bl_options = {"DEFAULT_CLOSED"}

    def draw_header(self, context):
        n = len(server.ACTIVITY.entries)
        self.layout.label(text=f"({n})" if n else "")

    def draw(self, context):
        layout = self.layout
        entries = list(server.ACTIVITY.entries)[:15]
        row = layout.row(align=True)
        row.operator("claude.undo_agent", icon="LOOP_BACK")
        row.operator("claude.clear_activity", text="", icon="TRASH")
        if not entries:
            layout.label(text="No tool calls yet", icon="INFO")
            return
        clients = {e["client"] for e in entries}
        width = _text_width(context, 30)
        for e in entries:
            box = layout.column(align=True)
            head = box.row(align=True)
            head.alert = not e["ok"]
            icon = CATEGORY_ICONS.get(e.get("category"), "TOOL_SETTINGS") if e["ok"] else "ERROR"
            head.label(text=e["tool"], icon=icon)
            sub = head.row()
            sub.alignment = "RIGHT"
            who = f"{e['client']} · " if len(clients) > 1 else ""
            sub.label(text=f"{who}{e['ms']:.0f} ms")
            detail = e["error"] if not e["ok"] else (e["summary"] or e["args"])
            if detail:
                line = box.row()
                line.scale_y = 0.7
                line.alert = not e["ok"]
                lines = wrap_px(context, detail, width)[:2]
                inner = line.column(align=True)
                for w in lines:
                    inner.label(text=w, icon="BLANK1")
            box.separator(factor=0.3)


class CLAUDE_PT_quick(_Base, bpy.types.Panel):
    bl_idname = "CLAUDE_PT_quick"
    bl_label = "Quick Tools"
    bl_parent_id = "CLAUDE_PT_chat"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        qt = context.scene.claude_tools
        layout = self.layout
        layout.use_property_split = False

        col = layout.column(align=True)
        col.label(text="Lighting", icon="LIGHT")
        row = col.row(align=True)
        row.prop(qt, "light_preset", text="")
        row.operator("claude.quick", text="Light", icon="CHECKMARK").action = "LIGHT"

        col = layout.column(align=True)
        col.label(text="Material → selection", icon="MATERIAL")
        row = col.row(align=True)
        row.prop(qt, "material_preset", text="")
        row.prop(qt, "use_color", text="", icon="COLOR")
        if qt.use_color:
            row.prop(qt, "material_color", text="")
        row.operator("claude.quick", text="Apply", icon="CHECKMARK").action = "MATERIAL"

        col = layout.column(align=True)
        col.label(text="Camera", icon="CAMERA_DATA")
        row = col.row(align=True)
        row.prop(qt, "aspect", text="")
        row.operator("claude.quick", text="Frame", icon="CHECKMARK").action = "FRAME"

        col = layout.column(align=True)
        col.label(text="Generate at cursor", icon="MOD_BUILD")
        row = col.row(align=True)
        row.prop(qt, "generate_kind", text="")
        row.operator("claude.quick", text="Make", icon="CHECKMARK").action = "GENERATE"

        row = layout.row(align=True)
        row.operator("claude.quick", text="Audit mesh", icon="MODIFIER").action = "AUDIT"
        row.operator("claude.quick", text="Drop to floor", icon="SORT_ASC").action = "DROP"


class CLAUDE_PT_settings(_Base, bpy.types.Panel):
    bl_idname = "CLAUDE_PT_settings"
    bl_label = "Connection & Settings"
    bl_parent_id = "CLAUDE_PT_chat"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        st = context.scene.claude_chat
        col = layout.column(align=True)
        col.prop(st, "effort")
        col.prop(st, "show_tool_steps")
        layout.separator()
        col = layout.column(align=True)
        col.label(text=f"{len(core.TOOLS)} tools exposed over MCP", icon="TOOL_SETTINGS")
        for c in server.SERVER.client_list():
            col.label(text=f"{c['name']}  ·  {c['calls']} calls", icon="LINKED")
        row = layout.row(align=True)
        row.operator("claude.register_mcp", icon="CONSOLE")
        row.operator("claude.register_mcp", text="", icon="COPYDOWN").copy_only = True
        layout.label(text="Other clients can connect at the same time", icon="INFO")


class ClaudeBlenderPreferences(bpy.types.AddonPreferences):
    bl_idname = ADDON_ID

    port: IntProperty(name="Port", default=server.DEFAULT_PORT, min=1024, max=65535)
    autostart: BoolProperty(name="Start tool server automatically", default=True)
    claude_path: StringProperty(name="claude CLI", subtype="FILE_PATH",
                                description="Leave empty to find it automatically")
    enter_sends: BoolProperty(name="Enter sends the prompt", default=True,
                              description="Confirming the prompt field with Enter sends it")
    hud: BoolProperty(name="Viewport HUD while Claude works", default=True)
    statusbar: BoolProperty(name="Status bar indicator", default=True)

    def draw(self, context):
        layout = self.layout
        col = layout.column()
        col.prop(self, "autostart")
        col.prop(self, "port")
        col.prop(self, "claude_path")
        col.prop(self, "enter_sends")
        col.prop(self, "hud")
        col.prop(self, "statusbar")
        found = chat.claude_binary(self.claude_path)
        col.label(text=f"claude: {found or 'not found'}", icon="CHECKMARK" if found else "ERROR")
        row = layout.row()
        row.operator("claude.register_mcp", icon="CONSOLE")


_hud_handle = None


def _draw_hud():
    p = prefs()
    if p is not None and not p.hud:
        return
    st = getattr(bpy.context.scene, "claude_chat", None)
    busy_chat = bool(st and st.busy)
    tool = server.SERVER.busy_tool or (chat.RUN.current_tool if busy_chat else "")
    recent = server.ACTIVITY.entries[0] if server.ACTIVITY.entries else None
    fresh = recent and time.time() - recent["time"] < 2.5
    if not (busy_chat or tool or fresh):
        return
    import blf
    import gpu
    from gpu_extras.batch import batch_for_shader
    region = bpy.context.region
    ui = bpy.context.preferences.system.ui_scale
    if busy_chat:
        text = f"Claude · {tool or 'thinking'}"
    elif tool:
        text = f"Claude · {tool}"
    else:
        text = f"Claude · {recent['tool']} {'✓' if recent['ok'] else '✕'}"
    size = 13 * ui
    blf.size(0, size)
    tw, th = blf.dimensions(0, text)
    pad = 8 * ui
    x, y = 16 * ui, 16 * ui + 24 * ui
    w, h = tw + pad * 2 + 14 * ui, th + pad * 2
    shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    gpu.state.blend_set("ALPHA")
    verts = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    batch = batch_for_shader(shader, "TRI_FAN", {"pos": verts})
    shader.bind()
    shader.uniform_float("color", (0.08, 0.08, 0.1, 0.78))
    batch.draw(shader)
    pulse = 0.55 + 0.45 * abs(((time.time() * 1.6) % 2) - 1)
    if busy_chat or tool:
        dot = (0.98, 0.62, 0.25, pulse)
    else:
        dot = (0.35, 0.85, 0.45, 1) if recent["ok"] else (0.95, 0.35, 0.3, 1)
    cx, cy, rr = x + pad + 4 * ui, y + h / 2, 4 * ui
    import math
    circle = [(cx, cy)] + [(cx + rr * math.cos(a / 12 * 2 * math.pi),
                            cy + rr * math.sin(a / 12 * 2 * math.pi)) for a in range(13)]
    b2 = batch_for_shader(shader, "TRI_FAN", {"pos": circle})
    shader.uniform_float("color", dot)
    b2.draw(shader)
    gpu.state.blend_set("NONE")
    blf.position(0, x + pad + 14 * ui, y + pad + 1, 0)
    blf.color(0, 0.95, 0.95, 0.95, 1)
    blf.draw(0, text)
    if region is not None and (busy_chat or tool):
        region.tag_redraw()


def _draw_statusbar(self, context):
    p = prefs(context)
    if p is not None and not p.statusbar:
        return
    srv = server.SERVER
    row = self.layout.row(align=True)
    if srv.running:
        n = len(srv.client_list())
        st = getattr(context.scene, "claude_chat", None)
        state = "working" if (st and st.busy) or srv.busy_tool else (
            f"{n} client{'s' if n != 1 else ''}" if n else "ready")
        row.label(text=f"Claude: {state}", icon="SOLO_ON")
    else:
        row.label(text="Claude: off", icon="SOLO_OFF")


CLASSES = (
    ClaudeMessage, ClaudeChatState, ClaudeQuickTools, ClaudeBlenderPreferences,
    CLAUDE_OT_send, CLAUDE_OT_cancel, CLAUDE_OT_new_chat, CLAUDE_OT_copy, CLAUDE_OT_show_older,
    CLAUDE_OT_compose, CLAUDE_MT_quick_prompts, CLAUDE_OT_server, CLAUDE_OT_run_tool,
    CLAUDE_OT_quick, CLAUDE_OT_undo_agent, CLAUDE_OT_clear_activity, CLAUDE_OT_register_mcp,
    CLAUDE_PT_chat, CLAUDE_PT_activity, CLAUDE_PT_quick, CLAUDE_PT_settings,
)


def register():
    global _hud_handle
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.claude_chat = PointerProperty(type=ClaudeChatState)
    bpy.types.Scene.claude_tools = PointerProperty(type=ClaudeQuickTools)
    if not bpy.app.background:
        _hud_handle = bpy.types.SpaceView3D.draw_handler_add(_draw_hud, (), "WINDOW",
                                                             "POST_PIXEL")
        bpy.types.STATUSBAR_HT_header.append(_draw_statusbar)


def unregister():
    global _hud_handle
    if _hud_handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_hud_handle, "WINDOW")
        _hud_handle = None
    try:
        bpy.types.STATUSBAR_HT_header.remove(_draw_statusbar)
    except (ValueError, AttributeError):
        pass
    del bpy.types.Scene.claude_chat
    del bpy.types.Scene.claude_tools
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
