import logging
import sys
import signal
import time
import os
import asyncio
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler
from config_manager import ConfigManager, ConfigValidationError
from msmp_client import MSMPClient, ServerEventListener
from rcon_client import RCONClient
from qq_bot_server import QQBotWebSocketServer
from connection_manager import ConnectionManager, ConnectionStatus
from log_system import LogManager, AdvancedLogFilter, LogArchiveManager


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
        """禁用包含特定关键词的日志"""
        self.disabled_keywords.add(keyword)
    
    def enable_keyword(self, keyword: str):
        """启用包含特定关键词的日志"""
        self.disabled_keywords.discard(keyword)
    
    def is_logger_disabled(self, logger_name: str) -> bool:
        """检查记录器是否禁用"""
        return logger_name in self.disabled_loggers
    
    def is_keyword_disabled(self, keyword: str) -> bool:
        """检查关键词是否禁用"""
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
        loop = asyncio.get_event_loop()
        
        print("\n" + "="*60)
        print("控制台已就绪，可以输入命令")
        print("输入 #help 查看系统命令列表")
        print("="*60 + "\n")
        
        while self.running and self.bot.running:
            try:
                line = await loop.run_in_executor(None, sys.stdin.readline)
                
                if line:
                    line = line.strip()
                    if not line:
                        continue
                    
                    if await self._handle_bot_command(line):
                        continue
                    
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
        
        system_command = command[1:].strip().lower()
        
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
            keyword = system_command[9:].strip()
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
            keyword = system_command[11:].strip()
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
            if hasattr(self.bot.qq_server, 'connection_manager'):
                results = await self.bot.qq_server.connection_manager.reconnect_all()
                print(f"重连结果: MSMP={results.get('msmp')}, RCON={results.get('rcon')}")
            else:
                print("连接管理器未初始化")
            return True
        
        elif system_command == 'reconnect_msmp':
            if hasattr(self.bot.qq_server, 'connection_manager'):
                success = await self.bot.qq_server.connection_manager.reconnect_msmp()
                print(f"MSMP重连: {'成功' if success else '失败'}")
            else:
                print("连接管理器未初始化")
            return True
        
        elif system_command == 'reconnect_rcon':
            if hasattr(self.bot.qq_server, 'connection_manager'):
                success = await self.bot.qq_server.connection_manager.reconnect_rcon()
                print(f"RCON重连: {'成功' if success else '失败'}")
            else:
                print("连接管理器未初始化")
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
        elif system_command == 'list':
            result = await self._handle_qq_command('list')
            print(result)
        elif system_command == 'tps':
            result = await self._handle_qq_command('tps')
            print(result)
        elif system_command == 'rules':
            result = await self._handle_qq_command('rules')
            print(result)
        elif system_command == 'sysinfo':
            result = await self._handle_qq_command('sysinfo')
            print(result)
        elif system_command == 'disk':
            result = await self._handle_qq_command('disk')
            print(result)
        elif system_command == 'process':
            result = await self._handle_qq_command('process')
            print(result)
        elif system_command == 'network':
            result = await self._handle_qq_command('network')
            print(result)
        elif system_command == 'listeners':
            result = await self._handle_qq_command('listeners')
            print(result)
        elif system_command == 'start':
            result = await self._handle_console_start()
            print(result)
        elif system_command == 'stop':
            result = await self._handle_console_stop()
            print(result)
        elif system_command == 'kill':
            result = await self._handle_console_kill()
            print(result)
        elif system_command == 'server_status':
            result = await self._get_server_process_status()
            print(result)
        else:
            print(f"未知的系统命令: #{system_command}，输入 #help 查看帮助")
        
        return True

    async def _handle_console_start(self) -> str:
        """控制台直接启动服务器"""
        try:
            if (self.bot.qq_server and 
                self.bot.qq_server.server_process and 
                self.bot.qq_server.server_process.poll() is None):
                return "服务器已经在运行中"
            
            start_script = self.bot.config_manager.get_server_start_script()
            if not start_script:
                return "服务器启动脚本未配置，请在 config.yml 中设置 server.start_script"
            
            if not os.path.exists(start_script):
                return f"启动脚本不存在: {start_script}"
            
            print("正在启动Minecraft服务器...")
            
            # 重置关闭模式
            if hasattr(self.bot.qq_server, 'command_handlers'):
                await self.bot.qq_server.command_handlers._reset_shutdown_mode()
                print("关闭模式已重置")
            
            await self.bot.qq_server._start_server_process(None, 0)
            
            return "服务器启动命令已执行"
            
        except Exception as e:
            self.logger.error(f"控制台启动服务器失败: {e}")
            return f"启动服务器失败: {e}"

    async def _handle_console_stop(self) -> str:
        """控制台直接停止服务器"""
        try:
            if not self.bot.qq_server or not self.bot.qq_server.command_handlers:
                return "qq_server 或 command_handlers 未初始化"
            
            print("正在停止Minecraft服务器...")
            
            # 使用统一的停止方法，添加 from_console 标志
            result = await self.bot.qq_server.command_handlers.handle_stop(
                user_id=0,
                group_id=0,
                websocket=None,
                is_private=False,
                from_console=True  # 添加这个标志
            )
            
            return result if result else "停止命令已执行"
            
        except Exception as e:
            error_msg = f"停止服务器失败: {e}"
            print(f"✗ {error_msg}")
            self.logger.error(f"控制台停止服务器失败: {e}", exc_info=True)
            return error_msg

    async def _handle_console_kill(self) -> str:
        """控制台直接强制杀死服务器"""
        if not self.bot.qq_server or not self.bot.qq_server.command_handlers:
            return "命令处理器未初始化"
        
        return await self.bot.qq_server.command_handlers._execute_kill_command()

    async def _get_server_process_status(self) -> str:
        """获取服务器进程状态"""
        try:
            if not self.bot.qq_server:
                return "QQ服务器未初始化"
            
            server_process = self.bot.qq_server.server_process
            
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
                
                recent_logs = self.bot.qq_server.get_recent_logs(3)
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
            if hasattr(self.bot.qq_server, 'command_handlers'):
                handler_method = getattr(self.bot.qq_server.command_handlers, f'handle_{command}', None)
                if handler_method and asyncio.iscoroutinefunction(handler_method):
                    result = await handler_method()
                    return result if result else f"命令 '{command}' 已执行"
                elif handler_method:
                    result = handler_method()
                    return result if result else f"命令 '{command}' 已执行"
            return f"命令 '{command}' 不可用或未实现"
        except Exception as e:
            self.logger.error(f"处理QQ命令失败: {e}")
            return f"命令执行失败: {e}"

    async def _forward_to_minecraft(self, command: str):
        """转发命令到Minecraft服务器"""
        try:
            if (self.bot.qq_server and 
                self.bot.qq_server.server_process and 
                self.bot.qq_server.server_process.poll() is None):
                
                try:
                    command_bytes = (command + '\n').encode('utf-8')
                    self.bot.qq_server.server_process.stdin.write(command_bytes)
                    self.bot.qq_server.server_process.stdin.flush()
                    self.logger.debug(f"已转发命令到服务器: {command}")
                except BrokenPipeError:
                    print("错误: 服务器进程的stdin管道已断开")
                except Exception as e:
                    print(f"错误: 转发命令失败 - {e}")
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
        
        if self.bot.config_manager.is_msmp_enabled():
            if self.bot.msmp_client:
                if self.bot.msmp_client.is_authenticated():
                    lines.append("MSMP: 已连接")
                else:
                    lines.append("MSMP: 未连接")
            else:
                lines.append("MSMP: 未初始化")
        else:
            lines.append("MSMP: 未启用")
        
        if self.bot.config_manager.is_rcon_enabled():
            if self.bot.rcon_client:
                if self.bot.rcon_client.is_connected():
                    lines.append("RCON: 已连接")
                else:
                    lines.append("RCON: 未连接")
            else:
                lines.append("RCON: 未初始化")
        else:
            lines.append("RCON: 未启用")
        
        if self.bot.qq_server and self.bot.qq_server.server_process:
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
    """MSMP_QQBot主程序"""
    
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
        
        self.msmp_client = None
        self.rcon_client = None
        self.qq_server = None
        self.console_handler = None
        self.loop = None
        self.running = False
        
        self.logger.info("=" * 50)
        self.logger.info("MSMP_QQBot 初始化完成")
        self.logger.info("=" * 50)
    
    def _setup_basic_logging(self):
        """设置基础日志，用于配置加载阶段"""
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            handlers=[logging.StreamHandler(sys.stdout)]
        )
    
    async def start(self):
        """启动服务"""
        self.logger.info("MSMP_QQBot 服务启动中...")
        
        self.loop = asyncio.get_running_loop()
        
        try:
            # 启动配置文件监控
            if self.config_manager:
                self.config_manager.start_file_monitor(check_interval=2)
            
            # 创建客户端
            if self.config_manager.is_rcon_enabled():
                self.rcon_client = RCONClient(
                    self.config_manager.get_rcon_host(),
                    self.config_manager.get_rcon_port(),
                    self.config_manager.get_rcon_password(),
                    self.logger
                )
            
            if self.config_manager.is_msmp_enabled():
                self.msmp_client = MSMPClient(
                    self.config_manager.get_msmp_host(),
                    self.config_manager.get_msmp_port(),
                    self.config_manager.get_msmp_password(),
                    self.logger,
                    self.config_manager
                )
                self.msmp_client.set_event_listener(self)
                self.msmp_client.start_background_loop()
            
            # 设置连接管理器的客户端
            await self.connection_manager.set_clients(
                self.msmp_client,
                self.rcon_client,
                self.config_manager
            )
            self.logger.info("连接管理器已绑定客户端")
            
            # 启动QQ机器人WebSocket服务器
            ws_token = self.config_manager.get_websocket_token() if self.config_manager.is_websocket_auth_enabled() else ""
            
            self.qq_server = QQBotWebSocketServer(
                self.config_manager.get_ws_port(),
                self.config_manager.get_qq_groups(),
                self.msmp_client,
                self.logger,
                ws_token,
                config_manager=self.config_manager,
                rcon_client=self.rcon_client,
                connection_manager=self.connection_manager
            )
            
            await self.qq_server.start()
            self.logger.info("QQ机器人服务器已启动")
            
            # 启动定时日志归档
            asyncio.create_task(self._periodic_log_archive())
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
                    await self.qq_server._start_server_process(None, 0)
            
            async def on_auto_stop_task(task):
                """自动停止任务回调"""
                if self.qq_server and self.qq_server.command_handlers:
                    await self.qq_server.command_handlers.handle_stop(
                        user_id=0,
                        group_id=0,
                        websocket=None,
                        is_private=False
                    )
            
            async def on_auto_restart_task(task):
                """自动重启任务回调"""
                if self.qq_server:
                    self.logger.info("执行服务器启动流程...")
                    await self.qq_server._start_server_process(None, 0)
                    
                    scheduled_config = self.config_manager.config.get('scheduled_tasks', {})
                    restart_config = scheduled_config.get('auto_restart', {})
                    restart_msg = restart_config.get('restart_success_message', '服务器已重启，欢迎回来！')
                    
                    await on_task_notify(task, restart_msg)
            
            async def on_task_notify(task, message):
                """任务通知回调 - 发送到QQ群"""
                if self.qq_server and self.qq_server.current_connection:
                    for group_id in self.qq_server.allowed_groups:
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
            
            # 注册配置重载回调
            if self.config_manager:
                async def on_config_reload(old_config, new_config):
                    if self.scheduled_task_manager:
                        self.scheduled_task_manager.reload_tasks_from_config()
                
                self.config_manager.register_reload_callback(on_config_reload)
            
            # 初始化控制台处理器
            self.console_handler = ConsoleCommandHandler(self, self.logger)
            self.logger.info("控制台命令处理器已初始化")
            
            # 启动控制台输入处理
            asyncio.create_task(self.console_handler.handle_console_input())
            
            self.running = True
            self.logger.info("MSMP_QQBot 服务启动成功")

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
                
                self.logger.info("开始定期日志归档...")
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
                        
            except Exception as e:
                self.logger.error(f"日志归档出错: {e}", exc_info=True)
                await asyncio.sleep(3600)  # 出错后 1 小时重试

    async def stop(self):
        """停止服务"""
        self.logger.info("正在停止MSMP_QQBot服务...")
        self.running = False
        
        # 停止定时任务管理器
        if hasattr(self, 'scheduled_task_manager') and self.scheduled_task_manager:
            self.scheduled_task_manager.stop()
            self.logger.info("定时任务管理器已停止")

        # 停止配置文件监控
        if self.config_manager:
            self.config_manager.stop_file_monitor()
            self.logger.info("配置文件监控已停止")
        
        try:
            if (self.qq_server and 
                self.config_manager.is_server_event_notify_enabled() and
                self.qq_server.is_connected()):
                await self.qq_server.broadcast_to_all_groups("MSMP_QQBot 已断开连接")
            
            if self.qq_server:
                await self.qq_server.stop()
            
            if self.msmp_client:
                self.msmp_client.close_sync()
            
            if self.rcon_client:
                self.rcon_client.close()
            
            self.logger.info("MSMP_QQBot 服务已停止")
            
        except Exception as e:
            self.logger.error(f"停止服务时出错: {e}", exc_info=True)
    
    def on_server_started(self, params: dict):
        """服务器启动事件"""
        self.logger.info("Minecraft服务器已启动")
        
        if self.config_manager.is_server_event_notify_enabled() and self.qq_server and self.qq_server.is_connected():
            try:
                asyncio.run_coroutine_threadsafe(
                    self.qq_server.broadcast_to_all_groups("Minecraft服务器已启动"),
                    self.loop
                )
            except Exception as e:
                self.logger.error(f"发送服务器启动通知失败: {e}", exc_info=True)
    
    def on_server_stopping(self, params: dict):
        """服务器停止事件"""
        self.logger.info("Minecraft服务器正在停止")
        
        if self.config_manager.is_server_event_notify_enabled() and self.qq_server and self.qq_server.is_connected():
            try:
                asyncio.run_coroutine_threadsafe(
                    self.qq_server.broadcast_to_all_groups("Minecraft服务器正在停止"),
                    self.loop
                )
            except Exception as e:
                self.logger.error(f"发送服务器停止通知失败: {e}", exc_info=True)
    
    def on_player_join(self, params: dict):
        """玩家加入事件"""
        player_name = params.get('name', 'Unknown')
        self.logger.info(f"玩家加入: {player_name}")
        
        if self.config_manager.is_player_event_notify_enabled() and self.qq_server and self.qq_server.is_connected():
            message = f"{player_name} 加入了游戏"
            try:
                asyncio.run_coroutine_threadsafe(
                    self.qq_server.broadcast_to_all_groups(message),
                    self.loop
                )
            except Exception as e:
                self.logger.error(f"发送玩家加入通知失败: {e}", exc_info=True)
    
    def on_player_leave(self, params: dict):
        """玩家离开事件"""
        player_name = params.get('name', 'Unknown')
        self.logger.info(f"玩家离开: {player_name}")
        
        if self.config_manager.is_player_event_notify_enabled() and self.qq_server and self.qq_server.is_connected():
            message = f"{player_name} 离开了游戏"
            try:
                asyncio.run_coroutine_threadsafe(
                    self.qq_server.broadcast_to_all_groups(message),
                    self.loop
                )
            except Exception as e:
                self.logger.error(f"发送玩家离开通知失败: {e}", exc_info=True)
    
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
    print("="*50)
    print("  MSMP_QQBot - Minecraft Server QQ Bridge")
    print("="*50)
    
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
        
        loop.run_until_complete(bridge.run_async())
        
    except Exception as e:
        logging.error(f"程序运行出错: {e}", exc_info=True)
        sys.exit(1)
    finally:
        if loop:
            loop.close()


if __name__ == "__main__":
    main()