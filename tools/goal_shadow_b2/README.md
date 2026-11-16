# B2：活跃快照与可尝试计划

独立研究工具，仅处理 `LISTEN_MUSIC`。正式应用没有导入本目录，没有安装观察或执行钩子。

- `sqlite_snapshot.py`：A 为独立工作线程的正常 SQLite `mode=ro` 事务；B 为专用 `LabOwner` 的短锁聚合。B 明确拒绝生产 `MusicDatabase`，不在其连接上安装回调。
- `capabilities.py`：收藏归属、本地文件访问、精确音源身份和既有推荐服务的准备能力。不会搜索、解析 URL、播放或写偏好。
- `decision.py`：硬约束、当前会话、明确偏好、历史证据和改变成本比较；执行器只接受有限的合成回执，不能接受真实业务回调。
- `data/`：先冻结的 16 个开发情境、30 个保留情境及字节哈希；不是自然语言测试。`freeze.py` 拒绝覆盖既有答案。
- `evaluate.py`：输入只有世界字段，答案不进入决策；输出目录已存在时拒绝运行，避免覆盖首次结果。

## 证据边界

资源存在和明确收藏归属是独立字段。`ATTEMPTABLE` 仅表示可以有界尝试，不代表 URL 已解析或 MPV 已解码。`PLAYBACK_CONFIRMED` 必须另有真实起播回执；本实验不生成这种真实回执。

榜单方案只有一个“交给既有 RecommendationService 准备并尝试”的业务宏，不假定它支持精确榜单 ID，不复制其选曲与换源策略。仅目录缓存且无准备能力仍为 `UNVERIFIED`。

失败回退只在明确未产生播放副作用时允许，最多 3 次业务计划尝试（2 次回退）；不确定的已接受加载、代次变化立即停止。模拟器预算不是生产网络预算，未来必须共享既有推荐总预算，不能嵌套放大重试。

SQLite `mode=ro` 保证 SQL 不写业务数据，不保证完全不参与 WAL/SHM 协调或不创建侧文件。不能用它声称目录逐字节零变化。本轮只打开生成的临时数据库。

## 复核

在干净基线副本中、使用项目开发环境，先创建 QApplication 与 ThemeManager，再执行 pytest，避免改变已有 Qt fixture 的初始化顺序：

```python
from PySide6.QtWidgets import QApplication
from app.ui.theme.manager import get_theme_manager
import pytest

app = QApplication([])
manager = get_theme_manager(app)
raise SystemExit(pytest.main([
    "tests/unit/test_goal_shadow_b2.py",
    "tests/ui/test_goal_shadow_b2_responsiveness.py",
    "-o", "addopts=", "-p", "no:cacheprovider", "-q",
    # 指定新的项目外 --basetemp 和 --junitxml 保存首次结果。
]))
```

环境设 `QT_QPA_PLATFORM=offscreen`。定向测试使用实际 MusicDatabase schema，但路径全部由 pytest 临时目录生成；不使用用户数据库。

```powershell
python -B -m tools.goal_shadow_b2.evaluate dev --output D:/new-b2-evidence/dev
python -B -m tools.goal_shadow_b2.evaluate holdout --output D:/new-b2-evidence/holdout
```

保留集只评估一次，不据此修改答案、词库或比较器。已有 B1 的 63 题和 61/63 结论保持原样。

## 后续最小接口建议（本轮未实施）

1. 数据库所有者提供 `read_collection_snapshot(limit, deadline, cancel)`，自行管理锁、现有回调和连接；返回成员、完整性、schema、关闭/重建代次、成员变更 revision。调用者不直接操作其连接。
2. 音源所有者提供无联网的稳定身份/别名解析、关闭状态和能力 revision；插件别名不能靠猜测精确匹配。
3. 执行层提供关联请求代次和资源 revision 的 accepted/started/failed/stale 回执，重试前可靠确认无副作用。

目前 A 更适合继续只读研究；B 需要正式所有者 API 才能安全接线。SQLite busy 等待和操作系统调度会超出名义毫秒预算，因此 GUI 必须隔离，不能承诺硬实时截止。未具备正式自动播放交付条件。

完整证据、计时和限制见 `docs/goal_research/20261117_B2活跃快照与可尝试能力验收.md`。
