"""SkillRegistry: skill scanning and indexing."""
import logging
from pathlib import Path
from typing import Optional

from .frontmatter import FrontmatterParseError, parse_skill_file
from .metadata import SkillMetadata

logger = logging.getLogger(__name__)


class SkillRegistry:
    """
    技能注册表。
    扫描多个 skill root，构建 SkillMetadata 列表。
    在 Agent 启动时调用 scan()，运行期间使用缓存结果。
    """

    def __init__(self, skill_roots: list[dict]) -> None:
        """
        skill_roots: 配置列表，每项包含 source、path、priority。
        示例：[
            {"source": "project", "path": ".agent/skills", "priority": 0},
            {"source": "user",    "path": "~/.agent/skills", "priority": 1},
        ]
        """
        self._roots = [
            {
                "source": r["source"],
                "path": Path(r["path"]).expanduser().resolve(),
                "priority": r.get("priority", 99),
            }
            for r in skill_roots
        ]

        self._index: list[SkillMetadata] = []
        self._scanned = False

    def scan(self) -> list[SkillMetadata]:
        """
        扫描所有 skill root，返回 SkillMetadata 列表。
        同名技能按 priority 取优先级更高的（数值越小优先级越高）。
        结果缓存在内存中，多次调用 scan() 重新扫描（刷新缓存）。
        """
        candidates: dict[str, tuple[SkillMetadata, int]] = {}  # name → (meta, priority)

        for root in sorted(self._roots, key=lambda r: r["priority"]):
            root_path: Path = root["path"]
            if not root_path.is_dir():
                continue

            for skill_dir in sorted(root_path.iterdir()):
                if not skill_dir.is_dir() or skill_dir.name.startswith("."):
                    continue

                skill_md = skill_dir / "SKILL.md"
                if not skill_md.exists():
                    continue

                try:
                    content = skill_md.read_text(encoding="utf-8")
                    parsed = parse_skill_file(content)
                    meta = SkillMetadata(
                        **parsed.frontmatter.model_dump(by_alias=False),
                        source=root["source"],
                        skill_path=str(skill_md),
                    )
                except (FrontmatterParseError, Exception) as e:
                    logger.warning("Failed to parse skill at %s: %s", skill_md, e)
                    continue

                # 冲突解决：优先级更高（priority 数值更小）的 source 胜出
                if meta.name not in candidates or root["priority"] < candidates[meta.name][1]:
                    candidates[meta.name] = (meta, root["priority"])

        self._index = [m for m, _ in candidates.values()]
        self._scanned = True
        return self._index

    def all(self) -> list[SkillMetadata]:
        """返回缓存的技能列表（未扫描时先触发 scan）。"""
        if not self._scanned:
            self.scan()
        return self._index

    def find(self, name: str, source: Optional[str] = None) -> Optional[SkillMetadata]:
        """
        按名称（可选 source）查找技能。
        Agent Core 在执行 LOAD_SKILL 时使用此接口定位技能目录。
        """
        for meta in self.all():
            if meta.name == name:
                if source is None or meta.source == source:
                    return meta
        return None

    def list_model_views(self) -> list[dict]:
        """返回模型可见层（过滤掉 disable_model_invocation=True 的技能）。"""
        return [
            meta.to_model_view()
            for meta in self.all()
            if not meta.disable_model_invocation
        ]

    def to_index_text(self) -> str:
        """
        生成注入给模型的技能索引文本（仅模型可见层）。
        示例：
          - name=pdf-form-filler | source=project | description=...
        """
        views = self.list_model_views()
        if not views:
            return "(no skills available)"
        return "\n".join(
            f"- name={v['name']} | source={v['source']} | description={v['description']}"
            for v in views
        )
