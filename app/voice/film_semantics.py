"""Finite film discovery vocabulary; no title guesses or playback permission."""
import re

from ..core.command_bus import CommandType

KINDS = {"电影": "movie", "电视剧": "tv", "动漫": "anime"}


def film_request(text):
    match = re.fullmatch(r"(?:我想看|我想看看|推荐|推荐几部|给我推荐几部)(电影|电视剧|动漫)(?:了)?", text)
    if match:
        return CommandType.MOVIE_RECOMMEND, {"kind": KINDS[match[1]]}
    match = re.fullmatch(r"(?:来点|给我找几部|给我推荐几部|推荐几部)(高分)?(悬疑|科幻|喜剧|动作|爱情|恐怖|剧情|动画|纪录片)(电影|电视剧|动漫)", text)
    if match:
        params = {"kind": KINDS[match[3]], "genre": match[2]}
        if match[1]:
            params["min_score"] = 8
        return CommandType.MOVIE_RECOMMEND, params
    match = re.fullmatch(r"继续看上次的(电影|电视剧|动漫)", text)
    if match:
        return CommandType.MOVIE_CONTINUE, {"kind": KINDS[match[1]]}
    return None
