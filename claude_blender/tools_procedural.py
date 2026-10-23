import math
import random

import bmesh
import bpy
from mathutils import Matrix, Vector, noise

from .core import (
    P, ToolError, bbox_info, r, summarize_obj, tool, vec3,
)
from .tools_build import _shade, apply_modifiers_up_to
from .tools_look import build_preset, set_principled

KINDS = ["terrain", "rock", "tree", "building", "city", "stairs", "room", "table", "chair",
         "shelf", "lamp", "fence"]


def _mat(name, preset, **overrides):
    mat = bpy.data.materials.get(name)
    if mat is None:
        mat = bpy.data.materials.new(name)
        build_preset(mat, preset, {k: v for k, v in overrides.items() if v is not None})
    return mat


def _obj(name, bm, coll, materials=(), smooth=False, location=(0, 0, 0)):
    me = bpy.data.meshes.new(name)
    bm.normal_update()
    bm.to_mesh(me)
    bm.free()
    for m in materials:
        me.materials.append(m)
    obj = bpy.data.objects.new(name, me)
    coll.objects.link(obj)
    obj.location = location
    _shade(obj, smooth)
    if not me.uv_layers:
        me.uv_layers.new(name="UVMap")
    return obj


def _box(bm, center, size, mat_index=0):
    res = bmesh.ops.create_cube(bm, size=1.0)
    verts = res["verts"]
    bmesh.ops.scale(bm, vec=size, verts=verts)
    bmesh.ops.translate(bm, vec=center, verts=verts)
    faces = list({f for v in verts for f in v.link_faces})
    for f in faces:
        f.material_index = mat_index
    return faces


def _cyl(bm, base, radius, height, segments=16, radius_top=None, mat_index=0, matrix=None):
    res = bmesh.ops.create_cone(bm, cap_ends=True, segments=segments, radius1=radius,
                                radius2=radius if radius_top is None else radius_top,
                                depth=height)
    verts = res["verts"]
    bmesh.ops.translate(bm, vec=Vector((0, 0, height / 2)), verts=verts)
    if matrix is not None:
        bmesh.ops.transform(bm, matrix=matrix, verts=verts)
    bmesh.ops.translate(bm, vec=base, verts=verts)
    for f in {f for v in verts for f in v.link_faces}:
        f.material_index = mat_index
    return verts


def _group(name, objs, coll, location=(0, 0, 0)):
    empty = bpy.data.objects.new(name, None)
    empty.empty_display_type = "PLAIN_AXES"
    empty.empty_display_size = 0.5
    coll.objects.link(empty)
    empty.location = location
    for o in objs:
        o.parent = empty
    return empty


def _collection(name):
    coll = bpy.data.collections.get(name)
    if coll is None:
        coll = bpy.data.collections.new(name)
        bpy.context.scene.collection.children.link(coll)
    return coll


def _height_fn(style, scale, seed, octaves, roughness):
    off = Vector((seed * 37.1, seed * 91.7, seed * 13.3))
    basis = "PERLIN_ORIGINAL"

    def f(x, y):
        p = Vector((x * scale, y * scale, 0)) + off
        if style == "mountains":
            v = noise.ridged_multi_fractal(p, 1.0 - roughness * 0.5, 2.0, octaves, 1.0, 2.0,
                                           noise_basis=basis)
            return v * 0.5
        if style == "dunes":
            d = noise.fractal(p * 0.5, 1.0, 2.0, 3, noise_basis=basis)
            return 0.5 + 0.5 * math.sin(x * scale * 6 + d * 4) * 0.6 + d * 0.3
        if style == "canyon":
            v = noise.fractal(p, 1.0 - roughness * 0.5, 2.0, octaves, noise_basis=basis)
            return 1.0 if v > 0.05 else max(0.0, 0.3 + v * 4)
        if style == "plateau":
            v = noise.fractal(p, 1.0 - roughness * 0.5, 2.0, octaves, noise_basis=basis)
            return round((v + 1) * 2.5) / 5.0 + v * 0.05
        v = noise.hetero_terrain(p, 1.0 - roughness * 0.5, 2.0, octaves, 0.6,
                                 noise_basis=basis)
        return v * 0.5
    return f


