import asyncio
import logging
import sys
import signal
import time
import os
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler
from config_manager import ConfigManager, ConfigValidationError
from msmp_client import MSMPClient, ServerEventListener
from rcon_client import RCONClient
from qq_bot_server import QQBotWebSocketServer

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
        import sys
        loop = asyncio.get_event_loop()
        
        while self.running and self.bot.running:
            try:
                # 异步读取控制台输入
                line = await loop.run_in_executor(None, sys.stdin.readline)
                if line:
                    line = line.strip()
                    if not line:
                        continue
                    
                    # 优先处理Bot系统命令
                    if await self._handle_bot_command(line):
                        continue
                    
                    # 否则转发到Minecraft服务器
                    await self._forward_to_minecraft(line)
                        
            except Exception as e:
                self.logger.error(f"处理控制台输入失败: {e}")
                await asyncio.sleep(1)
    
    async def _handle_bot_command(self, command: str) -> bool:
        """
        处理Bot系统命令
        返回 True 表示已处理，False 表示需要转发到服务器
        """
        command_lower = command.lower()
        
        # Bot系统命令
        if command_lower == 'status':
            status_info = await self._get_connection_status()
            print(status_info)
            return True
        
        elif command_lower == 'exit':
            self.logger.info("收到退出命令")
            self.running = False
            self.bot.running = False
            return True
        
        elif command_lower == 'help':
            self._print_console_help()
            return True
        
        elif command_lower == 'reload':
            try:
                self.bot.config_manager.reload()
                print("✓ 配置已重新加载")
            except Exception as e:
                print(f"✗ 重新加载配置失败: {e}")
            return True
        
        elif command_lower == 'logs':
            self._show_logs_info()
            return True
        
        # 不是系统命令，返回 False 让它转发到服务器
        return False
    
    async def _forward_to_minecraft(self, command: str):
        """转发命令到Minecraft服务器"""
        try:
            if (self.bot.qq_server and 
                self.bot.qq_server.server_process and 
                self.bot.qq_server.server_process.poll() is None):
                
                # 发送命令到服务器进程
                self.bot.qq_server.server_process.stdin.write(command + '\n')
                self.bot.qq_server.server_process.stdin.flush()
                self.logger.debug(f"已转发命令到服务器: {command}")
            else:
                print("错误: Minecraft服务器未运行")
                
        except Exception as e:
            self.logger.error(f"转发命令到服务器失败: {e}")
            print(f"错误: 转发命令失败 - {e}")
    
    async def _get_connection_status(self) -> str:
        """获取连接状态信息"""
        lines = ["系统连接状态:"]
        
        # QQ连接状态
        qq_status = "已连接" if (self.bot.qq_server and self.bot.qq_server.is_connected()) else "未连接"
        lines.append(f"QQ机器人: {qq_status}")
        
        # MSMP状态
        if self.bot.config_manager.is_msmp_enabled():
            if self.bot.msmp_client and self.bot.msmp_client.is_connected():
                lines.append("MSMP: 已连接")
            else:
                lines.append("MSMP: 未连接")
        
        # RCON状态
        if self.bot.config_manager.is_rcon_enabled():
            if self.bot.rcon_client and self.bot.rcon_client.is_connected():
                lines.append("RCON: 已连接")
            else:
                lines.append("RCON: 未连接")
        
        # 服务器进程状态
        if self.bot.qq_server and self.bot.qq_server.server_process:
            if self.bot.qq_server.server_process.poll() is None:
                lines.append(f"服务器进程: 运行中 (PID: {self.bot.qq_server.server_process.pid})")
            else:
                lines.append("服务器进程: 已停止")
        else:
            lines.append("服务器进程: 未启动")
        
        return "\n".join(lines)
    
    def _print_console_help(self):
        """打印控制台帮助信息"""
        print("""
控制台命令帮助
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
系统命令:
  status    - 查看连接状态
  reload    - 重新加载配置
  logs      - 显示日志文件信息
  help      - 显示此帮助
  exit      - 退出程序

服务器命令:
  直接输入任意命令将转发到Minecraft服务器
  例如: say Hello 或 list

提示:
  - 服务器必须处于运行状态才能接收命令
  - 所有非系统命令都会被转发到服务器
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
""")
    
    def _show_logs_info(self):
        """显示日志文件信息"""
        log_dir = "logs"
        if not os.path.exists(log_dir):
            print("日志目录不存在")
            return
        
        print("日志文件信息:")
        print("━━━━━━━━━━━━━━")
        for file in os.listdir(log_dir):
            if file.endswith('.log'):
                file_path = os.path.join(log_dir, file)
                size = os.path.getsize(file_path)
                mtime = time.strftime('%Y-%m-%d %H:%M:%S', 
                                    time.localtime(os.path.getmtime(file_path)))
                print(f"{file}: {size/1024/1024:.2f} MB, 修改时间: {mtime}")
        print("━━━━━━━━━━━━━━")
    
    def stop(self):
        """停止控制台处理"""
        self.running = False


