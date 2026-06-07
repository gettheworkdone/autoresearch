# autoresearch

`autoresearch` is a single-user Telegram bot that runs autonomous Codex CLI research loops on an Ubuntu VPS.

The active names are:

```text
repo folder:     autoresearch
Python package:  autoresearch
systemd service: autoresearch.service
install path:    /opt/autoresearch
data path:       /var/lib/autoresearch
run command:     python -m autoresearch
```

## What the bot does

A task/task has two main areas:

```text
/var/lib/autoresearch/tasks/<task-id>/
├── _system/       # protected bot-owned state
└── workspace/     # Codex writable sandbox
```

Codex is launched from `workspace/`, not the task root. The bot builds a fresh context snapshot before every turn from the protected files, recent active messages, recent active upload metadata, and previous turn results. A long workspace file listing is intentionally not inserted into prompts anymore; Codex should inspect the filesystem itself when needed.

## Install/update on Ubuntu VPS

From the repo root:

```bash
sudo systemctl stop autoresearch.service 2>/dev/null || true
sudo bash scripts/install_ubuntu.sh
sudo nano /opt/autoresearch/.env
sudo systemctl enable --now autoresearch.service
sudo journalctl -u autoresearch.service -f
```

Check service:

```bash
sudo systemctl status autoresearch.service
sudo journalctl -u autoresearch.service -f
```

Stop/restart:

```bash
sudo systemctl stop autoresearch.service
sudo systemctl restart autoresearch.service
```

## Minimal `.env`

```env
TELEGRAM_BOT_TOKEN=your_botfather_token
TELEGRAM_ALLOWED_USER_ID=your_numeric_telegram_user_id

DATA_ROOT=/var/lib/autoresearch
CODEX_BIN=/usr/bin/codex
CODEX_USER=codexrun
CODEX_HOME=/home/codexrun/.codex

DEFAULT_MODEL=gpt-5.5
AVAILABLE_MODELS=gpt-5.5,gpt-5.4,gpt-5.3-codex
DEFAULT_REASONING_EFFORT=standard
DEFAULT_MAX_RESULT_MESSAGES=50
DEFAULT_CONTEXT_MESSAGE_LIMIT=30
SEND_CONTEXT_MODE=file

CODEX_SANDBOX=workspace-write
CODEX_APPROVAL_POLICY=never
CODEX_ENABLE_LIVE_SEARCH=true
CODEX_ENABLE_NETWORK=true
RESUME_CODEX_SESSION=false

MAX_UPLOAD_MB=200
MAX_ZIP_TOTAL_MB=1000
MAX_ZIP_FILES=5000
LOOP_DELAY_SECONDS=2
AUTO_START_DRAFT_ON_MESSAGE=true
CODEX_TIMEOUT_SECONDS=7200

# Modern Kaggle token auth. Use token, not legacy username/key.
KAGGLE_API_TOKEN=KGAT_your_token_here
PASSTHROUGH_ENV_NAMES=KAGGLE_API_TOKEN
```

After changing `.env`:

```bash
sudo chown researchbot:aragent /opt/autoresearch/.env
sudo chmod 0640 /opt/autoresearch/.env
sudo systemctl restart autoresearch.service
```

## Codex login

```bash
sudo -iu codexrun codex login --device-auth
sudo -iu codexrun codex login status
```

## Telegram setup from scratch

1. Open Telegram and message `@BotFather`.
2. Run `/newbot`.
3. Copy the bot token into `/opt/autoresearch/.env` as `TELEGRAM_BOT_TOKEN`.
4. Get your numeric Telegram user ID from `@userinfobot`.
5. Put it into `.env` as `TELEGRAM_ALLOWED_USER_ID`.
6. Restart the service.

The bot registers Telegram slash commands automatically. If command popups do not appear when typing `/`, restart Telegram or reopen the chat.

## Creating a task

In Telegram:

```text
/start
/newtask
```

The wizard asks for:

1. task name
2. goal
3. credentials, optional
4. model
5. thinking/reasoning level
6. result-message batch limit
7. Telegram-message context limit
8. files and initial instruction

Important setup behavior:

```text
You may upload as many supplementary files as you want first.
Files and captions are saved, but iterations do not start from files/captions.
The autonomous loop starts only after you send one normal text message with the initial instruction.
```

## Core Telegram commands

```text
/menu                         show buttons
/help                         command list
/newtask                      create task
/tasks                        list/select/delete tasks
/deletetask NAME_OR_ID        delete an entire task folder from VPS
/status                       current task/model/reasoning/iteration/folder size
/folder                       task root and workspace path
/resume                       start/resume selected task
/pause                        pause after current Codex turn
/stop                         stop current Codex turn and loop
/continue                     continue after automatic batch-limit pause
/clearcontext                 rebuild next context from task files
```

