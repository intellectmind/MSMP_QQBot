"""
MSMP_QQBot 插件管理系统
支持动态加载、卸载和热重载插件
"""

import os
import sys
import logging
import importlib.util
import asyncio
import time
import nbtlib
from pathlib import Path
from typing import Dict, List, Any, Optional, Callable, Set
from abc import ABC, abstractmethod


class BotPlugin(ABC):
    """插件基类 - 所有插件必须继承此类"""
    
    # 插件元数据
    name: str = "Unknown Plugin"
    version: str = "1.0.0"
    author: str = "Unknown"
    description: str = ""
    
    def __init__(self, logger: logging.Logger):
        """
        插件初始化
        
        Args:
            logger: 日志对象
        """
        self.logger = logger
        self.config = {}
        self.enabled = True
    
    @abstractmethod
    async def on_load(self, plugin_manager: 'PluginManager'):
        """
        插件加载时调用
        
        Args:
            plugin_manager: 插件管理器实例
            
        Returns:
            True 表示加载成功，False 表示加载失败
        """
        pass
    
    @abstractmethod
    async def on_unload(self):
        """插件卸载时调用"""
        pass
    
    async def on_config_reload(self, old_config: Dict, new_config: Dict):
        """
        配置重新加载时调用
        
        Args:
            old_config: 旧配置
            new_config: 新配置
        """
        pass
    
    async def on_reload(self):
        """
        插件热重载时调用
        可以在这里执行清理和重新初始化操作
        """
        pass

    def get_plugin_help(self) -> str:
        """
        获取插件帮助信息（供全局help命令调用）
        
        Returns:
            str: 插件帮助信息
            
        注意: 子类可以重写此方法以提供自定义帮助信息
        """
        lines = [
            f"【{self.name}】 v{self.version}",
            f"作者: {self.author}",
            f"说明: {self.description}"
        ]
        
        return "\n".join(lines)
    

