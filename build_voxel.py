"""Build ct_voxel in place; use --no-openmp when OpenMP is unavailable."""
import os
from pathlib import Path
import sys
import pybind11
from setuptools import setup, Extension

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)

omp = "--no-openmp" not in sys.argv
if sys.platform == "win32":
    args = ["/O2", "/std:c++17", "/utf-8"] + (["/openmp:llvm"] if omp else [])
    link = []
else:
    args = ["-O3", "-std=c++17"] + (["-fopenmp"] if omp else [])
    link = ["-fopenmp"] if omp else []
ext = Extension("ct_voxel", ["ct_voxel.cpp"], include_dirs=[pybind11.get_include()],
                extra_compile_args=args, extra_link_args=link, language="c++")
setup(name="ct_voxel", ext_modules=[ext], script_args=["build_ext", "--inplace"])
