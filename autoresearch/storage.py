from __future__ import annotations

import json
import os
import shutil
import time
import uuid
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .config import Settings
from .models import GlobalState, TaskState, TaskStatus, utc_now_iso
from .utils import (
    atomic_write_text,
    directory_size_bytes,
    ensure_dir,
    human_size,
    safe_extract_zip,
    safe_filename,
    sha256_file,
    slugify,
)

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_ROOT = PACKAGE_ROOT / "templates"

SYSTEM_DIRS = [
    "messages",
    "files",
    "logs",
    "credentials",
    "custom_skills",
    "next_prompt",
    "context_snapshots",
    "constitution",
    "goal",
    "control",
    "checksums",
    "exports",
]


def _note_list(task: TaskState, key: str) -> list[str]:
    value = task.notes.get(key, [])
    if isinstance(value, list):
        return [str(x) for x in value]
    return []


class Storage:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.root = settings.data_root
        self.tasks_root = self.root / "tasks"
        self.state_path = self.root / "state.json"
        ensure_dir(self.root, 0o2770)
        ensure_dir(self.tasks_root, 0o2770)
        if not self.state_path.exists():
            self.save_state(GlobalState())

    def load_state(self) -> GlobalState:
        if not self.state_path.exists():
            return GlobalState()
        try:
            return GlobalState.from_dict(json.loads(self.state_path.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            backup = self.state_path.with_suffix(f".broken-{int(time.time())}.json")
            shutil.copy2(self.state_path, backup)
            return GlobalState()

    def save_state(self, state: GlobalState) -> None:
        ensure_dir(self.state_path.parent, 0o2770)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.state_path)
        try:
            os.chmod(self.state_path, 0o660)
        except PermissionError:
            pass

    def get_task(self, task_id: str) -> Optional[TaskState]:
        return self.load_state().tasks.get(task_id)

    def selected_task(self) -> Optional[TaskState]:
        state = self.load_state()
        if not state.selected_task_id:
            return None
        return state.tasks.get(state.selected_task_id)

    def upsert_task(self, task: TaskState, select: bool = False) -> None:
        state = self.load_state()
        task.touch()
        state.tasks[task.task_id] = task
        if select:
            state.selected_task_id = task.task_id
        self.save_state(state)
        self.save_task_control(task)

    def select_task(self, task_id: str) -> Optional[TaskState]:
        state = self.load_state()
        task = state.tasks.get(task_id)
        if not task:
            return None
        state.selected_task_id = task_id
        self.save_state(state)
        return task

    def list_tasks(self) -> List[TaskState]:
        return sorted(self.load_state().tasks.values(), key=lambda t: t.updated_at, reverse=True)

    def delete_task(self, task_id: str) -> bool:
        state = self.load_state()
        task = state.tasks.pop(task_id, None)
        if not task:
            return False
        if state.selected_task_id == task_id:
            state.selected_task_id = None
        self.save_state(state)
        shutil.rmtree(task.root_path, ignore_errors=True)
        return True

    def create_task(
        self,
        name: str,
        goal: str,
        model: str,
        max_result_messages: int,
        resume_codex_session: bool,
        initial_message: str | None = None,
        credentials: Optional[Dict[str, str]] = None,
        reasoning_effort: str = "standard",
        context_message_limit: int = 30,
    ) -> TaskState:
        slug = slugify(name)
        task_id = f"{slug}-{uuid.uuid4().hex[:8]}"
        task_root = self.tasks_root / task_id
        system_root = task_root / "_system"
        workspace = task_root / "workspace"

        ensure_dir(task_root, 0o2750)
        ensure_dir(system_root, 0o2750)
        for dirname in SYSTEM_DIRS:
            mode = 0o700 if dirname == "credentials" else 0o750
            ensure_dir(system_root / dirname, mode)
        ensure_dir(workspace, 0o2770)
        ensure_dir(workspace / "incoming", 0o2770)
        ensure_dir(workspace / "tools", 0o2770)
        ensure_dir(workspace / ".autoresearch_codex_output", 0o2770)
        ensure_dir(workspace / ".agents" / "skills" / "autoresearch-default", 0o2770)
        ensure_dir(workspace / ".conda" / "envs", 0o2770)
        ensure_dir(workspace / ".conda" / "pkgs", 0o2770)
        ensure_dir(workspace / "tmp", 0o2770)

        shutil.copy2(TEMPLATE_ROOT / "constitution.md", system_root / "constitution" / "CONSTITUTION.md")
        os.chmod(system_root / "constitution" / "CONSTITUTION.md", 0o640)
        atomic_write_text(system_root / "goal" / "GOAL.md", goal.strip() + "\n", 0o640)
        atomic_write_text(system_root / "next_prompt" / "next_prompt.txt", initial_message or goal, 0o640)

        shutil.copy2(TEMPLATE_ROOT / "AGENTS.md", task_root / "AGENTS.md")
        os.chmod(task_root / "AGENTS.md", 0o640)
        shutil.copy2(
            TEMPLATE_ROOT / "skill_template.md",
            workspace / ".agents" / "skills" / "autoresearch-default" / "SKILL.md",
        )
        os.chmod(workspace / ".agents" / "skills" / "autoresearch-default" / "SKILL.md", 0o660)
        shutil.copy2(TEMPLATE_ROOT / "task_local_miniconda.sh", workspace / "tools" / "install_miniconda_task_local.sh")
        os.chmod(workspace / "tools" / "install_miniconda_task_local.sh", 0o770)

        if credentials:
            self.write_credentials_env(system_root / "credentials" / "credentials.env", credentials)
        else:
            atomic_write_text(
                system_root / "credentials" / "credentials.env",
                "# Add credentials through Telegram /addcred KEY=value. Values are not shown to Codex prompts.\n",
                0o600,
            )

        task = TaskState(
            task_id=task_id,
            name=name.strip(),
            slug=slug,
            root=str(task_root),
            model=model,
            status=TaskStatus.DRAFT.value,
            max_result_messages=max_result_messages,
            resume_codex_session=resume_codex_session,
            reasoning_effort=reasoning_effort,
            context_message_limit=context_message_limit,
        )
        self.upsert_task(task, select=True)
        if initial_message:
            task.notes["initial_instruction_received"] = True
            self.upsert_task(task, select=True)
            self.add_user_message(task, initial_message, source="initial")
        self.init_workspace_git(task)
        self.ensure_workspace_group_writable(task)
        self.record_checksum_snapshot(task)
        return task

    def task_paths(self, task: TaskState) -> Dict[str, Path]:
        root = task.root_path
        system_root = root / "_system"
        return {
            "root": root,
            "system": system_root,
            "workspace": root / "workspace",
            "messages": system_root / "messages",
            "files": system_root / "files",
            "logs": system_root / "logs",
            "credentials": system_root / "credentials",
            "custom_skills": system_root / "custom_skills",
            "next_prompt": system_root / "next_prompt" / "next_prompt.txt",
            "context_snapshots": system_root / "context_snapshots",
            "constitution": system_root / "constitution" / "CONSTITUTION.md",
            "goal": system_root / "goal" / "GOAL.md",
            "control": system_root / "control",
            "checksums": system_root / "checksums",
            "exports": system_root / "exports",
        }

    def save_task_control(self, task: TaskState) -> None:
        paths = self.task_paths(task)
        ensure_dir(paths["control"], 0o750)
        atomic_write_text(paths["control"] / "state.json", json.dumps(task.to_dict(), indent=2), 0o640)

    def add_user_message(self, task: TaskState, text: str, source: str = "telegram") -> Path:
        paths = self.task_paths(task)
        existing = sorted(paths["messages"].glob("*.txt"))
        max_idx = int(task.notes.get("message_counter", 0) or 0)
        for old in existing:
            try:
                max_idx = max(max_idx, int(old.name.split("_", 1)[0]))
            except ValueError:
                continue
        idx = max_idx + 1
        task.notes["message_counter"] = idx
        timestamp = utc_now_iso().replace(":", "-")
        path = paths["messages"] / f"{idx:06d}_{timestamp}_{safe_filename(source)}.txt"
        content = f"timestamp_utc: {utc_now_iso()}\nsource: {source}\n\n{text.strip()}\n"
        atomic_write_text(path, content, 0o640)
        self.upsert_task(task, select=False)
        self.record_checksum_snapshot(task)
        return path

    def list_context_messages(self, task: TaskState) -> list[Path]:
        return self.list_all_messages(task)

    def list_all_messages(self, task: TaskState) -> list[Path]:
        return sorted(self.task_paths(task)["messages"].glob("*.txt"))

    def delete_message_permanently(self, task: TaskState, prefix: str) -> Optional[str]:
        """Delete a Telegram message file from disk and future model context.

        Older releases supported hiding messages through task.notes. That mode is
        intentionally removed: deleting in /messages now removes the file. If the
        deleted message was also copied into next_prompt.txt, replace that prompt
        with a neutral continuation instruction so the message cannot be fed back
        through the next-turn prompt.
        """
        files = self.list_all_messages(task)
        match = next((p for p in files if p.name.startswith(prefix)), None)
        if not match:
            return None

        name = match.name
        text = match.read_text(encoding="utf-8", errors="replace") if match.exists() else ""
        body = text.split("\n\n", 1)[1].strip() if "\n\n" in text else text.strip()
        match.unlink(missing_ok=True)

        # Remove legacy context-exclusion references if this task was created
        # with an older version of autoresearch.
        for key in ("excluded_message_files", "deleted_message_files"):
            values = [v for v in _note_list(task, key) if v != name]
            if values:
                task.notes[key] = values
            else:
                task.notes.pop(key, None)

        paths = self.task_paths(task)
        next_prompt = paths["next_prompt"]
        if body and next_prompt.exists():
            current = next_prompt.read_text(encoding="utf-8", errors="replace")
            if body in current:
                atomic_write_text(
                    next_prompt,
                    "Continue toward the current goal. Re-read active task state, remaining messages, uploads, and recent results before acting.\n",
                    0o640,
                )

        self.upsert_task(task, select=True)
        self.record_checksum_snapshot(task)
        return name

    def list_upload_metadata(self, task: TaskState, include_excluded: bool = True) -> list[Path]:
        # `include_excluded` is kept for backward compatibility. Metadata is now
        # actually deleted by the bot when the user taps the delete button.
        return sorted(self.task_paths(task)["files"].glob("*.metadata.json"))

    def exclude_upload_metadata_from_context(self, task: TaskState, prefix: str) -> Optional[str]:
        """Delete an upload metadata JSON file without deleting the upload itself."""
        files = self.list_upload_metadata(task, include_excluded=True)
        match = next((p for p in files if p.name.startswith(prefix)), None)
        if not match:
            return None
        name = match.name
        match.unlink(missing_ok=True)
        deleted = set(_note_list(task, "deleted_upload_metadata"))
        deleted.add(name)
        task.notes["deleted_upload_metadata"] = sorted(deleted)
        self.upsert_task(task, select=True)
        self.record_checksum_snapshot(task)
        return name

    def add_custom_skill(self, task: TaskState, name: str, content: str) -> Path:
        paths = self.task_paths(task)
        filename = safe_filename(name)
        if not filename.lower().endswith(".md"):
            filename += ".md"
        path = paths["custom_skills"] / filename
        atomic_write_text(path, content, 0o640)
        self.mirror_custom_skills(task)
        self.record_checksum_snapshot(task)
        return path

    def list_custom_skills(self, task: TaskState) -> list[Path]:
        return sorted(self.task_paths(task)["custom_skills"].glob("*.md"))

    def delete_custom_skill(self, task: TaskState, prefix_or_name: str) -> Optional[str]:
        paths = self.task_paths(task)
        skills = self.list_custom_skills(task)
        match = next((p for p in skills if p.name.startswith(prefix_or_name) or p.stem == prefix_or_name), None)
        if not match:
            return None
        name = match.name
        match.unlink(missing_ok=True)
        mirrored = paths["workspace"] / ".agents" / "skills" / slugify(Path(name).stem)
        shutil.rmtree(mirrored, ignore_errors=True)
        self.mirror_custom_skills(task)
        self.record_checksum_snapshot(task)
        return name

    def mirror_custom_skills(self, task: TaskState) -> None:
        paths = self.task_paths(task)
        skills_root = paths["workspace"] / ".agents" / "skills"
        ensure_dir(skills_root, 0o2770)
        for md in paths["custom_skills"].glob("*.md"):
            skill_name = slugify(md.stem)
            dest_dir = skills_root / skill_name
            ensure_dir(dest_dir, 0o2770)
            text = md.read_text(encoding="utf-8", errors="replace")
            if not text.lstrip().startswith("---"):
                text = (
                    f"---\nname: {skill_name}\n"
                    f"description: Custom user skill named {skill_name}.\n---\n\n{text}"
                )
            atomic_write_text(dest_dir / "SKILL.md", text, 0o660)

    def add_uploaded_file(
        self,
        task: TaskState,
        src_path: Path,
        original_name: str,
        max_zip_total_mb: int,
        max_zip_files: int,
    ) -> Tuple[Path, List[Path], str]:
        paths = self.task_paths(task)
        existing = sorted(paths["files"].glob("*"))
        idx = len([p for p in existing if p.is_file() and not p.name.endswith(".json")]) + 1
        safe = safe_filename(original_name)
        dest = paths["files"] / f"{idx:06d}_{safe}"
        shutil.copy2(src_path, dest)
        os.chmod(dest, 0o640)
        digest = sha256_file(dest)
        metadata = {
            "timestamp_utc": utc_now_iso(),
            "original_name": original_name,
            "stored_name": dest.name,
            "sha256": digest,
            "size_bytes": dest.stat().st_size,
        }
        atomic_write_text(dest.with_suffix(dest.suffix + ".metadata.json"), json.dumps(metadata, indent=2), 0o640)

        incoming = paths["workspace"] / "incoming" / dest.stem
        ensure_dir(incoming, 0o2770)
        workspace_copy = incoming / safe
        shutil.copy2(dest, workspace_copy)
        os.chmod(workspace_copy, 0o660)
        created = [workspace_copy]
        note = f"Stored upload as {dest.name} ({digest[:12]})"

        if safe.lower().endswith(".zip"):
            extract_dir = incoming / "extracted"
            count, total = safe_extract_zip(workspace_copy, extract_dir, max_zip_total_mb, max_zip_files)
            created.append(extract_dir)
            note += f"; extracted {count} files, {human_size(total)} uncompressed"

        self.record_checksum_snapshot(task)
        return dest, created, note

    def write_credentials_env(self, path: Path, credentials: Dict[str, str]) -> None:
        lines = ["# Stored by autoresearch. Do not print values in model outputs.\n"]
        for key, value in credentials.items():
            clean_key = key.strip()
            if not clean_key:
                continue
            escaped = value.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'{clean_key}="{escaped}"\n')
        atomic_write_text(path, "".join(lines), 0o600)

    def read_credentials(self, task: TaskState) -> Dict[str, str]:
        paths = self.task_paths(task)
        env_path = paths["credentials"] / "credentials.env"
        result: Dict[str, str] = {}
        if not env_path.exists():
            return result
        for raw in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] == '"':
                value = value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
            result[key.strip()] = value
        return result

    def credential_summary(self, task: TaskState) -> str:
        creds = self.read_credentials(task)
        if not creds:
            return "No task credentials stored."
        lines = ["Stored task credentials (values hidden):"]
        for key, value in sorted(creds.items()):
            prefix = value[:5] if value else ""
            lines.append(f"- {key}: set, length {len(value)}, prefix {prefix!r}...")
        return "\n".join(lines)

    def add_credential(self, task: TaskState, key: str, value: str) -> None:
        creds = self.read_credentials(task)
        creds[key] = value
        paths = self.task_paths(task)
        self.write_credentials_env(paths["credentials"] / "credentials.env", creds)
        self.record_checksum_snapshot(task)

    def write_next_prompt(self, task: TaskState, prompt: str) -> None:
        paths = self.task_paths(task)
        atomic_write_text(paths["next_prompt"], prompt.strip() + "\n", 0o640)
        self.record_checksum_snapshot(task)

    def read_next_prompt(self, task: TaskState) -> str:
        path = self.task_paths(task)["next_prompt"]
        if not path.exists():
            return "Continue toward the goal."
        return path.read_text(encoding="utf-8", errors="replace").strip()

    def update_goal(self, task: TaskState, text: str) -> Path:
        paths = self.task_paths(task)
        backup_dir = paths["goal"] .parent / "backups"
        ensure_dir(backup_dir, 0o750)
        if paths["goal"].exists():
            shutil.copy2(paths["goal"], backup_dir / f"GOAL_{int(time.time())}.md")
        atomic_write_text(paths["goal"], text.strip() + "\n", 0o640)
        self.record_checksum_snapshot(task)
        return paths["goal"]

    def update_constitution(self, task: TaskState, text: str) -> Path:
        paths = self.task_paths(task)
        backup_dir = paths["constitution"].parent / "backups"
        ensure_dir(backup_dir, 0o750)
        if paths["constitution"].exists():
            shutil.copy2(paths["constitution"], backup_dir / f"CONSTITUTION_{int(time.time())}.md")
        atomic_write_text(paths["constitution"], text.strip() + "\n", 0o640)
        self.record_checksum_snapshot(task)
        return paths["constitution"]

    def create_data_export_zip(self, task: TaskState) -> Path:
        """Create a ZIP with core protected data, excluding messages/logs/workspace."""
        paths = self.task_paths(task)
        ensure_dir(paths["exports"], 0o750)
        out = paths["exports"] / f"{task.slug}_core_data_{int(time.time())}.zip"
        include_roots = [
            (paths["files"], "uploaded_files"),
            (paths["constitution"].parent, "constitution"),
            (paths["goal"].parent, "goal"),
            (paths["credentials"], "credentials"),
            (paths["custom_skills"], "custom_skills"),
        ]
        with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            manifest = {
                "task_id": task.task_id,
                "task_name": task.name,
                "created_utc": utc_now_iso(),
                "contains": ["uploaded_files", "constitution", "goal", "credentials", "custom_skills"],
                "excluded": ["telegram messages", "logs", "context snapshots", "workspace/sandbox"],
            }
            zf.writestr("MANIFEST.json", json.dumps(manifest, indent=2))
            for source, arc_root in include_roots:
                if source.is_file():
                    zf.write(source, f"{arc_root}/{source.name}")
                    continue
                if not source.exists():
                    continue
                for p in sorted(source.rglob("*")):
                    if p.is_file():
                        zf.write(p, f"{arc_root}/{p.relative_to(source)}")
        os.chmod(out, 0o640)
        return out

    def folder_size_human(self, task: TaskState) -> str:
        return human_size(directory_size_bytes(task.root_path))

    def ensure_workspace_group_writable(self, task: TaskState) -> None:
        workspace = self.task_paths(task)["workspace"]
        if not workspace.exists():
            return
        for p in [workspace, *workspace.rglob("*")]:
            try:
                if p.is_dir():
                    os.chmod(p, 0o2770)
                elif p.is_file():
                    os.chmod(p, 0o660)
            except OSError:
                continue

    def init_workspace_git(self, task: TaskState) -> None:
        from .utils import run_quiet

        workspace = self.task_paths(task)["workspace"]
        if (workspace / ".git").exists():
            return
        run_quiet(["git", "init"], cwd=workspace, timeout=30)
        run_quiet(["git", "config", "user.email", "autoresearch@localhost"], cwd=workspace)
        run_quiet(["git", "config", "user.name", "Autoresearch"], cwd=workspace)
        readme = workspace / "README_WORKSPACE.md"
        if not readme.exists():
            atomic_write_text(
                readme,
                "# Workspace\n\nCodex may freely create, edit, and delete files in this folder.\n",
                0o660,
            )
        run_quiet(["git", "add", "-A"], cwd=workspace, timeout=30)
        run_quiet(["git", "commit", "-m", "checkpoint: initialize workspace"], cwd=workspace, timeout=30)
        self.ensure_workspace_group_writable(task)

    def commit_workspace(self, task: TaskState, message: str) -> Optional[str]:
        from .utils import run_quiet

        workspace = self.task_paths(task)["workspace"]
        run_quiet(["git", "add", "-A"], cwd=workspace, timeout=120)
        status = run_quiet(["git", "status", "--porcelain"], cwd=workspace, timeout=30)
        if not status.stdout.strip():
            return None
        commit = run_quiet(["git", "commit", "-m", message], cwd=workspace, timeout=120)
        if commit.returncode != 0:
            return None
        rev = run_quiet(["git", "rev-parse", "--short", "HEAD"], cwd=workspace, timeout=30)
        return rev.stdout.strip() or None

    def record_checksum_snapshot(self, task: TaskState) -> None:
        paths = self.task_paths(task)
        protected_roots = [paths["system"], task.root_path / "AGENTS.md"]
        rows = [f"# Protected-file checksum snapshot at {utc_now_iso()}\n"]
        for root in protected_roots:
            if root.is_file():
                try:
                    rows.append(f"{sha256_file(root)}  {root.relative_to(task.root_path)}\n")
                except OSError:
                    continue
            elif root.exists():
                for p in sorted(root.rglob("*")):
                    if p.is_file() and "checksums" not in p.parts:
                        try:
                            rows.append(f"{sha256_file(p)}  {p.relative_to(task.root_path)}\n")
                        except OSError:
                            continue
        atomic_write_text(paths["checksums"] / "protected.sha256", "".join(rows), 0o640)