## Task deletion

Use buttons:

```text
/tasks → tap 🗑 beside task → confirm YES, DELETE
```

Or command:

```text
/deletetask neurogolf-05071d04
/deletetask NeuroGolf
```

This removes the task from `state.json` and deletes its folder under `/var/lib/autoresearch/tasks/`.

## Export core task data

Use:

```text
/exportdata
```

or the `📦 Export` button.

The bot sends a ZIP containing:

```text
uploaded_files/
constitution/
goal/
credentials/
custom_skills/
MANIFEST.json
```

It deliberately excludes:

```text
Telegram messages
logs
context snapshots
workspace/sandbox files
```

## Models and thinking/reasoning

List configured model buttons:

```text
/models
```

Set model:

```text
/setmodel gpt-5.5
```

List thinking options:

```text
/reasoning
```

Set thinking:

```text
/setreasoning light
/setreasoning standard
/setreasoning heavy
/setreasoning extra
```

If the task is running, model/reasoning changes are queued and applied after the current turn.

`AVAILABLE_MODELS` in `.env` controls what appears as Telegram buttons. Use exact model IDs exposed by your Codex account.

## Context controls

Set how many latest active Telegram messages are inserted into the model context:

```text
/contextmessages
/contextmessages 30
/contextmessages 0
```

Manage individual messages:

```text
/messages
```

Tap `🗑` beside a message to permanently delete that message file from the VPS and future model context.

Manage upload metadata:

```text
/uploads
```

Tap `🗑` beside uploaded metadata to delete that metadata from future context. The uploaded file remains stored and mirrored in the workspace.

## Skills

Add skill from text:

```text
/addskill skill-name instructions...
```

Upload a Markdown file with `skill` in the filename to install it as a custom skill.

List/delete skills:

```text
/skills
```

## Goal, constitution, credentials

View/edit goal:

```text
/goal
/setgoal
```

View/edit constitution:

```text
/constitution
/setconstitution
```

When editing, send the full replacement text. Old versions are backed up.

View stored credential names safely:

```text
/credentials
```

Values are hidden in chat. Use `/exportdata` if you need the protected credential file back as part of the ZIP.

Add/update a task credential:

```text
/addcred KAGGLE_API_TOKEN=KGAT_...
```

## Kaggle token auth

Modern Kaggle token auth uses `KAGGLE_API_TOKEN`.

Recommended `.env`:

```env
KAGGLE_API_TOKEN=KGAT_your_token_here
PASSTHROUGH_ENV_NAMES=KAGGLE_API_TOKEN
```

Optional task-specific credential:

```text
/addcred KAGGLE_API_TOKEN=KGAT_your_token_here
```

Test from VPS:

```bash
sudo -iu codexrun bash -lc '
TOKEN="$(cat "$HOME/.kaggle/access_token" 2>/dev/null | tr -d "\r\n")"
env KAGGLE_API_TOKEN="$TOKEN" /opt/autoresearch/.venv/bin/kaggle competitions list -s titanic
'
```

Or with `.env`:

```bash
set -a
source /opt/autoresearch/.env
set +a
/opt/autoresearch/.venv/bin/python - <<'PY'
import os
from kaggle.api.kaggle_api_extended import KaggleApi
print("token present:", bool(os.getenv("KAGGLE_API_TOKEN")))
api = KaggleApi(); api.authenticate()
print(api.competitions_list(search="titanic")[:1])
PY
```

## Inspecting a running task on VPS

Current task workspace:

```bash
cd /var/lib/autoresearch/tasks/<task-id>/workspace
```

Logs:

```bash
ls -lah /var/lib/autoresearch/tasks/<task-id>/_system/logs
```

Latest context snapshot:

```bash
LATEST=$(ls -t /var/lib/autoresearch/tasks/<task-id>/_system/context_snapshots/*.md | head -1)
less "$LATEST"
```

Folder size:

```bash
du -sh /var/lib/autoresearch/tasks/<task-id>
```

Largest files excluding dot-directories:

```bash
find . -type f \( -path '*/.*' -prune -o -print0 \) | xargs -0 du -h | sort -hr | head -30
```

## Disk cleanup

Systemd journal:

```bash
journalctl --disk-usage
sudo journalctl --vacuum-size=200M
```

Autoresearch task sizes:

```bash
sudo du -sh /var/lib/autoresearch/tasks/*
```

Delete a task from Telegram with `/tasks` → `🗑`, or from shell only after stopping the service:

```bash
sudo systemctl stop autoresearch.service
sudo rm -rf /var/lib/autoresearch/tasks/<task-id>
sudo systemctl start autoresearch.service
```

If you delete manually, also remove stale task metadata from `/var/lib/autoresearch/state.json`, or use the bot delete button instead.
