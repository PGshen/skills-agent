"""SkillLoader: on-demand skill content loading."""
from pathlib import Path
from typing import Optional

from .frontmatter import FrontmatterParseError, parse_skill_file
from .metadata import SkillMetadata


class PathTraversalError(ValueError):
    """Raised when a resource path escapes the skill directory."""


class SkillLoader:
    """
    渐进式披露加载器。
    - Level 2: load_body()     — 读取 SKILL.md 正文（frontmatter 后的 Markdown）
    - Level 3: load_resource() — 读取技能目录内的资源文件
    """

    def load_body(self, meta: SkillMetadata) -> tuple[str, dict]:
        """
        加载 SKILL.md 正文（Level 2）。
        返回 (正文文本, 报告dict)。
        报告dict 含 {'chars': int, 'lines': int, 'skill_name': str}。
        """
        skill_md = Path(meta.skill_path)
        content = skill_md.read_text(encoding="utf-8")
        parsed = parse_skill_file(content)
        body = parsed.body
        return body, {
            "skill_name": meta.name,
            "chars": len(body),
            "lines": body.count("\n") + (1 if body and not body.endswith("\n") else 0),
        }

    def load_resource(
        self,
        meta: SkillMetadata,
        resource: str,
        section_hint: Optional[str] = None,
    ) -> tuple[str, dict]:
        """
        加载技能 resource 文件（Level 3）。
        resource 是相对于技能目录的路径，如 "reference/api.md"。
        section_hint：可选，指定只读某个一级标题章节（降低 token 消耗）。
        安全检查：禁止 path traversal，解析后路径必须在技能目录内。
        返回 (文件内容或章节内容, 报告dict)。
        """
        if ".." in resource:
            raise PathTraversalError(f"Resource path must not contain '..': {resource!r}")
        if resource.startswith("/"):
            raise PathTraversalError(f"Resource path must be relative, not absolute: {resource!r}")

        skill_dir = Path(meta.skill_path).parent.resolve()
        resolved = (skill_dir / resource).resolve()

        if not str(resolved).startswith(str(skill_dir)):
            raise PathTraversalError(
                f"Resource path {resource!r} resolves outside skill directory"
            )

        content = resolved.read_text(encoding="utf-8")

        if section_hint:
            content = _extract_section(content, section_hint)

        return content, {
            "skill_name": meta.name,
            "resource": resource,
            "chars": len(content),
            "lines": content.count("\n") + (1 if content and not content.endswith("\n") else 0),
        }


def _extract_section(content: str, section_hint: str) -> str:
    """
    从 Markdown 文本中提取指定一级标题（# Title）的章节内容。
    若未找到则返回完整内容。
    """
    lines = content.split("\n")
    target = section_hint.lstrip("#").strip().lower()

    start = None
    for i, line in enumerate(lines):
        if line.startswith("# ") and line[2:].strip().lower() == target:
            start = i
            break

    if start is None:
        return content

    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("# "):
            end = i
            break

    return "\n".join(lines[start:end])
