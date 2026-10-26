#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [[ ! -r /etc/os-release ]]; then
  echo "Cannot identify this Linux distribution. Install the packages in README.md manually." >&2
  exit 1
fi
# shellcheck disable=SC1091
source /etc/os-release
if [[ "${ID:-}" != "ubuntu" && "${ID:-}" != "debian" && " ${ID_LIKE:-} " != *" debian "* ]]; then
  echo "This helper supports Ubuntu/Debian. On other distributions, install the equivalent packages from README.md." >&2
  exit 1
fi

if [[ "$(id -u)" -eq 0 ]]; then
  APT=(apt-get)
elif command -v sudo >/dev/null 2>&1; then
  APT=(sudo apt-get)
else
  echo "Install system packages as root or install sudo, then rerun this script." >&2
  exit 1
fi

"${APT[@]}" update
"${APT[@]}" install -y \
  build-essential python3 python3-dev python3-venv python3-pip \
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

python3 -m venv .venv
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install -r requirements-local.txt
./.venv/bin/python build_voxel.py

echo
echo "Setup complete, including the C++ voxelizer. Start the simulator with: ./run_cpp_server.sh"