class MsmpQQBot(ServerEventListener):
    """MSMP_QQBot主程序"""
    
    def __init__(self, config_path: str = "config.yml"):
        self.config_path = config_path
        self.start_time = time.time()
        
        # 先设置基础日志，用于配置加载错误
        self._setup_basic_logging()
        
        # 加载配置
        try:
            self.config_manager = ConfigManager(config_path)
        except ConfigValidationError as e:
            print(f"配置验证失败:\n{e}")
            sys.exit(1)
        
        # 设置完整日志系统
        self._setup_logging()
        self.logger = logging.getLogger(__name__)
        
        # 初始化组件
        self.msmp_client = None
        self.rcon_client = None
        self.qq_server = None
        
        # 控制台处理器
        self.console_handler = None
        
        # 事件循环
        self.loop = None
        self.running = False
        
        self.logger.info("="*50)
        self.logger.info("MSMP_QQBot 初始化完成")
        self.logger.info("="*50)
    
    def _setup_basic_logging(self):
        """设置基础日志（用于配置加载阶段）"""
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            handlers=[logging.StreamHandler(sys.stdout)]
        )
    
    def _setup_logging(self):
        """设置完整的日志系统"""
        # 创建日志目录
        log_dir = "logs"
        os.makedirs(log_dir, exist_ok=True)
        
        # 获取根日志记录器
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.DEBUG if self.config_manager.is_debug_mode() else logging.INFO)
        
        # 清除现有的处理器
        for handler in root_logger.handlers[:]:
            root_logger.removeHandler(handler)
        
        # 1. 主日志文件 - 按大小轮转
        main_handler = RotatingFileHandler(
            os.path.join(log_dir, 'msmp_qqbot.log'),
            maxBytes=10*1024*1024,  # 10MB
            backupCount=5,
            encoding='utf-8'
        )
        main_handler.setLevel(logging.DEBUG)
        
        # 2. 错误日志文件 - 单独记录错误
        error_handler = RotatingFileHandler(
            os.path.join(log_dir, 'error.log'),
            maxBytes=5*1024*1024,   # 5MB
            backupCount=3,
            encoding='utf-8'
        )
        error_handler.setLevel(logging.ERROR)
        
        # 3. 控制台处理器
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.INFO)
        
        # 4. 运行日志 - 按天轮转
        runtime_handler = TimedRotatingFileHandler(
            os.path.join(log_dir, 'runtime.log'),
            when='midnight',  # 每天轮转
            interval=1,
            backupCount=7,    # 保留7天
            encoding='utf-8'
        )
        runtime_handler.setLevel(logging.INFO)
        
        # 设置日志格式
        detailed_formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s'
        )
        simple_formatter = logging.Formatter(
            '%(asctime)s - %(levelname)s - %(message)s'
        )
        
        # 应用格式
        main_handler.setFormatter(detailed_formatter)
        error_handler.setFormatter(detailed_formatter)
        console_handler.setFormatter(simple_formatter)
        runtime_handler.setFormatter(simple_formatter)
        
        # 添加处理器到根日志记录器
        root_logger.addHandler(main_handler)
        root_logger.addHandler(error_handler)
        root_logger.addHandler(console_handler)
        root_logger.addHandler(runtime_handler)
        
        # 设置特定库的日志级别
        logging.getLogger('websockets').setLevel(logging.WARNING)
        logging.getLogger('asyncio').setLevel(logging.WARNING)
        
        self.logger = logging.getLogger(__name__)
        self.logger.info("日志系统初始化完成")
    
    async def start(self):
        """启动服务"""
        self.logger.info("MSMP_QQBot 服务启动中...")
        
        # 设置主事件循环
        self.loop = asyncio.get_running_loop()
        
        try:
            # 先初始化QQ机器人WebSocket服务器
            ws_token = self.config_manager.get_websocket_token() if self.config_manager.is_websocket_auth_enabled() else ""
            
            # 初始化RCON客户端（如果启用）
            if self.config_manager.is_rcon_enabled():
                self.rcon_client = RCONClient(
                    self.config_manager.get_rcon_host(),
                    self.config_manager.get_rcon_port(),
                    self.config_manager.get_rcon_password(),
                    self.logger
                )
                self.logger.info("RCON客户端已初始化")
            
            self.qq_server = QQBotWebSocketServer(
                self.config_manager.get_ws_port(),
                self.config_manager.get_qq_groups(),
                None,  # 先不传递msmp_client
                self.logger,
                ws_token,
                self.config_manager,
                self.rcon_client  # 传递rcon_client
            )
            
            # 启动QQ机器人WebSocket服务器
            await self.qq_server.start()
            self.logger.info("QQ机器人服务器已启动")
            
            # 然后初始化MSMP客户端（如果启用，但不立即连接）
            if self.config_manager.is_msmp_enabled():
                self.msmp_client = MSMPClient(
                    self.config_manager.get_msmp_host(),
                    self.config_manager.get_msmp_port(),
                    self.config_manager.get_msmp_password(),
                    self.logger,
                    self.config_manager
                )
                
                # 启动MSMP客户端后台循环
                self.msmp_client.start_background_loop()
                
                # 设置MSMP事件监听器
                self.msmp_client.set_event_listener(self)
                
                # 立即设置到QQ服务器，这样即使连接失败也能处理命令
                self.qq_server.msmp_client = self.msmp_client
                
                # 延迟尝试连接MSMP服务器（非阻塞）
                asyncio.create_task(self._delayed_msmp_connect())
            else:
                self.logger.info("MSMP未启用，跳过MSMP客户端初始化")
            
            # 尝试连接RCON（如果启用）
            if self.rcon_client:
                asyncio.create_task(self._try_rcon_connection())
            
            # 初始化控制台命令处理器
            self.console_handler = ConsoleCommandHandler(self, self.logger)
            
            self.running = True
            self.logger.info("MSMP_QQBot 服务启动成功")
            self.logger.info("提示: 输入 'help' 查看控制台命令")
            
        except Exception as e:
            self.logger.error(f"启动服务失败: {e}", exc_info=True)
            await self.stop()
            raise

    async def _delayed_msmp_connect(self):
        """延迟连接MSMP服务器"""
        # 等待一段时间让WebSocket服务器稳定
        await asyncio.sleep(3)
        self.logger.info("准备连接MSMP服务器...")
        
        # 尝试连接MSMP服务器（非阻塞）
        asyncio.create_task(self._try_msmp_connection())
    
    async def _try_rcon_connection(self):
        """尝试连接RCON服务器"""
        await asyncio.sleep(3)
        self.logger.info("准备连接RCON服务器...")
        
        retry_count = 0
        max_retries = 5  # 增加重试次数
        
        while self.running and retry_count < max_retries:
            try:
                retry_count += 1
                self.logger.info(f"正在尝试连接RCON服务器 {self.config_manager.get_rcon_host()}:{self.config_manager.get_rcon_port()} (第{retry_count}次)")
                
                if self.rcon_client.connect():
                    self.logger.info("RCON服务器连接成功！")
                    # 发送连接成功通知
                    if self.config_manager.is_server_event_notify_enabled() and self.qq_server and self.qq_server.is_connected():
                        await self.qq_server.broadcast_to_all_groups("RCON连接已建立")
                    break
                else:
                    self.logger.warning("RCON服务器连接失败")
                    
            except Exception as e:
                self.logger.warning(f"RCON服务器连接失败: {e}")
            
            if retry_count < max_retries:
                delay = min(10 * retry_count, 30)  # 递增延迟，最多30秒
                self.logger.info(f"{delay}秒后尝试重新连接RCON服务器...")
                await asyncio.sleep(delay)
        else:
            if retry_count >= max_retries:
                self.logger.warning(f"RCON连接失败，已达到最大重试次数 {max_retries}")
            
    async def _try_msmp_connection(self):
        """尝试连接MSMP服务器（非阻塞）"""
        retry_count = 0
        max_retries = None  # 无限重试
        
        while self.running and (max_retries is None or retry_count < max_retries):
            try:
                # 检查是否已经连接（可能是服务器启动后连接的）
                if self.msmp_client and self.msmp_client.is_authenticated():
                    self.logger.info("MSMP已连接，跳过自动重连")
                    break
                
                retry_count += 1
                self.logger.info(f"正在尝试连接MSMP服务器 {self.config_manager.get_msmp_host()}:{self.config_manager.get_msmp_port()} (第{retry_count}次)")
                
                if self.msmp_client:
                    self.msmp_client.connect_sync()
                    
                    # 等待连接稳定
                    await asyncio.sleep(2)
                    
                    if self.msmp_client.is_authenticated():
                        self.logger.info("MSMP服务器连接成功！")
                        # 发送连接成功通知
                        if self.config_manager.is_server_event_notify_enabled() and self.qq_server and self.qq_server.is_connected():
                            await self.qq_server.broadcast_to_all_groups("MSMP连接已建立")
                        break  # 连接成功，退出循环
                    else:
                        self.logger.warning("MSMP服务器认证失败")
                else:
                    self.logger.error("MSMP客户端未初始化")
                    break
                    
            except Exception as e:
                self.logger.warning(f"MSMP服务器连接失败: {e}")
            
            # 计算重试延迟（指数退避，最大5分钟）
            delay = min(10 * (2 ** (retry_count - 1)), 300)
            self.logger.info(f"{delay}秒后尝试重新连接MSMP服务器...")
            await asyncio.sleep(delay)
    
    async def stop(self):
        """停止服务"""
        self.logger.info("正在停止MSMP_QQBot服务...")
        self.running = False
        
        # 停止控制台处理器
        if self.console_handler:
            self.console_handler.stop()
        
        try:
            # 发送停止通知
            if (self.qq_server and 
                self.config_manager.is_server_event_notify_enabled() and
                self.qq_server.is_connected()):
                await self.qq_server.broadcast_to_all_groups("MSMP_QQBot 已断开连接")
            
            # 停止QQ服务器
            if self.qq_server:
                await self.qq_server.stop()
            
            # 关闭MSMP连接
            if self.msmp_client:
                self.msmp_client.close_sync()
            
            # 关闭RCON连接
            if self.rcon_client:
                self.rcon_client.close()
            
            self.logger.info("MSMP_QQBot 服务已停止")
            
        except Exception as e:
            self.logger.error(f"停止服务时出错: {e}", exc_info=True)
    
    # ServerEventListener 实现
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
        # 根据MSMP协议，玩家信息在params中直接包含name字段
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
        # 根据MSMP协议，玩家信息在params中直接包含name字段
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
            # 启动控制台输入处理任务
            console_input_task = asyncio.create_task(self.console_handler.handle_console_input())
            
            # 保持主循环运行
            while self.running:
                await asyncio.sleep(1)
                
        except KeyboardInterrupt:
            self.logger.info("收到中断信号，正在停止...")
        finally:
            console_input_task.cancel()
            await self.stop()


def main():
    """主函数"""
    print("="*50)
    print("  MSMP_QQBot - Minecraft Server QQ Bridge")
    print("="*50)
    
    bridge = MsmpQQBot()
    
    try:
        # 设置信号处理
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        def signal_handler(signum, frame):
            print("\n收到停止信号，正在关闭...")
            if bridge.running:
                asyncio.create_task(bridge.stop())
        
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
        
        # 运行异步主循环
        loop.run_until_complete(bridge.run_async())
        
    except Exception as e:
        logging.error(f"程序运行出错: {e}", exc_info=True)
        sys.exit(1)
    finally:
        loop.close()


if __name__ == "__main__":
    main()