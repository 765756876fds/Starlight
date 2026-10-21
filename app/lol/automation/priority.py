"""候选优先级 + fallback 推进。"""
from __future__ import annotations

from collections.abc import Iterable


def pick_available(
    priority: Iterable[int],
    rejected: set[int],
    server_available: set[int] | None,
) -> int | None:
    """按优先级返回第一个未被 rejected 且在 server_available 中的英雄 id。

    server_available 为 None 时（拉取失败）不过滤服务器集合，但仍跳过 rejected。
    """
    for cid in priority:
        if cid in rejected:
            continue
        if server_available is not None and cid not in server_available:
            continue
        return int(cid)
    return None
