"""
ct_core.py - gVXR simulation engine (no GUI code in here).

Everything that touches gVXR runs on ONE dedicated worker thread, because an
OpenGL/EGL context belongs to the thread that created it and web GUIs call
handlers from arbitrary threads. Use on_gl_thread(fn, ...) to call any
function in this module that needs gVXR (build_scene, acquire,
single_projection, demo_objects).
"""
from __future__ import annotations

import json
import os
import sys
import shutil
import tempfile
import zipfile
import ctypes
import ctypes.util
import glob
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field

import numpy as np

_gl_thread = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gvxr")
_context_ready = False
_labels: list[str] = []
_renderer_selection: dict = {}
_renderer_details: dict = {}


def on_gl_thread(fn, *args, **kwargs):
    """Run fn on the gVXR thread and return its result (or raise its error)."""
    return _gl_thread.submit(fn, *args, **kwargs).result()


# Configuration
@dataclass
class ObjectCfg:
    name: str
    stl_path: str
    kind: str = "Compound"          # Element | Compound | Mixture
    material: str = "H2O"           # "Fe" / "26", "H2O", "Ti90Al6V4", ...
    density: float = 1.0            # g/cm3 (0 = keep gVXR default, elements only)
    offset_mm: tuple = (0.0, 0.0, 0.0)
    # Rotation about fixed world axes, applied x, then y, then z.
    # x spins the image, y tips the sample, and z is the CT rotation axis.
    rotate_deg: tuple = (0.0, 0.0, 0.0)


@dataclass
class ScanCfg:
    objects: list = field(default_factory=list)
    stl_unit: str = "mm"
    center_each: bool = True        # move every mesh's centre to the origin
    beam: str = "Cone"              # Cone | Parallel
    source_x_cm: float = 40.0       # source is at x = -source_x_cm
    detector_x_cm: float = 10.0     # detector is at x = +detector_x_cm
    energy_kev: float = 80.0        # monochromatic for now
    photons: int = 1000             # photons per ray (also used for Poisson noise)
    nx: int = 320
    ny: int = 160
    pixel_mm: float = 1.0
    n_angles: int = 90
    total_deg: float = 360.0
    noise: bool = False
    seed: int = 0
    spectrum: dict | None = None    # {keV: relative weight}; None = monochromatic at energy_kev


SPEC_SCALE = 100_000        # sets only the precision of the spectrum shape

def _spectrum_counts(cfg):
    total = float(sum(cfg.spectrum.values()))
    if total <= 0:
        raise ValueError("Spectrum weights must sum to a positive value.")
    return {float(k): max(int(round(SPEC_SCALE * w / total)), 1)
            for k, w in cfg.spectrum.items()}

def flat_value(cfg):
    """Incident energy per ray in MeV (gVXR has no falloff, so it is the same for every pixel)."""
    if cfg.spectrum:
        return sum(c * k for k, c in _spectrum_counts(cfg).items()) / 1000.0
    return cfg.photons * cfg.energy_kev / 1000.0


def angles(cfg: ScanCfg) -> np.ndarray:
    return np.linspace(0.0, cfg.total_deg, int(cfg.n_angles), endpoint=False)


