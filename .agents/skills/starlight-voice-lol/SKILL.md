---
name: starlight-voice-lol
description: 排查或改进星芒 Qwen3-ASR、IntentRouter、CommandBus、Edge TTS、SpeechQueue、LoL LCU 连接和技能计时提醒的执行与反馈链时使用。重点处理语义安全、别名、过期反馈和业务/播报状态，不授权真实游戏自动化。
---

# 星芒 Voice & LoL

遵守根目录 `AGENTS.md`。依据本轮许可追踪一个真实场景，不重复全面语义审计；路径相对仓库根目录。

## 沿完整链定位失败

`app/voice/{asr_bootstrap,asr,text,intent,runtime}.py` → `app/core/{command_bus,operation_result,notifications}.py` → 实际业务 handler → `app/speech/{speech_judge,speech_queue,tts}.py`。LoL 再看 `app/lol/lcu.py`、`app/lol/cooldown/{manager,context,models}.py` 和 `app/ui/panels/{current_game_panel,game_workbench,cooldown_panel}.py`。

分别确认采音、ASR 实际文本、路由、参数、handler 是否调用、业务状态改变、反馈回传、合成和播放。不要把“没有听见播报”直接判成别名/计时失败；诊断只保留必要文本、异常类型及关联 ID，不保存私人录音或凭据。

## 必须保持的安全边界

- 询问、否定、引用不能变成写操作；完整单引号引用的保护在归一化丢引号前执行，普通英文撇号不误伤。不凭 ASR 相似音猜英雄或技能。
- 缺参追问、取消、领域切换和超时沿用现有上下文；相同音频回调防重，不把不同音频连续说“下一首”合并。先读当前时间窗和键，不另建语音队列。
- LoL 创建、查询、取消复用英雄归一化；别名取消不能误伤其他英雄。查询包含仍未到期的 ACTIVE/REMINDED，不全局更改 list_active 的契约；到期、取消、对局结束正常清理，提醒不能重复。
- CommandBus 的“已接受”不是“执行成功”；核对 OperationResult 和异步完成回调，不通过吞异常掩盖未执行。
- TTS 初始化、合成、播放分别观察，有界超时/重试，失败后继续处理后续播报。业务成功和 speech_status 独立；TTS 失败不重新执行业务。
- 语音反馈代次只淘汰相关旧反馈，不取消普通 LoL 提醒，也不能撤销已发出的播放器/网络操作。
- LCU 未连接、鉴权失效、暂时接口不可用和真实对局状态分别判断；测试默认内存替身，不发现/记录真实客户端令牌，不启动 hook 或自动化。

## 验证

按场景选 `tests/unit/{test_lol_cooldown_cancel_and_list,test_voice_lol_execution,test_tts_reliability,test_speech_queue,test_notifications}.py` 和已有意图正反例。证明执行次数、状态、剩余时间和失败后恢复，而非只验证方法存在或没抛异常。

用最近一次运行的新日志解释实机问题；没有原始异常就说明证据缺口。真实麦克风、扬声器、Edge 网络和 LCU 需单独验收，mock 或文本注入不能代替。无新证据不改 ASR 模型、TTS 服务或 LCU 架构。
