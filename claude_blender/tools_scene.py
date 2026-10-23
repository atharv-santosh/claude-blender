import math
import os
import time

import bmesh
import bpy
import mathutils
from mathutils import Matrix, Vector
from mathutils.bvhtree import BVHTree

from .core import (
    NAMED_VIEWS, P, ToolError, bbox_info, direction_from_angles, find_view3d,
    jsonable, png_payload, r, resolve_object,
    resolve_objects, set_enum, temp_path, tool, world_bbox,
)

GEOMETRY_TYPES = {"MESH", "CURVE", "SURFACE", "FONT", "META"}


def _mesh_stats(obj, depsgraph):
    try:
        ev = obj.evaluated_get(depsgraph)
        me = ev.data if obj.type == "MESH" else ev.to_mesh()
    except RuntimeError:
        return None
    try:
        if me is None:
            return None
        tris = sum(len(p.vertices) - 2 for p in me.polygons)
        return {"verts": len(me.vertices), "faces": len(me.polygons), "tris": tris}
    finally:
        if obj.type != "MESH":
            ev.to_mesh_clear()


def _world_summary(world):
    if world is None:
        return None
    out = {"name": world.name}
    nt = world.node_tree
    if nt:
        out["nodes"] = sorted({n.type for n in nt.nodes} - {"OUTPUT_WORLD"})
        bg = next((n for n in nt.nodes if n.type == "BACKGROUND"), None)
        if bg:
            out["strength"] = r(bg.inputs["Strength"].default_value)
        env = next((n for n in nt.nodes if n.type == "TEX_ENVIRONMENT"), None)
        if env and env.image:
            out["hdri"] = os.path.basename(env.image.filepath)
        sky = next((n for n in nt.nodes if n.type == "TEX_SKY"), None)
        if sky:
            out["sky_type"] = sky.sky_type
    return out


@tool("scene_overview", """
Snapshot of the whole scene: file, render setup, frame range, units, world, the
collection tree, and every object with type, transform, size, materials,
modifiers and polycount. Start here. Use detail='brief' on big scenes, 'full'
for per-object rotation/scale/visibility.
""", "Inspect", mutates=False,
      detail=P.str("Amount of per-object detail", "normal", enum=["brief", "normal", "full"]),
      max_objects=P.int("Cap on objects listed", 150, minimum=1, maximum=2000))
def scene_overview(detail="normal", max_objects=150):
    scene = bpy.context.scene
    depsgraph = bpy.context.evaluated_depsgraph_get()
    rd = scene.render
    totals = {"verts": 0, "faces": 0, "tris": 0}
    by_type = {}
    rows = []
    for o in scene.objects:
        by_type[o.type] = by_type.get(o.type, 0) + 1
        stats = _mesh_stats(o, depsgraph) if o.type in GEOMETRY_TYPES else None
        if stats:
            for k in totals:
                totals[k] += stats[k]
        if len(rows) >= max_objects:
            continue
        if detail == "brief":
            rows.append(f"{o.name} [{o.type}]")
            continue
        row = {"name": o.name, "type": o.type,
               "location": r(o.matrix_world.translation, 3),
               "dimensions": r(o.dimensions, 3)}
        if o.parent:
            row["parent"] = o.parent.name
        mats = [s.material.name for s in o.material_slots if s.material]
        if mats:
            row["materials"] = mats
        if o.modifiers:
            row["modifiers"] = [f"{m.name}:{m.type}" for m in o.modifiers]
        if stats:
            row["tris"] = stats["tris"]
        if o.type == "LIGHT":
            row["light"] = f"{o.data.type} {r(o.data.energy, 1)}W"
        if o.type == "CAMERA":
            row["lens"] = r(o.data.lens, 1)
        if not o.visible_get():
            row["hidden"] = True
        if detail == "full":
            row["rotation_deg"] = r([math.degrees(a) for a in o.rotation_euler], 2)
            row["scale"] = r(o.scale, 3)
            row["collections"] = [c.name for c in o.users_collection]
            if o.animation_data and o.animation_data.action:
                row["animated"] = o.animation_data.action.name
        rows.append(row)

    def coll_tree(coll):
        node = {"name": coll.name, "objects": len(coll.objects)}
        kids = [coll_tree(c) for c in coll.children]
        if kids:
            node["children"] = kids
        return node

    world_bb = bbox_info([o for o in scene.objects if o.type in GEOMETRY_TYPES])
    return {
        "file": bpy.data.filepath or "(unsaved)",
        "blender": bpy.app.version_string,
        "scene": scene.name,
        "render": {"engine": rd.engine, "resolution": [rd.resolution_x, rd.resolution_y,
                                                       rd.resolution_percentage],
                   "camera": scene.camera.name if scene.camera else None,
                   "view_transform": scene.view_settings.view_transform,
                   "look": scene.view_settings.look},
        "frames": {"start": scene.frame_start, "end": scene.frame_end,
                   "current": scene.frame_current, "fps": rd.fps},
        "units": {"system": scene.unit_settings.system,
                  "scale": r(scene.unit_settings.scale_length)},
        "world": _world_summary(scene.world),
        "collections": coll_tree(scene.collection),
        "counts": {"objects": len(scene.objects), "by_type": by_type, **totals},
        "scene_bounds": world_bb,
        "selected": [o.name for o in bpy.context.selected_objects][:100],
        "active": bpy.context.view_layer.objects.active.name
        if bpy.context.view_layer.objects.active else None,
        "objects": rows,
        "truncated": len(scene.objects) > max_objects,
    }


