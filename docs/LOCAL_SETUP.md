# gVXR CT Simulator: Local setup

## What the Local option does

In Local mode, the C++ HTTP server listens on this computer. The browser opens
`http://127.0.0.1:5000`, submits scan settings, and displays CT previews and
download links. The C++ process parses requests, serves routes, and manages
jobs. Python handles scan computation and gVXR through `simulator_engine.py`.
There is no Flask dependency. Scan data is computed locally; the Local option
does not use a Cloudflare tunnel or a Colab proxy.

The server listens on all network interfaces, so another device on the same
network can open it using this computer's LAN address, for example
`http://10.81.34.247:5000`.

## What changed

- Added a `RUN_MODE` setting to the notebook. Choose `"local"` for Jupyter on
  this computer or `"colab"` for Google Colab. Colab retains its Cloudflare and
  Colab proxy options.
- The C++ server embeds Python and runs independently of the notebook kernel.
  A native graphics crash in the server therefore does not restart the Jupyter
  kernel.
- Local startup sets `CT_SIMULATOR_LOCAL=1`. `ct_core.py` uses that setting to
  create gVXR's hidden desktop OpenGL context with `gvxr.createOpenGLContext()`.
  This avoids the EGL device path that selected the AMD device and segfaulted
  on this machine. Without the setting, headless Colab continues to use EGL.
- The Python package list is recorded in `requirements-local.txt`, and the
  project environment is `.venv` with the `Python (CT Simulator .venv)` kernel.

## Run the server directly

From the project root (the parent folder of `docs/`), run:

```bash
./run_cpp_server.sh
```

The script compiles `cpp_server.cpp` to `ct_simulator_server` and launches it
on port 5000. It uses `.venv/bin/python` when available, or `python3`
otherwise. The selected Python environment must have the packages in
`requirements-local.txt`, and the system must provide `python3-config` plus
Python development headers and libraries for the same Python version.

Open `http://127.0.0.1:5000` on this computer. Stop the server with
**Ctrl+C** in the terminal. To select another port, pass it to the script,
for example `./run_cpp_server.sh 5050`. Set `PYTHON_BIN` to use another Python
interpreter.

The Colab notebook also builds and starts the C++ server. Both local and Colab
web interfaces therefore use the same HTTP implementation.

Use one server process at a time. If one is already running on port 5000, stop
it before starting another. Server diagnostics appear in the terminal.

## Environment setup

The `.venv` environment already has the packages from `requirements-local.txt`
and the registered Jupyter kernel. To recreate it on this Ubuntu machine:

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements-local.txt
./.venv/bin/python -m ipykernel install --prefix="$PWD/.venv" \
  --name ct-simulator --display-name "Python (CT Simulator .venv)"
```

The gVXR wheel also requires system graphics libraries and a working desktop
OpenGL stack. The notebook's first setup section lists the Ubuntu packages.

`ct_core.py` now requests NVIDIA GLX when its driver is installed, reports the
actual OpenGL vendor/renderer at context creation, and warns when it detects a
software renderer such as `llvmpipe`. `CT_SIMULATOR_GPU=auto` (the default)
requests NVIDIA when available; `nvidia` makes that request explicit, and
`system` leaves adapter choice to the system. The reported renderer, rather
than the GPU being present, confirms which device performed projection
rendering.
