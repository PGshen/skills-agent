# Agent Core 设计

**状态**：Phase A（ReAct 主循环 + MockModel）→ Phase B（脚本执行 + 死循环检测 + 上下文裁剪）分阶段实现

---

## 1. 设计目标

Agent Core 是整个系统的中枢编排器（orchestrator），职责是把"模型决策、技能系统、工具执行、流式输出、审计落盘"串成一个可控的执行流水线：

| 职责 | 说明 |
|------|------|
| 决策编排 | 驱动 ReAct 循环（Reason → Act → Observe → Repeat）|
| 技能管理 | 调用 SkillRegistry 获取索引、调用 SkillLoader 按需加载 |
| 工具执行 | 委派 ToolsRuntime 执行受控操作 |
| 输出推送 | 通过 OutputSink 接口推送进度与流式答案 |
| 审计落盘 | 通过 EventLogger 将所有关键事件写入 `events.jsonl` |

**不在 Agent Core 内处理**：
- 技能发现与元数据解析（Skill Registry 负责）
- 文件读取与路径安全（Skill Loader / Tools Runtime 负责）
- 模型 HTTP 通信（Model Adapter 负责）
- 终端/SSE 渲染（OutputSink 实现类负责）

---

## 2. ReAct 主循环

### 2.1 为什么是 ReAct + 结构化动作

本系统选择 **ReAct 风格主循环 + 结构化动作输出**，而非自由文本指令驱动工具。两者的本质区别：

| 方案 | 模型输出 | 工具触发方式 | 可控性 |
|------|----------|-------------|--------|
| 自由文本 | "请执行 fill.py" | 模型文本被解析/猜测 | 低（解析不稳定）|
| **结构化动作** | `{"type": "run_script", "params": {...}}` | JSON 校验后委派 | 高（可校验、可审计）|

**ReAct 三阶段**：

- **Reason**（思考/规划）：模型根据用户请求、当前状态与技能索引，决定下一步行动，可在此更新 Plan
- **Act**（动作）：输出单个结构化 Action（`load_skill` / `load_resource` / `run_script` / `update_plan` / `final_answer`）
- **Observe**（观察）：Agent Core 执行 Action，将结果摘要（observation）注入下一轮上下文

**关键约束**：
- **每轮单动作**：模型每轮只产出一个 Action（可同时含 UPDATE_PLAN），避免并发执行带来的状态混乱
- **观察不是模型调用**：Observe 是 Agent Core 执行动作后的结果，注入上下文后进入下一轮 Decide
- **模型不能直接执行工具**：必须通过结构化动作，经 Agent Core 校验后委派 Skill Loader / Tools Runtime

### 2.2 状态机

```
[*] → Init
Init → IndexSkills       (SkillRegistry.scan() → SkillMetadata 列表)
IndexSkills → Decide

Decide → UpdatePlan      (action.type == update_plan)
Decide → LoadSkill       (action.type == load_skill)
Decide → LoadResource    (action.type == load_resource)
Decide → RunScript       (action.type == run_script)
Decide → Final           (action.type == final_answer)

UpdatePlan → Decide      (Plan 更新后继续下一轮)
LoadSkill  → Observe
LoadResource → Observe
RunScript  → Observe

Observe → Decide         (observation 注入上下文，进入下一轮)
Final → [*]
```

### 2.3 执行时序

```
初始化
  │
  ├── SkillRegistry.scan()     → SkillMetadata 列表
  ├── 构造 AgentState           (session_id, user_input)
  └── EventLogger.emit(SESSION_START)
  │
  ▼
Turn 循环（max_turns 限制）
  │
  ├── ContextBuilder.build()   → messages（system + 技能索引 + history）
  ├── ModelAdapter.next_action(messages)  → Action
  ├── 死循环检测（动作哈希 + Plan 进度）
  │
  ├── Action 路由：
  │     UPDATE_PLAN   → state.plan.replace(new_plan); sink.on_plan_updated()
  │     LOAD_SKILL    → SkillLoader.load_body() → 注入 active_skills
  │     LOAD_RESOURCE → SkillLoader.load_resource() → 注入 observation
  │     RUN_SCRIPT    → ToolsRuntime.run_script() → 注入 observation
  │     FINAL_ANSWER  → sink.on_text_chunk(answer, done=True); 退出循环
  │
  └── EventLogger.emit(ACTION_COMPLETED / ACTION_FAILED)
  │
  ▼
循环结束（FINAL_ANSWER / 预算耗尽 / 死循环）
  └── EventLogger.emit(SESSION_END); sink.on_session_end()
```

