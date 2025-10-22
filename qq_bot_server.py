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
from collections import deque
from command_handler import CommandHandler, CommandHandlers
from rcon_client import RCONClient
from logging.handlers import RotatingFileHandler
from custom_listener import CustomMessageListener

class QQBotWebSocketServer:
    """
    QQ机器人WebSocket反向连接服务器
    支持OneBot 11协议
    """
    
    def __init__(self, port: int, allowed_groups: List[int], msmp_client, logger: logging.Logger, 
             access_token: str = "", config_manager=None, rcon_client=None):
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
        self.server_stopping = False
        
        # ============ 使用deque替代list，限制日志大小 ============
        max_logs = config_manager.get_max_server_logs() if config_manager else 100
        self.server_logs = deque(maxlen=max_logs)
        self.logger.info(f"初始化服务器日志缓冲区 (最大容量: {max_logs}条)")
        
        # 日志文件相关
        self.server_log_file = None
        self.log_dir = "logs"
        self.log_file_path = os.path.join(self.log_dir, "mc_server.log")
        self.max_log_file_size = 10 * 1024 * 1024  # 10MB
        self.backup_count = 5
        
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
        
        # ============ 注册配置重载回调 ============
        if self.config_manager:
            self.config_manager.register_reload_callback(self._on_config_reload)
            self.logger.info("已注册配置重载回调")
    
    # ============ 配置热重载相关方法 ============
    
    async def _on_config_reload(self, old_config: Dict, new_config: Dict):
        """配置重载时的回调函数
        
        Args:
            old_config: 旧配置字典
            new_config: 新配置字典
        """
        try:
            self.logger.info("=" * 60)
            self.logger.info("开始处理配置重载...")
            
            # 检查QQ群配置是否变化
            old_groups = old_config.get('qq', {}).get('groups', [])
            new_groups = new_config.get('qq', {}).get('groups', [])
            
            if old_groups != new_groups:
                self.allowed_groups = new_groups
                self.logger.info(f"QQ群列表已更新: {new_groups}")
                
                # 通知所有群配置已更新
                if self.current_connection and not self.current_connection.closed:
                    for group_id in self.allowed_groups:
                        try:
                            await self.send_group_message(
                                self.current_connection,
                                group_id,
                                "配置已重新加载，某些功能可能已更新"
                            )
                        except Exception as e:
                            self.logger.debug(f"发送配置更新通知失败: {e}")
            
            # 检查最大日志行数是否变化
            old_max_logs = old_config.get('advanced', {}).get('max_server_logs', 100)
            new_max_logs = new_config.get('advanced', {}).get('max_server_logs', 100)
            
            if old_max_logs != new_max_logs:
                self.logger.info(f"最大日志行数已更新: {old_max_logs} -> {new_max_logs}")
                # 创建新的deque对象
                old_logs = list(self.server_logs)
                self.server_logs = deque(maxlen=new_max_logs)
                # 保留尽可能多的旧日志
                self.server_logs.extend(old_logs)
            
            # 检查命令配置是否变化
            old_cmds = old_config.get('commands', {}).get('enabled_commands', {})
            new_cmds = new_config.get('commands', {}).get('enabled_commands', {})
            
            if old_cmds != new_cmds:
                self.logger.info("命令配置已变化，重新初始化命令系统...")
                self._init_command_system()
                self.logger.info("命令系统已重新初始化")
            
            # 重新加载自定义监听规则
            if self.custom_listener:
                self.custom_listener.reload_rules()
                self.logger.info("自定义监听规则已重新加载")
            
            self.logger.info("配置重载处理完成")
            self.logger.info("=" * 60)
            
        except Exception as e:
            self.logger.error(f"处理配置重载时出错: {e}", exc_info=True)
    
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
            names=['stop', '停止', '关闭', '/stop'],
            handler=self.command_handlers.handle_stop,
            admin_only=True,
            description='停止Minecraft服务器',
            usage='stop',
            cooldown=10
        )
        
        self.command_handler.register_command(
            names=['start', '启动', '开启', '/start'],
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

        self.command_handler.register_command(
            names=['kill', 'force-stop', '强制停止', '/kill'],
            handler=self.command_handlers.handle_kill,
            admin_only=True,
            description='强制中止Minecraft服务器进程(不保存数据)',
            usage='kill',
            cooldown=10
        )

        self.command_handler.register_command(
            names=['crash', 'crash-report', '崩溃报告', '/crash'],
            handler=self.command_handlers.handle_crash,
            admin_only=True,
            description='获取最新的服务器崩溃报告',
            usage='crash',
            cooldown=5
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
    
    # ============ 日志相关方法 ============
    
    def _store_server_log(self, log_line: str):
        """存储服务器日志到内存和文件
        
        Args:
            log_line: 单条日志行
        """
        import datetime
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        formatted_log = f"[{timestamp}] {log_line}"
        
        # 添加到 deque（自动限制大小，旧数据自动删除）
        self.server_logs.append(formatted_log)
        
        # 写入到日志文件
        self._write_to_log_file(formatted_log)
        
        # 处理自定义监听规则（仅当连接活跃时）
        if self.custom_listener and self.current_connection and not self.current_connection.closed:
            try:
                asyncio.create_task(self._process_server_log(log_line))
            except Exception as e:
                self.logger.error(f"创建日志处理任务失败: {e}")
        
        # 检查区块监控消息（仅当连接活跃时）
        if (self.config_manager and 
            self.config_manager.is_chunk_monitor_enabled() and
            self.current_connection and 
            not self.current_connection.closed):
            if self._is_chunk_monitor_message(log_line):
                asyncio.create_task(self._send_chunk_monitor_notification(log_line))
        
        if self.logger.isEnabledFor(logging.DEBUG):
            self.logger.debug(f"存储服务器日志: {log_line[:100]}...")
    
    def get_recent_logs(self, lines: int = 20) -> List[str]:
        """获取最近的服务器日志
        
        Args:
            lines: 获取的日志行数
            
        Returns:
            日志列表
        """
        if not self.server_logs:
            return ["暂无服务器日志"]
        
        # deque 支持切片和迭代，返回最后 lines 条
        return list(self.server_logs)[-lines:]
    
    def get_logs_info(self) -> str:
        """获取日志系统统计信息"""
        current_lines = len(self.server_logs)
        max_lines = self.server_logs.maxlen
        
        return (
            f"日志系统统计\n"
            f"{'=' * 40}\n"
            f"当前日志行数: {current_lines}/{max_lines}\n"
            f"内存占用: 约 {current_lines * 150 / 1024:.2f} KB\n"
            f"使用率: {current_lines / max_lines * 100:.1f}%\n"
            f"{'=' * 40}"
        )
    
    def _write_to_log_file(self, log_line: str):
        """写入日志到文件"""
        if self.server_log_file and not self.server_log_file.closed:
            try:
                self.server_log_file.write(log_line + '\n')
                self.server_log_file.flush()
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

    def _setup_log_file(self):
        """设置日志文件"""
        try:
            if os.path.exists(self.log_file_path):
                file_size = os.path.getsize(self.log_file_path)
                if file_size > self.max_log_file_size:
                    self._rotate_log_file()
            
            self.server_log_file = open(self.log_file_path, 'a', encoding='utf-8', buffering=1)
            self.logger.info(f"服务器日志文件已打开: {self.log_file_path}")
            
        except Exception as e:
            self.logger.error(f"设置日志文件失败: {e}")

    def _rotate_log_file(self):
        """轮转日志文件"""
        try:
            if os.path.exists(self.log_file_path):
                oldest_backup = f"{self.log_file_path}.{self.backup_count}"
                if os.path.exists(oldest_backup):
                    os.remove(oldest_backup)
                
                for i in range(self.backup_count - 1, 0, -1):
                    old_name = f"{self.log_file_path}.{i}"
                    new_name = f"{self.log_file_path}.{i + 1}"
                    if os.path.exists(old_name):
                        os.rename(old_name, new_name)
                
                backup_name = f"{self.log_file_path}.1"
                os.rename(self.log_file_path, backup_name)
                
                self.logger.info(f"已轮转日志文件: {self.log_file_path} -> {backup_name}")
                
        except Exception as e:
            self.logger.error(f"轮转日志文件失败: {e}")
    
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
        
        if self.access_token:
            headers = dict(websocket.request_headers)
            auth_header = headers.get('Authorization', '')
            if auth_header != f"Bearer {self.access_token}":
                self.logger.warning(f"鉴权失败,关闭连接: {client_ip}")
                await websocket.close(1008, "Unauthorized")
                return
        
        self.logger.info(f"QQ机器人已连接: {client_ip}")
        
        try:
            self.current_connection = websocket
            self.connected_clients.add(websocket)
            
            await self._send_meta_event(websocket, "connect")
            
            try:
                for group_id in self.allowed_groups:
                    await self.send_group_message(websocket, group_id, "MSMP_QQBot 已连接成功!")
            except Exception as e:
                self.logger.error(f"发送连接成功通知失败: {e}")
            
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
            
            try:
                await self._send_meta_event(websocket, "disconnect")
            except:
                pass
            
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
        if 'post_type' not in data:
            if 'echo' in data:
                if self.logger.isEnabledFor(logging.DEBUG):
                    self.logger.debug(f"收到API响应: {data.get('echo')}")
                return
            elif 'meta_event_type' in data:
                await self._handle_meta_event_message(websocket, data)
                return
            elif 'notice_type' in data:
                await self._handle_notice_event(websocket, data)
                return
            elif 'message_type' in data:
                data['post_type'] = 'message'
                await self._handle_message_event(websocket, data)
                return
            else:
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
        
        should_log = (self.logger.isEnabledFor(logging.DEBUG) or 
                     (self.config_manager and 
                      self.config_manager.is_log_messages_enabled()))
        
        if message_type == 'group':
            group_id = data.get('group_id', 0)
            
            if should_log:
                self.logger.info(f"收到群消息 - 群号: {group_id}, 用户: {user_id}, 内容: {raw_message}")
            
            if group_id not in self.allowed_groups:
                return
            
            if raw_message.startswith('!'):
                if not self.config_manager.is_admin(user_id):
                    return
                
                server_command = raw_message[1:].strip()
                if not server_command:
                    await self.send_group_message(websocket, group_id, "命令不能为空")
                    return
                
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
        
        elif message_type == 'private':
            if should_log:
                self.logger.info(f"收到私聊消息 - 用户: {user_id}, 内容: {raw_message}")
            
            if not self.config_manager.is_admin(user_id):
                return
            
            if raw_message.startswith('!'):
                server_command = raw_message[1:].strip()
                if not server_command:
                    await self.send_private_message(websocket, user_id, "命令不能为空")
                    return
                
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
                        
                except asyncio.TimeoutError:
                    await self.send_private_message(websocket, user_id, "命令执行超时,请稍后重试")
                    self.logger.warning(f"命令执行超时: {raw_message}")
                except Exception as e:
                    self.logger.error(f"命令处理失败: {e}", exc_info=True)
                    await self.send_private_message(websocket, user_id, f"命令执行出错: {str(e)}")
    
    async def _execute_server_command(self, command: str) -> Optional[str]:
        """执行Minecraft服务器命令并返回结果"""
        try:
            if (self.config_manager.is_rcon_enabled() and 
                self.rcon_client and 
                self.rcon_client.is_connected()):
                
                self.logger.info(f"通过RCON执行命令: {command}")
                
                try:
                    result = self.rcon_client.execute_command(command)
                    
                    if result:
                        cleaned = re.sub(r'§[0-9a-fk-or]', '', result).strip()
                        return cleaned if cleaned else "命令执行成功(无输出)"
                    else:
                        return "命令执行成功(无输出)"
                        
                except Exception as e:
                    self.logger.error(f"RCON执行命令失败: {e}")
                    return f"RCON执行失败: {str(e)}"
            
            elif (self.config_manager.is_msmp_enabled() and 
                  self.msmp_client and 
                  self.msmp_client.is_connected()):
                
                if command.lower().startswith(('allowlist', 'ban', 'op', 'gamerule', 'serversettings')):
                    self.logger.info(f"通过MSMP执行管理命令: {command}")
                    try:
                        result = self.msmp_client.execute_command_sync(command)
                        if result:
                            return str(result)[:500]  # 限制输出长度
                        else:
                            return "命令执行成功(无输出)"
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
    
    async def _process_server_log(self, log_line: str):
        """处理服务器日志中的自定义监听规则"""
        try:
            # 检查连接是否已关闭，如果关闭则跳过处理
            if not self.current_connection or self.current_connection.closed:
                if self.logger.isEnabledFor(logging.DEBUG):
                    self.logger.debug("QQ连接已断开，跳过日志处理")
                return
            
            if self.custom_listener and self.current_connection and not self.current_connection.closed:
                # 检查 RCON 和 MSMP 连接状态
                rcon_connected = self.rcon_client and self.rcon_client.is_connected() if self.rcon_client else False
                msmp_connected = self.msmp_client and self.msmp_client.is_connected() if self.msmp_client else False
                
                # 如果两个连接都断了，不需要处理日志中的服务器操作
                if not rcon_connected and not msmp_connected:
                    if self.logger.isEnabledFor(logging.DEBUG):
                        self.logger.debug("服务器连接已断开，跳过日志处理")
                    return
                
                # 获取实际的玩家数
                player_count = 0
                try:
                    # 优先使用RCON（同步，快速）
                    if rcon_connected:
                        player_info = self.rcon_client.get_player_list()
                        player_count = player_info.current_players
                        if self.logger.isEnabledFor(logging.DEBUG):
                            self.logger.debug(f"通过RCON获取玩家数: {player_count}")
                    # 其次尝试MSMP（异步）
                    elif msmp_connected:
                        try:
                            player_info = await asyncio.wait_for(
                                self.msmp_client.get_player_list(),
                                timeout=2.0
                            )
                            player_count = player_info.current_players
                            if self.logger.isEnabledFor(logging.DEBUG):
                                self.logger.debug(f"通过MSMP获取玩家数: {player_count}")
                        except asyncio.TimeoutError:
                            self.logger.warning("MSMP获取玩家数超时")
                except Exception as e:
                    if self.logger.isEnabledFor(logging.DEBUG):
                        self.logger.debug(f"获取玩家数失败: {e}")
                    player_count = 0
                
                # 构建上下文
                context = {
                    'player_count': player_count,
                    'server_tps': 20.0,
                    'memory_usage': 0.0,
                }
                
                # 处理自定义监听规则
                await self.custom_listener.process_message(
                    log_line=log_line,
                    websocket=self.current_connection,
                    group_ids=self.allowed_groups,
                    server_executor=self._execute_server_command,
                    context=context
                )
        except Exception as e:
            self.logger.error(f"处理自定义监听规则失败: {e}", exc_info=True)
    
    def _is_chunk_monitor_message(self, log_line: str) -> bool:
        """检查是否是区块监控消息"""
        return bool(re.search(r'\[chunkmonitor\].*?\[区块监控\].*?世界', log_line, re.IGNORECASE))
    
    async def _send_chunk_monitor_notification(self, log_line: str):
        """发送区块监控通知到QQ"""
        try:
            if not self.current_connection or self.current_connection.closed:
                self.logger.warning("无法发送区块监控通知:QQ机器人未连接")
                return
            
            cleaned_message = re.sub(r'§[0-9a-fk-or]', '', log_line).strip()
            
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
    
    async def _check_and_clean_file_locks(self):
        """检查并清理可能的文件锁"""
        try:
            working_dir = self.config_manager.get_server_working_directory()
            if not working_dir:
                start_script = self.config_manager.get_server_start_script()
                working_dir = os.path.dirname(start_script)
            
            # 1. 检查并清理 session.lock 文件
            session_lock = os.path.join(working_dir, "session.lock")
            if os.path.exists(session_lock):
                self.logger.warning(f"发现残留的session.lock文件: {session_lock}")
                try:
                    os.remove(session_lock)
                    self.logger.info("已清理session.lock文件")
                except Exception as e:
                    self.logger.warning(f"无法删除session.lock: {e}")
            
            # 2. 检查世界目录中的session.lock
            world_dirs = ["world", "world_nether", "world_the_end"]
            for world_dir in world_dirs:
                world_lock = os.path.join(working_dir, world_dir, "session.lock")
                if os.path.exists(world_lock):
                    self.logger.warning(f"发现世界锁文件: {world_lock}")
                    try:
                        os.remove(world_lock)
                        self.logger.info(f"已清理世界锁文件: {world_lock}")
                    except Exception as e:
                        self.logger.warning(f"无法删除世界锁文件 {world_lock}: {e}")
            
            # 3. 检查并清理 logs/latest.log 文件
            latest_log = os.path.join(working_dir, "logs", "latest.log")
            if os.path.exists(latest_log):
                self.logger.warning(f"发现被占用的latest.log文件: {latest_log}")
                try:
                    # 先尝试重命名而不是直接删除
                    backup_name = os.path.join(working_dir, "logs", f"latest.log.backup.{int(time.time())}")
                    os.rename(latest_log, backup_name)
                    self.logger.info(f"已重命名latest.log为: {backup_name}")
                except Exception as e:
                    self.logger.warning(f"无法处理latest.log: {e}")
                    # 如果重命名失败，尝试等待后删除
                    await asyncio.sleep(1)
                    try:
                        os.remove(latest_log)
                        self.logger.info("已强制删除latest.log文件")
                    except Exception as e2:
                        self.logger.error(f"无法删除latest.log: {e2}")
            
            # 4. 检查端口占用
            await self._check_port_availability()
            
            self.logger.info("文件锁和端口检查完成")
            
        except Exception as e:
            self.logger.warning(f"检查文件锁时出错: {e}")
    
    async def _check_port_availability(self):
        """检查MSMP和RCON端口是否被占用"""
        try:
            import socket
            
            # 检查MSMP端口
            if self.config_manager.is_msmp_enabled():
                msmp_port = self.config_manager.get_msmp_port()
                retry_count = 0
                max_retries = 6  # 等待最多 30 秒
                
                while retry_count < max_retries:
                    if not await self._is_port_in_use('localhost', msmp_port):
                        self.logger.info(f"MSMP端口 {msmp_port} 已释放")
                        break
                    
                    if retry_count == 0:
                        self.logger.warning(f"MSMP端口 {msmp_port} 仍被占用，等待释放...")
                    
                    retry_count += 1
                    await asyncio.sleep(5)
                
                if retry_count >= max_retries:
                    self.logger.warning(f"MSMP端口 {msmp_port} 在 30 秒后仍被占用，强制尝试释放...")
                    await self._kill_process_using_port(msmp_port)
            
            # 检查RCON端口
            if self.config_manager.is_rcon_enabled():
                rcon_port = self.config_manager.get_rcon_port()
                if await self._is_port_in_use('localhost', rcon_port):
                    self.logger.warning(f"RCON端口 {rcon_port} 被占用，尝试释放...")
                    await self._kill_process_using_port(rcon_port)
                    
        except Exception as e:
            self.logger.warning(f"检查端口可用性时出错: {e}")

    async def _is_port_in_use(self, host: str, port: int) -> bool:
        """检查端口是否被占用"""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(1)
                result = s.connect_ex((host, port))
                return result == 0
        except:
            return False

    async def _kill_process_using_port(self, port: int):
        """杀死占用指定端口的进程（Windows平台）"""
        if os.name != 'nt':
            return
        
        try:
            import subprocess
            
            # 使用 netstat 查找占用端口的进程
            result = subprocess.run(
                ['netstat', '-ano', '-p', 'TCP'],
                capture_output=True, 
                text=True, 
                timeout=10
            )
            
            if result.returncode == 0:
                for line in result.stdout.split('\n'):
                    if f':{port}' in line and 'LISTENING' in line:
                        parts = line.split()
                        if len(parts) >= 5:
                            pid = parts[-1]
                            self.logger.warning(f"发现进程 {pid} 占用端口 {port}")
                            
                            # 尝试终止进程
                            try:
                                subprocess.run(['taskkill', '/PID', pid, '/F'], 
                                             capture_output=True, timeout=10)
                                self.logger.info(f"已终止进程 {pid}")
                                await asyncio.sleep(2)  # 等待进程完全终止
                            except Exception as e:
                                self.logger.warning(f"终止进程 {pid} 失败: {e}")
            
        except Exception as e:
            self.logger.warning(f"检查端口占用进程失败: {e}")

    async def _start_server_process(self, websocket, group_id: int = 0, private_user_id: int = None):
        """启动服务器进程 - 支持控制台和QQ调用"""
        try:
            # 在启动前检查并清理可能的文件锁
            await self._check_and_clean_file_locks()
            
            self._setup_log_file()
            
            start_script = self.config_manager.get_server_start_script()
            working_dir = self.config_manager.get_server_working_directory()
            if not working_dir:
                working_dir = os.path.dirname(start_script)
            
            self.logger.info(f"启动脚本: {start_script}")
            self.logger.info(f"工作目录: {working_dir}")
            
            # 检查启动脚本是否存在
            if not os.path.exists(start_script):
                error_msg = f"启动脚本不存在: {start_script}"
                self.logger.error(error_msg)
                if websocket and not websocket.closed:
                    if group_id > 0:
                        await self.send_group_message(websocket, group_id, error_msg)
                    elif private_user_id:
                        await self.send_private_message(websocket, private_user_id, error_msg)
                return
            
            creationflags = 0
            if os.name == 'nt':
                creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
            
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
            
            # 立即检查进程状态
            return_code = self.server_process.poll()
            if return_code is not None:
                self.logger.error(f"服务器进程立即退出，返回码: {return_code}")
                error_msg = f"服务器启动失败，进程立即退出 (返回码: {return_code})"
                if websocket and not websocket.closed:
                    if group_id > 0:
                        await self.send_group_message(websocket, group_id, error_msg)
                    elif private_user_id:
                        await self.send_private_message(websocket, private_user_id, error_msg)
                self.server_process = None
                self._close_log_file()
                return
            
            if websocket and not websocket.closed:
                if group_id > 0:
                    await self.send_group_message(websocket, group_id, "服务器启动命令已执行")
                elif private_user_id:
                    await self.send_private_message(websocket, private_user_id, "服务器启动命令已执行")
            
            # 重置停止标志
            self.server_stopping = False
            
            # 启动日志读取和进程监控
            asyncio.create_task(self._read_server_output())
            asyncio.create_task(self._monitor_server_process(websocket, group_id))
            
        except Exception as e:
            self.logger.error(f"启动服务器进程失败: {e}", exc_info=True)
            
            if websocket and not websocket.closed:
                error_msg = f"启动服务器失败: {e}"
                if group_id > 0:
                    await self.send_group_message(websocket, group_id, error_msg)
                elif private_user_id:
                    await self.send_private_message(websocket, private_user_id, error_msg)
            
            self.server_process = None
            self._close_log_file()
            raise

    def _decode_line(self, line_bytes: bytes) -> str:
        """尝试用多种编码解码一行输出,优先保留中文"""
        if isinstance(line_bytes, str):
            return line_bytes
        
        encodings = ['gbk', 'gb2312', 'utf-8', 'utf-16', 'latin-1']
        
        for encoding in encodings:
            try:
                return line_bytes.decode(encoding)
            except (UnicodeDecodeError, LookupError):
                continue
        
        return line_bytes.decode('utf-8', errors='replace')

    async def _read_server_output(self):
        """读取服务器输出并在控制台显示,同时存储日志"""
        if not self.server_process:
            return
        
        try:
            self.logger.info("开始采集服务器输出...")
            self.logger.info("=" * 60)
            self.logger.info("Minecraft服务器日志 (您仍可在服务器窗口输入命令)")
            self.logger.info("=" * 60)
            
            empty_line_count = 0
            max_empty_lines = 10  # 连续空行的最大次数
            
            while self.server_process and not self.server_stopping:
                # 检查进程是否已结束
                if self.server_process.poll() is not None:
                    self.logger.info("检测到服务器进程已结束，停止日志采集")
                    break

                try:
                    # 使用非阻塞方式读取输出
                    line_bytes = await asyncio.get_event_loop().run_in_executor(
                        None, 
                        lambda: self.server_process.stdout.readline() if self.server_process else b''
                    )
                    
                    if line_bytes:
                        empty_line_count = 0  # 重置空行计数
                        try:
                            line_str = self._decode_line(line_bytes)
                        except Exception as e:
                            self.logger.warning(f"解码失败: {e}")
                            continue
                        
                        line_str = line_str.strip()
                        
                        if line_str:
                            print(f"[MC Server] {line_str}")
                            
                            # 只在未停止时存储日志
                            if not self.server_stopping:
                                self._store_server_log(line_str)
                            
                            if self._is_server_ready(line_str):
                                self.logger.info("检测到服务器启动完成")
                                asyncio.create_task(self._send_server_started_notification())
                    else:
                        # 空行处理
                        empty_line_count += 1
                        if empty_line_count >= max_empty_lines:
                            # 检查进程是否真的结束了
                            if self.server_process.poll() is not None:
                                self.logger.info("进程已结束，停止日志采集")
                                break
                            else:
                                # 进程还在运行，只是没有输出，重置计数继续等待
                                empty_line_count = 0
                        
                        # 没有数据时短暂休眠，避免CPU占用过高
                        await asyncio.sleep(0.1)
                        
                except Exception as e:
                    self.logger.error(f"读取输出行失败: {e}")
                    await asyncio.sleep(0.1)
                    continue
                
            self.logger.info("=" * 60)
            self.logger.info("服务器输出采集已结束")
            self.logger.info("=" * 60)
                    
        except Exception as e:
            self.logger.error(f"读取服务器输出失败: {e}", exc_info=True)
        finally:
            # 只有在真正停止时才关闭日志文件
            if self.server_stopping or (self.server_process and self.server_process.poll() is not None):
                self._close_log_file()

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
                
                for group_id in self.allowed_groups:
                    await self.send_group_message(self.current_connection, group_id, message)
                
                self.logger.info("已发送服务器启动完成通知到QQ群")
                
                await self._reconnect_after_server_start()
                
        except Exception as e:
            self.logger.error(f"发送启动通知失败: {e}")

    async def _reconnect_after_server_start(self):
        """服务器启动后重新连接MSMP和RCON"""
        try:
            self.logger.info("服务器已启动,尝试连接MSMP和RCON...")
            
            await asyncio.sleep(15)
            
            msmp_task = asyncio.create_task(self._reconnect_msmp_after_start())
            rcon_task = asyncio.create_task(self._reconnect_rcon_after_start())
            
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
                        return

                    self.logger.info("正在连接MSMP服务器...")
                    
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
            
            return_code = await asyncio.get_event_loop().run_in_executor(
                None, 
                self.server_process.wait
            )
            
            self.logger.info(f"服务器进程退出,返回码: {return_code}")
            
            # 服务器停止时立即关闭所有连接
            if hasattr(self, 'command_handlers'):
                await self.command_handlers._close_all_connections()
            else:
                self._close_log_file()
            
            if return_code == 0:
                message = "服务器正常关闭"
            else:
                message = f"服务器异常关闭,返回码: {return_code}"
            
            if self.current_connection and not self.current_connection.closed:
                for group_id in self.allowed_groups:
                    await self.send_group_message(self.current_connection, group_id, message)
            
            self.server_process = None
            
        except Exception as e:
            self.logger.error(f"监控服务器进程失败: {e}", exc_info=True)
            self.server_process = None
            self._close_log_file()

    async def _send_crash_report_file(self, websocket, user_id: int, group_id: int, file_path: str, is_private: bool = False):
        """直接发送崩溃报告文件到群或私聊"""
        try:
            if not websocket or websocket.closed:
                self.logger.warning("无法发送文件:WebSocket连接已关闭")
                return
            
            from pathlib import Path
            import json
            import time
            
            file_obj = Path(file_path)
            
            if not file_obj.exists():
                error_msg = f"文件不存在: {file_path}"
                if is_private:
                    await self.send_private_message(websocket, user_id, error_msg)
                else:
                    await self.send_group_message(websocket, group_id, error_msg)
                return
            
            # 获取文件大小
            file_size = file_obj.stat().st_size
            
            self.logger.info(f"正在发送崩溃报告: {file_obj.name} (大小: {file_size / (1024*1024):.2f}MB)")
            
            # 检查文件大小限制
            max_file_size = 50 * 1024 * 1024  # 50MB
            if file_size > max_file_size:
                error_msg = f"崩溃报告文件过大({file_size / (1024*1024):.2f}MB > {max_file_size / (1024*1024):.0f}MB)，请手动查看"
                if is_private:
                    await self.send_private_message(websocket, user_id, error_msg)
                else:
                    await self.send_group_message(websocket, group_id, error_msg)
                return
            
            # 使用 file:// 协议发送本地文件
            file_url = f"file:///{file_obj.absolute()}"  # 转换为绝对路径
            
            # 构建消息
            message_content = [
                {"type": "text", "data": {"text": f"【崩溃报告】{file_obj.name}\n文件大小: {file_size / (1024*1024):.2f}MB\n"}},
                {"type": "file", "data": {"file": file_url}}
            ]
            
            request = {
                "action": "send_msg" if (is_private and not group_id) else ("send_private_msg" if is_private else "send_group_msg"),
                "echo": f"crash_report_{int(time.time() * 1000)}",
                "params": {}
            }
            
            if is_private:
                request["params"]["user_id"] = user_id
            else:
                request["params"]["group_id"] = group_id
            
            request["params"]["message"] = message_content
            
            await websocket.send(json.dumps(request))
            self.logger.info(f"已发送崩溃报告文件: {file_obj.name}")
            
        except Exception as e:
            self.logger.error(f"发送崩溃报告文件失败: {e}", exc_info=True)
            try:
                error_msg = f"发送文件失败: {e}"
                if is_private:
                    await self.send_private_message(websocket, user_id, error_msg)
                else:
                    await self.send_group_message(websocket, group_id, error_msg)
            except:
                pass