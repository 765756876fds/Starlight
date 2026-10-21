"""PlayerQuery handler - 当前局实时查询（无历史战绩）"""
import time

from ..core.command_bus import Command
from ..lol.data.champion_catalog import ChampionCatalog
from ..speech.speech_queue import PRIORITY_B, SpeechItem
from .current_game_analysis import (
    champion_kda as _champion_kda,
)
from .current_game_analysis import (
    champion_level as _champion_level,
)
from .current_game_analysis import (
    economy as _economy,
)
from .current_game_analysis import (
    enemy_roster as _enemy_roster,
)
from .current_game_analysis import (
    most_deaths as _most_deaths,
)
from .current_game_analysis import (
    richest as _richest,
)
from .current_game_analysis import (
    self_kda as _self_kda,
)
from .current_game_analysis import (
    self_level as _self_level,
)


def _resolve_self_puuid(state_store) -> str:
    """从 StateStore 读 current_summoner.puuid，不直接调 LCU。"""
    if state_store is None:
        return None
    try:
        summ = state_store.get("lcu", "current_summoner")
        if summ and isinstance(summ, dict):
            return summ.get("puuid") or None
    except Exception:
        pass
    return None


def make_player_query_handler(service=None,
                              speech_queue=None,
                              state_store=None,
                              snapshot_provider=None,
                              champion_catalog=None):
    if champion_catalog is None:
        champion_catalog = ChampionCatalog()

    def handle(command: Command):
        params = command.params or {}
        qt = params.get("query_type", "current_self_kda")

        # --- 当前对局实时分析（纯确定性，读 live snapshot）---
        supported = (
            "current_richest", "current_self_kda", "economy_diff",
            "enemy_roster", "most_deaths", "champion_level",
            "champion_kda", "current_self_level",
        )
        if qt in supported:
            snap = snapshot_provider() if snapshot_provider else None
            text = "现在读不到对局数据。"
            if snap is not None:
                if qt == "economy_diff":
                    e = _economy(snap)
                    if e["our_n"] == 0:
                        text = "现在读不到对局经济数据。"
                    elif e["our_total"] == 0 and e["enemy_total"] == 0:
                        text = "读不到双方金币，暂时算不了经济差。"
                    else:
                        if e["state"] == "ahead":
                            text = f"我们经济领先约 {e['abs_diff']} 块。"
                        elif e["state"] == "behind":
                            text = f"我们经济落后约 {e['abs_diff']} 块。"
                        else:
                            text = "双方经济接近。"
                elif qt == "current_self_kda":
                    k = _self_kda(snap)
                    text = (f"你现在是 {k['champion']}，{k['kills']} 杀 {k['deaths']} 死 "
                            f"{k['assists']} 助攻，{k['cs']} 刀。") if k else text
                elif qt == "current_self_level":
                    s = _self_level(snap)
                    text = (f"你现在 {s['level']} 级。") if s else text
                elif qt == "current_richest":
                    side = params.get("side", "enemy")
                    r = _richest(snap, side)
                    if r:
                        who = "对面" if side == "enemy" else "我们"
                        basis = r.get("richness_basis", "gold")
                        if basis == "gold" and r["gold"]:
                            text = f"{who}最肥的是 {r['champion']}，{r['level']} 级 {r['gold']} 块。"
                        elif basis == "development_proxy":
                            text = f"按当前可见发育数据，{who} {r['champion']} 比较领先，{r['level']} 级 {r['cs']} 刀。"
                        else:
                            text = f"{who}等级最高的是 {r['champion']}，{r['level']} 级。"
                elif qt == "enemy_roster":
                    roster = _enemy_roster(snap)
                    if roster:
                        names = [r["champion"] for r in roster]
                        text = f"我目前识别到对面 {len(roster)} 人：{'、'.join(names)}。"
                    else:
                        text = "暂时没识别到对面玩家。"
                elif qt == "most_deaths":
                    md = _most_deaths(snap)
                    if md:
                        if md["count"] == 1:
                            text = f"现在死亡最多的是 {md['players'][0]['champion']}，死了 {md['max_deaths']} 次。"
                        else:
                            names = "、".join(p["champion"] for p in md["players"])
                            text = f"现在有 {md['count']} 个人并列死亡最多，都是 {md['max_deaths']} 次：{names}。"
                    else:
                        text = "现在还没人死。"
                elif qt == "champion_level":
                    champion_name = params.get("champion_name", "")
                    champ = champion_catalog.get_by_name(champion_name)
                    if champ is None:
                        text = "我没识别到这个英雄。"
                    else:
                        cl = _champion_level(snap, champion_name, champion_catalog)
                        if cl:
                            text = f"{cl['champion']}现在 {cl['level']} 级。"
                        else:
                            text = "当前没找到这个英雄的玩家。"
                elif qt == "champion_kda":
                    champion_name = params.get("champion_name", "")
                    champ = champion_catalog.get_by_name(champion_name)
                    if champ is None:
                        text = "我没识别到这个英雄。"
                    else:
                        ck = _champion_kda(snap, champion_name, champion_catalog)
                        if ck:
                            text = f"{ck['champion']}现在 {ck['kills']} 杀 {ck['deaths']} 死 {ck['assists']} 助攻。"
                        else:
                            text = "当前没找到这个英雄的玩家。"
            if speech_queue is not None and text:
                try:
                    speech_queue.enqueue(SpeechItem(
                        text=text, priority=PRIORITY_B,
                        expire_at=time.time() + 30.0,
                        category="player_query", interruptible=True))
                    print(f"[PlayerQuery] enqueued: {text}")
                except Exception as _e:
                    print(f"[PlayerQuery] enqueue failed: {_e}")
            else:
                print(f"[PlayerQuery] skip: sq={speech_queue is not None} text={text!r}")
            return {"query_type": qt, "result": text, "data_quality": {"status": "OK"}}

        # 其他 query_type 已下线（历史战绩功能已移除）
        text = "这个查询功能已经下线了。"
        if speech_queue is not None and text:
            try:
                speech_queue.enqueue(SpeechItem(
                    text=text, priority=PRIORITY_B,
                    expire_at=time.time() + 30.0,
                    category="player_query", interruptible=True))
            except Exception:
                pass
        return {"query_type": qt, "result": text, "data_quality": {"status": "DISABLED"}}
    return handle
