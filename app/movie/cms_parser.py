"""CMS 响应与分集地址解析；只处理输入数据，不访问外部资源。"""

import json


def parse_cms_response(text):
    """苹果 CMS 响应解析。

    已知坑：接口 Content-Type 常标 text/html 但 body 实际是合法 JSON
    （如 360zyzz 的 /api.php/provide/vod/...）。因此解析必须按 body 首字符
    判断（{ 或 [），不能按 Content-Type。非 JSON body 返回 {}。
    """
    s = (text or "").lstrip()
    if not s or s[0] not in "{[":
        return {}
    try:
        return json.loads(s)
    except Exception:
        return {}


def split_play_url(vod_play_url):
    """拆分苹果 CMS 的 vod_play_url。

    格式：名字$URL#名字2$URL2（# 分集、$ 分名与 URL）。
    返回 [(name, url), ...]；无有效段时返回 []。
    """
    out = []
    for seg in str(vod_play_url or "").split("$$$", 1)[0].split("#"):
        seg = seg.strip()
        if not seg or "$" not in seg:
            continue
        name, _, url = seg.partition("$")
        if not url.strip():
            continue
        out.append((name.strip(), url.strip()))
    return out


def first_play_url(vod_play_url):
    """取第一条 (name, url)；无则 None。"""
    parts = split_play_url(vod_play_url)
    return parts[0] if parts else None


def normalise_playback(item, line_index=None):
    """Separate CMS playback lines without changing source or original vod fields.

    $$$ separates lines, # separates episodes; never send a joined line to MPV.
    Items containing only an existing first_url keep their legacy contract.
    """
    result = dict(item)
    if not item.get("vod_play_url") and not item.get("play_url"):
        return result
    raw = str(item.get("vod_play_url") or item.get("play_url") or "")
    names = str(item.get("vod_play_from") or "").split("$$$")
    lines = [dict(index=i, name=names[i] if i < len(names) and names[i] else f"线路{i+1}", play_url=part)
             for i, part in enumerate(raw.split("$$$")) if split_play_url(part)]
    chosen = line_index if line_index is not None else item.get("_playback_line_index")
    selected = next((line for line in lines if line["index"] == chosen), None)
    if line_index is not None and selected is None:
        raise ValueError("playback line unavailable")
    selected = selected or (lines[0] if lines else None)
    result["_playback_lines"] = lines
    result["_playback_line_index"] = selected["index"] if selected else None
    result["play_url"] = selected["play_url"] if selected else ""
    first = first_play_url(result["play_url"])
    result["first_name"] = first[0] if first else ""
    result["first_url"] = first[1] if first else ""
    return result


def episode_eps(item):
    """取 item 的分集列表（非 dict / 无 play_url → 空列表）——episodes
    与 is_multi_episode 共用，消重复取数逻辑。"""
    if not isinstance(item, dict):
        return []
    value = item.get("play_url") or ""
    if "$$$" in value:
        value = normalise_playback(item).get("play_url", "")
    return split_play_url(value)
