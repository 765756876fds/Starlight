# 星芒（Starlight）全量代码仓库审计报告

- 审计日期：2026-10-21
- 分支：`a0-cleanup`（HEAD `3b3b0a9`）
- 审查范围：`app/`（229 个 .py）+ `main.py`，共 **47,137 行**
- 审查方式：只读静态审查（ruff 全量 + 手动静默失败/并发/架构/安全专项 + 依赖环分析）
- 技能来源：`.claude/skills/` 挂载的 `deep-review` / `codebase-review` / `vibe-audit`（触发指令已确认）
- 结论性质：本报告**不修改任何代码**，仅输出诊断与建议，供用户与主控 AI 决策

---

## 0. 健康度评分

| 维度 | 评分 | 依据 |
|------|------|------|
| 架构健康度 | 85/100 | app 内部绝对导入**无依赖环**；EventBus/CommandBus 解耦成熟；模块分层清晰 |
| 并发安全 | 70/100 | 全部线程 daemon=True（退出不卡）；EventBus 双池有 shutdown 收口；但同步 publish 与长 handler 阻塞发布者 |
| 异常处理 | 60/100 | 无裸 except；但 **203 处 except+pass 静默**、**656 处 blind `except Exception`**，异常分类缺失 |
| 日志规范 | 45/100 | `print(` **630 次 vs `logger.*` 82 次**（约 7.7:1），多数模块仍以 print 作为唯一输出 |
| 安全卫生 | 88/100 | **硬编码密钥 0 命中**；密钥三级链路（env > settings > tools/.qwen_key）统一；路径已全量绝对化 |
| 测试覆盖 | 72/100 | tests 152 个文件 vs app 229 个（文件级约 66%）；定向测试成熟，但全量组合跑测环境历史卡死未根治 |
| 风格规范 | 55/100 | ruff 命中 **2,444 条**：typing 现代化 1,104 条、blind except 656、未用 import 172 |
| Windows 平台兼容 | 80/100 | mpv 查找链、打包 frozen 路径、CWD 依赖已治理；QTimer 生命周期本轮已修复 |

**综合健康度：68/100（C+，可用，需工程化治理）。**

---

## 1. 严重问题（Critical）

### C1. `settings_panel.py` 运行期必崩：`QMessageBox` 未导入（7 处）

- 位置：`app/ui/panels/settings_panel.py:1293, 1304, 1426, 1430, 1431`
- 证据：文件 import 块（L23-28）**未导入 `QMessageBox`**，但以下真实运行路径直接使用：
  - `_on_key_save`（L1293 `QMessageBox.warning`、L1304 `QMessageBox.critical`）——设置页「保存 API Key」按钮触发
  - `_on_reset_defaults`（L1426 `QMessageBox.question`、L1430 `QMessageBox.Yes | No`、L1431 比较）——设置页「恢复默认设置」按钮触发
- 为什么是问题：Python 名称解析在运行到该行时才失败，触发 `NameError: name 'QMessageBox' is not defined` → **点击即崩溃**。ruff 已标 F821（7 处），属可复现的必崩路径。
- 成熟方案：在 L23-28 的 `PySide6.QtWidgets` import 中补 `QMessageBox`；或与本项目 Glass 风格统一，改用已导入的 `glass_warning / glass_question`（`..widgets.glass_msg` L38）。

### C2.（历史遗留，已闭环，仅登记）切 Tab 闪退与 settings.yaml 损坏崩溃

- 上一轮工程化治理已修复（commit `3b3b0a9`）：6 面板 showEvent/hideEvent 定时器治理、`core/config.py` 损坏兜底、5 处路径绝对化。本轮复检未发现回归。

---

## 2. 重要问题（Important）

### I1. 静默吞异常 203 处（except+pass / except+continue）

- 范围：全仓 `except ...: pass` 约 203 处（ruff S110 185 + 相邻判定），`try-except-continue` 13 处。
- 抽样证据：
  - `app/core/event_log.py:113` `except OSError: pass` —— 旧日志清理失败**无任何感知**，磁盘持续膨胀不可见
  - `app/music/online/manager.py:162` `except Exception: continue` —— 源取 URL 失败静默跳过，多源容灾设计合理但**失败零日志**，线上问题无从排查
  - `app/combo/listener.py:75` `except Exception: pass` —— 钩子清理幂等（低危），仍应 log.debug
