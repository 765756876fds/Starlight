# Starlight 2.0 — 下一步工作

## 2026-10-30 本次授权同步的停止条件

仅整理、说明、测试、精确提交与非强制推送到a0-cleanup；不改默认分支、不自动合并、不构建、不开始新功能。同步前再次fetch；如远端分叉或出现新生产回归，停止推送。完成后核对远端SHA、分支README、About及CI状态，保留所有排除文件。已知两个测试问题如需收口须另行定点处理，当前不修改替身或守卫来制造通过。

## 2026-10-30 两项修复完成后的停止点

停止代码施工，先审查两项修复增量和本轮测试证据。随后仅安排源码 Windows 验收：正常自动化命令与引用/否定对照（避免真实匹配）、正常音乐倍速、UI/语音/实际速度一致、MPV 不可用或自然失败时的准确提示。不得把已有 771/356/158 历史结果冒充本轮；本轮最终定向为 82 passed，较大范围 529 passed、1 个起始快照可复现的测试失败。该既有测试是否单独处理需另行授权，不随本轮顺手修复；不自动打包、提交或推送。

## 2026-10-30 对抗性回归接续（最新）

先审查本轮增量 diff、`docs/语音对抗性回归验收.md` 和 65 行能力矩阵。源码设备验收优先：询问/否定不误执行，音量/倍速/睡眠追问取消与超时，连续不同录音“下一首”，搜索中切换指令后的反馈，缺少播放器和 TTS 时的表现。不要直接打包。若继续施工，优先讨论旧 None 返回值的具体业务分类、手动 UI 与语音操作的代次边界；不要把全局代次当作底层取消系统。保留所有已有工作区和产物。

## 2026-10-30 最新接续

先审查语义层 diff 和 `docs/语音语义层整治验收.md`，再安排设备验收：明确媒体域、数字追问 12 秒边界、否定/多指令、网络失败和连续录音。不要直接打包。更精确的音乐搜索失败分类、movie.play 内部取消及设置语音入口另行设计。下方资源缺失结论是历史记录，不可当作当前事实。

## P0（阻塞安装包构建）

### P0-1：补齐 llama Vulkan/CPU 后端资源
- 现象：third_party/llama/vulkan/ 和 third_party/llama/cpu/ 目录不存在。
- 相关文件：scripts/build.spec、app/voice/asr_bootstrap.py
- 根因：源码树中未包含 ASR 运行时二进制。
- 推荐：从旧电脑或官方 llama.cpp release 获取，放到 third_party/llama/{vulkan,cpu}/。
- 完成标准：目录存在 + llama-server.exe 可执行 + build.spec 能正确收集。
- 状态：BLOCKED

### P0-2：补齐 Qwen ASR 模型文件
- 现象：models/Qwen3-ASR-0.6B-Q8_0.gguf 和 mmproj-*.gguf 不存在。
- 相关文件：scripts/setup_full.iss、app/voice/asr_bootstrap.py
- 推荐：从旧电脑拷贝或重新下载。
- 完成标准：models/ 下两个 .gguf 文件存在 + 大小正确。
- 状态：BLOCKED

### P0-3：真机 ASR 双后端验证
- 相关文件：app/voice/asr_bootstrap.py
- 推荐：源码态用 CPU 后端真机启动 llama-server，验证识别功能。
- 完成标准：冷启动时间 + 首次识别耗时 + 连续识别正常。
- 状态：BLOCKED（依赖 P0-1）

## P1（影响用户体验）

### P1-1：真机 SQLite 迁移验证
- 推荐：用旧安装版 Program Files 下的 music.db 真机测试迁移。
- 完成标准：迁移后数据完整 + integrity_check ok。
- 状态：UNVERIFIED

### P1-2：真机 SSL 验证
- 推荐：真机跑 overlay_assets 下载英雄头像，确认 certifi context 生效。
- 状态：UNVERIFIED

### P1-3：项目原有 pytest 回归
- 推荐：pip install -r requirements.txt 后跑全量 pytest。
- 状态：NOT RUN

## P2（优化项）

### P2-1：SQLite 迁移 WAL 场景测试
- 当前 backup API 测试不覆盖 WAL 并发写入。
- 状态：UNVERIFIED

### P2-2：item_stats.py / item_haste.py 真机验证
- 已改到 lol_data_dir()，但未真机测试。
- 状态：UNVERIFIED
