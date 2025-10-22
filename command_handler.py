import time
import logging
import os
import re
import asyncio
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
    command_key: str = ""

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
                       **kwargs) -> Optional[str]:
        """处理命令"""
        command_text = command_text.strip().lower()
        
        # 查找命令
        command = self.commands.get(command_text)
        
        if not command:
            return None
        
        # 检查命令是否启用
        is_admin = self.config_manager.is_admin(user_id)
        
        if command.admin_only:
            # 管理员命令权限检查 - 管理员不受限制
            if not is_admin and not self.config_manager.is_admin_command_enabled(command.names[0]):
                return f"命令 {command.names[0]} 已被禁用"
        else:
            # 基础命令权限检查（管理员不受限制）
            if not is_admin and command.command_key:
                if not self.config_manager.is_command_enabled(command.command_key):
                    return None  # 命令被禁用,静默返回
        
        # 检查管理员权限
        if command.admin_only and not is_admin:
            # 如果非管理员要使用管理员命令，需要检查是否开放
            if not self.config_manager.is_admin_command_enabled(command.names[0]):
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
            # 根据命令类型设置不同的超时时间
            if command.admin_only and command.names[0] in ['start', 'stop', 'log', 'reconnect']:
                timeout = 60.0  # 管理命令给更长时间
            else:
                timeout = 30.0  # 普通命令30秒
            
            result = await asyncio.wait_for(
                command.handler(
                    user_id=user_id,
                    group_id=group_id,
                    command_text=command_text,
                    **kwargs
                ),
                timeout=timeout
            )
            return result
            
        except asyncio.TimeoutError:
            self.logger.error(f"命令 {command.names[0]} 执行超时 ({timeout}秒)")
            return f"命令执行超时,请稍后重试或联系管理员"
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
                    # 管理员命令：管理员始终可见，非管理员只在启用时可见
                    if is_admin or self.config_manager.is_admin_command_enabled(command.names[0]):
                        admin_commands.append(command)
                else:
                    # 基础命令：管理员始终可见，非管理员只在启用时可见
                    if is_admin or (command.command_key and self.config_manager.is_command_enabled(command.command_key)):
                        basic_commands.append(command)
        
        lines = ["MSMP_QQBot 命令帮助", "━━━━━━━━━━━━━━"]
        
        if basic_commands:
            lines.append("\n基础命令:")
            for cmd in basic_commands:
                aliases = " / ".join(cmd.names[:3])
                lines.append(f"• {aliases}")
                if cmd.description:
                    lines.append(f"  {cmd.description}")
        
        if admin_commands:
            lines.append("\n管理员命令:")
            for cmd in admin_commands:
                aliases = " / ".join(cmd.names[:3])
                lines.append(f"• {aliases}")
                if cmd.description:
                    lines.append(f"  {cmd.description}")
            
            lines.append("\n直接命令执行:")
            lines.append("• !<命令>")
            lines.append("  可使用 ! 前缀直接执行服务器命令,需启用RCON")
            lines.append("  示例: !say Hello 或 !give @a diamond")
                
        # 显示禁用命令提示（只对管理员显示）
        if is_admin:
            disabled_commands = []
            
            # 基础命令禁用列表
            for cmd_key, enabled in self.config_manager.get_enabled_commands().items():
                if not enabled:
                    disabled_commands.append(cmd_key)
            
            # 管理员命令禁用列表（对非管理员）
            for cmd_key, enabled in self.config_manager.get_enabled_admin_commands().items():
                if not enabled:
                    disabled_commands.append(f"{cmd_key}(非管理员)")
            
            if disabled_commands:
                lines.append(f"\n已禁用命令: {', '.join(disabled_commands)}")
                lines.append("(管理员仍可使用所有命令)")
        
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
        self._stop_lock = asyncio.Lock()
        self._is_stopping = False
    
    @property
    def msmp_client(self):
        """动态获取 msmp_client"""
        return self.qq_server.msmp_client if self.qq_server else None
    
    def _get_active_client(self):
        """获取当前可用的客户端(优先MSMP,其次RCON)"""
        # 如果MSMP已启用且连接正常,使用MSMP
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
                player_list = "    ".join(player_info.player_names)
                lines.append(f"在线玩家:\n{player_list}")
            else:
                lines.append("\n暂无玩家在线")
            
            # 添加连接方式标识
            lines.append(f"\n[通过 {client_type.upper()} 查询]")
            
            return "\n".join(lines)
            
        except Exception as e:
            self.logger.error(f"执行list命令失败: {e}", exc_info=True)
            return f"获取玩家列表失败: {e}"
    
    async def handle_tps(self, **kwargs) -> str:
        """处理tps命令 - 仅通过RCON执行"""
        try:
            # TPS命令只能通过RCON执行
            if not self.config_manager.is_rcon_enabled():
                return "TPS命令需要启用RCON连接"
            
            if not self.rcon_client or not self.rcon_client.is_connected():
                return "RCON连接未就绪"
            
            # 从配置获取TPS命令
            tps_command = self.config_manager.get_tps_command()
            
            self.logger.info(f"执行TPS命令: {tps_command}")
            
            # 执行命令
            result = self.rcon_client.execute_command(tps_command)
            
            if result:
                # 清理RCON返回的颜色代码
                cleaned = re.sub(r'§[0-9a-fk-or]', '', result).strip()
                return f"服务器TPS信息:\n{cleaned}\n\n[通过 RCON 查询]"
            else:
                return "TPS命令执行成功,但无返回结果"
                
        except Exception as e:
            self.logger.error(f"执行TPS命令失败: {e}", exc_info=True)
            return f"获取TPS信息失败: {e}"
    
    async def handle_rules(self, **kwargs) -> str:
        """处理rules命令 - 仅通过MSMP查询游戏规则"""
        try:
            # 游戏规则只能通过MSMP查询
            if not self.config_manager.is_msmp_enabled():
                return "规则查询需要启用MSMP连接"
            
            if not self.msmp_client or not self.msmp_client.is_connected():
                return "MSMP连接未就绪"
            
            self.logger.info("查询服务器规则...")
            
            lines = ["服务器规则信息", "━━━━━━━━━━━━━━"]
            
            try:
                # 1. 获取所有游戏规则 - 使用正确的方法名
                gamerules_result = await self.msmp_client.get_game_rules()
                
                if 'result' in gamerules_result and isinstance(gamerules_result['result'], list):
                    gamerules_list = gamerules_result['result']
                    
                    # 将规则列表转换为字典，方便查询
                    gamerules_dict = {}
                    for rule in gamerules_list:
                        if isinstance(rule, dict) and 'key' in rule and 'value' in rule:
                            gamerules_dict[rule['key']] = rule['value']
                    
                    # 常用游戏规则列表
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
                        'announceAdvancements': '成就通告',
                        'showDeathMessages': '显示死亡信息'
                    }
                    
                    rules_found = False
                    for rule_key, rule_name in important_rules.items():
                        if rule_key in gamerules_dict:
                            if not rules_found:
                                lines.append("\n游戏规则:")
                                rules_found = True
                            
                            value = gamerules_dict[rule_key]
                            # 格式化布尔值
                            if isinstance(value, bool):
                                value_str = "启用" if value else "禁用"
                            elif isinstance(value, str) and value.lower() in ['true', 'false']:
                                value_str = "启用" if value.lower() == 'true' else "禁用"
                            else:
                                value_str = str(value)
                            lines.append(f"• {rule_name}: {value_str}")
                
                # 2. 获取服务器设置
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
                        # 使用正确的MSMP方法查询服务器设置
                        result = await self.msmp_client.send_request(f"serversettings/{setting_key}")
                        
                        if 'result' in result:
                            if not settings_found:
                                lines.append("\n服务器设置:")
                                settings_found = True
                            
                            value = result['result']
                            
                            if value is not None:
                                # 特殊处理不同类型的值
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
                                        value_str = f"{value} 秒"
                                else:
                                    value_str = str(value)
                                
                                lines.append(f"• {setting_name}: {value_str}")
                    except Exception as e:
                        self.logger.debug(f"查询设置 {setting_key} 失败: {e}")
                        continue
                
                # 3. 查询白名单状态
                try:
                    # 先检查白名单是否启用
                    use_allowlist_result = await self.msmp_client.send_request("serversettings/use_allowlist")
                    
                    if 'result' in use_allowlist_result:
                        if not settings_found:
                            lines.append("\n服务器设置:")
                            settings_found = True
                        
                        use_allowlist = use_allowlist_result['result']
                        
                        if use_allowlist:
                            # 如果启用了白名单，获取白名单玩家列表
                            try:
                                allowlist_result = await self.msmp_client.send_request("allowlist")
                                if 'result' in allowlist_result:
                                    players = allowlist_result['result']
                                    if isinstance(players, list):
                                        lines.append(f"• 白名单: 启用 ({len(players)} 个玩家)")
                                    else:
                                        lines.append("• 白名单: 启用")
                            except:
                                lines.append("• 白名单: 启用")
                        else:
                            lines.append("• 白名单: 关闭")
                except Exception as e:
                    self.logger.debug(f"查询白名单状态失败: {e}")
                
                # 如果没有获取到任何信息
                if len(lines) == 2:
                    lines.append("\n未能获取到规则信息")
                    lines.append("提示: MSMP连接正常但无法获取规则数据")
                    lines.append("可能原因:")
                    lines.append("1. MSMP插件版本过旧")
                    lines.append("2. 服务器权限配置问题")
                    lines.append("3. 查看服务器日志了解详情")
                
                lines.append("\n━━━━━━━━━━━━━━")
                lines.append("[通过 MSMP 查询]")
                
                return "\n".join(lines)
                
            except Exception as e:
                self.logger.error(f"获取规则信息失败: {e}", exc_info=True)
                return f"获取规则信息失败: {str(e)}\n提示: 请检查MSMP插件版本和配置"
                
        except Exception as e:
            self.logger.error(f"执行rules命令失败: {e}", exc_info=True)
            return f"查询服务器规则失败: {e}"
    
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
                f"MSMP连接: {msmp_status}\n"
                f"RCON连接: {rcon_status}\n"
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
        
        # 获取锁，确保 stop 命令只执行一次
        async with self._stop_lock:
            # 检查是否已经在停止中
            if self._is_stopping:
                return "服务器已在停止中，请勿重复执行"
            
            self._is_stopping = True
        
        try:
            client_type, client = self._get_active_client()
            
            if not client:
                self._is_stopping = False
                return "服务器连接未就绪，无法执行停止命令"
            
            # 发送执行中止消息
            if is_private:
                await self.qq_server.send_private_message(websocket, user_id, "正在停止服务器...")
            else:
                await self.qq_server.send_group_message(websocket, group_id, "正在停止服务器...")
            
            stop_success = False
            stop_method = ""
            
            try:
                if client_type == 'msmp':
                    stop_method = "MSMP"
                    # MSMP: 执行停止命令
                    result = client.execute_command_sync("server/stop")
                    
                    if 'result' not in result:
                        error_msg = result.get('error', {}).get('message', '未知错误')
                        self.logger.error(f"MSMP停止命令失败: {error_msg}")
                    else:
                        stop_success = True
                        self.logger.info("MSMP 停止命令已发送")
                    
                elif client_type == 'rcon':
                    stop_method = "RCON"
                    # RCON: 执行停止命令
                    result = client.execute_command("stop")
                    
                    if result is None:
                        # RCON执行stop命令后连接会断开，返回None是正常的
                        stop_success = True
                        self.logger.info("RCON停止命令已发送(连接正常断开)")
                    else:
                        stop_success = True
                        self.logger.info("RCON停止命令已发送")
                    
                    # RCON停止命令发送后立即关闭连接，避免后续错误
                    if self.rcon_client and self.rcon_client.is_connected():
                        try:
                            self.rcon_client.close()
                            self.logger.info("已关闭RCON连接")
                        except Exception as e:
                            self.logger.debug(f"关闭RCON连接时出错: {e}")
            
            except Exception as e:
                self.logger.warning(f"执行停止命令时出现异常(可能是正常断开): {e}")
                # 对于stop命令，某些异常可能是正常的（如连接断开）
                stop_success = True
            
            if not stop_success:
                self._is_stopping = False
                return f"停止服务器失败: 无法通过{stop_method}发送停止命令"
            
            self.logger.info("停止命令已发送，等待服务器关闭进程...")
            
            # 等待服务器进程结束（最多60秒）
            max_wait_time = 60
            wait_interval = 5
            waited_time = 0
            
            while (waited_time < max_wait_time and 
                   self.qq_server.server_process and 
                   self.qq_server.server_process.poll() is None):
                await asyncio.sleep(wait_interval)
                waited_time += wait_interval
                self.logger.info(f"等待服务器关闭... ({waited_time}/{max_wait_time}秒)")
            
            # 检查服务器进程状态
            server_stopped = True
            if self.qq_server.server_process:
                return_code = self.qq_server.server_process.poll()
                if return_code is None:
                    server_stopped = False
                    self.logger.warning(f"服务器进程在{max_wait_time}秒后仍未关闭")
                else:
                    self.logger.info(f"服务器进程已关闭，返回码: {return_code}")
            
            # 无论服务器是否完全关闭，都执行清理操作
            self.logger.info("执行清理操作...")
            await self._close_all_connections()
            
            if server_stopped:
                return "服务器已成功关闭"
            else:
                return "停止命令已发送，但服务器进程仍在运行中。可能需要手动检查或使用 #kill 命令强制停止"
            
        except Exception as e:
            self.logger.error(f"执行stop命令失败: {e}", exc_info=True)
            try:
                await self._close_all_connections()
            except:
                pass
            return f"停止服务器失败: {e}"
        
        finally:
            # 重置停止标志
            self._is_stopping = False

    async def _close_all_connections(self):
        """关闭所有连接"""
        # 设置停止标志，防止继续处理日志
        if self.qq_server:
            self.qq_server.server_stopping = True
        
        # 首先关闭 RCON 连接（最重要）
        if self.rcon_client:
            try:
                self.rcon_client.close()
                self.logger.info("已关闭 RCON 连接")
            except Exception as e:
                self.logger.debug(f"关闭 RCON 连接出错: {e}")
        
        # 然后关闭 MSMP 连接
        if self.msmp_client:
            try:
                await asyncio.wait_for(self.msmp_client.close(), timeout=5.0)
                self.logger.info("已关闭 MSMP 连接")
            except asyncio.TimeoutError:
                self.logger.warning("MSMP 连接关闭超时")
            except Exception as e:
                self.logger.debug(f"关闭 MSMP 连接出错: {e}")
        
        # 关闭服务器日志文件
        if self.qq_server:
            try:
                self.qq_server._close_log_file()
            except Exception as e:
                self.logger.debug(f"关闭日志文件出错: {e}")
    
    async def handle_start(self, user_id: int, group_id: int, websocket, is_private: bool = False, **kwargs) -> str:
        """处理start命令(管理员) - 支持MSMP和RCON"""
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
                return f"启动脚本不存在: {start_script}"
            
            # 发送执行中提示
            if websocket and not websocket.closed:
                if is_private:
                    await self.qq_server.send_private_message(websocket, user_id, "正在启动服务器...")
                else:
                    await self.qq_server.send_group_message(websocket, group_id, "正在启动服务器...")
            
            # 调用qq_server的启动方法
            await self.qq_server._start_server_process(websocket, group_id)
            
            # 添加启动后的连接提示
            connection_info = []
            if self.config_manager.is_msmp_enabled():
                connection_info.append("MSMP管理协议")
            if self.config_manager.is_rcon_enabled():
                connection_info.append("RCON远程控制")
            
            if connection_info and websocket and not websocket.closed:
                info_msg = f"服务器启动后，将自动尝试连接: {', '.join(connection_info)}"
                if is_private:
                    await self.qq_server.send_private_message(websocket, user_id, info_msg)
                else:
                    await self.qq_server.send_group_message(websocket, group_id, info_msg)
            
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

    async def handle_reconnect(self, user_id: int, group_id: int, websocket, is_private: bool = False, **kwargs) -> str:
        """处理reconnect命令 - 手动重连服务"""
        try:
            # 发送执行中提示
            if is_private:
                await self.qq_server.send_private_message(websocket, user_id, "正在尝试重新连接服务...")
            else:
                await self.qq_server.send_group_message(websocket, group_id, "正在尝试重新连接服务...")
            
            results = []
            
            # 尝试重连MSMP
            if self.config_manager.is_msmp_enabled() and self.msmp_client:
                try:
                    if self.msmp_client.is_connected():
                        results.append("MSMP: 已连接 (无需重连)")
                    else:
                        self.logger.info("手动重连MSMP服务器...")
                        self.msmp_client.connect_sync()
                        await asyncio.sleep(3)  # 等待连接稳定
                        
                        if self.msmp_client.is_connected():
                            results.append("MSMP: 连接成功 ✓")
                            self.logger.info("MSMP手动重连成功")
                        else:
                            results.append("MSMP: 连接失败 ✗")
                            self.logger.warning("MSMP手动重连失败")
                except Exception as e:
                    results.append(f"MSMP: 连接异常 - {str(e)}")
                    self.logger.error(f"MSMP手动重连异常: {e}")
            
            # 尝试重连RCON
            if self.config_manager.is_rcon_enabled() and self.rcon_client:
                try:
                    if self.rcon_client.is_connected():
                        results.append("RCON: 已连接 (无需重连)")
                    else:
                        self.logger.info("手动重连RCON服务器...")
                        success = self.rcon_client.connect()
                        
                        if success:
                            results.append("RCON: 连接成功 ✓")
                            self.logger.info("RCON手动重连成功")
                        else:
                            results.append("RCON: 连接失败 ✗")
                            self.logger.warning("RCON手动重连失败")
                except Exception as e:
                    results.append(f"RCON: 连接异常 - {str(e)}")
                    self.logger.error(f"RCON手动重连异常: {e}")
            
            if not results:
                return "没有启用任何服务连接，无需重连"
            
            # 构建结果消息
            message_lines = ["重连结果:", "━━━━━━━━━━━━━━"]
            message_lines.extend(results)
            message_lines.append("━━━━━━━━━━━━━━")
            
            return "\n".join(message_lines)
            
        except Exception as e:
            self.logger.error(f"执行reconnect命令失败: {e}", exc_info=True)
            return f"重连服务失败: {e}"

    async def handle_reconnect_msmp(self, user_id: int, group_id: int, websocket, is_private: bool = False, **kwargs) -> str:
        """处理reconnect_msmp命令 - 手动重连MSMP"""
        try:
            if not self.config_manager.is_msmp_enabled():
                return "MSMP未启用，无法重连"
            
            if not self.msmp_client:
                return "MSMP客户端未初始化"
            
            # 发送执行中提示
            if is_private:
                await self.qq_server.send_private_message(websocket, user_id, "正在重连MSMP服务器...")
            else:
                await self.qq_server.send_group_message(websocket, group_id, "正在重连MSMP服务器...")
            
            # 检查当前状态
            current_status = "已连接" if self.msmp_client.is_connected() else "未连接"
            
            try:
                # 先关闭现有连接
                if self.msmp_client.is_connected():
                    self.msmp_client.close_sync()
                    await asyncio.sleep(1)  # 等待关闭完成
                
                # 重新连接
                self.logger.info("手动重连MSMP服务器...")
                self.msmp_client.connect_sync()
                await asyncio.sleep(3)  # 等待连接稳定
                
                if self.msmp_client.is_connected():
                    result = f"MSMP重连成功 ✓\n原状态: {current_status}\n现状态: 已连接"
                    self.logger.info("MSMP手动重连成功")
                else:
                    result = f"MSMP重连失败 ✗\n原状态: {current_status}\n现状态: 未连接"
                    self.logger.warning("MSMP手动重连失败")
                
                return result
                
            except Exception as e:
                error_msg = f"MSMP重连异常: {str(e)}"
                self.logger.error(f"MSMP手动重连异常: {e}")
                return error_msg
            
        except Exception as e:
            self.logger.error(f"执行reconnect_msmp命令失败: {e}", exc_info=True)
            return f"重连MSMP失败: {e}"

    async def handle_reconnect_rcon(self, user_id: int, group_id: int, websocket, is_private: bool = False, **kwargs) -> str:
        """处理reconnect_rcon命令 - 手动重连RCON"""
        try:
            if not self.config_manager.is_rcon_enabled():
                return "RCON未启用，无法重连"
            
            if not self.rcon_client:
                return "RCON客户端未初始化"
            
            # 发送执行中提示
            if is_private:
                await self.qq_server.send_private_message(websocket, user_id, "正在重连RCON服务器...")
            else:
                await self.qq_server.send_group_message(websocket, group_id, "正在重连RCON服务器...")
            
            # 检查当前状态
            current_status = "已连接" if self.rcon_client.is_connected() else "未连接"
            
            try:
                # 先关闭现有连接
                if self.rcon_client.is_connected():
                    self.rcon_client.close()
                    await asyncio.sleep(1)  # 等待关闭完成
                
                # 重新连接
                self.logger.info("手动重连RCON服务器...")
                success = self.rcon_client.connect()
                
                if success:
                    result = f"RCON重连成功 ✓\n原状态: {current_status}\n现状态: 已连接"
                    self.logger.info("RCON手动重连成功")
                else:
                    result = f"RCON重连失败 ✗\n原状态: {current_status}\n现状态: 未连接"
                    self.logger.warning("RCON手动重连失败")
                
                return result
                
            except Exception as e:
                error_msg = f"RCON重连异常: {str(e)}"
                self.logger.error(f"RCON手动重连异常: {e}")
                return error_msg
            
        except Exception as e:
            self.logger.error(f"执行reconnect_rcon命令失败: {e}", exc_info=True)
            return f"重连RCON失败: {e}"

    def _read_recent_logs_from_file(self, lines: int = 10) -> List[str]:
        """从日志文件读取最近的日志"""
        try:
            log_file_path = "logs/mc_server.log"  # 更新为新的日志路径
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

    async def handle_kill(self, user_id: int, group_id: int, websocket, is_private: bool = False, **kwargs) -> str:
        """处理kill命令(管理员) - 调用通用方法"""
        return await self._execute_kill_command(user_id, group_id, websocket, is_private)
    
    async def _execute_kill_command(self, user_id: int = 0, group_id: int = 0, websocket = None, is_private: bool = False) -> str:
        """通用的kill命令执行方法"""
        try:
            if not self.qq_server or not self.qq_server.server_process:
                return "服务器进程未运行"
            
            # 检查进程是否还在运行
            if self.qq_server.server_process.poll() is not None:
                return "服务器进程已经停止"
            
            # 发送执行中止提示（如果有websocket连接）
            if websocket and not websocket.closed:
                if is_private:
                    await self.qq_server.send_private_message(websocket, user_id, "正在强制中止服务器进程并彻底清理...")
                else:
                    await self.qq_server.send_group_message(websocket, group_id, "正在强制中止服务器进程并彻底清理...")
            else:
                # 控制台版本
                print("正在强制杀死Minecraft服务器并彻底清理...")
            
            self.logger.info(f"{'控制台' if user_id == 0 else f'用户 {user_id}'} 执行强制中止服务器命令")
            
            # 第一步：先关闭所有连接
            await self._close_all_connections()
            
            # 第二步：强制杀死进程
            import signal
            import os
            import subprocess
            
            try:
                pid = self.qq_server.server_process.pid
                self.logger.info(f"强制终止进程 {pid}")
                
                if os.name == 'nt':  # Windows
                    # 使用 taskkill 强制终止进程树
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
                            # 备用方法
                            os.kill(pid, signal.SIGTERM)
                    except subprocess.TimeoutExpired:
                        self.logger.warning("taskkill 超时，尝试其他方法")
                        os.kill(pid, signal.SIGTERM)
                    except Exception as e:
                        self.logger.warning(f"taskkill 失败: {e}")
                        os.kill(pid, signal.SIGTERM)
                else:  # Linux/Mac
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
                    
            except ProcessLookupError:
                self.logger.info("进程已不存在")
            except Exception as e:
                self.logger.error(f"强制中止进程失败: {e}")
                return f"强制中止失败: {e}"
            
            # 第三步：等待进程结束
            try:
                await asyncio.wait_for(
                    asyncio.get_event_loop().run_in_executor(
                        None,
                        self.qq_server.server_process.wait
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
            
            # 第四步：彻底清理端口占用和文件锁
            await self._thorough_cleanup()
            
            # 第五步：清理进程引用和文件
            self.qq_server.server_process = None
            self.qq_server._close_log_file()
            
            return "服务器进程已强制中止，所有连接和端口已彻底清理"
            
        except Exception as e:
            self.logger.error(f"执行kill命令失败: {e}", exc_info=True)
            return f"强制中止失败: {e}"

    async def _thorough_cleanup(self):
        """彻底清理所有残留"""
        try:
            self.logger.info("开始彻底清理残留资源...")
            
            # 1. 强制清理文件锁
            await self._force_clean_file_locks()
            
            # 2. 等待确保所有资源释放
            await asyncio.sleep(3)
            
            self.logger.info("彻底清理完成")
            
        except Exception as e:
            self.logger.error(f"彻底清理失败: {e}")

    async def _clean_port(self, port: int, service_name: str):
        """清理指定端口"""
        try:
            if await self._is_port_in_use('localhost', port):
                self.logger.warning(f"{service_name}端口 {port} 被占用，正在清理...")
                
                # 使用多种方法清理端口
                await self._kill_process_by_port(port)
                await asyncio.sleep(2)
                
                # 验证清理结果
                if await self._is_port_in_use('localhost', port):
                    self.logger.error(f"{service_name}端口 {port} 清理失败")
                else:
                    self.logger.info(f"{service_name}端口 {port} 已释放")
                    
        except Exception as e:
            self.logger.error(f"清理{service_name}端口失败: {e}")

    async def _kill_process_by_port(self, port: int):
        """通过端口号杀死进程"""
        try:
            import subprocess
            
            # 方法1: 使用 netstat + taskkill
            result = subprocess.run(
                ['netstat', '-ano', '-p', 'TCP'], 
                capture_output=True, text=True, timeout=10
            )
            
            if result.returncode == 0:
                for line in result.stdout.split('\n'):
                    if f':{port}' in line and 'LISTENING' in line:
                        parts = line.split()
                        if len(parts) >= 5:
                            pid = parts[-1]
                            self.logger.info(f"发现进程 {pid} 占用端口 {port}")
                            
                            # 强制终止进程
                            subprocess.run(
                                ['taskkill', '/F', '/T', '/PID', pid],
                                capture_output=True, timeout=10
                            )
            
            # 方法2: 使用 PowerShell (备用)
            ps_cmd = f"Get-NetTCPConnection -LocalPort {port} | ForEach-Object {{ Stop-Process -Id $_.OwningProcess -Force }}"
            subprocess.run(
                ['powershell', '-Command', ps_cmd],
                capture_output=True, timeout=10
            )
            
        except Exception as e:
            self.logger.warning(f"通过端口杀死进程失败: {e}")

    async def _force_clean_file_locks(self):
        """强制清理文件锁"""
        try:
            working_dir = self.config_manager.get_server_working_directory()
            if not working_dir:
                start_script = self.config_manager.get_server_start_script()
                working_dir = os.path.dirname(start_script)
                
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
                        # 多次尝试删除
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

    async def _is_port_in_use(self, host: str, port: int) -> bool:
        """检查端口是否被占用"""
        try:
            import socket
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(1)
                result = s.connect_ex((host, port))
                return result == 0
        except:
            return False
    
    async def handle_crash(self, user_id: int, group_id: int, websocket, is_private: bool = False, **kwargs) -> str:
        """处理crash命令(管理员) - 获取最新的崩溃报告"""
        try:
            import os
            from pathlib import Path
            
            # 从 working_directory 查找 crash-reports
            working_dir = self.config_manager.get_server_working_directory()
            if not working_dir:
                start_script = self.config_manager.get_server_start_script()
                working_dir = os.path.dirname(start_script)
            
            crash_dir = os.path.join(working_dir, "crash-reports")
            crash_path = Path(crash_dir)
            
            if not crash_path.exists():
                return f"crash-reports 目录不存在: {crash_dir}"
            
            # 查找所有crash文件
            crash_files = list(crash_path.glob("crash-*.txt"))
            
            if not crash_files:
                return "未找到任何崩溃报告"
            
            # 按修改时间排序，获取最新的
            latest_crash = max(crash_files, key=lambda p: p.stat().st_mtime)
            
            self.logger.info(f"找到最新崩溃报告: {latest_crash.name}")
            
            # 发送文件
            await self.qq_server._send_crash_report_file(websocket, user_id, group_id, str(latest_crash), is_private)
            
            return None  # 文件已通过 _send_crash_report_file 发送
            
        except Exception as e:
            self.logger.error(f"处理崩溃报告失败: {e}", exc_info=True)
            return f"处理崩溃报告失败: {e}"

    async def handle_listeners(self, **kwargs) -> str:
        """处理 listeners 命令 - 显示所有自定义监听规则"""
        try:
            if not self.qq_server.custom_listener:
                return "自定义消息监听器未初始化"
            
            return self.qq_server.custom_listener.get_rules_info()
            
        except Exception as e:
            self.logger.error(f"执行 listeners 命令失败: {e}", exc_info=True)
            return f"获取监听规则失败: {e}"