def _socket_value(sock):
    if not hasattr(sock, "default_value"):
        return None
    v = sock.default_value
    try:
        return r(list(v), 3)
    except TypeError:
        return r(v, 3) if isinstance(v, float) else (v if isinstance(v, (int, bool, str)) else None)


def material_summary(mat):
    out = {"name": mat.name, "users": mat.users}
    nt = mat.node_tree
    if not nt:
        out["diffuse_color"] = r(list(mat.diffuse_color), 3)
        return out
    bsdf = next((n for n in nt.nodes if n.type == "BSDF_PRINCIPLED"), None)
    if bsdf:
        vals = {}
        for name in ("Base Color", "Metallic", "Roughness", "IOR", "Alpha",
                     "Transmission Weight", "Emission Strength", "Coat Weight",
                     "Subsurface Weight", "Sheen Weight"):
            s = bsdf.inputs.get(name)
            if s is None:
                continue
            vals[name] = "linked" if s.is_linked else _socket_value(s)
        out["principled"] = vals
    out["nodes"] = [f"{n.name}:{n.bl_idname}" for n in nt.nodes][:40]
    return out


def _modifier_settings(mod):
    out = {"name": mod.name, "type": mod.type, "show_viewport": mod.show_viewport,
           "show_render": mod.show_render}
    for p in mod.bl_rna.properties:
        if p.identifier in {"rna_type", "name", "type", "show_viewport", "show_render",
                            "show_in_editmode", "show_on_cage", "show_expanded",
                            "is_active", "is_override_data", "use_apply_on_spline",
                            "persistent_uid", "execution_time", "use_pin_to_last"}:
            continue
        try:
            v = getattr(mod, p.identifier)
        except AttributeError:
            continue
        if p.type == "POINTER":
            v = v.name if v is not None and hasattr(v, "name") else None
            if v is None:
                continue
        elif p.type == "COLLECTION":
            continue
        out[p.identifier] = jsonable(v)
    if mod.type == "NODES" and mod.node_group:
        inputs = {}
        for item in mod.node_group.interface.items_tree:
            if getattr(item, "in_out", None) == "INPUT" and item.identifier in mod.keys():
                inputs[item.name] = jsonable(mod[item.identifier])
        out["node_inputs"] = inputs
    return out


@tool("inspect_object", """
Everything about one object: local and world transforms, bounds, mesh stats,
UV/vertex-color layers, materials with key Principled values, the full
modifier stack with settings, constraints, keyframed channels, custom
properties, parent and children. Use before editing anything non-trivial.
""", "Inspect", mutates=False,
      name=P.str("Object name", required=True))
def inspect_object(name):
    o = resolve_object(name)
    depsgraph = bpy.context.evaluated_depsgraph_get()
    info = {
        "name": o.name, "type": o.type, "data": o.data.name if o.data else None,
        "parent": o.parent.name if o.parent else None,
        "children": [c.name for c in o.children],
        "collections": [c.name for c in o.users_collection],
        "visible": o.visible_get(), "hide_render": o.hide_render,
        "location": r(o.location), "rotation_deg": r([math.degrees(a) for a in o.rotation_euler], 3),
        "rotation_mode": o.rotation_mode, "scale": r(o.scale),
        "world_location": r(o.matrix_world.translation),
        "dimensions": r(o.dimensions), "world_bounds": bbox_info([o]),
    }
    if o.type in GEOMETRY_TYPES:
        info["evaluated"] = _mesh_stats(o, depsgraph)
    if o.type == "MESH":
        me = o.data
        info["mesh"] = {
            "verts": len(me.vertices), "edges": len(me.edges), "faces": len(me.polygons),
            "uv_layers": [uv.name for uv in me.uv_layers],
            "color_attributes": [c.name for c in me.color_attributes],
            "vertex_groups": [g.name for g in o.vertex_groups],
            "shape_keys": [k.name for k in me.shape_keys.key_blocks] if me.shape_keys else [],
            "smooth_faces": sum(1 for p in me.polygons if p.use_smooth),
        }
    if o.type == "LIGHT":
        L = o.data
        info["light"] = {"type": L.type, "energy": r(L.energy), "color": r(list(L.color)),
                         "size": r(getattr(L, "size", getattr(L, "shadow_soft_size", 0))),
                         "temperature": r(getattr(L, "temperature", 0)) if getattr(
                             L, "use_temperature", False) else None}
    if o.type == "CAMERA":
        c = o.data
        info["camera"] = {"lens": r(c.lens), "sensor": r(c.sensor_width), "type": c.type,
                          "dof": c.dof.use_dof,
                          "focus_object": c.dof.focus_object.name if c.dof.focus_object else None,
                          "focus_distance": r(c.dof.focus_distance), "fstop": r(c.dof.aperture_fstop),
                          "clip": [r(c.clip_start), r(c.clip_end)]}
    info["materials"] = [material_summary(s.material) if s.material else None
                         for s in o.material_slots]
    info["modifiers"] = [_modifier_settings(m) for m in o.modifiers]
    info["constraints"] = [{"name": c.name, "type": c.type,
                            "target": getattr(getattr(c, "target", None), "name", None)}
                           for c in o.constraints]
    ad = o.animation_data
    if ad and ad.action:
        info["animation"] = _action_channels(ad.action)
    custom = {k: jsonable(o[k]) for k in o.keys() if not k.startswith("_")}
    if custom:
        info["custom_properties"] = custom
    return info


