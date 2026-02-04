# Skill Registry 设计

本文档细化 Skill Registry（技能注册表）的设计：多根目录扫描、元数据解析（YAML 前言子集）、冲突解决、缓存与刷新策略，以及暴露给 Agent/模型的技能索引格式。整体协作参见：[agent-core.md](file:///Users/peng/Me/Ai/skills-agent/docs/design/agent-core.md) 与总览 [agent-skills-tech-design.md](file:///Users/peng/Me/Ai/skills-agent/docs/agent-skills-tech-design.md)。

## 1. 系统定位与职责边界

Skill Registry 的核心职责是：在 Agent 启动或按需刷新时，扫描多个 skill root，构建“仅元数据”的技能索引（Skill Index），供 Agent Core 在 Level 1 渐进披露阶段提供给模型选择。

必须保证：

- 只解析 YAML 前言（name/description 等），不读取 SKILL.md 正文
- 可发现多来源技能（项目级/用户级/内置级），并提供稳定的冲突解决策略
- 输出稳定、可缓存、可审计（索引结果可落盘/可追溯）

不在 Skill Registry 内处理：

- 读取技能正文与资源文件（由 Skill Loader 负责）
- 工具执行、权限合并与审批（由 Tools Runtime / Agent Core 负责）
- 模型交互与结构化动作解析（由 Model Adapter 负责）

## 2. 输入输出与接口契约（非代码约定）

### 2.1 输入：Skill Roots

skill roots 是一组目录（含优先级/来源标签），默认顺序（高→低）：

1. 项目级：`<repo>/.agent/skills/`
2. 用户级：`~/.agent/skills/`
3. 内置级：安装包自带目录（只读）

每个 root 需要携带：

- `source`: `project | user | builtin`
- `path`: 绝对路径
- `priority`: 数值或按顺序隐含

### 2.2 输出：Skill Index（仅元数据）

Skill Index 是一个技能条目数组，供 Agent Core 注入给模型。条目建议字段：

```json
{
  "name": "pdf-form-filler",
  "description": "Extract and fill PDF form fields using Python",
  "source": "project",
  "path": "/abs/repo/.agent/skills/pdf-form-filler",
  "controls": {
    "disable_model_invocation": false,
    "user_invocable": true,
    "allowed_tools": ["read_file", "run_script"]
  },
  "meta": {
    "version": "1.0.0",
    "author": "team"
  }
}
```

约束：

- `name` 与 `description` 必须存在且为短文本
- `controls` 中的字段是“执行约束提示”，最终强制执行由 Agent Core/Tools Runtime 完成

### 2.3 核心接口（建议）

- `build_index(roots, options) -> {skills, report}`
- `refresh_index(changes?) -> {skills, report}`

其中 `report` 用于审计与调试（扫描耗时、发现数量、忽略原因、冲突决议等）。

## 3. 目录约定与发现规则

### 3.1 技能目录结构识别

Skill Registry 仅通过文件系统结构识别技能：

- 技能目录：`<root>/<skill_dir>/`
- 必须存在：`<root>/<skill_dir>/SKILL.md`

技能名来源建议优先级：

1. `SKILL.md` YAML 前言 `name`
2. 若前言缺失或解析失败：拒绝该技能目录（默认严格模式）

### 3.2 忽略规则

建议忽略：

- 隐藏目录（以 `.` 开头）
- 非目录项
- 缺少 `SKILL.md` 的目录

## 4. 元数据解析（YAML 前言子集）

### 4.1 解析范围（子集字段）

必需字段：

- `name: <string>`
- `description: <string>`

建议支持字段：

- `version: <string>`
- `author: <string>`
- `disable-model-invocation: <bool>`
- `user-invocable: <bool>`
- `allowed-tools: <list|string>`

### 4.2 安全约束

前言内容将被用于“模型可见的技能索引”，因此必须防注入：

- 禁止前言出现 `<` 与 `>` 字符（默认强约束）
- 限制长度：例如前言最大 200 行、每行最大 500 字符
- 仅解析简单标量与简单列表；遇到复杂嵌套结构直接拒绝或忽略非白名单字段

### 4.3 容错策略（严格 vs 宽松）

默认建议严格模式：

- 前言解析失败或缺少必需字段：该技能目录不进入索引，并记录忽略原因

可选宽松模式（用于迁移/兼容）：

- 若缺少 `description`：用空串或默认描述并标记 `diagnostics`
- 若 `allowed-tools` 为字符串：按逗号拆分为列表

## 5. 冲突解决与优先级规则

冲突定义：同一 `name` 在多个 root 中存在。

默认规则：

- 选择优先级更高的 root（project > user > builtin）
- 若同一 root 内出现重复 name：选择最近修改时间更晚的一个，并记录冲突报告

显式覆盖：

- 结构化动作中可以指定 `source`
- Agent Core 可将 `source` 作为过滤条件请求 Skill Registry 只返回某来源条目

可审计输出：

- 对每个被覆盖的条目记录：被覆盖版本、来源、路径、决议原因

## 6. 缓存与刷新策略

### 6.1 缓存目标

- 避免每轮 Decide 都扫描全盘
- 保证技能索引稳定，支持在 run 生命周期内一致

### 6.2 刷新触发

建议刷新触发点：

- Agent 启动：必刷新
- `skills install/uninstall` 完成后：刷新
- 用户显式请求：`skills refresh`

可选增强：

- 基于目录 mtime 的轻量检测
- 基于文件哈希的增量刷新（更重）

### 6.3 缓存一致性

建议在一次 run 中固定使用同一个 Skill Index 版本（snapshot），避免执行过程中技能集变化导致不可回放。

- run 启动时：记录 index hash
- audit 落盘：保存 skills index 快照（或保存 hash + 可重建信息）

## 7. 暴露给模型的索引格式（Level 1 注入）

Agent Core 将 Skill Index 注入模型上下文时，建议只暴露必要字段：

- `name`
- `description`
- `source`

其余字段（path、controls、meta）属于执行层信息，可由 Agent Core 保留在运行态，不直接给模型或只以最小必要形式提示。

示例（面向模型的列表）：

```text
Available Skills:
- name=pdf-form-filler | source=project | description=Extract and fill PDF form fields using Python
- name=code-review | source=user | description=Review a Python codebase with our standards
```

## 8. 报告与可观测性（与事件流/落盘协作）

Skill Registry 需要产出结构化 report，供 Agent Core 输出事件并落盘，例如：

- 扫描 roots 列表与耗时
- 总发现数量、有效数量、忽略数量与原因
- 冲突数量与决议明细
- 最终 skills index hash（用于回放一致性）
