import asyncio
import websockets
import json
import logging
import subprocess
import os
from typing import List, Dict, Any, Optional
import time
from command_handler import CommandHandler, CommandHandlers
from rcon_client import RCONClient

class QQBotWebSocketServer:
    """
    QQ机器人WebSocket反向连接服务器
    支持OneBot 11协议
    """
    
    def __init__(self, port: int, allowed_groups: List[int], msmp_client, logger: logging.Logger, access_token: str = "", config_manager=None, rcon_client=None):
        self.port = port
        self.allowed_groups = allowed_groups
        self.msmp_client = msmp_client
        self.rcon_client = rcon_client
        self.logger = logger
        self.access_token = access_token
        self.config_manager = config_manager
        
        self.current_connection = None
        self.server = None
        self.connected_clients = set()
        self.server_process = None  # 存储服务器进程
        
        # 初始化命令处理器
        self.command_handler = None
        self.command_handlers = None
        self._init_command_system()
    
    def _init_command_system(self):
        """初始化命令系统"""
        if self.config_manager:
            self.command_handler = CommandHandler(self.config_manager, self.logger)
            self.command_handlers = CommandHandlers(
                self.msmp_client, 
                self.rcon_client,
                self, 
                self.config_manager, 
                self.logger
            )
            self._register_commands()
    
    def _register_commands(self):
        """注册所有命令"""
        # 基础命令
        self.command_handler.register_command(
            names=['list', '在线列表', '玩家列表', '/list', 'çŽ©å®¶', 'åœ¨çº¿'],
            handler=self.command_handlers.handle_list,
            description='查看在线玩家列表',
            usage='list',
            cooldown=5
        )
        
        self.command_handler.register_command(
            names=['status', '状态', '/status', 'çŠ¶æ€'],
            handler=self.command_handlers.handle_status,
            description='查看服务器状态',
            usage='status',
            cooldown=5
        )
        
        self.command_handler.register_command(
            names=['help', '帮助', '/help', 'å¸®åŠ©'],
            handler=self.command_handlers.handle_help,
            description='显示帮助信息',
            usage='help'
        )
        
        # 管理员命令
        self.command_handler.register_command(
            names=['stop', '停止', '关服', '/stop', 'åœæ­¢', 'å…³æœ'],
            handler=self.command_handlers.handle_stop,
            admin_only=True,
            description='停止Minecraft服务器',
            usage='stop',
            cooldown=10
        )
        
        self.command_handler.register_command(
            names=['start', '启动', '开服', '/start', 'å¯åŠ¨', 'å¼€æœ'],
            handler=self.command_handlers.handle_start,
            admin_only=True,
            description='启动Minecraft服务器',
            usage='start',
            cooldown=10
        )
        
        self.command_handler.register_command(
            names=['reload', '重载', '/reload', 'é‡è½½'],
            handler=self.command_handlers.handle_reload,
            admin_only=True,
            description='重新加载配置文件',
            usage='reload',
            cooldown=30
        )
        
        self.logger.info(f"已注册 {len(self.command_handler.list_commands())} 个命令")
    
    async def start(self):
        """启动WebSocket服务器"""
        self.logger.info(f"启动WebSocket服务器，端口: {self.port}")
        
        if self.access_token:
            self.logger.info("WebSocket鉴权已启用")
        
        self.server = await websockets.serve(
            self._handle_connection,
            "0.0.0.0",
            self.port
        )
        
        self.logger.info("WebSocket服务器启动成功，等待QQ机器人连接...")
    
    async def stop(self):
        """停止WebSocket服务器"""
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.logger.info("WebSocket服务器已停止")
    
    async def _handle_connection(self, websocket, path):
        """处理客户端连接"""
        client_ip = websocket.remote_address[0]
        
        # 检查鉴权令牌
        if self.access_token:
            headers = dict(websocket.request_headers)
            auth_header = headers.get('Authorization', '')
            if auth_header != f"Bearer {self.access_token}":
                self.logger.warning(f"鉴权失败，关闭连接: {client_ip}")
                await websocket.close(1008, "Unauthorized")
                return
        
        self.logger.info(f"QQ机器人已连接: {client_ip}")
        self.current_connection = websocket
        self.connected_clients.add(websocket)
        
        # 发送连接成功响应
        await self._send_meta_event(websocket, "connect")
        
        # 发送连接成功通知到所有群
        try:
            for group_id in self.allowed_groups:
                await self.send_group_message(websocket, group_id, "MSMP_QQBot 连接成功！")
                
            # 检查MSMP连接状态并通知
            if self.config_manager.is_msmp_enabled() and self.msmp_client and self.msmp_client.is_authenticated():
                for group_id in self.allowed_groups:
                    await self.send_group_message(websocket, group_id, "已连接到Minecraft服务器 (MSMP)")
            elif self.config_manager.is_rcon_enabled() and self.rcon_client and self.rcon_client.is_connected():
                for group_id in self.allowed_groups:
                    await self.send_group_message(websocket, group_id, "已连接到Minecraft服务器 (RCON)")
            else:
                for group_id in self.allowed_groups:
                    await self.send_group_message(websocket, group_id, 
                        "Minecraft服务器未连接，管理员可使用 start 命令启动服务器")
        except Exception as e:
            self.logger.error(f"发送连接通知失败: {e}")
        
        try:
            async for message in websocket:
                await self._handle_message(websocket, message)
                
        except websockets.exceptions.ConnectionClosed:
            self.logger.info(f"QQ机器人已断开连接: {client_ip}")
        except Exception as e:
            self.logger.error(f"连接处理异常: {e}", exc_info=True)
        finally:
            self.connected_clients.discard(websocket)
            if self.current_connection == websocket:
                self.current_connection = None
            
            # 发送断开连接元事件
            try:
                await self._send_meta_event(websocket, "disconnect")
            except:
                pass
    
    async def _handle_message(self, websocket, message: str):
        """处理接收到的消息"""
        try:
            if self.logger.isEnabledFor(logging.DEBUG):
                self.logger.debug(f"收到原始消息: {message[:200]}")
            
            data = json.loads(message)
            await self._handle_onebot_message(websocket, data)
            
        except json.JSONDecodeError as e:
            self.logger.error(f"JSON解析失败: {e}")
        except Exception as e:
            self.logger.error(f"处理消息失败: {e}", exc_info=True)
    
    async def _handle_onebot_message(self, websocket, data: Dict[str, Any]):
        """处理OneBot协议消息"""
        # 获取post_type
        if 'post_type' not in data:
            # 检查是否是API响应
            if 'echo' in data:
                if self.logger.isEnabledFor(logging.DEBUG):
                    self.logger.debug(f"收到API响应: {data.get('echo')}")
                return
            # 检查是否是元数据消息
            elif 'meta_event_type' in data:
                await self._handle_meta_event_message(websocket, data)
                return
            # 检查其他可能的消息格式
            elif 'notice_type' in data:
                await self._handle_notice_event(websocket, data)
                return
            elif 'message_type' in data:
                data['post_type'] = 'message'
                await self._handle_message_event(websocket, data)
                return
            else:
                if self.logger.isEnabledFor(logging.DEBUG):
                    self.logger.warning(f"无法识别的消息格式: {data}")
                return
        
        post_type = data.get('post_type')
        
        if post_type == 'message':
            await self._handle_message_event(websocket, data)
        elif post_type == 'meta_event':
            await self._handle_meta_event_message(websocket, data)
        elif post_type == 'request':
            await self._handle_request_event(websocket, data)
        elif post_type == 'notice':
            await self._handle_notice_event(websocket, data)
        else:
            self.logger.warning(f"未知的post_type: {post_type}")
    
    async def _handle_message_event(self, websocket, data: Dict[str, Any]):
        """处理消息事件"""
        message_type = data.get('message_type', '')
        
        # 只处理群消息
        if message_type != 'group':
            return
        
        group_id = data.get('group_id', 0)
        raw_message = data.get('raw_message', '').strip()
        user_id = data.get('user_id', 0)
        
        # 日志记录
        should_log = (self.logger.isEnabledFor(logging.DEBUG) or 
                     (self.config_manager and 
                      self.config_manager.is_log_messages_enabled()))
        
        if should_log:
            self.logger.info(f"收到群消息 - 群号: {group_id}, 用户: {user_id}, 内容: {raw_message}")
        
        # 检查是否是允许的群
        if group_id not in self.allowed_groups:
            return
        
        # 使用命令处理器处理命令
        if self.command_handler:
            try:
                result = await self.command_handler.handle_command(
                    command_text=raw_message,
                    user_id=user_id,
                    group_id=group_id,
                    websocket=websocket,
                    msmp_client=self.msmp_client
                )
                
                if result:
                    await self.send_group_message(websocket, group_id, result)
                    
            except Exception as e:
                self.logger.error(f"命令处理失败: {e}", exc_info=True)
                await self.send_group_message(websocket, group_id, f"命令执行出错: {str(e)}")
    
    async def _handle_meta_event_message(self, websocket, data: Dict[str, Any]):
        """处理元事件"""
        meta_event_type = data.get('meta_event_type', 'unknown')
        
        if meta_event_type == 'heartbeat':
            if self.logger.isEnabledFor(logging.DEBUG):
                self.logger.debug("收到心跳事件")
        elif meta_event_type == 'lifecycle':
            sub_type = data.get('sub_type', 'unknown')
            self.logger.info(f"生命周期事件: {sub_type}")
        else:
            self.logger.info(f"收到元事件: {meta_event_type}")
    
    async def _handle_request_event(self, websocket, data: Dict[str, Any]):
        """处理请求事件"""
        request_type = data.get('request_type', 'unknown')
        self.logger.info(f"收到请求事件: {request_type}")
    
    async def _handle_notice_event(self, websocket, data: Dict[str, Any]):
        """处理通知事件"""
        notice_type = data.get('notice_type', '')
        sub_type = data.get('sub_type', '')
        
        # 处理群成员增加通知
        if notice_type == 'group_increase':
            group_id = data.get('group_id', 0)
            user_id = data.get('user_id', 0)
            
            if (group_id in self.allowed_groups and 
                self.config_manager and 
                self.config_manager.is_welcome_new_members_enabled()):
                
                welcome_msg = self.config_manager.get_welcome_message()
                await self.send_group_message(websocket, group_id, welcome_msg)
                self.logger.info(f"新成员加入群 {group_id}: {user_id}")
    
    async def _send_meta_event(self, websocket, event_type: str):
        """发送元事件"""
        try:
            if websocket.closed:
                return
            
            meta_event = {
                "post_type": "meta_event",
                "meta_event_type": "lifecycle",
                "sub_type": event_type,
                "time": int(time.time())
            }
            
            await websocket.send(json.dumps(meta_event))
        except Exception as e:
            if self.logger.isEnabledFor(logging.DEBUG):
                self.logger.debug(f"发送元事件失败: {e}")
    
    async def send_group_message(self, websocket, group_id: int, message: str):
        """发送群消息"""
        try:
            if not websocket or websocket.closed:
                self.logger.warning("无法发送消息：WebSocket连接已关闭")
                return
            
            # 限制消息长度
            max_length = self.config_manager.get_max_message_length() if self.config_manager else 500
            if len(message) > max_length:
                message = message[:max_length] + "..."
                
            request = {
                "action": "send_group_msg",
                "echo": f"group_msg_{int(time.time() * 1000)}",
                "params": {
                    "group_id": group_id,
                    "message": message,
                    "auto_escape": False
                }
            }
            
            await websocket.send(json.dumps(request))
            
        except Exception as e:
            self.logger.error(f"发送群消息失败: {e}", exc_info=True)
    
    async def broadcast_to_all_groups(self, message: str):
        """广播消息到所有配置的QQ群"""
        if not self.current_connection or self.current_connection.closed:
            self.logger.warning("无法发送群消息：QQ机器人未连接")
            return
        
        for group_id in self.allowed_groups:
            await self.send_group_message(self.current_connection, group_id, message)
    
    def is_connected(self) -> bool:
        """检查是否有活动连接"""
        return self.current_connection is not None and not self.current_connection.closed
    
    async def _start_server_process(self, websocket, group_id: int):
        """启动服务器进程（由命令处理器调用）"""
        try:
            start_script = self.config_manager.get_server_start_script()
            working_dir = self.config_manager.get_server_working_directory()
            if not working_dir:
                working_dir = os.path.dirname(start_script)
            
            self.logger.info(f"启动脚本: {start_script}")
            self.logger.info(f"工作目录: {working_dir}")
            
            # 启动服务器进程
            creationflags = 0
            if os.name == 'nt':  # Windows
                creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
            
            self.server_process = subprocess.Popen(
                start_script,
                cwd=working_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.PIPE,
                text=True,
                encoding='utf-8',
                bufsize=1,
                universal_newlines=True,
                creationflags=creationflags
            )
            
            self.logger.info(f"服务器进程已创建，PID: {self.server_process.pid}")
            
            # 启动后台任务来读取输出
            asyncio.create_task(self._read_server_output())
            
            # 启动后台任务来监控进程状态
            asyncio.create_task(self._monitor_server_process(websocket, group_id))
            
        except Exception as e:
            self.logger.error(f"启动服务器进程失败: {e}", exc_info=True)
            raise

    async def _read_server_output(self):
        """读取服务器输出并在控制台显示"""
        if not self.server_process:
            return
        
        try:
            self.logger.info("开始捕获服务器输出...")
            
            while self.server_process and self.server_process.poll() is None:
                try:
                    line = await asyncio.get_event_loop().run_in_executor(
                        None, 
                        self.server_process.stdout.readline
                    )
                    if line:
                        line = line.strip()
                        if line:
                            print(f"[Minecraft Server] {line}")
                            
                            # 检查服务器启动完成的关键词
                            if self._is_server_ready(line):
                                self.logger.info("检测到服务器启动完成")
                                await self._send_server_started_notification()
                                
                except Exception as e:
                    self.logger.error(f"读取输出行失败: {e}")
                    break
                
                await asyncio.sleep(0.1)
                
        except Exception as e:
            self.logger.error(f"读取服务器输出失败: {e}", exc_info=True)

    def _is_server_ready(self, line: str) -> bool:
        """检查服务器是否启动完成"""
        line_lower = line.lower()
        ready_keywords = ['done', 'server started']
        return any(keyword in line_lower for keyword in ready_keywords)

    async def _send_server_started_notification(self):
        """发送服务器启动成功通知"""
        try:
            if self.current_connection and not self.current_connection.closed:
                for group_id in self.allowed_groups:
                    await self.send_group_message(
                        self.current_connection, 
                        group_id, 
                        "Minecraft服务器启动完成！"
                    )
                self.logger.info("已发送服务器启动完成通知到QQ群")
                
                # 尝试连接MSMP
                await self._reconnect_msmp_after_start()
                
        except Exception as e:
            self.logger.error(f"发送启动通知失败: {e}")

    async def _reconnect_msmp_after_start(self):
        """服务器启动后重新连接MSMP"""
        try:
            self.logger.info("服务器已启动，尝试连接MSMP...")
            
            # 等待一段时间让MSMP服务完全启动
            await asyncio.sleep(15)
            
            if self.msmp_client:
                try:
                    if self.msmp_client.is_authenticated():
                        self.logger.info("MSMP已连接，无需重复连接")
                        return  # 直接返回，不重新连接

                    self.logger.info("正在连接MSMP服务器...")
                    
                    # 重新连接
                    self.msmp_client.connect_sync()
                    await asyncio.sleep(5)
                    
                    if self.msmp_client.is_authenticated():
                        self.logger.info("MSMP服务器连接成功")
                        if self.current_connection and not self.current_connection.closed:
                            for group_id in self.allowed_groups:
                                await self.send_group_message(
                                    self.current_connection, 
                                    group_id, 
                                    "已连接到Minecraft服务器管理协议"
                                )
                    else:
                        self.logger.warning("MSMP服务器连接失败")
                        if self.current_connection and not self.current_connection.closed:
                            for group_id in self.allowed_groups:
                                await self.send_group_message(
                                    self.current_connection, 
                                    group_id, 
                                    "MSMP连接失败，部分功能可能受限"
                                )
                        
                except Exception as e:
                    self.logger.warning(f"MSMP连接失败: {e}")
                    if self.current_connection and not self.current_connection.closed:
                        for group_id in self.allowed_groups:
                            await self.send_group_message(
                                self.current_connection, 
                                group_id, 
                                f"MSMP连接失败: {e}"
                            )
                    
        except Exception as e:
            self.logger.error(f"重新连接MSMP失败: {e}", exc_info=True)

    async def _monitor_server_process(self, websocket, group_id: int):
        """监控服务器进程状态"""
        try:
            self.logger.info("开始监控服务器进程...")
            
            # 使用异步方式等待进程结束
            return_code = await asyncio.get_event_loop().run_in_executor(
                None, 
                self.server_process.wait
            )
            
            self.logger.info(f"服务器进程退出，返回码: {return_code}")
            
            if return_code == 0:
                message = "服务器正常关闭"
            else:
                message = f"服务器异常关闭，返回码: {return_code}"
            
            # 发送关闭通知到QQ群
            if self.current_connection and not self.current_connection.closed:
                for group_id in self.allowed_groups:
                    await self.send_group_message(self.current_connection, group_id, message)
            
            # 清理进程引用
            self.server_process = None
            
        except Exception as e:
            self.logger.error(f"监控服务器进程失败: {e}", exc_info=True)
            self.server_process = None