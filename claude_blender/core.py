import base64
import difflib
import fnmatch
import math
import os
import re
import tempfile

import bpy
import mathutils
from mathutils import Matrix, Vector

TOOLS = {}


class ToolError(Exception):
    """A failure the caller can fix: bad arguments, missing objects, etc.

    Reported back to Claude as a plain message, with no traceback.
    """


COMMON_ALIASES = {
    "object": ["name", "objects", "target"], "obj": ["name", "objects"],
    "objects": ["assign_to", "target", "name"], "target": ["objects", "name"],
    "name": ["objects"], "color": ["base_color"], "colour": ["color", "base_color"],
    "base_colour": ["base_color"], "position": ["location"], "loc": ["location"],
    "pos": ["location"], "rot": ["rotation"], "rotation_euler": ["rotation"],
    "dimensions": ["size"], "type": ["kind"], "shape": ["kind"], "strength": ["power"],
    "energy": ["power"], "material_name": ["material"], "frames": ["end"],
    "emission_color": ["base_color"],
}


def _coerce(value, spec):
    kinds = {spec.get("type")} | {a.get("type") for a in spec.get("anyOf", [])}
    if isinstance(value, str) and kinds & {"array", "object"}:
        txt = value.strip()
        if txt[:1] in "[{":
            import json
            try:
                return json.loads(txt)
            except ValueError:
                return value
    return value


class Tool:
    def __init__(self, name, fn, description, category, params, required, mutates):
        self.name = name
        self.fn = fn
        self.description = description
        self.category = category
        self.params = params
        self.required = required
        self.mutates = mutates

    def schema(self):
        return {
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "inputSchema": {
                "type": "object",
                "properties": self.params,
                "required": self.required,
            },
            "annotations": {
                "readOnlyHint": not self.mutates,
                "destructiveHint": False,
            },
        }

    def call(self, args):
        args = dict(args or {})
        for key in [k for k in args if k not in self.params]:
            for target in COMMON_ALIASES.get(key, []):
                if target in self.params and target not in args:
                    args[target] = args.pop(key)
                    break
        args = {k: _coerce(v, self.params.get(k, {})) for k, v in args.items()}
        unknown = sorted(set(args) - set(self.params))
        if unknown:
            raise ToolError(
                f"{self.name}: unknown parameter(s) {unknown}. "
                f"Valid parameters: {sorted(self.params)}"
            )
        missing = [r for r in self.required if r not in args]
        if missing:
            raise ToolError(f"{self.name}: missing required parameter(s) {missing}")
        return self.fn(**args)


def tool(name, description, category, /, mutates=True, **params):
    def deco(fn):
        props, required = {}, []
        for key, spec in params.items():
            spec = dict(spec)
            if spec.pop("_required", False):
                required.append(key)
            props[key] = spec
        TOOLS[name] = Tool(name, fn, description.strip(), category, props, required, mutates)
        return fn

    return deco


class P:
    @staticmethod
    def _base(kind, desc, default, required, **extra):
        spec = {"type": kind, "description": desc}
        if default is not None:
            spec["default"] = default
        if required:
            spec["_required"] = True
        spec.update({k: v for k, v in extra.items() if v is not None})
        return spec

    @staticmethod
    def str(desc, default=None, required=False, enum=None):
        return P._base("string", desc, default, required, enum=enum)

    @staticmethod
    def num(desc, default=None, required=False, minimum=None, maximum=None):
        return P._base("number", desc, default, required, minimum=minimum, maximum=maximum)

    @staticmethod
    def int(desc, default=None, required=False, minimum=None, maximum=None):
        return P._base("integer", desc, default, required, minimum=minimum, maximum=maximum)

    @staticmethod
    def bool(desc, default=None, required=False):
        return P._base("boolean", desc, default, required)

    @staticmethod
    def vec3(desc, default=None, required=False):
        spec = {"description": desc + " ([x,y,z]; a single number means all three)",
                "anyOf": [{"type": "array", "items": {"type": "number"}, "minItems": 3,
                           "maxItems": 3}, {"type": "number"}]}
        if default is not None:
            spec["default"] = default
        if required:
            spec["_required"] = True
        return spec

    @staticmethod
    def color(desc, default=None, required=False):
        spec = {
            "description": desc + " Hex '#RRGGBB', a CSS-style name, or [r,g,b] in 0-1 sRGB.",
            "anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "number"},
                                           "minItems": 3, "maxItems": 4}],
        }
        if default is not None:
            spec["default"] = default
        if required:
            spec["_required"] = True
        return spec

    @staticmethod
    def objects(desc="Objects to act on.", required=False, default=None):
        spec = {
            "description": desc + (
                " Accepts a name, a list of names, or a selector: a glob ('Tree*'), "
                "'re:<regex>', 'selected', 'active', 'all', 'type:MESH', "
                "'collection:<name>'."
            ),
            "anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}],
        }
        if default is not None:
            spec["default"] = default
        if required:
            spec["_required"] = True
        return spec

    @staticmethod
    def list(desc, items, default=None, required=False):
        return P._base("array", desc, default, required, items=items)

    @staticmethod
    def dict(desc, default=None, required=False):
        return P._base("object", desc, default, required)