# gVXR setup (GL thread only)
def _configure_renderer_selection():
    """Request the best available NVIDIA OpenGL path before gVXR loads.

    Linux EGL runtimes (including Colab) may have the NVIDIA driver installed
    without registering its EGL vendor JSON. Desktop Linux can use GLVND's
    NVIDIA offload variables.
    """
    global _renderer_selection
    if _renderer_selection:
        return _renderer_selection

    requested = os.environ.get("CT_SIMULATOR_GPU", "auto").strip().lower()
    if requested not in {"auto", "nvidia", "system"}:
        raise ValueError("CT_SIMULATOR_GPU must be 'auto', 'nvidia', or 'system'.")

    info = {"requested": requested, "platform": sys.platform, "selected_driver": "system default"}
    if requested == "system":
        info["message"] = "Respecting the system OpenGL/EGL device choice."
        _renderer_selection = info
        print(f"[gVXR] Renderer selection: {info['message']}")
        return info

    if sys.platform.startswith("linux"):
        is_headless = os.environ.get("CT_SIMULATOR_LOCAL") != "1"
        if is_headless:
            candidates = [
                "/usr/lib64-nvidia/libEGL_nvidia.so.0",
                "/usr/lib/x86_64-linux-gnu/libEGL_nvidia.so.0",
                "/usr/lib/nvidia/current/libEGL_nvidia.so.0",
                "/usr/local/nvidia/lib64/libEGL_nvidia.so.0",
            ]
            libraries = [p for p in candidates if os.path.exists(p)]
            if not libraries:
                libraries = glob.glob("/usr/lib*/**/libEGL_nvidia.so.0", recursive=True)
            if libraries:
                # Keep an explicitly selected EGL vendor.
                explicit_vendor = os.environ.get("__EGL_VENDOR_LIBRARY_FILENAMES")
                if explicit_vendor:
                    info.update(selected_driver="explicit EGL vendor", nvidia_egl_library=libraries[0],
                                egl_vendor_json=explicit_vendor)
                    info["message"] = "Respecting the existing EGL vendor selection."
                else:
                    vendor_dir = os.path.join(tempfile.gettempdir(), "ct_simulator_egl_vendor")
                    os.makedirs(vendor_dir, exist_ok=True)
                    vendor_json = os.path.join(vendor_dir, "10_nvidia.json")
                    with open(vendor_json, "w", encoding="utf-8") as f:
                        json.dump({"file_format_version": "1.0.0",
                                   "ICD": {"library_path": libraries[0]}}, f)
                    os.environ["__EGL_VENDOR_LIBRARY_FILENAMES"] = vendor_json
                    info.update(selected_driver="NVIDIA EGL", nvidia_egl_library=libraries[0],
                                egl_vendor_json=vendor_json)
                    info["message"] = "Registered NVIDIA EGL before gVXR context creation."
            else:
                info["message"] = (
                    "NVIDIA EGL library not found; EGL will use the system default."
                    if requested == "auto" else
                    "NVIDIA was requested, but its EGL library was not found; EGL will use the system default."
                )
        else:
            # GLVND uses these for NVIDIA PRIME when the GLX driver is installed.
            glx_candidates = [
                "/usr/lib/x86_64-linux-gnu/libGLX_nvidia.so.0",
                "/usr/lib64-nvidia/libGLX_nvidia.so.0",
                "/usr/lib/nvidia/current/libGLX_nvidia.so.0",
            ]
            glx_libraries = [p for p in glx_candidates if os.path.exists(p)]
            if not glx_libraries:
                glx_libraries = glob.glob("/usr/lib*/**/libGLX_nvidia.so.0", recursive=True)
            if glx_libraries:
                os.environ.setdefault("__NV_PRIME_RENDER_OFFLOAD", "1")
                os.environ.setdefault("__GLX_VENDOR_LIBRARY_NAME", "nvidia")
                info.update(selected_driver="NVIDIA GLX", nvidia_glx_library=glx_libraries[0])
                info["message"] = "Requested NVIDIA GLX before desktop OpenGL context creation."
            else:
                info["message"] = (
                    "NVIDIA GLX library not found; using the system desktop OpenGL choice."
                    if requested == "auto" else
                    "NVIDIA was requested, but its GLX library was not found; using the system desktop OpenGL choice."
                )
    else:
        info["message"] = "No automatic GPU selection rule for this platform; using the system default."

    _renderer_selection = info
    print(f"[gVXR] Renderer selection: {info['message']}")
    return info


def renderer_info() -> dict:
    """Return the OpenGL vendor, renderer, and version captured on the GL thread."""
    return dict(_renderer_details)


def renderer_selection_info() -> dict:
    """Return the best-effort platform selection request made before context creation."""
    return dict(_renderer_selection)


