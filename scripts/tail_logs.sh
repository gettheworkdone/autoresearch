#!/usr/bin/env bash
set -euo pipefail
sudo journalctl -u autoresearch.service -f