def _action_channels(action):
    chans = []
    fcurves = list(getattr(action, "fcurves", []) or [])
    if not fcurves:
        for layer in getattr(action, "layers", []):
            for strip in layer.strips:
                for bag in getattr(strip, "channelbags", []):
                    fcurves.extend(bag.fcurves)
    for fc in fcurves[:60]:
        kps = fc.keyframe_points
        chans.append({"path": fc.data_path, "index": fc.array_index, "keys": len(kps),
                      "range": [r(kps[0].co.x, 1), r(kps[-1].co.x, 1)] if len(kps) else None,
                      "interpolation": kps[0].interpolation if len(kps) else None})
    return {"action": action.name, "frame_range": r(list(action.frame_range), 1),
            "channels": chans}


@tool("query_objects", """
Find objects by any combination of filters: name selector, type, material,
collection, modifier type, distance from a point or object, inside a box,
visibility. Returns compact rows sorted as asked. Use to resolve vague
references ('the red chairs near the table').
""", "Inspect", mutates=False,
      match=P.objects("Name selector to start from.", default="all"),
      type=P.str("Object type filter, e.g. MESH, LIGHT, CAMERA, EMPTY, CURVE"),
      material=P.str("Only objects using a material whose name contains this"),
      collection=P.str("Only objects in this collection (recursive)"),
      has_modifier=P.str("Only objects with a modifier of this type, e.g. SUBSURF"),
      near=P.objects("Object to measure distance from"),
      near_point=P.vec3("Point to measure distance from"),
      radius=P.num("Max distance from `near`/`near_point`"),
      inside_min=P.vec3("Box filter minimum corner (origin must be inside)"),
      inside_max=P.vec3("Box filter maximum corner"),
      visible=P.bool("Only visible (true) or only hidden (false) objects"),
      sort=P.str("Sort order", "name", enum=["name", "distance", "size", "height", "polycount"]),
      limit=P.int("Max rows", 100, minimum=1, maximum=5000))
def query_objects(match="all", type=None, material=None, collection=None, has_modifier=None,
                  near=None, near_point=None, radius=None, inside_min=None, inside_max=None,
                  visible=None, sort="name", limit=100):
    objs = resolve_objects(match, allow_empty=True)
    if type:
        objs = [o for o in objs if o.type == type.upper()]
    if material:
        m = material.lower()
        objs = [o for o in objs if any(s.material and m in s.material.name.lower()
                                       for s in o.material_slots)]
    if collection:
        coll = bpy.data.collections.get(collection)
        if coll is None:
            raise ToolError(f"No collection {collection!r}")
        members = set(coll.all_objects)
        objs = [o for o in objs if o in members]
    if has_modifier:
        objs = [o for o in objs if any(md.type == has_modifier.upper() for md in o.modifiers)]
    if visible is not None:
        objs = [o for o in objs if o.visible_get() == visible]
    ref = None
    if near is not None:
        ref = resolve_object(near).matrix_world.translation.copy()
    elif near_point is not None:
        ref = Vector(near_point)
    if inside_min is not None and inside_max is not None:
        lo, hi = Vector(inside_min), Vector(inside_max)
        objs = [o for o in objs if all(lo[i] <= o.matrix_world.translation[i] <= hi[i]
                                       for i in range(3))]
    depsgraph = bpy.context.evaluated_depsgraph_get()
    rows = []
    for o in objs:
        loc = o.matrix_world.translation
        row = {"name": o.name, "type": o.type, "location": r(loc, 3),
               "dimensions": r(o.dimensions, 3)}
        if ref is not None:
            row["distance"] = r((loc - ref).length, 3)
            if radius is not None and row["distance"] > radius:
                continue
        if sort == "polycount" and o.type in GEOMETRY_TYPES:
            stats = _mesh_stats(o, depsgraph)
            row["tris"] = stats["tris"] if stats else 0
        rows.append(row)
    keyfn = {
        "name": lambda x: x["name"],
        "distance": lambda x: x.get("distance", 0),
        "size": lambda x: -max(x["dimensions"]),
        "height": lambda x: -x["location"][2],
        "polycount": lambda x: -x.get("tris", 0),
    }[sort]
    rows.sort(key=keyfn)
    return {"count": len(rows), "objects": rows[:limit]}


def _perspective(fov, aspect, near, far):
    f = 1.0 / math.tan(fov / 2)
    return Matrix(((f / aspect, 0, 0, 0), (0, f, 0, 0),
                   (0, 0, (far + near) / (near - far), 2 * far * near / (near - far)),
                   (0, 0, -1, 0)))


