import math
import os

import bpy
from mathutils import Vector

from .core import (
    P, ToolError, color3, color4, direction_from_angles, kelvin_to_rgb,
    link_to_collection, look_at_quat, out_socket, r, resolve_objects,
    set_enum, socket, summarize_obj, tool, vec3, world_bbox,
)


PRINCIPLED_ALIASES = {
    "base_color": "Base Color", "color": "Base Color", "metallic": "Metallic",
    "roughness": "Roughness", "ior": "IOR", "alpha": "Alpha", "normal": "Normal",
    "transmission": "Transmission Weight", "emission_color": "Emission Color",
    "emission": "Emission Color", "emission_strength": "Emission Strength",
    "coat": "Coat Weight", "coat_roughness": "Coat Roughness", "sheen": "Sheen Weight",
    "sheen_tint": "Sheen Tint", "subsurface": "Subsurface Weight",
    "subsurface_radius": "Subsurface Radius", "specular": "Specular IOR Level",
    "specular_tint": "Specular Tint", "anisotropic": "Anisotropic",
    "anisotropic_rotation": "Anisotropic Rotation", "thin_film": "Thin Film Thickness",
    "thin_film_ior": "Thin Film IOR", "coat_tint": "Coat Tint",
    "diffuse_roughness": "Diffuse Roughness",
}


def _nodes(mat):
    if not mat.node_tree:
        try:
            mat.use_nodes = True
        except AttributeError:
            pass
    return mat.node_tree


def principled(mat):
    nt = _nodes(mat)
    node = next((n for n in nt.nodes if n.type == "BSDF_PRINCIPLED"), None)
    if node is None:
        node = nt.nodes.new("ShaderNodeBsdfPrincipled")
        out = next((n for n in nt.nodes if n.type == "OUTPUT_MATERIAL"), None) or \
            nt.nodes.new("ShaderNodeOutputMaterial")
        nt.links.new(node.outputs[0], out.inputs["Surface"])
    return node


def _set_socket_value(sock, value):
    if sock.type == "RGBA":
        sock.default_value = color4(value)
    elif sock.type == "VECTOR":
        sock.default_value = vec3(value) if not isinstance(value, (int, float)) else (value,) * 3
    elif sock.type == "BOOLEAN":
        sock.default_value = bool(value)
    else:
        sock.default_value = float(value)


def set_principled(mat, **values):
    node = principled(mat)
    applied = {}
    for key, value in values.items():
        if value is None:
            continue
        sname = PRINCIPLED_ALIASES.get(key, key)
        sock = socket(node, sname)
        if sock is None:
            raise ToolError(f"Principled BSDF has no input {key!r}. Use one of "
                            f"{sorted(PRINCIPLED_ALIASES)} or a socket name.")
        if sock.is_linked and key not in ("normal",):
            for link in list(sock.links):
                mat.node_tree.links.remove(link)
        _set_socket_value(sock, value)
        applied[sname] = value
    if "Base Color" in applied:
        mat.diffuse_color = color4(applied["Base Color"])
    return applied


class G:
    def __init__(self, mat):
        self.nt = _nodes(mat)
        self.nt.nodes.clear()
        self.out = self.node("ShaderNodeOutputMaterial", 600, 0)
        self.bsdf = self.node("ShaderNodeBsdfPrincipled", 250, 0)
        self.link(self.bsdf.outputs[0], self.out.inputs["Surface"])
        self._coords = None

    def node(self, kind, x, y, **props):
        n = self.nt.nodes.new(kind)
        n.location = (x, y)
        for k, v in props.items():
            setattr(n, k, v)
        return n

    def link(self, a, b):
        self.nt.links.new(a, b)

    def coords(self, scale=1.0, space="Object"):
        if self._coords is None:
            tc = self.node("ShaderNodeTexCoord", -1100, 0)
            mp = self.node("ShaderNodeMapping", -900, 0)
            self.link(tc.outputs[space], mp.inputs["Vector"])
            mp.inputs["Scale"].default_value = (scale, scale, scale)
            self._coords = mp
        return self._coords.outputs["Vector"]

    def tex(self, kind, x, y, scale=None, vector=True, **inputs):
        n = self.node(kind, x, y)
        if vector and socket(n, "Vector") is not None:
            self.link(self.coords(), socket(n, "Vector"))
        if scale is not None and socket(n, "Scale") is not None:
            socket(n, "Scale").default_value = scale
        for k, v in inputs.items():
            s = socket(n, k.replace("_", " ").title(), k)
            if s is not None:
                s.default_value = v
        return n

    def ramp(self, fac, stops, x, y):
        cr = self.node("ShaderNodeValToRGB", x, y)
        self.link(fac, socket(cr, "Fac", "Factor"))
        els = cr.color_ramp.elements
        while len(els) > len(stops):
            els.remove(els[-1])
        while len(els) < len(stops):
            els.new(0.5)
        for el, (pos, col) in zip(els, stops):
            el.position = pos
            el.color = color4(col) if not isinstance(col, (int, float)) else (col, col, col, 1)
        return cr

    def bump(self, height, strength=0.2, distance=0.02, x=0, y=-500):
        b = self.node("ShaderNodeBump", x, y)
        b.inputs["Strength"].default_value = strength
        b.inputs["Distance"].default_value = distance
        self.link(height, b.inputs["Height"])
        self.link(b.outputs["Normal"], socket(self.bsdf, "Normal"))
        return b

    def bsdf_in(self, name):
        return socket(self.bsdf, PRINCIPLED_ALIASES.get(name, name))

    def set(self, **values):
        for k, v in values.items():
            if v is not None:
                _set_socket_value(self.bsdf_in(k), v)


def _fac(node):
    return out_socket(node, "Fac", "Factor", "Distance")


def _p_simple(g, o):
    g.set(base_color=o.get("base_color", "#B0B0B0"), roughness=o.get("roughness", 0.5),
          metallic=o.get("metallic", 0.0))


def _p_plastic(g, o):
    g.set(base_color=o.get("base_color", "#D32F2F"), roughness=o.get("roughness", 0.25),
          specular=0.5, coat=o.get("coat", 0.0))


def _p_matte(g, o):
    g.set(base_color=o.get("base_color", "#BDBDBD"), roughness=o.get("roughness", 0.9),
          specular=0.3)


def _p_rubber(g, o):
    g.set(base_color=o.get("base_color", "#1E1E1E"), roughness=o.get("roughness", 0.75),
          sheen=0.3)
    n = g.tex("ShaderNodeTexNoise", -500, -500, scale=o.get("scale", 120.0), detail=2.0)
    g.bump(_fac(n), strength=o.get("bump", 0.05))


def _p_ceramic(g, o):
    g.set(base_color=o.get("base_color", "#F4F1EA"), roughness=o.get("roughness", 0.3),
          coat=1.0, coat_roughness=0.03)


def _metal(color, rough):
    def build(g, o):
        g.set(base_color=o.get("base_color", color), metallic=1.0,
              roughness=o.get("roughness", rough))
        n = g.tex("ShaderNodeTexNoise", -500, 150, scale=o.get("scale", 8.0), detail=6.0)
        mr = g.node("ShaderNodeMapRange", -250, 150)
        g.link(_fac(n), mr.inputs["Value"])
        base_r = o.get("roughness", rough)
        mr.inputs["To Min"].default_value = max(0.0, base_r - 0.08)
        mr.inputs["To Max"].default_value = min(1.0, base_r + 0.08)
        g.link(out_socket(mr, "Result"), g.bsdf_in("roughness"))
    return build


