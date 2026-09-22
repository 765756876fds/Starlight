# Overlay UI Design Spec - H.4.4-R3

## 1. 参考产品研究

### 1.1 Rift Companion (macOS)
**借鉴点：**
- 只读设计：不修改游戏，只展示信息
- 侧边面板：贴在游戏窗口右侧
- 信息密度：每个英雄一行，紧凑布局
- 召唤术技能 CD 追踪：点击标记使用，自动倒计时
- 透明度可调：不遮挡游戏视野

**设计模式：**
- 五人纵向排列
- 每行：Champion Portrait + 两个 Summoner Spell Icon
- CD 状态用颜色/透明度区分
- 无多余文字，纯图标为主

---

### 1.2 Chronokeeper (Windows)
**借鉴点：**
- 轻量级：不占用屏幕太多空间
- 点击交互：点击一次图标记录技能使用
- 视觉进度条：CD 用进度条显示剩余时间
- 语音命令：支持语音触发记录
- 触发提醒：CD 好时语音提醒

**设计模式：**
- 图标 + 进度条 + 倒计时数字
- READY 状态高亮，COOLDOWN 状态变暗
- 自动隐藏 / 可调透明度

---

### 1.3 OP.GG Desktop (Spell & Skill Tracker)
**借鉴点：**
- 技能追踪：召唤师技能 + 英雄 Q/W/E/R
- 动态 CD：根据装备、符文、等级调整 CD
- 团队共享：多玩家同步数据
- 透明度调节：用户可调整 overlay 透明度

**注意：** OP.GG 官方已说明旧的 spell & skill tracker overlay 因 Riot 政策变化有调整。

---

### 1.4 LeagueAkari
**借鉴点：**
- LCU API 数据驱动：不读内存，只走官方 API
- 多窗口架构：主窗口 + 辅助窗口 + Overlay 窗口
- 配置化：窗口位置、大小、透明度可配置
- 开源 MIT 协议

---

### 1.5 lol-summoners-overlay (GitHub)
**借鉴点：**
- 纯图标设计：不显示文字，纯图标 + 倒计时
- 紧凑布局：5 人纵向排列，每行高度很小
- ImGui/DX11：高性能渲染，不影响游戏帧率

---

## 2. Layout 设计

### 2.1 整体布局
```
屏幕右侧，垂直排列五名敌方英雄

┌─────────────────────────────────┐
│ [Portrait]  [Q][W][E][R] [F][I] │  ← Enemy 1
│ [Portrait]  [Q][W][E][R] [F][I] │  ← Enemy 2
│ [Portrait]  [Q][W][E][R] [F][I] │  ← Enemy 3
│ [Portrait]  [Q][W][E][R] [F][I] │  ← Enemy 4
│ [Portrait]  [Q][W][E][R] [F][I] │  ← Enemy 5
└─────────────────────────────────┘
```

### 2.2 单行结构
```
┌─────────────────────────────────────────────────────┐
│ [48px]  [24px][24px][24px][24px]  [32px][32px]    │
│ 头像     Q    W    E    R           Flash Ignite   │
└─────────────────────────────────────────────────────┘
```

**从左到右顺序：**
1. Champion Portrait (48px)
2. Q/W/E/R Skill Icons (各 24px)
3. Summoner Spell Icons (各 32px)

**为什么这个顺序：**
- 头像最左，方便识别英雄
- Q/W/E/R 在中间，方便看技能状态
- 召唤师技能在最右（靠近屏幕边缘），方便点击

---

## 3. Icon Size 规范

| 元素 | 尺寸 | 说明 |
|------|------|------|
| Champion Portrait | 48x48 px | 圆形，略大于技能图标 |
| Q/W/E/R Skill Icon | 24x24 px | 较小，不占太多空间 |
| Summoner Spell Icon | 32x32 px | 比 Q/W/E/R 大，方便点击 |
| Countdown Text | 10px | 小号，半透明，不抢视线 |
| 行间距 | 4px | 紧凑，但不拥挤 |
| 卡片内边距 | 4px | 最小 padding |

**DPI 适配：**
- 基准：1080p 分辨率下的尺寸
- 高 DPI (1.5x/2x)：按比例放大
- 不写死像素值，用相对单位

