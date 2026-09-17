"""ResponseFormatter - Player Query 结构化结果 → 短中文语音文本"""
from typing import Dict, Any


def format_query_result(result: Dict[str, Any]) -> str:
    """把 PlayerQueryService 返回的 dict 转成简短中文播报文本。

    纯字符串格式化，不访问 DB / LCU / LLM / TTS。
    """
    try:
        dq = result.get("data_quality") or {}
        status = dq.get("status")

        # 错误/缺失状态
        if status == "INVALID_QUERY_TYPE":
            return "不支持的查询类型。"
        if status == "INVALID_LIMIT":
            return "查询次数无效。"
        if status == "MISSING_IDENTITY":
            return "当前还无法确定你的玩家身份。"
        if status == "MISSING_LOCAL_MATCH_ID":
            return "请指定要分析的比赛。"
        if status == "NO_MATCHES":
            return "暂时没有足够的比赛数据。"
        if status == "MATCH_NOT_FOUND":
            return "未找到该场比赛。"

        qt = result.get("query_type", "")
        res = result.get("result") or {}

        if qt == "recent":
            return _format_recent(res, dq)
        if qt == "champion_stats":
            return _format_champion_stats(res, dq)
        if qt == "role_stats":
            return _format_role_stats(res, dq)
        if qt == "match_analysis":
            return _format_match_analysis(res)
        return "查询完成。"
    except Exception:
        return "查询结果解析失败。"


def _format_recent(res: Dict, dq: Dict) -> str:
    sample = res.get("sample") or {}
    completed = sample.get("completed_games", 0) or 0
    incomplete = sample.get("incomplete_games", 0) or 0
    matches = res.get("recent_matches") or []
    wins = sum(1 for m in matches if m.get("result") == "Win")
    losses = sum(1 for m in matches if m.get("result") == "Lose")
    unknown = len(matches) - wins - losses

    if completed == 0 and incomplete == 0:
        return "暂时没有足够的比赛数据。"

    parts = [f"最近{completed}场"]
    if wins or losses:
        parts.append(f"{wins}胜{losses}负")
    if unknown:
        parts.append(f"{unknown}场结果未知")
    if incomplete:
        parts.append(f"{incomplete}场数据不完整")
    return "，".join(parts) + "。"


def _format_champion_stats(res: Dict, dq: Dict) -> str:
    stats = res.get("champion_stats") or []
    if not stats:
        return "暂时没有足够的英雄统计数据。"
    top = max(stats, key=lambda c: c.get("games", 0))
    name = top.get("champion_name") or "未知英雄"
    games = top.get("games", 0)
    wins = top.get("wins", 0)
    losses = top.get("losses", 0)
    return f"玩得最多的是{name}，共{games}场，{wins}胜{losses}负。"


def _format_role_stats(res: Dict, dq: Dict) -> str:
    stats = res.get("role_stats") or []
    if not stats:
        return "暂时没有足够的位置统计数据。"
    top = max(stats, key=lambda r: r.get("games", 0))
    pos = top.get("position") or "未知位置"
    games = top.get("games", 0)
    wins = top.get("wins", 0)
    losses = top.get("losses", 0)
    return f"主要位置是{pos}，共{games}场，{wins}胜{losses}负。"


def _format_match_analysis(res: Dict) -> str:
    if not res:
        return "未找到该场比赛。"
    match = res.get("match") or {}
    result = match.get("result", "Unknown")
    duration = match.get("duration")
    text = f"本场比赛{result}"
    if duration:
        minutes = int(duration // 60)
        seconds = int(duration % 60)
        text += f"，时长{minutes}分{seconds}秒"
    events = res.get("events") or {}
    kills = events.get("champion_kills", 0)
    if kills:
        text += f"，共{kills}次击杀"
    return text + "。"