def _p_brushed(g, o):
    g.set(base_color=o.get("base_color", "#B8BCC2"), metallic=1.0,
          roughness=o.get("roughness", 0.32), anisotropic=0.8)
    tc = g.node("ShaderNodeTexCoord", -1100, -300)
    mp = g.node("ShaderNodeMapping", -900, -300)
    g.link(tc.outputs["Object"], mp.inputs["Vector"])
    s = o.get("scale", 1.0)
    mp.inputs["Scale"].default_value = (2.0 * s, 300.0 * s, 2.0 * s)
    n = g.node("ShaderNodeTexNoise", -650, -300)
    g.link(mp.outputs["Vector"], socket(n, "Vector"))
    socket(n, "Detail").default_value = 8.0
    mr = g.node("ShaderNodeMapRange", -400, -300)
    g.link(_fac(n), mr.inputs["Value"])
    base_r = o.get("roughness", 0.32)
    mr.inputs["To Min"].default_value = base_r - 0.12
    mr.inputs["To Max"].default_value = base_r + 0.12
    g.link(out_socket(mr, "Result"), g.bsdf_in("roughness"))
    g.bump(_fac(n), strength=o.get("bump", 0.04))


def _p_rusted(g, o):
    g.set(metallic=1.0)
    n = g.tex("ShaderNodeTexNoise", -650, 100, scale=o.get("scale", 3.0), detail=12.0,
              roughness=0.65)
    mask = g.ramp(_fac(n), [(0.45, (0, 0, 0)), (0.6, (1, 1, 1))], -400, 100)
    col = g.ramp(_fac(n), [(0.3, o.get("secondary_color", "#5A2A12")),
                           (0.7, "#A0522D")], -400, 350)
    mix = g.node("ShaderNodeMix", -100, 250, data_type="RGBA")
    g.link(mask.outputs["Color"], socket(mix, "Factor"))
    mix_inputs = [s for s in mix.inputs if s.type == "RGBA"]
    mix_inputs[0].default_value = color4(o.get("base_color", "#8A8D91"))
    g.link(col.outputs["Color"], mix_inputs[1])
    g.link([s for s in mix.outputs if s.type == "RGBA"][0], g.bsdf_in("base_color"))
    inv = g.node("ShaderNodeMath", -100, -100, operation="SUBTRACT")
    inv.inputs[0].default_value = 1.0
    g.link(mask.outputs["Color"], inv.inputs[1])
    g.link(inv.outputs[0], g.bsdf_in("metallic"))
    rr = g.node("ShaderNodeMapRange", -100, -300)
    g.link(mask.outputs["Color"], rr.inputs["Value"])
    rr.inputs["To Min"].default_value = 0.3
    rr.inputs["To Max"].default_value = 0.9
    g.link(out_socket(rr, "Result"), g.bsdf_in("roughness"))
    g.bump(_fac(n), strength=o.get("bump", 0.3), y=-600)


def _p_glass(g, o):
    g.set(base_color=o.get("base_color", "#FFFFFF"), roughness=o.get("roughness", 0.0),
          transmission=1.0, ior=o.get("ior", 1.45))


def _p_frosted(g, o):
    g.set(base_color=o.get("base_color", "#F2F6F8"), roughness=o.get("roughness", 0.35),
          transmission=1.0, ior=1.45)


def _p_water(g, o):
    g.set(base_color=o.get("base_color", "#D6EEF2"), roughness=0.02, transmission=1.0,
          ior=1.333)
    n = g.tex("ShaderNodeTexNoise", -500, -500, scale=o.get("scale", 4.0), detail=4.0)
    g.bump(_fac(n), strength=o.get("bump", 0.25), distance=0.05)


def _p_ice(g, o):
    g.set(base_color=o.get("base_color", "#DDF3FF"), roughness=0.08, transmission=0.95,
          ior=1.31, subsurface=0.1)
    n = g.tex("ShaderNodeTexVoronoi", -500, -500, scale=o.get("scale", 6.0))
    g.bump(_fac(n), strength=o.get("bump", 0.15))


def _p_wood(g, o):
    s = o.get("scale", 1.0)
    tc = g.node("ShaderNodeTexCoord", -1300, 0)
    mp = g.node("ShaderNodeMapping", -1100, 0)
    g.link(tc.outputs["Object"], mp.inputs["Vector"])
    mp.inputs["Scale"].default_value = (0.08 * s, 1.0 * s, 1.0 * s)
    noise = g.node("ShaderNodeTexNoise", -900, -200)
    g.link(mp.outputs["Vector"], socket(noise, "Vector"))
    socket(noise, "Scale").default_value = 3.0
    socket(noise, "Detail").default_value = 6.0
    wave = g.node("ShaderNodeTexWave", -650, 0, wave_type="RINGS")
    wave.rings_direction = "X"
    g.link(mp.outputs["Vector"], socket(wave, "Vector"))
    socket(wave, "Scale").default_value = 9.0
    socket(wave, "Distortion").default_value = 3.0
    socket(wave, "Detail").default_value = 2.0
    g.link(_fac(noise), socket(wave, "Phase Offset") or socket(wave, "Detail Scale"))
    fib_map = g.node("ShaderNodeMapping", -900, -450)
    g.link(tc.outputs["Object"], fib_map.inputs["Vector"])
    fib_map.inputs["Scale"].default_value = (2.0 * s, 120.0 * s, 120.0 * s)
    fib = g.node("ShaderNodeTexNoise", -650, -450)
    g.link(fib_map.outputs["Vector"], socket(fib, "Vector"))
    socket(fib, "Detail").default_value = 4.0
    mix = g.node("ShaderNodeMath", -450, -150, operation="MULTIPLY_ADD")
    g.link(_fac(fib), mix.inputs[0])
    mix.inputs[1].default_value = 0.25
    g.link(_fac(wave), mix.inputs[2])
    base = o.get("base_color", "#9A6A3F")
    col = g.ramp(mix.outputs[0], [(0.15, base),
                                  (0.75, o.get("secondary_color", "#7A5030")),
                                  (1.05, "#5E3B22")], -250, 0)
    g.link(col.outputs["Color"], g.bsdf_in("base_color"))
    rr = g.node("ShaderNodeMapRange", -150, -250)
    g.link(mix.outputs[0], rr.inputs["Value"])
    base_r = o.get("roughness", 0.5)
    rr.inputs["To Min"].default_value = base_r - 0.08
    rr.inputs["To Max"].default_value = base_r + 0.08
    g.link(out_socket(rr, "Result"), g.bsdf_in("roughness"))
    g.bump(mix.outputs[0], strength=o.get("bump", 0.05), y=-550)
    if o.get("coat"):
        g.set(coat=o["coat"], coat_roughness=0.08)


def _p_marble(g, o):
    n = g.tex("ShaderNodeTexNoise", -800, 0, scale=o.get("scale", 2.0), detail=14.0,
              roughness=0.6, distortion=4.0)
    wave = g.node("ShaderNodeTexWave", -550, 0)
    g.link(g.coords(), socket(wave, "Vector"))
    socket(wave, "Scale").default_value = 1.5 * o.get("scale", 2.0) / 2.0
    socket(wave, "Distortion").default_value = 12.0
    g.link(_fac(n), socket(wave, "Phase Offset") or socket(wave, "Detail Scale"))
    col = g.ramp(_fac(wave), [(0.0, o.get("secondary_color", "#3B3B3B")),
                              (0.12, o.get("base_color", "#F3F1EC")),
                              (1.0, o.get("base_color", "#F3F1EC"))], -300, 0)
    g.link(col.outputs["Color"], g.bsdf_in("base_color"))
    g.set(roughness=o.get("roughness", 0.12), coat=0.6, coat_roughness=0.05,
          subsurface=0.05)


def _p_concrete(g, o):
    n = g.tex("ShaderNodeTexNoise", -800, 0, scale=o.get("scale", 6.0), detail=10.0,
              roughness=0.7)
    v = g.tex("ShaderNodeTexVoronoi", -800, -300, scale=o.get("scale", 6.0) * 8)
    col = g.ramp(_fac(n), [(0.3, o.get("secondary_color", "#7A7A76")),
                           (0.7, o.get("base_color", "#A9A8A2"))], -450, 0)
    g.link(col.outputs["Color"], g.bsdf_in("base_color"))
    g.set(roughness=o.get("roughness", 0.85))
    add = g.node("ShaderNodeMath", -450, -300, operation="ADD")
    g.link(_fac(n), add.inputs[0])
    g.link(_fac(v), add.inputs[1])
    g.bump(add.outputs[0], strength=o.get("bump", 0.25))


