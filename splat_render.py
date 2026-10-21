#!/usr/bin/env python3
"""
splat_render.py -- the blender-bridge prototype: import a splat PLY /
point cloud, render a beauty pass, headless.

THE PRODUCT IDEA (why this repo exists): AGENTS ARE THE INTERFACE TO
BLENDER. An agent drops a PLY here, runs one headless command, and gets
back a rendered beauty pass + a machine-readable report -- no UI, no
human clicking. Blender becomes a callable in the fleet's toolchain.

WHAT IT DOES
  1. Reads a PLY: plain point clouds (x,y,z[,red,green,blue]) AND
     3DGS-style splat PLYs (f_dc_0..2 etc. -- the SH DC term decodes to
     RGB as 0.5 + f_dc * 0.282095, kept as vertex colors for the color
     rung below; scales/rotations are parsed and reported).
  2. Makes the points renderable: duplivert instances of a small
     icosphere on every vertex (v1's honest geometry stand-in -- see
     the ladder).
  3. Renders a beauty pass headless: Cycles CPU, area key light + rim,
     soft world, depth-of-field off, PNG out.
  4. Writes a JSON report beside the render: point count, bounds,
     color source, camera fit, timings -- the agent's receipt.

THE LADDER (stated, not hidden):
  rung 1 (this file): point-cloud instancing, one principled material.
  rung 2: per-instance COLOR (quantized material buckets from vertex
          colors) -- the decode is already in place.
  rung 3: true gaussian splats -- per-gaussian covariances (scale_* /
          rot_*) evaluated in a shader; the parsers below already carry
          the parameters through.

USAGE (headless, the whole point):
  blender -b -P splat_render.py -- \
      [--ply input.ply] [--out render.png] [--report report.json] \
      [--res 960 540] [--samples 64] [--points-max 60000]
  With no --ply, a synthetic 40k-point colored structure is generated
  so the pipeline proves itself with zero dependencies.

Requires Blender 4.x+ (wm.ply_import). Stdlib-only otherwise.
"""

import argparse
import array
import json
import math
import random
import struct
import sys
import time
from pathlib import Path

import bpy

# Blender 5's bundled python ships NO numpy -- stdlib only below
# (array + struct cover the PLY work numpy would have done).

SH_C0 = 0.28209479177387814   # 3DGS SH DC -> RGB: 0.5 + f_dc * C0


# -- PLY reading, by hand: stdlib-only, both dialects ------------------------

def read_ply(path: str) -> dict:
    """Parse ascii or binary_little_endian PLY, stdlib-only. Returns
    points (N,3), colors (N,3) or None, and every other property
    (scales, rotations, opacity, f_dc -- carried for the ladder)."""
    data = Path(path).read_bytes()
    if data[:3] != b"ply":
        raise ValueError("not a PLY")
    header_end = data.find(b"end_header\n") + len(b"end_header\n")
    header = data[:header_end].decode("ascii", "replace")
    fmt = "ascii"
    for ln in header.splitlines():
        if ln.startswith("format "):
            fmt = ln.split()[1]
            break
    props, n_vertices, in_vertex = [], 0, False
    for ln in header.splitlines():
        parts = ln.split()
        if parts[:2] == ["element", "vertex"]:
            n_vertices = int(parts[2]); in_vertex = True
        elif parts[:1] == ["element"]:
            in_vertex = False
        elif in_vertex and parts[:1] == ["property"]:
            props.append((parts[1], parts[2]))   # (type, name)
    tmap = {"float": "f", "float32": "f", "double": "d",
            "uchar": "B", "uint8": "B", "char": "b",
            "ushort": "H", "short": "h", "uint": "I", "int": "i"}
    if fmt == "ascii":
        cols = {name: [] for _, name in props}
        lines = data[header_end:].decode("ascii", "replace").splitlines()
        for ln in lines[:n_vertices]:
            vals = ln.split()
            for (t, name), v in zip(props, vals):
                cols[name].append(int(v) if t in ("uchar", "char", "ushort",
                    "short", "uint", "int") else float(v))
    else:
        row = struct.Struct("<" + "".join(tmap[t] for t, _ in props))
        rows = list(row.iter_unpack(data[header_end:
            header_end + row.size * n_vertices]))
        cols = {name: [r[i] for r in rows]
                for i, (_, name) in enumerate(props)}
    pts = [(float(cols["x"][i]), float(cols["y"][i]), float(cols["z"][i]))
           for i in range(n_vertices)]
    colors = None
    if "red" in cols and "green" in cols and "blue" in cols:
        mx = max(max(cols["red"]), max(cols["green"]), max(cols["blue"]))
        div = 255.0 if mx > 1.001 else 1.0
        colors = [(cols["red"][i] / div, cols["green"][i] / div,
                   cols["blue"][i] / div) for i in range(n_vertices)]
    elif "f_dc_0" in cols and "f_dc_1" in cols and "f_dc_2" in cols:
        colors = [(
            min(1.0, max(0.0, 0.5 + cols["f_dc_0"][i] * SH_C0)),
            min(1.0, max(0.0, 0.5 + cols["f_dc_1"][i] * SH_C0)),
            min(1.0, max(0.0, 0.5 + cols["f_dc_2"][i] * SH_C0)))
            for i in range(n_vertices)]
    extras = {name: True for _, name in props
              if name not in ("x", "y", "z", "red", "green", "blue",
                              "f_dc_0", "f_dc_1", "f_dc_2")}
    return {"points": pts, "colors": colors, "extras": extras,
            "n": n_vertices, "format": fmt}


