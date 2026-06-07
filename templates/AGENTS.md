# Autoresearch Project Rules

Autoresearch is controlled by the Telegram bot. The bot embeds the protected constitution, goal, active Telegram messages, active upload metadata, and recent results into the prompt before each Codex turn.

Protected paths:

- `_system/`
- `AGENTS.md`

Never modify protected paths. Work only inside the task workspace. New code, experiments, notebooks, downloads, and reports belong under `workspace/`.

The user can delete old Telegram messages and upload metadata from future context through the bot. Respect the current prompt and inspect files directly when needed.

For modern Kaggle authentication, prefer `KAGGLE_API_TOKEN` if available.
