import math
import os
import time

import bpy
from mathutils import Vector

from .core import (
    P, ToolError, enum_items, link_to_collection, r, resolve_object, resolve_objects,
    set_enum, temp_path, tool, world_bbox,
)

INTERPOLATIONS = ["CONSTANT", "LINEAR", "BEZIER", "SINE", "QUAD", "CUBIC", "QUART", "QUINT",
                  "EXPO", "CIRC", "BACK", "BOUNCE", "ELASTIC"]


def fcurves_of(id_block):
    ad = getattr(id_block, "animation_data", None)
    if not ad or not ad.action:
        return []
    action = ad.action
    try:
        from bpy_extras import anim_utils
        slot = getattr(ad, "action_slot", None)
        if slot is not None and hasattr(anim_utils, "action_get_channelbag_for_slot"):
            bag = anim_utils.action_get_channelbag_for_slot(action, slot)
            if bag is not None:
                return list(bag.fcurves)
    except ImportError:
        pass
    legacy = getattr(action, "fcurves", None)
    if legacy is not None:
        return list(legacy)
    out = []
    for layer in getattr(action, "layers", []):
        for strip in layer.strips:
            for bag in getattr(strip, "channelbags", []):
                out.extend(bag.fcurves)
    return out


def _style_keys(obj, paths, interpolation="BEZIER", easing="AUTO", frames=None):
    for fc in fcurves_of(obj):
        if paths and fc.data_path not in paths:
            continue
        for kp in fc.keyframe_points:
            if frames is not None and int(round(kp.co.x)) not in frames:
                continue
            kp.interpolation = interpolation
            if hasattr(kp, "easing"):
                kp.easing = easing
        fc.update()


def _add_fmod(obj, path, kind, index=None, **settings):
    for fc in fcurves_of(obj):
        if fc.data_path == path and (index is None or fc.array_index == index):
            m = fc.modifiers.new(kind)
            for k, v in settings.items():
                setattr(m, k, v)


def _key(obj, path, frame, value=None, index=-1):
    if value is not None:
        setattr(obj, path, value)
    obj.keyframe_insert(path, frame=frame, index=index)


PRESETS = ["keyframes", "turntable", "orbit", "spin", "bounce", "float", "wiggle", "pulse",
           "grow_in", "shrink_out", "drop_in", "slide_in", "follow_path", "clear"]


@tool("animate", f"""
Animate objects with motion presets or explicit keyframes. presets:
{', '.join(PRESETS)}.
- keyframes: `keys` = [{{"frame": 1, "location": [..], "rotation": [deg..],
  "scale": [..], "<any.rna.path>": value}}, ...]
- turntable/spin: full rotations about `axis` (loops seamlessly, linear)
- orbit: a camera (or anything) circles `target` at its current radius
- bounce, float (gentle bob), pulse (scale breathing): loop with `cycles`
- wiggle: procedural noise shake (strength in m / degrees)
- grow_in / shrink_out / drop_in / slide_in: entrance and exit moves with
  overshoot; with several objects `stagger` offsets each by N frames
- follow_path: travel along curve `path` (tangent-aligned)
- clear: remove animation
Easing: interpolation (BEZIER, LINEAR, BACK, BOUNCE, ELASTIC, SINE, EXPO, ...)
and easing (EASE_IN, EASE_OUT, EASE_IN_OUT). set_range updates the scene's
frame range to cover the animation.
""", "Animate",
      objects=P.objects("Objects to animate", required=True),
      preset=P.str("Animation preset", required=True, enum=PRESETS),
      start=P.int("Start frame", 1),
      end=P.int("End frame", 120),
      keys=P.list("Explicit keyframes (preset=keyframes)", {"type": "object"}),
      axis=P.str("Rotation axis", "z", enum=["x", "y", "z"]),
      turns=P.num("Rotations for turntable/spin/orbit", 1.0),
      target=P.str("Orbit center object"),
      height=P.num("Bounce/float/drop height, slide distance", 1.0),
      cycles=P.num("Repetitions for bounce/float/pulse", 2),
      strength=P.num("Wiggle strength / pulse amount", 0.1),
      stagger=P.int("Frame offset between consecutive objects", 0),
      path=P.str("Curve object for follow_path"),
      interpolation=P.str("Keyframe interpolation", enum=INTERPOLATIONS),
      easing=P.str("Easing", enum=["AUTO", "EASE_IN", "EASE_OUT", "EASE_IN_OUT"]),
      set_range=P.bool("Fit the scene frame range to the animation", True))
