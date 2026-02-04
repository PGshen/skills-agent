# Skill Loader 设计

本文档细化 Skill Loader（技能加载器）的设计：SKILL.md 正文加载、资源按需读取、渐进式披露策略、大小/行数限制、注入上下文格式、以及安全与落盘协作。整体协作参见：[agent-core.md](file:///Users/peng/Me/Ai/skills-agent/docs/design/agent-core.md) 与总览 [agent-skills-tech-design.md](file:///Users/peng/Me/Ai/skills-agent/docs/agent-skills-tech-design.md)。

## 1. 系统定位与职责边界

Skill Loader 是“按需内容加载层”，其核心目标是实现 Skills 的渐进式披露（Level 2/3），并保证：

- 只加载被选中的技能正文（Level 2），未触发技能的正文不得读取
- 资源文件按动作精确读取（Level 3），不做全量扫描与全量加载
- 输出受控：避免把大块资源塞进上下文，优先摘要与片段
- 安全受控：防止路径穿越、符号链接逃逸、越界读取

不在 Skill Loader 内处理：

- 技能发现与元数据索引（由 Skill Registry 负责）
- 权限合并与审批（由 Tools Runtime / Agent Core 负责）
- 模型交互与结构化动作解析（由 Model Adapter 负责）

## 2. 输入输出与接口契约（非代码约定）

### 2.1 输入

- `skill_ref`：技能引用（name/source/path），由 Skill Registry 提供
- `relative_path`：资源相对路径（例如 `reference/kpi.md`），来自结构化动作
- `section_hint`：可选章节提示（例如 `## 指标定义`），用于片段截取
- `limits`：加载限制（最大行数/最大字符数/最大文件大小等）

### 2.2 输出

- `skill_body`：SKILL.md 正文文本（去除 YAML 前言）
- `resource_excerpt`：资源片段或摘要（优先摘要 + 引用指向）
- `load_report`：结构化加载报告（路径、大小、裁剪策略、哈希、引用文件）

### 2.3 核心接口（建议）

- `load_skill_body(skill_ref, limits) -> {text, report}`
- `load_resource(skill_ref, relative_path, section_hint?, limits) -> {text_or_excerpt, report}`

## 3. 渐进式披露实现策略

Skill Loader 只实现 Level 2/3；Level 1（元数据）由 Skill Registry 完成。

### 3.1 Level 2：加载 SKILL.md 正文

加载规则：

- 读取 `<skill_dir>/SKILL.md`
- 解析并剥离 YAML 前言，只返回 Markdown 正文
- 如果正文超过阈值：拒绝或裁剪，并要求技能作者拆分到 `reference/`（具体策略见 5.2）

注入建议（供 Agent Core 使用）：

- 在上下文中以“技能块”形式注入：
  - skill identity：name/source/path
  - 关键约束：allowed-tools/disable-model-invocation（由 Registry 提供）
  - skill body：可能裁剪后的正文

### 3.2 Level 3：按需加载资源文件

资源加载必须由结构化动作显式触发（`load_resource`），并且：

- 只允许在技能目录内读取（`skill_dir` 作为根）
- 只读取单个文件（由 `relative_path` 指定），不允许 glob
- 默认返回片段/摘要而不是全量（避免上下文膨胀）

section_hint 行为：

- 若提供章节标题：尝试从该标题开始截取到下一个同级标题，最多截取 `max_excerpt_chars`
- 若不提供：默认取文件开头或关键区域（例如 Contents/Overview），同样受上限限制

## 4. 安全模型（必须）

### 4.1 路径越界防护

对 `relative_path` 必须做严格校验：

- 禁止 `..` 路径段
- 禁止绝对路径
- 计算 `resolved_path = realpath(skill_dir / relative_path)` 后要求其前缀为 `realpath(skill_dir)`
- 禁止跟随符号链接越界（realpath 可解决大部分，仍建议对每级路径做一致性检查）

### 4.2 允许读取的文件类型（可选增强）

MVP 可先允许任意文本文件读取，但建议预留配置：

- 默认允许：`.md` `.txt` `.json` `.yaml` `.yml`
- 默认拒绝：二进制/可执行/超大文件

### 4.3 敏感信息与落盘协作

Skill Loader 返回的 `report` 应包含：

- `sha256`（内容哈希或片段哈希）
- `bytes_read` / `chars_returned`
- `truncated`（是否裁剪）
- `storage_ref`（若大块内容落盘，返回引用路径）

Agent Core 可据此把“资源大块内容”落盘到 `.agent/runs/<run_id>/observations/`，并在上下文只保留摘要 + `storage_ref`。

## 5. 输出控制：裁剪与摘要策略

### 5.1 为什么必须输出控制

Skills 机制在设计上允许技能目录包含大量参考资料，Skill Loader 必须保证“按需加载 + 输出受控”，否则 ReAct 循环会快速累积上下文，导致 token 过长。

### 5.2 SKILL.md 正文策略（Level 2）

建议默认限制：

- `max_skill_body_lines = 500`
- `max_skill_body_chars = 40_000`（或由配置控制）

超限处理（优先级）：

1. 拒绝加载，并在报告中提示：将长内容拆分到 `reference/` 并在正文中索引
2. 或裁剪加载：保留开头的概览/目录 + 关键步骤段落，并明确标注 `truncated=true`

裁剪原则：

- 保留“触发条件、步骤、验收、自检、资源索引”等关键段
- 丢弃“冗长背景材料、重复示例”，改为引用 `reference/*` 路径

### 5.3 资源文件策略（Level 3）

建议默认限制：

- `max_resource_file_bytes = 2_000_000`
- `max_resource_excerpt_chars = 12_000`

输出方式：

- 默认返回片段（excerpt）而不是全文
- 若模型明确要求全文：仍需要上限；超过上限则落盘并在上下文提供分段读取建议

## 6. 注入上下文格式（供 Agent Core 复用）

Skill Loader 建议提供统一的“注入块”模板，便于 Agent Core 拼装上下文并支持审计：

```text
[Skill: <name> | source=<source>]
[Skill Path: <path>]
[Load Report: sha256=<...> truncated=<true|false> bytes_read=<...>]
<skill body or resource excerpt>
```

其中 `<skill body or resource excerpt>` 必须是“已受控输出”的文本。

## 7. 与 Model Adapter 的协作点（流式解析不影响加载语义）

即使引入“结构化 JSON token 级流式解析”，Skill Loader 的语义也不改变：

- Model Adapter 负责从流式 JSON 中尽早解析出 `action.type` 与 `relative_path`
- Agent Core 可以提前回显“即将加载哪个技能/哪个资源”
- 但实际读取仍发生在本轮动作被完整校验通过之后（避免流式阶段的半成品指令触发 IO）

## 8. 失败处理

Skill Loader 的失败应返回结构化错误（由 Agent Core 记录为 observation）：

- `SkillNotFound`：技能目录不存在或 SKILL.md 缺失
- `InvalidFrontmatter`：SKILL.md 前言无法解析（若 Loader 需要复核）
- `PathTraversalBlocked`：检测到越界路径
- `FileTooLarge`：超过大小阈值
- `SectionNotFound`：section_hint 未命中（可降级为文件开头片段）
- `IOError`：读取失败（权限/不存在/编码异常）

Agent Core 在下一轮 Decide 中将错误作为 observation 提供给模型，让模型更新 Plan 或选择替代路径。