def _terrain_material():
    mat = bpy.data.materials.get("Terrain")
    if mat is not None:
        return mat
    mat = bpy.data.materials.new("Terrain")
    from .tools_look import G, _fac
    g = G(mat)
    geo = g.node("ShaderNodeNewGeometry", -1300, 200)
    sep_n = g.node("ShaderNodeSeparateXYZ", -1100, 0)
    g.link(geo.outputs["Normal"], sep_n.inputs[0])
    attr = g.node("ShaderNodeAttribute", -1300, 500)
    attr.attribute_name = "height01"
    hn = g.tex("ShaderNodeTexNoise", -1100, 600, scale=8.0, detail=6.0)
    add = g.node("ShaderNodeMath", -850, 450, operation="MULTIPLY_ADD")
    g.link(_fac(hn), add.inputs[0])
    add.inputs[1].default_value = 0.12
    g.link(out_fac(attr), add.inputs[2])
    height_col = g.ramp(add.outputs[0], [(0.0, "#C2B280"), (0.08, "#4E6B2E"),
                                         (0.45, "#5D6B3A"), (0.62, "#6E6458"),
                                         (0.85, "#8C8780"), (0.93, "#F4F6F8")], -600, 450)
    slope = g.ramp(sep_n.outputs["Z"], [(0.72, (1, 1, 1)), (0.86, (0, 0, 0))], -850, 0)
    mix = g.node("ShaderNodeMix", -300, 200, data_type="RGBA")
    g.link(slope.outputs["Color"], [s for s in mix.inputs if s.name == "Factor"][0])
    rgba = [s for s in mix.inputs if s.type == "RGBA"]
    g.link(height_col.outputs["Color"], rgba[0])
    rgba[1].default_value = (0.22, 0.2, 0.18, 1)
    g.link([s for s in mix.outputs if s.type == "RGBA"][0], g.bsdf_in("base_color"))
    g.set(roughness=0.9)
    fine = g.tex("ShaderNodeTexNoise", -800, -400, scale=60.0, detail=8.0)
    g.bump(_fac(fine), strength=0.25)
    mat["claude_preset"] = "terrain"
    return mat


def out_fac(node):
    for name in ("Fac", "Factor"):
        s = node.outputs.get(name)
        if s is not None:
            return s
    return node.outputs[0]


def gen_terrain(coll, p, rng):
    size = float(p.get("size", 40.0))
    res = int(p.get("resolution", 160))
    height = float(p.get("height", 6.0))
    style = p.get("style", "hills")
    scale = float(p.get("noise_scale", 0.06))
    octaves = int(p.get("octaves", 6))
    rough = float(p.get("roughness", 0.5))
    island = bool(p.get("island", False))
    fn = _height_fn(style, scale, p.get("seed", 0), octaves, rough)
    bm = bmesh.new()
    bmesh.ops.create_grid(bm, x_segments=res, y_segments=res, size=size / 2)
    hs = []
    for v in bm.verts:
        h = fn(v.co.x, v.co.y)
        if island:
            d = min(1.0, (Vector((v.co.x, v.co.y)).length / (size / 2)))
            h *= max(0.0, 1 - d ** 2.2)
        hs.append(h)
    lo, hi = min(hs), max(hs)
    span = max(hi - lo, 1e-6)
    layer = bm.verts.layers.float.new("height01")
    for v, h in zip(bm.verts, hs):
        t = (h - lo) / span
        v.co.z = t * height
        v[layer] = t
    obj = _obj(p.get("name", "Terrain"), bm, coll, [_terrain_material()], smooth=True,
               location=vec3(p.get("location", (0, 0, 0))))
    created = [obj]
    if p.get("water_level") is not None:
        wl = float(p["water_level"])
        wbm = bmesh.new()
        bmesh.ops.create_grid(wbm, x_segments=1, y_segments=1, size=size / 2 * 1.02)
        water_mat = _mat("Water", "water")
        w = _obj("Water", wbm, coll, [water_mat], location=obj.location + Vector((0, 0, wl * height)))
        created.append(w)
    return created, {"min_height": 0.0, "max_height": height, "style": style}


