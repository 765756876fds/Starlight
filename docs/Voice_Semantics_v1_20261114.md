# Voice Semantics v1（2026-11-14）

## 范围与现有能力

基线为 a0-cleanup / 6a1f2459fff3e4e5e47fcf24a8cb9af14e11bab7。
施工前 24 个 tracked 修改、43 个未跟踪文件，暂存区为空。所有历史成果保留，原安装包不变。

| 现有 CommandType / Intent | Action / Domain | 参数与典型表达 | 安全与上下文 |
| --- | --- | --- | --- |
| MUSIC_PAUSE / MUSIC_RESUME | pause / resume，music；旧裸指令为全局媒体 | 无参数；暂停音乐、继续音乐 | 明确音乐设置 scope=music；旧“暂停／继续”沿用全局暂停快照，不是本轮语义上下文 |
| MUSIC_NEXT / MUSIC_PREV | next / previous，music | 无参数；换首歌、切下一首、上一首歌 | 已经支持，不重复开发；本轮增加原始问号与引用保护 |
| MUSIC_PLAY / MUSIC_STOP / MUSIC_TOGGLE | play / stop / toggle，music | 播放音乐、停止音乐、播放暂停 | 既有明确规则不变，不纳入本轮语义组合 |
| MUSIC_SEARCH_PLAY | search_play，music | query；播放某歌、来一首某歌 | 原规则优先；歌名可以包含否定词，不做自由组合 |
| MUSIC_VOLUME_* / MUSIC_SET_SPEED / MUSIC_SEEK | volume / speed / seek | volume、step、speed、seconds | 既有参数校验；音量／倍速缺参沿用 12 秒、一次数值追问 |
| MUSIC_SLEEP_TIMER_* | sleep_timer，music | seconds | 原有缺参追问、取消和时效不变 |
| MUSIC_*PLAYLIST / MUSIC_ADD_NEXT / MUSIC_PLAY_RANK_INDEX | playlist / next / rank，music | name、song_query、query、index | 原服务和队列执行不变 |
| MUSIC_SET_PLAY_MODE / MUSIC_HIGHLIGHT* / MUSIC_RESCAN / MUSIC_CLEANUP / MUSIC_CLEAR_QUEUE / MUSIC_IDENTIFY | mode / highlight / library / identify | 原有参数 | 不扩展动作权限，不改播放器和队列 |
| system.unknown / system.clarify / system.context_cancel | 公共保护 | 不执行或简短提示 | 现有否定、询问、转述、整句引用、多命令与追问取消继续保留 |

主要盲区不是“下一首”的同义词数量，而是对象在前的自然说法，以及无对象表达意外继承全局媒体权限。

## 最小实现

- 新增 `app/voice/music_semantics.py`：不可变候选，仅允许 pause、resume、next、previous 映射到既有 CommandType。完整句式匹配有限动作／对象词组，不推断任意词语组合。
- `IntentRouter` 先运行旧规则；只有未命中才接入明确音乐候选。旧四动作额外检查原始问号／引用标记，防止归一化删掉安全信息。
- 明确音乐候选设置 `scope=music`，复用现有 Runtime、CommandBus 和 handler。没有修改 runtime.py、registry.py、ASR、VAD、TTS、线程、电影、LoL 或 MPV。
- 不保存上一句媒体上下文，不引入 LLM、服务、线程、网络请求、配置项或运行依赖。无额外后台资源。

支持的新增表达示例：

- 暂停：音乐先别放了、把音乐先停一下、这首歌先暂停一下、歌曲先别放了、先暂停一下音乐。
- 继续：音乐接着放、接着放音乐、这首歌接着听、继续放歌曲。
- 下一首：帮我换一首歌、音乐切到下一首。
- 上一首：音乐切回上一首、换回上一首歌。

“换首歌／切下一首／上一首歌”本来就支持。

### 当前对象的保守边界

现有 Runtime 分别通过音乐／电影 provider 同步查询 IPC pause（单次上限一秒），没有可直接复用的可靠原子当前媒体快照。为避免扩展运行时，本版不启用无对象自然候选：

“先停一下／停一下／别放了／接着放／接着播放／接着听”提示明确说音乐，不执行。音乐单独存在、双媒体并存、没有媒体、状态过期时都不猜测；上一句“暂停音乐”或“继续电影”也不能授权下一句省略对象。

旧的专用“暂停／继续”全局控制语义保留，以保护既有行为。不能把这两个旧命令当作本版已实现了通用当前对象理解。

