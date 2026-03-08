# Skill Registry 设计

**状态**：Phase A 完整实现

---

## 1. 设计目标

Skill Registry 是技能发现层，在 Agent 启动时扫描多个 skill root，构建"仅元数据"的技能索引（Skill Index），供 Agent Core 注入给模型使用（渐进式披露 Level 1）。

**核心职责**：
- 扫描多根目录，发现所有合法 SKILL.md
- 解析 YAML 前言（PyYAML safe_load），构建 SkillMetadata 列表
- 处理同名技能冲突（高优先级 source 胜出）
- 提供 `find()` 接口供 Agent Core 按名称查询技能

**不在此处理**：
- 读取 SKILL.md 正文（Skill Loader 负责）
- 工具执行与权限（Tools Runtime 负责）
- 模型通信（Model Adapter 负责）

---

## 2. 数据流

```
skill_roots（配置中的目录列表）
    │
    ▼
[SkillRegistry.scan()]
    │
    ├── 遍历每个 root 目录
    ├── 发现 <root>/<skill_dir>/SKILL.md
    ├── FrontmatterParser.parse() → (name, description, controls...)
    └── 构造 SkillMetadata，加入索引
    │
    ▼
SkillIndex（list[SkillMetadata]）
    │
    ├── [AgentCore] 调用 .to_model_view() 注入给模型（Level 1）
    └── [AgentCore] 调用 .find(name) 定位技能目录（执行前）
```

---

## 3. SkillRegistry 接口

```python
# src/skills/registry.py
from pathlib import Path
from typing import Optional
from .metadata import SkillMetadata
from .frontmatter import FrontmatterParser

class SkillRegistry:
    """
    技能注册表。
    扫描多个 skill root，构建 SkillMetadata 列表。
    在 Agent 启动时调用 scan()，运行期间使用缓存结果。
    """

    def __init__(self, skill_roots: list[dict]):
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

            for skill_dir in root_path.iterdir():
                if not skill_dir.is_dir() or skill_dir.name.startswith("."):
                    continue

                skill_md = skill_dir / "SKILL.md"
                if not skill_md.exists():
                    continue

                try:
                    meta = FrontmatterParser.parse_skill(skill_md, root["source"])
                except Exception:
                    continue  # 解析失败：忽略该技能，记录日志

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

    def to_index_text(self) -> str:
        """
        生成注入给模型的技能索引文本（仅模型可见层）。
        示例：
          - name=pdf-form-filler | source=project | description=...
        """
        lines = []
        for meta in self.all():
            view = meta.to_model_view()
            lines.append(
                f"- name={view['name']} | source={view['source']} | description={view['description']}"
            )
        return "\n".join(lines) if lines else "(no skills available)"
```

---

## 4. FrontmatterParser

```python
# src/skills/frontmatter.py
import yaml
from pathlib import Path
from .metadata import SkillMetadata, ResourceLimits

# YAML 前言字段白名单（只解析这些字段，忽略其他）
_ALLOWED_FIELDS = {
    "name", "description", "version", "allowed-tools", "resource-limits",
}

class FrontmatterParser:
    """
    SKILL.md YAML 前言解析器。
    安全约束：使用 yaml.safe_load，拒绝注入字符，限制大小。
    """

    MAX_FRONTMATTER_LINES = 50
    MAX_FIELD_LENGTH = 500

    @classmethod
    def parse_skill(cls, skill_md: Path, source: str) -> SkillMetadata:
        """
        解析 SKILL.md 文件，提取 YAML 前言并构造 SkillMetadata。
        抛出 ValueError / yaml.YAMLError 表示解析失败。
        """
        raw = skill_md.read_text(encoding="utf-8")
        frontmatter, _ = cls._split(raw)
        data = cls._parse_yaml(frontmatter)

        name = cls._require_str(data, "name")
        description = cls._require_str(data, "description")

        # 安全检查：禁止含 < 或 > 的值（防止 HTML/提示注入）
        for field in (name, description):
            if "<" in field or ">" in field:
                raise ValueError(f"Frontmatter field contains forbidden characters: {field!r}")

        # 解析 allowed-tools
        allowed_tools = data.get("allowed-tools", [])
        if isinstance(allowed_tools, str):
            allowed_tools = [t.strip() for t in allowed_tools.split(",")]

        # 解析 resource-limits
        limits_raw = data.get("resource-limits", {}) or {}
        resource_limits = ResourceLimits(
            max_script_time_sec=limits_raw.get("max-script-time-sec", 30),
            max_concurrent_scripts=limits_raw.get("max-concurrent-scripts", 2),
            max_memory_mb=limits_raw.get("max-memory-mb"),
            allow_network=limits_raw.get("allow-network", False),
        )

        return SkillMetadata(
            name=name,
            description=description,
            source=source,
            skill_path=str(skill_md),
            version=str(data.get("version", "1.0")),
            allowed_tools=allowed_tools,
            resource_limits=resource_limits,
        )

    @classmethod
    def _split(cls, content: str) -> tuple[str, str]:
        """
        分离 YAML 前言（--- ... ---）与 Markdown 正文。
        返回 (frontmatter_text, body_text)。
        若无前言则 frontmatter_text 为空串。
        """
        lines = content.split("\n")
        if not lines or lines[0].strip() != "---":
            return "", content

        end = None
        for i, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                end = i
                break

        if end is None:
            return "", content

        if end > cls.MAX_FRONTMATTER_LINES:
            raise ValueError(f"Frontmatter exceeds {cls.MAX_FRONTMATTER_LINES} lines")

        frontmatter = "\n".join(lines[1:end])
        body = "\n".join(lines[end + 1:])
        return frontmatter, body

    @classmethod
    def _parse_yaml(cls, text: str) -> dict:
        """使用 yaml.safe_load 解析，不允许任意 Python 对象。"""
        result = yaml.safe_load(text) or {}
        if not isinstance(result, dict):
            raise ValueError("Frontmatter must be a YAML mapping")
        # 只保留白名单字段
        return {k: v for k, v in result.items() if k in _ALLOWED_FIELDS}

    @classmethod
    def _require_str(cls, data: dict, key: str) -> str:
        val = data.get(key)
        if not val or not isinstance(val, str):
            raise ValueError(f"Missing or invalid required field: '{key}'")
        if len(val) > cls.MAX_FIELD_LENGTH:
            raise ValueError(f"Field '{key}' exceeds max length {cls.MAX_FIELD_LENGTH}")
        return val.strip()
```

