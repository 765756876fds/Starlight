"""
LCU (League Client Update) 客户端连接

负责与 LoL 客户端通信：
- 客户端状态（大厅、房间、英雄选择、游戏中）
- GameFlow 状态
- Champion Select 信息
- 玩家信息
- 自动接受、自动 Ban/Pick（可选）

通过读取锁文件获取端口和认证信息。
使用 HTTPS + 基础认证通信。
"""
import json
import os
import re
import ssl
import threading
import time
from typing import Any, Dict, List, Optional

import requests
import urllib3
from websocket import create_connection, WebSocketConnectionClosedException

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class LCUConnection:
    """
    LCU 客户端连接管理器

    自动检测客户端进程、读取锁文件、建立连接。
    支持断线重连。
    """

    def __init__(self, process_name: str = "LeagueClientUx.exe",
                 lockfile_path: Optional[str] = None,
                 reconnect_interval: float = 5.0):
        self.process_name = process_name
        self.lockfile_path = lockfile_path
        self.reconnect_interval = reconnect_interval

        self._host = "127.0.0.1"
        self._port: Optional[int] = None
        self._auth_token: Optional[str] = None
        self._protocol = "https"
        self._session: Optional[requests.Session] = None
        self._ws: Optional[Any] = None

        self._connected = False
        self._running = False
        self._reconnect_thread: Optional[threading.Thread] = None
        self._event_listeners: List[callable] = []
        self._lock = threading.Lock()

        # 游戏流程状态
        self._gameflow_phase = "None"
        self._champ_select_data: Optional[Dict[str, Any]] = None

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def gameflow_phase(self) -> str:
        return self._gameflow_phase

    @property
    def base_url(self) -> str:
        return f"{self._protocol}://{self._host}:{self._port}"

    def start(self):
        """启动 LCU 连接（自动重连）"""
        self._running = True
        self._reconnect_thread = threading.Thread(
            target=self._reconnect_loop, daemon=True, name="lcu-reconnect"
        )
        self._reconnect_thread.start()

    def stop(self):
        """停止 LCU 连接"""
        self._running = False
        self._disconnect()
        if self._reconnect_thread:
            self._reconnect_thread.join(timeout=2)

    def _reconnect_loop(self):
        """重连循环"""
        while self._running:
            if not self._connected:
                try:
                    self._connect()
                except Exception as e:
                    print(f"[LCU] 连接失败: {e}")
            time.sleep(self.reconnect_interval)

    def _connect(self):
        """建立连接"""
        # 1. 尝试多种方式发现连接信息
        port, token, protocol = self._discover_connection_info()

        if not port or not token:
            return

        self._port = port
        self._auth_token = token
        self._protocol = protocol

        # 2. 创建会话
        self._session = requests.Session()
        self._session.verify = False
        self._session.auth = requests.auth.HTTPBasicAuth("riot", self._auth_token)
        self._session.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
        })

        # 3. 测试连接
        try:
            resp = self._session.get(f"{self.base_url}/lol-gameflow/v1/gameflow-phase", timeout=2)
            if resp.status_code == 200:
                self._connected = True
                self._gameflow_phase = resp.json()
                # token 脱敏输出
                masked_token = self._mask_token(token)
                print(f"[LCU] 已连接, 端口={self._port}, token={masked_token}, 游戏流程={self._gameflow_phase}")

                # 4. 启动 WebSocket 监听
                self._start_websocket()
        except Exception as e:
            print(f"[LCU] 连接测试失败: {e}")
            self._disconnect()

    def _discover_connection_info(self) -> tuple:
        """
        发现 LCU 连接信息（端口 + token）

        优先级：
        1. 标准锁文件（国际服 / 官方服）
        2. LeagueClientUx 进程命令行参数（腾讯服 / 国服）

        Returns:
            (port, token, protocol) 元组，失败返回 (None, None, None)
        """
        # 方式 1: 锁文件
        lockfile = self._find_lockfile()
        if lockfile:
            try:
                with open(lockfile, "r") as f:
                    content = f.read().strip()
                parts = content.split(":")
                if len(parts) >= 5:
                    port = int(parts[2])
                    token = parts[3]
                    protocol = parts[4]
                    print(f"[LCU] 从锁文件发现连接信息: port={port}")
                    return port, token, protocol
            except Exception as e:
                print(f"[LCU] 锁文件解析失败: {e}")

        # 方式 2: 进程命令行参数（腾讯服 / 国服）
        try:
            port, token = self._find_from_process_cmdline()
            if port and token:
                print(f"[LCU] 从进程命令行发现连接信息: port={port}")
                return port, token, "https"
        except Exception as e:
            print(f"[LCU] 进程命令行发现失败: {e}")

        return None, None, None

    def _find_from_process_cmdline(self) -> tuple:
        """
        从 LeagueClientUx 进程命令行参数提取端口和 token

        腾讯服不使用标准 lockfile，而是通过命令行参数传递：
        --app-port=XXXXX
        --remoting-auth-token=XXXXX

        Returns:
            (port, token) 元组，失败返回 (None, None)
        """
        try:
            import psutil
        except ImportError:
            print("[LCU] psutil 未安装，无法从进程命令行获取连接信息")
            return None, None

        for proc in psutil.process_iter(["name", "cmdline"]):
            try:
                if proc.info["name"] != self.process_name:
                    continue

                cmdline = proc.info.get("cmdline") or []
                app_port = None
                auth_token = None

                for arg in cmdline:
                    if arg.startswith("--app-port="):
                        app_port = int(arg.split("=", 1)[1])
                    elif arg.startswith("--remoting-auth-token="):
                        auth_token = arg.split("=", 1)[1]

                if app_port and auth_token:
                    return app_port, auth_token

            except (psutil.NoSuchProcess, psutil.AccessDenied, KeyError):
                continue

        return None, None

    def _mask_token(self, token: str) -> str:
        """脱敏 token，只显示前 4 位和后 2 位"""
        if not token or len(token) < 6:
            return "****"
        return f"{token[:4]}...{token[-2:]}"

    def _find_lockfile(self) -> Optional[str]:
        """查找 LCU 锁文件"""
        if self.lockfile_path and os.path.exists(self.lockfile_path):
            return self.lockfile_path

        # 常见路径
        possible_paths = [
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Riot Games", "Riot Client", "Config", "lockfile"),
            "C:\\Riot Games\\League of Legends\\lockfile",
            "D:\\Riot Games\\League of Legends\\lockfile",
            "D:\\WeGameApps\\英雄联盟\\lockfile",
        ]

        # 从进程查找
        try:
            import psutil
            for proc in psutil.process_iter(["name", "exe"]):
                if proc.info["name"] == self.process_name and proc.info["exe"]:
                    lockfile = os.path.join(os.path.dirname(proc.info["exe"]), "lockfile")
                    if os.path.exists(lockfile):
                        return lockfile
        except ImportError:
            pass

        for path in possible_paths:
            if os.path.exists(path):
                return path

        return None

    def _parse_lockfile(self, lockfile_path: str):
        """解析锁文件格式：name:pid:port:password:protocol"""
        with open(lockfile_path, "r") as f:
            content = f.read().strip()

        parts = content.split(":")
        if len(parts) >= 5:
            self._port = int(parts[2])
            self._auth_token = parts[3]
            self._protocol = parts[4]

    def _start_websocket(self):
        """启动 WebSocket 监听事件"""
        ws_url = f"wss://{self._host}:{self._port}/"
        try:
            self._ws = create_connection(
                ws_url,
                sslopt={"cert_reqs": ssl.CERT_NONE},
                header=["Authorization: Basic " + self._basic_auth()],
                timeout=5,
            )
            # 订阅所有事件
            self._ws.send(json.dumps([5, "OnJsonApiEvent"]))

            # 启动监听线程
            thread = threading.Thread(
                target=self._ws_listen_loop, daemon=True, name="lcu-ws"
            )
            thread.start()
        except Exception as e:
            print(f"[LCU] WebSocket 连接失败: {e}")

    def _ws_listen_loop(self):
        """WebSocket 监听循环"""
        while self._connected and self._ws:
            try:
                result = self._ws.recv()
                if result:
                    data = json.loads(result)
                    if len(data) >= 3 and isinstance(data[2], dict):
                        event = data[2]
                        self._handle_ws_event(event)
            except WebSocketConnectionClosedException:
                print("[LCU] WebSocket 断开")
                self._disconnect()
                break
            except Exception as e:
                if self._connected:
                    print(f"[LCU] WebSocket 错误: {e}")
                time.sleep(0.1)

    def _handle_ws_event(self, event: Dict[str, Any]):
        """处理 WebSocket 事件"""
        uri = event.get("uri", "")
        data = event.get("data")
        event_type = event.get("eventType", "")

        # 游戏流程变化
        if "/lol-gameflow/v1/gameflow-phase" in uri:
            self._gameflow_phase = data if isinstance(data, str) else data.get("phase", "None")
            print(f"[LCU] 游戏流程变化: {self._gameflow_phase}")

        # 英雄选择变化
        elif "/lol-champ-select/v1/session" in uri:
            self._champ_select_data = data

        # 通知监听器
        for listener in self._event_listeners:
            try:
                listener(uri, data, event_type)
            except Exception:
                pass

    def _basic_auth(self) -> str:
        """生成基础认证 token"""
        import base64
        credentials = f"riot:{self._auth_token}"
        return base64.b64encode(credentials.encode()).decode()

    def _disconnect(self):
        """断开连接"""
        self._connected = False
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None
        if self._session:
            self._session.close()
            self._session = None

    # ===== 公共 API =====

    def request(self, method: str, endpoint: str,
                data: Optional[Dict[str, Any]] = None) -> Optional[Any]:
        """
        发送 LCU API 请求

        Args:
            method: GET / POST / PUT / PATCH / DELETE
            endpoint: API 端点，如 /lol-gameflow/v1/gameflow-phase
            data: 请求体数据

        Returns:
            响应 JSON 数据或 None
        """
        if not self._connected or not self._session:
            return None

        url = f"{self.base_url}{endpoint}"
        try:
            if method.upper() == "GET":
                resp = self._session.get(url, timeout=5)
            elif method.upper() == "POST":
                resp = self._session.post(url, json=data, timeout=5)
            elif method.upper() == "PUT":
                resp = self._session.put(url, json=data, timeout=5)
            elif method.upper() == "PATCH":
                resp = self._session.patch(url, json=data, timeout=5)
            elif method.upper() == "DELETE":
                resp = self._session.delete(url, timeout=5)
            else:
                return None

            if resp.status_code in (200, 201, 204):
                if resp.content:
                    try:
                        return resp.json()
                    except json.JSONDecodeError:
                        return resp.text
                return None
            return None
        except Exception as e:
            print(f"[LCU] 请求失败 {method} {endpoint}: {e}")
            return None

    def get(self, endpoint: str) -> Optional[Any]:
        """GET 请求"""
        return self.request("GET", endpoint)

    def post(self, endpoint: str, data: Optional[Dict[str, Any]] = None) -> Optional[Any]:
        """POST 请求"""
        return self.request("POST", endpoint, data)

    def get_gameflow_phase(self) -> str:
        """获取当前游戏流程阶段"""
        if self._connected:
            phase = self.get("/lol-gameflow/v1/gameflow-phase")
            if phase:
                self._gameflow_phase = phase
        return self._gameflow_phase

    def get_champ_select_session(self) -> Optional[Dict[str, Any]]:
        """获取英雄选择会话"""
        if self._champ_select_data:
            return self._champ_select_data
        return self.get("/lol-champ-select/v1/session")

    def accept_matchmaking(self) -> bool:
        """接受匹配（自动接受）"""
        result = self.post("/lol-matchmaking/v1/ready-check/accept")
        return result is not None or self._connected

    def add_event_listener(self, listener: callable):
        """添加事件监听器 listener(uri, data, event_type)"""
        self._event_listeners.append(listener)

    def close(self):
        """关闭连接"""
        self.stop()
