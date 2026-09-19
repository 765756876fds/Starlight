# V1.x Test Evidence Summary

> 只记录已有的真实证据。`REAL_TESTED` = 真实环境 + 真实场景；无证据的不写。

| Capability | Automated | Real Environment | Real Scenario | Status |
|---|---|---|---|---|
| Voice / ASR (Qwen3-ASR :8080) | PASS | PASS | 麦克风监听 + 语音识别 | REAL_TESTED |
| Rule-first Intent | PASS | PASS | 明确短句 intent≈0ms | REAL_TESTED |
| LLM fallback safety gate (`alarm.*` 拦截) | PASS | PASS | 裸陈述不建 USER_MEMORY | REAL_TESTED |
| Music control (mpv IPC + FTS5) | PASS | PASS | 语音控制播放 | REAL_TESTED |
| 2999 playerlist / items / runes / summonerSpells | PASS | PASS | 真实对局解析进 PlayerInfo | REAL_TESTED |
| GameAlarm / GameState | PASS | PASS | 对局内提醒触发 | REAL_TESTED |
| Objective 重启时间基准 (ac30c9a) | PASS | PASS | 51:40 训练局重启不补播“60秒后巢虫” | REAL_TESTED |
| TemporaryGameMemory (USER_MEMORY/SYSTEM 隔离) | PASS | PASS | 多记忆记/查/取消 | REAL_TESTED |
| CUSTOM 到期“该X了” | PASS | PASS | 训练局“定10秒后喝水”→“该喝水了”只一次 | REAL_TESTED |
| SpeechJudge / SpeechQueue | PASS | PASS | 队列上限/TTL/去重/打断 | DONE |
| ActionFeedback 直连 | PASS | PASS | “记好了，莎弥拉闪现”即时 | REAL_TESTED |

## Known gaps (not blocking)
- LCU champ-select 边界：INTEGRATED，未做大规模真人回归。
- 2999 长掉线（>2 轮）后 `state_diff.reset()` 会清 USER_MEMORY：LOW，已记录不修。
- CooldownManager 与 GameAlarmEngine 双计时历史重叠：本阶段明确不处理。