def _orbit_camera(targets, azimuth, elevation, fov, aspect, padding):
    from .tools_look import _fit_distance_exact
    bb = world_bbox(targets) if targets else None
    if bb is None:
        bb = (Vector((-5, -5, -5)), Vector((5, 5, 5)))
    lo, hi = bb
    center = (lo + hi) / 2
    radius = max((hi - lo).length / 2, 0.05)
    d = direction_from_angles(azimuth, elevation)
    rot = (-d).to_track_quat("-Z", "Y")
    corners = [Vector((x, y, z)) for x in (lo.x, hi.x) for y in (lo.y, hi.y) for z in (lo.z, hi.z)]
    tan_v = math.tan(fov / 2)
    dist = _fit_distance_exact(rot, center, corners, tan_v * aspect, tan_v, padding)
    eye = center + d * dist
    cam_world = Matrix.Translation(eye) @ rot.to_matrix().to_4x4()
    return cam_world, dist, radius


def _gpu_capture(space, region, view_matrix, proj_matrix, width, height):
    import gpu
    import numpy as np
    offscreen = gpu.types.GPUOffScreen(width, height)
    try:
        offscreen.draw_view3d(bpy.context.scene, bpy.context.view_layer, space, region,
                              view_matrix, proj_matrix, do_color_management=True)
        buf = offscreen.texture_color.read()
        buf.dimensions = width * height * 4
        try:
            arr = np.frombuffer(buf, dtype=np.uint8)
        except (TypeError, ValueError):
            arr = np.array(buf.to_list(), dtype=np.uint8)
    finally:
        offscreen.free()
    px = arr.astype(np.float32).reshape(height, width, 4) / 255.0
    px[..., 3] = 1.0
    return px


def _render_capture(cam_world, fov, width, height, shading, camera=None):
    import numpy as np
    scene = bpy.context.scene
    rd = scene.render
    cam_data = None
    if camera is not None:
        cam = camera
    else:
        cam_data = bpy.data.cameras.new("_claude_look_cam")
        cam_data.sensor_fit = "VERTICAL"
        cam_data.angle_y = fov
        cam_data.clip_end = 10000
        cam = bpy.data.objects.new("_claude_look_cam", cam_data)
        scene.collection.objects.link(cam)
        cam.matrix_world = cam_world
    saved = (scene.camera, rd.engine, rd.resolution_x, rd.resolution_y,
             rd.resolution_percentage, rd.filepath, rd.image_settings.file_format,
             rd.film_transparent)
    path = temp_path(f"look_{time.time_ns()}.png")
    try:
        scene.camera = cam
        engine = "BLENDER_WORKBENCH" if shading in ("solid", "wireframe") else "BLENDER_EEVEE"
        set_enum(rd, "engine", engine, ("BLENDER_EEVEE_NEXT",))
        if shading == "wireframe":
            scene.display.shading.type = "WIREFRAME"
        rd.resolution_x, rd.resolution_y, rd.resolution_percentage = width, height, 100
        rd.film_transparent = False
        rd.image_settings.file_format = "PNG"
        rd.filepath = path
        bpy.ops.render.render(write_still=True)
        img = bpy.data.images.load(path, check_existing=False)
        try:
            arr = np.empty(width * height * 4, dtype=np.float32)
            img.pixels.foreach_get(arr)
        finally:
            bpy.data.images.remove(img)
        px = arr.reshape(height, width, 4)
        px[..., 3] = 1.0
        return px
    finally:
        (scene.camera, rd.engine, rd.resolution_x, rd.resolution_y,
         rd.resolution_percentage, rd.filepath, rd.image_settings.file_format,
         rd.film_transparent) = saved
        if shading == "wireframe":
            scene.display.shading.type = "SOLID"
        if cam_data is not None:
            bpy.data.objects.remove(cam)
            bpy.data.cameras.remove(cam_data)
        try:
            os.remove(path)
        except OSError:
            pass


def _compose_grid(tiles, cols, gap=4):
    import numpy as np
    h, w = tiles[0].shape[:2]
    rows = math.ceil(len(tiles) / cols)
    H, W = rows * h + (rows - 1) * gap, cols * w + (cols - 1) * gap
    out = np.full((H, W, 4), 0.12, dtype=np.float32)
    out[..., 3] = 1.0
    for i, t in enumerate(tiles):
        rr, cc = divmod(i, cols)
        y0 = H - (rr + 1) * h - rr * gap
        x0 = cc * (w + gap)
        out[y0:y0 + h, x0:x0 + w] = t
    return out


def _encode(px, fmt="png"):
    h, w = px.shape[:2]
    from .core import save_pixels_png
    path = temp_path(f"look_{time.time_ns()}.png")
    save_pixels_png(px.ravel(), w, h, path)
    if fmt == "jpeg":
        jpg = path[:-4] + ".jpg"
        img = bpy.data.images.load(path, check_existing=False)
        try:
            img.filepath_raw = jpg
            img.file_format = "JPEG"
            img.save()
        finally:
            bpy.data.images.remove(img)
        os.remove(path)
        import base64
        with open(jpg, "rb") as fh:
            data = base64.b64encode(fh.read()).decode("ascii")
        os.remove(jpg)
        return {"mime": "image/jpeg", "base64": data}
    payload = png_payload(path)
    os.remove(path)
    return payload


