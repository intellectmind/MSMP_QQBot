"""
玩家上线坐标管理插件

功能:
- 获取玩家上线坐标
- 修改玩家上线坐标
"""
import os
import logging
import nbtlib
from typing import Optional, Tuple, List, Dict, Any
from plugin_manager import BotPlugin

class PlayerDataModifier:
    """玩家数据修改器"""
    
    def __init__(self, world_path: str, logger: logging.Logger):
        self.world_path = world_path
        self.playerdata_path = os.path.join(world_path, "playerdata")
        self.logger = logger
        
        if os.path.exists(self.playerdata_path):
            self.logger.info(f"玩家数据修改器已初始化: {self.playerdata_path}")
        else:
            self.logger.error(f"playerdata 目录不存在: {self.playerdata_path}")
    
    def _find_player_dat_files(self, player_identifier: str) -> List[str]:
        """查找玩家 dat 文件（包括 .dat 和 .dat_old）"""
        dat_files = []
        try:
            if not os.path.exists(self.playerdata_path):
                self.logger.error("playerdata 目录不存在")
                return dat_files
            
            # 首先假设输入是UUID，直接查找
            dat_file = os.path.join(self.playerdata_path, f"{player_identifier}.dat")
            dat_old_file = os.path.join(self.playerdata_path, f"{player_identifier}.dat_old")
            
            if os.path.exists(dat_file):
                dat_files.append(dat_file)
            if os.path.exists(dat_old_file):
                dat_files.append(dat_old_file)
            
            # 如果输入包含连字符(可能是UUID格式但没有)，尝试添加
            if len(player_identifier) == 32 and '-' not in player_identifier:
                uuid_with_dash = f"{player_identifier[:8]}-{player_identifier[8:12]}-{player_identifier[12:16]}-{player_identifier[16:20]}-{player_identifier[20:]}"
                dat_file = os.path.join(self.playerdata_path, f"{uuid_with_dash}.dat")
                dat_old_file = os.path.join(self.playerdata_path, f"{uuid_with_dash}.dat_old")
                
                if os.path.exists(dat_file):
                    dat_files.append(dat_file)
                if os.path.exists(dat_old_file):
                    dat_files.append(dat_old_file)
            
            # 如果还没找到，尝试从 usercache.json 查找UUID
            if not dat_files:
                usercache_path = os.path.join(os.path.dirname(self.world_path), "usercache.json")
                if os.path.exists(usercache_path):
                    try:
                        import json
                        with open(usercache_path, 'r', encoding='utf-8') as f:
                            cache = json.load(f)
                        
                        for entry in cache:
                            if entry.get('name', '').lower() == player_identifier.lower():
                                uuid = entry.get('uuid')
                                dat_file = os.path.join(self.playerdata_path, f"{uuid}.dat")
                                dat_old_file = os.path.join(self.playerdata_path, f"{uuid}.dat_old")
                                
                                if os.path.exists(dat_file):
                                    dat_files.append(dat_file)
                                if os.path.exists(dat_old_file):
                                    dat_files.append(dat_old_file)
                                
                                if dat_files:
                                    self.logger.info(f"从 usercache.json 找到玩家 {player_identifier} 的UUID: {uuid}")
                                break
                    except Exception as e:
                        self.logger.debug(f"查询 usercache.json 失败: {e}")
            
            if not dat_files:
                self.logger.warning(f"找不到玩家 {player_identifier} 的 dat 文件")
            else:
                self.logger.info(f"找到玩家 {player_identifier} 的数据文件: {dat_files}")
            
            return dat_files
            
        except Exception as e:
            self.logger.error(f"查找玩家 dat 文件失败: {e}")
            return []
    
    def get_player_pos(self, player_identifier: str) -> Optional[Tuple[float, float, float]]:
        """获取玩家坐标（从 .dat 文件）"""
        try:
            dat_files = self._find_player_dat_files(player_identifier)
            
            # 优先使用 .dat 文件
            dat_file = None
            for file_path in dat_files:
                if file_path.endswith('.dat') and not file_path.endswith('.dat_old'):
                    dat_file = file_path
                    break
            
            if not dat_file and dat_files:
                # 如果没有 .dat 文件但有 .dat_old 文件，使用第一个
                dat_file = dat_files[0]
            
            if not dat_file:
                self.logger.warning(f"找不到玩家 {player_identifier} 的 dat 文件")
                return None
            
            nbt_file = nbtlib.load(dat_file)
            
            if 'Pos' in nbt_file:
                pos = nbt_file['Pos']
                x = float(pos[0])
                y = float(pos[1])
                z = float(pos[2])
                
                self.logger.info(f"玩家 {player_identifier} 当前坐标: ({x}, {y}, {z})")
                return (x, y, z)
            else:
                self.logger.warning(f"玩家 NBT 数据中不存在 Pos 标签")
                return None
            
        except Exception as e:
            self.logger.error(f"读取玩家坐标失败: {e}", exc_info=True)
            return None
    
    def set_player_pos(self, player_identifier: str, x: float, y: float, z: float) -> bool:
        """设置玩家坐标（同时修改 .dat 和 .dat_old 文件）"""
        try:
            if not (-30000000 <= x <= 30000000 and -64 <= y <= 320 and -30000000 <= z <= 30000000):
                self.logger.error(f"坐标超出范围: ({x}, {y}, {z})")
                return False
            
            dat_files = self._find_player_dat_files(player_identifier)
            if not dat_files:
                self.logger.error(f"找不到玩家 {player_identifier} 的 dat 文件")
                return False
            
            success_count = 0
            total_files = len(dat_files)
            
            for dat_file_path in dat_files:
                try:
                    nbt_file = nbtlib.load(dat_file_path)
                    
                    if 'Pos' in nbt_file:
                        nbt_file['Pos'] = nbtlib.tag.List[nbtlib.tag.Double]([
                            nbtlib.tag.Double(x),
                            nbtlib.tag.Double(y),
                            nbtlib.tag.Double(z)
                        ])
                        
                        nbt_file.save()
                        success_count += 1
                        self.logger.info(f"已成功修改文件 {os.path.basename(dat_file_path)} 的坐标: ({x}, {y}, {z})")
                    else:
                        self.logger.warning(f"文件 {os.path.basename(dat_file_path)} 中不存在 Pos 标签")
                        
                except Exception as e:
                    self.logger.error(f"修改文件 {os.path.basename(dat_file_path)} 失败: {e}")
            
            if success_count > 0:
                self.logger.info(f"成功修改了 {success_count}/{total_files} 个文件")
                return True
            else:
                self.logger.error(f"未能成功修改任何文件")
                return False
            
        except Exception as e:
            self.logger.error(f"修改玩家坐标失败: {e}", exc_info=True)
            return False


