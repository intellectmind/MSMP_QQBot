import websockets
import json
import logging
import subprocess
import os
import sys
import re
import psutil
import asyncio
import hashlib
from typing import List, Dict, Any, Optional
import time
from collections import deque
from dataclasses import dataclass
from command_handler import CommandHandler, CommandHandlers, PerCallMSMPClient, PerCallRCONClient
from rcon_client import RCONClient
from msmp_client import MSMPClient
from logging.handlers import RotatingFileHandler
from custom_listener import CustomMessageListener
from custom_command_handler import CustomCommandHandler


@dataclass
class ServerRuntime:
    """单个MC服务器的Bot托管运行时状态。"""
    key: str
    config: Dict[str, Any]
    process: Any = None
    stopping: bool = False
    notify_context: Optional[Dict[str, Any]] = None
    logs: Any = None
    log_file: Any = None
    log_file_path: str = ""
    output_task: Any = None
    monitor_task: Any = None
    log_idle_task: Any = None
    last_log_update_time: Optional[float] = None
    log_idle_kill: bool = False
    manual_kill: bool = False


class QQBotWebSocketServer:
    """
    QQ机器人WebSocket反向连接服务器
    支持OneBot 11协议
    """
    
    def __init__(self, port: int, allowed_groups: List[int], msmp_client, logger: logging.Logger, 
         access_token: str = "", config_manager=None, rcon_client=None, connection_manager=None,
         plugin_manager=None):
        """
        初始化 QQBotWebSocketServer
        
        Args:
            port: WebSocket 监听端口
            allowed_groups: 允许的 QQ 群列表
            msmp_client: MSMP 客户端
            logger: 日志对象
            access_token: WebSocket 访问令牌
            config_manager: 配置管理器
            rcon_client: RCON 客户端
            connection_manager: 连接管理器
            plugin_manager: 插件管理器（新增）
        """
        self.port = port
        self.msmp_client = msmp_client
        self.rcon_client = rcon_client
        self.logger = logger
        self.allowed_groups = self._normalize_group_ids(allowed_groups)
        self.access_token = access_token
        self.config_manager = config_manager
        self.connection_manager = connection_manager
        self.plugin_manager = plugin_manager
        self.logger.info(f"插件管理器已设置: {plugin_manager is not None}")

        self.current_connection = None
        self.loop = None
        self.server = None
        self.connected_clients = set()
        self.server_process = None
        self.active_server_config = None
        self.server_stopping = False
        self._process_notify_context = {}
        self.server_runtimes: Dict[str, ServerRuntime] = {}
        self.active_server_key = ""

        # 日志空闲监控
        self._last_log_update_time = None
        self._log_idle_monitor_task = None
        self._server_output_task = None
        self._server_monitor_task = None
        self._background_tasks = set()
        self._recent_event_notifications = {}
        # 标记是否为日志空闲导致的关闭
        self._log_idle_kill = False
        
        # 标记是否为手动kill
        self._manual_kill = False
        
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
        
        # 初始化自定义指令处理器
        if self.config_manager:
            try:
                self.custom_command_handler = CustomCommandHandler(self.config_manager, self.logger)
                self.logger.info("自定义指令处理器已初始化")
            except Exception as e:
                self.logger.error(f"初始化自定义指令处理器失败: {e}")
                self.custom_command_handler = None

        # 注册配置重新加载回调
        if self.config_manager:
            self.config_manager.register_reload_callback(self._on_config_reload)
            self.logger.info("已注册配置重新加载回调")

    def _websocket_open(self, websocket) -> bool:
        """兼容 websockets legacy/server 两套连接对象。"""
        if not websocket:
            return False

        closed = getattr(websocket, "closed", None)
        if closed is not None:
            return not closed

        state = getattr(websocket, "state", None)
        if state is not None:
            return getattr(state, "name", "") == "OPEN"

        close_code = getattr(websocket, "close_code", None)
        return close_code is None

    def _normalize_group_ids(self, groups: Optional[List[Any]]) -> List[int]:
        normalized = []
        for group_id in groups or []:
            try:
                normalized.append(int(group_id))
            except (TypeError, ValueError):
                self.logger.debug(f"忽略无效QQ群号: {group_id}")
        return sorted(set(normalized))

    def _refresh_allowed_groups(self) -> List[int]:
        """从独立服务器配置刷新群列表，避免旧缓存拦截消息。"""
        if self.config_manager and hasattr(self.config_manager, 'get_qq_groups'):
            self.allowed_groups = self._normalize_group_ids(self.config_manager.get_qq_groups())
        return self.allowed_groups

    def _is_group_allowed(self, group_id: int, context_servers: Optional[List[Dict[str, Any]]] = None) -> bool:
        if context_servers:
            return True
        return int(group_id or 0) in self._refresh_allowed_groups()

    def _server_runtime_key(self, server_config: Optional[Dict[str, Any]]) -> str:
        server_config = server_config or {}
        config_file = str(server_config.get('_config_file') or '').strip()
        if config_file:
            digest = hashlib.sha1(config_file.encode('utf-8')).hexdigest()[:8]
            name = str(server_config.get('name') or os.path.splitext(os.path.basename(config_file))[0])
            safe_name = re.sub(r'[^A-Za-z0-9_.-]+', '_', name).strip('._') or 'server'
            return f"{safe_name}_{digest}"
        key = str(server_config.get('name') or "default")
        key = os.path.splitext(os.path.basename(key))[0]
        return key.strip() or "default"

    def _runtime_display_key(self, runtime: Optional[ServerRuntime]) -> str:
        if not runtime:
            return "unknown"
        name = str((runtime.config or {}).get('name') or '').strip()
        return name or runtime.key

    def _runtime_for_server(self, server_config: Optional[Dict[str, Any]], create: bool = False) -> Optional[ServerRuntime]:
        key = self._server_runtime_key(server_config)
        runtime = self.server_runtimes.get(key)
        if runtime or not create:
            return runtime

        max_logs = self.config_manager.get_max_server_logs(server_config) if self.config_manager else 100
        safe_key = re.sub(r'[^A-Za-z0-9_.-]+', '_', key)
        runtime = ServerRuntime(
            key=key,
            config=dict(server_config or {}),
            notify_context={},
            logs=deque(maxlen=max_logs),
            log_file_path=os.path.join(self.log_dir, f"mc_server_{safe_key}.log")
        )
        self.server_runtimes[key] = runtime
        return runtime

    def _runtime_for_key(self, key: str) -> Optional[ServerRuntime]:
        return self.server_runtimes.get(str(key or "").strip())

    def _resolve_runtime_path(self, value: str) -> str:
        path = str(value or "").strip().replace('\\', os.sep).replace('/', os.sep)
        if not path:
            return ""
        if os.path.isabs(path):
            return path
        return os.path.abspath(path)

    def _server_script_paths(self, server_config: Optional[Dict[str, Any]]) -> tuple[str, str]:
        server_config = server_config or {}
        server_section = server_config.get('server', {}) if isinstance(server_config.get('server'), dict) else {}
        start_script = self._resolve_runtime_path(server_section.get('start_script'))
        configured_workdir = self._resolve_runtime_path(server_section.get('working_directory'))
        working_dir = configured_workdir or (os.path.dirname(start_script) if start_script else "")
        return start_script, working_dir

    def _script_command(self, script: str) -> List[str]:
        suffix = os.path.splitext(str(script or ""))[1].lower()
        if os.name == 'nt' and suffix in {'.bat', '.cmd'}:
            return ['cmd.exe', '/c', script]
        if suffix == '.sh':
            return ['sh', script]
        return [script]

    def _hidden_process_options(self):
        if os.name != 'nt':
            return 0, None
        creationflags = 0
        if hasattr(subprocess, 'CREATE_NEW_PROCESS_GROUP'):
            creationflags |= subprocess.CREATE_NEW_PROCESS_GROUP
        if hasattr(subprocess, 'CREATE_NO_WINDOW'):
            creationflags |= subprocess.CREATE_NO_WINDOW
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        return creationflags, startupinfo

    def _set_active_runtime(self, runtime: Optional[ServerRuntime]):
        """同步旧单实例字段，兼容尚未完全改造的调用点。"""
        if not runtime:
            running = [item for item in self.server_runtimes.values() if item.process and item.process.poll() is None]
            runtime = running[-1] if running else None

        if not runtime:
            self.active_server_key = ""
            self.server_process = None
            self.active_server_config = None
            self.server_stopping = False
            self._process_notify_context = {}
            self.server_logs = deque(maxlen=self.server_logs.maxlen if self.server_logs else 100)
            self.server_log_file = None
            self.log_file_path = os.path.join(self.log_dir, "mc_server.log")
            return

        self.active_server_key = runtime.key
        self.server_process = runtime.process
        self.active_server_config = dict(runtime.config or {})
        self.server_stopping = runtime.stopping
        self._process_notify_context = runtime.notify_context or {}
        self.server_logs = runtime.logs
        self.server_log_file = runtime.log_file
        self.log_file_path = runtime.log_file_path

    def get_server_process(self, server_config: Optional[Dict[str, Any]] = None):
        runtime = self._runtime_for_server(server_config) if server_config else self._runtime_for_key(self.active_server_key)
        return runtime.process if runtime else None

    def is_server_process_running(self, server_config: Optional[Dict[str, Any]] = None) -> bool:
        process = self.get_server_process(server_config)
        return bool(process and process.poll() is None)

    def set_server_stopping(self, server_config: Optional[Dict[str, Any]], stopping: bool):
        runtime = self._runtime_for_server(server_config)
        if runtime:
            runtime.stopping = stopping
            if runtime.key == self.active_server_key:
                self.server_stopping = stopping

    def set_server_manual_kill(self, server_config: Optional[Dict[str, Any]], manual_kill: bool):
        runtime = self._runtime_for_server(server_config)
        if runtime:
            runtime.manual_kill = manual_kill
            if runtime.key == self.active_server_key:
                self._manual_kill = manual_kill

    def get_running_runtime_count(self) -> int:
        return sum(1 for runtime in self.server_runtimes.values() if runtime.process and runtime.process.poll() is None)

    def get_running_runtime_keys(self) -> List[str]:
        return [
            self._runtime_display_key(runtime)
            for runtime in self.server_runtimes.values()
            if runtime.process and runtime.process.poll() is None
        ]

    def get_running_server_configs(self) -> List[Dict[str, Any]]:
        """返回当前由 Bot 托管且仍在运行的服务器配置。"""
        return [
            dict(runtime.config or {})
            for runtime in self.server_runtimes.values()
            if runtime.process and runtime.process.poll() is None
        ]

    async def send_server_stdin(self, command: str, server_config: Optional[Dict[str, Any]] = None) -> str:
        """直接写入 Bot 托管的 MC 服务端 stdin，供 GUI/控制台终端使用。"""
        command = str(command or "").strip()
        if not command:
            return "MC命令不能为空"

        runtime = self._runtime_for_server(server_config) if server_config else self._runtime_for_key(self.active_server_key)
        if not runtime:
            return "目标服务器未由 Bot 托管"

        process = runtime.process
        display_key = self._runtime_display_key(runtime)
        if not process or process.poll() is not None:
            return f"{display_key} 服务器未运行"
        if not process.stdin:
            return f"{display_key} 服务器stdin不可用"

        try:
            await asyncio.to_thread(process.stdin.write, (command + "\n").encode("utf-8"))
            await asyncio.to_thread(process.stdin.flush)
            self.logger.debug(f"已写入 {display_key} MC控制台: {command}")
            return f"[{display_key}] 已发送: {command}"
        except BrokenPipeError:
            return f"{display_key} 服务器stdin管道已断开"
        except Exception as e:
            self.logger.error(f"写入 {display_key} MC控制台失败: {e}", exc_info=True)
            return f"{display_key} 命令发送失败: {e}"

    async def _promote_active_runtime_after_stop(self, stopped_key: str):
        """当前active服务器停止后，切到仍在运行的其它托管服务器。"""
        self._set_active_runtime(None)
        if self.active_server_config:
            try:
                await self._configure_clients_for_server(self.active_server_config)
                if self.connection_manager and hasattr(self.connection_manager, 'reset_shutdown_mode'):
                    await self.connection_manager.reset_shutdown_mode()
                if self.command_handlers:
                    self.command_handlers._is_stopping = False
                    if hasattr(self.command_handlers, '_stopping_servers'):
                        self.command_handlers._stopping_servers.clear()
                    self.command_handlers._shutdown_initiated = False
                    if hasattr(self.command_handlers, '_shutdown_event'):
                        self.command_handlers._shutdown_event.clear()
                self.logger.info(f"已切换当前托管连接到 {self.active_server_key}")
            except Exception as e:
                self.logger.warning(f"切换当前托管连接失败: {e}")
            return

        if self.connection_manager and hasattr(self.connection_manager, 'set_active_server_config'):
            self.connection_manager.set_active_server_config(None)
            if hasattr(self.connection_manager, 'set_clients'):
                await self.connection_manager.set_clients(None, None, self.config_manager)

    def _get_request_headers(self, websocket) -> Dict[str, str]:
        headers = getattr(websocket, "request_headers", None)
        if headers is None:
            request = getattr(websocket, "request", None)
            headers = getattr(request, "headers", None) if request else None
        return dict(headers or {})

    def _track_background_task(self, coro, description: str):
        """跟踪短生命周期后台任务，避免异常丢失和任务集合泄漏。"""
        try:
            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                running_loop = None

            target_loop = self.loop if self.loop and self.loop.is_running() else running_loop
            if running_loop is not None and target_loop is running_loop:
                task = asyncio.create_task(coro)
            elif target_loop and target_loop.is_running():
                future = asyncio.run_coroutine_threadsafe(coro, target_loop)

                def future_cleanup(done_future):
                    try:
                        done_future.result()
                    except Exception as e:
                        self.logger.error(f"{description}失败: {e}", exc_info=True)

                future.add_done_callback(future_cleanup)
                return future
            else:
                raise RuntimeError("没有可用事件循环执行后台任务")

            self._background_tasks.add(task)

            def cleanup(done_task):
                self._background_tasks.discard(done_task)
                try:
                    done_task.result()
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    self.logger.error(f"{description}失败: {e}", exc_info=True)

            task.add_done_callback(cleanup)
            return task
        except Exception:
            coro.close()
            raise

    def on_server_started(self, params: Dict[str, Any]):
        """MSMP服务器启动事件。"""
        self.logger.info("MSMP事件: Minecraft服务器已启动")
        self._dispatch_msmp_event("server_started", params or {}, "Minecraft服务器已启动")

    def on_server_stopping(self, params: Dict[str, Any]):
        """MSMP服务器停止事件。"""
        self.logger.info("MSMP事件: Minecraft服务器正在停止")
        self._dispatch_msmp_event("server_stopping", params or {}, "Minecraft服务器正在停止")

    def on_player_join(self, params: Dict[str, Any]):
        """MSMP玩家加入事件。"""
        player_name = (params or {}).get('name') or (params or {}).get('player') or 'Unknown'
        self.logger.info(f"MSMP事件: 玩家加入 {player_name}")
        self._dispatch_msmp_event("player_join", params or {}, f"{player_name} 加入了游戏")

    def on_player_leave(self, params: Dict[str, Any]):
        """MSMP玩家离开事件。"""
        player_name = (params or {}).get('name') or (params or {}).get('player') or 'Unknown'
        self.logger.info(f"MSMP事件: 玩家离开 {player_name}")
        self._dispatch_msmp_event("player_leave", params or {}, f"{player_name} 离开了游戏")

    def _dispatch_msmp_event(self, event_name: str, params: Dict[str, Any], message: str):
        self._track_background_task(
            self._handle_msmp_event(event_name, params, message),
            f"处理MSMP事件 {event_name}"
        )

    async def _handle_msmp_event(self, event_name: str, params: Dict[str, Any], message: str):
        active_server = self.active_server_config or {}
        target_name = active_server.get('name', '')
        if self.plugin_manager:
            if event_name in ("player_join", "player_leave"):
                player_name = params.get('name') or params.get('player') or 'Unknown'
                await self.plugin_manager.trigger_event(
                    event_name,
                    player_name,
                    target_server=active_server,
                    target_server_name=target_name
                )
            else:
                await self.plugin_manager.trigger_event(
                    event_name,
                    target_server=active_server,
                    target_server_name=target_name
                )

        if not self.config_manager or not self.is_connected():
            return

        if event_name.startswith("server_"):
            enabled = self.config_manager.is_server_event_notify_enabled(active_server)
        else:
            enabled = self.config_manager.is_player_event_notify_enabled(active_server)
        if not enabled:
            return

        dedupe_key = (event_name, self.active_server_key, message)
        now = time.time()
        if now - self._recent_event_notifications.get(dedupe_key, 0) < 3:
            return
        self._recent_event_notifications[dedupe_key] = now
        runtime = self._runtime_for_key(self.active_server_key)
        await self._send_process_notification(message, runtime)
    
    async def _on_config_reload(self, old_config: Dict, new_config: Dict):
        """配置重载时的回调函数
        
        Args:
            old_config: 旧配置字典
            new_config: 新配置字典
        """
        try:
            self.logger.info("=" * 60)
            self.logger.info("开始处理配置重载...")
            
            old_groups = sorted({
                group
                for server in old_config.get('_server_files', []) or []
                for group in ((server.get('qq') or {}).get('groups') or [])
            })
            new_groups = self.config_manager.get_qq_groups() if self.config_manager else []

            if sorted(old_groups) != sorted(new_groups):
                self.allowed_groups = self._normalize_group_ids(new_groups)
                self.logger.info(f"QQ群列表已更新: {new_groups}")
                
                # 通知所有群配置已更新
                if self._websocket_open(self.current_connection):
                    for group_id in self.allowed_groups:
                        try:
                            await self.send_group_message(
                                self.current_connection,
                                group_id,
                                "配置已重新加载，某些功能可能已更新"
                            )
                        except Exception as e:
                            self.logger.debug(f"发送配置更新通知失败: {e}")
            
            # 检查各托管服务器最大日志行数是否变化
            for runtime in self.server_runtimes.values():
                old_max_logs = runtime.logs.maxlen or 100
                new_max_logs = (
                    self.config_manager.get_max_server_logs(runtime.config)
                    if self.config_manager else old_max_logs
                )
                if old_max_logs != new_max_logs:
                    self.logger.info(f"{runtime.key} 最大日志行数已更新: {old_max_logs} -> {new_max_logs}")
                    old_logs = list(runtime.logs)
                    runtime.logs = deque(maxlen=new_max_logs)
                    runtime.logs.extend(old_logs)
                    if runtime.key == self.active_server_key:
                        self.server_logs = runtime.logs
            
            old_server_files = old_config.get('_server_files', []) or []
            new_server_files = new_config.get('_server_files', []) or []
            if old_server_files != new_server_files:
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
            self.command_handler = CommandHandler(self.config_manager, self.logger, self)
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
            names=['mc', 'command', '控制台', '/mc'],
            handler=self.command_handlers.handle_mc,
            admin_only=True,
            description='向Bot托管的Minecraft服务端控制台发送命令',
            usage='mc <服务器编号或名称> <命令>',
            cooldown=1
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

        # 插件管理命令
        self.command_handler.register_command(
            names=['plugins', '插件', '/plugins'],
            handler=self.command_handlers.handle_plugins,
            admin_only=False,
            description='查看已加载的插件及其命令',
            usage='plugins',
            command_key='plugins'
        )

        self.command_handler.register_command(
            names=['reload_plugin', '重载插件', '/reload_plugin'],
            handler=self.command_handlers.handle_reload_plugin,
            admin_only=True,
            description='重新加载指定插件',
            usage='reload_plugin <插件名称>'
        )

        self.command_handler.register_command(
            names=['unload_plugin', '卸载插件', '/unload_plugin'],
            handler=self.command_handlers.handle_unload_plugin,
            admin_only=True,
            description='卸载指定插件',
            usage='unload_plugin <插件名称>'
        )

        self.command_handler.register_command(
            names=['load_plugin', '加载插件', '/load_plugin'],
            handler=self.command_handlers.handle_load_plugin,
            admin_only=True,
            description='加载指定插件',
            usage='load_plugin <插件名称>'
        )
            
        self.logger.info(f"已注册 {len(self.command_handler.list_commands())} 个命令")
    
    # ============ 日志相关方法 ============
    
    def _store_server_log(self, log_line: str, runtime: Optional[ServerRuntime] = None):
        """存储服务器日志到内存和文件
        
        捕获的是 _read_server_output() 读取到的 MC 服务器标准输出日志
        同时用于触发日志空闲重启检测
        
        Args:
            log_line: 单条MC服务器输出日志行
        """
        import datetime
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        formatted_log = f"[{timestamp}] {log_line}"
        
        runtime = runtime or self._runtime_for_key(self.active_server_key)
        logs = runtime.logs if runtime else self.server_logs
        logs.append(formatted_log)
        
        # 更新日志最后更新时间
        if runtime:
            runtime.last_log_update_time = time.time()
            if runtime.key == self.active_server_key:
                self._last_log_update_time = runtime.last_log_update_time
        else:
            self._last_log_update_time = time.time()
        
        # 写入到日志文件
        self._write_to_log_file(formatted_log, runtime)
        
        # 处理自定义监听规则（仅当连接活跃时）
        if self.custom_listener and self._websocket_open(self.current_connection):
            try:
                self._track_background_task(
                    self._process_server_log(log_line, runtime),
                    "处理服务端日志监听"
                )
            except Exception as e:
                self.logger.error(f"创建日志处理任务失败: {e}")
        
        # 检查区块监控消息（仅当连接活跃时）
        if (self.config_manager and 
            self.config_manager.is_chunk_monitor_enabled((runtime.config if runtime else self.active_server_config)) and
            self._websocket_open(self.current_connection)):
            if self._is_chunk_monitor_message(log_line):
                self._track_background_task(
                    self._send_chunk_monitor_notification(
                        log_line,
                        runtime.config if runtime else self.active_server_config
                    ),
                    "发送区块监控通知"
                )
        
        if self.logger.isEnabledFor(logging.DEBUG):
            self.logger.debug(f"存储服务器日志: {log_line[:100]}...")
    
    def get_recent_logs(self, lines: int = 20, server_config: Optional[Dict[str, Any]] = None) -> List[str]:
        """获取最近的服务器日志
        
        Args:
            lines: 获取的日志行数
            
        Returns:
            日志列表
        """
        runtime = self._runtime_for_server(server_config) if server_config else self._runtime_for_key(self.active_server_key)
        if server_config and not runtime:
            return ["该服务器没有Bot托管日志"]
        logs = runtime.logs if runtime else self.server_logs
        if not logs:
            return ["暂无服务器日志"]
        
        # deque 支持切片和迭代，返回最后 lines 条
        return list(logs)[-lines:]
    
    def get_logs_info(self, server_config: Optional[Dict[str, Any]] = None) -> str:
        """获取日志系统统计信息"""
        runtime = self._runtime_for_server(server_config) if server_config else self._runtime_for_key(self.active_server_key)
        if server_config and not runtime:
            server_name = server_config.get('name') or server_config.get('_config_file') or '目标服务器'
            return (
                f"日志系统统计\n"
                f"{'=' * 40}\n"
                f"服务器: {server_name}\n"
                f"状态: 未创建Bot托管日志缓存\n"
                f"{'=' * 40}"
            )
        logs = runtime.logs if runtime else self.server_logs
        current_lines = len(logs)
        max_lines = logs.maxlen or 1
        
        return (
            f"日志系统统计\n"
            f"{'=' * 40}\n"
            f"当前日志行数: {current_lines}/{max_lines}\n"
            f"内存占用: 约 {current_lines * 150 / 1024:.2f} KB\n"
            f"使用率: {current_lines / max_lines * 100:.1f}%\n"
            f"{'=' * 40}"
        )
    
    def _write_to_log_file(self, log_line: str, runtime: Optional[ServerRuntime] = None):
        """写入日志到文件"""
        log_file = runtime.log_file if runtime else self.server_log_file
        if log_file and not log_file.closed:
            try:
                log_file.write(log_line + '\n')
                log_file.flush()
            except Exception as e:
                self.logger.error(f"写入日志文件失败: {e}")

    def _close_log_file(self, runtime: Optional[ServerRuntime] = None):
        """关闭日志文件"""
        log_file = runtime.log_file if runtime else self.server_log_file
        if log_file and not log_file.closed:
            try:
                log_file.close()
                self.logger.info(f"{runtime.key if runtime else '服务器'} 日志文件已关闭")
            except Exception as e:
                self.logger.error(f"关闭日志文件失败: {e}")
        if runtime:
            runtime.log_file = None
            if runtime.key == self.active_server_key:
                self.server_log_file = None

    def _setup_log_file(self, runtime: Optional[ServerRuntime] = None):
        """设置日志文件"""
        try:
            log_file_path = runtime.log_file_path if runtime else self.log_file_path
            if os.path.exists(log_file_path):
                file_size = os.path.getsize(log_file_path)
                if file_size > self.max_log_file_size:
                    self._rotate_log_file(log_file_path)
            
            log_file = open(log_file_path, 'a', encoding='utf-8', buffering=1)
            if runtime:
                runtime.log_file = log_file
                if runtime.key == self.active_server_key:
                    self.server_log_file = log_file
                    self.log_file_path = log_file_path
            else:
                self.server_log_file = log_file
            self.logger.info(f"服务器日志文件已打开: {log_file_path}")
            
        except Exception as e:
            self.logger.error(f"设置日志文件失败: {e}")

    def _rotate_log_file(self, log_file_path: Optional[str] = None):
        """轮转日志文件"""
        try:
            log_file_path = log_file_path or self.log_file_path
            if os.path.exists(log_file_path):
                oldest_backup = f"{log_file_path}.{self.backup_count}"
                if os.path.exists(oldest_backup):
                    os.remove(oldest_backup)
                
                for i in range(self.backup_count - 1, 0, -1):
                    old_name = f"{log_file_path}.{i}"
                    new_name = f"{log_file_path}.{i + 1}"
                    if os.path.exists(old_name):
                        os.rename(old_name, new_name)
                
                backup_name = f"{log_file_path}.1"
                os.rename(log_file_path, backup_name)
                
                self.logger.info(f"已轮转日志文件: {log_file_path} -> {backup_name}")
                
        except Exception as e:
            self.logger.error(f"轮转日志文件失败: {e}")
    
    async def start(self):
        """启动WebSocket服务器"""
        self.logger.info(f"启动WebSocket服务器,端口: {self.port}")
        self.loop = asyncio.get_running_loop()
        
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
        await self._cancel_server_tasks()

        for websocket in list(self.connected_clients):
            try:
                if self._websocket_open(websocket):
                    await websocket.close()
            except Exception as e:
                self.logger.debug(f"关闭QQ客户端连接失败: {e}")
        self.connected_clients.clear()
        self.current_connection = None

        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.logger.info("WebSocket服务器已停止")

    async def _cancel_server_tasks(self, runtime: Optional[ServerRuntime] = None):
        """取消服务端相关后台任务。"""
        if runtime:
            await self._cancel_runtime_tasks(runtime)
            return

        for item in list(self.server_runtimes.values()):
            await self._cancel_runtime_tasks(item)

        tasks = [
            ("server_output", self._server_output_task),
            ("server_monitor", self._server_monitor_task),
            ("log_idle_monitor", self._log_idle_monitor_task)
        ]

        for name, task in tasks:
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    self.logger.debug(f"{name}任务已取消")
                except Exception as e:
                    self.logger.debug(f"取消{name}任务时出错: {e}")

        self._server_output_task = None
        self._server_monitor_task = None
        self._log_idle_monitor_task = None

        background_tasks = list(self._background_tasks)
        for task in background_tasks:
            if task and not task.done():
                task.cancel()

        if background_tasks:
            await asyncio.gather(*background_tasks, return_exceptions=True)
            self._background_tasks.clear()

    async def _cancel_runtime_tasks(self, runtime: ServerRuntime):
        tasks = [
            ("server_output", runtime.output_task),
            ("server_monitor", runtime.monitor_task),
            ("log_idle_monitor", runtime.log_idle_task)
        ]
        for name, task in tasks:
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    self.logger.debug(f"{runtime.key} {name}任务已取消")
                except Exception as e:
                    self.logger.debug(f"取消{runtime.key} {name}任务时出错: {e}")
        runtime.output_task = None
        runtime.monitor_task = None
        runtime.log_idle_task = None
        if runtime.key == self.active_server_key:
            self._server_output_task = None
            self._server_monitor_task = None
            self._log_idle_monitor_task = None
    
    async def _handle_connection(self, websocket, path=None):
        """处理客户端连接"""
        remote_address = getattr(websocket, "remote_address", None)
        client_ip = remote_address[0] if remote_address else "unknown"
        
        if self.access_token:
            headers = self._get_request_headers(websocket)
            auth_header = headers.get('Authorization', '')
            if auth_header != f"Bearer {self.access_token}":
                self.logger.warning(f"鉴权失败,关闭连接: {client_ip}")
                await websocket.close(1008, "Unauthorized")
                return
        
        self.logger.info(f"QQ机器人已连接: {client_ip}")
        
        try:
            self.current_connection = websocket
            self.connected_clients.add(websocket)
            self._refresh_allowed_groups()
            
            await self._send_meta_event(websocket, "connect")
            
            try:
                await self.send_admin_private_notifications("MSMP_QQBot 已连接成功!", websocket)
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
                self.current_connection = self._select_open_websocket()
            
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
            elif self._is_onebot_api_response(data):
                if self.logger.isEnabledFor(logging.DEBUG):
                    self.logger.debug(
                        "收到无echo API响应: retcode=%s message=%s",
                        data.get('retcode'),
                        data.get('message') or data.get('wording')
                    )
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

    @staticmethod
    def _is_onebot_api_response(data: Dict[str, Any]) -> bool:
        """识别 OneBot API 响应包，避免当成事件处理。"""
        return (
            isinstance(data, dict)
            and any(key in data for key in ('status', 'retcode', 'data'))
            and not any(key in data for key in ('message_type', 'notice_type', 'request_type', 'meta_event_type'))
        )
    
    async def _handle_message_event(self, websocket, data: Dict[str, Any]):
        """处理消息事件"""
        message_type = data.get('message_type', '')
        raw_message = data.get('raw_message', '').strip()
        user_id = data.get('user_id', 0)
        context_servers = (
            self.config_manager.resolve_servers_for_context(user_id, data.get('group_id', 0), False)
            if self.config_manager and message_type == 'group' and hasattr(self.config_manager, 'resolve_servers_for_context')
            else []
        )
        context_server = context_servers[0] if context_servers else (
            self.config_manager.resolve_server_for_group(data.get('group_id', 0))
            if self.config_manager and message_type == 'group'
            else None
        )
        
        should_log = (self.logger.isEnabledFor(logging.DEBUG) or 
                     (self.config_manager and 
                      self.config_manager.is_log_messages_enabled(context_server)))
        
        if message_type == 'group':
            group_id = data.get('group_id', 0)
            
            if should_log:
                self.logger.info(f"收到群消息 - 群号: {group_id}, 用户: {user_id}, 内容: {raw_message}")
            
            if not self._is_group_allowed(group_id, context_servers):
                self.logger.debug(f"忽略未授权群消息 - 群号: {group_id}, 用户: {user_id}")
                return
        
            # ① 先检查自定义指令（优先级最高）
            if self.custom_command_handler:
                try:
                    custom_message, custom_targets, error_message = self._resolve_custom_command_targets(
                        raw_message,
                        user_id,
                        group_id,
                        context_servers or ([context_server] if context_server else [])
                    )
                    if error_message:
                        await self.send_group_message(websocket, group_id, error_message)
                        return

                    handled = False
                    for custom_target_server in custom_targets:
                        handled = await self.custom_command_handler.process_group_message(
                            message=custom_message,
                            user_id=user_id,
                            group_id=group_id,
                            websocket=websocket,
                            server_executor=lambda cmd, target=custom_target_server: self._execute_server_command(cmd, target),
                            target_server=custom_target_server
                        )
                        if handled:
                            break
                    
                    if handled:
                        return  # 已处理自定义指令，不继续处理其他命令
                        
                except Exception as e:
                    self.logger.error(f"处理自定义指令失败: {e}", exc_info=True)
            
            # ② 再检查 ! 开头的服务器命令
            if raw_message.startswith('!'):
                raw_server_command = raw_message[1:].strip()
                if not raw_server_command:
                    await self.send_group_message(websocket, group_id, "命令不能为空")
                    return

                server_command, target_server, error_message = self._resolve_direct_server_command(
                    raw_server_command,
                    user_id=user_id,
                    group_id=group_id,
                    is_private=False
                )
                if error_message:
                    await self.send_group_message(websocket, group_id, error_message)
                    return
                
                try:
                    result = await asyncio.wait_for(
                        self._execute_server_command(server_command, target_server),
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
            
            # ③ 最后检查普通命令（help, list, tps等）和插件命令
            if self.command_handler:
                try:
                    parts = raw_message.split(maxsplit=1)
                    base_command = parts[0].lower() if parts else ""
                    command_args = parts[1] if len(parts) > 1 else ""
                    
                    if not base_command:
                        return
                    
                    # 调用命令处理器，让它自动判断是插件命令还是内置命令
                    result = await asyncio.wait_for(
                        self.command_handler.handle_command(
                            command_text=base_command,
                            command_args=command_args,
                            user_id=user_id,
                            group_id=group_id,
                            websocket=websocket,
                            msmp_client=self.msmp_client,
                            config_manager=self.config_manager, 
                            rcon_client=self.rcon_client,
                            plugin_manager=self.plugin_manager,
                            connection_manager=self.connection_manager
                        ),
                        timeout=30.0
                    )
                    
                    if result:
                        await self.send_group_message(websocket, group_id, result)

                except asyncio.TimeoutError:
                    await self.send_group_message(websocket, group_id, "命令执行超时，请稍后重试")
                    self.logger.warning(f"命令执行超时: {raw_message}")
                except Exception as e:  
                    self.logger.error(f"命令处理失败: {e}", exc_info=True)
                    await self.send_group_message(websocket, group_id, f"命令出错: {str(e)}")
        
        elif message_type == 'private':
            if should_log:
                self.logger.info(f"收到私聊消息 - 用户: {user_id}, 内容: {raw_message}")
            
            # ① 处理 ! 开头的服务器命令（仅管理员）
            if raw_message.startswith('!'):
                raw_server_command = raw_message[1:].strip()
                if not raw_server_command:
                    await self.send_private_message(websocket, user_id, "命令不能为空")
                    return

                server_command, target_server, error_message = self._resolve_direct_server_command(
                    raw_server_command,
                    user_id=user_id,
                    group_id=0,
                    is_private=True
                )
                if error_message:
                    await self.send_private_message(websocket, user_id, error_message)
                    return
                
                try:
                    result = await asyncio.wait_for(
                        self._execute_server_command(server_command, target_server),
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
            
            # ② 处理普通命令和插件命令（私聊）
            if self.command_handler:
                try:
                    parts = raw_message.split(maxsplit=1)
                    base_command = parts[0].lower() if parts else ""
                    command_args = parts[1] if len(parts) > 1 else ""
                    
                    if not base_command:
                        return
                    
                    # 调用命令处理器，处理插件命令和内置命令
                    result = await asyncio.wait_for(
                        self.command_handler.handle_command(
                            command_text=base_command,
                            command_args=command_args,
                            user_id=user_id,
                            group_id=0,  # 私聊时群组ID为0
                            websocket=websocket,
                            msmp_client=self.msmp_client,
                            config_manager=self.config_manager,
                            rcon_client=self.rcon_client,
                            plugin_manager=self.plugin_manager,
                            connection_manager=self.connection_manager,
                            is_private=True  # 标记为私聊模式
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
    
    def _resolve_direct_server_command(
        self,
        command: str,
        user_id: int = 0,
        group_id: int = 0,
        is_private: bool = False
    ):
        """解析 ! 直通 MC 命令的目标服务器。

        多服务器场景下使用 `!1 say hi` 或 `!server1 say hi`，单服务器权限上下文可省略。
        """
        command = str(command or "").strip()
        if not self.config_manager:
            return command, self.active_server_config or {}, None

        servers = self.config_manager.get_servers() if hasattr(self.config_manager, 'get_servers') else []
        candidates = (
            self.config_manager.resolve_servers_for_context(user_id, group_id, is_private)
            if hasattr(self.config_manager, 'resolve_servers_for_context')
            else []
        )

        if servers and not candidates:
            return command, {}, "当前群聊/私聊没有权限操作该服务器"

        if not command:
            return command, {}, "命令不能为空"

        first, _, rest = command.partition(" ")
        selected_server = self._resolve_candidate_server(first, candidates)

        if selected_server:
            if not self._server_in_candidates(selected_server, candidates):
                return rest.strip(), {}, "当前群聊/私聊没有权限操作该服务器"
            actual_command = rest.strip()
            if not actual_command:
                return actual_command, selected_server, "请在服务器编号或名称后输入要执行的 MC 命令"
            return actual_command, selected_server, None

        named_command, named_server = self._extract_leading_server_selector(command, candidates)
        if named_server:
            if not named_command:
                return named_command, named_server, "请在服务器编号或名称后输入要执行的 MC 命令"
            return named_command, named_server, None

        if len(candidates) == 1:
            return command, candidates[0], None

        if len(candidates) > 1:
            lines = ["当前可操作多个服务器，请在 ! 后指定服务器编号或名称。", ""]
            for index, server in enumerate(candidates, 1):
                name = server.get('name') or f'server{index}'
                lines.append(f"{index}. {name}: !{index} {command} 或 !{name} {command}")
            return command, {}, "\n".join(lines)

        return command, self.active_server_config or {}, None

    def _resolve_custom_command_targets(
        self,
        message: str,
        user_id: int,
        group_id: int,
        candidates: List[Dict[str, Any]]
    ):
        """解析自定义指令目标服务器，避免同群多服时误执行到第一个服务器。"""
        message = str(message or "").strip()
        candidates = [server for server in (candidates or []) if server]
        if not message or not candidates:
            return message, [], None

        selected_message, selected_server = self._extract_custom_command_server_selector(message, candidates)
        if selected_server:
            return selected_message, [selected_server], None

        if len(candidates) == 1:
            return message, candidates, None

        matched_servers = (
            self.custom_command_handler.find_matching_servers(message, user_id, candidates)
            if self.custom_command_handler and hasattr(self.custom_command_handler, 'find_matching_servers')
            else []
        )
        if len(matched_servers) <= 1:
            return message, matched_servers, None

        return message, [], self._format_custom_command_selection_hint(message, matched_servers, candidates)

    def _extract_custom_command_server_selector(self, message: str, candidates: List[Dict[str, Any]]):
        """支持 `1 指令`、`server1 指令`、`指令 1`、`指令 server1`。"""
        first, _, rest = message.partition(" ")
        if rest:
            server = self._resolve_candidate_server(first, candidates)
            if server:
                return rest.strip(), server

            selected_message, selected_server = self._extract_leading_server_selector(message, candidates)
            if selected_server:
                return selected_message, selected_server

        before_last, separator, last = message.rpartition(" ")
        if separator:
            server = self._resolve_candidate_server(last, candidates)
            if server:
                return before_last.strip(), server

        return message, None

    def _extract_leading_server_selector(self, message: str, candidates: List[Dict[str, Any]]):
        """支持服务器名包含空格时按最长名称前缀匹配。"""
        text = str(message or "").strip()
        text_lower = text.lower()
        for server in sorted(candidates or [], key=lambda item: len(str(item.get('name') or '')), reverse=True):
            name = str(server.get('name') or '').strip()
            if not name:
                continue
            name_lower = name.lower()
            if text_lower == name_lower:
                return "", server
            if text_lower.startswith(name_lower + " "):
                return text[len(name):].strip(), server
        return text, None

    def _resolve_candidate_server(self, selector: str, candidates: List[Dict[str, Any]]):
        selector = str(selector or "").strip()
        if not selector:
            return None

        if selector.isdigit():
            index = int(selector) - 1
            if 0 <= index < len(candidates):
                return candidates[index]

        selector_lower = selector.lower()
        for server in candidates:
            name = str(server.get('name') or '').lower()
            config_file = str(server.get('_config_file') or '').lower()
            if selector_lower in (name, config_file):
                return server
        return None

    def _format_custom_command_selection_hint(
        self,
        message: str,
        servers: List[Dict[str, Any]],
        candidates: List[Dict[str, Any]]
    ) -> str:
        lines = ["当前自定义指令可匹配多个服务器，请指定服务器编号或名称。", ""]
        for server in servers:
            index = self._candidate_server_index(server, candidates)
            name = server.get('name') or f'server{index}'
            lines.append(f"{index}. {name}: {message} {index} 或 {message} {name}")
        return "\n".join(lines)

    def _candidate_server_index(self, server: Dict[str, Any], candidates: List[Dict[str, Any]]) -> int:
        target_file = server.get('_config_file')
        target_name = str(server.get('name') or '').lower()
        for index, candidate in enumerate(candidates, 1):
            if target_file and candidate.get('_config_file') == target_file:
                return index
            if target_name and str(candidate.get('name') or '').lower() == target_name:
                return index
        return 1

    def _server_in_candidates(self, server: Dict[str, Any], candidates: List[Dict[str, Any]]) -> bool:
        target_file = server.get('_config_file')
        target_name = str(server.get('name', '')).lower()
        for candidate in candidates:
            if target_file and candidate.get('_config_file') == target_file:
                return True
            if target_name and str(candidate.get('name', '')).lower() == target_name:
                return True
        return False

    def _same_server(self, left: Optional[Dict[str, Any]], right: Optional[Dict[str, Any]]) -> bool:
        left = left or {}
        right = right or {}
        if not left or not right:
            return False
        left_key = left.get('_config_file') or left.get('name')
        right_key = right.get('_config_file') or right.get('name')
        return bool(left_key and right_key and str(left_key).lower() == str(right_key).lower())

    def _build_per_call_rcon_client(self, server_config: Dict[str, Any]):
        rcon_config = (server_config or {}).get('rcon') or {}
        if not rcon_config.get('enabled', False):
            return None
        port = rcon_config.get('port')
        password = rcon_config.get('password')
        if not port or not password:
            return None
        return PerCallRCONClient(
            rcon_config.get('host') or 'localhost',
            int(port),
            password,
            self.logger
        )

    def _build_per_call_msmp_client(self, server_config: Dict[str, Any]):
        msmp_config = (server_config or {}).get('msmp') or {}
        if not msmp_config.get('enabled', False):
            return None
        port = msmp_config.get('port')
        password = msmp_config.get('password')
        if not port or not password:
            return None
        return PerCallMSMPClient(
            msmp_config.get('host') or 'localhost',
            int(port),
            password,
            self.logger,
            self.config_manager
        )

    async def _execute_server_command(self, command: str, server_config: Optional[Dict[str, Any]] = None) -> Optional[str]:
        """执行Minecraft服务器命令并返回结果"""
        try:
            target_server = server_config or self.active_server_config or {}
            rcon_enabled = bool((target_server.get('rcon') or {}).get('enabled', False))
            msmp_enabled = bool((target_server.get('msmp') or {}).get('enabled', False))
            use_managed_clients = (
                not server_config or
                self._same_server(server_config, self.active_server_config)
            )
            rcon_client = self.rcon_client if use_managed_clients else self._build_per_call_rcon_client(target_server)
            msmp_client = self.msmp_client if use_managed_clients else self._build_per_call_msmp_client(target_server)

            if (rcon_enabled and 
                use_managed_clients and
                self.connection_manager and
                not await self.connection_manager.is_rcon_connected()):
                await self.connection_manager.reconnect_rcon()
                rcon_client = self.rcon_client

            if rcon_enabled and rcon_client:
                
                self.logger.info(f"通过RCON执行命令: {command}")
                
                try:
                    if isinstance(rcon_client, PerCallRCONClient):
                        connected, result = await asyncio.to_thread(
                            rcon_client.run_connected,
                            lambda client: client.execute_command(command)
                        )
                        if not connected:
                            return "服务器连接未就绪"
                    else:
                        if not await asyncio.to_thread(rcon_client.is_connected):
                            return "服务器连接未就绪"
                        result = await asyncio.to_thread(rcon_client.execute_command, command)
                    
                    if result is None:
                        return "RCON执行失败: 未收到服务器响应"
                    if result:
                        cleaned = re.sub(r'[§&][0-9a-fk-orA-FK-OR]', '', result)
                        cleaned = re.sub(r'\x1b\[[0-9;]*m', '', cleaned).strip()
                        return cleaned if cleaned else "命令执行成功(无输出)"
                    else:
                        return "命令执行成功(无输出)"
                        
                except Exception as e:
                    self.logger.error(f"RCON执行命令失败: {e}")
                    return f"RCON执行失败: {str(e)}"
            
            elif msmp_enabled and msmp_client:
                
                if command.lower().startswith(('allowlist', 'ban', 'op', 'gamerule', 'serversettings')):
                    self.logger.info(f"通过MSMP执行管理命令: {command}")
                    try:
                        if isinstance(msmp_client, PerCallMSMPClient):
                            connected, result = await asyncio.to_thread(
                                msmp_client.run_connected,
                                lambda client: client.execute_command_sync(command)
                            )
                            if not connected:
                                return "服务器连接未就绪"
                        else:
                            if not await asyncio.to_thread(msmp_client.is_connected):
                                return "服务器连接未就绪"
                            result = await asyncio.to_thread(msmp_client.execute_command_sync, command)
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
            
            welcome_config = (
                self.config_manager.get_server_welcome_config(group_id)
                if self.config_manager and hasattr(self.config_manager, 'get_server_welcome_config')
                else {'enabled': False, 'message': ''}
            )
            if self._is_group_allowed(group_id) and welcome_config.get('enabled'):
                welcome_msg = self._format_welcome_message(
                    welcome_config.get('message'),
                    user_id,
                    group_id
                )
                await self.send_group_message(websocket, group_id, welcome_msg)
                self.logger.info(f"新成员加入群 {group_id}: {user_id}")

    def _format_welcome_message(self, template: str, user_id: int, group_id: int) -> str:
        """格式化新成员欢迎消息，支持 @ 新成员。"""
        at_code = f"[CQ:at,qq={user_id}]"
        message = str(template or "{at} 欢迎加入！输入 help 查看可用命令")
        return (
            message
            .replace("{at}", at_code)
            .replace("{user_id}", str(user_id))
            .replace("{group_id}", str(group_id))
        )
    
    async def _send_meta_event(self, websocket, event_type: str):
        """记录本地生命周期；反向 OneBot 连接中不向客户端发送 meta_event。"""
        if self.logger.isEnabledFor(logging.DEBUG):
            self.logger.debug(f"本地连接生命周期: {event_type}")
    
    async def send_group_message(self, websocket, group_id: int, message: str):
        """发送群消息"""
        try:
            if not self._websocket_open(websocket):
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
            if not self._websocket_open(websocket):
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
        if not self._websocket_open(self.current_connection):
            self.logger.warning("无法发送群消息:QQ机器人未连接")
            return
        
        for group_id in self._refresh_allowed_groups():
            await self.send_group_message(self.current_connection, group_id, message)

    def _get_admin_user_ids(self) -> List[int]:
        """获取所有服务器配置中的QQ管理员，自动去重。"""
        admin_ids = []
        if self.config_manager and hasattr(self.config_manager, 'get_qq_admins'):
            admin_ids = self.config_manager.get_qq_admins()

        normalized = []
        for admin_id in admin_ids or []:
            try:
                normalized.append(int(admin_id))
            except (TypeError, ValueError):
                self.logger.debug(f"忽略无效QQ管理员: {admin_id}")
        return sorted(set(normalized))

    async def send_admin_private_notifications(self, message: str, websocket=None):
        """仅向QQ管理员私聊发送Bot生命周期通知。"""
        target_websocket = self._get_notification_websocket(websocket)
        if not target_websocket:
            self.logger.warning("无法发送管理员私聊通知:QQ机器人未连接")
            return

        admin_ids = self._get_admin_user_ids()
        if not admin_ids:
            self.logger.warning("无法发送管理员私聊通知:未配置QQ管理员")
            return

        for admin_id in admin_ids:
            await self.send_private_message(target_websocket, admin_id, message)

    async def _send_process_notification(self, message: str, runtime: Optional[ServerRuntime] = None):
        """按启动来源发送服务端进程通知。"""
        context = (runtime.notify_context if runtime else self._process_notify_context) or {}
        websocket = self._get_notification_websocket(context.get("websocket"))
        private_user_id = context.get("private_user_id")
        group_id = context.get("group_id", 0)

        if private_user_id:
            if websocket:
                await self.send_private_message(websocket, private_user_id, message)
            else:
                self.logger.debug("私聊启动通知无可用连接，跳过群广播")
            return

        if group_id > 0:
            if websocket:
                await self.send_group_message(websocket, group_id, message)
            else:
                self.logger.debug("群聊启动通知无可用连接，跳过群广播")
            return

        if self._websocket_open(self.current_connection):
            server_groups = []
            if runtime and isinstance(runtime.config, dict):
                qq_config = runtime.config.get('qq') or {}
                server_groups = qq_config.get('groups') or []
            target_groups = server_groups or self._refresh_allowed_groups()
            for allowed_group_id in target_groups:
                await self.send_group_message(self.current_connection, allowed_group_id, message)

    def _get_notification_websocket(self, preferred_websocket):
        """优先使用触发命令的连接，重连后退到当前连接。"""
        if self._websocket_open(preferred_websocket):
            return preferred_websocket
        if self._websocket_open(self.current_connection):
            return self.current_connection
        self.current_connection = self._select_open_websocket()
        return self.current_connection
    
    def is_connected(self) -> bool:
        """检查是否有活动连接"""
        if self._websocket_open(self.current_connection):
            return True
        self.current_connection = self._select_open_websocket()
        return self.current_connection is not None

    def _select_open_websocket(self):
        """从仍然存活的反向WS连接里选择一个可用连接。"""
        for websocket in list(self.connected_clients):
            if self._websocket_open(websocket):
                return websocket
            self.connected_clients.discard(websocket)
        return None
    
    async def _process_server_log(self, log_line: str, runtime: Optional[ServerRuntime] = None):
        """处理服务器日志中的自定义监听规则"""
        active_server = (runtime.config if runtime else self.active_server_config) or {}
        custom_listeners = active_server.get('custom_listeners') or {}
        if not custom_listeners.get('enabled', False):
            return
        try:
            # 检查连接是否已关闭，如果关闭则跳过处理
            if not self._websocket_open(self.current_connection):
                if self.logger.isEnabledFor(logging.DEBUG):
                    self.logger.debug("QQ连接已断开，跳过日志处理")
                return
            
            if self.custom_listener and self._websocket_open(self.current_connection):
                use_managed_clients = self._same_server(active_server, self.active_server_config or {})
                target_rcon = self.rcon_client if use_managed_clients else self._build_per_call_rcon_client(active_server)
                target_msmp = self.msmp_client if use_managed_clients else self._build_per_call_msmp_client(active_server)
                                
                # 1. 获取实时玩家数量
                player_count = 0
                try:
                    if isinstance(target_rcon, PerCallRCONClient):
                        connected, player_info = await asyncio.to_thread(
                            target_rcon.run_connected,
                            lambda client: client.get_player_list()
                        )
                        if not connected:
                            player_info = None
                    elif target_rcon and await asyncio.to_thread(target_rcon.is_connected):
                        player_info = await asyncio.to_thread(target_rcon.get_player_list)
                    else:
                        player_info = None

                    if player_info:
                        player_count = player_info.current_players
                        if self.logger.isEnabledFor(logging.DEBUG):
                            self.logger.debug(f"通过RCON获取玩家数: {player_count}")
                    elif isinstance(target_msmp, PerCallMSMPClient):
                        connected, player_info = await asyncio.to_thread(
                            target_msmp.run_connected,
                            lambda client: client.get_player_list_sync()
                        )
                        if connected and player_info:
                            player_count = player_info.current_players
                            if self.logger.isEnabledFor(logging.DEBUG):
                                self.logger.debug(f"通过MSMP获取玩家数: {player_count}")
                    elif target_msmp and await asyncio.to_thread(target_msmp.is_connected):
                        try:
                            player_info = await asyncio.wait_for(
                                asyncio.to_thread(target_msmp.get_player_list_sync),
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
                
                # 2. 获取实时的TPS值
                server_tps = 20.0
                try:
                    if target_rcon:
                        commands_config = active_server.get('commands') or {}
                        tps_command = commands_config.get('tps_command') or self.config_manager.get_tps_command()
                        if isinstance(target_rcon, PerCallRCONClient):
                            connected, tps_result = await asyncio.to_thread(
                                target_rcon.run_connected,
                                lambda client: client.execute_command(tps_command)
                            )
                            if not connected:
                                tps_result = None
                        elif await asyncio.to_thread(target_rcon.is_connected):
                            tps_result = await asyncio.to_thread(
                                target_rcon.execute_command,
                                tps_command
                            )
                        else:
                            tps_result = None
                        
                        if tps_result:
                            # 清理颜色代码
                            cleaned_tps = re.sub(r'[§&][0-9a-fk-orA-FK-OR]', '', tps_result).strip()
                            
                            # 使用与handle_tps相同的正则提取逻辑
                            tps_value = self._extract_tps_from_text(cleaned_tps, active_server)
                            if tps_value is not None:
                                server_tps = tps_value
                                if self.logger.isEnabledFor(logging.DEBUG):
                                    self.logger.debug(f"从RCON获取实时TPS: {server_tps}")
                except Exception as e:
                    if self.logger.isEnabledFor(logging.DEBUG):
                        self.logger.debug(f"获取TPS失败: {e}")
                    server_tps = 20.0
                
                # 3. 获取实时内存使用率
                memory_usage = 0.0
                try:
                    memory = psutil.virtual_memory()
                    memory_usage = memory.percent
                    if self.logger.isEnabledFor(logging.DEBUG):
                        self.logger.debug(f"获取内存使用率: {memory_usage}%")
                except Exception as e:
                    if self.logger.isEnabledFor(logging.DEBUG):
                        self.logger.debug(f"获取内存信息失败: {e}")
                    memory_usage = 0.0
                
                # 构建完整的上下文
                context = {
                    'player_count': player_count,
                    'server_tps': server_tps,
                    'memory_usage': memory_usage,
                    'target_server': active_server,
                }
                active_qq = active_server.get('qq') or {}
                target_groups = active_qq.get('groups') or self._refresh_allowed_groups()
                
                # 处理自定义监听规则
                await self.custom_listener.process_message(
                    log_line=log_line,
                    websocket=self.current_connection,
                    group_ids=target_groups,
                    server_executor=self._execute_server_command,
                    context=context
                )
        except Exception as e:
            self.logger.error(f"处理自定义监听规则失败: {e}", exc_info=True)
    
    def _extract_tps_from_text(self, text: str, server_config: Optional[Dict[str, Any]] = None) -> Optional[float]:
        """从文本中提取TPS值 - 复用handle_tps的逻辑
        
        Args:
            text: 已清理颜色代码的文本
            
        Returns:
            TPS值或None
        """
        try:
            commands_config = (server_config or {}).get('commands') or {}
            tps_regex = commands_config.get('tps_regex') or self.config_manager.get_tps_regex()
            tps_group_index = commands_config.get('tps_group_index') or self.config_manager.get_tps_group_index()
            
            if tps_group_index < 1:
                tps_group_index = 1
            
            pattern = re.compile(tps_regex, re.IGNORECASE)
            match = pattern.search(text)
            
            if match:
                try:
                    tps_str = match.group(tps_group_index)
                    tps_value = float(tps_str)
                    
                    if 0 <= tps_value <= 20:
                        return tps_value
                    else:
                        self.logger.debug(f"TPS值超出范围: {tps_value}")
                        return None
                except (IndexError, ValueError) as e:
                    self.logger.debug(f"提取TPS捕获组失败: {e}")
                    return None
            else:
                # 尝试备用正则
                fallback_patterns = [
                    r'(\d+(?:\.\d+)?)',
                    r'TPS[:\s]+(\d+(?:\.\d+)?)',
                    r'(\d+(?:\.\d+)?)\s*(?:tps|TPS)',
                ]
                
                for fallback_regex in fallback_patterns:
                    try:
                        fallback_pattern = re.compile(fallback_regex, re.IGNORECASE)
                        fallback_match = fallback_pattern.search(text)
                        if fallback_match:
                            fallback_tps_str = fallback_match.group(1)
                            fallback_tps_value = float(fallback_tps_str)
                            if 0 <= fallback_tps_value <= 20:
                                self.logger.debug(f"使用备用正则提取TPS: {fallback_tps_value}")
                                return fallback_tps_value
                    except Exception as e:
                        self.logger.debug(f"备用正则失败: {e}")
                        continue
                
                return None
                
        except Exception as e:
            self.logger.error(f"提取TPS值时出错: {e}")
            return None

    def _is_chunk_monitor_message(self, log_line: str) -> bool:
        """检查是否是区块监控消息"""
        return bool(re.search(r'\[chunkmonitor\].*?\[区块监控\].*?世界', log_line, re.IGNORECASE))
    
    async def _send_chunk_monitor_notification(self, log_line: str, server_config: Optional[Dict[str, Any]] = None):
        """发送区块监控通知到QQ"""
        try:
            if not self._websocket_open(self.current_connection):
                self.logger.warning("无法发送区块监控通知:QQ机器人未连接")
                return
            
            cleaned_message = re.sub(r'§[0-9a-fk-or]', '', log_line).strip()
            active_server = server_config or self.active_server_config or {}
            qq_config = active_server.get('qq') or {}
            target_admins = qq_config.get('admins') or self.config_manager.get_qq_admins()
            target_groups = qq_config.get('groups') or self._refresh_allowed_groups()
            
            if self.config_manager.should_notify_admins_on_chunk_monitor(active_server):
                for admin_id in target_admins:
                    try:
                        await self.send_private_message(
                            self.current_connection,
                            admin_id,
                            f"区块监控告警:\n{cleaned_message}"
                        )
                    except Exception as e:
                        self.logger.error(f"发送管理员私聊通知失败: {e}")
            
            if self.config_manager.should_notify_groups_on_chunk_monitor(active_server):
                for group_id in target_groups:
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
    
    async def _check_and_clean_file_locks(self, server_config: Optional[Dict[str, Any]] = None):
        """检查并清理可能的文件锁"""
        try:
            _start_script, working_dir = self._server_script_paths(server_config)
            if not working_dir:
                return
            
            cleaned_files = []
            
            # 1. 检查并清理 session.lock 文件
            session_lock = os.path.join(working_dir, "session.lock")
            if os.path.exists(session_lock):
                # 检查文件是否真的被占用（尝试删除）
                try:
                    os.remove(session_lock)
                    cleaned_files.append("session.lock")
                    self.logger.info("已清理主锁文件")
                except PermissionError:
                    self.logger.warning("主锁文件被占用，无法删除")
                except Exception as e:
                    self.logger.debug(f"清理主锁文件时出错: {e}")
            
            # 2. 检查世界目录中的session.lock
            world_dirs = ["world", "world_nether", "world_the_end"]
            for world_dir in world_dirs:
                world_lock = os.path.join(working_dir, world_dir, "session.lock")
                if os.path.exists(world_lock):
                    try:
                        # 检查文件大小和修改时间，判断是否真的需要清理
                        file_size = os.path.getsize(world_lock)
                        file_mtime = os.path.getmtime(world_lock)
                        current_time = time.time()
                        
                        # 如果文件很小且是最近创建的，可能是残留锁文件
                        if file_size < 100 and (current_time - file_mtime) < 3600:  # 1小时内创建的小文件
                            os.remove(world_lock)
                            cleaned_files.append(f"{world_dir}/session.lock")
                            self.logger.debug(f"已清理世界锁文件: {world_dir}")
                        else:
                            self.logger.debug(f"跳过正常的世界锁文件: {world_dir} (大小: {file_size}字节)")
                            
                    except PermissionError:
                        self.logger.warning(f"世界锁文件被占用: {world_dir}")
                    except Exception as e:
                        self.logger.debug(f"清理世界锁文件 {world_dir} 时出错: {e}")
            
            # 3. 检查并清理 logs/latest.log 文件
            latest_log = os.path.join(working_dir, "logs", "latest.log")
            if os.path.exists(latest_log):
                try:
                    # 检查文件是否被占用
                    with open(latest_log, 'a', encoding='utf-8') as test_file:
                        test_file.write("")  # 尝试写入空内容
                    
                    self.logger.debug("latest.log 文件未被占用，无需处理")
                    
                except (PermissionError, IOError):
                    self.logger.warning("latest.log 文件被占用，尝试重命名...")
                    try:
                        # 先尝试重命名而不是直接删除
                        backup_name = os.path.join(working_dir, "logs", f"latest.log.backup.{int(time.time())}")
                        os.rename(latest_log, backup_name)
                        cleaned_files.append("logs/latest.log")
                        self.logger.info(f"已重命名被占用的日志文件: {backup_name}")
                    except Exception as e:
                        self.logger.warning(f"重命名日志文件失败: {e}")
            
            # 4. 检查端口占用
            await self._check_port_availability(server_config)
            
            if cleaned_files:
                self.logger.info(f"文件锁清理完成: 清理了 {len(cleaned_files)} 个文件")
            else:
                self.logger.debug("无需清理文件锁")
            
        except Exception as e:
            self.logger.warning(f"检查文件锁时出错: {e}")
    
    async def _check_port_availability(self, server_config: Optional[Dict[str, Any]] = None):
        """检查MSMP和RCON端口是否被占用"""
        try:
            import socket
            server_config = server_config or self.active_server_config or {}
            msmp_config = server_config.get('msmp') or {}
            rcon_config = server_config.get('rcon') or {}
            
            # 检查MSMP端口
            if msmp_config.get('enabled', False):
                msmp_port = int(msmp_config.get('port') or 0)
                retry_count = 0
                max_retries = 6  # 等待最多 30 秒
                
                while msmp_port and retry_count < max_retries:
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
            if rcon_config.get('enabled', False):
                rcon_port = int(rcon_config.get('port') or 0)
                if rcon_port and await self._is_port_in_use('localhost', rcon_port):
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

    async def _configure_clients_for_server(self, server_config: Dict[str, Any]):
        """按当前服务器配置重建托管进程使用的 MSMP/RCON 客户端。"""
        server_config = server_config or {}
        msmp_config = server_config.get('msmp') or {}
        rcon_config = server_config.get('rcon') or {}

        if self.msmp_client and (hasattr(self.msmp_client, 'shutdown_sync') or hasattr(self.msmp_client, 'close_sync')):
            try:
                shutdown = getattr(self.msmp_client, 'shutdown_sync', None) or self.msmp_client.close_sync
                await asyncio.to_thread(shutdown)
            except Exception as e:
                self.logger.debug(f"关闭旧MSMP客户端失败: {e}")

        if self.rcon_client and hasattr(self.rcon_client, 'close'):
            try:
                self.rcon_client.close()
            except Exception as e:
                self.logger.debug(f"关闭旧RCON客户端失败: {e}")

        self.msmp_client = None
        self.rcon_client = None

        if rcon_config.get('enabled', False):
            self.rcon_client = RCONClient(
                rcon_config.get('host') or 'localhost',
                int(rcon_config.get('port') or 25575),
                rcon_config.get('password') or '',
                self.logger
            )

        if msmp_config.get('enabled', False):
            self.msmp_client = MSMPClient(
                msmp_config.get('host') or 'localhost',
                int(msmp_config.get('port') or 21111),
                msmp_config.get('password') or '',
                self.logger,
                self.config_manager
            )
            self.msmp_client.set_event_listener(self)
            self.msmp_client.start_background_loop()

        if self.command_handlers:
            self.command_handlers.rcon_client = self.rcon_client

        if self.connection_manager:
            if hasattr(self.connection_manager, 'set_active_server_config'):
                self.connection_manager.set_active_server_config(server_config)
            await self.connection_manager.set_clients(
                self.msmp_client,
                self.rcon_client,
                self.config_manager
            )

        self.logger.info(
            "已按服务器 %s 绑定连接客户端: MSMP=%s, RCON=%s",
            server_config.get('name', '未命名'),
            bool(self.msmp_client),
            bool(self.rcon_client)
        )

    async def _start_server_process(
        self,
        websocket,
        group_id: int = 0,
        private_user_id: Optional[int] = None,
        server_config: Optional[Dict[str, Any]] = None
    ):
        """启动服务器进程 - 支持控制台和QQ调用"""
        self._process_notify_context = {
            "websocket": websocket,
            "group_id": group_id,
            "private_user_id": private_user_id
        }

        try:
            if not server_config and self.config_manager:
                server_config = self.config_manager.resolve_server("")
            server_config = server_config or {}
            start_script, working_dir = self._server_script_paths(server_config)
            if not start_script:
                error_msg = "启动脚本未配置，请在对应 servers/*.yml 的 server.start_script 中配置"
                self.logger.error(error_msg)
                await self._send_process_notification(error_msg, None)
                return False

            if server_config.get('name'):
                self.logger.info(f"目标服务器: {server_config.get('name')}")
            self.logger.info(f"启动脚本: {start_script}")
            self.logger.info(f"工作目录: {working_dir}")

            if not os.path.exists(start_script):
                error_msg = f"启动脚本不存在: {start_script}"
                self.logger.error(error_msg)
                await self._send_process_notification(error_msg, None)
                return False
            if working_dir and not os.path.isdir(working_dir):
                error_msg = f"工作目录不存在: {working_dir}"
                self.logger.error(error_msg)
                await self._send_process_notification(error_msg, None)
                return False

            runtime = self._runtime_for_server(server_config, create=True)
            runtime.config = dict(server_config)
            runtime.notify_context = {
                "websocket": websocket,
                "group_id": group_id,
                "private_user_id": private_user_id
            }
            self._set_active_runtime(runtime)
            await self._configure_clients_for_server(server_config)
            # 在启动前检查并清理可能的文件锁
            await self._check_and_clean_file_locks(server_config)
            
            self._setup_log_file(runtime)
            
            creationflags, startupinfo = self._hidden_process_options()

            await self._cancel_server_tasks(runtime)
            
            runtime.process = subprocess.Popen(
                self._script_command(start_script),
                cwd=working_dir,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=False,
                creationflags=creationflags,
                startupinfo=startupinfo
            )
            self._set_active_runtime(runtime)
            
            self.logger.info(f"{runtime.key} 服务器进程已创建,PID: {runtime.process.pid}")
            
            # 立即检查进程状态
            return_code = runtime.process.poll()
            if return_code is not None:
                self.logger.error(f"服务器进程立即退出，返回码: {return_code}")
                error_msg = f"服务器启动失败，进程立即退出 (返回码: {return_code})"
                await self._send_process_notification(error_msg, runtime)
                runtime.process = None
                self._set_active_runtime(None)
                self._close_log_file(runtime)
                return False
            
            # 重置停止标志
            runtime.stopping = False
            self.server_stopping = False
            
            # 启动日志读取和进程监控
            runtime.output_task = asyncio.create_task(self._read_server_output(runtime))
            runtime.monitor_task = asyncio.create_task(self._monitor_server_process(websocket, group_id, runtime))
            self._server_output_task = runtime.output_task
            self._server_monitor_task = runtime.monitor_task

            # 重置所有标记
            runtime.manual_kill = False
            runtime.log_idle_kill = False
            runtime.last_log_update_time = time.time()
            self._manual_kill = False
            self._log_idle_kill = False
            self._last_log_update_time = runtime.last_log_update_time
            
            # 启动日志空闲监控
            if self.config_manager and self.config_manager.get_log_idle_restart_timeout(runtime.config) > 0:
                runtime.log_idle_task = asyncio.create_task(self._monitor_log_idle(runtime))
                self._log_idle_monitor_task = runtime.log_idle_task
                self.logger.info(f"{runtime.key} 日志空闲监控已启动")

            return True
            
        except Exception as e:
            self.logger.error(f"启动服务器进程失败: {e}", exc_info=True)
            
            error_msg = f"启动服务器失败: {e}"
            runtime = locals().get('runtime')
            await self._send_process_notification(error_msg, runtime)
            
            if runtime:
                runtime.process = None
                self._close_log_file(runtime)
            self._set_active_runtime(None)
            raise

    async def _monitor_log_idle(self, runtime: Optional[ServerRuntime] = None):
        """监控日志空闲时间 - 如果日志长时间未更新则自动重启服务器
        
        监控的是 _store_server_log() 方法更新的时间戳
        当 MC 服务器假死、卡顿导致无日志输出时，自动触发重启
        """
        runtime = runtime or self._runtime_for_key(self.active_server_key)
        if not runtime:
            return
        timeout = self.config_manager.get_log_idle_restart_timeout(runtime.config)
        
        if timeout <= 0:
            self.logger.debug("日志空闲监控已禁用")
            return
        
        self.logger.info(f"日志空闲监控已启动,超时时间: {timeout}秒")
        self.logger.info("监控的是 MC 服务器标准输出日志,如果日志超过设定时间未更新则自动重启")
        
        try:
            while runtime.process and runtime.process.poll() is None:
                # 检查是否有日志更新记录
                if runtime.last_log_update_time is None:
                    # 第一次运行,初始化时间
                    runtime.last_log_update_time = time.time()
                    await asyncio.sleep(10)  # 先等待一段时间让日志开始输出
                    continue
                
                # 计算距离上次日志更新的时间
                time_since_last_log = time.time() - runtime.last_log_update_time
                
                if time_since_last_log > timeout:
                    self.logger.warning(
                        f"检测到 MC 服务器日志已{time_since_last_log:.0f}秒未更新(超时: {timeout}秒),准备自动重启..."
                    )
                    
                    # 设置标记，防止异常停止重启再次触发
                    runtime.log_idle_kill = True
                    if runtime.key == self.active_server_key:
                        self._log_idle_kill = True
                    
                    # 发送通知消息（这里发送一次，_monitor_server_process 中就不要再发送了）
                    msg = f"检测到服务器日志已停止更新({int(time_since_last_log)}秒),正在自动重启..."
                    await self._send_process_notification(msg, runtime)
                    
                    # 杀死进程
                    try:
                        if runtime.process and runtime.process.poll() is None:
                            import signal
                            pid = runtime.process.pid
                            self.logger.info(f"{runtime.key} 因日志空闲,强制终止进程 {pid}")
                            
                            if os.name == 'nt':
                                import subprocess
                                try:
                                    subprocess.run(
                                        ['taskkill', '/F', '/T', '/PID', str(pid)],
                                        timeout=10,
                                        capture_output=True
                                    )
                                except:
                                    os.kill(pid, signal.SIGTERM)
                            else:
                                os.killpg(os.getpgid(pid), signal.SIGKILL)
                    
                    except Exception as e:
                        self.logger.error(f"强制终止进程失败: {e}")
                    
                    # 退出监控,由 _monitor_server_process 处理重启
                    return
                
                # 每5秒检查一次
                await asyncio.sleep(5)
        
        except asyncio.CancelledError:
            self.logger.debug("日志空闲监控被取消")
        except Exception as e:
            self.logger.error(f"日志空闲监控异常: {e}", exc_info=True)

    def _decode_line(self, line_bytes: bytes) -> str:
        """尝试用多种编码解码一行输出,优先保留中文"""
        if isinstance(line_bytes, str):
            return line_bytes
        
        encodings = ['utf-8', 'gbk', 'gb2312', 'utf-16', 'latin-1']
        
        for encoding in encodings:
            try:
                return line_bytes.decode(encoding)
            except (UnicodeDecodeError, LookupError):
                continue
        
        return line_bytes.decode('utf-8', errors='replace')

    async def _read_server_output(self, runtime: Optional[ServerRuntime] = None):
        """读取服务器输出并在控制台显示,同时存储日志"""
        runtime = runtime or self._runtime_for_key(self.active_server_key)
        if not runtime or not runtime.process:
            return
        
        try:
            self.logger.info(f"开始采集 {runtime.key} 服务器输出...")
            self.logger.info("=" * 60)
            self.logger.info(f"Minecraft服务器日志 [{runtime.key}]")
            self.logger.info("=" * 60)
            
            empty_line_count = 0
            max_empty_lines = 10  # 连续空行的最大次数
            
            while runtime.process:
                # 检查进程是否已结束
                if runtime.process.poll() is not None:
                    self.logger.info(f"检测到 {runtime.key} 服务器进程已结束，继续采集剩余输出...")
                    # 进程结束后继续读取剩余输出
                    break

                try:
                    # 使用非阻塞方式读取输出
                    line_bytes = await asyncio.get_event_loop().run_in_executor(
                        None, 
                        lambda: runtime.process.stdout.readline() if runtime.process else b''
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
                            print(f"[MC Server][{self._runtime_display_key(runtime)}] {line_str}", flush=True)
                            
                            # 始终存储日志，即使正在停止
                            self._store_server_log(line_str, runtime)
                            
                            if self._is_server_ready(line_str):
                                self.logger.info("检测到服务器启动完成")
                                self._track_background_task(
                                    self._send_server_started_notification(runtime),
                                    "发送服务器启动完成通知"
                                )
                                
                            # 检查服务器关闭相关的日志
                            if self._is_server_stopping(line_str):
                                self.logger.info("检测到服务器正在关闭")
                    else:
                        # 空行处理
                        empty_line_count += 1
                        if empty_line_count >= max_empty_lines:
                            # 检查进程是否真的结束了
                            if runtime.process.poll() is not None:
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
            
            # 进程结束后，继续读取所有剩余输出
            remaining_output_read = False
            
            while True:
                try:
                    line_bytes = await asyncio.get_event_loop().run_in_executor(
                        None, 
                        lambda: runtime.process.stdout.readline() if runtime.process else b''
                    )
                    
                    if not line_bytes:
                        break
                        
                    try:
                        line_str = self._decode_line(line_bytes).strip()
                        if line_str:
                            print(f"[MC Server][{self._runtime_display_key(runtime)}] {line_str}", flush=True)
                            self._store_server_log(line_str, runtime)
                            remaining_output_read = True
                    except Exception as e:
                        self.logger.debug(f"解码剩余输出失败: {e}")
                        
                except Exception as e:
                    self.logger.debug(f"读取剩余输出时出错: {e}")
                    break
            
            if remaining_output_read:
                self.logger.debug("服务器剩余输出已读取")
            
            self.logger.info(f"{runtime.key} 服务器输出采集结束")
                    
        except Exception as e:
            self.logger.error(f"读取服务器输出失败: {e}", exc_info=True)

    def _is_server_stopping(self, line: str) -> bool:
        """检查服务器是否正在关闭"""
        line_lower = line.lower()
        stopping_keywords = [
            'stopping server',
            '正在保存世界'
        ]
        return any(keyword in line_lower for keyword in stopping_keywords)

    def _is_server_ready(self, line: str) -> bool:
        """检查服务器是否启动完成"""
        line_lower = line.lower()
        ready_keywords = ['done (', 'server started']
        return any(keyword in line_lower for keyword in ready_keywords)

    async def _send_server_started_notification(self, runtime: Optional[ServerRuntime] = None):
        """发送服务器启动成功通知"""
        try:
            runtime = runtime or self._runtime_for_key(self.active_server_key)
            await self._send_process_notification("Minecraft服务器启动完成!", runtime)

            self.logger.info(f"{runtime.key if runtime else '服务器'} 启动完成")

            # 触发插件事件
            if hasattr(self, 'plugin_manager') and self.plugin_manager:
                self.logger.debug("触发 server_started 事件给所有插件")
                active_server = runtime.config if runtime else (self.active_server_config or {})
                await self.plugin_manager.trigger_event(
                    "server_started",
                    target_server=active_server,
                    target_server_name=active_server.get('name', '')
                )
            else:
                self.logger.warning("无法触发插件事件: plugin_manager 未设置")
            
            # 确保关闭模式已重置
            if hasattr(self, 'command_handlers') and self.command_handlers:
                await self.command_handlers._reset_shutdown_mode()
                self.logger.info("命令处理器关闭模式已重置")
            
            # 通过连接管理器重置关闭模式
            if hasattr(self, 'connection_manager') and self.connection_manager:
                await self.connection_manager.reset_shutdown_mode()
                self.logger.info("连接管理器关闭模式已重置")
                self.logger.debug("服务器启动通知完成，连接已在启动时建立")
            else:
                self.logger.warning("连接管理器未初始化，无法自动连接")
                
        except Exception as e:
            self.logger.error(f"发送启动通知失败: {e}", exc_info=True)

    async def _monitor_server_process(self, websocket, group_id: int, runtime: Optional[ServerRuntime] = None):
        """监控服务器进程状态 - 支持异常停止自动无限重启"""
        try:
            runtime = runtime or self._runtime_for_key(self.active_server_key)
            if not runtime or not runtime.process:
                return
            self.logger.info(f"开始监控 {runtime.key} 服务器进程...")
            
            return_code = await asyncio.get_event_loop().run_in_executor(
                None, 
                runtime.process.wait
            )
            
            self.logger.info(f"{runtime.key} 服务器进程退出,返回码: {return_code}")
            if runtime.log_idle_task and not runtime.log_idle_task.done():
                runtime.log_idle_task.cancel()
                try:
                    await runtime.log_idle_task
                except asyncio.CancelledError:
                    self.logger.debug(f"{runtime.key} 日志空闲监控已随服务器进程退出而取消")
                runtime.log_idle_task = None
                if runtime.key == self.active_server_key:
                    self._log_idle_monitor_task = None
            
            # 等待日志采集任务完成
            await asyncio.sleep(2)
            
            # ============ 检查是否为手动kill ============
            if runtime.manual_kill:
                self.logger.info("检测到手动kill命令,跳过自动重启")
                runtime.manual_kill = False  # 重置标记
                if runtime.key == self.active_server_key:
                    self._manual_kill = False
                
                # 执行关闭逻辑
                if runtime.key == self.active_server_key and hasattr(self, 'command_handlers'):
                    await self.command_handlers._close_all_connections()
                else:
                    self._close_log_file(runtime)
                
                was_active = runtime.key == self.active_server_key
                runtime.process = None
                if was_active:
                    await self._promote_active_runtime_after_stop(runtime.key)
                return  # 退出,不进行任何重启
            
            # ============ 检查是否为日志空闲导致的关闭 ============
            if runtime.log_idle_kill:
                self.logger.info("检测到日志空闲导致的关闭,跳过异常停止重启")
                runtime.log_idle_kill = False  # 重置标记
                if runtime.key == self.active_server_key:
                    self._log_idle_kill = False
                
                # 重置日志更新时间
                runtime.last_log_update_time = time.time()
                
                # 等待重启延迟
                delay = self.config_manager.get_crash_restart_delay(runtime.config)
                self.logger.info(f"等待{delay}秒后进行重启...")
                await asyncio.sleep(delay)
                
                # 执行自动重启(无限制)
                self.logger.info("执行日志空闲触发的自动重启...")
                
                try:
                    # 重置关闭模式,允许重新连接
                    if hasattr(self, 'command_handlers'):
                        await self.command_handlers._reset_shutdown_mode()
                    if hasattr(self, 'connection_manager'):
                        await self.connection_manager.reset_shutdown_mode()
                    
                    # 执行启动
                    await self._start_server_process(
                        websocket,
                        group_id,
                        private_user_id=(runtime.notify_context or {}).get("private_user_id"),
                        server_config=runtime.config
                    )
                    
                    self.logger.info("服务器自动重启成功")
                    
                except Exception as e:
                    self.logger.error(f"自动重启失败: {e}", exc_info=True)
                    
                    failed_msg = "服务器自动重启失败,将在{}秒后重新尝试...".format(
                        self.config_manager.get_crash_restart_delay(runtime.config)
                    )
                    await self._send_process_notification(failed_msg, runtime)
                    
                    was_active = runtime.key == self.active_server_key
                    runtime.process = None
                    self._close_log_file(runtime)
                    if was_active:
                        await self._promote_active_runtime_after_stop(runtime.key)
                
                return  # 返回,避免执行后续的异常停止逻辑
            
            # ============ 异常停止检测和自动重启 ============
            is_normal_stop = return_code == 0
            should_auto_restart = (
                self.config_manager and 
                self.config_manager.is_auto_restart_on_crash_enabled(runtime.config) and
                not is_normal_stop  # 异常停止(非零返回码)
            )
            
            if should_auto_restart:
                self.logger.warning(f"检测到服务器异常停止(返回码: {return_code}),准备自动重启...")
                
                # 发送通知消息
                await self._send_process_notification("检测到服务器异常停止,正在自动重启...", runtime)
                
                # 等待重启延迟
                delay = self.config_manager.get_crash_restart_delay(runtime.config)
                self.logger.info(f"等待{delay}秒后进行重启...")
                await asyncio.sleep(delay)
                
                # 执行自动重启(无限制)
                self.logger.info("执行自动重启...")
                
                try:
                    # 重置关闭模式,允许重新连接
                    if hasattr(self, 'command_handlers'):
                        await self.command_handlers._reset_shutdown_mode()
                    if hasattr(self, 'connection_manager'):
                        await self.connection_manager.reset_shutdown_mode()
                    
                    # 执行启动
                    await self._start_server_process(
                        websocket,
                        group_id,
                        private_user_id=(runtime.notify_context or {}).get("private_user_id"),
                        server_config=runtime.config
                    )
                    
                    self.logger.info("服务器自动重启成功")
                    
                except Exception as e:
                    self.logger.error(f"自动重启失败: {e}", exc_info=True)
                    
                    failed_msg = "服务器自动重启失败,将在{}秒后重新尝试...".format(
                        self.config_manager.get_crash_restart_delay(runtime.config)
                    )
                    await self._send_process_notification(failed_msg, runtime)
                    
                    was_active = runtime.key == self.active_server_key
                    runtime.process = None
                    self._close_log_file(runtime)
                    if was_active:
                        await self._promote_active_runtime_after_stop(runtime.key)
                
                return  # 返回,避免执行后续的正常停止逻辑
            
            # ============ 正常停止处理 ============
            # 服务器停止时关闭所有连接
            if runtime.key == self.active_server_key and hasattr(self, 'command_handlers'):
                await self.command_handlers._close_all_connections()
            else:
                self._close_log_file(runtime)
            
            manual_stop = bool(runtime.stopping)
            if return_code == 0:
                message = "服务器正常关闭"
            else:
                message = f"服务器异常关闭,返回码: {return_code}"
            
            if manual_stop and return_code == 0:
                self.logger.debug("手动stop已由命令返回关闭结果，跳过重复正常关闭通知")
            else:
                await self._send_process_notification(message, runtime)
            
            was_active = runtime.key == self.active_server_key
            runtime.process = None
            runtime.stopping = False
            self._close_log_file(runtime)
            if was_active:
                await self._promote_active_runtime_after_stop(runtime.key)
            
        except Exception as e:
            self.logger.error(f"监控服务器进程失败: {e}", exc_info=True)
            if runtime:
                was_active = runtime.key == self.active_server_key
                runtime.process = None
                runtime.stopping = False
                self._close_log_file(runtime)
                if was_active:
                    await self._promote_active_runtime_after_stop(runtime.key)

    async def _send_crash_report_file(self, websocket, user_id: int, group_id: int, file_path: str, is_private: bool = False):
        """直接发送崩溃报告文件到群或私聊"""
        try:
            if not self._websocket_open(websocket):
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