def animate(objects, preset, start=1, end=120, keys=None, axis="z", turns=1.0, target=None,
            height=1.0, cycles=2, strength=0.1, stagger=0, path=None, interpolation=None,
            easing=None, set_range=True):
    objs = resolve_objects(objects)
    scene = bpy.context.scene
    ai = "xyz".index(axis)
    last_frame = end
    report = []
    for n, o in enumerate(objs):
        s = start + n * stagger
        e = end + n * stagger
        last_frame = max(last_frame, e)
        dur = max(1, e - s)
        if preset == "clear":
            o.animation_data_clear()
            for c in [c for c in o.constraints if c.type == "FOLLOW_PATH"]:
                o.constraints.remove(c)
            report.append({"object": o.name, "cleared": True})
            continue
        if preset == "keyframes":
            if not keys:
                raise ToolError("preset=keyframes needs `keys`")
            paths = set()
            for k in keys:
                if "frame" not in k:
                    raise ToolError(f"Every key needs a frame: {k}")
                f = k["frame"] + n * stagger
                for prop, val in k.items():
                    if prop in ("frame", "interpolation", "easing"):
                        continue
                    if prop == "rotation":
                        prop, val = "rotation_euler", [math.radians(a) for a in val]
                    try:
                        if "." in prop or "[" in prop:
                            parent_path, _, attr = prop.rpartition(".")
                            owner = o.path_resolve(parent_path) if parent_path else o
                            setattr(owner, attr, val)
                            o.keyframe_insert(prop, frame=f)
                        else:
                            _key(o, prop, f, val)
                    except (AttributeError, TypeError, ValueError) as exc:
                        raise ToolError(f"Can't key {prop!r} on {o.name}: {exc}")
                    paths.add(prop)
                    if k.get("interpolation") or k.get("easing"):
                        _style_keys(o, {prop}, k.get("interpolation", "BEZIER"),
                                    k.get("easing", "AUTO"), {int(round(f))})
            report.append({"object": o.name, "keys": len(keys), "paths": sorted(paths)})
        elif preset in ("turntable", "spin"):
            base = o.rotation_euler[ai]
            o.rotation_euler[ai] = base
            _key(o, "rotation_euler", s, index=ai)
            o.rotation_euler[ai] = base + 2 * math.pi * turns
            _key(o, "rotation_euler", e, index=ai)
            o.rotation_euler[ai] = base
            _style_keys(o, {"rotation_euler"}, "LINEAR")
            if preset == "spin":
                _add_fmod(o, "rotation_euler", "CYCLES", ai)
            report.append({"object": o.name, "turns": turns, "frames": [s, e]})
        elif preset == "orbit":
            if not target:
                raise ToolError("orbit needs `target`")
            tgt = resolve_object(target)
            lo, hi = world_bbox([tgt])
            center = (lo + hi) / 2
            pivot = bpy.data.objects.new(f"{o.name}_OrbitPivot", None)
            pivot.empty_display_type = "SPHERE"
            pivot.empty_display_size = 0.2
            link_to_collection(pivot)
            pivot.location = center
            bpy.context.view_layer.update()
            mw = o.matrix_world.copy()
            o.parent = pivot
            o.matrix_parent_inverse = pivot.matrix_world.inverted()
            o.matrix_world = mw
            if o.type in ("CAMERA", "LIGHT") and not any(c.type == "TRACK_TO" for c in o.constraints):
                tc = o.constraints.new("TRACK_TO")
                tc.target = tgt
                tc.track_axis, tc.up_axis = "TRACK_NEGATIVE_Z", "UP_Y"
            pivot.rotation_euler.z = 0
            _key(pivot, "rotation_euler", s, index=2)
            pivot.rotation_euler.z = 2 * math.pi * turns
            _key(pivot, "rotation_euler", e, index=2)
            _style_keys(pivot, {"rotation_euler"}, "LINEAR")
            report.append({"object": o.name, "pivot": pivot.name, "center": r(center)})
        elif preset in ("bounce", "float"):
            z0 = o.location.z
            n_cyc = max(1, int(cycles))
            per = dur / n_cyc
            for c in range(n_cyc + 1):
                f0 = s + c * per
                o.location.z = z0
                _key(o, "location", f0, index=2)
                if c < n_cyc:
                    o.location.z = z0 + height
                    _key(o, "location", f0 + per / 2, index=2)
            o.location.z = z0
            if preset == "bounce":
                for fc in fcurves_of(o):
                    if fc.data_path == "location" and fc.array_index == 2:
                        for kp in fc.keyframe_points:
                            at_top = abs(kp.co.y - (z0 + height)) < 1e-6
                            kp.interpolation = "QUAD"
                            kp.easing = "EASE_IN" if at_top else "EASE_OUT"
            else:
                _style_keys(o, {"location"}, "SINE", "EASE_IN_OUT")
            report.append({"object": o.name, "cycles": n_cyc, "height": height})
        elif preset == "pulse":
            s0 = o.scale.copy()
            n_cyc = max(1, int(cycles))
            per = dur / n_cyc
            for c in range(n_cyc + 1):
                o.scale = s0
                _key(o, "scale", s + c * per)
                if c < n_cyc:
                    o.scale = s0 * (1 + strength)
                    _key(o, "scale", s + c * per + per / 2)
            o.scale = s0
            _style_keys(o, {"scale"}, "SINE", "EASE_IN_OUT")
            report.append({"object": o.name, "cycles": n_cyc})
        elif preset == "wiggle":
            _key(o, "location", s)
            _key(o, "rotation_euler", s)
            for idx in range(3):
                _add_fmod(o, "location", "NOISE", idx, strength=strength * 2,
                          scale=max(1.0, dur / 10), phase=idx * 13.7 + n * 3.1)
                _add_fmod(o, "rotation_euler", "NOISE", idx,
                          strength=math.radians(strength * 40), scale=max(1.0, dur / 8),
                          phase=idx * 7.3 + n * 5.9)
            report.append({"object": o.name, "noise_strength": strength})
        elif preset in ("grow_in", "shrink_out"):
            s0 = o.scale.copy()
            first, second = (s0 * 0.0001, s0) if preset == "grow_in" else (s0, s0 * 0.0001)
            o.scale = first
            _key(o, "scale", s)
            o.scale = second
            _key(o, "scale", e)
            o.scale = s0
            _style_keys(o, {"scale"}, interpolation or "BACK",
                        easing or ("EASE_OUT" if preset == "grow_in" else "EASE_IN"))
            report.append({"object": o.name, "frames": [s, e]})
        elif preset in ("drop_in", "slide_in"):
            loc0 = o.location.copy()
            offset = Vector((0, 0, height)) if preset == "drop_in" else Vector((-height, 0, 0))
            o.location = loc0 + offset
            _key(o, "location", s)
            o.location = loc0
            _key(o, "location", e)
            _style_keys(o, {"location"}, interpolation or
                        ("BOUNCE" if preset == "drop_in" else "BACK"), easing or "EASE_OUT")
            report.append({"object": o.name, "frames": [s, e]})
        elif preset == "follow_path":
            if not path:
                raise ToolError("follow_path needs `path` (a curve object)")
            curve = resolve_object(path)
            if curve.type != "CURVE":
                raise ToolError(f"{curve.name} is not a curve")
            con = o.constraints.new("FOLLOW_PATH")
            con.target = curve
            con.use_curve_follow = True
            con.use_fixed_location = True
            o.location = (0, 0, 0)
            con.offset_factor = 0.0
            con.keyframe_insert("offset_factor", frame=s)
            con.offset_factor = 1.0
            con.keyframe_insert("offset_factor", frame=e)
            for fc in fcurves_of(o):
                if "offset_factor" in fc.data_path:
                    for kp in fc.keyframe_points:
                        kp.interpolation = interpolation or "BEZIER"
                        kp.easing = easing or "EASE_IN_OUT"
            report.append({"object": o.name, "path": curve.name})
        if interpolation and preset in ("keyframes", "bounce", "float", "pulse"):
            _style_keys(o, set(), interpolation, easing or "AUTO")
    if set_range and preset != "clear":
        scene.frame_start = start
        scene.frame_end = last_frame
    return {"preset": preset, "results": report,
            "scene_range": [scene.frame_start, scene.frame_end]}