def _p_fabric(g, o):
    g.set(base_color=o.get("base_color", "#3F51B5"), roughness=o.get("roughness", 0.85),
          sheen=1.0, sheen_tint="#FFFFFF")
    wave_a = g.tex("ShaderNodeTexWave", -600, -300, scale=o.get("scale", 120.0))
    wave_b = g.tex("ShaderNodeTexWave", -600, -550, scale=o.get("scale", 120.0))
    wave_b.bands_direction = "Y"
    mult = g.node("ShaderNodeMath", -350, -400, operation="MULTIPLY")
    g.link(_fac(wave_a), mult.inputs[0])
    g.link(_fac(wave_b), mult.inputs[1])
    g.bump(mult.outputs[0], strength=o.get("bump", 0.15))


def _p_leather(g, o):
    g.set(base_color=o.get("base_color", "#5D3A22"), roughness=o.get("roughness", 0.55),
          coat=0.2, coat_roughness=0.4)
    v = g.tex("ShaderNodeTexVoronoi", -600, -400, scale=o.get("scale", 60.0))
    g.bump(_fac(v), strength=o.get("bump", 0.2))


def _p_car_paint(g, o):
    g.set(base_color=o.get("base_color", "#B71C1C"), metallic=0.6,
          roughness=o.get("roughness", 0.35), coat=1.0, coat_roughness=0.02)
    flakes = g.tex("ShaderNodeTexVoronoi", -600, -400, scale=o.get("scale", 900.0))
    g.bump(_fac(flakes), strength=o.get("bump", 0.06))


def _p_emission(g, o):
    c = o.get("base_color", "#FF2BD6")
    g.set(base_color="#050505", emission_color=c,
          emission_strength=o.get("emission_strength", 15.0), roughness=0.4)


def _p_skin(g, o):
    g.set(base_color=o.get("base_color", "#E0AC8F"), roughness=o.get("roughness", 0.45),
          subsurface=0.25, subsurface_radius=(1.0, 0.35, 0.2))
    n = g.tex("ShaderNodeTexNoise", -500, -500, scale=o.get("scale", 200.0), detail=3.0)
    g.bump(_fac(n), strength=o.get("bump", 0.05))


def _p_brick(g, o):
    s = o.get("scale", 4.0)
    b = g.tex("ShaderNodeTexBrick", -600, 0, scale=s)
    socket(b, "Color1").default_value = color4(o.get("base_color", "#8E3B2E"))
    socket(b, "Color2").default_value = color4(o.get("secondary_color", "#6E2C22"))
    socket(b, "Mortar").default_value = color4("#BDB6A8")
    socket(b, "Mortar Size").default_value = 0.015
    g.link(out_socket(b, "Color"), g.bsdf_in("base_color"))
    g.set(roughness=o.get("roughness", 0.85))
    inv = g.node("ShaderNodeMath", -300, -400, operation="SUBTRACT")
    inv.inputs[0].default_value = 1.0
    g.link(_fac(b), inv.inputs[1])
    g.bump(inv.outputs[0], strength=o.get("bump", 0.5))


def _p_tiles(g, o):
    s = o.get("scale", 3.0)
    b = g.tex("ShaderNodeTexBrick", -600, 0, scale=s)
    b.offset = 0.0
    socket(b, "Brick Width").default_value = 0.5
    socket(b, "Row Height").default_value = 0.5
    socket(b, "Color1").default_value = color4(o.get("base_color", "#EDEDED"))
    socket(b, "Color2").default_value = color4(o.get("secondary_color", "#E2E2E2"))
    socket(b, "Mortar").default_value = color4("#9E9E9E")
    socket(b, "Mortar Size").default_value = 0.01
    g.link(out_socket(b, "Color"), g.bsdf_in("base_color"))
    g.set(roughness=o.get("roughness", 0.15), coat=0.5)
    inv = g.node("ShaderNodeMath", -300, -400, operation="SUBTRACT")
    inv.inputs[0].default_value = 1.0
    g.link(_fac(b), inv.inputs[1])
    g.bump(inv.outputs[0], strength=o.get("bump", 0.4))


def _p_checker(g, o):
    c = g.tex("ShaderNodeTexChecker", -500, 0, scale=o.get("scale", 10.0))
    socket(c, "Color1").default_value = color4(o.get("base_color", "#EEEEEE"))
    socket(c, "Color2").default_value = color4(o.get("secondary_color", "#333333"))
    g.link(out_socket(c, "Color"), g.bsdf_in("base_color"))
    g.set(roughness=o.get("roughness", 0.5))


def _p_ground(g, o):
    n = g.tex("ShaderNodeTexNoise", -800, 0, scale=o.get("scale", 0.6), detail=8.0)
    col = g.ramp(_fac(n), [(0.35, o.get("secondary_color", "#3E5C2A")),
                           (0.65, o.get("base_color", "#6B7F3A"))], -450, 0)
    g.link(col.outputs["Color"], g.bsdf_in("base_color"))
    g.set(roughness=o.get("roughness", 0.95))
    fine = g.tex("ShaderNodeTexNoise", -800, -350, scale=o.get("scale", 0.6) * 60, detail=4.0)
    g.bump(_fac(fine), strength=o.get("bump", 0.3))


def _p_holographic(g, o):
    g.set(base_color=o.get("base_color", "#C9CED6"), metallic=1.0,
          roughness=o.get("roughness", 0.15), thin_film=500.0, thin_film_ior=1.6)
    n = g.tex("ShaderNodeTexNoise", -600, -400, scale=o.get("scale", 3.0), detail=2.0)
    mr = g.node("ShaderNodeMapRange", -300, -400)
    g.link(_fac(n), mr.inputs["Value"])
    mr.inputs["To Min"].default_value = 250.0
    mr.inputs["To Max"].default_value = 900.0
    g.link(out_socket(mr, "Result"), g.bsdf_in("thin_film"))


def _p_toon(g, o):
    nt = g.nt
    nt.nodes.remove(g.bsdf)
    diff = g.node("ShaderNodeBsdfDiffuse", -500, 0)
    diff.inputs["Color"].default_value = color4(o.get("base_color", "#4FC3F7"))
    try:
        s2r = g.node("ShaderNodeShaderToRGB", -300, 0)
    except RuntimeError:
        raise ToolError("toon needs EEVEE (Shader to RGB)")
    g.link(diff.outputs[0], s2r.inputs[0])
    ramp = g.ramp(s2r.outputs["Color"], [(0.0, "#1A1A2E"), (0.35,
                                                            o.get("base_color", "#4FC3F7")),
                                         (0.8, "#FFFFFF")], -100, 0)
    ramp.color_ramp.interpolation = "CONSTANT"
    em = g.node("ShaderNodeEmission", 200, 0)
    g.link(ramp.outputs["Color"], em.inputs["Color"])
    g.link(em.outputs[0], g.out.inputs["Surface"])


PRESETS = {
    "plastic": (_p_plastic, "Glossy dielectric plastic"),
    "matte": (_p_matte, "Flat diffuse paint / clay"),
    "rubber": (_p_rubber, "Dark rough rubber with micro bump"),
    "ceramic": (_p_ceramic, "Glazed ceramic with clear coat"),
    "chrome": (_metal("#E8E8E8", 0.04), "Mirror chrome"),
    "polished_metal": (_metal("#C8CBD0", 0.12), "Polished steel"),
    "brushed_metal": (_p_brushed, "Anisotropic brushed steel/aluminium"),
    "gold": (_metal("#FFC64A", 0.15), "Polished gold"),
    "copper": (_metal("#E08A5E", 0.2), "Copper"),
    "brass": (_metal("#D9B65A", 0.25), "Brass"),
    "aluminum": (_metal("#D5D8DC", 0.3), "Satin aluminium"),
    "iron": (_metal("#6E6F72", 0.5), "Dull cast iron"),
    "rusted_metal": (_p_rusted, "Metal with procedural rust patches"),
    "glass": (_p_glass, "Clear glass"),
    "frosted_glass": (_p_frosted, "Frosted / sandblasted glass"),
    "water": (_p_water, "Water surface with ripples"),
    "ice": (_p_ice, "Translucent ice"),
    "wood": (_p_wood, "Procedural wood grain (set coat for varnish)"),
    "marble": (_p_marble, "Polished veined marble"),
    "concrete": (_p_concrete, "Rough concrete"),
    "fabric": (_p_fabric, "Woven fabric with sheen"),
    "leather": (_p_leather, "Grained leather"),
    "car_paint": (_p_car_paint, "Metallic flake paint with clear coat"),
    "emission": (_p_emission, "Glowing neon / light panel (emission_strength)"),
    "skin": (_p_skin, "Subsurface skin"),
    "brick": (_p_brick, "Brick wall (UV-less, object space)"),
    "tiles": (_p_tiles, "Square ceramic tiles"),
    "checker": (_p_checker, "Checkerboard"),
    "ground": (_p_ground, "Grass/dirt ground blend"),
    "holographic": (_p_holographic, "Iridescent thin-film metal"),
    "toon": (_p_toon, "Cel-shaded (EEVEE only)"),
    "simple": (_p_simple, "Plain Principled BSDF"),
}


