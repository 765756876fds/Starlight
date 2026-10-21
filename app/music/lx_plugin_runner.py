"""
落雪音乐(LX Music) 插件运行环境
支持运行 globalThis.lx 接口的.js音源插件
"""
import json
from typing import Any

import requests
from py_mini_racer import MiniRacer


class LxPluginRunner:
    """落雪音乐插件运行器"""

    def __init__(self, plugin_path: str):
        self.ctx = MiniRacer()
        self.plugin_path = plugin_path
        self._event_handlers = {}
        self._init_bridge()
        self._load_plugin(plugin_path)

    def _init_bridge(self):
        """初始化落雪的 globalThis.lx 桥接"""
        # 注入Python调用JS的桥接函数
        bridge_js = """
        // 事件总线
        var _listeners = {};

        // 挂到globalThis.lx
        globalThis.lx = {
            EVENT_NAMES: {
                inited: 'inited',
                request: 'request',
                updateAlert: 'updateAlert',
            },
            on: function(event, handler) {
                if (!_listeners[event]) _listeners[event] = [];
                _listeners[event].push(handler);
            },
            send: function(event, data) {
                if (typeof PythonBridge !== 'undefined') {
                    PythonBridge.send(event, JSON.stringify(data));
                }
            },
            request: function(url, options, callback) {
                if (typeof PythonBridge !== 'undefined') {
                    PythonBridge.request(url, JSON.stringify(options), function(err, resp) {
                        callback(err, JSON.parse(resp));
                    });
                }
            },
            utils: {
                md5: function(s) { return s; },
            },
            env: 'desktop',
            version: '2.12.6',
        };
        """
        self.ctx.eval(bridge_js)

        # 绑定Python回调
        def py_send(event, data_json):
            data = json.loads(data_json)
            print(f"[插件] send: {event}, data: {str(data)[:200]}")
            if event == 'inited':
                self._inited = data
                self._sources = data.get('sources', {})

        def py_request(url, options_json, callback):
            options = json.loads(options_json)
            method = options.get('method', 'GET')
            headers = options.get('headers', {})
            try:
                if method == 'GET':
                    resp = requests.get(url, headers=headers, timeout=15)
                else:
                    resp = requests.post(url, headers=headers, json=options.get('body'), timeout=15)
                
                resp_data = {
                    'status': resp.status_code,
                    'body': resp.json() if 'application/json' in resp.headers.get('content-type', '') else resp.text,
                }
                callback(None, json.dumps(resp_data))
            except Exception as e:
                callback(str(e), None)

        # 在JS里暴露Python桥接
        self.ctx.eval("""
        globalThis.PythonBridge = {
            send: function(event, data) {
                // 调用Python的send
                py_send(event, data);
            },
            request: function(url, options, callback) {
                py_request(url, options, function(err, resp) {
                    callback(err, resp);
                });
            }
        };
        """)

        # 暴露Python函数到JS
        # 注意：py_mini_racer不支持直接传回调，这里先用简化版
        # 后面改成同步调用

    def _load_plugin(self, path: str):
        """加载插件文件"""
        with open(path, 'r', encoding='utf-8') as f:
            code = f.read()
        print(f"[插件] 加载: {path}, 大小: {len(code)} 字符")
        self.ctx.eval(code)
        print("[插件] 加载完成")

    def search(self, keyword: str, source: str = None) -> list[dict[str, Any]]:
        """搜索歌曲"""
        # 触发request事件，action=search
        # 先简化：直接调用插件的搜索逻辑
        # 这里先返回空，后面完善
        return []
