from __future__ import annotations

import asyncio
import getpass
import json
import os
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .models import CodexRunResult, TaskState
from .storage import Storage
from .utils import redact_env_values, text_between

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
REASONING_TO_CODEX = {
    "light": "low",
    "standard": "medium",
    "heavy": "high",
    "extra": "xhigh",
}


class CodexRunner:
    def __init__(self, settings, storage: Storage):
        self.settings = settings
        self.storage = storage
        self.current_process: Optional[asyncio.subprocess.Process] = None

    def build_context_prompt(self, task: TaskState) -> Tuple[str, Path]:
        paths = self.storage.task_paths(task)
        task.iteration += 1
        iteration = task.iteration
        next_prompt = self.storage.read_next_prompt(task)
        credentials = self.storage.read_credentials(task)
        # Include passthrough env names in the available-name list without values.
        credential_names = sorted(set(credentials.keys()) | {n for n in self.settings.passthrough_env_names if os.getenv(n)})

        def read(path: Path, default: str = "") -> str:
            if path.exists():
                return path.read_text(encoding="utf-8", errors="replace")
            return default

        messages = []
        message_files = self.storage.list_context_messages(task)
        limit = max(0, task.context_message_limit)
        selected_messages = message_files[-limit:] if limit else []
        for p in selected_messages:
            messages.append(f"--- {p.name} ---\n{read(p)}")

        previous_results = []
        for p in sorted(paths["logs"].glob("*_result.md"))[-12:]:
            previous_results.append(f"--- {p.name} ---\n{read(p)}")

        upload_meta = []
        for p in self.storage.list_upload_metadata(task, include_excluded=False)[-80:]:
            upload_meta.append(f"--- {p.name} ---\n{read(p)}")

        prompt = f"""
You are running one autonomous turn of autoresearch, a Telegram-controlled Codex research agent.

Task id: {task.task_id}
Task name: {task.name}
Iteration: {iteration}
Current model: {task.model}
Current reasoning effort: {task.reasoning_effort}
Telegram messages included in this context: {len(selected_messages)}/{len(message_files)} active messages, limit={limit}
Clear-context requested before this turn: {task.clear_context_requested}

Runtime layout:
- Codex is launched with current working directory set to the task workspace.
- The only writable directory is the current directory: workspace/.
- Protected _system/ files are embedded below by the Telegram bot. Treat them as authoritative context.
- Uploaded files are mirrored into workspace/incoming/. Use workspace/incoming/ for inspection and experiments.
- Do not attempt to create, modify, chmod, move, or delete files outside the current workspace.
- Do not attempt to create .git outside the current workspace.

Credential environment variables available by name only: {', '.join(credential_names) if credential_names else '(none)'}
Do not print credential values.

# Constitution
{read(paths['constitution'], '(missing constitution)')}

# Goal
{read(paths['goal'], '(missing goal)')}

# Planned next prompt
{next_prompt}

# Latest active Telegram messages
{chr(10).join(messages) if messages else '(no Telegram messages included; limit may be 0 or messages may be disabled)'}

# Recent active upload metadata
{chr(10).join(upload_meta) if upload_meta else '(no upload metadata included)'}

# Previous turn results
{chr(10).join(previous_results) if previous_results else '(no previous turn results yet)'}

# Required behavior for this turn
1. Treat this prompt as the already re-read protected context.
2. Inspect the current directory and workspace/incoming/ as needed.
3. Incorporate included active user messages and uploaded files before acting.
4. Work only in the current directory, which is workspace/. You may create, modify, and delete files there.
5. Never write outside the current workspace. Do not touch ../_system, ../AGENTS.md, or ../.git.
6. Use internet/web search when useful if the Codex runtime exposes it.
7. If you need Python packages, use task-local environments under .venv, .conda, or .miniconda3 inside the current workspace.
8. If you need conda and it is not available, you may run tools/install_miniconda_task_local.sh and use .miniconda3.
9. Do not return until commands, tests, downloads, or experiments you started have finished, timed out, or failed with a clear reason.
10. End with the exact AUTORESEARCH_RESULT, NEXT_PROMPT, and STOP_REASON blocks from the constitution.
""".strip()

        context_path = paths["context_snapshots"] / f"{iteration:06d}_context.md"
        prompt = redact_env_values(prompt, credential_names)
        context_path.write_text(prompt + "\n", encoding="utf-8")
        try:
            os.chmod(context_path, 0o640)
        except PermissionError:
            pass
        return prompt, context_path

    def collect_image_attachments(self, task: TaskState, max_images: int = 8) -> List[Path]:
        paths = self.storage.task_paths(task)
        images: List[Path] = []
        for p in sorted(paths["files"].glob("*"), reverse=True):
            if p.is_file() and p.suffix.lower() in IMAGE_EXTS and not p.name.endswith(".json"):
                images.append(p)
            if len(images) >= max_images:
                break
        return list(reversed(images))

    def build_env(self, task: TaskState) -> Dict[str, str]:
        paths = self.storage.task_paths(task)
        credentials = self.storage.read_credentials(task)
        run_user = self.settings.codex_user or getpass.getuser()
        run_home = Path(f"/home/{run_user}") if self.settings.codex_user else Path.home()

        env: Dict[str, str] = {
            "PATH": os.getenv("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": str(run_home),
            "USER": run_user,
            "LOGNAME": run_user,
            "LANG": os.getenv("LANG", "C.UTF-8"),
            "LC_ALL": os.getenv("LC_ALL", "C.UTF-8"),
            "CODEX_HOME": str(self.settings.codex_home),
            "TMPDIR": str(paths["workspace"] / "tmp"),
            "PIP_CACHE_DIR": str(paths["workspace"] / ".cache" / "pip"),
            "PIPX_HOME": str(paths["workspace"] / ".pipx"),
            "PIPX_BIN_DIR": str(paths["workspace"] / ".pipx" / "bin"),
            "CONDA_ENVS_PATH": str(paths["workspace"] / ".conda" / "envs"),
            "CONDA_PKGS_DIRS": str(paths["workspace"] / ".conda" / "pkgs"),
            "PYTHONUNBUFFERED": "1",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "safe.directory",
            "GIT_CONFIG_VALUE_0": str(paths["workspace"]),
        }

        # Global .env passthroughs, e.g. KAGGLE_API_TOKEN. Task credentials override these.
        for name in self.settings.passthrough_env_names:
            value = os.getenv(name)
            if value:
                env[name] = value
        env.update(credentials)
        return env

    def _command_with_env(self, env: Dict[str, str], codex_args: List[str]) -> Tuple[List[str], Optional[Dict[str, str]]]:
        codex_bin = self.settings.codex_bin
        current_user = getpass.getuser()

        if self.settings.codex_user and current_user != self.settings.codex_user:
            env_pairs = [f"{k}={v}" for k, v in env.items()]
            cmd = ["sudo", "-n", "-u", self.settings.codex_user, "env", "-i", *env_pairs, codex_bin, *codex_args]
            return cmd, None
        return [codex_bin, *codex_args], env

    def build_codex_args(
        self,
        task: TaskState,
        final_message_path: Path,
        image_paths: List[Path],
        include_live_search_config: bool = True,
        include_reasoning_config: bool = True,
    ) -> List[str]:
        paths = self.storage.task_paths(task)
        args = [
            "exec",
            "--cd",
            str(paths["workspace"]),
            "--model",
            task.model,
            "--sandbox",
            self.settings.codex_sandbox,
            "--skip-git-repo-check",
            "--json",
            "--output-last-message",
            str(final_message_path),
            "-c",
            f'approval_policy="{self.settings.codex_approval_policy}"',
            "-c",
            f'sandbox_mode="{self.settings.codex_sandbox}"',
            "-c",
            "sandbox_workspace_write.exclude_slash_tmp=true",
            "-c",
            "sandbox_workspace_write.exclude_tmpdir_env_var=false",
            "-c",
            'shell_environment_policy.inherit="all"',
            "-c",
            "shell_environment_policy.ignore_default_excludes=true",
        ]

        if self.settings.codex_enable_network:
            args += ["-c", "sandbox_workspace_write.network_access=true"]
        if self.settings.codex_enable_live_search and include_live_search_config:
            args += ["-c", 'web_search="live"']
        if include_reasoning_config:
            effort = REASONING_TO_CODEX.get(task.reasoning_effort, "medium")
            args += ["-c", f'model_reasoning_effort="{effort}"']

        for image in image_paths:
            args += ["--image", str(image)]
        args += ["-"]
        return args

    async def run_once(self, task: TaskState) -> CodexRunResult:
        start = time.monotonic()
        paths = self.storage.task_paths(task)
        self.storage.mirror_custom_skills(task)
        prompt, context_path = self.build_context_prompt(task)
        iteration = task.iteration

        output_dir = paths["workspace"] / ".autoresearch_codex_output"
        output_dir.mkdir(parents=True, exist_ok=True)

        final_message_path = output_dir / f"{iteration:06d}_final_message.md"
        stdout_path = paths["logs"] / f"{iteration:06d}_codex_stdout.jsonl"
        stderr_path = paths["logs"] / f"{iteration:06d}_codex_stderr.txt"
        result_path = paths["logs"] / f"{iteration:06d}_result.md"
        metadata_path = paths["logs"] / f"{iteration:06d}_metadata.json"

        image_paths = self.collect_image_attachments(task)
        env = self.build_env(task)
        codex_args = self.build_codex_args(task, final_message_path, image_paths)
        cmd, proc_env = self._command_with_env(env, codex_args)

        result = await self._run_command(
            task=task,
            iteration=iteration,
            prompt=prompt,
            context_path=context_path,
            start=start,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            final_message_path=final_message_path,
            cmd=cmd,
            proc_env=proc_env,
        )

        if self._should_retry_without_optional_config(result, stderr_path):
            retry_stdout = paths["logs"] / f"{iteration:06d}_codex_stdout_retry_optional_config.jsonl"
            retry_stderr = paths["logs"] / f"{iteration:06d}_codex_stderr_retry_optional_config.txt"
            final_message_path.unlink(missing_ok=True)
            codex_args = self.build_codex_args(
                task,
                final_message_path,
                image_paths,
                include_live_search_config=False,
                include_reasoning_config=False,
            )
            cmd, proc_env = self._command_with_env(env, codex_args)
            result = await self._run_command(
                task=task,
                iteration=iteration,
                prompt=prompt,
                context_path=context_path,
                start=start,
                stdout_path=retry_stdout,
                stderr_path=retry_stderr,
                final_message_path=final_message_path,
                cmd=cmd,
                proc_env=proc_env,
                error_prefix="Retried without optional web_search/model_reasoning_effort config after Codex CLI rejected config. ",
            )

        result_path.write_text(result.result_text + "\n", encoding="utf-8")
        try:
            os.chmod(result_path, 0o640)
        except PermissionError:
            pass

        metadata = {
            "iteration": iteration,
            "ok": result.ok,
            "returncode": result.returncode,
            "elapsed_seconds": result.elapsed_seconds,
            "stop_reason": result.stop_reason,
            "context_path": str(context_path),
            "stdout_path": str(result.stdout_path),
            "stderr_path": str(result.stderr_path),
            "final_message_path": str(final_message_path),
            "command_redacted": self.redacted_command(result.command or cmd),
            "image_attachments": [str(p) for p in image_paths],
            "model": task.model,
            "reasoning_effort": task.reasoning_effort,
            "context_message_limit": task.context_message_limit,
        }
        metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        try:
            os.chmod(metadata_path, 0o640)
        except PermissionError:
            pass
        return result

    async def _run_command(
        self,
        task: TaskState,
        iteration: int,
        prompt: str,
        context_path: Path,
        start: float,
        stdout_path: Path,
        stderr_path: Path,
        final_message_path: Path,
        cmd: List[str],
        proc_env: Optional[Dict[str, str]],
        error_prefix: str = "",
    ) -> CodexRunResult:
        paths = self.storage.task_paths(task)
        with stdout_path.open("wb") as stdout_f, stderr_path.open("wb") as stderr_f:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=stdout_f,
                    stderr=stderr_f,
                    env=proc_env,
                    cwd=str(paths["workspace"]),
                )
                self.current_process = proc
                assert proc.stdin is not None
                proc.stdin.write(prompt.encode("utf-8"))
                await proc.stdin.drain()
                proc.stdin.close()

                try:
                    await asyncio.wait_for(proc.wait(), timeout=self.settings.codex_timeout_seconds)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
                    error = f"{error_prefix}Codex timed out after {self.settings.codex_timeout_seconds} seconds"
                    final_message_path.write_text(error + "\n", encoding="utf-8")
                    return self._finish_result(task, iteration, False, -1, stdout_path, stderr_path, final_message_path, context_path, start, cmd, error=error)
            except FileNotFoundError as exc:
                error = f"{error_prefix}Could not start Codex CLI: {exc}"
                stderr_path.write_text(error + "\n", encoding="utf-8")
                final_message_path.write_text(error + "\n", encoding="utf-8")
                return self._finish_result(task, iteration, False, 127, stdout_path, stderr_path, final_message_path, context_path, start, cmd, error=error)
            finally:
                self.current_process = None

        rc = proc.returncode if "proc" in locals() and proc.returncode is not None else 1
        return self._finish_result(
            task,
            iteration,
            rc == 0,
            rc,
            stdout_path,
            stderr_path,
            final_message_path,
            context_path,
            start,
            cmd,
            error=error_prefix.strip() or None,
        )

    def _should_retry_without_optional_config(self, result: CodexRunResult, stderr_path: Path) -> bool:
        if result.ok:
            return False
        if not stderr_path.exists():
            return False
        err = stderr_path.read_text(encoding="utf-8", errors="replace").lower()
        needles = [
            "web_search",
            "model_reasoning_effort",
            "unknown config",
            "invalid config",
            "unexpected argument '--search'",
        ]
        return any(n in err for n in needles)

    def _finish_result(
        self,
        task: TaskState,
        iteration: int,
        ok: bool,
        returncode: int,
        stdout_path: Path,
        stderr_path: Path,
        final_message_path: Path,
        context_path: Path,
        start: float,
        cmd: List[str],
        error: Optional[str] = None,
    ) -> CodexRunResult:
        final_text = ""
        if final_message_path.exists():
            final_text = final_message_path.read_text(encoding="utf-8", errors="replace")
        if not final_text and stderr_path.exists():
            final_text = stderr_path.read_text(encoding="utf-8", errors="replace")[-8000:]

        result_text = text_between(final_text, "<AUTORESEARCH_RESULT>", "</AUTORESEARCH_RESULT>")
        next_prompt = text_between(final_text, "<NEXT_PROMPT>", "</NEXT_PROMPT>")
        stop_reason = text_between(final_text, "<STOP_REASON>", "</STOP_REASON>")

        if not result_text:
            result_text = final_text.strip() or error or "Codex produced no final message."
        if error and error not in result_text:
            result_text = f"{error}\n\n{result_text}".strip()
        if not next_prompt:
            next_prompt = "Continue toward the goal. First inspect the latest logs and workspace state."
        if not stop_reason:
            stop_reason = "blocked" if not ok else "continue"

        stop_reason = stop_reason.strip().split()[0].lower()
        if stop_reason not in {"continue", "goal_done", "blocked", "needs_user"}:
            stop_reason = "continue" if ok else "blocked"

        return CodexRunResult(
            ok=ok,
            iteration=iteration,
            returncode=returncode,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            final_message_path=final_message_path,
            context_path=context_path,
            elapsed_seconds=round(time.monotonic() - start, 2),
            result_text=result_text.strip(),
            next_prompt=next_prompt.strip(),
            stop_reason=stop_reason,
            error=error,
            command=self.redacted_command(cmd),
        )

    def redacted_command(self, cmd: List[str]) -> List[str]:
        redacted = []
        for item in cmd:
            if "=" in item:
                key, _value = item.split("=", 1)
                if any(s in key.upper() for s in ["KEY", "TOKEN", "SECRET", "PASSWORD"]):
                    redacted.append(f"{key}=<redacted>")
                    continue
            redacted.append(item)
        return redacted

    async def cancel_current(self) -> None:
        proc = self.current_process
        if proc and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=10)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
