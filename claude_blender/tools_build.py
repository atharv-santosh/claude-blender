import math
import random

import bmesh
import bpy
import mathutils
from mathutils import Matrix, Vector

from .core import (
    P, ToolError, bbox_info, euler_deg, link_to_collection, r,
    resolve_object, resolve_objects, select_only, set_enum, summarize_obj, tool,
    vec3, window_override, world_bbox,
)

PRIMITIVES = ["cube", "rounded_cube", "plane", "grid", "circle", "uv_sphere", "ico_sphere",
              "cylinder", "cone", "torus", "capsule", "monkey", "text", "empty", "curve",
              "tube"]
KIND_ALIASES = {"sphere": "uv_sphere", "ball": "uv_sphere", "orb": "uv_sphere",
                "icosphere": "ico_sphere", "box": "cube", "block": "cube",
                "rounded_box": "rounded_cube", "suzanne": "monkey", "ring": "torus",
                "donut": "torus", "pill": "capsule", "line": "curve", "path": "curve",
                "pipe": "tube", "disc": "circle", "disk": "circle", "floor": "plane",
                "quad": "plane", "null": "empty", "font": "text", "pyramid": "cone"}


def _mesh_from_bmesh(name, bm):
    me = bpy.data.meshes.new(name)
    bm.to_mesh(me)
    bm.free()
    return me


def _build_bmesh(kind, segments, rings, radius_ratio):
    bm = bmesh.new()
    if kind in ("cube", "rounded_cube"):
        bmesh.ops.create_cube(bm, size=1.0)
    elif kind == "plane":
        bmesh.ops.create_grid(bm, x_segments=1, y_segments=1, size=0.5)
    elif kind == "grid":
        bmesh.ops.create_grid(bm, x_segments=segments, y_segments=segments, size=0.5)
    elif kind == "circle":
        bmesh.ops.create_circle(bm, cap_ends=True, segments=segments, radius=0.5)
    elif kind == "uv_sphere":
        bmesh.ops.create_uvsphere(bm, u_segments=segments, v_segments=rings, radius=0.5)
    elif kind == "ico_sphere":
        bmesh.ops.create_icosphere(bm, subdivisions=max(1, min(7, rings)), radius=0.5)
    elif kind == "cylinder":
        bmesh.ops.create_cone(bm, cap_ends=True, segments=segments, radius1=0.5, radius2=0.5,
                              depth=1.0)
    elif kind == "cone":
        bmesh.ops.create_cone(bm, cap_ends=True, segments=segments, radius1=0.5,
                              radius2=0.5 * radius_ratio, depth=1.0)
    elif kind == "monkey":
        bmesh.ops.create_monkey(bm)
        bmesh.ops.scale(bm, vec=(0.5, 0.5, 0.5), verts=bm.verts)
    elif kind == "capsule":
        bmesh.ops.create_uvsphere(bm, u_segments=segments, v_segments=rings | 1, radius=0.25)
        for v in bm.verts:
            v.co.z += 0.25 if v.co.z > 1e-6 else (-0.25 if v.co.z < -1e-6 else 0)
    elif kind == "torus":
        major, minor = 0.5 - 0.5 * radius_ratio * 0.5, 0.5 * radius_ratio * 0.5
        ring_verts = []
        for i in range(segments):
            a = 2 * math.pi * i / segments
            ring = []
            for j in range(max(3, rings)):
                b = 2 * math.pi * j / max(3, rings)
                x = (major + minor * math.cos(b)) * math.cos(a)
                y = (major + minor * math.cos(b)) * math.sin(a)
                ring.append(bm.verts.new((x, y, minor * math.sin(b))))
            ring_verts.append(ring)
        n = max(3, rings)
        for i in range(segments):
            for j in range(n):
                a, b = ring_verts[i], ring_verts[(i + 1) % segments]
                bm.faces.new((a[j], b[j], b[(j + 1) % n], a[(j + 1) % n]))
    else:
        raise ToolError(f"Unknown primitive {kind!r}")
    return bm


def _shade(obj, smooth, angle_deg=30):
    me = obj.data
    if not isinstance(me, bpy.types.Mesh):
        return
    if smooth:
        if hasattr(me, "shade_smooth"):
            me.shade_smooth()
        else:
            for p in me.polygons:
                p.use_smooth = True
        if angle_deg and hasattr(me, "set_sharp_from_angle"):
            me.set_sharp_from_angle(angle=math.radians(angle_deg))
    else:
        if hasattr(me, "shade_flat"):
            me.shade_flat()
        else:
            for p in me.polygons:
                p.use_smooth = False


def assign_material(obj, mat, slot=None):
    if obj.data is None or not hasattr(obj.data, "materials"):
        return False
    if slot is None:
        if obj.data.materials:
            obj.data.materials[0] = mat
            for s in obj.material_slots:
                if s.link == "OBJECT":
                    s.material = mat
        else:
            obj.data.materials.append(mat)
    else:
        while len(obj.data.materials) <= slot:
            obj.data.materials.append(None)
        obj.data.materials[slot] = mat
    return True


def _resolve_material(spec):
    from .tools_look import material_from_spec
    return material_from_spec(spec)


def _single_user_data(obj):
    if obj.data is not None and obj.data.users > 1:
        obj.data = obj.data.copy()


