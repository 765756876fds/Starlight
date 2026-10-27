"""泛化播放指令（"播放歌曲/放首歌/播音乐"）路由回归测试。

修：用户说"播放歌曲"时，旧逻辑把"歌曲"当歌名去本地搜索→本地无匹配→
在线搜"歌曲"→播了一首无关歌曲（《宿敌》）。新逻辑：
- 泛化词 → 本地随机挑一首播放，不走在线兜底；
- 本地库空 → 明确提示，不在线乱搜；
- 指定歌名（如"晴天"）→ 保留原本地优先 + 在线兜底；
- 明确在线指令不受影响。
"""
import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

from app.music.controller import MusicController


def _make_ctrl(tmp_path, db_name="vague.db"):
    from app.music.database import MusicDatabase
    db_path = str(tmp_path / db_name)
    db = MusicDatabase(db_path)
    cfg = {
        "database": db_path,
        "scan_dirs": [],
        "extensions": [".flac"],
        "mpv_path": "mpv",
        "mpv_ipc_pipe": "\\\\.\\pipe\\test_vague",
        "default_volume": 70,
    }
    with patch("app.music.controller.MpvController"):
        ctrl = MusicController(cfg)
    ctrl.mpv = MagicMock()
    ctrl.db = db
    ctrl.search = MagicMock()
    ctrl.online_manager = MagicMock()
    return ctrl, db


_LOCAL_ABC = [
    {"id": 1, "title": "晴天", "artist": "周杰伦", "path": "/a.flac", "duration": 10},
    {"id": 2, "title": "麻雀", "artist": "李荣浩", "path": "/b.flac", "duration": 10},
    {"id": 3, "title": "命运", "artist": "铁柱", "path": "/c.flac", "duration": 10},
]


class TestVaguePlayRouting:
    @pytest.mark.parametrize("q", [
        "歌曲", "歌", "音乐", "首歌", "来首歌", "放首歌", "播音乐",
        "来首", "放首", "播首", "听首", "随便", "随便放", "播放", "放",
    ])
    def test_vague_plays_local_random_no_online(self, tmp_path, q):
        """泛化词：有本地歌曲 → 本地随机播放，绝不调用在线搜索。"""
        ctrl, db = _make_ctrl(tmp_path)
        ctrl.db.get_all_songs = MagicMock(return_value=list(_LOCAL_ABC))
        # play_song 会真实走 _play_song 路径（mpv 已 mock），但我们只关心
        # 是否选了本地、是否没调在线。patch play_song 直接记录入参。
        with patch.object(ctrl, "play_song", wraps=ctrl.play_song) as ps:
            result = ctrl.play_search_result(q)
        assert result is not None
        assert result["title"] in {"晴天", "麻雀", "命运"}
        ps.assert_called_once()
        # 本地 search 不应被调用（泛化词直接走 db.get_all_songs 随机）
        ctrl.search.search.assert_not_called()
        # 在线搜索绝不触发
        ctrl.online_manager.search_all.assert_not_called()
        db.close()

    def test_vague_empty_local_library_no_online(self, tmp_path):
        """本地库为空 → 不在线乱搜，返回 None。"""
        ctrl, db = _make_ctrl(tmp_path)
        ctrl.db.get_all_songs = MagicMock(return_value=[])
        result = ctrl.play_search_result("歌曲")
        assert result is None
        ctrl.search.search.assert_not_called()
        ctrl.online_manager.search_all.assert_not_called()
        db.close()

    def test_specific_local_title_hits_local(self, tmp_path):
        """指定本地存在的歌名 → 本地命中播放，不走在线。"""
        ctrl, db = _make_ctrl(tmp_path)
        local_hit = {"id": 1, "title": "晴天", "artist": "周杰伦",
                     "path": "/a.flac", "duration": 10}
        ctrl.search.search.return_value = [local_hit]
        result = ctrl.play_search_result("晴天")
        assert result is not None
        assert result["title"] == "晴天"
        ctrl.search.search.assert_called_once_with("晴天", limit=10)
        ctrl.online_manager.search_all.assert_not_called()
        db.close()

    def test_specific_unknown_title_falls_back_online(self, tmp_path):
        """本地不存在但可在线找到的歌名 → 仍走在线兜底（保留旧行为）。"""
        ctrl, db = _make_ctrl(tmp_path)
        ctrl.search.search.return_value = []  # 本地无匹配
        track = MagicMock()
        track.title = "天龙八部之宿敌"
        track.artist = "许嵩"
        track.source_name = "wy"
        ctrl.online_manager.search_all.return_value = {"wy": [track]}
        ctrl.online_manager.get_play_url.return_value = {"url": "http://x/y.mp3", "headers": {}}
        ctrl._apply_stream = MagicMock(return_value={"title": "天龙八部之宿敌", "online": True})
        ctrl.play_search_result("宿敌")
        ctrl.search.search.assert_called_once_with("宿敌", limit=10)
        ctrl.online_manager.search_all.assert_called_once()
        db.close()

    def test_online_explicit_not_broken(self, tmp_path):
        """明确在线指令（含"在线"字样）不应被误判为泛化词。"""
        ctrl, db = _make_ctrl(tmp_path)
        ctrl.search.search.return_value = []
        track = MagicMock()
        track.title = "测试在线歌"
        track.artist = "x"
        track.source_name = "wy"
        ctrl.online_manager.search_all.return_value = {"wy": [track]}
        ctrl.online_manager.get_play_url.return_value = {"url": "http://x/y.mp3", "headers": {}}
        ctrl._apply_stream = MagicMock(return_value={"title": "测试在线歌", "online": True})
        ctrl.play_search_result("在线搜索周杰伦")
        ctrl.search.search.assert_called_once_with("在线搜索周杰伦", limit=10)
        ctrl.online_manager.search_all.assert_called_once()
        db.close()

    def test_is_vague_classifier(self, tmp_path):
        """分类器单测：真泛化词返回 True，真歌名返回 False。"""
        ctrl, db = _make_ctrl(tmp_path)
        for q in ["歌曲", "歌", "音乐", "来首歌", "播音乐",
                  "随便放", "", "  ", "《》"]:
            assert ctrl._is_vague_play_query(q), f"应判为泛化: {q!r}"
        for q in ["晴天", "周杰伦的晴天", "宿敌", "许嵩", "在线搜索周杰伦",
                  "播放晴天", "麻雀李荣浩", "新歌", "老歌", "情歌", "情歌王"]:
            assert not ctrl._is_vague_play_query(q), f"不应判为泛化: {q!r}"
        db.close()