def _engine_id(name):
    return {"eevee": "BLENDER_EEVEE", "cycles": "CYCLES",
            "workbench": "BLENDER_WORKBENCH"}.get(name.lower(), name.upper())


@tool("render_settings", """
Read or change render configuration in one call. Everything is optional;
with no arguments it just reports. quality presets: draft (fast previews),
balanced, final (high samples + denoise). view_transform/look are validated
against what this Blender offers (e.g. 'AgX' + 'AgX - Punchy'). gpu=true asks
Cycles to use the GPU. output: path prefix for renders, format PNG/JPEG/
OPEN_EXR/FFMPEG.
""", "Render",
      engine=P.str("eevee, cycles or workbench"),
      quality=P.str("Preset", enum=["draft", "balanced", "final"]),
      samples=P.int("Render samples", minimum=1, maximum=65536),
      resolution=P.list("[width, height]", {"type": "integer"}),
      percentage=P.int("Resolution percentage", minimum=1, maximum=400),
      fps=P.int("Frames per second", minimum=1, maximum=240),
      frame_range=P.list("[start, end]", {"type": "integer"}),
      denoise=P.bool("Denoise (Cycles)"),
      transparent=P.bool("Transparent film"),
      view_transform=P.str("Color management view transform, e.g. AgX, Filmic, Standard"),
      look=P.str("Color management look, e.g. 'AgX - Punchy'"),
      exposure=P.num("Exposure (stops)"),
      motion_blur=P.bool("Motion blur"),
      gpu=P.bool("Cycles: render on GPU"),
      raytracing=P.bool("EEVEE: screen-space/ray-traced reflections & GI"),
      output=P.str("Output path prefix"),
      format=P.str("Output file format"))
