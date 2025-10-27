"""
玩家坐标管理插件
这是一个示例插件，演示如何创建 MSMP_QQBot 插件

功能:
- 获取玩家坐标
- 修改玩家坐标
- 玩家传送
"""

import os
import logging
from typing import Optional, Tuple, List, Dict, Any
from plugin_manager import BotPlugin

try:
    import nbtlib
    HAS_NBTLIB = True
except ImportError:
    HAS_NBTLIB = False


class PlayerDataModifier:
    """玩家数据修改器"""
    
    def __init__(self, world_path: str, logger: logging.Logger):
        self.world_path = world_path
        self.playerdata_path = os.path.join(world_path, "playerdata")
        self.logger = logger
        
        if not HAS_NBTLIB:
            self.logger.error("未安装 nbtlib 库，请运行: pip install nbtlib")
        
        if os.path.exists(self.playerdata_path):
            self.logger.info(f"玩家数据修改器已初始化: {self.playerdata_path}")
        else:
            self.logger.error(f"playerdata 目录不存在: {self.playerdata_path}")
    
    def _find_player_dat_file(self, player_identifier: str) -> Optional[str]:
        """查找玩家 dat 文件"""
        try:
            if not os.path.exists(self.playerdata_path):
                self.logger.error("playerdata 目录不存在")
                return None
            
            # 首先假设输入是UUID，直接查找
            dat_file = os.path.join(self.playerdata_path, f"{player_identifier}.dat")
            if os.path.exists(dat_file):
                return dat_file
            
            # 如果输入包含连字符(可能是UUID格式但没有)，尝试添加
            if len(player_identifier) == 32 and '-' not in player_identifier:
                uuid_with_dash = f"{player_identifier[:8]}-{player_identifier[8:12]}-{player_identifier[12:16]}-{player_identifier[16:20]}-{player_identifier[20:]}"
                dat_file = os.path.join(self.playerdata_path, f"{uuid_with_dash}.dat")
                if os.path.exists(dat_file):
                    return dat_file
            
            # 尝试从 usercache.json 查找UUID
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
                            if os.path.exists(dat_file):
                                self.logger.info(f"从 usercache.json 找到玩家 {player_identifier} 的UUID: {uuid}")
                                return dat_file
                except Exception as e:
                    self.logger.debug(f"查询 usercache.json 失败: {e}")
            
            self.logger.warning(f"找不到玩家 {player_identifier} 的 dat 文件")
            return None
            
        except Exception as e:
            self.logger.error(f"查找玩家 dat 文件失败: {e}")
            return None
    
    def get_player_pos(self, player_identifier: str) -> Optional[Tuple[float, float, float]]:
        """获取玩家坐标"""
        if not HAS_NBTLIB:
            self.logger.error("nbtlib 库未安装")
            return None
        
        try:
            dat_file_path = self._find_player_dat_file(player_identifier)
            
            if not dat_file_path:
                self.logger.warning(f"找不到玩家 {player_identifier} 的 dat 文件")
                return None
            
            nbt_file = nbtlib.load(dat_file_path)
            
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
        """设置玩家坐标"""
        if not HAS_NBTLIB:
            self.logger.error("nbtlib 库未安装")
            return False
        
        try:
            if not (-30000000 <= x <= 30000000 and -64 <= y <= 320 and -30000000 <= z <= 30000000):
                self.logger.error(f"坐标超出范围: ({x}, {y}, {z})")
                return False
            
            dat_file_path = self._find_player_dat_file(player_identifier)
            if not dat_file_path:
                self.logger.error(f"找不到玩家 {player_identifier} 的 dat 文件")
                return False
            
            nbt_file = nbtlib.load(dat_file_path)
            
            if 'Pos' in nbt_file:
                nbt_file['Pos'] = nbtlib.tag.List[nbtlib.tag.Double]([
                    nbtlib.tag.Double(x),
                    nbtlib.tag.Double(y),
                    nbtlib.tag.Double(z)
                ])
                
                nbt_file.save()
                
                self.logger.info(f"已成功修改玩家 {player_identifier} 的坐标: ({x}, {y}, {z})")
                return True
            else:
                self.logger.error(f"玩家 NBT 数据中不存在 Pos 标签")
                return False
            
        except Exception as e:
            self.logger.error(f"修改玩家坐标失败: {e}", exc_info=True)
            return False