- 成熟方案：统一规则「**吞异常必须写日志**」：`except X: logger.debug(..., exc_info=True)`；对「有意忽略」用 `# noqa: S110` + 注释理由；循环容灾场景至少 `logger.warning` 一次并带源名。

### I2. 日志规范：print 630 vs logger 82

- 证据：`app/music/controller.py` 107 处 print、`main.py` 89 处、`app/third_party/fingerprint/cli.py` 56 处、`app/ui/panels/music_panel.py` 34 处。
- 为什么是问题：print 无级别/无时间戳/无文件输出；用户环境（无控制台窗口）下 print 即「丢失诊断」；与已接入 `app.core.logger.get_logger()`（JSONL 落盘 logs/）的模块形成双轨，排查断裂。
- 成熟方案：全量 print → `logger.debug/info/warning/error` 迁移（保留 CLI 入口模块的 print）；为 `main.py` 与 `music/controller.py` 两个高频模块先行（占全部 print 31%）。

### I3. blind `except Exception` 656 处（BLE001）

- 证据：ruff BLE001 656 处，为全仓最大单类问题。多数在 IO/网络边界（可接受），但**业务逻辑内**亦有（如 `app/combo/manager.py:214/252/313`、`app/music/controller.py` 多处）。
- 为什么是问题：异常分类缺失——`except Exception` 把编程错误（KeyError/TypeError）与 IO 错误混为一谈，掩盖真实故障、增加排查成本。
- 成熟方案：边界按类型捕获（`(requests.Timeout, OSError)`）；业务逻辑让编程错误自然上抛；引入自定义异常层级（`AppError` 家族）。

### I4. EventBus `publish` 同步分发，长 handler 阻塞发布者

- 位置：`app/core/event_bus.py`（publish 同步遍历 handler；publish_async 双池，shutdown(wait=True) 收口）
- 为什么是问题：UI 线程 publish 时，任一慢 handler（如网络回调）会**卡住发布者**（含 UI）；双池中 handler 若阻塞，`shutdown(wait=True)` 会拖慢退出。
- 成熟方案：默认改异步发布 + 显式同步通道；handler 超时/熔断；退出时 `shutdown(wait=False)` 或对已知长任务单独收口。

### I5. 上帝对象：单文件 2000 行

- 证据：`app/music/controller.py` 2000 行、`app/ui/panels/music_panel.py` 1941、`app/ui/panels/movie_panel.py` 1740、`app/ui/panels/settings_panel.py` 1467、`main.py` 1112。
- 为什么是问题：SRP 破坏——controller 兼状态机/网络/缓存/事件/UI 驱动；面板兼布局/数据/绘制/弹窗。改一处牵全身，测试难以隔离。
- 成熟方案：按职责拆 service/repository/presenter（见架构演进 §5.2）。

### I6. 测试全量组合历史卡死（环境问题，未根治）

- 证据：`test_movie*.py` 18 文件组合在 sources 模块导入后 CPU 停滞（历史已确认非本轮引入）；单文件/过滤组合秒过。
- 成熟方案：修复测试隔离（QApplication fixture 化、网络全 Mock、`QT_QPA_PLATFORM=offscreen` 固化进 conftest）；引入 pytest-xdist/超时看门狗，杜绝「卡死拖垮整次回归」。

### I7. 可变默认参数（B008，2 处）

- `app/ui/panels/music_panel.py:1473` `def ...(..., p=None) -> ...: p.get(...)` —— 默认参数在定义时求值，若 `p` 传入 None 等状态，行为异常且难查
- `app/ui/widgets/music_card_delegate.py:151` `id()` 作默认参数 —— 同理
- 成熟方案：默认参数取 `None`，函数体内判空赋值。

### I8. `main.py` 其余工程项

- L966-969 `_startup_timer` 无 stop（空转 500ms，低危）
- 89 处 print（并入 I2）
- startup/stop 顺序逻辑健在（SYSTEM_SHUTDOWN 发布 → 各链 stop），良性。

---

## 3. 建议改进（Suggestions）

