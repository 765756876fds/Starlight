# Voice / TTS 线程调用链审计（面向外部专家）

> 静态分析文档，基于 `app/voice/` 与 `app/speech/` 源码，仅梳理线程模型与同步语义，未修改任何代码。
> 覆盖文件：`voice/runtime.py`、`voice/asr.py`、`voice/asr_bootstrap.py`、`speech/speech_queue.py`、`speech/tts.py`、`speech/speech_judge.py`。

---

## 1. 线程全景（谁创建了谁）

| 线程 | 创建者 / 入口 | 生命周期 | 职责 |
|---|---|---|---|
| `voice-mic`（麦克风采集） | `VoiceRuntime._start_microphone()`，由 UI 开关 `toggle_mic` 触发 | 开关打开 → 关闭（daemon） | 读麦克风 → 音量阈值 VAD 切分 → 语音段入有界队列 |
| `voice-asr`（识别） | `VoiceRuntime.start()`，语音运行时启动链 | start → stop（daemon） | 串行消费队列 → ASR 转写 → 意图解析 → 命令执行 |
| `tts-loop`（asyncio 事件循环） | `TTSController.start()` | start → stop（daemon） | 承载 Edge TTS / Qwen TTS 的 asyncio 协程（`run_forever`） |
| `tts-speak`（播报） | `TTSController.speak()`，每次播报一个（包括 EventBus `SPEECH_SAY` 回调与 SpeechQueue worker 触发） | 单次播报（daemon） | 生成音频（网络请求）→ mpv 进程播放 → 清理 |
| `speech-queue-worker`（播报队列） | `SpeechQueue.start()` | start → shutdown（daemon） | 按优先级出队、处理打断、调用 `tts.speak()` |

**线程依赖关系**（单向、无环）：

```
[主线程] VoiceRuntime.start()
   ├─ 启动 voice-asr（常驻识别）
   └─ toggle_mic → voice-mic（采集）
voice-mic --queue.Queue(8)--> voice-asr --EventBus--> speech-judge(主线程回调) --SpeechQueue--> speech-queue-worker --tts.speak--> tts-speak
```

- 采集与识别**解耦**（注释场景 6/25）：mic 只读硬件 + VAD，识别线程串行消费队列。
- 识别线程消费完一段语音 → `_process_speech`：ASR 转写 → 发布 `VOICE_TEXT_RECEIVED` → 意图解析 → `_execute_intent` 发命令（CommandBus）。

---

## 2. 锁与阻塞原语

| 原语 | 位置 | 用途 |
|---|---|---|
| `queue.Queue(maxsize=8)` | `voice/runtime.py` | mic→ASR 语音段队列；**满时丢最旧一段**（用户最新指令优先语义）；stop 时排空 + `put(None)` sentinel 通知识别线程退出 |
| `threading.Lock` | `voice/asr.py`（L64/L247） | ASR 客户端内部状态与模型加载互斥 |
| `threading.Lock` | `voice/asr_bootstrap.py`（L44） | 模型下载/拉起 llama-server 的**单飞锁**（并发请求只拉起一次） |
| `threading.Lock` + `threading.Condition` | `speech/speech_queue.py` | worker 等待/唤醒；`enqueue()` 快速返回，worker 侧 `condition.wait(0.5)` |
| `threading.Lock` | `speech/tts.py`（L90） | `_state` 状态机（IDLE/GENERATING/PLAYING/STOPPING/FAILED）与 mpv 进程引用互斥 |
| `threading.Event` | `speech/tts.py` `_stop_flag` | 停止标志：`stop_playback()` 置位，`_speak_thread` 各阶段检查 |
| 代际计数器 | `voice/runtime.py` `_asr_generation`；`speech/tts.py` `_generation` | **防旧线程残留**：stop→start / 新 speak 后，旧线程下一轮发现代际过期即退出，杜绝双消费者/旧线程覆盖新状态 |
| `join()` | `voice/runtime.py` start 时 `join(timeout=1.0)` 旧识别线程；`speech_queue.shutdown()` `join(timeout=2)` | 退出收敛；均带超时，超时由代际标记兜底 |

