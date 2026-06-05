import logging
import sys
import signal
import time
import os
import asyncio
from pathlib import Path
from concurrent.futures import CancelledError as FutureCancelledError
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler
from config_manager import ConfigManager, ConfigValidationError
from msmp_client import ServerEventListener
from qq_bot_server import QQBotWebSocketServer
from connection_manager import ConnectionManager, ConnectionStatus
from log_system import LogManager, AdvancedLogFilter, LogArchiveManager
from plugin_manager import PluginManager
from version import APP_NAME, APP_VERSION


BASE_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent


def _configure_standard_streams():
    """确保 GUI 管道读取中文输出时编码一致。"""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
            except Exception:
                pass


class LogFilter(logging.Filter):
    """自定义日志过滤器 - 支持动态启用/禁用特定日志"""
    
    def __init__(self):
        super().__init__()
        self.disabled_loggers = set()
        self.disabled_keywords = set()
    
    def filter(self, record: logging.LogRecord) -> bool:
        """过滤日志记录"""
        if record.name in self.disabled_loggers:
            return False
        
        message = record.getMessage()
        for keyword in self.disabled_keywords:
            if keyword in message:
                return False
        
        return True
    
    def disable_logger(self, logger_name: str):
        """禁用特定记录器"""
        self.disabled_loggers.add(logger_name)
    
    def enable_logger(self, logger_name: str):
        """启用特定记录器"""
        self.disabled_loggers.discard(logger_name)
    
    def disable_keyword(self, keyword: str):
        """禁用包含特定关键字的日志"""
        self.disabled_keywords.add(keyword)
    
    def enable_keyword(self, keyword: str):
        """启用包含特定关键字的日志"""
        self.disabled_keywords.discard(keyword)
    
    def is_logger_disabled(self, logger_name: str) -> bool:
        """检查记录器是否禁用"""
        return logger_name in self.disabled_loggers
    
    def is_keyword_disabled(self, keyword: str) -> bool:
        """检查关键字是否禁用"""
        return keyword in self.disabled_keywords
    
    def get_status(self) -> dict:
        """获取过滤器状态"""
        return {
            'disabled_loggers': list(self.disabled_loggers),
            'disabled_keywords': list(self.disabled_keywords),
            'global_level': 'DEBUG'
        }


