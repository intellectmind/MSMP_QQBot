import asyncio
import logging
import sys
import signal
import time
from logging.handlers import RotatingFileHandler
from config_manager import ConfigManager, ConfigValidationError
from msmp_client import MSMPClient, ServerEventListener
from rcon_client import RCONClient
from qq_bot_server import QQBotWebSocketServer

class MsmpQQBot(ServerEventListener):
    """MSMP_QQBot主程序"""
    
    def __init__(self, config_path: str = "config.yml"):
        self.config_path = config_path
        self.start_time = time.time()
        
        # 加载配置
        try:
            self.config_manager = ConfigManager(config_path)
        except ConfigValidationError as e:
            print(f"配置验证失败:\n{e}")
            sys.exit(1)
        
        # 设置日志
        self._setup_logging()
        self.logger = logging.getLogger(__name__)
        
        # 初始化组件
        self.msmp_client = None
        self.rcon_client = None
        self.qq_server = None
        
        # 事件循环
        self.loop = None
        self.running = False
        
        self.logger.info("="*50)
        self.logger.info("MSMP_QQBot 初始化完成")
        self.logger.info("="*50)
    
    def _setup_logging(self):
        """设置日志"""
        # 文件处理器 - 使用轮转
        file_handler = RotatingFileHandler(
            'msmp_qqbot.log',
            maxBytes=10*1024*1024,  # 10MB
            backupCount=5,
            encoding='utf-8'
        )
        file_handler.setFormatter(logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        ))
        
        # 控制台处理器
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(logging.Formatter(
            '%(asctime)s - %(levelname)s - %(message)s'
        ))
        
        # 配置根日志记录器
        logging.basicConfig(
            level=logging.DEBUG if self.config_manager.is_debug_mode() else logging.INFO,
            handlers=[console_handler, file_handler]
        )
    
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
            
            self.running = True
            self.logger.info("MSMP_QQBot 服务启动成功")
            
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
        max_retries = 3  # RCON只尝试3次
        
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
                delay = 10
                self.logger.info(f"{delay}秒后尝试重新连接RCON服务器...")
                await asyncio.sleep(delay)

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
            console_input_task = asyncio.create_task(self._handle_console_input())
            
            # 保持主循环运行
            while self.running:
                await asyncio.sleep(1)
                
        except KeyboardInterrupt:
            self.logger.info("收到中断信号，正在停止...")
        finally:
            console_input_task.cancel()
            await self.stop()

    async def _handle_console_input(self):
        """处理控制台输入并转发到Minecraft服务器"""
        import sys
        loop = asyncio.get_event_loop()
        
        while self.running:
            try:
                # 异步读取控制台输入
                line = await loop.run_in_executor(None, sys.stdin.readline)
                if line:
                    line = line.strip()
                    if not line:
                        continue
                    
                    # 检查是否是QQBot命令
                    if await self._handle_qqbot_command(line):
                        continue
                    
                    # 否则转发到Minecraft服务器
                    await self._forward_to_minecraft_server(line)
                        
            except Exception as e:
                self.logger.error(f"处理控制台输入失败: {e}")
                await asyncio.sleep(1)

    async def _handle_qqbot_command(self, command: str) -> bool:
        """处理QQBot命令，返回True如果是QQBot命令"""
        command_lower = command.lower()
        
        # QQBot系统命令
        if command_lower in ['status', 'exit']:
            if command_lower == 'status':
                status_info = await self._get_connection_status()
                print(status_info)
            elif command_lower == 'exit':
                self.logger.info("收到退出命令")
                self.running = False
            return True
        
        return False

    async def _forward_to_minecraft_server(self, command: str):
        """转发命令到Minecraft服务器"""
        try:
            if (self.qq_server and 
                self.qq_server.server_process and 
                self.qq_server.server_process.poll() is None):
                
                # 发送命令到服务器进程
                self.qq_server.server_process.stdin.write(command + '\n')
                self.qq_server.server_process.stdin.flush()
                self.logger.debug(f"已转发命令到服务器: {command}")
            else:
                print("错误: Minecraft服务器未运行")
                
        except Exception as e:
            self.logger.error(f"转发命令到服务器失败: {e}")

    async def _get_connection_status(self) -> str:
        """获取连接状态信息"""
        lines = ["系统连接状态:"]
        
        # QQ连接状态
        qq_status = "已连接" if (self.qq_server and self.qq_server.is_connected()) else "未连接"
        lines.append(f"QQ机器人: {qq_status}")
        
        # MSMP状态
        if self.config_manager.is_msmp_enabled():
            if self.msmp_client and self.msmp_client.is_connected():
                lines.append("MSMP: 已连接")
            else:
                lines.append("MSMP: 未连接")
        
        # RCON状态
        if self.config_manager.is_rcon_enabled():
            if self.rcon_client and self.rcon_client.is_connected():
                lines.append("RCON: 已连接")
            else:
                lines.append("RCON: 未连接")
        
        # 服务器进程状态
        if self.qq_server and self.qq_server.server_process:
            if self.qq_server.server_process.poll() is None:
                lines.append(f"服务器进程: 运行中 (PID: {self.qq_server.server_process.pid})")
            else:
                lines.append("服务器进程: 已停止")
        else:
            lines.append("服务器进程: 未启动")
        
        return "\n".join(lines)

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