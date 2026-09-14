"""
pytest 配置和共享 fixtures
"""
import os
import sys

# 将项目根目录加入 Python 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest


@pytest.fixture
def event_bus():
    """全新的 EventBus 实例"""
    from app.core.event_bus import EventBus
    return EventBus()


@pytest.fixture
def test_db_path(tmp_path):
    """临时数据库路径"""
    return str(tmp_path / "test_music.db")


@pytest.fixture
def music_db(test_db_path):
    """临时音乐数据库"""
    from app.music.database import MusicDatabase
    db = MusicDatabase(test_db_path)
    yield db
    db.close()