| # | 问题 | 位置 | 建议 |
|---|------|------|------|
| S1 | `typing.Dict/List/Optional` 过时用法 1,104 条（UP006/UP035/UP045） | 全仓 | 一次 `ruff check --fix`（纯风格、零行为变化） |
| S2 | import 未排序/未格式化 122 处（I001） | 全仓 | 同上 `ruff check --fix`（isort 规则） |
| S3 | 未使用 import 172 处（F401） | 多为 `app/core/__init__.py` re-export | 为 __init__ 重导出补 `__all__`；`app/combo/manager.py:24` 真实未用，直接删 |
| S4 | 字典重复键 3 处（F601） | `app/lol/ongoing/account_history.py:37/40/47`（900/430 重复，值相同） | 去重（当前无害，防未来改值不一致） |
| S5 | `RUF012`（类属性可变默认）20 处、`F841`（未用变量）16 处 | 全仓 | 按提示清理 |
| S6 | `except Exception: pass` 中「有意忽略」 | 全仓 | 加 `# noqa: S110` + 注释，与「吞错误」区分 |
| S7 | `waveform_slider.py:33-36` 循环 QTimer 无 stop | `app/ui/widgets/waveform_slider.py` | 与面板一致接入 show/hide 治理（本轮未覆盖） |
| S8 | 测试文件 152 个但无统一 fixture 入口 | `tests/conftest.py` | 固化 offscreen + QApplication + 网络 Mock 全局策略 |

---

## 4. 安全卫生结论（专项）

- **硬编码密钥：0 命中**（sk-*/AKIA/ghp_/xox/api_key 字面量全仓扫描无结果）——密钥管理走 env > `settings.yaml` > `tools/.qwen_key`（gitignore）三级链路，健康。
- **路径依赖**：本轮已全量迁移 PROJECT_ROOT 绝对化；复检 `git status` 无 CWD 依赖残留写点。
- **Windows 平台**：打包 frozen 路径、mpv 查找链、IPC 管道均已适配；UI 定时器生命周期已治理。

---

## 5. 架构演进建议

1. **短期（1-2 个迭代）**
   - 修复 C1（QMessageBox import）——一行级，立即消除必崩路径
   - `ruff check --fix` 批量清理风格（S1/S2/S3），建立 `pyproject.toml` 规则基线
   - 全量 print→logger（I2），先覆盖 `main.py` + `music/controller.py`
2. **中期**
   - 拆解上帝对象：`music/controller.py` → `MusicService`（业务）+ `MpvSession`（进程/IPC）+ `OnlineResolver`（网络）；面板 → Presenter + View 分离
   - 异常策略落地：自定义异常层级 + 统一日志中间件（I1/I3）
   - EventBus 异步化改造（I4）+ 订阅审计（`subscribe/unsubscribe` 成对自动化检查）
3. **长期**
   - 测试基建：pytest 隔离环境 + 卡死看门狗 + 覆盖率门禁（I6）
   - 引入静态类型（mypy 渐进式）与 CI（ruff + 定向回归）作为提交门禁

---

## 6. 附录：方法学与依赖

- 工具：`ruff`（已安装，v0.x 命中 2,444 条）；`mypy/bandit/pylint` 未安装 → 按用户指示**降级使用静态扫描**（ruff 规则 + 手写专项扫描脚本）。
- 技能：`.claude/skills/deep-review`（aspects：core/errors/arch/concurrency/python）、`codebase-review`（`--full --audit` 方法论）、`vibe-audit`（8 类目 + Clean Code/Vibe-Coding 双透镜）——均已读 SKILL.md 并按 sequential 路径执行。
- 扫描脚本（%TEMP%，已保留可复现）：`scan_secrets.py`（密钥）、`scan_arch.py`（依赖环/except/print）、ruff 全量输出 `ruff_out.txt`。
- 关键数字口径：依赖环分析仅覆盖 `app.*` 绝对导入（142 条边）；相对导入环未完全建模，结论以「无绝对导入环」为准。
- 本报告**未修改任何代码、未执行任何 git 写操作**（`.claude/skills/` 为新增只读克隆目录；`docs/audit_report_2026.md` 为本报告本体）。
