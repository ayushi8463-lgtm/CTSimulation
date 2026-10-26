"""Writes win_env.bat with paths for the Python that will be embedded in the C++ server."""
import os, sys, sysconfig
from pathlib import Path

base = sys.base_prefix
purelib = sysconfig.get_paths()["purelib"]          # venv\Lib\site-packages when run from the venv
include = os.path.join(base, "include")
libs = os.path.join(base, "libs")
if not os.path.isfile(os.path.join(include, "Python.h")):
    sys.exit(f"Python.h not found in {include}. Install Python from python.org (not the Microsoft Store build) "
             "and keep 'Download debug binaries / development files' defaults.")
with open(Path(__file__).resolve().parent / "win_env.bat", "w") as f:
    f.write(f'set "PY_BASE={base}"\n')
    f.write(f'set "PY_INCLUDE={include}"\n')
    f.write(f'set "PY_LIBS={libs}"\n')
    f.write(f'set "PY_SITE={purelib}"\n')
print("Python base:", base, "| site-packages:", purelib)
