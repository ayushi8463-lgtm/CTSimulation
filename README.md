# gVXR CT Simulator

A local CT simulation app with a browser interface, a C++ HTTP server, and a
Python/gVXR scan engine. The browser UI is served at `http://127.0.0.1:5000`;
scan processing and generated files stay on the computer running the server.

## Fresh Linux setup (Ubuntu/Debian)

These instructions start from a downloaded/cloned copy of this folder and do
not depend on the Python environment used to develop it. Use a desktop Linux
session with a working OpenGL driver. CPU/software OpenGL can work, but scans
may be much slower. NVIDIA users should install the NVIDIA driver recommended
for their distribution before starting the app.

### 1. Install system dependencies

From this project folder, run:

```bash
sudo apt update
sudo apt install -y build-essential python3 python3-dev python3-venv python3-pip \
  libgomp1 libgl1 libegl1 libopengl0 libglu1-mesa libgl1-mesa-dri \
  libegl-mesa0 libglx-mesa0 libx11-6 libxext6 libxrender1 libxrandr2 \
  libxinerama1 libxcursor1 libxi6 libxxf86vm1 libxcb1 libxcb-res0 \
  libxcb-xinerama0 libxcb-xkb1 libxcb-icccm4 libxcb-image0 \
  libxcb-keysyms1 libxcb-render-util0 libxcb-shape0 libxcb-randr0 \
  libxcb-composite0 libxcb-cursor0 libxcb-dri2-0 libxcb-dri3-0 \
  libxcb-ewmh2 libxcb-glx0 libxcb-present0 libxcb-sync1 libxcb-xfixes0 \
  libxcb-xinput0 libxcb-util1 libxcb-xv0 libxcb-damage0 libxcb-dpms0 \
  libxcb-record0 libxcb-screensaver0 libxcb-shm0 libxcb-xtest0 \
  libxcb-xf86dri0 libxcb-xvmc0 libxkbcommon-x11-0
```

Or use the helper script, which runs the same package installation and creates
the Python environment, installs the Python dependencies, and builds the
voxelizer:

```bash
./setup_local_linux.sh
```

### 2. Create the Python environment

If you installed system packages manually, create the isolated environment and
install the Python dependencies:

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install -r requirements-local.txt
./.venv/bin/python build_voxel.py
```

`requirements-local.txt` includes the same simulation and image-processing
packages installed by the Colab notebook (`gvxr`, NumPy, Matplotlib, TIFF,
mesh, image, scientific, HDF5, YAML, X-ray attenuation, and pybind11 packages),
plus setuptools to build the voxelizer and notebook-support packages. Colab
provides its own notebook runtime. The app server itself does not require Flask
or a web framework.

### 3. Start the app

```bash
./run_cpp_server.sh
```

Open [http://127.0.0.1:5000](http://127.0.0.1:5000) in a browser on this
computer. Keep the terminal open while using the app; press **Ctrl+C** there to
stop it. Choose another port by passing it as an argument:

```bash
./run_cpp_server.sh 5050
```

Use `CT_SIMULATOR_GPU=auto` (default) to prefer NVIDIA when available,
`CT_SIMULATOR_GPU=nvidia` to explicitly request NVIDIA, or
`CT_SIMULATOR_GPU=system` to let the system choose the OpenGL adapter. The
server prints the OpenGL vendor, renderer, and version when it creates the
graphics context.

The setup helper builds the voxelizer before you start the server. It is used
by **Generate datasets** to create ground-truth volumes; single projections
and full scans do not call it. Running the build during setup means every UI
action is ready when the server starts.

### Run in Google Colab

To run the app in Colab, open
[CT_Simulator_Colab.ipynb](notebooks/CT_Simulator_Colab.ipynb) in Google Colab
and run its cells from top to bottom. The notebook installs its Python and
system dependencies, asks you to upload the project source files, builds the
C++ voxelizer, and compiles and starts the web server.

Use the notebook's built-in embedded view to open the app. If it stays blank,
run the Cloudflare tunnel section and open the temporary URL it prints. Keep
the Colab runtime running while using the app; generated files live in that
runtime.

## Troubleshooting

- **`python3-config` or Python headers missing:** install `python3-dev` and
  `build-essential`, then rerun the server launcher.
- **`No module named setuptools` while building the voxelizer:** run
  `./.venv/bin/python -m pip install -r requirements-local.txt`, then rerun
  `./.venv/bin/python build_voxel.py`.
- **gVXR/OpenGL library or context errors:** confirm the system libraries above
  are installed, launch from the graphical desktop session, and check that the
  graphics driver is working. A headless SSH session may not have a desktop
  OpenGL context.
- **Port already in use:** stop the other server or choose a different port.
- **Mesh import:** STL, OBJ, PLY, and COLLADA DAE are supported. OBJ, PLY, and
  DAE are converted to STL for gVXR; DAE support comes from `pycollada`.

## Project layout

- `index.html` — browser interface
- `cpp_server.cpp`, `run_cpp_server.sh` — local HTTP server and launcher
- `simulator_engine.py`, `ct_core.py`, `ct_variations.py` — scan and image
  processing
- `requirements-local.txt`, `setup_local_linux.sh` — Python/system setup
- `build_voxel.py`, `ct_voxel.cpp` — ground-truth voxelizer built during setup
- `notebooks/` — Colab and validation/reconstruction examples
- `docs/` — additional setup notes and reference material

See [docs/LOCAL_SETUP.md](docs/LOCAL_SETUP.md) for implementation details.
