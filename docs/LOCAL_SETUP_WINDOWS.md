# gVXR CT Simulator: Windows local setup (no Cloudflare / Colab)

Prerequisites (one time):
1. Python 3.10-3.12 from python.org, 64-bit, with "Add to PATH" ticked (not the Microsoft Store build).
2. Visual Studio 2022 Build Tools with the C++ workload (needed to compile the server). Install from an admin PowerShell, then open a NEW PowerShell window:

       winget install Microsoft.VisualStudio.2022.BuildTools --override "--quiet --wait --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"

3. Up-to-date GPU driver with OpenGL support (gVXR uses a hidden desktop OpenGL context on Windows).

`ct_core.py` logs the OpenGL vendor and renderer when the context starts. Windows
and the graphics driver choose the desktop OpenGL adapter; on hybrid laptops,
add `ct_simulator_server.exe` (and the Python executable if needed) under
**Settings → System → Display → Graphics** and select **High performance**.
Confirm the log reports the intended GPU; a software renderer such as `llvmpipe`
means the run is not using GPU OpenGL.

Run these in PowerShell from the project root (the parent folder of `docs/`):

    python -m venv .venv
    .\.venv\Scripts\python.exe -m pip install -r requirements-windows.txt
    .\.venv\Scripts\python.exe build_voxel.py          # only needed for ground-truth volumes
    .\run_cpp_server.bat                                # or: .\run_cpp_server.bat 5050

Open http://127.0.0.1:5000. Stop with Ctrl+C. Windows Firewall may ask once to allow the server;
choose "Private networks" (the server listens on all interfaces, like on Linux).

Dataset CLI:  .\.venv\Scripts\python.exe make_dataset.py CONFIG.yaml

Troubleshooting:
- "Python.h not found": use the python.org installer, not the Store version.
- `ct_voxel` import fails with a libomp140 error: run `python build_voxel.py --no-openmp`.
- gVXR blank/crash on startup: update the GPU driver; on laptops set python.exe / ct_simulator_server.exe to use the high-performance GPU in Windows Graphics settings.
- Server exits at startup: run from cmd to read the printed Python traceback.