@tool("look", """
See the scene. Renders the 3D view to an image WITHOUT moving the user's
viewport. view: 'current' (what the user sees), 'camera' (scene camera), a
named angle (front/back/left/right/top/bottom/iso/iso_back/three_quarter/hero),
or 'orbit' with azimuth/elevation. Named and orbit views auto-frame `target`
(default: all visible geometry). Pass `views` (list) for a contact sheet of
several angles in one image -- the cheapest way to understand 3D shape.
shading: solid (fast, shows form), material (preview colors), rendered (final
look, slower), wireframe. isolate=true hides everything except `target`.
""", "Inspect", mutates=False,
      view=P.str("Single view", "current"),
      views=P.list("Several views composed into one grid image, e.g. "
                   "['front','right','top','iso']", {"type": "string"}),
      target=P.objects("Objects to frame (named/orbit views)"),
      azimuth=P.num("Orbit azimuth in degrees (0 = front, 90 = right)", 35),
      elevation=P.num("Orbit elevation in degrees", 20),
      shading=P.str("Viewport shading", "solid",
                    enum=["solid", "material", "rendered", "wireframe"]),
      isolate=P.bool("Temporarily hide everything except `target`", False),
      overlays=P.bool("Draw grid/gizmos/outlines (current view only)", False),
      lens_deg=P.num("Field of view in degrees for framed views", 40),
      padding=P.num("Framing padding factor", 1.15),
      max_size=P.int("Long edge of each tile in pixels", 768, minimum=128, maximum=2048),
      format=P.str("Image encoding", "png", enum=["png", "jpeg"]))
def look(view="current", views=None, target=None, azimuth=35, elevation=20, shading="solid",
         isolate=False, overlays=False, lens_deg=40, padding=1.15, max_size=768,
         format="png"):
    scene = bpy.context.scene
    area, region, space = find_view3d()
    gpu_ok = space is not None and not bpy.app.background
    view_list = list(views) if views else [view]
    if len(view_list) > 9:
        raise ToolError("At most 9 views per contact sheet")

    if target is not None:
        targets = resolve_objects(target)
    else:
        targets = [o for o in scene.objects if o.visible_get() and o.type in GEOMETRY_TYPES]

    if gpu_ok:
        aspect = region.width / max(1, region.height)
    else:
        aspect = scene.render.resolution_x / max(1, scene.render.resolution_y)
    if len(view_list) > 1:
        max_size = min(max_size, 640)
        aspect = 4 / 3
    w = max_size if aspect >= 1 else max(64, int(max_size * aspect))
    h = max(64, int(w / aspect)) if aspect >= 1 else max_size
    fov = math.radians(lens_deg)

    hidden, saved_shading, saved_overlay = [], None, None
    tiles, labels = [], []
    try:
        if isolate and target is not None:
            keep = set(targets)
            for o in scene.objects:
                if o not in keep and o.visible_get() and o.type not in {"LIGHT", "CAMERA"}:
                    o.hide_set(True)
                    hidden.append(o)
        if gpu_ok:
            saved_shading = space.shading.type
            space.shading.type = {"solid": "SOLID", "material": "MATERIAL",
                                  "rendered": "RENDERED", "wireframe": "WIREFRAME"}[shading]
            saved_overlay = space.overlay.show_overlays
            space.overlay.show_overlays = bool(overlays)

        for v in view_list:
            v = v.lower()
            if v == "current":
                if not gpu_ok:
                    raise ToolError("No interactive 3D viewport; use a named view instead")
                rv3d = space.region_3d
                tw, th = region.width, region.height
                s = min(1.0, max_size / max(tw, th))
                tw, th = max(1, int(tw * s)), max(1, int(th * s))
                if len(view_list) > 1:
                    tw, th = w, h
                tiles.append(_gpu_capture(space, region, rv3d.view_matrix,
                                          rv3d.window_matrix, tw, th))
            elif v == "camera":
                cam = scene.camera
                if cam is None:
                    raise ToolError("Scene has no camera. Create one with camera_setup.")
                rd = scene.render
                cam_aspect = (rd.resolution_x * rd.pixel_aspect_x) / max(
                    1, rd.resolution_y * rd.pixel_aspect_y)
                tw = max_size if cam_aspect >= 1 else int(max_size * cam_aspect)
                th = int(tw / cam_aspect) if cam_aspect >= 1 else max_size
                if len(view_list) > 1:
                    tw, th = w, h
                if gpu_ok:
                    dg = bpy.context.evaluated_depsgraph_get()
                    proj = cam.calc_matrix_camera(dg, x=tw, y=th,
                                                  scale_x=rd.pixel_aspect_x,
                                                  scale_y=rd.pixel_aspect_y)
                    tiles.append(_gpu_capture(space, region, cam.matrix_world.inverted(),
                                              proj, tw, th))
                else:
                    tiles.append(_render_capture(None, None, tw, th, shading, camera=cam))
            else:
                if v == "orbit":
                    az, el = azimuth, elevation
                elif v in NAMED_VIEWS:
                    az, el = NAMED_VIEWS[v]
                else:
                    raise ToolError(f"Unknown view {v!r}. Use current, camera, orbit or "
                                    f"{sorted(NAMED_VIEWS)}")
                cam_world, dist, radius = _orbit_camera(targets, az, el, fov, w / h, padding)
                if gpu_ok:
                    proj = _perspective(fov, w / h, max(0.001, (dist - radius) * 0.1),
                                        dist + radius * 8 + 100)
                    tiles.append(_gpu_capture(space, region, cam_world.inverted(), proj, w, h))
                else:
                    tiles.append(_render_capture(cam_world, fov, w, h, shading))
            labels.append(v)
    finally:
        for o in hidden:
            o.hide_set(False)
        if gpu_ok and saved_shading is not None:
            space.shading.type = saved_shading
            space.overlay.show_overlays = saved_overlay

    if len(tiles) == 1:
        px = tiles[0]
    else:
        th0, tw0 = tiles[0].shape[:2]
        tiles = [t if t.shape[:2] == (th0, tw0) else _resize(t, tw0, th0) for t in tiles]
        cols = 2 if len(tiles) <= 4 else 3
        px = _compose_grid(tiles, cols)
    return {"views": labels,
            "layout": "grid, left-to-right then top-to-bottom" if len(tiles) > 1 else "single",
            "framed": [o.name for o in targets][:20] if target is not None else "all visible",
            "image": _encode(px, format)}


