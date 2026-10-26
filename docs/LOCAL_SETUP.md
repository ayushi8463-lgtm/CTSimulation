# Local Linux setup details

For a complete clean-install guide, use the [project README](../README.md).
This page describes the local runtime behavior and environment controls.

## Runtime

The C++ HTTP server listens on port 5000 by default. The browser opens
`http://127.0.0.1:5000`, submits scan settings, and displays previews/downloads.
Python handles computation and gVXR through `simulator_engine.py`. There is no
Flask dependency, Cloudflare tunnel, or Colab proxy in Local mode.

The launcher compiles `cpp_server.cpp` using the system C++ compiler and Python
embed development headers, then runs the server. It uses `.venv/bin/python`
when present, otherwise `python3`. The selected Python environment must have
the packages in `requirements-local.txt`.

The Linux setup helper builds the voxelizer as part of environment setup, in
line with the Colab notebook's setup section. Single projections and full scans
can run without it, while **Generate datasets** uses it to create
ground-truth volumes. If you created the Python environment manually, build it
with:

```bash
./.venv/bin/python build_voxel.py
```

Complete setup before launching the server so dataset generation is ready
immediately.

## Graphics environment

Local startup sets `CT_SIMULATOR_LOCAL=1`. `ct_core.py` uses
`gvxr.createOpenGLContext()` for the desktop OpenGL context. Headless Colab
continues to use its EGL context path. On first context creation, the Python
engine reports the OpenGL vendor, renderer, and version.

`CT_SIMULATOR_GPU=auto` (default) requests NVIDIA when available;
`nvidia` makes that request explicit; `system` leaves adapter selection to the
system. The renderer reported by OpenGL is the indication of which device did
the work.

## Direct launch

```bash
./run_cpp_server.sh              # default port 5000
./run_cpp_server.sh 5050         # choose a port
CT_SIMULATOR_GPU=system ./run_cpp_server.sh
```

Set `PYTHON_BIN` if using a Python interpreter other than the local `.venv`.
The launcher needs `python3-config` and Python development files for a matching
Python version.