def synthetic_ply(path: str, n: int = 40000) -> None:
    """A colored torus-knot-ish cloud, so the pipeline self-proves."""
    rng = random.Random(7)
    rows = []
    pack = struct.Struct("<fffBBB").pack
    for i in range(n):
        t = 12 * math.pi * i / n
        c3 = math.cos(3 * t)
        x = (2.2 + 0.9 * c3) * math.cos(t) + rng.gauss(0, 0.02)
        y = (2.2 + 0.9 * c3) * math.sin(t) + rng.gauss(0, 0.02)
        z = 0.9 * math.sin(3 * t) + 0.4 * math.sin(5 * t) + rng.gauss(0, 0.02)
        r = int(255 * (0.5 + 0.5 * math.cos(t)))
        g = int(255 * (0.5 + 0.5 * math.cos(t + 2.1)))
        b = int(255 * (0.5 + 0.5 * math.cos(t + 4.2)))
        rows.append(pack(x, y, z, r, g, b))
    with open(path, "wb") as f:
        f.write(b"ply\nformat binary_little_endian 1.0\n"
                b"element vertex %d\n" % n +
                b"property float x\nproperty float y\nproperty float z\n"
                b"property uchar red\nproperty uchar green\n"
                b"property uchar blue\nend_header\n")
        f.write(b"".join(rows))


# -- the blender side ---------------------------------------------------------

