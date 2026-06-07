#!/usr/bin/env bash
set -euo pipefail
sudo systemctl stop autoresearch.service
sudo systemctl status autoresearch.service --no-pager || true
