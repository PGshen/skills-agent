# 系统框架架构 设计

**状态**：本文档是全系统架构总览，随各子系统设计同步更新。

---

## 1. 系统定位

Agent Skills 系统是一个以"技能（Skill）"为核心组织单元的 AI Agent 框架。用户在 SKILL.md 中以 Markdown + YAML 前言的形式定义技能，Agent Core 通过 ReAct 循环驱动模型选择、加载、执行技能，最终完成用户任务。

**核心设计哲学**：
- **渐进式披露**：技能内容分三层按需注入模型上下文，避免上下文膨胀
- **控制层隔离**：`allowed_tools` 等安全控制字段永不发送给模型，防止提示注入
- **输出解耦**：OutputSink 接口使 Agent Core 与 CLI/HTTP 等输出方式完全解耦
- **审计优先**：所有执行行为以事件流形式落盘，支持回放与评估

---

## 2. 项目目录结构

```
skills-agent/
├── src/
│   ├── agent/                    # Agent Core 模块
│   │   ├── __init__.py
│   │   ├── core.py              # AgentCore 主类（ReAct 主循环）
│   │   ├── state.py             # AgentState 运行态状态
│   │   ├── plan.py              # Plan、Step、Action 数据结构
│   │   ├── events.py            # EventType、Event、EventLogger
│   │   └── context.py           # ContextBuilder（上下文组装）
│   │
│   ├── skills/                   # Skills 子系统
│   │   ├── __init__.py
│   │   ├── metadata.py          # SkillMetadata、ResourceLimits
│   │   ├── registry.py          # SkillRegistry（扫描与索引）
│   │   ├── loader.py            # SkillLoader（按需内容加载）
│   │   └── frontmatter.py       # YAML 前言解析（PyYAML safe_load）
│   │
│   ├── model/                    # Model Adapter 模块
│   │   ├── __init__.py
│   │   ├── base.py              # ModelAdapter 抽象基类
│   │   ├── mock.py              # MockModel（Phase A/B 测试驱动）
│   │   ├── anthropic.py         # AnthropicAdapter（Phase C）
│   │   └── streaming.py         # StreamingJSONParser（Phase C）
│   │
│   ├── tools/                    # Tools Runtime 模块
│   │   ├── __init__.py
│   │   ├── runtime.py           # ToolsRuntime 主类
│   │   ├── executor.py          # 工具执行器（read_file/list_dir/grep/run_script）
│   │   ├── permissions.py       # 权限合并与校验
│   │   └── approval.py          # 审批机制（交互式/非交互式）
│   │
│   ├── output/                   # 流式输出子系统
│   │   ├── __init__.py
│   │   ├── sink.py              # OutputSink 基类（NullSink = 基类本身）
│   │   ├── cli_sink.py          # CLISink（终端输出）
│   │   └── sse_sink.py          # SSESink（HTTP SSE，Phase C）
│   │
│   ├── session/                  # 会话管理子系统
│   │   ├── __init__.py
│   │   ├── session.py           # SessionContext、SessionManager
│   │   └── compressor.py        # ConversationCompressor（Phase B）
│   │
│   ├── cli/                      # CLI 入口
│   │   ├── __init__.py
│   │   ├── main.py              # argparse 入口、子命令路由
│   │   ├── run.py               # `skills-agent run` 单次模式
│   │   └── chat.py              # `skills-agent chat` 会话模式
│   │
│   └── common/                   # 公共基础设施
│       ├── __init__.py
│       ├── config.py            # 配置加载（config.json）
│       ├── logging.py           # 日志配置
│       └── security.py          # 路径越界防护（realpath 校验）
│
├── tests/
│   ├── conftest.py
│   ├── unit/                    # 单元测试
│   └── fixtures/
│       └── skills/              # 测试用技能目录
│
├── docs/
│   ├── agent-skills-tech-design.md   # 总体技术设计
│   └── design/                       # 各子系统详细设计
│       ├── framework-architecture.md # 本文档
│       ├── agent-core.md
│       ├── skill-registry.md
│       ├── skill-loader.md
│       ├── model-adapter.md
│       ├── tools-runtime.md
│       ├── chat-session.md
│       ├── streaming-output.md
│       ├── distribution-cli.md       # 后续阶段（暂不实现）
│       └── evals.md                  # 后续阶段（暂不实现）
│
├── .agent/                       # 本地 Agent 数据目录（不提交）
│   ├── skills/                  # 项目级技能
│   ├── runs/                    # 运行记录（events.jsonl 等）
│   ├── sessions/                # Chat 会话记录
│   └── config.json              # 本地配置
│
├── pyproject.toml
└── README.md
```