class PluginManager:
    """插件管理器 - 支持热加载"""
    
    def __init__(self, plugin_dir: str = "plugins", logger: logging.Logger = None):
        """
        初始化插件管理器
        
        Args:
            plugin_dir: 插件目录路径
            logger: 日志对象
        """
        self.plugin_dir = Path(plugin_dir)
        self.logger = logger or logging.getLogger(__name__)
        self.plugins: Dict[str, BotPlugin] = {}
        self.plugin_modules: Dict[str, Any] = {}  # 存储模块对象
        self.command_handlers: Dict[str, Callable] = {}
        self.event_listeners: Dict[str, List[Callable]] = {}
        self.loaded_files: Set[str] = set()  # 记录已加载的文件
        
        # 创建插件目录
        self.plugin_dir.mkdir(exist_ok=True)
        
        # 添加插件目录到 Python 路径
        if str(self.plugin_dir.absolute()) not in sys.path:
            sys.path.insert(0, str(self.plugin_dir.absolute()))
        
        self.logger.info(f"插件管理器已初始化, 插件目录: {self.plugin_dir.absolute()}")
    
    async def load_plugins(self):
        """扫描并加载所有插件"""
        if not self.plugin_dir.exists():
            self.logger.warning(f"插件目录不存在: {self.plugin_dir}")
            return
        
        # 扫描所有 .py 文件
        plugin_files = list(self.plugin_dir.glob("*.py"))
        
        if not plugin_files:
            self.logger.info("未发现任何插件")
            return
        
        self.logger.info(f"发现 {len(plugin_files)} 个插件文件")
        
        for plugin_file in plugin_files:
            # 跳过 __init__.py 和以 _ 开头的文件
            if plugin_file.name.startswith("_"):
                continue
            
            await self._load_plugin_file(plugin_file)
    
    async def _load_plugin_file(self, plugin_file: Path) -> bool:
        """加载单个插件文件"""
        try:
            module_name = plugin_file.stem
            self.logger.info(f"正在加载插件: {module_name}")
            
            # 如果模块已加载，先卸载
            if module_name in sys.modules:
                await self._unload_plugin_module(module_name)
            
            # 动态加载模块
            spec = importlib.util.spec_from_file_location(module_name, plugin_file)
            if not spec or not spec.loader:
                self.logger.error(f"无法加载插件模块: {plugin_file}")
                return False
            
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            
            # 查找插件类
            plugin_class = self._find_plugin_class(module)
            
            if not plugin_class:
                self.logger.warning(f"插件 {module_name} 中未找到有效的插件类 (需继承 BotPlugin)")
                return False
            
            # 实例化插件
            plugin_instance = plugin_class(self.logger.getChild(f"plugin.{module_name}"))
            
            # 调用插件的 on_load 方法
            success = await plugin_instance.on_load(self)
            
            if success:
                self.plugins[module_name] = plugin_instance
                self.plugin_modules[module_name] = module
                self.loaded_files.add(str(plugin_file.absolute()))
                self.logger.info(f"插件加载成功: {plugin_instance.name} v{plugin_instance.version} (作者: {plugin_instance.author})")
                return True  # 明确返回 True
            else:
                self.logger.error(f"插件加载失败: {module_name}")
                return False
        
        except Exception as e:
            self.logger.error(f"加载插件 {plugin_file.name} 时出错: {e}", exc_info=True)
            return False
    
    def _find_plugin_class(self, module) -> Optional[type]:
        """从模块中查找插件类"""
        for item_name in dir(module):
            item = getattr(module, item_name)
            
            # 检查是否是类且继承自 BotPlugin
            if (isinstance(item, type) and 
                issubclass(item, BotPlugin) and 
                item is not BotPlugin):
                return item
        
        return None
    
    async def unload_plugins(self):
        """卸载所有插件"""
        for module_name, plugin in list(self.plugins.items()):
            try:
                await plugin.on_unload()
                self.logger.info(f"插件已卸载: {module_name}")
            except Exception as e:
                self.logger.error(f"卸载插件 {module_name} 时出错: {e}", exc_info=True)
        
        # 清理模块引用
        for module_name in list(self.plugin_modules.keys()):
            await self._unload_plugin_module(module_name)
        
        self.plugins.clear()
        self.plugin_modules.clear()
        self.command_handlers.clear()
        self.event_listeners.clear()
        self.loaded_files.clear()
    
    async def _unload_plugin_module(self, module_name: str):
        """卸载插件模块"""
        try:
            # 从模块缓存中移除
            if module_name in sys.modules:
                del sys.modules[module_name]
            
            # 从我们的模块字典中移除
            if module_name in self.plugin_modules:
                del self.plugin_modules[module_name]
                
        except Exception as e:
            self.logger.warning(f"卸载模块 {module_name} 时出错: {e}")
    
    async def reload_plugin(self, plugin_name: str) -> bool:
        """
        重新加载指定插件
        
        Args:
            plugin_name: 插件名称（文件名，不含.py后缀）
            
        Returns:
            bool: 重载是否成功
        """
        try:
            plugin_file = self.plugin_dir / f"{plugin_name}.py"
            
            if not plugin_file.exists():
                self.logger.error(f"插件文件不存在: {plugin_file}")
                return False
            
            # 检查插件是否已加载
            if plugin_name not in self.plugins:
                self.logger.error(f"插件未加载: {plugin_name}")
                return False
            
            self.logger.info(f"正在重新加载插件: {plugin_name}")
            
            # 调用插件的 on_reload 方法（如果存在）
            plugin_instance = self.plugins[plugin_name]
            if hasattr(plugin_instance, 'on_reload'):
                try:
                    await plugin_instance.on_reload()
                    self.logger.debug(f"已调用插件的 on_reload 方法: {plugin_name}")
                except Exception as e:
                    self.logger.warning(f"调用插件 on_reload 方法失败: {e}")
            
            # 先卸载插件
            await self.unload_plugin(plugin_name)
            
            # 等待一小段时间确保完全卸载
            await asyncio.sleep(0.1)
            
            # 重新加载插件
            success = await self._load_plugin_file(plugin_file)
            
            if success:
                self.logger.info(f"插件重载成功: {plugin_name}")
                return True
            else:
                self.logger.error(f"插件重载失败: {plugin_name}")
                return False
                
        except Exception as e:
            self.logger.error(f"重载插件 {plugin_name} 时出错: {e}", exc_info=True)
            return False
    
    async def unload_plugin(self, plugin_name: str) -> bool:
        """
        卸载指定插件
        
        Args:
            plugin_name: 插件名称
            
        Returns:
            bool: 卸载是否成功
        """
        try:
            if plugin_name not in self.plugins:
                self.logger.warning(f"插件未加载: {plugin_name}")
                return False
            
            plugin_instance = self.plugins[plugin_name]
            
            # 调用插件的卸载方法
            await plugin_instance.on_unload()
            
            # 清理插件注册的命令和事件监听器
            self._cleanup_plugin_handlers(plugin_name)
            
            # 移除插件引用
            del self.plugins[plugin_name]
            
            # 卸载模块
            await self._unload_plugin_module(plugin_name)
            
            # 从已加载文件列表中移除
            plugin_file = self.plugin_dir / f"{plugin_name}.py"
            self.loaded_files.discard(str(plugin_file.absolute()))
            
            self.logger.info(f"插件已卸载: {plugin_name}")
            return True
            
        except Exception as e:
            self.logger.error(f"卸载插件 {plugin_name} 时出错: {e}", exc_info=True)
            return False
    
    def _cleanup_plugin_handlers(self, plugin_name: str):
        """清理插件注册的命令和事件监听器"""
        # 清理命令处理器
        commands_to_remove = []
        for cmd_name, handler_info in self.command_handlers.items():
            # 通过函数名判断是否属于该插件
            handler_func = handler_info.get('handler')
            if (handler_func and 
                hasattr(handler_func, '__module__') and 
                handler_func.__module__.startswith(plugin_name)):
                commands_to_remove.append(cmd_name)
        
        for cmd_name in commands_to_remove:
            del self.command_handlers[cmd_name]
            self.logger.debug(f"已清理插件命令: {cmd_name}")
        
        # 清理事件监听器
        for event_name, listeners in list(self.event_listeners.items()):
            listeners_to_remove = []
            for listener in listeners:
                if (hasattr(listener, '__module__') and 
                    listener.__module__.startswith(plugin_name)):
                    listeners_to_remove.append(listener)
            
            for listener in listeners_to_remove:
                listeners.remove(listener)
                self.logger.debug(f"已清理插件事件监听器: {event_name}")
            
            # 如果事件没有监听器了，移除整个事件
            if not listeners:
                del self.event_listeners[event_name]
    
    async def load_plugin(self, plugin_name: str) -> bool:
        """
        加载指定插件
        
        Args:
            plugin_name: 插件名称（文件名，不含.py后缀）
            
        Returns:
            bool: 加载是否成功
        """
        try:
            plugin_file = self.plugin_dir / f"{plugin_name}.py"
            
            if not plugin_file.exists():
                self.logger.error(f"插件文件不存在: {plugin_file}")
                return False
            
            return await self._load_plugin_file(plugin_file)
            
        except Exception as e:
            self.logger.error(f"加载插件 {plugin_name} 时出错: {e}", exc_info=True)
            return False
    
    async def scan_and_reload_changed(self) -> Dict[str, bool]:
        """
        扫描插件目录，重新加载发生变化的插件
        
        Returns:
            Dict[str, bool]: 重载结果 {插件名: 是否成功}
        """
        results = {}
        
        if not self.plugin_dir.exists():
            return results
        
        # 扫描所有 .py 文件
        plugin_files = list(self.plugin_dir.glob("*.py"))
        
        for plugin_file in plugin_files:
            if plugin_file.name.startswith("_"):
                continue
            
            plugin_name = plugin_file.stem
            file_path = str(plugin_file.absolute())
            
            # 检查文件是否已加载且是否发生变化
            if file_path in self.loaded_files:
                # 检查文件修改时间
                current_mtime = plugin_file.stat().st_mtime
                
                # 这里需要记录文件的加载时间，简化实现：总是重新加载
                # 在实际应用中，可以记录文件的修改时间进行比较
                results[plugin_name] = await self.reload_plugin(plugin_name)
            else:
                # 新插件，加载它
                results[plugin_name] = await self.load_plugin(plugin_name)
        
        return results
    
    def register_command(self, command_name: str, handler: Callable, 
                        names: List[str] = None, admin_only: bool = False,
                        description: str = "", usage: str = "", 
                        cooldown: int = 0, command_key: str = ""):
        """
        注册命令 (从插件中调用)
        
        Args:
            command_name: 命令名称
            handler: 命令处理函数
            names: 命令的所有别名
            admin_only: 是否仅管理员可用
            description: 命令描述
            usage: 使用说明
            cooldown: 冷却时间
            command_key: 命令键
        """
        if names is None:
            names = [command_name]
        
        self.command_handlers[command_name] = {
            "handler": handler,
            "names": names,
            "admin_only": admin_only,
            "description": description,
            "usage": usage,
            "cooldown": cooldown,
            "command_key": command_key
        }
        
        self.logger.debug(f"已注册命令: {command_name} (别名: {', '.join(names)})")
    
    def register_event_listener(self, event_name: str, listener: Callable):
        """
        注册事件监听器
        
        Args:
            event_name: 事件名称
            listener: 事件处理函数
        """
        if event_name not in self.event_listeners:
            self.event_listeners[event_name] = []
        
        self.event_listeners[event_name].append(listener)
        self.logger.debug(f"已注册事件监听器: {event_name}")
    
    async def trigger_event(self, event_name: str, *args, **kwargs):
        """
        触发事件
        
        Args:
            event_name: 事件名称
            *args: 位置参数
            **kwargs: 关键字参数
        """
        if event_name not in self.event_listeners:
            return
        
        for listener in self.event_listeners[event_name]:
            try:
                if callable(listener):
                    if hasattr(listener, '__call__'):
                        import inspect
                        if inspect.iscoroutinefunction(listener):
                            await listener(*args, **kwargs)
                        else:
                            listener(*args, **kwargs)
            except Exception as e:
                self.logger.error(f"触发事件 {event_name} 时出错: {e}", exc_info=True)
    
    def get_plugin(self, plugin_name: str) -> Optional[BotPlugin]:
        """获取指定插件"""
        return self.plugins.get(plugin_name)
    
    def get_all_plugins(self) -> Dict[str, BotPlugin]:
        """获取所有已加载的插件"""
        return self.plugins.copy()
    
    def get_plugin_info(self, plugin_name: str) -> Optional[Dict[str, Any]]:
        """获取插件信息"""
        plugin = self.get_plugin(plugin_name)
        if not plugin:
            return None
        
        return {
            "name": plugin.name,
            "version": plugin.version,
            "author": plugin.author,
            "description": plugin.description,
            "enabled": plugin.enabled
        }
    
    def list_plugins(self) -> List[Dict[str, Any]]:
        """列出所有已加载插件的信息"""
        return [
            self.get_plugin_info(name)
            for name in self.plugins.keys()
        ]
    
    async def reload_config(self, old_config: Dict, new_config: Dict):
        """通知所有插件配置已重新加载"""
        for plugin in self.plugins.values():
            try:
                await plugin.on_config_reload(old_config, new_config)
            except Exception as e:
                self.logger.error(f"插件配置重新加载失败: {e}", exc_info=True)
    
    def get_plugin_status(self) -> str:
        """获取插件系统状态信息"""
        lines = ["插件系统状态", "=" * 20]
        lines.append(f"已加载插件: {len(self.plugins)}")
        lines.append(f"注册命令: {len(self.command_handlers)}")
        lines.append(f"事件监听器: {sum(len(lst) for lst in self.event_listeners.values())}")
        
        if self.plugins:
            lines.append("\n已加载插件列表:")
            for name, plugin in self.plugins.items():
                lines.append(f"  • {plugin.name} v{plugin.version} - {plugin.author}")
                lines.append(f"    描述: {plugin.description}")
                lines.append(f"    状态: {'启用' if plugin.enabled else '禁用'}")
        
        return "\n".join(lines)

    def find_plugin_by_name(self, search_name: str) -> Optional[BotPlugin]:
        """
        通过插件文件名或插件名称查找插件
        
        Args:
            search_name: 插件文件名或插件显示名称
            
        Returns:
            找到的插件实例，未找到返回None
        """
        search_name_lower = search_name.lower().strip()
        
        # 1. 首先按文件名查找（精确匹配）
        if search_name_lower in self.plugins:
            return self.plugins[search_name_lower]
        
        # 2. 按插件显示名称查找（不区分大小写，包含匹配）
        for plugin_name, plugin in self.plugins.items():
            plugin_display_name = plugin.name.lower() if plugin.name else ""
            if (search_name_lower in plugin_display_name or 
                search_name_lower == plugin_name):
                return plugin
        
        # 3. 尝试模糊匹配（包含关系）
        for plugin_name, plugin in self.plugins.items():
            plugin_display_name = plugin.name.lower() if plugin.name else ""
            if (search_name_lower in plugin_display_name or 
                search_name_lower in plugin_name.lower()):
                return plugin
        
        return None

    def get_plugin_search_hints(self, search_name: str) -> List[str]:
        """
        获取插件搜索提示
        
        Args:
            search_name: 搜索名称
            
        Returns:
            匹配的插件名称列表
        """
        search_name_lower = search_name.lower().strip()
        hints = []
        
        for plugin_name, plugin in self.plugins.items():
            plugin_display_name = plugin.name.lower() if plugin.name else ""
            if (search_name_lower in plugin_display_name or 
                search_name_lower in plugin_name.lower()):
                hints.append(f"{plugin.name} (文件名: {plugin_name})")
        
        return hints