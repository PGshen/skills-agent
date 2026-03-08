# Skill Loader 设计

**状态**：Phase A 完整实现

---

## 1. 设计目标

Skill Loader 是"按需内容加载层"，实现 Skills 渐进式披露的 Level 2（SKILL.md 正文）和 Level 3（资源文件片段）。

**核心职责**：
- 解析并剥离 YAML 前言，返回 SKILL.md 的 Markdown 正文
- 按结构化动作精确读取资源文件（`reference/`、`assets/` 等）
- 强制路径越界防护（禁止 `..`、禁止符号链接逃逸）
- 输出受控：正文和资源片段大小有上限，超限截断

**不在此处理**：
- 技能发现与元数据索引（Skill Registry 负责）
- 权限合并与审批（Tools Runtime 负责）
- 模型通信（Model Adapter 负责）

---

## 2. 渐进式披露层次

| 层级 | 触发时机 | 加载内容 | 负责方 |
|------|----------|----------|--------|
| Level 1 | Agent 启动 | 元数据（name/description/source） | Skill Registry |
| Level 2 | LOAD_SKILL 动作 | SKILL.md 正文（去除前言） | **Skill Loader** |
| Level 3 | LOAD_RESOURCE 动作 | 资源文件片段 | **Skill Loader** |

---

## 3. SkillLoader 接口

```python
# src/skills/loader.py
from pathlib import Path
from typing import Optional
from .metadata import SkillMetadata
from .frontmatter import FrontmatterParser
from ..common.security import validate_path_within_root

class LoadReport(dict):
    """加载报告（作为 dict 传入 EventLogger.data）"""
    # 包含字段：sha256, bytes_read, chars_returned, truncated, storage_ref

class SkillLoader:
    """
    技能内容加载器。
    Level 2：load_body()     → SKILL.md 正文（去前言）
    Level 3：load_resource() → 资源文件片段
    """

    # 正文大小限制
    MAX_BODY_LINES = 500
    MAX_BODY_CHARS = 40_000

    # 资源文件大小限制
    MAX_RESOURCE_BYTES = 2_000_000
    MAX_RESOURCE_EXCERPT_CHARS = 12_000

    def __init__(self):
        pass

    def load_body(self, meta: SkillMetadata) -> tuple[str, LoadReport]:
        """
        加载 SKILL.md 正文（Level 2）。
        - 解析并剥离 YAML 前言
        - 超过大小限制时截断（保留开头），标记 truncated=True
        - 返回 (body_text, report)
        """
        skill_dir = Path(meta.skill_path).parent
        skill_md = Path(meta.skill_path)

        raw = skill_md.read_text(encoding="utf-8")
        _, body = FrontmatterParser._split(raw)

        truncated = False
        lines = body.split("\n")

        if len(lines) > self.MAX_BODY_LINES:
            body = "\n".join(lines[:self.MAX_BODY_LINES])
            body += f"\n\n[...正文超过 {self.MAX_BODY_LINES} 行，已截断。完整内容请通过 load_resource 按需读取。]"
            truncated = True
        elif len(body) > self.MAX_BODY_CHARS:
            body = body[:self.MAX_BODY_CHARS]
            body += "\n\n[...正文超过字符限制，已截断。]"
            truncated = True

        import hashlib
        sha256 = hashlib.sha256(body.encode()).hexdigest()

        report = LoadReport(
            sha256=sha256,
            bytes_read=len(raw.encode()),
            chars_returned=len(body),
            truncated=truncated,
            storage_ref=None,
        )
        return body, report

    def load_resource(
        self,
        meta: SkillMetadata,
        relative_path: str,
        section_hint: Optional[str] = None,
        max_excerpt_chars: int = None,
    ) -> tuple[str, LoadReport]:
        """
        加载资源文件片段（Level 3）。
        - relative_path 必须在技能目录内（严格路径校验）
        - 若提供 section_hint，截取该章节内容
        - 超过大小限制时截断
        - 返回 (excerpt_text, report)
        """
        max_chars = max_excerpt_chars or self.MAX_RESOURCE_EXCERPT_CHARS
        skill_dir = Path(meta.skill_path).parent

        # 路径安全校验
        abs_path = self._resolve_safe(skill_dir, relative_path)

        if abs_path.stat().st_size > self.MAX_RESOURCE_BYTES:
            raise FileTooLargeError(
                f"Resource file too large: {relative_path} "
                f"({abs_path.stat().st_size} bytes > {self.MAX_RESOURCE_BYTES})"
            )

        content = abs_path.read_text(encoding="utf-8", errors="replace")

        # 提取章节（若有 section_hint）
        if section_hint:
            content = self._extract_section(content, section_hint) or content[:max_chars]

        truncated = False
        if len(content) > max_chars:
            content = content[:max_chars]
            content += f"\n\n[...已截断至 {max_chars} 字符]"
            truncated = True

        import hashlib
        sha256 = hashlib.sha256(content.encode()).hexdigest()

        report = LoadReport(
            sha256=sha256,
            bytes_read=abs_path.stat().st_size,
            chars_returned=len(content),
            truncated=truncated,
            storage_ref=None,
        )
        return content, report

    def _resolve_safe(self, skill_dir: Path, relative_path: str) -> Path:
        """
        解析资源路径，确保在技能目录内。
        防止路径穿越（..）和符号链接逃逸。
        """
        if ".." in relative_path or relative_path.startswith("/"):
            raise PathTraversalError(f"Invalid relative path: {relative_path!r}")

        resolved = (skill_dir / relative_path).resolve()
        skill_dir_resolved = skill_dir.resolve()

        if not str(resolved).startswith(str(skill_dir_resolved)):
            raise PathTraversalError(
                f"Path traversal detected: {relative_path!r} escapes skill directory"
            )

        if not resolved.exists():
            raise ResourceNotFoundError(f"Resource not found: {relative_path}")

        return resolved

    def _extract_section(self, content: str, section_hint: str) -> Optional[str]:
        """
        从 Markdown 内容中提取指定章节（标题行到下一同级标题之间的内容）。
        section_hint 示例：'## 指标定义' 或 '指标定义'
        """
        lines = content.split("\n")
        start_idx = None
        hint = section_hint.lstrip("#").strip()

        for i, line in enumerate(lines):
            if hint in line and line.startswith("#"):
                start_idx = i
                level = len(line) - len(line.lstrip("#"))
                break

        if start_idx is None:
            return None

        # 找到下一个同级或更高级别的标题
        end_idx = len(lines)
        for i in range(start_idx + 1, len(lines)):
            if lines[i].startswith("#"):
                cur_level = len(lines[i]) - len(lines[i].lstrip("#"))
                if cur_level <= level:
                    end_idx = i
                    break

        return "\n".join(lines[start_idx:end_idx])
```

