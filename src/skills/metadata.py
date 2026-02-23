from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional
from pathlib import Path


@dataclass
class ResourceLimits:
    """资源配额限制"""
    max_script_time_sec: int = 30
    max_memory_mb: int = 512
    max_concurrent_scripts: int = 2
    allow_network: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "max_script_time_sec": self.max_script_time_sec,
            "max_memory_mb": self.max_memory_mb,
            "max_concurrent_scripts": self.max_concurrent_scripts,
            "allow_network": self.allow_network,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ResourceLimits":
        return cls(
            max_script_time_sec=data.get("max_script_time_sec", 30),
            max_memory_mb=data.get("max_memory_mb", 512),
            max_concurrent_scripts=data.get("max_concurrent_scripts", 2),
            allow_network=data.get("allow_network", False),
        )


@dataclass
class SkillMetadata:
    """技能元数据（Level 1）"""
    skill_id: str
    name: str
    description: str
    source: str  # project/user/builtin
    path: Path

    # 可选字段
    version: Optional[str] = None
    author: Optional[str] = None
    allowed_tools: Optional[List[str]] = None
    disable_model_invocation: bool = False
    user_invocable: bool = True
    requires: List[str] = field(default_factory=list)
    load_priority: str = "normal"  # high/normal/low
    resource_limits: ResourceLimits = field(default_factory=ResourceLimits)

    # 元信息
    frontmatter_hash: Optional[str] = None
    scanned_at: Optional[str] = None

    def __str__(self) -> str:
        ver = f"@{self.version}" if self.version else ""
        return f"SkillMetadata({self.source}:{self.name}{ver}, path={self.path})"

    @staticmethod
    def generate_skill_id(source: str, name: str, version: Optional[str] = None) -> str:
        """生成技能 ID，格式：{source}:{name}:{version}"""
        ver = version if version else "unversioned"
        return f"{source}:{name}:{ver}"

    def get_priority_score(self) -> int:
        """获取优先级分数（用于排序）"""
        priority_map = {"high": 0, "normal": 1, "low": 2}
        return priority_map.get(self.load_priority, 1)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "name": self.name,
            "description": self.description,
            "source": self.source,
            "path": str(self.path),
            "version": self.version,
            "author": self.author,
            "allowed_tools": self.allowed_tools,
            "disable_model_invocation": self.disable_model_invocation,
            "user_invocable": self.user_invocable,
            "requires": self.requires,
            "load_priority": self.load_priority,
            "resource_limits": self.resource_limits.to_dict(),
            "frontmatter_hash": self.frontmatter_hash,
            "scanned_at": self.scanned_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SkillMetadata":
        return cls(
            skill_id=data["skill_id"],
            name=data["name"],
            description=data["description"],
            source=data["source"],
            path=Path(data["path"]),
            version=data.get("version"),
            author=data.get("author"),
            allowed_tools=data.get("allowed_tools"),
            disable_model_invocation=data.get("disable_model_invocation", False),
            user_invocable=data.get("user_invocable", True),
            requires=data.get("requires", []),
            load_priority=data.get("load_priority", "normal"),
            resource_limits=ResourceLimits.from_dict(data.get("resource_limits", {})),
            frontmatter_hash=data.get("frontmatter_hash"),
            scanned_at=data.get("scanned_at"),
        )


@dataclass
class LoadedSkill:
    """已加载技能（Level 2）"""
    metadata: SkillMetadata
    body: str
    loaded_at_turn: int
    token_estimate: int
    body_hash: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "metadata": self.metadata.to_dict(),
            "body_preview": self.body[:200] + "..." if len(self.body) > 200 else self.body,
            "loaded_at_turn": self.loaded_at_turn,
            "token_estimate": self.token_estimate,
            "body_hash": self.body_hash,
        }
