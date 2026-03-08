## Phase A: 核心链路（MockModel 驱动）

### 任务 A0.1: 创建项目结构与配置

**目标**: 创建标准 Python 项目结构，配置 `pyproject.toml` 与开发环境

**输出文件**:
```
├── src/
│   ├── __init__.py
│   ├── agent/
│   │   ├── __init__.py
│   │   ├── core.py           # AgentCore 主类（ReAct 主循环）
│   │   ├── state.py          # AgentState 运行态状态
│   │   ├── plan.py           # Plan、Step、Action 数据结构
│   │   ├── events.py         # EventType、Event、EventLogger
│   │   └── context.py        # ContextBuilder（上下文组装，Phase B）
│   ├── skills/
│   │   ├── __init__.py
│   │   ├── metadata.py       # SkillMetadata、ResourceLimits
│   │   ├── registry.py       # SkillRegistry（扫描与索引）
│   │   ├── loader.py         # SkillLoader（按需内容加载）
│   │   └── frontmatter.py    # YAML 前言解析（PyYAML safe_load）
│   ├── model/
│   │   ├── __init__.py
│   │   ├── base.py           # ModelAdapter 抽象基类 + parse_action_response + RetryAdapter
│   │   ├── mock.py           # MockModel（Phase A/B 测试驱动）
│   │   ├── anthropic.py      # AnthropicAdapter（Phase C）
│   │   └── streaming.py      # StreamingJSONParser（Phase C）
│   ├── tools/
│   │   ├── __init__.py
│   │   ├── runtime.py        # ToolsRuntime 主类
│   │   ├── executor.py       # 工具执行器（read_file/list_dir/grep/run_script）
│   │   ├── permissions.py    # 权限合并与校验
│   │   └── approval.py       # 审批机制（交互式/非交互式）
│   ├── cli/
│   │   ├── __init__.py
│   │   ├── main.py           # argparse 入口、子命令路由
│   │   ├── run.py            # `skills-agent run` 单次模式
│   │   └── chat.py           # `skills-agent chat` 会话模式
│   ├── output/
│   │   ├── __init__.py
│   │   ├── sink.py           # OutputSink 基类（NullSink = 基类本身）
│   │   ├── cli_sink.py       # CLISink（终端输出）
│   │   └── sse_sink.py       # SSESink（HTTP SSE，Phase C）
│   ├── session/
│   │   ├── __init__.py
│   │   ├── session.py        # SessionContext、SessionManager
│   │   └── compressor.py     # ConversationCompressor（Phase B）
│   └── common/
│       ├── __init__.py
│       ├── config.py         # 配置加载（config.json）
│       ├── logging.py        # 日志配置
│       └── security.py       # 路径越界防护（realpath 校验）
├── tests/
│   ├── __init__.py
│   ├── conftest.py
│   ├── unit/
│   │   └── __init__.py
│   ├── integration/
│   │   └── __init__.py
│   └── fixtures/
│       └── skills/
│           └── example-skill/
│               └── SKILL.md
├── .agent/                    # 本地 Agent 数据目录（不提交）
│   ├── skills/               # 项目级技能
│   ├── runs/                 # 运行记录（events.jsonl 等）
│   ├── sessions/             # Chat 会话记录
│   └── config.json           # 本地配置
├── pyproject.toml
└── .python-version
```

**pyproject.toml 关键字段**:
```toml
[project]
name = "skills-agent"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "PyYAML>=6.0",
    "requests>=2.31.0",
    "pydantic>=2.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-cov",
]

[project.scripts]
skills-agent = "src.cli.main:main"

[tool.pytest.ini_options]
testpaths = ["tests"]
```

**验收标准**:
- [ ] `uv sync` 成功安装依赖
- [ ] `uv run pytest tests/` 可正常发现并运行（零测试也算通过）
- [ ] `python -c "import yaml; import pydantic; import requests"` 无报错

---

### 任务 A1.1: SkillMetadata 数据结构

**目标**: 实现技能元数据的核心数据结构，包含双层分离（模型可见层 vs 内部控制层）

**输入**: 无（第一个代码任务）

**输出文件**: `src/skills/metadata.py`

**背景说明**:

技能元数据分为两层：
- **模型可见层**：`name`、`description`、`source`。这些字段被打包进 context 发给模型，帮助模型选择技能。
- **内部控制层**：在模型可见层基础上，额外包含 `allowed-tools`、`resource-limits` 等控制字段。这些字段**绝不发给模型**（防止提示注入与权限提升）。

**数据结构**:

```python
from pydantic import BaseModel, Field
from typing import Optional

class ResourceLimits(BaseModel):
    """资源配额。enforced = 运行时强制执行；best-effort = 尽量遵守但不保证。"""
    max_script_time_sec: int = Field(default=30)           # enforced: subprocess timeout
    max_concurrent_scripts: int = Field(default=2)         # enforced: semaphore
    max_memory_mb: Optional[int] = Field(default=None)     # best-effort: macOS 无法可靠强制
    allow_network: bool = Field(default=False)             # best-effort: 进程级无法隔离

class SkillMetadata(BaseModel):
    """技能元数据（内部完整版，含控制层字段）"""
    # === 模型可见层 ===
    name: str
    description: str
    source: str                    # 来源根目录标识：project / user / builtin
    skill_path: str                # SKILL.md 所在绝对路径（不发给模型）

    # === 内部控制层（不发给模型）===
    version: str = "1.0"
    author: Optional[str] = None
    disable_model_invocation: bool = False   # True 时从模型可见索引中隐藏（仅允许用户手动调用）
    user_invocable: bool = True              # 是否在 CLI/UI 列表中展示为可直接调用
    allowed_tools: list[str] = Field(default_factory=list)
    requires: list[str] = Field(default_factory=list)      # 依赖的其他技能名称列表
    load_priority: str = "normal"            # high / normal / low，影响多技能加载顺序
    resource_limits: ResourceLimits = Field(default_factory=ResourceLimits)
    run_mode: str = "inline"                 # MVP-reserved: subagent 行为等同 inline

    def to_model_view(self) -> dict:
        """返回模型可见层字段。仅包含 name/description/source。
        注意：disable_model_invocation 的过滤由 SkillRegistry.list_model_views() 负责，
        本方法仅做字段投影，不做过滤。
        """
        return {
            "name": self.name,
            "description": self.description,
            "source": self.source,
        }
```

**验收标准**:
- [ ] `SkillMetadata` 可正常构造，字段有默认值
- [ ] `to_model_view()` 只返回 `name`/`description`/`source` 三个字段，不含 `allowed_tools`、`requires` 等控制字段
- [ ] `ResourceLimits` 可通过 pydantic 验证
- [ ] `disable_model_invocation`、`requires`、`load_priority` 等新字段有正确默认值

**测试用例** (`tests/unit/test_metadata.py`):
```python
def test_to_model_view_excludes_controls():
    meta = SkillMetadata(
        name="test-skill",
        description="A test skill",
        source="project",
        skill_path="/tmp/test/SKILL.md",
        allowed_tools=["read_file", "run_script"],
        requires=["base-utils"],
    )
    view = meta.to_model_view()
    assert set(view.keys()) == {"name", "description", "source"}
    assert "allowed_tools" not in view
    assert "resource_limits" not in view
    assert "requires" not in view
    assert "disable_model_invocation" not in view

def test_resource_limits_defaults():
    limits = ResourceLimits()
    assert limits.max_script_time_sec == 30
    assert limits.max_concurrent_scripts == 2
    assert limits.max_memory_mb is None

def test_disable_model_invocation_default_false():
    meta = SkillMetadata(
        name="test-skill",
        description="A test skill",
        source="project",
        skill_path="/tmp/test/SKILL.md",
    )
    assert meta.disable_model_invocation is False
    assert meta.load_priority == "normal"
    assert meta.requires == []
```

