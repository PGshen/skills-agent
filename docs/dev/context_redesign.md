# Context Management 重新设计

## 一、背景与现状

### 1.1 原始架构（单一 ReAct 循环）

`ContextBuilder`（`src/agent/context.py`）最初为**单一 ReAct Agent** 设计，负责将所有信息拼装成一次 LLM 调用的 `messages` 列表：

```
[system] ← 工具列表 + 当前计划摘要 + Skill 索引
[user/assistant...] ← SessionContext 历史
[user] ← 当前用户输入
[assistant/user...] ← ReAct turn 历史 (action, observation)
[user] ← (可选) stall injection
```

trimming 策略分三个梯度（从轻到重）：
1. 截断 Observation 内容（保留前 500 字符）
2. 丢弃最旧的 ReAct 轮次（保留最近 3 对）
3. 截断 Skill body 内容（保留首行）

### 1.2 升级后架构（三层 Multi-Agent）

最新实现（`9fa7f85`）将单一 Agent 拆分为三类专职角色：

```
EntryAgent (AgentCore)
  ├─ 一次分类调用 → SIMPLE | COMPLEX
  ├─ SIMPLE → ReactAgent (直接执行)
  └─ COMPLEX → OrchestratorAgent
                 ├─ Decompose 阶段（一次模型调用）
                 ├─ Execute 阶段（每步 → ReactAgent）
                 └─ Synthesize 阶段（一次模型调用）
```

## 二、存在的问题

### 2.1 ContextBuilder 事实上已成孤儿

三个新 Agent 各自在代码中 **内联拼装 messages**，没有复用 `ContextBuilder`：

| Agent | 当前做法 |
|-------|---------|
| `core.py` `_classify()` | `[{user: classify_prompt}]` 直接内联 |
| `core.py` `_run_simple()` | 构造 SubTask，委托给 ReactAgent |
| `orchestrator.py` `_decompose()` | `list(history_messages) + [{user: prompt}]` 内联 |
| `orchestrator.py` `_synthesize()` | 同上 |
| `react_agent.py` `_build_messages()` | 独立方法，**不调用 ContextBuilder** |

原始 `ContextBuilder.build()` 的签名接受 `AgentState`（包含 `plan`、`active_skills` 等），这些字段只在旧单一 Agent 中有意义，与新架构不兼容。

### 2.2 token 预算一刀切

当前全局 100K token 上限，但各角色的实际需求差异巨大：

| 角色 | 典型 token 需求 | 瓶颈 |
|------|--------------|------|
| 分类调用 | < 1K | 无 |
| Orchestrator Decompose | 2–8K | session history |
| Orchestrator Synthesize | 8–32K | step results 汇总 |
| ReactAgent 执行循环 | 10–100K | react turn 历史 + skill body |

### 2.3 Session History 传递不透明

`history_messages` 由调用方透传，Orchestrator 将其原样放入 decompose 和 synthesize 的 messages 中，但：
- 没有针对各角色做过滤（synthesize 时旧 history 占据宝贵 token）
- ReactAgent 完全**不接收** session history（只看当前 SubTask.context 字符串），导致用户的多轮偏好（如「用中文回复」）不能传达给执行层

### 2.4 截断策略存在信息丢失风险

旧 `_trim_to_limit` 直接截断 Observation 和 Skill body，可能丢弃后续步骤所依赖的关键信息。应以**压缩（语义提炼）**为主，截断只作为不得已的最后保底。

### 2.5 缺乏对 ReactAgent 历史的压缩能力

ReAct 循环中，随着轮次增加，历史 messages 不断增长，旧实现只能粗暴丢弃最旧的 react 对，而无法保留其语义摘要。

---

## 三、设计决策

| 编号 | 问题 | 决策 |
|------|------|------|
| D-1 | ClassifyContextBuilder 是否独立成类 | **是**，统一在一处管理上下文，逻辑更清晰 |
| D-2 | ReactAgent 如何感知 session history | Orchestrator 在构建 `SubTask.context` 时**注入关键历史摘要** |
| D-3 | history_max_tokens 默认值 | **64K**（现代模型上下文普遍较大） |
| D-4 | 截断 vs 压缩 | **以压缩为主**，Builder 不主动截断；仅在压缩后仍超预算时，记录警告并放行（由上游保证） |

