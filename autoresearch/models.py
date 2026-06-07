from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class TaskStatus(str, Enum):
    DRAFT = "draft"
    RUNNING = "running"
    PAUSED = "paused"
    PAUSED_LIMIT = "paused_limit"
    STOPPED = "stopped"
    DONE = "done"
    ERROR = "error"


@dataclass
class TaskState:
    task_id: str
    name: str
    slug: str
    root: str
    model: str
    status: str = TaskStatus.DRAFT.value
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    max_result_messages: int = 50
    result_messages_sent: int = 0
    iteration: int = 0
    pending_model: Optional[str] = None
    reasoning_effort: str = "standard"
    pending_reasoning_effort: Optional[str] = None
    context_message_limit: int = 30
    clear_context_requested: bool = False
    resume_codex_session: bool = False
    last_stop_reason: Optional[str] = None
    last_commit: Optional[str] = None
    notes: Dict[str, Any] = field(default_factory=dict)

    @property
    def root_path(self) -> Path:
        return Path(self.root)

    def touch(self) -> None:
        self.updated_at = utc_now_iso()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "name": self.name,
            "slug": self.slug,
            "root": self.root,
            "model": self.model,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "max_result_messages": self.max_result_messages,
            "result_messages_sent": self.result_messages_sent,
            "iteration": self.iteration,
            "pending_model": self.pending_model,
            "reasoning_effort": self.reasoning_effort,
            "pending_reasoning_effort": self.pending_reasoning_effort,
            "context_message_limit": self.context_message_limit,
            "clear_context_requested": self.clear_context_requested,
            "resume_codex_session": self.resume_codex_session,
            "last_stop_reason": self.last_stop_reason,
            "last_commit": self.last_commit,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TaskState":
        return cls(
            task_id=data["task_id"],
            name=data["name"],
            slug=data["slug"],
            root=data["root"],
            model=data.get("model", "gpt-5.5"),
            status=data.get("status", TaskStatus.DRAFT.value),
            created_at=data.get("created_at", utc_now_iso()),
            updated_at=data.get("updated_at", utc_now_iso()),
            max_result_messages=int(data.get("max_result_messages", 50)),
            result_messages_sent=int(data.get("result_messages_sent", 0)),
            iteration=int(data.get("iteration", 0)),
            pending_model=data.get("pending_model"),
            reasoning_effort=data.get("reasoning_effort", "standard"),
            pending_reasoning_effort=data.get("pending_reasoning_effort"),
            context_message_limit=int(data.get("context_message_limit", 30)),
            clear_context_requested=bool(data.get("clear_context_requested", False)),
            resume_codex_session=bool(data.get("resume_codex_session", False)),
            last_stop_reason=data.get("last_stop_reason"),
            last_commit=data.get("last_commit"),
            notes=dict(data.get("notes", {})),
        )


@dataclass
class GlobalState:
    selected_task_id: Optional[str] = None
    tasks: Dict[str, TaskState] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "selected_task_id": self.selected_task_id,
            "tasks": {k: v.to_dict() for k, v in self.tasks.items()},
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GlobalState":
        return cls(
            selected_task_id=data.get("selected_task_id"),
            tasks={k: TaskState.from_dict(v) for k, v in data.get("tasks", {}).items()},
        )


@dataclass
class CodexRunResult:
    ok: bool
    iteration: int
    returncode: int
    stdout_path: Path
    stderr_path: Path
    final_message_path: Path
    context_path: Path
    elapsed_seconds: float
    result_text: str
    next_prompt: str
    stop_reason: str
    commit_hash: Optional[str] = None
    error: Optional[str] = None
    command: List[str] = field(default_factory=list)