---

## 3. 核心数据结构

### 3.1 SkillMetadata（技能元数据）

**文件**：[src/skills/metadata.py](../../src/skills/metadata.py)

```python
from pydantic import BaseModel, Field
from typing import Optional

class ResourceLimits(BaseModel):
    """资源配额。enforced = 运行时强制执行；best-effort = 尽量遵守但不保证。"""
    max_script_time_sec: int = 30          # enforced: subprocess timeout
    max_concurrent_scripts: int = 2        # enforced: semaphore
    max_memory_mb: Optional[int] = None    # best-effort: macOS 无法可靠强制
    allow_network: bool = False            # best-effort: 进程级无法隔离

class SkillMetadata(BaseModel):
    """技能元数据（内部完整版）"""
    # === 模型可见层（注入给模型的字段）===
    name: str
    description: str
    source: str                    # project / user / builtin

    # === 内部控制层（绝不发给模型）===
    skill_path: str                # SKILL.md 所在绝对路径
    version: str = "1.0"
    allowed_tools: list[str] = Field(default_factory=list)
    resource_limits: ResourceLimits = Field(default_factory=ResourceLimits)

    def to_model_view(self) -> dict:
        """返回模型可见层字段。仅 name/description/source。"""
        return {"name": self.name, "description": self.description, "source": self.source}
```

### 3.2 Plan、Step、Action（规划与动作）

**文件**：[src/agent/plan.py](../../src/agent/plan.py)

```python
from pydantic import BaseModel, Field
from enum import Enum
from typing import Optional
import time

class StepStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"

class Step(BaseModel):
    id: str
    description: str
    status: StepStatus = StepStatus.PENDING
    notes: Optional[str] = None

class Plan(BaseModel):
    goal: str
    steps: list[Step] = Field(default_factory=list)
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)

    def replace(self, new_plan: "Plan") -> "Plan":
        """全量替换（MVP 唯一支持的更新方式）。"""
        return Plan(goal=self.goal, steps=new_plan.steps,
                    created_at=self.created_at, updated_at=time.time())

    def current_step(self) -> Optional[Step]:
        for step in self.steps:
            if step.status not in (StepStatus.DONE, StepStatus.FAILED):
                return step
        return None

class ActionType(str, Enum):
    LOAD_SKILL = "load_skill"
    LOAD_RESOURCE = "load_resource"
    RUN_SCRIPT = "run_script"
    UPDATE_PLAN = "update_plan"
    FINAL_ANSWER = "final_answer"

class Action(BaseModel):
    type: ActionType
    params: dict = Field(default_factory=dict)
```

### 3.3 AgentState（运行态状态）

**文件**：[src/agent/state.py](../../src/agent/state.py)

```python
from pydantic import BaseModel, Field
from typing import Optional
from .plan import Plan
from ..skills.metadata import SkillMetadata

class AgentState(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    session_id: str
    user_input: str
    plan: Optional[Plan] = None
    active_skills: list[SkillMetadata] = Field(default_factory=list)
    turn_count: int = 0
    last_plan_progress_turn: int = 0
    recent_action_hashes: list[str] = Field(default_factory=list)
    dead_loop_triggered: bool = False

    def is_done(self) -> bool:
        if self.dead_loop_triggered:
            return True
        if self.plan is None:
            return False
        return all(s.status in ("done", "failed") for s in self.plan.steps)
```

### 3.4 Event（事件流）

**文件**：[src/agent/events.py](../../src/agent/events.py)

```python
from pydantic import BaseModel, Field
from enum import Enum
import time, uuid

class EventType(str, Enum):
    SESSION_START = "session_start"
    SESSION_END = "session_end"
    PLAN_CREATED = "plan_created"
    PLAN_UPDATED = "plan_updated"
    ACTION_REQUESTED = "action_requested"
    ACTION_COMPLETED = "action_completed"
    ACTION_FAILED = "action_failed"
    SKILL_LOADED = "skill_loaded"
    SCRIPT_STARTED = "script_started"
    SCRIPT_COMPLETED = "script_completed"
    MODEL_REQUEST = "model_request"
    MODEL_RESPONSE = "model_response"
    FINAL_ANSWER = "final_answer"
    DEAD_LOOP_DETECTED = "dead_loop_detected"
    ERROR = "error"

class Event(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    type: EventType
    timestamp: float = Field(default_factory=time.time)
    session_id: str
    data: dict = Field(default_factory=dict)

class EventLogger:
    def __init__(self, path: str, session_id: str):
        self.path = path
        self.session_id = session_id

    def emit(self, event_type: EventType, data: dict = None) -> Event:
        event = Event(type=event_type, session_id=self.session_id, data=data or {})
        with open(self.path, "a") as f:
            f.write(event.model_dump_json() + "\n")
        return event
```