class PlayerCoordinatesPlugin(BotPlugin):
    """玩家坐标管理插件"""
    
    name = "玩家坐标管理"
    version = "1.0.0"
    author = "MSMP_QQBot"
    description = "提供玩家坐标查询和修改功能"
    
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
        },
        "tppos": {
            "names": ["tppos", "传送坐标", "tp"],
            "description": "传送玩家到指定坐标",
            "usage": "tppos <玩家名> <x> <y> <z>",
            "admin_only": True,
        }
    }
    
    async def on_load(self, plugin_manager: 'PluginManager') -> bool:
        """插件加载"""
        try:
            self.logger.info(f"正在加载 {self.name} 插件...")
            
            self.plugin_manager = plugin_manager
            self.modifier = None
            self.world_path = None
            
            # 注册命令（先注册，后初始化 modifier）
            await self._register_commands()
            
            # 稍后在命令执行时初始化 modifier
            self.logger.info(f"{self.name} 插件加载成功")
            return True
            
        except Exception as e:
            self.logger.error(f"加载 {self.name} 插件失败: {e}", exc_info=True)
            return False
    
    async def on_unload(self):
        """插件卸载"""
        self.logger.info(f"正在卸载 {self.name} 插件...")
        self.modifier = None
    
    async def _register_commands(self):
        """注册命令"""
        
        self.plugin_manager.register_command(
            command_name="getpos",
            handler=self.handle_getpos,
            names=["getpos", "查询坐标", "查看坐标"],
            admin_only=False,
            description="查询玩家的坐标信息",
            usage="getpos <玩家名>"
        )
        
        self.plugin_manager.register_command(
            command_name="setpos",
            handler=self.handle_setpos,
            names=["setpos", "设置坐标", "修改坐标"],
            admin_only=True,
            description="修改玩家的坐标（需要玩家离线）",
            usage="setpos <玩家名> <x> <y> <z>"
        )
        
        self.plugin_manager.register_command(
            command_name="tppos",
            handler=self.handle_tppos,
            names=["tppos", "传送坐标", "tp"],
            admin_only=True,
            description="传送玩家到指定坐标",
            usage="tppos <玩家名> <x> <y> <z>"
        )
        
        self.logger.info("已注册所有命令")
    
    def _init_modifier(self):
        """延迟初始化 modifier - 在命令执行时调用"""
        if self.modifier:
            return  # 已经初始化过了
        
        # 启动脚本路径（从配置中获取）
        start_script = "G:/paper-1.21.10/start.bat"
        script_dir = os.path.dirname(start_script)
        
        # 检查多个可能的位置
        possible_paths = [
            os.path.join(script_dir, "world"),  # 启动脚本所在目录
            "./world",
            "world",
            os.path.join(".", "world"),
        ]
        
        for path in possible_paths:
            # 将 / 转换为 os.sep（Windows 上是 \）
            path = path.replace("/", os.sep)
            full_path = os.path.abspath(path)
            playerdata_path = os.path.join(full_path, "playerdata")
            
            self.logger.debug(f"检查路径: {full_path}")
            
            if os.path.exists(full_path) and os.path.exists(playerdata_path):
                self.logger.info(f"找到 world 文件夹: {full_path}")
                self.world_path = full_path
                self.modifier = PlayerDataModifier(full_path, self.logger)
                return
        
        self.logger.warning(f"未找到 world/playerdata 目录，已检查的路径: {[os.path.abspath(p.replace('/', os.sep)) for p in possible_paths]}")
    
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
        
        return "\n".join(lines)
    
    async def handle_getpos(self, command_text: str = "", user_id: int = 0, **kwargs) -> str:
        """处理 getpos 命令"""
        # 延迟初始化
        self._init_modifier()
        
        if not self.modifier:
            return "玩家坐标插件未正确初始化，找不到 world/playerdata 目录"
        
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
    
    async def handle_setpos(self, command_text: str = "", user_id: int = 0, **kwargs) -> str:
        """处理 setpos 命令"""
        self._init_modifier()
        
        if not self.modifier:
            return "玩家坐标插件未正确初始化"
        
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
            
            success = self.modifier.set_player_pos(player_name, x, y, z)
            
            if success:
                return (
                    f"成功修改玩家坐标!\n"
                    f"{'─' * 22}\n"
                    f"玩家: {player_name}\n"
                    f"新坐标: ({x:.0f}, {y:.0f}, {z:.0f})\n"
                    f"{'─' * 22}\n"
                    f"玩家下次登录时将在新位置出现"
                )
            else:
                return (
                    f"修改失败!\n"
                    "可能原因:\n"
                    f"• 找不到玩家数据: {player_name}\n"
                    "• 玩家可能还在线\n"
                    "• 文件权限问题"
                )
            
        except Exception as e:
            self.logger.error(f"处理 setpos 命令失败: {e}")
            return f"命令执行失败: {e}"
    
    async def handle_tppos(self, command_text: str = "", user_id: int = 0, **kwargs) -> str:
        """处理 tppos 命令"""
        try:
            parts = command_text.strip().split()
            
            if len(parts) < 4:
                return "用法: tppos <玩家名> <x> <y> <z>"
            
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
            
            return (
                f"传送命令已准备\n"
                f"{'─' * 22}\n"
                f"玩家: {player_name}\n"
                f"目标坐标: ({x}, {y}, {z})\n"
                f"{'─' * 22}\n"
                f"请确保玩家在线"
            )
            
        except Exception as e:
            self.logger.error(f"处理 tppos 命令失败: {e}")
            return f"命令执行失败: {e}"
    
    async def on_config_reload(self, old_config: dict, new_config: dict):
        """配置重新加载"""
        pass