"""
DEOBFUSCATE-01: obfuscatedPuuid 还原（腾讯服 ChampSelect HIDDEN 混淆）

fixture：identity_diag_20261001_105951_cs.json 里 4 个真实腾讯服 obfuscatedPuuid
（cs 阶段 myTeam HIDDEN 成员），同文件自己（UNHIDDEN）真 puuid 作为结构参照。
"""
import pytest

pytestmark = pytest.mark.unit

import re

from app.lol.ongoing.deobfuscate import deobfuscate_puuid

# 真实腾讯服数据（identity_diag_20261001_105951_cs.json）
REAL_OBFUSCATED = [
    "31f5f18b-e380-0f4b-2470-4ddfe6821ac9",
    "0d6f0f2e-9854-02ba-30d5-2bd327858da2",
    "f7ca414f-2cf2-03c6-0cc2-79ddf3479d79",
    "5aa5fec1-5ce3-0cb8-0354-afcd2bd61e4b",
]
# 同文件自己（UNHIDDEN）真 puuid：v5 + variant=b
SELF_REAL = "dac5d02a-68ed-5596-b089-d193bc2944cf"

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


class TestDeobfuscatePuuid:
    def test_real_tencent_puuids_resolve_to_v5(self):
        """4 个真实腾讯服 obfuscatedPuuid → 解出合法 v5 UUID + 合法 variant"""
        for ob in REAL_OBFUSCATED:
            real = deobfuscate_puuid(ob)
            assert _UUID_RE.match(real), f"{ob} → {real!r}"
            assert real[14] == "5", f"{ob} → version={real[14]}"
            assert real[19] in ("8", "9", "a", "b"), f"{ob} → variant={real[19]}"

    def test_resolved_shares_structure_with_self_puuid(self):
        """解出的 puuid 与同文件自己真 puuid（v5/variant=b）结构同构"""
        real = deobfuscate_puuid(REAL_OBFUSCATED[0])
        assert real[14] == SELF_REAL[14] == "5"
        assert real[19] == SELF_REAL[19] == "b"

    def test_invalid_inputs_return_empty(self):
        """非法输入：空串 / None / 非 UUID / 短串 / 非法 hex / 奇数长度 → 空字符串"""
        assert deobfuscate_puuid("") == ""
        assert deobfuscate_puuid(None) == ""
        assert deobfuscate_puuid("not-a-uuid") == ""
        assert deobfuscate_puuid("abcd") == ""
        assert deobfuscate_puuid("zzzzzzzz-zzzz-zzzz-zzzz-zzzzzzzzzzzz") == ""
        assert deobfuscate_puuid("31f5f18be3800f4b24704ddfe6821ac") == ""  # 奇数长度 hex

    def test_obfuscated_itself_is_not_valid_uuid_semantics(self):
        """obfuscatedPuuid 的 version/variant 位也被 XOR 打乱（如 '0'/'2'），
        输出验证只在"明文→还原"方向成立；反向再解会被校验拦下返回空——
        这正是生产路径不把 obfuscated 当合法 puuid 用、必须先还原的原因。"""
        # 31f5f18b-e380-0f4b-2470-...：version='0'、variant='2'，均不在允许集
        assert REAL_OBFUSCATED[0][14] == "0"
        assert REAL_OBFUSCATED[0][19] == "2"
        real = deobfuscate_puuid(REAL_OBFUSCATED[0])
        assert real[14] == "5" and real[19] == "b"