### 3.5 OutputSink（输出接口）

**文件**：[src/output/sink.py](../../src/output/sink.py)

```python
from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..agent.plan import Plan

class OutputSink:
    """输出接收器基类（空实现即为 NullSink）。"""
    def on_progress(self, action: str, detail: str = "") -> None: ...
    def on_plan_updated(self, plan: "Plan") -> None: ...
    def on_text_chunk(self, chunk: str, done: bool) -> None: ...
    def on_observation(self, source: str, content: str) -> None: ...
    def on_error(self, message: str, recoverable: bool = True) -> None: ...
    def on_session_end(self, turn_count: int, status: str) -> None: ...

NullSink = OutputSink
```

### 3.6 SessionContext（会话上下文）

**文件**：[src/session/session.py](../../src/session/session.py)

```python
from pydantic import BaseModel, Field
import uuid, time

class ConversationTurn(BaseModel):
    role: str      # "user" 或 "assistant"
    content: str
    timestamp: float = Field(default_factory=time.time)

class SessionContext(BaseModel):
    session_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    compressed_summary: str = ""
    recent_turns: list[ConversationTurn] = Field(default_factory=list)
    last_used_skills: list[str] = Field(default_factory=list)
    total_turn_count: int = 0
    recent_window_k: int = 3
```

---

## 4. 模块依赖关系

```
CLI (run.py / chat.py)
    │
    ├── AgentCore ──────────────────────────────────────────────┐
    │       │                                                   │
    │       ├── SkillRegistry (扫描元数据)                       │
    │       ├── SkillLoader (按需加载正文/资源)                   │
    │       ├── ModelAdapter (决策)                              │
    │       │       └── MockModel / AnthropicAdapter             │
    │       ├── ToolsRuntime (受控执行)                          │
    │       │       ├── Executor (read_file/grep/run_script)    │
    │       │       ├── Permissions (权限合并)                   │
    │       │       └── Approval (审批)                         │
    │       ├── ContextBuilder (上下文组装)                      │
    │       ├── EventLogger (事件落盘)                           │
    │       └── OutputSink → CLISink / SSESink / NullSink       │
    │                                                           │
    └── SessionManager (会话持久化) ─────────────────────────────┘
            └── ConversationCompressor (Phase B，历史压缩)

Common (config / logging / security)
    └── 被以上所有模块使用
```

**关键依赖约束**：

| 约束 | 说明 |
|------|------|
| Agent Core 不依赖具体 Sink | 只持有 `OutputSink` 接口引用 |
| Model Adapter 不做规划 | Plan 更新语义由 Agent Core 决定 |
| Skill Registry 不读正文 | 只解析 YAML 前言 |
| Tools Runtime 不做决策 | 仅执行 Agent Core 传来的指令 |
| 控制层字段永不入模型 | `to_model_view()` 是唯一向模型输出的出口 |

---

## 5. 各阶段实现范围

| 模块 | Phase A | Phase B | Phase C |
|------|---------|---------|---------|
| SkillMetadata + Plan + Action + AgentState + Event | ✅ | — | — |
| SkillRegistry + SkillLoader | ✅ | — | — |
| MockModel | ✅ | — | — |
| AgentCore（ReAct 主循环，无脚本执行） | ✅ | — | — |
| OutputSink + NullSink + CLISink（进度/答案） | ✅ | — | — |
| 基础 CLI（run / chat / skills list） | ✅ | — | — |
| SessionContext + SessionManager（会话骨架） | ✅ | — | — |
| ToolsRuntime（run_script + 权限） | — | ✅ | — |
| ContextBuilder（token 裁剪） | — | ✅ | — |
| 死循环检测（动作哈希 + Plan 进度） | — | ✅ | — |
| ConversationCompressor（会话压缩） | — | ✅ | — |
| AnthropicAdapter | — | — | ✅ |
| StreamingJSONParser | — | — | ✅ |
| CLISink（流式逐字答案） | — | — | ✅ |
| SSESink | — | — | ✅ |

