# Evals 设计

本文档细化评估与回归（Evals）的设计：用例格式、判分规则（技能触发/动作序列/安全合规/输出质量/流式一致性）、以及报告产物（JSON/Markdown）。整体协作参见：[agent-core.md](file:///Users/peng/Me/Ai/skills-agent/docs/design/agent-core.md)、[model-adapter.md](file:///Users/peng/Me/Ai/skills-agent/docs/design/model-adapter.md)、[skill-registry.md](file:///Users/peng/Me/Ai/skills-agent/docs/design/skill-registry.md)、[skill-loader.md](file:///Users/peng/Me/Ai/skills-agent/docs/design/skill-loader.md)、[tools-runtime.md](file:///Users/peng/Me/Ai/skills-agent/docs/design/tools-runtime.md)。

## 1. 评估目标与原则

### 1.1 评估目标

- 验证 Skills 机制是否按“标准完整实现”工作：渐进式披露、技能索引、按需加载、权限与审计、分发一致性
- 验证 Agent Core 的 ReAct 主循环是否可控：一轮一动作、预算约束、失败降级、可回放
- 验证 Model Adapter 的结构化输出是否稳定：JSON schema 合规、纠错重试、（可选）流式解析一致性

### 1.2 原则

- 以“过程合规”为一等指标：不仅看最终文本，还要看动作序列与安全策略是否正确
- 测试必须可回放：以 `.agent/runs/<run_id>/events.jsonl` 为事实来源，评估从事件流重建执行过程
- 分层评估：优先用 MockModel 离线验证执行链路，再引入真实模型做触发与输出质量评估

## 2. 评估分层（Levels）

### 2.1 L0：纯单元（不跑 Agent）

验证纯函数与协议：

- frontmatter 解析与安全约束
- 流式 JSON 解析器（路径回调、incremental delta）
- 路径越界防护（skill loader/tools runtime）

### 2.2 L1：MockModel 端到端（推荐默认）

用例驱动 MockModel 输出动作序列，验证：

- Agent Core 状态机与预算约束
- Skill Registry/Loader/Tools Runtime 的接口与错误处理
- 事件流与落盘格式是否符合回放需要

### 2.3 L2：真实模型（可选）

验证更接近线上真实行为：

- 技能触发正确性（precision/recall）
- 输出质量与格式稳定性
- 结构化 JSON 合规率与重试成本

## 3. 用例格式（Case Spec）

每个用例是一个目录或一个 JSON/YAML 文件。建议以目录组织，便于携带输入文件与期望产物：

```
.agent/evals/<suite>/<case_id>/
  case.json
  inputs/
  expected/
```

### 3.1 case.json（建议字段）

```json
{
  "id": "basic_select_skill",
  "input": "请把这个PDF表单填好",
  "setup": {
    "skill_roots": ["./.agent/skills"],
    "preinstall_skills": []
  },
  "expect": {
    "skills_any_of": ["pdf-form-filler"],
    "skills_all_of": [],
    "action_sequence_constraints": [
      {"must_occur": "select_skills", "before": "run_script"},
      {"forbid": "write_file"}
    ],
    "output": {
      "contains_any": ["已完成", "表单"],
      "format": "markdown"
    }
  },
  "constraints": {
    "max_turns": 8,
    "max_tool_calls": 15,
    "deny_tools": ["network_request"]
  }
}
```

说明：

- `expect.skills_any_of / skills_all_of` 用于触发评估
- `action_sequence_constraints` 用于过程合规评估
- `constraints` 用于统一预算与安全约束（Agent Core/Tools Runtime 必须执行）

### 3.2 允许用例指定 MockModel 输出（L1）

为让 MockModel 可控，允许在用例中指定“预期动作脚本”：

```json
{
  "mock": {
    "turns": [
      {"action": {"type": "select_skills", "payload": {"skills": [{"name": "pdf-form-filler", "source": "project"}]}}},
      {"action": {"type": "run_script", "payload": {"skill": {"name": "pdf-form-filler", "source": "project"}, "relative_path": "scripts/fill.py", "args": []}}},
      {"action": {"type": "final_answer", "payload": {"content": "done"}}}
    ],
    "streaming": {
      "enabled": true,
      "chunk_bytes": 12
    }
  }
}
```

这用于验证：

- 动作序列被正确执行
- 流式 JSON chunk 被正确解析并回显

## 4. 评分维度与判分规则

### 4.1 技能触发（Trigger）

从事件流中提取 `select_skills` 动作的 skill name 列表，与用例期望比较：

- precision：选中的技能里有多少是期望技能
- recall：期望技能里有多少被选中

判定建议：

- `skills_all_of` 必须全部出现，否则 fail
- `skills_any_of` 至少出现一个，否则 fail

### 4.2 动作序列合规（Sequence Compliance）

从 `events.jsonl` 重建动作序列，检查约束：

- 必须顺序：例如 `select_skills` 必须在 `load_resource/run_script` 之前
- 一轮一动作：同一 turn 不得执行多个工具动作
- 禁止动作：例如 forbid `write_file`、forbid `network_request`

### 4.3 渐进式披露合规（Progressive Disclosure）

验证“只在需要时才加载”：

- 未触发技能不应出现 `load_skill_body` 事件（或等价证据）
- 未请求资源不应出现 `load_resource` 事件
- 事件中的加载报告应体现“受控输出”（truncated/sha256/storage_ref）

### 4.4 权限与审批合规（Security & Approval）

验证 Tools Runtime 的关键策略：

- deny_tools 中的工具不得执行，出现则 fail
- 需要审批的工具必须出现 `approval_required`，且只有 `approval_granted` 后才允许执行
- 路径越界尝试必须被 `PathTraversalBlocked` 拦截

### 4.5 预算合规（Budget）

- `max_turns/max_tool_calls/max_script_runs` 不得超限，超限则 fail
- 当预算耗尽：必须产出降级输出，并在审计中标记 stop reason

### 4.6 输出质量（Output）

输出质量应按用例定义的“轻量可判定条件”评估，避免依赖主观判断：

- contains_any / contains_all
- regex_match
- structured 格式（例如必须是 markdown 或必须是 JSON）

### 4.7 流式一致性（Streaming Consistency，可选）

当启用 token 级流式解析：

- delta 事件拼接后应等价于最终 `final_answer.content`
- 流式回调不得引发提前执行 IO（必须在完整 JSON 校验后才执行动作）

## 5. 评估输入来源：事件流与快照

评估引擎以 run 目录为输入，至少需要：

- `.agent/runs/<run_id>/events.jsonl`
- （可选）`state.json`、`final.md`、`observations/*`

原则：

- events.jsonl 是事实来源
- 大块输出通过 ref + hash 追溯，不要求全部进入事件 data

## 6. 报告产物（Artifacts）

### 6.1 JSON 报告（机器可读）

建议输出：

```json
{
  "suite": "smoke",
  "case_id": "basic_select_skill",
  "run_id": "20260203_123456_abcd",
  "pass": true,
  "scores": {
    "trigger": {"precision": 1.0, "recall": 1.0},
    "sequence": {"pass": true},
    "security": {"pass": true},
    "budget": {"pass": true},
    "output": {"pass": true}
  },
  "failures": [],
  "stats": {"turns": 3, "tool_calls": 1}
}
```

### 6.2 Markdown 报告（人读）

- 汇总通过率、失败用例列表
- 展示关键失败原因与相关事件片段引用（event id / 行号范围）

## 7. MockModel 在评估中的定位

MockModel 是默认评估驱动器，目标不是模拟真实智能，而是提供：

- 可重复、可控的动作输出（用于验证执行链路）
- 可选流式输出（用于验证 token 级体验链路）

真实模型评估（L2）应作为增量阶段，用于验证触发与输出质量，而不是基础链路正确性。