def _capture_renderer_info():
    """Query the active context without adding a PyOpenGL dependency."""
    global _renderer_details
    try:
        if sys.platform == "darwin":
            gl = ctypes.CDLL("/System/Library/Frameworks/OpenGL.framework/OpenGL")
        else:
            gl = ctypes.CDLL(ctypes.util.find_library("GL") or "libGL.so.1")
        get_string = gl.glGetString
        get_string.argtypes = [ctypes.c_uint]
        get_string.restype = ctypes.c_char_p
        def query(token):
            value = get_string(token)
            return value.decode("utf-8", errors="replace") if value else None
        _renderer_details = {
            "vendor": query(0x1F00),
            "renderer": query(0x1F01),
            "version": query(0x1F02),
        }
        print(f"[gVXR] OpenGL context: {_renderer_details}")
    except Exception as exc:
        _renderer_details = {"diagnostic_error": str(exc)}
        print(f"[gVXR] OpenGL renderer query failed: {exc}")


def _gx():
    global _context_ready
    _configure_renderer_selection()
    from gvxrPython3 import gvxr

    if not _context_ready:
        if os.environ.get("CT_SIMULATOR_LOCAL") == "1":
            # Use a hidden native context on desktop Linux. Some AMD EGL contexts
            # can crash during creation.
            gvxr.createOpenGLContext()
        else:
            try:
                gvxr.createWindow(0, 0, "EGL")      # headless (Colab)
            except Exception:
                gvxr.createOpenGLContext()
        _capture_renderer_info()
        _context_ready = True
    return gvxr


def _reset_scene(gvxr):
    _labels.clear()
    for name in ("removePolygonMeshesFromSceneGraph",
                 "removePolygonMeshesFromXRayRenderer"):
        fn = getattr(gvxr, name, None)
        if fn is not None:
            try:
                fn()
            except Exception:
                pass


def _rotate(gvxr, deg: float):
    if deg == 0:
        return
    if hasattr(gvxr, "rotateScene"):
        gvxr.rotateScene(deg, 0, 0, 1)
    else:
        for label in _labels:
            gvxr.rotateNode(label, deg, 0, 0, 1)


def _set_material(gvxr, label: str, o: ObjectCfg):
    kind = o.kind
    if kind == "Element":
        try:
            gvxr.setElement(label, int(o.material))
        except ValueError:
            gvxr.setElement(label, o.material)
        if o.density and o.density > 0:
            gvxr.setDensity(label, float(o.density), "g/cm3")
    elif kind == "Compound":
        gvxr.setCompound(label, o.material)
        gvxr.setDensity(label, float(o.density), "g/cm3")
    elif kind == "Mixture":
        gvxr.setMixture(label, o.material)
        gvxr.setDensity(label, float(o.density), "g/cm3")
    else:
        raise ValueError(f"Unknown material kind: {kind}")


def demo_objects() -> list[ObjectCfg]:
    """Return a watertight water block and an off-axis aluminium ball."""
    import trimesh

    block = os.path.join(tempfile.gettempdir(), "demo_water_block.stl")
    ball = os.path.join(tempfile.gettempdir(), "demo_ball.stl")
    ball_radius_mm = 12.0
    block_extents_mm = (60.0, 40.0, 30.0)
    if not os.path.exists(block):
        trimesh.creation.box(extents=block_extents_mm).export(block)
    ball_offset_y_mm = float(block_extents_mm[1] / 2.0 + ball_radius_mm + 5.0)
    if not os.path.exists(ball):
        trimesh.creation.icosphere(subdivisions=3, radius=ball_radius_mm).export(ball)
    return [
        ObjectCfg("Water block", block, "Compound", "H2O", 1.0, (0, 0, 0)),
        ObjectCfg("Aluminium ball", ball, "Element", "Al", 2.70, (0, ball_offset_y_mm, 0)),
    ]


