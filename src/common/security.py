"""Path traversal protection (realpath validation)."""
from pathlib import Path


class PathTraversalError(Exception):
    pass


def validate_path_within_root(path: Path, root: Path) -> Path:
    """
    Resolve *path* and verify it stays inside *root*.

    Raises PathTraversalError if the resolved path escapes the root.
    Returns the resolved absolute Path on success.
    """
    resolved_root = root.resolve()
    resolved_path = path.resolve()

    if not str(resolved_path).startswith(str(resolved_root)):
        raise PathTraversalError(
            f"Path '{path}' escapes root directory '{root}'"
        )

    return resolved_path
