"""SKILL.md YAML frontmatter parser (uses yaml.safe_load)."""
from dataclasses import dataclass
from typing import Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


class ResourceLimitsConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    max_script_time_sec: int = Field(default=30, alias="max-script-time-sec")
    max_concurrent_scripts: int = Field(default=2, alias="max-concurrent-scripts")
    max_memory_mb: Optional[int] = Field(default=None, alias="max-memory-mb")
    allow_network: bool = Field(default=False, alias="allow-network")


class SkillFrontmatter(BaseModel):
    """结构化的 SKILL.md frontmatter（经过类型校验，含默认值）。"""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    # 必填字段
    name: str
    description: str

    # 可选字段（含默认值）
    version: str = "1.0"
    author: Optional[str] = None
    disable_model_invocation: bool = Field(default=False, alias="disable-model-invocation")
    user_invocable: bool = Field(default=True, alias="user-invocable")
    allowed_tools: list[str] = Field(default_factory=list, alias="allowed-tools")
    requires: list[str] = Field(default_factory=list)
    load_priority: str = Field(default="normal", alias="load-priority")
    resource_limits: ResourceLimitsConfig = Field(
        default_factory=ResourceLimitsConfig, alias="resource-limits"
    )
    run_mode: str = Field(default="inline", alias="run-mode")

    @field_validator("version", mode="before")
    @classmethod
    def coerce_version_to_str(cls, v: object) -> str:
        """YAML 中 version: 1.0 会被解析为 float，统一转为 str。"""
        return str(v)


@dataclass
class ParsedSkillFile:
    frontmatter: SkillFrontmatter   # 结构化 frontmatter（经 Pydantic 验证）
    body: str                       # frontmatter 之后的 Markdown 正文


class FrontmatterParseError(Exception):
    pass


def parse_skill_file(content: str) -> ParsedSkillFile:
    """
    解析 SKILL.md 文件内容。
    返回 ParsedSkillFile，或在格式错误/安全违规时抛出 FrontmatterParseError。
    """
    # 1. 检查是否含危险字符
    if "<" in content:
        raise FrontmatterParseError("Content contains '<' which may indicate YAML tag injection")

    # 2. 提取 frontmatter 块（--- ... ---）
    lines = content.split("\n")
    if not lines or lines[0].strip() != "---":
        raise FrontmatterParseError("SKILL.md must start with '---'")

    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break
    if end_idx is None:
        raise FrontmatterParseError("Frontmatter closing '---' not found")

    fm_text = "\n".join(lines[1:end_idx])
    body = "\n".join(lines[end_idx + 1:]).strip()

    # 3. 用 yaml.safe_load 解析
    try:
        fm_raw = yaml.safe_load(fm_text)
    except yaml.YAMLError as e:
        raise FrontmatterParseError(f"YAML parse error: {e}")

    if not isinstance(fm_raw, dict):
        raise FrontmatterParseError("Frontmatter must be a YAML mapping")

    # 4. Pydantic 验证（类型校验 + 白名单 extra="forbid" + 默认值填充）
    try:
        frontmatter = SkillFrontmatter.model_validate(fm_raw)
    except ValidationError as e:
        raise FrontmatterParseError(f"Frontmatter validation error: {e}") from e

    return ParsedSkillFile(frontmatter=frontmatter, body=body)
