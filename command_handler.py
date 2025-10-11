import time
import logging
import os
import re
from typing import Callable, Dict, List, Optional, Any
from dataclasses import dataclass
from collections import defaultdict

@dataclass
class Command:
    """命令定义"""
    names: List[str]
    handler: Callable
    admin_only: bool = False
    description: str = ""
    usage: str = ""
    cooldown: int = 0

class RateLimiter:
    """命令速率限制器"""
    def __init__(self, default_cooldown: int = 3):
        self.default_cooldown = default_cooldown
        self.last_use = defaultdict(dict)
    
    def can_use(self, user_id: int, command: str, cooldown: int = None) -> tuple:
        """检查用户是否可以使用命令"""
        if cooldown is None:
            cooldown = self.default_cooldown
        
        now = time.time()
        last_time = self.last_use[user_id].get(command, 0)
        elapsed = now - last_time
        
        if elapsed >= cooldown:
            self.last_use[user_id][command] = now
            return True, None
        else:
            remaining = int(cooldown - elapsed)
            return False, remaining
    
    def reset_user(self, user_id: int):
        """重置用户的所有冷却"""
        if user_id in self.last_use:
            del self.last_use[user_id]

class CommandHandler:
    """命令处理器"""
    
    def __init__(self, config_manager, logger: logging.Logger):
        self.config_manager = config_manager
        self.logger = logger
        self.commands: Dict[str, Command] = {}
        self.rate_limiter = RateLimiter(config_manager.get_command_cooldown())
    
    def register_command(self, 
                        names: List[str], 
                        handler: Callable,
                        admin_only: bool = False,
                        description: str = "",
                        usage: str = "",
                        cooldown: int = 0):
        """注册命令"""
        command = Command(
            names=names,
            handler=handler,
            admin_only=admin_only,
            description=description,
            usage=usage,
            cooldown=cooldown
        )
        
        for name in names:
            self.commands[name.lower()] = command
        
        self.logger.debug(f"已注册命令: {', '.join(names)}")
    
    async def handle_command(self, 
                           command_text: str,
                           user_id: int,
                           group_id: int,
                           **kwargs) -> Optional[str]:
        """处理命令"""
        command_text = command_text.strip().lower()
        
        # 查找命令
        command = self.commands.get(command_text)
        
        if not command:
            return None
        
        # 检查管理员权限
        if command.admin_only and not self.config_manager.is_admin(user_id):
            return "权限不足:此命令仅限管理员使用"
        
        # 检查冷却时间
        can_use, remaining = self.rate_limiter.can_use(
            user_id, 
            command.names[0],
            command.cooldown if command.cooldown > 0 else None
        )
        
        if not can_use:
            return f"命令冷却中,请等待 {remaining} 秒"
        
        # 执行命令
        try:
            result = await command.handler(
                user_id=user_id,
                group_id=group_id,
                command_text=command_text,
                **kwargs
            )
            return result
        except Exception as e:
            self.logger.error(f"执行命令 {command.names[0]} 时出错: {e}", exc_info=True)
            return f"命令执行失败: {str(e)}"
    
    def get_help_message(self, user_id: int, detailed: bool = False) -> str:
        """获取帮助消息"""
        is_admin = self.config_manager.is_admin(user_id)
        
        basic_commands = []
        admin_commands = []
        
        seen_commands = set()
        for name, command in self.commands.items():
            if command.names[0] not in seen_commands:
                seen_commands.add(command.names[0])
                
                if command.admin_only:
                    if is_admin:
                        admin_commands.append(command)
                else:
                    basic_commands.append(command)
        
        lines = ["MSMP_QQBot 命令帮助", "━━━━━━━━━━━━━━"]
        
        if basic_commands:
            lines.append("\n基础命令:")
            for cmd in basic_commands:
                aliases = " / ".join(cmd.names[:3])
                lines.append(f"• {aliases}")
                if cmd.description:
                    lines.append(f"  {cmd.description}")
        
        if admin_commands and is_admin:
            lines.append("\n管理员命令:")
            for cmd in admin_commands:
                aliases = " / ".join(cmd.names[:3])
                lines.append(f"• {aliases}")
                if cmd.description:
                    lines.append(f"  {cmd.description}")
            
            lines.append("\n直接命令执行:")
            lines.append("• !<命令>")
            lines.append("  管理员可使用 ! 前缀直接执行服务器命令，需启用RCON")
            lines.append("  示例: !say Hello 或 !give @a diamond")
        
        lines.append("\n━━━━━━━━━━━━━━")
        lines.append("提示: 直接输入命令,无需斜杠")
        
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
        # 不直接保存 msmp_client,而是保存 qq_server
        # 然后通过 qq_server.msmp_client 动态获取
        self.qq_server = qq_server
        self.rcon_client = rcon_client
        self.config_manager = config_manager
        self.logger = logger
    
    @property
    def msmp_client(self):
        """动态获取 msmp_client"""
        return self.qq_server.msmp_client if self.qq_server else None
    
    def _get_active_client(self):
        """获取当前可用的客户端(优先MSMP,其次RCON)"""
        # 如果MSMP已可用且连接正常,使用MSMP
        if (self.config_manager.is_msmp_enabled() and 
            self.msmp_client and 
            self.msmp_client.is_connected()):
            return 'msmp', self.msmp_client
        
        # 否则尝试使用RCON
        if (self.config_manager.is_rcon_enabled() and 
            self.rcon_client and 
            self.rcon_client.is_connected()):
            return 'rcon', self.rcon_client
        
        return None, None
    
    async def handle_list(self, **kwargs) -> str:
        """处理list命令 - 支持MSMP和RCON自动切换"""
        try:
            client_type, client = self._get_active_client()
            
            if not client:
                return "服务器连接未就绪(MSMP和RCON均未连接)"
            
            # 获取玩家列表
            try:
                if client_type == 'msmp':
                    player_info = client.get_player_list_sync()
                else:  # rcon
                    player_info = client.get_player_list()
            except Exception as e:
                self.logger.error(f"获取玩家列表失败 ({client_type}): {e}")
                return f"获取玩家列表失败: {str(e)}"
            
            lines = [f"在线人数: {player_info.current_players}/{player_info.max_players}"]
            
            if player_info.current_players > 0 and player_info.player_names:
                lines.append("\n在线玩家:")
                for i, player_name in enumerate(player_info.player_names, 1):
                    lines.append(f"{i}. {player_name.strip()}")
            else:
                lines.append("\n暂无玩家在线")
            
            # 添加连接方式标识
            lines.append(f"\n[通过 {client_type.upper()} 查询]")
            
            return "\n".join(lines)
            
        except Exception as e:
            self.logger.error(f"执行list命令失败: {e}", exc_info=True)
            return f"获取玩家列表失败: {e}"
    
    async def handle_status(self, **kwargs) -> str:
        """处理status命令 - 显示所有连接状态"""
        try:
            qq_status = "已连接" if self.qq_server.is_connected() else "未连接"
            
            # MSMP状态
            msmp_status = "未启用"
            if self.config_manager.is_msmp_enabled():
                if not self.msmp_client:
                    msmp_status = "客户端未初始化"
                elif not self.msmp_client.is_connected():
                    msmp_status = "未连接"
                else:
                    try:
                        status = self.msmp_client.get_server_status_sync()
                        started = status.get('started', False)
                        version = status.get('version', {})
                        version_name = version.get('name', 'Unknown')
                        
                        player_info = self.msmp_client.get_player_list_sync()
                        
                        msmp_status = (
                            f"运行中\n"
                            f"版本: {version_name}\n"
                            f"在线: {player_info.current_players}/{player_info.max_players}"
                        )
                    except Exception as e:
                        msmp_status = f"连接异常: {e}"
            
            # RCON状态
            rcon_status = "未启用"
            if self.config_manager.is_rcon_enabled():
                if not self.rcon_client:
                    rcon_status = "客户端未初始化"
                elif not self.rcon_client.is_connected():
                    rcon_status = "未连接"
                else:
                    try:
                        player_info = self.rcon_client.get_player_list()
                        rcon_status = f"运行中\n在线: {player_info.current_players}/{player_info.max_players}"
                    except Exception as e:
                        rcon_status = f"连接异常: {e}"
            
            return (
                "系统状态\n"
                "━━━━━━━━━━━━━━\n"
                f"QQ机器人: {qq_status}\n"
                f"MSMP连接:\n{msmp_status}\n"
                f"RCON连接:\n{rcon_status}\n"
                "━━━━━━━━━━━━━━"
            )
            
        except Exception as e:
            self.logger.error(f"执行status命令失败: {e}", exc_info=True)
            return f"获取状态失败: {e}"
    
    async def handle_help(self, user_id: int, **kwargs) -> str:
        """处理help命令"""
        from command_handler import CommandHandler
        if hasattr(self.qq_server, 'command_handler'):
            return self.qq_server.command_handler.get_help_message(user_id)
        return "帮助系统未初始化"
    
    async def handle_stop(self, user_id: int, group_id: int, websocket, is_private: bool = False, **kwargs) -> str:
        """处理stop命令(管理员) - 支持MSMP和RCON"""
        try:
            client_type, client = self._get_active_client()
            
            if not client:
                return "服务器连接未就绪,无法执行停止命令"
            
            # 发送执行中提示
            if is_private:
                await self.qq_server.send_private_message(websocket, user_id, "正在停止服务器...")
            else:
                await self.qq_server.send_group_message(websocket, group_id, "正在停止服务器...")
            
            if client_type == 'msmp':
                # 检查服务器状态
                try:
                    status = client.get_server_status_sync()
                    if not status.get('started', False):
                        return "服务器已经是停止状态"
                except Exception as e:
                    self.logger.warning(f"获取服务器状态失败: {e}")
                
                result = client.execute_command_sync("server/stop")
                
                if 'result' in result:
                    return None  # 不返回消息
                else:
                    error_msg = result.get('error', {}).get('message', '未知错误')
                    return f"停止服务器失败: {error_msg}"
            
            else:  # rcon
                success = client.stop_server()
                if success:
                    return None  # 不返回消息
                else:
                    return "停止服务器失败"
                
        except Exception as e:
            self.logger.error(f"执行stop命令失败: {e}", exc_info=True)
            return f"停止服务器失败: {e}"
    
    async def handle_start(self, user_id: int, group_id: int, websocket, is_private: bool = False, **kwargs) -> str:
        """处理start命令(管理员)"""
        try:
            # 检查是否已有服务器进程在运行
            if self.qq_server.server_process and self.qq_server.server_process.poll() is None:
                return "服务器已经在启动或运行中"
            
            # 获取启动脚本路径
            start_script = self.config_manager.get_server_start_script()
            if not start_script:
                return (
                    "服务器启动脚本未配置\n"
                    "请在 config.yml 中配置 server.start_script"
                )
            
            if not os.path.exists(start_script):
                return f"❌ 启动脚本不存在: {start_script}"
            
            # 发送执行中提示
            if is_private:
                await self.qq_server.send_private_message(websocket, user_id, "正在启动服务器...")
            else:
                await self.qq_server.send_group_message(websocket, group_id, "正在启动服务器...")
            
            # 调用qq_server的启动方法
            await self.qq_server._start_server_process(websocket, group_id)
            
            return None  # 不返回消息
            
        except Exception as e:
            self.logger.error(f"执行start命令失败: {e}")
            return f"启动服务器失败: {e}"
    
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
            # 检查是否有服务器进程在运行
            server_running = self.qq_server.server_process and self.qq_server.server_process.poll() is None
            
            # 获取最近的日志（优先从内存获取）
            recent_logs = self.qq_server.get_recent_logs(20)
            
            if not recent_logs:
                # 如果内存中没有日志，尝试从文件读取
                file_logs = self._read_recent_logs_from_file(10)
                if file_logs:
                    lines = [f"最近 {len(file_logs)} 条服务器日志 (从文件读取):"]
                    lines.append("━━━━━━━━━━━━━━")
                    lines.extend(file_logs)
                    lines.append("━━━━━━━━━━━━━━")
                    lines.append("提示: 服务器当前未运行，显示的是历史日志")
                    return "\n".join(lines)
                else:
                    return "暂无服务器日志输出"
            
            # 构建响应消息
            status = "运行中" if server_running else "已停止"
            lines = [f"最近 {len(recent_logs)} 条服务器日志 (服务器{status}):"]
            lines.append("━━━━━━━━━━━━━━")
            
            for log in recent_logs:
                # 限制单条日志长度，避免消息过长
                if len(log) > 100:
                    log = log[:100] + "..."
                lines.append(log)
            
            lines.append("━━━━━━━━━━━━━━")
            if server_running:
                lines.append("提示: 日志实时更新，再次发送 log 查看最新日志")
            else:
                lines.append("提示: 服务器已停止，日志不再更新")
            
            return "\n".join(lines)
            
        except Exception as e:
            self.logger.error(f"执行log命令失败: {e}", exc_info=True)
            return f"获取日志失败: {e}"

    def _read_recent_logs_from_file(self, lines: int = 10) -> List[str]:
        """从日志文件读取最近的日志"""
        try:
            log_file_path = "mc_server.log"
            if not os.path.exists(log_file_path):
                return []
            
            # 读取文件最后几行
            with open(log_file_path, 'r', encoding='utf-8') as f:
                # 简单的方法：读取所有行然后取最后几行
                all_lines = f.readlines()
                recent_lines = all_lines[-lines:] if len(all_lines) >= lines else all_lines
                return [line.strip() for line in recent_lines if line.strip()]
                
        except Exception as e:
            self.logger.error(f"读取日志文件失败: {e}")
            return []