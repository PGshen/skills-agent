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


# ---------------------------------------------------------------------------
# Write / delete tool executors
# ---------------------------------------------------------------------------

@dataclass
class WriteFileResult:
    success: bool
    error: str = ""


@dataclass
class DeleteFileResult:
    success: bool
    error: str = ""


class WriteFileExecutor:
    """Write a file in one of two modes:

    - Full overwrite: pass ``content`` (creates or replaces the whole file).
    - Patch:          pass ``old_str`` + ``new_str`` (replaces the first and
                      only occurrence of ``old_str``; fails if 0 or >1 matches).

    Exactly one mode must be chosen per call.
    """

    def run(
        self,
        path: Path,
        content: Optional[str] = None,
        *,
        old_str: Optional[str] = None,
        new_str: Optional[str] = None,
    ) -> WriteFileResult:
        if content is not None and old_str is not None:
            return WriteFileResult(
                success=False,
                error="Provide either 'content' (overwrite) or 'old_str'/'new_str' (patch), not both.",
            )
        if content is None and old_str is None:
            return WriteFileResult(
                success=False,
                error="Provide either 'content' (overwrite) or 'old_str'/'new_str' (patch).",
            )

        try:
            if content is not None:
                # ── Full overwrite ────────────────────────────────────────────
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            else:
                # ── Patch ─────────────────────────────────────────────────────
                if new_str is None:
                    return WriteFileResult(
                        success=False, error="'new_str' is required when using patch mode."
                    )
                if not path.exists():
                    return WriteFileResult(
                        success=False, error=f"File not found: {path}"
                    )
                original = path.read_text(encoding="utf-8")
                count = original.count(old_str)  # type: ignore[arg-type]
                if count == 0:
                    return WriteFileResult(
                        success=False,
                        error="'old_str' not found in file — no changes made.",
                    )
                if count > 1:
                    return WriteFileResult(
                        success=False,
                        error=(
                            f"'old_str' found {count} times in file; "
                            "it must match exactly once for a safe patch."
                        ),
                    )
                path.write_text(original.replace(old_str, new_str, 1), encoding="utf-8")  # type: ignore[arg-type]

            return WriteFileResult(success=True)
        except PermissionError as exc:
            return WriteFileResult(success=False, error=f"Permission denied: {exc}")
        except Exception as exc:
            return WriteFileResult(success=False, error=str(exc))


class DeleteFileExecutor:
    """Delete a single file (does not delete directories)."""

    def run(self, path: Path) -> DeleteFileResult:
        try:
            if path.is_dir():
                return DeleteFileResult(success=False, error=f"Path is a directory: {path}")
            path.unlink()
            return DeleteFileResult(success=True)
        except FileNotFoundError:
            return DeleteFileResult(success=False, error=f"File not found: {path}")
        except PermissionError as exc:
            return DeleteFileResult(success=False, error=f"Permission denied: {exc}")
        except Exception as exc:
            return DeleteFileResult(success=False, error=str(exc))


# ---------------------------------------------------------------------------
# Web search executor + adapters
# ---------------------------------------------------------------------------

@dataclass
class SearchResult:
    title: str
    url: str
    content: str


@dataclass
class WebSearchResult:
    results: list
    error: str = ""
    answer: str = ""


class WebSearchAdapter:
    """Base class for web search adapters. Subclass and override search()."""

    def search(self, query: str, max_results: int = 5) -> WebSearchResult:
        raise NotImplementedError


class TavilyAdapter(WebSearchAdapter):
    """Web search adapter backed by the Tavily Search API."""

    _BASE_URL = "https://api.tavily.com/search"

    def __init__(self, api_key: str, max_tokens: int = 6000):
        self._api_key = api_key
        self._max_tokens = max_tokens

    def search(self, query: str, max_results: int = 5) -> WebSearchResult:
        import requests
        try:
            resp = requests.post(
                self._BASE_URL,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={
                    "query": query,
                    "max_results": max_results,
                    "search_depth": "advanced",
                    "include_raw_content": False,
                    "max_tokens": self._max_tokens,
                    "include_answer": True,
                },
                timeout=20,
            )
            resp.raise_for_status()
            data = resp.json()
            results = [
                SearchResult(
                    title=r.get("title", ""),
                    url=r.get("url", ""),
                    content=r.get("content", ""),
                )
                for r in data.get("results", [])
            ]
            return WebSearchResult(results=results, answer=data.get("answer"))
        except Exception as exc:
            return WebSearchResult(results=[], error=str(exc))


class WebSearchExecutor:
    """Delegates web search to a pluggable WebSearchAdapter."""

    def __init__(self, adapter: WebSearchAdapter):
        self._adapter = adapter

    def run(self, query: str, max_results: int = 5) -> WebSearchResult:
        return self._adapter.search(query, max_results=max_results)
