"""P1 路径自适应：PROJECT_ROOT 单一来源 + 6 处 data/ 路径不再依赖 CWD。

覆盖：
- PROJECT_ROOT 来自 app/core/path_resolve.py（单一来源）
- 5 个 lol_data 常量基于 PROJECT_ROOT（绝对路径）
- 绕过启动脚本（chdir 到别处）时 catalog 仍能加载真实数据
"""
import pytest

pytestmark = pytest.mark.unit

import pytest

from app.core.path_resolve import PROJECT_ROOT
from app.lol.objectives.rules import DEFAULT_PATH, load_ruleset
from app.lol.data.champion_catalog import DEFAULT_DATA as CHAMP_DATA, ChampionCatalog
from app.lol.data.ability_catalog import DEFAULT_DATA as ABIL_DATA, AbilityCatalog
from app.lol.data.summoner_spell_catalog import _SPELLS_PATH
from app.lol.data.cache import DEFAULT_CACHE_DIR


# 5 个 lol_data 常量（main.py 的 music_sources 路径是函数内局部值，无法静态断言）
_LOL_CONSTANTS = [
    DEFAULT_PATH,
    CHAMP_DATA,
    ABIL_DATA,
    _SPELLS_PATH,
    DEFAULT_CACHE_DIR,
]


def test_project_root_is_single_source():
    """PROJECT_ROOT 是单一来源：绝对路径 + 全部常量基于它。"""
    assert PROJECT_ROOT.is_absolute()
    for c in _LOL_CONSTANTS:
        assert c.is_relative_to(PROJECT_ROOT), f"{c} 未基于 PROJECT_ROOT"


def test_lol_data_constants_are_absolute():
    """5 个常量都是绝对路径（不依赖 CWD）。"""
    for c in _LOL_CONSTANTS:
        assert c.is_absolute(), f"{c} 不是绝对路径"


def test_lol_data_constants_point_to_existing():
    """5 个常量解析到的文件/目录存在（项目内 data/lol_data）。"""
    for c in _LOL_CONSTANTS:
        assert c.exists(), f"{c} 不存在"


def test_catalog_loads_from_other_cwd(tmp_path, monkeypatch):
    """绕过启动脚本（chdir 到别处）时，catalog 仍能加载真实数据。"""
    monkeypatch.chdir(tmp_path)
    # 相对路径版会在此失败（找不到 data/...）；绝对路径版应成功
    champ = ChampionCatalog()
    abil = AbilityCatalog()
    ruleset = load_ruleset()
    assert len(ruleset.rules) > 0
    # 加载了真实数据（非内置 3 英雄最小集）
    assert len(champ._by_id) > 3, "champion catalog 应加载完整数据"


def test_cache_dir_absolute():
    assert DEFAULT_CACHE_DIR.is_absolute()