def _resize(px, w, h):
    import numpy as np
    sh, sw = px.shape[:2]
    ys = (np.arange(h) * sh / h).astype(int)
    xs = (np.arange(w) * sw / w).astype(int)
    return px[ys][:, xs]


@tool("render_image", """
Render a final image through the scene camera with the scene's render
settings, optionally overriding engine/samples/resolution for this render
only. Saves to `filepath` (default: temp) and returns the image so you can
judge lighting/materials. For quick checks keep samples low (16-64).
""", "Render", mutates=False,
      engine=P.str("Override engine for this render: eevee, cycles, workbench"),
      samples=P.int("Override sample count", minimum=1, maximum=4096),
      resolution=P.list("Override [width, height] in pixels", {"type": "integer"}),
      percentage=P.int("Override resolution percentage", minimum=1, maximum=400),
      camera=P.str("Camera object to render through (default scene camera)"),
      filepath=P.str("Where to save (PNG/JPEG/EXR by extension). Default: temp file"),
      transparent=P.bool("Transparent background for this render"),
      frame=P.int("Frame to render (default current)"),
      return_image=P.bool("Return the image to you", True),
      max_size=P.int("Long edge of the returned preview", 1024, minimum=128, maximum=2048))
def render_image(engine=None, samples=None, resolution=None, percentage=None, camera=None,
                 filepath=None, transparent=None, frame=None, return_image=True,
                 max_size=1024):
    from .core import downscale_image_file
    scene = bpy.context.scene
    rd = scene.render
    if camera:
        cam = resolve_object(camera)
        if cam.type != "CAMERA":
            raise ToolError(f"{cam.name} is not a camera")
    else:
        cam = scene.camera
    if cam is None:
        raise ToolError("Scene has no camera. Create one with camera_setup first.")

    saved = {"camera": scene.camera, "engine": rd.engine, "rx": rd.resolution_x,
             "ry": rd.resolution_y, "pct": rd.resolution_percentage, "path": rd.filepath,
             "fmt": rd.image_settings.file_format, "transp": rd.film_transparent,
             "frame": scene.frame_current}
    saved_samples = None
    out = filepath and os.path.abspath(os.path.expanduser(filepath))
    out = out or temp_path(f"render_{time.strftime('%H%M%S')}.png")
    ext = os.path.splitext(out)[1].lower()
    fmt = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".exr": "OPEN_EXR",
           ".tif": "TIFF", ".tiff": "TIFF", ".webp": "WEBP"}.get(ext, "PNG")
    t0 = time.perf_counter()
    try:
        scene.camera = cam
        if engine:
            alias = {"eevee": "BLENDER_EEVEE", "cycles": "CYCLES",
                     "workbench": "BLENDER_WORKBENCH"}.get(engine.lower(), engine.upper())
            set_enum(rd, "engine", alias, ("BLENDER_EEVEE_NEXT",))
        if samples:
            if rd.engine == "CYCLES":
                saved_samples = ("cycles", scene.cycles.samples)
                scene.cycles.samples = samples
            elif "EEVEE" in rd.engine:
                saved_samples = ("eevee", scene.eevee.taa_render_samples)
                scene.eevee.taa_render_samples = samples
        if resolution:
            rd.resolution_x, rd.resolution_y = int(resolution[0]), int(resolution[1])
        if percentage:
            rd.resolution_percentage = percentage
        if transparent is not None:
            rd.film_transparent = transparent
        if frame is not None:
            scene.frame_set(frame)
        rd.image_settings.file_format = fmt
        rd.filepath = out
        os.makedirs(os.path.dirname(out), exist_ok=True)
        bpy.ops.render.render(write_still=True)
    finally:
        engine_used = rd.engine
        scene.camera = saved["camera"]
        rd.engine = saved["engine"]
        rd.resolution_x, rd.resolution_y = saved["rx"], saved["ry"]
        rd.resolution_percentage = saved["pct"]
        rd.filepath = saved["path"]
        rd.image_settings.file_format = saved["fmt"]
        rd.film_transparent = saved["transp"]
        if frame is not None:
            scene.frame_set(saved["frame"])
        if saved_samples:
            if saved_samples[0] == "cycles":
                scene.cycles.samples = saved_samples[1]
            else:
                scene.eevee.taa_render_samples = saved_samples[1]
    secs = time.perf_counter() - t0
    result = {"filepath": out, "engine": engine_used, "seconds": round(secs, 2),
              "camera": cam.name}
    if return_image and os.path.exists(out):
        preview = temp_path(f"preview_{time.time_ns()}.png")
        _, size = downscale_image_file(out, max_size, preview)
        result["preview_size"] = size
        result["image"] = png_payload(preview)
        os.remove(preview)
    return result


