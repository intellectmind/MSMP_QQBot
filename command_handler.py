import time
import logging
import os
import re
import asyncio
import inspect
from pathlib import Path
from typing import Callable, Dict, List, Optional, Any
from dataclasses import dataclass
from collections import defaultdict
from rcon_client import RCONClient
from msmp_client import MSMPClient

@dataclass
class Command:
    """命令定义"""
    names: List[str]
    handler: Callable
    admin_only: bool = False
    description: str = ""
    usage: str = ""
    cooldown: int = 0
    command_key: str = ""


@dataclass
class RateLimitReservation:
    """一次命令冷却预占，失败时可安全回滚。"""
    user_id: int
    command: str
    timestamp: float
    previous_timestamp: Optional[float]


class PerCallRCONClient:
    """按命令创建短连接的RCON代理，用于多服务器插件路由。"""

    def __init__(self, host: str, port: int, password: str, logger: logging.Logger):
        self.host = host
        self.port = port
        self.password = password
        self.logger = logger

    def _with_client(self, action):
        client = RCONClient(self.host, self.port, self.password, self.logger)
        try:
            if not client.connect():
                return None
            return action(client)
        finally:
            client.close()

    def run_connected(self, action):
        client = RCONClient(self.host, self.port, self.password, self.logger)
        try:
            if not client.connect():
                return False, None
            return True, action(client)
        finally:
            client.close()

    def is_connected(self) -> bool:
        return bool(self._with_client(lambda _client: True))

    def execute_command(self, command: str):
        return self._with_client(lambda client: client.execute_command(command))

    def get_player_list(self):
        return self._with_client(lambda client: client.get_player_list())


class PerCallMSMPClient:
    """按命令创建短连接的MSMP代理，用于多服务器查询路由。"""

    def __init__(self, host: str, port: int, password: str, logger: logging.Logger, config_manager=None):
        self.host = host
        self.port = port
        self.password = password
        self.logger = logger
        self.config_manager = config_manager

    def _with_client(self, action):
        client = MSMPClient(self.host, self.port, self.password, self.logger, self.config_manager)
        loop_started = False
        try:
            client.start_background_loop()
            loop_started = True
            if not client.connect_sync():
                return None
            return action(client)
        finally:
            if loop_started:
                try:
                    if client.is_connected():
                        client.close_sync()
                finally:
                    client.loop.call_soon_threadsafe(client.loop.stop)
                    if client.thread and client.thread.is_alive():
                        client.thread.join(timeout=2)

    def run_connected(self, action):
        client = MSMPClient(self.host, self.port, self.password, self.logger, self.config_manager)
        loop_started = False
        try:
            client.start_background_loop()
            loop_started = True
            if not client.connect_sync():
                return False, None
            return True, action(client)
        finally:
            if loop_started:
                try:
                    if client.is_connected():
                        client.close_sync()
                finally:
                    client.loop.call_soon_threadsafe(client.loop.stop)
                    if client.thread and client.thread.is_alive():
                        client.thread.join(timeout=2)

    def is_connected(self) -> bool:
        return bool(self._with_client(lambda _client: True))

    def get_server_status_sync(self):
        return self._with_client(lambda client: client.get_server_status_sync())

    def get_game_rules_sync(self):
        return self._with_client(lambda client: client.get_game_rules_sync())

    def get_player_list_sync(self):
        return self._with_client(lambda client: client.get_player_list_sync())

    def send_request_sync(self, method: str, params: Any = None):
        return self._with_client(lambda client: client.send_request_sync(method, params))

    def execute_command_sync(self, command: str):
        return self._with_client(lambda client: client.execute_command_sync(command))


class RateLimiter:
    """命令速率限制器"""
    def __init__(self, default_cooldown: int = 3):
        self.default_cooldown = default_cooldown
        self.last_use = defaultdict(dict)
        self._last_cleanup = 0
        self.cleanup_interval = 300
        self.entry_ttl = 3600
    
    def check(self, user_id: int, command: str, cooldown: int = None) -> tuple:
        """只检查冷却，不写入使用时间。"""
        if cooldown is None:
            cooldown = self.default_cooldown
        
        now = time.time()
        self._cleanup_expired(now)
        last_time = self.last_use.get(user_id, {}).get(command, 0)
        elapsed = now - last_time
        
        if elapsed >= cooldown:
            return True, None
        remaining = max(1, int(cooldown - elapsed))
        return False, remaining

    def begin_use(self, user_id: int, command: str, cooldown: int = None) -> tuple:
        """预占冷却，命令失败时用 reservation 回滚。"""
        if cooldown is None:
            cooldown = self.default_cooldown

        now = time.time()
        self._cleanup_expired(now)
        user_commands = self.last_use[user_id]
        previous_timestamp = user_commands.get(command)
        elapsed = now - (previous_timestamp or 0)

        if elapsed < cooldown:
            remaining = max(1, int(cooldown - elapsed))
            return False, remaining, None

        user_commands[command] = now
        return True, None, RateLimitReservation(user_id, command, now, previous_timestamp)

    def commit(self, reservation: Optional[RateLimitReservation]):
        """确认冷却预占。当前实现中预占时间就是最终使用时间。"""
        return

    def rollback(self, reservation: Optional[RateLimitReservation]):
        """回滚未成功执行的冷却预占，避免异常/超时消耗冷却。"""
        if not reservation:
            return

        command_times = self.last_use.get(reservation.user_id)
        if not command_times:
            return
        if command_times.get(reservation.command) != reservation.timestamp:
            return
        if reservation.previous_timestamp is None:
            del command_times[reservation.command]
            if not command_times:
                del self.last_use[reservation.user_id]
        else:
            command_times[reservation.command] = reservation.previous_timestamp

    def can_use(self, user_id: int, command: str, cooldown: int = None) -> tuple:
        """兼容旧调用：检查通过时立即提交冷却。"""
        can_use, remaining, _reservation = self.begin_use(user_id, command, cooldown)
        return can_use, remaining
    
    def reset_user(self, user_id: int):
        """重置用户的所有冷却"""
        if user_id in self.last_use:
            del self.last_use[user_id]

    def _cleanup_expired(self, now: float):
        if now - self._last_cleanup < self.cleanup_interval:
            return

        self._last_cleanup = now
        expired_users = []
        for user_id, command_times in self.last_use.items():
            expired_commands = [
                command for command, last_time in command_times.items()
                if now - last_time > self.entry_ttl
            ]
            for command in expired_commands:
                del command_times[command]
            if not command_times:
                expired_users.append(user_id)

        for user_id in expired_users:
            del self.last_use[user_id]

