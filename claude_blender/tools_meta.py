import contextlib
import io
import math
import os
import time
import traceback

import bmesh
import bpy
import mathutils
from mathutils import Vector

from .core import (
    P, TOOLS, ToolError, jsonable, r, resolve_objects, select_only, tool, window_override,
    world_bbox,
)

CHECKPOINT_DIR = os.path.expanduser("~/.claude_blender/checkpoints")


@tool("execute_python", """
Run arbitrary Python inside Blender (bpy, bmesh, mathutils, math, Vector and
C/D shortcuts are preloaded). Assign to `result` to return a value; print()
output is captured. Reports which objects were added/removed. Prefer the
dedicated tools -- they validate, frame and report better -- and use this for
anything they don't cover.
""", "Advanced",
      code=P.str("Python source", required=True))
def execute_python(code):
    before = {o.name for o in bpy.data.objects}
    ns = {"bpy": bpy, "bmesh": bmesh, "mathutils": mathutils, "math": math, "Vector": Vector,
          "C": bpy.context, "D": bpy.data, "result": None}
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            exec(compile(code, "<claude>", "exec"), ns)
    except Exception as exc:
        tb = traceback.format_exc().splitlines()
        user = [ln for ln in tb if "<claude>" in ln or not ln.startswith("  File")]
        raise ToolError(f"{type(exc).__name__}: {exc}\n" + "\n".join(user[-8:]) +
                        (f"\nstdout before error:\n{buf.getvalue()[-1500:]}" if buf.getvalue() else ""))
    after = {o.name for o in bpy.data.objects}
    out = {"result": jsonable(ns.get("result")), "stdout": buf.getvalue()[-6000:]}
    if after - before:
        out["objects_added"] = sorted(after - before)[:50]
    if before - after:
        out["objects_removed"] = sorted(before - after)[:50]
    return out


@tool("batch", """
Run several tool calls in order in one round trip, as ONE undo step. Each
item is {"tool": name, "args": {...}}. Results come back per call; with
stop_on_error (default) the batch halts at the first failure and reports
which call failed. Ideal for building a scene in one go.
""", "Advanced",
      calls=P.list("Tool calls", {"type": "object", "properties": {
          "tool": {"type": "string"}, "args": {"type": "object"}}, "required": ["tool"]},
          required=True),
      stop_on_error=P.bool("Stop at the first failing call", True))
def batch(calls, stop_on_error=True):
    from .server import ACTIVITY, _summarize_args, _summarize_result
    results = []
    for i, call in enumerate(calls):
        name = call.get("tool")
        if name == "batch":
            raise ToolError("batch can't contain batch")
        t = TOOLS.get(name)
        t0 = time.perf_counter()
        if t is None:
            res = {"index": i, "tool": name, "ok": False, "error": f"Unknown tool {name!r}"}
        else:
            try:
                value = t.call(call.get("args") or {})
                res = {"index": i, "tool": name, "ok": True, "result": value}
            except ToolError as exc:
                res = {"index": i, "tool": name, "ok": False, "error": str(exc)}
            except Exception as exc:
                res = {"index": i, "tool": name, "ok": False,
                       "error": f"{type(exc).__name__}: {exc}"}
        ACTIVITY.add(tool=name or "?", category=t.category if t else "?",
                     args=_summarize_args(call.get("args")), ok=res["ok"],
                     ms=(time.perf_counter() - t0) * 1000, client="batch",
                     error=res.get("error", "")[:300],
                     summary=_summarize_result(res.get("result")) if res["ok"] else "",
                     images=0)
        results.append(res)
        if not res["ok"] and stop_on_error:
            break
    images = []
    for res in results:
        val = res.get("result")
        if isinstance(val, dict) and "image" in val:
            images.append(val.pop("image"))
    out = {"completed": sum(1 for x in results if x["ok"]), "total": len(calls),
           "results": jsonable(results)}
    if images:
        out["images"] = images
    return out


def _undo(steps, redo=False):
    ctx = window_override()
    if not ctx:
        raise ToolError("Undo needs the Blender UI (not available in background mode)")
    done = 0
    with bpy.context.temp_override(**ctx):
        for _ in range(steps):
            op = bpy.ops.ed.redo if redo else bpy.ops.ed.undo
            if op.poll():
                op()
                done += 1
    return done


@tool("undo", """
Undo (or redo) the last N steps. Every mutating tool call is its own undo
step labelled 'Claude: <tool>', so undo steps=1 reverts exactly your last
call. history=true lists recent undo steps without changing anything.
""", "Advanced",
      steps=P.int("How many steps", 1, minimum=1, maximum=64),
      redo=P.bool("Redo instead of undo", False),
      history=P.bool("Only list the undo history", False))
