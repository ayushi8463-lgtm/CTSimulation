"""Python simulation engine callable by the native C++ web server."""
import io
import json
import os
import re
import tempfile
from pathlib import Path
from dataclasses import asdict

import numpy as np
from PIL import Image

import ct_core as core
import ct_variations as var

SUPPORTED_MESH_EXTENSIONS = {".stl", ".obj", ".ply", ".dae"}


def to_uint8(a):
    lo, hi = np.percentile(a, [0.5, 99.5])
    if hi <= lo:
        hi = lo + 1e-9
    return (np.clip((a - lo) / (hi - lo), 0, 1) * 255).astype(np.uint8)


def to_png(a_uint8):
    buf = io.BytesIO()
    Image.fromarray(a_uint8).save(buf, format="PNG")
    return buf.getvalue()


def _num(form, key, default, cast=float):
    value = (form.get(key) or "").strip()
    if value == "":
        return default
    try:
        return cast(value)
    except ValueError:
        raise ValueError(f"Invalid value for '{key}': {value!r}")


def _int(value):
    return int(float(value))


def make_scan_config(form, uploads):
    if form.get("demo") == "on":
        objects = core.demo_objects()
    else:
        objects = []
        file_indices = sorted(int(m.group(1)) for key in uploads
                              if (m := re.fullmatch(r"file(\d+)", key)))
        for i in file_indices:
            upload = uploads.get(f"file{i}") or {}
            filename = os.path.basename(upload.get("filename", "")).replace(" ", "_")
            path = upload.get("path", "")
            if not filename or not path:
                continue
            extension = Path(filename).suffix.lower()
            if extension not in SUPPORTED_MESH_EXTENSIONS:
                supported = ", ".join(sorted(SUPPORTED_MESH_EXTENSIONS))
                raise ValueError(f"Unsupported mesh format '{extension or '(no extension)'}'. Use: {supported}.")
            objects.append(core.ObjectCfg(
                name=(form.get(f"name{i}") or os.path.splitext(filename)[0]).strip(),
                stl_path=path,
                kind=form.get(f"kind{i}", "Compound"),
                material=(form.get(f"material{i}") or "").strip(),
                density=_num(form, f"density{i}", 0.0),
                offset_mm=(_num(form, f"ox{i}", 0.0), _num(form, f"oy{i}", 0.0),
                           _num(form, f"oz{i}", 0.0)),
                rotate_deg=(_num(form, f"rx{i}", 0.0), _num(form, f"ry{i}", 0.0),
                            _num(form, f"rz{i}", 0.0)),
            ))
    if not objects:
        raise ValueError("No objects: tick the demo box or upload at least one supported mesh file (STL, OBJ, PLY, or DAE).")

    cfg = core.ScanCfg(
        objects=objects,
        stl_unit=form.get("unit", "mm"),
        center_each=form.get("center") == "on",
        beam=form.get("beam", "Cone"),
        source_x_cm=_num(form, "src", 40.0),
        detector_x_cm=_num(form, "det", 10.0),
        energy_kev=_num(form, "energy", 80.0),
        photons=_num(form, "photons", 1000, _int),
        nx=_num(form, "nx", 320, _int),
        ny=_num(form, "ny", 160, _int),
        pixel_mm=_num(form, "px", 1.0),
        n_angles=max(_num(form, "n_angles", 90, _int), 1),
        total_deg=_num(form, "total", 360.0),
        noise=form.get("noise") == "on",
        seed=_num(form, "seed", 0, _int),
    )
    if form.get("spectrum") == "poly":
        cfg.spectrum = var.kramers_spectrum(_num(form, "kvp", 120.0))
    if cfg.nx < 1 or cfg.ny < 1 or cfg.pixel_mm <= 0 or cfg.photons < 1:
        raise ValueError("Detector size, pixel size and photon count must be positive.")

    fx = var.EffectsCfg(
        poisson=cfg.noise, photons=0,
        psf_sigma_px=_num(form, "blur", 0.0),
        scatter_frac=_num(form, "scatter", 0.0) / 100.0,
        gain_sigma=_num(form, "gain", 0.0) / 100.0,
        flat_mismatch_sigma=_num(form, "mismatch", 0.0) / 100.0,
        read_noise=_num(form, "readnoise", 0.0),
        flat_frames=max(_num(form, "flat_frames", 1, _int), 1),
        dark_offset=_num(form, "dark", 0.0),
        seed=cfg.seed,
    )
    if min(fx.psf_sigma_px, fx.scatter_frac, fx.gain_sigma, fx.flat_mismatch_sigma,
           fx.read_noise, fx.dark_offset) < 0:
        raise ValueError("Imaging effect values cannot be negative.")
    return cfg, _num(form, "angle", 0.0), fx


def _parse_batch(form, cfg, fx):
    try:
        spec = json.loads(form.get("sweep") or "{}")
    except ValueError:
        raise ValueError("Could not read the variation settings.")
    mode = "grid" if spec.get("mode") == "grid" else "ofat"
    params = var.parse_sweep(spec.get("params", []), cfg, fx)
    return dict(params=params, mode=mode,
                baseline=bool(spec.get("baseline", True)),
                clean=bool(spec.get("clean_copy", True)))