def build_preset(mat, preset, overrides):
    if preset not in PRESETS:
        raise ToolError(f"Unknown preset {preset!r}. Presets: {sorted(PRESETS)}")
    g = G(mat)
    PRESETS[preset][0](g, overrides)
    plain = {k: overrides[k] for k in ("metallic", "ior", "alpha", "transmission",
                                         "emission_strength", "coat", "subsurface", "sheen")
             if k in overrides and overrides[k] is not None}
    if preset in ("simple", "plastic", "matte", "ceramic", "glass", "frosted_glass"):
        plain.update({k: overrides[k] for k in ("base_color", "roughness")
                      if overrides.get(k) is not None})
    if plain:
        set_principled(mat, **plain)
    if (overrides.get("alpha") or 1.0) < 1.0 or preset in ("glass", "frosted_glass", "water", "ice"):
        for attr, val in (("surface_render_method", "DITHERED"), ("blend_method", "BLEND")):
            if hasattr(mat, attr):
                try:
                    setattr(mat, attr, val)
                except (TypeError, ValueError):
                    pass
                break
    bc = overrides.get("base_color")
    if bc is not None:
        mat.diffuse_color = color4(bc)
    mat["claude_preset"] = preset
    return mat


def material_from_spec(spec):
    if not isinstance(spec, str):
        c = color4(spec)
        key = "Color_" + "".join(f"{int(min(1, max(0, v)) * 255):02X}" for v in c[:3])
        mat = bpy.data.materials.get(key) or bpy.data.materials.new(key)
        set_principled(mat, base_color=spec, roughness=0.5)
        return mat
    if spec in bpy.data.materials:
        return bpy.data.materials[spec]
    low = spec.lower().replace(" ", "_").replace("-", "_")
    if low in PRESETS:
        mat = bpy.data.materials.new(low.replace("_", " ").title().replace(" ", ""))
        return build_preset(mat, low, {})
    try:
        c = color4(spec)
    except ToolError:
        raise ToolError(f"{spec!r} is not a material, preset ({sorted(PRESETS)}) or color")
    mat = bpy.data.materials.new(f"Color_{spec.strip('#')}")
    set_principled(mat, base_color=spec, roughness=0.5)
    return mat


@tool("create_material", """
Create a physically based material from a procedural preset and optionally
assign it. Presets build real node graphs (object-space textures, so no UVs
needed): plastic, matte, rubber, ceramic, chrome, polished_metal,
brushed_metal, gold, copper, brass, aluminum, iron, rusted_metal, glass,
frosted_glass, water, ice, wood, marble, concrete, fabric, leather,
car_paint, emission, skin, brick, tiles, checker, ground, holographic, toon,
simple. Tune with base_color, secondary_color (veins/rust/grain/mortar),
roughness, metallic, scale (texture frequency), bump, emission_strength, coat.
""", "Look",
      preset=P.str("Preset", "simple"),
      name=P.str("Material name (default from preset)"),
      base_color=P.color("Main color"),
      secondary_color=P.color("Secondary color (veins, rust, grain, mortar, checker)"),
      roughness=P.num("Roughness 0-1", minimum=0, maximum=1),
      metallic=P.num("Metallic 0-1", minimum=0, maximum=1),
      scale=P.num("Texture scale / frequency multiplier"),
      bump=P.num("Bump strength"),
      emission_strength=P.num("Emission strength (emission preset glows in base_color)"),
      coat=P.num("Clear coat weight 0-1"),
      ior=P.num("Index of refraction"),
      alpha=P.num("Opacity 0-1"),
      assign_to=P.objects("Objects to assign it to (replaces slot 0)"),
      replace=P.bool("Rebuild an existing material of the same name instead of making .001", True))
def create_material(preset="simple", name=None, base_color=None, secondary_color=None,
                    roughness=None, metallic=None, scale=None, bump=None,
                    emission_strength=None, coat=None, ior=None, alpha=None, assign_to=None,
                    replace=True):
    from .tools_build import assign_material
    preset = preset.lower().replace(" ", "_").replace("-", "_")
    name = name or preset.replace("_", " ").title().replace(" ", "")
    mat = bpy.data.materials.get(name) if replace else None
    mat = mat or bpy.data.materials.new(name)
    overrides = {k: v for k, v in dict(
        base_color=base_color, secondary_color=secondary_color, roughness=roughness,
        metallic=metallic, scale=scale, bump=bump, emission_strength=emission_strength,
        coat=coat, ior=ior, alpha=alpha).items() if v is not None}
    build_preset(mat, preset, overrides)
    assigned = []
    if assign_to is not None:
        for o in resolve_objects(assign_to):
            if assign_material(o, mat):
                assigned.append(o.name)
    return {"material": mat.name, "preset": preset, "assigned_to": assigned,
            "nodes": len(mat.node_tree.nodes)}


TEXTURE_KINDS = ["noise", "voronoi", "wave", "musgrave", "checker", "brick", "gradient",
                 "image", "magic"]


@tool("edit_material", """
Change an existing material. `set` takes Principled inputs by friendly name
(base_color, roughness, metallic, ior, alpha, transmission, emission_color,
emission_strength, coat, coat_roughness, sheen, subsurface, specular,
anisotropic, thin_film) or exact socket name. `texture` drives one input from
a texture: {"kind": noise|voronoi|wave|checker|brick|gradient|magic|image,
"to": base_color|roughness|metallic|bump|emission_color, "scale": 5,
"colors": [c1, c2] (for color inputs), "range": [min, max] (for values),
"strength": 0.3 (bump), "path": "/abs/file.png" (image), "projection":
"uv"|"box"}. describe=true returns the node graph.
""", "Look",
      material=P.str("Material name", required=True),
      set=P.dict("Principled input values"),
      texture=P.dict("Texture to plug into an input (see description)"),
      describe=P.bool("Return the node graph", False))
def edit_material(material, set=None, texture=None, describe=False):
    mat = bpy.data.materials.get(material)
    if mat is None:
        raise ToolError(f"No material {material!r}. Materials: "
                        f"{[m.name for m in bpy.data.materials][:40]}")
    applied = set_principled(mat, **set) if set else {}
    added = None
    if texture:
        added = _add_texture(mat, texture)
    out = {"material": mat.name, "applied": applied}
    if added:
        out["texture"] = added
    if describe or not (set or texture):
        nt = mat.node_tree
        out["nodes"] = [{"name": n.name, "type": n.bl_idname,
                         "inputs": {s.name: (_val(s) if not s.is_linked else "linked")
                                    for s in n.inputs if s.enabled and hasattr(s, "default_value")}}
                        for n in nt.nodes]
        out["links"] = [f"{l.from_node.name}.{l.from_socket.name} -> {l.to_node.name}.{l.to_socket.name}"
                        for l in nt.links]
    return out


def _val(s):
    v = s.default_value
    try:
        return r(list(v), 3)
    except TypeError:
        return r(v, 3) if isinstance(v, float) else v