**锁持有纪律**：所有对外部副作用（mpv 进程操作、网络生成、队列唤醒）均在**锁外**执行——`speech_queue` 明确"先退出锁再调 TTS"，`tts.stop_playback()` 锁内只改状态、锁外 terminate 进程。

---

## 3. TTS 打断机制（用户说"停止/暂停"时）

**打断链路**（两条入口，最终汇于 `TTSController.stop_playback()`）：

```
入口 A：语音命令 → CommandBus（如 MUSIC_PAUSE / SPEECH 相关）→ SpeechQueue.stop_current()
入口 B：更高优先级播报入队 → SpeechQueue._check_interrupt_needed()（锁内标记 current.cancelled + _interrupt_requested）
      └─ worker 轮询检测（_wait_for_playback_with_interrupt_check，每 100ms）→ tts.stop_playback()
```

`stop_playback()` 细节（**非阻塞、不持关键锁等待**）：
1. 锁内：状态 → `STOPPING`，`_stop_flag.set()`
2. 锁外：`_play_process.terminate()` → `wait(2s)` → 超时 `kill()` → `wait(1s)`（mpv 硬杀兜底）
3. 清理临时音频文件，状态 → `IDLE`，发布 `SPEECH_FINISHED(stopped)`（防重复）

**会不会卡住识别线程或持有关键锁？** —— **不会**：
- `stop_playback()` 自身运行在调用方线程（worker 或 UI 回调），非阻塞返回；
- `tts-speak` 线程是独立 daemon，被打断后各阶段检查 `_stop_flag` / `_generation` 自行退出；
- 识别线程（voice-asr）**从不持有 TTS 锁**；采集线程有回声抑制闸门 `_should_collect_speech()`：TTS 正在播放时，麦克风新语音段直接丢弃（不误识别自己的播报）。

---

## 4. 退出与重入

| 场景 | 机制 | 残留风险 |
|---|---|---|
| `VoiceRuntime.stop()` → 立即 `start()` | 排空队列 + sentinel；start 先 `join(1.0)` 旧识别线程；代际递增——旧线程下一轮发现 `_asr_generation != my_gen` 自动退出 | 低：join 超时（transcribe 卡 30s 超时）由代际兜底 |
| TTS 新 speak 取代旧 speak | `_generation += 1`；旧 `tts-speak` 线程在生成后/播放后/等待后三道关卡检查代际，过期即清理退出且**不发布事件**（防覆盖新线程状态） | 低：daemon 线程 + 代际双保险 |
| `TTSController.stop()` | `_running=False` + `stop_playback()` + `event_loop.call_soon_threadsafe(stop)` 停 asyncio | 中：`tts-loop` 线程 daemon 无 join（由解释器 daemon 语义保证） |
| `SpeechQueue.shutdown()` | `notify_all()` + `join(2s)` + `stop_current()` | 低：join 带超时 |
| ASR 识别失败 / llama-server 掉线 | `asr_bootstrap` 单锁拉起；`transcribe` 异常在 `_asr_loop` try/except 兜底，**采集线程不受影响**（故障隔离） | 低：识别线程继续循环 |

**静态观察（潜在死代码/改进点，供专家参考）**：
- `speech/tts.py` L101 声明 `_watcher_thread` 但**未发现实际创建点**（grep 全文件仅此一处引用）——为未启用的"播放完成监听"占位；
- `speech/speech_queue.py` `_worker_iteration` 中存在 `self._current_status = "stopping"` 的赋值（属性名与 `_current` 不符，属无效语句），打断逻辑实际由 `_interrupt_requested` + item.status 驱动，行为正确。

---

## 5. 结论

- 线程模型为**单向管道式**（mic → asr → queue → tts），无共享可变环，锁纪律良好（副作用均在锁外）；
- 打断链路**非阻塞、可中断、不反向卡识别**，代际计数器是防止旧线程残留与状态覆盖的核心机制；
- 退出路径均有 sentinel / Event / join 组合，带超时兜底；剩余风险集中在 daemon 线程无 join 的进程退出语义（由解释器保证，可接受）。
