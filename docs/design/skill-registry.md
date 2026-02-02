# Skill Registry 设计（子文档，占位）

本文档将细化技能注册表：多根目录扫描、元数据解析（YAML 前言子集）、冲突解决、缓存与刷新策略、以及暴露给 Agent/模型的技能索引格式。

## 1. 输入与输出

- 输入：skill roots（项目级/用户级/内置级）
- 输出：Skill Index（仅元数据）

## 2. 扫描与冲突解决

- root 优先级：project > user > builtin
- 同名技能冲突：默认取高优先级；支持显式 source 覆盖

## 3. 元数据解析与安全约束

- 必需：name/description
- 可选：allowed-tools/disable-model-invocation/user-invocable/version/author
- 安全：frontmatter 字符约束、复杂结构拒绝策略