---

## 3. Plan 生命周期

Plan 是 ReAct 循环中的"显式可审计状态"，记录目标分解与执行进度，使多轮推进稳定可控。

### 3.1 Plan 结构

```json
{
  "goal": "填写并提交 PDF 表单",
  "steps": [
    {"id": "s1", "description": "加载 pdf-form-filler 技能",   "status": "done"},
    {"id": "s2", "description": "执行 fill.py 脚本填写表单",   "status": "in_progress"},
    {"id": "s3", "description": "验证结果并输出报告",           "status": "pending"}
  ],
  "created_at": 1700000000.0,
  "updated_at": 1700000050.0
}
```

### 3.2 创建时机

**第一轮 Decide（IndexSkills 完成后）**：提示词要求模型同时产出：
1. 初始 Plan（可粗粒度，通常 2-4 步）
2. 第一个动作（通常是 `update_plan` + `load_skill`）

模型在第一轮既完成规划又给出首步动作，避免需要一轮"纯规划轮"带来的预算浪费。

### 3.3 更新时机与更新方式

**更新发生在每次 Observe 之后的下一轮 Decide 中**：

```
执行 Action → 收集 Observation → 注入上下文 → 下一轮 Decide
                                                    │
                                             模型可在此输出
                                         action.type == update_plan
                                         (含完整新 Plan)
```

**MVP 阶段：仅支持全量替换（replace）**

模型在响应中返回完整的新 Plan 对象，Agent Core 用 `Plan.replace()` 覆盖当前 Plan：

```python
# Plan.replace() 实现
def replace(self, new_plan: "Plan") -> "Plan":
    return Plan(
        goal=self.goal,          # 保留原始 goal
        steps=new_plan.steps,    # 替换所有 steps
        created_at=self.created_at,
        updated_at=time.time(),
    )
```

增量 patch 模式（仅更新部分 steps）列为后续增强项。原因：
- patch 格式要求模型稳定产出语义正确的变更描述（op/path/value）
- Agent Core 需实现合并逻辑与冲突处理
- MVP 阶段不引入此复杂度，全量替换已满足需求

**Plan 更新触发 OutputSink 通知**：

```python
state.plan = state.plan.replace(new_plan)
self._sink.on_plan_updated(state.plan)   # → CLISink 打印步骤列表
self._logger.emit(EventType.PLAN_UPDATED, state.plan.model_dump())
```

### 3.4 Plan 进度追踪（死循环检测辅助）

每次 Plan 有步骤状态变化时，记录当前 turn_count：

```python
# Agent Core 在每轮 Decide 后检查 Plan 进度
old_step_statuses = {s.id: s.status for s in state.plan.steps}
# ... 执行 Action ...
new_step_statuses = {s.id: s.status for s in state.plan.steps}
if old_step_statuses != new_step_statuses:
    state.last_plan_progress_turn = state.turn_count
```

如果连续 `dead_loop_stall_turns`（默认 4）轮无步骤状态变化，触发预警事件（见第 5 节）。

### 3.5 Plan 持久化与崩溃恢复

**持久化时机**：每次 Plan 更新后，立即将 AgentState 写入 `.agent/runs/<session_id>/state.json`。

**崩溃恢复策略（compress-rebuild）**：

Agent 启动时若检测到未完成的 run（state.json 中 status != completed），可通过 `--resume <session-id>` 恢复：

```
恢复流程：
  1. 从 state.json 加载 AgentState（含 Plan + active_skills）
  2. 从 events.jsonl 提取已执行动作序列与 observation 摘要
  3. 构造"恢复摘要消息"注入上下文：
     {"role": "user", "content":
       "<resume>已完成步骤：s1（加载技能），s2（执行脚本，退出码=0）
        当前状态：s3 待执行
        请基于以上摘要继续推进任务。</resume>"}
  4. 继续 ReAct 循环
```

**为什么用压缩重建而不是精确重建**：精确重放需要完整保存每轮的 messages 原文，存储成本高且实现复杂。压缩重建用事件流摘要重构语义，对模型来说足够推进任务。

