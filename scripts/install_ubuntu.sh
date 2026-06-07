#!/usr/bin/env bash
set -euo pipefail

APP_NAME="${APP_NAME:-autoresearch}"
SERVICE_NAME="${SERVICE_NAME:-autoresearch}"
INSTALL_DIR="${INSTALL_DIR:-/opt/autoresearch}"
DATA_ROOT="${DATA_ROOT:-/var/lib/autoresearch}"
BOT_USER="${BOT_USER:-researchbot}"
CODEX_USER="${CODEX_USER:-codexrun}"
GROUP="${GROUP:-aragent}"
NODE_MAJOR_REQUIRED="${NODE_MAJOR_REQUIRED:-22}"
MINICONDA_ROOT="${MINICONDA_ROOT:-/opt/autoresearch-miniconda3}"

log() {
  echo "[autoresearch-install] $*"
}

fail() {
  echo "[autoresearch-install] ERROR: $*" >&2
  exit 1
}

if [[ $EUID -ne 0 ]]; then
  fail "Run as root: sudo bash scripts/install_ubuntu.sh"
fi

case "$MINICONDA_ROOT" in
  /root|/root/*)
    fail "MINICONDA_ROOT must not be under /root. systemd runs as $BOT_USER and cannot execute Python through /root. Use /opt/autoresearch-miniconda3."
    ;;
esac

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

stop_old_services() {
  log "Stopping current/old services if they exist."
  systemctl disable --now "${SERVICE_NAME}.service" >/dev/null 2>&1 || true
  systemctl daemon-reload || true
}

install_base_packages() {
  log "Installing base Ubuntu packages."
  apt-get update
  apt-get install -y \
    ca-certificates \
    curl \
    git \
    gnupg \
    lsb-release \
    rsync \
    sudo \
    tar \
    unzip \
    wget \
    xz-utils
}

ensure_node() {
  local current_major="0"
  if command -v node >/dev/null 2>&1; then
    current_major="$(node -p 'Number(process.versions.node.split(".")[0])' 2>/dev/null || echo 0)"
  fi

  if [[ "$current_major" =~ ^[0-9]+$ ]] && (( current_major >= NODE_MAJOR_REQUIRED )); then
    log "Node.js $(node --version) found."
    return 0
  fi

  log "Installing Node.js ${NODE_MAJOR_REQUIRED}.x from NodeSource."
  curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR_REQUIRED}.x" | bash -
  apt-get install -y nodejs

  if ! command -v node >/dev/null 2>&1; then
    fail "Node.js installation failed."
  fi

  current_major="$(node -p 'Number(process.versions.node.split(".")[0])')"
  if (( current_major < NODE_MAJOR_REQUIRED )); then
    fail "Node.js $(node --version) is too old. Need ${NODE_MAJOR_REQUIRED}.x or newer."
  fi

  log "Node.js $(node --version) installed."
}

install_miniconda_if_needed() {
  if [[ -x "$MINICONDA_ROOT/bin/python" ]]; then
    log "Miniconda already exists at $MINICONDA_ROOT."
    log "Using Miniconda Python: $("$MINICONDA_ROOT/bin/python" --version)"
    chmod -R a+rX "$MINICONDA_ROOT" || true
    return 0
  fi

  local arch installer_url installer_path
  arch="$(uname -m)"
  case "$arch" in
    x86_64|amd64)
      installer_url="https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh"
      ;;
    aarch64|arm64)
      installer_url="https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-aarch64.sh"
      ;;
    *)
      fail "Unsupported CPU architecture for automatic Miniconda install: $arch"
      ;;
  esac

  log "Installing Miniconda to $MINICONDA_ROOT."
  mkdir -p "$MINICONDA_ROOT"
  installer_path="$MINICONDA_ROOT/miniconda.sh"
  wget "$installer_url" -O "$installer_path"
  bash "$installer_path" -b -u -p "$MINICONDA_ROOT"
  rm -rf "$installer_path"

  if [[ ! -x "$MINICONDA_ROOT/bin/python" ]]; then
    fail "Miniconda Python was not found at $MINICONDA_ROOT/bin/python after installation."
  fi

  chmod -R a+rX "$MINICONDA_ROOT" || true
  log "Miniconda installed."
  log "Using Miniconda Python: $("$MINICONDA_ROOT/bin/python" --version)"
}

check_miniconda_python_version() {
  "$MINICONDA_ROOT/bin/python" - <<'PY'
import sys
if sys.version_info < (3, 10):
    raise SystemExit(f"Miniconda Python is too old: {sys.version.split()[0]}. Need Python 3.10+.")
print(f"Miniconda Python OK: {sys.version.split()[0]}")
PY
}

ensure_users_and_groups() {
  if ! getent group "$GROUP" >/dev/null; then
    groupadd --system "$GROUP"
  fi

  if ! id "$BOT_USER" >/dev/null 2>&1; then
    useradd --system --create-home --shell /bin/bash --gid "$GROUP" "$BOT_USER"
  else
    usermod -aG "$GROUP" "$BOT_USER"
  fi

  if ! id "$CODEX_USER" >/dev/null 2>&1; then
    useradd --system --create-home --shell /bin/bash --gid "$GROUP" "$CODEX_USER"
  else
    usermod -aG "$GROUP" "$CODEX_USER"
  fi

  mkdir -p "/home/$CODEX_USER/.codex"
  chown -R "$CODEX_USER:$GROUP" "/home/$CODEX_USER/.codex"
  chmod 0750 "/home/$CODEX_USER" || true
}

copy_repo() {
  mkdir -p "$INSTALL_DIR" "$DATA_ROOT"
  log "Copying repository from $REPO_ROOT to $INSTALL_DIR."

  local src_real dst_real
  src_real="$(readlink -f "$REPO_ROOT")"
  dst_real="$(readlink -f "$INSTALL_DIR" 2>/dev/null || echo "$INSTALL_DIR")"

  if [[ "$src_real" == "$dst_real" ]]; then
    log "Source and install directory are the same; skipping rsync."
  else
    rsync -a --delete \
      --exclude .git \
      --exclude .venv \
      --exclude .env \
      --exclude .mypy_cache \
      --exclude __pycache__ \
      ./ "$INSTALL_DIR"/
  fi

  chown -R "$BOT_USER:$GROUP" "$INSTALL_DIR" "$DATA_ROOT"
  chmod 2770 "$DATA_ROOT"
  chmod 2750 "$INSTALL_DIR"
}

create_or_update_bot_venv() {
  log "Creating bot virtual environment at $INSTALL_DIR/.venv."
  rm -rf "$INSTALL_DIR/.venv"

  if ! "$MINICONDA_ROOT/bin/python" -m venv --copies "$INSTALL_DIR/.venv"; then
    log "venv --copies failed; retrying with normal venv."
    "$MINICONDA_ROOT/bin/python" -m venv "$INSTALL_DIR/.venv"
  fi

  "$INSTALL_DIR/.venv/bin/python" -m pip install --upgrade pip wheel
  "$INSTALL_DIR/.venv/bin/python" -m pip install -r "$INSTALL_DIR/requirements.txt"

  "$INSTALL_DIR/.venv/bin/python" - <<'PY'
import sys
import telegram
print(f"Bot venv Python OK: {sys.version.split()[0]}")
print(f"python-telegram-bot OK: {telegram.__version__}")
PY

  "$INSTALL_DIR/.venv/bin/python" -m compileall -q "$INSTALL_DIR/autoresearch"
  chown -R "$BOT_USER:$GROUP" "$INSTALL_DIR/.venv"
  chmod -R u+rwX,g+rX,o-rwx "$INSTALL_DIR/.venv"
  chmod 0750 "$INSTALL_DIR/.venv/bin/python"* || true
}

install_codex_cli() {
  log "Installing/updating Codex CLI."
  npm install -g @openai/codex@latest

  CODEX_BIN="$(command -v codex || true)"
  if [[ -z "$CODEX_BIN" ]]; then
    fail "Codex binary was not found after npm install."
  fi
  log "Using Codex: $CODEX_BIN"
}

write_sudoers_rule() {
  cat > /etc/sudoers.d/autoresearch-codex <<EOF_SUDOERS
$BOT_USER ALL=($CODEX_USER) NOPASSWD: /usr/bin/env, $CODEX_BIN
EOF_SUDOERS
  chmod 0440 /etc/sudoers.d/autoresearch-codex
  visudo -cf /etc/sudoers.d/autoresearch-codex
}

set_or_append_env() {
  local file="$1"
  local key="$2"
  local value="$3"
  if grep -qE "^${key}=" "$file"; then
    sed -i "s#^${key}=.*#${key}=${value}#" "$file"
  else
    printf "\n%s=%s\n" "$key" "$value" >> "$file"
  fi
}

write_env_if_missing() {
  if [[ ! -f "$INSTALL_DIR/.env.example" ]]; then
    fail "$INSTALL_DIR/.env.example is missing. The repository is incomplete."
  fi

  if [[ ! -f "$INSTALL_DIR/.env" ]]; then
    cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
  else
    log "$INSTALL_DIR/.env already exists; preserving token/settings where possible."
  fi

  set_or_append_env "$INSTALL_DIR/.env" "DATA_ROOT" "$DATA_ROOT"
  set_or_append_env "$INSTALL_DIR/.env" "CODEX_BIN" "$CODEX_BIN"
  set_or_append_env "$INSTALL_DIR/.env" "CODEX_USER" "$CODEX_USER"
  set_or_append_env "$INSTALL_DIR/.env" "CODEX_HOME" "/home/$CODEX_USER/.codex"
  grep -qE '^DEFAULT_MODEL=' "$INSTALL_DIR/.env" || echo 'DEFAULT_MODEL=gpt-5.5' >> "$INSTALL_DIR/.env"
  grep -qE '^AVAILABLE_MODELS=' "$INSTALL_DIR/.env" || echo 'AVAILABLE_MODELS=gpt-5.5,gpt-5.4,gpt-5.3-codex' >> "$INSTALL_DIR/.env"
  grep -qE '^DEFAULT_REASONING_EFFORT=' "$INSTALL_DIR/.env" || echo 'DEFAULT_REASONING_EFFORT=standard' >> "$INSTALL_DIR/.env"
  grep -qE '^DEFAULT_MAX_RESULT_MESSAGES=' "$INSTALL_DIR/.env" || echo 'DEFAULT_MAX_RESULT_MESSAGES=50' >> "$INSTALL_DIR/.env"
  grep -qE '^DEFAULT_CONTEXT_MESSAGE_LIMIT=' "$INSTALL_DIR/.env" || echo 'DEFAULT_CONTEXT_MESSAGE_LIMIT=30' >> "$INSTALL_DIR/.env"
  grep -qE '^SEND_CONTEXT_MODE=' "$INSTALL_DIR/.env" || echo 'SEND_CONTEXT_MODE=file' >> "$INSTALL_DIR/.env"
  if grep -qE '^PASSTHROUGH_ENV_NAMES=' "$INSTALL_DIR/.env"; then
    if ! grep -qE '^PASSTHROUGH_ENV_NAMES=.*KAGGLE_API_TOKEN' "$INSTALL_DIR/.env"; then
      sed -i 's#^PASSTHROUGH_ENV_NAMES=.*#&,KAGGLE_API_TOKEN#' "$INSTALL_DIR/.env"
    fi
  else
    echo 'PASSTHROUGH_ENV_NAMES=KAGGLE_API_TOKEN' >> "$INSTALL_DIR/.env"
  fi
  grep -qE '^CODEX_SANDBOX=' "$INSTALL_DIR/.env" || echo 'CODEX_SANDBOX=workspace-write' >> "$INSTALL_DIR/.env"
  grep -qE '^CODEX_APPROVAL_POLICY=' "$INSTALL_DIR/.env" || echo 'CODEX_APPROVAL_POLICY=never' >> "$INSTALL_DIR/.env"
  grep -qE '^CODEX_ENABLE_LIVE_SEARCH=' "$INSTALL_DIR/.env" || echo 'CODEX_ENABLE_LIVE_SEARCH=true' >> "$INSTALL_DIR/.env"
  grep -qE '^CODEX_ENABLE_NETWORK=' "$INSTALL_DIR/.env" || echo 'CODEX_ENABLE_NETWORK=true' >> "$INSTALL_DIR/.env"
  grep -qE '^AUTO_START_DRAFT_ON_MESSAGE=' "$INSTALL_DIR/.env" || echo 'AUTO_START_DRAFT_ON_MESSAGE=true' >> "$INSTALL_DIR/.env"

  chown "$BOT_USER:$GROUP" "$INSTALL_DIR/.env"
  chmod 0640 "$INSTALL_DIR/.env"
}

write_systemd_service() {
  cp "$INSTALL_DIR/systemd/autoresearch.service" "/etc/systemd/system/${SERVICE_NAME}.service"
  systemctl daemon-reload
}

verify_service_python_executes_as_bot_user() {
  log "Verifying service Python is executable by $BOT_USER."
  sudo -u "$BOT_USER" "$INSTALL_DIR/.venv/bin/python" - <<'PY'
import sys
print("Service-user Python execution OK:", sys.version.split()[0])
PY
}

repair_existing_task_permissions() {
  if [[ ! -d "$DATA_ROOT/tasks" ]]; then
    return 0
  fi

  log "Repairing existing task permissions for $BOT_USER/$CODEX_USER shared access."
  chown -R "$BOT_USER:$GROUP" "$DATA_ROOT" || true
  chmod 2770 "$DATA_ROOT" "$DATA_ROOT/tasks" || true

  local task workspace system_dir
  for task in "$DATA_ROOT"/tasks/*; do
    [[ -d "$task" ]] || continue
    workspace="$task/workspace"
    system_dir="$task/_system"

    chmod 2750 "$task" || true
    [[ -f "$task/AGENTS.md" ]] && chmod 0640 "$task/AGENTS.md" || true

    if [[ -d "$system_dir" ]]; then
      chmod 2750 "$system_dir" || true
      find "$system_dir" -type d -exec chmod 0750 {} + 2>/dev/null || true
      find "$system_dir" -type f -exec chmod 0640 {} + 2>/dev/null || true
      if [[ -d "$system_dir/credentials" ]]; then
        chmod 0700 "$system_dir/credentials" || true
        find "$system_dir/credentials" -type f -exec chmod 0600 {} + 2>/dev/null || true
      fi
    fi

    if [[ -d "$workspace" ]]; then
      chmod 2770 "$workspace" || true
      find "$workspace" -type d -exec chmod 2770 {} + 2>/dev/null || true
      find "$workspace" -type f -exec chmod 0660 {} + 2>/dev/null || true
    fi
  done
}

stop_old_services
install_base_packages
ensure_node
install_miniconda_if_needed
check_miniconda_python_version
ensure_users_and_groups
copy_repo
create_or_update_bot_venv
install_codex_cli
write_sudoers_rule
write_env_if_missing
repair_existing_task_permissions
write_systemd_service
verify_service_python_executes_as_bot_user

cat <<NEXT

Installed app name: $APP_NAME
Service name:       ${SERVICE_NAME}.service
Installed to:       $INSTALL_DIR
Data root:          $DATA_ROOT
Bot Python:         $INSTALL_DIR/.venv/bin/python
Miniconda:          $MINICONDA_ROOT
Node:               $(node --version)
npm:                $(npm --version)
Codex binary:       $CODEX_BIN

Next steps:
1. Edit the env file:

   sudo nano $INSTALL_DIR/.env

2. Set at least:

   TELEGRAM_BOT_TOKEN=your_bot_token_here
   TELEGRAM_ALLOWED_USER_ID=your_numeric_telegram_user_id
   DEFAULT_MODEL=gpt-5.5
   AVAILABLE_MODELS=gpt-5.5,gpt-5.4,gpt-5.3-codex
   DEFAULT_REASONING_EFFORT=standard
   DEFAULT_MAX_RESULT_MESSAGES=50
   DEFAULT_CONTEXT_MESSAGE_LIMIT=30
   SEND_CONTEXT_MODE=file
   KAGGLE_API_TOKEN=your_kaggle_token_if_needed
   PASSTHROUGH_ENV_NAMES=KAGGLE_API_TOKEN

3. Log in Codex for the isolated user:

   sudo -iu $CODEX_USER codex login --device-auth
   sudo -iu $CODEX_USER codex login status

4. Start the service:

   sudo systemctl enable --now ${SERVICE_NAME}.service
   sudo journalctl -u ${SERVICE_NAME}.service -f

NEXT
