# 全项目功能体检报告 —— LoL 模块

> 只读调查（2026-10-12），不改代码。覆盖：对局信息（十人/段位/战绩/绝活★）、连招、CD 提醒、装备提醒、玩家情报、自动化。数据来源：app/lol/ 全模块 + app/combo/ + app/ui/panels/current_game_panel.py + main.py 接线。

---

## 一、对局信息（十人 / 段位 / 战绩 / 绝活 Top3★）

### ① 现有功能
- **十人信息**：真人排位局已实测正常（`teamOne=5(puuid5) teamTwo=5(puuid5)`）；人机局敌方为空（bot 不进 session，数据源特性）
- **段位**：单双排 + 灵活组排（tier/division/LP），ranked 查询失败时复用旧快照降级
- **战绩**：最近 20 场（匹配+排位），胜率/KDA/连胜连败
- **绝活 Top3**：常用英雄前 3（见 ★ 详解）
- **主要位置**：出场最多位置（TOP/JUNGLE/MIDDLE/BOTTOM/UTILITY）

### ② 实现方式

**数据源**（腾讯服，全部禁系统代理直连）：
| 数据 | 来源 | 说明 |
|---|---|---|
| 身份（10 人） | LCU `/lol-gameflow/v1/session`（ChampSelect/InProgress）+ GSM 补充 | gameflow teamOne/teamTwo 缺身份时，GSM 按 self puuid 查同局（必须 gameId 同局校验才 merge，防跨局污染） |
| 段位 | LCU `/lol-league/v2/entries`（ranked_tier 等） | 双队列（solo + flex） |
| 战绩 | SGP `match-history-query` SUMMARY（entitlements token，禁代理） | fetch_count=50 拉取 → 过滤 `_RANKED_QUEUES={0,400,420,430,440,460,470,490}` → 取最近 20 |
| 熟练度（规划层） | LCU `/lol-champion-mastery` | 见玩家情报节 |

**依赖**：LCU 连接（WebSocket 禁用 → HTTP 轮询）、SGP 服务、2999 每 1s 轮询；RateLimiter 20rps + SnapshotCache 按 puuid 缓存（当前局生命周期）。
**触发链**：OngoingGameService 独立轮询 gameflow-phase → session 构建（10 人）→ loader 线程池（4 worker，每玩家并行查 summoner/ranked/history）→ PlayerSnapshot → UI 卡片。断连熔断（LCU 不可用跳过本轮不重建 session）、session 按 gameId 去重、retry 状态按 game_id 持久。

**限制**：
- 战绩只取匹配+排位（训练/大乱斗/人机不计入——设计取舍，不是缺陷）
- 战绩/段位有缓存但**跨对局不持久**（换一局重新查，首次加载 ~几秒）
- SGP/GSM 依赖腾讯服在线；离线时降级为"获取中…"（有状态机：AVAILABLE/NO_DATA/ERROR）
- 隐私：国服有隐藏战绩/隐身（summoner_privacy.py 处理，隐私玩家显示"-"）

### ★ 绝活 Top3 算法（重点展开）

**UI 实际显示的"绝活Top3"来自 loader._summarize（战绩场次聚合），不是 LCU 熟练度！**

| 问题 | 答案 |
|---|---|
| **怎么算** | 纯**出场次数降序**排序 → 同次数按**最近场次优先**（game_creation 大者在前）→ 取前 3。**胜率/KDA 完全不参与排序**（KDA/胜率只单独显示，不混合进绝活排名） |
| **样本量门槛** | **无门槛**。1 场的英雄也能进统计并可能进 Top3（20 场内若 3 个英雄各只玩过 1 场，全进 Top3） |
| **会不会误导** | 会，两个场景：① "绝活"= **玩得多**，不是"玩得好"——20 场里 15 场亚索 5 场均 60% 胜率的青钢影，Top3 把亚索排第一（青钢影可能第 2）② 边缘案例：1 场 10/0/5 的"神一场"英雄也能上榜（games=1 排在同场次后，若其他英雄也低场次则上榜） |
| **数据窗口** | 最近 **20 场**（`_recent_n=20`，从 fetch_count=50 拉取后过滤 PVE/娱乐模式再截断）；**跨对局不持久**，每局重新统计 |
| **去重/归属** | 按 champion_name 聚合（非 champion_id）；名称异常（"?"）也会计入 |

**算法代码**（loader.py:305-322）：
```python
champ_count[cname] += 1          # 次数
champ_last[cname] = max(..., m.game_creation)  # 最近场次
ranked = sorted(champ_count, key=lambda c: (-champ_count[c], -champ_last[c]))
top3 = ranked[:3]
```