def prepare(mode, form, uploads):
    if mode not in ("single", "scan", "batch"):
        mode = "scan"
    cfg, angle, fx = make_scan_config(form, uploads)
    batch = _parse_batch(form, cfg, fx) if mode == "batch" else None
    mesh_warnings = core.configuration_warnings(cfg)
    return {"mode": mode, "cfg": cfg, "angle": angle, "fx": fx,
            "batch": batch, "mesh_warnings": mesh_warnings}


def execute(prepared, report_progress):
    mode, cfg, angle, fx = (prepared[k] for k in ("mode", "cfg", "angle", "fx"))
    def add_warnings(message):
        notes = prepared.get("mesh_warnings", [])
        if not notes:
            return message
        return message + "\n\nWarnings:\n" + "\n".join(f"⚠ {note}" for note in notes)

    if mode == "batch":
        batch = prepared["batch"]
        out_root = tempfile.mkdtemp(prefix="ctbatch_")
        thumbs, reference = {}, {}
        def progress(value, message):
            report_progress(min(max(value, 0.0), 0.99), add_warnings(message))
        def on_dataset(row, atten):
            if "hi" not in reference:
                reference["hi"] = max(float(np.percentile(atten[0], 99.5)), 1e-6)
            img = (np.clip(atten[0] / reference["hi"], 0, 1) * 255).astype(np.uint8)
            thumbs[int(row["id"])] = to_png(img)
        result = var.generate(cfg, fx, batch["params"], out_root, batch["mode"],
                              batch["baseline"], batch["clean"],
                              progress=progress, on_dataset=on_dataset)
        rows = [{k: row[k] for k in ("id", "folder", "variation", "effects")}
                for row in result["rows"]]
        message = add_warnings(
            f"Done: {len(rows)} datasets generated. Download them as one zip (includes manifest.csv).")
        report_progress(1.0, message)
        return {"kind": "batch", "n": 0, "batch_dir": out_root,
                "message": message,
                "batch_rows": rows, "thumbs": thumbs, "batch_zip": result["zip"],
                "manifest": result["manifest"]}

    if mode == "single":
        report_progress(0.1, add_warnings("Building scene..."))
        raw, flat = core.on_gl_thread(core.single_projection, cfg, angle)
        att, _, _ = var.apply_effects(raw, flat, cfg.photons, fx)
        message = add_warnings(f"Single projection at {angle:.1f} deg done.")
        report_progress(1.0, message)
        return {"kind": mode, "n": 0, "message": message,
                "images": {"proj": to_png(to_uint8(att))}}

    n = int(cfg.n_angles)
    step = cfg.total_deg / n
    report_progress(0.02, add_warnings("Building scene..."))
    flat = core.on_gl_thread(core.build_scene, cfg)
    block = max(1, n // 20)
    chunks, done = [], 0
    while done < n:
        count = min(block, n - done)
        chunks.append(core.on_gl_thread(core.acquire, count, step))
        done += count
        report_progress(0.05 + 0.85 * done / n, f"Projection {done}/{n}")
    raw = np.concatenate(chunks)
    att, counts, flat_counts = var.apply_effects(raw, flat, cfg.photons, fx)
    sino = att[:, att.shape[1] // 2, :]
    report_progress(0.92, "Reconstructing preview...")
    rec = core.quick_recon(att, cfg)
    zip_path = core.save_dataset(cfg, att, counts, flat_counts,
                                 dark_level=fx.dark_offset, effects=asdict(fx))
    sino_zip = core.save_sinogram_dataset(cfg, att)
    message = add_warnings(
        f"Done: {n} projections of {cfg.nx}x{cfg.ny}, {cfg.total_deg:.0f} deg scan.")
    report_progress(1.0, message)
    return {"kind": mode, "n": n, "total_deg": cfg.total_deg, "message": message,
            "att": att, "zip": zip_path, "sino_zip": sino_zip,
            "images": {"proj": to_png(to_uint8(att[0])),
                       "sino": to_png(to_uint8(sino)),
                       "rec": to_png(to_uint8(rec))}}


def image_bytes(payload, kind):
    """Return a pre-rendered result image for the C++ HTTP image route."""
    images = payload.get("images", {}) if isinstance(payload, dict) else {}
    return images.get(str(kind), b"")


def projection_bytes(payload, index):
    """Render one CT projection for the browser's projection slider."""
    if not isinstance(payload, dict):
        return b""
    projections = payload.get("att")
    index = int(index)
    if projections is None or index < 0 or index >= len(projections):
        return b""
    return to_png(to_uint8(projections[index]))


def thumb_bytes(payload, row_id):
    """Return a generated dataset thumbnail for the batch-results grid."""
    thumbs = payload.get("thumbs", {}) if isinstance(payload, dict) else {}
    return thumbs.get(int(row_id), b"")


def batch_rows_json(payload):
    """Serialize batch rows for the C++ HTTP route."""
    rows = payload.get("batch_rows", []) if isinstance(payload, dict) else []
    return json.dumps(rows, default=str)

