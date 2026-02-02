# Agent Core 设计

本文档细化 Agent Core 的设计：规划机制、ReAct 主循环、技能触发策略、上下文组织、动态计划更新、错误恢复、预算与退出条件，以及与 Skill Registry / Skill Loader / Tools Runtime / Model Adapter 的接口契约。

总览参见：[agent-skills-tech-design.md](file:///Users/peng/Me/Ai/skills-agent/docs/agent-skills-tech-design.md)。

## 1. 设计目标

- 提供稳定的多轮执行主循环，支持动态规划与可回放审计
- 把 Skills 机制作为一等能力：技能发现、选择、渐进式披露、资源按需加载
- 把安全与可控作为一等约束：最小权限、审批、预算限制、失败降级
- 支持评估与回归：在 MockModel 下离线重放动作序列并判分

## 2. Agent 运行模型（ReAct + 显式 Plan）

### 2.1 为什么是 ReAct + 显式 Plan

- ReAct 负责“循环骨架”：Reason → Act → Observe → Repeat
- Plan 是 ReAct 循环中的“显式状态”：把目标拆成 steps，记录假设与约束，使多轮推进更稳定、可控、可评估
- Skills 机制要求严格的“按需加载”，显式 Plan 有助于抑制模型一次性加载过多技能/资源

### 2.2 关键原则

- 每一轮只允许一次模型调用（一次 Decide）：模型在同一响应中完成“是否更新 Plan + 输出下一步结构化动作”
- Observe 不是模型调用：Observe 是 Agent 执行动作后的结果（observation），注入上下文后进入下一轮 Decide
- 模型不能直接执行工具：必须输出结构化动作，由 Agent Core 校验后委派给 Tools Runtime / Skill Loader

## 3. 总体时序与状态机

### 3.1 时序（含渐进式披露）

```mermaid
sequenceDiagram
  autonumber
  participant U as User
  participant A as Agent Core
  participant R as Skill Registry
  participant M as Model
  participant L as Skill Loader
  participant T as Tools Runtime

  U->>A: request
  A->>R: load skill index (metadata only)
  R-->>A: skills (name/description/source/controls)
  A->>M: Decide#1 prompt + skill index
  M-->>A: action (+ optional plan_update)

  loop until final_answer or budget exceeded
    alt action == select_skills
      A->>L: load SKILL.md body
      L-->>A: skill body
      A->>M: Decide prompt + loaded skill body
      M-->>A: action (+ optional plan_update)
    else action == load_resource
      A->>L: load resource file (scoped)
      L-->>A: resource excerpt/summary
      A->>M: Decide prompt + observation
      M-->>A: action (+ optional plan_update)
    else action == run_script
      A->>T: run script (controlled)
      T-->>A: execution summary
      A->>M: Decide prompt + observation
      M-->>A: action (+ optional plan_update)
    else action == final_answer
      A-->>U: response
    end
  end
```

### 3.2 状态机（Agent Core 视角）

```mermaid
stateDiagram-v2
  [*] --> Init
  Init --> IndexSkills: load metadata only
  IndexSkills --> Decide

  Decide --> LoadSkillBody: select_skills
  Decide --> LoadResource: load_resource
  Decide --> RunScript: run_script
  Decide --> Final: final_answer

  LoadSkillBody --> Observe
  LoadResource --> Observe
  RunScript --> Observe

  Observe --> Decide: inject observation (+ plan update)
  Final --> [*]
```

## 4. Run State（运行态状态）数据模型

Agent Core 维护 Run State，驱动整个多轮执行过程并用于审计与回放。

### 4.1 核心字段（概念层）

- Request：用户请求（原文 + 归一化）
- SkillIndex：技能元数据列表（name/description/source/controls）
- LoadedSkills：已加载技能正文的集合（按需）
- Plan：当前计划（可动态改写）
- Budget：执行预算与限制（max_turns/max_tool_calls/max_script_runs/max_context_chars）
- Context：对话上下文（system+developer+user+observations+skill bodies）
- AuditTrail：动作与观察的结构化记录（可落盘）

### 4.2 Plan 结构（建议）

```json
{
  "goal": "完成用户任务",
  "steps": [
    {"id": "s1", "title": "选择并加载相关技能", "status": "pending"},
    {"id": "s2", "title": "执行技能流程并收集结果", "status": "pending"},
    {"id": "s3", "title": "自检与输出", "status": "pending"}
  ],
  "assumptions": [],
  "constraints": {
    "max_turns": 12,
    "max_tool_calls": 30,
    "max_script_runs": 6
  }
}
```

### 4.3 Plan 生命周期

- 创建：`IndexSkills` 完成后的第一次 `Decide` 中，由提示词要求模型同时产出初始 Plan + 下一步动作
- 更新：每次执行动作产生 observation 后的下一次 `Decide` 中，模型基于“当前 Plan + 新 observation + 预算”更新 Plan
- 表达：支持两种方式
  - 全量替换：`plan` 字段返回完整新计划
  - 增量更新：`plan_update` 以 patch 形式描述变更（便于审计与评估）

## 5. 结构化动作协议（Agent Core 侧约束）

动作协议的完整定义由 [model-adapter.md](file:///Users/peng/Me/Ai/skills-agent/docs/design/model-adapter.md) 展开；Agent Core 关注的是“允许哪些动作、何时允许、如何校验、如何执行”。

### 5.1 动作类型（最小集合）

- `select_skills`：选择要加载的技能（可能多选，但需受控）
- `load_resource`：读取技能资源文件（reference/assets）
- `run_script`：执行技能脚本（scripts）
- `final_answer`：输出最终答复

### 5.2 动作守卫（Guardrails）

- `select_skills`
  - 禁止选择 `disable-model-invocation: true` 的技能（除非用户显式指定并允许）
  - 每轮最多加载 N 个技能正文（默认 1~2），超过则拒绝并要求模型分批

- `load_resource`
  - `relative_path` 必须落在已选择技能目录内
  - 默认只读取必要片段或做摘要（避免把整本手册塞进上下文）

- `run_script`
  - 必须在技能目录内且路径受控
  - 必须通过 Tools Runtime 权限合并与审批（见 tools-runtime.md）
  - 必须有超时与输出截断

## 6. 技能选择策略（Skills-first 的规划落地）

### 6.1 选择顺序（递进策略）

默认策略（可配置）：

1. 先选择“工作流/规范型技能”（例如代码评审规范、报告格式规范）
2. 再选择“领域能力型技能”（例如 PDF/Excel/数据库操作）
3. 如果需要外部操作（脚本/写文件/网络），优先选择包含自检与验收步骤的技能

### 6.2 同名技能冲突处理

- 默认：按 Skill Root 优先级选择（project > user > builtin）
- 显式覆盖：模型可指定 source，Agent Core 校验合法性后执行

### 6.3 防止过早加载

- 模型只能看到技能索引（name/description），看不到正文
- 要求模型解释选择原因（reason），并鼓励“先加载一个最相关技能，再迭代”

## 7. 上下文组织（Context Packing）

### 7.1 注入层级（对应渐进式披露）

- Level 1：技能索引（只含 name/description/source/controls）
- Level 2：已加载的 SKILL.md 正文（仅被选择的技能）
- Level 3：资源片段/脚本输出摘要（仅在动作请求后）

### 7.2 上下文分区建议

Agent Core 在构造模型输入时按固定分区拼装，以便评估、调试与稳定性：

- System/Developer：全局规则、安全约束、动作协议
- User Request：原始请求
- Run State Summary：当前 Plan、预算剩余、已加载技能列表
- Skills Index：可用技能元数据
- Loaded Skills：已加载技能正文（可裁剪）
- Observations：工具/脚本/资源读取结果（摘要 + 指向来源）

## 8. 预算与退出条件

### 8.1 预算（Budget）

预算用于将 Agent Core 从“可能发散的多轮对话”约束为“可验证的执行过程”：

- `max_turns`：最大 Decide 轮数
- `max_tool_calls`：最大工具调用次数
- `max_script_runs`：最大脚本执行次数
- `max_context_chars`：上下文大小上限（近似控制）

### 8.2 退出条件（Stop Conditions）

- 模型输出 `final_answer`
- 预算耗尽：输出降级答复（包含已完成部分、下一步建议、阻塞原因）
- 连续失败：同一动作类型或同一工具失败超过阈值，触发降级/停止

## 9. 错误处理与恢复策略

### 9.1 工具失败

- 失败被记录为 observation，模型在下一次 Decide 中决定重试/改路径/降级
- Agent Core 可提供“自动重试上限”作为硬约束（例如网络/脚本最多重试 1 次）

### 9.2 脚本执行异常

- 超时：直接终止并产出超时 observation
- 非 0 退出码：记录 stdout/stderr 摘要 + exit_code，要求模型走失败分支或请求更换方案

### 9.3 权限拒绝 / 需要审批

- 将“拒绝原因 + 可重放请求”作为 observation
- 模型应更新 Plan（插入“请求审批/改用只读方案/输出建议”）

## 10. 审计与可回放（Evals 依赖）

Agent Core 输出的 AuditTrail 建议包含：

- 每轮 Decide 的模型输出（动作 + plan_update）
- 每次动作的输入参数、校验结果、执行耗时、输出摘要
- 预算变化与停止原因

评估侧可用同一份审计数据进行：

- 动作序列合规判分（必须先 select_skills 才能 load_resource/run_script）
- 预算合规判分
- 安全策略命中判分

## 11. 与其他子系统的接口契约（概要）

### 11.1 Skill Registry

参见：[skill-registry.md](file:///Users/peng/Me/Ai/skills-agent/docs/design/skill-registry.md)。

- 输入：skill roots
- 输出：SkillIndex（元数据集合）
- 约束：启动阶段只返回元数据，不读取正文

### 11.2 Skill Loader

参见：[skill-loader.md](file:///Users/peng/Me/Ai/skills-agent/docs/design/skill-loader.md)。

- `load_skill_body(skill_ref) -> text`
- `load_resource(skill_ref, relative_path, section_hint?) -> excerpt/summary`
- 约束：路径必须在技能目录内；正文/资源大小受控

### 11.3 Tools Runtime

参见：[tools-runtime.md](file:///Users/peng/Me/Ai/skills-agent/docs/design/tools-runtime.md)。

- `run_script(skill_ref, relative_path, args) -> exec_summary`
- 约束：权限合并、审批、超时、输出截断、审计

### 11.4 Model Adapter

参见：[model-adapter.md](file:///Users/peng/Me/Ai/skills-agent/docs/design/model-adapter.md)。

- `chat(messages, schema) -> structured_action`
- 约束：结构化解析、重试、纠错与 MockModel 支持