**另一套"Mastery Top3"（LCU 熟练度）——已实现但未接线**：
- `champion_mastery.py` 按 **level DESC, points DESC** 取前 3（官方熟练度，含全部历史，非 20 场窗口）
- `intelligence.py` ChampionStats 排序为 **games DESC → KDA DESC**（这套排序里有 KDA 参与，但也没用上）
- **现状**：CurrentGamePanel 初始化了 `OngoingIntelligenceService`（L139）但**渲染路径从未调用**（`self._intelligence` 仅赋值不消费；L237 注释"不再做任何身份匹配、缓存反查、历史/绝活查询"）→ **两套更科学的算法躺在代码里，实际显示的是最朴素的场次计数**。这是"半成品功能"典型：规划了 P.4.x 智能分析层，接线只做了初始化。

---

## 二、连招

### ① 现有功能
- 语音"使用盲僧连招"/按键触发连招（按键序列 → 模拟按键，需管理员权限）
- 管理 UI：英雄列表/搜索/添加英雄/连招详情/编辑/删除/触发键捕获/前台门开关
- 41 个连招测试基线（combo 20 + combo_edit 15 + 扩展）

### ② 实现方式
- **数据**：config/combos.yaml（DEFAULT_YAML 内置盲僧/锤石等，aliases 支持俗称索引）
- **触发**：全局 keyboard.hook（listener 过滤修饰键 `_SKIP_KEYS`）→ ComboManager（自我触发屏蔽：is_executing + 执行后 grace 窗口）→ executor 模拟按键
- **前台门**：win_focus（仅 LoL 前台才触发，防误触）；总开关不持久化（每次默认关）
- **校验**：validation.py 44 行（非法按键/组合校验）
- **限制**：需要管理员权限模拟按键；游戏内防检测依赖"模拟人类节奏"（无注入、无读内存，红线合规）

### ③ 对标
| 能力 | Akari / 商业连招工具 | 本项目 |
|---|---|---|
| 连招触发 | 相似（按键/语音） | ✅ 齐平 |
| 防误触 | 前台门 | ✅ 齐平 |
| 连招库 | 社区大量预置 | ❌ 仅内置少数 + 用户自建（无云端/社区库） |
| 智能检测（技能是否可用再执行） | 部分有 | ❌ 无（连招是盲打，不读技能 CD——CD 是另一条语音链路） |

### ④ 增补候选
- 🟢 **连招库导入/导出**（分享 JSON）——用户自建后可备份/分享，工作量小
- 🟢 **触发前校验当前英雄**（只在"选了对应英雄"时触发，防换英雄后误触发）——需要身份数据，已有（snapshot current champion），工作量中
- 🟡 **技能 CD 联动**（CD 没好不执行连招）——需要把 cooldown 状态接进 executor，工作量大且有延迟风险
- ⚪ **云端连招社区**——需服务端，花架子

---

## 三、CD 提醒（技能冷却）

### ① 现有功能
- 语音"盲僧 R 还有几秒" / 手动记录技能 → 到点语音提醒（"敌方XX大招已好"）
- 支持 Q/W/E/R（"大招"=R 别名归一化）、敌我双方（敌方/我方/自己）
- 召唤师技能 haste 修正（明朗之靴 3158=+10、猩红明朗 3164=+20 已修、副系启迪 +18）
- 手动记录倒计时 + 固定值计算（calculator）

### ② 实现方式
- **数据源**：2999 实时快照（敌方 level/items/runes 已接，cast_snapshot 带真实 level/runes）；**敌方技能实际 CD 不可精确算**（2999 不暴露敌方小符文 → 无法判断星界洞悉等 → 放弃精确，采用"手动记录 + 固定值倒计时"）
- **触发链**：语音意图 → CooldownManager.create_reminder → Scheduler 一次性计时 → 到点 COOLDOWN_READY → TTS 播报
- **依赖**：AbilityCatalog/ChampionCatalog/SummonerSpellCatalog 静态数据表（本地 json）
- **限制**：CD 准确性依赖"记录时机"（语音说"盲僧R了"那一刻起算）；敌方 CD 是估算（无符文修正）；haste 只算装备+副系启迪的已知项

### ③ 对标
- **Akari**：不提供 CD 语音提醒（是信息面板）；本项目"语音记 CD + 语音报 CD"是对局语音助手的独有功能
- **op.gg**：赛后数据，无实时 CD
- 本质差异：本项目是"语音交互层"而非"数据面板"

### ④ 增补候选
- 🟢 **敌方召唤师技能 CD 提醒**（闪现/传送 300s 倒计时——有固定值，数据链已通，加记录入口即可）
- 🟢 **"已就绪"播报开关**（可关，防吵）
- 🟡 **精确 CD（读敌方符文）**——🔴 数据源拿不到（2999 不暴露小符文），已判死，见报告背景
- ⚪ **CD 覆盖所有技能**——需 2999 提供敌方技能等级（无），做不了

