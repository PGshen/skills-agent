# Distribution & CLI 设计（子文档，占位）

本文档将细化技能分发与命令行：zip 技能包格式、安装/卸载/校验、技能导出、以及与 Skill Registry 的一致性策略。

## 1. zip 包格式

- 多技能打包：`<skill-name>/SKILL.md` 为每个技能的入口
- 校验：SKILL.md 必须存在；前言可解析；禁止路径穿越

## 2. 安装与卸载

- 安装：临时目录解压 -> 校验 -> 原子移动
- 卸载：按 name + source 删除

## 3. verify 与信任增强

- MVP：输出文件哈希用于人工审计
- 可选：manifest.json + 签名校验

