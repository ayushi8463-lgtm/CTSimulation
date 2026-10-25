"""
ct_variations.py - realistic detector effects + controlled-variation datasets.

No web code in here. simulator_engine.py calls:
    apply_effects()   turn ideal gVXR images into realistic ones
    parse_sweep()     validate the "vary these parameters" request from the page
    plan_runs()       list every dataset that will be made
    generate()        run gVXR once per physical setup, post-process, save, zip
"""
from __future__ import annotations

import copy
import csv
import itertools
import json
import math
import os
import zipfile
from dataclasses import asdict, dataclass, replace

import numpy as np
from scipy.ndimage import gaussian_filter

import ct_core as core


# Detector effects
@dataclass
class EffectsCfg:
    poisson: bool = False           # photon (shot) noise
    photons: int = 0                # photons per pixel for the noise level (0 = use cfg.photons)
    psf_sigma_px: float = 0.0       # detector blur: Gaussian sigma in pixels
    scatter_frac: float = 0.0       # smooth scatter haze, fraction of the open beam (0.02 = 2 %)
    gain_sigma: float = 0.0         # fixed pixel-to-pixel gain variation (0.02 = 2 %)
    flat_mismatch_sigma: float = 0.0  # gain change between flat and scan -> ring artefacts
    read_noise: float = 0.0         # electronic noise, standard deviation in counts
    flat_frames: int = 1            # number of independent flat frames averaged
    dark_offset: float = 0.0        # detector dark level in counts (removed by dark correction)
    seed: int = 0


def is_ideal(fx: EffectsCfg) -> bool:
    return (not fx.poisson and fx.psf_sigma_px == 0 and fx.scatter_frac == 0
            and fx.gain_sigma == 0 and fx.flat_mismatch_sigma == 0
            and fx.read_noise == 0 and fx.dark_offset == 0)


def apply_effects(raw, flat, photons, fx: EffectsCfg):
    """Turn ideal gVXR images into realistic ones.

    raw: (n, ny, nx) or (ny, nx);  flat: (ny, nx);  photons: cfg.photons.
    Returns (atten, proj_counts, flat_counts) with
        atten = -ln((P - dark) / (F - dark)).
    """
    rng = np.random.default_rng(int(fx.seed))
    T = np.clip(raw / np.maximum(flat, 1e-12), 1e-6, 1.0)     # ideal transmission
    I0 = float(max(fx.photons or photons, 1))
    ny, nx = flat.shape
    lead = (0,) * (T.ndim - 2)

    P = I0 * T                                                # counts behind the object
    F = np.full((ny, nx), I0)                                 # counts of the open beam

    if fx.scatter_frac > 0:                                   # smooth, object-dependent haze
        P = P + fx.scatter_frac * I0 * gaussian_filter(T, lead + (30, 30))
    if fx.psf_sigma_px > 0:                                   # detector blur acts on the signal
        P = gaussian_filter(P, lead + (fx.psf_sigma_px,) * 2)

    gain = 1.0 + fx.gain_sigma * rng.standard_normal((ny, nx))
    gain_scan = gain * (1.0 + fx.flat_mismatch_sigma * rng.standard_normal((ny, nx)))
    P, F = P * gain_scan, F * gain

    n = max(int(fx.flat_frames), 1)
    if fx.poisson:
        P = rng.poisson(np.maximum(P, 0)).astype(np.float64)
        F = rng.poisson(np.maximum(F * n, 0)) / n
    if fx.read_noise > 0:
        P = P + rng.normal(0, fx.read_noise, P.shape)
        F = F + rng.normal(0, fx.read_noise / np.sqrt(n), F.shape)
    P, F = P + fx.dark_offset, F + fx.dark_offset

    floor = 0.5 if (fx.poisson or fx.read_noise > 0) else 1e-6 * I0
    num = np.maximum(P - fx.dark_offset, floor)               # dark-corrected
    den = np.maximum(F - fx.dark_offset, floor)
    atten = (-np.log(np.clip(num / den, 1e-6, None))).astype(np.float32)
    return atten, P.astype(np.float32), F.astype(np.float32)


