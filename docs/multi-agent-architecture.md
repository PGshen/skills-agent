# Multi-Agent 架构重构技术方案

## 一、核心问题诊断

当前 `AgentCore` 是单一循环，同一个模型实例既要负责"规划"又要负责"执行"，这在认知上就是矛盾的：

- 模型看到 `update_plan` 工具，自然倾向先把计划整理清楚再干活
- 系统提示中"计划"和"执行工具"混在同一层，没有强制约束角色边界
- `update_plan` 的 observation 无论怎么措辞，都无法真正让模型切换到执行模式

根本解法：**用代码强制执行角色分离**，而不是靠 prompt 劝说。

---

## 二、整体架构

```
                    ┌──────────────────────────────────────────┐
                    │           AgentCore (EntryAgent)          │
                    │  · 接收 user_input                        │
                    │  · 一次模型调用判断复杂度                   │
                    │  · 简单 → 直接回答                         │
                    │  · 复杂 → 委派给 OrchestratorAgent         │
                    └──────────────────────────────────────────┘
                                        │
                         ┌──────────────▼──────────────┐
                         │      OrchestratorAgent       │
                         │  · 一次模型调用生成计划        │
                         │  · 按序驱动 ReactAgent 执行   │
                         │  · 收集结果，处理失败/重规划   │
                         │  · 最终一次调用综合答案        │
                         └──────────────────────────────┘
                                   │     │     │
                            ┌──────▼─┐ ┌─▼──┐ ┌▼──────┐
                            │ReactA-1│ │R-2 │ │R-3    │   ← 各自独立的 ReAct 循环
                            │step描述│ │... │ │...    │   ← 只有执行工具，无 update_plan
                            └────────┘ └────┘ └───────┘
```

**三类 Agent 各司其职：**

| Agent | 职责 | 模型调用次数 | 可用 Actions |
|-------|------|------------|------------|
| `EntryAgent` (AgentCore) | 分类路由 | 1次分类 + 可能1次直接回答 | 无工具，纯文本 |
| `OrchestratorAgent` | 规划 + 综合 | 1次分解 + 1次综合 | 无执行工具，只 re-plan |
| `ReactAgent` | 原子任务执行 | N 次 ReAct 循环 | 全部执行工具，**无 update_plan** |

---

## 三、公共接口设计（新增数据结构）

新建 `src/agent/multi_agent.py`，只包含三个 Agent 之间传递数据的 Pydantic 模型：

```python
class TaskComplexity(str, Enum):
    SIMPLE = "simple"    # 直接回答，无需工具
    COMPLEX = "complex"  # 需要规划 + 工具

class SubTask(BaseModel):
    step_id: str
    description: str          # 该步骤的目标（精炼过的）
    context: str = ""         # 上游步骤的结果摘要（由 Orchestrator 注入）
    goal: str = ""            # 原始用户目标（用于 ReactAgent 理解大背景）

class TaskResult(BaseModel):
    step_id: str
    success: bool
    output: str               # 自然语言描述结果（给 Orchestrator 看）
    artifacts: list[str] = [] # 产生的文件路径等副产物
```

**这三个数据结构只在 `agent/` 层内部流转，不暴露给 CLI 或其他模块。**

---

## 四、ReactAgent（原子执行器）

### 4.1 职责

接受一个 `SubTask`，用 ReAct 循环把它执行完毕，返回 `TaskResult`。

**关键约束：**
- Action 集合中**不包含 `update_plan`**（从 prompt 和执行逻辑中都移除）
- 不维护 `Plan` 对象，只有 `ReactState`（精简版状态）
- `final_answer` 不做计划完整性检查

### 4.2 系统提示（精简聚焦）

```
You are an executor agent. Your ONLY job is to complete this ONE task:

TASK: {task.description}

BACKGROUND CONTEXT (from completed steps):
{task.context}

OVERALL GOAL: {task.goal}

Rules:
- Use tools to produce real output NOW. No planning, no reflection.
- Call final_answer when the task is done. Content must be a concise result summary.
- If you cannot complete the task, call final_answer with "FAILED: <reason>".

Available tools:
{tools_section}
```

