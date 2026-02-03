# Model Adapter 设计

本文档细化模型适配层：统一 chat 接口、结构化动作输出协议、解析与重试策略、流式（token 级）结构化输出、以及 MockModel 的实现要求。

## 1. 目标

- 不绑定供应商：可适配 OpenAI-compatible/Anthropic/本地模型
- 强制结构化动作输出，支持鲁棒解析与错误恢复
- 支持 token 级流式体验：在保持结构化 JSON 输出的前提下增量解析并回调

## 2. 结构化动作协议

- 动作类型与载荷格式
- 输出校验与纠错（缺字段、非法枚举、路径非法等）

## 3. 统一接口（非代码约定）

Model Adapter 暴露两类能力：

- 非流式：返回完整结构化对象（一次性 JSON）
- 流式：返回 chunk 迭代器（字符流/字节流），由上层进行流式解析并同时构建最终对象

语义约束：

- 每次 `Decide` 必须最终产出一个完整、可校验的 JSON 对象（动作 + 可选 plan_update）
- 流式阶段允许提前暴露局部字段（用于回显），但不得绕过最终完整校验

## 4. Token 级流式：流式 JSON 解析与路径回调

为同时满足“结构化可控”与“token 级体验”，推荐让模型输出固定 schema 的 JSON，并对流式输出做增量解析。

### 4.1 解析器能力要点

- 逐字符有限状态机（VALUE/KEY/COLON/COMMA/NUMBER/TRUE/FALSE/NULL 等）解析 JSON
- 栈维护：对象/数组/根值，保证可处理嵌套结构
- 路径维护：属性名与数组索引组成 path（例如 `["choices", 0, "delta"]`）
- 路径匹配：注册模式与回调，支持精确匹配与通配符（`*`）匹配
- realtime 模式：解析过程中实时回调
- incremental 模式：对字符串字段仅回调新增部分（delta），避免重复发送完整值

### 4.2 回调注册（路径示例）

建议在 Agent Core 或 Model Adapter 内部注册以下路径回调，用于回显：

- `$.action.type`：尽早输出“本轮准备执行的动作类型”
- `$.action.skills[*].name` / `$.action.skills[*].source`：回显选用技能
- `$.final_answer` 或 `$.final_answer.content`：以 delta 形式输出 assistant 文本
- `$.plan_update`：回显计划变更（可选）

### 4.3 失败与回退

- 若流式过程中 JSON 不可完成闭合或校验失败：
  - 记录审计事件（包含错误类型、已接收字符数、截断片段哈希）
  - 触发重试或降级（由 Agent Core 的预算与策略决定）
- 若流式回调与最终对象不一致，以最终校验通过的对象为准，回调仅用于体验

## 5. MockModel
## 3. MockModel

- 用例驱动输出动作序列
