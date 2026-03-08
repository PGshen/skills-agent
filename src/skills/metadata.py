"""SkillMetadata: extends SkillFrontmatter with runtime fields (source, skill_path)."""

from .frontmatter import ResourceLimitsConfig, SkillFrontmatter

# 统一别名：ResourceLimits = ResourceLimitsConfig（两者等同）
ResourceLimits = ResourceLimitsConfig


class SkillMetadata(SkillFrontmatter):
    """
    技能元数据（完整版）。
    = SkillFrontmatter（来自 SKILL.md YAML）
    + source、skill_path（由 SkillRegistry 在扫描时注入）
    """

    source: str      # 来源根目录标识：project / user / builtin
    skill_path: str  # SKILL.md 所在绝对路径（不发给模型）

    def to_model_view(self) -> dict:
        """返回模型可见层字段（仅 name/description/source）。

        控制字段（allowed_tools 等）绝不进入 model context。
        disable_model_invocation 的过滤由 SkillRegistry.list_model_views() 负责。
        """
        return {
            "name": self.name,
            "description": self.description,
            "source": self.source,
        }