class PlayerCoordinatesPlugin(BotPlugin):
    """玩家坐标管理插件"""
    
    name = "玩家上线坐标管理"
    version = "1.1.0"
    author = "MSMP_QQBot"
    description = "提供玩家上线坐标查询和修改功能"
    
    COMMANDS_HELP = {
        "getpos": {
            "names": ["getpos", "查询坐标", "查看坐标"],
            "description": "查询玩家的坐标信息",
            "usage": "getpos <玩家名>",
            "admin_only": False,
        },
        "setpos": {
            "names": ["setpos", "设置坐标", "修改坐标"],
            "description": "修改玩家的坐标（需要玩家离线）",
            "usage": "setpos <玩家名> <x> <y> <z>",
            "admin_only": True,
        }
    }
    
    def __init__(self, logger):
        super().__init__(logger)
        self.plugin_manager = None
        self.config_manager = None
        self.modifier = None
        self.world_path = None
    
    async def on_load(self, plugin_manager: 'PluginManager') -> bool:
        """插件加载"""
        try:
            self.logger.info(f"正在加载 {self.name} 插件...")
            
            self.plugin_manager = plugin_manager
            
            # 注册命令（先注册，后初始化 modifier）
            await self._register_commands()
            
            self.logger.info(f"{self.name} 插件加载成功")
            return True
            
        except Exception as e:
            self.logger.error(f"加载 {self.name} 插件失败: {e}", exc_info=True)
            return False
    
    async def on_unload(self):
        """插件卸载"""
        self.logger.info(f"正在卸载 {self.name} 插件...")
        self.modifier = None
        self.config_manager = None
    
    async def _register_commands(self):
        """注册命令"""
        
        self.plugin_manager.register_command(
            command_name="getpos",
            handler=self.handle_getpos,
            names=self.COMMANDS_HELP["getpos"]["names"],
            admin_only=self.COMMANDS_HELP["getpos"]["admin_only"],
            description=self.COMMANDS_HELP["getpos"]["description"],
            usage=self.COMMANDS_HELP["getpos"]["usage"]
        )
        
        self.plugin_manager.register_command(
            command_name="setpos",
            handler=self.handle_setpos,
            names=self.COMMANDS_HELP["setpos"]["names"],
            admin_only=self.COMMANDS_HELP["setpos"]["admin_only"],
            description=self.COMMANDS_HELP["setpos"]["description"],
            usage=self.COMMANDS_HELP["setpos"]["usage"]
        )
        
        self.logger.info("已注册所有命令")
    
    def _get_working_directory(self, config_manager=None) -> str:
        """获取服务器工作目录 - 使用 ConfigManager
        
        逻辑：
        1. 如果 working_directory 非空且存在 → 使用它
        2. 如果 working_directory 为空 → 使用 start_script 所在目录
        """
        try:
            if config_manager is None:
                self.logger.error("ConfigManager 不可用")
                return ""
            
            if not hasattr(config_manager, 'get_server_working_directory'):
                self.logger.error("ConfigManager 缺少必要方法")
                return ""
            
            # 获取工作目录配置
            working_dir = config_manager.get_server_working_directory()
            
            # 情况1: working_directory 非空且存在 → 直接使用
            if working_dir and os.path.exists(working_dir):
                self.logger.info(f"工作目录: {working_dir}")
                self.config_manager = config_manager
                return working_dir
            
            # 情况2: working_directory 为空 → 使用 start_script 所在目录
            if not working_dir:
                start_script = config_manager.get_server_start_script()
                
                if start_script and os.path.exists(start_script):
                    working_dir = os.path.dirname(start_script)
                    if working_dir:
                        self.logger.info(f"从启动脚本推断工作目录: {working_dir}")
                        self.config_manager = config_manager
                        return working_dir
            
            # 其他情况: 失败
            self.logger.error(f"无法获取有效的工作目录，working_directory={working_dir}")
            return ""
            
        except Exception as e:
            self.logger.error(f"获取工作目录异常: {e}", exc_info=True)
            return ""
    
    def _init_modifier(self, config_manager=None):
        """延迟初始化 modifier - 在命令执行时调用"""
        if self.modifier:
            return  # 已经初始化过了
        
        # 获取工作目录
        working_dir = self._get_working_directory(config_manager)
        
        if not working_dir:
            self.logger.error("无法确定服务器工作目录")
            return
        
        self.logger.info(f"使用工作目录: {working_dir}")
        
        # 检查是否是有效的世界目录
        if os.path.exists(os.path.join(working_dir, "playerdata")):
            # working_dir 本身就是世界目录
            self.world_path = working_dir
            self.modifier = PlayerDataModifier(working_dir, self.logger)
            return
        elif os.path.exists(os.path.join(working_dir, "world", "playerdata")):
            # working_dir 包含 world 子目录
            world_path = os.path.join(working_dir, "world")
            self.world_path = world_path
            self.modifier = PlayerDataModifier(world_path, self.logger)
            return
        else:
            self.logger.error(f"在工作目录 {working_dir} 中找不到 playerdata 文件夹")
    
    def get_plugin_help(self) -> str:
        """获取插件帮助信息"""
        lines = [
            f"【{self.name}】 v{self.version}",
            f"作者: {self.author}",
            f"说明: {self.description}",
            ""
        ]
        
        basic_cmds = [cmd for cmd, info in self.COMMANDS_HELP.items() if not info.get("admin_only", False)]
        if basic_cmds:
            lines.append("【基础命令】")
            for cmd in basic_cmds:
                info = self.COMMANDS_HELP[cmd]
                main_name = info['names'][0]
                aliases = ' / '.join(info['names'][1:])
                lines.append(f"• {main_name}" + (f" ({aliases})" if aliases else ""))
                lines.append(f"  {info['description']}")
                lines.append(f"  用法: {info['usage']}")
                lines.append("")
        
        admin_cmds = [cmd for cmd, info in self.COMMANDS_HELP.items() if info.get("admin_only", False)]
        if admin_cmds:
            lines.append("【管理员命令】")
            for cmd in admin_cmds:
                info = self.COMMANDS_HELP[cmd]
                main_name = info['names'][0]
                aliases = ' / '.join(info['names'][1:])
                lines.append(f"• {main_name}" + (f" ({aliases})" if aliases else "") + " [管理员]")
                lines.append(f"  {info['description']}")
                lines.append(f"  用法: {info['usage']}")
                if cmd != admin_cmds[-1]:  # 不是最后一个命令时添加空行
                    lines.append("")
        
        return "\n".join(lines)
    
    async def handle_getpos(self, user_id: int, group_id: int, command_text: str, 
                           config_manager=None, **kwargs) -> str:
        """处理 getpos 命令"""
        # 延迟初始化，传入 config_manager
        self._init_modifier(config_manager)
        
        if not self.modifier:
            return "玩家坐标插件未正确初始化，找不到 world/playerdata 目录。请检查服务器工作目录配置。"
        
        try:
            parts = command_text.strip().split()
            
            if not parts:
                return "用法: getpos <玩家名>"
            
            player_name = parts[0]
            coords = self.modifier.get_player_pos(player_name)
            
            if coords:
                x, y, z = coords
                return (
                    f"玩家坐标信息\n"
                    f"{'─' * 20}\n"
                    f"玩家: {player_name}\n"
                    f"X坐标: {x:.2f}\n"
                    f"Y坐标: {y:.2f}\n"
                    f"Z坐标: {z:.2f}\n"
                    f"{'─' * 20}"
                )
            else:
                return f"无法找到玩家 {player_name} 的数据"
                
        except Exception as e:
            self.logger.error(f"处理 getpos 命令失败: {e}")
            return f"命令执行失败: {e}"

    async def handle_setpos(self, user_id: int, group_id: int, command_text: str,
                           config_manager=None, rcon_client=None, **kwargs) -> str:
        """处理 setpos 命令"""
        # 延迟初始化，传入 config_manager
        self._init_modifier(config_manager)
        
        if not self.modifier:
            return "玩家坐标插件未正确初始化，找不到 world/playerdata 目录。请检查服务器工作目录配置。"
        
        # 检查管理员权限
        if config_manager and not config_manager.is_admin(user_id):
            return "权限不足：此命令仅限管理员使用"
        
        try:
            parts = command_text.strip().split()
            
            if len(parts) < 4:
                return "用法: setpos <玩家名> <x> <y> <z>"
            
            player_name = parts[0]
            
            try:
                x = float(parts[1])
                y = float(parts[2])
                z = float(parts[3])
            except ValueError:
                return "错误: 坐标必须是数字!"
            
            if not (-30000000 <= x <= 30000000 and -64 <= y <= 320 and -30000000 <= z <= 30000000):
                return (
                    f"错误: 坐标超出范围!\n"
                    f"输入坐标: ({x}, {y}, {z})\n"
                    f"允许范围: X,Z[-30000000,30000000] Y[-64,320]"
                )
            
            # 尝试踢出玩家
            kick_success = await self._kick_player(player_name, rcon_client)
            
            if kick_success:
                self.logger.info(f"已踢出玩家: {player_name}")
            else:
                self.logger.warning(f"无法踢出玩家或玩家未在线: {player_name}")
            
            # 修改坐标
            success = self.modifier.set_player_pos(player_name, x, y, z)
            
            if success:
                return (
                    f"成功修改玩家坐标!\n"
                    f"{'─' * 22}\n"
                    f"玩家: {player_name}\n"
                    f"新坐标: ({x:.0f}, {y:.0f}, {z:.0f})\n"
                    f"{'─' * 22}\n"
                    f"已同时更新 .dat 和 .dat_old 文件\n"
                    f"玩家下次登录时将在新位置出现"
                )
            else:
                return (
                    f"修改失败!\n"
                    "可能原因:\n"
                    f"• 找不到玩家数据: {player_name}\n"
                    "• 玩家可能还在线\n"
                    "• 文件权限问题\n"
                    "• world/playerdata 目录访问失败"
                )
            
        except Exception as e:
            self.logger.error(f"处理 setpos 命令失败: {e}")
            return f"命令执行失败: {e}"
    
    async def _kick_player(self, player_name: str, rcon_client=None) -> bool:
        """尝试踢出玩家
        
        Args:
            player_name: 玩家名
            rcon_client: RCON 客户端
            
        Returns:
            bool: 踢出是否成功
        """
        try:
            # 检查 RCON 客户端是否可用
            if not rcon_client:
                self.logger.debug("RCON 客户端不可用，跳过踢出玩家步骤")
                return False
            
            if not rcon_client.is_connected():
                self.logger.debug("RCON 连接不可用，跳过踢出玩家步骤")
                return False
            
            # 执行踢出命令
            kick_command = f"kick {player_name} 坐标已修改，请重新登录"
            result = rcon_client.execute_command(kick_command)
            
            self.logger.info(f"已执行踢出命令: {kick_command}")
            self.logger.debug(f"踢出命令结果: {result}")
            
            return True
            
        except Exception as e:
            self.logger.warning(f"踢出玩家 {player_name} 时出错: {e}")
            return False
    
    async def on_config_reload(self, old_config: dict, new_config: dict):
        """配置重新加载"""
        # 当配置重新加载时，重置 modifier 以便重新初始化
        if self.modifier:
            self.logger.info("配置已重新加载，重置玩家数据修改器")
            self.modifier = None
            self.world_path = None