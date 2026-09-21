# PHASE A0.6 — Cleanup Verification

> 只读验收。对象：A0.5 Cleanup（commit 
>
> `db5937e`
>
>  \+ 文档 
>
> `84a7fc1`
>
> ）。
> 本阶段未修改任何业务代码；未安装依赖；未进入 A1。
> 基线 HEAD：
>
> `84a7fc1`
>
> ，分支 
>
> `a0-cleanup`
>
> 。



***

## 1. Git 验收



* branch: `a0-cleanup`

* HEAD: `84a7fc1`（符合基线）

* log:



```
84a7fc1 docs(a0): append A0.5 cleanup execution record

db5937e chore(a0-cleanup): remove dead automation features layer, archive ptt, drop Faster-Whisper, prune stale config/docs

41f291b chore: freeze pre-cleanup workspace

fded3a6 fix(music): mpv cleanup ...
```



* 确认 `84a7fc1` 链上确实包含：


  * `db5937e` cleanup：是

  * A0 文档追加：是

  * automation features archive（→ `archive/automation_features/`）：是（`app/lol/automation/features/*.py` 在 fded3a6→HEAD diff 中以删除侧出现）

  * ptt archive（→ `archive/voice/ptt.py`）：是

  * Faster-Whisper 配置清理：是

  * 旧 `auto:` 配置清理：是

* working tree: **不干净** —— `M docs/A0_PROJECT_AUDIT.md`。

  经 `git diff` 核实：该差异为 Markdown 换行 / 行尾归一化（整段中文被重新折行，如 `本报告为**只读考古审计**结论。` 被拆成多行），**纯文档 cosmetic，无代码影响**，非本次清理引入的业务改动。建议由负责人决定提交或 `checkout -- docs/...`，本只读阶段不动它。

## 2. Git 运行时产物污染

**runtime artifact pollution: PRESENT**

已被 Git tracking 并进入 commit（`41f291b`/`db5937e`）的运行时产物：



| 类型  | 数量 | 路径                                                                |
| --- | -- | ----------------------------------------------------------------- |
| mp3 | 81 | `data/tts/*.mp3`、`data/tts_test/*.mp3`、`tools/asr_fixtures/*.mp3` |
| wav | 30 | `tools/asr_records/*.wav`                                         |
| pdf | 1  | 根目录一个中文文件名 PDF（diff 显示 `Bin 0 -> 64379 bytes`）                    |
| db  | 0  | ——                                                                |



* `.gitignore` 未覆盖 `data/tts/`、`data/tts_test/`、`tools/asr_records/`、`tools/asr_fixtures/`，因此被 `git add -A` 一并纳入。

* 这些**不影响运行，也不是代码回归**，但属仓库噪音。

* **处理建议（不执行，不改历史）**：未来单独一次提交中：① 在 `.gitignore` 增加 `data/tts/`、`data/tts_test/`、`tools/asr_records/`、`tools/asr_fixtures/`、`*.pdf`；② `git rm --cached -r` 上述路径后提交。**禁止** filter-repo /rebase/reset 改写已推送历史。

## 3. Faster-Whisper 残留审计

全项目搜索 `faster_whisper / FasterWhisper / WhisperModel / faster-whisper`：



* production import（`app/`）：**0**

* test import（`tests/`）：**0**（原 `tests/manual_test_asr.py` 已移至 `archive/tests/`）

* docs mention：`项目工作报告.md`（历史记录，保留原文，非生产）

* config mention：`settings.yaml` 中 `asr.faster_whisper` 块与 `engine` 注释项已删除 —— **0**

* requirements mention：requirements.txt 本就未列 —— **0**

* archive mention：`archive/tests/manual_test_asr.py`、`audit_asr_tts.py`（根探针，非生产）

**production Faster-Whisper reference = 0** ✅

## 4. PTT 残留审计



* `app/voice/ptt.py`：已不存在（已 `git mv` → `archive/voice/ptt.py`）。

* production import / 实例化 / 启动 / 进入 Voice runtime：**0**

* 残留：


  * `archive/voice/ptt.py`：ARCHIVED

  * `tools/run_voice_music_test.py:27`、`tools/test_e2e_voice_music.py:26`：`from app.voice.ptt import PTTMicrophone` —— 一次性探针脚本，**NON-PRODUCTION**（若运行会 import 失败，已知，不影响主链）

**结论：PTT 生产引用 = 0，标记 ARCHIVED / NON-PRODUCTION，未恢复。** ✅

## 5. Automation features 死抽象残留



* `app/lol/automation/features/`：已不存在（→ `archive/automation_features/`）。

* 生产 app/ 内 grep：`from .features`、`automation.features`、`self._features`、`.tick()` 调用：**全部 0**

* 生产实例化数量：**0**

* CommandBus 注册：**0**

* 真实执行链未变：`service._tick()` 直接调用 `self._queue` / `self._ready_check` / `self._champ_select`。

**production references to archived feature abstraction = 0** ✅

## 6. 旧 `auto:` 配置残留



