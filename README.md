# gVXR CT Simulator

The simulator uses Python for gVXR and CT processing, a native C++ server for
the web interface and HTTP API, and a C++ voxelizer for optional ground-truth
volumes.

## Run locally

- Linux: follow [local setup](docs/LOCAL_SETUP.md), then run
  `./run_cpp_server.sh` from this folder.
- Windows: follow [Windows setup](docs/LOCAL_SETUP_WINDOWS.md), then run
  `run_cpp_server.bat` from this folder.
- Google Colab: open [CT_Simulator_Colab.ipynb](notebooks/CT_Simulator_Colab.ipynb)
  and run its cells from top to bottom.

## Folders

- `notebooks/` — Colab setup, benchmark, validation, and reconstruction examples.
- `docs/` — setup instructions and project plan; `docs/reference/` contains the
  project brief PDF.
- `presentation/` — slide decks and their source data.

The server and Python modules stay in the project root because the launchers
and embedded Python load them from there. Keep the root files together when
copying the project or uploading it to Colab.

The web interface accepts STL, OBJ, PLY, and COLLADA DAE surface meshes. OBJ,
PLY, and DAE files are read with `trimesh` and converted to STL for the gVXR
scene; DAE loading uses the `pycollada` dependency.
