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


class LogFilter(logging.Filter):
    """自定义日志过滤器 - 支持动态启用/禁用特定日志"""
    
    def __init__(self):
        super().__init__()
        self.disabled_loggers = set()  # 禁用的日志记录器名称
        self.disabled_keywords = set()  # 禁用的日志关键词
    
    def filter(self, record: logging.LogRecord) -> bool:
        """过滤日志记录"""
        # 检查是否禁用了该记录器
        if record.name in self.disabled_loggers:
            return False
        
        # 检查是否包含禁用的关键词
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


class ConsoleCommandHandler:
    """
    统一的控制台命令处理器
    """
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
                # 异步读取控制台输入
                line = await loop.run_in_executor(None, sys.stdin.readline)
                
                if line:
                    line = line.strip()
                    if not line:
                        continue
                    
                    # 优先处理Bot系统命令（带#前缀）
                    if await self._handle_bot_command(line):
                        continue
                    
                    # 否则转发到Minecraft服务器
                    await self._forward_to_minecraft(line)
                        
            except EOFError:
                # 输入流结束
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
        
        if system_command == 'status':
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
                    print("✓ 配置已重新加载")
                else:
                    print("✗ 配置重新加载失败")
            except Exception as e:
                print(f"✗ 配置重新加载失败: {e}")
        elif system_command == 'logs':
            self._show_logs_info()
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
        elif system_command == 'reconnect':
            result = await self._handle_qq_command('reconnect')
            print(result)
        elif system_command == 'reconnect_msmp':
            result = await self._handle_qq_command('reconnect_msmp')
            print(result)
        elif system_command == 'reconnect_rcon':
            result = await self._handle_qq_command('reconnect_rcon')
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
        elif system_command == 'logstats':
            if self.bot.qq_server:
                print(self.bot.qq_server.get_logs_info())
            else:
                print("QQ服务器未初始化")
        elif system_command == 'toggle_mc_log':
            self._toggle_mc_server_log()
        elif system_command == 'toggle_bot_log':
            self._toggle_bot_log()
        elif system_command == 'log_status':
            self._show_log_status()
        elif system_command.startswith('mute_log '):
            keyword = system_command[9:].strip()
            if keyword:
                self._mute_log_keyword(keyword)
            else:
                print("用法: #mute_log <关键词>")
        elif system_command.startswith('unmute_log '):
            keyword = system_command[11:].strip()
            if keyword:
                self._unmute_log_keyword(keyword)
            else:
                print("用法: #unmute_log <关键词>")
        else:
            print(f"未知的系统命令: #{system_command}，输入 #help 查看帮助")
        
        return True
    
    def _toggle_mc_server_log(self):
        """切换MC服务端日志输出"""
        filter_obj = self.bot.log_filter
        
        if filter_obj.is_keyword_disabled('[MC Server]'):
            filter_obj.enable_keyword('[MC Server]')
            print("✓ MC服务端日志已启用")
            self.logger.info("MC服务端日志已启用")
        else:
            filter_obj.disable_keyword('[MC Server]')
            print("✓ MC服务端日志已禁用")
            self.logger.info("MC服务端日志已禁用")
    
    def _toggle_bot_log(self):
        """切换MSMP_QQBot日志输出"""
        filter_obj = self.bot.log_filter
        
        bot_loggers = [
            '__main__',
            'config_manager',
            'qq_bot_server',
            'msmp_client',
            'rcon_client',
            'command_handler',
            'custom_listener'
        ]
        
        all_disabled = all(filter_obj.is_logger_disabled(name) for name in bot_loggers)
        
        if all_disabled:
            for logger_name in bot_loggers:
                filter_obj.enable_logger(logger_name)
            print("✓ MSMP_QQBot日志已启用")
            self.logger.info("MSMP_QQBot日志已启用")
        else:
            for logger_name in bot_loggers:
                filter_obj.disable_logger(logger_name)
            print("✓ MSMP_QQBot日志已禁用")
    
    def _show_log_status(self):
        """显示日志状态"""
        filter_obj = self.bot.log_filter
        
        print("\n日志状态信息:")
        print("=" * 50)
        
        mc_status = "禁用" if filter_obj.is_keyword_disabled('[MC Server]') else "启用"
        print(f"MC服务端日志: {mc_status}")
        
        bot_loggers = [
            '__main__',
            'config_manager',
            'qq_bot_server',
            'msmp_client',
            'rcon_client',
            'command_handler',
            'custom_listener'
        ]
        all_disabled = all(filter_obj.is_logger_disabled(name) for name in bot_loggers)
        bot_status = "禁用" if all_disabled else "启用"
        print(f"MSMP_QQBot日志: {bot_status}")
        
        if filter_obj.disabled_keywords:
            print(f"\n禁用的关键词 ({len(filter_obj.disabled_keywords)} 个):")
            for keyword in sorted(filter_obj.disabled_keywords):
                print(f"  - {keyword}")
        else:
            print("\n无禁用的关键词")
        
        print("=" * 50)
    
    def _mute_log_keyword(self, keyword: str):
        """禁用包含指定关键词的日志"""
        filter_obj = self.bot.log_filter
        
        if filter_obj.is_keyword_disabled(keyword):
            print(f"关键词 '{keyword}' 已经被禁用")
        else:
            filter_obj.disable_keyword(keyword)
            print(f"✓ 已禁用包含 '{keyword}' 的日志")
            self.logger.info(f"已禁用包含 '{keyword}' 的日志")
    
    def _unmute_log_keyword(self, keyword: str):
        """启用包含指定关键词的日志"""
        filter_obj = self.bot.log_filter
        
        if not filter_obj.is_keyword_disabled(keyword):
            print(f"关键词 '{keyword}' 未被禁用")
        else:
            filter_obj.enable_keyword(keyword)
            print(f"✓ 已启用包含 '{keyword}' 的日志")
            self.logger.info(f"已启用包含 '{keyword}' 的日志")

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
            
            await self.bot.qq_server._start_server_process(None, 0)
            
            return "✓ 服务器启动命令已执行"
            
        except Exception as e:
            self.logger.error(f"控制台启动服务器失败: {e}")
            return f"启动服务器失败: {e}"

    async def _handle_console_stop(self) -> str:
        """控制台直接停止服务器"""
        try:
            client_type, client = self.bot.qq_server.command_handlers._get_active_client()
            
            if not client:
                return "服务器连接未就绪，无法执行停止命令"
            
            print("正在停止Minecraft服务器...")
            
            if client_type == 'msmp':
                try:
                    status = client.get_server_status_sync()
                    if not status.get('started', False):
                        return "服务器已经是停止状态"
                    
                    result = client.execute_command_sync("server/stop")
                    
                    if 'result' in result:
                        return "✓ 停止命令已发送"
                    else:
                        error_msg = result.get('error', {}).get('message', '未知错误')
                        return f"✗ 停止服务器失败: {error_msg}"
                
                except Exception as e:
                    return f"MSMP停止命令失败: {e}"
            
            else:
                success = client.stop_server()
                if success:
                    return "✓ 停止命令已发送"
                else:
                    return "✗ RCON停止命令失败"
                    
        except Exception as e:
            self.logger.error(f"控制台停止服务器失败: {e}")
            return f"停止服务器失败: {e}"

    async def _handle_console_kill(self) -> str:
        """控制台直接强制杀死服务器 - 调用通用方法"""
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