class CommandHandler:
    """命令处理器"""
    
    def __init__(self, config_manager, logger: logging.Logger, qq_server=None):
        self.config_manager = config_manager
        self.logger = logger
        self.qq_server = qq_server
        self.commands: Dict[str, Command] = {}
        self.msmp_client = None
        self.rcon_client = None
        self.rate_limiter = RateLimiter(config_manager.get_command_cooldown())

    async def _run_handler(self, handler: Callable, timeout: float, **kwargs):
        """运行同步或异步命令处理器，并统一套超时。"""
        if inspect.iscoroutinefunction(handler):
            return await asyncio.wait_for(handler(**kwargs), timeout=timeout)

        return await asyncio.wait_for(
            asyncio.to_thread(handler, **kwargs),
            timeout=timeout
        )

    def _build_handler_kwargs(self, command_args: str, user_id: int, group_id: int,
                              target_context: Dict[str, Any], extra_kwargs: Dict[str, Any]) -> Dict[str, Any]:
        """合并命令处理参数，目标服务器上下文优先，避免重复关键字。"""
        handler_kwargs = {
            'command_text': command_args,
            'user_id': user_id,
            'group_id': group_id,
        }
        handler_kwargs.update(target_context)
        for key, value in (extra_kwargs or {}).items():
            if key not in handler_kwargs:
                handler_kwargs[key] = value
        return handler_kwargs
    
    def register_command(self, 
                        names: List[str], 
                        handler: Callable,
                        admin_only: bool = False,
                        description: str = "",
                        usage: str = "",
                        cooldown: int = 0,
                        command_key: str = ""):
        """注册命令"""
        command = Command(
            names=names,
            handler=handler,
            admin_only=admin_only,
            description=description,
            usage=usage,
            cooldown=cooldown,
            command_key=command_key
        )
        
        for name in names:
            self.commands[name.lower()] = command
        
        self.logger.debug(f"已注册命令: {', '.join(names)}")
    
    async def handle_command(self, 
                       command_text: str,
                       user_id: int,
                       group_id: int,
                       command_args: str = "",
                       plugin_manager = None,
                       **kwargs) -> Optional[str]:
        """处理命令执行"""
        command_text = command_text.strip().lower()
        is_private = bool(kwargs.get('is_private', False))
        from_console = bool(kwargs.get('from_console', False))
        command_args, target_context = self._extract_target_server(command_args, user_id, group_id, is_private, from_console)
        if target_context.get('server_access_denied'):
            return "当前群聊/私聊没有权限操作该服务器"

        target_rcon = self._build_target_rcon_client(target_context)
        if target_rcon:
            kwargs = dict(kwargs)
            kwargs['rcon_client'] = target_rcon
            target_context['target_rcon_client'] = target_rcon

        target_msmp = self._build_target_msmp_client(target_context)
        if target_msmp:
            kwargs = dict(kwargs)
            kwargs['msmp_client'] = target_msmp
            target_context['target_msmp_client'] = target_msmp
        
        self.logger.debug(
            f"处理命令: '{command_text}', 参数: '{command_args}', "
            f"目标服务器: {target_context.get('target_server_name')}, 用户: {user_id}"
        )
        
        # 第一步：检查是否是插件命令。内置命令别名保留，避免插件覆盖核心控制命令。
        builtin_command = self.commands.get(command_text)
        if plugin_manager and not builtin_command:
            for cmd_name, cmd_info in plugin_manager.command_handlers.items():
                cmd_names = cmd_info.get('normalized_names') or {str(name).lower() for name in cmd_info.get('names', [])}
                # 检查命令是否匹配（不区分大小写）
                if command_text in cmd_names:
                    self.logger.debug(f"找到插件命令: {cmd_name}")
                    if target_context.get('requires_server_selection'):
                        return self._format_server_selection_hint(command_text, target_context.get('candidate_servers') or [])
                    
                    handler = cmd_info.get('handler')
                    admin_only = cmd_info.get('admin_only', False)
                    is_admin = self._is_effective_admin(
                        user_id,
                        target_context.get('target_server'),
                        from_console
                    )
                    
                    # 检查权限
                    if admin_only and not is_admin:
                        return "权限不足：此命令仅限管理员使用"

                    if hasattr(plugin_manager, 'is_callable_enabled_for_server') and not plugin_manager.is_callable_enabled_for_server(
                        handler, target_context.get('target_server')
                    ):
                        return f"当前服务器未启用此插件命令: {command_text}"

                    cooldown = int(cmd_info.get('cooldown') or 0)
                    rate_limit_key = self._rate_limit_key(cmd_name, target_context.get('target_server'))
                    can_use, remaining, reservation = self.rate_limiter.begin_use(
                        user_id,
                        rate_limit_key,
                        cooldown if cooldown > 0 else None
                    )
                    if not can_use:
                        return f"命令冷却中，请等待 {remaining} 秒"
                    
                    # 执行插件命令
                    try:
                        timeout = 60.0 if admin_only else 30.0
                        
                        plugin_kwargs = self._build_handler_kwargs(
                            command_args, user_id, group_id, target_context, kwargs
                        )
                        
                        result = await self._run_handler(handler, timeout, **plugin_kwargs)
                        self.rate_limiter.commit(reservation)
                        return result
                        
                    except asyncio.TimeoutError:
                        self.rate_limiter.rollback(reservation)
                        self.logger.error(f"命令 {cmd_name} 执行超时 ({timeout}秒)")
                        return f"命令执行超时，请稍后重试"
                    except Exception as e:
                        self.rate_limiter.rollback(reservation)
                        self.logger.error(f"执行插件命令 {cmd_name} 时出错: {e}", exc_info=True)
                        return f"命令执行失败: {str(e)}"
        
        # 第二步：检查内置命令
        command = builtin_command
        
        if not command:
            self.logger.debug(f"未找到命令: '{command_text}'")
            return None
        
        self.logger.debug(f"找到命令: {command.names[0]}")
        if target_context.get('requires_server_selection') and command.names[0] != 'status':
            return self._format_server_selection_hint(command.names[0], target_context.get('candidate_servers') or [])
        
        # 检查命令是否可用
        is_admin = self._is_effective_admin(
            user_id,
            target_context.get('target_server'),
            from_console
        )
        
        if command.admin_only:
            # 管理员命令权限检查
            if not is_admin and not self._target_admin_command_enabled(target_context.get('target_server'), command.names[0]):
                return f"命令 {command.names[0]} 未向普通成员开放"
        else:
            # 基础命令权限检查
            if not is_admin and command.command_key:
                if not self._target_command_enabled(target_context.get('target_server'), command.command_key):
                    return None
        
        # 检查管理员权限
        if command.admin_only and not is_admin:
            if not self._target_admin_command_enabled(target_context.get('target_server'), command.names[0]):
                return "权限不足：此命令仅限管理员使用"
        
        # 检查冷却时间
        rate_limit_key = self._rate_limit_key(command.names[0], target_context.get('target_server'))
        can_use, remaining, reservation = self.rate_limiter.begin_use(
            user_id, 
            rate_limit_key,
            command.cooldown if command.cooldown > 0 else None
        )
        
        if not can_use:
            return f"命令冷却中，请等待 {remaining} 秒"
        
        # 执行命令
        try:
            timeout = 60.0 if command.admin_only and command.names[0] in ['start', 'stop', 'log', 'reconnect'] else 30.0
            
            command_kwargs = self._build_handler_kwargs(
                command_args, user_id, group_id, target_context, kwargs
            )
            result = await self._run_handler(
                command.handler,
                timeout,
                **command_kwargs
            )
            self.rate_limiter.commit(reservation)
            return result
            
        except asyncio.TimeoutError:
            self.rate_limiter.rollback(reservation)
            self.logger.error(f"命令 {command.names[0]} 执行超时 ({timeout}秒)")
            return f"命令执行超时，请稍后重试"
        except Exception as e:
            self.rate_limiter.rollback(reservation)
            self.logger.error(f"执行命令 {command.names[0]} 时出错: {e}", exc_info=True)
            return f"命令执行失败: {str(e)}"

    def _extract_target_server(self, command_args: str, user_id: int = 0, group_id: int = 0, is_private: bool = False, from_console: bool = False):
        """从命令参数开头解析服务器编号或名称，并把上下文传给所有命令。"""
        args = str(command_args or "").strip()
        servers = self.config_manager.get_servers() if hasattr(self.config_manager, 'get_servers') else []
        candidates = servers if from_console else self._context_servers(user_id, group_id, is_private)
        if servers and not candidates and not from_console:
            context = self._target_context({}, "")
            context['server_access_denied'] = True
            return args, context
        if not args or not servers:
            return args, self._implicit_target_context(candidates)

        first, _, rest = args.partition(" ")
        server = self._resolve_server_selector(first, candidates, from_console)
        if server:
            if not self._server_in_candidates(server, candidates):
                context = self._target_context({}, first)
                context['candidate_servers'] = candidates
                context['server_access_denied'] = True
                return rest.strip(), context
            context = self._target_context(server, first)
            context['candidate_servers'] = candidates
            return rest.strip(), context

        named_server, selector, named_rest = self._match_named_server_prefix(args, candidates or servers)
        if named_server:
            if not self._server_in_candidates(named_server, candidates):
                context = self._target_context({}, selector)
                context['candidate_servers'] = candidates
                context['server_access_denied'] = True
                return named_rest.strip(), context
            context = self._target_context(named_server, selector)
            context['candidate_servers'] = candidates
            return named_rest.strip(), context

        return args, self._implicit_target_context(candidates)

    def _resolve_server_selector(self, selector: str, candidates: List[Dict[str, Any]], from_console: bool):
        selector = str(selector or "").strip()
        if not selector:
            return None
        if selector.isdigit():
            source = self.config_manager.get_servers() if from_console and hasattr(self.config_manager, 'get_servers') else candidates
            index = int(selector) - 1
            if 0 <= index < len(source or []):
                return dict(source[index])
            return None
        selector_lower = selector.lower()
        for server in candidates or []:
            if str(server.get('name') or '').lower() == selector_lower:
                return dict(server)
        if from_console and hasattr(self.config_manager, 'resolve_server'):
            return self.config_manager.resolve_server(selector)
        return None

    def _match_named_server_prefix(self, args: str, servers: List[Dict[str, Any]]):
        args = str(args or "").strip()
        args_lower = args.lower()
        for server in sorted(servers or [], key=lambda item: len(str(item.get('name') or '')), reverse=True):
            name = str(server.get('name') or '').strip()
            if not name:
                continue
            name_lower = name.lower()
            if args_lower == name_lower:
                return dict(server), name, ""
            if args_lower.startswith(name_lower + " "):
                return dict(server), name, args[len(name):].strip()
        return None, "", args

    def _context_servers(self, user_id: int, group_id: int, is_private: bool):
        if hasattr(self.config_manager, 'resolve_servers_for_context'):
            return self.config_manager.resolve_servers_for_context(user_id, group_id, is_private)
        return []

    def _implicit_target_context(self, candidates):
        if len(candidates) == 1:
            context = self._target_context(candidates[0], "")
            context['candidate_servers'] = candidates
            return context
        context = self._target_context({}, "")
        context['candidate_servers'] = candidates
        if len(candidates) > 1:
            context['requires_server_selection'] = True
        return context

    def _server_in_candidates(self, server, candidates) -> bool:
        target_file = server.get('_config_file')
        target_name = str(server.get('name', '')).lower()
        for candidate in candidates:
            if target_file and candidate.get('_config_file') == target_file:
                return True
            if target_name and str(candidate.get('name', '')).lower() == target_name:
                return True
        return False

    def _target_context(self, server, selector: str):
        server = server or {}
        return {
            'target_server': server,
            'target_server_selector': selector,
            'target_server_name': server.get('name', 'server1') if server else ''
        }

    def _format_server_selection_hint(self, command_text: str, servers: List[Dict[str, Any]]) -> str:
        lines = ["当前可操作多个服务器，请在命令后指定服务器编号或名称。", ""]
        for index, server in enumerate(servers, 1):
            name = server.get('name') or f'server{index}'
            lines.append(f"{index}. {name}：{command_text} {index} 或 {command_text} {name}")
        return "\n".join(lines)

    def _rate_limit_key(self, command_name: str, target_server: Optional[Dict[str, Any]]) -> str:
        server_key = (target_server or {}).get('_config_file') or (target_server or {}).get('name') or 'default'
        return f"{command_name}:{server_key}"

    def _target_command_enabled(self, target_server: Optional[Dict[str, Any]], command_name: str) -> bool:
        commands = (target_server or {}).get('commands') or {}
        enabled = commands.get('enabled_commands') or {}
        return enabled.get(command_name, True)

    def _target_admin_command_enabled(self, target_server: Optional[Dict[str, Any]], command_name: str) -> bool:
        commands = (target_server or {}).get('commands') or {}
        enabled = commands.get('enabled_admin_commands') or {}
        return enabled.get(command_name, False)

    def _target_protocol_enabled(self, target_server: Optional[Dict[str, Any]], protocol: str) -> bool:
        protocol_config = (target_server or {}).get(protocol) or {}
        if protocol == 'msmp':
            return bool(protocol_config.get('enabled', False))
        if protocol == 'rcon':
            return bool(protocol_config.get('enabled', False))
        return False

    def _target_is_active_server(self, target_server: Optional[Dict[str, Any]]) -> bool:
        if not target_server or not self.qq_server:
            return False
        active_server = getattr(self.qq_server, 'active_server_config', None) or {}
        return self._same_server_config(target_server, active_server)

    @staticmethod
    def _same_server_config(left: Dict[str, Any], right: Dict[str, Any]) -> bool:
        left_key = (left or {}).get('_config_file') or (left or {}).get('name')
        right_key = (right or {}).get('_config_file') or (right or {}).get('name')
        return bool(left_key and right_key and str(left_key).lower() == str(right_key).lower())

    def _build_target_rcon_client(self, target_context):
        server = target_context.get('target_server') or {}
        if not server:
            return None
        if self._target_is_active_server(server):
            return None

        rcon_config = server.get('rcon') or {}
        if not rcon_config.get('enabled', False):
            return None
        port = rcon_config.get('port')
        host = rcon_config.get('host') or 'localhost'
        password = rcon_config.get('password')
        if not port or not password:
            return None
        return PerCallRCONClient(host, int(port), password, self.logger)

    def _build_target_msmp_client(self, target_context):
        server = target_context.get('target_server') or {}
        if not server:
            return None
        if self._target_is_active_server(server):
            return None

        msmp_config = server.get('msmp') or {}
        if not msmp_config.get('enabled', False):
            return None
        port = msmp_config.get('port')
        host = msmp_config.get('host') or 'localhost'
        password = msmp_config.get('password')
        if not port or not password:
            return None
        return PerCallMSMPClient(host, int(port), password, self.logger, self.config_manager)

    @staticmethod
    def _server_command_config(target_server: Optional[Dict[str, Any]], key: str, default: Any) -> Any:
        commands = (target_server or {}).get('commands') or {}
        return commands.get(key, default)

    def _is_effective_admin(
        self,
        user_id: int,
        target_server: Optional[Dict[str, Any]] = None,
        from_console: bool = False
    ) -> bool:
        """本机 GUI/控制台始终视为管理员；QQ 侧按服务器 admins 判断。"""
        if from_console:
            return True
        return self.config_manager.is_server_admin(user_id, target_server)

    def get_help_message(
        self,
        user_id: int,
        detailed: bool = False,
        target_server: Optional[Dict[str, Any]] = None,
        from_console: bool = False
    ) -> str:
        """获取帮助消息"""
        target_server = target_server or {}
        is_admin = self._is_effective_admin(user_id, target_server, from_console)
        
        basic_commands = []
        admin_commands = []
        enabled_admin_commands = []  # 对非管理员开放的管理员命令
        
        seen_commands = set()
        for name, command in self.commands.items():
            if command.names[0] not in seen_commands:
                seen_commands.add(command.names[0])
                
                if command.admin_only:
                    if is_admin:
                        admin_commands.append(command)
                    else:
                        # 检查这个管理员命令是否在目标服务器对非管理员开放
                        if self._target_admin_command_enabled(target_server, command.names[0]):
                            enabled_admin_commands.append(command)
                else:
                    if is_admin or (command.command_key and self._target_command_enabled(target_server, command.command_key)):
                        basic_commands.append(command)
        
        lines = ["MSMP_QQBot 命令帮助", "••••••••••"]
        
        if basic_commands:
            lines.append("\n【基础命令】")
            for cmd in basic_commands:
                aliases = " / ".join(cmd.names[:3])
                lines.append(f"• {aliases}")
                if cmd.description:
                    lines.append(f"  {cmd.description}")
                if cmd.usage and detailed:
                    lines.append(f"  用法: {cmd.usage}")
        
        # 对非管理员显示开放的管理员命令
        if not is_admin and enabled_admin_commands:
            lines.append("\n【开放的管理员命令】")
            for cmd in enabled_admin_commands:
                aliases = " / ".join(cmd.names[:3])
                lines.append(f"• {aliases}")
                if cmd.description:
                    lines.append(f"  {cmd.description}")
        
        if admin_commands:
            lines.append("\n【管理员专属命令】")
            for cmd in admin_commands:
                aliases = " / ".join(cmd.names[:3])
                lines.append(f"• {aliases}")
                if cmd.description:
                    lines.append(f"  {cmd.description}")
            
            lines.append("\n【直接命令执行】")
            lines.append("• !<命令>")
            lines.append("  使用 ! 前缀直接执行服务器命令")
            lines.append("  示例: !say Hello、!1 say Hello 或 !server1 say Hello")
        
        # ========== 添加自定义指令信息 ==========
        custom_commands_config = target_server.get('custom_commands') or {}
        if custom_commands_config.get('enabled', False):
            try:
                custom_rules = custom_commands_config.get('rules', [])
                
                if custom_rules:
                    # 按权限过滤自定义指令
                    user_custom_commands = []
                    for rule in custom_rules:
                        if not rule.get('enabled', True):
                            continue
                        
                        admin_only = rule.get('admin_only', False)
                        if admin_only and not is_admin:
                            continue
                        
                        user_custom_commands.append(rule)
                    
                    if user_custom_commands:
                        lines.append("\n【自定义指令】")
                        lines.append(f"已启用 {len(user_custom_commands)} 个自定义指令:")
                        
                        for rule in user_custom_commands:
                            rule_name = rule.get('name', 'unknown')
                            description = rule.get('description', '')
                            pattern = rule.get('pattern', '')
                            admin_tag = " [仅管理员]" if rule.get('admin_only', False) else ""
                            
                            lines.append(f"• {rule_name}{admin_tag}")
                            if description:
                                lines.append(f"  {description}")
                            if pattern:
                                # 简化显示触发模式
                                pattern_preview = pattern[:40]
                                if len(pattern) > 40:
                                    pattern_preview += "..."
                                lines.append(f"  触发: {pattern_preview}")
            
            except Exception as e:
                pass
        
        # ========== 添加自定义消息监听器信息 ==========
        custom_listeners_config = target_server.get('custom_listeners') or {}
        if custom_listeners_config.get('enabled', False):
            try:
                listener_rules = custom_listeners_config.get('rules', [])
                
                if listener_rules:
                    enabled_listeners = [r for r in listener_rules if r.get('enabled', True)]
                    
                    if enabled_listeners:
                        lines.append("\n【服务器消息监听】")
                        lines.append(f"已启用 {len(enabled_listeners)} 个监听规则")
                        
                        # 显示前几个规则的名称
                        listener_names = [r.get('name', 'unknown') for r in enabled_listeners]
                        for name in listener_names[:5]:  # 最多显示5个
                            lines.append(f"• {name}")
                        
                        if len(listener_names) > 5:
                            lines.append(f"... 还有 {len(listener_names) - 5} 个规则")
                        
                        if is_admin:
                            lines.append(f"使用 listeners 查看完整监听规则详情")
            
            except Exception as e:
                pass
        
        # 添加使用提示
        if not is_admin:
            lines.append(f"\n提示: 当前有 {len(enabled_admin_commands)} 个管理员命令对您开放")
        else:
            lines.append(f"\n您是管理员，可以使用所有命令")

        return "\n".join(lines)
    
    def list_commands(self, admin_only: bool = False) -> List[str]:
        """列出所有命令"""
        seen = set()
        result = []
        for name, command in self.commands.items():
            if command.names[0] not in seen:
                seen.add(command.names[0])
                if not admin_only or command.admin_only:
                    result.append(command.names[0])
        return result