def _add_texture(mat, spec):
    kind = str(spec.get("kind", "noise")).lower()
    to = str(spec.get("to", "base_color")).lower()
    nt = mat.node_tree
    bsdf = principled(mat)
    x0 = bsdf.location.x - 700
    y0 = bsdf.location.y - 200 * len([n for n in nt.nodes if n.location.x < bsdf.location.x])
    tc = nt.nodes.new("ShaderNodeTexCoord")
    tc.location = (x0 - 400, y0)
    mp = nt.nodes.new("ShaderNodeMapping")
    mp.location = (x0 - 200, y0)
    proj = spec.get("projection", "uv" if kind == "image" else "object")
    nt.links.new(tc.outputs["UV" if proj == "uv" else "Object"], mp.inputs["Vector"])
    s = float(spec.get("scale", 1.0 if kind == "image" else 5.0))
    if kind == "image":
        path = os.path.abspath(os.path.expanduser(str(spec.get("path", ""))))
        if not os.path.exists(path):
            raise ToolError(f"Image not found: {path}")
        tex = nt.nodes.new("ShaderNodeTexImage")
        tex.image = bpy.data.images.load(path, check_existing=True)
        if to not in ("base_color", "emission_color"):
            try:
                tex.image.colorspace_settings.name = "Non-Color"
            except TypeError:
                pass
        if proj == "box":
            tex.projection = "BOX"
            tex.projection_blend = 0.2
        mp.inputs["Scale"].default_value = (s, s, s)
    else:
        idname = {"noise": "ShaderNodeTexNoise", "voronoi": "ShaderNodeTexVoronoi",
                  "wave": "ShaderNodeTexWave", "musgrave": "ShaderNodeTexNoise",
                  "checker": "ShaderNodeTexChecker", "brick": "ShaderNodeTexBrick",
                  "gradient": "ShaderNodeTexGradient", "magic": "ShaderNodeTexMagic"}.get(kind)
        if idname is None:
            raise ToolError(f"Unknown texture kind {kind!r}. Kinds: {TEXTURE_KINDS}")
        tex = nt.nodes.new(idname)
        sc = socket(tex, "Scale")
        if sc is not None:
            sc.default_value = s
        if kind in ("noise", "musgrave"):
            socket(tex, "Detail").default_value = float(spec.get("detail", 6.0))
    tex.location = (x0, y0)
    nt.links.new(mp.outputs["Vector"], tex.inputs["Vector"])
    fac = out_socket(tex, "Fac", "Factor", "Distance", "Color")
    color_out = out_socket(tex, "Color")

    if to == "bump":
        bump = nt.nodes.new("ShaderNodeBump")
        bump.location = (x0 + 300, y0 - 200)
        bump.inputs["Strength"].default_value = float(spec.get("strength", 0.3))
        nt.links.new(fac, bump.inputs["Height"])
        nt.links.new(bump.outputs["Normal"], socket(bsdf, "Normal"))
        return {"kind": kind, "to": "bump"}
    target = socket(bsdf, PRINCIPLED_ALIASES.get(to, to))
    if target is None:
        raise ToolError(f"Unknown input {to!r}")
    if target.type == "RGBA":
        if kind == "image":
            nt.links.new(color_out, target)
        else:
            cr = nt.nodes.new("ShaderNodeValToRGB")
            cr.location = (x0 + 250, y0)
            nt.links.new(fac, socket(cr, "Fac", "Factor"))
            cols = spec.get("colors") or ["#202020", "#E0E0E0"]
            cr.color_ramp.elements[0].color = color4(cols[0])
            cr.color_ramp.elements[1].color = color4(cols[-1])
            nt.links.new(cr.outputs["Color"], target)
    else:
        lo, hi = (spec.get("range") or [0.2, 0.8])[:2]
        mr = nt.nodes.new("ShaderNodeMapRange")
        mr.location = (x0 + 250, y0)
        nt.links.new(fac, mr.inputs["Value"])
        mr.inputs["To Min"].default_value = lo
        mr.inputs["To Max"].default_value = hi
        nt.links.new(out_socket(mr, "Result"), target)
    return {"kind": kind, "to": target.name}


RIG_COLLECTION = "Claude Lights"

LIGHT_RIGS = {
    "three_point": [("Key", "AREA", 40, 35, 3.0, 1.0, 4800, 1.2),
                    ("Fill", "AREA", -55, 15, 3.2, 0.3, 6800, 2.0),
                    ("Rim", "AREA", 165, 40, 3.0, 0.9, 7500, 0.6)],
    "studio_soft": [("Softbox L", "AREA", -45, 30, 3.0, 0.7, 5600, 2.5),
                    ("Softbox R", "AREA", 50, 30, 3.0, 0.7, 5600, 2.5),
                    ("Top", "AREA", 0, 85, 3.0, 0.5, 6000, 2.0)],
    "product": [("Key", "AREA", 20, 55, 2.8, 1.0, 5600, 2.2),
                ("Strip L", "AREA", -110, 20, 2.6, 0.7, 6500, 0.5),
                ("Strip R", "AREA", 110, 20, 2.6, 0.7, 6500, 0.5),
                ("Kicker", "AREA", 180, 10, 3.0, 0.4, 7000, 1.0)],
    "dramatic": [("Key", "SPOT", 75, 25, 3.0, 1.6, 3600, 0.15),
                 ("Rim", "AREA", -150, 35, 3.0, 0.5, 8000, 0.4)],
    "rembrandt": [("Key", "AREA", 45, 45, 2.8, 1.0, 4300, 0.8),
                  ("Fill", "AREA", -60, 10, 3.5, 0.12, 6000, 2.0)],
    "neon": [("Magenta", "AREA", -80, 15, 2.5, 1.0, None, 1.0),
             ("Cyan", "AREA", 80, 15, 2.5, 1.0, None, 1.0),
             ("Top", "AREA", 0, 80, 3.0, 0.15, 7000, 1.5)],
    "silhouette": [("Backlight", "AREA", 180, 15, 2.5, 2.5, 6000, 2.5)],
    "golden_hour": [("Sky fill", "AREA", -120, 40, 4.0, 0.12, 9000, 3.0)],
    "midday": [],
    "overcast": [("Cloud layer", "AREA", 0, 89, 3.0, 0.35, 7000, 5.0)],
    "night": [("Moon", "SUN", -40, 35, 0, 0.25, 9500, 0),
              ("Practical", "POINT", 60, 10, 1.8, 0.6, 2700, 0.1)],
}
NEON_COLORS = {"Magenta": "#FF2BD6", "Cyan": "#00E5FF"}
WORLD_FOR_RIG = {
    "three_point": ("color", "#1B1D22", 1.0), "studio_soft": ("color", "#3A3C40", 1.0),
    "product": ("color", "#F2F2F2", 0.6), "dramatic": ("color", "#050507", 1.0),
    "rembrandt": ("color", "#0A0A0C", 1.0), "neon": ("color", "#07040F", 1.0),
    "silhouette": ("color", "#0B0B0B", 1.0), "golden_hour": ("sky", 6, 0.25),
    "midday": ("sky", 58, 0.3), "overcast": ("color", "#B9C3CC", 0.9),
    "night": ("color", "#03060F", 1.0),
}


def _apply_light_color(light, kelvin=None, color=None):
    if color is not None:
        light.color = color3(color)
        if hasattr(light, "use_temperature"):
            light.use_temperature = False
    elif kelvin:
        if hasattr(light, "use_temperature"):
            light.use_temperature = True
            light.temperature = kelvin
            light.color = (1, 1, 1)
        else:
            light.color = kelvin_to_rgb(kelvin)


def _make_light(name, ltype, coll, location, target, energy, size, kelvin=None, color=None):
    data = bpy.data.lights.new(name, ltype)
    data.energy = energy
    if ltype == "AREA":
        data.shape = "RECTANGLE"
        data.size, data.size_y = size, size * 0.75
    elif ltype == "SPOT":
        data.spot_size = math.radians(35)
        data.spot_blend = 0.35
        data.shadow_soft_size = size
    elif ltype == "POINT":
        data.shadow_soft_size = size
    elif ltype == "SUN":
        data.angle = math.radians(1.2)
    _apply_light_color(data, kelvin, color)
    obj = bpy.data.objects.new(name, data)
    coll.objects.link(obj)
    obj.location = location
    obj.rotation_euler = look_at_quat(location, target).to_euler()
    return obj