def _all_objects():
    return list(bpy.context.scene.objects)


def _match_one(selector):
    scene_objs = _all_objects()
    s = selector.strip()
    low = s.lower()
    if low == "selected":
        return [o for o in scene_objs if o.select_get()]
    if low == "active":
        o = bpy.context.view_layer.objects.active
        return [o] if o else []
    if low == "all":
        return scene_objs
    if low.startswith("type:"):
        t = s[5:].strip().upper()
        return [o for o in scene_objs if o.type == t]
    if low.startswith("collection:"):
        coll = bpy.data.collections.get(s[11:].strip())
        if coll is None:
            raise ToolError(f"No collection named {s[11:].strip()!r}. "
                            f"Collections: {[c.name for c in bpy.data.collections][:30]}")
        return [o for o in coll.all_objects if o.name in bpy.context.scene.objects]
    if low.startswith("re:"):
        rx = re.compile(s[3:])
        return [o for o in scene_objs if rx.search(o.name)]
    if any(ch in s for ch in "*?["):
        return [o for o in scene_objs if fnmatch.fnmatchcase(o.name, s)]
    obj = bpy.context.scene.objects.get(s)
    return [obj] if obj else []


def resolve_objects(spec, allow_empty=False, types=None):
    if spec is None:
        spec = "selected"
    selectors = [spec] if isinstance(spec, str) else list(spec)
    found, seen = [], set()
    for sel in selectors:
        matches = _match_one(str(sel))
        if not matches and not allow_empty:
            names = [o.name for o in _all_objects()]
            close = difflib.get_close_matches(str(sel), names, n=5, cutoff=0.5)
            hint = f" Did you mean {close}?" if close else (
                f" Scene objects: {names[:25]}{'...' if len(names) > 25 else ''}")
            raise ToolError(f"Nothing matches {sel!r}.{hint}")
        for o in matches:
            if o.name not in seen:
                seen.add(o.name)
                found.append(o)
    if types:
        types = {t.upper() for t in types}
        found = [o for o in found if o.type in types]
        if not found and not allow_empty:
            raise ToolError(f"No objects of type {sorted(types)} in {spec!r}")
    return found


def resolve_object(spec):
    objs = resolve_objects(spec)
    if len(objs) != 1:
        raise ToolError(f"Expected exactly one object for {spec!r}, got "
                        f"{[o.name for o in objs][:10]}")
    return objs[0]


def link_to_collection(obj, collection=None):
    if collection:
        coll = bpy.data.collections.get(collection)
        if coll is None:
            coll = bpy.data.collections.new(collection)
            bpy.context.scene.collection.children.link(coll)
    else:
        coll = bpy.context.collection or bpy.context.scene.collection
    coll.objects.link(obj)
    return coll


def select_only(objs, active=None):
    for o in bpy.context.view_layer.objects:
        if o is not None and o.select_get():
            o.select_set(False)
    for o in objs:
        try:
            o.select_set(True)
        except RuntimeError:
            pass
    target = active or (objs[0] if objs else None)
    if target is not None:
        bpy.context.view_layer.objects.active = target


def world_corners(obj):
    return [obj.matrix_world @ Vector(c) for c in obj.bound_box]


def world_bbox(objs):
    pts = []
    for o in objs:
        if o.type in {"MESH", "CURVE", "SURFACE", "FONT", "META", "VOLUME", "POINTCLOUD",
                      "CURVES", "GREASEPENCIL", "LATTICE", "ARMATURE"}:
            pts.extend(world_corners(o))
        else:
            pts.append(o.matrix_world.translation.copy())
    if not pts:
        return None
    lo = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
    hi = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
    return lo, hi