---

## 4. Transparency 规范

| 元素 | Alpha 值 | 说明 |
|------|----------|------|
| 背景 | 0.0 ~ 0.15 | 几乎透明，只用于分隔行 |
| Champion Portrait | 0.75 ~ 0.85 | 略透明，但仍可识别 |
| Q/W/E/R Ready Icon | 0.60 ~ 0.70 | 半透明，不抢视线 |
| Q/W/E/R CD Icon | 0.40 ~ 0.50 | 更暗，表示 CD 中 |
| Summoner Spell Ready | 0.80 ~ 0.90 | 较亮，方便点击 |
| Summoner Spell CD | 0.50 ~ 0.60 | 变暗，表示 CD 中 |
| Countdown Text | 0.60 ~ 0.70 | 半透明，不醒目 |

**设计原则：**
- READY 状态 → 较亮
- COOLDOWN 状态 → 较暗
- 整体"融进游戏"，不是"贴在游戏上"

---

## 5. State 定义

| 状态 | 视觉表现 | 说明 |
|------|----------|------|
| **READY** | 图标正常亮度，清晰 | 技能可用 |
| **COOLDOWN** | 图标变暗 + 倒计时数字 | 技能 CD 中，显示剩余时间 |
| **UNAVAILABLE** | 图标 50% 透明度 + 灰色 | 不知道是否有这个技能 |
| **UNKNOWN** | 图标 30% 透明度 + 占位 | 数据缺失，未识别 |

**状态转换：**
```
UNKNOWN → (读取到数据) → UNAVAILABLE → (确认有这个技能) → READY
READY → (点击记录使用) → COOLDOWN → (CD 结束) → READY
```

---

## 6. Cooldown Display

### 6.1 倒计时显示方式
**推荐方案：图标右下角小字**
```
┌──────┐
│ [F]  │
│      │
│  4:32│  ← 右下角，10px，半透明
└──────┘
```

**为什么这个方案：**
- 不破坏图标整体形状
- 倒计时不抢视线
- 位置固定，用户习惯后自然会看右下角
- 比进度条更轻量

### 6.2 时间格式
- < 60 秒：显示秒数，如 "45"
- >= 60 秒：显示分:秒，如 "4:32"
- 不显示毫秒，不显示小数点

---

## 7. Interaction 设计

### 7.1 点击行为
| 状态 | 左键点击 | 说明 |
|------|----------|------|
| READY (召唤师技能) | 记录技能使用，开始 CD | 主要交互 |
| COOLDOWN (召唤师技能) | 不响应 / 重新记录 | 避免重复记录 |
| Q/W/E/R | 暂不开放点击 | 后续阶段再做 |

### 7.2 Hover Tooltip
- 鼠标悬停时显示：英雄名称 + Riot ID
- 默认不显示文字，保持简洁
- Tooltip 半透明，小字体

### 7.3 拖拽 / 移动
- 暂不支持拖拽移动
- 固定在屏幕右侧
- 后续再做 Settings UI 调整位置

### 7.4 Click-through
- 普通区域：不拦截鼠标（游戏正常操作）
- 召唤师技能图标：拦截鼠标（可以点击）
- 技术实现：图标区域设置 WA_HITTEST_VISIBLE，其他区域设置 WA_HITTEST_TRANSPARENT

---

## 8. Position 规范

### 8.1 默认位置
- **水平**：屏幕右侧，margin = 20px
- **垂直**：顶部 margin = 80px（避开游戏左上角的小地图和队友头像）
- **宽度**：自适应，约 240px
- **高度**：5 行 × ~56px = 280px + padding

### 8.2 安全区域
```
x + width <= screen_width - 20px
y + height <= screen_height - 40px
```

### 8.3 DPI 适配
- 自动获取屏幕分辨率
- 根据 DPI scaling 调整图标大小
- 不写死像素值

---

## 9. Five-player Ordering

### 9.1 排序规则
- **按 position 排序**：TOP → JG → MID → ADC → SUP
- 如果 position 未知，按 2999 playerlist 返回顺序

### 9.2 为什么不推断 role
- 2999 数据可能不可靠
- 凭空推断 role 会导致错误
- 用真实 position 字段，没有就按默认顺序

