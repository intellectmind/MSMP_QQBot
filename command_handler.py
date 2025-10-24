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
        
        # 检查命令是否可用
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
            return f"命令冷却中,请等待 {remaining} 秒'"
        
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
        enabled_admin_commands = []  # 对非管理员开放的管理员命令
        
        seen_commands = set()
        for name, command in self.commands.items():
            if command.names[0] not in seen_commands:
                seen_commands.add(command.names[0])
                
                if command.admin_only:
                    if is_admin:
                        admin_commands.append(command)
                    else:
                        # 检查这个管理员命令是否对非管理员开放
                        if self.config_manager.is_admin_command_enabled(command.names[0]):
                            enabled_admin_commands.append(command)
                else:
                    if is_admin or (command.command_key and self.config_manager.is_command_enabled(command.command_key)):
                        basic_commands.append(command)
        
        lines = ["MSMP_QQBot 命令帮助", "══════════════"]
        
        if basic_commands:
            lines.append("\n基础命令:")
            for cmd in basic_commands:
                aliases = " / ".join(cmd.names[:3])
                lines.append(f"• {aliases}")
                if cmd.description:
                    lines.append(f"  {cmd.description}")
                if cmd.usage and detailed:
                    lines.append(f"  用法: {cmd.usage}")
        
        # 对非管理员显示开放的管理员命令
        if not is_admin and enabled_admin_commands:
            lines.append("\n开放的管理员命令:")
            for cmd in enabled_admin_commands:
                aliases = " / ".join(cmd.names[:3])
                lines.append(f"• {aliases}")
                if cmd.description:
                    lines.append(f"  {cmd.description}")
        
        if admin_commands:
            lines.append("\n管理员专属命令:")
            for cmd in admin_commands:
                aliases = " / ".join(cmd.names[:3])
                lines.append(f"• {aliases}")
                if cmd.description:
                    lines.append(f"  {cmd.description}")
            
            lines.append("\n直接命令执行:")
            lines.append("• !<命令>")
            lines.append("  使用 ! 前缀直接执行服务器命令")
            lines.append("  示例: !say Hello 或 !give @a diamond")
        
        # 显示禁用命令提示（仅对管理员显示）
        if is_admin:
            disabled_commands = []
            
            for cmd_key, enabled in self.config_manager.get_enabled_commands().items():
                if not enabled:
                    disabled_commands.append(cmd_key)
            
            for cmd_key, enabled in self.config_manager.get_enabled_admin_commands().items():
                if not enabled:
                    disabled_commands.append(f"{cmd_key}(非管理员)")
            
            if disabled_commands:
                lines.append(f"\n已禁用命令: {', '.join(disabled_commands)}")
        
        # 添加使用提示
        if not is_admin:
            lines.append(f"\n提示: 当前有 {len(enabled_admin_commands)} 个管理员命令对您开放")
            lines.append("   如需更多权限，请联系管理员")
        else:
            lines.append(f"\n您是管理员，可以使用所有 {len(admin_commands)} 个管理员命令")
        
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
        self.logger.info("已进入关闭模式，停止所有连接检测")
    
    async def handle_list(self, **kwargs) -> str:
        """处理list命令"""
        try:
            client_type, client = await self.qq_server.connection_manager.get_preferred_client()
            
            if not client:
                return "服务器连接未就绪\n请使用 #reconnect 手动重连"
            
            try:
                if client_type == 'msmp':
                    player_info = client.get_player_list_sync()
                else:
                    player_info = client.get_player_list()
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
            if not self.config_manager.is_rcon_enabled():
                return "TPS查询需要启用RCON连接"

            client_type, client = await self.qq_server.connection_manager.get_client_for_command("tps")
            
            if not client or client_type != 'rcon':
                return "TPS命令需要RCON连接\n请使用 #reconnect_rcon 重连"
            
            tps_command = self.config_manager.get_tps_command()
            result = client.execute_command(tps_command)
            
            if result:
                # 第一步：清理Minecraft颜色代码 (§[0-9a-fk-or] 或 &[0-9a-fk-or])
                cleaned = re.sub(r'[§&][0-9a-fk-orA-FK-OR]', '', result).strip()
                
                self.logger.debug(f"原始TPS返回: {result}")
                self.logger.debug(f"清理后的TPS返回: {cleaned}")
                
                # 第二步：尝试使用正则表达式提取TPS值
                tps_value = self._extract_tps_value(cleaned)
                
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
                        f"正则表达式: {self.config_manager.get_tps_regex()}\n"
                        f"捕获组索引: {self.config_manager.get_tps_group_index()}\n"
                        f"清理后的文本: {cleaned}"
                    )
                
                # 第四步：是否显示原始输出
                if self.config_manager.is_tps_raw_output_enabled():
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


    def _extract_tps_value(self, text: str) -> Optional[float]:
        """从服务器返回的文本中提取TPS值"""
        try:
            tps_regex = self.config_manager.get_tps_regex()
            tps_group_index = self.config_manager.get_tps_group_index()
            
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
            if not self.config_manager.is_msmp_enabled():
                return "规则查询需要启用MSMP连接"
            
            client_type, client = await self.qq_server.connection_manager.ensure_connected()
            
            if not client or client_type != 'msmp':
                return "MSMP连接未就绪\n请使用 #reconnect_msmp 手动重连"
            
            self.logger.info("查询服务器规则...")
            
            lines = ["服务器规则信息", "=" * 20]
            
            try:
                gamerules_result = await self.msmp_client.get_game_rules()
                
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
                            result = await self.msmp_client.send_request(f"serversettings/{setting_key}")
                            
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
        """处理status命令"""
        try:
            qq_status = "已连接" if self.qq_server.is_connected() else "未连接"
            
            msmp_status = "未启用"
            msmp_connected = False
            if self.config_manager.is_msmp_enabled():
                if not self.msmp_client:
                    msmp_status = "客户端未初始化"
                elif not self.msmp_client.is_connected():
                    msmp_status = "未连接"
                else:
                    try:
                        status = self.msmp_client.get_server_status_sync()
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
            if self.config_manager.is_rcon_enabled():
                if not self.rcon_client:
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
            
            if self.qq_server and self.qq_server.server_process:
                if self.qq_server.server_process.poll() is None:
                    # 服务器进程正在运行
                    server_process_running = True
                    try:
                        # 尝试获取更详细的状态
                        client_type, client = await self.qq_server.connection_manager.ensure_connected()
                        if client:
                            if client_type == 'msmp':
                                player_info = self.msmp_client.get_player_list_sync()
                                mc_server_status = (
                                    f"运行中 (PID: {self.qq_server.server_process.pid})\n"
                                    f"在线: {player_info.current_players}/{player_info.max_players}"
                                )
                            elif client_type == 'rcon':
                                player_info = self.rcon_client.get_player_list()
                                mc_server_status = (
                                    f"运行中 (PID: {self.qq_server.server_process.pid})\n"
                                    f"在线: {player_info.current_players}/{player_info.max_players}"
                                )
                            else:
                                mc_server_status = f"运行中 (PID: {self.qq_server.server_process.pid})"
                        else:
                            mc_server_status = f"运行中 (PID: {self.qq_server.server_process.pid}) - 连接异常"
                    except Exception as e:
                        mc_server_status = f"运行中 (PID: {self.qq_server.server_process.pid}) - 状态获取失败"
                else:
                    return_code = self.qq_server.server_process.poll()
                    mc_server_status = f"已停止 (退出码: {return_code})"
            else:
                # 服务器进程未运行，但可能有外部接入
                if external_access:
                    mc_server_status = "运行中 (外部接入)"
                else:
                    mc_server_status = "未启动"
            
            # 构建状态信息
            status_lines = [
                "系统状态总览",
                "■■■■■■■■■■■■■■■",
                f"QQ机器人: {qq_status}",
                f"MC服务器: {mc_server_status}",
                f"MSMP连接: {msmp_status}",
                f"RCON连接: {rcon_status}"
            ]
            
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
            return self.qq_server.command_handler.get_help_message(user_id)
        return "帮助系统未初始化"
    
    async def handle_stop(self, user_id: int, group_id: int, websocket, is_private: bool = False, **kwargs) -> str:
        """处理stop命令(管理员) - 支持MSMP和RCON"""
        
        async with self._stop_lock:
            if self._is_stopping:
                return "服务器已在停止中，请勿重复执行"
            
            self._is_stopping = True
        
        try:
            # 先检查服务器是否在运行
            if not self.qq_server or not self.qq_server.server_process:
                self._is_stopping = False
                return "服务器未运行"
            
            if self.qq_server.server_process.poll() is not None:
                self._is_stopping = False
                return "服务器已经停止"
            
            if is_private:
                await self.qq_server.send_private_message(websocket, user_id, "正在停止服务器...")
            else:
                await self.qq_server.send_group_message(websocket, group_id, "正在停止服务器...")
            
            # 第一步：立即设置服务器停止标志，停止日志采集
            self.qq_server.server_stopping = True
            
            # 第二步：先尝试通过连接发送停止命令（在关闭连接之前）
            stop_success = False
            
            try:
                # 使用连接管理器获取客户端
                client_type, client = await self.qq_server.connection_manager.ensure_connected()
                
                if client:
                    if client_type == 'msmp':
                        result = client.execute_command_sync("server/stop")
                        if 'result' in result:
                            stop_success = True
                            self.logger.info("MSMP 停止命令已发送")
                    
                    elif client_type == 'rcon':
                        result = client.execute_command("stop")
                        stop_success = True
                        self.logger.info("RCON停止命令已发送")
                else:
                    self.logger.warning("无可用连接发送停止命令")
            
            except Exception as e:
                self.logger.warning(f"通过连接发送停止命令失败: {e}")
            
            # 第三步：如果无法通过连接停止，尝试通过标准输入发送停止命令
            if not stop_success:
                try:
                    if (self.qq_server.server_process and 
                        self.qq_server.server_process.poll() is None and
                        self.qq_server.server_process.stdin):
                        
                        stop_command = "stop\n"
                        self.qq_server.server_process.stdin.write(stop_command.encode('utf-8'))
                        self.qq_server.server_process.stdin.flush()
                        self.logger.info("已通过标准输入发送停止命令")
                        stop_success = True
                except Exception as e:
                    self.logger.warning(f"通过标准输入发送停止命令失败: {e}")
            
            # 第四步：立即彻底关闭所有连接
            await self._thorough_shutdown()
            
            self.logger.info("停止命令已发送，等待服务器关闭进程...")
            
            # 等待服务器关闭
            max_wait_time = 60
            wait_interval = 5
            waited_time = 0
            
            while (waited_time < max_wait_time and 
                   self.qq_server.server_process and 
                   self.qq_server.server_process.poll() is None):
                await asyncio.sleep(wait_interval)
                waited_time += wait_interval
                self.logger.info(f"等待服务器关闭... ({waited_time}/{max_wait_time}秒)")
            
            # 检查服务器是否已关闭
            server_stopped = True
            if self.qq_server.server_process:
                return_code = self.qq_server.server_process.poll()
                if return_code is None:
                    server_stopped = False
                    self.logger.warning(f"服务器进程在{max_wait_time}秒后仍未关闭")
                else:
                    self.logger.info(f"服务器进程已关闭，返回码: {return_code}")
            
            # 给日志采集任务一点时间读取剩余输出
            await asyncio.sleep(2)
            
            result_message = "服务器已成功关闭" if server_stopped else "停止命令已发送，但服务器进程仍在运行中。可能需要手动检查或使用 #kill 命令强制停止"
            print(result_message)
            
            return result_message
            
        except Exception as e:
            self.logger.error(f"执行stop命令失败: {e}", exc_info=True)
            # 出错时也要执行关闭
            await self._thorough_shutdown()
            error_msg = f"停止服务器失败: {e}"
            if kwargs.get('from_console', False):
                print(error_msg)
            return error_msg
        
        finally:
            self._is_stopping = False

    async def _thorough_shutdown(self):
        """彻底关闭所有连接"""
        self.logger.info("执行彻底关闭操作...")
        
        # 第一步：通过连接管理器设置关闭模式
        if hasattr(self.qq_server, 'connection_manager'):
            await self.qq_server.connection_manager.set_shutdown_mode()
        
        # 第二步：强制关闭MSMP连接
        if self.msmp_client:
            try:
                if hasattr(self.msmp_client, 'close_sync'):
                    self.msmp_client.close_sync()
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
        if hasattr(self.qq_server, 'connection_manager'):
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
        if hasattr(self, '_shutdown_event'):
            self._shutdown_event.clear()
        
        # 重置连接管理器的关闭模式
        if hasattr(self.qq_server, 'connection_manager'):
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
        if hasattr(self.qq_server, 'connection_manager'):
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
            # 第一步：重置关闭模式，允许重新连接
            await self._reset_shutdown_mode()
            
            if self.qq_server.server_process and self.qq_server.server_process.poll() is None:
                return "服务器已经在启动或运行中"
            
            start_script = self.config_manager.get_server_start_script()
            if not start_script:
                return (
                    "服务器启动脚本未配置\n"
                    "请在 config.yml 中配置 server.start_script"
                )
            
            if not os.path.exists(start_script):
                return f"启动脚本不存在: {start_script}"
            
            if websocket and not websocket.closed:
                if is_private:
                    await self.qq_server.send_private_message(websocket, user_id, "正在启动服务器...")
                else:
                    await self.qq_server.send_group_message(websocket, group_id, "正在启动服务器...")
            
            await self.qq_server._start_server_process(websocket, group_id)
            
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
            
            return None
            
        except Exception as e:
            self.logger.error(f"执行start命令失败: {e}")
            return f"启动服务器失败: {e}"

    async def _reset_shutdown_mode(self):
        """重置关闭模式"""
        # 检查是否已经在正常模式
        if not self._is_stopping and not (hasattr(self, '_shutdown_event') and self._shutdown_event.is_set()):
            self.logger.debug("已在正常模式")
            return
        
        self.logger.info("重置关闭模式")
        
        # 重置命令处理器的关闭标志
        self._is_stopping = False
        if hasattr(self, '_shutdown_event'):
            self._shutdown_event.clear()
        
        # 重置连接管理器的关闭模式
        if hasattr(self.qq_server, 'connection_manager'):
            await self.qq_server.connection_manager.reset_shutdown_mode()
        
        # 重置MSMP客户端的关闭模式
        if self.msmp_client and hasattr(self.msmp_client, '_shutdown_mode'):
            self.msmp_client._shutdown_mode = False
        
        # 清空连接缓存
        if hasattr(self.qq_server, 'connection_manager'):
            await self.qq_server.connection_manager.invalidate_all_caches()
        
        # 重置关闭标志
        if hasattr(self, '_connections_closed'):
            self._connections_closed = False
        if hasattr(self, '_shutdown_initiated'):
            self._shutdown_initiated = False
        
        self.logger.debug("关闭模式已重置")
    
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
            server_running = self.qq_server.server_process and self.qq_server.server_process.poll() is None
            recent_logs = self.qq_server.get_recent_logs(20)
            
            if not recent_logs:
                return "暂无服务器日志输出"
            
            status = "运行中" if server_running else "已停止"
            lines = [f"最近 {len(recent_logs)} 条服务器日志 (服务器{status}):"]
            lines.append("■■■■■■■■■■■■■■")
            
            for log in recent_logs:
                if len(log) > 100:
                    log = log[:100] + "..."
                lines.append(log)
            
            lines.append("■■■■■■■■■■■■■■")
            if server_running:
                lines.append("提示: 日志实时更新，再次发送 log 查看最新日志")
            else:
                lines.append("提示: 服务器已停止，日志不再更新")
            
            return "\n".join(lines)
            
        except Exception as e:
            self.logger.error(f"执行log命令失败: {e}", exc_info=True)
            return f"获取日志失败: {e}"

    async def handle_reconnect(self, user_id: int, group_id: int, websocket, is_private: bool = False, **kwargs) -> str:
        """处理reconnect命令 - 手动重连服务器"""
        try:
            if is_private:
                await self.qq_server.send_private_message(websocket, user_id, "正在尝试重新连接服务器...")
            else:
                await self.qq_server.send_group_message(websocket, group_id, "正在尝试重新连接服务器...")
            
            results = await self.qq_server.connection_manager.reconnect_all()
            
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
            if not self.config_manager.is_msmp_enabled():
                return "MSMP未启用，无法重连"
            
            if is_private:
                await self.qq_server.send_private_message(websocket, user_id, "正在重连MSMP服务器...")
            else:
                await self.qq_server.send_group_message(websocket, group_id, "正在重连MSMP服务器...")
            
            success = await self.qq_server.connection_manager.reconnect_msmp()
            
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
            if not self.config_manager.is_rcon_enabled():
                return "RCON未启用，无法重连"
            
            if is_private:
                await self.qq_server.send_private_message(websocket, user_id, "正在重连RCON服务器...")
            else:
                await self.qq_server.send_group_message(websocket, group_id, "正在重连RCON服务器...")
            
            success = await self.qq_server.connection_manager.reconnect_rcon()
            
            if success:
                return "RCON重连成功"
            else:
                return "RCON重连失败"
            
        except Exception as e:
            self.logger.error(f"执行reconnect_rcon命令失败: {e}", exc_info=True)
            return f"重连RCON失败: {e}"

    async def handle_kill(self, user_id: int, group_id: int, websocket, is_private: bool = False, **kwargs) -> str:
        """处理kill命令(管理员) - 强制杀死服务器进程"""
        return await self._execute_kill_command(user_id, group_id, websocket, is_private)
    
    async def _execute_kill_command(self, user_id: int = 0, group_id: int = 0, websocket = None, is_private: bool = False) -> str:
        """通用的kill命令执行方法"""
        try:
            if not self.qq_server or not self.qq_server.server_process:
                return "服务器进程未运行"
            
            if self.qq_server.server_process.poll() is not None:
                return "服务器进程已经停止"
            
            # 立即执行统一关闭操作
            await self._immediate_shutdown()
                        
            import signal
            import subprocess
            
            try:
                pid = self.qq_server.server_process.pid
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
            
            # 等待日志采集完成
            await asyncio.sleep(2)
            
            await self._thorough_cleanup()
            
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
            
            await self._force_clean_file_locks()
            await asyncio.sleep(3)
            
            self.logger.info("彻底清理完成")
            
        except Exception as e:
            self.logger.error(f"彻底清理失败: {e}")

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
            
            working_dir = self.config_manager.get_server_working_directory()
            if not working_dir:
                start_script = self.config_manager.get_server_start_script()
                working_dir = os.path.dirname(start_script)
            
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