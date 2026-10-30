# Starlight 2.0 — 当前状态快照

## 2026-10-30 GitHub 同步前快照（最新；已授权提交和非强制推送）

核验时分支a0-cleanup，HEAD与实际fetch到的origin/a0-cleanup同为6d42ed40f06617562ff2fea8059c7ab4fa40bf75，无领先/落后。已按58个明确路径选择正式代码、测试、图标、README与维护/语音说明；Lite安装器辅助脚本1项和备份/实验/数据库/安装包等26个未跟踪文件不纳入，原地保留。

暂存快照隔离回归727 passed、2 failed、0 warnings。两个问题分别为既有movie pauser未注入测试、识曲测试替身缺has_handler；真实CommandBus识曲调用一次已验证，未发现新生产回归，未删除或弱化测试。README已如实记录，不称为完整稳定发行。当前实际Git状态应重新查询；下文“未提交/不推送”等为旧轮次记录，本次授权已解除指定范围限制。远端默认分支保持“么西么西”，新版README按a0-cleanup分支验证。

## 2026-10-30 两项定点修复（最新）

分支 a0-cleanup，HEAD 6d42ed40f06617562ff2fea8059c7ab4fa40bf75 未变，暂存区为空。ASCII 单引号引用保护和音乐倍速结果确认已修复；最后定向测试 82 passed，0 warnings。更大范围回归有 529 passed、1 个施工前即可复现的 movie pauser 未接线测试失败，不能称为全绿；详见 WORK_LOG。当前 35 个 tracked 修改、49 个未跟踪文件：较施工前增加的 tracked 脏文件仅 music/mpv.py、music/mpv_adapter.py、test_music_speed.py；新增未跟踪仅两份必要测试。其余既有工作及产物保留，未构建、暂存、提交或推送；待用户实机验收。

## 2026-10-30 对抗性回归补记（最新）

分支 a0-cleanup，HEAD 6d42ed40f06617562ff2fea8059c7ab4fa40bf75，暂存区为空。新增语义对抗性测试 158 项通过；同轮相关回归 771 passed、4 warnings，最后全局媒体反馈修复后受影响回归 356 passed（均有重叠，不相加）。明确询问/引用/否定边界、严格数字、追问取消、旧任务/待播反馈、真实音量步长与歌单失败反馈。能力矩阵 65 行、56 个不同意图值。仅修改必要语义代码和少量直接接口，保留所有既有成果；没有构建、提交、推送或真实设备验收。详情见 `docs/语音对抗性回归验收.md`。下方旧快照不代表当前资源或发布状态。

本轮开始 30 个 tracked 修改、43 个未跟踪文件；结束 32 个 tracked 修改、47 个未跟踪文件，暂存区仍为空。新出现的 tracked 脏文件为 speech_queue.py、test_voice_dispatch.py；新文件为 numbers.py、test_voice_adversarial.py、能力矩阵和本轮验收报告。其余既有修改与未跟踪资料保留，text.py 与用户语音说明原本已未跟踪，本轮为增量修改。

## 2026-10-30 语义层补记（优先于下方历史快照）

实际分支 a0-cleanup，HEAD 6d42ed40f06617562ff2fea8059c7ab4fa40bf75，保留多轮未提交成果。本轮涉及 voice/intent、runtime、registry、text、CommandBus、OperationResult、音乐搜索返回、电影语音搜索有效性和测试/文档。详见 `docs/语音语义层整治验收.md`，合并回归 595 passed、4 warnings。下方 2ead507 与资源缺失是历史记录，本轮未重新审计整包资源。无提交、推送、打包。

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
