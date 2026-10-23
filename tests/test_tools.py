import base64
import json
import os
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import bpy

import claude_blender
from claude_blender import core, server

OUT = sys.argv[sys.argv.index("--") + 1] if "--" in sys.argv[:-1] else os.path.join(HERE, "out")
os.makedirs(OUT, exist_ok=True)

claude_blender.register()
results = []


def save_images(name, result):
    imgs = []
    if isinstance(result, dict):
        if "image" in result:
            imgs.append(result["image"])
        imgs += result.get("images") or []
    for i, img in enumerate(imgs):
        ext = "jpg" if img.get("mime") == "image/jpeg" else "png"
        path = os.path.join(OUT, f"{name}{'_' + str(i) if i else ''}.{ext}")
        with open(path, "wb") as fh:
            fh.write(base64.b64decode(img["base64"]))


def call(label, tool, expect_ok=True, **args):
    t0 = time.perf_counter()
    try:
        resp = server.run_tool(tool, args, client="test")
    except Exception:
        resp = {"ok": False, "error": traceback.format_exc()}
    ms = (time.perf_counter() - t0) * 1000
    ok = resp["ok"] == expect_ok
    results.append((label, ok, ms, "" if resp["ok"] else resp["error"].splitlines()[0][:160]))
    if resp["ok"]:
        save_images(label, resp["result"])
    elif expect_ok:
        print(f"\n--- {label} FAILED ---\n{resp['error']}\n")
    return resp.get("result")


for o in list(bpy.data.objects):
    bpy.data.objects.remove(o)

call("overview_empty", "scene_overview")
call("schema_count", "query_objects")
call("floor", "create_object", kind="plane", name="Floor", size=[20, 20, 0], material="concrete")
call("cube", "create_object", kind="cube", name="Crate", size=[1, 1, 1], location=[0, 0, 3],
     material="wood", bevel=0.03)
call("sphere", "create_object", kind="uv_sphere", name="Ball", radius=0.5, location=[2, 0, 2],
     material="chrome")
call("torus", "create_object", kind="torus", name="Ring", size=[1.5, 1.5, 0.4], location=[-2, 0, 1],
     material="gold")
call("capsule", "create_object", kind="capsule", name="Pill", radius=0.3, location=[4, 0, 1],
     color="#FF7043")
call("cone", "create_object", kind="cone", name="Cone", size=[1, 1, 1.5], origin="bottom",
     location=[-4, 0, 0], radius_ratio=0.0, material="plastic")
call("monkey", "create_object", kind="monkey", name="Suzanne", location=[0, 3, 1], subdivision=2,
     material="ceramic")
call("text", "create_object", kind="text", text="CLAUDE", name="Title", location=[0, -3, 0.5],
     rotation=[90, 0, 0], extrude=0.05, material="emission")
call("tube", "create_object", kind="tube", name="Pipe", points=[[-3, 3, 0.2], [-1, 4, 1], [1, 5, 0.2]],
     radius=0.08, material="copper")
call("bad_kind", "create_object", expect_ok=False, kind="dodecahedron")
call("bad_param", "create_object", expect_ok=False, kind="cube", wibble="red")
call("alias_kind", "create_object", kind="sphere", name="AliasOrb", size=0.16, location="[0, 0, 6]")
call("alias_params", "create_material", preset="emission", color="#66CCFF", objects=["AliasOrb"])
call("alias_object", "inspect_object", object="AliasOrb")
call("bad_name", "inspect_object", expect_ok=False, name="Crat")

call("drop", "transform_objects", objects=["Crate", "Ball", "Ring", "Pill"], drop_to_surface=True)
r = call("measure_crate", "measure", objects="Crate", against="Floor")
print("measure:", json.dumps(r)[:400])
call("transform_add", "transform_objects", objects="Ring", rotation=[0, 0, 45], mode="add")
call("look_at", "transform_objects", objects="Pill", look_at="Crate")
call("apply_scale", "transform_objects", objects="Crate", scale=[1, 2, 1])
call("apply_scale2", "transform_objects", objects="Crate", apply=["scale"])
call("set_origin", "set_origin", objects="Ball", mode="bottom")
call("inspect", "inspect_object", name="Crate")
call("query", "query_objects", type="MESH", near="Crate", radius=5, sort="distance")
call("raycast", "raycast", origin=[0, 0, 10])

call("dup", "duplicate_objects", objects="Cone", count=5, offset=[0, 1.2, 0], rotation=[0, 0, 15])
call("arrange_grid", "arrange_objects", objects="Cone*", pattern="grid", center=[8, 0, 0], spacing=1.5)
call("arrange_circle", "arrange_objects", objects="Ball", pattern="circle", count=8, radius=3,
     center=[0, -8, 0], face_center=True, random_scale=[0.6, 1.2], seed=3)
