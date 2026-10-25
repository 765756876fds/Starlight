# Starlight 2.0 — 已知问题清单

状态标签：CONFIRMED / REPORTED / FIXED_UNVERIFIED / TESTED / BLOCKED / CLOSED

## CONFIRMED（当前代码已证实）

### ISS-001：llama 后端资源缺失
- 症状：third_party/llama/vulkan/ 和 cpu/ 目录不存在。
- 证据：Test-Path 返回 False。
- 影响：build.spec binaries 引用的文件缺失，无法构建安装包。
- 状态：BLOCKED

### ISS-002：ASR 模型文件缺失
- 症状：models/*.gguf 不存在。
- 证据：Get-ChildItem 返回空。
- 影响：setup_full.iss 模型引用缺失。
- 状态：BLOCKED

## FIXED_UNVERIFIED（已修改，验证不充分）

### ISS-003：用户数据目录分离
- 症状：旧版写 Program Files，新版应写 LOCALAPPDATA。
- 修复：path_resolve.py 加 USER_DATA_DIR + migrate_legacy_user_data()。
- 验证：mock 测试通过，真机未验证。
- 状态：FIXED_UNVERIFIED

### ISS-004：ASR 双后端回退
- 修复：asr_bootstrap.py vulkan → cpu 回退。
- 验证：8 个 mock 测试通过，真机未验证。
- 状态：FIXED_UNVERIFIED

### ISS-005：SQLite 迁移
- 修复：sqlite3 backup API + 临时文件 + 原子替换。
- 验证：5 个 mock 测试通过（含 integrity_check），真机未验证。
- 状态：FIXED_UNVERIFIED

### ISS-006：Qwen Key 迁移
- 修复：migrate_legacy_user_data() 加 .qwen_key 迁移。
- 验证：3 个 mock 测试通过，真机未验证。
- 状态：FIXED_UNVERIFIED

### ISS-007：lol_data 路径统一
- 修复：lol_data_dir() 统一入口，所有读取方改用。
- 验证：3 个 mock 测试通过，真机未验证。
- 状态：FIXED_UNVERIFIED

### ISS-008：SSL certifi context
- 修复：overlay_assets.py 显式 ssl.create_default_context(cafile=certifi.where())。
- 验证：代码审查通过，真机下载未验证。
- 状态：FIXED_UNVERIFIED

### ISS-009：mpv 路径回退
- 修复：config.py mpv_path 失效自动回退到 bundled mpv。
- 验证：代码审查通过，真机未验证。
- 状态：FIXED_UNVERIFIED

### ISS-010：current_game_panel c 变量初始化
- 修复：c = None 提前初始化。
- 验证：静态检查测试通过。
- 状态：TESTED（静态）