def bake_origin_bottom(obj):
    me = obj.data
    if not isinstance(me, bpy.types.Mesh) or not me.vertices:
        return
    zs = [v.co.z for v in me.vertices]
    xs = [v.co.x for v in me.vertices]
    ys = [v.co.y for v in me.vertices]
    offset = Vector(((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2, min(zs)))
    me.transform(Matrix.Translation(-offset))


def apply_transform_data(obj, loc=False, rot=False, scale=False):
    if obj.data is None or not hasattr(obj.data, "transform"):
        raise ToolError(f"{obj.name} ({obj.type}) has no transformable data")
    _single_user_data(obj)
    basis = obj.matrix_basis.copy()
    l, q, s = basis.decompose()
    keep = Matrix.LocRotScale(None if loc else l, None if rot else q, None if scale else s)
    applied = keep.inverted() @ basis
    obj.data.transform(applied)
    obj.matrix_basis = keep
    for child in obj.children:
        child.matrix_parent_inverse = applied @ child.matrix_parent_inverse
    if obj.type == "MESH":
        obj.data.update()


def apply_modifiers_up_to(obj, mod=None):
    if obj.type != "MESH":
        raise ToolError(f"{obj.name} is a {obj.type}; use modify_stack action=convert_to_mesh")
    mods = list(obj.modifiers)
    if not mods:
        return []
    idx = len(mods) - 1 if mod is None else mods.index(mod)
    states = [m.show_viewport for m in mods]
    try:
        for i, m in enumerate(mods):
            m.show_viewport = i <= idx and (states[i] or i == idx)
        dg = bpy.context.evaluated_depsgraph_get()
        dg.update()
        ev = obj.evaluated_get(dg)
        new_me = bpy.data.meshes.new_from_object(ev, preserve_all_data_layers=True, depsgraph=dg)
    finally:
        for m, s in zip(mods, states):
            m.show_viewport = s
    applied = [m.name for m in mods[:idx + 1]]
    for m in mods[:idx + 1]:
        obj.modifiers.remove(m)
    old = obj.data
    new_me.name = old.name
    obj.data = new_me
    if old.users == 0:
        bpy.data.meshes.remove(old)
    return applied


@tool("create_object", f"""
Create a primitive with real dimensions baked into the mesh (scale stays 1).
kind: {', '.join(PRIMITIVES)}. `size` is the [x,y,z] bounding size in meters
(cube defaults to 1m). origin='bottom' puts the pivot at the base so
`location` is where it stands. Optional in the same call: material (existing
name, preset like 'brushed_metal', or a color), bevel, subdivision, smooth
shading, parent, collection. text: `text` is the string. curve/tube: `points`
is the path; tube gets a round profile of `radius`.
""", "Build",
      kind=P.str(f"Primitive kind: {', '.join(PRIMITIVES)} (common synonyms like "
                 f"sphere/box/pipe work too)", required=True),
      name=P.str("Object name"),
      location=P.vec3("World location", [0, 0, 0]),
      rotation=P.vec3("Rotation in degrees (XYZ)"),
      size=P.vec3("Bounding size [x,y,z] in meters"),
      radius=P.num("Radius shortcut for round kinds (sets size to 2r); tube profile radius"),
      origin=P.str("Pivot placement", "center", enum=["center", "bottom"]),
      segments=P.int("Radial segments / grid subdivisions", 32, minimum=3, maximum=512),
      rings=P.int("Rings (uv_sphere/torus minor), subdivisions (ico_sphere)", 16, minimum=2,
                  maximum=256),
      radius_ratio=P.num("cone: top/bottom radius ratio; torus: thickness 0-1", 0.0),
      material=P.str("Material name, preset name or color"),
      color=P.color("Quick solid color (creates a simple material)"),
      bevel=P.num("Bevel width in meters (adds a Bevel modifier)"),
      bevel_segments=P.int("Bevel segments", 3, minimum=1, maximum=16),
      subdivision=P.int("Subdivision Surface levels", minimum=0, maximum=6),
      smooth=P.bool("Shade smooth (with 30-degree auto sharp edges)"),
      text=P.str("Text body for kind=text"),
      extrude=P.num("Text/curve extrusion depth"),
      points=P.list("Control points for kind=curve/tube", {"type": "array", "items": {"type": "number"}}),
      closed=P.bool("Close the curve into a loop", False),
      collection=P.str("Collection to put it in (created if missing)"),
      parent=P.str("Parent object"))
def create_object(kind, name=None, location=(0, 0, 0), rotation=None, size=None, radius=None,
                  origin="center", segments=32, rings=16, radius_ratio=0.0, material=None,
                  color=None, bevel=None, bevel_segments=3, subdivision=None, smooth=None,
                  text=None, extrude=None, points=None, closed=False, collection=None,
                  parent=None):
    kind = kind.lower().strip().replace(" ", "_").replace("-", "_")
    kind = KIND_ALIASES.get(kind, kind)
    if kind not in PRIMITIVES:
        raise ToolError(f"Unknown kind {kind!r}. Kinds: {PRIMITIVES}")
    name = name or kind.replace("_", " ").title().replace(" ", "")
    if radius is not None and size is None and kind not in ("tube", "curve"):
        size = [radius * 2] * 3
        if kind in ("cylinder", "cone", "capsule"):
            size = [radius * 2, radius * 2, radius * 4 if kind == "capsule" else radius * 2]

    if kind == "empty":
        obj = bpy.data.objects.new(name, None)
        obj.empty_display_type = "PLAIN_AXES"
        obj.empty_display_size = (max(size) / 2) if size else 0.5
    elif kind == "text":
        cu = bpy.data.curves.new(name, "FONT")
        cu.body = text or name
        cu.align_x, cu.align_y = "CENTER", "CENTER"
        cu.extrude = extrude if extrude is not None else 0.02
        cu.size = size[2] if size else 1.0
        obj = bpy.data.objects.new(name, cu)
    elif kind in ("curve", "tube"):
        if not points or len(points) < 2:
            raise ToolError(f"kind={kind} needs at least 2 `points`")
        cu = bpy.data.curves.new(name, "CURVE")
        cu.dimensions = "3D"
        spl = cu.splines.new("BEZIER")
        spl.bezier_points.add(len(points) - 1)
        for bp, p in zip(spl.bezier_points, points):
            bp.co = vec3(p, "point")
            bp.handle_left_type = bp.handle_right_type = "AUTO"
        spl.use_cyclic_u = closed
        if kind == "tube":
            cu.bevel_depth = radius or 0.05
            cu.bevel_resolution = 6
            cu.use_fill_caps = True
        elif extrude:
            cu.extrude = extrude
        obj = bpy.data.objects.new(name, cu)
    else:
        bm = _build_bmesh(kind, segments, rings, radius_ratio if radius_ratio else (
            0.5 if kind == "torus" else 0.0))
        me = _mesh_from_bmesh(name, bm)
        if size is not None:
            sz = vec3(size, "size")
            if kind in ("plane", "grid", "circle"):
                sz.z = 1.0
            dims = Vector((
                max(v.co.x for v in me.vertices) - min(v.co.x for v in me.vertices),
                max(v.co.y for v in me.vertices) - min(v.co.y for v in me.vertices),
                max(v.co.z for v in me.vertices) - min(v.co.z for v in me.vertices),
            ))
            sc = [sz[i] / dims[i] if dims[i] > 1e-9 else 1.0 for i in range(3)]
            me.transform(Matrix.Diagonal((*sc, 1.0)))
        obj = bpy.data.objects.new(name, me)
        if origin == "bottom":
            bake_origin_bottom(obj)
        if me.uv_layers.__len__() == 0:
            me.uv_layers.new(name="UVMap")

    link_to_collection(obj, collection)
    obj.location = vec3(location, "location")
    if rotation is not None:
        obj.rotation_euler = euler_deg(rotation)
    if parent:
        par = resolve_object(parent)
        obj.parent = par
        obj.matrix_parent_inverse = par.matrix_world.inverted()

    if obj.type == "MESH":
        if kind == "rounded_cube" and bevel is None:
            bevel = min(obj.dimensions) * 0.08 if obj.dimensions.length else 0.04
        if bevel:
            mod = obj.modifiers.new("Bevel", "BEVEL")
            mod.width = bevel
            mod.segments = bevel_segments
            mod.limit_method = "ANGLE"
            if hasattr(mod, "harden_normals"):
                mod.harden_normals = smooth is not False
            smooth = True if smooth is None else smooth
        if subdivision:
            mod = obj.modifiers.new("Subdivision", "SUBSURF")
            mod.levels = subdivision
            mod.render_levels = subdivision
            smooth = True if smooth is None else smooth
        if smooth is None:
            smooth = kind in ("uv_sphere", "ico_sphere", "cylinder", "cone", "torus", "capsule",
                              "monkey")
        _shade(obj, smooth)

    mat = None
    if material or color is not None:
        mat = _resolve_material(material or color)
        if color is not None and material:
            from .tools_look import set_principled
            set_principled(mat, base_color=color)
        assign_material(obj, mat)

    select_only([obj])
    bpy.context.view_layer.update()
    return {"created": [summarize_obj(obj)], "dimensions": r(obj.dimensions),
            "material": mat.name if mat else None, "bounds": bbox_info([obj])}


def _point_or_object(spec):
    if isinstance(spec, str):
        return resolve_object(spec).matrix_world.translation.copy()
    return vec3(spec, "target")


@tool("transform_objects", """
Move/rotate/scale objects. mode='set' assigns values, 'add' offsets them
(rotation in degrees). pivot='group' rotates/scales a multi-selection around
its shared center instead of each origin. Extras that compose in one call:
look_at (object name or point; aims -Z for cameras/lights, +Y otherwise),
drop_to_surface (slide down until the bottom rests on whatever is below, or
the ground), apply (bake 'location'/'rotation'/'scale' into the mesh).
""", "Build",
      objects=P.objects("Objects to transform", required=True),
      location=P.vec3("Location (set) or offset (add)"),
      rotation=P.vec3("Rotation degrees XYZ (set) or delta (add)"),
      scale=P.vec3("Scale (set) or multiplier (add)"),
      mode=P.str("How values combine", "set", enum=["set", "add"]),
      pivot=P.str("Rotation/scale pivot", "individual", enum=["individual", "group"]),
      look_at=P.objects("Object name or [x,y,z] to aim at"),
      drop_to_surface=P.bool("Lower each object until it rests on the surface below", False),
      ground_z=P.num("Ground height when nothing is below", 0.0),
      apply=P.list("Bake transform parts into data: location, rotation, scale",
                   {"type": "string", "enum": ["location", "rotation", "scale"]}))
def transform_objects(objects, location=None, rotation=None, scale=None, mode="set",
                      pivot="individual", look_at=None, drop_to_surface=False, ground_z=0.0,
                      apply=None):
    objs = resolve_objects(objects)
    group_center = None
    if pivot == "group":
        lo, hi = world_bbox(objs)
        group_center = (lo + hi) / 2

    for o in objs:
        if location is not None:
            loc = vec3(location, "location")
            o.location = (o.location + loc) if mode == "add" else loc
        if rotation is not None:
            rot = euler_deg(rotation)
            if pivot == "group" and mode == "add":
                m = rot.to_matrix().to_4x4()
                T = Matrix.Translation(group_center)
                o.matrix_world = T @ m @ T.inverted() @ o.matrix_world
            elif mode == "add":
                o.rotation_euler = mathutils.Euler(
                    [a + b for a, b in zip(o.rotation_euler, rot)], o.rotation_euler.order)
            else:
                o.rotation_euler = rot
        if scale is not None:
            sc = vec3(scale, "scale")
            if pivot == "group":
                m = Matrix.Diagonal((*sc, 1.0))
                T = Matrix.Translation(group_center)
                o.matrix_world = T @ m @ T.inverted() @ o.matrix_world
            elif mode == "add":
                o.scale = Vector([a * b for a, b in zip(o.scale, sc)])
            else:
                o.scale = sc
    bpy.context.view_layer.update()

    if look_at is not None:
        if isinstance(look_at, str):
            target = resolve_object(look_at)
            lo, hi = world_bbox([target])
            tp = (lo + hi) / 2
        else:
            tp = vec3(look_at, "look_at")
        for o in objs:
            eye = o.matrix_world.translation
            d = tp - eye
            if d.length < 1e-9:
                continue
            q = d.to_track_quat("-Z", "Y") if o.type in {"CAMERA", "LIGHT"} else \
                d.to_track_quat("Y", "Z")
            o.rotation_mode = "XYZ"
            o.rotation_euler = q.to_euler()
        bpy.context.view_layer.update()

    results = []
    if drop_to_surface:
        depsgraph = bpy.context.evaluated_depsgraph_get()
        scene = bpy.context.scene
        moving = set(objs)
        for o in sorted(objs, key=lambda x: world_bbox([x])[0].z):
            lo, hi = world_bbox([o])
            best = ground_z
            for fx in (0.5, 0.2, 0.8):
                for fy in (0.5, 0.2, 0.8):
                    start = Vector((lo.x + (hi.x - lo.x) * fx, lo.y + (hi.y - lo.y) * fy,
                                    lo.z - 1e-4))
                    hit, loc, _n, _i, hobj, _m = scene.ray_cast(depsgraph, start,
                                                                Vector((0, 0, -1)))
                    if hit and hobj is not None and hobj.original not in moving:
                        best = max(best, loc.z)
            o.location.z += best - lo.z
            results.append({"name": o.name, "rests_at_z": r(best)})
        bpy.context.view_layer.update()

    if apply:
        for o in objs:
            apply_transform_data(o, "location" in apply, "rotation" in apply, "scale" in apply)
        bpy.context.view_layer.update()

    out = {"objects": [{"name": o.name, "location": r(o.location),
                        "rotation_deg": r([math.degrees(a) for a in o.rotation_euler], 2),
                        "scale": r(o.scale), "dimensions": r(o.dimensions)} for o in objs[:50]]}
    if results:
        out["dropped"] = results
    return out


@tool("set_origin", """
Move object origins without moving geometry. mode: geometry (center of
bounds), bottom (bottom-center of bounds -- best for things that stand on the
floor), world (origin at 0,0,0), point (at `point`).
""", "Build",
      objects=P.objects("Objects", required=True),
      mode=P.str("Where the origin goes", "bottom", enum=["geometry", "bottom", "world", "point"]),
      point=P.vec3("World point for mode=point"))
def set_origin(objects, mode="bottom", point=None):
    objs = resolve_objects(objects)
    done = []
    for o in objs:
        if o.data is None or not hasattr(o.data, "transform"):
            continue
        _single_user_data(o)
        lo, hi = world_bbox([o])
        if mode == "geometry":
            target = (lo + hi) / 2
        elif mode == "bottom":
            target = Vector(((lo.x + hi.x) / 2, (lo.y + hi.y) / 2, lo.z))
        elif mode == "world":
            target = Vector((0, 0, 0))
        else:
            if point is None:
                raise ToolError("mode=point needs `point`")
            target = vec3(point)
        delta_local = o.matrix_world.inverted() @ target
        o.data.transform(Matrix.Translation(-delta_local))
        for child in o.children:
            child.matrix_parent_inverse = Matrix.Translation(-delta_local) @ child.matrix_parent_inverse
        o.matrix_world = o.matrix_world @ Matrix.Translation(delta_local)
        done.append({"name": o.name, "origin": r(target)})
    bpy.context.view_layer.update()
    return {"modified": done}


def _duplicate(o, linked=False, collection=None):
    dup = o.copy()
    if o.data is not None and not linked:
        dup.data = o.data.copy()
    if collection:
        link_to_collection(dup, collection)
    else:
        for c in o.users_collection:
            c.objects.link(dup)
    return dup


@tool("duplicate_objects", """
Duplicate objects `count` times with cumulative offset/rotation/scale steps --
a quick array of independent objects (e.g. a row of posts, a spiral
staircase via rotation + offset with pivot). linked=true shares mesh data.
""", "Build",
      objects=P.objects("Objects to duplicate", required=True),
      count=P.int("Number of copies", 1, minimum=1, maximum=2000),
      offset=P.vec3("Location step per copy", [0, 0, 0]),
      rotation=P.vec3("Rotation step per copy in degrees", [0, 0, 0]),
      scale=P.num("Uniform scale multiplier per copy", 1.0),
      pivot=P.vec3("Rotate copies around this world point instead of their origins"),
      linked=P.bool("Share mesh data with the original", False),
      collection=P.str("Collection for the copies"))
def duplicate_objects(objects, count=1, offset=(0, 0, 0), rotation=(0, 0, 0), scale=1.0,
                      pivot=None, linked=False, collection=None):
    objs = resolve_objects(objects)
    off, rot = vec3(offset), euler_deg(rotation)
    created = []
    for o in objs:
        base = o.matrix_world.copy()
        for i in range(1, count + 1):
            dup = _duplicate(o, linked, collection)
            step_rot = mathutils.Euler([a * i for a in rot]).to_matrix().to_4x4()
            if pivot is not None:
                T = Matrix.Translation(vec3(pivot))
                m = T @ step_rot @ T.inverted() @ base
                m = Matrix.Translation(off * i) @ m
                loc, q, s = m.decompose()
                dup.matrix_world = Matrix.LocRotScale(loc, q, s * (scale ** i))
            else:
                loc, q, s = base.decompose()
                q2 = (step_rot.to_quaternion() @ q)
                dup.matrix_world = Matrix.LocRotScale(loc + off * i, q2, s * (scale ** i))
            dup.name = f"{o.name}.{i:03d}"
            created.append(dup)
    bpy.context.view_layer.update()
    return {"created": [summarize_obj(d) for d in created[:60]], "count": len(created)}


@tool("arrange_objects", """
Lay objects out in a pattern. pattern: grid, line, circle, arc, spiral,
random (inside `area`), stack (on top of each other). If `count` exceeds the
number of objects, copies of them are made cyclically (linked=true shares
mesh data). Optional per-object randomness: jitter (meters), random_rotation
(max degrees about Z), random_scale ([min,max]). face_center turns objects
toward the pattern center (circle/arc/spiral). align_bottom keeps every
object's base on `center`'s Z.
""", "Build",
      objects=P.objects("Objects to arrange (templates when count is larger)", required=True),
      pattern=P.str("Layout", "grid", enum=["grid", "line", "circle", "arc", "spiral",
                                            "random", "stack"]),
      count=P.int("Total objects wanted (copies made as needed)", minimum=1, maximum=5000),
      center=P.vec3("Pattern center / start", [0, 0, 0]),
      spacing=P.num("Distance between neighbors (grid/line/spiral/stack gap)", 2.0),
      columns=P.int("Grid columns (default: square-ish)", minimum=1),
      radius=P.num("Circle/arc/spiral radius", 5.0),
      arc_degrees=P.num("Arc sweep", 180.0),
      axis=P.str("Line direction", "x", enum=["x", "y", "z"]),
      area=P.vec3("Random pattern box size [x,y,z]", [10, 10, 0]),
      face_center=P.bool("Rotate to face the pattern center", False),
      jitter=P.num("Random location offset", 0.0),
      random_rotation=P.num("Random Z rotation up to this many degrees", 0.0),
      random_scale=P.list("Random uniform scale [min, max]", {"type": "number"}),
      align_bottom=P.bool("Put every object's base at center Z", True),
      seed=P.int("Random seed", 0),
      linked=P.bool("Copies share mesh data", True))
def arrange_objects(objects, pattern="grid", count=None, center=(0, 0, 0), spacing=2.0,
                    columns=None, radius=5.0, arc_degrees=180.0, axis="x", area=(10, 10, 0),
                    face_center=False, jitter=0.0, random_rotation=0.0, random_scale=None,
                    align_bottom=True, seed=0, linked=True):
    templates = resolve_objects(objects)
    rng = random.Random(seed)
    items = list(templates)
    n = count or len(items)
    i = 0
    while len(items) < n:
        items.append(_duplicate(templates[i % len(templates)], linked))
        i += 1
    items = items[:n] if count else items
    c = vec3(center)
    positions = []
    if pattern == "grid":
        cols = columns or math.ceil(math.sqrt(n))
        rows = math.ceil(n / cols)
        for k in range(n):
            rr, cc = divmod(k, cols)
            positions.append(c + Vector(((cc - (cols - 1) / 2) * spacing,
                                         ((rows - 1) / 2 - rr) * spacing, 0)))
    elif pattern == "line":
        ax = Vector({"x": (1, 0, 0), "y": (0, 1, 0), "z": (0, 0, 1)}[axis])
        for k in range(n):
            positions.append(c + ax * (k - (n - 1) / 2) * spacing)
    elif pattern in ("circle", "arc"):
        sweep = 360.0 if pattern == "circle" else arc_degrees
        for k in range(n):
            t = k / n if pattern == "circle" else (k / max(1, n - 1) - 0.5)
            a = math.radians(sweep * t) + (math.pi / 2 if pattern == "arc" else 0)
            positions.append(c + Vector((math.cos(a) * radius, math.sin(a) * radius, 0)))
    elif pattern == "spiral":
        a = 0.0
        for k in range(n):
            rad = radius * 0.15 + spacing * a / (2 * math.pi)
            positions.append(c + Vector((math.cos(a) * rad, math.sin(a) * rad, 0)))
            a += spacing / max(rad, 0.3)
    elif pattern == "random":
        ar = vec3(area)
        for _ in range(n):
            positions.append(c + Vector((rng.uniform(-ar.x / 2, ar.x / 2),
                                         rng.uniform(-ar.y / 2, ar.y / 2),
                                         rng.uniform(-ar.z / 2, ar.z / 2))))
    elif pattern == "stack":
        z = c.z
        for o in items:
            lo, hi = world_bbox([o])
            positions.append(Vector((c.x, c.y, z)))
            z += (hi.z - lo.z) + spacing * 0.0
    for o, p in zip(items, positions):
        if jitter:
            p = p + Vector((rng.uniform(-jitter, jitter), rng.uniform(-jitter, jitter), 0))
        o.location = p
        if face_center and pattern in ("circle", "arc", "spiral"):
            d = c - p
            o.rotation_euler.z = math.atan2(d.y, d.x) + math.pi / 2
        if random_rotation:
            o.rotation_euler.z += math.radians(rng.uniform(-random_rotation, random_rotation))
        if random_scale:
            s = rng.uniform(random_scale[0], random_scale[1] if len(random_scale) > 1 else
                            random_scale[0])
            o.scale = o.scale * s
    bpy.context.view_layer.update()
    if align_bottom or pattern == "stack":
        for o, p in zip(items, positions):
            lo, _ = world_bbox([o])
            o.location.z += p.z - lo.z
        bpy.context.view_layer.update()
    return {"objects": [summarize_obj(o) for o in items[:80]], "count": len(items),
            "bounds": bbox_info(items)}


@tool("align_distribute", """
Align objects along an axis (min/center/max edge of their bounds, matched to
the group or to a reference object) and/or distribute them with equal gaps.
""", "Build",
      objects=P.objects("Objects", required=True),
      axis=P.str("Axis", "x", enum=["x", "y", "z"]),
      align=P.str("Which bound to align", enum=["min", "center", "max"]),
      reference=P.str("Align to this object instead of the group bounds"),
      distribute=P.bool("Space objects evenly along axis", False),
      gap=P.num("Fixed gap between bounds when distributing (default: keep span)"))
def align_distribute(objects, axis="x", align=None, reference=None, distribute=False, gap=None):
    objs = resolve_objects(objects)
    ai = "xyz".index(axis)
    if align:
        ref_objs = [resolve_object(reference)] if reference else objs
        lo, hi = world_bbox(ref_objs)
        tgt = {"min": lo[ai], "max": hi[ai], "center": (lo[ai] + hi[ai]) / 2}[align]
        for o in objs:
            olo, ohi = world_bbox([o])
            cur = {"min": olo[ai], "max": ohi[ai], "center": (olo[ai] + ohi[ai]) / 2}[align]
            o.location[ai] += tgt - cur
        bpy.context.view_layer.update()
    if distribute and len(objs) > 1:
        boxes = sorted(((world_bbox([o]), o) for o in objs), key=lambda b: b[0][0][ai])
        widths = [b[0][1][ai] - b[0][0][ai] for b in boxes]
        start = boxes[0][0][0][ai]
        if gap is None:
            span = boxes[-1][0][1][ai] - start
            gap = (span - sum(widths)) / (len(boxes) - 1)
        pos = start
        for (bb, o), w in zip(boxes, widths):
            o.location[ai] += pos - bb[0][ai]
            pos += w + gap
        bpy.context.view_layer.update()
    return {"objects": [summarize_obj(o) for o in objs], "gap": r(gap) if gap else None}


def _gn_node(ng, kind, x, y, **props):
    n = ng.nodes.new(kind)
    n.location = (x, y)
    for k, v in props.items():
        setattr(n, k, v)
    return n


def _in(node, name, nth=0):
    hits = [s for s in node.inputs if s.name == name and getattr(s, "enabled", True)]
    if not hits:
        raise ToolError(f"Node {node.bl_idname} has no input {name!r}")
    return hits[min(nth, len(hits) - 1)]


def _out(node, name):
    hits = [s for s in node.outputs if s.name == name and getattr(s, "enabled", True)]
    if not hits:
        raise ToolError(f"Node {node.bl_idname} has no output {name!r}")
    return hits[0]


def _random_value(ng, data_type, lo, hi, seed, x, y):
    n = _gn_node(ng, "FunctionNodeRandomValue", x, y)
    n.data_type = data_type
    _in(n, "Min").default_value = lo
    _in(n, "Max").default_value = hi
    _in(n, "Seed").default_value = seed
    return n


def _compare(ng, op, b, x, y):
    n = _gn_node(ng, "FunctionNodeCompare", x, y)
    n.data_type = "FLOAT"
    n.operation = op
    _in(n, "B").default_value = b
    return n


@tool("scatter_on_surface", """
Scatter instances of `sources` across the surface of `target` with a
non-destructive Geometry Nodes setup on a new object (the target is not
modified; tweak later with modify_stack). Control: count or density,
min_distance (Poisson spacing), random scale range, random Z spin,
align_to_normal (0 = upright, 1 = follow the surface), max_slope_deg (only
flat-ish areas), z_range (only within heights). Several sources are picked at
random per point. Sources are hidden by default because they act as templates.
""", "Build",
      sources=P.objects("Objects to scatter", required=True),
      target=P.str("Surface object", required=True),
      count=P.int("Approximate number of instances", minimum=1, maximum=200000),
      density=P.num("Instances per square meter (used if count is not given)", 1.0),
      min_distance=P.num("Minimum spacing between instances (Poisson disk)", 0.0),
      scale_range=P.list("Random uniform scale [min, max]", {"type": "number"}, [0.8, 1.2]),
      random_rotation=P.num("Random spin about the local up axis, degrees", 360.0),
      align_to_normal=P.num("0 = stay upright, 1 = align to surface normal", 0.0,
                            minimum=0, maximum=1),
      max_slope_deg=P.num("Skip surface steeper than this (degrees from flat)"),
      z_range=P.list("Only scatter where world Z is within [min, max]", {"type": "number"}),
      seed=P.int("Random seed", 0),
      realize=P.bool("Realize instances into real geometry (heavier)", False),
      hide_sources=P.bool("Hide the source objects", True),
      name=P.str("Name of the scatter object"))
def scatter_on_surface(sources, target, count=None, density=1.0, min_distance=0.0,
                       scale_range=(0.8, 1.2), random_rotation=360.0, align_to_normal=0.0,
                       max_slope_deg=None, z_range=None, seed=0, realize=False,
                       hide_sources=True, name=None):
    srcs = resolve_objects(sources)
    tgt = resolve_object(target)
    if tgt.type != "MESH":
        raise ToolError(f"Target {tgt.name} must be a mesh")
    if tgt in srcs:
        raise ToolError("Target cannot also be a source")
    name = name or f"Scatter_{tgt.name}"

    dg = bpy.context.evaluated_depsgraph_get()
    bm = bmesh.new()
    bm.from_object(tgt, dg)
    bm.transform(tgt.matrix_world)
    area = sum(f.calc_area() for f in bm.faces)
    bm.free()
    if count:
        density = count / max(area, 1e-6)

    src_coll = bpy.data.collections.new(f"{name}_sources")
    bpy.context.scene.collection.children.link(src_coll)
    for s in srcs:
        if s.name not in src_coll.objects:
            src_coll.objects.link(s)
    for lc in bpy.context.view_layer.layer_collection.children:
        if lc.collection == src_coll:
            lc.exclude = True

    ng = bpy.data.node_groups.new(name, "GeometryNodeTree")
    ng.interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    ng.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    L = ng.links.new
    gout = _gn_node(ng, "NodeGroupOutput", 1400, 0)
    _gn_node(ng, "NodeGroupInput", -900, 0)

    info = _gn_node(ng, "GeometryNodeObjectInfo", -700, 0, transform_space="RELATIVE")
    _in(info, "Object").default_value = tgt

    dist = _gn_node(ng, "GeometryNodeDistributePointsOnFaces", -300, 0)
    if min_distance and min_distance > 0:
        dist.distribute_method = "POISSON"
        _in(dist, "Distance Min").default_value = min_distance
        _in(dist, "Density Max").default_value = density
    else:
        _in(dist, "Density").default_value = density
    _in(dist, "Seed").default_value = seed
    L(_out(info, "Geometry"), _in(dist, "Mesh"))

    masks = []
    if max_slope_deg is not None:
        normal = _gn_node(ng, "GeometryNodeInputNormal", -900, -300)
        sep = _gn_node(ng, "ShaderNodeSeparateXYZ", -700, -300)
        cmp = _compare(ng, "GREATER_EQUAL", math.cos(math.radians(max_slope_deg)), -500, -300)
        L(_out(normal, "Normal"), _in(sep, "Vector"))
        L(_out(sep, "Z"), _in(cmp, "A"))
        masks.append(cmp)
    if z_range:
        pos = _gn_node(ng, "GeometryNodeInputPosition", -900, -500)
        sep = _gn_node(ng, "ShaderNodeSeparateXYZ", -700, -500)
        L(_out(pos, "Position"), _in(sep, "Vector"))
        lo_c = _compare(ng, "GREATER_EQUAL", z_range[0], -500, -500)
        hi_c = _compare(ng, "LESS_EQUAL", z_range[1] if len(z_range) > 1 else 1e9, -500, -650)
        L(_out(sep, "Z"), _in(lo_c, "A"))
        L(_out(sep, "Z"), _in(hi_c, "A"))
        masks += [lo_c, hi_c]
    if masks:
        sel = _out(masks[0], "Result")
        for m in masks[1:]:
            band = _gn_node(ng, "FunctionNodeBooleanMath", -350, -400, operation="AND")
            L(sel, _in(band, "Boolean", 0))
            L(_out(m, "Result"), _in(band, "Boolean", 1))
            sel = _out(band, "Boolean")
        L(sel, _in(dist, "Selection"))

    coll = _gn_node(ng, "GeometryNodeCollectionInfo", -300, 400, transform_space="ORIGINAL")
    _in(coll, "Collection").default_value = src_coll
    _in(coll, "Separate Children").default_value = True
    _in(coll, "Reset Children").default_value = True

    inst = _gn_node(ng, "GeometryNodeInstanceOnPoints", 300, 0)
    L(_out(dist, "Points"), _in(inst, "Points"))
    L(_out(coll, "Instances"), _in(inst, "Instance"))
    _in(inst, "Pick Instance").default_value = True
    if len(srcs) > 1:
        pick = _random_value(ng, "INT", 0, len(srcs) - 1, seed + 7, 0, 300)
        L(_out(pick, "Value"), _in(inst, "Instance Index"))

    align = _gn_node(ng, "FunctionNodeAlignRotationToVector", 0, -150, axis="Z")
    _in(align, "Factor").default_value = align_to_normal
    L(_out(dist, "Normal"), _in(align, "Vector"))
    L(_out(align, "Rotation"), _in(inst, "Rotation"))

    lo_s, hi_s = (list(scale_range) + [scale_range[0]])[:2] if scale_range else (1.0, 1.0)
    sc = _random_value(ng, "FLOAT", lo_s, hi_s, seed + 3, 0, -350)
    L(_out(sc, "Value"), _in(inst, "Scale"))

    last = _out(inst, "Instances")
    if random_rotation:
        spin = _random_value(ng, "FLOAT", 0.0, math.radians(random_rotation), seed + 5,
                             300, -350)
        comb = _gn_node(ng, "ShaderNodeCombineXYZ", 500, -350)
        L(_out(spin, "Value"), _in(comb, "Z"))
        rot = _gn_node(ng, "GeometryNodeRotateInstances", 700, 0)
        L(last, _in(rot, "Instances"))
        L(_out(comb, "Vector"), _in(rot, "Rotation"))
        _in(rot, "Local Space").default_value = True
        last = _out(rot, "Instances")
    if realize:
        real = _gn_node(ng, "GeometryNodeRealizeInstances", 1000, 0)
        L(last, _in(real, "Geometry"))
        last = _out(real, "Geometry")
    L(last, gout.inputs[0])

    me = bpy.data.meshes.new(name)
    obj = bpy.data.objects.new(name, me)
    link_to_collection(obj)
    mod = obj.modifiers.new("Scatter", "NODES")
    mod.node_group = ng
    if not hide_sources:
        for lc in bpy.context.view_layer.layer_collection.children:
            if lc.collection == src_coll:
                lc.exclude = False

    dg = bpy.context.evaluated_depsgraph_get()
    dg.update()
    ev = obj.evaluated_get(dg)
    n_inst = 0
    try:
        n_inst = sum(1 for inst_ in dg.object_instances
                     if inst_.is_instance and inst_.parent and inst_.parent.original == obj)
    except Exception:
        pass
    if realize and ev.data is not None:
        n_inst = len(ev.data.vertices)
    return {"created": [summarize_obj(obj)], "node_group": ng.name,
            "source_collection": src_coll.name, "surface_area": r(area, 3),
            "density_per_m2": r(density, 4),
            "instances": n_inst if not realize else None,
            "realized_vertices": n_inst if realize else None}


@tool("delete_objects", """
Delete objects (optionally with their children) and purge data left unused.
""", "Build",
      objects=P.objects("Objects to delete", required=True),
      with_children=P.bool("Also delete children recursively", False),
      purge=P.bool("Remove meshes/materials that end up unused", True))
def delete_objects(objects, with_children=False, purge=True):
    objs = resolve_objects(objects)
    if with_children:
        extra = []
        for o in objs:
            extra.extend(o.children_recursive)
        objs = list(dict.fromkeys(objs + extra))
    names = [o.name for o in objs]
    datas = {o.data for o in objs if o.data is not None}
    for o in objs:
        bpy.data.objects.remove(o, do_unlink=True)
    purged = 0
    if purge:
        for d in datas:
            try:
                if d.users == 0:
                    for coll in (bpy.data.meshes, bpy.data.curves, bpy.data.lights,
                                 bpy.data.cameras):
                        if d.name in coll and coll[d.name] == d:
                            coll.remove(d)
                            purged += 1
                            break
            except ReferenceError:
                pass
        for m in list(bpy.data.materials):
            if m.users == 0:
                bpy.data.materials.remove(m)
                purged += 1
    return {"deleted": names, "purged_datablocks": purged}


@tool("organize", """
Scene housekeeping in one tool. action:
- move_to_collection / link_to_collection (collection created if missing)
- parent (to `parent`, keeping world transforms) / unparent
- group_under_empty (new empty at the group center becomes the parent)
- rename (`name`; use '{i}' for numbering, '{name}' for the old name)
- join (merge meshes into the first object)
- hide / show (viewport and render) / select (also makes first active)
- set_color (viewport object color, for solid-mode color coding)
""", "Build",
      objects=P.objects("Objects", required=True),
      action=P.str("What to do", required=True,
                   enum=["move_to_collection", "link_to_collection", "parent", "unparent",
                         "group_under_empty", "rename", "join", "hide", "show", "select",
                         "set_color"]),
      collection=P.str("Collection name"),
      parent=P.str("Parent object"),
      name=P.str("New name / pattern, or empty name for group_under_empty"),
      color=P.color("Viewport color for set_color"))
def organize(objects, action, collection=None, parent=None, name=None, color=None):
    objs = resolve_objects(objects)
    if action in ("move_to_collection", "link_to_collection"):
        if not collection:
            raise ToolError("collection is required")
        coll = bpy.data.collections.get(collection)
        if coll is None:
            coll = bpy.data.collections.new(collection)
            bpy.context.scene.collection.children.link(coll)
        for o in objs:
            if action == "move_to_collection":
                for c in list(o.users_collection):
                    c.objects.unlink(o)
            if o.name not in coll.objects:
                coll.objects.link(o)
        return {"modified": [o.name for o in objs], "collection": coll.name}
    if action == "parent":
        par = resolve_object(parent)
        for o in objs:
            if o == par:
                continue
            mw = o.matrix_world.copy()
            o.parent = par
            o.matrix_parent_inverse = par.matrix_world.inverted()
            o.matrix_world = mw
        return {"modified": [o.name for o in objs], "parent": par.name}
    if action == "unparent":
        for o in objs:
            mw = o.matrix_world.copy()
            o.parent = None
            o.matrix_world = mw
        return {"modified": [o.name for o in objs]}
    if action == "group_under_empty":
        lo, hi = world_bbox(objs)
        empty = bpy.data.objects.new(name or "Group", None)
        empty.empty_display_type = "CUBE"
        empty.empty_display_size = max((hi - lo).length / 2, 0.1)
        link_to_collection(empty, collection)
        empty.location = Vector(((lo.x + hi.x) / 2, (lo.y + hi.y) / 2, lo.z))
        bpy.context.view_layer.update()
        for o in objs:
            mw = o.matrix_world.copy()
            o.parent = empty
            o.matrix_parent_inverse = empty.matrix_world.inverted()
            o.matrix_world = mw
        return {"created": [summarize_obj(empty)], "children": [o.name for o in objs]}
    if action == "rename":
        if not name:
            raise ToolError("name is required")
        out = []
        for i, o in enumerate(objs, 1):
            old = o.name
            o.name = name.replace("{i}", f"{i:02d}").replace("{name}", old) if (
                "{" in name) else (name if len(objs) == 1 else f"{name}.{i:03d}")
            out.append([old, o.name])
        return {"renamed": out}
    if action == "join":
        meshes = [o for o in objs if o.type == "MESH"]
        if len(meshes) < 2:
            raise ToolError("join needs at least two mesh objects")
        base = meshes[0]
        _single_user_data(base)
        bm = bmesh.new()
        bm.from_mesh(base.data)
        inv = base.matrix_world.inverted()
        mat_index = {m.name: i for i, m in enumerate(base.data.materials) if m}
        for o in meshes[1:]:
            me = o.data.copy()
            me.transform(inv @ o.matrix_world)
            remap = []
            for m in o.data.materials:
                if m is None:
                    remap.append(0)
                    continue
                if m.name not in mat_index:
                    base.data.materials.append(m)
                    mat_index[m.name] = len(base.data.materials) - 1
                remap.append(mat_index[m.name])
            for p in me.polygons:
                if remap:
                    p.material_index = remap[min(p.material_index, len(remap) - 1)]
            bm.from_mesh(me)
            bpy.data.meshes.remove(me)
        bm.to_mesh(base.data)
        bm.free()
        joined = [o.name for o in meshes[1:]]
        for o in meshes[1:]:
            bpy.data.objects.remove(o, do_unlink=True)
        if name:
            base.name = name
        return {"modified": [base.name], "joined": joined}
    if action in ("hide", "show"):
        for o in objs:
            o.hide_set(action == "hide")
            o.hide_render = action == "hide"
        return {"modified": [o.name for o in objs], "hidden": action == "hide"}
    if action == "select":
        select_only(objs)
        return {"selected": [o.name for o in objs]}
    if action == "set_color":
        from .core import color4
        c = color4(color or "#FFFFFF")
        for o in objs:
            o.color = c
        return {"modified": [o.name for o in objs]}
    raise ToolError(f"Unknown action {action}")


@tool("boolean", """
Boolean `target` with one or more `cutters`: difference (carve), union,
intersect. Non-destructive by default (modifier); apply=true bakes it.
Cutters are hidden (display as wire) unless keep_cutters_visible.
delete_cutters only applies when apply=true.
""", "Build",
      target=P.str("Object that gets cut / merged", required=True),
      cutters=P.objects("Cutter objects", required=True),
      operation=P.str("Boolean operation", "difference",
                      enum=["difference", "union", "intersect"]),
      solver=P.str("Solver", "exact", enum=["exact", "fast", "manifold"]),
      apply=P.bool("Bake into the mesh", False),
      delete_cutters=P.bool("Delete cutters after applying", False),
      keep_cutters_visible=P.bool("Leave cutters visible", False))
def boolean(target, cutters, operation="difference", solver="exact", apply=False,
            delete_cutters=False, keep_cutters_visible=False):
    tgt = resolve_object(target)
    cuts = [c for c in resolve_objects(cutters) if c != tgt]
    if tgt.type != "MESH":
        raise ToolError("Boolean target must be a mesh")
    mods = []
    for c in cuts:
        m = tgt.modifiers.new(f"Bool_{c.name}", "BOOLEAN")
        m.operation = operation.upper()
        m.object = c
        set_enum(m, "solver", solver.upper(), ("EXACT",))
        mods.append(m)
        if not keep_cutters_visible:
            c.display_type = "WIRE"
            c.hide_render = True
    result = {"target": tgt.name, "cutters": [c.name for c in cuts],
              "modifiers": [m.name for m in mods]}
    if apply:
        before = len(tgt.data.polygons)
        apply_modifiers_up_to(tgt, mods[-1])
        result["applied"] = True
        result["faces_before_after"] = [before, len(tgt.data.polygons)]
        if delete_cutters:
            for c in cuts:
                bpy.data.objects.remove(c, do_unlink=True)
            result["deleted_cutters"] = True
        elif not keep_cutters_visible:
            for c in cuts:
                c.hide_set(True)
    return result


def _set_rna(owner, key, value, where):
    prop = owner.bl_rna.properties.get(key)
    if prop is None:
        valid = [p.identifier for p in owner.bl_rna.properties
                 if not p.is_readonly and p.identifier != "rna_type"]
        raise ToolError(f"{where} has no setting {key!r}. Settable: {valid}")
    if prop.type == "POINTER":
        if value is None:
            setattr(owner, key, None)
            return
        fixed = prop.fixed_type.bl_rna.identifier if prop.fixed_type else ""
        pool = {"Object": bpy.data.objects, "Collection": bpy.data.collections,
                "Material": bpy.data.materials, "Image": bpy.data.images,
                "Texture": bpy.data.textures, "NodeTree": bpy.data.node_groups,
                "GeometryNodeTree": bpy.data.node_groups}.get(fixed)
        if pool is None:
            raise ToolError(f"{where}.{key} points to {fixed}; can't set it by name")
        target = pool.get(value)
        if target is None:
            raise ToolError(f"{where}.{key}: no {fixed} named {value!r}")
        setattr(owner, key, target)
    elif prop.type == "ENUM":
        if getattr(prop, "is_enum_flag", False):
            setattr(owner, key, set(value if isinstance(value, list) else [value]))
        else:
            set_enum(owner, key, str(value).upper(), (str(value),))
    else:
        try:
            setattr(owner, key, value)
        except (TypeError, ValueError, AttributeError) as exc:
            raise ToolError(f"{where}.{key} = {value!r} failed: {exc}")


def _set_gn_input(mod, key, value):
    for item in mod.node_group.interface.items_tree:
        if getattr(item, "in_out", None) == "INPUT" and item.name == key:
            ident = item.identifier
            if isinstance(value, str):
                obj = bpy.data.objects.get(value) or bpy.data.materials.get(value) or \
                    bpy.data.collections.get(value)
                value = obj if obj is not None else value
            mod[ident] = value
            return True
    return False


@tool("modify_stack", """
Edit an object's modifier stack. action:
- add: new modifier of `type` (SUBSURF, BEVEL, ARRAY, MIRROR, SOLIDIFY,
  BOOLEAN, DISPLACE, WAVE, SCREW, SKIN, REMESH, DECIMATE, WELD, SMOOTH,
  SHRINKWRAP, SIMPLE_DEFORM, CURVE, LATTICE, WIREFRAME, TRIANGULATE, ...)
  with `settings` applied
- set: change `settings` on modifier `name` (RNA property names, e.g.
  {"count": 5, "relative_offset_displace": [1.1,0,0]}; pointers by object
  name; Geometry Nodes inputs by their interface name)
- remove / apply (bakes this and every modifier above it) / move (to `index`)
- apply_all, list, convert_to_mesh (curves/text -> real mesh object)
Errors list the valid setting names, so a wrong guess is cheap.
""", "Build",
      objects=P.objects("Objects", required=True),
      action=P.str("Operation", required=True,
                   enum=["add", "set", "remove", "apply", "apply_all", "move", "list",
                         "convert_to_mesh"]),
      type=P.str("Modifier type for add"),
      name=P.str("Modifier name (set/remove/apply/move); name for add"),
      settings=P.dict("Property values to set"),
      index=P.int("Target stack position for move (0 = first)"))
def modify_stack(objects, action, type=None, name=None, settings=None, index=None):
    from .tools_scene import _modifier_settings
    objs = resolve_objects(objects)
    out = []
    for o in objs:
        if action == "convert_to_mesh":
            dg = bpy.context.evaluated_depsgraph_get()
            me = bpy.data.meshes.new_from_object(o.evaluated_get(dg), depsgraph=dg)
            new = bpy.data.objects.new(o.name + "_mesh", me)
            for c in o.users_collection:
                c.objects.link(new)
            new.matrix_world = o.matrix_world.copy()
            old = o.name
            bpy.data.objects.remove(o, do_unlink=True)
            new.name = old
            out.append({"object": new.name, "converted": True, "verts": len(me.vertices)})
            continue
        if action == "add":
            if not type:
                raise ToolError("add needs `type`")
            try:
                m = o.modifiers.new(name or type.title(), type.upper())
            except (TypeError, RuntimeError) as exc:
                from .core import enum_items
                raise ToolError(f"Can't add {type}: {exc}. Valid: "
                                f"{[t for t in enum_items(bpy.types.Modifier, 'type') if not t.startswith('GREASE')]}")
            if m is None:
                raise ToolError(f"{o.name} ({o.type}) can't take a {type} modifier")
            for k, v in (settings or {}).items():
                if m.type == "NODES" and m.node_group and _set_gn_input(m, k, v):
                    continue
                _set_rna(m, k, v, f"{m.type} modifier")
            if index is not None:
                o.modifiers.move(len(o.modifiers) - 1, max(0, min(index, len(o.modifiers) - 1)))
            out.append({"object": o.name, "added": _modifier_settings(m)})
        elif action in ("set", "remove", "apply", "move"):
            m = o.modifiers.get(name) if name else None
            if m is None and type:
                m = next((x for x in o.modifiers if x.type == type.upper()), None)
            if m is None:
                raise ToolError(f"{o.name} has no modifier {name or type!r}. "
                                f"Stack: {[x.name for x in o.modifiers]}")
            if action == "set":
                for k, v in (settings or {}).items():
                    if m.type == "NODES" and m.node_group and _set_gn_input(m, k, v):
                        continue
                    _set_rna(m, k, v, f"{m.type} modifier")
                o.update_tag()
                out.append({"object": o.name, "modifier": _modifier_settings(m)})
            elif action == "remove":
                o.modifiers.remove(m)
                out.append({"object": o.name, "removed": name or type})
            elif action == "apply":
                out.append({"object": o.name, "applied": apply_modifiers_up_to(o, m),
                            "verts": len(o.data.vertices)})
            else:
                cur = list(o.modifiers).index(m)
                o.modifiers.move(cur, max(0, min(index or 0, len(o.modifiers) - 1)))
                out.append({"object": o.name, "stack": [x.name for x in o.modifiers]})
        elif action == "apply_all":
            out.append({"object": o.name, "applied": apply_modifiers_up_to(o),
                        "verts": len(o.data.vertices) if o.type == "MESH" else None})
        elif action == "list":
            out.append({"object": o.name, "modifiers": [_modifier_settings(m) for m in o.modifiers]})
    bpy.context.view_layer.update()
    return {"results": out}


EDIT_OPS = ["recalculate_normals", "flip_normals", "merge_by_distance", "delete_loose",
            "dissolve_degenerate", "fill_holes", "triangulate", "quadify", "subdivide",
            "bevel_edges", "extrude", "inset", "delete_faces", "solidify", "smooth",
            "randomize", "assign_material", "shade_smooth", "shade_flat",
            "smart_uv_project", "cube_uv_project", "separate_loose"]


def _face_filter(bm, obj, facing, angle, z_above, z_below, material_index):
    mw = obj.matrix_world
    nmat = mw.to_3x3().inverted().transposed()
    want = Vector(facing).normalized() if facing is not None else None
    cos_lim = math.cos(math.radians(angle))
    faces = []
    for f in bm.faces:
        if want is not None and (nmat @ f.normal).normalized().dot(want) < cos_lim:
            continue
        c = mw @ f.calc_center_median()
        if z_above is not None and c.z < z_above:
            continue
        if z_below is not None and c.z > z_below:
            continue
        if material_index is not None and f.material_index != material_index:
            continue
        faces.append(f)
    return faces


@tool("edit_mesh", f"""
Direct mesh editing with bmesh, no edit-mode juggling. op: {', '.join(EDIT_OPS)}.
Face-based ops (extrude, inset, delete_faces, assign_material, subdivide,
bevel_edges, triangulate) act on faces chosen by filters: facing (world
direction, e.g. [0,0,1] = faces pointing up) within `angle` degrees,
z_above/z_below (world height of face centers), material_index; no filter =
all faces. amount means: extrude distance, inset thickness, bevel width,
merge distance, solidify thickness, randomize strength, smooth factor.
""", "Build",
      objects=P.objects("Mesh objects", required=True),
      op=P.str("Operation", required=True, enum=EDIT_OPS),
      amount=P.num("Main amount for the op (see description)"),
      cuts=P.int("Subdivide cuts / bevel segments / smooth iterations", 1, minimum=1,
                 maximum=64),
      facing=P.vec3("Face filter: world direction"),
      angle=P.num("Face filter: tolerance in degrees", 30.0),
      z_above=P.num("Face filter: face center Z >= this"),
      z_below=P.num("Face filter: face center Z <= this"),
      material_index=P.int("Face filter: material slot index"),
      material=P.str("Material (name/preset/color) for assign_material"),
      individual=P.bool("extrude/inset faces individually rather than as a region", False),
      seed=P.int("Seed for randomize", 0))
def edit_mesh(objects, op, amount=None, cuts=1, facing=None, angle=30.0, z_above=None,
              z_below=None, material_index=None, material=None, individual=False, seed=0):
    objs = resolve_objects(objects, types={"MESH"})
    results = []
    for o in objs:
        _single_user_data(o)
        if op in ("smart_uv_project", "cube_uv_project"):
            results.append(_uv_project(o, op, amount))
            continue
        if op in ("shade_smooth", "shade_flat"):
            _shade(o, op == "shade_smooth", amount if amount is not None else 30)
            results.append({"object": o.name, "op": op})
            continue
        if op == "separate_loose":
            results.append(_separate_loose(o))
            continue
        me = o.data
        bm = bmesh.new()
        bm.from_mesh(me)
        before = (len(bm.verts), len(bm.faces))
        filtered = any(x is not None for x in (facing, z_above, z_below, material_index))
        faces = _face_filter(bm, o, facing, angle, z_above, z_below, material_index) \
            if filtered else list(bm.faces)
        note = None

        if op == "recalculate_normals":
            bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
        elif op == "flip_normals":
            bmesh.ops.reverse_faces(bm, faces=faces)
        elif op == "merge_by_distance":
            bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=amount or 0.0001)
        elif op == "delete_loose":
            loose_e = [e for e in bm.edges if not e.link_faces]
            bmesh.ops.delete(bm, geom=loose_e, context="EDGES")
            loose_v = [v for v in bm.verts if not v.link_edges]
            bmesh.ops.delete(bm, geom=loose_v, context="VERTS")
        elif op == "dissolve_degenerate":
            bmesh.ops.dissolve_degenerate(bm, dist=amount or 0.0001, edges=bm.edges)
        elif op == "fill_holes":
            bmesh.ops.holes_fill(bm, edges=bm.edges, sides=int(amount or 0))
        elif op == "triangulate":
            bmesh.ops.triangulate(bm, faces=faces)
        elif op == "quadify":
            bmesh.ops.join_triangles(bm, faces=faces, angle_face_threshold=math.radians(40),
                                     angle_shape_threshold=math.radians(40))
        elif op == "subdivide":
            edges = list({e for f in faces for e in f.edges})
            bmesh.ops.subdivide_edges(bm, edges=edges, cuts=cuts, use_grid_fill=True,
                                      smooth=amount or 0.0)
        elif op == "bevel_edges":
            fset = set(faces)
            edges = [e for e in bm.edges if len(e.link_faces) == 2 and
                     (not filtered or any(f in fset for f in e.link_faces)) and
                     e.calc_face_angle(0) > math.radians(angle if filtered else 30)]
            bmesh.ops.bevel(bm, geom=edges, offset=amount or 0.02, segments=cuts,
                            profile=0.5, affect="EDGES", clamp_overlap=True)
            note = f"{len(edges)} edges beveled"
        elif op == "extrude":
            dist = amount if amount is not None else 0.2
            if individual:
                res = bmesh.ops.extrude_discrete_faces(bm, faces=faces)
                for f in res["faces"]:
                    bmesh.ops.translate(bm, verts=f.verts, vec=f.normal * dist)
            else:
                res = bmesh.ops.extrude_face_region(bm, geom=faces)
                new_verts = [g for g in res["geom"] if isinstance(g, bmesh.types.BMVert)]
                avg_n = Vector()
                for f in faces:
                    avg_n += f.normal
                avg_n = avg_n.normalized() if avg_n.length else Vector((0, 0, 1))
                bmesh.ops.translate(bm, verts=new_verts, vec=avg_n * dist)
                bmesh.ops.delete(bm, geom=faces, context="FACES_ONLY")
            note = f"{len(faces)} faces extruded"
        elif op == "inset":
            th = amount if amount is not None else 0.05
            if individual:
                bmesh.ops.inset_individual(bm, faces=faces, thickness=th, use_even_offset=True)
            else:
                bmesh.ops.inset_region(bm, faces=faces, thickness=th, use_even_offset=True)
            note = f"{len(faces)} faces inset"
        elif op == "delete_faces":
            bmesh.ops.delete(bm, geom=faces, context="FACES")
        elif op == "solidify":
            bmesh.ops.solidify(bm, geom=faces, thickness=amount or 0.05)
        elif op == "smooth":
            verts = list({v for f in faces for v in f.verts}) if filtered else list(bm.verts)
            for _ in range(cuts):
                bmesh.ops.smooth_vert(bm, verts=verts, factor=amount or 0.5,
                                      use_axis_x=True, use_axis_y=True, use_axis_z=True)
        elif op == "randomize":
            rng = random.Random(seed)
            verts = list({v for f in faces for v in f.verts}) if filtered else list(bm.verts)
            strength = amount or 0.02
            for v in verts:
                v.co += v.normal * rng.uniform(-strength, strength)
        elif op == "assign_material":
            if not material:
                raise ToolError("assign_material needs `material`")
            mat = _resolve_material(material)
            mats = list(me.materials)
            if mat.name in [m.name for m in mats if m]:
                idx = [m.name if m else None for m in mats].index(mat.name)
            else:
                me.materials.append(mat)
                idx = len(me.materials) - 1
            for f in faces:
                f.material_index = idx
            note = f"{len(faces)} faces -> {mat.name}"
        bm.normal_update()
        bm.to_mesh(me)
        after = (len(bm.verts), len(bm.faces))
        bm.free()
        me.update()
        results.append({"object": o.name, "op": op, "faces_affected": len(faces) if filtered
                        else "all", "verts_before_after": [before[0], after[0]],
                        "faces_before_after": [before[1], after[1]], "note": note})
    return {"results": results}