def bbox_info(objs):
    bb = world_bbox(objs)
    if bb is None:
        return None
    lo, hi = bb
    return {"min": r(lo), "max": r(hi), "center": r((lo + hi) / 2), "size": r(hi - lo)}


def look_at_quat(eye, target):
    direction = Vector(target) - Vector(eye)
    if direction.length < 1e-9:
        return mathutils.Quaternion()
    return direction.to_track_quat("-Z", "Y")


def direction_from_angles(azimuth_deg, elevation_deg):
    az, el = math.radians(azimuth_deg), math.radians(elevation_deg)
    return Vector((math.sin(az) * math.cos(el), -math.cos(az) * math.cos(el), math.sin(el)))


NAMED_VIEWS = {
    "front": (0, 0), "back": (180, 0), "right": (90, 0), "left": (-90, 0),
    "top": (0, 89.9), "bottom": (0, -89.9), "iso": (45, 30), "iso_back": (225, 30),
    "three_quarter": (35, 18), "hero": (30, 12),
}


def fit_distance(size_radius, fov_rad, padding=1.15):
    return max(0.01, size_radius * padding / math.sin(max(1e-3, fov_rad / 2)))


def r(v, nd=4):
    if isinstance(v, (int, bool)) or v is None:
        return v
    if isinstance(v, float):
        out = round(v, nd)
        return 0.0 if out == 0 else out
    if isinstance(v, (Vector, mathutils.Euler, mathutils.Color, mathutils.Quaternion, tuple, list)):
        return [r(x, nd) for x in v]
    return v


def jsonable(v, depth=0):
    if depth > 6:
        return str(v)
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        return r(v)
    if isinstance(v, (Vector, mathutils.Euler, mathutils.Color, mathutils.Quaternion)):
        return r(list(v))
    if isinstance(v, Matrix):
        return [r(list(row)) for row in v]
    if isinstance(v, dict):
        return {str(k): jsonable(x, depth + 1) for k, x in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [jsonable(x, depth + 1) for x in v]
    if hasattr(v, "name") and hasattr(v, "bl_rna"):
        return v.name
    try:
        return [jsonable(x, depth + 1) for x in v]
    except TypeError:
        return str(v)


def vec3(v, name="value"):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return Vector((v, v, v))
    try:
        vals = [float(x) for x in v]
    except (TypeError, ValueError):
        raise ToolError(f"{name} must be [x, y, z], got {v!r}")
    if len(vals) != 3:
        raise ToolError(f"{name} must have 3 components, got {len(vals)}")
    return Vector(vals)


def euler_deg(v, name="rotation"):
    v = vec3(v, name)
    return None if v is None else mathutils.Euler([math.radians(x) for x in v], "XYZ")


NAMED_COLORS = {
    "white": "#FFFFFF", "black": "#000000", "red": "#E53935", "green": "#43A047",
    "blue": "#1E88E5", "yellow": "#FDD835", "orange": "#FB8C00", "purple": "#8E24AA",
    "pink": "#EC407A", "cyan": "#00BCD4", "magenta": "#D81B60", "teal": "#00897B",
    "brown": "#6D4C41", "gray": "#808080", "grey": "#808080", "silver": "#C0C0C0",
    "gold": "#FFC64A", "copper": "#DA8A67", "navy": "#1A237E", "beige": "#E8DCC4",
    "ivory": "#FFFFF0", "olive": "#6B8E23", "maroon": "#800000", "lime": "#C0CA33",
    "coral": "#FF7F50", "turquoise": "#40E0D0", "charcoal": "#36454F", "cream": "#FFFDD0",
    "sky": "#87CEEB", "forest": "#228B22", "sand": "#C2B280", "brick": "#9C4A3A",
}


def srgb_to_linear(c):
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def color4(value, default=(0.8, 0.8, 0.8, 1.0)):
    if value is None:
        return tuple(default)
    if isinstance(value, str):
        s = value.strip().lower()
        if s in NAMED_COLORS:
            s = NAMED_COLORS[s].lower()
        if s.endswith("k") and s[:-1].replace(".", "").isdigit():
            rgb = kelvin_to_rgb(float(s[:-1]))
            return (*rgb, 1.0)
        s = s.lstrip("#")
        if len(s) == 3:
            s = "".join(ch * 2 for ch in s)
        if len(s) not in (6, 8) or not re.fullmatch(r"[0-9a-f]+", s):
            raise ToolError(f"Can't parse color {value!r}")
        vals = [int(s[i:i + 2], 16) / 255 for i in range(0, len(s), 2)]
        rgb = [srgb_to_linear(c) for c in vals[:3]]
        a = vals[3] if len(vals) == 4 else 1.0
        return (*rgb, a)
    vals = [float(x) for x in value]
    rgb = [srgb_to_linear(max(0.0, c)) if c <= 1.0 else c for c in vals[:3]]
    return (*rgb, vals[3] if len(vals) > 3 else 1.0)


def color3(value, default=(0.8, 0.8, 0.8)):
    return color4(value, (*default, 1.0))[:3]


def kelvin_to_rgb(kelvin):
    t = max(1000.0, min(40000.0, kelvin)) / 100.0
    if t <= 66:
        red = 255.0
        green = 99.4708025861 * math.log(t) - 161.1195681661
        blue = 0 if t <= 19 else 138.5177312231 * math.log(t - 10) - 305.0447927307
    else:
        red = 329.698727446 * ((t - 60) ** -0.1332047592)
        green = 288.1221695283 * ((t - 60) ** -0.0755148492)
        blue = 255.0
    rgb = [srgb_to_linear(min(255.0, max(0.0, c)) / 255.0) for c in (red, green, blue)]
    m = max(rgb) or 1.0
    return tuple(c / m for c in rgb)


def enum_items(owner, prop):
    try:
        setattr(owner, prop, "__claude_probe__")
    except TypeError as exc:
        found = re.findall(r"'([^']+)'", str(exc).split(" in ", 1)[-1])
        if found:
            return found
    except (AttributeError, ValueError):
        pass
    try:
        return [i.identifier for i in owner.bl_rna.properties[prop].enum_items]
    except (KeyError, AttributeError):
        return []


def set_enum(owner, prop, value, aliases=()):
    tried = [value, *aliases]
    for cand in tried:
        try:
            setattr(owner, prop, cand)
            return cand
        except (TypeError, ValueError):
            continue
    valid = enum_items(owner, prop)
    raise ToolError(f"Invalid {prop} {value!r}. Valid: {valid}")


def socket(node, *names):
    for n in names:
        for s in node.inputs:
            if s.identifier == n or s.name == n:
                return s
    return None


def out_socket(node, *names):
    for n in names:
        for s in node.outputs:
            if s.identifier == n or s.name == n:
                return s
    return node.outputs[0] if node.outputs else None


def png_payload(path, label=None):
    with open(path, "rb") as fh:
        data = base64.b64encode(fh.read()).decode("ascii")
    out = {"mime": "image/png", "base64": data}
    if label:
        out["label"] = label
    return out


def temp_path(name):
    folder = os.path.join(tempfile.gettempdir(), "claude_blender")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, name)