---

## 4. AgentCore 接口（概要）

```python
# src/agent/core.py
from ..skills.registry import SkillRegistry
from ..skills.loader import SkillLoader
from ..model.base import ModelAdapter
from ..tools.runtime import ToolsRuntime
from ..output.sink import OutputSink, NullSink
from ..agent.events import EventLogger
from .state import AgentState
from .plan import Plan, Action, ActionType, StepStatus
import hashlib, json

class AgentCore:
    def __init__(
        self,
        model: ModelAdapter,
        registry: SkillRegistry,
        loader: SkillLoader,
        event_logger: EventLogger,
        tools: ToolsRuntime = None,     # Phase B 引入
        sink: OutputSink = None,
        max_turns: int = 20,
        dead_loop_window: int = 6,      # 检测窗口（动作 hash 队列长度）
        dead_loop_stall_turns: int = 4, # Plan 无进展多少轮触发警告
    ):
        self._model = model
        self._registry = registry
        self._loader = loader
        self._logger = event_logger
        self._tools = tools
        self._sink = sink or NullSink()
        self._max_turns = max_turns
        self._dead_loop_window = dead_loop_window
        self._dead_loop_stall_turns = dead_loop_stall_turns

    def run(
        self,
        user_input: str,
        history_messages: list[dict] = None,
    ) -> str:
        """
        执行完整 ReAct 循环，返回最终答案字符串。

        history_messages：由 SessionContext.build_history_messages() 生成（chat 模式）。
        单次模式（run 命令）：不传 history_messages，等同于 []。
        """
```

---

## 5. 上下文组装（ContextBuilder）

### 5.1 消息层次

```
messages = [
  # ── 系统层（每轮重建）────────────────────────────────────────────
  {"role": "system", "content": SYSTEM_PROMPT + skill_index + plan_summary},

  # ── 历史摘要层（chat 模式，有 compressed_summary 时）──────────────
  {"role": "user", "content": "<summary>历史摘要：...</summary>"},

  # ── 近期原文层（chat 模式，最近 K 轮）────────────────────────────
  {"role": "user",      "content": "第 N-1 轮用户输入"},
  {"role": "assistant", "content": "第 N-1 轮助手回复"},

  # ── 任务内 ReAct 历史（本次任务的 act/observe 交替）──────────────
  {"role": "assistant", "content": json.dumps(prev_action)},
  {"role": "user",      "content": "Observation: <result>"},

  # ── 当前层──────────────────────────────────────────────────────
  {"role": "user", "content": user_input},
]
```

### 5.2 系统 Prompt 结构

```python
SYSTEM_PROMPT_TEMPLATE = """
你是一个 AI Agent，通过 ReAct 循环（推理 → 行动 → 观察）完成用户任务。

## 可用技能索引
{skill_index}

## 当前计划
{plan_summary}

## 行动协议
每次回复必须是一个 JSON 对象，格式：
{
  "action": {
    "type": "<load_skill|load_resource|run_script|update_plan|final_answer>",
    "params": { ... }
  }
}

## 约束
- 每次只能输出一个 action
- load_resource / run_script 必须在 load_skill 之后
- update_plan 中的 plan 为完整新 Plan（全量替换）
- final_answer 时输出完整最终答案
"""
```

### 5.3 技能索引注入格式（模型可见层）

```
Available Skills:
- name=pdf-form-filler | source=project | description=Extract and fill PDF form fields
- name=code-review     | source=user    | description=Review code with team standards
```

### 5.4 上下文裁剪（Phase B）

触发条件：当估算 token 数接近 `max_context_tokens`（默认阈值 80%）时：

```python
def _trim_to_limit(self, msgs: list[dict], max_tokens: int) -> list[dict]:
    """
    策略（由宽到严）：
    1. 截断 observation 内容（保留前 500 字符）
    2. 丢弃最老的 ReAct 历史轮（保留最近 3 轮）
    3. 截断已加载技能正文（保留摘要行）
    """
```

**不可丢弃**：user_input（当前用户输入）、Plan 摘要（goal + current_step）、技能索引

---

## 6. 死循环检测（Phase B）

双重机制：主要机制快速响应，辅助机制提前预警。

### 6.1 动作哈希检测（主要机制）