---

### 任务 A1.2: Plan、Step、Action 数据结构

**目标**: 实现 Agent 规划层的核心数据结构

**输出文件**: `src/agent/plan.py`

**背景说明**:

- **Plan** 是 Agent 的目标分解，包含有序的 Step 列表。
- **Step** 代表一个子目标，有 `pending / in_progress / done / failed` 四种状态。
- **Plan 更新策略**：MVP 阶段仅支持**全量替换**（model 返回完整新 Plan，Agent Core 用新 Plan 覆盖旧 Plan）。增量 patch 模式暂不实现（原因：减少 model 输出格式复杂度，降低部分更新失败风险）。

**数据结构**:

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
    id: str                        # 唯一标识，如 "step-1"
    description: str               # 子目标描述
    status: StepStatus = StepStatus.PENDING
    notes: Optional[str] = None    # 执行过程中的补充说明

class Plan(BaseModel):
    goal: str                      # 整体任务目标（来自用户输入）
    steps: list[Step] = Field(default_factory=list)
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)

    def replace(self, new_plan: "Plan") -> "Plan":
        """全量替换：用新 Plan 覆盖旧 Plan，保留 goal。"""
        return Plan(
            goal=self.goal,
            steps=new_plan.steps,
            created_at=self.created_at,
            updated_at=time.time(),
        )

    def current_step(self) -> Optional[Step]:
        """返回第一个非 done/failed 的 step。"""
        for step in self.steps:
            if step.status not in (StepStatus.DONE, StepStatus.FAILED):
                return step
        return None
```

**Action（动作）数据结构**（也放在 `src/agent/plan.py` 或 `src/agent/actions.py`）:

```python
class ActionType(str, Enum):
    LOAD_SKILL = "load_skill"        # 触发技能，加载 SKILL.md 正文
    LOAD_RESOURCE = "load_resource"  # 加载技能 resource 文件
    RUN_SCRIPT = "run_script"        # 执行技能脚本
    UPDATE_PLAN = "update_plan"      # 全量替换当前计划
    FINAL_ANSWER = "final_answer"    # 结束输出最终答案

class Action(BaseModel):
    type: ActionType
    params: dict = Field(default_factory=dict)
    # 常用 params：
    # load_skill:    {"skill_name": "xxx"}
    # load_resource: {"skill_name": "xxx", "resource": "path/to/file"}
    # run_script:    {"skill_name": "xxx", "script": "run.sh", "args": [...]}
    # update_plan:   {"plan": {...}}  # 完整 Plan JSON
    # final_answer:  {"content": "..."}
```

**验收标准**:
- [ ] `Plan` 可正常构造并序列化为 JSON
- [ ] `replace()` 保留 `goal` 和 `created_at`，更新 `updated_at`
- [ ] `current_step()` 返回第一个非终态 step

**测试用例** (`tests/unit/test_plan.py`):
```python
def test_current_step_skips_done():
    plan = Plan(goal="test", steps=[
        Step(id="s1", description="first", status=StepStatus.DONE),
        Step(id="s2", description="second"),
        Step(id="s3", description="third"),
    ])
    assert plan.current_step().id == "s2"

def test_replace_preserves_goal():
    old = Plan(goal="my goal", steps=[Step(id="s1", description="old")])
    new_plan = Plan(goal="ignored", steps=[Step(id="s2", description="new")])
    result = old.replace(new_plan)
    assert result.goal == "my goal"
    assert result.steps[0].id == "s2"
```

---

### 任务 A1.3: Event（事件）数据结构

**目标**: 实现结构化事件流，用于审计、调试与崩溃恢复

**输出文件**: `src/agent/events.py`

**背景说明**:

所有 Agent 运行时行为都产生事件，追加写入 `events.jsonl`。崩溃恢复时从此文件重建上下文（compress-rebuild 策略：不逐条重放消息，而是从事件中提取 Plan 快照 + 动作摘要重新构建 context）。

**数据结构**:

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
    """将事件追加写入 JSONL 文件。"""
    def __init__(self, path: str, session_id: str):
        self.path = path
        self.session_id = session_id

    def emit(self, event_type: EventType, data: dict = None) -> Event:
        event = Event(type=event_type, session_id=self.session_id, data=data or {})
        with open(self.path, "a") as f:
            f.write(event.model_dump_json() + "\n")
        return event
```

**验收标准**:
- [ ] `EventLogger.emit()` 追加写入 JSONL，每行是合法 JSON
- [ ] 读回文件可用 `Event.model_validate_json()` 反序列化
- [ ] `session_id` 正确传入每条事件

---

### 任务 A1.4: AgentState（Agent 状态）数据结构

**目标**: 实现 Agent 运行时状态容器，统一管理 Plan、活跃技能、已用 token 等

**输出文件**: `src/agent/state.py`

**数据结构**:

```python
from pydantic import BaseModel, Field
from typing import Optional
from .plan import Plan
from ..skills.metadata import SkillMetadata

class AgentState(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    session_id: str
    user_input: str                # 当前轮次用户输入（供 ContextBuilder 使用）
    plan: Optional[Plan] = None
    active_skills: list[SkillMetadata] = Field(default_factory=list)
    # 整体运行状态：running / completed / failed / dead_loop / max_turns
    status: str = "running"
    # 当前 turn 数（用于死循环检测中的 Plan 进度监测）
    turn_count: int = 0
    # 上次 Plan 有步骤状态变化时的 turn_count（进度监测用）
    last_plan_progress_turn: int = 0
    # 近期动作 hash 队列（动作去重死循环检测用），最近 K 条
    recent_action_hashes: list[str] = Field(default_factory=list)
    # 是否已触发死循环中止
    dead_loop_triggered: bool = False

    def is_done(self) -> bool:
        """是否达到退出条件：已收到 FINAL_ANSWER（status=completed/failed），或已触发死循环中止。"""
        if self.dead_loop_triggered:
            return True
        if self.status in ("completed", "failed", "dead_loop", "max_turns"):
            return True
        if self.plan is None:
            return False
        return all(
            s.status in ("done", "failed") for s in self.plan.steps
        )
```

**验收标准**:
- [ ] `AgentState` 可正常构造，`user_input` 为必填字段，`status` 默认 `"running"`
- [ ] `is_done()` 在 plan 为 None 时返回 False
- [ ] `is_done()` 在 dead_loop_triggered=True 时返回 True
- [ ] `is_done()` 在 `status="completed"` 时返回 True
- [ ] `is_done()` 在所有 steps 都为 done/failed 时返回 True

---

### 任务 A1.5: OutputSink 接口与 NullSink

**目标**: 定义流式输出接口，使 Agent Core 与终端/API 等具体输出方式解耦

**输出文件**: `src/output/sink.py`

**背景说明**:

详见 [流式输出设计](../design/streaming-output.md)。Agent Core 通过 `OutputSink` 接口向外推送执行进度与流式文本，具体实现由调用方提供（CLI 传入 `CLISink`，测试传入 `NullSink`，未来 API 传入 `SSESink`）。Agent Core 代码与输出方式完全解耦。

**数据结构**:

```python
# src/output/sink.py
from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..agent.plan import Plan

class OutputSink:
    """
    输出接收器基类（非抽象，所有方法有空实现）。
    子类只需覆盖关心的方法。
    测试时直接使用 NullSink（等同于此基类）。
    """

    def on_progress(self, action: str, detail: str = "") -> None:
        """Agent 正在执行某动作：加载技能、运行脚本等。"""

    def on_plan_updated(self, plan: "Plan") -> None:
        """Plan 被更新，展示新的步骤列表与状态。"""

    def on_text_chunk(self, chunk: str, done: bool) -> None:
        """最终答案流式文本片段。done=True 时表示答案完整。"""

    def on_observation(self, source: str, content: str) -> None:
        """工具/脚本执行结果摘要（供 --verbose 模式展示）。"""

    def on_error(self, message: str, recoverable: bool = True) -> None:
        """错误发生。recoverable=True 表示 Agent 将继续尝试。"""

    def on_session_end(self, turn_count: int, status: str) -> None:
        """会话结束。status: completed / failed / dead_loop / max_turns。"""


# NullSink = OutputSink 基类本身（空实现即可，无需子类）
NullSink = OutputSink
```

**验收标准**:
- [ ] `NullSink` 所有方法可正常调用，无异常
- [ ] `AgentCore` 构造时接受 `sink: OutputSink = None`，默认 `NullSink()`
- [ ] 传入自定义 sink 时，执行动作可触发对应方法

**测试用例** (`tests/unit/test_sink.py`):
```python
def test_null_sink_no_exception():
    sink = NullSink()
    sink.on_progress("load_skill", "test")
    sink.on_text_chunk("hello", False)
    sink.on_text_chunk(" world", True)
    sink.on_session_end(3, "completed")

def test_custom_sink_receives_calls():
    received = []
    class RecordSink(OutputSink):
        def on_progress(self, action, detail=""):
            received.append(("progress", action, detail))
        def on_text_chunk(self, chunk, done):
            received.append(("chunk", chunk, done))

    sink = RecordSink()
    sink.on_progress("run_script", "analyze.py")
    sink.on_text_chunk("result", True)
    assert received == [
        ("progress", "run_script", "analyze.py"),
        ("chunk", "result", True),
    ]
```

---

### 任务 A2.1: SKILL.md Frontmatter 解析器

**目标**: 解析 SKILL.md 文件，提取 YAML frontmatter 中的元数据字段

**输入**: 无前置代码依赖（独立工具函数）

**输出文件**: `src/skills/frontmatter.py`

**背景说明**:

SKILL.md 格式：
```
---
name: example-skill
description: "An example skill"
version: "1.0"
allowed-tools:
  - read_file
  - run_script
resource-limits:
  max-script-time-sec: 60
  allow-network: false
---

# 正文内容...
```

解析策略：
- 使用 `yaml.safe_load()`（PyYAML）解析 frontmatter，**禁止** `yaml.load()`（安全原因）
- 解析前检查内容中是否含 `<` 字符（防御 YAML 标签注入），若有则拒绝
- 解析后对字段名做白名单校验（只允许已知字段）
- 正文（frontmatter 之后的 Markdown 文本）单独返回

**数据结构**:

```python
from dataclasses import dataclass
from typing import Optional
import yaml

ALLOWED_FRONTMATTER_FIELDS = {
    "name", "description", "version",
    "author",
    "disable-model-invocation", "user-invocable",
    "allowed-tools", "requires", "load-priority",
    "resource-limits", "run-mode",
}

@dataclass
class ParsedSkillFile:
    frontmatter: dict       # 原始 frontmatter 字典（字段已验证为白名单）
    body: str               # frontmatter 之后的 Markdown 正文

class FrontmatterParseError(Exception):
    pass

def parse_skill_file(content: str) -> ParsedSkillFile:
    """
    解析 SKILL.md 文件内容。
    返回 ParsedSkillFile，或在格式错误/安全违规时抛出 FrontmatterParseError。
    """
    ...
```

**实现要点**:

```python
def parse_skill_file(content: str) -> ParsedSkillFile:
    # 1. 检查是否含危险字符
    if "<" in content:
        raise FrontmatterParseError("Content contains '<' which may indicate YAML tag injection")

    # 2. 提取 frontmatter 块（--- ... ---）
    lines = content.split("\n")
    if not lines or lines[0].strip() != "---":
        raise FrontmatterParseError("SKILL.md must start with '---'")

    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break
    if end_idx is None:
        raise FrontmatterParseError("Frontmatter closing '---' not found")

    fm_text = "\n".join(lines[1:end_idx])
    body = "\n".join(lines[end_idx + 1:]).strip()

    # 3. 用 yaml.safe_load 解析
    try:
        fm = yaml.safe_load(fm_text)
    except yaml.YAMLError as e:
        raise FrontmatterParseError(f"YAML parse error: {e}")

    if not isinstance(fm, dict):
        raise FrontmatterParseError("Frontmatter must be a YAML mapping")

    # 4. 字段白名单校验
    unknown = set(fm.keys()) - ALLOWED_FRONTMATTER_FIELDS
    if unknown:
        raise FrontmatterParseError(f"Unknown frontmatter fields: {unknown}")

    return ParsedSkillFile(frontmatter=fm, body=body)
```

**验收标准**:
- [ ] 正常 SKILL.md 解析成功，frontmatter 字段正确
- [ ] 含 `<` 字符时抛出 `FrontmatterParseError`
- [ ] 使用未知字段时抛出 `FrontmatterParseError`
- [ ] `yaml.load()` 未在代码中出现（grep 验证）

**测试用例** (`tests/unit/test_frontmatter.py`):
```python
VALID_SKILL = """---
name: example-skill
description: An example
version: "1.0"
allowed-tools:
  - read_file
---

# Example Skill Body
This is the body.
"""

def test_parse_valid_skill():
    result = parse_skill_file(VALID_SKILL)
    assert result.frontmatter["name"] == "example-skill"
    assert result.frontmatter["allowed-tools"] == ["read_file"]
    assert "Example Skill Body" in result.body

def test_rejects_angle_bracket():
    content = VALID_SKILL.replace("example-skill", "!!python/object:os.system")
    # 注意：上面替换后不含 < ，另造一个含 < 的
    with pytest.raises(FrontmatterParseError):
        parse_skill_file("---\nname: test\ndescription: <script>\n---\n")

def test_rejects_unknown_fields():
    content = "---\nname: test\ndescription: ok\nunknown-field: bad\n---\n"
    with pytest.raises(FrontmatterParseError, match="Unknown frontmatter fields"):
        parse_skill_file(content)
```

---

### 任务 A2.2: Skill Registry（技能注册与索引）

**目标**: 实现技能根目录扫描与 SkillIndex 构建

**输入**: `src/skills/metadata.py`（A1.1），`src/skills/frontmatter.py`（A2.1）

**输出文件**: `src/skills/registry.py`

**背景说明**:

**SkillIndex 双层设计**（核心安全机制）：
- Registry 内部维护完整 `SkillMetadata`（含 `allowed_tools` 等控制字段）
- 向模型提供 context 时，调用 `to_model_view()` 只暴露 `name/description/source`
- 控制字段**绝不进入 model context**（防止模型利用权限声明发动提示注入）

**Skill Root 优先级**（低索引 = 高优先级）：

| 优先级 | 来源 | 标识 |
|--------|------|------|
| 0 (最高) | 项目级 | `project` |
| 1 | 用户级 | `user` |
| 2 (最低) | 内置 | `builtin` |

同名技能取最高优先级来源。

**数据结构**:

```python
from pathlib import Path
from .metadata import SkillMetadata, ResourceLimits
from .frontmatter import parse_skill_file, FrontmatterParseError
import logging

logger = logging.getLogger(__name__)

class SkillRegistry:
    """
    技能注册表。扫描 skill roots，构建 SkillMetadata 索引。

    使用方：
        registry = SkillRegistry(roots=[("project", "/path/to/skills"), ...])
        registry.scan()
        # 给模型用（安全）：
        model_views = registry.list_model_views()
        # 内部用（包含控制字段）：
        meta = registry.get("skill-name")
    """

    SOURCE_PRIORITY = {"project": 0, "user": 1, "builtin": 2}

    def __init__(self, roots: list[tuple[str, str]]):
        """
        roots: [(source_label, path), ...]
        source_label 必须是 "project" / "user" / "builtin" 之一
        """
        self._roots = roots
        self._index: dict[str, SkillMetadata] = {}

    def scan(self) -> None:
        """扫描所有 skill roots，构建索引。同名技能取高优先级来源。"""
        ...

    def get(self, name: str) -> SkillMetadata | None:
        """按名称获取完整 SkillMetadata（含控制字段，仅内部使用）。"""
        return self._index.get(name)

    def find(self, name: str) -> SkillMetadata | None:
        """get() 的别名，供 AgentCore 调用（语义更清晰）。"""
        return self._index.get(name)

    def list_model_views(self) -> list[dict]:
        """返回所有技能的模型可见层列表（安全，可发给模型）。
        过滤掉 disable_model_invocation=True 的技能，使其对模型不可见。
        """
        return [
            meta.to_model_view()
            for meta in self._index.values()
            if not meta.disable_model_invocation
        ]

    def _load_metadata(self, skill_dir: Path, source: str) -> SkillMetadata | None:
        """从 SKILL.md 加载单个技能元数据。失败时记录警告并返回 None。"""
        ...
```

**实现要点（`scan` 方法）**:
1. 按 `SOURCE_PRIORITY` 排序 roots（低优先级先处理，高优先级后处理并覆盖）
2. 遍历每个 root 目录下的一级子目录，查找 `SKILL.md`
3. 调用 `_load_metadata()` 解析，失败则 `logger.warning` 并跳过
4. 同名技能：高优先级来源覆盖低优先级

**实现要点（`_load_metadata` 方法）**:
1. 读取 `SKILL.md` 内容
2. 调用 `parse_skill_file()` 解析 frontmatter
3. 从 frontmatter 构建 `ResourceLimits` 和 `SkillMetadata`，注意字段名映射：
   - `allowed-tools` → `allowed_tools`
   - `disable-model-invocation` → `disable_model_invocation`
   - `user-invocable` → `user_invocable`
   - `load-priority` → `load_priority`
   - `resource-limits` → `resource_limits`（再做二级字段映射）
4. `skill_path` = `SKILL.md` 的绝对路径字符串

**验收标准**:
- [ ] `scan()` 正常加载测试 fixtures 中的技能
- [ ] `list_model_views()` 不含 `allowed_tools`、`requires` 等控制字段
- [ ] `list_model_views()` 不包含 `disable_model_invocation=True` 的技能
- [ ] `get("example-skill")` / `find("example-skill")` 返回含完整控制字段的 `SkillMetadata`
- [ ] 同名技能优先级规则正确（project > user > builtin）
- [ ] 解析失败的技能目录不影响其他技能加载
- [ ] `disable-model-invocation: true` 的技能不出现在 `list_model_views()` 结果中，但可通过 `get()` 查到

**测试用例** (`tests/unit/test_registry.py`):
```python
import pytest
from pathlib import Path

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "skills"

def test_scan_loads_fixture_skills(tmp_path):
    # 使用 tests/fixtures/skills 下的示例技能
    registry = SkillRegistry(roots=[("project", str(FIXTURES_DIR))])
    registry.scan()
    views = registry.list_model_views()
    assert len(views) > 0
    for view in views:
        assert set(view.keys()) == {"name", "description", "source"}

def test_project_overrides_builtin(tmp_path):
    # 创建两个 root，同名技能
    for source in ["builtin", "project"]:
        skill_dir = tmp_path / source / "my-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: my-skill\ndescription: from {source}\n---\n"
        )
    registry = SkillRegistry(roots=[
        ("project", str(tmp_path / "project")),
        ("builtin", str(tmp_path / "builtin")),
    ])
    registry.scan()
    meta = registry.get("my-skill")
    assert meta.source == "project"
```

---

### 任务 A2.3: Skill Loader（技能内容加载器）

**目标**: 实现渐进式披露的二、三级加载（SKILL.md 正文 + 资源文件）

**输入**: `src/skills/registry.py`（A2.2），`src/skills/frontmatter.py`（A2.1）

**输出文件**: `src/skills/loader.py`

**背景说明**:

渐进式披露三层：
- **Level 1**（Registry scan 完成）：`name`/`description` 已在 SkillIndex，供模型选择技能
- **Level 2**（`load_body` 触发）：读取 `SKILL.md` 正文（frontmatter 之后的 Markdown），注入 context
- **Level 3**（`load_resource` 触发）：读取 `skills/<name>/reference/` 或 `assets/` 下的具体文件

**接口**:

```python
from pathlib import Path
from typing import Optional
from .metadata import SkillMetadata

class SkillLoader:
    def load_body(self, meta: SkillMetadata) -> tuple[str, dict]:
        """
        加载 SKILL.md 正文（Level 2）。
        返回 (正文文本, 报告dict)。
        正文为 frontmatter 之后的 Markdown 文本（去除 YAML 前言）。
        报告dict 含 {'chars': int, 'lines': int} 等统计信息。
        """
        ...

    def load_resource(
        self,
        meta: SkillMetadata,
        resource: str,
        section_hint: Optional[str] = None,
    ) -> tuple[str, dict]:
        """
        加载技能 resource 文件（Level 3）。
        resource 是相对于技能目录的路径，如 "reference/api.md"。
        section_hint：可选，指定只读某个章节（降低 token 消耗）。
        安全检查：禁止 path traversal，解析后路径必须在技能目录内。
        返回 (文件内容/摘要, 报告dict)。
        """
        ...
```

**安全要求**:
- `load_resource` 必须验证 `resource` 不含 `..` 且是相对路径
- 解析后的绝对路径必须以技能目录为前缀（realpath 校验，防 path traversal）

**验收标准**:
- [ ] `load_body()` 返回正文字符串，不含 frontmatter 分隔符
- [ ] `load_body()` 返回的 dict 包含文件统计信息
- [ ] `load_resource()` 拒绝含 `..` 的路径（抛出异常）
- [ ] `load_resource()` 拒绝绝对路径（抛出异常）
- [ ] `load_resource()` 路径在技能目录外时拒绝（realpath 校验）

---

### 任务 A3.1: MockModel

**目标**: 实现可脚本化的 MockModel，用于 Phase A 的完整测试，无需真实 LLM

**输入**: `src/agent/plan.py`（A1.2），`src/agent/actions.py`（A1.2）

**输出文件**: `src/model/mock.py`

**背景说明**:

MockModel 的设计原则：
- 接受 `messages: list[dict]` 输入（标准 chat 格式）
- 返回预先脚本化的 `Action` 序列（按调用次序弹出）
- 耗尽脚本后返回 `FINAL_ANSWER`（避免无限循环）
- 记录每次调用的 messages（用于测试断言 context 构建是否正确）
- 支持 dict 格式的动作（自动转换为 Action 对象）

**接口**:

```python
from .base import ModelAdapter
from ..agent.plan import Action, ActionType

class MockModel(ModelAdapter):
    """
    按顺序返回预设 Action 序列。
    用于单元测试与 Phase A/B 端到端验证。
    当序列耗尽时返回 FINAL_ANSWER（避免死循环）。
    """

    def __init__(self, actions: list[Action | dict]):
        """
        actions: 预设动作列表。
        每项可以是 Action 对象或 dict（自动转换）。
        示例：
            MockModel([
                Action(type=ActionType.UPDATE_PLAN, params={"plan": {...}}),
                Action(type=ActionType.LOAD_SKILL, params={"skill_name": "data-analysis"}),
                Action(type=ActionType.FINAL_ANSWER, params={"content": "done"}),
            ])
        """
        self._actions: list[Action] = []
        for a in actions:
            if isinstance(a, dict):
                self._actions.append(Action.model_validate(a))
            else:
                self._actions.append(a)
        self._index = 0
        self._call_history: list[list[dict]] = []

    def next_action(self, messages: list[dict]) -> Action:
        self._call_history.append(messages)

        if self._index >= len(self._actions):
            # 序列耗尽，返回 final_answer 防止死循环
            return Action(
                type=ActionType.FINAL_ANSWER,
                params={"content": "[MockModel: action sequence exhausted]"},
            )

        action = self._actions[self._index]
        self._index += 1
        return action

    @property
    def call_count(self) -> int:
        return len(self._call_history)

    def reset(self) -> None:
        self._index = 0
        self._call_history.clear()
```

**验收标准**:
- [ ] MockModel 按顺序返回预设 Action（支持 dict 和 Action 两种格式）
- [ ] 脚本耗尽后返回 `FINAL_ANSWER`，内容为 `"[MockModel: action sequence exhausted]"`
- [ ] `call_count` 在 AgentCore 完成 3 轮后等于 3
- [ ] `reset()` 清空 call_history 并重置 index
- [ ] `isinstance(MockModel([...]), ModelAdapter)` 为 True

---

### 任务 A3.2: ModelAdapter 接口 + 解析工具

**目标**: 定义模型适配器的抽象接口（ABC）、JSON 解析工具函数和重试包装器

**输出文件**: `src/model/base.py`

**接口与实现**:

```python
# src/model/base.py
from abc import ABC, abstractmethod
from ..agent.plan import Action, ActionType
import json, re

class ModelResponseError(Exception):
    """模型响应无法解析为合法 Action 时抛出。"""
    pass

class ModelAdapter(ABC):
    """
    模型适配器抽象基类。
    Agent Core 仅依赖此接口，不依赖具体实现。
    """

    @abstractmethod
    def next_action(self, messages: list[dict]) -> Action:
        """
        向模型发送 messages，解析并返回单个 Action。
        messages 格式：OpenAI 风格 [{"role": str, "content": str}, ...]
        失败时抛出 ModelResponseError。
        """

    def close(self) -> None:
        """释放资源（连接池等），默认空实现。"""


def parse_action_response(raw: str) -> Action:
    """
    从模型原始文本解析 Action。
    三步降级解析：
    1. 直接 json.loads()
    2. 提取最外层 {} 块再解析（去除 code fence 等噪声）
    3. 仍失败则抛出 ModelResponseError
    """
    raw = raw.strip()

    # 步骤 1：直接解析
    try:
        data = json.loads(raw)
        return _validate_action(data)
    except (json.JSONDecodeError, ValueError):
        pass

    # 步骤 2：提取 JSON 块
    match = re.search(r'\{.*\}', raw, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group())
            return _validate_action(data)
        except (json.JSONDecodeError, ValueError):
            pass

    raise ModelResponseError(f"Cannot parse model response as Action: {raw[:200]!r}")


def _validate_action(data: dict) -> Action:
    """最小 schema 校验：type 必须是合法枚举，params 必须是 dict。"""
    if "type" not in data:
        raise ValueError("Missing 'type' field")
    return Action(type=data["type"], params=data.get("params", {}))


class RetryAdapter(ModelAdapter):
    """
    包装任意 ModelAdapter，在解析失败时自动重试。
    重试时附加纠错消息（要求模型仅输出修正 JSON）。
    """

    def __init__(self, inner: ModelAdapter, max_retries: int = 2):
        self._inner = inner
        self._max_retries = max_retries

    def next_action(self, messages: list[dict]) -> Action:
        last_error = None
        for attempt in range(self._max_retries + 1):
            try:
                return self._inner.next_action(messages)
            except ModelResponseError as e:
                last_error = e
                if attempt < self._max_retries:
                    messages = messages + [{
                        "role": "user",
                        "content": (
                            f"你的上一次输出无法解析：{e}\n"
                            "请仅输出一个合法的 JSON 对象，格式：\n"
                            '{"type": "<动作类型>", "params": {...}}'
                        )
                    }]
        raise last_error
```

**验收标准**:
- [ ] `MockModel` 继承 `ModelAdapter`，`isinstance(MockModel([...]), ModelAdapter)` 为 True
- [ ] `parse_action_response()` 正确解析干净 JSON
- [ ] `parse_action_response()` 正确从带 code fence 的输出中提取 JSON
- [ ] `parse_action_response()` 在无法解析时抛出 `ModelResponseError`
- [ ] `RetryAdapter` 在第一次失败后追加纠错消息并重试，最多重试 `max_retries` 次

---

### 任务 A3.3: Agent Core 主循环（ReAct）

**目标**: 实现 Agent 的核心 ReAct 主循环，串联所有子系统

**输入**: A1.x（数据结构）、A2.x（技能子系统）、A3.1/A3.2（模型层）

**输出文件**: `src/agent/core.py`

---

#### 3.3.1 总体结构

`AgentCore` 是整个系统的编排器，将"模型决策 → 动作执行 → 观察注入"串成受控流水线。**不在 AgentCore 内处理**：技能发现（SkillRegistry 负责）、模型 HTTP 通信（ModelAdapter 负责）、终端渲染（OutputSink 负责）。

**构造参数**:

```python
# src/agent/core.py
class AgentCore:
    def __init__(
        self,
        model: ModelAdapter,
        registry: SkillRegistry,
        loader: SkillLoader,
        event_logger: EventLogger,
        tools: ToolsRuntime = None,         # Phase B 引入
        sink: OutputSink = None,            # 默认 NullSink
        max_turns: int = 20,
        dead_loop_window: int = 6,          # 动作 hash 检测窗口大小 K
        dead_loop_stall_turns: int = 4,     # Plan 无进展多少轮触发警告（Phase B）
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
```

---

#### 3.3.2 `run()` 方法：完整执行时序

```python
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

执行时序分为「初始化」和「Turn 循环」两段：

```
初始化
  │
  ├── skill_metas = registry.scan()      → list[SkillMetadata]
  ├── 构造 AgentState(session_id, user_input)
  └── logger.emit(SESSION_START, {...})
  │
  ▼
Turn 循环（while not state.is_done() and turn_count < max_turns）
  │
  ├── turn_count += 1；state.turn_count = turn_count
  ├── messages = _build_context(state, skill_metas, history_messages, react_history)
  ├── action = model.next_action(messages)
  ├── logger.emit(ACTION_REQUESTED, action)
  │
  ├── 死循环检测（动作 hash）──────────────────────────────────
  │     _check_dead_loop_hash(action, state)
  │     若触发 → state.dead_loop_triggered=True
  │              sink.on_error("Dead loop detected", recoverable=False)
  │              logger.emit(DEAD_LOOP_DETECTED, {"reason": "repeated_action"})
  │              break
  │
  ├── observation = _execute_action(action, state)
  ├── logger.emit(ACTION_COMPLETED, {"observation": observation[:200]})
  │
  ├── react_history.append((action, observation))
  │
  └── 若 action.type == FINAL_ANSWER → break（_execute_action 已调 sink）
  │
  ▼