---

## 四、新设计方案

### 4.1 核心思路：角色专用的 ContextBuilder

用**三个专用 Builder** 替代当前单一 `ContextBuilder`，共享一个 `ContextUtils` 工具层：

```
ContextUtils                      ← 无状态工具（token 估算）
  │
  ├─ ClassifyContextBuilder       ← EntryAgent 分类调用
  ├─ OrchestratorContextBuilder   ← Decompose / Synthesize
  └─ ReactContextBuilder          ← ReactAgent 执行循环（含历史压缩）
```

### 4.2 各 Builder 职责与消息结构

#### A. `ClassifyContextBuilder`

**用途**：EntryAgent 一次性分类调用

**消息结构**：
```
[user] classify_prompt(user_input)
```

- 不需要 system prompt（分类 prompt 本身已包含 few-shot 示例）
- Session history 不注入（分类决策不依赖历史）
- 不需要 trimming，输入规模天然受控

**接口**：
```python
class ClassifyContextBuilder:
    def build(self, user_input: str) -> list[dict]:
        ...
```

---

#### B. `OrchestratorContextBuilder`

**用途**：Orchestrator 的 Decompose 和 Synthesize 两次调用，以及构建传递给 ReactAgent 的 SubTask.context

**Decompose 消息结构**：
```
[system] orchestrator_system_prompt
[user/assistant...] ← session_history（由上游 ConversationCompressor 保证已压缩）
[user] decompose_prompt(user_input)
```

**Synthesize 消息结构**：
```
[system] orchestrator_system_prompt
[user/assistant...] ← session_history（同上）
[user] synthesize_prompt(user_input, step_results_text)
```

- session_history 由 `SessionContext.build_history_messages()` 提供，Builder 信任其已压缩，直接注入
- step_results_text 为所有 `TaskResult.output` 的原文拼接，**不截断**；若上游 ReactAgent 的 final_answer 内容过长，应由 ReactAgent 自身负责输出精炼摘要

**`build_subtask_context()` — 历史摘要注入**：

为了让 ReactAgent 感知用户的多轮偏好，Orchestrator 在构建每步 SubTask 时，调用此方法将 session history 注入 `SubTask.context`。

**重要前提**：`SessionContext.compressed_summary` 只是"已被移出 `recent_turns` 的旧轮摘要"，**不包含最近 K 轮**的内容（`recent_turns` 是独立字段）。用户的最新偏好（如「用中文回复」）通常在 `recent_turns` 里而非 `compressed_summary` 里。因此，不能只注入 `compressed_summary`，必须将两者合并：

```python
def build_subtask_context(
    self,
    step: Step,
    prior_results: list[TaskResult],
    session: SessionContext,           # 传整个 SessionContext，而非单一字段
) -> str:
    """
    从 session 提炼用户偏好文本，注入 SubTask.context。

    策略：调用 session.build_history_messages()，将其内容（compressed_summary
    + recent_turns 最后 K 轮）转换为纯文本字符串，而非 messages 结构。
    这样可以同时覆盖旧摘要和最新偏好。

    返回格式示例：

    [Session context]
    历史对话摘要：用户要求使用中文回复，代码风格遵循 PEP 8。
    User: 请帮我实现 quicksort
    Assistant: 已实现并保存到 quicksort.py

    [Completed steps so far]
      [✓] Step 1: 实现了 quicksort 函数（见 quicksort.py）
    """
```

实现方式：复用 `session.build_history_messages()` 的输出，将每条消息的 `content` 字段按 `role` 拼接为纯文本，注入到 SubTask.context 的 `[Session context]` 区块中。若 `build_history_messages()` 返回空列表，则跳过该区块。

**接口**：
```python
class OrchestratorContextBuilder:
    def __init__(self, max_context_tokens: int = 64_000): ...

    def build_decompose(
        self,
        user_input: str,
        history_messages: list[dict],
    ) -> list[dict]: ...

    def build_synthesize(
        self,
        user_input: str,
        results: list[TaskResult],
        history_messages: list[dict],
    ) -> list[dict]: ...

    def build_subtask_context(
        self,
        step: Step,
        prior_results: list[TaskResult],
        history_summary: str,
    ) -> str: ...
```