```python
def _check_dead_loop_hash(self, action: Action, state: AgentState) -> bool:
    """
    维护最近 K 轮动作的 (type + key_params) 哈希队列。
    若检测到同一哈希出现 >= 2 次则判定死循环。
    """
    key = json.dumps({"type": action.type, "params": action.params}, sort_keys=True)
    h = hashlib.sha256(key.encode()).hexdigest()[:16]

    state.recent_action_hashes.append(h)
    if len(state.recent_action_hashes) > self._dead_loop_window:
        state.recent_action_hashes.pop(0)

    # 若窗口内同一哈希出现 >= 2 次
    return len(state.recent_action_hashes) != len(set(state.recent_action_hashes))
```

### 6.2 Plan 进展检测（辅助机制）

```python
def _check_dead_loop_stall(self, state: AgentState) -> bool:
    """
    检查最近 N 轮 Plan 中是否有步骤状态变化。
    无变化则产出告警事件（不中止，仅预警）。
    """
    if state.turn_count - state.last_plan_progress_turn >= self._dead_loop_stall_turns:
        self._logger.emit(EventType.DEAD_LOOP_DETECTED, {
            "reason": "plan_stall",
            "turns_without_progress": state.turn_count - state.last_plan_progress_turn,
        })
        return True
    return False
```

### 6.3 触发后处理

```python
if self._check_dead_loop_hash(action, state):
    state.dead_loop_triggered = True
    self._sink.on_error("Dead loop detected: repeated action", recoverable=False)
    self._logger.emit(EventType.DEAD_LOOP_DETECTED, {"reason": "repeated_action"})
    break  # 退出 ReAct 循环，输出降级答复
```

---

## 7. Action 执行路由

```python
def _execute_action(self, action: Action, state: AgentState) -> str:
    """执行单个 Action，返回 observation 字符串注入下一轮 context。"""

    if action.type == ActionType.UPDATE_PLAN:
        new_plan = Plan.model_validate(action.params["plan"])
        state.plan = state.plan.replace(new_plan) if state.plan else new_plan
        self._sink.on_plan_updated(state.plan)
        self._logger.emit(EventType.PLAN_UPDATED, state.plan.model_dump())
        return "Plan updated."

    elif action.type == ActionType.LOAD_SKILL:
        skill_name = action.params["skill_name"]
        meta = self._registry.find(skill_name)
        if not meta:
            return f"Error: skill '{skill_name}' not found."
        body, report = self._loader.load_body(meta)
        state.active_skills.append(meta)
        self._sink.on_progress("load_skill", skill_name)
        self._logger.emit(EventType.SKILL_LOADED, {"skill": skill_name, **report})
        return f"[Skill: {skill_name}]\n{body}"

    elif action.type == ActionType.LOAD_RESOURCE:
        skill_name = action.params["skill_name"]
        resource = action.params["resource"]
        meta = self._registry.find(skill_name)
        excerpt, report = self._loader.load_resource(meta, resource,
                          section_hint=action.params.get("section_hint"))
        self._sink.on_progress("load_resource", resource)
        return f"[Resource: {resource}]\n{excerpt}"

    elif action.type == ActionType.RUN_SCRIPT:
        # Phase B: 委派 ToolsRuntime
        if not self._tools:
            return "Error: script execution not enabled in this phase."
        result = self._tools.run_script(
            skill_name=action.params["skill_name"],
            script=action.params["script"],
            args=action.params.get("args", []),
        )
        self._sink.on_observation("script", result.get("stdout", "")[:300])
        return f"Script exit_code={result['exit_code']}\n{result.get('stdout', '')[:500]}"

    elif action.type == ActionType.FINAL_ANSWER:
        content = action.params.get("content", "")
        self._sink.on_text_chunk(content, done=True)
        self._logger.emit(EventType.FINAL_ANSWER, {"content": content})
        return content  # 调用方检测此 action type 后退出循环

    else:
        return f"Error: unknown action type '{action.type}'."
```

---

## 8. 预算与退出条件

### 8.1 预算参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `max_turns` | 20 | 最大 ReAct 循环轮数 |
| `max_tool_calls` | 30 | 最大工具调用次数（Phase B）|
| `max_script_executions` | 10 | 最大脚本执行次数（Phase B）|
| `max_context_tokens` | 100000 | 上下文 token 估算上限 |