def build_scene(ply: dict, points_max: int) -> dict:
    """Point cloud -> mesh (vertices only) -> duplivert spheres, a
    two-light beauty rig, and a camera fitted to the bounds."""
    pts = ply["points"]
    stride = max(1, len(pts) // points_max)
    pts = pts[::stride]
    colors = ply["colors"][::stride] if ply["colors"] is not None else None

    mesh = bpy.data.meshes.new("splat_points")
    mesh.from_pydata([tuple(p) for p in pts], [], [])
    mesh.update()
    obj = bpy.data.objects.new("splat_points", mesh)
    bpy.context.collection.objects.link(obj)
    if colors is not None:
        flat = []
        for c in colors:
            flat.extend((c[0], c[1], c[2], 1.0))
        attr = mesh.attributes.new("Col", 'FLOAT_COLOR', 'POINT')
        attr.data.foreach_set("color", flat)

    # the dot is a hand-built octahedron: no bmesh/operator calls, no
    # version-dependent operator signatures (Blender 5 renamed
    # create_icosphere's args; from_pydata is stable across versions)
    dot = bpy.data.meshes.new("dot")
    r = 0.028
    dot.from_pydata(
        [(0, 0, r), (0, 0, -r), (r, 0, 0), (-r, 0, 0),
         (0, r, 0), (0, -r, 0)], [],
        [(0, 2, 4), (0, 4, 3), (0, 3, 5), (0, 5, 2),
         (1, 4, 2), (1, 3, 4), (1, 5, 3), (1, 2, 5)])
    dot.update()
    dot_obj = bpy.data.objects.new("dot", dot)
    bpy.context.collection.objects.link(dot_obj)
    mat = bpy.data.materials.new("point_mat")
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes["Principled BSDF"]
    bsdf.inputs["Roughness"].default_value = 0.55
    bsdf.inputs["Base Color"].default_value = (0.85, 0.82, 0.78, 1.0)
    dot_obj.data.materials.append(mat)
    dot_obj.parent = obj
    obj.instance_type = 'VERTS'
    # (the dot also renders once at its own origin spot -- inside the
    # cloud's volume for the synthetic, out of frame for real bounds)

    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj

    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]; zs = [p[2] for p in pts]
    lo = (min(xs), min(ys), min(zs)); hi = (max(xs), max(ys), max(zs))
    center = tuple((lo[i] + hi[i]) / 2 for i in range(3))
    span = max(hi[i] - lo[i] for i in range(3))
    cam_data = bpy.data.cameras.new("cam")
    cam = bpy.data.objects.new("cam", cam_data)
    bpy.context.collection.objects.link(cam)
    d = span * 1.7
    from mathutils import Vector
    cam.location = (center[0] + d * 0.55, center[1] - d * 0.9,
                    center[2] + d * 0.35)
    direction = Vector(center) - cam.location
    cam.rotation_euler = direction.to_track_quat('-Z', 'Y').to_euler()
    cam_data.lens = 60
    bpy.context.scene.camera = cam

    key = bpy.data.objects.new("key", bpy.data.lights.new("key", 'AREA'))
    key.data.energy = span * span * 22
    key.data.size = span * 2.2
    key.location = (center[0] + span, center[1] - span, center[2] + span)
    bpy.context.collection.objects.link(key)
    rim = bpy.data.objects.new("rim", bpy.data.lights.new("rim", 'AREA'))
    rim.data.energy = span * span * 10
    rim.data.size = span * 1.4
    rim.data.color = (0.7, 0.8, 1.0)
    rim.location = (center[0] - span, center[1] - span * 0.3,
                    center[2] + span * 0.7)
    bpy.context.collection.objects.link(rim)
    bpy.context.scene.world = bpy.data.worlds.new("world")
    bpy.context.scene.world.use_nodes = True
    bg = bpy.context.scene.world.node_tree.nodes["Background"]
    bg.inputs[0].default_value = (0.02, 0.022, 0.03, 1.0)
    bg.inputs[1].default_value = 1.0
    return {"center": list(center), "span": span,
            "stride": stride, "rendered_points": len(pts)}


def main() -> int:
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    ap = argparse.ArgumentParser()
    ap.add_argument("--ply")
    ap.add_argument("--out", default="render.png")
    ap.add_argument("--report", default="render_report.json")
    ap.add_argument("--res", nargs=2, type=int, default=[960, 540])
    ap.add_argument("--samples", type=int, default=64)
    ap.add_argument("--points-max", type=int, default=60000)
    a = ap.parse_args(argv)

    t0 = time.time()
    synth = a.ply is None
    if synth:
        a.ply = "synthetic_points.ply"
        synthetic_ply(a.ply)
    ply = read_ply(a.ply)
    t_read = time.time() - t0

    # start from a clean file so the script is idempotent headless
    bpy.ops.wm.read_factory_settings(use_empty=True)
    info = build_scene(ply, a.points_max)

    scene = bpy.context.scene
    scene.render.engine = 'CYCLES'
    scene.cycles.device = 'CPU'
    scene.cycles.samples = a.samples
    scene.render.resolution_x, scene.render.resolution_y = a.res
    # absolute: Blender resolves relative outputs against the (unsaved,
    # headless) .blend's directory -- the render saves nowhere without this
    scene.render.filepath = str(Path(a.out).resolve())
    scene.render.film_transparent = False
    t1 = time.time()
    bpy.ops.render.render(write_still=True)

    report = {
        "ply": a.ply, "synthetic": synth,
        "ply_format": ply["format"], "points_in_ply": ply["n"],
        "color_source": "rgb" if ply["colors"] is not None and not synth
            and "f_dc_0" not in ply["extras"] else
            ("f_dc (SH DC decoded)" if ply["colors"] is not None
             else "none"),
        "extra_properties": sorted(ply["extras"].keys()),
        "rendered_points": info["rendered_points"], "stride": info["stride"],
        "bounds_span": round(info["span"], 3),
        "engine": "CYCLES CPU", "samples": a.samples,
        "res": a.res, "render_png": a.out,
        "t_read_s": round(t_read, 2), "t_render_s": round(time.time() - t1, 2),
        "ladder": "rung 1: instanced points, single material; colors "
                  "decoded and stored as the 'Col' attribute for rung 2; "
                  "scales/rotations parsed (see extra_properties) for "
                  "rung 3 (true gaussians)",
    }
    Path(a.report).write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    return 0


main()
