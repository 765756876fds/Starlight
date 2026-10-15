"""电影链：苹果 CMS 搜索 + mpv 独立视频实例播放（与音乐互斥）。"""
from .movie_controller import MovieController, split_play_url, first_play_url, parse_cms_response

__all__ = ["MovieController", "split_play_url", "first_play_url", "parse_cms_response"]