def undo(steps=1, redo=False, history=False):
    if history:
        try:
            wm = bpy.context.window_manager
            ctx = window_override()
            with bpy.context.temp_override(**ctx):
                items = [s.name for s in getattr(wm, "undo_history", [])] if hasattr(
                    wm, "undo_history") else None
        except Exception:
            items = None
        from .server import ACTIVITY
        recent = [f"{e['tool']} ({'ok' if e['ok'] else 'failed'})" for e in
                  list(ACTIVITY.entries)[:20]]
        return {"undo_history": items, "recent_agent_calls": recent}
    done = _undo(steps, redo)
    if done == 0:
        raise ToolError(f"Nothing to {'redo' if redo else 'undo'}")
    return {"undone" if not redo else "redone": done,
            "objects_now": len(bpy.context.scene.objects)}


def _checkpoint_folder():
    stem = os.path.splitext(os.path.basename(bpy.data.filepath))[0] or "untitled"
    folder = os.path.join(CHECKPOINT_DIR, stem)
    os.makedirs(folder, exist_ok=True)
    return folder


@tool("checkpoint", """
Save-points for risky edits. action=save writes a copy of the whole .blend
(your open file is untouched). list shows them. restore opens one (the
original file on disk is never overwritten). Use save before big
destructive operations (applying booleans, joining, mass deletes).
""", "Advanced",
      action=P.str("What to do", "save", enum=["save", "list", "restore", "delete"]),
      name=P.str("Label for save; file name or label for restore/delete"))
def checkpoint(action="save", name=None):
    folder = _checkpoint_folder()
    if action == "save":
        label = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in (name or "auto"))
        path = os.path.join(folder, f"{time.strftime('%Y%m%d-%H%M%S')}_{label}.blend")
        bpy.ops.wm.save_as_mainfile(filepath=path, copy=True, check_existing=False)
        return {"saved": path, "label": label}
    files = sorted((f for f in os.listdir(folder) if f.endswith(".blend")), reverse=True)
    if action == "list":
        return {"folder": folder, "checkpoints": [
            {"file": f, "size_mb": r(os.path.getsize(os.path.join(folder, f)) / 1e6, 2)}
            for f in files[:30]]}
    if not name:
        raise ToolError("name is required")
    match = next((f for f in files if f == name or name in f), None)
    if match is None:
        raise ToolError(f"No checkpoint matching {name!r}. Have: {files[:15]}")
    path = os.path.join(folder, match)
    if action == "delete":
        os.remove(path)
        return {"deleted": match}
    original = bpy.data.filepath or "(unsaved)"

    def load():
        ctx = window_override()
        with bpy.context.temp_override(**ctx):
            bpy.ops.wm.open_mainfile(filepath=path, load_ui=False)
        return None
    bpy.app.timers.register(load, first_interval=0.2)
    return {"restoring": match, "previous_file": original,
            "note": "The checkpoint opens in a moment. The session now points at the "
                    "checkpoint copy; the user's original file on disk is untouched. "
                    "Use File > Save As to write it back over the original if wanted."}


@tool("focus_view", """
Point the USER's viewport (not just your own look) at something: frame
objects from an angle, switch shading, or look through the camera. Use it
to present results ('here's the chair I made').
""", "Advanced",
      target=P.objects("Objects to frame (default: all)"),
      view=P.str("Angle", "keep", enum=["keep", "front", "back", "left", "right", "top",
                                         "iso", "camera"]),
      shading=P.str("Shading", enum=["solid", "material", "rendered", "wireframe"]),
      select=P.bool("Also select the targets", True))
def focus_view(target=None, view="keep", shading=None, select=True):
    from .core import find_view3d, NAMED_VIEWS, direction_from_angles
    area, region, space = find_view3d()
    if space is None:
        raise ToolError("No 3D viewport is open")
    objs = resolve_objects(target) if target is not None else [
        o for o in bpy.context.scene.objects if o.visible_get()]
    if select and objs:
        select_only(objs)
    rv3d = space.region_3d
    if shading:
        space.shading.type = shading.upper()
    if view == "camera":
        if bpy.context.scene.camera is None:
            raise ToolError("Scene has no camera")
        rv3d.view_perspective = "CAMERA"
    else:
        if rv3d.view_perspective == "CAMERA":
            rv3d.view_perspective = "PERSP"
        bb = world_bbox(objs)
        if bb:
            lo, hi = bb
            rv3d.view_location = (lo + hi) / 2
            rv3d.view_distance = max((hi - lo).length * 2.2, 1.5)
        if view != "keep":
            az, el = NAMED_VIEWS[view]
            d = direction_from_angles(az, el)
            rv3d.view_rotation = (d).to_track_quat("Z", "Y")
    area.tag_redraw()
    return {"framed": [o.name for o in objs][:30], "view": view,
            "shading": space.shading.type}
