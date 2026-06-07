#!/usr/bin/env bash
set -euo pipefail
CODEX_USER="${CODEX_USER:-codexrun}"
sudo -iu "$CODEX_USER" bash -lc 'which codex && codex --version && codex login status'
