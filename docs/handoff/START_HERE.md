# Starlight 2.0 — AI 助手接手入口

## 当前接手指引（2026-11-16）

先阅读[当前状态](CURRENT_STATE.md)、[维护汇总与优先级](PRODUCT_ROADMAP.md#当前项目状态与维护优先级2026-11-16)及[最新工作记录](WORK_LOG.md)，再重新查询Git；下方旧HEAD、资源缺失、验收和授权说明均是原日期快照，不是当前事实。

- 星芒是音乐/影视为核心的Windows本地个人娱乐助手；保留已有游戏辅助，不是通用聊天机器人。
- 正式语义路线已确定为组合语义、实时媒体情境、有界本地交互记忆与原业务执行；模型实验已结束，不自动继续正式模型集成。
- 自然语义V1已通过工程验收；语音下一首反馈已获工程及用户实机双验收并关闭。其他自然语义场景、空队列观察、历史卡顿和长期效果的边界见维护汇总。
- 本次任务仅更新文档并正常同步；旧记录中的“不提交”等属于当轮边界，不覆盖本次明确授权，也不授权下一轮开发或打包。

## 历史接手说明（原文保留）


> 本文档是未来所有 AI 助手接手 Starlight 项目的唯一入口。
> **文档可能落后于代码。接手时必须先重新检查 Git 状态和实际代码。**

## 项目基本信息

- 项目名称：Starlight（星芒）
- 定位：Python + PySide6 桌面整合应用（本地音乐/电影/LoL 辅助/语音助手）
- 项目路径：`C:\娱乐助手\Starlight`
- 当前分支：`a0-cleanup`
- 最近核实 HEAD：`2ead507b957d08f70e9085da24809a068503f94c`
- 工作区状态：有未提交修改（详见 CURRENT_STATE.md）

## 当前开发阶段

第四轮修复完成（R1-R4 + P0-A/B/C + lol_data 路径统一 + Qwen Key 迁移）。
尚未进入安装包构建阶段。

## 最重要的风险

1. **third_party/llama/vulkan/ 和 cpu/ 不存在** — build.spec 引用的 ASR 后端缺失。
2. **models/*.gguf 不存在** — Qwen ASR 模型缺失。
3. **真机验证全部未做** — ASR 双后端、SSL、SQLite 迁移都是 mock/静态检查。
4. **不能构建安装包** — 资源缺失 + 真机未验证。

## 交接文档阅读顺序

1. 本文（START_HERE.md）
2. CURRENT_STATE.md（最新状态快照）
3. NEXT_STEPS.md（下一步工作）
4. KNOWN_ISSUES.md（已知问题）
5. PROJECT_OVERVIEW.md（架构概览）
6. WORK_LOG.md（工作历史）
7. DECISIONS.md（用户已批准的方向）
8. MAINTENANCE_RULES.md（维护规则）

## 新 AI 接手检查流程

1. 先读本文 + CURRENT_STATE.md + NEXT_STEPS.md。
2. 重新检查 `git rev-parse HEAD` 和 `git status --short`，确认工作区状态。
3. 任务相关代码以当前实际文件为准，不凭文档记忆。
4. 遵守下方安全约束。

## 安全约束（用户明确要求）

- 禁止 `git reset`、`git clean`、`git stash`、`git checkout`、`git restore`。
- 禁止 `git add .`、`git add -A`。
- 禁止读取、修改、删除、暂存 `.coverage`。
- 禁止未经授权提交、推送、构建安装包。
- 禁止修改已安装的 Full 版本和用户真实数据。
- 修改前必须备份到临时目录。
- 每阶段完成后报告实际结果，不把 mock 测试说成真机验证。
