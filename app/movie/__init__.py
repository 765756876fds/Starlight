"""电影链：苹果 CMS 搜索 + mpv 独立视频实例播放（与音乐互斥）。"""
from .movie_controller import (
    MovieController,
    first_play_url,
    parse_cms_response,
    split_play_url,
)

__all__ = ["MovieController", "first_play_url", "parse_cms_response", "split_play_url"]
