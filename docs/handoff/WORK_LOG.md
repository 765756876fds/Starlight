# Starlight 2.0 — 工作历史

## 2026-10-26：第四轮集中修复

**工作目标**：恢复 R1-R4 + P0-A/B/C 功能，统一 lol_data 路径，补 Qwen Key 迁移。

**修改文件**：18 个 modified + 2 个新增测试文件。

**行为变化**：
- 用户数据目录分离（Program Files → LOCALAPPDATA）
- ASR 双后端回退（vulkan → cpu）
- 模型种子迁移（Program Files → 用户目录）
- SQLite backup API 一致性迁移
- lol_data_dir() 统一路径入口
- Qwen Key 自动迁移
- SSL certifi 显式 context
- mpv 路径失效自动回退
- current_game_panel c 变量初始化

**测试结果**：24 个 unittest 通过（mock）。

**未执行**：真机 ASR、真机 SSL、真机 SQLite 迁移、全量 pytest。

**未提交**：工作区保留修改，未 commit。

## 历史记录（待核实）

- 项目原名称 → Starlight 重命名：历史记录，待核实
- EventBus 并发改造：历史记录，待核实
- OnlineResolver / MpvAdapter 可测试性改造：历史记录，待核实
- LoL 英雄填充 / 绝活缓存刷新：历史记录，待核实
- 语音打断 / ASR 后端回退：本轮已重新实现，mock 测试通过