@tool("analyze_mesh", """
Topology and production-readiness audit for mesh objects: tri/quad/n-gon
counts, non-manifold and boundary edges, loose geometry, duplicate vertices,
zero-area faces, flipped/inconsistent normals, high-valence poles, missing
UVs, unapplied or negative scale, origin placement, surface area and volume.
Returns a list of issues with severity and the tool call that fixes each.
""", "Inspect", mutates=False,
      objects=P.objects("Mesh objects to audit", default="selected"),
      evaluated=P.bool("Analyze with modifiers applied", False),
      merge_distance=P.num("Distance under which vertices count as duplicates", 0.0001))
def analyze_mesh(objects="selected", evaluated=False, merge_distance=0.0001):
    objs = resolve_objects(objects, types={"MESH"})
    depsgraph = bpy.context.evaluated_depsgraph_get()
    reports = []
    for o in objs[:50]:
        bm = bmesh.new()
        if evaluated:
            bm.from_object(o, depsgraph)
        else:
            bm.from_mesh(o.data)
        bm.verts.ensure_lookup_table()
        issues = []

        def issue(sev, text, fix=None):
            entry = {"severity": sev, "issue": text}
            if fix:
                entry["fix"] = fix
            issues.append(entry)

        nfaces = len(bm.faces)
        tris = sum(1 for f in bm.faces if len(f.verts) == 3)
        quads = sum(1 for f in bm.faces if len(f.verts) == 4)
        ngons = nfaces - tris - quads
        non_manifold = [e for e in bm.edges if not e.is_manifold and not e.is_boundary]
        boundary = [e for e in bm.edges if e.is_boundary]
        loose_v = [v for v in bm.verts if not v.link_edges]
        loose_e = [e for e in bm.edges if not e.link_faces]
        zero_faces = [f for f in bm.faces if f.calc_area() < 1e-10]
        poles = [v for v in bm.verts if len(v.link_edges) > 6]
        inconsistent = [e for e in bm.edges if e.is_manifold and not e.is_contiguous]

        size = len(bm.verts)
        kd = mathutils.kdtree.KDTree(size)
        for i, v in enumerate(bm.verts):
            kd.insert(v.co, i)
        kd.balance()
        dupes = 0
        for i, v in enumerate(bm.verts):
            for (_, j, _) in kd.find_range(v.co, merge_distance):
                if j > i:
                    dupes += 1
                    break

        closed = not boundary and not non_manifold and nfaces > 0
        volume = bm.calc_volume(signed=True) if closed else None
        area = sum(f.calc_area() for f in bm.faces)
        sx, sy, sz = o.scale

        if non_manifold:
            issue("error", f"{len(non_manifold)} non-manifold edges (3+ faces share an edge)",
                  "edit_mesh op=merge_by_distance, or inspect with look isolate=true")
        if inconsistent:
            issue("error", f"{len(inconsistent)} edges with inconsistent normals",
                  "edit_mesh op=recalculate_normals")
        if closed and volume is not None and volume < 0:
            issue("error", "Normals point inward (negative signed volume)",
                  "edit_mesh op=recalculate_normals")
        if dupes:
            issue("warning", f"{dupes} duplicate vertices within {merge_distance}",
                  f"edit_mesh op=merge_by_distance distance={merge_distance}")
        if loose_v or loose_e:
            issue("warning", f"{len(loose_v)} loose verts, {len(loose_e)} loose edges",
                  "edit_mesh op=delete_loose")
        if zero_faces:
            issue("warning", f"{len(zero_faces)} zero-area faces", "edit_mesh op=dissolve_degenerate")
        if ngons:
            issue("info", f"{ngons} n-gons (5+ sided faces); may shade badly under subdivision",
                  "edit_mesh op=triangulate or op=quadify")
        if poles:
            issue("info", f"{len(poles)} vertices with 7+ edges (poles) can pinch when smoothed")
        if boundary and nfaces:
            issue("info", f"Open mesh: {len(boundary)} boundary edges (fine for planes, bad for solids)",
                  "edit_mesh op=fill_holes")
        if o.data.uv_layers.__len__() == 0 and nfaces:
            issue("warning", "No UV map: image textures will not map correctly",
                  "edit_mesh op=smart_uv_project")
        if min(sx, sy, sz) < 0:
            issue("warning", "Negative scale: flips normals and breaks some modifiers",
                  "transform_objects apply=['scale']")
        elif max(abs(sx - 1), abs(sy - 1), abs(sz - 1)) > 1e-4:
            issue("info", f"Unapplied scale {r(o.scale, 3)}: bevel/solidify/array behave unevenly",
                  "transform_objects apply=['scale']")
        bb = world_bbox([o])
        if bb:
            center = (bb[0] + bb[1]) / 2
            extent = max((bb[1] - bb[0]).length, 1e-6)
            if (o.matrix_world.translation - center).length > extent * 1.5:
                issue("info", "Origin is far from the geometry",
                      "set_origin mode=geometry")
        bm.free()
        reports.append({
            "object": o.name,
            "stats": {"verts": size, "faces": nfaces, "tris": tris, "quads": quads, "ngons": ngons,
                      "surface_area": r(area, 4), "volume": r(abs(volume), 4) if volume else None,
                      "closed": closed},
            "issues": issues,
            "verdict": "clean" if not issues else (
                "has errors" if any(i["severity"] == "error" for i in issues) else "minor issues"),
        })
    return {"reports": reports}