日志开关命令 (使用 # 前缀):
  #toggle_mc_log   - 开启/禁用 MC服务端日志输出
  #toggle_bot_log  - 开启/禁用 MSMP_QQBot日志输出
  #log_status      - 显示日志开关状态
  #mute_log <关键词>   - 禁用包含指定关键词的日志
  #unmute_log <关键词> - 启用包含指定关键词的日志

服务器管理命令 (使用 # 前缀):
  #start           - 启动Minecraft服务器
  #stop            - 停止Minecraft服务器
  #kill            - 强制杀死服务器进程(不保存数据,紧急用)
  #server_status   - 查看服务器进程状态

服务器查询命令 (使用 # 前缀):
  #list            - 查看在线玩家列表
  #tps             - 查看服务器TPS性能
  #rules           - 查看服务器游戏规则

系统监控命令 (使用 # 前缀):
  #sysinfo         - 查看系统信息 (CPU、内存、硬盘、网络)
  #disk            - 查看硬盘使用情况
  #process         - 查看Java进程信息
  #network         - 查看网络信息和实时带宽

连接管理命令 (使用 # 前缀):
  #reconnect       - 重新连接所有服务 (MSMP和RCON)
  #reconnect_msmp  - 重新连接MSMP
  #reconnect_rcon  - 重新连接RCON

其他命令 (使用 # 前缀):
  #listeners       - 查看自定义消息监听规则

Minecraft命令 (无 # 前缀):
  直接输入任意Minecraft命令将转发到服务器
  示例: list
        say Hello everyone!
        give @a diamond

日志开关示例:
  #toggle_mc_log   - 禁用MC服务端日志
  #toggle_bot_log  - 禁用Bot日志
  #log_status      - 查看所有日志状态
  #mute_log ERROR  - 禁用包含 ERROR 的日志
  #unmute_log ERROR - 启用包含 ERROR 的日志

================================================
        """
        print(help_text)
    
    def _show_logs_info(self):
        """显示日志文件信息"""
        log_dir = "logs"
        if not os.path.exists(log_dir):
            print("日志目录不存在")
            return
        
        print("日志文件信息:")
        print("=" * 50)
        files_found = False
        for file in os.listdir(log_dir):
            if file.endswith('.log'):
                files_found = True
                file_path = os.path.join(log_dir, file)
                size = os.path.getsize(file_path)
                mtime = time.strftime('%Y-%m-%d %H:%M:%S', 
                                    time.localtime(os.path.getmtime(file_path)))
                
                if size > 1024*1024:
                    size_str = f"{size/(1024*1024):.2f} MB"
                elif size > 1024:
                    size_str = f"{size/1024:.2f} KB"
                else:
                    size_str = f"{size} B"
                
                print(f"{file:20} {size_str:>10}  修改: {mtime}")
        
        if not files_found:
            print("没有日志文件")
        
        print("=" * 50)
    
    def stop(self):
        """停止控制台处理"""
        self.running = False


class MsmpQQBot(ServerEventListener):
    """MSMP_QQBot主程序"""
    
    def __init__(self, config_path: str = "config.yml"):
        self.config_path = config_path
        self.start_time = time.time()
        
        # 基础日志
        self._setup_basic_logging()
        
        # 加载配置
        try:
            self.config_manager = ConfigManager(config_path)
        except ConfigValidationError as e:
            print(f"配置验证失败:\n{e}")
            sys.exit(1)
        
        # 完整日志系统 (包含过滤器)
        self._setup_logging()
        self.logger = logging.getLogger(__name__)
        
        # 初始化组件
        self.msmp_client = None
        self.rcon_client = None
        self.qq_server = None
        self.console_handler = None
        self.loop = None
        self.running = False
        
        self.logger.info("="*50)
        self.logger.info("MSMP_QQBot 初始化完成")
        self.logger.info("="*50)
    
    def _setup_basic_logging(self):
        """设置基础日志，用于配置加载阶段"""
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            handlers=[logging.StreamHandler(sys.stdout)]
        )
    
    def _setup_logging(self):
        """设置完整的日志系统"""
        log_dir = "logs"
        os.makedirs(log_dir, exist_ok=True)
        
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.DEBUG if self.config_manager.is_debug_mode() else logging.INFO)
        
        for handler in root_logger.handlers[:]:
            root_logger.removeHandler(handler)
        
        # 创建日志过滤器
        self.log_filter = LogFilter()
        
        # 主日志文件 - 按大小轮转
        main_handler = RotatingFileHandler(
            os.path.join(log_dir, 'msmp_qqbot.log'),
            maxBytes=10*1024*1024,
            backupCount=5,
            encoding='utf-8'
        )
        main_handler.setLevel(logging.DEBUG)
        main_handler.addFilter(self.log_filter)
        
        # 错误日志文件 - 单独记录错误
        error_handler = RotatingFileHandler(
            os.path.join(log_dir, 'error.log'),
            maxBytes=5*1024*1024,
            backupCount=3,
            encoding='utf-8'
        )
        error_handler.setLevel(logging.ERROR)
        error_handler.addFilter(self.log_filter)
        
        # 控制台处理器
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.INFO)
        console_handler.addFilter(self.log_filter)
        
        # 运行日志 - 按天轮转
        runtime_handler = TimedRotatingFileHandler(
            os.path.join(log_dir, 'runtime.log'),
            when='midnight',
            interval=1,
            backupCount=7,
            encoding='utf-8'
        )
        runtime_handler.setLevel(logging.INFO)
        runtime_handler.addFilter(self.log_filter)
        
        # 日志格式
        detailed_formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s'
        )
        simple_formatter = logging.Formatter(
            '%(asctime)s - %(levelname)s - %(message)s'
        )
        
        main_handler.setFormatter(detailed_formatter)
        error_handler.setFormatter(detailed_formatter)
        console_handler.setFormatter(simple_formatter)
        runtime_handler.setFormatter(simple_formatter)
        
        root_logger.addHandler(main_handler)
        root_logger.addHandler(error_handler)
        root_logger.addHandler(console_handler)
        root_logger.addHandler(runtime_handler)
        
        logging.getLogger('websockets').setLevel(logging.WARNING)
        logging.getLogger('asyncio').setLevel(logging.WARNING)
        
        self.logger = logging.getLogger(__name__)
        self.logger.info("日志系统初始化完成")
    
    async def start(self):
        """启动服务"""
        self.logger.info("MSMP_QQBot 服务启动中...")
        
        self.loop = asyncio.get_running_loop()
        
        try:
            # ============ 启动配置文件监控 ============
            if self.config_manager:
                self.config_manager.start_file_monitor(check_interval=2)
                self.logger.info("配置文件监控已启动 (每2秒检查一次)")
            
            # 初始化RCON客户端（如果启用）
            if self.config_manager.is_rcon_enabled():
                self.rcon_client = RCONClient(
                    self.config_manager.get_rcon_host(),
                    self.config_manager.get_rcon_port(),
                    self.config_manager.get_rcon_password(),
                    self.logger
                )
                self.logger.info("RCON客户端已初始化")
            
            # 启动QQ机器人WebSocket服务器
            ws_token = self.config_manager.get_websocket_token() if self.config_manager.is_websocket_auth_enabled() else ""
            
            self.qq_server = QQBotWebSocketServer(
                self.config_manager.get_ws_port(),
                self.config_manager.get_qq_groups(),
                None,
                self.logger,
                ws_token,
                self.config_manager,
                self.rcon_client
            )
            
            await self.qq_server.start()
            self.logger.info("QQ机器人服务器已启动")
            
            # 初始化MSMP客户端（如果启用）
            if self.config_manager.is_msmp_enabled():
                self.msmp_client = MSMPClient(
                    self.config_manager.get_msmp_host(),
                    self.config_manager.get_msmp_port(),
                    self.config_manager.get_msmp_password(),
                    self.logger,
                    self.config_manager
                )
                
                self.msmp_client.start_background_loop()
                self.msmp_client.set_event_listener(self)
                self.qq_server.msmp_client = self.msmp_client
                
                asyncio.create_task(self._delayed_msmp_connect())
            else:
                self.logger.info("MSMP未启用，跳过MSMP客户端初始化")
            
            self.console_handler = ConsoleCommandHandler(self, self.logger)
            self.logger.info("控制台命令处理器已初始化")
            
            # 启动控制台输入处理
            asyncio.create_task(self.console_handler.handle_console_input())
            
            self.running = True
            self.logger.info("MSMP_QQBot 服务启动成功")
            self.logger.info("提示: 输入 '#help' 查看控制台命令")
                
        except Exception as e:
            self.logger.error(f"启动服务失败: {e}", exc_info=True)
            await self.stop()
            raise

    async def _delayed_msmp_connect(self):
        """延迟连接MSMP服务器"""
        await asyncio.sleep(3)
        self.logger.info("准备连接MSMP服务器...")
        asyncio.create_task(self._try_msmp_connection())
    
    async def _try_rcon_connection(self):
        """尝试连接RCON服务器"""
        await asyncio.sleep(3)
        self.logger.info("准备连接RCON服务器...")
        
        retry_count = 0
        max_retries = 5
        
        while self.running and retry_count < max_retries:
            try:
                retry_count += 1
                self.logger.info(f"正在尝试连接RCON服务器 {self.config_manager.get_rcon_host()}:{self.config_manager.get_rcon_port()} (第{retry_count}次)")
                
                if self.rcon_client.connect():
                    self.logger.info("RCON服务器连接成功！")
                    if self.config_manager.is_server_event_notify_enabled() and self.qq_server and self.qq_server.is_connected():
                        await self.qq_server.broadcast_to_all_groups("RCON连接已建立")
                    break
                else:
                    self.logger.warning("RCON服务器连接失败")
                    
            except Exception as e:
                self.logger.warning(f"RCON服务器连接失败: {e}")
            
            if retry_count < max_retries:
                delay = min(10 * retry_count, 30)
                self.logger.info(f"{delay}秒后尝试重新连接RCON服务器...")
                await asyncio.sleep(delay)
        else:
            if retry_count >= max_retries:
                self.logger.warning(f"RCON连接失败，已达到最大重试次数 {max_retries}")
            
    async def _try_msmp_connection(self):
        """尝试连接MSMP服务器（非阻塞）"""
        retry_count = 0
        max_retries = None
        
        while self.running and (max_retries is None or retry_count < max_retries):
            try:
                if self.msmp_client and self.msmp_client.is_authenticated():
                    self.logger.info("MSMP已连接，跳过自动重连")
                    break
                
                retry_count += 1
                self.logger.info(f"正在尝试连接MSMP服务器 {self.config_manager.get_msmp_host()}:{self.config_manager.get_msmp_port()} (第{retry_count}次)")
                
                if self.msmp_client:
                    self.msmp_client.connect_sync()
                    
                    await asyncio.sleep(2)
                    
                    if self.msmp_client.is_authenticated():
                        self.logger.info("MSMP服务器连接成功！")
                        if self.config_manager.is_server_event_notify_enabled() and self.qq_server and self.qq_server.is_connected():
                            await self.qq_server.broadcast_to_all_groups("MSMP连接已建立")
                        break
                    else:
                        self.logger.warning("MSMP服务器认证失败")
                else:
                    self.logger.error("MSMP客户端未初始化")
                    break
                    
            except Exception as e:
                self.logger.warning(f"MSMP服务器连接失败: {e}")
            
            delay = min(10 * (2 ** (retry_count - 1)), 300)
            self.logger.info(f"{delay}秒后尝试重新连接MSMP服务器...")
            await asyncio.sleep(delay)
    
    async def stop(self):
        """停止服务"""
        self.logger.info("正在停止MSMP_QQBot服务...")
        self.running = False
        
        # ============ 停止配置文件监控 ============
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