注意：没有 `update_plan` 声明，没有"计划进度"部分，第一句就是"你唯一的工作是完成这一个任务"。

### 4.3 `final_answer` 解析为 `TaskResult`

```python
content = action.params.get("content", "")
success = not content.startswith("FAILED:")
return TaskResult(
    step_id=state.task.step_id,
    success=success,
    output=content,
)
```

---

## 五、OrchestratorAgent（规划调度器）

### 5.1 职责

- **阶段 A（分解）**：一次模型调用，生成 `Plan`
- **阶段 B（循环执行）**：按序取 pending step，创建 `ReactAgent` 执行，收集 `TaskResult`
- **阶段 C（综合）**：一次模型调用，基于所有步骤结果生成最终答案

整个过程中，**模型不做任何执行决策**；`ReactAgent` 决策工具调用；`OrchestratorAgent` 代码决策步骤顺序。

### 5.2 数据结构

```python
class OrchestratorState(BaseModel):
    session_id: str
    user_input: str
    plan: Optional[Plan] = None
    results: list[TaskResult] = Field(default_factory=list)
    status: str = "running"
```

### 5.3 内部流程

```
run():
  1. _decompose(user_input) -> Plan
     · 构造规划 prompt，调用 model.next_action()
     · 期望返回 Action(type=UPDATE_PLAN, params={plan: ...})
     · 若解析失败，降级为单步计划
     · sink.on_plan_updated(plan)

  2. while plan.current_step() is not None:
       step = plan.current_step()
       step.status = IN_PROGRESS
       sink.on_subtask_start(step.id, step.description)

       context = _build_step_context(results_so_far)
       subtask = SubTask(step_id, description, context, goal=user_input)

       react = ReactAgent(model, registry, loader, ...)
       result = react.run(subtask)
       results.append(result)

       step.status = DONE if result.success else FAILED
       sink.on_subtask_done(step.id, result.success, result.output[:100])

       if run_dir: save_orchestrator_state(run_dir, state)

  3. final = _synthesize(user_input, results) -> str
     · 构造综合 prompt（包含每步结果摘要）
     · 期望返回 Action(type=FINAL_ANSWER, params={content: ...})
     · return content
```

### 5.4 规划 prompt 设计

```
You are a task planner. Decompose the user's request into a list of atomic steps.

Each step must be:
- A single concrete action (write ONE file, run ONE command, search for ONE thing)
- Independently executable
- Specific enough that an executor can complete it without further clarification

Output format (JSON only):
{
  "type": "update_plan",
  "params": {
    "plan": {
      "goal": "<user's goal>",
      "steps": [
        {"id": "1", "description": "<atomic step>", "status": "pending"},
        ...
      ]
    }
  }
}

User request: {user_input}
```

### 5.5 步骤上下文构建

```python
def _build_step_context(self, results: list[TaskResult]) -> str:
    if not results:
        return ""
    lines = ["Completed steps so far:"]
    for r in results:
        status = "✓" if r.success else "✗"
        lines.append(f"  [{status}] Step {r.step_id}: {r.output[:200]}")
    return "\n".join(lines)
```

### 5.6 综合 prompt 设计

```
You are a result synthesizer. Given the user's original request and the results
of each completed step, write a comprehensive final answer.

User request: {user_input}

Step results:
{formatted_results}

Write a clear, complete response to the user. Include relevant outputs, file paths,
or summaries as appropriate.

Output: {"type": "final_answer", "params": {"content": "<your answer>"}}
```

---

## 六、AgentCore（EntryAgent，保持外部接口不变）

### 6.1 接口保持完全兼容

`AgentCore.__init__()` 和 `AgentCore.run()` 签名**一字不改**。CLI 代码零改动。

### 6.2 内部实现