# Polychromatic beam
def kramers_spectrum(kvp: float, step: float = 10.0, e_min: float = 20.0) -> dict:
    """Approximate filtered tube spectrum {keV: weight} (Kramers law, crude filtration).

    Good enough to show beam hardening. For realistic spectra, generate one with
    SpekPy or xpecgen and put it in ScanCfg.spectrum.
    """
    kvp = float(kvp)
    if not 30 <= kvp <= 300:
        raise ValueError("Tube voltage must be between 30 and 300 kVp.")
    e = np.arange(e_min, kvp, step)
    w = (kvp - e) / e * (1.0 - np.exp(-(e / 30.0) ** 3))
    return {float(a): float(b) for a, b in zip(e, w)}


# Sweep parameters: key -> (type, scale, check, hint)
_PHYSICS = {
    "energy_kev":    (float, 1.0, lambda x: x > 0,   "must be > 0"),
    "kvp":           (float, 1.0, lambda x: 30 <= x <= 300, "must be 30 to 300"),
    "source_x_cm":   (float, 1.0, lambda x: x > 0,   "must be > 0"),
    "detector_x_cm": (float, 1.0, lambda x: x >= 0,  "must be >= 0"),
    "n_angles":      (int,   1.0, lambda x: x >= 1,  "must be >= 1"),
    "pixel_mm":      (float, 1.0, lambda x: x > 0,   "must be > 0"),
}
_OBJ_ATTRS = {
    "density":  (float, lambda x: x >= 0),
    "material": (str,   lambda x: len(x) > 0),
    "offset_x": (float, lambda x: True), "offset_y": (float, lambda x: True),
    "offset_z": (float, lambda x: True),
    "rotate_x": (float, lambda x: True), "rotate_y": (float, lambda x: True),
    "rotate_z": (float, lambda x: True),
}
_FX = {
    "photons":             (int,   1.0,  lambda x: x >= 1, "must be >= 1"),
    "flat_frames":         (int,   1.0,  lambda x: x >= 1, "must be >= 1"),
    "psf_sigma_px":        (float, 1.0,  lambda x: x >= 0, "must be >= 0"),
    "scatter_frac":        (float, 0.01, lambda x: x >= 0, "must be >= 0"),
    "gain_sigma":          (float, 0.01, lambda x: x >= 0, "must be >= 0"),
    "flat_mismatch_sigma": (float, 0.01, lambda x: x >= 0, "must be >= 0"),
    "read_noise":          (float, 1.0,  lambda x: x >= 0, "must be >= 0"),
    "dark_offset":         (float, 1.0,  lambda x: x >= 0, "must be >= 0"),
}
MAX_VALUES_PER_PARAM = 10
MAX_PARAMS = None                # parameter rows are unlimited; UI warns about large sweeps


def is_fx_key(key: str) -> bool:
    return key.startswith("fx.")


def set_param(cfg, key, value):
    """Return a copy of cfg with one physical parameter changed.

    key: a ScanCfg field ("energy_kev", ...), "kvp" (sets a polychromatic spectrum),
    or "obj0.material_spec", "obj1.offset_y", "obj0.rotate_z", ...
    """
    cfg = copy.deepcopy(cfg)
    if key == "kvp":
        cfg.spectrum = kramers_spectrum(value)
    elif key.startswith("obj"):
        idx, attr = key[3:].split(".")
        o = cfg.objects[int(idx)]
        if attr == "material_spec":
            o.kind, o.material, o.density = value
        elif attr.startswith(("offset_", "rotate_")):
            field = "offset_mm" if attr.startswith("offset_") else "rotate_deg"
            t = list(getattr(o, field))
            t["xyz".index(attr[-1])] = float(value)
            setattr(o, field, tuple(t))
        else:
            setattr(o, attr, value)
    else:
        setattr(cfg, key, value)
    return cfg


