# Starlight 2.0 — 下一步工作

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
