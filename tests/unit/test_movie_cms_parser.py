"""CMS 纯解析边界及旧控制器入口的兼容性；不进行真实网络或播放。"""

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.movie import cms_parser, movie_controller


@pytest.mark.parametrize("text, expected", [
    (' \n\t{"list": [], "code": 1}', {"list": [], "code": 1}),
    ('[{"id": 1}]', [{"id": 1}]),
    ('{"sub": {"subs": [{"id": 2}]}}', {"sub": {"subs": [{"id": 2}]}}),
    ('null', {}),
    ('"text"', {}),
    ('[bad', {}),
    (None, {}),
])
def test_response_preserves_container_and_fallback_contract(text, expected):
    assert cms_parser.parse_cms_response(text) == expected


@pytest.mark.parametrize("text, expected", [
    (' A $ https://media.invalid/a$token #坏片段#B$ #$https://media.invalid/b ',
     [('A', 'https://media.invalid/a$token'), ('', 'https://media.invalid/b')]),
    ('A$u#A$u#B$v', [('A', 'u'), ('A', 'u'), ('B', 'v')]),
    ('#bad#bad$  #', []),
])
def test_episode_order_duplicates_and_first_separator_are_preserved(text, expected):
    assert cms_parser.split_play_url(text) == expected
    assert cms_parser.first_play_url(text) == (expected[0] if expected else None)


@pytest.mark.parametrize("item, expected", [
    ({"play_url": '一$u#二$v', "vod_name": '剧名'}, [('一', 'u'), ('二', 'v')]),
    ({"play_url": None}, []),
    ({"vod_play_url": '一$u'}, []),
    (['一$u'], []),
])
def test_episode_input_is_not_mutated(item, expected):
    before = deepcopy(item)
    assert cms_parser.episode_eps(item) == expected
    assert item == before


@pytest.mark.parametrize("name", [
    'parse_cms_response', 'split_play_url', 'first_play_url', 'episode_eps',
])
def test_legacy_controller_exports_the_same_function(name):
    assert getattr(movie_controller, name) is getattr(cms_parser, name)


def test_controller_search_and_episode_consumers_keep_their_schema(monkeypatch):
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(text='{"list": [{"vod_id": 9, "vod_name": "测试剧", '
                               '"vod_pic": "https://img.invalid/a.jpg", '
                               '"vod_play_url": "一$u#二$v"}]}')

    monkeypatch.setattr(movie_controller.requests, 'get', get)
    # 无需创建播放器、历史库或注册事件，即可执行真实搜索结果转换及选集路径。
    ctrl = movie_controller.MovieController.__new__(movie_controller.MovieController)
    items = ctrl._search_one('https://source.invalid/api', ' 测试剧 ', timeout=2)
    assert items == [{
        'vod_id': 9, 'vod_name': '测试剧', 'vod_remarks': '',
        'vod_pic': 'https://img.invalid/a.jpg', 'play_url': '一$u#二$v',
        'first_name': '一', 'first_url': 'u',
    }]
    assert calls[0][1]['params'] == {'ac': 'videolist', 'wd': '测试剧'}
    assert calls[0][1]['timeout'] == 2
    assert ctrl.episodes(items[0]) == [('一', 'u'), ('二', 'v')]
    assert ctrl.is_multi_episode(items[0]) is True


def test_parser_source_can_execute_without_controller_resources(monkeypatch):
    """单独加载纯模块；包 __init__ 的既有 eager 导入不在本轮修改范围。"""
    import builtins

    original = builtins.__import__

    def only_stdlib(name, *args, **kwargs):
        assert name == 'json', f'unexpected parser dependency: {name}'
        return original(name, *args, **kwargs)

    path = Path(cms_parser.__file__)
    code = compile(path.read_text(encoding='utf-8'), str(path), 'exec')
    namespace = {'__name__': '_isolated_movie_cms_parser'}
    with monkeypatch.context() as patch:
        patch.setattr(builtins, '__import__', only_stdlib)
        exec(code, namespace)
        assert namespace['first_play_url']('第一集$u#第二集$v') == ('第一集', 'u')
        assert namespace['parse_cms_response']('{"list": []}') == {'list': []}
