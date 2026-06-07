from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import zipfile
from pathlib import Path, PurePosixPath
from typing import Iterable, List, Tuple

SLUG_RE = re.compile(r"[^a-zA-Z0-9._-]+")


def slugify(value: str, max_len: int = 64) -> str:
    value = value.strip().replace(" ", "-")
    value = SLUG_RE.sub("-", value)
    value = value.strip("-._").lower()
    if not value:
        value = "task"
    return value[:max_len].strip("-._") or "task"


def ensure_dir(path: Path, mode: int = 0o750) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, mode)
    except PermissionError:
        pass


def atomic_write_text(path: Path, text: str, mode: int = 0o640) -> None:
    ensure_dir(path.parent)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    try:
        os.chmod(path, mode)
    except PermissionError:
        pass


def append_text(path: Path, text: str, mode: int = 0o640) -> None:
    ensure_dir(path.parent)
    with path.open("a", encoding="utf-8") as f:
        f.write(text)
    try:
        os.chmod(path, mode)
    except PermissionError:
        pass


def safe_filename(name: str) -> str:
    name = name.strip().replace("\\", "/").split("/")[-1]
    name = re.sub(r"[^a-zA-Z0-9._ -]+", "_", name).strip(" .")
    return name or "file"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def directory_size_bytes(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for p in path.rglob("*"):
        try:
            if p.is_file() or p.is_symlink():
                total += p.lstat().st_size
        except OSError:
            continue
    return total


def human_size(num: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(num)
    for unit in units:
        if size < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{num} B"


def safe_extract_zip(zip_path: Path, dest_dir: Path, max_total_mb: int, max_files: int) -> Tuple[int, int]:
    """Safely extract a ZIP file while blocking traversal, absolute paths, and symlinks."""
    ensure_dir(dest_dir, 0o770)
    max_total = max_total_mb * 1024 * 1024
    total = 0
    count = 0
    dest_resolved = dest_dir.resolve()

    with zipfile.ZipFile(zip_path) as zf:
        infos = zf.infolist()
        if len(infos) > max_files:
            raise ValueError(f"ZIP contains {len(infos)} files, limit is {max_files}")

        for info in infos:
            raw_name = info.filename.replace("\\", "/")
            pure = PurePosixPath(raw_name)
            if pure.is_absolute() or ".." in pure.parts:
                raise ValueError(f"Unsafe ZIP path: {info.filename!r}")

            file_type = (info.external_attr >> 16) & 0o170000
            if file_type == 0o120000:
                raise ValueError(f"Refusing to extract ZIP symlink: {info.filename!r}")

            total += info.file_size
            if total > max_total:
                raise ValueError(f"ZIP uncompressed size exceeds {max_total_mb} MB limit")

            target = (dest_dir / Path(*pure.parts)).resolve()
            if not str(target).startswith(str(dest_resolved) + os.sep) and target != dest_resolved:
                raise ValueError(f"Unsafe ZIP target: {target}")

            if info.is_dir():
                ensure_dir(target, 0o770)
            else:
                ensure_dir(target.parent, 0o770)
                with zf.open(info) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
                try:
                    os.chmod(target, 0o660)
                except PermissionError:
                    pass
                count += 1

    return count, total


def run_quiet(args: List[str], cwd: Path | None = None, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def split_telegram_text(text: str, limit: int = 3800) -> List[str]:
    if len(text) <= limit:
        return [text]
    parts: List[str] = []
    current: List[str] = []
    current_len = 0
    for line in text.splitlines(keepends=True):
        if current_len + len(line) > limit and current:
            parts.append("".join(current))
            current = []
            current_len = 0
        if len(line) > limit:
            if current:
                parts.append("".join(current))
                current = []
                current_len = 0
            for i in range(0, len(line), limit):
                parts.append(line[i : i + limit])
            continue
        current.append(line)
        current_len += len(line)
    if current:
        parts.append("".join(current))
    return parts or [""]


def text_between(text: str, start_tag: str, end_tag: str) -> str | None:
    pattern = re.compile(re.escape(start_tag) + r"(.*?)" + re.escape(end_tag), re.S)
    match = pattern.search(text)
    if not match:
        return None
    return match.group(1).strip()


def list_recent_files(path: Path, max_items: int = 80) -> str:
    rows = []
    if not path.exists():
        return "(missing)"
    items = []
    for p in path.rglob("*"):
        if p.is_file():
            try:
                items.append((p.stat().st_mtime, p))
            except OSError:
                continue
    for _, p in sorted(items, reverse=True)[:max_items]:
        try:
            rel = p.relative_to(path)
        except ValueError:
            rel = p
        rows.append(f"- {rel} ({human_size(p.stat().st_size)})")
    return "\n".join(rows) if rows else "(no files)"


def redact_env_values(text: str, secret_keys: Iterable[str]) -> str:
    result = text
    for key in secret_keys:
        key = key.strip()
        if not key:
            continue
        result = re.sub(rf"({re.escape(key)}\s*=\s*)[^\s]+", rf"\1<redacted>", result)
    return result