def parse_sweep(params, cfg, fx):
    """Validate the page's parameter list.

    params: [{"key": "energy_kev", "values": ["40", "80"]}, ...]
    Returns [(key, [(label, value), ...]), ...]; raises ValueError with a clear message.
    """
    if not params:
        raise ValueError("Choose at least one parameter to vary.")
    out, seen = [], set()
    for p in params:
        key = str(p.get("key", "")).strip()
        labels = [str(v).strip() for v in p.get("values", []) if str(v).strip() != ""]
        if key in seen:
            raise ValueError(f"'{key}' is listed twice.")
        seen.add(key)
        if not labels:
            raise ValueError(f"No values given for '{key}'.")
        if len(labels) > MAX_VALUES_PER_PARAM:
            raise ValueError(f"At most {MAX_VALUES_PER_PARAM} values per parameter.")

        if is_fx_key(key):
            name = key[3:]
            if name not in _FX:
                raise ValueError(f"Unknown parameter '{key}'.")
            typ, scale, check, hint = _FX[name]
            if name == "photons" and not fx.poisson:
                raise ValueError("Photons per pixel only changes the result when "
                                 "'Add photon noise' is ticked.")
        elif key in _PHYSICS:
            typ, scale, check, hint = _PHYSICS[key]
            if key == "energy_kev" and cfg.spectrum:
                raise ValueError("Energy has no effect with a polychromatic beam. "
                                 "Vary the tube voltage (kVp) instead.")
        elif key.startswith("obj") and "." in key:
            idx_s, attr = key[3:].split(".", 1)
            if not idx_s.isdigit() or (attr not in _OBJ_ATTRS and attr != "material_spec"):
                raise ValueError(f"Unknown parameter '{key}'.")
            if int(idx_s) >= len(cfg.objects):
                raise ValueError(f"Object {int(idx_s) + 1} is not loaded, "
                                 f"so '{key}' cannot be varied.")
            if attr == "material_spec":
                typ, check = str, lambda x: True
                scale, hint = 1.0, "must be formatted as type|material|density"
            else:
                typ, check = _OBJ_ATTRS[attr]
                scale, hint = 1.0, "is not allowed"
        else:
            raise ValueError(f"Unknown parameter '{key}'.")

        vals = []
        for lab in labels:
            try:
                if key.startswith("obj") and key.endswith(".material_spec"):
                    parts = [part.strip() for part in lab.split("|")]
                    if len(parts) != 3:
                        raise ValueError
                    kind, material, density_text = parts
                    density = float(density_text)
                    if kind not in {"Element", "Compound", "Mixture"}:
                        raise ValueError
                    if not material or not np.isfinite(density) or density <= 0:
                        raise ValueError
                    v = (kind, material, density)
                elif typ is str:
                    v = lab
                else:
                    x = float(lab)
                    v = int(x) if typ is int else x * scale
            except ValueError:
                raise ValueError(f"'{lab}' is not a valid value for '{key}'.")
            if not check(v):
                raise ValueError(f"Value {lab} for '{key}' {hint}.")
            vals.append((lab, v))
        out.append((key, vals))
    return out


# Dataset planning
def _signature(cfg) -> str:
    return json.dumps(asdict(cfg), sort_keys=True, default=str)


def plan_runs(base_cfg, base_fx, params, mode="ofat", baseline=True, clean_copy=True):
    """List of runs. Each run: name, changed, cfg, variants[(variant_name, EffectsCfg)]."""
    combos = []                                   # list of (name, [(key, label, value), ...])
    if baseline:
        combos.append(("baseline", []))
    if mode == "random":
        spec=params
        sampled=sample_random(int(spec.get("n",0)),spec.get("ranges",{}),spec.get("seed",0))
        for i, sample in enumerate(sampled):
            changes=[]
            for key,val in sample.items(): changes.append((key,str(val),val))
            combos.append((f"random_{i:04d}",changes))
    elif mode == "grid":
        keys = sorted(params, key=lambda kv: is_fx_key(kv[0]))     # physics keys first
        for combo in itertools.product(*[[(k, l, v) for l, v in vals] for k, vals in keys]):
            combos.append(("_".join(f"{k}={l}" for k, l, _ in combo), list(combo)))
    else:                                         # one factor at a time
        for key, vals in params:
            for lab, v in vals:
                combos.append((f"{key}={lab}", [(key, lab, v)]))

    runs = []
    for name, changes in combos:
        cfg, fx = base_cfg, base_fx
        for key, _, val in changes:
            if is_fx_key(key):
                fx = replace(fx, **{key[3:]: val})
            else:
                cfg = set_param(cfg, key, val)
        ideal = replace(EffectsCfg(), seed=fx.seed)
        if is_ideal(fx):
            variants = [("clean", ideal)]
        else:
            variants = ([("clean", ideal)] if clean_copy else []) + [("realistic", fx)]
        runs.append(dict(name=name, changed={k: l for k, l, _ in changes},
                         cfg=cfg, variants=variants))
    return runs


