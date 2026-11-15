# 星芒基础语义对照实验（仅研发）

本包不被正式语音运行时导入。不调用CommandBus、播放器、偏好数据库或ASR/TTS。
模型只提取四字段基础语义；A、B直接检索、B特征辅助、C统一交给同一个纯Python情境决策器。

## 复现

使用新的外部目录，不覆盖已有实验：

```powershell
python -B -m tools.semantic_light.run --output D:\new-semantic-experiment
python -B -m tools.semantic_light.run --data tools/semantic_light/confirmation_data --output D:\fresh-confirmation
python -B -m tools.semantic_light.analyze D:\new-semantic-experiment --output D:\new-semantic-analysis
python -m pytest tests/unit/test_semantic_light.py -o addopts= -p no:cacheprovider --basetemp=D:\isolated-tests
```

运行器只使用已存在的Qwen3-Embedding-0.6B-Q8_0与Qwen3.5-0.8B-Q8_0、现有llama Vulkan二进制。
复用上轮的进程所有权、250ms进程RAM/GPU采样、last pooling、L2归一化和有界读取。
本地loopback请求不使用代理，不改变代理设置；模型不下载，不启动外网服务。

## 固定协议与局限

- 新160条合成语料，40开发/120保留；两者无原句重叠，保留原句不进入向量原型。第一轮data和结果完全不改。
- 标准语义和动作计划分别编写；oracle单元测试只验证共同决策器，不运行文本解析器，不作为模型成绩。
- 作者预先知道产品例子并同时编写词汇与语料。这是有限领域、组合措辞评估，不是第三方盲测或开放中文理解证明。
- A使用概念词、形态、评价/指令作用域、目标提示和语言行为组合；不保存整句→业务命令表。
- B每个基础概念多原型+开发样本，使用真实向量；direct和hybrid共享同一次向量请求。hybrid公开借用A的否定/转述/目标/评价与保存特征，不能把增益全部归因Embedding。
- B阈值仅在开发集按“严格正确-2×错误执行建议”选择并在保留推理前写盘。相似度不是概率。
- C只给用户原话和简短语义定义，无播放器状态，无业务参数；原生聊天模板、非思考、temperature0、seed42、96输出token、Schema约束。
- 初次零样例C在开发集即信号空塌缩，完整原始40/120结果保留在外部。本包当前版本仅用两条原开发样本校正示例格式；最终比较使用另冻80条全新确认题，A与B也参加。不能把校正后的旧120条重测冒充未见测试；本轮没有重测旧120。两个实验协议及实际源码分开保存。
- 确认前oracle检查发现“暂停+保存”组合还需要拒绝，统一决策器增加同样保护；没有修改A词汇来修正首轮错误。这个改动对三方案一致，初轮/确认轮不能称严格单变量消融。
- 所有方案状态只由同一decide读取；名字仅从原文书名号提取，类型仅从明确词及合成偏好提取，候选/历史只用合成稳定ID。没有业务API调用。
- `movie.search`、`movie.select_candidate`等是模拟计划标签，不宣称生产CommandBus已经注册。正式连接必须另行审计、授权与实现。
- 用户已确定的评价切歌、喜欢并保存、电影停止后替代推荐只在模拟中表达。weak_feedback是输出建议，绝不写用户数据库。
- 主指标为四字段语义精确一致、动作/处置正确、动作+参数+弱反馈严格一致。错误可重叠：语义错误、oracle决策错误、参数错误；字段差异但动作相同单列。
- 性能使用QPC，不将相似度当置信概率；A为独立导入开销与实验Python工作集，模型RAM/WSS/private/GPU分列，不相加。
- 每个模型一次完整评估、一个请求在途、每模型20分钟；单请求30秒；超时可记录失败，不无限重试。模型进程统一退出并检查端口。
- 既有ASR保留；空闲共存不等于活动ASR并发竞争已测。机器非独占，未清OS文件缓存。

原始预测、模型日志、采样和报告在外部目录；GGUF、私人历史、数据库、原始音频、大产物不进入Git。