def _uv_project(o, op, amount):
    ctx = window_override()
    prev_active = bpy.context.view_layer.objects.active
    prev_mode = o.mode
    select_only([o])
    ov = dict(ctx)
    ov.update({"active_object": o, "object": o, "selected_objects": [o],
               "selected_editable_objects": [o]})
    with bpy.context.temp_override(**ov):
        bpy.ops.object.mode_set(mode="EDIT")
        bpy.ops.mesh.select_all(action="SELECT")
        if op == "smart_uv_project":
            bpy.ops.uv.smart_project(angle_limit=math.radians(amount or 66), island_margin=0.02)
        else:
            bpy.ops.uv.cube_project(cube_size=amount or 1.0)
        bpy.ops.object.mode_set(mode="OBJECT" if prev_mode != "EDIT" else "EDIT")
    if prev_active:
        bpy.context.view_layer.objects.active = prev_active
    return {"object": o.name, "op": op, "uv_layers": [u.name for u in o.data.uv_layers]}


def _separate_loose(o):
    bm = bmesh.new()
    bm.from_mesh(o.data)
    bm.verts.ensure_lookup_table()
    seen, islands = set(), []
    for v in bm.verts:
        if v.index in seen:
            continue
        stack, island = [v], []
        seen.add(v.index)
        while stack:
            cur = stack.pop()
            island.append(cur.index)
            for e in cur.link_edges:
                w = e.other_vert(cur)
                if w.index not in seen:
                    seen.add(w.index)
                    stack.append(w)
        islands.append(set(island))
    bm.free()
    if len(islands) <= 1:
        return {"object": o.name, "op": "separate_loose", "parts": 1}
    source = o.data.copy()
    created = []
    for k, isl in enumerate(islands):
        bm = bmesh.new()
        bm.from_mesh(source)
        bm.verts.ensure_lookup_table()
        bmesh.ops.delete(bm, geom=[v for v in bm.verts if v.index not in isl], context="VERTS")
        if k == 0:
            target = o
        else:
            target = o.copy()
            target.data = source.copy()
            for c in o.users_collection:
                c.objects.link(target)
            target.name = f"{o.name}.part{k:02d}"
            created.append(target.name)
        bm.to_mesh(target.data)
        bm.free()
    bpy.data.meshes.remove(source)
    return {"object": o.name, "op": "separate_loose", "parts": len(islands), "created": created}