def _bvh(obj, depsgraph):
    bm = bmesh.new()
    bm.from_object(obj, depsgraph)
    bm.transform(obj.matrix_world)
    tree = BVHTree.FromBMesh(bm)
    verts = [v.co.copy() for v in bm.verts]
    bm.free()
    return tree, verts


@tool("measure", """
Spatial facts. For each object: world bounds, size, lowest point and whether
it floats above / sinks below whatever is under it. With `against`, also the
origin distance, closest surface-to-surface gap, per-axis bounding-box gaps
and whether the meshes actually intersect. Use to verify placement before
claiming something 'sits on' or 'touches' something else.
""", "Inspect", mutates=False,
      objects=P.objects("Objects to measure", default="selected"),
      against=P.objects("Optional second set to compare with"),
      ground_z=P.num("Ground height used when nothing is below an object", 0.0))
def measure(objects="selected", against=None, ground_z=0.0):
    objs = resolve_objects(objects)
    depsgraph = bpy.context.evaluated_depsgraph_get()
    scene = bpy.context.scene
    rows = []
    for o in objs[:40]:
        bb = world_bbox([o])
        lo, hi = bb
        row = {"name": o.name, "bounds_min": r(lo), "bounds_max": r(hi), "size": r(hi - lo),
               "lowest_z": r(lo.z)}
        start = Vector(((lo.x + hi.x) / 2, (lo.y + hi.y) / 2, lo.z - 1e-4))
        support_z, support = ground_z, "ground plane"
        try:
            hit, loc, _n, _i, hit_obj, _m = scene.ray_cast(depsgraph, start, Vector((0, 0, -1)))
        except TypeError:
            hit = False
        if hit and hit_obj is not None and hit_obj.name != o.name:
            support_z, support = loc.z, hit_obj.name
        row["support"] = support
        row["gap_to_support"] = r(lo.z - support_z, 4)
        gap = lo.z - support_z
        row["contact"] = ("resting" if abs(gap) < 0.002 else
                          "floating" if gap > 0 else "sunk/intersecting")
        rows.append(row)
    result = {"objects": rows}

    if against is not None:
        others = resolve_objects(against)
        pairs = []
        for a in objs[:10]:
            for b in others[:10]:
                if a == b:
                    continue
                pa, pb = a.matrix_world.translation, b.matrix_world.translation
                (alo, ahi), (blo, bhi) = world_bbox([a]), world_bbox([b])
                axis_gap = [r(max(blo[i] - ahi[i], alo[i] - bhi[i]), 4) for i in range(3)]
                pair = {"a": a.name, "b": b.name, "origin_distance": r((pa - pb).length),
                        "bbox_gap_xyz": axis_gap,
                        "bboxes_overlap": all(g < 0 for g in axis_gap)}
                if a.type in GEOMETRY_TYPES and b.type in GEOMETRY_TYPES:
                    ta, va = _bvh(a, depsgraph)
                    tb, vb = _bvh(b, depsgraph)
                    pair["meshes_intersect"] = bool(ta.overlap(tb))
                    step = max(1, len(va) // 3000)
                    best = float("inf")
                    for v in va[::step]:
                        hitp = tb.find_nearest(v)
                        if hitp[0] is not None:
                            best = min(best, hitp[3])
                    step = max(1, len(vb) // 3000)
                    for v in vb[::step]:
                        hitp = ta.find_nearest(v)
                        if hitp[0] is not None:
                            best = min(best, hitp[3])
                    pair["closest_surface_distance"] = 0.0 if pair["meshes_intersect"] else r(best, 4)
                pairs.append(pair)
        result["pairs"] = pairs
    return result


@tool("raycast", """
Cast a ray into the scene and report the first hit (object, point, normal,
distance). Use to find surface heights for placement or check line of sight.
""", "Inspect", mutates=False,
      origin=P.vec3("Ray start", required=True),
      direction=P.vec3("Ray direction", [0, 0, -1]),
      max_distance=P.num("Max ray length", 10000.0))
def raycast(origin, direction=(0, 0, -1), max_distance=10000.0):
    depsgraph = bpy.context.evaluated_depsgraph_get()
    d = Vector(direction)
    if d.length < 1e-9:
        raise ToolError("direction must be non-zero")
    hit, loc, normal, _idx, obj, _m = bpy.context.scene.ray_cast(
        depsgraph, Vector(origin), d.normalized(), distance=max_distance)
    if not hit:
        return {"hit": False}
    return {"hit": True, "object": obj.name, "location": r(loc), "normal": r(normal),
            "distance": r((loc - Vector(origin)).length)}
