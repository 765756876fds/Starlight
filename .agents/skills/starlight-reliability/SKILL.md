---
name: starlight-reliability
description: 在星芒出现 Qt 跨线程更新、SQLite 关闭竞态、旧异步结果、线程或进程未释放、挂起、原生崩溃、超时重试或假成功时做定点调查和回归验证。普通文案布局任务不应因此扩展成全项目审计。
---

# 星芒 Reliability Engineer

按根目录 `AGENTS.md` 保护现场，遵守只读/修改范围。先确定可观察失败，再作最小对照；以下路径相对仓库根目录。

## 入口与判断

- Qt 请求代次和回传：`app/ui/widgets/latest_task.py`，音乐/电影面板各自的搜索 worker。只忽略旧结果，不假称撤销底层 HTTP。
- 扫描/数据库：`app/music/{scanner,database,controller}.py`。查询和 close 应使用同一保护机制；先通知任务停、有限等待并核对结果，超时不是退出成功。锁内不等待需要同一锁的任务，不在持锁时 join 或反向调用业务。
- 单实例：`app/ui/single_instance.py`。先接 readyRead，再补读 socket 已缓冲数据；保留分段消息和一次消费契约。
- 执行/反馈：`app/core/{command_bus,operation_result,notifications}.py`、`app/speech/{tts,speech_queue}.py`。检查每个出口是否完成一次、异常是否有结果、清空后晚结果是否复活。

## 调查方式

1. 保存首次命令、环境、退出码、完整日志与线程堆栈；区分 Python 断言、Qt 原生异常、超时和外部服务失败。
2. 用外部临时副本复现；对照应来自施工前实际工作区或明确提交，保持测试环境/命令一致。不得覆盖主工作区来切基线。
3. 使用 Event、Barrier、Qt socket 或真实事件循环控制竞态，不靠增加 sleep 或把异步替身变同步来证明安全。数据库/文件均隔离。
4. 原生异常使用 faulthandler/有界诊断，保留失败而非无限重跑；Qt 测试应像 main.py 一样在业务窗口前初始化 ThemeManager，并收尾自己创建的窗口、定时器和线程。不要为了规避问题禁用生产守卫。
5. 只读任务只记录；获准修复时让原失败探针转通过，再做受影响回归。无法确定根因或需要大改则停止扩展，列明唯一下一步证据。

## 验证边界

优先参考 `tests/unit/{test_music_scanner_lifecycle,test_single_instance,test_discovery_reliability,test_notifications}.py` 和对应 UI 回归。记录锁顺序、停止超时后的真实状态、回调次数和晚结果去向；不把 join 超时、进队列、None 或一次偶然通过报成完成。

报告真实缺陷、条件性风险、基线测试问题、未实测分别列出。修测试生命周期不等于修复了所有 Qt 内部原生问题；没有证据不归咎于随机偶发或新提交。