def gen_rock(coll, p, rng):
    count = int(p.get("count", 1))
    size = float(p.get("size", 1.0))
    created = []
    mat = _mat("Rock", "concrete", base_color=p.get("color", "#7D7A74"),
               secondary_color="#4F4C47", scale=3.0, bump=0.4)
    base = vec3(p.get("location", (0, 0, 0)))
    for i in range(count):
        seed = int(p.get("seed", 0)) + i * 17
        bm = bmesh.new()
        bmesh.ops.create_icosphere(bm, subdivisions=int(p.get("detail", 4)), radius=0.5)
        off = Vector((seed * 3.7, seed * 1.3, seed * 7.1))
        jag = float(p.get("jaggedness", 0.5))
        for v in bm.verts:
            n = v.co.normalized()
            big = noise.fractal(n * 1.2 + off, 0.8, 2.0, 3)
            small = noise.fractal(n * 5.0 + off, 0.5, 2.0, 4)
            v.co = n * (0.5 + big * 0.18 + small * 0.05 * jag * 2)
        planes = [Vector((rng.uniform(-1, 1), rng.uniform(-1, 1), rng.uniform(-0.2, 1))).normalized()
                  for _ in range(int(3 + jag * 6))]
        for pl in planes:
            d = rng.uniform(0.28, 0.42)
            for v in bm.verts:
                over = v.co.dot(pl) - d
                if over > 0:
                    v.co -= pl * over
        sx, sy, sz = rng.uniform(0.8, 1.3), rng.uniform(0.7, 1.1), rng.uniform(0.45, 0.8)
        bmesh.ops.scale(bm, vec=(sx * size, sy * size, sz * size), verts=bm.verts)
        minz = min(v.co.z for v in bm.verts)
        for v in bm.verts:
            v.co.z = max(v.co.z, minz + sz * size * 0.12)
        minz = min(v.co.z for v in bm.verts)
        bmesh.ops.translate(bm, vec=(0, 0, -minz), verts=bm.verts)
        spread = size * 2.2
        loc = base + (Vector((rng.uniform(-1, 1), rng.uniform(-1, 1), 0)) * spread * math.sqrt(count)
                      if count > 1 else Vector())
        o = _obj(f"{p.get('name', 'Rock')}.{i:02d}" if count > 1 else p.get("name", "Rock"),
                 bm, coll, [mat], smooth=bool(p.get("smooth", True)), location=loc)
        o.rotation_euler.z = rng.uniform(0, 2 * math.pi)
        created.append(o)
    return created, {}


def gen_tree(coll, p, rng):
    style = p.get("style", "deciduous")
    h = float(p.get("height", 5.0))
    count = int(p.get("count", 1))
    base = vec3(p.get("location", (0, 0, 0)))
    bark = _mat("Bark", "wood", base_color="#5A4030", secondary_color="#3A2A1E", scale=6.0,
                bump=0.3, roughness=0.85)
    created = []
    for i in range(count):
        trng = random.Random(int(p.get("seed", 0)) + i * 31)
        leaf_col = p.get("leaf_color") or "#3F7A2E"
        leaves = _mat(f"Leaves_{i % 3}", "fabric", base_color=leaf_col, scale=40.0,
                      roughness=0.7, bump=0.4)
        th = h * trng.uniform(0.85, 1.15)
        bm = bmesh.new()
        if style == "pine":
            _cyl(bm, Vector((0, 0, 0)), th * 0.035, th * 0.35, 10, th * 0.025, 0)
            tiers = int(p.get("tiers", 5))
            for t in range(tiers):
                f = t / tiers
                rad = th * 0.26 * (1 - f * 0.75)
                z = th * (0.22 + f * 0.62)
                verts = _cyl(bm, Vector((0, 0, z)), rad, th * 0.28, 12, 0.0, 1)
                for v in verts:
                    if v.co.z < z + 0.01:
                        v.co.x += trng.uniform(-0.06, 0.06) * rad
                        v.co.y += trng.uniform(-0.06, 0.06) * rad
                        v.co.z += trng.uniform(-0.05, 0.05) * rad
        else:
            trunk_h = th * 0.55
            _cyl(bm, Vector((0, 0, 0)), th * 0.05, trunk_h, 12, th * 0.032, 0)
            nb = int(p.get("branches", 4))
            tips = []
            for b in range(nb):
                ang = 2 * math.pi * b / nb + trng.uniform(-0.4, 0.4)
                tilt = math.radians(trng.uniform(35, 55))
                ln = th * trng.uniform(0.22, 0.32)
                z0 = trunk_h * trng.uniform(0.7, 0.95)
                rot = Matrix.Rotation(ang, 4, "Z") @ Matrix.Rotation(tilt, 4, "Y")
                _cyl(bm, Vector((0, 0, z0)), th * 0.022, ln, 8, th * 0.012, 0, rot.to_3x3().to_4x4())
                tips.append(Vector((0, 0, z0)) + (rot.to_3x3() @ Vector((0, 0, ln))))
            tips.append(Vector((0, 0, trunk_h + th * 0.1)))
            for tip in tips:
                res = bmesh.ops.create_icosphere(bm, subdivisions=2, radius=th * trng.uniform(0.17, 0.24))
                off = Vector((trng.random() * 50, trng.random() * 50, 0))
                for v in res["verts"]:
                    v.co *= 1 + noise.noise(v.co * 3 + off) * 0.35
                    v.co.z *= 0.8
                bmesh.ops.translate(bm, vec=tip, verts=res["verts"])
                for f in {f for v in res["verts"] for f in v.link_faces}:
                    f.material_index = 1
        loc = base + (Vector((trng.uniform(-1, 1), trng.uniform(-1, 1), 0)) * h * 0.9 *
                      math.sqrt(count) if count > 1 else Vector())
        name = p.get("name", "Tree")
        o = _obj(f"{name}.{i:02d}" if count > 1 else name, bm, coll, [bark, leaves],
                 smooth=not p.get("low_poly", True), location=loc)
        o.rotation_euler.z = trng.uniform(0, 2 * math.pi)
        created.append(o)
    return created, {"style": style}