* `config/settings.yaml`：旧 `auto:` 段已删除；新 `automation:` 段保留并被 main 读取。

* config loader /config model /docs/tests：无 `config.get("auto.*")` 读取。

* **旧&#x20;**`auto:`**&#x20;已完全不再被代码读取。** ✅

## 7. `feature_status()` 契约审计



* 定义：`app/lol/automation/service.py:56`。

* 当前实现：直接读 `self._config` 的布尔开关，返回

  `{"auto_queue": {"enabled": bool}, "auto_accept": {...}, "auto_ban": {...}, "auto_pick": {...}}`。

* **语义（按代码，非推测）**：表示「该功能的**配置开关被勾选 / 打开**」，即 *configured/enabled flag*。


  * 它**不**表示 worker 线程正在跑（`running`）；

  * **不**表示 LCU 已连接；

  * **不**表示当前 gameflow phase 满足执行条件。

* 调用方：仅 `tests/test_lol_automation.py:509`。**UI 当前不调用&#x20;**`feature_status()`。

* 架构问题（只提不改）：未来 UI / 状态若展示 `feature_status()`，需明确区分

  `config.enabled`（配置勾选）与 `worker.running`（线程存活）/ LCU 连接态，否则会出现「配置开着但实际没在执行」的语义混淆。列入后续设计，本阶段不修。

## 8. 核心生产链完整性

清理仅触及 `automation/service.py`、`voice/asr.py`、`voice/__init__.py`、`settings.yaml`、`main.py`（删一行 import）、README；核心链文件（`lol/runtime.py`、`lcu.py`、`live_client.py`、`state_diff`、`event_bus`、`command_bus`、`music/controller.py`、`mpv.py`、`speech/*`、`player/*`）均未在 diff 中被修改。



* LCU: **PASS**（未改）

* 2999 → GameSnapshot → StateDiff → EventBus: **PASS**（未改）

* EventBus: **PASS**

* CommandBus: **PASS**

* Voice：Mic → RMS → Qwen3-ASR → IntentRouter → Rule → CommandBus：**PASS**（仅移除 Faster-Whisper fallback，主链未动）

* Music：CommandBus → MusicController → mpv：**PASS**

* Speech：EventBus → SpeechJudge → SpeechQueue → Edge-TTS：**PASS**

* Player foundation：**PASS**（未改，`player/query_handler.py` 仅在 UI 侧被触碰，foundation 调用关系保留）

## 9. Cooldown 缺口（只确认，不修复）



* 规则可产出 `cooldown.start / cooldown.cancel / cooldown.cancel_all`（`app/voice/intent.py:122-131`）。

* `VoiceRuntime._execute_intent`（`app/voice/runtime.py:205+`）仅有 music.\* 与 alarm.\* 分支，*无 cooldown. 分支*\*。

* 即：语音 → Rule → `cooldown.*` → **断链**。

* **确认属实，标记 A2 /later，本阶段绝对不修。**

## 10. 测试



* `py_compile`：app/ + tests/ 共 **153 文件全部通过**。

* pytest：**blocked by missing dependency**（当前解释器未安装 pytest /requests/pyyaml，按要求未擅自安装）。

* 基线 314 passed / 7 skipped 为**历史归档结果，非本次新跑**，不得当作当前结果引用。

* 说明：`import requests` 在此解释器直接失败，属环境限制，非代码回归。



***

## 11. 结论

### PASS（已确认安全）



* Git 链正确（`fded3a6 → 41f291b → db5937e → 84a7fc1`），HEAD=84a7fc1

* Faster-Whisper 生产引用 = 0；PTT 生产引用 = 0；features 死抽象生产引用 = 0；旧 `auto:` 生产读取 = 0

* 核心生产链（LCU / 2999 / EventBus / CommandBus / Voice / Music / Speech / Player）全部未改、完整

* `py_compile` 153 文件通过

* `feature_status()` 仍兼容测试契约（`tests/test_lol_automation.py:509`）

### WARN（后续处理，不阻断 A1）



1. 运行时产物（81 mp3 + 30 wav + 1 pdf）已 tracking/committed，需未来单独提交 `git rm --cached` + 补 `.gitignore`（不改历史）。

2. working tree `M docs/A0_PROJECT_AUDIT.md` 为 markdown 换行归一化，doc-only，待负责人决定提交或还原。

3. `feature_status()` 仅表示 config 勾选，未来需与 `worker.running` / LCU 连接态区分（架构问题）。

4. tools/ 两个探针仍 import 已归档的 `PTTMicrophone`（NON-PRODUCTION，运行会失败）。

5. cooldown 语音桥接缺口确认存在 → A2。

6. 两套计时（GameAlarmEngine / CooldownManager）、ChampSelect 双轮询 —— REFACTOR LATER，未动。

### BLOCKER



* 无。

### Final Verdict

**A1 READY**

> A0.6 结束。未开发任何新功能，未进入 Auto Queue / Auto Accept / Auto Ban / Auto Pick，未修改业务代码。