def _load_mesh(path: str):
    """Load a supported surface mesh as a single trimesh object."""
    import trimesh
    ext = os.path.splitext(path)[1].lower()
    if ext not in {".stl", ".obj", ".ply", ".dae"}:
        raise ValueError(f"Unsupported mesh format '{ext or '(no extension)'}'. Use STL, OBJ, PLY, or DAE.")
    try:
        mesh = trimesh.load(path, force="mesh", process=False)
    except Exception as exc:
        if ext == ".dae":
            raise ValueError(f"Could not load {path}. COLLADA support requires the pycollada package.") from exc
        raise ValueError(f"Could not load mesh file {path}: {exc}") from exc
    if len(mesh.vertices) == 0:
        raise ValueError(f"{os.path.basename(path)} contains no geometry.")
    return mesh


def configuration_warnings(cfg: ScanCfg) -> list[str]:
    """Describe open meshes and parts whose bounds indicate a possible overlap."""
    scale = {"mm": 1.0, "cm": 10.0, "m": 1000.0,
             "um": 0.001, "µm": 0.001}.get(str(cfg.stl_unit).lower())
    if scale is None:
        return []

    warnings = []
    bounds = []
    for obj in cfg.objects:
        mesh = _load_mesh(obj.stl_path)
        # STL repeats vertices for each triangle; merge them before checking if the mesh is closed.
        mesh.merge_vertices()
        if not mesh.is_watertight:
            warnings.append(
                f"{obj.name} is an open mesh. The projection will still run; "
                "ground-truth voxels will use the current approximate surface-fill method."
            )
        mesh.apply_scale(scale)
        rx, ry, rz = (float(x or 0) for x in obj.rotate_deg)
        pivot = mesh.bounds.mean(axis=0) if cfg.center_each else (0.0, 0.0, 0.0)
        from trimesh.transformations import rotation_matrix
        for deg, axis in ((rx, (1, 0, 0)), (ry, (0, 1, 0)), (rz, (0, 0, 1))):
            if deg:
                mesh.apply_transform(rotation_matrix(np.deg2rad(deg), axis, pivot))
        if cfg.center_each:
            mesh.apply_translation(-mesh.bounds.mean(axis=0))
        mesh.apply_translation(np.asarray(obj.offset_mm, dtype=float))
        bounds.append(mesh.bounds)

    for i, first in enumerate(cfg.objects):
        for j in range(i + 1, len(cfg.objects)):
            second = cfg.objects[j]
            overlap = np.minimum(bounds[i][1], bounds[j][1]) - np.maximum(bounds[i][0], bounds[j][0])
            if np.all(overlap > 0):
                warnings.append(
                    f"{first.name} and {second.name} have intersecting bounding boxes, "
                    "so the parts may overlap. The scan will still run; where their "
                    "ground-truth voxels overlap, the later part takes precedence."
                )
    return warnings


def oriented_stl(o: ObjectCfg, center_each: bool, tmp_dir: str, index: int = 0) -> str:
    """Return an STL path for gVXR, converting other formats and applying rotation.

    The rotation is baked into a temporary copy of the mesh, so it does not
    depend on how gVXR composes its own transforms. Axes are the fixed world
    axes, applied in the order x, y, z. The pivot is the mesh's bounding-box
    centre when meshes are centred individually, otherwise the origin, so parts
    of one assembly keep their positions relative to each other.
    """
    rx, ry, rz = (float(v or 0.0) for v in o.rotate_deg)
    ext = os.path.splitext(o.stl_path)[1].lower()
    if ext == ".stl" and rx == 0.0 and ry == 0.0 and rz == 0.0:
        return o.stl_path

    from trimesh.transformations import rotation_matrix

    mesh = _load_mesh(o.stl_path)
    pivot = mesh.bounds.mean(axis=0) if center_each else np.zeros(3)

    M = np.eye(4)
    for deg, axis in ((rx, (1, 0, 0)), (ry, (0, 1, 0)), (rz, (0, 0, 1))):
        if deg:
            M = rotation_matrix(np.deg2rad(deg), axis, pivot) @ M
    mesh.apply_transform(M)

    out = os.path.join(tmp_dir, f"mesh_{index}.stl")
    mesh.export(out)
    return out