---

## 4. 路径安全模型

### 4.1 校验流程

```
relative_path（来自模型的结构化动作）
    │
    ├── 禁止包含 '..'
    ├── 禁止以 '/' 开头（绝对路径）
    │
    ▼
abs_path = (skill_dir / relative_path).resolve()
    │
    ├── resolve() 解析所有符号链接
    ├── 要求 str(abs_path).startswith(str(skill_dir.resolve()))
    └── 不满足 → 抛出 PathTraversalError
```

### 4.2 允许的文件类型

MVP 阶段不限制文件类型，但二进制文件以 `errors="replace"` 读取（不抛出）。后续可扩展为白名单：`.md .txt .json .yaml .yml`。

---

## 5. 输出格式（供 Agent Core 注入上下文）

### Level 2（技能正文块）

```
[Skill: pdf-form-filler | source=project]
[Load Report: sha256=abc123 truncated=false chars=2048]

# PDF Form Filler

...（SKILL.md 正文内容）...
```

### Level 3（资源片段块）

```
[Resource: reference/kpi.md | skill=pdf-form-filler]
[Load Report: sha256=def456 truncated=true chars=12000]

## 指标定义

...（章节内容）...
[...已截断至 12000 字符]
```

---

## 6. 错误类型

| 错误 | 场景 | Agent Core 处理 |
|------|------|-----------------|
| `PathTraversalError` | `..` 或逃逸路径 | 返回 error observation，模型改路径 |
| `ResourceNotFoundError` | 文件不存在 | 返回 error observation |
| `FileTooLargeError` | 超过 2MB 限制 | 返回 error observation，建议分段读取 |
| `SectionNotFoundError` | section_hint 未命中 | 降级为文件开头片段 |
| `UnicodeDecodeError` | 二进制文件 | `errors="replace"` 读取，不抛出 |

```python
class SkillLoaderError(Exception): pass
class PathTraversalError(SkillLoaderError): pass
class ResourceNotFoundError(SkillLoaderError): pass
class FileTooLargeError(SkillLoaderError): pass
class SectionNotFoundError(SkillLoaderError): pass
```

---

## 7. 大块内容落盘协作

当资源内容超过上下文注入阈值时，Agent Core 可将完整内容落盘：

```python
# Agent Core 侧（伪代码）
excerpt, report = loader.load_resource(meta, "reference/large-doc.md")

if report["chars_returned"] >= STORAGE_THRESHOLD:
    # 落盘到 observations/
    storage_ref = event_logger.save_blob(excerpt, suffix=".md")
    report["storage_ref"] = storage_ref

# 注入上下文时只用摘要，完整内容通过 storage_ref 可追溯
obs_text = f"[Resource: reference/large-doc.md | truncated=True | ref={storage_ref}]\n{excerpt[:500]}..."
```

---

## 8. 验收标准

- [ ] `load_body()` 返回去除 YAML 前言的正文，`report["sha256"]` 非空
- [ ] 正文超过 500 行时截断，`report["truncated"] == True`
- [ ] `load_resource()` 成功读取 `reference/test.md` 并返回内容
- [ ] `section_hint="## 指标定义"` 正确截取对应章节
- [ ] `relative_path="../secret.txt"` 抛出 `PathTraversalError`
- [ ] `relative_path="/etc/passwd"` 抛出 `PathTraversalError`
- [ ] 文件不存在时抛出 `ResourceNotFoundError`

---

## 9. 设计权衡说明

| 决策点 | 选择 | 替代方案 | 选择理由 |
|--------|------|----------|----------|
| 正文超限策略 | 截断（保留开头） | 拒绝加载 | 保留技能概述和步骤摘要，不完全丢弃 |
| section_hint 未命中 | 降级为文件开头 | 抛出错误 | 宽容策略，优先给模型有用信息 |
| 路径校验方式 | realpath 前缀比较 | 黑名单字符过滤 | realpath 正确处理符号链接，更安全 |
| 资源片段大小 | 12000 chars | 更大/更小 | 约 3000 tokens，在上下文中合理；更大则单次 observation 占比过高 |
| 读取错误处理 | errors="replace" | 严格 UTF-8 | 技能中可能含非 UTF-8 资源，宽容读取避免崩溃 |
