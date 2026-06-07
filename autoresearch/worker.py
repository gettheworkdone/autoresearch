from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Dict, Optional

from .config import Settings
from .models import CodexRunResult, TaskState, TaskStatus
from .runner import CodexRunner
from .storage import Storage

SendFunc = Callable[[str, Optional[dict]], Awaitable[None]]
SendFileFunc = Callable[[str, str], Awaitable[None]]


class WorkerManager:
    def __init__(
        self,
        settings: Settings,
        storage: Storage,
        runner: CodexRunner,
        send_text: SendFunc,
        send_file: SendFileFunc,
    ):
        self.settings = settings
        self.storage = storage
        self.runner = runner
        self.send_text = send_text
        self.send_file = send_file
        self.tasks: Dict[str, asyncio.Task] = {}

    def is_running(self, task_id: str) -> bool:
        task = self.tasks.get(task_id)
        return bool(task and not task.done())

    async def start(self, task: TaskState) -> None:
        if self.is_running(task.task_id):
            await self.send_text(f"Task `{task.name}` is already running.", None)
            return
        if task.status == TaskStatus.DRAFT.value and not task.notes.get("initial_instruction_received"):
            await self.send_text(
                "Draft task is not ready yet. Upload any supplementary files you want, then send one normal text message with the initial instruction. Iteration 1 will start after that text message.",
                None,
            )
            return
        task.status = TaskStatus.RUNNING.value
        self.storage.upsert_task(task, select=True)
        self.tasks[task.task_id] = asyncio.create_task(self._loop(task.task_id))
        await self.send_text(f"▶️ Started `{task.name}` with model `{task.model}`.", None)

    async def pause(self, task: TaskState) -> None:
        task.status = TaskStatus.PAUSED.value
        self.storage.upsert_task(task, select=True)
        await self.send_text("⏸ Pause requested. If Codex is running, it will stop after the current turn.", None)

    async def stop(self, task: TaskState) -> None:
        task.status = TaskStatus.STOPPED.value
        self.storage.upsert_task(task, select=True)
        await self.runner.cancel_current()
        active = self.tasks.get(task.task_id)
        if active and not active.done():
            active.cancel()
        await self.send_text("⏹ Stopped. You can resume later with /resume.", None)

    async def continue_after_limit(self, task: TaskState) -> None:
        task.result_messages_sent = 0
        task.status = TaskStatus.RUNNING.value
        self.storage.upsert_task(task, select=True)
        await self.start(task)

    async def _loop(self, task_id: str) -> None:
        while True:
            task = self.storage.get_task(task_id)
            if not task:
                return
            if task.status != TaskStatus.RUNNING.value:
                return
            if task.result_messages_sent >= task.max_result_messages:
                task.status = TaskStatus.PAUSED_LIMIT.value
                self.storage.upsert_task(task, select=True)
                await self.send_text(
                    f"⏸ Auto-paused after {task.max_result_messages} result messages. Use /continue to run another batch or /setlimit N.",
                    {"continue": "Continue", "pause": "Keep paused", "stop": "Stop"},
                )
                return

            await self.send_text(
                f"🔄 Turn {task.iteration + 1} starting. Model: `{task.model}`. Folder size: {self.storage.folder_size_human(task)}.",
                None,
            )

            try:
                result = await self.runner.run_once(task)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                task = self.storage.get_task(task_id) or task
                task.status = TaskStatus.ERROR.value
                task.last_stop_reason = "exception"
                self.storage.upsert_task(task, select=True)
                await self.send_text(f"❌ Worker error: `{exc}`", None)
                return

            task = self.storage.get_task(task_id) or task
            await self._handle_result(task, result)

            task = self.storage.get_task(task_id) or task
            if task.pending_model:
                old = task.model
                task.model = task.pending_model
                task.pending_model = None
                self.storage.upsert_task(task, select=True)
                await self.send_text(f"🔁 Model switched after turn: `{old}` → `{task.model}`.", None)

            task = self.storage.get_task(task_id) or task
            if task.pending_reasoning_effort:
                old = task.reasoning_effort
                task.reasoning_effort = task.pending_reasoning_effort
                task.pending_reasoning_effort = None
                self.storage.upsert_task(task, select=True)
                await self.send_text(f"🧠 Reasoning switched after turn: `{old}` → `{task.reasoning_effort}`.", None)

            task = self.storage.get_task(task_id) or task
            if result.stop_reason == "goal_done":
                task.status = TaskStatus.DONE.value
                self.storage.upsert_task(task, select=True)
                await self.send_text("✅ Goal marked done by Codex. Use /resume if you want more work.", None)
                return
            if result.stop_reason in {"blocked", "needs_user"} or not result.ok:
                task.status = TaskStatus.PAUSED.value
                self.storage.upsert_task(task, select=True)
                await self.send_text(
                    f"⏸ Paused because Codex reported `{result.stop_reason}`. Send instructions/files, then /resume.",
                    None,
                )
                if result.stderr_path.exists() and result.stderr_path.stat().st_size > 0:
                    await self.send_file(str(result.stderr_path), f"Codex stderr for turn {result.iteration}")
                return
            if task.status != TaskStatus.RUNNING.value:
                return
            await asyncio.sleep(self.settings.loop_delay_seconds)

    async def _handle_result(self, task: TaskState, result: CodexRunResult) -> None:
        commit = self.storage.commit_workspace(task, f"checkpoint: codex turn {result.iteration}")
        if commit:
            result.commit_hash = commit
            task.last_commit = commit
        task.iteration = result.iteration
        task.result_messages_sent += 1
        task.last_stop_reason = result.stop_reason
        task.clear_context_requested = False
        self.storage.write_next_prompt(task, result.next_prompt)
        self.storage.upsert_task(task, select=True)

        size = self.storage.folder_size_human(task)
        status = "✅" if result.ok else "⚠️"
        commit_line = f"\nCommit: `{commit}`" if commit else "\nCommit: no workspace changes"
        message = (
            f"{status} Turn {result.iteration} result\n"
            f"Stop reason: `{result.stop_reason}`\n"
            f"Elapsed: {result.elapsed_seconds}s\n"
            f"Folder size: {size}"
            f"{commit_line}\n\n"
            f"{result.result_text}\n\n"
            f"Next prompt saved to `_system/next_prompt/next_prompt.txt`."
        )
        await self.send_text(message, {"pause": "Pause", "stop": "Stop", "status": "Status"})

        if self.settings.send_context_mode == "file":
            await self.send_file(str(result.context_path), f"Context snapshot for turn {result.iteration}")
        elif self.settings.send_context_mode == "summary":
            await self.send_text(
                f"Context snapshot saved: `{result.context_path.name}` ({result.context_path.stat().st_size} bytes).",
                None,
            )
