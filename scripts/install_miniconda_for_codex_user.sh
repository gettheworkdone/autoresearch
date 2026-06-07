#!/usr/bin/env bash
set -euo pipefail
CODEX_USER="${CODEX_USER:-codexrun}"
if [[ $EUID -ne 0 ]]; then
  echo "Run as root: sudo bash scripts/install_miniconda_for_codex_user.sh" >&2
  exit 1
fi
sudo -iu "$CODEX_USER" bash -lc '
set -euo pipefail
mkdir -p "$HOME/miniconda3"
wget https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh -O "$HOME/miniconda3/miniconda.sh"
bash "$HOME/miniconda3/miniconda.sh" -b -u -p "$HOME/miniconda3"
rm -f "$HOME/miniconda3/miniconda.sh"
"$HOME/miniconda3/bin/conda" init bash
"$HOME/miniconda3/bin/conda" config --set auto_activate_base false || true
"$HOME/miniconda3/bin/python" --version
'
echo "Miniconda installed for $CODEX_USER. Restart shells/services if needed."