# Scan setup and acquisition (GL thread only)
def build_scene(cfg: ScanCfg) -> np.ndarray:
    """Set up source, detector and objects. Returns the flat-field image."""
    if not cfg.objects:
        raise ValueError("No objects: upload an STL, OBJ, PLY, or DAE file, or tick the demo box.")
    for o in cfg.objects:
        if not os.path.exists(o.stl_path):
            raise FileNotFoundError(o.stl_path)
        if os.path.splitext(o.stl_path)[1].lower() not in {".stl", ".obj", ".ply", ".dae"}:
            raise ValueError(f"Unsupported mesh format for {o.name}. Use STL, OBJ, PLY, or DAE.")

    gvxr = _gx()
    _reset_scene(gvxr)

    gvxr.setSourcePosition(-float(cfg.source_x_cm), 0.0, 0.0, "cm")
    if cfg.beam == "Parallel":
        gvxr.useParallelBeam()
    else:
        gvxr.usePointSource()
    if cfg.spectrum:                                  # polychromatic beam (beam hardening)
        gvxr.resetBeamSpectrum()
        for kev, c in _spectrum_counts(cfg).items():
            gvxr.addEnergyBinToSpectrum(kev, "keV", c)
    else:
        gvxr.setMonoChromatic(float(cfg.energy_kev), "keV", int(cfg.photons))

    gvxr.setDetectorPosition(float(cfg.detector_x_cm), 0.0, 0.0, "cm")
    gvxr.setDetectorUpVector(0, 0, -1)
    gvxr.setDetectorNumberOfPixels(int(cfg.nx), int(cfg.ny))
    gvxr.setDetectorPixelSize(float(cfg.pixel_mm), float(cfg.pixel_mm), "mm")

    flat = np.full((cfg.ny, cfg.nx), flat_value(cfg), dtype=np.float32)

    tmp_dir = tempfile.mkdtemp(prefix="oriented_")
    try:
        for i, o in enumerate(cfg.objects):
            label = f"obj{i}_{o.name}".replace(" ", "_")
            path = oriented_stl(o, cfg.center_each, tmp_dir, i)
            gvxr.loadMeshFile(label, path, cfg.stl_unit)
            _labels.append(label)
            if cfg.center_each:
                gvxr.moveToCentre(label)
            ox, oy, oz = o.offset_mm
            if ox or oy or oz:
                gvxr.translateNode(label, float(ox), float(oy), float(oz), "mm")
            _set_material(gvxr, label, o)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return flat


def acquire(n: int, step_deg: float) -> np.ndarray:
    """Compute n projections, rotating the scene by step_deg after each."""
    gvxr = _gx()
    out = []
    for _ in range(int(n)):
        out.append(np.array(gvxr.computeXRayImage(), dtype=np.float32))
        _rotate(gvxr, step_deg)
    return np.stack(out)


def single_projection(cfg: ScanCfg, angle_deg: float = 0.0):
    flat = build_scene(cfg)
    gvxr = _gx()
    _rotate(gvxr, angle_deg)
    raw = np.array(gvxr.computeXRayImage(), dtype=np.float32)
    return raw, flat


# Post-processing
def quick_recon(atten: np.ndarray, cfg: ScanCfg, row: int | None = None) -> np.ndarray:
    """Parallel-beam filtered back-projection of one detector row.

    Only a quick-look check: the real geometry is cone-beam, so sizes and
    values are approximate. Use TIGRE/ASTRA for proper reconstruction.
    """
    from skimage.transform import iradon

    if row is None:
        row = atten.shape[1] // 2
    sino = atten[:, row, :].T                       # (detector, angles)
    return iradon(sino, theta=angles(cfg), filter_name="ramp", circle=False)


