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
        
        # 心跳相关
        self.last_pong_time = 0
        self.heartbeat_interval = 30
        self.heartbeat_timeout = 90
        self.heartbeat_task = None
        self.receive_task = None
        
        # 重连控制
        self.reconnecting = False
        self.reconnect_lock = asyncio.Lock()
        self.max_reconnect_delay = 300
        self.reconnect_attempts = 0
        
        self.loop = asyncio.new_event_loop()
        self.thread = None
    
    async def connect(self):
        """连接到MSMP服务器"""
        try:
            headers = {"Authorization": f"Bearer {self.auth_token}"}
            self.websocket = await websockets.connect(
                f"ws://{self.host}:{self.port}",
                extra_headers=headers,
                ping_interval=None,
                ping_timeout=None,
                close_timeout=10
            )
            
            self.connected = True
            self.authenticated = True
            self.last_pong_time = time.time()
            self.reconnect_attempts = 0
            self.logger.info(f"已连接到MSMP服务器 {self.host}:{self.port}")
            
            # 启动消息接收循环
            self.receive_task = asyncio.create_task(self._receive_loop())
            
            # 启动心跳检测
            await asyncio.sleep(2)
            self.heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            
            # 验证连接有效性
            try:
                test_response = await asyncio.wait_for(
                    self.send_request("server/status"),
                    timeout=10.0
                )
                self.logger.info("MSMP连接验证成功")
                return True
                
            except asyncio.TimeoutError:
                self.logger.warning("连接验证超时，但保持连接状态")
                return True
            except Exception as e:
                self.logger.warning(f"连接验证失败: {e}，但保持连接状态")
                return True
        
        except Exception as e:
            self.logger.error(f"连接MSMP服务器失败: {e}")
            self.connected = False
            self.authenticated = False
            raise
    
    async def _heartbeat_loop(self):
        """心跳检测循环"""
        consecutive_failures = 0
        max_consecutive_failures = 3
        
        while self.connected:
            try:
                if self.websocket and not self.websocket.closed:
                    try:
                        # 使用 websocket.ping()
                        pong_waiter = await self.websocket.ping()
                        await asyncio.wait_for(pong_waiter, timeout=10.0)
                        
                        self.last_pong_time = time.time()
                        consecutive_failures = 0
                        
                        if self.logger.isEnabledFor(logging.DEBUG):
                            self.logger.debug("心跳检测成功")
                            
                    except asyncio.TimeoutError:
                        consecutive_failures += 1
                        self.logger.warning(f"心跳超时 (连续失败: {consecutive_failures}/{max_consecutive_failures})")
                        
                        if consecutive_failures >= max_consecutive_failures:
                            self.logger.error("心跳连续失败，触发重连")
                            await self.attempt_reconnect()
                            break
                    
                    # 检查上次成功时间
                    time_since_last_success = time.time() - self.last_pong_time
                    if time_since_last_success > self.heartbeat_timeout:
                        self.logger.warning(f"心跳超时 ({time_since_last_success:.1f}秒)，重新连接...")
                        await self.attempt_reconnect()
                        break
                
                await asyncio.sleep(self.heartbeat_interval)
                
            except Exception as e:
                self.logger.error(f"心跳循环异常: {e}")
                await self.attempt_reconnect()
                break
    
    def get_detailed_status(self) -> dict:
        """获取详细连接状态"""
        return {
            "connected": self.connected,
            "authenticated": self.authenticated,
            "websocket_open": self.websocket and not self.websocket.closed,
            "pending_requests": len(self.pending_requests),
            "last_pong_time": self.last_pong_time,
            "time_since_last_pong": time.time() - self.last_pong_time if self.last_pong_time else None,
            "reconnect_attempts": self.reconnect_attempts
        }

    async def _receive_loop(self):
        """消息接收循环"""
        try:
            async for message in self.websocket:
                try:
                    await self._handle_message(message)
                except Exception as e:
                    self.logger.error(f"处理消息时出错: {e}", exc_info=True)
                    
        except websockets.exceptions.ConnectionClosed as e:
            self.logger.info(f"MSMP连接已关闭: {e}")
            self.connected = False
            self.authenticated = False
            asyncio.create_task(self.attempt_reconnect())
            
        except Exception as e:
            self.logger.error(f"接收循环异常: {e}", exc_info=True)
            self.connected = False
            self.authenticated = False
            asyncio.create_task(self.attempt_reconnect())
    
    async def _handle_message(self, message: str):
        """处理接收到的消息"""
        try:
            if self.logger.isEnabledFor(logging.DEBUG):
                self.logger.debug(f"收到MSMP消息: {message[:200]}")
            
            data = json.loads(message)
            
            # 检查是否是响应消息（有 id 字段）
            if 'id' in data and data['id'] is not None:
                request_id = data['id']
                future = self.pending_requests.pop(request_id, None)
                
                if future and not future.done():
                    if 'error' in data:
                        error_msg = data['error'].get('message', 'Unknown error')
                        future.set_exception(Exception(error_msg))
                    else:
                        future.set_result(data)
            
            # 检查是否是通知消息（有 method 字段）
            elif 'method' in data:
                await self._handle_notification(data)
                
        except json.JSONDecodeError as e:
            self.logger.error(f"JSON解析失败: {e}")
        except Exception as e:
            self.logger.error(f"处理MSMP消息失败: {e}", exc_info=True)
    
    async def _handle_notification(self, notification: Dict[str, Any]):
        """处理通知消息"""
        method = notification.get('method', '')
        params = notification.get('params', [])
        
        # 从数组中提取参数对象
        params_obj = params[0] if params and len(params) > 0 else {}
        
        if self.logger.isEnabledFor(logging.DEBUG):
            self.logger.debug(f"处理通知 - 方法: {method}, 参数: {params_obj}")
        
        if self.event_listener:
            try:
                # 处理通知方法
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
    
    async def send_request(self, method: str, params: List[Any] = None) -> Dict[str, Any]:
        """发送JSON-RPC请求"""
        if not self.connected or not self.websocket or self.websocket.closed:
            raise Exception("MSMP连接未就绪")
        
        request_id = self.request_id_counter
        self.request_id_counter += 1
        
        # 构建请求
        request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": f"minecraft:{method}" if not method.startswith("minecraft:") else method,
            "params": params or []
        }
        
        future = asyncio.Future()
        self.pending_requests[request_id] = future
        
        try:
            request_json = json.dumps(request)
            if self.logger.isEnabledFor(logging.DEBUG):
                self.logger.debug(f"发送MSMP请求: {request_json[:200]}")
            
            await self.websocket.send(request_json)
            
            # 等待响应，设置超时
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
        response = await self.send_request("players")
        
        info = PlayerListInfo()
        
        if 'result' in response:
            players = response['result']
            info.player_names = [player.get('name', '') for player in players if 'name' in player]
            info.current_players = len(info.player_names)
            
            # 获取最大玩家数
            try:
                max_players_response = await self.send_request("serversettings/max_players")
                info.max_players = max_players_response.get('result', 20)
            except Exception as e:
                self.logger.warning(f"获取最大玩家数失败: {e}")
                info.max_players = 20
        
        return info
    
    async def execute_command(self, command: str) -> Dict[str, Any]:
        """执行命令"""
        if command.lower() == "server/stop":
            params = []
            response = await self.send_request("server/stop", params)
            return response
        else:
            params = [{"command": command}]
            response = await self.send_request("server/command", params)
        return response
    
    async def get_game_rules(self) -> Dict[str, Any]:
        """获取游戏规则"""
        response = await self.send_request("gamerules")
        return response
    
    async def attempt_reconnect(self):
        """尝试重新连接 - 使用锁防止并发重连"""
        if self.reconnecting:
            return
        
        async with self.reconnect_lock:
            if self.reconnecting:
                return
            
            self.reconnecting = True
            
            try:
                # 清理状态
                self.connected = False
                self.authenticated = False
                
                # 取消心跳任务
                if self.heartbeat_task and not self.heartbeat_task.done():
                    self.heartbeat_task.cancel()
                
                # 关闭WebSocket连接
                if self.websocket and not self.websocket.closed:
                    await self.websocket.close()
                
                # 指数退避重连
                self.reconnect_attempts += 1
                delay = min(
                    5 * (2 ** (self.reconnect_attempts - 1)),
                    self.max_reconnect_delay
                )
                
                self.logger.info(f"第 {self.reconnect_attempts} 次重连尝试，{delay}秒后执行...")
                await asyncio.sleep(delay)
                
                # 尝试重新连接
                await self.connect()
                self.logger.info("重新连接成功")
                
            except Exception as e:
                self.logger.error(f"重新连接失败: {e}")
            finally:
                self.reconnecting = False
    
    async def close(self):
        """关闭连接"""
        self.connected = False
        self.authenticated = False
        
        # 取消任务
        if self.heartbeat_task and not self.heartbeat_task.done():
            self.heartbeat_task.cancel()
        
        if self.receive_task and not self.receive_task.done():
            self.receive_task.cancel()
        
        # 清理pending请求
        for request_id, future in list(self.pending_requests.items()):
            if not future.done():
                future.set_exception(Exception("连接已关闭"))
        self.pending_requests.clear()
        
        # 关闭WebSocket
        if self.websocket and not self.websocket.closed:
            await self.websocket.close()
    
    def is_authenticated(self) -> bool:
        """检查是否已认证（完整检查）"""
        result = (self.authenticated and 
                 self.connected and 
                 self.websocket and 
                 not self.websocket.closed)
        
        # 调试日志
        if not result and self.logger.isEnabledFor(logging.DEBUG):
            self.logger.debug(
                f"认证检查失败: authenticated={self.authenticated}, "
                f"connected={self.connected}, "
                f"websocket={'exists' if self.websocket else 'None'}, "
                f"closed={self.websocket.closed if self.websocket else 'N/A'}"
            )
        
        return result
    
    def is_connected(self) -> bool:
        """检查是否已连接（简化检查，更可靠）"""
        return self.connected and self.websocket and not self.websocket.closed
    
    def get_connection_status(self) -> str:
        if not self.websocket or self.websocket.closed:
            return "连接已关闭"
        if not self.connected:
            return "连接未就绪"
        if not self.authenticated:
            return "未认证"
        return "连接正常"
    
    def set_event_listener(self, listener):
        """设置事件监听器"""
        self.event_listener = listener
    
    def start_background_loop(self):
        """在后台线程中启动事件循环"""
        def run_loop():
            asyncio.set_event_loop(self.loop)
            self.loop.run_forever()
        
        self.thread = threading.Thread(target=run_loop, daemon=True)
        self.thread.start()
    
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
    
    def execute_command_sync(self, command: str) -> Dict[str, Any]:
        """同步执行命令"""
        future = asyncio.run_coroutine_threadsafe(self.execute_command(command), self.loop)
        return future.result(timeout=30)
    
    def close_sync(self):
        """同步关闭连接"""
        future = asyncio.run_coroutine_threadsafe(self.close(), self.loop)
        return future.result(timeout=5)


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