循环结束
  ├── 若 turn_count >= max_turns 且未完成 → sink.on_error("Budget exhausted")
  └── logger.emit(SESSION_END, {"turns": turn_count, "status": state.status})
      返回最终答案（或降级答复）
```

**关键约束**：
- 每轮模型只产出一个 Action（每轮单动作原则）
- Observe 是 AgentCore 执行后的结果，注入 react_history 进入下一轮，不触发额外模型调用
- `RUN_SCRIPT` 在 Phase A 返回错误 observation，Phase B 委派 ToolsRuntime

---

#### 3.3.3 上下文组装（`_build_context`）

消息层次（按此顺序拼接 `messages` 列表）：

```
messages = [
  # ── 系统层（每轮重建）─────────────────────────────────────────
  {"role": "system", "content": SYSTEM_PROMPT（含技能索引 + Plan 摘要）},

  # ── 历史层（chat 模式，由调用方传入）──────────────────────────
  *history_messages,                  # 已压缩摘要 + 近期原文轮次

  # ── 任务内 ReAct 历史（本次任务的 act/observe 交替）───────────
  {"role": "assistant", "content": json.dumps(prev_action)},
  {"role": "user",      "content": f"Observation: {observation}"},
  ...（按 react_history 顺序追加）

  # ── 当前层────────────────────────────────────────────────────
  {"role": "user", "content": user_input},
]
```

**系统 Prompt 模板**（`SYSTEM_PROMPT_TEMPLATE`）：

```python
SYSTEM_PROMPT_TEMPLATE = """
你是一个 AI Agent，通过 ReAct 循环（推理 → 行动 → 观察）完成用户任务。

## 可用技能索引
{skill_index}

## 当前计划
{plan_summary}

## 行动协议
每次回复必须是一个 JSON 对象，格式：
{{
  "type": "<load_skill|load_resource|run_script|update_plan|final_answer>",
  "params": {{ ... }}
}}

## 约束
- 每次只能输出一个 action
- load_resource / run_script 必须在 load_skill 之后
- update_plan 中的 plan 为完整新 Plan（全量替换）
- final_answer 时输出完整最终答案
"""
```

**技能索引注入格式**（调用 `registry.list_model_views()`，**不含** `allowed_tools` 等控制字段）：

```
Available Skills:
- name=pdf-form-filler | source=project | description=Extract and fill PDF form fields
- name=code-review     | source=user    | description=Review code with team standards
```

**Plan 摘要注入格式**（`state.plan` 存在时）：

```
Goal: 填写并提交 PDF 表单
Steps:
  [done]        s1: 加载 pdf-form-filler 技能
  [in_progress] s2: 执行 fill.py 脚本填写表单
  [pending]     s3: 验证结果并输出报告
```

---

#### 3.3.4 Action 执行分发（`_execute_action`）

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
        if not meta:
            return f"Error: skill '{skill_name}' not found."
        excerpt, report = self._loader.load_resource(
            meta, resource, section_hint=action.params.get("section_hint")
        )
        self._sink.on_progress("load_resource", resource)
        return f"[Resource: {resource}]\n{excerpt}"

    elif action.type == ActionType.RUN_SCRIPT:
        # Phase A：RUN_SCRIPT 未实现，返回错误 observation
        return "Error: script execution not enabled in this phase."

    elif action.type == ActionType.FINAL_ANSWER:
        content = action.params.get("content", "")
        self._sink.on_text_chunk(content, done=True)
        self._logger.emit(EventType.FINAL_ANSWER, {"content": content})
        state.status = "completed"
        return content  # 调用方检测 action.type == FINAL_ANSWER 后 break

    else:
        return f"Error: unknown action type '{action.type}'."
```

**注意**：`registry.find()` 按名称查询（内部等同于 `get()`）；`loader.load_body()` 和 `load_resource()` 均返回 `(str, dict)` 元组。

---

#### 3.3.5 死循环检测（双机制）

**机制一：动作 hash 检测（主要机制，Phase A 实现，触发则中止）**

维护最近 K 轮动作的 `(type + key_params)` 哈希队列，同一哈希出现 >= 2 次即判定死循环：

```python
import hashlib
import json

def _check_dead_loop_hash(self, action: Action, state: AgentState) -> bool:
    key = json.dumps({"type": action.type, "params": action.params}, sort_keys=True)
    h = hashlib.sha256(key.encode()).hexdigest()[:16]

    state.recent_action_hashes.append(h)
    if len(state.recent_action_hashes) > self._dead_loop_window:
        state.recent_action_hashes.pop(0)

    # 窗口内同一哈希出现 >= 2 次
    return len(state.recent_action_hashes) != len(set(state.recent_action_hashes))
```

触发后处理：
```python
if self._check_dead_loop_hash(action, state):
    state.dead_loop_triggered = True
    self._sink.on_error("Dead loop detected: repeated action", recoverable=False)
    self._logger.emit(EventType.DEAD_LOOP_DETECTED, {"reason": "repeated_action"})
    break  # 退出 ReAct 循环
```

**机制二：Plan 进展检测（辅助机制，Phase B 实现，仅告警不中止）**

每轮 Action 执行后比对 Plan 步骤状态变化，若连续 `dead_loop_stall_turns`（默认 4）轮无变化，发出告警事件：

```python
def _check_dead_loop_stall(self, state: AgentState) -> bool:
    if state.turn_count - state.last_plan_progress_turn >= self._dead_loop_stall_turns:
        self._logger.emit(EventType.DEAD_LOOP_DETECTED, {
            "reason": "plan_stall",
            "turns_without_progress": state.turn_count - state.last_plan_progress_turn,
        })
        return True
    return False
```

Plan 进展追踪（在 `_execute_action` 前后对比步骤状态）：
```python
old_statuses = {s.id: s.status for s in state.plan.steps} if state.plan else {}
observation = self._execute_action(action, state)
new_statuses = {s.id: s.status for s in state.plan.steps} if state.plan else {}
if old_statuses != new_statuses:
    state.last_plan_progress_turn = state.turn_count
```

---

#### 3.3.6 退出条件与错误处理

**退出条件**：

| 条件 | 说明 | 输出 |
|------|------|------|
| `FINAL_ANSWER` | 正常完成 | 最终答案 |
| `max_turns` 耗尽 | 轮次超限 | 降级答复（包含已完成部分） |
| 死循环检测触发 | 动作重复 | 降级答复 + `DEAD_LOOP_DETECTED` 事件 |

**错误处理**：

| 错误类型 | 处理方式 |
|----------|----------|
| 模型输出非法 JSON | `ModelAdapter`/`RetryAdapter` 内部重试（最多 2 次），仍失败则作为 observation |
| Skill 不存在 | 返回 `"Error: skill '...' not found."` observation，模型下一轮更新 Plan |
| 路径越界 | `SkillLoader` 抛出，返回 `PathTraversalBlocked` observation |
| 未知 action type | 返回 `"Error: unknown action type '...'"` observation |

---

#### 3.3.7 审计落盘

运行目录：`.agent/runs/<session_id>/`