call("arrange_spiral", "arrange_objects", objects="Pill", pattern="spiral", count=20, spacing=0.8,
     center=[-8, -8, 0])
call("align", "align_distribute", objects="Cone*", axis="x", align="min", distribute=False)
call("distribute", "align_distribute", objects="Cone*", axis="y", distribute=True)

for preset in ["brushed_metal", "rusted_metal", "glass", "wood", "marble", "concrete", "fabric",
               "car_paint", "skin", "brick", "tiles", "holographic", "ice", "water", "leather",
               "toon", "checker", "ground", "rubber", "frosted_glass"]:
    call(f"mat_{preset}", "create_material", preset=preset, base_color="#4477AA"
         if preset == "car_paint" else None)
call("mat_assign", "create_material", preset="car_paint", base_color="#1565C0", name="BluePaint",
     assign_to="Suzanne")
call("mat_edit", "edit_material", material="BluePaint", set={"roughness": 0.2, "coat": 1})
call("mat_tex", "edit_material", material="BluePaint",
     texture={"kind": "voronoi", "to": "roughness", "scale": 30, "range": [0.1, 0.5]})
call("mat_bump", "edit_material", material="BluePaint", texture={"kind": "noise", "to": "bump"})
call("mat_describe", "edit_material", material="BluePaint", describe=True)

call("mod_add", "modify_stack", objects="Pill", action="add", type="ARRAY",
     settings={"count": 3, "relative_offset_displace": [1.2, 0, 0]})
call("mod_set", "modify_stack", objects="Pill", action="set", name="Array", settings={"count": 4})
call("mod_badset", "modify_stack", expect_ok=False, objects="Pill", action="set", name="Array",
     settings={"cnt": 4})
call("mod_list", "modify_stack", objects="Pill", action="list")
call("mod_apply", "modify_stack", objects="Pill", action="apply", name="Array")
call("bool_cutter", "create_object", kind="cylinder", name="Cutter", radius=0.25, size=[0.5, 0.5, 3],
     location=[0, 0, 0.5])
call("bool", "boolean", target="Crate", cutters="Cutter", apply=True, delete_cutters=True)

for op, kw in [("triangulate", {}), ("quadify", {}), ("merge_by_distance", {"amount": 0.001}),
               ("recalculate_normals", {}), ("extrude", {"facing": [0, 0, 1], "amount": 0.2}),
               ("inset", {"facing": [0, 0, 1], "amount": 0.05}),
               ("bevel_edges", {"amount": 0.02, "cuts": 2}),
               ("assign_material", {"facing": [0, 0, 1], "material": "gold"}),
               ("randomize", {"amount": 0.01}), ("smooth", {"amount": 0.3, "cuts": 2}),
               ("subdivide", {"cuts": 1}), ("smart_uv_project", {}), ("shade_smooth", {})]:
    call(f"edit_{op}", "edit_mesh", objects="Ring", op=op, **kw)
call("analyze", "analyze_mesh", objects=["Ring", "Crate", "Suzanne"])

call("organize_coll", "organize", objects="Cone*", action="move_to_collection", collection="Cones")
call("organize_group", "organize", objects="collection:Cones", action="group_under_empty", name="ConeGroup")
call("organize_rename", "organize", objects="Ball*", action="rename", name="Orb_{i}")
call("organize_join", "organize", objects="Orb_*", action="join", name="Orbs")
call("organize_color", "organize", objects="Orbs", action="set_color", color="red")

gen = {
    "terrain": {"size": 30, "resolution": 80, "height": 5, "style": "mountains", "water_level": 0.2,
                "location": [0, 40, 0], "seed": 2},
    "rock": {"count": 4, "size": 0.7, "location": [10, 10, 0], "seed": 1},
    "tree": {"style": "deciduous", "count": 3, "location": [-10, 10, 0]},
    "building": {"style": "office", "floors": 8, "footprint": [12, 9], "location": [30, 0, 0]},
    "city": {"blocks": [3, 3], "location": [80, 0, 0], "max_floors": 14, "seed": 4},
    "stairs": {"steps": 10, "location": [-20, 0, 0]},
    "room": {"location": [0, -25, 0], "door": {"wall": "left"}, "window": {"wall": "back"}},
    "table": {"location": [0, -25, 0]},
    "chair": {"location": [0.8, -25, 0]},
    "shelf": {"location": [-2, -23, 0]},
    "lamp": {"location": [2.3, -23, 0]},
    "fence": {"points": [[-15, -10, 0], [-5, -10, 0], [-5, -2, 0]]},
}
for kind, params in gen.items():
    call(f"gen_{kind}", "generate", kind=kind, params=params)