def _rig_collection(replace):
    coll = bpy.data.collections.get(RIG_COLLECTION)
    removed = []
    if coll and replace:
        for o in list(coll.objects):
            removed.append(o.name)
            data = o.data
            bpy.data.objects.remove(o, do_unlink=True)
            if data is not None and data.users == 0:
                pool = bpy.data.lights if isinstance(data, bpy.types.Light) else bpy.data.meshes
                pool.remove(data)
    if coll is None:
        coll = bpy.data.collections.new(RIG_COLLECTION)
        bpy.context.scene.collection.children.link(coll)
    return coll, removed


@tool("lighting_rig", """
Build a complete, correctly scaled lighting setup around `target` (default:
all visible geometry) and match the world to it. Presets: three_point,
studio_soft, product (with optional seamless backdrop), dramatic, rembrandt,
neon, silhouette, golden_hour (low warm sun + sky), midday, overcast, night.
Lights are sized and powered from the subject's size, color temperatures
are physically based, and everything goes in a 'Claude Lights' collection so
re-running replaces the previous rig. rotate turns the whole rig around Z;
intensity scales all lights; disable_other_lights hides lights outside the rig.
""", "Look",
      preset=P.str("Rig preset", "three_point", enum=sorted(LIGHT_RIGS)),
      target=P.objects("Subject to light"),
      intensity=P.num("Multiplier on all light power", 1.0),
      rotate=P.num("Rotate the rig around the subject, degrees", 0.0),
      backdrop=P.bool("Add a seamless curved backdrop (cyclorama) under/behind the subject",
                      False),
      backdrop_color=P.color("Backdrop color", "#E8E8E8"),
      set_world=P.bool("Also set a matching world", True),
      replace=P.bool("Remove the previous Claude rig first", True),
      disable_other_lights=P.bool("Hide lights not in the rig", False))
def lighting_rig(preset="three_point", target=None, intensity=1.0, rotate=0.0, backdrop=False,
                 backdrop_color="#E8E8E8", set_world=True, replace=True,
                 disable_other_lights=False):
    if preset not in LIGHT_RIGS:
        raise ToolError(f"Unknown preset {preset!r}. Presets: {sorted(LIGHT_RIGS)}")
    targets = resolve_objects(target) if target is not None else [
        o for o in bpy.context.scene.objects
        if o.visible_get() and o.type in {"MESH", "CURVE", "FONT", "SURFACE", "META"}
        and RIG_COLLECTION not in [c.name for c in o.users_collection]
        and not o.name.startswith("Backdrop")]
    bb = world_bbox(targets)
    if bb is None:
        center, radius, floor = Vector((0, 0, 0.5)), 1.0, 0.0
    else:
        lo, hi = bb
        center, radius, floor = (lo + hi) / 2, max((hi - lo).length / 2, 0.1), lo.z
    coll, removed = _rig_collection(replace)
    stray = bpy.data.objects.get(SKY_SUN)
    if replace and stray is not None and WORLD_FOR_RIG[preset][0] != "sky":
        removed.append(stray.name)
        data = stray.data
        bpy.data.objects.remove(stray, do_unlink=True)
        if data.users == 0:
            bpy.data.lights.remove(data)
    created = []
    for role, ltype, az, el, dmult, power, kelvin, smult in LIGHT_RIGS[preset]:
        if ltype == "SUN":
            d = direction_from_angles(az + rotate, el)
            loc = center + d * radius * 4
            energy = power * intensity
            obj = _make_light(f"{preset}_{role}", "SUN", coll, loc, center, energy, 0, kelvin)
        else:
            dist = radius * dmult
            loc = center + direction_from_angles(az + rotate, el) * dist
            base = {"AREA": 250.0, "SPOT": 900.0, "POINT": 300.0}[ltype]
            energy = base * power * intensity * (dist / 3.0) ** 2
            obj = _make_light(f"{preset}_{role}", ltype, coll, loc, center, energy,
                              max(0.05, radius * smult), kelvin, NEON_COLORS.get(role))
        created.append(obj)
    if backdrop:
        created.append(_backdrop(center, radius, floor, backdrop_color, coll))
    hidden = []
    if disable_other_lights:
        for o in bpy.context.scene.objects:
            if o.type == "LIGHT" and o not in created:
                o.hide_set(True)
                o.hide_render = True
                hidden.append(o.name)
    world = None
    if set_world:
        mode, a, strength = WORLD_FOR_RIG[preset]
        if mode == "sky":
            world = set_world_impl(mode="sky", sun_elevation=a, sun_azimuth=rotate + 55,
                                   strength=strength)
            sun = bpy.data.objects.get(SKY_SUN)
            if sun is not None:
                for c in list(sun.users_collection):
                    c.objects.unlink(sun)
                coll.objects.link(sun)
                created.append(sun)
        else:
            world = set_world_impl(mode="color", color=a, strength=strength * 0.15
                                   if preset not in ("overcast", "product") else strength)
    return {"preset": preset, "created": [summarize_obj(o) for o in created],
            "replaced": removed, "subject_center": r(center), "subject_radius": r(radius),
            "world": world, "hidden_lights": hidden,
            "tip": "Check with look(view='camera', shading='rendered') or render_image."}


def _backdrop(center, radius, floor, color, coll):
    import bmesh
    w = radius * 8
    depth, height, bend = radius * 6, radius * 5, radius * 1.5
    bm = bmesh.new()
    profile = []
    profile.append((-depth / 2 - bend, 0.0))
    steps = 12
    for i in range(steps + 1):
        a = math.pi / 2 * i / steps
        profile.append((depth / 2 - bend + math.sin(a) * bend, bend - math.cos(a) * bend))
    profile.append((depth / 2, height))
    rows = []
    for x in (-w / 2, w / 2):
        rows.append([bm.verts.new((x, y, z)) for (y, z) in profile])
    for i in range(len(profile) - 1):
        bm.faces.new((rows[0][i], rows[1][i], rows[1][i + 1], rows[0][i + 1]))
    me = bpy.data.meshes.new("Backdrop")
    bm.to_mesh(me)
    bm.free()
    for p in me.polygons:
        p.use_smooth = True
    obj = bpy.data.objects.new("Backdrop", me)
    coll.objects.link(obj)
    obj.location = (center.x, center.y, floor)
    mat = bpy.data.materials.get("Backdrop") or bpy.data.materials.new("Backdrop")
    set_principled(mat, base_color=color, roughness=0.7)
    me.materials.append(mat)
    return obj


@tool("add_light", """
Add a single light aimed at a target. kind: point, sun, spot, area. Power in
watts (sun: irradiance strength), color as hex/name or temperature like
'3200K'. Place by explicit location, or by azimuth/elevation/distance around
the target.
""", "Look",
      kind=P.str("Light type", "area", enum=["point", "sun", "spot", "area"]),
      name=P.str("Name"),
      target=P.objects("What to aim at (object name) -- also the orbit center"),
      location=P.vec3("Explicit location"),
      azimuth=P.num("Orbit azimuth (0 = front, 90 = right)", 45),
      elevation=P.num("Orbit elevation", 35),
      distance=P.num("Orbit distance (default: 3x subject radius)"),
      power=P.num("Watts (sun: strength)", 500.0),
      color=P.color("Color or temperature string like '3200K'", "#FFFFFF"),
      size=P.num("Area size / soft shadow radius (m)", 1.0),
      spot_angle=P.num("Spot cone angle in degrees", 45.0))
