from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def _str_env(*names: str, default: str) -> str:
    for name in names:
        value = os.getenv(name)
        if value is not None and value.strip() != "":
            return value.strip()
    return default


def _path_env(*names: str, default: str) -> Path:
    return Path(_str_env(*names, default=default)).resolve()


def _csv_env(name: str, default: str = "") -> list[str]:
    raw = os.getenv(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


def _default_codex_bin() -> str:
    return shutil.which("codex") or "/usr/bin/codex"


def normalize_reasoning_label(value: str) -> str:
    """Normalize user-facing reasoning labels.

    We store user-friendly labels in task state. The runner maps them to Codex
    config values.
    """
    v = (value or "").strip().lower().replace("_", "-")
    aliases = {
        "l": "light",
        "low": "light",
        "light": "light",
        "fast": "light",
        "m": "standard",
        "med": "standard",
        "medium": "standard",
        "normal": "standard",
        "std": "standard",
        "standard": "standard",
        "h": "heavy",
        "high": "heavy",
        "heavy": "heavy",
        "x": "extra",
        "xhigh": "extra",
        "extra-high": "extra",
        "extra_high": "extra",
        "extra": "extra",
        "max": "extra",
    }
    return aliases.get(v, v)


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    telegram_allowed_user_id: int
    data_root: Path

    codex_bin: str
    codex_user: str
    codex_home: Path
    default_model: str
    available_models: list[str]
    default_reasoning_effort: str
    codex_timeout_seconds: int
    resume_codex_session: bool
    codex_enable_live_search: bool
    codex_enable_network: bool
    codex_sandbox: str
    codex_approval_policy: str
    passthrough_env_names: list[str]

    send_context_mode: str
    auto_start_draft_on_message: bool
    default_context_message_limit: int

    max_upload_mb: int
    max_zip_total_mb: int
    max_zip_files: int

    default_max_result_messages: int
    loop_delay_seconds: int

    @classmethod
    def load(cls, env_file: Optional[str] = None) -> "Settings":
        if load_dotenv is not None:
            if env_file:
                load_dotenv(env_file)
            else:
                load_dotenv()

        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is required")

        allowed = _int_env("TELEGRAM_ALLOWED_USER_ID", 0)
        if allowed <= 0:
            raise RuntimeError("TELEGRAM_ALLOWED_USER_ID is required and must be numeric")

        send_context_mode = _str_env("SEND_CONTEXT_MODE", default="file").lower()
        if send_context_mode == "message":
            send_context_mode = "summary"
        if send_context_mode not in {"file", "summary", "off"}:
            raise RuntimeError("SEND_CONTEXT_MODE must be one of: file, summary, off")

        codex_sandbox = _str_env("CODEX_SANDBOX", default="workspace-write")
        if codex_sandbox not in {"read-only", "workspace-write", "danger-full-access"}:
            raise RuntimeError("CODEX_SANDBOX must be read-only, workspace-write, or danger-full-access")

        approval_policy = _str_env("CODEX_APPROVAL_POLICY", default="never")
        if approval_policy not in {"untrusted", "on-request", "never"}:
            raise RuntimeError("CODEX_APPROVAL_POLICY must be untrusted, on-request, or never")

        default_model = _str_env("DEFAULT_MODEL", default="gpt-5.5")
        models = _csv_env("AVAILABLE_MODELS", default_model)
        if default_model not in models:
            models.insert(0, default_model)

        reasoning = normalize_reasoning_label(_str_env("DEFAULT_REASONING_EFFORT", default="standard"))
        if reasoning not in {"light", "standard", "heavy", "extra"}:
            raise RuntimeError("DEFAULT_REASONING_EFFORT must be light, standard, heavy, or extra")

        default_context_limit = _int_env("DEFAULT_CONTEXT_MESSAGE_LIMIT", 30)
        if default_context_limit < 0:
            raise RuntimeError("DEFAULT_CONTEXT_MESSAGE_LIMIT must be >= 0")

        passthrough = _csv_env("PASSTHROUGH_ENV_NAMES", "KAGGLE_API_TOKEN")

        return cls(
            telegram_bot_token=token,
            telegram_allowed_user_id=allowed,
            data_root=_path_env("DATA_ROOT", default="/var/lib/autoresearch"),
            codex_bin=_str_env("CODEX_BIN", default=_default_codex_bin()),
            codex_user=_str_env("CODEX_USER", default="codexrun"),
            codex_home=_path_env("CODEX_HOME", default="/home/codexrun/.codex"),
            default_model=default_model,
            available_models=models,
            default_reasoning_effort=reasoning,
            codex_timeout_seconds=_int_env("CODEX_TIMEOUT_SECONDS", 7200),
            resume_codex_session=_bool_env("RESUME_CODEX_SESSION", False),
            codex_enable_live_search=_bool_env("CODEX_ENABLE_LIVE_SEARCH", True),
            codex_enable_network=_bool_env("CODEX_ENABLE_NETWORK", True),
            codex_sandbox=codex_sandbox,
            codex_approval_policy=approval_policy,
            passthrough_env_names=passthrough,
            send_context_mode=send_context_mode,
            auto_start_draft_on_message=_bool_env("AUTO_START_DRAFT_ON_MESSAGE", True),
            default_context_message_limit=default_context_limit,
            max_upload_mb=_int_env("MAX_UPLOAD_MB", 200),
            max_zip_total_mb=_int_env("MAX_ZIP_TOTAL_MB", 1000),
            max_zip_files=_int_env("MAX_ZIP_FILES", 5000),
            default_max_result_messages=_int_env("DEFAULT_MAX_RESULT_MESSAGES", 50),
            loop_delay_seconds=_int_env("LOOP_DELAY_SECONDS", 2),
        )