| 文件 | 内容 | 写入时机 |
|------|------|----------|
| `events.jsonl` | 所有 Event（追加写） | 每个事件即时写入 |
| `state.json` | `AgentState` 快照 | 每轮覆盖写（最终状态） |
| `final.md` | 最终答案 | `FINAL_ANSWER` 动作完成时 |

**事件序列**（正常 3 轮完成示例）：

```
SESSION_START
ACTION_REQUESTED  (turn=1, UPDATE_PLAN)
PLAN_UPDATED
ACTION_COMPLETED  (turn=1)
ACTION_REQUESTED  (turn=2, LOAD_SKILL)
SKILL_LOADED
ACTION_COMPLETED  (turn=2)
ACTION_REQUESTED  (turn=3, FINAL_ANSWER)
FINAL_ANSWER
ACTION_COMPLETED  (turn=3)
SESSION_END
```

---

**验收标准**:
- [ ] MockModel 驱动 AgentCore 完成 3 轮 ReAct 循环（UPDATE_PLAN → LOAD_SKILL → FINAL_ANSWER）
- [ ] `events.jsonl` 包含完整事件序列：SESSION_START、ACTION_REQUESTED × 3、PLAN_UPDATED、SKILL_LOADED、FINAL_ANSWER、ACTION_COMPLETED × 3、SESSION_END
- [ ] 技能索引通过 `list_model_views()` 注入 context，不含控制字段（`allowed_tools` 等）
- [ ] 技能索引以 `name=... | source=... | description=...` 格式出现在系统 prompt 中
- [ ] Plan 摘要以 `[status] id: description` 格式注入系统 prompt
- [ ] `UPDATE_PLAN` 动作正确触发 `Plan.replace()`，`goal` 保留，`steps` 被新值覆盖
- [ ] `LOAD_SKILL` 对不存在技能返回 `"Error: skill '...' not found."` observation
- [ ] 死循环检测：MockModel 重复返回同一 Action，同一 hash 出现 2 次后 `dead_loop_triggered=True`，循环中止
- [ ] `run(user_input, history_messages=[...])` 支持 chat 模式传入历史消息，history_messages 注入在系统 prompt 之后、react_history 之前
- [ ] `AgentState.is_done()` 在 FINAL_ANSWER 后返回 True
- [ ] `.agent/runs/<session_id>/events.jsonl` 文件存在且内容完整
- [ ] `CLISink` 可见进度输出（stderr），最终答案输出到 stdout（Phase A 配合 A4.2 验证）

---

### 任务 A4.1: 基础 CLI

**目标**: 实现三个基础 CLI 命令：`run`、`skills list`、`skills inspect`

**输入**: A3.3（AgentCore）、A2.2（SkillRegistry）

**输出文件**: `src/cli/main.py`

**命令规格**:

```
# 运行 Agent
skills-agent run --skill-root ./skills "请帮我分析这个问题"

# 列出所有技能
skills-agent skills list --skill-root ./skills

# 查看技能详情
skills-agent skills inspect <skill-name> --skill-root ./skills
```

**实现说明**:
- 使用 Python 标准库 `argparse`（不引入 `click`/`typer`，保持轻量）
- `run` 命令：构建 `SkillRegistry`、`SkillLoader`、`MockModel`（Phase A）、`AgentCore`，然后调用 `run()`
- `skills list`：输出 `name` | `description` | `source` 表格
- `skills inspect`：输出技能完整 frontmatter（注意：`allowed_tools` 等在此可以显示，因为是给运维人员看的）

**验收标准**:
- [ ] `skills-agent skills list` 输出技能列表，无报错
- [ ] `skills-agent run "test"` 使用 MockModel 完成一次循环并输出结果
- [ ] `--help` 输出清晰的帮助文本

---

### 任务 A4.2: CLISink（终端实时输出）

**目标**: 实现面向终端的 OutputSink，提供实时进度展示与流式文本输出

**输入**: A1.5（OutputSink 接口）

**输出文件**: `src/output/cli_sink.py`

**实现规格**:

```python
# src/output/cli_sink.py
import sys
from .sink import OutputSink
from ..agent.plan import Plan, StepStatus

# ANSI 颜色码
_DIM    = "\033[2m"
_RED    = "\033[31m"
_YELLOW = "\033[33m"
_RESET  = "\033[0m"

_STEP_ICON = {
    StepStatus.PENDING:     "○",
    StepStatus.IN_PROGRESS: "→",
    StepStatus.DONE:        "✓",
    StepStatus.FAILED:      "✗",
}

class CLISink(OutputSink):
    """
    终端输出实现。
    进度信息输出到 stderr（不污染管道），最终答案输出到 stdout。
    """

    def __init__(self, verbose: bool = False, color: bool = True):
        self._verbose = verbose
        self._color = color and sys.stderr.isatty()
        self._answer_started = False  # 是否已开始输出答案（用于首次换行）

    def _dim(self, text: str) -> str:
        return f"{_DIM}{text}{_RESET}" if self._color else text

    def _red(self, text: str) -> str:
        return f"{_RED}{text}{_RESET}" if self._color else text

    def _yellow(self, text: str) -> str:
        return f"{_YELLOW}{text}{_RESET}" if self._color else text

    def on_progress(self, action: str, detail: str = "") -> None:
        label = f"{action}: {detail}" if detail else action
        print(self._dim(f"  → {label}"), file=sys.stderr)

    def on_plan_updated(self, plan: Plan) -> None:
        print(self._dim("[Plan]"), file=sys.stderr)
        for step in plan.steps:
            icon = _STEP_ICON.get(step.status, "?")
            line = f"  {icon} {step.description}"
            if step.status == StepStatus.IN_PROGRESS:
                line = self._dim(line)
            print(line, file=sys.stderr)

    def on_observation(self, source: str, content: str) -> None:
        if not self._verbose:
            return
        preview = content[:300].replace("\n", " ")
        if len(content) > 300:
            preview += "..."
        print(self._dim(f"  [obs/{source}] {preview}"), file=sys.stderr)

    def on_error(self, message: str, recoverable: bool = True) -> None:
        if recoverable:
            print(self._yellow(f"[WARN] {message}"), file=sys.stderr)
        else:
            print(self._red(f"[ERROR] {message}"), file=sys.stderr)

    def on_session_end(self, turn_count: int, status: str) -> None:
        print(
            self._dim(f"\nCompleted in {turn_count} turns ({status})."),
            file=sys.stderr,
        )

    def on_text_chunk(self, chunk: str, done: bool) -> None:
        if not self._answer_started and chunk:
            # 首个 chunk 前确保进度行已换行
            print(file=sys.stderr, flush=True)
            self._answer_started = True

        sys.stdout.write(chunk)
        sys.stdout.flush()

        if done:
            if chunk and not chunk.endswith("\n"):
                sys.stdout.write("\n")
                sys.stdout.flush()
            self._answer_started = False
```

**实现要点**:
- 进度/计划/观察/错误 → `stderr`（不影响管道和重定向，`skills-agent run "q" > out.txt` 时进度仍显示在终端）
- 最终答案 → `stdout`（可被管道捕获）
- `color=True` 时检测 `stderr.isatty()`，非终端环境（CI/重定向）自动禁用颜色
- Phase A：`on_text_chunk` 在 `FINAL_ANSWER` 动作后被一次性调用（`done=True`，MockModel 不支持流式）
- Phase C：接入流式 JSON 解析器后，`on_text_chunk` 被逐字触发

