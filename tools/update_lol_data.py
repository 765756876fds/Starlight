"""一次性脚本：更新 data/lol_data/ 的英雄 + 技能 CD（或全部数据）。

用法：
    python tools/update_lol_data.py          # 只更新 英雄 + 技能 CD
    python tools/update_lol_data.py all      # 更新 英雄 + 技能 + 装备中文名

实际逻辑在 app/lol/data/lol_data_updater.py（设置页「🔄 更新数据」按钮同源）。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.lol.data.lol_data_updater import update_all, update_champions_and_abilities  # noqa: E402


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "all":
        stats = update_all()
    else:
        stats = update_champions_and_abilities()
    print(f"champions={stats.get('champions')} abilities={stats.get('abilities')} "
          f"items={stats.get('items', '-')} failed={stats.get('failed', 0)}")
    return 0 if stats.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