class ConsoleCommandHandler:
    """统一的控制台命令处理器"""
    def __init__(self, bot_instance, logger):
        self.bot = bot_instance
        self.logger = logger
        self.running = True

    async def handle_console_input(self):
        """统一处理控制台输入"""
        import asyncio
        
        print("\n" + "="*60)
        print("控制台已就绪，可以输入命令")
        print("输入 #help 查看系统命令列表")
        print("="*60 + "\n")
        
        # 使用 asyncio 的异步标准输入
        loop = asyncio.get_event_loop()
        
        while self.running and getattr(self.bot, 'running', True):
            try:
                # 使用异步方式读取输入，避免阻塞
                line = await loop.run_in_executor(None, sys.stdin.readline)
                
                if not line:  # EOF 或空输入
                    self.logger.info("检测到输入流结束")
                    break
                    
                line = line.strip()
                if not line:
                    continue
                
                # 首先检查是否是stop命令
                if line.lower() == 'stop':
                    result = await self._handle_console_stop()
                    print(result)
                    continue
                
                # 处理系统命令（带#前缀的）
                if await self._handle_bot_command(line):
                    continue
                
                # 其他命令转发到Minecraft服务器
                await self._forward_to_minecraft(line)
                    
            except EOFError:
                self.logger.warning("输入流已关闭")
                break
            except Exception as e:
                self.logger.error(f"处理控制台输入失败: {e}")
                await asyncio.sleep(1)
    
    async def _handle_bot_command(self, command: str) -> bool:
        """处理Bot系统命令"""
        if not command.startswith('#'):
            return False
        
        raw_system_command = command[1:].strip()
        system_command = raw_system_command.lower()
        
        # === 插件管理命令 ===
        if system_command.startswith('reload_plugin '):
            plugin_name = raw_system_command[14:].strip()
            if plugin_name:
                result = await self._handle_qq_command(f'reload_plugin {plugin_name}')
                print(result)
            else:
                print("用法: #reload_plugin <插件名称>")
            return True

        elif system_command.startswith('unload_plugin '):
            plugin_name = raw_system_command[14:].strip()
            if plugin_name:
                result = await self._handle_qq_command(f'unload_plugin {plugin_name}')
                print(result)
            else:
                print("用法: #unload_plugin <插件名称>")
            return True

        elif system_command.startswith('load_plugin '):
            plugin_name = raw_system_command[12:].strip()
            if plugin_name:
                result = await self._handle_qq_command(f'load_plugin {plugin_name}')
                print(result)
            else:
                print("用法: #load_plugin <插件名称>")
            return True

        elif system_command.split(maxsplit=1)[0] == 'plugins':
            result = await self._handle_qq_command(raw_system_command)
            print(result)
            return True

        elif system_command.split(maxsplit=1)[0] in {'mc', 'command'}:
            result = await self._handle_mc_console_command(raw_system_command)
            print(result)
            return True
        
        # === 日志管理命令 ===
        if system_command == 'log_status':
            status = self.bot.log_manager.get_log_status()
            print("\n日志状态信息:")
            print("=" * 50)
            print(f"MC服务端日志: {status['mc_server_log']}")
            print(f"MSMP_QQBot日志: {status['bot_log']}")
            print(f"全局级别: {status['global_level']}")
            
            if status['disabled_keywords']:
                print(f"\n禁用的关键词 ({len(status['disabled_keywords'])} 个):")
                for keyword in sorted(status['disabled_keywords']):
                    print(f"  - {keyword}")
            else:
                print("\n无禁用的关键词")
            
            if status['disabled_loggers']:
                print(f"\n禁用的Logger ({len(status['disabled_loggers'])} 个):")
                for logger_name in sorted(status['disabled_loggers']):
                    print(f"  - {logger_name}")
            else:
                print("\n无禁用的Logger")
            print("=" * 50)
            return True
        
        elif system_command == 'toggle_mc_log':
            enabled = self.bot.log_manager.toggle_mc_server_log()
            print(f"MC服务端日志已{'启用' if enabled else '禁用'}")
            return True
        
        elif system_command == 'toggle_bot_log':
            enabled = self.bot.log_manager.toggle_bot_log()
            print(f"MSMP_QQBot日志已{'启用' if enabled else '禁用'}")
            return True
        
        elif system_command.startswith('mute_log '):
            keyword = raw_system_command[9:].strip()
            if keyword:
                success = self.bot.log_manager.mute_keyword(keyword)
                if success:
                    print(f"已禁用包含 '{keyword}' 的日志")
                else:
                    print(f"关键词 '{keyword}' 已经被禁用")
            else:
                print("用法: #mute_log <关键词>")
            return True
        
        elif system_command.startswith('unmute_log '):
            keyword = raw_system_command[11:].strip()
            if keyword:
                success = self.bot.log_manager.unmute_keyword(keyword)
                if success:
                    print(f"已启用包含 '{keyword}' 的日志")
                else:
                    print(f"关键词 '{keyword}' 未被禁用")
            else:
                print("用法: #unmute_log <关键词>")
            return True
        
        elif system_command == 'logstats':
            print(self.bot.log_manager.get_logs_info())
            return True
        
        # === 日志归档命令 ===
        elif system_command == 'archive_logs':
            try:
                result = await self.bot.log_manager.archive_logs()
                if result:
                    print(f"日志归档完成")
                    print(f"  压缩: {result['compressed']} 个文件")
                    print(f"  归档: {result['archived']} 个文件")
                    print(f"  删除: {result['deleted']} 个文件")
                else:
                    print("日志归档失败")
            except Exception as e:
                print(f"归档出错: {e}")
            return True

        elif system_command == 'archive_stats':
            stats = self.bot.log_manager.get_archive_stats()
            print("日志归档统计:")
            print(f"  总文件数: {stats['total_files']}")
            print(f"  总大小: {stats['total_size_mb']:.2f} MB")
            print(f"  压缩大小: {stats['compressed_size_mb']:.2f} MB")
            print("\n按日期统计:")
            for date, info in sorted(stats['by_date'].items(), reverse=True)[:10]:
                print(f"  {date}: {info['count']} 文件, {info['size_mb']:.2f} MB")
            return True

        # === 连接管理命令 ===
        elif system_command == 'reconnect':
            result = await self._handle_qq_command(raw_system_command)
            print(result)
            return True
        
        elif system_command == 'reconnect_msmp':
            result = await self._handle_qq_command(raw_system_command)
            print(result)
            return True
        
        elif system_command == 'reconnect_rcon':
            result = await self._handle_qq_command(raw_system_command)
            print(result)
            return True
        
        elif system_command == 'connection status':
            if hasattr(self.bot.qq_server, 'connection_manager'):
                status = await self.bot.qq_server.connection_manager.get_connection_status()
                print("连接管理器状态:")
                print(f"  MSMP: {'启用' if status['msmp_enabled'] else '禁用'} - {'已连接' if status['msmp_connected'] else '未连接'}")
                print(f"  RCON: {'启用' if status['rcon_enabled'] else '禁用'} - {'已连接' if status['rcon_connected'] else '未连接'}")
                print(f"  关闭模式: {'是' if status['shutdown_mode'] else '否'}")
                print(f"  缓存TTL: {status['cache_ttl']} 秒")
                print(f"  缓存条目: {status['cache_size']}")
            else:
                print("连接管理器未初始化")
            return True
    
        # === 其他系统命令 ===
        elif system_command == 'status':
            print(await self._get_connection_status())
        elif system_command.startswith('status '):
            result = await self._handle_qq_command(raw_system_command)
            print(result)
        elif system_command == 'exit':
            self.running = False
            self.bot.running = False
            print("正在退出程序...")
        elif system_command == 'help':
            self._print_console_help()
        elif system_command == 'reload':
            try:
                if self.bot.config_manager.reload():
                    print("配置已重新加载")
                else:
                    print("配置重新加载失败")
            except Exception as e:
                print(f"配置重新加载失败: {e}")
        elif system_command == 'logs':
            print(self.bot.log_manager.get_logs_info())
        elif system_command.split(maxsplit=1)[0] in {
            'list', 'tps', 'rules', 'sysinfo', 'disk', 'process', 'network', 'listeners',
            'start', 'stop', 'kill', 'reconnect', 'reconnect_msmp', 'reconnect_rcon',
            'log', 'crash', 'plugins', 'load_plugin', 'unload_plugin', 'reload_plugin',
        }:
            result = await self._handle_qq_command(raw_system_command)
            print(result)
        elif system_command == 'server_status' or system_command.startswith('server_status '):
            selector = raw_system_command.split(maxsplit=1)[1].strip() if len(raw_system_command.split(maxsplit=1)) > 1 else ""
            result = await self._get_server_process_status(selector)
            print(result)
        else:
            print(f"未知的系统命令: #{raw_system_command}，输入 #help 查看帮助")
        
        return True

    async def _handle_console_start(self) -> str:
        """兼容旧入口，统一走多服务器命令处理。"""
        return await self._handle_qq_command("start")

    async def _handle_console_stop(self) -> str:
        """兼容旧入口，统一走多服务器命令处理。"""
        try:
            if not self.bot.qq_server or not self.bot.qq_server.command_handlers:
                return "qq_server 或 command_handlers 未初始化"
            return await self._handle_qq_command("stop")
            
        except Exception as e:
            error_msg = f"停止服务器失败: {e}"
            self.logger.error(f"控制台停止服务器失败: {e}", exc_info=True)
            return error_msg

    async def _handle_console_kill(self) -> str:
        """兼容旧入口，统一走多服务器命令处理。"""
        if not self.bot.qq_server or not self.bot.qq_server.command_handlers:
            return "命令处理器未初始化"
        return await self._handle_qq_command("kill")

    async def _get_server_process_status(self, selector: str = "") -> str:
        """获取服务器进程状态"""
        try:
            if not self.bot.qq_server:
                return "QQ服务器未初始化"

            target_server = None
            if selector:
                target_server = self.bot.config_manager.resolve_server(selector)
                if not target_server:
                    return f"未找到服务器: {selector}"

            running_configs = (
                self.bot.qq_server.get_running_server_configs()
                if hasattr(self.bot.qq_server, 'get_running_server_configs')
                else []
            )
            if not selector and len(running_configs) > 1:
                lines = ["当前有多个Bot托管服务器在运行，请指定服务器。", ""]
                for index, server in enumerate(running_configs, 1):
                    name = server.get('name') or server.get('_config_file') or f"server{index}"
                    lines.append(f"{index}. {name}: #server_status {name}")
                return "\n".join(lines)

            server_process = (
                self.bot.qq_server.get_server_process(target_server)
                if target_server and hasattr(self.bot.qq_server, 'get_server_process')
                else self.bot.qq_server.server_process
            )

            if not server_process:
                return "服务器进程状态: 未启动"
            
            return_code = server_process.poll()
            
            if return_code is None:
                lines = [
                    "服务器进程状态: 运行中",
                    f"进程PID: {server_process.pid}",
                    f"日志文件: {self.bot.qq_server.log_file_path}",
                    f"日志行数: {len(self.bot.qq_server.server_logs)}"
                ]
                
                recent_logs = self.bot.qq_server.get_recent_logs(3, target_server)
                if recent_logs:
                    lines.append("最近日志:")
                    for log in recent_logs:
                        lines.append(f"  - {log}")
                
                return "\n".join(lines)
            else:
                return f"服务器进程状态: 已停止 (退出码: {return_code})"
                
        except Exception as e:
            self.logger.error(f"获取服务器进程状态失败: {e}")
            return f"获取状态失败: {e}"
    
    async def _handle_qq_command(self, command: str) -> str:
        """通过QQ命令系统处理命令"""
        try:
            if not self.bot.qq_server or not self.bot.qq_server.command_handler:
                return "命令系统未初始化"
                        
            # 分割命令和参数
            parts = command.split(maxsplit=1)
            base_command = parts[0].lower()
            command_args = parts[1] if len(parts) > 1 else ""
                        
            result = await self.bot.qq_server.command_handler.handle_command(
                command_text=base_command,
                command_args=command_args,
                user_id=0,
                group_id=0,
                websocket=None,
                msmp_client=self.bot.qq_server.msmp_client,
                config_manager=self.bot.config_manager,
                rcon_client=self.bot.qq_server.rcon_client,
                plugin_manager=getattr(self.bot, 'plugin_manager', None),
                connection_manager=getattr(self.bot, 'connection_manager', None),
                is_private=True,
                from_console=True
            )

            known_command = base_command in self.bot.qq_server.command_handler.commands
            known_plugin_command = self._is_registered_plugin_command(base_command)
            if result is None and not known_command and not known_plugin_command:
                return f"命令 '{base_command}' 不可用或未实现"
            return result if result else f"命令 '{base_command}' 已执行"
                
        except Exception as e:
            self.logger.error(f"处理QQ命令失败: {e}")
            return f"命令执行失败: {e}"

    async def _handle_mc_console_command(self, command: str) -> str:
        """处理 #mc/#command <服务器> <MC命令>，直接写入 Bot 托管服务端 stdin。"""
        if not self.bot.qq_server or not hasattr(self.bot.qq_server, 'send_server_stdin'):
            return "QQ服务器未初始化，无法发送MC命令"

        _, _, args = str(command or "").partition(" ")
        args = args.strip()
        if not args:
            return "用法: #mc <服务器编号或名称> <MC命令>"

        target_server, mc_command, error = self._resolve_mc_console_target(args)
        if error:
            return error

        stdin_result = await self.bot.qq_server.send_server_stdin(mc_command, target_server)
        fallback_markers = (
            "目标服务器未由 Bot 托管",
            "服务器未运行",
            "服务器stdin不可用",
            "服务器stdin管道已断开",
        )
        if not any(marker in str(stdin_result) for marker in fallback_markers):
            return stdin_result

        executor = getattr(self.bot.qq_server, '_execute_server_command', None)
        if not callable(executor):
            return stdin_result
        result = await executor(mc_command, target_server)
        return result or stdin_result

    def _resolve_mc_console_target(self, args: str):
        """解析控制台 MC 命令目标。多服运行时必须显式指定目标服务器。"""
        qq_server = self.bot.qq_server
        config_manager = self.bot.config_manager
        running_configs = (
            qq_server.get_running_server_configs()
            if qq_server and hasattr(qq_server, 'get_running_server_configs')
            else []
        )

        args = str(args or "").strip()
        first, _, rest = args.partition(" ")
        selected_server = (
            config_manager.resolve_server(first)
            if config_manager and hasattr(config_manager, 'resolve_server')
            else None
        )

        if selected_server:
            if not rest.strip():
                return selected_server, "", "请在服务器编号或名称后输入MC命令"
            return selected_server, rest.strip(), None

        if config_manager and hasattr(config_manager, 'get_servers'):
            named_server, named_rest = self._match_server_name_prefix(args, config_manager.get_servers())
            if named_server:
                if not named_rest.strip():
                    return named_server, "", "请在服务器编号或名称后输入MC命令"
                return named_server, named_rest.strip(), None

        if len(running_configs) == 1:
            return running_configs[0], args.strip(), None

        if len(running_configs) > 1:
            lines = ["当前有多个Bot托管服务器在运行，请指定目标服务器。", ""]
            for index, server in enumerate(running_configs, 1):
                name = server.get('name') or server.get('_config_file') or f"server{index}"
                lines.append(f"{index}. {name}: #mc {name} {args.strip()}")
            return {}, "", "\n".join(lines)

        return {}, "", "没有正在运行的Bot托管服务器"

    def _match_server_name_prefix(self, args: str, servers):
        args = str(args or "").strip()
        args_lower = args.lower()
        for server in sorted(servers or [], key=lambda item: len(str(item.get('name') or '')), reverse=True):
            name = str(server.get('name') or '').strip()
            if not name:
                continue
            name_lower = name.lower()
            if args_lower == name_lower:
                return dict(server), ""
            if args_lower.startswith(name_lower + " "):
                return dict(server), args[len(name):].strip()
        return None, args

    def _is_registered_plugin_command(self, command_name: str) -> bool:
        plugin_manager = getattr(self.bot, 'plugin_manager', None)
        if not plugin_manager:
            return False
        command_name = str(command_name or "").lower()
        for cmd_info in plugin_manager.command_handlers.values():
            names = cmd_info.get('names') or []
            if command_name in [str(name).lower() for name in names]:
                return True
        return False

    async def _forward_to_minecraft(self, command: str):
        """转发命令到Minecraft服务器"""
        try:
            running_keys = (
                self.bot.qq_server.get_running_runtime_keys()
                if self.bot.qq_server and hasattr(self.bot.qq_server, 'get_running_runtime_keys')
                else []
            )
            if len(running_keys) > 1:
                print(
                    "错误: 当前有多个Bot托管服务器在运行，不能裸输入MC命令。\n"
                    f"运行中服务器: {', '.join(running_keys)}\n"
                    "请在GUI的MC终端选择目标服务器，或使用 #mc <服务器> <MC命令> 路由。"
                )
                return

            if self.bot.qq_server and hasattr(self.bot.qq_server, 'send_server_stdin'):
                result = await self.bot.qq_server.send_server_stdin(command)
                if result:
                    print(result)
            else:
                print("错误: Minecraft服务器未运行")
                
        except Exception as e:
            self.logger.error(f"转发命令到服务器失败: {e}")
            print(f"错误: 转发命令失败 - {e}")
    
    async def _get_connection_status(self) -> str:
        """获取连接状态信息"""
        lines = ["系统连接状态:", "=" * 50]
        
        qq_status = "已连接" if (self.bot.qq_server and self.bot.qq_server.is_connected()) else "未连接"
        lines.append(f"QQ机器人: {qq_status}")
        if self.bot.qq_server:
            lines.append(f"OneBot反向WS监听: 0.0.0.0:{self.bot.qq_server.port}")
            lines.append(f"OneBot连接数: {len(self.bot.qq_server.connected_clients)}")
            groups = ", ".join(map(str, self.bot.qq_server.allowed_groups)) or "未配置"
            lines.append(f"允许群: {groups}")
            active_server = self.bot.qq_server.active_server_config or {}
            if active_server:
                lines.append(f"当前托管服务器: {active_server.get('name', '未命名')}")
        else:
            active_server = {}
        
        msmp_config = (active_server.get('msmp') or {}) if active_server else {}
        msmp_enabled = bool(msmp_config.get('enabled', False))
        msmp_client = getattr(self.bot.qq_server, 'msmp_client', None) if self.bot.qq_server else self.bot.msmp_client
        if msmp_enabled:
            if msmp_client:
                if msmp_client.is_authenticated():
                    lines.append("MSMP: 已连接")
                else:
                    lines.append("MSMP: 未连接")
            else:
                lines.append("MSMP: 未初始化")
        else:
            lines.append("MSMP: 未启用")
        
        rcon_config = (active_server.get('rcon') or {}) if active_server else {}
        rcon_enabled = bool(rcon_config.get('enabled', False))
        rcon_client = getattr(self.bot.qq_server, 'rcon_client', None) if self.bot.qq_server else self.bot.rcon_client
        if rcon_enabled:
            if rcon_client:
                if rcon_client.is_connected():
                    lines.append("RCON: 已连接")
                else:
                    lines.append("RCON: 未连接")
            else:
                lines.append("RCON: 未初始化")
        else:
            lines.append("RCON: 未启用")
        
        running_keys = (
            self.bot.qq_server.get_running_runtime_keys()
            if self.bot.qq_server and hasattr(self.bot.qq_server, 'get_running_runtime_keys')
            else []
        )
        if running_keys:
            lines.append(f"Bot托管服务器: {', '.join(running_keys)}")
        elif self.bot.qq_server and self.bot.qq_server.server_process:
            if self.bot.qq_server.server_process.poll() is None:
                lines.append(f"服务器进程: 运行中 (PID: {self.bot.qq_server.server_process.pid})")
            else:
                lines.append("服务器进程: 已停止")
        else:
            lines.append("服务器进程: 未启动")
        
        lines.append("=" * 50)
        return "\n".join(lines)
    
    def _print_console_help(self):
        """打印控制台帮助信息"""
        help_text = """
========== MSMP_QQBot 控制台命令帮助 ==========

系统命令 (使用 # 前缀):
  #status          - 查看系统连接状态
  #reload          - 重新加载配置文件
  #logs            - 显示日志文件信息
  #help            - 显示此帮助信息
  #exit            - 退出程序
  #logstats        - 查看日志系统统计信息

日志管理命令 (使用 # 前缀):
  #log_status      - 显示日志开关状态
  #toggle_mc_log   - 开启/禁用 MC服务端日志输出
  #toggle_bot_log  - 开启/禁用 MSMP_QQBot日志输出
  #mute_log <关键词>   - 禁用包含指定关键词的日志
  #unmute_log <关键词> - 启用包含指定关键词的日志

日志归档命令 (使用 # 前缀):
  #archive_logs    - 执行日志归档操作
  #archive_stats   - 查看日志归档统计信息

连接管理命令 (使用 # 前缀):
  #connection status - 查看连接管理器状态
  #reconnect       - 重新连接所有服务 (MSMP和RCON)
  #reconnect_msmp  - 重新连接MSMP
  #reconnect_rcon  - 重新连接RCON

服务器管理命令 (使用 # 前缀):
  #start           - 启动Minecraft服务器
  #stop            - 停止Minecraft服务器
  #kill            - 强制杀死服务器进程(不保存数据,紧急用)
  #server_status   - 查看服务器进程状态

插件管理命令 (使用 # 前缀):
  #plugins         - 查看已加载的插件及其命令
  #load_plugin <插件名>  - 加载指定插件
  #unload_plugin <插件名> - 卸载指定插件
  #reload_plugin <插件名> - 重新加载指定插件(热重载)

服务器查询命令 (使用 # 前缀):
  #list            - 查看在线玩家列表
  #tps             - 查看服务器TPS(每秒刻数)性能
  #rules           - 查看服务器游戏规则和设置
  #sysinfo         - 查看系统信息 (CPU、内存、硬盘、网络)
  #disk            - 查看硬盘使用情况
  #process         - 查看Java进程信息
  #network         - 查看网络信息和实时带宽
  #listeners       - 查看所有自定义消息监听规则

Minecraft命令 (无 # 前缀):
  直接输入任意Minecraft命令将转发到服务器
  示例: list
        say Hello everyone!
        give @a diamond

================================================
        """
        print(help_text)
    
    def stop(self):
        """停止控制台处理"""
        self.running = False


