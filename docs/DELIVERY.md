# 项目交付说明

## 项目状态
PHASE A - PHASE F 全部完成。

| 阶段 | 功能 | 状态 |
|---|---|---|
| A | LoL 客户端自动化（Queue / Accept / Ban / Pick / Fallback） | 真机验证通过 |
| B | 手动技能计时器 | 单测通过 |
| C | UI 控制中心（托盘 + 3 Tab） | 单测通过 |
| D | 队友/敌方信息展示 | 单测通过 |
| E | 游戏内 Overlay | 单测通过 |
| F | 在线音乐（archive.org + JS 桥接） | 酷我源真机验证 |

## 依赖
- Python 3.11+
- Node.js 18+（已装 v22.23.2）
- mpv
- ffmpeg（可选）

## 安装
1. `pip install -r requirements.txt`
2. `cd app/music/online/js_bridge && npm install`
3. 把 MusicFree 音源放到 `data/music_sources/musicfree/`
4. 把 LX Music 音源放到 `data/music_sources/lx/`

## 启动
```
python main.py            # 带 UI
python main.py --headless # 无 UI
```

## 已知限制
- 网易云/QQ 音乐/酷狗：搜索可用，播放需 userVariables 填 token
- 酷我音乐：搜索 + 播放都可用
- LX 音源：仅 Huibq / 爱坤 可加载
- 独占全屏：Overlay 需切 Borderless Windowed
- Riot 政策：自动 Ban/Pick / 敌方战绩 / 技能计时器属敏感功能，自行判断使用

## 验证清单
- [ ] UI 能启动
- [ ] 托盘图标出现
- [ ] 本地音乐能播放
- [ ] 在线搜索酷我音乐能播放
- [ ] 语音"敌方亚索闪现交了"能创建计时器
- [ ] LoL 客户端自动 Queue / Accept 能工作
- [ ] ChampSelect 阶段能看到队友/敌方信息
- [ ] 进入游戏能看到 Overlay