def render_settings(engine=None, quality=None, samples=None, resolution=None, percentage=None,
                    fps=None, frame_range=None, denoise=None, transparent=None,
                    view_transform=None, look=None, exposure=None, motion_blur=None, gpu=None,
                    raytracing=None, output=None, format=None):
    scene = bpy.context.scene
    rd = scene.render
    if engine:
        set_enum(rd, "engine", _engine_id(engine), ("BLENDER_EEVEE_NEXT",))
    if quality:
        qs = {"draft": (16, 32, 50), "balanced": (64, 128, 100), "final": (256, 512, 100)}[quality]
        if rd.engine == "CYCLES":
            scene.cycles.samples = qs[1]
            scene.cycles.use_denoising = True
            scene.cycles.use_adaptive_sampling = True
        elif "EEVEE" in rd.engine:
            scene.eevee.taa_render_samples = qs[0] * 2
        rd.resolution_percentage = qs[2]
    if samples:
        if rd.engine == "CYCLES":
            scene.cycles.samples = samples
        elif "EEVEE" in rd.engine:
            scene.eevee.taa_render_samples = samples
    if resolution:
        rd.resolution_x, rd.resolution_y = int(resolution[0]), int(resolution[1])
    if percentage:
        rd.resolution_percentage = percentage
    if fps:
        rd.fps, rd.fps_base = fps, 1.0
    if frame_range:
        scene.frame_start, scene.frame_end = int(frame_range[0]), int(frame_range[1])
    if denoise is not None and hasattr(scene, "cycles"):
        scene.cycles.use_denoising = denoise
    if transparent is not None:
        rd.film_transparent = transparent
    vs = scene.view_settings
    if view_transform:
        set_enum(vs, "view_transform", view_transform)
    if look:
        valid = enum_items(vs, "look")
        cand = look if look in valid else next(
            (v for v in valid if v.lower().endswith(look.lower())), None)
        if cand is None:
            raise ToolError(f"Invalid look {look!r}. Valid for {vs.view_transform}: {valid}")
        vs.look = cand
    if exposure is not None:
        vs.exposure = exposure
    if motion_blur is not None:
        rd.use_motion_blur = motion_blur
    if gpu is not None and hasattr(scene, "cycles"):
        scene.cycles.device = "GPU" if gpu else "CPU"
        if gpu:
            try:
                prefs = bpy.context.preferences.addons["cycles"].preferences
                for kind in ("METAL", "OPTIX", "CUDA", "HIP", "ONEAPI"):
                    try:
                        prefs.compute_device_type = kind
                        break
                    except TypeError:
                        continue
                prefs.get_devices()
                for d in prefs.devices:
                    d.use = True
            except (KeyError, AttributeError):
                pass
    if raytracing is not None and hasattr(scene.eevee, "use_raytracing"):
        scene.eevee.use_raytracing = raytracing
    if output:
        rd.filepath = output
    if format:
        set_enum(rd.image_settings, "file_format", format.upper())

    out = {"engine": rd.engine, "resolution": [rd.resolution_x, rd.resolution_y],
           "percentage": rd.resolution_percentage, "fps": rd.fps,
           "frame_range": [scene.frame_start, scene.frame_end],
           "transparent": rd.film_transparent, "view_transform": vs.view_transform,
           "look": vs.look, "exposure": r(vs.exposure), "output": rd.filepath,
           "format": rd.image_settings.file_format, "motion_blur": rd.use_motion_blur,
           "camera": scene.camera.name if scene.camera else None,
           "valid_view_transforms": enum_items(vs, "view_transform"),
           "valid_looks": enum_items(vs, "look")}
    if rd.engine == "CYCLES":
        out["cycles"] = {"samples": scene.cycles.samples, "denoise": scene.cycles.use_denoising,
                         "device": scene.cycles.device}
    elif "EEVEE" in rd.engine:
        out["eevee"] = {"samples": scene.eevee.taa_render_samples,
                        "raytracing": getattr(scene.eevee, "use_raytracing", None)}
    return out


