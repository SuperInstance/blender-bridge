# blender-bridge

**Agents are the interface to Blender.** An agent drops a geometry file
in, runs one headless command, and gets a rendered beauty pass plus a
machine-readable report back. No UI, no clicking — Blender becomes a
callable in the fleet's toolchain, the same way the room liaison made
the capture bench a callable.

## Current rung: `splat_render.py`

Imports a **splat PLY / point cloud** and renders a beauty pass,
headless:

```
blender -b -P splat_render.py -- \
    [--ply input.ply] [--out render.png] [--report report.json] \
    [--res 960 540] [--samples 64] [--points-max 60000]
```

With no `--ply`, a synthetic 40k-point colored torus knot is generated
so the pipeline proves itself with zero dependencies (that is the
committed `render.png`: Cycles CPU, 48 samples, 1.65 s).

### What it handles

- **Plain point PLYs** (`x,y,z[,red,green,blue]`, ascii or
  binary_little_endian).
- **3DGS-style splat PLYs**: the SH DC term (`f_dc_0..2`) decodes to RGB
  (`0.5 + f_dc * 0.282095`) and is stored as the mesh's `Col` vertex
  color attribute; every other property (scales, rotations, opacity) is
  parsed and reported in the JSON receipt — carried for the ladder
  below.
- **Points → renderable**: duplivert instances of a small octahedron on
  every vertex (the octahedron is built with `from_pydata` — no
  operator signatures, stable across Blender versions).
- **Beauty rig**: warm area key, cool area rim, dark world, camera
  auto-fitted to the point cloud's bounds.
- **The receipt** (`render_report.json`): point count, bounds span,
  color source, extra PLY properties found, camera fit, render time —
  everything an agent needs to know its render happened and what of.

Stdlib-only inside Blender's bundled Python (Blender 5 ships without
numpy).

### The ladder (stated, not hidden)

1. **Done** — instanced points, single principled material, beauty rig,
   receipts (`render.png`).
2. **Done (2026-10-21)** — per-instance COLOR via material buckets:
   points quantized 4×4×4 by decoded RGB, one instancer-dot per bucket
   with its own material. Proven on a synthetic **3DGS-dialect PLY**
   (30k gaussians with `f_dc_*`/`opacity`/`scale_*`/`rot_*` — the SH DC
   decode carries the color; the receipt says which source it used):
   `render_splat.png`, 18 buckets, pixel-verified color, 1.96 s.
3. **Then** — true gaussian splats: per-gaussian covariances from
   `scale_*` / `rot_*` evaluated in a shader (the parameters are
   already parsed and reported in the receipt).

## Environment

- Blender 5.0.1 (`/usr/bin/blender`), headless (`-b`), Cycles CPU.
- No sudo needed at runtime; nothing here requires the GUI — the whole
  point.

## Provenance of rung 1

Built and proven 2026-10-21 for the splat-room programme (frame assets
shipped separately to the Jetson); the synthetic render committed
beside this README is the pipeline's own proof, rendered by exactly the
command above.