class MsmpQQBot(ServerEventListener):
    """MSMP_QQBot 主程序"""
    
    def __init__(self, config_path: str = "config.yml"):
        self.config_path = config_path
        self.start_time = time.time()
        
        self._setup_basic_logging()
        
        try:
            self.config_manager = ConfigManager(config_path)
        except ConfigValidationError as e:
            print(f"配置验证失败:\n{e}")
            sys.exit(1)
        
        # 初始化日志管理器
        self.log_manager = LogManager(self.config_manager)
        self.log_manager.setup_logging(self.config_manager.is_debug_mode())
        self.logger = logging.getLogger(__name__)
        
        # 连接管理器
        self.connection_manager = ConnectionManager(self.logger, cache_ttl=5)
        self.logger.info("连接管理器已初始化")
        
        # 初始化插件管理器
        self.plugin_manager = PluginManager(
            plugin_dir="plugins",
            logger=self.logger
        )
        self.logger.info("插件管理器已初始化")
        
        self.msmp_client = None
        self.rcon_client = None
        self.qq_server = None
        self.console_handler = None
        self.loop = None
        self._log_archive_task = None
        self.running = False
        
        self.logger.info("=" * 50)
        self.logger.info(f"{APP_NAME} v{APP_VERSION} 初始化完成")
        self.logger.info("=" * 50)
    
    def _setup_basic_logging(self):
        """设置基础日志，用于配置加载阶段"""
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            handlers=[logging.StreamHandler(sys.stdout)]
        )

    def _schedule_main_coroutine(self, coro, description: str):
        """把跨线程回调提交到主事件循环，避免阻塞 MSMP 接收线程。"""
        if not self.loop or self.loop.is_closed():
            self.logger.warning(f"主事件循环不可用，跳过: {description}")
            coro.close()
            return

        def log_task_result(future):
            try:
                future.result()
            except (asyncio.CancelledError, FutureCancelledError):
                self.logger.debug(f"{description}已取消")
            except Exception as e:
                self.logger.error(f"{description}失败: {e}", exc_info=True)

        try:
            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                running_loop = None

            if running_loop is self.loop:
                task = self.loop.create_task(coro)
                task.add_done_callback(log_task_result)
                return

            future = asyncio.run_coroutine_threadsafe(coro, self.loop)
            future.add_done_callback(log_task_result)
        except Exception as e:
            coro.close()
            self.logger.error(f"提交主事件循环任务失败({description}): {e}", exc_info=True)
    
    async def start(self):
        """启动服务"""
        self.logger.info(f"{APP_NAME} v{APP_VERSION} 服务启动中...")
        self.running = True
        
        self.loop = asyncio.get_running_loop()
        if self.config_manager:
            self.config_manager.set_event_loop(self.loop)
        
        try:
            # 启动配置文件监控
            if self.config_manager:
                self.config_manager.start_file_monitor(check_interval=2)
            
            # RCON/MSMP 客户端按 servers/*.yml 的目标服务器在 #start 时创建。
            await self.connection_manager.set_clients(
                self.msmp_client,
                self.rcon_client,
                self.config_manager
            )
            self.logger.info("连接管理器已绑定客户端")
            
            # 启动 QQ 机器人 WebSocket 服务器
            ws_token = self.config_manager.get_websocket_token() if self.config_manager.is_websocket_auth_enabled() else ""
            
            self.qq_server = QQBotWebSocketServer(
                self.config_manager.get_ws_port(),
                self.config_manager.get_qq_groups(),
                self.msmp_client,
                self.logger,
                ws_token,
                config_manager=self.config_manager,
                rcon_client=self.rcon_client,
                connection_manager=self.connection_manager,
                plugin_manager=self.plugin_manager
            )
            
            await self.qq_server.start()
            self.logger.info("QQ机器人服务器已启动")
            
            self.plugin_manager.qq_server = self.qq_server
            self.logger.info("已设置插件管理器的 QQServer 引用")
            
            # 加载所有插件
            self.logger.info("=" * 60)
            self.logger.info("正在加载插件...")
            self.logger.info("=" * 60)
            await self.plugin_manager.load_plugins()
            self.logger.info("=" * 60)
            self.logger.info(f"插件加载完成，共加载 {len(self.plugin_manager.plugins)} 个插件")
            self.logger.info("=" * 60)
            
            # 启动定时日志归档
            self._log_archive_task = asyncio.create_task(self._periodic_log_archive())
            self.logger.info("定时日志归档任务已启动")
            
            # 启动定时任务管理器
            from scheduled_tasks import ScheduledTaskManager
            
            self.scheduled_task_manager = ScheduledTaskManager(
                self.config_manager,
                self.qq_server,
                self.logger
            )
            
            # 设置任务回调
            async def on_auto_start_task(task):
                """自动启动任务回调"""
                if self.qq_server:
                    await self.qq_server._start_server_process(
                        None,
                        0,
                        server_config=getattr(task, 'server_config', None)
                    )
            
            async def on_auto_stop_task(task):
                """自动停止任务回调"""
                if self.qq_server and self.qq_server.command_handlers:
                    await self.qq_server.command_handlers.handle_stop(
                        user_id=0,
                        group_id=0,
                        websocket=None,
                        is_private=False,
                        target_server=getattr(task, 'server_config', None),
                        target_server_selector=(getattr(task, 'server_config', {}) or {}).get('name', '')
                    )
            
            async def on_auto_restart_task(task):
                """自动重启任务回调"""
                if self.qq_server:
                    self.logger.info("执行服务器启动流程...")
                    await self.qq_server._start_server_process(
                        None,
                        0,
                        server_config=getattr(task, 'server_config', None)
                    )
                    
                    restart_config = getattr(task, 'task_config', None) or {}
                    restart_msg = restart_config.get('restart_success_message', 'server restarted')
                    
                    await on_task_notify(task, restart_msg)
            
            async def on_task_notify(task, message):
                """任务通知回调 - 发送到QQ群"""
                if self.qq_server and self.qq_server.current_connection:
                    server_config = getattr(task, 'server_config', None) or {}
                    qq_config = server_config.get('qq') or {}
                    target_groups = qq_config.get('groups') or self.qq_server._refresh_allowed_groups()
                    for group_id in target_groups:
                        try:
                            await self.qq_server.send_group_message(
                                self.qq_server.current_connection,
                                group_id,
                                message
                            )
                        except Exception as e:
                            self.logger.error(f"发送定时通知失败: {e}")
            
            self.scheduled_task_manager.set_start_callback(on_auto_start_task)
            self.scheduled_task_manager.set_stop_callback(on_auto_stop_task)
            self.scheduled_task_manager.set_restart_callback(on_auto_restart_task)
            self.scheduled_task_manager.set_notify_callback(on_task_notify)
            
            # 启动定时任务管理器
            self.scheduled_task_manager.start()
            
            # 注册配置重新加载回调
            if self.config_manager:
                async def on_config_reload(old_config, new_config):
                    """配置重新加载时的回调"""
                    # 通知插件管理器
                    await self.plugin_manager.reload_config(old_config, new_config)
                    self.logger.info("插件配置已更新")
                    
                    # 其他配置更新逻辑
                    if self.scheduled_task_manager:
                        self.scheduled_task_manager.reload_tasks_from_config()
                
                self.config_manager.register_reload_callback(on_config_reload)
            
            self.logger.info(f"{APP_NAME} v{APP_VERSION} 服务启动成功")

        except Exception as e:
            self.logger.error(f"启动服务失败: {e}", exc_info=True)
            await self.stop()
            raise
    
    async def _periodic_log_archive(self):
        """定时归档日志"""
        while self.running:
            try:
                # 每 24 小时执行一次
                await asyncio.sleep(86400)
                
                self.logger.info("开始定时日志归档...")
                result = await self.log_manager.archive_logs()
                
                if result:
                    self.logger.info(
                        f"日志归档完成 - "
                        f"压缩: {result['compressed']}, "
                        f"归档: {result['archived']}, "
                        f"删除: {result['deleted']}"
                    )
                    
                    # 清理超期归档（超过30天）
                    cleanup_result = await self.log_manager.cleanup_archives(days=30)
                    if cleanup_result and cleanup_result['deleted'] > 0:
                        self.logger.info(
                            f"清理超期归档 - "
                            f"删除: {cleanup_result['deleted']}, "
                            f"释放: {cleanup_result['freed_space_mb']:.2f}MB"
                        )
                        
            except asyncio.CancelledError:
                self.logger.debug("定时日志归档任务已取消")
                raise
            except Exception as e:
                self.logger.error(f"日志归档出错: {e}", exc_info=True)
                await asyncio.sleep(3600)  # 出错后 1 小时重试

    async def stop(self):
        """停止服务"""
        self.logger.info("正在停止 MSMP_QQBot 服务...")
        self.running = False

        if self._log_archive_task and not self._log_archive_task.done():
            self._log_archive_task.cancel()
            try:
                await self._log_archive_task
            except asyncio.CancelledError:
                self.logger.debug("定时日志归档任务已停止")
        self._log_archive_task = None
        
        # 卸载所有插件
        self.logger.info("=" * 60)
        self.logger.info("正在卸载插件...")
        await self.plugin_manager.unload_plugins()
        self.logger.info("=" * 60)
        self.logger.info("所有插件已卸载")
        
        # 停止定时任务管理器
        if hasattr(self, 'scheduled_task_manager') and self.scheduled_task_manager:
            self.scheduled_task_manager.stop()
            self.logger.info("定时任务管理器已停止")

        # 停止配置文件监控
        if self.config_manager:
            self.config_manager.stop_file_monitor()
            self.logger.info("配置文件监控已停止")
        
        try:
            if self.qq_server and self.qq_server.is_connected():
                await self.qq_server.send_admin_private_notifications("MSMP_QQBot 已断开连接")
            
            if self.qq_server:
                await self.qq_server.stop()
            
            if self.msmp_client:
                shutdown = getattr(self.msmp_client, 'shutdown_sync', None) or self.msmp_client.close_sync
                await asyncio.to_thread(shutdown)
            
            if self.rcon_client:
                self.rcon_client.close()
            
            self.logger.info("MSMP_QQBot 服务已停止")
            
        except Exception as e:
            self.logger.error(f"停止服务时出错: {e}", exc_info=True)
    
    def on_server_started(self, params: dict):
        """服务器启动事件"""
        self.logger.info("Minecraft服务器已启动")
        
        # 触发插件事件
        if self.plugin_manager:
            self.logger.debug("触发 server_started 事件给所有插件")
            active_server = self.qq_server.active_server_config if self.qq_server else None
            self._schedule_main_coroutine(
                self.plugin_manager.trigger_event(
                    "server_started",
                    target_server=active_server or {},
                    target_server_name=(active_server or {}).get('name', '')
                ),
                "触发 server_started 插件事件"
            )
        else:
            self.logger.warning("plugin_manager 为 None，无法触发事件")
        
        active_server = self.qq_server.active_server_config if self.qq_server else None
        if self.config_manager.is_server_event_notify_enabled(active_server) and self.qq_server and self.qq_server.is_connected():
            self._schedule_main_coroutine(
                self.qq_server._send_process_notification("Minecraft服务器已启动"),
                "发送服务器启动通知"
            )

    def on_server_stopping(self, params: dict):
        """服务器停止事件"""
        self.logger.info("Minecraft服务器正在停止")
        
        # 触发插件事件
        if self.plugin_manager:
            self.logger.debug("触发 server_stopping 事件给所有插件")
            active_server = self.qq_server.active_server_config if self.qq_server else None
            self._schedule_main_coroutine(
                self.plugin_manager.trigger_event(
                    "server_stopping",
                    target_server=active_server or {},
                    target_server_name=(active_server or {}).get('name', '')
                ),
                "触发 server_stopping 插件事件"
            )
        
        active_server = self.qq_server.active_server_config if self.qq_server else None
        if self.config_manager.is_server_event_notify_enabled(active_server) and self.qq_server and self.qq_server.is_connected():
            self._schedule_main_coroutine(
                self.qq_server._send_process_notification("Minecraft服务器正在停止"),
                "发送服务器停止通知"
            )

    def on_player_join(self, params: dict):
        """玩家加入事件"""
        player_name = params.get('name', 'Unknown')
        self.logger.info(f"玩家加入: {player_name}")
        
        # 触发插件事件
        active_server = self.qq_server.active_server_config if self.qq_server else None
        if self.plugin_manager:
            self.logger.debug(f"触发 player_join 事件给所有插件: {player_name}")
            self._schedule_main_coroutine(
                self.plugin_manager.trigger_event(
                    "player_join",
                    player_name,
                    target_server=active_server or {},
                    target_server_name=(active_server or {}).get('name', '')
                ),
                "触发 player_join 插件事件"
            )

        if self.config_manager.is_player_event_notify_enabled(active_server) and self.qq_server and self.qq_server.is_connected():
            message = f"{player_name} 加入了游戏"
            self._schedule_main_coroutine(
                self.qq_server._send_process_notification(message),
                "发送玩家加入通知"
            )

    def on_player_leave(self, params: dict):
        """玩家离开事件"""
        player_name = params.get('name', 'Unknown')
        self.logger.info(f"玩家离开: {player_name}")
        
        # 触发插件事件
        active_server = self.qq_server.active_server_config if self.qq_server else None
        if self.plugin_manager:
            self.logger.debug(f"触发 player_leave 事件给所有插件: {player_name}")
            self._schedule_main_coroutine(
                self.plugin_manager.trigger_event(
                    "player_leave",
                    player_name,
                    target_server=active_server or {},
                    target_server_name=(active_server or {}).get('name', '')
                ),
                "触发 player_leave 插件事件"
            )

        if self.config_manager.is_player_event_notify_enabled(active_server) and self.qq_server and self.qq_server.is_connected():
            message = f"{player_name} 离开了游戏"
            self._schedule_main_coroutine(
                self.qq_server._send_process_notification(message),
                "发送玩家离开通知"
            )
    
    async def run_async(self):
        """异步运行主循环"""
        await self.start()
        
        try:
            while self.running:
                await asyncio.sleep(1)
                
        except KeyboardInterrupt:
            self.logger.info("收到中断信号，正在停止...")
        finally:
            await self.stop()

