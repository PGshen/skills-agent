"""Tool executors: read_file, list_dir, grep, run_script."""
import logging
import os
import re
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from skills.metadata import ResourceLimits

logger = logging.getLogger(__name__)

# Output truncation threshold (characters)
MAX_OUTPUT_CHARS = 10_000

# Minimum environment variable whitelist passed to child processes
_ENV_WHITELIST = {"PATH", "HOME", "USER", "LANG", "LC_ALL"}

# Global registry of semaphores keyed by max_concurrent value
_semaphores: dict[int, threading.Semaphore] = {}
_semaphores_lock = threading.Lock()


def _get_semaphore(max_concurrent: int) -> threading.Semaphore:
    """Return (or create) the shared Semaphore for a given concurrency limit."""
    with _semaphores_lock:
        if max_concurrent not in _semaphores:
            _semaphores[max_concurrent] = threading.Semaphore(max_concurrent)
        return _semaphores[max_concurrent]


@dataclass
class ScriptResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


class ScriptExecutor:
    """
    Executes scripts in a controlled subprocess.

    Constraints enforced:
    - Timeout via subprocess timeout parameter (hard kill on expiry)
    - Concurrency limit via threading.Semaphore (queues excess callers)
    - Environment variable whitelist (strips PYTHONPATH etc.)
    - Output truncation at MAX_OUTPUT_CHARS

    Best-effort only (logged but not enforced):
    - max_memory_mb  (macOS lacks RLIMIT_AS)
    - allow_network  (cannot block DNS/TCP at Python level)
    """

    def execute(
        self,
        script_path: str,
        args: list[str],
        cwd: str,
        limits: ResourceLimits,
        env_overrides: Optional[dict] = None,
    ) -> ScriptResult:
        """
        Run *script_path* with *args* inside *cwd*, subject to *limits*.

        *env_overrides* is merged on top of the whitelist env after cleaning.
        """
        # Best-effort warnings for unenforced limits
        if limits.max_memory_mb is not None:
            logger.warning(
                "max_memory_mb=%d is best-effort only on this platform; "
                "not enforced by OS.",
                limits.max_memory_mb,
            )
        if not limits.allow_network:
            logger.warning(
                "allow_network=False is best-effort only; "
                "network access cannot be blocked at the Python process level."
            )

        # Build clean environment
        clean_env = {k: v for k, v in os.environ.items() if k in _ENV_WHITELIST}
        if env_overrides:
            clean_env.update(env_overrides)

        cmd = [str(script_path)] + [str(a) for a in args]
        timeout = limits.max_script_time_sec
        max_concurrent = limits.max_concurrent_scripts

        semaphore = _get_semaphore(max_concurrent)

        # Block here if concurrency limit is reached (queuing callers)
        with semaphore:
            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    cwd=cwd,
                    env=clean_env,
                    # shell=False (default) — prevents shell injection
                )
                return ScriptResult(
                    returncode=proc.returncode,
                    stdout=proc.stdout[:MAX_OUTPUT_CHARS],
                    stderr=proc.stderr[:MAX_OUTPUT_CHARS],
                    timed_out=False,
                )
            except subprocess.TimeoutExpired:
                return ScriptResult(
                    returncode=-1,
                    stdout="",
                    stderr=f"Script timed out after {timeout}s",
                    timed_out=True,
                )


# ---------------------------------------------------------------------------
# Read-only tool executors
# ---------------------------------------------------------------------------

class ReadFileExecutor:
    """Read a file and return its content (truncated at max_bytes)."""

    def run(self, path: Path, max_bytes: int = 100_000) -> dict:
        try:
            content = path.read_bytes()[:max_bytes].decode("utf-8", errors="replace")
            return {"content": content, "truncated": len(path.read_bytes()) > max_bytes}
        except FileNotFoundError:
            return {"error": f"File not found: {path}"}
        except PermissionError:
            return {"error": f"Permission denied: {path}"}
        except Exception as exc:
            return {"error": str(exc)}


class ListDirExecutor:
    """List a directory's immediate children."""

    def run(self, path: Path, max_entries: int = 100) -> dict:
        try:
            entries = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name))
            names = [
                e.name + ("/" if e.is_dir() else "")
                for e in entries[:max_entries]
            ]
            return {"entries": names, "truncated": len(list(path.iterdir())) > max_entries}
        except FileNotFoundError:
            return {"error": f"Directory not found: {path}"}
        except NotADirectoryError:
            return {"error": f"Not a directory: {path}"}
        except Exception as exc:
            return {"error": str(exc)}


class GrepExecutor:
    """Search for a regex pattern under a root path."""

    def run(self, pattern: str, root: Path, max_results: int = 50) -> dict:
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            return {"error": f"Invalid regex pattern: {exc}"}

        matches: list[dict] = []
        try:
            for fpath in root.rglob("*"):
                if not fpath.is_file():
                    continue
                try:
                    for lineno, line in enumerate(
                        fpath.read_text(errors="replace").splitlines(), start=1
                    ):
                        if compiled.search(line):
                            matches.append({
                                "file": str(fpath),
                                "line": lineno,
                                "content": line,
                            })
                            if len(matches) >= max_results:
                                return {"matches": matches, "truncated": True}
                except (PermissionError, OSError):
                    continue
        except Exception as exc:
            return {"error": str(exc)}

        return {"matches": matches, "truncated": False}