不执行：普通陈述（这首歌不好听、声音有点大）、否定、疑问／反问、转述／引用、多个动作、多个对象、不完整或重复噪声。明确电影和 LoL 命令继续走原规则。

## 影子对比与证据

先在未接入路由前，对旧规则和纯候选方案运行 175 条输入，不执行播放器：正向 20 条，旧规则命中 6、新候选方案命中 20；其余 155 条旧规则产生 7 个操作候选，新方案 0。

随后补充全角标点、书名号和括号，最终去重为 **178 条独立文本：20 正向、158 不应执行**。真实 IntentRouter 对比：

| 指标 | 修改前规则 | 最终路由 |
| --- | --- | --- |
| 正确动作 | 6 / 20 | 20 / 20 |
| 不应执行却产生操作候选 | 9 / 158 | 0 / 158 |

这是人工构造语料的命中／误触发计数，不是统计概率或真实麦克风识别率。前后解析及逐条结果保存在项目外 `starlight-semantics-v1-*` 证据目录。

## 测试

使用项目 Python，统一 `-B -X faulthandler`；测试入口将 logger、pytest tmp、JUnit 放在外部证据目录，`-o addopts= -p no:cacheprovider` 禁用默认 coverage 参数，不触碰 `.coverage`。

| 批次 | 通过 | 失败 | 跳过 | 警告 | 退出码 |
| --- | --- | --- | --- | --- | --- |
| 接入前新探针＋既有语义／领域测试 | 199 | 36 | 0 | 0 | 1 |
| 同一组合接入后 | 235 | 0 | 0 | 0 | 0 |
| 最终相关单元回归（含最终新增 105 项） | 669 | 0 | 0 | 0 | 0 |
| Qt 语音反馈与电影窗口回归 | 18 | 0 | 0 | 0 | 0 |
| 干净 HEAD＋本轮三份源码／测试的隔离候选 | 257 | 0 | 0 | 0 | 0 |

最终单元与 Qt 为 687 个不同用例；较早组合及隔离候选有重叠，不重复累计。隔离候选证明本轮不依赖历史未提交成果。首败记录保留。

最终单元文件：`test_music_semantics_v1`、`test_voice_semantics`、`test_intent`、`test_intent_v2_domain`、`test_intent_search_negation`、`test_intent_music_clear_queue`、`test_voice_adversarial`、`test_voice_dispatch`、`test_voice_movie`、`test_runtime_global_pause`、`test_music_speed_voice`、`test_music_playlist_voice`、`test_voice_decouple`、`test_voice_decouple_integration`、`test_voice_echo_guard`、`test_voice_lol_execution`（均在 tests/unit）。

实际入口命令（展开的准确参数及日志另存外部目录）：

```powershell
.venv\Scripts\python.exe -B -X faulthandler <evidence>\run_tests.py <repo> <evidence> final-regression <上述16个测试文件>
.venv\Scripts\python.exe -B -X faulthandler <evidence>\run_qt.py <repo> <evidence> qt tests/ui/test_voice_feedback_view.py tests/ui/test_movie_voice_window.py
.venv\Scripts\python.exe -X pycache_prefix=<evidence>\compile-cache -m compileall -q app/voice/intent.py app/voice/music_semantics.py tests/unit/test_music_semantics_v1.py
git diff --check
git diff --cached --check
```

Qt 入口先建立 QApplication 和 ThemeManager，设置／日志隔离；不操作真实媒体。单元执行覆盖真实 Router→Runtime→CommandBus，但 handler／IPC 是替身，不能代替实机验收。编译通过，日志未见原生异常标记。

Ruff 未完成：项目和系统 Python 都没有该模块，项目外安装工具首次下载未完成，随后官方 PyPI 有界下载 90 秒超时，未改变运行依赖。没有把无法运行的检查写成通过。

## 交付边界与实机验收

原安装包不更新。使用本轮源码：播放音乐后说“音乐先别放了”“音乐接着放”“音乐切到下一首”“音乐切回上一首”；再说“不要暂停音乐”“他说让我暂停音乐”“先暂停电影再切歌”，确认不执行；“先停一下”应提示明确对象。

真实麦克风、ASR 文本偏差、扬声器和设备播放尚未验收。不保证有限语料覆盖全部自然语言，也不启用 Voice Semantics v2。

提交、远端同步和新 SHA 对应 CI 以本轮最终回执为准，不引用旧 CI 作为本轮通过证据。