```python
class AgentCore:
    def __init__(self, model, registry, loader, event_logger, tools=None, sink=None,
                 max_turns=20, dead_loop_window=6, dead_loop_stall_turns=4,
                 max_context_tokens=100_000, run_dir=None):
        # 保存所有参数（不变）
        # 新增：构建 OrchestratorAgent 实例
        self._orchestrator = OrchestratorAgent(
            model=model, registry=registry, loader=loader,
            event_logger=event_logger, tools=tools, sink=sink,
            max_turns_per_subtask=max_turns // 2,
            max_context_tokens=max_context_tokens,
            run_dir=run_dir,
        )

    def run(self, user_input, history_messages=None, initial_state=None) -> str:
        # 1. 分类（一次模型调用）
        complexity = self._classify(user_input, history_messages or [])
        # 2. 路由
        if complexity == TaskComplexity.SIMPLE:
            return self._direct_answer(user_input, history_messages or [])
        else:
            return self._orchestrator.run(user_input, history_messages=history_messages, ...)
```

### 6.3 分类 prompt

```
Classify the following user request as "simple" or "complex".

Simple: Can be answered immediately with general knowledge, no tools needed.
  Examples: "What is quicksort?", "Translate this sentence", "Explain this code"

Complex: Requires writing files, running code, searching the web, or multiple steps.
  Examples: "Write a quicksort script and save it", "Search X and summarize", "Build Y"

Output JSON only: {"type": "final_answer", "params": {"content": "simple"}}
                   or {"type": "final_answer", "params": {"content": "complex"}}

User request: {user_input}
```

容错：若解析失败，默认 `complex`（保守策略，宁可多做不少做）。

---

## 七、OutputSink 扩展（最小改动）

在 `src/output/sink.py` 中追加 3 个方法（带默认空实现，不破坏现有代码）：

```python
def on_subtask_start(self, step_id: str, description: str) -> None: ...
def on_subtask_done(self, step_id: str, success: bool, summary: str) -> None: ...
def on_route_decision(self, complexity: str) -> None: ...
```

---

## 八、文件改动清单

### 新建文件

| 文件 | 内容 |
|------|------|
| `src/agent/multi_agent.py` | `TaskComplexity`, `SubTask`, `TaskResult` |
| `src/agent/react_agent.py` | `ReactState`, `ReactAgent` |
| `src/agent/orchestrator.py` | `OrchestratorState`, `OrchestratorAgent` |

### 修改文件

| 文件 | 改动范围 |
|------|---------|
| `src/agent/core.py` | 完全重写内部实现，保持 `AgentCore.__init__` + `run()` 签名不变 |
| `src/output/sink.py` | 追加 3 个方法（空实现） |
| `src/agent/events.py` | 追加 EventType：`SUBTASK_START`, `SUBTASK_DONE`, `ROUTE_DECISION` |

### 零改动文件

- `src/cli/` — 不变
- `src/model/` — 不变
- `src/tools/` — 不变
- `src/session/` — 不变
- `src/skills/` — 不变
- `src/agent/plan.py` — 不变（Plan/Step/Action/ActionType 复用）
- `src/agent/state.py` — 不变（AgentState 仅用于 crash recovery 兼容层）

---

## 九、关键设计决策

| 决策 | 理由 |
|------|------|
| ReactAgent 不含 `update_plan` | 用代码约束代替 prompt 劝说，模型物理上无法规划 |
| Orchestrator 模型只做分解和综合 | 执行决策归 ReactAgent，Orchestrator 只做结构化调度 |
| 步骤串行执行 | 保持简单，避免文件冲突和上下文依赖问题 |
| 共用同一个 ModelAdapter 实例 | ModelAdapter 无状态，可安全复用 |
| ReactAgent.max_turns = max_turns // 2 | 原子任务快速失败，避免单步耗尽全局轮次 |

---

## 十、迁移路径

1. **Phase 1**：新建三个 Agent 文件 + 扩展 OutputSink/EventType，旧 `core.py` 保持不变 → 跑通现有测试
2. **Phase 2**：`core.py` 切换为委派给 `OrchestratorAgent`，更新单元测试
3. **Phase 3**：删除旧 `core.py` 中已废弃的方法，清理 `context.py`