def main():
    """主函数"""
    _configure_standard_streams()
    os.chdir(BASE_DIR)
    print("=" * 50)
    print(f"  {APP_NAME} v{APP_VERSION} - Minecraft Server QQ Bridge")
    print("=" * 50)
    
    bridge = MsmpQQBot()
    loop = None
    
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        def signal_handler(signum, frame):
            print("\n收到停止信号，正在关闭...")
            if bridge.running:
                asyncio.create_task(bridge.stop())
        
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
        
        # 启动主服务
        loop.run_until_complete(bridge.start())
        
        # 启动控制台命令处理器（在主服务启动后）
        console_handler = ConsoleCommandHandler(bridge, bridge.logger)
        console_task = asyncio.ensure_future(console_handler.handle_console_input())
        
        # 主循环
        try:
            while bridge.running:
                loop.run_until_complete(asyncio.sleep(1))
                
        except KeyboardInterrupt:
            print("\n收到中断信号，正在停止...")
        finally:
            # 停止控制台处理器
            console_handler.stop()
            if not console_task.done():
                console_task.cancel()
            
            # 停止主服务
            loop.run_until_complete(bridge.stop())
        
    except Exception as e:
        logging.error(f"程序运行出错: {e}", exc_info=True)
        sys.exit(1)
    finally:
        if loop:
            loop.close()


if __name__ == "__main__":
    main()
