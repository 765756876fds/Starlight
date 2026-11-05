# Starlight 2.0 — 架构概览

## 2026-11-05 用户通知边界

- `core/notifications.py` 是有限内存记录：关联操作、合并阶段、保留 200 条，业务状态和播报状态分开。没有数据库、原始日志解析或业务任务队列。
- 语音入口创建操作标识，`ContextVar` 仅传递通知上下文；`Command.notification_id` 让现有异步命令保留关联。CommandBus 的业务返回值不变，通知观察异常只记日志，不能改变已完成的业务结果。
- `SpeechItem.notification_id` 关联播报状态。原有优先级、过期和独立游戏提醒保留；播报失败只更新附注，不改变业务状态或重新执行命令。
- Qt 的 `NotificationStream` 将后台变化排队回 GUI；原控制台入口改为通知中心，StatusBar 的胶囊只展示当前交互，点击进入已有通知页。没有增加顶层 TAB 或第二套语音服务。
- 新组件的语义颜色及图标角色集中在 `ui/theme/notification_style.py`。目前接入暗色主题，浅色调色板只作为未来统一适配接口，未增加主题切换功能。
- `LogBus`、TeeStream 和原有日志落盘机制保持；普通通知不订阅原始技术日志。开发人员仍从既有日志文件排查问题。

## 定位

Starlight（星芒）是一个 Python + PySide6 桌面整合应用，整合本地音乐播放、电影播放、LoL 游戏辅助（连招/对局信息/英雄数据）和语音助手（ASR + TTS）。

## 技术栈

- GUI：PySide6 (Qt)
- 播放器：MPV（外部进程 + IPC）
- 数据库：SQLite（music.db / player.db）
- LoL 接口：LCU（League Client）+ Live Client API（2999 端口）
- ASR：Qwen3-ASR + llama-server（vulkan/cpu 双后端）
- TTS：Edge TTS / Qwen TTS
- 打包：PyInstaller onedir + Inno Setup

## 主要模块

- app/core/：配置、路径解析、日志、事件总线
- app/music/：音乐播放、扫描、队列
- app/movie/：电影播放、海报缓存
- app/lol/：LoL 相关（连招、对局信息、英雄数据、LCU）
- app/voice/：ASR 启动器、语音识别
- app/speech/：TTS
- app/ui/：Qt UI 面板和对话框

## 路径解析（R1 后）

- 程序资源目录（只读）：frozen 态 = exe 目录（Program Files\星芒）
- 用户数据目录（可写）：frozen 态 = %LOCALAPPDATA%\Starlight
  - config/settings.yaml
  - data/music.db, player.db
  - data/lol_data/（用户更新的英雄数据）
  - data/movie/posters/（电影海报缓存）
  - models/（ASR 模型）
  - logs/
  - tools/.qwen_key

源码模式：两者都在项目目录。

## 已知约束（待核实）

- EventBus 并发模型：待核实
- mpv 生命周期：子进程管理，待核实
- Qt 线程安全：待核实