def save_pixels_png(pixels_float, width, height, path):
    img = bpy.data.images.new("_claude_tmp", width, height, alpha=True)
    try:
        img.pixels.foreach_set(pixels_float)
        img.filepath_raw = path
        img.file_format = "PNG"
        img.save()
    finally:
        bpy.data.images.remove(img)
    return path


def downscale_image_file(src, max_size, dst):
    img = bpy.data.images.load(src, check_existing=False)
    try:
        w, h = img.size
        if max(w, h) > max_size:
            s = max_size / max(w, h)
            img.scale(max(1, int(w * s)), max(1, int(h * s)))
        img.filepath_raw = dst
        img.file_format = "PNG"
        img.save()
        return dst, list(img.size)
    finally:
        bpy.data.images.remove(img)


def window_override():
    wm = bpy.context.window_manager
    for window in getattr(wm, "windows", []):
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                region = next((rg for rg in area.regions if rg.type == "WINDOW"), None)
                return {"window": window, "screen": window.screen, "area": area,
                        "region": region}
    if getattr(wm, "windows", None):
        w = wm.windows[0]
        return {"window": w, "screen": w.screen}
    return {}


def find_view3d():
    best = None
    wm = bpy.context.window_manager
    for window in getattr(wm, "windows", []):
        for area in window.screen.areas:
            if area.type != "VIEW_3D":
                continue
            region = next((rg for rg in area.regions if rg.type == "WINDOW"), None)
            if region is None:
                continue
            size = region.width * region.height
            if best is None or size > best[0]:
                best = (size, area, region, area.spaces.active)
    return best[1:] if best else (None, None, None)


def summarize_obj(o):
    return {"name": o.name, "type": o.type, "location": r(o.matrix_world.translation)}