---

#### C. `ReactContextBuilder`

**用途**：ReactAgent 每轮执行循环的 messages 拼装

**消息结构**：
```
[system] react_system_prompt(goal, task_description, context, tools_section)
[assistant/user...] ← react_turn_history (action, observation 交替)
[user] ← (可选) stall_injection
```

- **不注入 session history**：背景信息通过 `SubTask.context` 注入 system prompt，比原始 history 更精准
- `SubTask.context` 由 `OrchestratorContextBuilder.build_subtask_context()` 构建，已包含 session 偏好摘要和先前步骤摘要

**react 历史压缩策略**：

当 react 历史超出预算时，**不直接丢弃**，而是调用 `ReactHistoryCompressor` 将最旧的 N 对历史（action + observation）压缩为一条摘要消息，保留语义信息：

```
压缩前:
  [assistant] {"type": "read_file", ...}
  [user] Observation: <文件内容 3000 字>
  [assistant] {"type": "grep", ...}
  [user] Observation: <grep 结果 500 字>

压缩后:
  [user] [History summary] 已读取 config.py（包含 DB_URL、MAX_CONN 配置），
         并搜索了 "import" 关键字，找到 12 处引用。
```

压缩由 `ReactHistoryCompressor`（新增组件，位于 `src/session/react_compressor.py`）负责，接受历史对列表，返回一条 summary user 消息。

**接口**：
```python
class ReactContextBuilder:
    def __init__(
        self,
        max_context_tokens: int = 100_000,
        compressor: ReactHistoryCompressor | None = None,
    ): ...

    def build(
        self,
        task: SubTask,
        registry: SkillRegistry,
        react_history: list[tuple],
        available_tools: list[str],
        stall_injection: str | None = None,
    ) -> list[dict]: ...
```

当 `compressor` 为 `None` 时（测试场景），超出预算只记录警告，不做处理（MockModel 不会真正超限）。

---

### 4.3 消息流完整视图

```
用户输入
    │
    ▼
[ClassifyContextBuilder.build]
    [user: classify_prompt] → 模型 → SIMPLE / COMPLEX
    │
    ├── SIMPLE ──────────────────────────────────────────────────────────────────────┐
    │                                                                                │
    └── COMPLEX                                                                      │
            │                                                                        │
            ▼                                                                        │
  [OrchestratorContextBuilder.build_decompose]                                      │
      [sys][history_messages][decompose_prompt] → 模型 → Plan                       │
            │                                                                        │
            │ (per step)                                                             │
            ├─ [OrchestratorContextBuilder.build_subtask_context]                   │
            │      → SubTask.context（含 session 摘要 + 先前步骤摘要）              │
            │                                                                        │
            ▼                                                                        │
  [ReactContextBuilder.build]                                                        │
      [sys: task + context + tools][react_turns...] → 模型 → Action                 │
      ↑ (每轮循环，react_turns 增长；超预算时 ReactHistoryCompressor 压缩)          │
            │                                                                        │
            ▼                                                                        │
  [OrchestratorContextBuilder.build_synthesize]                                     │
      [sys][history_messages][synthesize_prompt(step_results)] → 模型               │
            │                                                                        │
            └────────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
                         Final Answer
```

### 4.4 Session History 分层管理

```
用户多轮对话
    │
    ▼
SessionContext
    ├── compressed_summary: str         ← ConversationCompressor 维护的语义压缩摘要
    └── recent_turns: list[ConversationTurn]  ← 最近 K 轮原文

    build_history_messages() →
        [user: <summary>...</summary>]  ← 压缩摘要（若存在）
        [user][assistant]...            ← 最近 K 轮原文
```

**各角色的 history 使用方式**：

| 角色 | 使用方式 |
|------|---------|
| ClassifyContextBuilder | 不使用 |
| OrchestratorContextBuilder | 直接注入 `build_history_messages()` 的输出 |
| ReactContextBuilder | 不直接使用；通过 `SubTask.context` 接收由 `build_subtask_context()` 提炼的纯文本（含 compressed_summary + recent_turns） |