def add_light(kind="area", name=None, target=None, location=None, azimuth=45, elevation=35,
              distance=None, power=500.0, color="#FFFFFF", size=1.0, spot_angle=45.0):
    tgts = resolve_objects(target) if target is not None else []
    bb = world_bbox(tgts) if tgts else None
    center = (bb[0] + bb[1]) / 2 if bb else Vector((0, 0, 0))
    radius = max((bb[1] - bb[0]).length / 2, 0.1) if bb else 1.0
    loc = vec3(location) if location is not None else \
        center + direction_from_angles(azimuth, elevation) * (distance or radius * 3)
    kelvin = None
    if isinstance(color, str) and color.strip().lower().endswith("k"):
        kelvin, color = float(color.strip()[:-1]), None
    coll = bpy.context.collection or bpy.context.scene.collection
    obj = _make_light(name or f"{kind.title()} Light", kind.upper(), coll, loc, center, power,
                      size, kelvin, color)
    if kind == "spot":
        obj.data.spot_size = math.radians(spot_angle)
    return {"created": [summarize_obj(obj)], "aimed_at": r(center)}


BUILTIN_HDRIS = ["city", "courtyard", "forest", "interior", "night", "studio", "sunrise",
                 "sunset"]


def _builtin_hdri(name):
    base = bpy.utils.system_resource("DATAFILES", path="studiolights/world")
    for ext in (".exr", ".hdr"):
        p = os.path.join(base, name + ext)
        if os.path.exists(p):
            return p
    raise ToolError(f"No built-in HDRI {name!r} in {base}. Options: {BUILTIN_HDRIS}")


