"""公共基础设施模块的单元测试"""

import json

import pytest

from src.common.config import Config
from src.common.hash_utils import compute_file_hash, compute_text_hash
from src.common.security import (
    PathTraversalError,
    sanitize_frontmatter,
    validate_path_in_root,
    validate_relative_path,
)


class TestConfig:
    """测试配置管理器"""

    def test_config_default(self):
        """测试默认配置"""
        config = Config()
        assert config.get("budget.max_turns") == 12
        assert config.get("model.provider") == "mock"
        assert config.get("execution.allowed_tools") == [
            "read_file",
            "list_dir",
            "grep",
            "run_script",
        ]

    def test_config_get_nested(self):
        """测试嵌套配置获取"""
        config = Config()
        assert config.get("budget.max_turns") == 12
        assert config.get("budget.max_tool_calls") == 30
        assert config.get("model.provider") == "mock"

    def test_config_get_default(self):
        """测试默认值"""
        config = Config()
        assert config.get("nonexistent") is None
        assert config.get("nonexistent", "default") == "default"
        assert config.get("nested.nonexistent", 42) == 42

    def test_config_load_user_config(self, tmp_path):
        """测试加载用户配置"""
        config_file = tmp_path / "config.json"
        user_config = {
            "budget": {"max_turns": 20},
            "model": {"provider": "openai"},
            "custom_key": "custom_value",
        }
        with open(config_file, "w") as f:
            json.dump(user_config, f)

        config = Config(config_file)

        # 验证用户配置覆盖默认值
        assert config.get("budget.max_turns") == 20
        assert config.get("model.provider") == "openai"

        # 验证默认配置仍然存在（未被覆盖的部分）
        assert config.get("budget.max_tool_calls") == 30

        # 验证新增配置
        assert config.get("custom_key") == "custom_value"

    def test_config_to_dict(self):
        """测试导出为字典"""
        config = Config()
        config_dict = config.to_dict()

        assert isinstance(config_dict, dict)
        assert "budget" in config_dict
        assert "model" in config_dict
        assert config_dict["budget"]["max_turns"] == 12


class TestSecurity:
    """测试安全工具"""

    def test_validate_path_in_root_success(self, tmp_path):
        """测试路径校验成功"""
        root = tmp_path / "root"
        root.mkdir()
        file_path = root / "file.txt"
        file_path.touch()

        result = validate_path_in_root(file_path, root)
        assert result == file_path.resolve()

    def test_validate_path_in_root_nested_success(self, tmp_path):
        """测试嵌套路径校验成功"""
        root = tmp_path / "root"
        root.mkdir()
        nested_dir = root / "subdir" / "nested"
        nested_dir.mkdir(parents=True)
        file_path = nested_dir / "file.txt"
        file_path.touch()

        result = validate_path_in_root(file_path, root)
        assert result == file_path.resolve()

    def test_validate_path_in_root_traversal(self, tmp_path):
        """测试路径穿越检测"""
        root = tmp_path / "root"
        root.mkdir()
        outside = tmp_path / "outside.txt"
        outside.touch()

        with pytest.raises(PathTraversalError):
            validate_path_in_root(outside, root)

    def test_validate_path_in_root_symlink_traversal(self, tmp_path):
        """测试符号链接穿越检测"""
        root = tmp_path / "root"
        root.mkdir()
        outside = tmp_path / "outside.txt"
        outside.touch()

        # 创建指向外部文件的符号链接
        symlink = root / "link.txt"
        symlink.symlink_to(outside)

        # 符号链接解析后应该在 root 外
        with pytest.raises(PathTraversalError):
            validate_path_in_root(symlink, root)

    def test_validate_relative_path_safe(self):
        """测试安全的相对路径"""
        assert validate_relative_path("scripts/test.py") is True
        assert validate_relative_path("reference/doc.md") is True
        assert validate_relative_path("subdir/nested/file.txt") is True

    def test_validate_relative_path_traversal(self):
        """测试路径穿越检测"""
        assert validate_relative_path("../etc/passwd") is False
        assert validate_relative_path("../../root/.ssh/id_rsa") is False
        assert validate_relative_path("dir/../../../etc/passwd") is False

    def test_validate_relative_path_absolute(self):
        """测试绝对路径检测"""
        assert validate_relative_path("/etc/passwd") is False
        assert validate_relative_path("/root/.ssh/id_rsa") is False

    def test_sanitize_frontmatter_safe(self):
        """测试安全的前言文本"""
        text = "name: test-skill\ndescription: A test skill"
        result = sanitize_frontmatter(text)
        assert result == text

    def test_sanitize_frontmatter_remove_brackets(self):
        """测试移除尖括号"""
        text = "name: <script>alert(1)</script>"
        result = sanitize_frontmatter(text)
        assert "<" not in result
        assert ">" not in result
        assert result == "name: scriptalert(1)/script"


class TestHashUtils:
    """测试哈希工具"""

    def test_compute_text_hash(self):
        """测试文本哈希"""
        hash1 = compute_text_hash("hello")
        hash2 = compute_text_hash("hello")
        hash3 = compute_text_hash("world")

        # 相同文本产生相同哈希
        assert hash1 == hash2

        # 不同文本产生不同哈希
        assert hash1 != hash3

        # SHA256 产生 64 个十六进制字符
        assert len(hash1) == 64
        assert all(c in "0123456789abcdef" for c in hash1)

    def test_compute_text_hash_known_value(self):
        """测试已知值的哈希"""
        # "hello" 的 SHA256 哈希是已知的
        expected = "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
        result = compute_text_hash("hello")
        assert result == expected

    def test_compute_file_hash(self, tmp_path):
        """测试文件哈希"""
        file1 = tmp_path / "file1.txt"
        file2 = tmp_path / "file2.txt"
        file3 = tmp_path / "file3.txt"

        file1.write_text("hello world")
        file2.write_text("hello world")
        file3.write_text("different content")

        hash1 = compute_file_hash(file1)
        hash2 = compute_file_hash(file2)
        hash3 = compute_file_hash(file3)

        # 相同内容产生相同哈希
        assert hash1 == hash2

        # 不同内容产生不同哈希
        assert hash1 != hash3

        # SHA256 产生 64 个十六进制字符
        assert len(hash1) == 64

    def test_compute_file_hash_large_file(self, tmp_path):
        """测试大文件哈希（验证分块读取）"""
        large_file = tmp_path / "large.txt"

        # 创建一个大于 8192 字节的文件（触发多次读取）
        content = "x" * 10000
        large_file.write_text(content)

        hash_result = compute_file_hash(large_file)

        # 验证与直接计算的哈希一致
        expected = compute_text_hash(content)
        assert hash_result == expected
