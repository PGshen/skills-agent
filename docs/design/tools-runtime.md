# Tools Runtime 设计

本文档细化 Tools Runtime（工具运行时）的设计：工具集合定义、权限合并规则、审批机制、审计格式、以及脚本执行的受控策略（超时/工作目录隔离/环境变量清理/输出截断）。整体协作参见：[agent-core.md](file:///Users/peng/Me/Ai/skills-agent/docs/design/agent-core.md)、[model-adapter.md](file:///Users/peng/Me/Ai/skills-agent/docs/design/model-adapter.md)、[skill-loader.md](file:///Users/peng/Me/Ai/skills-agent/docs/design/skill-loader.md)。

## 1. 系统定位与职责边界

Tools Runtime 是“可控执行层”，负责把 Agent Core 的结构化动作转换为受控的本地操作，并在执行前后强制执行安全策略与审计落盘协作：

- 工具调用校验：权限合并、危险操作审批、参数约束与路径约束
- 工具执行：读取文件、目录遍历、文本搜索、执行脚本等
- 输出受控：输出截断、摘要、内容哈希与引用落盘
- 统一审计：为 Agent Core 的事件流与 run 落盘提供结构化执行记录

不在 Tools Runtime 内处理：

- 决策与规划（由 Agent Core）
- 技能索引与正文/资源加载（正文/资源读取建议由 Skill Loader；只读工具也可复用其安全策略）
- 结构化动作解析与流式 JSON 解析（由 Model Adapter）

## 2. 工具集合与能力分级

### 2.1 最小工具集（MVP）

- 只读类：
  - `read_file`
  - `list_dir`
  - `grep`（按文本模式搜索）
- 执行类：
  - `run_script`

### 2.2 高风险工具（默认关闭）

- `write_file`
- `delete_file`
- `network_request`

这些工具即使启用也必须强制审批，并提供可重放请求（用于 CI/非交互场景）。

### 2.3 工具风险等级

- low：只读、无副作用
- medium：本地执行（脚本/命令），可能有副作用
- high：写入/删除/网络外联，破坏性或外泄风险

风险等级用于默认策略与审计标记。

## 3. 权限模型（最小权限 + 三方合并）

### 3.1 合并规则

最终允许工具集合：

全局配置允许 ∩ 技能 `allowed-tools`（若存在） ∩ 运行时策略（风险/环境/模式）

解释：

- 全局配置：系统级开关（例如默认只允许只读）
- 技能 allowed-tools：技能自身声明进一步收紧
- 运行时策略：根据运行模式（交互/CI）、风险等级、白名单等再收紧

### 3.2 典型默认策略

- 默认仅允许只读类工具
- `run_script` 需要显式开启，并且默认需要审批或至少需要 “本次 run 授权”
- 高风险工具默认关闭，除非用户显式开启且通过审批

### 3.3 授权的作用域

Tools Runtime 的授权可以按三种粒度配置（建议从严到宽）：

- 单次调用：每次调用都审批
- 本次 run：同一种工具在同一 run 内复用授权
- 会话/全局：不建议默认启用（风险高）

授权状态必须落盘到 run 目录（可审计、可回放）。

## 4. 审批机制（交互式与非交互式）

### 4.1 交互式审批（CLI）

当触发需要审批的工具时：

- Tools Runtime 生成审批请求对象（包含工具名、参数、风险、影响范围、可重放命令）
- Agent Core 通过事件流对外输出 `approval_required`
- CLI 由用户确认后返回 `approval_granted/denied`

### 4.2 非交互式（CI/服务端）

非交互模式下：

- 若工具需要审批：默认拒绝并返回结构化错误
- 同时输出“可重放请求”（例如 CLI 命令或 JSON 请求体）供人工执行或配置白名单

### 4.3 审批请求格式（示意）

```json
{
  "tool": "run_script",
  "risk": "medium",
  "reason": "Skill pdf-form-filler requested script execution",
  "params": {
    "skill": {"name": "pdf-form-filler", "source": "project"},
    "relative_path": "scripts/fill.py",
    "args": ["--input", "data.json"]
  },
  "replay": "agent run --approve run_script --skill pdf-form-filler -- scripts/fill.py --input data.json"
}
```

## 5. 工具执行策略（安全与可控）

### 5.1 read_file（只读）

约束：

- 允许读取的根路径受控（例如限定在 repo 根目录与技能目录，或由配置列出）
- 单次读取最大字节数限制
- 对大文件返回摘要或片段，并提供落盘引用

输出：

- `content_excerpt` + `sha256` + `truncated` + `storage_ref`（可选）

### 5.2 list_dir（只读）

约束：

- 限定根路径
- 返回条目数上限
- 默认隐藏敏感文件（可配置）

### 5.3 grep（只读）

约束：

- 限定根路径与文件类型
- 结果行数上限与单行截断
- 支持返回“仅文件列表”或“内容片段”（由参数控制）

### 5.4 run_script（受控执行）

核心目标：在不引入 OS 级沙箱的前提下，实现可接受的风险控制与可审计执行。

#### 5.4.1 路径与来源约束

- 脚本必须位于技能目录内（`<skill_dir>/scripts/...`）
- `relative_path` 必须通过与 Skill Loader 同等级的路径越界防护（realpath 前缀校验）

#### 5.4.2 进程与资源约束

- 超时：例如 30s（可配置）
- 工作目录：默认在 `.agent/runs/<run_id>/sandbox/`（隔离临时文件），必要时允许显式切换到技能目录
- 环境变量：默认清空或只保留最小白名单（PATH 等），禁止注入敏感变量
- 输出限制：stdout/stderr 按字符数截断，完整输出可落盘到 `observations/`

#### 5.4.3 参数约束

- args 必须是数组，每个元素长度受限
- 默认禁止 shell=True；仅允许直接 exec（避免命令注入）
- 如需执行 bash 脚本：仍以文件路径 + 参数执行，不拼接字符串命令

#### 5.4.4 退出码与错误处理

- exit_code != 0：返回结构化错误并附带 stdout/stderr 摘要
- 超时：标记为 `timeout=true` 并返回摘要

## 6. 输出控制：摘要、截断与落盘引用

Tools Runtime 的返回值应避免把大块内容直接注入上下文，遵循：

- 返回摘要（必要字段 + 关键片段）
- 大块内容落盘到 `.agent/runs/<run_id>/observations/` 或 `artifacts/`
- 在返回对象中提供 `storage_ref`（文件路径）与 `sha256`

这与 Agent Core 的上下文裁剪策略（见 agent-core.md）配套，保证“token 可控但可回放”。

## 7. 审计格式（与事件流对齐）

Tools Runtime 需要产出结构化审计记录，供 Agent Core 写入 `events.jsonl`。建议字段：

```json
{
  "tool": "run_script",
  "risk": "medium",
  "ts_start": "2026-02-03T12:34:56.789Z",
  "ts_end": "2026-02-03T12:34:57.120Z",
  "params": {"...": "..."},
  "result": {"ok": true, "exit_code": 0, "stdout_ref": "observations/stdout_3.txt"},
  "policy": {"approved": true, "approval_scope": "run"},
  "hashes": {"stdout_sha256": "..."}
}
```

说明：

- `params` 必须可脱敏（或仅记录摘要）
- 重要产物必须可追溯（ref + hash）

## 8. 错误类型（返回给 Agent Core 作为 observation）

建议统一错误类型（便于模型在下一轮 Decide 中处理）：

- `ToolNotAllowed`
- `ApprovalRequired`
- `ApprovalDenied`
- `PathTraversalBlocked`
- `FileTooLarge`
- `Timeout`
- `ExitNonZero`
- `IOError`

Agent Core 把错误作为 observation 注入上下文，并允许模型更新 Plan（例如改用只读方案、请求审批、降级输出）。
