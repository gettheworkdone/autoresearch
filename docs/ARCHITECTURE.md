# Architecture

`autoresearch` is a Telegram-controlled Codex CLI loop.

## Main folders

```text
/var/lib/autoresearch/
├── state.json
└── tasks/
    └── <task-id>/
        ├── _system/
        └── workspace/
```

`_system/` is bot-owned protected state. `workspace/` is the only writable research sandbox for Codex.

## Protected state

```text
_system/messages/           Telegram messages
_system/files/              original uploaded files and metadata
_system/logs/               Codex stdout/stderr/results/metadata
_system/credentials/        task credentials
_system/custom_skills/      user skills
_system/next_prompt/        next-turn prompt
_system/context_snapshots/  exact compiled prompt sent to Codex
_system/constitution/       task constitution
_system/goal/               durable goal
_system/exports/            core-data export ZIPs
```

## Context compilation

Before each Codex turn, the bot creates `_system/context_snapshots/<N>_context.md`.

Included:

- task metadata
- constitution
- goal
- next prompt
- latest N Telegram messages
- upload metadata
- recent result logs
- required behavior rules

Not included:

- deleted messages
- deleted upload metadata
- the full workspace file listing
- Telegram messages beyond the per-task context limit
- credential values

## Output-to-input loop

Codex must return:

```text
<AUTORESEARCH_RESULT>...</AUTORESEARCH_RESULT>
<NEXT_PROMPT>...</NEXT_PROMPT>
<STOP_REASON>continue</STOP_REASON>
```

The bot saves `AUTORESEARCH_RESULT` into logs, writes `NEXT_PROMPT` into `_system/next_prompt/next_prompt.txt`, then starts the next turn unless paused/stopped/blocked.

## Task deletion

The bot deletes both:

- task folder under `/var/lib/autoresearch/tasks/<task-id>`
- task entry from `/var/lib/autoresearch/state.json`

Use the bot button or `/deletetask`. Manual shell deletion can leave stale state.

## Core-data export

`/exportdata` creates a ZIP with:

- uploaded files
- constitution
- goal
- credentials
- custom skills

It excludes messages, logs, context snapshots, and workspace.
