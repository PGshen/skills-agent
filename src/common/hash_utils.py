"""哈希与校验工具"""

import hashlib
from pathlib import Path


def compute_file_hash(file_path: Path) -> str:
    """
    计算文件 SHA256 哈希

    Args:
        file_path: 文件路径

    Returns:
        SHA256 哈希值（十六进制字符串）

    Examples:
        >>> from pathlib import Path
        >>> import tempfile
        >>> with tempfile.NamedTemporaryFile(mode='w', delete=False) as f:
        ...     _ = f.write("hello world")
        ...     temp_path = Path(f.name)
        >>> hash_val = compute_file_hash(temp_path)
        >>> len(hash_val)
        64
        >>> temp_path.unlink()  # 清理
    """
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


def compute_text_hash(text: str) -> str:
    """
    计算文本 SHA256 哈希

    Args:
        text: 文本内容

    Returns:
        SHA256 哈希值（十六进制字符串）

    Examples:
        >>> compute_text_hash("hello")
        '2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824'
        >>> compute_text_hash("hello") == compute_text_hash("hello")
        True
        >>> compute_text_hash("hello") == compute_text_hash("world")
        False
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