---

## 6. 关键配置格式

### 6.1 `.agent/config.json`

```json
{
  "skill_roots": [
    {"source": "project", "path": ".agent/skills", "priority": 0},
    {"source": "user",    "path": "~/.agent/skills", "priority": 1},
    {"source": "builtin", "path": "/path/to/builtin", "priority": 2}
  ],
  "model": {
    "provider": "mock",
    "params": {}
  },
  "budget": {
    "max_turns": 20,
    "max_tool_calls": 30,
    "max_script_executions": 10,
    "max_context_tokens": 100000
  },
  "execution": {
    "require_approval_for": ["run_script", "write_file"],
    "allowed_tools": ["read_file", "list_dir", "grep", "run_script"]
  },
  "security": {
    "max_skill_body_lines": 500,
    "max_resource_file_bytes": 2000000,
    "block_angle_brackets_in_frontmatter": true
  }
}
```

### 6.2 SKILL.md 前言格式

```yaml
---
name: pdf-form-filler
description: Extract and fill PDF form fields using Python
version: "1.0"
allowed-tools:
  - read_file
  - run_script
resource-limits:
  max-script-time-sec: 60
  allow-network: false
---

# PDF Form Filler

（技能正文从这里开始，不含前言）
```

### 6.3 运行目录结构

```
.agent/
├── runs/<session-id>/
│   ├── events.jsonl        # 事件流（事实来源）
│   ├── state.json          # AgentState 快照
│   ├── final.md            # 最终输出
│   └── observations/       # 大块工具输出（按引用存储）
│
└── sessions/<session-id>/
    ├── session.json        # 会话元信息（只写一次）
    ├── conversation.jsonl  # 完整对话原始记录
    └── context.json        # 活跃上下文状态（每轮更新）
```

---

## 7. 数据流（完整执行流程）

```
用户输入（run 模式 / chat 模式）
    │
    ▼
[CLI] 解析参数，构造 AgentCore（注入 sink/model/registry/loader/tools）
    │
    ▼
[AgentCore] 初始化 AgentState，创建 EventLogger，emit SESSION_START
    │
    ▼
[SkillRegistry] 扫描技能根目录 → SkillMetadata 列表（仅元数据）
    │
    ▼
[AgentCore] Turn 1：构造 context（系统 prompt + 技能索引），调用 ModelAdapter
    │
    ▼
[ModelAdapter] 输出 Action（e.g., UPDATE_PLAN + LOAD_SKILL）
    │
    ▼
[AgentCore] 执行 Action：
  - UPDATE_PLAN → Plan.replace()，sink.on_plan_updated()
  - LOAD_SKILL  → SkillLoader.load_body()，sink.on_progress()
  - LOAD_RESOURCE → SkillLoader.load_resource()
  - RUN_SCRIPT  → ToolsRuntime.run_script()（权限校验 + 审批）
  - FINAL_ANSWER → sink.on_text_chunk()，emit FINAL_ANSWER，退出循环
    │
    ▼
[AgentCore] 收集 Observation，inject 到下一轮 context，重复循环
    │
    ▼
[AgentCore] emit SESSION_END，持久化 AgentState
    │
    ▼
（chat 模式）[SessionManager] 记录对话，检查是否需要压缩，save context.json
```

---

## 8. 设计文档索引

| 子系统 | 设计文档 | 实现阶段 |
|--------|----------|----------|
| Agent Core（ReAct 主循环） | [agent-core.md](agent-core.md) | Phase A/B |
| Skill Registry（技能扫描） | [skill-registry.md](skill-registry.md) | Phase A |
| Skill Loader（按需加载） | [skill-loader.md](skill-loader.md) | Phase A |
| Model Adapter（模型适配） | [model-adapter.md](model-adapter.md) | Phase A（Mock）/ C（Anthropic）|
| Tools Runtime（受控执行） | [tools-runtime.md](tools-runtime.md) | Phase B |
| 流式输出（OutputSink） | [streaming-output.md](streaming-output.md) | Phase A（基础）/ C（流式）|
| Chat 会话管理 | [chat-session.md](chat-session.md) | Phase A（骨架）/ B（压缩）|
| CLI 与分发 | [distribution-cli.md](distribution-cli.md) | Phase A（基础 CLI）/ 后续（分发）|
| 评估与回归 | [evals.md](evals.md) | 后续阶段 |