---

## 四、装备提醒

### ① 现有功能
- 敌方第一件成装（price>2000）语音播报："对面XX出了YY"
- 特殊四件（护臂 2420 / 杀人书 3041 / 中娅 3157 / 复活甲 3026）每人每件额外播一次
- 首帧静默（进场不轰炸）；每人每件只播一次（卖装重买不重播）；多人同帧合并一条语音
- 18 个测试

### ② 实现方式
- **数据源**：2999 items（dict 列表，含 price/displayName/itemID）；consumable 字段判断消耗品（缺字段当非消耗品兜底 + price>0）
- **触发链**：main.py 订阅 `game.time_update`（~1Hz）→ `EquipmentUpdateNotifier.update(snap.players)` → 状态机（每玩家 first_announced / special_announced set / initialized）→ SpeechQueue 播报；`game.ended` → clear() 重置
- **限制**：只能识别"出装时刻"（2999 每帧全量 items，靠 diff 状态判断新增）；买装瞬间到播报有 ~1s 延迟；离线/无 2999 时不工作

### ③ 对标
- **Blitz/op.gg 对局内覆盖层**：显示装备但无语音提醒；本项目的"语音报敌方出装"是对局语音助手独有
- 竞品无直接对标——**这是差异化功能**（且实现干净：纯状态 diff + 规则）

### ④ 增补候选
- 🟢 **高价值成装白名单播报**（如"对面出了无尽/破败"专门提醒，防具/输出决策）——规则已具备（price>2000 已有），加白名单列表即可
- 🟢 **敌方经济领先提醒**（装备总价差值 > X 时提醒）——数据已有（items price 求和），工作量小
- ⚪ **预测敌方下一步出装**——需要完整出装序列数据 + 模型，花架子

---

## 五、玩家情报（对局外查询 + 对局内敌方资料）

### ① 现有功能
- **对局内敌方资料**（EnemyRoster）：敌方是谁/等级/装备/召唤师技能（只读快照，不发请求）
- **玩家搜索**（player_search）：对局外输入名字查战绩/段位
- **规划层**（未接线）：Mastery Top3、胜率/KDA 聚合、常用位置、当前英雄判断（是/否绝活）

### ② 实现方式
- **数据源**：2999 快照（对局内）、SGP/LCU（对局外搜索）、champion_catalog（ID↔名称↔称号映射，214 条 ALIASES）
- **EnemyRoster 限制**：敌方 Ability Haste / Ultimate Haste 2999 不提供 → haste 一律 0（haste_source="unavailable" 记录来源，不猜）
- **触发链**：语音"查一下XXX战绩" → player_search → SGP → 摘要播报
- **规划层现状**（P.4.1/P.4.5/P.4.6）：intelligence.py（纯计算层）/ intelligence_service.py（聚合器）/ champion_mastery.py（LCU 熟练度）**全部实现完毕但 UI 渲染路径不消费**（同绝活 Top3 结论）——半成品待接线

### ③ 对标
- **Akari**：对局内 10 人面板含段位/熟练度——本项目面板已有段位+绝活（场次版）；Akari 的熟练度展示 = 本项目的 mastery_top3（未接线，接线后即齐平）
- **op.gg 赛前查战绩**：网页端完整；本项目语音版是简化（20 场摘要）
- 差距：**mastery_top3 接线**是最近的对标补齐点（代码已写好，只差调用）

### ④ 增补候选
- 🟢 **接线 mastery_top3 + KDA 参与绝活排序**（代码已存在，改 UI 消费路径 + 排序规则，工作量小）——**直接解决"1 场神一场上榜"的误导**
- 🟢 **语音问"对面中单强不强"**（聚合 胜率/场次/绝活 → 一句话结论"对面亚索 20 场 65% 胜率，是绝活"）——数据全有，加意图+模板即可
- 🟡 **对局外完整战绩详情 UI**（历史对局列表）——数据有（match_detail.py 237 行），缺 UI
- ⚪ **AI 行为预测**（预测对面打野路线）——数据源拿不到（无小地图全量视野），🔴 边界

---

## 六、自动化（自动接受 / BP / 延迟接受）

### ① 现有功能
- **自动接受**：排队 ready-check 自动点接受（可配延迟 `accept_delay`，⚙ UI 已收进 settings.yaml:90）
- **自动禁用/选择**：按 preset 优先级列表（bot/normal/flex/solo_duo 四套）自动 ban/pick
- 开关：AUTO_ACCEPT / AUTO_BANPICK 命令切换；不持久化总开关
- 状态机：IDLE→QUEUE→READY_CHECK→CHAMP_SELECT→IN_GAME（state_machine.py）