class CommandHandlers:
    """命令处理器集合"""
    
    def __init__(self, msmp_client, rcon_client, qq_server, config_manager, logger):
        self.qq_server = qq_server
        self.rcon_client = rcon_client
        self.config_manager = config_manager
        self.logger = logger
        self._stop_lock = asyncio.Lock()
        self._is_stopping = False
        self._stopping_servers = set()
        self._shutdown_event = asyncio.Event()
        self._shutdown_initiated = False
    
    @property
    def msmp_client(self):
        """动态获取 msmp_client"""
        return self.qq_server.msmp_client if self.qq_server else None
    
    def set_shutdown_mode(self):
        """设置关闭模式，停止所有连接检测"""
        self._shutdown_event.set()
        self._is_stopping = True
        self._stopping_servers.add(self._server_operation_key())
        self.logger.info("已进入关闭模式，停止所有连接检测")

    def _server_operation_key(self, target_server: Optional[Dict[str, Any]] = None) -> str:
        if target_server:
            return str(target_server.get('_config_file') or target_server.get('name') or 'default')
        active_server = getattr(self.qq_server, 'active_server_config', None) if self.qq_server else None
        return str((active_server or {}).get('_config_file') or (active_server or {}).get('name') or 'default')
    
    async def handle_list(self, **kwargs) -> str:
        """处理list命令"""
        try:
            target_server = kwargs.get('target_server') or {}
            target_is_active = self._target_is_active_server(target_server)
            target_msmp = (
                kwargs.get('target_msmp_client') or
                kwargs.get('msmp_client')
            )
            target_rcon = (
                kwargs.get('target_rcon_client') or
                kwargs.get('rcon_client')
            )
            if target_msmp:
                client_type, client = 'msmp', target_msmp
            elif target_rcon:
                client_type, client = 'rcon', target_rcon
            elif target_is_active:
                client_type, client = await self.qq_server.connection_manager.get_preferred_client()
            else:
                client_type, client = None, None
            
            # 如果没有连接，尝试自动重连一次
            if not client:
                if not target_is_active:
                    return "目标服务器连接未就绪，请检查该服务器的 MSMP/RCON 配置"
                self.logger.info("检测到连接未就绪，尝试自动重连...")
                await self.qq_server.connection_manager.reconnect_all()
                
                # 重连后再次获取客户端
                client_type, client = await self.qq_server.connection_manager.get_preferred_client()
                
                if not client:
                    return "服务器连接未就绪\n自动重连失败，请使用 reconnect 手动重连"
            
            try:
                if client_type == 'msmp':
                    player_info = await asyncio.to_thread(client.get_player_list_sync)
                else:
                    player_info = await asyncio.to_thread(client.get_player_list)
            except Exception as e:
                self.logger.error(f"获取玩家列表失败: {e}")
                return f"获取玩家列表失败: {str(e)}"
            
            lines = [f"在线人数: {player_info.current_players}/{player_info.max_players}"]
            
            if player_info.current_players > 0 and player_info.player_names:
                player_list = "    ".join(player_info.player_names)
                lines.append(f"在线玩家:\n{player_list}")
            else:
                lines.append("\n暂无玩家在线")
            
            lines.append(f"\n[通过 {client_type.upper()} 查询]")
            return "\n".join(lines)
            
        except Exception as e:
            self.logger.error(f"执行list命令失败: {e}", exc_info=True)
            return f"获取玩家列表失败: {e}"

    async def handle_tps(self, **kwargs) -> str:
        """处理tps命令"""
        try:
            target_server = kwargs.get('target_server') or {}
            target_is_active = self._target_is_active_server(target_server)
            if not self._target_protocol_enabled(target_server, 'rcon'):
                return "TPS查询需要启用RCON连接"

            target_rcon = kwargs.get('target_rcon_client') or kwargs.get('rcon_client')
            if target_rcon:
                client_type, client = 'rcon', target_rcon
            elif target_is_active:
                client_type, client = await self.qq_server.connection_manager.get_client_for_command("tps")
            else:
                client_type, client = None, None
            
            # 如果没有RCON连接，尝试自动重连一次
            if not client or client_type != 'rcon':
                if not target_is_active:
                    return "TPS命令需要目标服务器的RCON连接，请检查该服务器RCON配置"
                self.logger.info("检测到RCON连接未就绪，尝试自动重连...")
                await self.qq_server.connection_manager.reconnect_rcon()
                
                # 重连后再次获取客户端
                client_type, client = await self.qq_server.connection_manager.get_client_for_command("tps")
                
                if not client or client_type != 'rcon':
                    return "TPS命令需要RCON连接\n自动重连失败，请使用 reconnect_rcon 重连"
            
            tps_command = self._target_command_config(target_server, 'tps_command', self.config_manager.get_tps_command())
            result = await asyncio.to_thread(client.execute_command, tps_command)
            
            if result:
                # 第一步：清理Minecraft颜色代码 (§[0-9a-fk-or] 或 &[0-9a-fk-or])
                cleaned = re.sub(r'[§&][0-9a-fk-orA-FK-OR]', '', result).strip()
                
                self.logger.debug(f"原始TPS返回: {result}")
                self.logger.debug(f"清理后的TPS返回: {cleaned}")
                
                # 第二步：尝试使用正则表达式提取TPS值
                tps_value = self._extract_tps_value(cleaned, target_server)
                
                # 第三步：构建响应消息
                message_lines = ["服务器TPS信息:"]
                message_lines.append("=" * 20)
                
                if tps_value is not None:
                    # 评估TPS状态
                    tps_status = self._evaluate_tps_status(tps_value)
                    message_lines.append(f"解析的TPS值: {tps_value:.1f} {tps_status}")
                else:
                    message_lines.append(" 无法解析TPS值，请查看原始信息")
                    # 当无法解析时，记录调试信息
                    self.logger.warning(
                        f"TPS值解析失败\n"
                        f"正则表达式: {self._target_command_config(target_server, 'tps_regex', self.config_manager.get_tps_regex())}\n"
                        f"捕获组索引: {self._target_command_config(target_server, 'tps_group_index', self.config_manager.get_tps_group_index())}\n"
                        f"清理后的文本: {cleaned}"
                    )
                
                # 第四步：是否显示原始输出
                if self._target_command_config(target_server, 'tps_show_raw_output', self.config_manager.is_tps_raw_output_enabled()):
                    message_lines.append("")
                    message_lines.append("服务器原始TPS信息:")
                    message_lines.append("-" * 20)
                    message_lines.append(cleaned)
                
                message_lines.append("=" * 20)
                message_lines.append("[通过 RCON 查询]")
                
                return "\n".join(message_lines)
            else:
                return "TPS命令执行成功,但无返回结果"
                
        except Exception as e:
            self.logger.error(f"执行TPS命令失败: {e}", exc_info=True)
            return f"获取TPS信息失败: {e}"

    def _target_command_config(self, target_server: Dict[str, Any], key: str, default: Any) -> Any:
        commands = (target_server or {}).get('commands') or {}
        return commands.get(key, default)

    def _target_is_active_server(self, target_server: Optional[Dict[str, Any]]) -> bool:
        if not target_server or not self.qq_server:
            return False
        active_server = getattr(self.qq_server, 'active_server_config', None) or {}
        return self._same_server_config(target_server, active_server)

    @staticmethod
    def _target_protocol_enabled(target_server: Optional[Dict[str, Any]], protocol: str) -> bool:
        protocol_config = (target_server or {}).get(protocol) or {}
        if protocol in ('msmp', 'rcon'):
            return bool(protocol_config.get('enabled', False))
        return False

    def _extract_tps_value(self, text: str, target_server: Optional[Dict[str, Any]] = None) -> Optional[float]:
        """从服务器返回的文本中提取TPS值"""
        try:
            target_server = target_server or {}
            tps_regex = self._target_command_config(target_server, 'tps_regex', self.config_manager.get_tps_regex())
            tps_group_index = self._target_command_config(target_server, 'tps_group_index', self.config_manager.get_tps_group_index())
            
            # 验证group_index的有效性
            if tps_group_index < 1:
                self.logger.warning(f"无效的tps_group_index: {tps_group_index}, 使用默认值1")
                tps_group_index = 1
            
            # 使用提供的正则表达式进行匹配
            pattern = re.compile(tps_regex, re.IGNORECASE)
            match = pattern.search(text)
            
            if match:
                # 获取指定的捕获组
                try:
                    tps_str = match.group(tps_group_index)
                    tps_value = float(tps_str)
                    
                    # 验证TPS值的合理性（0-20之间）
                    if 0 <= tps_value <= 20:
                        self.logger.debug(f"成功提取TPS值: {tps_value}")
                        return tps_value
                    else:
                        self.logger.warning(f"TPS值超出合理范围: {tps_value}")
                        return None
                        
                except (IndexError, ValueError) as e:
                    self.logger.warning(f"提取捕获组失败 (group {tps_group_index}): {e}")
                    return None
            else:
                self.logger.warning(f"正则表达式未匹配: {tps_regex}")
                self.logger.debug(f"尝试匹配的文本: {text[:200]}")
                
                # 第五步：如果第一次匹配失败，尝试更宽松的正则表达式
                # 这可以处理一些不标准的格式
                fallback_patterns = [
                    r'(\d+(?:\.\d+)?)',  # 任何数字或浮点数
                    r'TPS[:\s]+(\d+(?:\.\d+)?)',  # TPS: 数字
                    r'(\d+(?:\.\d+)?)\s*(?:tps|TPS)',  # 数字 TPS
                ]
                
                for fallback_regex in fallback_patterns:
                    try:
                        fallback_pattern = re.compile(fallback_regex, re.IGNORECASE)
                        fallback_match = fallback_pattern.search(text)
                        if fallback_match:
                            fallback_tps_str = fallback_match.group(1)
                            fallback_tps_value = float(fallback_tps_str)
                            if 0 <= fallback_tps_value <= 20:
                                self.logger.info(
                                    f"使用备用正则表达式成功提取TPS值: {fallback_tps_value}\n"
                                    f"备用正则: {fallback_regex}"
                                )
                                return fallback_tps_value
                    except Exception as e:
                        self.logger.debug(f"备用正则表达式匹配失败 ({fallback_regex}): {e}")
                        continue
                
                return None
                
        except Exception as e:
            self.logger.error(f"提取TPS值时出错: {e}")
            return None


    def _evaluate_tps_status(self, tps_value: float) -> str:
        """评估TPS状态并返回状态标签"""
        if tps_value >= 19.5:
            return "优秀"
        elif tps_value >= 15:
            return "良好"
        elif tps_value >= 10:
            return "一般"
        elif tps_value >= 5:
            return "较差"
        else:
            return "很差"


    # 清理Minecraft格式代码
    @staticmethod
    def _clean_minecraft_colors(text: str) -> str:
        """清理Minecraft颜色代码和格式代码
        
        支持以下格式:
        - §[0-9a-fk-or] - Minecraft标准颜色代码
        - &[0-9a-fk-or] - 另一种常见格式
        """
        # 清理 § 格式的颜色代码
        text = re.sub(r'§[0-9a-fk-orA-FK-OR]', '', text)
        # 清理 & 格式的颜色代码
        text = re.sub(r'&[0-9a-fk-orA-FK-OR]', '', text)
        # 清理其他常见的ANSI转义序列
        text = re.sub(r'\x1b\[[0-9;]*m', '', text)
        return text
        
    async def handle_rules(self, **kwargs) -> str:
        """处理rules命令"""
        try:
            target_server = kwargs.get('target_server') or getattr(self.qq_server, 'active_server_config', None) or {}
            target_is_active = self._target_is_active_server(target_server)
            if not self._target_protocol_enabled(target_server, 'msmp'):
                return "规则查询需要启用MSMP连接"

            client = kwargs.get('target_msmp_client') or kwargs.get('msmp_client')
            if not client and target_is_active:
                client = self.msmp_client
            if not client:
                if not target_is_active:
                    return "目标服务器MSMP连接未就绪，请检查该服务器MSMP配置"
                client_type, client = await self.qq_server.connection_manager.ensure_connected()
                if client_type != 'msmp':
                    client = None

            if not client:
                return "MSMP连接未就绪\n请使用 #reconnect_msmp 手动重连"
            
            self.logger.info("查询服务器规则...")
            
            lines = ["服务器规则信息", "=" * 20]
            
            try:
                gamerules_result = await asyncio.to_thread(client.get_game_rules_sync)
                
                if 'result' in gamerules_result and isinstance(gamerules_result['result'], list):
                    gamerules_list = gamerules_result['result']
                    
                    gamerules_dict = {}
                    for rule in gamerules_list:
                        if isinstance(rule, dict) and 'key' in rule and 'value' in rule:
                            gamerules_dict[rule['key']] = rule['value']
                    
                    important_rules = {
                        'keepInventory': '死亡不掉落',
                        'doDaylightCycle': '时间循环',
                        'doMobSpawning': '生物生成',
                        'mobGriefing': '生物破坏',
                        'doFireTick': '火焰蔓延',
                        'pvp': 'PVP模式',
                        'commandBlockOutput': '命令方块输出',
                        'naturalRegeneration': '自然生命恢复',
                        'doWeatherCycle': '天气循环',
                        'announceAdvancements': '成就通知',
                        'showDeathMessages': '显示死亡信息'
                    }
                    
                    rules_found = False
                    for rule_key, rule_name in important_rules.items():
                        if rule_key in gamerules_dict:
                            if not rules_found:
                                lines.append("\n游戏规则:")
                                rules_found = True
                            
                            value = gamerules_dict[rule_key]
                            if isinstance(value, bool):
                                value_str = "启用" if value else "禁用"
                            elif isinstance(value, str) and value.lower() in ['true', 'false']:
                                value_str = "启用" if value.lower() == 'true' else "禁用"
                            else:
                                value_str = str(value)
                            lines.append(f"• {rule_name}: {value_str}")
                    
                    server_settings = {
                        'difficulty': '难度',
                        'view_distance': '视距',
                        'simulation_distance': '模拟距离',
                        'max_players': '最大玩家数',
                        'game_mode': '默认游戏模式',
                        'spawn_protection_radius': '出生点保护半径',
                        'player_idle_timeout': '闲置超时时间'
                    }
                    
                    settings_found = False
                    for setting_key, setting_name in server_settings.items():
                        try:
                            result = await asyncio.to_thread(
                                client.send_request_sync,
                                f"serversettings/{setting_key}"
                            )
                            
                            if 'result' in result:
                                if not settings_found:
                                    lines.append("\n服务器设置:")
                                    settings_found = True
                                
                                value = result['result']
                                
                                if value is not None:
                                    if setting_key == 'difficulty':
                                        if isinstance(value, str):
                                            difficulty_map = {
                                                'peaceful': '和平',
                                                'easy': '简单',
                                                'normal': '普通',
                                                'hard': '困难'
                                            }
                                            value_str = difficulty_map.get(value.lower(), value)
                                        else:
                                            difficulty_map = {0: '和平', 1: '简单', 2: '普通', 3: '困难'}
                                            value_str = difficulty_map.get(value, str(value))
                                    elif setting_key == 'game_mode':
                                        gamemode_map = {
                                            'survival': '生存',
                                            'creative': '创造',
                                            'adventure': '冒险',
                                            'spectator': '旁观'
                                        }
                                        value_str = gamemode_map.get(str(value).lower(), str(value))
                                    elif setting_key in ['view_distance', 'simulation_distance']:
                                        value_str = f"{value} 区块"
                                    elif setting_key == 'spawn_protection_radius':
                                        value_str = f"{value} 方块"
                                    elif setting_key == 'player_idle_timeout':
                                        if value == 0:
                                            value_str = "禁用"
                                        else:
                                            value_str = f"{value} 分'"
                                    else:
                                        value_str = str(value)
                                    
                                    lines.append(f"• {setting_name}: {value_str}")
                        except Exception as e:
                            self.logger.debug(f"查询设置 {setting_key} 失败: {e}")
                            continue
                    
                    if len(lines) == 2:
                        lines.append("\n未能获取到规则信息")
                        lines.append("提示: MSMP连接正常但无法获取规则数据")
                        lines.append("可能原因:")
                        lines.append("1. MSMP插件版本过旧")
                        lines.append("2. 服务器权限配置问题")
                        lines.append("3. 查看服务器日志了解详情")
                    
                    lines.append("\n" + "=" * 20)
                    lines.append("[通过 MSMP 查询]")
                    
                    return "\n".join(lines)
                    
            except Exception as e:
                self.logger.error(f"获取规则信息失败: {e}", exc_info=True)
                return f"获取规则信息失败: {str(e)}\n提示: 请检查MSMP插件版本和配置"
                    
        except Exception as e:
            self.logger.error(f"执行rules命令失败: {e}", exc_info=True)
            return f"查询服务器规则失败: {e}"
    
    async def handle_status(self, **kwargs) -> str:
        """处理status命令：按当前群聊/私聊上下文聚合可操作服务器。"""
        try:
            explicit_selector = str(kwargs.get('target_server_selector') or '').strip()
            explicit_target = kwargs.get('target_server') or {}
            if explicit_selector and explicit_target:
                status_servers = [explicit_target]
            else:
                status_servers = [server for server in (kwargs.get('candidate_servers') or []) if server]
            if not status_servers:
                target_server = kwargs.get('target_server') or getattr(self.qq_server, 'active_server_config', None) or {}
                status_servers = [target_server] if target_server else []

            if len(status_servers) <= 1:
                single_kwargs = self._status_kwargs_for_server(status_servers[0] if status_servers else {}, kwargs)
                return await self._handle_single_status(include_qq=True, **single_kwargs)

            qq_status = "已连接" if self.qq_server.is_connected() else "未连接"
            lines = ["系统状态总览", "■■■■■■■■■■■■■■■", f"QQ机器人: {qq_status}"]
            for index, server in enumerate(status_servers, 1):
                single_kwargs = self._status_kwargs_for_server(server, kwargs)
                lines.append("")
                lines.append(f"【{index}. {server.get('name') or f'server{index}'}】")
                lines.append(await self._handle_single_status(include_qq=False, **single_kwargs))
            return "\n".join(lines)
        except Exception as e:
            self.logger.error(f"执行status命令失败: {e}", exc_info=True)
            return f"获取状态失败: {e}"

    def _status_kwargs_for_server(self, target_server: Dict[str, Any], base_kwargs: Dict[str, Any]) -> Dict[str, Any]:
        """为聚合 status 构建单个服务器的连接上下文。"""
        result = dict(base_kwargs)
        result['target_server'] = target_server or {}
        active_server = getattr(self.qq_server, 'active_server_config', None) or {}
        if target_server and not self._same_server_config(target_server, active_server):
            result.pop('msmp_client', None)
            result.pop('rcon_client', None)
            result['target_msmp_client'] = self._build_status_msmp_client(target_server)
            result['target_rcon_client'] = self._build_status_rcon_client(target_server)
        return result

    def _build_status_rcon_client(self, target_server: Dict[str, Any]):
        rcon_config = (target_server or {}).get('rcon') or {}
        if not rcon_config.get('enabled', False):
            return None
        port = rcon_config.get('port')
        password = rcon_config.get('password')
        if not port or not password:
            return None
        return PerCallRCONClient(rcon_config.get('host') or 'localhost', int(port), password, self.logger)

    def _build_status_msmp_client(self, target_server: Dict[str, Any]):
        msmp_config = (target_server or {}).get('msmp') or {}
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

    async def _handle_single_status(self, include_qq: bool = True, **kwargs) -> str:
        """生成单个服务器状态。"""
        try:
            qq_status = "已连接" if self.qq_server.is_connected() else "未连接"
            active_server = getattr(self.qq_server, 'active_server_config', None) or {}
            target_server = kwargs.get('target_server') or active_server
            active_server_name = active_server.get('name', '默认服务器')
            target_server_name = target_server.get('name') or active_server_name
            target_msmp = kwargs.get('target_msmp_client') or kwargs.get('msmp_client')
            target_rcon = kwargs.get('target_rcon_client') or kwargs.get('rcon_client')
            target_is_active = not target_server or self._same_server_config(target_server, active_server)
            
            msmp_status = "未启用"
            msmp_connected = False
            if self._target_protocol_enabled(target_server, 'msmp'):
                msmp_for_status = target_msmp or (self.msmp_client if target_is_active else None)
                if not msmp_for_status:
                    msmp_status = "客户端未初始化"
                else:
                    try:
                        status = await asyncio.to_thread(msmp_for_status.get_server_status_sync)
                        version = status.get('version', {})
                        version_name = version.get('name', 'Unknown')
                                            
                        msmp_status = (
                            f"运行中\n"
                            f"版本: {version_name}"
                        )
                        msmp_connected = True
                    except Exception as e:
                        msmp_status = f"连接异常: {e}"
            
            rcon_status = "未启用"
            rcon_connected = False
            rcon_player_info = None
            if self._target_protocol_enabled(target_server, 'rcon'):
                if target_rcon:
                    try:
                        if isinstance(target_rcon, PerCallRCONClient):
                            rcon_connected, rcon_player_info = await asyncio.to_thread(
                                target_rcon.run_connected,
                                lambda client: client.get_player_list()
                            )
                        else:
                            rcon_connected = await asyncio.to_thread(target_rcon.is_connected)
                        rcon_status = "运行中" if rcon_connected else "未连接"
                    except Exception as e:
                        rcon_status = f"连接异常: {e}"
                elif not target_is_active:
                    rcon_status = "客户端未初始化"
                elif not self.rcon_client:
                    rcon_status = "客户端未初始化"
                elif not self.rcon_client.is_connected():
                    rcon_status = "未连接"
                else:
                    try:
                        rcon_status = f"运行中"
                        rcon_connected = True
                    except Exception as e:
                        rcon_status = f"连接异常: {e}"
            
            # 检测外部接入状态
            external_access = msmp_connected or rcon_connected
            
            # 添加 Minecraft 服务器状态
            mc_server_status = "未启动"
            server_process_running = False
            target_process = (
                self.qq_server.get_server_process(target_server)
                if self.qq_server and hasattr(self.qq_server, 'get_server_process') and target_server
                else (self.qq_server.server_process if self.qq_server else None)
            )
            player_client_type = None
            player_client = None
            if msmp_connected and target_msmp:
                player_client_type, player_client = 'msmp', target_msmp
            elif rcon_connected and target_rcon:
                player_client_type, player_client = 'rcon', target_rcon
            
            if self.qq_server and target_process:
                if target_process.poll() is None:
                    # 服务器进程正在运行
                    server_process_running = True
                    try:
                        # 尝试获取更详细的状态
                        if player_client:
                            client_type, client = player_client_type, player_client
                        elif target_is_active:
                            client_type, client = await self.qq_server.connection_manager.ensure_connected()
                        else:
                            client_type, client = None, None
                        if client:
                            if client_type == 'msmp':
                                player_info = await asyncio.to_thread(client.get_player_list_sync)
                                mc_server_status = (
                                    f"{target_server_name} 运行中 (PID: {target_process.pid})\n"
                                    f"在线: {player_info.current_players}/{player_info.max_players}"
                                )
                            elif client_type == 'rcon':
                                rcon_for_status = client
                                player_info = rcon_player_info or await asyncio.to_thread(rcon_for_status.get_player_list)
                                mc_server_status = (
                                    f"{target_server_name} 运行中 (PID: {target_process.pid})\n"
                                    f"在线: {player_info.current_players}/{player_info.max_players}"
                                )
                            else:
                                mc_server_status = f"{target_server_name} 运行中 (PID: {target_process.pid})"
                        else:
                            mc_server_status = f"{target_server_name} 运行中 (PID: {target_process.pid}) - 连接异常"
                    except Exception as e:
                        mc_server_status = f"运行中 (PID: {target_process.pid}) - 状态获取失败"
                else:
                    return_code = target_process.poll()
                    mc_server_status = f"已停止 (退出码: {return_code})"
            else:
                # 服务器进程未运行，但可能有外部接入
                if external_access:
                    mc_server_status = "运行中 (外部接入)"
                    try:
                        if player_client:
                            client_type, client = player_client_type, player_client
                        elif target_is_active:
                            client_type, client = await self.qq_server.connection_manager.get_preferred_client()
                        else:
                            client_type, client = None, None
                        if client_type == 'msmp':
                            player_info = await asyncio.to_thread(client.get_player_list_sync)
                            mc_server_status = (
                                f"{target_server_name} 运行中 (外部接入)\n"
                                f"在线: {player_info.current_players}/{player_info.max_players}"
                            )
                        elif client_type == 'rcon':
                            player_info = rcon_player_info or await asyncio.to_thread(client.get_player_list)
                            mc_server_status = (
                                f"{target_server_name} 运行中 (外部接入)\n"
                                f"在线: {player_info.current_players}/{player_info.max_players}"
                            )
                    except Exception as e:
                        self.logger.debug(f"获取外部接入服务器状态失败: {e}")
                else:
                    mc_server_status = "未启动"
            
            # 构建状态信息
            status_lines = []
            if include_qq:
                status_lines.extend([
                    "系统状态总览",
                    "■■■■■■■■■■■■■■■",
                    f"QQ机器人: {qq_status}",
                ])
            status_lines.extend([
                f"MC服务器: {mc_server_status}",
                f"MSMP连接: {msmp_status}",
                f"RCON连接: {rcon_status}"
            ])
            
            # 添加外部接入提示（当有外部接入但服务器进程未运行时）
            if external_access and not server_process_running:
                status_lines.append("■■■■■■■■■■■■■■■")
                status_lines.append("检测到外部接入: 服务器通过MSMP/RCON远程管理")
            
            return "\n".join(status_lines)
            
        except Exception as e:
            self.logger.error(f"执行status命令失败: {e}", exc_info=True)
            return f"获取状态失败: {e}"

    def _format_uptime(self, seconds: float) -> str:
        """格式化运行时间"""
        days = int(seconds // 86400)
        hours = int((seconds % 86400) // 3600)
        minutes = int((seconds % 3600) // 60)
        seconds = int(seconds % 60)
        
        if days > 0:
            return f"{days}天{hours}时{minutes}分"
        elif hours > 0:
            return f"{hours}时{minutes}分{seconds}秒"
        elif minutes > 0:
            return f"{minutes}分{seconds}秒"
        else:
            return f"{seconds}秒"
    
    async def handle_help(self, user_id: int, **kwargs) -> str:
        """处理help命令"""
        if hasattr(self.qq_server, 'command_handler'):
            return self.qq_server.command_handler.get_help_message(
                user_id,
                target_server=kwargs.get('target_server') or getattr(self.qq_server, 'active_server_config', None),
                from_console=bool(kwargs.get('from_console', False))
            )
        return "帮助系统未初始化"
    
    async def handle_stop(self, user_id: int, group_id: int, websocket, is_private: bool = False, **kwargs) -> str:
        """处理stop命令(管理员) - 支持MSMP和RCON"""
        stop_key = self._server_operation_key(kwargs.get('target_server') or {})
        try:
            if not self.qq_server:
                return "服务器未运行"

            server_selector = str(kwargs.get('target_server_selector', '') or '').strip()
            requested_server = kwargs.get('target_server') or (
                self.config_manager.resolve_server(server_selector) if server_selector else {}
            )
            if server_selector and not requested_server:
                return f"未找到服务器: {server_selector}"
            stop_key = self._server_operation_key(requested_server)

            async with self._stop_lock:
                if stop_key in self._stopping_servers:
                    return "服务器已在停止中，请勿重复执行"
                self._stopping_servers.add(stop_key)

            active_server = getattr(self.qq_server, 'active_server_config', None) or {}
            target_is_active = not requested_server or self._same_server_config(requested_server, active_server)
            target_process = (
                self.qq_server.get_server_process(requested_server)
                if hasattr(self.qq_server, 'get_server_process') and requested_server
                else self.qq_server.server_process
            )
            local_process_running = (
                target_process and
                target_process.poll() is None
            )

            target_msmp = kwargs.get('target_msmp_client') or kwargs.get('msmp_client')
            target_rcon = kwargs.get('target_rcon_client') or kwargs.get('rcon_client')
            if target_msmp:
                client_type, client = 'msmp', target_msmp
            elif target_rcon:
                client_type, client = 'rcon', target_rcon
            elif target_is_active:
                client_type, client = await self.qq_server.connection_manager.ensure_connected()
            else:
                client_type, client = None, None
            
            # 外部接入没有本地 server_process，仍允许通过 RCON/MSMP 停止。
            if not local_process_running and not client:
                return "服务器未运行"
            
            if websocket:
                if is_private:
                    await self.qq_server.send_private_message(websocket, user_id, "正在停止服务器...")
                else:
                    await self.qq_server.send_group_message(websocket, group_id, "正在停止服务器...")
            
            # ============ 触发服务器停止事件 ============
            if hasattr(self.qq_server, 'plugin_manager') and self.qq_server.plugin_manager:
                self.logger.info("触发 server_stopping 事件给所有插件")
                event_server = kwargs.get('target_server') or getattr(self.qq_server, 'active_server_config', None) or {}
                await self.qq_server.plugin_manager.trigger_event(
                    "server_stopping",
                    target_server=event_server,
                    target_server_name=event_server.get('name', '')
                )
            # ============ 事件触发结束 ============
            
            # 只有停止当前Bot托管的服务器时，才暂停本地日志采集。
            if requested_server and hasattr(self.qq_server, 'set_server_stopping'):
                self.qq_server.set_server_stopping(requested_server, True)
            elif target_is_active:
                self.qq_server.server_stopping = True
            
            # 第二步：先尝试通过连接发送停止命令（在关闭连接之前）
            stop_success = False
            
            try:
                if client:
                    if client_type == 'msmp':
                        result = await asyncio.to_thread(client.execute_command_sync, "server/stop")
                        if 'result' in result:
                            stop_success = True
                            self.logger.info("MSMP 停止命令已发送")
                    
                    elif client_type == 'rcon':
                        result = await asyncio.to_thread(client.execute_command, "stop")
                        if result is None:
                            raise RuntimeError("RCON未返回停止命令响应")
                        stop_success = True
                        self.logger.info("RCON停止命令已发送")
                else:
                    self.logger.warning("无可用连接发送停止命令")
            
            except Exception as e:
                self.logger.warning(f"通过连接发送停止命令失败: {e}")
            
            # 第三步：如果无法通过连接停止，尝试通过标准输入发送停止命令
            if not stop_success:
                try:
                    if (local_process_running and
                        target_process and
                        target_process.poll() is None and
                        target_process.stdin):
                        
                        stop_command = "stop\n"
                        target_process.stdin.write(stop_command.encode('utf-8'))
                        target_process.stdin.flush()
                        self.logger.info("已通过标准输入发送停止命令")
                        stop_success = True
                except Exception as e:
                    self.logger.warning(f"通过标准输入发送停止命令失败: {e}")

            if not stop_success:
                if requested_server and hasattr(self.qq_server, 'set_server_stopping'):
                    self.qq_server.set_server_stopping(requested_server, False)
                elif target_is_active:
                    self.qq_server.server_stopping = False
                return "无法发送停止命令：RCON/MSMP未成功执行，且没有可用的本地标准输入"
            
            # 第四步：停止当前活动服务器时才关闭全局连接，避免 stop 2 误断 server1。
            if target_is_active:
                await self._thorough_shutdown()
            
            if local_process_running:
                self.logger.info("停止命令已发送，等待服务器关闭进程...")
            else:
                self.logger.info("外部接入服务器停止命令已发送")
            
            # 等待服务器关闭
            max_wait_time = 60
            wait_interval = 5
            waited_time = 0
            
            while (local_process_running and
                   waited_time < max_wait_time and 
                   target_process and 
                   target_process.poll() is None):
                await asyncio.sleep(wait_interval)
                waited_time += wait_interval
                self.logger.info(f"等待服务器关闭... ({waited_time}/{max_wait_time}秒)")
            
            # 检查服务器是否已关闭
            server_stopped = True
            if local_process_running and target_process:
                return_code = target_process.poll()
                if return_code is None:
                    server_stopped = False
                    self.logger.warning(f"服务器进程在{max_wait_time}秒后仍未关闭")
                    if requested_server and hasattr(self.qq_server, 'set_server_stopping'):
                        self.qq_server.set_server_stopping(requested_server, False)
                    elif target_is_active:
                        self.qq_server.server_stopping = False
                else:
                    self.logger.info(f"服务器进程已关闭，返回码: {return_code}")
            
            # 给日志采集任务一点时间读取剩余输出
            await asyncio.sleep(2)
            
            if local_process_running:
                result_message = "服务器已成功关闭" if server_stopped else "停止命令已发送，但服务器进程仍在运行中。可能需要手动检查或使用 #kill 命令强制停止"
            else:
                result_message = "外部接入服务器停止命令已发送"
            if kwargs.get('from_console', False):
                print(result_message)
            
            return result_message
            
        except Exception as e:
            self.logger.error(f"执行stop命令失败: {e}", exc_info=True)
            # 只有当前活动服务器出错时才清理全局连接，避免误断其他目标。
            if locals().get('target_is_active', True):
                await self._thorough_shutdown()
            error_msg = f"停止服务器失败: {e}"
            if kwargs.get('from_console', False):
                print(error_msg)
            return error_msg
        
        finally:
            self._stopping_servers.discard(stop_key)
            self._is_stopping = bool(self._stopping_servers)

    async def _thorough_shutdown(self):
        """彻底关闭所有连接"""
        self.logger.info("执行彻底关闭操作...")
        
        # 第一步：通过连接管理器设置关闭模式
        if getattr(self.qq_server, 'connection_manager', None):
            await self.qq_server.connection_manager.set_shutdown_mode()
        
        # 第二步：强制关闭MSMP连接
        if self.msmp_client:
            try:
                if hasattr(self.msmp_client, 'close_sync'):
                    await asyncio.to_thread(self.msmp_client.close_sync)
                elif hasattr(self.msmp_client, 'close'):
                    await asyncio.wait_for(self.msmp_client.close(), timeout=3.0)
                self.logger.info("MSMP连接已强制关闭")
            except Exception as e:
                self.logger.debug(f"强制关闭MSMP连接时出错: {e}")
        
        # 第三步：强制关闭RCON连接
        if self.rcon_client:
            try:
                if hasattr(self.rcon_client, 'close'):
                    self.rcon_client.close()
                self.logger.info("RCON连接已强制关闭")
            except Exception as e:
                self.logger.debug(f"强制关闭RCON连接时出错: {e}")
        
        # 第四步：关闭日志文件
        if self.qq_server:
            try:
                self.qq_server._close_log_file()
                self.logger.info("服务器日志文件已关闭")
            except Exception as e:
                self.logger.debug(f"关闭日志文件出错: {e}")
        
        self._shutdown_initiated = True
        self.logger.info("彻底关闭操作完成")

    async def _immediate_shutdown(self):
        """立即关闭所有连接（用于kill命令）"""
        await self._unified_shutdown(immediate=True)

    async def _close_all_connections(self):
        """关闭所有连接（用于正常停止）"""
        await self._unified_shutdown(immediate=False)

    async def _unified_shutdown(self, immediate: bool = False):
        """统一的关闭方法"""
        if hasattr(self, '_shutdown_initiated') and self._shutdown_initiated:
            self.logger.debug("关闭模式已设置，跳过重复操作")
            return
        
        self.logger.info("执行统一关闭操作...")
        
        # 设置关闭标志
        self._is_stopping = True
        
        # 第一步：通过连接管理器设置关闭模式，这会停止所有重连
        if getattr(self.qq_server, 'connection_manager', None):
            await self.qq_server.connection_manager.set_shutdown_mode()
        
        # 第二步：如果是立即关闭，强制关闭连接
        if immediate:
            if self.msmp_client and hasattr(self.msmp_client, 'close'):
                try:
                    await asyncio.wait_for(self.msmp_client.close(), timeout=3.0)
                except Exception as e:
                    self.logger.debug(f"立即关闭MSMP连接时出错: {e}")
            
            if self.rcon_client and hasattr(self.rcon_client, 'close'):
                try:
                    self.rcon_client.close()
                except Exception as e:
                    self.logger.debug(f"立即关闭RCON连接时出错: {e}")
        
        # 第三步：关闭日志文件
        if self.qq_server and immediate:
            try:
                self.qq_server._close_log_file()
            except Exception as e:
                self.logger.debug(f"关闭日志文件出错: {e}")
        
        self._shutdown_initiated = True
        self.logger.info("统一关闭操作完成")
    
    async def _reset_shutdown_mode(self):
        """重置关闭模式"""
        self.logger.info("开始重置关闭模式...")
        
        # 重置命令处理器的关闭标志
        self._is_stopping = False
        self._stopping_servers.clear()
        if hasattr(self, '_shutdown_event'):
            self._shutdown_event.clear()
        
        # 重置连接管理器的关闭模式
        if getattr(self.qq_server, 'connection_manager', None):
            await self.qq_server.connection_manager.reset_shutdown_mode()
        
        # 重置MSMP客户端的关闭模式
        if self.msmp_client and hasattr(self.msmp_client, 'set_shutdown_mode'):
            # 如果MSMP客户端有重置方法，调用它
            if hasattr(self.msmp_client, 'reset_shutdown_mode'):
                self.msmp_client.reset_shutdown_mode()
            else:
                # 否则手动重置相关标志
                if hasattr(self.msmp_client, '_shutdown_mode'):
                    self.msmp_client._shutdown_mode = False
        
        # 重置RCON客户端状态
        if self.rcon_client:
            # 确保RCON客户端处于可重连状态
            if hasattr(self.rcon_client, 'authenticated'):
                self.rcon_client.authenticated = False
            if hasattr(self.rcon_client, 'socket') and self.rcon_client.socket:
                try:
                    self.rcon_client.socket.close()
                except:
                    pass
                self.rcon_client.socket = None
        
        # 清空连接缓存
        if getattr(self.qq_server, 'connection_manager', None):
            await self.qq_server.connection_manager.invalidate_all_caches()
        
        # 重置关闭标志
        if hasattr(self, '_connections_closed'):
            self._connections_closed = False
        if hasattr(self, '_shutdown_initiated'):
            self._shutdown_initiated = False
        
        self.logger.info("关闭模式已完全重置")
    
    async def handle_start(self, user_id: int, group_id: int, websocket, is_private: bool = False, **kwargs) -> str:
        """处理start命令(管理员) - 支持MSMP和RCON"""
        try:
            server_selector = str(kwargs.get('target_server_selector', '') or '').strip()
            server_config = kwargs.get('target_server') or self.config_manager.resolve_server(server_selector)
            if not server_config:
                return f"未找到服务器: {server_selector}"

            target_process = self.qq_server.get_server_process(server_config) if hasattr(self.qq_server, 'get_server_process') else self.qq_server.server_process
            if target_process and target_process.poll() is None:
                return "服务器已经在启动或运行中"

            target_msmp = kwargs.get('target_msmp_client') or kwargs.get('msmp_client')
            target_rcon = kwargs.get('target_rcon_client') or kwargs.get('rcon_client')
            if target_msmp:
                try:
                    if await asyncio.to_thread(target_msmp.is_connected):
                        return "服务器已通过外部接入运行 (MSMP)，无需启动本地进程"
                except Exception:
                    pass
            if target_rcon:
                try:
                    if await asyncio.to_thread(target_rcon.is_connected):
                        return "服务器已通过外部接入运行 (RCON)，无需启动本地进程"
                except Exception:
                    pass

            # 确实需要启动本地进程时，再重置关闭模式，允许重新连接。
            await self._reset_shutdown_mode()

            server_section = server_config.get('server', {}) if isinstance(server_config.get('server'), dict) else {}
            if self.qq_server and hasattr(self.qq_server, '_server_script_paths'):
                start_script, _working_dir = self.qq_server._server_script_paths(server_config)
            else:
                start_script = str(server_section.get('start_script') or '').strip()
                if start_script and not os.path.isabs(start_script):
                    start_script = os.path.abspath(start_script)
            if not start_script:
                return (
                    "服务器启动脚本未配置\n"
                    "请在对应 servers/*.yml 中配置 server.start_script"
                )
            
            if not os.path.exists(start_script):
                return f"启动脚本不存在: {start_script}"
            
            started = await self.qq_server._start_server_process(
                websocket,
                group_id,
                private_user_id=user_id if is_private else None,
                server_config=server_config
            )
            if not started:
                return None
            
            connection_info = []
            if (server_config.get('msmp') or {}).get('enabled', False):
                connection_info.append("MSMP")
            if (server_config.get('rcon') or {}).get('enabled', False):
                connection_info.append("RCON")
            
            if connection_info and self.qq_server._websocket_open(websocket):
                server_name = server_config.get('name', '默认服务器')
                info_msg = f"正在启动 {server_name}...启动后，将自动尝试连接: {', '.join(connection_info)}"
                if is_private:
                    await self.qq_server.send_private_message(websocket, user_id, info_msg)
                else:
                    await self.qq_server.send_group_message(websocket, group_id, info_msg)
            
            return None
            
        except Exception as e:
            self.logger.error(f"执行start命令失败: {e}")
            return f"启动服务器失败: {e}"

    async def handle_mc(self, command_text: str = "", **kwargs) -> str:
        """向 Bot 托管的目标 MC 服务端 stdin 写入控制台命令。"""
        command = str(command_text or "").strip()
        if not command:
            return "用法: mc <服务器编号或名称> <MC控制台命令>"
        if not self.qq_server or not hasattr(self.qq_server, 'send_server_stdin'):
            return "Bot托管控制台不可用"
        target_server = kwargs.get('target_server') or {}
        return await self.qq_server.send_server_stdin(command, target_server)
    
    async def handle_reload(self, user_id: int, **kwargs) -> str:
        """处理reload命令(管理员)"""
        try:
            self.config_manager.reload()
            return "配置已重新加载"
        except Exception as e:
            self.logger.error(f"重新加载配置失败: {e}")
            return f"重新加载配置失败: {e}"

    async def handle_log(self, user_id: int, **kwargs) -> str:
        """处理log命令 - 显示最近的服务器日志"""
        try:
            target_server = kwargs.get('target_server') or getattr(self.qq_server, 'active_server_config', None) or {}
            target_process = (
                self.qq_server.get_server_process(target_server)
                if hasattr(self.qq_server, 'get_server_process') and target_server
                else self.qq_server.server_process
            )
            if not target_process and not (hasattr(self.qq_server, '_runtime_for_server') and self.qq_server._runtime_for_server(target_server)):
                return "该服务器没有Bot托管日志，外部接入服务器请查看对应服务端控制台"

            server_running = target_process and target_process.poll() is None
            recent_logs = self.qq_server.get_recent_logs(20, target_server)
            
            if not recent_logs:
                return "暂无服务器日志输出"
            
            status = "运行中" if server_running else "已停止"
            lines = [f"最近 {len(recent_logs)} 条服务器日志 (服务器{status}):"]
            lines.append("▬" * 20)
            
            # 不截断日志，完整显示
            for i, log in enumerate(recent_logs, 1):
                lines.append(f"{i}. {log}")
            
            lines.append("▬" * 20)
            if server_running:
                lines.append("提示: 日志实时更新，再次发送 log 查看最新日志")
            else:
                lines.append("提示: 服务器已停止，日志不再更新")
            
            # 分页发送（避免消息过长）
            message = "\n".join(lines)
            
            # 如果消息过长，分多条发送
            max_length = self._target_advanced_config(target_server, 'max_message_length', self.config_manager.get_max_message_length()) if self.config_manager else 2500
            
            if len(message) > max_length:
                # 分页显示
                pages = []
                current_page = []
                current_length = 0
                
                for line in lines:
                    line_length = len(line) + 1  # +1 for newline
                    if current_length + line_length > max_length and current_page:
                        pages.append("\n".join(current_page))
                        current_page = [line]
                        current_length = line_length
                    else:
                        current_page.append(line)
                        current_length += line_length
                
                if current_page:
                    pages.append("\n".join(current_page))
                
                # 返回第一页，其他页面需要通过多次调用显示
                return pages[0] + f"\n\n[第 1/{len(pages)} 页，共 {len(recent_logs)} 条日志]"
            else:
                return message
            
        except Exception as e:
            self.logger.error(f"执行log命令失败: {e}", exc_info=True)
            return f"获取日志失败: {e}"

    async def handle_reconnect(self, user_id: int, group_id: int, websocket, is_private: bool = False, **kwargs) -> str:
        """处理reconnect命令 - 手动重连服务器"""
        try:
            target_server = kwargs.get('target_server') or {}
            target_is_active = self._target_is_active_server(target_server)
            if websocket and is_private:
                await self.qq_server.send_private_message(websocket, user_id, "正在尝试重新连接服务器...")
            elif websocket:
                await self.qq_server.send_group_message(websocket, group_id, "正在尝试重新连接服务器...")

            target_msmp = kwargs.get('target_msmp_client') or kwargs.get('msmp_client')
            target_rcon = kwargs.get('target_rcon_client') or kwargs.get('rcon_client')
            if target_msmp or target_rcon:
                results = {
                    'msmp': await asyncio.to_thread(target_msmp.is_connected) if target_msmp else False,
                    'rcon': await asyncio.to_thread(target_rcon.is_connected) if target_rcon else False,
                }
            elif target_is_active:
                results = await self.qq_server.connection_manager.reconnect_all()
            else:
                return "目标服务器连接未就绪，请检查该服务器的 MSMP/RCON 配置"
            
            message_lines = ["重连结果:", "■■■■■■■■■■■■■■"]
            
            if results.get('msmp'):
                message_lines.append("MSMP: 连接成功")
            else:
                message_lines.append("MSMP: 连接失败")
            
            if results.get('rcon'):
                message_lines.append("RCON: 连接成功")
            else:
                message_lines.append("RCON: 连接失败")
                        
            return "\n".join(message_lines)
            
        except Exception as e:
            self.logger.error(f"执行reconnect命令失败: {e}", exc_info=True)
            return f"重连服务器失败: {e}"

    async def handle_reconnect_msmp(self, user_id: int, group_id: int, websocket, is_private: bool = False, **kwargs) -> str:
        """处理reconnect_msmp命令 - 手动重连MSMP"""
        try:
            target_server = kwargs.get('target_server') or getattr(self.qq_server, 'active_server_config', None) or {}
            target_is_active = self._target_is_active_server(target_server)
            if not self._target_protocol_enabled(target_server, 'msmp'):
                return "MSMP未启用，无法重连"
            
            if websocket and is_private:
                await self.qq_server.send_private_message(websocket, user_id, "正在重连MSMP服务器...")
            elif websocket:
                await self.qq_server.send_group_message(websocket, group_id, "正在重连MSMP服务器...")

            target_msmp = kwargs.get('target_msmp_client') or kwargs.get('msmp_client')
            if target_msmp:
                success = await asyncio.to_thread(target_msmp.is_connected)
            elif target_is_active:
                success = await self.qq_server.connection_manager.reconnect_msmp()
            else:
                return "目标服务器MSMP连接未就绪，请检查该服务器MSMP配置"
            
            if success:
                return "MSMP重连成功"
            else:
                return "MSMP重连失败"
            
        except Exception as e:
            self.logger.error(f"执行reconnect_msmp命令失败: {e}", exc_info=True)
            return f"重连MSMP失败: {e}"

    async def handle_reconnect_rcon(self, user_id: int, group_id: int, websocket, is_private: bool = False, **kwargs) -> str:
        """处理reconnect_rcon命令 - 手动重连RCON"""
        try:
            target_server = kwargs.get('target_server') or getattr(self.qq_server, 'active_server_config', None) or {}
            target_is_active = self._target_is_active_server(target_server)
            if not self._target_protocol_enabled(target_server, 'rcon'):
                return "RCON未启用，无法重连"
            
            if websocket and is_private:
                await self.qq_server.send_private_message(websocket, user_id, "正在重连RCON服务器...")
            elif websocket:
                await self.qq_server.send_group_message(websocket, group_id, "正在重连RCON服务器...")

            target_rcon = kwargs.get('target_rcon_client') or kwargs.get('rcon_client')
            if target_rcon:
                success = await asyncio.to_thread(target_rcon.is_connected)
            elif target_is_active:
                success = await self.qq_server.connection_manager.reconnect_rcon()
            else:
                return "目标服务器RCON连接未就绪，请检查该服务器RCON配置"
            
            if success:
                return "RCON重连成功"
            else:
                return "RCON重连失败"
            
        except Exception as e:
            self.logger.error(f"执行reconnect_rcon命令失败: {e}", exc_info=True)
            return f"重连RCON失败: {e}"

    async def handle_kill(self, user_id: int, group_id: int, websocket, is_private: bool = False, **kwargs) -> str:
        """处理kill命令(管理员) - 强制杀死服务器进程"""
        target_server = kwargs.get('target_server') or {}
        return await self._execute_kill_command(user_id, group_id, websocket, is_private, target_server=target_server)

    @staticmethod
    def _same_server_config(left: Dict[str, Any], right: Dict[str, Any]) -> bool:
        left_key = (left or {}).get('_config_file') or (left or {}).get('name')
        right_key = (right or {}).get('_config_file') or (right or {}).get('name')
        return bool(left_key and right_key and str(left_key).lower() == str(right_key).lower())
    
    async def _execute_kill_command(self, user_id: int = 0, group_id: int = 0, websocket = None, is_private: bool = False, target_server: Optional[Dict[str, Any]] = None) -> str:
        """通用的kill命令执行方法"""
        try:
            target_process = (
                self.qq_server.get_server_process(target_server)
                if self.qq_server and hasattr(self.qq_server, 'get_server_process') and target_server
                else (self.qq_server.server_process if self.qq_server else None)
            )
            if not self.qq_server or not target_process:
                return "服务器进程未运行"
            
            if target_process.poll() is not None:
                return "服务器进程已经停止"
            
            # ============ 触发服务器停止事件 ============
            if hasattr(self.qq_server, 'plugin_manager') and self.qq_server.plugin_manager:
                self.logger.info("触发 server_stopping 事件给所有插件 (kill命令)")
                event_server = target_server or getattr(self.qq_server, 'active_server_config', None) or {}
                await self.qq_server.plugin_manager.trigger_event(
                    "server_stopping",
                    target_server=event_server,
                    target_server_name=event_server.get('name', '')
                )
            # ============ 事件触发结束 ============
            
            # 设置手动kill标记
            if target_server and hasattr(self.qq_server, 'set_server_manual_kill'):
                self.qq_server.set_server_manual_kill(target_server, True)
            else:
                self.qq_server._manual_kill = True

            active_server = getattr(self.qq_server, 'active_server_config', None) or {}
            if not target_server or self._same_server_config(target_server, active_server):
                # 只关闭当前全局连接绑定服务器，避免 kill 2 误断 server1。
                await self._immediate_shutdown()
                        
            import signal
            import subprocess
            
            try:
                pid = target_process.pid
                self.logger.info(f"强制终止进程 {pid}")
                
                if os.name == 'nt':
                    try:
                        result = subprocess.run(
                            ['taskkill', '/F', '/T', '/PID', str(pid)], 
                            timeout=10, 
                            capture_output=True, 
                            text=True
                        )
                        if result.returncode == 0:
                            self.logger.info(f"已强制终止进程树 {pid}")
                        else:
                            self.logger.warning(f"taskkill 返回非零状态: {result.returncode}")
                            os.kill(pid, signal.SIGTERM)
                    except subprocess.TimeoutExpired:
                        self.logger.warning("taskkill 超时，尝试其他方法")
                        os.kill(pid, signal.SIGTERM)
                    except Exception as e:
                        self.logger.warning(f"taskkill 失败: {e}")
                        os.kill(pid, signal.SIGTERM)
                else:
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
                    
            except ProcessLookupError:
                self.logger.info("进程已不存在")
            except Exception as e:
                self.logger.error(f"强制中止进程失败: {e}")
                return f"强制中止失败: {e}"
            
            try:
                await asyncio.wait_for(
                    asyncio.get_event_loop().run_in_executor(
                        None,
                        target_process.wait
                    ),
                    timeout=10.0
                )
                self.logger.info("进程已确认终止")
            except asyncio.TimeoutError:
                self.logger.warning("进程在10秒内未响应，尝试强制杀死")
                try:
                    if os.name == 'nt':
                        os.kill(pid, signal.SIGKILL)
                    else:
                        os.kill(pid, signal.SIGKILL)
                except:
                    pass
            
            # 等待日志采集完成
            await asyncio.sleep(2)
            
            await self._thorough_cleanup(target_server or getattr(self.qq_server, 'active_server_config', None) or {})
            
            if target_server and hasattr(self.qq_server, '_runtime_for_server'):
                runtime = self.qq_server._runtime_for_server(target_server)
                if runtime:
                    runtime.process = None
                    self.qq_server._close_log_file(runtime)
                    self.qq_server._set_active_runtime(None)
            else:
                self.qq_server.server_process = None
                self.qq_server._close_log_file()
            
            return "服务器进程已强制中止"
            
        except Exception as e:
            self.logger.error(f"执行kill命令失败: {e}", exc_info=True)
            return f"强制中止失败: {e}"

    async def _thorough_cleanup(self, server_config: Optional[Dict[str, Any]] = None):
        """彻底清理所有残留"""
        try:
            self.logger.info("开始彻底清理残留资源...")
            
            await self._force_clean_file_locks(server_config)
            await asyncio.sleep(3)
            
            self.logger.info("彻底清理完成")
            
        except Exception as e:
            self.logger.error(f"彻底清理失败: {e}")

    async def _force_clean_file_locks(self, server_config: Optional[Dict[str, Any]] = None):
        """强制清理文件锁"""
        try:
            working_dir = self._target_working_dir(server_config or getattr(self.qq_server, 'active_server_config', None) or {})
            if not working_dir:
                self.logger.warning("无法清理文件锁：目标服务器未配置工作目录或启动脚本")
                return
                
            import time
            
            files_to_clean = [
                os.path.join(working_dir, "session.lock"),
                os.path.join(working_dir, "world", "session.lock"),
                os.path.join(working_dir, "world_nether", "session.lock"),
                os.path.join(working_dir, "world_the_end", "session.lock"),
            ]
            
            for file_path in files_to_clean:
                if os.path.exists(file_path):
                    try:
                        for attempt in range(3):
                            try:
                                os.remove(file_path)
                                self.logger.info(f"已删除: {file_path}")
                                break
                            except Exception:
                                if attempt < 2:
                                    await asyncio.sleep(1)
                                else:
                                    raise
                    except Exception as e:
                        self.logger.warning(f"无法删除 {file_path}: {e}")
                        
        except Exception as e:
            self.logger.error(f"强制清理文件锁失败: {e}")

    async def handle_crash(self, user_id: int, group_id: int, websocket, is_private: bool = False, **kwargs) -> str:
        """处理crash命令(管理员) - 获取最新的崩溃报告"""
        try:
            import os
            from pathlib import Path
            
            target_server = kwargs.get('target_server') or getattr(self.qq_server, 'active_server_config', None) or {}
            working_dir = self._target_working_dir(target_server)
            if not working_dir:
                return "目标服务器未配置 server.working_directory 或 server.start_script"
            
            crash_dir = os.path.join(working_dir, "crash-reports")
            crash_path = Path(crash_dir)
            
            if not crash_path.exists():
                return f"crash-reports 目录不存在: {crash_dir}"
            
            crash_files = list(crash_path.glob("crash-*.txt"))
            
            if not crash_files:
                return "未找到任何崩溃报告"
            
            latest_crash = max(crash_files, key=lambda p: p.stat().st_mtime)
            
            self.logger.info(f"找到最新崩溃报告: {latest_crash.name}")
            
            await self.qq_server._send_crash_report_file(websocket, user_id, group_id, str(latest_crash), is_private)
            
            return None
            
        except Exception as e:
            self.logger.error(f"处理崩溃报告失败: {e}", exc_info=True)
            return f"处理崩溃报告失败: {e}"

    def _target_advanced_config(self, target_server: Dict[str, Any], key: str, default: Any) -> Any:
        advanced = (target_server or {}).get('advanced') or {}
        return advanced.get(key, default)

    def _target_working_dir(self, target_server: Dict[str, Any]) -> str:
        if self.config_manager and hasattr(self.config_manager, 'get_server_working_directory'):
            return self.config_manager.get_server_working_directory(target_server)
        server_section = (target_server or {}).get('server') or {}
        working_dir = str(server_section.get('working_directory') or '').strip().replace('\\', os.sep).replace('/', os.sep)
        if working_dir:
            return working_dir if os.path.isabs(working_dir) else os.path.abspath(working_dir)
        start_script = str(server_section.get('start_script') or '').strip().replace('\\', os.sep).replace('/', os.sep)
        if start_script and not os.path.isabs(start_script):
            start_script = os.path.abspath(start_script)
        return os.path.dirname(start_script) if start_script else ''

    async def handle_listeners(self, **kwargs) -> str:
        """处理 listeners 命令 - 显示所有自定义消息监听规则"""
        try:
            if not self.qq_server.custom_listener:
                return "自定义消息监听器未初始化"
            
            return self.qq_server.custom_listener.get_rules_info()
            
        except Exception as e:
            self.logger.error(f"执行 listeners 命令失败: {e}", exc_info=True)
            return f"获取监听规则失败: {e}"

    async def handle_sysinfo(self, **kwargs) -> str:
        """处理sysinfo命令 - 显示系统信息"""
        try:
            from system_monitor import SystemMonitor
            
            monitor = SystemMonitor(self.logger)
            stats = monitor.get_system_stats()
            
            if stats:
                return monitor.format_system_info(stats)
            else:
                return "无法获取系统信息"
                
        except ImportError:
            return "系统监控模块未安装,请先安装 psutil: pip install psutil"
        except Exception as e:
            self.logger.error(f"执行sysinfo命令失败: {e}", exc_info=True)
            return f"获取系统信息失败: {e}"

    async def handle_disk(self, **kwargs) -> str:
        """处理disk命令 - 显示磁盘信息"""
        try:
            from system_monitor import SystemMonitor
            
            monitor = SystemMonitor(self.logger)
            return monitor.get_disk_info("/")
            
        except ImportError:
            return "系统监控模块未安装,请先安装 psutil: pip install psutil"
        except Exception as e:
            self.logger.error(f"执行disk命令失败: {e}", exc_info=True)
            return f"获取磁盘信息失败: {e}"

    async def handle_process(self, **kwargs) -> str:
        """处理process命令 - 显示Java进程信息"""
        try:
            from system_monitor import SystemMonitor
            
            monitor = SystemMonitor(self.logger)
            return monitor.get_process_info("java")
            
        except ImportError:
            return "系统监控模块未安装,请先安装 psutil: pip install psutil"
        except Exception as e:
            self.logger.error(f"执行process命令失败: {e}", exc_info=True)
            return f"获取进程信息失败: {e}"

    async def handle_network(self, **kwargs) -> str:
        """处理network命令 - 显示网络信息和实时带宽"""
        try:
            from system_monitor import SystemMonitor
            
            monitor = SystemMonitor(self.logger)
            return monitor.get_network_info()
            
        except ImportError:
            return "系统监控模块未安装,请先安装 psutil: pip install psutil"
        except Exception as e:
            self.logger.error(f"执行network命令失败: {e}", exc_info=True)
            return f"获取网络信息失败: {e}"

    async def handle_plugins(self, command_text: str = "", **kwargs) -> str:
        """处理 plugins 命令 - 显示已加载的插件及其指令，支持查看单个插件详情和分页"""
        try:
            if not self.qq_server or not hasattr(self.qq_server, 'plugin_manager'):
                return "插件管理器未初始化"
            
            plugin_manager = self.qq_server.plugin_manager
            plugins = plugin_manager.plugins
            target_server = kwargs.get('target_server') or {}
            
            if not plugins:
                return "暂无已加载的插件"
            
            # 如果有参数，显示单个插件的详细帮助
            if command_text:
                search_name = command_text.strip()
                
                # 检查是否是页码参数
                if search_name.isdigit():
                    page_num = int(search_name)
                    return await self._get_plugins_page(plugins, page_num, target_server=target_server)
                
                # 使用新的查找方法
                plugin = plugin_manager.find_plugin_by_name(search_name)
                
                if not plugin:
                    # 提供搜索提示
                    hints = plugin_manager.get_plugin_search_hints(search_name)
                    if hints:
                        lines = [f"未找到精确匹配的插件: {search_name}", "您可能想查看:"]
                        lines.extend([f"• {hint}" for hint in hints])
                        return "\n".join(lines)
                    else:
                        available_plugins = []
                        for plugin_name, plugin_obj in plugins.items():
                            available_plugins.append(f"{plugin_obj.name} (文件名: {plugin_name})")
                        
                        return (
                            f"未找到插件: {search_name}\n"
                            f"可用插件: {', '.join(available_plugins)}"
                        )
                
                # 获取插件的实际文件名（用于显示）
                plugin_filename = None
                for filename, plugin_obj in plugins.items():
                    if plugin_obj == plugin:
                        plugin_filename = filename
                        break
                
                # 使用插件的 get_plugin_help 方法
                server_enabled = plugin_manager.is_plugin_enabled_for_server(plugin_filename, target_server) if target_server else True
                server_status_line = ""
                if target_server:
                    server_name = target_server.get('name') or target_server.get('_config_file') or '当前服务器'
                    server_status_line = f"[{server_name}: {'启用' if server_enabled else '禁用'}]"
                if target_server and not server_enabled:
                    return self._format_plugin_basic_info(plugin, plugin_filename, target_server)

                if hasattr(plugin, 'get_plugin_help'):
                    plugin_help = plugin.get_plugin_help()
                    if plugin_help and plugin_help.strip():
                        # 在帮助信息开头添加插件标识
                        help_lines = plugin_help.split('\n')
                        if server_status_line:
                            help_lines.insert(0, server_status_line)
                        if plugin_filename:
                            identifier_line = f"[插件文件: {plugin_filename}]"
                            if help_lines and not help_lines[0].startswith('['):
                                help_lines.insert(0, identifier_line)
                            elif len(help_lines) > 1 and not help_lines[1].startswith('['):
                                help_lines.insert(1, identifier_line)
                        return '\n'.join(help_lines)
                    else:
                        # 如果插件没有提供帮助信息，显示基本信息
                        return self._format_plugin_basic_info(plugin, plugin_filename, target_server)
                else:
                    # 如果插件没有 get_plugin_help 方法，显示基本信息
                    return self._format_plugin_basic_info(plugin, plugin_filename, target_server)
            
            # 没有参数时，显示第一页
            return await self._get_plugins_page(plugins, 1, target_server=target_server)
            
        except Exception as e:
            self.logger.error(f"处理 plugins 命令失败: {e}", exc_info=True)
            return f"获取插件信息失败: {e}"

    async def _get_plugins_page(
        self,
        plugins: Dict,
        page_num: int,
        plugins_per_page: int = 2,
        target_server: Optional[Dict[str, Any]] = None
    ) -> str:
        """获取指定页的插件列表"""
        try:
            # 计算分页信息
            plugin_list = list(plugins.items())
            total_plugins = len(plugin_list)
            total_pages = (total_plugins + plugins_per_page - 1) // plugins_per_page
            
            # 验证页码
            if page_num < 1 or page_num > total_pages:
                return f"页码无效，请输入 1-{total_pages} 之间的数字"
            
            # 计算当前页的起始和结束索引
            start_idx = (page_num - 1) * plugins_per_page
            end_idx = min(start_idx + plugins_per_page, total_plugins)
            
            # 构建页面内容
            lines = [
                f"已加载的插件信息 (第 {page_num}/{total_pages} 页)",
                "=" * 10,
                f"总数: {total_plugins} 个插件\n"
            ]
            
            # 显示当前页的插件
            for i in range(start_idx, end_idx):
                plugin_name, plugin = plugin_list[i]
                
                lines.append(f"【{plugin.name}】v{plugin.version}")
                lines.append(f"文件: {plugin_name}.py")
                lines.append(f"作者: {plugin.author}")
                lines.append(f"说明: {plugin.description}")
                lines.append(f"状态: {'启用' if plugin.enabled else '禁用'}")
                server_enabled = True
                if target_server:
                    server_enabled = self.qq_server.plugin_manager.is_plugin_enabled_for_server(plugin_name, target_server)
                    server_name = target_server.get('name') or target_server.get('_config_file') or '当前服务器'
                    lines.append(f"{server_name}: {'启用' if server_enabled else '禁用'}")
                
                # 显示插件注册的命令
                plugin_cmds = []
                if server_enabled:
                    for cmd_name, cmd_info in self.qq_server.plugin_manager.command_handlers.items():
                        handler = cmd_info.get('handler')
                        # 检查处理器是否属于当前插件
                        if handler and hasattr(handler, '__self__'):
                            if handler.__self__ == plugin:
                                names = cmd_info.get('names', [])
                                description = cmd_info.get('description', '')
                                admin_only = cmd_info.get('admin_only', False)
                                
                                if names:
                                    cmd_line = f"  • {' / '.join(names[:3])}"  # 最多显示3个别名
                                    if description:
                                        cmd_line += f" - {description}"
                                    if admin_only:
                                        cmd_line += " [管理员]"
                                    plugin_cmds.append(cmd_line)
                
                if plugin_cmds:
                    lines.append("插件命令:")
                    lines.extend(plugin_cmds[:5])  # 最多显示5个命令
                
                if i < end_idx - 1:  # 不是最后一个插件时添加分隔线
                    lines.append("-" * 10)
            
            # 添加分页导航
            lines.append("\n" + "=" * 10)
            lines.append("分页导航:")
            
            if total_pages > 1:
                nav_lines = []
                if page_num > 1:
                    nav_lines.append(f"• 上一页: plugins {page_num - 1}")
                if page_num < total_pages:
                    nav_lines.append(f"• 下一页: plugins {page_num + 1}")
                
                if nav_lines:
                    lines.extend(nav_lines)
            
            # 添加使用提示
            lines.append("\n使用提示:")
            lines.append("• 使用 'plugins <插件名>' 查看单个插件的详细帮助")
            lines.append("• 使用 'plugins <页码>' 查看指定页的插件列表")
            lines.append("• 支持使用插件文件名或显示名称进行搜索")
            
            return "\n".join(lines)
            
        except Exception as e:
            self.logger.error(f"生成插件分页失败: {e}", exc_info=True)
            return f"生成插件列表失败: {e}"

    def _format_plugin_basic_info(
        self,
        plugin,
        plugin_filename: str = None,
        target_server: Optional[Dict[str, Any]] = None
    ) -> str:
        """格式化插件基本信息"""
        lines = [
            f"【{plugin.name}】v{plugin.version}",
            f"作者: {plugin.author}",
            f"说明: {plugin.description}",
            f"状态: {'启用' if plugin.enabled else '禁用'}"
        ]
        
        if plugin_filename:
            lines.append(f"文件: {plugin_filename}.py")
        if plugin_filename and target_server:
            server_enabled = self.qq_server.plugin_manager.is_plugin_enabled_for_server(plugin_filename, target_server)
            server_name = target_server.get('name') or target_server.get('_config_file') or '当前服务器'
            lines.append(f"{server_name}: {'启用' if server_enabled else '禁用'}")
        
        lines.extend([
            "",
            "该插件没有提供详细的帮助信息。"
        ])
        
        return "\n".join(lines)
        
    async def handle_reload_plugin(self, user_id: int, command_text: str = "", **kwargs) -> str:
        """处理reload_plugin命令(管理员) - 重新加载插件"""
        try:
            from_console = kwargs.get('from_console', False)
            if not from_console and not self.config_manager.is_server_admin(user_id, kwargs.get('target_server')):
                return "权限不足: 此命令仅限管理员使用"
            
            if not command_text:
                return "用法: reload_plugin <插件名称或显示名称>"
            
            search_name = command_text.strip()
            
            if not self.qq_server.plugin_manager:
                return "插件管理器未初始化"
            
            plugin = self.qq_server.plugin_manager.find_plugin_by_name(search_name)
            if not plugin:
                return f"未找到插件: {search_name}"
            
            # 获取插件的实际文件名（支持子目录）
            plugin_filename = None
            for filename, plugin_obj in self.qq_server.plugin_manager.plugins.items():
                if plugin_obj == plugin:
                    plugin_filename = filename
                    break
            
            if not plugin_filename:
                return f"无法确定插件的文件名: {search_name}"
            
            # 使用插件管理器重新加载
            success = await self.qq_server.plugin_manager.reload_plugin(plugin_filename)
            
            if success:
                return f"插件 '{plugin.name}' (文件: {plugin_filename}) 重载成功"
            else:
                return f"插件 '{plugin.name}' (文件: {plugin_filename}) 重载失败"
                
        except Exception as e:
            self.logger.error(f"执行reload_plugin命令失败: {e}", exc_info=True)
            return f"重载插件失败: {e}"

    async def handle_unload_plugin(self, user_id: int, command_text: str = "", **kwargs) -> str:
        """处理unload_plugin命令(管理员) - 卸载插件"""
        try:
            # 控制台调用时跳过权限检查
            from_console = kwargs.get('from_console', False)
            if not from_console and not self.config_manager.is_server_admin(user_id, kwargs.get('target_server')):
                return "权限不足: 此命令仅限管理员使用"
            
            if not command_text:
                return "用法: unload_plugin <插件名称或显示名称>"
            
            search_name = command_text.strip()
            
            if not self.qq_server.plugin_manager:
                return "插件管理器未初始化"
            
            plugin = self.qq_server.plugin_manager.find_plugin_by_name(search_name)
            if not plugin:
                # 提供搜索提示
                hints = self.qq_server.plugin_manager.get_plugin_search_hints(search_name)
                if hints:
                    lines = [f"未找到精确匹配的插件: {search_name}", "您可能想卸载:"]
                    lines.extend([f"• {hint}" for hint in hints])
                    return "\n".join(lines)
                else:
                    available_plugins = []
                    for plugin_name, plugin_obj in self.qq_server.plugin_manager.plugins.items():
                        available_plugins.append(f"{plugin_obj.name} (文件: {plugin_name})")
                    
                    return (
                        f"未找到插件: {search_name}\n"
                        f"当前已加载的插件: {', '.join(available_plugins)}"
                    )
            
            # 获取插件的实际文件名（支持子目录）
            plugin_filename = None
            for filename, plugin_obj in self.qq_server.plugin_manager.plugins.items():
                if plugin_obj == plugin:
                    plugin_filename = filename
                    break
            
            if not plugin_filename:
                return f"无法确定插件的文件名: {search_name}"

            target_server = kwargs.get('target_server') or {}
            if target_server and hasattr(self.qq_server.plugin_manager, 'set_plugin_enabled_for_server'):
                config_path = self.qq_server.plugin_manager.set_plugin_enabled_for_server(
                    plugin_filename,
                    target_server,
                    False
                )
                server_name = target_server.get('name') or target_server.get('_config_file') or '当前服务器'
                return (
                    f"插件 '{plugin.name}' 已在服务器 {server_name} 禁用\n"
                    f"配置文件: {config_path}"
                )
            
            success = await self.qq_server.plugin_manager.unload_plugin(plugin_filename)
            
            if success:
                return f"插件 '{plugin.name}' (文件: {plugin_filename}) 卸载成功"
            else:
                return f"插件 '{plugin.name}' (文件: {plugin_filename}) 卸载失败"
                
        except Exception as e:
            self.logger.error(f"执行unload_plugin命令失败: {e}", exc_info=True)
            return f"卸载插件失败: {e}"

    async def handle_load_plugin(self, user_id: int, command_text: str = "", **kwargs) -> str:
        """处理load_plugin命令(管理员) - 加载插件"""
        try:
            # 控制台调用时跳过权限检查
            from_console = kwargs.get('from_console', False)
            if not from_console and not self.config_manager.is_server_admin(user_id, kwargs.get('target_server')):
                return "权限不足: 此命令仅限管理员使用"
            
            if not command_text:
                return "用法: load_plugin <插件名称或显示名称>"
            
            search_name = command_text.strip()
            
            # 移除可能的.py后缀
            if search_name.endswith('.py'):
                search_name = search_name[:-3]
            
            if not self.qq_server.plugin_manager:
                return "插件管理器未初始化"
            
            self.logger.info(f"开始加载插件: {search_name}")
            
            # 首先检查插件是否已经加载
            existing_plugin = self.qq_server.plugin_manager.find_plugin_by_name(search_name)
            if existing_plugin:
                # 获取已加载插件的文件名
                existing_filename = None
                for filename, plugin_obj in self.qq_server.plugin_manager.plugins.items():
                    if plugin_obj == existing_plugin:
                        existing_filename = filename
                        break
                
                target_server = kwargs.get('target_server') or {}
                if existing_filename and target_server and hasattr(self.qq_server.plugin_manager, 'set_plugin_enabled_for_server'):
                    config_path = self.qq_server.plugin_manager.set_plugin_enabled_for_server(
                        existing_filename,
                        target_server,
                        True
                    )
                    server_name = target_server.get('name') or target_server.get('_config_file') or '当前服务器'
                    return (
                        f"插件 '{existing_plugin.name}' 已在服务器 {server_name} 启用\n"
                        f"配置文件: {config_path}"
                    )

                if existing_filename:
                    return f"插件 '{existing_plugin.name}' (文件: {existing_filename}) 已经加载"
                else:
                    return f"插件 '{existing_plugin.name}' 已经加载"
            
            plugin_filename = self.qq_server.plugin_manager.find_available_plugin_file(search_name)
            if not plugin_filename:
                available_files = sorted(self.qq_server.plugin_manager.get_available_plugin_files().keys())
                if available_files:
                    return (
                        f"未找到插件文件: {search_name}\n"
                        f"可用的插件文件: {', '.join(available_files)}"
                    )
                return f"未找到插件文件: {search_name}，插件目录为空"
            
            # 尝试加载插件
            success = await self.qq_server.plugin_manager.load_plugin(plugin_filename)
            
            if success:
                # 再次确认插件确实加载成功
                loaded_plugin = self.qq_server.plugin_manager.get_plugin(plugin_filename)
                if loaded_plugin:
                    target_server = kwargs.get('target_server') or {}
                    if target_server and hasattr(self.qq_server.plugin_manager, 'set_plugin_enabled_for_server'):
                        config_path = self.qq_server.plugin_manager.set_plugin_enabled_for_server(
                            plugin_filename,
                            target_server,
                            True
                        )
                        server_name = target_server.get('name') or target_server.get('_config_file') or '当前服务器'
                        return (
                            f"插件 '{loaded_plugin.name}' 已加载并在服务器 {server_name} 启用\n"
                            f"配置文件: {config_path}"
                        )
                    return f"插件 '{loaded_plugin.name}' (文件: {plugin_filename}) 加载成功"
                else:
                    self.logger.warning(f"插件 {plugin_filename} 加载返回成功但未找到插件实例")
                    return f"插件 '{search_name}' 加载状态异常"
            else:
                # 检查插件是否实际上加载成功了（处理异步加载的情况）
                loaded_plugin = self.qq_server.plugin_manager.get_plugin(plugin_filename)
                if loaded_plugin:
                    self.logger.warning(f"插件 {plugin_filename} 加载返回失败但实际已加载")
                    return f"插件 '{loaded_plugin.name}' (文件: {plugin_filename}) 已加载（状态报告异常）"
                else:
                    return f"插件 '{search_name}' 加载失败，请检查插件文件是否正确"
                        
        except Exception as e:
            self.logger.error(f"执行load_plugin命令失败: {e}", exc_info=True)
            return f"加载插件失败: {e}"