**验收标准**:
- [ ] `skills-agent run "test"` 时 stderr 可看到 `→ load_skill: xxx` 等进度行
- [ ] 最终答案出现在 stdout，`skills-agent run "q" 2>/dev/null` 只输出答案
- [ ] `--verbose` 时 observation 可见，不带 `--verbose` 时不显示
- [ ] Plan 更新时步骤状态符号正确（○ → ✓ ✗）
- [ ] 非终端环境（管道输出）下 ANSI 颜色自动禁用

**CLI 集成**：修改 `A4.1` 中的 `run` 和 `chat` 命令，默认传入 `CLISink(verbose=args.verbose, color=not args.no_color)`，并在 `argparse` 中添加 `--verbose` 和 `--no-color` flags。

---

### 任务 A4.3: Chat 会话模式

**目标**: 实现交互式多轮对话模式，管理会话状态，在无压缩策略时支持连续追问

**输入**: A4.1（CLI 骨架）、A4.2（CLISink）、A3.3（AgentCore）

**输出文件**: `src/cli/chat.py`、`src/session/session.py`

**背景说明**:

详见 [Chat 会话管理设计](../design/chat-session.md)。Phase A 实现基础会话管理（创建、存储近期对话、读取恢复），上下文压缩在 Phase B5 实现。

**会话状态数据结构**:

```python
# src/session/session.py
from pydantic import BaseModel, Field
from typing import Optional
import uuid, time, json
from pathlib import Path

class ConversationTurn(BaseModel):
    role: str           # "user" 或 "assistant"
    content: str
    timestamp: float = Field(default_factory=time.time)

class SessionContext(BaseModel):
    """活跃上下文状态（写入 context.json，每轮更新）"""
    session_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    compressed_summary: str = ""          # 历史压缩摘要（Phase B 填充）
    recent_turns: list[ConversationTurn] = Field(default_factory=list)
    last_used_skills: list[str] = Field(default_factory=list)
    total_turn_count: int = 0
    recent_window_k: int = 3              # 近期原文保留轮数

    def build_history_messages(self) -> list[dict]:
        """
        组装发给模型的历史消息层（不含系统层和当前用户输入）。
        返回格式：[{"role": ..., "content": ...}, ...]
        层 1：历史摘要（有 compressed_summary 时）
        层 2：近期原文（最近 K 轮）
        """
        msgs = []
        if self.compressed_summary:
            msgs.append({
                "role": "user",
                "content": f"<summary>历史对话摘要：{self.compressed_summary}</summary>",
            })
        cutoff = self.recent_window_k * 2
        recent = self.recent_turns[-cutoff:] if len(self.recent_turns) > cutoff else self.recent_turns
        msgs.extend({"role": t.role, "content": t.content} for t in recent)
        return msgs

    def record_turn(self, user_input: str, assistant_response: str) -> None:
        """记录本轮对话，追加到 recent_turns，更新计数。"""
        self.recent_turns.append(ConversationTurn(role="user", content=user_input))
        self.recent_turns.append(ConversationTurn(role="assistant", content=assistant_response))
        self.total_turn_count += 1

    def estimated_recent_tokens(self) -> int:
        """粗略估算 recent_turns 的 token 数（字符数 / 4）。"""
        total_chars = sum(len(t.content) for t in self.recent_turns)
        return total_chars // 4


class SessionManager:
    """
    会话文件管理器。
    负责创建、持久化、加载会话文件（session.json / context.json / conversation.jsonl）。
    """

    def __init__(self, sessions_root: str = ".agent/sessions"):
        self._root = Path(sessions_root)

    def create(self, model_id: str = "mock", config: dict = None) -> SessionContext:
        """创建新会话，写入 session.json，返回空 SessionContext。"""
        ctx = SessionContext()
        session_dir = self._root / ctx.session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        meta = {
            "session_id": ctx.session_id,
            "created_at": time.time(),
            "model_id": model_id,
            "config_snapshot": config or {},
        }
        (session_dir / "session.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2)
        )
        return ctx

    def load(self, session_id: str) -> Optional[SessionContext]:
        """加载已有会话的 context.json，不存在返回 None。"""
        context_path = self._root / session_id / "context.json"
        if not context_path.exists():
            return None
        return SessionContext.model_validate_json(context_path.read_text())

    def save(self, ctx: SessionContext) -> None:
        """持久化 context.json（覆盖写入）。"""
        session_dir = self._root / ctx.session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / "context.json").write_text(ctx.model_dump_json(indent=2))

    def append_log(self, ctx: SessionContext, user_input: str, response: str) -> None:
        """追加一轮记录到 conversation.jsonl（原始完整记录，永久保留）。"""
        session_dir = self._root / ctx.session_id
        entry = {
            "turn": ctx.total_turn_count,
            "timestamp": time.time(),
            "user": user_input,
            "assistant": response,
        }
        with open(session_dir / "conversation.jsonl", "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def list_sessions(self) -> list[dict]:
        """列出所有会话的元信息（session.json）。"""
        result = []
        if not self._root.exists():
            return result
        for session_dir in sorted(self._root.iterdir(), reverse=True):
            meta_path = session_dir / "session.json"
            if meta_path.exists():
                result.append(json.loads(meta_path.read_text()))
        return result
```

**chat 命令主循环**:

```python
# src/cli/chat.py
import sys
from ..session.session import SessionManager
from ..agent.core import AgentCore
from ..output.cli_sink import CLISink

def run_chat(args):
    """交互式聊天模式主循环。"""
    session_manager = SessionManager(sessions_root=args.session_dir)

    # 支持 --resume <session-id> 恢复已有会话
    if getattr(args, 'resume', None):
        ctx = session_manager.load(args.resume)
        if ctx is None:
            print(f"Session '{args.resume}' not found.", file=sys.stderr)
            return
        print(f"Resuming session {ctx.session_id} (turn {ctx.total_turn_count})")
    else:
        ctx = session_manager.create(model_id=getattr(args, 'model', 'mock'))
        print(f"Chat session started. Session: {ctx.session_id}")

    sink = CLISink(verbose=args.verbose)
    core = build_agent_core(args, sink=sink)

    print("Type 'exit' or press Ctrl+C to quit.\n")

    while True:
        try:
            user_input = input("You: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nGoodbye.")
            break

        if user_input.lower() in ("exit", "quit", "q"):
            break
        if not user_input:
            continue

        # 组装含历史的消息层
        history_messages = ctx.build_history_messages()

        # 执行 Agent
        response = core.run(user_input, history_messages=history_messages)

        # 更新并持久化会话
        ctx.record_turn(user_input, response)
        session_manager.append_log(ctx, user_input, response)
        session_manager.save(ctx)
```

**AgentCore.run() 接口（完整签名）**:

```python
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

**验收标准**:
- [ ] `skills-agent chat` 启动后可连续输入多条消息，每轮独立执行
- [ ] 第 2 轮请求时，`history_messages` 包含第 1 轮的 user/assistant 消息
- [ ] `exit` 或 Ctrl+C 正常退出，不丢失已记录数据
- [ ] `context.json` 和 `conversation.jsonl` 在 `.agent/sessions/<uuid>/` 下正确生成
- [ ] `--resume <session-id>` 可加载已有会话状态继续对话

---

## Phase A 验收标准（整体）

Phase A 完成后，以下命令应全部通过：

```bash
# 单元测试
uv run pytest tests/unit/ -v

# 基础 CLI 功能
uv run skills-agent skills list --skill-root tests/fixtures/skills
uv run skills-agent run --skill-root tests/fixtures/skills "hello"

# 代码安全检查
grep -r "yaml.load(" src/  # 应无输出
grep -r "allowed_tools" src/agent/core.py  # 应无输出（控制字段不进入 Agent Core 模板）
```