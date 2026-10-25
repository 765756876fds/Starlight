# Starlight 2.0 — 当前状态快照

检查时间：2026-10-26
分支：a0-cleanup
HEAD：2ead507b957d08f70e9085da24809a068503f94c

## 工作区修改清单（git status --short）

```
 M app/combo/config.py
 M app/combo/manager.py
 M app/core/config.py
 M app/core/path_resolve.py
 M app/lol/data/ability_catalog.py
 M app/lol/data/champion_catalog.py
 M app/lol/data/cache.py
 M app/lol/data/item_haste.py
 M app/lol/data/item_stats.py
 M app/lol/data/lol_data_updater.py
 M app/lol/data/summoner_spell_catalog.py
 M app/lol/objectives/rules.py
 M app/speech/tts.py
 M app/ui/dialogs/ban_pick_config_dialog.py
 M app/ui/overlay/overlay_assets.py
 M app/ui/panels/combo_panel.py
 M app/ui/panels/current_game_panel.py
 M app/ui/panels/movie_panel.py
 M app/ui/panels/ongoing_panel.py
 M app/ui/panels/settings_panel.py
 M app/voice/asr_bootstrap.py
 M scripts/build.spec
?? tests/unit/test_asr_bootstrap_r2.py
?? tests/unit/test_path_migration_r1.py
```

## 最近完成的修复

- R1：用户数据目录分离（%LOCALAPPDATA%\Starlight）
- R2：ASR 双后端（vulkan → cpu 回退）
- R3：模型种子迁移（Program Files → 用户目录）
- R4：build.spec 打包配置更新
- P0-A：SSL certifi 显式 context
- P0-B：mpv 路径失效自动回退
- P0-C：current_game_panel c 变量初始化
- 第四轮：lol_data_dir() 统一入口 + Qwen Key 迁移 + SQLite backup API

## 测试结果

命令：`python -m unittest tests.unit.test_asr_bootstrap_r2 tests.unit.test_path_migration_r1`
结果：24 tests OK（1 个 Windows 文件锁导致的 teardown 清理错误，非业务问题）。

## 尚未解决的问题

1. third_party/llama/vulkan/ 和 cpu/ 不存在
2. models/*.gguf 不存在
3. 真机 ASR 双后端验证未做
4. 真机 SSL 验证未做
5. 真机 SQLite 迁移验证未做
6. 项目原有 180+ pytest 未跑

## 是否具备进入下一阶段的条件

不具备。资源缺失（llama 后端 + 模型）+ 真机未验证，不能构建安装包。
