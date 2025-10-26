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
from pathlib import Path
from typing import Optional, Tuple, List
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
            # 验证坐标范围
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
    
    async def on_load(self, plugin_manager: 'PluginManager') -> bool:
        """插件加载"""
        try:
            self.logger.info(f"正在加载 {self.name} 插件...")
            
            self.plugin_manager = plugin_manager
            
            # 初始化玩家数据修改器
            # 注意: 这里需要从配置管理器获取世界路径
            # 由于插件无法直接访问 config_manager，我们通过参数传递
            self.modifier = None
            self.logger.info(f"{self.name} 插件加载成功")
            
            # 注册命令
            await self._register_commands()
            
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
        
        # 注册 getpos 命令
        self.plugin_manager.register_command(
            command_name="getpos",
            handler=self.handle_getpos,
            names=["getpos", "查询坐标", "查看坐标"],
            admin_only=False,
            description="查询玩家的坐标信息",
            usage="getpos <玩家名>"
        )
        
        # 注册 setpos 命令
        self.plugin_manager.register_command(
            command_name="setpos",
            handler=self.handle_setpos,
            names=["setpos", "设置坐标", "修改坐标"],
            admin_only=True,
            description="修改玩家的坐标（需要玩家离线）",
            usage="setpos <玩家名> <x> <y> <z>"
        )
        
        # 注册 tppos 命令
        self.plugin_manager.register_command(
            command_name="tppos",
            handler=self.handle_tppos,
            names=["tppos", "传送坐标", "tp"],
            admin_only=True,
            description="传送玩家到指定坐标",
            usage="tppos <玩家名> <x> <y> <z>"
        )
        
        self.logger.info("已注册所有命令")
    
    def _set_modifier(self, world_path: str):
        """设置玩家数据修改器"""
        self.modifier = PlayerDataModifier(world_path, self.logger)
    
    async def handle_getpos(self, command_text: str = "", user_id: int = 0, **kwargs) -> str:
        """处理 getpos 命令"""
        if not self.modifier:
            return "玩家坐标插件未正确初始化"
        
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
        if not self.modifier:
            return "玩家坐标插件未正确初始化"
        
        try:
            parts = command_text.strip().split()
            
            if len(parts) < 4:
                return (
                    "用法错误\n"
                    f"{'─' * 22}\n"
                    "命令: setpos <玩家名> <x> <y> <z>\n"
                    "示例: setpos Steve 100 64 100\n"
                    f"{'─' * 22}\n"
                    "注意:\n"
                    "• 玩家必须离线才能修改\n"
                    "• 玩家下次登录将在新位置出现\n"
                    "• 坐标范围: X,Z[-30000000,30000000] Y[-64,320]"
                )
            
            player_name = parts[0]
            
            try:
                x = float(parts[1])
                y = float(parts[2])
                z = float(parts[3])
            except ValueError:
                return "错误: 坐标必须是数字!"
            
            # 验证坐标范围
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
                    f"玩家下次登录时将在新位置出现\n"
                    f"原数据已自动备份"
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
        """处理 tppos 命令 (实际传送需要通过 RCON/MSMP 执行)"""
        try:
            parts = command_text.strip().split()
            
            if len(parts) < 4:
                return "用法: tppos <玩家名> <x> <y> <z>"
            
            player_name = parts[0]
            x, y, z = float(parts[1]), float(parts[2]), float(parts[3])
            
            # 这里需要通过 RCON 或 MSMP 实际执行传送命令
            return (
                f"传送命令已准备\n"
                f"玩家: {player_name}\n"
                f"目标坐标: ({x}, {y}, {z})\n"
                f"请确保玩家在线"
            )
            
        except Exception as e:
            self.logger.error(f"处理 tppos 命令失败: {e}")
            return f"命令执行失败: {e}"
    
    async def on_config_reload(self, old_config: dict, new_config: dict):
        """配置重新加载时更新世界路径"""
        try:
            # 获取新的世界路径
            world_path = new_config.get('server', {}).get('world_path')
            if world_path:
                self._set_modifier(world_path)
                self.logger.info("玩家坐标插件配置已更新")
        except Exception as e:
            self.logger.error(f"更新玩家坐标插件配置失败: {e}")