---

## 5. 目录识别规则

| 规则 | 说明 |
|------|------|
| 有效技能目录 | `<root>/<dir>/SKILL.md` 存在 |
| 忽略隐藏目录 | 目录名以 `.` 开头则跳过 |
| 忽略非目录项 | 文件直接跳过 |
| 缺少 SKILL.md | 目录跳过，不报错 |
| 前言解析失败 | 记录 WARNING 日志，跳过该技能 |
| 缺少 name/description | 解析失败，跳过 |

---

## 6. 冲突解决规则

同一 `name` 在多个 root 中存在时：

```
优先级数值越小 → 优先级越高 → 胜出

project (priority=0) > user (priority=1) > builtin (priority=2)
```

同一 root 内重复 name（理论上不应发生）：取最后扫描到的一个，并写入 WARNING 日志。

冲突决议记录在 `scan()` 的返回日志（可选）中，格式示例：

```json
{
  "conflicts": [
    {
      "name": "code-review",
      "winner": {"source": "project", "path": "..."},
      "overridden": {"source": "user", "path": "..."}
    }
  ]
}
```

---

## 7. 缓存策略

- **一次 run 内固定索引**：Agent Core 在 run 开始时调用 `scan()`，之后使用缓存，run 期间不再重新扫描。这保证同一次任务中技能集不会变化，事件流可回放。
- **chat 模式**：每轮用户请求调用 `all()`，无需重新扫描（技能目录预期不在对话中途变化）。
- **显式刷新**：`skills-agent skills refresh` 命令或安装/卸载技能后触发重新 `scan()`。

---

## 8. 验收标准

- [ ] `scan()` 在测试 fixtures 目录（含 2 个技能）返回 2 个 SkillMetadata
- [ ] `find("example-skill")` 返回正确的 SkillMetadata，`skill_path` 正确指向 SKILL.md
- [ ] 同名技能：project 来源优先于 user 来源
- [ ] 前言缺少 `name` 时该技能被忽略（不影响其他技能的扫描）
- [ ] 前言含 `<` 字符时抛出 ValueError（而不是将注入内容送入模型）
- [ ] `to_index_text()` 输出不含 `allowed_tools`、`skill_path` 等控制字段

---

## 9. 设计权衡说明

| 决策点 | 选择 | 替代方案 | 选择理由 |
|--------|------|----------|----------|
| 前言解析库 | PyYAML safe_load | yaml.load / 手写解析 | safe_load 禁止任意对象，安全有保证 |
| 前言字段白名单 | 只解析已知字段 | 允许任意字段 | 防止技能作者注入未知字段影响系统行为 |
| 冲突解决策略 | 按 priority 数值取优先 | 用户显式指定 | 规则简单、可预测、无需用户干预 |
| 正文读取位置 | 不在 Registry 读 | Registry 读全部 | 将"发现"与"按需加载"职责严格分离 |
| 扫描时机 | 启动时一次性扫描 | 按需懒加载 | 保证 run 期间技能集一致，便于审计回放 |
