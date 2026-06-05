import json
import asyncio
import websockets
import logging
from typing import List, Dict, Any, Optional
from dataclasses import dataclass
import time
import threading

@dataclass
class PlayerListInfo:
    current_players: int = 0
    max_players: int = 20
    player_names: List[str] = None
    
    def __init__(self):
        self.player_names = []
    
    def __str__(self):
        return f"PlayerListInfo{{current={self.current_players}, max={self.max_players}, players={', '.join(self.player_names)}}}"

class MSMPClient:
    """
    Minecraft Server Management Protocol (MSMP) 客户端
    基于 JSON-RPC 2.0 over WebSocket
    支持 MSMP 2.0 协议
    """
    
    def __init__(self, host: str, port: int, auth_token: str, logger: logging.Logger, config_manager=None):
        self.host = host
        self.port = port
        self.auth_token = auth_token
        self.logger = logger
        self.config_manager = config_manager
        
        self.websocket = None
        self.connected = False
        self.authenticated = False
        self.request_id_counter = 1
        self.pending_requests = {}
        self.event_listener = None
        
        # 心跳相关 - 新版 MSMP 使用 server/status 通知作为状态心跳
        self.last_status_time = 0
        self.last_activity_time = 0
        self.latest_server_status = {}
        
        # 从配置读取心跳间隔，默认30秒
        if config_manager:
            self.heartbeat_check_interval = config_manager.config.get('advanced', {}).get('heartbeat_interval', 30)
        else:
            self.heartbeat_check_interval = 30
        
        # activity超时 = 心跳间隔 * 3 (允许错过2-3次通知)
        self.activity_timeout = self.heartbeat_check_interval * 3
        
        self.logger.debug(
            f"MSMP心跳配置: 检查间隔={self.heartbeat_check_interval}秒, "
            f"超时时间={self.activity_timeout}秒"
        )
        
        self.heartbeat_task = None
        self.receive_task = None
        
        self.loop = asyncio.new_event_loop()
        self.thread = None
        self._loop_ready = threading.Event()
    
    async def connect(self):
        """连接到MSMP服务器"""
        try:
            # 检查是否已经连接
            if self.connected and self._websocket_open():
                self.logger.debug("MSMP已经连接,无需重复连接")
                return True
                
            headers = {"Authorization": f"Bearer {self.auth_token}"}
            self.websocket = await self._connect_websocket(headers)
            
            self.connected = True
            self.authenticated = True
            self.last_status_time = time.time()
            self.last_activity_time = time.time()
            self.logger.info(f"已连接到MSMP服务器 {self.host}:{self.port}")
            
            # 启动消息接收循环
            self.receive_task = asyncio.create_task(self._receive_loop())
            
            # 等待一下确保接收循环已启动
            await asyncio.sleep(1)
            
            # 启动心跳检测
            self.heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            
            return True
            
        except Exception as e:
            self.logger.error(f"连接MSMP服务器失败: {e}")
            self.connected = False
            self.authenticated = False
            raise

    async def _connect_websocket(self, headers: Dict[str, str]):
        """兼容 websockets 新旧版本的请求头参数名。"""
        uri = f"ws://{self.host}:{self.port}"
        connect_options = {
            "ping_interval": None,
            "ping_timeout": None,
            "close_timeout": 10
        }

        try:
            return await websockets.connect(
                uri,
                additional_headers=headers,
                **connect_options
            )
        except TypeError:
            return await websockets.connect(
                uri,
                extra_headers=headers,
                **connect_options
            )

    def _websocket_open(self) -> bool:
        """兼容 websockets legacy/client 两套连接对象。"""
        if not self.websocket:
            return False

        closed = getattr(self.websocket, "closed", None)
        if closed is not None:
            return not closed

        state = getattr(self.websocket, "state", None)
        if state is not None:
            return getattr(state, "name", "") == "OPEN"

        close_code = getattr(self.websocket, "close_code", None)
        return close_code is None
    
    def set_shutdown_mode(self):
        """设置关闭模式,停止所有活动"""
        self.connected = False
        self.authenticated = False
        
        # 取消所有任务
        if self.heartbeat_task and not self.heartbeat_task.done():
            self.heartbeat_task.cancel()
        
        if self.receive_task and not self.receive_task.done():
            self.receive_task.cancel()
        
        # 清理pending请求
        for request_id, future in list(self.pending_requests.items()):
            if not future.done():
                future.set_exception(Exception("连接已关闭"))
        self.pending_requests.clear()
        
        self.logger.debug("MSMP客户端已进入关闭模式")
    
    async def _heartbeat_loop(self):
        """心跳检测循环 - 基于 server/activity 通知"""
        consecutive_failures = 0
        max_consecutive_failures = 3
        
        while self.connected:
            try:
                if self._websocket_open():
                    # 新版以 server/status 为心跳；旧版 activity 仍作为兼容信号。
                    last_signal_time = max(self.last_status_time, self.last_activity_time)
                    time_since_last_signal = time.time() - last_signal_time
                    
                    if time_since_last_signal > self.activity_timeout:
                        consecutive_failures += 1
                        self.logger.warning(
                            f"长时间未收到 MSMP 状态心跳 ({time_since_last_signal:.1f}秒), "
                            f"连续失败: {consecutive_failures}/{max_consecutive_failures}"
                        )
                        
                        if consecutive_failures >= max_consecutive_failures:
                            self.logger.error("心跳超时,连接可能已断开")
                            self.connected = False
                            self.authenticated = False
                            break
                    else:
                        # 重置失败计数
                        if consecutive_failures > 0:
                            self.logger.debug("心跳恢复正常")
                            consecutive_failures = 0
                        
                        if self.logger.isEnabledFor(logging.DEBUG):
                            self.logger.debug(
                                f"心跳正常 (距上次状态/活动通知: {time_since_last_signal:.1f}秒)"
                            )
                
                # 等待下次检查
                await asyncio.sleep(self.heartbeat_check_interval)
                
            except asyncio.CancelledError:
                self.logger.debug("心跳循环被取消")
                break
            except Exception as e:
                self.logger.error(f"心跳循环异常: {e}")
                break

    async def _receive_loop(self):
        """消息接收循环"""
        try:
            async for message in self.websocket:
                try:
                    await self._handle_message(message)
                except Exception as e:
                    self.logger.error(f"处理消息时出错: {e}", exc_info=True)
                    
        except websockets.exceptions.ConnectionClosed as e:
            self.logger.debug(f"MSMP连接已关闭: {e}")
            self.connected = False
            self.authenticated = False
            
        except asyncio.CancelledError:
            self.logger.debug("接收循环被取消")
            
        except Exception as e:
            self.logger.error(f"接收循环异常: {e}", exc_info=True)
            self.connected = False
            self.authenticated = False
    
    async def _handle_message(self, message: str):
        """处理接收到的消息"""
        try:
            if self.logger.isEnabledFor(logging.DEBUG):
                self.logger.debug(f"收到MSMP消息: {message[:200]}")
            
            data = json.loads(message)
            
            # 检查是否是响应消息(有 id 字段)
            if 'id' in data and data['id'] is not None:
                request_id = data['id']
                future = self.pending_requests.pop(request_id, None)
                
                if future and not future.done():
                    if 'error' in data:
                        error_msg = data['error'].get('message', 'Unknown error')
                        future.set_exception(Exception(error_msg))
                    else:
                        future.set_result(data)
            
            # 检查是否是通知消息(有 method 字段)
            elif 'method' in data:
                await self._handle_notification(data)
                
        except json.JSONDecodeError as e:
            self.logger.error(f"JSON解析失败: {e}")
        except Exception as e:
            self.logger.error(f"处理MSMP消息失败: {e}", exc_info=True)
    
    async def _handle_notification(self, notification: Dict[str, Any]):
        """处理通知消息"""
        method = notification.get('method', '')
        params_obj = self._extract_params_object(notification.get('params'))
        
        if self.logger.isEnabledFor(logging.DEBUG):
            self.logger.debug(f"处理通知 - 方法: {method}, 参数: {params_obj}")
        
        if method == 'minecraft:notification/server/status':
            self.last_status_time = time.time()
            self.latest_server_status = params_obj.get('status', params_obj)
            if self.logger.isEnabledFor(logging.DEBUG):
                self.logger.debug(f"收到 server/status 心跳通知 (时间戳: {self.last_status_time})")
            return  # status通知只用于心跳和缓存,不需传递给事件监听器

        if method == 'minecraft:notification/server/activity':
            self.last_activity_time = time.time()
            if self.logger.isEnabledFor(logging.DEBUG):
                self.logger.debug(f"收到 server/activity 活动通知 (时间戳: {self.last_activity_time})")
            return  # activity通知只用于兼容旧版心跳,不需传递给事件监听器
        
        # 处理其他通知
        if self.event_listener:
            try:
                if method == 'minecraft:notification/server/started':
                    self.event_listener.on_server_started(params_obj)
                elif method == 'minecraft:notification/server/stopping':
                    self.event_listener.on_server_stopping(params_obj)
                elif method == 'minecraft:notification/players/joined':
                    self.event_listener.on_player_join(params_obj)
                elif method == 'minecraft:notification/players/left':
                    self.event_listener.on_player_leave(params_obj)
                elif method == 'minecraft:notification/server/saving':
                    self.logger.info("服务器正在保存...")
                elif method == 'minecraft:notification/server/saved':
                    self.logger.info("服务器保存完成")
                else:
                    if self.logger.isEnabledFor(logging.DEBUG):
                        self.logger.debug(f"未处理的通知: {method}")
            except Exception as e:
                self.logger.error(f"处理事件监听器回调时出错: {e}", exc_info=True)
    
    def _extract_params_object(self, params: Any) -> Dict[str, Any]:
        """新版通知直接传对象，旧实现可能传单元素数组。"""
        if isinstance(params, dict):
            return params
        if isinstance(params, list) and params:
            first = params[0]
            return first if isinstance(first, dict) else {"value": first}
        return {}

    async def send_request(self, method: str, params: Any = None) -> Dict[str, Any]:
        """发送JSON-RPC请求"""
        if not self.connected or not self._websocket_open():
            raise Exception("MSMP连接未就绪")
        
        request_id = self.request_id_counter
        self.request_id_counter += 1
        
        # 构建请求
        request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": f"minecraft:{method}" if not method.startswith("minecraft:") else method
        }
        if params is not None:
            request["params"] = params
        
        future = asyncio.Future()
        self.pending_requests[request_id] = future
        
        try:
            request_json = json.dumps(request)
            if self.logger.isEnabledFor(logging.DEBUG):
                self.logger.debug(f"发送MSMP请求: {request_json[:200]}")
            
            await self.websocket.send(request_json)
            
            # 等待响应,设置超时
            return await asyncio.wait_for(future, timeout=30.0)
            
        except asyncio.TimeoutError:
            self.pending_requests.pop(request_id, None)
            raise Exception(f"请求 {method} 超时")
        except Exception as e:
            self.pending_requests.pop(request_id, None)
            raise e
    
    async def get_server_status(self) -> Dict[str, Any]:
        """获取服务器状态"""
        response = await self.send_request("server/status")
        return response.get('result', {})
    
    async def get_player_list(self) -> PlayerListInfo:
        """获取玩家列表"""
        try:
            response = await self.send_request("players")
            result = response.get('result', [])
        except Exception as e:
            self.logger.warning(f"通过 players 接口获取玩家失败,尝试 server/status: {e}")
            result = await self.get_server_status()
        
        info = PlayerListInfo()

        players = self._extract_players(result)
        info.player_names = [name for name in (self._extract_player_name(player) for player in players) if name]
        info.current_players = len(info.player_names)
        info.max_players = self._extract_max_players(result)

        # 获取最大玩家数
        try:
            max_players_response = await self.send_request("serversettings/max_players")
            info.max_players = self._extract_setting_value(max_players_response.get('result'), info.max_players)
        except Exception as e:
            self.logger.warning(f"获取最大玩家数失败: {e}")
        
        return info

    def _extract_players(self, result: Any) -> List[Any]:
        """兼容 players 结果数组、包装对象和 server/status 结构。"""
        if isinstance(result, list):
            return result

        if isinstance(result, dict):
            status = result.get("status")
            if isinstance(status, dict):
                return self._extract_players(status)

            for key in ("players", "player"):
                players = result.get(key)
                if isinstance(players, list):
                    return players
                if isinstance(players, dict):
                    sample = players.get("sample")
                    if isinstance(sample, list):
                        return sample

        return []

    def _extract_player_name(self, player: Any) -> str:
        if isinstance(player, str):
            return player
        if not isinstance(player, dict):
            return ""

        for key in ("name", "username"):
            value = player.get(key)
            if isinstance(value, str):
                return value

        nested_player = player.get("player")
        if isinstance(nested_player, dict):
            return self._extract_player_name(nested_player)

        return ""

    def _extract_max_players(self, result: Any) -> int:
        if not isinstance(result, dict):
            return 20

        status = result.get("status")
        if isinstance(status, dict):
            value = self._extract_max_players(status)
            if value != 20:
                return value

        players = result.get("players") or result.get("player")
        if isinstance(players, dict):
            for key in ("max", "max_players", "maxPlayers"):
                value = players.get(key)
                if isinstance(value, int):
                    return value

        for key in ("max_players", "maxPlayers"):
            value = result.get(key)
            if isinstance(value, int):
                return value

        return 20

    def _extract_setting_value(self, result: Any, default: Any) -> Any:
        if isinstance(result, dict):
            for key in ("value", "max_players", "maxPlayers"):
                if key in result:
                    return result[key]
        return result if result is not None else default
    
    async def execute_command(self, command: str) -> Dict[str, Any]:
        """执行命令"""
        if command.lower() == "server/stop":
            response = await self.send_request("server/stop")
            return response
        else:
            params = [{"command": command}]
            response = await self.send_request("server/command", params)
        return response
    
    async def get_game_rules(self) -> Dict[str, Any]:
        """获取游戏规则"""
        response = await self.send_request("gamerules")
        return response

    async def close(self):
        """关闭连接"""
        self.connected = False
        self.authenticated = False
        
        # 取消任务
        if self.heartbeat_task and not self.heartbeat_task.done():
            self.heartbeat_task.cancel()
            try:
                await self.heartbeat_task
            except asyncio.CancelledError:
                pass
        
        if self.receive_task and not self.receive_task.done():
            self.receive_task.cancel()
            try:
                await self.receive_task
            except asyncio.CancelledError:
                pass
        
        # 清理pending请求
        for request_id, future in list(self.pending_requests.items()):
            if not future.done():
                future.set_exception(Exception("连接已关闭"))
        self.pending_requests.clear()
        
        # 关闭WebSocket
        if self._websocket_open():
            await self.websocket.close()
    
    def is_authenticated(self) -> bool:
        """检查是否已认证(完整检查)"""
        result = (self.authenticated and 
                 self.connected and 
                 self._websocket_open())
        
        # 调试日志
        if not result and self.logger.isEnabledFor(logging.DEBUG):
            self.logger.debug(
                f"认证检查失败: authenticated={self.authenticated}, "
                f"connected={self.connected}, "
                f"websocket={'exists' if self.websocket else 'None'}, "
                f"open={self._websocket_open() if self.websocket else 'N/A'}"
            )
        
        return result
    
    def is_connected(self) -> bool:
        """检查是否已连接(简化检查,更可靠)"""
        return self.connected and self._websocket_open()
    
    def get_connection_status(self) -> str:
        """获取连接状态描述"""
        if not self._websocket_open():
            return "连接已关闭"
        if not self.connected:
            return "连接未就绪"
        if not self.authenticated:
            return "未认证"
        
        # 检查心跳状态
        time_since_signal = time.time() - max(self.last_status_time, self.last_activity_time)
        if time_since_signal > self.activity_timeout:
            return f"心跳超时({time_since_signal:.0f}秒)"
        
        return "连接正常"
    
    def get_detailed_status(self) -> Dict[str, Any]:
        """获取详细状态信息"""
        last_signal_time = max(self.last_status_time, self.last_activity_time)
        time_since_signal = time.time() - last_signal_time
        
        return {
            'connected': self.connected,
            'authenticated': self.authenticated,
            'websocket_open': self._websocket_open(),
            'last_status_seconds_ago': round(time.time() - self.last_status_time, 1) if self.last_status_time else -1,
            'last_activity_seconds_ago': round(time.time() - self.last_activity_time, 1) if self.last_activity_time else -1,
            'last_signal_seconds_ago': round(time_since_signal, 1),
            'activity_timeout': self.activity_timeout,
            'heartbeat_status': 'normal' if time_since_signal < self.activity_timeout else 'timeout',
            'status_description': self.get_connection_status(),
            'server_status': self.latest_server_status
        }
    
    def set_event_listener(self, listener):
        """设置事件监听器"""
        self.event_listener = listener
    
    def start_background_loop(self):
        """在后台线程中启动事件循环"""
        def run_loop():
            asyncio.set_event_loop(self.loop)
            self._loop_ready.set()
            self.loop.run_forever()
        
        if self.thread and self.thread.is_alive():
            return

        self._loop_ready.clear()
        self.thread = threading.Thread(target=run_loop, daemon=True)
        self.thread.start()
        if not self._loop_ready.wait(timeout=5):
            raise RuntimeError("MSMP后台事件循环启动超时")
    
    # 同步方法包装器
    def connect_sync(self):
        """同步连接"""
        future = asyncio.run_coroutine_threadsafe(self.connect(), self.loop)
        return future.result(timeout=30)
    
    def get_server_status_sync(self) -> Dict[str, Any]:
        """同步获取服务器状态"""
        future = asyncio.run_coroutine_threadsafe(self.get_server_status(), self.loop)
        return future.result(timeout=10)
    
    def get_player_list_sync(self) -> PlayerListInfo:
        """同步获取玩家列表"""
        future = asyncio.run_coroutine_threadsafe(self.get_player_list(), self.loop)
        return future.result(timeout=10)

    def send_request_sync(self, method: str, params: Any = None) -> Dict[str, Any]:
        """同步发送JSON-RPC请求"""
        future = asyncio.run_coroutine_threadsafe(self.send_request(method, params), self.loop)
        return future.result(timeout=30)
    
    def execute_command_sync(self, command: str) -> Dict[str, Any]:
        """同步执行命令"""
        future = asyncio.run_coroutine_threadsafe(self.execute_command(command), self.loop)
        return future.result(timeout=30)

    def get_game_rules_sync(self) -> Dict[str, Any]:
        """同步获取游戏规则"""
        future = asyncio.run_coroutine_threadsafe(self.get_game_rules(), self.loop)
        return future.result(timeout=30)
    
    def close_sync(self):
        """同步关闭连接"""
        future = asyncio.run_coroutine_threadsafe(self.close(), self.loop)
        return future.result(timeout=5)

    def shutdown_sync(self):
        """同步关闭连接并停止后台事件循环线程。"""
        try:
            if self.loop and self.loop.is_running():
                future = asyncio.run_coroutine_threadsafe(self.close(), self.loop)
                future.result(timeout=5)
            else:
                self.connected = False
                self.authenticated = False
        finally:
            if self.loop and self.loop.is_running():
                self.loop.call_soon_threadsafe(self.loop.stop)
            if self.thread and self.thread.is_alive():
                self.thread.join(timeout=5)
            self.thread = None


class ServerEventListener:
    """服务器事件监听器接口"""
    
    def on_server_started(self, params: Dict[str, Any]):
        pass
    
    def on_server_stopping(self, params: Dict[str, Any]):
        pass
    
    def on_player_join(self, params: Dict[str, Any]):
        pass
    
    def on_player_leave(self, params: Dict[str, Any]):
        pass
