# Agent Skills 系统开发计划 V2

**目标受众**: Claude Code AI 编程助手
**项目**: Agent Skills 技能系统 Python 原生实现
**参考文档**:
- [总体技术设计](file:///Users/peng/Me/Ai/skills-agent/docs/agent-skills-tech-design.md)
- [框架架构](file:///Users/peng/Me/Ai/skills-agent/docs/design/framework-architecture.md)
- [Agent Core 设计](file:///Users/peng/Me/Ai/skills-agent/docs/design/agent-core.md)
- [Skill Registry 设计](file:///Users/peng/Me/Ai/skills-agent/docs/design/skill-registry.md)
- [Skill Loader 设计](file:///Users/peng/Me/Ai/skills-agent/docs/design/skill-loader.md)
- [Model Adapter 设计](file:///Users/peng/Me/Ai/skills-agent/docs/design/model-adapter.md)
- [Tools Runtime 设计](file:///Users/peng/Me/Ai/skills-agent/docs/design/tools-runtime.md)
- [流式输出设计](file:///Users/peng/Me/Ai/skills-agent/docs/design/streaming-output.md)
- [Chat 会话管理设计](file:///Users/peng/Me/Ai/skills-agent/docs/design/chat-session.md)

---

## 开发原则

### 🤖 针对 AI 助手的特殊要求

1. **每个任务完全独立**：包含所有必要的上下文、依赖说明、数据结构定义
2. **明确的验收标准**：每个任务有清晰的"完成定义"（Definition of Done）
3. **可测试优先**：每个任务包含测试用例，便于验证实现正确性
4. **增量可运行**：每完成一个阶段，系统可独立运行并验证
5. **依赖策略**：允许 `PyYAML >= 6.0`、`requests >= 2.31`、`pydantic >= 2.0`；禁止 LangChain / LlamaIndex / AutoGPT；流式 JSON 解析器自行实现
6. **现代化工具链**：使用 `uv` 作为包管理器

### 📋 任务结构

每个任务包含：
- **目标**：清晰描述要实现什么
- **输入**：需要哪些已有代码/文件
- **输出**：生成哪些文件
- **数据结构**：涉及的核心数据结构（完整定义）
- **验收标准**：如何验证任务完成
- **测试用例**：必须通过的测试

---

## 阶段划分

### 🎯 三阶段交付目标

| 阶段 | 名称 | 核心目标 |
|------|------|----------|
| **Phase A** | 核心链路（MockModel 驱动） | 用 MockModel 跑通完整 ReAct 循环、技能发现、计划管理、事件流 |
| **Phase B** | 脚本执行 + 权限 + 上下文裁剪 | 受控执行脚本、强制工具白名单、上下文 token 管理、死循环检测、崩溃恢复 |
| **Phase C** | 真实 LLM 适配器 + 流式解析 | 对接 Anthropic API、自研流式 JSON 解析器、端到端集成测试 |

### 📅 任务编号规则

- `A0.x` = Phase A，基础设施
- `A1.x` = Phase A，核心数据结构
- `A2.x` = Phase A，技能子系统
- `A3.x` = Phase A，Agent Core + MockModel
- `A4.x` = Phase A，基础 CLI + OutputSink
- `B1.x` = Phase B，Tools Runtime
- `B2.x` = Phase B，权限与安全
- `B3.x` = Phase B，上下文管理
- `B4.x` = Phase B，稳定性（死循环 + 恢复）
- `B5.x` = Phase B，会话管理与多轮对话
- `C1.x` = Phase C，流式 JSON 解析器
- `C2.x` = Phase C，Model Adapter（Anthropic）+ 流式输出接入
- `C3.x` = Phase C，集成验证

## 依赖关系图

```
A0.1（项目结构）
  └─> A1.1（SkillMetadata）
        └─> A1.2（Plan/Action）
              └─> A1.3（Event）
                    └─> A1.4（AgentState）
                          └─> A1.5（OutputSink / NullSink）
  └─> A2.1（Frontmatter 解析）
        └─> A2.2（Skill Registry）  ← 依赖 A1.1
              └─> A2.3（Skill Loader）
  └─> A3.1（MockModel）  ← 依赖 A1.2
        └─> A3.2（ModelAdapter + parse_action_response + RetryAdapter）
              └─> A3.3（Agent Core）  ← 依赖全部 A1.x + A2.x + A3.1/A3.2 + A1.5
                    └─> A4.1（CLI 骨架）
                          └─> A4.2（CLISink）  ← 依赖 A1.5
                                └─> A4.3（Chat 模式）  ← 依赖 A4.1 + A4.2
                                      └─> B5.1（ConversationCompressor）  ← 依赖 A3.2
                          └─> B1.1（ScriptExecutor）
                                └─> B2.1（权限）
                          └─> B3.1（Context 裁剪）
                          └─> B4.1（崩溃恢复）
                                └─> C1.1（流式 JSON）
                                      └─> C2.1（Anthropic Adapter）
                                            └─> C2.2（流式接入 CLISink）
                                                  └─> C2.3（SSESink）
                                                        └─> C3.1（集成测试）
```

---

## 进度追踪

### Phase A：核心链路（MockModel 驱动）

| 任务 | 名称 | 状态 | 备注 |
|------|------|------|------|
| A0.1 | 创建项目结构与配置 | ✅ 已完成 | |
| A1.1 | SkillMetadata 数据结构 | ✅ 已完成 | |
| A1.2 | Plan、Step、Action 数据结构 | ✅ 已完成 | |
| A1.3 | Event（事件）数据结构 | ✅ 已完成 | |
| A1.4 | AgentState（Agent 状态）数据结构 | ✅ 已完成 | |
| A1.5 | OutputSink 接口与 NullSink | ✅ 已完成 | |
| A2.1 | SKILL.md Frontmatter 解析器 | ✅ 已完成 | |
| A2.2 | Skill Registry（技能注册与索引） | ⬜ 待开始 | |
| A2.3 | Skill Loader（技能内容加载器） | ⬜ 待开始 | |
| A3.1 | MockModel | ⬜ 待开始 | |
| A3.2 | ModelAdapter 接口 + 解析工具 | ⬜ 待开始 | |
| A3.3 | Agent Core 主循环（ReAct） | ⬜ 待开始 | |
| A4.1 | 基础 CLI | ⬜ 待开始 | |
| A4.2 | CLISink（终端实时输出） | ⬜ 待开始 | |
| A4.3 | Chat 会话模式 | ⬜ 待开始 | |

### Phase B：脚本执行 + 权限 + 上下文裁剪

| 任务 | 名称 | 状态 | 备注 |
|------|------|------|------|
| B1.1 | Tools Runtime（脚本执行器） | ⬜ 待开始 | |
| B2.1 | 权限强制执行 | ⬜ 待开始 | |
| B3.1 | ContextBuilder（上下文组装与裁剪） | ⬜ 待开始 | |
| B4.1 | AgentState 持久化与崩溃恢复 | ⬜ 待开始 | |
| B5.1 | 会话上下文压缩（ConversationCompressor） | ⬜ 待开始 | |

### Phase C：真实 LLM 适配器 + 流式 JSON 解析器

| 任务 | 名称 | 状态 | 备注 |
|------|------|------|------|
| C1.1 | 流式 JSON 解析器（FSM） | ⬜ 待开始 | |
| C2.1 | Anthropic Model Adapter | ⬜ 待开始 | |
| C2.2 | 流式输出接入（StreamingJSONParser → CLISink） | ⬜ 待开始 | |
| C2.3 | SSESink（API 流式输出预留） | ⬜ 待开始 | |
| C3.1 | 端到端集成测试 | ⬜ 待开始 | |

### 状态说明

| 符号 | 含义 |
|------|------|
| ⬜ 待开始 | 尚未启动 |
| 🔄 进行中 | 正在开发 |
| ✅ 已完成 | 通过验收标准 |
| ❌ 阻塞 | 有问题待解决 |

---

## 完成标准

### Phase A 完成
- [ ] 所有 A 阶段单元测试通过（`uv run pytest tests/unit/ -v`）
- [ ] `skills-agent skills list` 输出格式化技能列表
- [ ] MockModel 驱动的 ReAct 循环可完整执行
- [ ] 控制字段（`allowed_tools`）不出现在 model context 中（代码审查确认）
- [ ] events.jsonl 包含完整的动作审计记录
- [ ] `skills-agent run "test"` 执行时 stderr 可见进度行（CLISink）
- [ ] `skills-agent chat` 启动后支持至少 3 轮连续追问，历史消息正确传入 AgentCore

### Phase B 完成
- [ ] 脚本执行超时行为正确（用 `sleep 100` 脚本验证）
- [ ] 权限白名单拦截未授权工具调用
- [ ] Context 裁剪在大历史时不超出 token 上限
- [ ] 死循环检测在 dead_loop_window 内触发
- [ ] 崩溃恢复后 Plan 状态正确重建
- [ ] 10 轮对话后触发历史压缩，`compressed_summary` 非空，`recent_turns` 长度不超过 K*2

### Phase C 完成
- [ ] 流式 JSON 解析器单元测试全部通过
- [ ] Anthropic API 集成测试（tool use 模式）通过
- [ ] 真实 API 调用时最终答案逐字打印（流式输出，非等待完整响应）
- [ ] SSESink 单元测试通过，SSE 格式正确
- [ ] 端到端 CLI 可用真实模型完成一次完整任务

---

## ⚠️ 实现注意事项

### 安全红线
1. **禁止** `yaml.load()`，只用 `yaml.safe_load()`
2. **禁止** `allowed_tools` 等控制字段进入 model context
3. **禁止** `load_resource` 允许 path traversal（必须校验 `..`）
4. **禁止** 执行脚本时不设置 `cwd`（必须限制在技能目录内）

### 接口稳定性
- `ModelAdapter.next_action(messages) -> Action` 是核心接口，Phase A/B 用 MockModel，Phase C 换 AnthropicAdapter，**接口不变**
- `SkillRegistry.list_model_views()` 是安全边界，调用方不需要知道控制字段存在
- `OutputSink` 的所有方法不应抛出异常（实现者负责内部异常处理）
- `SkillLoader.load_body()` 和 `load_resource()` 均返回 `(str, dict)` 元组，`dict` 为统计报告

### 关键参数默认值（与 agent-core.md 对齐）

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `AgentCore.max_turns` | 20 | 最大 ReAct 循环轮数 |
| `AgentCore.dead_loop_window` | 6 | 动作 hash 队列长度 |
| `AgentCore.dead_loop_stall_turns` | 4 | Plan 无进展告警阈值 |
| `SessionContext.recent_window_k` | 3 | 近期原文保留轮数 |
| `ConversationCompressor.threshold_ratio` | 0.25 | 压缩触发比例 |
| `ContextBuilder.max_context_tokens` | 100_000 | context token 上限 |

### 测试策略
- Phase A/B 的所有核心逻辑必须可用 MockModel 测试，**不依赖真实 API**
- `MockModel(actions=[...])` 接受 `Action` 对象或 dict，支持 `call_count` 和 `reset()`
- 集成测试单独标记 `@pytest.mark.integration`，CI 默认跳过
