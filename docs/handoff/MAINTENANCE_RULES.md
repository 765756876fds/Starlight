# Starlight 2.0 — 维护规则

## 每次重要修改前

1. 读取 START_HERE.md、CURRENT_STATE.md、NEXT_STEPS.md。
2. 重新检查 `git rev-parse HEAD` 和 `git status --short`。
3. 阅读任务相关的代码和问题记录。
4. 保护所有已有工作区修改，不覆盖。

## 每次重要修改后

1. 更新 WORK_LOG.md（日期、目标、修改文件、行为变化、测试结果、剩余风险）。
2. 更新 CURRENT_STATE.md（重新跑 git status --short 和 diff --stat）。
3. 更新 NEXT_STEPS.md 和 KNOWN_ISSUES.md。
4. 影响架构时更新 PROJECT_OVERVIEW.md。
5. 影响用户已批准方向时更新 DECISIONS.md。

## 测试记录规则

- 只记录真实执行的测试结果。
- 不得把 mock 测试当成真机测试。
- 不得把静态检查说成功能验证。
- 未执行的测试标记为"未执行"或"BLOCKED"。

## 换账号 / 换 AI 助手

1. 新助手必须先读 START_HERE.md。
2. 按顺序读其他交接文档。
3. 核对实际 Git 状态和代码。
4. 文档与代码不一致时，以当前代码为准，指出差异后再更新文档。
5. 不得要求用户重新讲述档案中已有且仍有效的项目背景。

## 安全红线

- 禁止读取、修改、删除、暂存 .coverage。
- 禁止 git reset/clean/stash/checkout/restore。
- 禁止 git add . / git add -A。
- 禁止未经授权提交、推送、构建安装包。
- 禁止修改已安装的 Full 版本和用户真实数据。
- 禁止关闭 SSL 验证。
- 禁止杀外部进程。