def _geometry(cfg: ScanCfg) -> dict:
    """Derived acquisition geometry in mm (shared by both dataset writers)."""
    sod = float(cfg.source_x_cm) * 10.0                               # source -> rotation axis
    sdd = (float(cfg.source_x_cm) + float(cfg.detector_x_cm)) * 10.0  # source -> detector
    mag = sdd / sod if cfg.beam == "Cone" else 1.0
    return {
        "beam": cfg.beam,
        "SOD_mm": sod,
        "SDD_mm": sdd,
        "magnification": mag,
        "detector_pixels_nx_ny": [int(cfg.nx), int(cfg.ny)],
        "detector_pixel_size_mm": float(cfg.pixel_mm),
        "voxel_size_mm_at_object": float(cfg.pixel_mm) / mag,
        "rotation_axis": "z (vertical axis of the detector image)",
        "rotation_center_offset_mm": 0.0,
        "n_projections": int(cfg.n_angles),
        "total_angle_deg": float(cfg.total_deg),
        "angle_step_deg": float(cfg.total_deg) / int(cfg.n_angles),
        "angle_convention": "gVXR rotateScene about +z, degrees, end angle excluded",
        "beam_spectrum": "polychromatic" if cfg.spectrum else "monochromatic",
        "energy_keV": None if cfg.spectrum else float(cfg.energy_kev),
        "spectrum_keV_weights": ({str(k): float(v) for k, v in cfg.spectrum.items()}
                                 if cfg.spectrum else None),
        "photons_per_ray": int(cfg.photons),
        "noise_applied": bool(cfg.noise),
        "noise_seed": int(cfg.seed),
        "projection_data": "attenuation = -ln(I/I0), float32",
    }


def _material_mu_cm_inv(obj, energy_kev):
    """Return linear attenuation in cm^-1 using xraylib; fail clearly if unsupported."""
    try:
        import xraylib
        if obj.kind == "Element":
            z = int(obj.material) if str(obj.material).isdigit() else xraylib.SymbolToAtomicNumber(str(obj.material))
            mass_mu = xraylib.CS_Total(z, float(energy_kev))
        else:
            formula = str(obj.material)
            mass_mu = xraylib.CS_Total_CP(formula, float(energy_kev))
        return float(mass_mu) * float(obj.density)
    except Exception as e:
        raise ValueError(f"Cannot calculate attenuation for {obj.name} ({obj.material}); install xraylib and use a valid chemical formula: {e}") from e