### ② 实现方式
- **数据源**：LCU HTTP 轮询（worker 线程周期 poll gameflow phase）
- **执行**：`ChampSelectActionExecutor`——**只模拟键盘/按键事件（kb 事件），不碰鼠标、不注入、不读内存（红线合规）**；ban/pick 通过 LCU 接口（action 提交）？需确认——action_executor 用 LCU request_raw 提交 champ select action
- **ReadyCheckController**：4 步流转 + accept 复核 + `_stop.wait(accept_delay)` 延迟接受
- **限制**：依赖 LCU 就绪；BP 优先级列表需用户预设（无智能推荐）；延迟接受只对"接受"生效（ban/pick 无延迟）

### ③ 对标
- **Akari**：自动接受/自动 BP 是标配——本项目已齐平
- **差距**：Akari 有更细的 BP 策略（按段位/队友英雄联动）——本项目是静态优先级列表

### ④ 增补候选
- 🟢 **BP 联动己方已选英雄**（队友先选了亚索，自动禁用不再禁亚索/自动选补位）——LCU 能读队友选择，工作量中
- 🟢 **自动秒退配置**（遇到对面绝活王/连败王自动秒）——绝活数据已有（Top3），加规则引擎
- 🟡 **BP 智能推荐**（按我方/敌方阵容推荐 ban）——需阵容数据 + 规则库，工作量中
- 🔴 **自动买装备/自动操作游戏内行为**——违反红线（不碰鼠标/不注入）

---

## 七、数据源总览 & 通用限制

| 数据源 | 协议 | 可用性 | 关键限制 |
|---|---|---|---|
| LCU（lcu.py 427） | HTTP + WebSocket（WS 已禁用） | 客户端在线即用 | 任一请求异常会触发共享连接断连窗口（已用独立直连 getter 缓解） |
| 2999（live_client.py 135） | HTTP /liveclientdata，1s 轮询 | 对局中可用 | **不暴露敌方符文/技能等级** → 精确 CD 做不了；加载界面 game_time=0 |
| SGP（sgp_provider.py） | HTTPS，entitlements token | 腾讯服在线 | 禁系统代理（国内域名）；隐私玩家查不到 |
| GSM（gsm_provider.py） | HTTPS，league-session token | 对局中可用 | 必须同局 gameId 校验才 merge；两个 token 绝不混用 |
| 静态数据（data/lol_data/） | 本地 json | 离线可用 | champions 173 英雄（含皮肤变体 241 条需去重） |

**通用红线**：不碰鼠标/不注入/不读内存——自动化与连招都在此边界内实现。

---

## 八、增补候选汇总（按优先级）

| 候选 | 标注 | 所属块 |
|---|---|---|
| **接线 mastery_top3 + KDA 进绝活排序**（代码已写好只差调用，解决"1 场上榜"误导） | 🟢 好用且能做 | 玩家情报 |
| **语音"对面中单强不强"一句话结论** | 🟢 好用且能做 | 玩家情报 |
| **敌方召唤师技能 CD 提醒（闪现 300s）** | 🟢 好用且能做 | CD |
| **高价值装备白名单播报（无尽/破败）** | 🟢 好用且能做 | 装备 |
| **连招库导入/导出** | 🟢 好用且能做 | 连招 |
| **BP 联动己方已选英雄 / 秒退配置** | 🟡 能做但工作量大 | 自动化 |
| **对局外战绩详情 UI** | 🟡 能做但工作量大 | 玩家情报 |
| **语音问战绩 → 详细面板** | 🟡 能做但工作量大 | 玩家情报 |
| **精确敌方 CD（符文修正）** | 🔴 做不了（2999 不暴露小符文） | CD |
| **游戏内自动操作（买装/走位）** | 🔴 违反红线 | 自动化 |
| **AI 行为预测（打野路线）** | 🔴 数据源拿不到 | 玩家情报 |

---

## 附：待核实项
- **action_executor 的 ban/pick 提交方式**：确认是 LCU champ-select action 接口（非键盘模拟）——影响"自动 BP 是否属于红线内"的表述（当前按"LCU 接口提交"理解，未逐行确认）
- **mastery_top3 在 PlayerSnapshot 里的传递**：UI 是否曾尝试用（L297-336 只用了 top_champions，mastery 字段存在但未渲染）——已确认未用
- **连胜/连败窗口**：_streak 是最近 20 场内连续？还是跨场？未逐行确认（影响"连胜提醒"表述）

## 建议优先做（若采纳）
1. **绝活排序升级**（🟢）：代码已存在（mastery + KDA 排序），只需接线 + 调排序——花最小成本解决"神一场上榜"的误导，直接对标 Akari 熟练度展示
2. **语音一句话情报**（🟢）："对面中单强不强"——所有数据已就绪，加意图模板即可，是对局体验最大增量
3. **闪现 CD 提醒**（🟢）：固定 300s 倒计时，数据链已通，工作量小
