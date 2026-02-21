"""安全工具：路径校验、注入防护"""

from pathlib import Path


class PathTraversalError(Exception):
    """路径穿越错误"""

    pass


def validate_path_in_root(file_path: Path, root_path: Path) -> Path:
    """
    验证文件路径在根目录内，防止路径穿越

    Args:
        file_path: 要验证的文件路径
        root_path: 根目录

    Returns:
        解析后的绝对路径

    Raises:
        PathTraversalError: 路径在根目录外

    Examples:
        >>> root = Path("/app/skills")
        >>> file_path = Path("/app/skills/my-skill/script.py")
        >>> validate_path_in_root(file_path, root)
        PosixPath('/app/skills/my-skill/script.py')

        >>> file_path = Path("/etc/passwd")
        >>> validate_path_in_root(file_path, root)  # doctest: +SKIP
        Traceback (most recent call last):
        PathTraversalError: Path /etc/passwd is outside root /app/skills
    """
    try:
        # 解析为绝对路径
        abs_file = file_path.resolve()
        abs_root = root_path.resolve()

        # 检查是否在根目录内
        abs_file.relative_to(abs_root)

        return abs_file
    except ValueError as e:
        raise PathTraversalError(
            f"Path {file_path} is outside root {root_path}"
        ) from e


def validate_relative_path(relative_path: str) -> bool:
    """
    验证相对路径的安全性

    Args:
        relative_path: 相对路径字符串

    Returns:
        是否安全

    Examples:
        >>> validate_relative_path("scripts/test.py")
        True
        >>> validate_relative_path("reference/doc.md")
        True
        >>> validate_relative_path("../etc/passwd")
        False
        >>> validate_relative_path("/etc/passwd")
        False
    """
    # 禁止绝对路径
    if relative_path.startswith("/"):
        return False

    # 禁止 .. 路径段
    if ".." in Path(relative_path).parts:
        return False

    return True


def sanitize_frontmatter(text: str) -> str:
    """
    净化 YAML 前言，移除危险字符

    Args:
        text: 前言文本

    Returns:
        净化后的文本

    Examples:
        >>> sanitize_frontmatter("name: test")
        'name: test'
        >>> sanitize_frontmatter("name: <script>alert(1)</script>")
        'name: scriptalert(1)/script'
    """
    # 移除尖括号（防止注入）
    text = text.replace("<", "").replace(">", "")
    return text