---

## 10. Champion Name 统一规范

### 10.1 问题现状
- 2999 返回：`赏金猎人`
- CooldownManager 存储：`厄运小姐`
- 导致 Overlay 匹配不上

### 10.2 统一方案
**整个系统使用 canonical champion ID 作为内部主键：**

```
2999 raw champion name
    ↓
ChampionResolver (normalize_champion_name)
    ↓
canonical champion_id (如 21)
    ↓
ChampionCatalog
    ↓
canonical display name (如 "厄运小姐")
```

**规则：**
- 内部全部用 champion_id 做 key
- 中文名称只用于显示
- Overlay 和 CooldownManager 都用同一个 champion_id 匹配

---

## 11. Summoner Spell 统一规范

### 11.1 问题现状
- 2999 返回：`解封的传送`、`惩戒（深寒）`
- 图标映射：找不到对应图标

### 11.2 统一方案
**建立 SummonerSpellResolver：**

```
Raw Spell Name (中文变体)
    ↓
SummonerSpellResolver
    ↓
canonical spell ID (如 12)
    ↓
SummonerSpellCatalog
    ↓
Icon Resource (SummonerTeleport.png)
```

**Canonical Spell ID 列表：**
- FLASH (4)
- TELEPORT (12)
- IGNITE (14)
- BARRIER (21)
- CLEANSE (1)
- EXHAUST (3)
- GHOST (6)
- HEAL (7)
- SMITE (11)

**变体映射：**
- "解封的传送" → TELEPORT
- "原初惩戒" / "惩戒（深寒）" → SMITE
- "闪现" / "Flash" → FLASH

---

## 12. Q/W/E/R Skill Icon

### 12.1 数据链
```
canonical champion_id
    ↓
ChampionCatalog
    ↓
Champion spell metadata (技能列表)
    ↓
Q/W/E/R icon URL
    ↓
本地缓存 (data/assets/champions/{version}/spells/)
    ↓
Overlay 显示
```

### 12.2 缓存策略
- 首次遇到某英雄 → 下载该英雄技能资源
- 后续直接读缓存
- 不需要一次下载所有英雄

---

## 13. 数据流架构

```
2999 Live Data
    ↓
GameSnapshot (enemy_team)
    ↓
OverlayViewModel (get_rows)
    ↓
Overlay UI 显示

Overlay Click (召唤师技能)
    ↓
CommandBus (COOLDOWN_START)
    ↓
CooldownManager
    ↓
Reminder (ACTIVE)

CooldownManager (active reminders)
    ↓
OverlayViewModel (读取 reminders)
    ↓
Overlay UI 显示 CD 状态
```

**关键原则：**
- UI 只读不写：不自己启动 timer
- 数据源唯一：CooldownManager 是唯一的 CD 数据源
- 双向数据流：2999 → UI 显示，UI 点击 → CooldownManager → UI 显示 CD

---

## 14. Policy / Compliance Note

### 14.1 风险记录
- OP.GG 官方已停止旧的 spell & skill tracker overlay，原因是 Riot Games 政策变化
- 本项目必须单独评估当前 Riot Third-Party Application Policy
- 不能因为其他项目曾经实现过就认为当前功能自动合规

### 14.2 当前合规状态
- 数据来源：2999 Live Client Data API（官方公开 API）
- 不读取游戏内存
- 不注入游戏进程
- 不模拟输入
- 纯只读展示 + 用户主动点击记录

### 14.3 建议
- 后续单独做合规评估
- 当前先继续开发，记录风险
- 不因为风险擅自删除功能

---

## 15. 下一步实施顺序

1. **统一 ChampionResolver**：用 champion_id 做内部主键
2. **统一 SummonerSpellResolver**：处理变体名称
3. **修复 Overlay 匹配**：用 champion_id 匹配 CooldownManager reminders
4. **添加语音提示**：点击后说"记好了"
5. **Q/W/E/R 真实图标**：下载并缓存技能图标
6. **视觉微调**：调整透明度、尺寸、间距
7. **真实测试**：完整链路验证

---

**文档版本：** v1.0  
**创建日期：** 2026-09-22  
**阶段：** H.4.4-R3 Design Spec