def ground_truth_volumes(cfg: ScanCfg):
    """Voxelize the mesh parts into an aligned label volume and mono-energy mu volume.

    The volume uses object-space isotropic voxels at the detector sampling pitch
    corrected for magnification. Label 0 denotes background; objects are numbered
    from 1 in cfg.objects order (later objects take precedence where they overlap).
    """
    import trimesh
    pitch = max(float(cfg.pixel_mm) / max(float(_geometry(cfg)["magnification"]), 1e-9), 1e-6)
    meshes=[]
    scale={"mm":1.0,"cm":10.0,"m":1000.0,"um":0.001,"µm":0.001}.get(str(cfg.stl_unit).lower())
    if scale is None: raise ValueError(f"Unsupported STL unit for ground truth: {cfg.stl_unit}")
    for obj in cfg.objects:
        mesh=_load_mesh(obj.stl_path)
        # STL repeats vertices for each triangle. Merge them so watertight meshes
        # can use the C++ voxelizer.
        mesh.merge_vertices()
        # Use the C++ voxelizer for watertight meshes; trimesh handles open meshes.
        mesh.apply_scale(scale)
        rx,ry,rz=(float(x or 0) for x in obj.rotate_deg)
        pivot=mesh.bounds.mean(axis=0) if cfg.center_each else np.zeros(3)
        from trimesh.transformations import rotation_matrix
        for deg,axis in ((rx,(1,0,0)),(ry,(0,1,0)),(rz,(0,0,1))):
            if deg: mesh.apply_transform(rotation_matrix(np.deg2rad(deg),axis,pivot))
        if cfg.center_each: mesh.apply_translation(-mesh.bounds.mean(axis=0))
        mesh.apply_translation(np.asarray(obj.offset_mm,dtype=float))
        meshes.append(mesh)
    bounds=np.array([m.bounds for m in meshes])
    # Voxel centers are at origin + (index + 0.5) * pitch. Pad the grid so
    # flood fill can reach the exterior.
    lo=np.floor(bounds[:,0,:].min(axis=0)/pitch)*pitch-pitch
    hi=np.ceil(bounds[:,1,:].max(axis=0)/pitch)*pitch+pitch
    shape=tuple(np.maximum(np.ceil((hi-lo)/pitch).astype(int),1))
    labels=np.zeros(shape,dtype=np.uint16)
    try:
        import ct_voxel
    except ImportError as e:
        raise RuntimeError("Build the notebook's C++ ct_voxel extension before generating ground truth.") from e
    for label,mesh in enumerate(meshes,1):
        if mesh.is_watertight:
            mask=ct_voxel.voxelize(np.asarray(mesh.vertices,dtype=np.float64),
                                   np.asarray(mesh.faces,dtype=np.int64),
                                   tuple(map(float,lo)),float(pitch),tuple(map(int,shape)))
            labels[np.asarray(mask,dtype=bool)]=label
        else:
            # Map world-space voxel centers into trimesh coordinates to avoid a
            # half-voxel offset.
            vox=mesh.voxelized(pitch).fill()
            T=np.linalg.inv(vox.transform)
            cells=np.indices(shape).reshape(3,-1).T
            centres=lo+(cells+0.5)*pitch
            vi=np.rint(centres@T[:3,:3].T+T[:3,3]).astype(int)
            ok=np.all((vi>=0)&(vi<np.asarray(vox.matrix.shape)),axis=1)
            inside=np.zeros(len(cells),dtype=bool)
            inside[ok]=vox.matrix[vi[ok,0],vi[ok,1],vi[ok,2]]
            labels[inside.reshape(shape)]=label
    counts=_spectrum_counts(cfg) if cfg.spectrum else None
    energy=(sum(n*e for e,n in counts.items())/sum(counts.values())) if counts else float(cfg.energy_kev)
    mu_by_label=np.asarray([0.0]+[_material_mu_cm_inv(o,energy) for o in cfg.objects],dtype=np.float32)
    mu=mu_by_label[labels]
    return labels,mu,{"axis_order":"x,y,z","origin_mm":lo.tolist(),"voxel_size_mm":pitch,
                      "label_map":{"0":"background",**{str(i+1):o.name for i,o in enumerate(cfg.objects)}},
                      "attenuation_energy_kev":energy,"attenuation_units":"cm^-1",
                      "attenuation_method":"xraylib total mass attenuation coefficient times density"}