def _building_mesh(w, d, floors, fh, cols_x, cols_y, win_w, win_h, depth, parapet,
                   storefront, rng, roof_units):
    bm = bmesh.new()
    res = bmesh.ops.create_cube(bm, size=1.0)
    bmesh.ops.scale(bm, vec=(w, d, floors * fh), verts=res["verts"])
    bmesh.ops.translate(bm, vec=(0, 0, floors * fh / 2), verts=res["verts"])

    def cut(axis, n):
        if n < 2:
            return
        edges = [e for e in bm.edges
                 if abs((e.verts[0].co - e.verts[1].co).normalized()[axis]) > 0.99]
        bmesh.ops.subdivide_edges(bm, edges=edges, cuts=n - 1, use_grid_fill=True)

    cut(2, floors)
    cut(0, cols_x)
    cut(1, cols_y)
    bm.faces.ensure_lookup_table()
    walls = [f for f in bm.faces if abs(f.normal.z) < 0.1]
    old_verts = set(bm.verts)
    bmesh.ops.inset_individual(bm, faces=walls, thickness=min(w / cols_x, fh) * 0.05,
                               use_even_offset=True)
    panes = [f for f in bm.faces if abs(f.normal.z) < 0.1 and
             not any(v in old_verts for v in f.verts)]
    windows = []
    zaxis = Vector((0, 0, 1))
    for f in panes:
        c = f.calc_center_median()
        floor_idx = int(c.z // fh)
        tangent = f.normal.cross(zaxis).normalized()
        cell_w = (w / cols_x) if abs(f.normal.y) > 0.5 else (d / cols_y)
        ww = cell_w * (0.92 if storefront and floor_idx == 0 else win_w)
        wh = fh * (0.82 if storefront and floor_idx == 0 else win_h)
        zc = floor_idx * fh + (fh * 0.5 if not (storefront and floor_idx == 0) else fh * 0.45)
        for v in f.verts:
            sx = 1 if (v.co - c).dot(tangent) > 0 else -1
            sz = 1 if v.co.z > c.z else -1
            v.co = Vector((c.x, c.y, zc)) + tangent * (ww / 2) * sx + zaxis * (wh / 2) * sz
        windows.append(f)
    ext = bmesh.ops.extrude_discrete_faces(bm, faces=windows)
    for f in ext["faces"]:
        bmesh.ops.translate(bm, verts=f.verts, vec=-f.normal * depth)
        f.material_index = 1
    tops = [f for f in bm.faces if f.normal.z > 0.9 and
            min(v.co.z for v in f.verts) > floors * fh - 1e-4]
    if parapet > 0 and tops:
        bmesh.ops.inset_region(bm, faces=tops, thickness=min(w, d) * 0.04, use_even_offset=True)
        ret = bmesh.ops.extrude_face_region(bm, geom=tops)
        nv = [g for g in ret["geom"] if isinstance(g, bmesh.types.BMVert)]
        bmesh.ops.translate(bm, verts=nv, vec=(0, 0, -parapet))
        bmesh.ops.delete(bm, geom=tops, context="FACES_ONLY")
        for f in [g for g in ret["geom"] if isinstance(g, bmesh.types.BMFace)]:
            f.material_index = 2
    for _ in range(roof_units):
        uw, ud = rng.uniform(0.08, 0.2) * w, rng.uniform(0.08, 0.2) * d
        cx, cy = rng.uniform(-w / 3, w / 3), rng.uniform(-d / 3, d / 3)
        uh = rng.uniform(0.6, 1.4)
        _box(bm, Vector((cx, cy, floors * fh - parapet + uh / 2)), (uw, ud, uh), 2)
    return bm


BUILDING_STYLES = {
    "office": ("concrete", "#B9BDC2", 0.88, 0.72, 0.12, True),
    "residential": ("brick", "#8E3B2E", 0.42, 0.55, 0.15, False),
    "modern": ("matte", "#ECECEC", 0.7, 0.62, 0.25, True),
    "industrial": ("concrete", "#8C8A84", 0.35, 0.4, 0.1, False),
}


def gen_building(coll, p, rng, location=None):
    style = p.get("style", "office")
    if style not in BUILDING_STYLES:
        raise ToolError(f"style must be one of {sorted(BUILDING_STYLES)}")
    wall_preset, wall_col, win_w, win_h, depth, storefront = BUILDING_STYLES[style]
    fp = p.get("footprint") or [12.0, 10.0]
    w, d = float(fp[0]), float(fp[1] if len(fp) > 1 else fp[0])
    floors = int(p.get("floors", 6))
    fh = float(p.get("floor_height", 3.2))
    bay = float(p.get("bay_width", 3.0))
    cols_x, cols_y = max(1, round(w / bay)), max(1, round(d / bay))
    bm = _building_mesh(w, d, floors, fh, cols_x, cols_y,
                        float(p.get("window_width", win_w)), float(p.get("window_height", win_h)),
                        depth, float(p.get("parapet", 0.9)),
                        bool(p.get("storefront", storefront)), rng,
                        int(p.get("roof_units", rng.randint(1, 3))))
    walls = _mat(f"Building_{style}", wall_preset,
                 base_color=p.get("wall_color", wall_col), scale=2.0)
    glass = bpy.data.materials.get("Window Glass")
    if glass is None:
        glass = bpy.data.materials.new("Window Glass")
        set_principled(glass, base_color="#1E2A33", roughness=0.04, metallic=0.4,
                       specular=1.0, coat=1.0)
    roof = _mat("Roof", "concrete", base_color="#5E5E5E", scale=4.0)
    loc = location if location is not None else vec3(p.get("location", (0, 0, 0)))
    o = _obj(p.get("name", f"Building_{style}"), bm, coll, [walls, glass, roof], smooth=False,
             location=loc)
    return [o], {"floors": floors, "height": r(floors * fh), "bays": [cols_x, cols_y]}


def gen_city(coll, p, rng):
    blocks = p.get("blocks") or [4, 4]
    bx, by = int(blocks[0]), int(blocks[1] if len(blocks) > 1 else blocks[0])
    lot = float(p.get("lot_size", 16.0))
    street = float(p.get("street_width", 8.0))
    max_floors = int(p.get("max_floors", 18))
    created = []
    pitch = lot + street
    origin = vec3(p.get("location", (0, 0, 0)))
    styles = list(BUILDING_STYLES)
    for i in range(bx):
        for j in range(by):
            cx = (i - (bx - 1) / 2) * pitch
            cy = (j - (by - 1) / 2) * pitch
            dist = math.hypot(cx, cy) / (pitch * max(bx, by) / 2 + 1e-6)
            floors = max(2, int(max_floors * (1 - dist * 0.7) * rng.uniform(0.45, 1.0)))
            fw = lot * rng.uniform(0.6, 0.95)
            fd = lot * rng.uniform(0.6, 0.95)
            sub = {"style": rng.choice(styles), "footprint": [fw, fd], "floors": floors,
                   "name": f"Building_{i}_{j}", "roof_units": rng.randint(0, 3)}
            objs, _ = gen_building(coll, sub, rng, origin + Vector((cx, cy, 0)))
            objs[0].rotation_euler.z = rng.choice([0, math.pi / 2, math.pi, 1.5 * math.pi])
            created.extend(objs)
    gbm = bmesh.new()
    size = max(bx, by) * pitch + street
    bmesh.ops.create_grid(gbm, x_segments=1, y_segments=1, size=size / 2)
    asphalt = _mat("Asphalt", "concrete", base_color="#2E2F31", secondary_color="#232425",
                   scale=12.0, roughness=0.9)
    ground = _obj("Streets", gbm, coll, [asphalt], location=origin + Vector((0, 0, -0.01)))
    created.append(ground)
    return created, {"blocks": [bx, by], "buildings": bx * by}


def gen_stairs(coll, p, rng):
    steps = int(p.get("steps", 12))
    width = float(p.get("width", 1.2))
    rise = float(p.get("rise", 0.18))
    run = float(p.get("run", 0.28))
    spiral = p.get("spiral_radius")
    bm = bmesh.new()
    if spiral:
        R = float(spiral)
        turn = math.radians(float(p.get("degrees_per_step", 22.5)))
        for s in range(steps):
            a = s * turn
            faces = _box(bm, Vector((R / 2 + 0.1, 0, rise * (s + 0.5))), (R, run * 1.6, rise * 0.4))
            verts = list({v for f in faces for v in f.verts})
            bmesh.ops.rotate(bm, cent=(0, 0, 0), matrix=Matrix.Rotation(a, 3, "Z"), verts=verts)
        _cyl(bm, Vector((0, 0, 0)), 0.12, rise * (steps + 1), 16)
    else:
        solid = bool(p.get("solid", True))
        for s in range(steps):
            hgt = rise * (s + 1) if solid else rise
            zc = hgt / 2 if solid else rise * (s + 0.5)
            _box(bm, Vector((0, run * (s + 0.5), zc)), (width, run, hgt))
    mat = _mat("Stairs", p.get("material_preset", "wood"), base_color=p.get("color"))
    o = _obj(p.get("name", "Stairs"), bm, coll, [mat], location=vec3(p.get("location", (0, 0, 0))))
    created = [o]
    if p.get("railing", True) and not spiral:
        cu = bpy.data.curves.new("Railing", "CURVE")
        cu.dimensions = "3D"
        cu.bevel_depth = 0.025
        cu.use_fill_caps = True
        for side in (-1, 1):
            spl = cu.splines.new("POLY")
            spl.points.add(1)
            x = side * width / 2
            spl.points[0].co = (x, 0, 0.9, 1)
            spl.points[1].co = (x, run * steps, rise * steps + 0.9, 1)
        rail = bpy.data.objects.new("Railing", cu)
        coll.objects.link(rail)
        rail.data.materials.append(_mat("Railing Metal", "brushed_metal"))
        rail.parent = o
        created.append(rail)
    return created, {"total_rise": r(rise * steps), "total_run": r(run * steps)}


def gen_room(coll, p, rng):
    w = float(p.get("width", 6.0))
    d = float(p.get("depth", 5.0))
    h = float(p.get("height", 2.8))
    t = float(p.get("wall_thickness", 0.15))
    origin = vec3(p.get("location", (0, 0, 0)))
    floor_mat = _mat("Floor Wood", "wood", scale=1.5, coat=0.3)
    wall_mat = _mat("Wall Paint", "matte", base_color=p.get("wall_color", "#E9E4DA"))
    created = []
    fbm = bmesh.new()
    _box(fbm, Vector((0, 0, -0.05)), (w + 2 * t, d + 2 * t, 0.1))
    created.append(_obj("Floor", fbm, coll, [floor_mat], location=origin))
    open_side = p.get("open_side", "front")
    walls = {"back": (Vector((0, d / 2 + t / 2, h / 2)), (w + 2 * t, t, h)),
             "front": (Vector((0, -d / 2 - t / 2, h / 2)), (w + 2 * t, t, h)),
             "left": (Vector((-w / 2 - t / 2, 0, h / 2)), (t, d, h)),
             "right": (Vector((w / 2 + t / 2, 0, h / 2)), (t, d, h))}
    wall_objs = {}
    for side, (c, s) in walls.items():
        if side == open_side:
            continue
        bm = bmesh.new()
        _box(bm, c, s)
        wall_objs[side] = _obj(f"Wall_{side}", bm, coll, [wall_mat], location=origin)
        created.append(wall_objs[side])

    def cutout(side, center_along, bottom, cw, ch, label):
        wall = wall_objs.get(side)
        if wall is None:
            raise ToolError(f"No {side} wall to cut a {label} into (open_side={open_side})")
        c, s = walls[side]
        cbm = bmesh.new()
        if side in ("back", "front"):
            cc = Vector((center_along, c.y, bottom + ch / 2))
            _box(cbm, cc, (cw, t * 3, ch))
        else:
            cc = Vector((c.x, center_along, bottom + ch / 2))
            _box(cbm, cc, (t * 3, cw, ch))
        cutter = _obj(f"_cut_{label}", cbm, coll, location=origin)
        m = wall.modifiers.new(label, "BOOLEAN")
        m.operation, m.object = "DIFFERENCE", cutter
        apply_modifiers_up_to(wall, m)
        bpy.data.objects.remove(cutter, do_unlink=True)

    win = p.get("window")
    if win is not False:
        win = win or {}
        side = win.get("wall", "back")
        cutout(side, float(win.get("offset", 0.0)), float(win.get("sill", 0.9)),
               float(win.get("width", 1.6)), float(win.get("height", 1.3)), "Window")
    door = p.get("door")
    if door:
        side = door.get("wall", "left")
        cutout(side, float(door.get("offset", 0.0)), 0.0, float(door.get("width", 0.9)),
               float(door.get("height", 2.1)), "Door")
    if p.get("ceiling", False):
        cbm = bmesh.new()
        _box(cbm, Vector((0, 0, h + 0.05)), (w + 2 * t, d + 2 * t, 0.1))
        created.append(_obj("Ceiling", cbm, coll, [wall_mat], location=origin))
    return created, {"interior": [w, d, h], "open_side": open_side}


def gen_table(coll, p, rng):
    w, d, h = (p.get("size") or [1.6, 0.9, 0.75])[:3]
    top_t = 0.04
    bm = bmesh.new()
    _box(bm, Vector((0, 0, h - top_t / 2)), (w, d, top_t))
    leg = 0.06
    for sx in (-1, 1):
        for sy in (-1, 1):
            _box(bm, Vector((sx * (w / 2 - leg * 1.5), sy * (d / 2 - leg * 1.5), (h - top_t) / 2)),
                 (leg, leg, h - top_t))
    mat = _mat("Table Wood", "wood", coat=0.4, base_color=p.get("color"))
    o = _obj(p.get("name", "Table"), bm, coll, [mat], location=vec3(p.get("location", (0, 0, 0))))
    o.modifiers.new("Bevel", "BEVEL").width = 0.006
    return [o], {}


def gen_chair(coll, p, rng):
    seat_h, w = 0.45, 0.45
    bm = bmesh.new()
    _box(bm, Vector((0, 0, seat_h)), (w, w, 0.04))
    leg = 0.035
    for sx in (-1, 1):
        for sy in (-1, 1):
            hgt = seat_h if sy < 0 else 0.95
            _box(bm, Vector((sx * (w / 2 - leg), sy * (w / 2 - leg), hgt / 2)), (leg, leg, hgt))
    _box(bm, Vector((0, w / 2 - leg, 0.8)), (w - leg * 2, 0.025, 0.22))
    mat = _mat("Chair Wood", "wood", coat=0.3, base_color=p.get("color"))
    o = _obj(p.get("name", "Chair"), bm, coll, [mat], location=vec3(p.get("location", (0, 0, 0))))
    o.modifiers.new("Bevel", "BEVEL").width = 0.004
    return [o], {}


def gen_shelf(coll, p, rng):
    w, d, h = (p.get("size") or [1.0, 0.32, 1.8])[:3]
    shelves = int(p.get("shelves", 5))
    t = 0.025
    bm = bmesh.new()
    for sx in (-1, 1):
        _box(bm, Vector((sx * (w / 2 - t / 2), 0, h / 2)), (t, d, h))
    for i in range(shelves):
        z = t / 2 + (h - t) * i / (shelves - 1)
        _box(bm, Vector((0, 0, z)), (w - 2 * t, d, t))
    _box(bm, Vector((0, d / 2 - 0.004, h / 2)), (w, 0.008, h))
    mat = _mat("Shelf", "wood", base_color=p.get("color", "#C8A37A"), scale=2.0)
    o = _obj(p.get("name", "Shelf"), bm, coll, [mat], location=vec3(p.get("location", (0, 0, 0))))
    return [o], {"shelves": shelves}


def gen_lamp(coll, p, rng):
    h = float(p.get("height", 1.6))
    bm = bmesh.new()
    _cyl(bm, Vector((0, 0, 0)), 0.16, 0.03, 32)
    _cyl(bm, Vector((0, 0, 0.03)), 0.012, h - 0.25, 12)
    _cyl(bm, Vector((0, 0, h - 0.3)), 0.22, 0.3, 32, 0.12, 1)
    metal = _mat("Lamp Metal", "brushed_metal")
    shade = bpy.data.materials.get("Lamp Shade")
    if shade is None:
        shade = bpy.data.materials.new("Lamp Shade")
        set_principled(shade, base_color="#F3E9D2", roughness=0.8, subsurface=0.3,
                       emission_color="#FFD9A0", emission_strength=1.5)
    loc = vec3(p.get("location", (0, 0, 0)))
    o = _obj(p.get("name", "Floor Lamp"), bm, coll, [metal, shade], smooth=True, location=loc)
    ld = bpy.data.lights.new("Lamp Bulb", "POINT")
    ld.energy = float(p.get("power", 60.0))
    ld.shadow_soft_size = 0.08
    if hasattr(ld, "use_temperature"):
        ld.use_temperature, ld.temperature = True, 2700
    else:
        from .core import kelvin_to_rgb
        ld.color = kelvin_to_rgb(2700)
    bulb = bpy.data.objects.new("Lamp Bulb", ld)
    coll.objects.link(bulb)
    bulb.parent = o
    bulb.location = (0, 0, h - 0.2)
    return [o, bulb], {}


def gen_fence(coll, p, rng):
    start = vec3(p.get("start", (-5, 0, 0)))
    end = vec3(p.get("end", (5, 0, 0)))
    pts = [vec3(x) for x in p.get("points", [])] or [start, end]
    spacing = float(p.get("post_spacing", 2.0))
    h = float(p.get("height", 1.1))
    bm = bmesh.new()
    for a, b in zip(pts, pts[1:]):
        seg = b - a
        n = max(1, round(seg.length / spacing))
        ang = math.atan2(seg.y, seg.x)
        rot = Matrix.Rotation(ang, 4, "Z")
        for i in range(n + 1):
            pos = a + seg * (i / n)
            _box(bm, pos + Vector((0, 0, h / 2)), (0.1, 0.1, h))
        for z in (h * 0.35, h * 0.8):
            faces = _box(bm, Vector((0, 0, 0)), (seg.length, 0.05, 0.09))
            verts = list({v for f in faces for v in f.verts})
            bmesh.ops.transform(bm, matrix=rot, verts=verts)
            bmesh.ops.translate(bm, vec=(a + b) / 2 + Vector((0, 0, z)), verts=verts)
    mat = _mat("Fence Wood", "wood", base_color=p.get("color", "#8A6E52"), roughness=0.8)
    o = _obj(p.get("name", "Fence"), bm, coll, [mat])
    return [o], {"segments": len(pts) - 1}


GENERATORS = {"terrain": gen_terrain, "rock": gen_rock, "tree": gen_tree,
              "building": gen_building, "city": gen_city, "stairs": gen_stairs,
              "room": gen_room, "table": gen_table, "chair": gen_chair, "shelf": gen_shelf,
              "lamp": gen_lamp, "fence": gen_fence}


@tool("generate", """
Generate a complete procedural asset, with materials, from a few parameters.
Everything lands in its own collection. kinds and their `params`:
- terrain: size, resolution, height, style (hills|mountains|dunes|canyon|
  plateau), noise_scale, octaves, roughness, island, water_level (0-1 of
  height, adds a water plane), seed. Height/slope-aware grass/rock/snow material.
- rock: count, size, jaggedness 0-1, detail, color, seed
- tree: style (deciduous|pine), height, count, branches, tiers, leaf_color,
  low_poly, seed
- building: style (office|residential|modern|industrial), footprint [w,d],
  floors, floor_height, bay_width, window_width/height (0-1 of the bay),
  storefront, parapet, roof_units, wall_color
- city: blocks [x,y], lot_size, street_width, max_floors (taller toward the
  center), seed
- stairs: steps, width, rise, run, spiral_radius (+degrees_per_step), railing,
  material_preset
- room: width, depth, height, wall_thickness, open_side (front|back|left|
  right|none), window {wall, width, height, sill, offset} or false, door
  {wall, width, height, offset}, ceiling, wall_color
- table (size [w,d,h]), chair, shelf (size, shelves), lamp (height, power;
  includes a warm point light), fence (start, end or points, post_spacing,
  height)
All kinds accept location, name and seed.
""", "Build",
      kind=P.str("What to generate", required=True, enum=KINDS),
      params=P.dict("Generator parameters (see description)", {}),
      collection=P.str("Collection name (default: kind)"))
def generate(kind, params=None, collection=None):
    params = dict(params or {})
    gen = GENERATORS.get(kind)
    if gen is None:
        raise ToolError(f"Unknown kind {kind!r}. Kinds: {KINDS}")
    rng = random.Random(params.get("seed", 0))
    coll = _collection(collection or kind.title())
    created, extra = gen(coll, params, rng)
    bpy.context.view_layer.update()
    return {"kind": kind, "collection": coll.name,
            "created": [summarize_obj(o) for o in created[:60]], "count": len(created),
            "bounds": bbox_info([o for o in created if o.type == "MESH"]), **extra}