def sample_random(n, ranges, seed=0):
    """Sample n parameter dictionaries uniformly from {key: (low, high)} ranges."""
    rng=np.random.default_rng(int(seed)); out=[]
    for _ in range(int(n)):
        sample={}
        for key, bounds in ranges.items():
            if len(bounds)!=2: raise ValueError(f"Range for {key} must be [min, max].")
            lo,hi=map(float,bounds)
            if not np.isfinite([lo,hi]).all() or hi < lo: raise ValueError(f"Invalid range for {key}.")
            sample[key]=float(rng.uniform(lo,hi)) if hi>lo else lo
        out.append(sample)
    return out


def count_runs(params, mode="ofat", baseline=True) -> int:
    """Number of runs a sweep would create, computed without building them
    (so a huge grid is rejected before any memory is used)."""
    lens = [len(vals) for _, vals in params]
    n = math.prod(lens) if mode == "grid" else sum(lens)
    return n + (1 if baseline else 0)


def count_datasets(plan) -> int:
    return sum(len(r["variants"]) for r in plan)


# Generate datasets
def _safe(name: str) -> str:
    s = "".join(ch if ch.isalnum() or ch in "=._-" else "_" for ch in name)
    return s[:60]


def _acquire(cfg, cb):
    """Slow gVXR step: ideal raw projections + flat field."""
    flat = core.on_gl_thread(core.build_scene, cfg)
    n = int(cfg.n_angles)
    step = cfg.total_deg / n
    block, done, chunks = max(1, n // 10), 0, []
    while done < n:
        m = min(block, n - done)
        chunks.append(core.on_gl_thread(core.acquire, m, step))
        done += m
        cb(done / n)
    return np.concatenate(chunks), flat


def generate(base_cfg, base_fx, params, out_root, mode="ofat", baseline=True,
             clean_copy=True, progress=None, on_dataset=None):
    """Create every dataset of the plan under out_root and bundle them in one zip.

    gVXR runs once per distinct physical setup; the effect variants (clean, realistic,
    different noise levels, ...) are fast post-processing of the same ideal projections.
    Returns dict(rows, manifest, zip).
    """
    import tifffile                                           # noqa: F401 (checked early)
    import h5py

    progress = progress or (lambda p, msg: None)
    plan = plan_runs(base_cfg, base_fx, params, mode, baseline, clean_copy)
    groups = {}
    for run in plan:
        groups.setdefault(_signature(run["cfg"]), []).append(run)
    G = len(groups)
    os.makedirs(out_root, exist_ok=True)

    next_id = 0                                   # ids follow the plan order
    for run in plan:
        run["ids"] = list(range(next_id, next_id + len(run["variants"])))
        next_id += len(run["variants"])

    rows = []
    schema_meta = {"schema_version":"1.0","format":"one HDF5 file per sample",
                   "projection_layout":"(angle, y, x)","projection_units":"dimensionless attenuation -ln(I/I0)",
                   "raw_units":"detector counts","flat_frames_default":1}
    splits = {"train":[],"validation":[],"test":[]}
    for g, runs in enumerate(groups.values()):
        cfg = runs[0]["cfg"]
        label = ", ".join(runs[0]["changed"].keys()) or "base scan"

        def cb(frac, g=g, label=label):
            progress((g + 0.85 * frac) / G, f"Simulating scan {g + 1}/{G} ({label}) ...")

        raw, flat = _acquire(cfg, cb)

        for run in runs:
            for (variant, fx), ds_id in zip(run["variants"], run["ids"]):
                progress((g + 0.85) / G, f"Saving dataset {ds_id + 1} ({run['name']}, {variant}) ...")
                sample_seed = (int(base_fx.seed) + 1_000_003 * (ds_id + 1)) % (2**32)
                fx = replace(fx, seed=sample_seed)
                atten, P, F = apply_effects(raw, flat, cfg.photons, fx)
                folder = f"ds{ds_id:03d}_{_safe(run['name'])}_{variant}"
                sample_path=os.path.join(out_root, f"sample_{ds_id:05d}.h5")
                labels, mu, gt_meta = core.ground_truth_volumes(cfg)
                with h5py.File(sample_path,"w") as h:
                    h.attrs["sample_id"]=ds_id
                    h.attrs["scan_config_json"]=json.dumps(asdict(cfg),default=str)
                    h.attrs["effects_json"]=json.dumps(asdict(fx),default=str)
                    h.attrs["ground_truth_json"]=json.dumps(gt_meta)
                    h.create_dataset("projection/attenuation",data=atten.astype(np.float32),chunks=(1,min(cfg.ny,64),min(cfg.nx,128)),compression="gzip",compression_opts=4,shuffle=True)
                    h.create_dataset("projection/raw_counts",data=P.astype(np.float32),chunks=(1,min(cfg.ny,64),min(cfg.nx,128)),compression="gzip",compression_opts=4,shuffle=True)
                    h.create_dataset("detector/flat_counts",data=F.astype(np.float32),compression="gzip",compression_opts=4,shuffle=True)
                    h.create_dataset("ground_truth/labels",data=labels,chunks=True,compression="gzip",compression_opts=4,shuffle=True)
                    h.create_dataset("ground_truth/attenuation_cm_inv",data=mu,chunks=True,compression="gzip",compression_opts=4,shuffle=True)
                    h.create_dataset("angles_deg",data=core.angles(cfg).astype(np.float32))
                row = {
                    "id": ds_id, "file": os.path.basename(sample_path), "folder": folder, "variation": run["name"],
                    "effects": variant,
                    "changed": json.dumps(run["changed"]),
                    "beam": cfg.beam,
                    "spectrum": "polychromatic" if cfg.spectrum else f"mono {cfg.energy_kev} keV",
                    "photons_noise": fx.photons or cfg.photons, "noise_seed": int(fx.seed),
                    "flat_frames": int(fx.flat_frames),
                    "SOD_cm": cfg.source_x_cm, "det_cm": cfg.detector_x_cm,
                    "n_angles": cfg.n_angles, "nx": cfg.nx, "ny": cfg.ny, "pixel_mm": cfg.pixel_mm,
                    "objects": ";".join(f"{o.name}:{o.material}@{o.density}" for o in cfg.objects),
                    **{f"fx_{k}": v for k, v in asdict(fx).items()},
                }
                rows.append(row)
                # Save progress after each sample so interrupted runs can resume.
                manifest_path=os.path.join(out_root,"manifest.csv")
                with open(manifest_path,"w",newline="") as mf:
                    fields=list(rows[0].keys())
                    writer=csv.DictWriter(mf,fieldnames=fields)
                    writer.writeheader(); writer.writerows(rows)
                split = ("test" if ds_id % 10 == 0 else "validation" if ds_id % 10 == 1 else "train")
                splits[split].append(os.path.basename(sample_path))
                if on_dataset:
                    on_dataset(row, atten)
        del raw, flat
    rows.sort(key=lambda r: r["id"])

    progress(0.97, "Writing manifest and zip ...")
    manifest = os.path.join(out_root, "manifest.csv")
    with open(manifest, "w", newline="") as f:
        fields=list(rows[0].keys()) if rows else ["id","file","variation","effects","noise_seed"]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    splits = {"train":[],"validation":[],"test":[]}
    for row in rows:
        sid=int(row["id"])
        split=("test" if sid % 10 == 0 else "validation" if sid % 10 == 1 else "train")
        splits[split].append(row.get("file", f"sample_{sid:05d}.h5"))
    with open(os.path.join(out_root,"metadata.json"),"w") as f: json.dump(schema_meta,f,indent=2)
    with open(os.path.join(out_root,"splits.json"),"w") as f: json.dump(splits,f,indent=2)
    with open(os.path.join(out_root,"README.md"),"w") as f:
        f.write("# Synthetic CT dataset\n\nOne `sample_*.h5` file per sample. Each file contains `projection/attenuation`, `projection/raw_counts`, `detector/flat_counts`, `ground_truth/labels`, `ground_truth/attenuation_cm_inv`, and `angles_deg`. Ground truth uses isotropic object-space voxels; label 0 is background. `manifest.csv` indexes samples, `metadata.json` describes the schema, and `splits.json` gives deterministic train/validation/test membership.\n")
    zip_path = os.path.join(out_root, "datasets.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=3) as z:
        for root, _, files in os.walk(out_root):
            for fn in files:
                if fn == "datasets.zip":
                    continue
                p = os.path.join(root, fn)
                z.write(p, arcname=os.path.relpath(p, out_root))
    return dict(rows=rows, manifest=manifest, zip=zip_path)
