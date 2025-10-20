import websockets
import json
import logging
import subprocess
import os
import sys
import re
import asyncio
from typing import List, Dict, Any, Optional
import time
from command_handler import CommandHandler, CommandHandlers
from rcon_client import RCONClient
from logging.handlers import RotatingFileHandler
from custom_listener import CustomMessageListener

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
        self.server_process = None
        
        # 服务器日志存储
        self.server_logs = []
        self.max_log_lines = 100
        
        # 日志文件相关
        self.server_log_file = None
        self.log_dir = "logs"
        self.log_file_path = os.path.join(self.log_dir, "mc_server.log")
        self.max_log_file_size = 10 * 1024 * 1024  # 10MB
        self.backup_count = 5
        
        # 确保日志目录存在
        os.makedirs(self.log_dir, exist_ok=True)

        # 初始化命令系统
        self.command_handler = None
        self.command_handlers = None
        self._init_command_system()

        # 初始化自定义消息监听器
        if self.config_manager:
            try:
                self.custom_listener = CustomMessageListener(self.config_manager, self.logger)
                self.logger.info("自定义消息监听器已初始化")
            except Exception as e:
                self.logger.error(f"初始化自定义消息监听器失败: {e}")
                self.custom_listener = None
        else:
            self.custom_listener = None
    
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
            names=['list', '在线列表', '玩家列表', '/list', '玩家', '在线'],
            handler=self.command_handlers.handle_list,
            description='查看在线玩家列表',
            usage='list',
            cooldown=5,
            command_key='list'
        )
        
        self.command_handler.register_command(
            names=['tps', '/tps', '服务器tps'],
            handler=self.command_handlers.handle_tps,
            description='查看服务器TPS(每秒刻数)性能',
            usage='tps',
            cooldown=5,
            command_key='tps'
        )
        
        self.command_handler.register_command(
            names=['rules', '规则', '/rules', '游戏规则', '服务器规则'],
            handler=self.command_handlers.handle_rules,
            description='查看服务器游戏规则和设置',
            usage='rules',
            cooldown=5,
            command_key='rules'
        )
        
        self.command_handler.register_command(
            names=['status', '状态', '/status'],
            handler=self.command_handlers.handle_status,
            description='查看服务器状态',
            usage='status',
            cooldown=5,
            command_key='status'
        )
        
        self.command_handler.register_command(
            names=['help', '帮助', '/help'],
            handler=self.command_handlers.handle_help,
            description='显示帮助信息',
            usage='help',
            command_key='help'
        )
        
        # 管理员命令
        self.command_handler.register_command(
            names=['stop', '停止', '关服', '/stop'],
            handler=self.command_handlers.handle_stop,
            admin_only=True,
            description='停止Minecraft服务器',
            usage='stop',
            cooldown=10
        )
        
        self.command_handler.register_command(
            names=['start', '启动', '开服', '/start'],
            handler=self.command_handlers.handle_start,
            admin_only=True,
            description='启动Minecraft服务器',
            usage='start',
            cooldown=10
        )
        
        self.command_handler.register_command(
            names=['reload', '重载', '/reload'],
            handler=self.command_handlers.handle_reload,
            admin_only=True,
            description='重新加载配置文件',
            usage='reload',
            cooldown=30
        )

        self.command_handler.register_command(
            names=['log', '日志', '/log', '服务器日志'],
            handler=self.command_handlers.handle_log,
            admin_only=True,
            description='查看最近20条的服务器日志',
            usage='log',
            cooldown=10
        )
        
        # 新增的重连命令
        self.command_handler.register_command(
            names=['reconnect', '重连', '/reconnect'],
            handler=self.command_handlers.handle_reconnect,
            admin_only=True,
            description='重新连接所有服务(MSMP和RCON)',
            usage='reconnect',
            cooldown=10
        )
        
        self.command_handler.register_command(
            names=['reconnect_msmp', '重连msmp', '/reconnect_msmp'],
            handler=self.command_handlers.handle_reconnect_msmp,
            admin_only=True,
            description='重新连接MSMP服务',
            usage='reconnect_msmp',
            cooldown=10
        )
        
        self.command_handler.register_command(
            names=['reconnect_rcon', '重连rcon', '/reconnect_rcon'],
            handler=self.command_handlers.handle_reconnect_rcon,
            admin_only=True,
            description='重新连接RCON服务',
            usage='reconnect_rcon',
            cooldown=10
        )

        # 系统监控命令
        self.command_handler.register_command(
            names=['sysinfo', '系统信息', '/sysinfo', '系统', 'sys'],
            handler=self.command_handlers.handle_sysinfo,
            admin_only=True,
            description='查看服务器系统信息(CPU、内存、硬盘、网络)',
            usage='sysinfo',
            cooldown=5,
            command_key='sysinfo'
        )

        self.command_handler.register_command(
            names=['disk', '硬盘', '/disk', '磁盘', '磁盘使用'],
            handler=self.command_handlers.handle_disk,
            admin_only=True,
            description='查看服务器硬盘详细使用情况',
            usage='disk',
            cooldown=5,
            command_key='disk'
        )

        self.command_handler.register_command(
            names=['process', '进程', '/process', 'proc', 'java'],
            handler=self.command_handlers.handle_process,
            admin_only=True,
            description='查看Java进程运行信息',
            usage='process',
            cooldown=5,
            command_key='process'
        )

        self.command_handler.register_command(
            names=['network', '网络', '/network', 'net', '网络信息'],
            handler=self.command_handlers.handle_network,
            admin_only=True,
            description='查看网络信息和实时带宽速度',
            usage='network',
            cooldown=5,
            command_key='network'
        )

        self.command_handler.register_command(
            names=['listeners', '监听规则', '/listeners', '监听'],
            handler=self.command_handlers.handle_listeners,
            admin_only=True,
            description='查看所有自定义消息监听规则',
            usage='listeners',
            cooldown=5
        )
            
        self.logger.info(f"已注册 {len(self.command_handler.list_commands())} 个命令")
    
    async def start(self):
        """启动WebSocket服务器"""
        self.logger.info(f"启动WebSocket服务器,端口: {self.port}")
        
        if self.access_token:
            self.logger.info("WebSocket鉴权已启用")
        
        self.server = await websockets.serve(
            self._handle_connection,
            "0.0.0.0",
            self.port
        )
        
        self.logger.info("WebSocket服务器启动成功,等待QQ机器人连接...")
    
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
                self.logger.warning(f"鉴权失败,关闭连接: {client_ip}")
                await websocket.close(1008, "Unauthorized")
                return
        
        self.logger.info(f"QQ机器人已连接: {client_ip}")
        
        # 使用 try-finally 确保资源清理
        try:
            self.current_connection = websocket
            self.connected_clients.add(websocket)
            
            # 发送连接成功响应
            await self._send_meta_event(websocket, "connect")
            
            # 发送连接成功通知到所有群
            try:
                for group_id in self.allowed_groups:
                    await self.send_group_message(websocket, group_id, "MSMP_QQBot 连接成功!")
                    
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
                            "Minecraft服务器未连接,管理员可使用 start 命令启动服务器")
            except Exception as e:
                self.logger.error(f"发送连接通知失败: {e}")
            
            # 消息处理循环
            try:
                async for message in websocket:
                    await self._handle_message(websocket, message)
            except websockets.exceptions.ConnectionClosed:
                self.logger.info(f"QQ机器人已断开连接: {client_ip}")
            except Exception as e:
                self.logger.error(f"连接处理异常: {e}", exc_info=True)
                
        finally:
            # 确保资源清理
            self.connected_clients.discard(websocket)
            if self.current_connection == websocket:
                self.current_connection = None
            
            # 发送断开连接元事件
            try:
                await self._send_meta_event(websocket, "disconnect")
            except:
                pass  # 忽略发送失败
            
            self.logger.debug(f"已清理客户端资源: {client_ip}")
    
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
        raw_message = data.get('raw_message', '').strip()
        user_id = data.get('user_id', 0)
        
        # 日志记录
        should_log = (self.logger.isEnabledFor(logging.DEBUG) or 
                     (self.config_manager and 
                      self.config_manager.is_log_messages_enabled()))
        
        # 处理群消息
        if message_type == 'group':
            group_id = data.get('group_id', 0)
            
            if should_log:
                self.logger.info(f"收到群消息 - 群号: {group_id}, 用户: {user_id}, 内容: {raw_message}")
            
            # 检查是否是允许的群
            if group_id not in self.allowed_groups:
                return
            
            # 检查是否是管理员直接执行服务器命令 (以!开头)
            if raw_message.startswith('!'):
                if not self.config_manager.is_admin(user_id):
                    return
                
                server_command = raw_message[1:].strip()
                if not server_command:
                    await self.send_group_message(websocket, group_id, "命令不能为空")
                    return
                
                # 添加超时保护
                try:
                    result = await asyncio.wait_for(
                        self._execute_server_command(server_command),
                        timeout=30.0
                    )
                    
                    if result:
                        await self.send_group_message(websocket, group_id, f"命令执行结果:\n{result}")
                    else:
                        await self.send_group_message(websocket, group_id, "命令已发送,但无返回结果")
                        
                except asyncio.TimeoutError:
                    await self.send_group_message(websocket, group_id, "命令执行超时(30秒),请检查服务器状态")
                    self.logger.warning(f"服务器命令执行超时: {server_command}")
                except Exception as e:
                    await self.send_group_message(websocket, group_id, f"命令执行失败: {str(e)}")
                    self.logger.error(f"执行服务器命令异常: {e}", exc_info=True)
                
                return
            
            # 使用命令处理器处理Bot命令
            if self.command_handler:
                try:
                    result = await asyncio.wait_for(
                        self.command_handler.handle_command(
                            command_text=raw_message,
                            user_id=user_id,
                            group_id=group_id,
                            websocket=websocket,
                            msmp_client=self.msmp_client
                        ),
                        timeout=30.0
                    )
                    
                    if result:
                        await self.send_group_message(websocket, group_id, result)
                        
                except asyncio.TimeoutError:
                    await self.send_group_message(websocket, group_id, "命令执行超时,请稍后重试")
                    self.logger.warning(f"命令执行超时: {raw_message}")
                except Exception as e:
                    self.logger.error(f"命令处理失败: {e}", exc_info=True)
                    await self.send_group_message(websocket, group_id, f"命令执行出错: {str(e)}")
        
        # 处理私聊消息
        elif message_type == 'private':
            if should_log:
                self.logger.info(f"收到私聊消息 - 用户: {user_id}, 内容: {raw_message}")
            
            # 检查是否是管理员
            if not self.config_manager.is_admin(user_id):
                return
            
            # 管理员私聊可以使用所有功能
            
            # 检查是否是直接执行服务器命令 (以!开头)
            if raw_message.startswith('!'):
                server_command = raw_message[1:].strip()
                if not server_command:
                    await self.send_private_message(websocket, user_id, "命令不能为空")
                    return
                
                # 添加超时保护
                try:
                    result = await asyncio.wait_for(
                        self._execute_server_command(server_command),
                        timeout=30.0
                    )
                    
                    if result:
                        await self.send_private_message(websocket, user_id, f"命令执行结果:\n{result}")
                    else:
                        await self.send_private_message(websocket, user_id, "命令已发送,但无返回结果")
                        
                except asyncio.TimeoutError:
                    await self.send_private_message(websocket, user_id, "命令执行超时(30秒),请检查服务器状态")
                    self.logger.warning(f"服务器命令执行超时: {server_command}")
                except Exception as e:
                    await self.send_private_message(websocket, user_id, f"命令执行失败: {str(e)}")
                    self.logger.error(f"执行服务器命令异常: {e}", exc_info=True)
                
                return
            
            # 使用命令处理器处理Bot命令
            if self.command_handler:
                try:
                    result = await asyncio.wait_for(
                        self.command_handler.handle_command(
                            command_text=raw_message,
                            user_id=user_id,
                            group_id=0,
                            websocket=websocket,
                            msmp_client=self.msmp_client,
                            is_private=True
                        ),
                        timeout=30.0
                    )
                    
                    if result is not None:
                        await self.send_private_message(websocket, user_id, result)
                    else:
                        # 对于返回 None 的命令（如 start/stop），不发送"未知命令"提示
                        # 因为这些命令已经在执行过程中发送了状态消息
                        pass
                        
                except asyncio.TimeoutError:
                    await self.send_private_message(websocket, user_id, "命令执行超时,请稍后重试")
                    self.logger.warning(f"命令执行超时: {raw_message}")
                except Exception as e:
                    self.logger.error(f"命令处理失败: {e}", exc_info=True)
                    await self.send_private_message(websocket, user_id, f"命令执行出错: {str(e)}")
    
    async def _execute_server_command(self, command: str) -> Optional[str]:
        """执行Minecraft服务器命令并返回结果"""
        try:
            # 优先使用RCON执行游戏命令
            if (self.config_manager.is_rcon_enabled() and 
                self.rcon_client and 
                self.rcon_client.is_connected()):
                
                self.logger.info(f"通过RCON执行命令: {command}")
                
                try:
                    result = self.rcon_client.execute_command(command)
                    
                    if result:
                        # 清理RCON返回的颜色代码
                        cleaned = re.sub(r'§[0-9a-fk-or]', '', result).strip()
                        return cleaned if cleaned else "命令执行成功(无输出)"
                    else:
                        return "命令执行成功(无输出)"
                        
                except Exception as e:
                    self.logger.error(f"RCON执行命令失败: {e}")
                    return f"RCON执行失败: {str(e)}"
            
            # MSMP只能用于管理操作，不能执行游戏命令
            elif (self.config_manager.is_msmp_enabled() and 
                  self.msmp_client and 
                  self.msmp_client.is_connected()):
                
                # 检查是否是MSMP支持的管理命令
                if command.lower().startswith(('allowlist', 'ban', 'op', 'gamerule', 'serversettings')):
                    self.logger.info(f"通过MSMP执行管理命令: {command}")
                    try:
                        result = self.msmp_client.execute_command_sync(command)
                        # 处理MSMP响应...
                    except Exception as e:
                        self.logger.error(f"MSMP执行管理命令失败: {e}")
                        return f"MSMP执行失败: {str(e)}"
                else:
                    return "MSMP不支持执行游戏命令,请使用RCON"
            
            else:
                return "服务器连接未就绪"
                
        except Exception as e:
            self.logger.error(f"执行服务器命令异常: {e}", exc_info=True)
            return f"命令执行异常: {str(e)}"
    
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
                self.logger.warning("无法发送消息:WebSocket连接已关闭")
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
    
    async def send_private_message(self, websocket, user_id: int, message: str):
        """发送私聊消息"""
        try:
            if not websocket or websocket.closed:
                self.logger.warning("无法发送消息:WebSocket连接已关闭")
                return
            
            # 限制消息长度
            max_length = self.config_manager.get_max_message_length() if self.config_manager else 500
            if len(message) > max_length:
                message = message[:max_length] + "..."
                
            request = {
                "action": "send_private_msg",
                "echo": f"private_msg_{int(time.time() * 1000)}",
                "params": {
                    "user_id": user_id,
                    "message": message,
                    "auto_escape": False
                }
            }
            
            await websocket.send(json.dumps(request))
            
        except Exception as e:
            self.logger.error(f"发送私聊消息失败: {e}", exc_info=True)
    
    async def broadcast_to_all_groups(self, message: str):
        """广播消息到所有配置的QQ群"""
        if not self.current_connection or self.current_connection.closed:
            self.logger.warning("无法发送群消息:QQ机器人未连接")
            return
        
        for group_id in self.allowed_groups:
            await self.send_group_message(self.current_connection, group_id, message)
    
    def is_connected(self) -> bool:
        """检查是否有活动连接"""
        return self.current_connection is not None and not self.current_connection.closed
    
    async def _start_server_process(self, websocket, group_id: int = 0, private_user_id: int = None):
        """启动服务器进程 - 支持控制台和QQ调用"""
        try:
            # 设置日志文件
            self._setup_log_file()
            
            start_script = self.config_manager.get_server_start_script()
            working_dir = self.config_manager.get_server_working_directory()
            if not working_dir:
                working_dir = os.path.dirname(start_script)
            
            self.logger.info(f"启动脚本: {start_script}")
            self.logger.info(f"工作目录: {working_dir}")
            
            # 重定向stdin，让服务器可以接受控制台输入
            creationflags = 0
            if os.name == 'nt':  # Windows
                creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
            
            # 使用二进制模式打开stdout，手动处理编码
            self.server_process = subprocess.Popen(
                start_script,
                cwd=working_dir,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=False,
                bufsize=1,
                creationflags=creationflags
            )
            
            self.logger.info(f"服务器进程已创建,PID: {self.server_process.pid}")
            self.logger.info("现在您可以在控制台直接输入命令到Minecraft服务器")
            
            # 发送启动成功通知（如果有WebSocket连接）
            if websocket and not websocket.closed:
                if group_id > 0:
                    await self.send_group_message(websocket, group_id, "服务器启动命令已执行")
                elif private_user_id:
                    await self.send_private_message(websocket, private_user_id, "服务器启动命令已执行")
            
            # 启动后台任务来读取输出
            asyncio.create_task(self._read_server_output())
            
            # 启动后台任务来监控进程状态
            asyncio.create_task(self._monitor_server_process(websocket, group_id))
            
        except Exception as e:
            self.logger.error(f"启动服务器进程失败: {e}", exc_info=True)
            
            # 发送错误通知（如果有WebSocket连接）
            if websocket and not websocket.closed:
                error_msg = f"启动服务器失败: {e}"
                if group_id > 0:
                    await self.send_group_message(websocket, group_id, error_msg)
                elif private_user_id:
                    await self.send_private_message(websocket, private_user_id, error_msg)
            
            raise

    def _decode_line(self, line_bytes: bytes) -> str:
        """尝试用多种编码解码一行输出，优先保留中文"""
        if isinstance(line_bytes, str):
            return line_bytes
        
        # 按优先级尝试编码 - GBK优先（包含中文），再尝试UTF-8
        encodings = ['gbk', 'gb2312', 'utf-8', 'utf-16', 'latin-1']
        
        for encoding in encodings:
            try:
                return line_bytes.decode(encoding)
            except (UnicodeDecodeError, LookupError):
                continue
        
        # 最后的救命稻草 - 用 replace 模式强制解码
        return line_bytes.decode('utf-8', errors='replace')

    async def _read_server_output(self):
        """读取服务器输出并在控制台显示，同时存储日志"""
        if not self.server_process:
            return
        
        try:
            self.logger.info("开始采集服务器输出...")
            self.logger.info("=" * 60)
            self.logger.info("Minecraft服务器日志 (您仍可在服务器窗口输入命令)")
            self.logger.info("=" * 60)
            
            while self.server_process and self.server_process.poll() is None:
                try:
                    # 使用 readline() 读取一行（二进制）
                    line_bytes = await asyncio.get_event_loop().run_in_executor(
                        None, 
                        self.server_process.stdout.readline
                    )
                    
                    if line_bytes:
                        # 解码字节为字符串
                        try:
                            line_str = self._decode_line(line_bytes)
                        except Exception as e:
                            self.logger.warning(f"解码失败: {e}")
                            continue
                        
                        line_str = line_str.strip()
                        
                        if line_str:
                            # 直接输出到控制台，带前缀
                            print(f"[MC Server] {line_str}")
                            
                            # 存储日志到列表和文件
                            self._store_server_log(line_str)
                            
                            # 检查服务器启动完成的关键词
                            if self._is_server_ready(line_str):
                                self.logger.info("检测到服务器启动完成")
                                asyncio.create_task(self._send_server_started_notification())
                                
                except Exception as e:
                    self.logger.error(f"读取输出行失败: {e}")
                    await asyncio.sleep(0.1)
                    continue
                
                await asyncio.sleep(0.01)
            
            self.logger.info("=" * 60)
            self.logger.info("服务器输出采集已结束")
            self.logger.info("=" * 60)
                    
        except Exception as e:
            self.logger.error(f"读取服务器输出失败: {e}", exc_info=True)
        finally:
            self._close_log_file()

    async def _handle_console_input(self):
        """处理控制台输入并发送到服务器进程"""
        import sys
        loop = asyncio.get_event_loop()
        
        while self.server_process and self.server_process.poll() is None:
            try:
                # 异步读取控制台输入
                line = await loop.run_in_executor(None, sys.stdin.readline)
                if line:
                    line = line.strip()
                    if line and self.server_process:
                        # 编码为字节（UTF-8）并发送
                        try:
                            # 确保编码为字节
                            if isinstance(line, str):
                                command_bytes = (line + '\n').encode('utf-8')
                            else:
                                command_bytes = line + b'\n'
                            
                            self.server_process.stdin.write(command_bytes)
                            self.server_process.stdin.flush()
                            self.logger.debug(f"已发送命令到服务器: {line}")
                        except BrokenPipeError:
                            self.logger.error("服务器进程的stdin管道已断开")
                            break
                        except Exception as e:
                            self.logger.error(f"发送命令到服务器失败: {e}")
                            # 尝试重新连接或处理错误
                            break
            except Exception as e:
                self.logger.error(f"处理控制台输入失败: {e}")
                break
            
            await asyncio.sleep(0.1)

    def _setup_log_file(self):
        """设置日志文件"""
        try:
            # 如果文件太大，先进行轮转
            if os.path.exists(self.log_file_path):
                file_size = os.path.getsize(self.log_file_path)
                if file_size > self.max_log_file_size:
                    self._rotate_log_file()
            
            # 打开日志文件（追加模式）
            self.server_log_file = open(self.log_file_path, 'a', encoding='utf-8', buffering=1)  # 行缓冲
            self.logger.info(f"服务器日志文件已打开: {self.log_file_path}")
            
        except Exception as e:
            self.logger.error(f"设置日志文件失败: {e}")

    def _rotate_log_file(self):
        """轮转日志文件"""
        try:
            if os.path.exists(self.log_file_path):
                # 删除最旧的备份文件
                oldest_backup = f"{self.log_file_path}.{self.backup_count}"
                if os.path.exists(oldest_backup):
                    os.remove(oldest_backup)
                
                # 重命名现有的备份文件
                for i in range(self.backup_count - 1, 0, -1):
                    old_name = f"{self.log_file_path}.{i}"
                    new_name = f"{self.log_file_path}.{i + 1}"
                    if os.path.exists(old_name):
                        os.rename(old_name, new_name)
                
                # 重命名当前日志文件
                backup_name = f"{self.log_file_path}.1"
                os.rename(self.log_file_path, backup_name)
                
                self.logger.info(f"已轮转日志文件: {self.log_file_path} -> {backup_name}")
                
        except Exception as e:
            self.logger.error(f"轮转日志文件失败: {e}")

    def _write_to_log_file(self, log_line: str):
        """写入日志到文件"""
        if self.server_log_file and not self.server_log_file.closed:
            try:
                self.server_log_file.write(log_line + '\n')
                self.server_log_file.flush()  # 确保立即写入
            except Exception as e:
                self.logger.error(f"写入日志文件失败: {e}")

    def _close_log_file(self):
        """关闭日志文件"""
        if self.server_log_file and not self.server_log_file.closed:
            try:
                self.server_log_file.close()
                self.logger.info("服务器日志文件已关闭")
            except Exception as e:
                self.logger.error(f"关闭日志文件失败: {e}")

    async def _process_server_log(self, log_line: str):
        """处理服务器日志中的自定义监听规则"""
        try:
            if self.custom_listener and self.current_connection and not self.current_connection.closed:
                await self.custom_listener.process_message(
                    log_line=log_line,
                    websocket=self.current_connection,
                    group_ids=self.allowed_groups,
                    server_executor=self._execute_server_command
                )
        except Exception as e:
            self.logger.error(f"处理自定义监听规则失败: {e}", exc_info=True)

    def _store_server_log(self, log_line: str):
        """存储服务器日志到内存和文件"""
        import datetime
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        formatted_log = f"[{timestamp}] {log_line}"
        
        # 添加到内存日志列表
        self.server_logs.append(formatted_log)
        
        # 限制内存中的日志数量
        if len(self.server_logs) > self.max_log_lines:
            self.server_logs = self.server_logs[-self.max_log_lines:]
        
        # 写入到日志文件
        self._write_to_log_file(formatted_log)
        
        # 处理自定义监听规则
        if self.custom_listener:
            try:
                asyncio.create_task(self._process_server_log(log_line))
            except Exception as e:
                self.logger.error(f"创建日志处理任务失败: {e}")

        # 检查是否是区块监控消息
        if self.config_manager and self.config_manager.is_chunk_monitor_enabled():
            if self._is_chunk_monitor_message(log_line):
                asyncio.create_task(self._send_chunk_monitor_notification(log_line))
        
        # 调试输出
        if self.logger.isEnabledFor(logging.DEBUG):
            self.logger.debug(f"存储服务器日志: {log_line[:100]}...")

    def get_recent_logs(self, lines: int = 20) -> List[str]:
        """获取最近的服务器日志"""
        if not self.server_logs:
            return ["暂无服务器日志"]
        
        return self.server_logs[-lines:]

    def _is_server_ready(self, line: str) -> bool:
        """检查服务器是否启动完成"""
        line_lower = line.lower()
        ready_keywords = ['done (', 'server started']
        return any(keyword in line_lower for keyword in ready_keywords)

    async def _send_server_started_notification(self):
        """发送服务器启动成功通知"""
        try:
            if self.current_connection and not self.current_connection.closed:
                message = "Minecraft服务器启动完成!"
                
                # 发送到所有群
                for group_id in self.allowed_groups:
                    await self.send_group_message(self.current_connection, group_id, message)
                
                self.logger.info("已发送服务器启动完成通知到QQ群")
                
                # 尝试连接MSMP和RCON
                await self._reconnect_after_server_start()
                
        except Exception as e:
            self.logger.error(f"发送启动通知失败: {e}")

    async def _reconnect_after_server_start(self):
        """服务器启动后重新连接MSMP和RCON"""
        try:
            self.logger.info("服务器已启动,尝试连接MSMP和RCON...")
            
            # 等待一段时间让服务完全启动
            await asyncio.sleep(15)
            
            # 并行尝试连接MSMP和RCON
            msmp_task = asyncio.create_task(self._reconnect_msmp_after_start())
            rcon_task = asyncio.create_task(self._reconnect_rcon_after_start())
            
            # 等待两个任务完成
            await asyncio.gather(msmp_task, rcon_task, return_exceptions=True)
            
            self.logger.info("服务器启动后连接尝试完成")
            
        except Exception as e:
            self.logger.error(f"重新连接服务失败: {e}", exc_info=True)

    async def _reconnect_msmp_after_start(self):
        """服务器启动后重新连接MSMP"""
        try:
            if self.msmp_client:
                try:
                    if self.msmp_client.is_authenticated():
                        self.logger.info("MSMP已连接,无需重复连接")
                        return  # 直接返回,不重新连接

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
                                    "已连接到Minecraft服务器管理协议 (MSMP)"
                                )
                    else:
                        self.logger.warning("MSMP服务器连接失败")
                        if self.current_connection and not self.current_connection.closed:
                            for group_id in self.allowed_groups:
                                await self.send_group_message(
                                    self.current_connection, 
                                    group_id, 
                                    "MSMP连接失败,部分功能可能受限"
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
            self.logger.error(f"重新连接MSMP失败: {e}")

    async def _reconnect_rcon_after_start(self):
        """服务器启动后重新连接RCON"""
        try:
            if self.rcon_client and self.config_manager.is_rcon_enabled():
                try:
                    if self.rcon_client.is_connected():
                        self.logger.info("RCON已连接,无需重复连接")
                        return

                    self.logger.info("正在连接RCON服务器...")
                    
                    # 尝试连接RCON
                    if self.rcon_client.connect():
                        self.logger.info("RCON服务器连接成功")
                        if self.current_connection and not self.current_connection.closed:
                            for group_id in self.allowed_groups:
                                await self.send_group_message(
                                    self.current_connection, 
                                    group_id, 
                                    "已连接到Minecraft服务器远程控制 (RCON)"
                                )
                    else:
                        self.logger.warning("RCON服务器连接失败")
                        if self.current_connection and not self.current_connection.closed:
                            for group_id in self.allowed_groups:
                                await self.send_group_message(
                                    self.current_connection, 
                                    group_id, 
                                    "RCON连接失败,游戏命令执行功能受限"
                                )
                        
                except Exception as e:
                    self.logger.warning(f"RCON连接失败: {e}")
                    if self.current_connection and not self.current_connection.closed:
                        for group_id in self.allowed_groups:
                            await self.send_group_message(
                                self.current_connection, 
                                group_id, 
                                f"RCON连接失败: {e}"
                            )
        except Exception as e:
            self.logger.error(f"重新连接RCON失败: {e}")

    async def _monitor_server_process(self, websocket, group_id: int):
        """监控服务器进程状态"""
        try:
            self.logger.info("开始监控服务器进程...")
            
            # 使用异步方式等待进程结束
            return_code = await asyncio.get_event_loop().run_in_executor(
                None, 
                self.server_process.wait
            )
            
            self.logger.info(f"服务器进程退出,返回码: {return_code}")
            
            # 关闭日志文件
            self._close_log_file()
            
            if return_code == 0:
                message = "服务器正常关闭"
            else:
                message = f"服务器异常关闭,返回码: {return_code}"
            
            # 发送关闭通知到QQ群
            if self.current_connection and not self.current_connection.closed:
                for group_id in self.allowed_groups:
                    await self.send_group_message(self.current_connection, group_id, message)
            
            # 清理进程引用
            self.server_process = None
            
        except Exception as e:
            self.logger.error(f"监控服务器进程失败: {e}", exc_info=True)
            self.server_process = None
            self._close_log_file()

    CHUNK_MONITOR_PATTERN = r'\[chunkmonitor\].*?\[区块监控\].*?世界'

    def _is_chunk_monitor_message(self, log_line: str) -> bool:
        """检查是否是区块监控消息"""
        return bool(re.search(self.CHUNK_MONITOR_PATTERN, log_line, re.IGNORECASE))

    async def _send_chunk_monitor_notification(self, log_line: str):
        """发送区块监控通知到QQ"""
        try:
            if not self.current_connection or self.current_connection.closed:
                self.logger.warning("无法发送区块监控通知:QQ机器人未连接")
                return
            
            # 清理日志行中的颜色代码
            cleaned_message = re.sub(r'§[0-9a-fk-or]', '', log_line).strip()
            
            # 向管理员发送私聊通知
            if self.config_manager.should_notify_admins_on_chunk_monitor():
                for admin_id in self.config_manager.get_qq_admins():
                    try:
                        await self.send_private_message(
                            self.current_connection,
                            admin_id,
                            f"区块监控告警:\n{cleaned_message}"
                        )
                    except Exception as e:
                        self.logger.error(f"发送管理员私聊通知失败: {e}")
            
            # 向QQ群发送通知
            if self.config_manager.should_notify_groups_on_chunk_monitor():
                for group_id in self.allowed_groups:
                    try:
                        await self.send_group_message(
                            self.current_connection,
                            group_id,
                            f"区块监控告警:\n{cleaned_message}"
                        )
                    except Exception as e:
                        self.logger.error(f"发送群通知失败: {e}")
            
            self.logger.info(f"已发送区块监控通知: {log_line[:100]}")
            
        except Exception as e:
            self.logger.error(f"发送区块监控通知异常: {e}", exc_info=True)