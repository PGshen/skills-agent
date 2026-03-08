"""SkillMetadata and ResourceLimits data structures."""

from typing import Optional

from pydantic import BaseModel, Field


class ResourceLimits(BaseModel):
    """资源配额。enforced = 运行时强制执行；best-effort = 尽量遵守但不保证。"""

    max_script_time_sec: int = Field(default=30)        # enforced: subprocess timeout
    max_concurrent_scripts: int = Field(default=2)      # enforced: semaphore
    max_memory_mb: Optional[int] = Field(default=None)  # best-effort: macOS 无法可靠强制
    allow_network: bool = Field(default=False)          # best-effort: 进程级无法隔离


class SkillMetadata(BaseModel):
    """技能元数据（内部完整版，含控制层字段）"""

    # === 模型可见层 ===
    name: str
    description: str
    source: str        # 来源根目录标识：project / user / builtin
    skill_path: str    # SKILL.md 所在绝对路径（不发给模型）

    # === 内部控制层（不发给模型）===
    version: str = "1.0"
    author: Optional[str] = None
    disable_model_invocation: bool = False  # True 时从模型可见索引中隐藏（仅允许用户手动调用）
    user_invocable: bool = True             # 是否在 CLI/UI 列表中展示为可直接调用
    allowed_tools: list[str] = Field(default_factory=list)
    requires: list[str] = Field(default_factory=list)   # 依赖的其他技能名称列表
    load_priority: str = "normal"           # high / normal / low，影响多技能加载顺序
    resource_limits: ResourceLimits = Field(default_factory=ResourceLimits)
    run_mode: str = "inline"               # MVP-reserved: subagent 行为等同 inline

    def to_model_view(self) -> dict:
        """返回模型可见层字段。仅包含 name/description/source。

        注意：disable_model_invocation 的过滤由 SkillRegistry.list_model_views() 负责，
        本方法仅做字段投影，不做过滤。
        """
        return {
            "name": self.name,
            "description": self.description,
            "source": self.source,
        }