@tool("render_animation", """
Render a frame range to image files (blocking: Blender is busy until done,
so keep ranges and samples modest). Returns the written file paths. step
renders every Nth frame -- handy for a quick motion contact sheet.
""", "Render",
      start=P.int("First frame (default scene start)"),
      end=P.int("Last frame (default scene end)"),
      step=P.int("Frame step", 1, minimum=1),
      directory=P.str("Output folder (default temp)"),
      max_frames=P.int("Safety cap on frames rendered", 250, minimum=1, maximum=10000))
def render_animation(start=None, end=None, step=1, directory=None, max_frames=250):
    scene = bpy.context.scene
    rd = scene.render
    if scene.camera is None:
        raise ToolError("Scene has no camera")
    s = scene.frame_start if start is None else start
    e = scene.frame_end if end is None else end
    frames = list(range(s, e + 1, step))
    if len(frames) > max_frames:
        raise ToolError(f"{len(frames)} frames exceeds max_frames={max_frames}")
    folder = os.path.abspath(os.path.expanduser(directory)) if directory else \
        os.path.dirname(temp_path("anim/x"))
    os.makedirs(folder, exist_ok=True)
    saved = (rd.filepath, scene.frame_current, rd.image_settings.file_format)
    written = []
    t0 = time.perf_counter()
    try:
        if rd.image_settings.file_format == "FFMPEG":
            rd.image_settings.file_format = "PNG"
        for f in frames:
            scene.frame_set(f)
            rd.filepath = os.path.join(folder, f"frame_{f:04d}")
            bpy.ops.render.render(write_still=True)
            ext = rd.file_extension
            written.append(rd.filepath + ext)
    finally:
        rd.filepath, _, rd.image_settings.file_format = saved
        scene.frame_set(saved[1])
    return {"frames": len(written), "files": written[:20], "directory": folder,
            "seconds": round(time.perf_counter() - t0, 1)}
