# Evals（评估与回归）设计

**状态**：后续阶段，暂不实现

> Phase A/B 通过 MockModel 驱动的端到端测试（pytest）验证链路正确性。
> MockModel（按序列驱动 Agent 主循环）在 Phase A 实现，但仅用于骨架验证，不涉及本文档描述的评分与回归报告能力。
> 本文档保留设计草案，供后续实现参考。

---

## 1. 评估目标

Evals 系统的目标是以**过程合规**（而非仅看最终文本）为一等指标，验证：

| 验证维度 | 说明 |
|----------|------|
| 技能触发正确性 | 用户请求 → 正确的技能被选中 |
| 动作序列合规 | `load_skill` 必须在 `run_script` 之前 |
| 渐进式披露合规 | 未被选中的技能不应被加载 |
| 权限与审批合规 | 禁止工具不得执行，审批流程必须正确 |
| 预算合规 | turn/tool_call/script 数量不超限 |
| 输出质量 | 最终答案包含期望内容 |

**事实来源**：所有验证基于 `.agent/runs/<session_id>/events.jsonl`，不依赖主观判断。

---

## 2. 评估分层

| 层级 | 名称 | 工具 | 说明 |
|------|------|------|------|
| L0 | 纯单元 | pytest | 纯函数：frontmatter 解析、路径校验、流式 JSON 解析 |
| L1 | MockModel 端到端 | pytest + MockModel | 验证执行链路：状态机、接口、事件流格式 |
| L2 | 真实模型（可选） | evals runner | 验证触发质量与输出稳定性 |

Phase A/B 中 L0 + L1 已通过 pytest 覆盖。L2 在后续阶段实现。

---

## 3. 用例格式（Case Spec）

每个用例一个目录：

```
.agent/evals/<suite>/<case_id>/
  case.json      # 用例定义
  inputs/        # 输入文件（可选）
  expected/      # 期望产物（可选）
```

**case.json**：

```json
{
  "id": "basic_select_skill",
  "description": "用户请求填写 PDF 表单，期望触发 pdf-form-filler 技能",
  "input": "请把这个 PDF 表单填好",
  "setup": {
    "skill_roots": [{"source": "project", "path": "tests/fixtures/skills", "priority": 0}]
  },
  "mock": {
    "actions": [
      {"type": "update_plan",   "params": {"plan": {"goal": "填写 PDF 表单", "steps": [{"id": "s1", "description": "加载技能", "status": "pending"}]}}},
      {"type": "load_skill",    "params": {"skill_name": "pdf-form-filler"}},
      {"type": "run_script",    "params": {"skill_name": "pdf-form-filler", "script": "scripts/fill.py", "args": []}},
      {"type": "final_answer",  "params": {"content": "表单已填写完成"}}
    ]
  },
  "expect": {
    "skills_all_of": ["pdf-form-filler"],
    "action_sequence": [
      {"must_occur": "load_skill", "before": "run_script"}
    ],
    "output_contains_any": ["表单", "完成"],
    "budget": {"max_turns": 8, "max_tool_calls": 10}
  }
}
```

---

## 4. 评分维度与规则

### 4.1 技能触发（Trigger）

从 `events.jsonl` 提取所有 `SKILL_LOADED` 事件的 skill name 列表：

- `skills_all_of`：期望技能必须全部出现，否则 fail
- `skills_any_of`：至少出现一个，否则 fail
- `skills_none_of`：指定技能不得出现，出现则 fail

### 4.2 动作序列合规（Sequence）

从事件流重建 Action 序列：

```python
# 约束示例：load_skill 必须在 run_script 之前
constraints = [
    {"must_occur": "load_skill", "before": "run_script"},
    {"forbid": "write_file"},
]
```

### 4.3 预算合规（Budget）

事件流统计：turn 数、tool_call 数、script 执行数，与 case.json 中的 `budget` 对比。

### 4.4 输出质量（Output）

从 `FINAL_ANSWER` 事件提取 `content` 字段：

- `output_contains_any`：至少含其中一个字符串
- `output_contains_all`：必须全部包含
- `output_regex`：正则匹配

### 4.5 渐进式披露合规

- 未被 `load_skill` 动作触发的技能不应出现 `SKILL_LOADED` 事件
- 未被 `load_resource` 动作触发的资源不应出现读取记录

---

## 5. 评估引擎接口（草案）

```python
# src/evals/runner.py（后续实现）
class EvalsRunner:
    def run_case(self, case: dict) -> "EvalResult":
        """
        1. 用 case.mock.actions 构造 MockModel
        2. 构造 AgentCore（注入 MockModel + NullSink）
        3. 执行 AgentCore.run(case.input)
        4. 读取生成的 events.jsonl
        5. 按 case.expect 评分
        6. 返回 EvalResult
        """
```

---

## 6. 报告格式（草案）

**JSON 报告（机器可读）**：

```json
{
  "suite": "smoke",
  "case_id": "basic_select_skill",
  "pass": true,
  "scores": {
    "trigger":  {"pass": true,  "matched": ["pdf-form-filler"]},
    "sequence": {"pass": true},
    "budget":   {"pass": true,  "turns": 4, "tool_calls": 2},
    "output":   {"pass": true,  "matched": ["表单"]}
  },
  "failures": []
}
```

**Markdown 报告（人读）**：汇总通过率、失败用例列表、关键失败原因与事件行号引用。

---

## 7. 与现有测试的关系

| 测试类型 | 位置 | 说明 |
|----------|------|------|
| L0 单元测试 | `tests/unit/` | 已在 Phase A 实现，覆盖所有公共接口 |
| L1 端到端测试 | `tests/integration/` | 已在 Phase A 实现，MockModel 驱动 |
| L2 Evals runner | `src/evals/` | 后续阶段，自动化评分报告 |

Phase A/B 的 pytest 覆盖 L0 + L1，已能验证大部分链路正确性。Evals runner 主要价值在于：

1. 批量运行大量用例并生成结构化评分报告
2. 真实模型触发质量评估（precision/recall）
3. 回归测试：模型版本升级后验证行为不退化
4. 流式一致性验证：delta 回调拼接结果与最终答案一致