def save_dataset(cfg: ScanCfg, atten: np.ndarray, raw: np.ndarray,
                 flat: np.ndarray, out_dir: str | None = None,
                 dark_level: float = 0.0, effects: dict | None = None,
                 make_zip: bool = True) -> str:
    """Write a reconstruction-ready dataset folder and return the path of a .zip
    (or of the folder itself when make_zip=False).

    Layout:
        projections/proj_0000.tif ...   attenuation, float32, one file per angle
        raw_projections.npy             raw detector intensities (n, ny, nx)
        flat.tif, dark.tif              flat field and dark field
        angles.csv                      index, angle_deg
        geometry.json                   SOD, SDD, pixel size, magnification, ...
        scan_config.json                full ScanCfg (materials, energy, noise, ...)
        effects.json                    imaging effects applied (if given)
    """
    import csv
    import tifffile

    out_dir = out_dir or tempfile.mkdtemp(prefix="ctsim_")
    proj_dir = os.path.join(out_dir, "projections")
    os.makedirs(proj_dir, exist_ok=True)

    # --- projections: one TIFF per angle ---------------------------------
    ang = angles(cfg)
    for i in range(len(atten)):
        tifffile.imwrite(os.path.join(proj_dir, f"proj_{i:04d}.tif"),
                         atten[i].astype(np.float32))

    # --- flat / dark / raw -----------------------------------------------
    tifffile.imwrite(os.path.join(out_dir, "flat.tif"), flat.astype(np.float32))
    tifffile.imwrite(os.path.join(out_dir, "dark.tif"),
                     np.full(flat.shape, float(dark_level), dtype=np.float32))
    np.save(os.path.join(out_dir, "raw_projections.npy"), raw.astype(np.float32))

    # --- angles ------------------------------------------------------------
    with open(os.path.join(out_dir, "angles.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["index", "angle_deg"])
        for i, a in enumerate(ang):
            w.writerow([i, float(a)])

    # --- geometry (derived values, in mm) ----------------------------------
    with open(os.path.join(out_dir, "geometry.json"), "w") as f:
        json.dump(_geometry(cfg), f, indent=2)

    # --- full scan configuration (materials, offsets, rotations, ...) ------
    with open(os.path.join(out_dir, "scan_config.json"), "w") as f:
        json.dump(asdict(cfg), f, indent=2, default=str)
    if effects is not None:
        with open(os.path.join(out_dir, "effects.json"), "w") as f:
            json.dump(effects, f, indent=2, default=str)
    if not make_zip:
        return out_dir

    # --- zip the whole folder ----------------------------------------------
    zip_path = os.path.join(out_dir, "ct_dataset.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _, files in os.walk(out_dir):
            for fn in files:
                if fn == "ct_dataset.zip":
                    continue
                p = os.path.join(root, fn)
                z.write(p, arcname=os.path.relpath(p, out_dir))
    return zip_path


def save_sinogram_dataset(cfg: ScanCfg, atten: np.ndarray,
                          rows: list | None = None,
                          out_dir: str | None = None) -> str:
    """Write sinogram data only and return the path of a .zip.

    Layout:
        sinograms/sino_row_XXXX.tif   one (n_angles x nx) sinogram per detector row
        sinogram_stack.npy            all rows, shape (ny, n_angles, nx)
        angles.csv, geometry.json, scan_config.json
    rows=None writes every detector row as a TIFF; pass e.g.
    [ny // 4, ny // 2, 3 * ny // 4] to keep the zip small for big scans.
    """
    import csv
    import tifffile

    out_dir = out_dir or tempfile.mkdtemp(prefix="ctsino_")
    sino_dir = os.path.join(out_dir, "sinograms")
    os.makedirs(sino_dir, exist_ok=True)

    ny = atten.shape[1]
    rows = list(range(ny)) if rows is None else rows
    for r in rows:
        tifffile.imwrite(os.path.join(sino_dir, f"sino_row_{r:04d}.tif"),
                         atten[:, r, :].astype(np.float32))

    np.save(os.path.join(out_dir, "sinogram_stack.npy"),
            np.transpose(atten, (1, 0, 2)).astype(np.float32))

    with open(os.path.join(out_dir, "angles.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["index", "angle_deg"])
        for i, a in enumerate(angles(cfg)):
            w.writerow([i, float(a)])

    with open(os.path.join(out_dir, "geometry.json"), "w") as f:
        json.dump(_geometry(cfg), f, indent=2)
    with open(os.path.join(out_dir, "scan_config.json"), "w") as f:
        json.dump(asdict(cfg), f, indent=2, default=str)

    zip_path = os.path.join(out_dir, "sinogram_dataset.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _, files in os.walk(out_dir):
            for fn in files:
                if fn == "sinogram_dataset.zip":
                    continue
                p = os.path.join(root, fn)
                z.write(p, arcname=os.path.relpath(p, out_dir))
    return zip_path