**预算保证**：上游 `ConversationCompressor` 在 `history_max_tokens=64K` 阈值处主动触发压缩（Phase B 已实现），Builder 层信任此结果，不再重复截断。

### 4.5 压缩组件总览

| 组件 | 文件 | 职责 |
|------|------|------|
| `ConversationCompressor` | `src/session/compressor.py` | 跨轮次 session history 压缩（已存在） |
| `ReactHistoryCompressor` | `src/session/react_compressor.py` | 单次 ReAct 循环内的 react 历史压缩（**新增**） |

`ReactHistoryCompressor` 接口：
```python
class ReactHistoryCompressor:
    def compress(
        self,
        pairs: list[tuple[dict, dict]],   # (action_msg, observation_msg) 列表
    ) -> dict:                            # 返回一条 user role 的 summary 消息
        ...
```

---

## 五、接口变更摘要

### 保留

- `SessionContext.build_history_messages()` 格式不变
- `SubTask` / `TaskResult` 数据结构不变
- `_plan_summary()` 辅助函数（迁入 OrchestratorContextBuilder）
- `SkillRegistry.to_index_text()` 调用不变

### 移除

- `ContextBuilder`（整个类，不再使用）
- `ContextBuilder.build()` 对 `AgentState` 的依赖
- 所有基于字符数的 `obs_truncate_chars`、`min_react_turns` 截断参数

### 新增

```python
# src/agent/context.py（重写）

def estimate_tokens(text: str) -> int:
    """Rough estimate: char count / 4"""
    ...

class ClassifyContextBuilder:
    def build(self, user_input: str) -> list[dict]: ...

class OrchestratorContextBuilder:
    def __init__(self, max_context_tokens: int = 64_000): ...
    def build_decompose(self, user_input: str, history_messages: list[dict]) -> list[dict]: ...
    def build_synthesize(self, user_input: str, results: list[TaskResult],
                         history_messages: list[dict]) -> list[dict]: ...
    def build_subtask_context(self, step: Step, prior_results: list[TaskResult],
                              session: SessionContext) -> str: ...

class ReactContextBuilder:
    def __init__(self, max_context_tokens: int = 100_000,
                 compressor: "ReactHistoryCompressor | None" = None): ...
    def build(self, task: SubTask, registry: SkillRegistry,
              react_history: list[tuple], available_tools: list[str],
              stall_injection: str | None = None) -> list[dict]: ...

# src/session/react_compressor.py（新文件）

class ReactHistoryCompressor:
    def compress(self, pairs: list[tuple[dict, dict]]) -> dict: ...
```

---

## 六、实现计划

### Step 1：重写 `src/agent/context.py`

实现三个 Builder 和 `estimate_tokens`，删除旧 `ContextBuilder`。此步骤不改动任何调用方。

### Step 2：接入 `ReactContextBuilder`

将 `react_agent.py` 中的内联 `_build_messages()` 方法替换为 `ReactContextBuilder.build()`。单元测试可立即覆盖（`compressor=None` 模式）。

### Step 3：接入 `OrchestratorContextBuilder`

将 `orchestrator.py` 中 `_decompose`、`_synthesize` 的内联消息拼装替换为 Builder 调用；在 `_execute()` 循环中调用 `build_subtask_context()` 构建 SubTask.context，注入 `session.compressed_summary`。

### Step 4：接入 `ClassifyContextBuilder`

将 `core.py` 中 `_classify()` 的内联消息构建替换为 Builder 调用。

### Step 5：实现 `ReactHistoryCompressor`

在 `src/session/react_compressor.py` 中实现，并在 `ReactContextBuilder` 中集成。在 CLI 入口处注入真实 compressor（MockModel 测试保持 `compressor=None`）。

### Step 6：更新测试

为三个 Builder 补充单元测试，覆盖：
- 正常消息拼装结构
- `SubTask.context` 含历史摘要的注入格式
- react 历史超预算时 compressor 被调用（mock compressor 验证）
- `compressor=None` 时超预算只记录警告
