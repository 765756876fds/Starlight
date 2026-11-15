# 星芒独立语义实验

这是研发工具，不属于应用运行时。模型只产生候选，代码不调用播放器、CommandBus.execute、用户偏好库或真实麦克风。不要将实验安全门直接作为生产授权器。

## 复现

使用项目现有Python环境和本地模型，不下载模型。输出必须使用新的项目外目录；已有语料/协议/运行记录拒绝覆盖。

```powershell
python -B -m tools.semantic_lab.gguf_inspect C:\StarlightModels\Semantic --hash --output D:\semantic-run\models.json
python -B -m tools.semantic_lab.audit --output D:\semantic-run\capabilities.json
python -B -m tools.semantic_lab.run --models C:\StarlightModels\Semantic --engine third_party\llama\vulkan\llama-server.exe --output D:\semantic-run\experiment
python -B -m tools.semantic_lab.analyze D:\semantic-run\experiment --inventory D:\semantic-run\models.json --output D:\semantic-run\analysis
python -m pytest tests/unit/test_semantic_lab.py -o addopts="" -p no:cacheprovider --basetemp=D:\semantic-run\pytest --junitxml=D:\semantic-run\junit.xml
```

先自行创建输出父目录。`MODELS`只对应本轮实际发现的四个文件名；换一组本地文件时先检查GGUF清单，不凭相似名称猜模型。两个mmproj是视觉投影，文本实验不加载。

## 固定协议

- 160条完全合成语料，10类，每类4开发+12保留，共40/120。数据和动作白名单的SHA256在`data/frozen.sha256.json`。
- 标准答案是本轮明确的产品实验策略，不是普遍语言真理。特别是“不好听”在推荐场景可候选下一首/负反馈，“一般般”只有虚拟习惯开关为真才候选切歌；没有创建真实习惯数据库。
- 同一句话在不同媒体状态下有不同标准答案。部分开发/保留词面相同而状态不同，分析额外给出真正新措辞子集；不声称完全独立词面泛化。
- 原有规则基线使用真实IntentRouter，只对合成状态模拟对应运行时情境分支，绝不发IPC或业务命令。测试含尚未接通语音的业务与新表达，因此基线分数不能当作既有命令可靠率。
- Embedding使用真实Qwen向量、last pooling、EOS元数据、L2归一化和余弦检索。动作描述各2条，加40条开发样例；槽位由披露的有限规则提取。阈值/差距仅使用开发集选择，保留集调用前写入冻结校准文件。相似度不是概率。
- 三个生成模型共用同一系统提示、两个开发样例、嵌入GGUF的聊天模板与JSON Schema；非思考，temperature=0、seed=42、上下文4096、最大输出256。显式禁止复用提示KV缓存。格式约束不代表语义正确。
- 每次只运行一个实验模型。已有ASR服务不停止、不改端口。服务器仅绑定127.0.0.1，使用随机临时认证，不使用系统代理，不改网络设置；`--offline`禁止自动下载。
- 读取/推理请求45秒、模型加载90秒、每模型总30分钟、退出等待5秒。低于768MiB可用物理内存停止。实验结束回收自己启动的进程并核对端口。

## 统计边界

原始预测、固定答案、协议和逐250ms的进程资源采样保留在外部输出中，不自动提交。`analyze`不调参，不改变标准答案：格式无效必须算错误，安全退化不能算理解成功。

同时给出主动作/动作加参数的严格正确数、否定/转述/非命令、隐含意图、错误动作候选、过滤前后结果。`active_candidate`额外区分“否定动作主题”与“肯定可执行候选”；不要仅因输出里出现pause就认定会误执行。

RAM使用Windows实际working set、private bytes及OS峰值；GPU采用该PID的PDH dedicated/shared计数，250ms峰值是采样峰值而非驱动精确高水位。不要将working set/private/GPU共享内存相加。测量模型服务进程，Python实验进程另记。保留正常桌面和已有ASR负载，非独占实验机；冷启动指新进程，不清系统文件缓存。

Windows Python3.11的monotonic可能把亚毫秒规则处理取整为0；额外使用perf_counter_ns进行有界规则延迟测量，不能把0ms当作真实值。

本轮原始推理源码哈希与快照另存。实验工具单元测试发现并修正了格式失败统计和采样初始化失败的进程回收分支；没有据保留集改变提示词、动作原型或槽位规则。原始结果不覆盖，最终分析记录统计版本。

## 官方方法参考

- [Qwen3 Embedding：instruction、last-token pooling与归一化](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B)
- [llama.cpp server：JSON Schema及聊天模板参数](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)
- [Qwen3.5-0.8B：非思考模式说明](https://huggingface.co/Qwen/Qwen3.5-0.8B)

不将模型、私人数据、安装器、日志、进程转储或大体积原始产物加入Git。结果只能支持下一步讨论，不能据本实验直接给模型业务执行权限。
