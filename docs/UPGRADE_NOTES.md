# Upgrade notes

This version adds bot-level management features requested during testing.

## New controls

- Delete tasks by button or `/deletetask NAME_OR_ID`.
- Export core task data with `/exportdata`.
- List/set configured models with `/models` and `/setmodel`.
- List/set thinking levels with `/reasoning` and `/setreasoning light|standard|heavy|extra`.
- Set per-task Telegram-message context count with `/contextmessages` or `/contextmessages N`.
- Permanently delete individual Telegram messages from the VPS and future model context with `/messages` buttons.
- Delete upload metadata from future model context with `/uploads` buttons; uploaded files remain saved.
- Delete custom skills with `/skills` buttons.
- View/edit goal and constitution with `/goal`, `/setgoal`, `/constitution`, `/setconstitution`.
- View masked credential names with `/credentials`.

## Context changes

The prompt no longer includes a long workspace file listing. Codex must inspect the workspace directly.

Only the latest N active Telegram messages are inserted into the model context. N is configured by:

```env
DEFAULT_CONTEXT_MESSAGE_LIMIT=30
```

and can be changed per task with `/contextmessages`.

## Kaggle auth change

The `.env.example` now uses modern token auth:

```env
KAGGLE_API_TOKEN=KGAT_...
PASSTHROUGH_ENV_NAMES=KAGGLE_API_TOKEN
```

Only `KAGGLE_API_TOKEN` is used in the default Kaggle setup.
