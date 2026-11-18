"""Search ordering only; isolated controller shells, no user DB or real HTTP."""
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from app.movie.movie_controller import MovieController


def scenario():
    names = ['量子资源', '非凡资源', '暴风资源', '光速资源', '极速资源', 'other']
    state = {'sources': [{'name': name, 'url': f'https://synthetic.invalid/source-{i}', 'enabled': True}
                         for i, name in enumerate(names)], 'active': '量子资源'}
    manager = SimpleNamespace(
        get_enabled_sources=lambda: [dict(s) for s in state['sources'] if s.get('enabled', True)],
        get_active_name=lambda: state['active'],
    )
    manager._save = Mock()
    controller = MovieController.__new__(MovieController)
    controller._search_local = threading.local()
    controller._source_manager = manager
    controller._search_one = Mock(side_effect=requests.ConnectTimeout('synthetic'))
    return controller, state


def names_called(controller, state):
    names = {s['url']: s['name'] for s in state['sources']}
    return [names[c.args[0]] for c in controller._search_one.call_args_list]


def test_active_failure_reaches_enabled_jisu_before_original_fallback():
    controller, state = scenario()
    preferred = state['sources'][4]
    controller._search_one.side_effect = lambda url, *args: (
        [{'vod_name': '公开测试片名', 'first_url': 'synthetic'}] if url == preferred['url']
        else (_ for _ in ()).throw(requests.ConnectTimeout('synthetic')))
    result = controller.search('公开测试片名')
    assert len(result) == 1
    assert result[0]['_source'] == '极速资源'
    assert names_called(controller, state) == ['量子资源', '极速资源']
    assert state['active'] == '量子资源'
    controller._source_manager._save.assert_not_called()


def test_current_success_never_queries_preferred_fallback():
    controller, state = scenario()
    controller._search_one.side_effect = None
    controller._search_one.return_value = [{'vod_name': 'public'}]
    assert controller.search('public')[0]['_source'] == '量子资源'
    assert names_called(controller, state) == ['量子资源']


def test_jisu_current_is_not_duplicated():
    controller, state = scenario()
    state['active'] = '极速资源'
    controller.search('public')
    assert names_called(controller, state) == ['极速资源', '量子资源', '非凡资源']


@pytest.mark.parametrize('change', ['disabled', 'removed', 'renamed'])
def test_unavailable_preferred_keeps_original_order_without_old_address(change):
    controller, state = scenario()
    previous = state['sources'][4]['url']
    if change == 'disabled':
        state['sources'][4]['enabled'] = False
    elif change == 'removed':
        state['sources'].pop(4)
    else:
        state['sources'][4]['name'] = 'user renamed source'
    assert controller.search('public') == []
    assert names_called(controller, state) == ['量子资源', '非凡资源', '暴风资源']
    assert previous not in [c.args[0] for c in controller._search_one.call_args_list]


def test_changed_preferred_endpoint_uses_current_configured_value_only():
    controller, state = scenario()
    old = state['sources'][4]['url']
    state['sources'][4]['url'] = 'https://new-configured.synthetic.invalid/cms'
    controller.search('public')
    urls = [c.args[0] for c in controller._search_one.call_args_list]
    assert urls[1] == state['sources'][4]['url']
    assert old not in urls
    controller._source_manager._save.assert_not_called()


def test_empty_preferred_continues_original_fallback_and_stops_at_three():
    controller, state = scenario()
    controller._search_one.side_effect = lambda url, *args: []
    assert controller.search('public') == []
    assert names_called(controller, state) == ['量子资源', '极速资源', '非凡资源']
    assert controller.last_search_error.count('无结果') == 3


def test_all_connection_failures_are_limited_to_three_and_not_normal_empty():
    controller, state = scenario()
    assert controller.search('public') == []
    assert names_called(controller, state) == ['量子资源', '极速资源', '非凡资源']
    assert controller.last_search_error.count('ConnectTimeout') == 3
    assert controller.last_search_source is None


def test_duplicate_current_and_alias_endpoint_is_requested_only_once():
    controller, state = scenario()
    state['sources'].insert(1, dict(state['sources'][0]))
    state['sources'].insert(2, dict(state['sources'][0], name='same endpoint alias'))
    controller.search('public')
    urls = [c.args[0] for c in controller._search_one.call_args_list]
    assert len(urls) == len(set(urls)) == 3
    assert state['sources'][6]['url'] in urls  # jisu shifted by two inserted entries


def test_disabled_current_does_not_gain_priority():
    controller, state = scenario()
    state['sources'][0]['enabled'] = False
    controller.search('public')
    assert names_called(controller, state) == ['极速资源', '非凡资源', '暴风资源']


def test_empty_config_does_not_reintroduce_default_sources():
    controller, state = scenario()
    state['sources'] = []
    assert controller.search('public') == []
    assert controller.last_search_error == '无可用源'
    controller._search_one.assert_not_called()
