"""SSH 装备来源测试：明朗之靴（3158）+10 / 猩红明朗之靴（3164）+20。

- itemID 命中：3158 → +10，3164 → +20
- 名称 fallback：中文"猩红明朗" → +20；英文 "Crimson Lucidity" → +20
- ionian 拼写修正："Ionian Boots of Lucidity" → +10
- 空 items → 不加
"""
from app.lol.cooldown.ssh_provider import summoner_spell_haste


def test_item_id_3158_lucidity_plus_10():
    ssh, sources, _ = summoner_spell_haste([{"itemID": 3158}], [])
    assert ssh == 10.0
    assert "item:3158" in sources


def test_item_id_3164_crimson_plus_20():
    ssh, sources, _ = summoner_spell_haste([{"itemID": 3164}], [])
    assert ssh == 20.0
    assert "item:3164" in sources


def test_name_fallback_chinese_crimson_plus_20():
    ssh, sources, _ = summoner_spell_haste([{"displayName": "猩红明朗之靴"}], [])
    assert ssh == 20.0
    assert any("猩红明朗" in s for s in sources)


def test_name_fallback_chinese_crimson_full_name_plus_20():
    """2999 全称场景：'猩红明朗之靴' 同时含'明朗之靴'，长 key 优先 → +20"""
    ssh, sources, _ = summoner_spell_haste([{"displayName": "猩红明朗之靴"}], [])
    assert ssh == 20.0
    assert any("猩红明朗" in s for s in sources)


def test_name_fallback_english_crimson_plus_20():
    ssh, sources, _ = summoner_spell_haste([{"displayName": "Crimson Lucidity"}], [])
    assert ssh == 20.0
    assert any("crimson" in s for s in sources)


def test_name_fallback_english_ionian_plus_10():
    """ionian 拼写修正后，标准英文名可命中 +10"""
    ssh, sources, _ = summoner_spell_haste([{"displayName": "Ionian Boots of Lucidity"}], [])
    assert ssh == 10.0
    assert any("ionian" in s for s in sources)


def test_empty_items_no_bonus():
    ssh, sources, status = summoner_spell_haste([], [])
    assert ssh == 0.0
    assert sources == []
    assert status == "unavailable"
