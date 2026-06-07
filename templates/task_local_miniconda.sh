#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_DIR="$ROOT/.miniconda3"
if [[ -x "$INSTALL_DIR/bin/python" ]]; then
  echo "Miniconda already installed at $INSTALL_DIR"
  exit 0
fi
mkdir -p "$INSTALL_DIR"
wget -O "$ROOT/miniconda.sh" https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh
bash "$ROOT/miniconda.sh" -b -u -p "$INSTALL_DIR"
rm -f "$ROOT/miniconda.sh"
"$INSTALL_DIR/bin/conda" config --set auto_activate_base false || true
mkdir -p "$ROOT/.conda/envs" "$ROOT/.conda/pkgs"
echo "Installed task-local Miniconda at $INSTALL_DIR"
echo "Use: source $INSTALL_DIR/etc/profile.d/conda.sh"