call("gen_pine", "generate", kind="tree", params={"style": "pine", "count": 2, "location": [-14, 14, 0]})
call("gen_spiral", "generate", kind="stairs", params={"spiral_radius": 1.2, "steps": 16,
                                                      "location": [-25, 0, 0]})

r = call("scatter", "scatter_on_surface", sources=["Rock.*"], target="Terrain", count=150,
         min_distance=0.4, max_slope_deg=30, scale_range=[0.3, 0.9], seed=3)
print("scatter:", json.dumps(r)[:300])
call("scatter_trees", "scatter_on_surface", sources=["Tree.00", "Tree.01"], target="Terrain",
     density=0.08, z_range=[1.5, 3.5], align_to_normal=0.3, realize=False)

for preset in ["three_point", "studio_soft", "product", "dramatic", "neon", "golden_hour",
               "midday", "overcast", "night", "rembrandt", "silhouette"]:
    call(f"rig_{preset}", "lighting_rig", preset=preset, target="Suzanne", backdrop=preset == "product")
call("add_light", "add_light", kind="spot", target="Suzanne", color="2700K", power=800)
call("world_hdri", "set_world", mode="hdri", hdri="builtin:forest", strength=1.0)
call("world_grad", "set_world", mode="gradient", color="#9FC5E8", color2="#2B2B2B")
call("world_sky", "set_world", mode="sky", sun_elevation=20, fog_density=0.0)
call("cam", "camera_setup", target="Suzanne", azimuth=30, elevation=12, aspect="4:5", dof=True, lens=85)
call("look_camera", "look", view="camera", max_size=400)
call("look_sheet", "look", views=["front", "right", "top", "iso"], target="Suzanne", max_size=320)
call("look_building", "look", view="iso", target="Building_office", max_size=400, shading="material")
call("look_city", "look", view="hero", target="collection:City", max_size=400)
call("look_terrain", "look", view="iso", target="Terrain", max_size=400, shading="material")

call("rig_final", "lighting_rig", preset="product", target="Suzanne", backdrop=True)
call("cam_final", "camera_setup", target="Suzanne", azimuth=25, elevation=10, aspect="1:1", lens=70)
call("render_settings", "render_settings", engine="eevee", quality="draft", view_transform="AgX",
     look="Punchy")
call("render", "render_image", resolution=[480, 480], samples=16, max_size=480)
call("render_cycles", "render_image", engine="cycles", resolution=[320, 320], samples=8, max_size=320)

call("anim_turn", "animate", objects="Suzanne", preset="turntable", start=1, end=96)
call("anim_bounce", "animate", objects="Orbs", preset="bounce", height=2, cycles=3)
call("anim_grow", "animate", objects="Rock.*", preset="grow_in", start=1, end=20, stagger=5)
call("anim_wiggle", "animate", objects="Pill", preset="wiggle", strength=0.05)
call("anim_keys", "animate", objects="Title", preset="keyframes",
     keys=[{"frame": 1, "location": [0, -3, 0.5]}, {"frame": 30, "location": [0, -3, 2.5],
                                                     "interpolation": "BACK"}])
call("anim_orbit", "animate", objects="Camera", preset="orbit", target="Suzanne", start=1, end=96)
call("anim_path", "animate", objects="Crate", preset="follow_path", path="Pipe")
call("inspect_anim", "inspect_object", name="Suzanne")
call("anim_clear", "animate", objects="Pill", preset="clear")

call("python", "execute_python", code="result = len(bpy.data.objects)\nprint('hi')")
call("python_err", "execute_python", expect_ok=False, code="raise ValueError('boom')")
call("batch", "batch", calls=[
    {"tool": "create_object", "args": {"kind": "cube", "name": "B1", "location": [0, 0, 10]}},
    {"tool": "create_object", "args": {"kind": "cube", "name": "B2", "location": [2, 0, 10]}},
    {"tool": "look", "args": {"view": "front", "target": ["B1", "B2"], "max_size": 200}},
])
call("batch_fail", "batch", calls=[{"tool": "nope"}, {"tool": "scene_overview"}])
call("checkpoint", "checkpoint", action="save", name="test")
call("checkpoint_list", "checkpoint", action="list")
call("delete", "delete_objects", objects=["B1", "B2"])
call("overview_full", "scene_overview", detail="brief")
call("undo_bg", "undo", expect_ok=False)

print("\n================ RESULTS ================")
fails = 0
for label, ok, ms, err in results:
    if not ok:
        fails += 1
    print(f"{'PASS' if ok else 'FAIL'}  {label:<22} {ms:8.0f} ms  {err}")
print(f"\n{len(results) - fails}/{len(results)} passed; images in {OUT}")
print(f"{len(core.TOOLS)} tools registered: {sorted(core.TOOLS)}")