### 8.2 退出条件

| 条件 | 说明 | 输出 |
|------|------|------|
| FINAL_ANSWER | 正常完成 | 最终答案 |
| max_turns 耗尽 | 轮次超限 | 降级答复（包含已完成部分）|
| 死循环检测触发 | 动作重复 | 降级答复 + 错误事件 |
| 连续失败 | 同一动作失败 >= 3 次 | 降级答复 |

---

## 9. 错误处理策略

| 错误类型 | 处理方式 |
|----------|----------|
| 模型输出非法 JSON | Model Adapter 内部重试（最多 2 次），仍失败则作为 observation |
| Skill 不存在 | 返回 error observation，模型在下一轮更新 Plan |
| 路径越界 | 返回 PathTraversalBlocked observation，模型改路径 |
| 脚本超时 | 返回 timeout observation，模型走失败分支 |
| 脚本非 0 退出 | 返回 exit_code + stdout/stderr 摘要，模型决策 |
| 权限拒绝 | 返回 ToolNotAllowed observation，模型请求只读替代方案 |

---

## 10. 审计落盘策略

运行目录：`.agent/runs/<session_id>/`

| 文件 | 内容 | 说明 |
|------|------|------|
| `events.jsonl` | 所有 Event（追加写） | 事实来源，支持回放 |
| `state.json` | AgentState 快照 | 每轮覆盖写（最终状态）|
| `final.md` | 最终答案 | 任务完成时写入 |
| `observations/` | 大块工具输出 | events 中存引用路径 |

**核心原则**：大块内容（脚本 stdout > 500 字符）不直接写进 events，落盘到 `observations/` 并在 event.data 存 `storage_ref` + `sha256`。

---

## 11. 与其他子系统的接口

| 子系统 | 调用方式 | 关键约束 |
|--------|----------|----------|
| SkillRegistry | `registry.scan()` → `list[SkillMetadata]` | 只返回元数据，不读正文 |
| SkillRegistry | `registry.find(name)` → `SkillMetadata` | |
| SkillLoader | `loader.load_body(meta)` → `(str, dict)` | 正文大小受限，去除 YAML 前言 |
| SkillLoader | `loader.load_resource(meta, path)` → `(str, dict)` | 路径必须在技能目录内 |
| ModelAdapter | `model.next_action(messages)` → `Action` | 每轮返回单个 Action |
| ToolsRuntime | `tools.run_script(skill_name, script, args)` | Phase B，权限校验 + 审批 |
| OutputSink | `sink.on_XXX()` 系列方法 | 不可抛出异常 |
| EventLogger | `logger.emit(event_type, data)` | 追加写 JSONL |

参见：[skill-registry.md](skill-registry.md) / [skill-loader.md](skill-loader.md) / [model-adapter.md](model-adapter.md) / [tools-runtime.md](tools-runtime.md) / [streaming-output.md](streaming-output.md)

---

## 12. 验收标准

### Phase A（核心链路，MockModel 驱动）

- [ ] MockModel 驱动 AgentCore 完成 3 轮 ReAct 循环（LOAD_SKILL → LOAD_RESOURCE → FINAL_ANSWER）
- [ ] `events.jsonl` 包含 SESSION_START、ACTION_REQUESTED × 3、SESSION_END 等完整事件序列
- [ ] `CLISink` 可见进度输出（stderr），最终答案输出到 stdout
- [ ] `AgentState.is_done()` 在 FINAL_ANSWER 后返回 True
- [ ] UPDATE_PLAN 动作正确触发 `Plan.replace()`，`goal` 保留，`steps` 被新值覆盖
- [ ] `events.jsonl` 中 PLAN_UPDATED 事件包含完整新 Plan 数据

### Phase B（脚本执行 + 稳定性）

- [ ] `RUN_SCRIPT` 动作通过 ToolsRuntime 执行，超时被正确中止
- [ ] 死循环检测：同一动作重复 2 次后 `dead_loop_triggered=True`，ReAct 循环终止
- [ ] Plan 进展检测：连续 4 轮无进展时 `DEAD_LOOP_DETECTED` 事件写入 events.jsonl
- [ ] 上下文超限时 `_trim_to_limit` 正确裁剪，不丢弃 user_input 和 Plan goal