def _polyhaven_hdri(asset_id, res="1k"):
    import urllib.request
    cache = os.path.expanduser("~/.claude_blender/hdri")
    os.makedirs(cache, exist_ok=True)
    path = os.path.join(cache, f"{asset_id}_{res}.hdr")
    if not os.path.exists(path):
        url = f"https://dl.polyhaven.org/file/ph-assets/HDRIs/hdr/{res}/{asset_id}_{res}.hdr"
        req = urllib.request.Request(url, headers={"User-Agent": "claude-blender"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp, open(path, "wb") as fh:
                fh.write(resp.read())
        except Exception as exc:
            if os.path.exists(path):
                os.remove(path)
            raise ToolError(f"Could not download Poly Haven HDRI {asset_id!r}: {exc}")
    return path


SKY_SUN = "Sky Sun"


def _sky_sun(elevation, azimuth, coll=None):
    obj = bpy.data.objects.get(SKY_SUN)
    if obj is None or obj.type != "LIGHT":
        data = bpy.data.lights.new(SKY_SUN, "SUN")
        obj = bpy.data.objects.new(SKY_SUN, data)
        (coll or bpy.context.scene.collection).objects.link(obj)
    el = max(-5.0, min(90.0, elevation))
    h = max(0.0, math.sin(math.radians(el)))
    obj.data.energy = 2.6 + 2.0 * h
    obj.data.angle = math.radians(1.5 if el > 15 else 3.0)
    _apply_light_color(obj.data, kelvin=2200 + 3600 * min(1.0, h * 1.6))
    d = direction_from_angles(azimuth, el)
    obj.rotation_mode = "XYZ"
    obj.rotation_euler = (-d).to_track_quat("-Z", "Y").to_euler()
    obj.location = d * 20
    return obj


def set_world_impl(mode="sky", color="#202226", color2=None, strength=None, sun_elevation=35.0,
                   sun_azimuth=45.0, hdri=None, rotation=0.0, fog_density=0.0,
                   hide_background=False, sun_lamp=True):
    if strength is None:
        strength = 0.3 if mode == "sky" else 1.0
    scene = bpy.context.scene
    world = scene.world or bpy.data.worlds.new("World")
    scene.world = world
    try:
        world.use_nodes = True
    except AttributeError:
        pass
    nt = world.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputWorld")
    out.location = (600, 0)
    bg = nt.nodes.new("ShaderNodeBackground")
    bg.location = (300, 0)
    bg.inputs["Strength"].default_value = strength
    info = {"mode": mode, "strength": strength}
    if mode == "color":
        bg.inputs["Color"].default_value = color4(color)
        nt.links.new(bg.outputs[0], out.inputs["Surface"])
    elif mode == "gradient":
        tc = nt.nodes.new("ShaderNodeTexCoord")
        tc.location = (-600, 0)
        sep = nt.nodes.new("ShaderNodeSeparateXYZ")
        sep.location = (-400, 0)
        nt.links.new(tc.outputs["Generated"], sep.inputs[0])
        ramp = nt.nodes.new("ShaderNodeValToRGB")
        ramp.location = (-150, 0)
        nt.links.new(sep.outputs["Z"], socket(ramp, "Fac", "Factor"))
        ramp.color_ramp.elements[0].position = 0.45
        ramp.color_ramp.elements[0].color = color4(color2 or "#2B2B2B")
        ramp.color_ramp.elements[1].position = 0.7
        ramp.color_ramp.elements[1].color = color4(color)
        nt.links.new(ramp.outputs["Color"], bg.inputs["Color"])
        nt.links.new(bg.outputs[0], out.inputs["Surface"])
    elif mode == "sky":
        sky = nt.nodes.new("ShaderNodeTexSky")
        sky.location = (0, 0)
        chosen = set_enum(sky, "sky_type", "MULTIPLE_SCATTERING", ("NISHITA", "HOSEK_WILKIE"))
        if hasattr(sky, "sun_elevation"):
            sky.sun_elevation = math.radians(sun_elevation)
            sky.sun_rotation = math.radians(180.0 - sun_azimuth)
        if hasattr(sky, "sun_disc"):
            sky.sun_disc = not sun_lamp
        nt.links.new(sky.outputs[0], bg.inputs["Color"])
        nt.links.new(bg.outputs[0], out.inputs["Surface"])
        info["sky_type"] = chosen
        if sun_lamp:
            sun = _sky_sun(sun_elevation, sun_azimuth)
            info["sun_lamp"] = {"name": sun.name, "strength": r(sun.data.energy, 2)}
    elif mode == "hdri":
        if not hdri:
            raise ToolError(f"mode=hdri needs `hdri`: a file path, 'builtin:<name>' "
                            f"({BUILTIN_HDRIS}) or a Poly Haven id like 'kloofendal_48d_partly_cloudy_puresky'")
        if hdri.startswith("builtin:"):
            path = _builtin_hdri(hdri.split(":", 1)[1])
        elif os.path.sep in hdri or hdri.startswith("~"):
            path = os.path.abspath(os.path.expanduser(hdri))
            if not os.path.exists(path):
                raise ToolError(f"HDRI not found: {path}")
        else:
            path = _polyhaven_hdri(hdri)
        tc = nt.nodes.new("ShaderNodeTexCoord")
        tc.location = (-700, 0)
        mp = nt.nodes.new("ShaderNodeMapping")
        mp.location = (-500, 0)
        mp.inputs["Rotation"].default_value = (0, 0, math.radians(rotation))
        env = nt.nodes.new("ShaderNodeTexEnvironment")
        env.location = (-250, 0)
        env.image = bpy.data.images.load(path, check_existing=True)
        nt.links.new(tc.outputs["Generated"], mp.inputs["Vector"])
        nt.links.new(mp.outputs["Vector"], env.inputs["Vector"])
        nt.links.new(env.outputs["Color"], bg.inputs["Color"])
        nt.links.new(bg.outputs[0], out.inputs["Surface"])
        info["hdri"] = path
    else:
        raise ToolError("mode must be color, gradient, sky or hdri")
    if hide_background:
        lp = nt.nodes.new("ShaderNodeLightPath")
        lp.location = (300, 300)
        flat = nt.nodes.new("ShaderNodeBackground")
        flat.location = (300, -200)
        flat.inputs["Color"].default_value = color4(color2 or "#1E1E1E")
        mix = nt.nodes.new("ShaderNodeMixShader")
        mix.location = (450, 0)
        nt.links.new(lp.outputs["Is Camera Ray"], mix.inputs[0])
        nt.links.new(bg.outputs[0], mix.inputs[1])
        nt.links.new(flat.outputs[0], mix.inputs[2])
        nt.links.new(mix.outputs[0], out.inputs["Surface"])
    if fog_density > 0:
        vol = nt.nodes.new("ShaderNodeVolumePrincipled")
        vol.location = (300, -400)
        socket(vol, "Density").default_value = fog_density
        nt.links.new(vol.outputs[0], out.inputs["Volume"])
        info["fog_density"] = fog_density
    return info


@tool("set_world", """
Set the environment. mode: sky (physical sky plus a matching 'Sky Sun'
lamp whose warmth and power follow sun_elevation; sun_azimuth uses the same
convention as everything else: 0 = front, 90 = right), color (flat), gradient (color on top,
color2 at the horizon), hdri (file path, 'builtin:<city|courtyard|forest|
interior|night|studio|sunrise|sunset>' which ships with Blender, or a Poly
Haven id downloaded and cached). hide_background lights with the world but
shows color2 to the camera. fog_density adds world volume scatter.
""", "Look",
      mode=P.str("Environment type", "sky", enum=["sky", "color", "gradient", "hdri"]),
      color=P.color("Color (color/gradient top)", "#202226"),
      color2=P.color("Gradient horizon color / camera background"),
      strength=P.num("Background strength (default 0.3 for sky, 1 otherwise)"),
      sun_elevation=P.num("Sky sun elevation, degrees (5-10 = golden hour)", 35.0),
      sun_azimuth=P.num("Sky sun azimuth, degrees (0 = front, 90 = right)", 45.0),
      sun_lamp=P.bool("Sky mode: add/update a matching sun lamp", True),
      hdri=P.str("HDRI source for mode=hdri"),
      rotation=P.num("HDRI rotation, degrees", 0.0),
      fog_density=P.num("Volumetric fog density (0 = none). 0.01-0.05 is subtle", 0.0),
      hide_background=P.bool("Hide the environment from the camera", False))
def set_world(mode="sky", color="#202226", color2=None, strength=None, sun_elevation=35.0,
              sun_azimuth=45.0, sun_lamp=True, hdri=None, rotation=0.0, fog_density=0.0,
              hide_background=False):
    return set_world_impl(mode, color, color2, strength, sun_elevation, sun_azimuth, hdri,
                          rotation, fog_density, hide_background, sun_lamp)


ASPECTS = {"16:9": (1920, 1080), "4:3": (1600, 1200), "1:1": (1080, 1080),
           "4:5": (1080, 1350), "9:16": (1080, 1920), "21:9": (2560, 1080),
           "3:2": (1800, 1200)}


def _fit_distance_exact(cam_rot, center, corners, tan_h, tan_v, padding):
    rinv = cam_rot.to_matrix().inverted()
    need = 0.0
    for c in corners:
        p = rinv @ (c - center)
        need = max(need,
                   abs(p.x) * padding / tan_h + p.z,
                   abs(p.y) * padding / tan_v + p.z)
    return max(need, 0.05)


@tool("camera_setup", """
Create or update a camera and compose the shot. Framing modes:
- frame: auto-fit `target` exactly in frame from azimuth/elevation (0 = front,
  90 = right side) with `padding`; exact fit from the real bounding corners
- look_at: keep `location`, aim at target
- place: explicit location + rotation
Also: lens (mm), aspect ('16:9','1:1','4:5','9:16','21:9','3:2','4:3') which
sets the render resolution, dof (focus on target, fstop), shift for
off-center/rule-of-thirds composition, ortho, set_active.
""", "Look",
      name=P.str("Camera name (created if missing)", "Camera"),
      mode=P.str("Framing mode", "frame", enum=["frame", "look_at", "place"]),
      target=P.objects("What to frame / aim at (default all visible geometry)"),
      azimuth=P.num("Orbit azimuth degrees (frame mode)", 30.0),
      elevation=P.num("Orbit elevation degrees (frame mode)", 15.0),
      padding=P.num("Framing padding (1 = tight)", 1.1),
      location=P.vec3("Camera location (look_at/place)"),
      rotation=P.vec3("Camera rotation degrees (place)"),
      lens=P.num("Focal length mm", 50.0),
      aspect=P.str("Aspect ratio preset"),
      dof=P.bool("Depth of field focused on the target"),
      fstop=P.num("Aperture f-stop for DOF", 2.8),
      shift=P.list("Lens shift [x, y] for off-center composition", {"type": "number"}),
      ortho=P.bool("Orthographic projection", False),
      set_active=P.bool("Make it the scene camera", True))
def camera_setup(name="Camera", mode="frame", target=None, azimuth=30.0, elevation=15.0,
                 padding=1.1, location=None, rotation=None, lens=50.0, aspect=None, dof=None,
                 fstop=2.8, shift=None, ortho=False, set_active=True):
    scene = bpy.context.scene
    cam = bpy.data.objects.get(name)
    if cam is None or cam.type != "CAMERA":
        data = bpy.data.cameras.new(name)
        cam = bpy.data.objects.new(name, data)
        link_to_collection(cam)
    c = cam.data
    c.lens = lens
    c.type = "ORTHO" if ortho else "PERSP"
    c.clip_end = max(c.clip_end, 1000)
    if aspect:
        if aspect not in ASPECTS:
            raise ToolError(f"aspect must be one of {sorted(ASPECTS)}")
        scene.render.resolution_x, scene.render.resolution_y = ASPECTS[aspect]
    if shift:
        c.shift_x, c.shift_y = shift[0], shift[1] if len(shift) > 1 else 0.0
    rd = scene.render
    ar = (rd.resolution_x * rd.pixel_aspect_x) / max(1, rd.resolution_y * rd.pixel_aspect_y)
    c.sensor_fit = "AUTO"

    targets = resolve_objects(target) if target is not None else [
        o for o in scene.objects if o.visible_get() and o.type in
        {"MESH", "CURVE", "FONT", "SURFACE", "META"} and not o.name.startswith("Backdrop")]
    bb = world_bbox(targets) if targets else None
    center = (bb[0] + bb[1]) / 2 if bb else Vector((0, 0, 0))

    if mode == "frame":
        if not bb:
            raise ToolError("Nothing to frame")
        corners = [Vector((x, y, z)) for x in (bb[0].x, bb[1].x) for y in (bb[0].y, bb[1].y)
                   for z in (bb[0].z, bb[1].z)]
        d = direction_from_angles(azimuth, elevation)
        rot = (-d).to_track_quat("-Z", "Y")
        half = c.sensor_width / 2 / c.lens
        tan_h, tan_v = (half, half / ar) if ar >= 1 else (half * ar, half)
        if ortho:
            rinv = rot.to_matrix().inverted()
            ext = [rinv @ (p - center) for p in corners]
            w = max(abs(p.x) for p in ext) * 2 * padding
            h = max(abs(p.y) for p in ext) * 2 * padding
            c.ortho_scale = max(w, h * ar) if ar >= 1 else max(w / ar, h)
            dist = max((bb[1] - bb[0]).length * 2, 1)
        else:
            dist = _fit_distance_exact(rot, center, corners, tan_h, tan_v, padding)
        cam.location = center + d * dist
        cam.rotation_mode = "XYZ"
        cam.rotation_euler = rot.to_euler()
    elif mode == "look_at":
        if location is not None:
            cam.location = vec3(location)
        cam.rotation_euler = look_at_quat(cam.location, center).to_euler()
    else:
        if location is not None:
            cam.location = vec3(location)
        if rotation is not None:
            cam.rotation_euler = [math.radians(a) for a in rotation]
    bpy.context.view_layer.update()

    if dof is not None:
        c.dof.use_dof = dof
        if dof:
            if len(targets) == 1:
                c.dof.focus_object = targets[0]
            else:
                c.dof.focus_object = None
                c.dof.focus_distance = (cam.matrix_world.translation - center).length
            c.dof.aperture_fstop = fstop
    if set_active:
        scene.camera = cam
    return {"camera": cam.name, "location": r(cam.location),
            "rotation_deg": r([math.degrees(a) for a in cam.rotation_euler], 2),
            "lens": c.lens, "resolution": [rd.resolution_x, rd.resolution_y],
            "distance_to_target": r((cam.location - center).length, 3),
            "tip": "Verify with look(view='camera')."}
