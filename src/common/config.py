"""配置加载与管理"""

import copy
import json
from pathlib import Path
from typing import Any, Optional


class Config:
    """配置管理器"""

    DEFAULT_CONFIG = {
        "skill_roots": [
            {"source": "project", "path": ".agent/skills", "priority": 0},
            {"source": "user", "path": "~/.agent/skills", "priority": 1},
        ],
        "model": {"provider": "mock", "params": {}},
        "budget": {
            "max_turns": 12,
            "max_tool_calls": 30,
            "max_script_executions": 10,
            "max_context_tokens": 100000,
        },
        "execution": {
            "require_approval_for": ["run_script"],
            "allowed_tools": ["read_file", "list_dir", "grep", "run_script"],
        },
        "security": {
            "max_skill_body_lines": 500,
            "max_resource_file_bytes": 2000000,
        },
        "logging": {"level": "INFO", "format": "text"},
    }

    def __init__(self, config_path: Optional[Path] = None):
        """
        初始化配置管理器

        Args:
            config_path: 配置文件路径（可选）
        """
        self.config_path = config_path
        self._config: dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        """加载配置"""
        self._config = copy.deepcopy(self.DEFAULT_CONFIG)

        if self.config_path and self.config_path.exists():
            with open(self.config_path) as f:
                user_config = json.load(f)
                self._merge_config(user_config)

    def _merge_config(self, user_config: dict[str, Any]) -> None:
        """
        合并用户配置

        Args:
            user_config: 用户配置字典
        """
        def deep_update(target: dict[str, Any], source: dict[str, Any]) -> None:
            for k, v in source.items():
                if isinstance(v, dict) and isinstance(target.get(k), dict):
                    deep_update(target[k], v)
                else:
                    target[k] = v

        deep_update(self._config, user_config)

    def get(self, key: str, default: Any = None) -> Any:
        """
        获取配置项（支持点号分隔的嵌套键）

        Args:
            key: 配置键，支持 "a.b.c" 格式
            default: 默认值

        Returns:
            配置值

        Examples:
            >>> config = Config()
            >>> config.get("budget.max_turns")
            12
            >>> config.get("nonexistent", "default")
            'default'
        """
        keys = key.split(".")
        value = self._config
        for k in keys:
            if isinstance(value, dict):
                value = value.get(k)
                if value is None:
                    return default
            else:
                return default
        return value

    def to_dict(self) -> dict[str, Any]:
        """
        导出为字典

        Returns:
            配置字典的副本
        """
        return self._config.copy()
