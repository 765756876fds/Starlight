# Starlight 2.0 — 架构概览

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
