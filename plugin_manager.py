import os
import sys
import logging
import importlib.util
import asyncio
import time
import nbtlib
import json
import aiohttp
from openai import AsyncOpenAI
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
        self.dependencies = []  # 插件依赖列表
    
    @abstractmethod
    async def on_load(self, plugin_manager: 'PluginManager') -> bool:
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
            f"【{self.name}】v{self.version}",
            f"作者: {self.author}",
            f"说明: {self.description}"
        ]
        
        return "\n".join(lines)


class PluginManager:
    """插件管理器 - 支持热加载和子目录结构"""
    
    def __init__(self, plugin_dir: str = "plugins", logger: logging.Logger = None, qq_server=None):
        """
        初始化插件管理器
        
        Args:
            plugin_dir: 插件目录路径
            logger: 日志对象
            qq_server: QQBotWebSocketServer实例（用于访问服务端日志）
        """
        self.plugin_dir = Path(plugin_dir)
        self.logger = logger or logging.getLogger(__name__)
        self.qq_server = qq_server
        self.plugins: Dict[str, BotPlugin] = {}
        self.plugin_modules: Dict[str, Any] = {}
        self.command_handlers: Dict[str, Dict[str, Any]] = {}
        self.event_listeners: Dict[str, List[Callable]] = {}
        self.loaded_files: Set[str] = set()
        self.plugin_file_paths: Dict[str, Path] = {}
        self.plugin_dependencies: Dict[str, List[str]] = {}
        
        self.plugin_dir.mkdir(exist_ok=True)
        if str(self.plugin_dir.absolute()) not in sys.path:
            sys.path.insert(0, str(self.plugin_dir.absolute()))
        
        self.logger.info(f"插件管理器已初始化, 插件目录: {self.plugin_dir.absolute()}")
    
    async def load_plugins(self):
        """扫描并加载所有插件（包括子目录）"""
        if not self.plugin_dir.exists():
            self.logger.warning(f"插件目录不存在: {self.plugin_dir}")
            return
        
        # 递归扫描所有 .py 文件（包括子目录）
        plugin_files = list(self.plugin_dir.rglob("*.py"))
        
        if not plugin_files:
            self.logger.info("未发现任何插件")
            return
        
        self.logger.info(f"发现 {len(plugin_files)} 个插件文件")
        
        for plugin_file in plugin_files:
            # 跳过 __init__.py 和以 _ 开头的文件
            if plugin_file.name.startswith("_") or plugin_file.name == "__init__.py":
                continue
            
            await self._load_plugin_file(plugin_file)
    
    async def _load_plugin_file(self, plugin_file: Path) -> bool:
        """加载单个插件文件"""
        try:
            # 生成模块名：将文件路径转换为模块路径
            # 例如: plugins/whitelist_audit/whitelist_audit.py -> whitelist_audit.whitelist_audit
            relative_path = plugin_file.relative_to(self.plugin_dir)
            module_name = str(relative_path).replace('.py', '').replace(os.sep, '.')
            
            self.logger.info(f"正在加载插件: {module_name} (文件: {plugin_file})")
            
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
            
            # 检查依赖关系
            if hasattr(plugin_instance, 'dependencies') and plugin_instance.dependencies:
                missing_deps = self._check_dependencies(plugin_instance.dependencies)
                if missing_deps:
                    self.logger.error(
                        f"插件 {module_name} 依赖缺失: {', '.join(missing_deps)}"
                    )
                    return False
            
            # 调用插件的 on_load 方法
            success = await plugin_instance.on_load(self)
            
            if success:
                self.plugins[module_name] = plugin_instance
                self.plugin_modules[module_name] = module
                self.plugin_file_paths[module_name] = plugin_file
                self.loaded_files.add(str(plugin_file.absolute()))
                self.logger.info(
                    f"插件加载成功: {plugin_instance.name} v{plugin_instance.version} "
                    f"(作者: {plugin_instance.author})"
                )
                return True
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
    
    def _check_dependencies(self, dependencies: List[str]) -> List[str]:
        """检查插件依赖是否满足，返回缺失的依赖"""
        missing = []
        for dep in dependencies:
            if dep not in self.plugins:
                missing.append(dep)
        return missing
    
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
        self.plugin_file_paths.clear()
        self.command_handlers.clear()
        self.event_listeners.clear()
        self.loaded_files.clear()
        self.plugin_dependencies.clear()
    
    async def _unload_plugin_module(self, module_name: str):
        """卸载插件模块"""
        try:
            if module_name in sys.modules:
                del sys.modules[module_name]
            
            if module_name in self.plugin_modules:
                del self.plugin_modules[module_name]
                
        except Exception as e:
            self.logger.warning(f"卸载模块 {module_name} 时出错: {e}")
    
    async def reload_plugin(self, plugin_name: str) -> bool:
        """
        重新加载指定插件
        
        Args:
            plugin_name: 插件名称（模块名，如 whitelist_audit.whitelist_audit）
            
        Returns:
            bool: 重新加载是否成功
        """
        try:
            if plugin_name not in self.plugin_file_paths:
                self.logger.error(f"插件文件路径未找到: {plugin_name}")
                return False
            
            plugin_file = self.plugin_file_paths[plugin_name]
            
            if not plugin_file.exists():
                self.logger.error(f"插件文件不存在: {plugin_file}")
                return False
            
            if plugin_name not in self.plugins:
                self.logger.error(f"插件未加载: {plugin_name}")
                return False
            
            self.logger.info(f"正在重新加载插件: {plugin_name}")
            
            # 调用插件的 on_reload 方法
            plugin_instance = self.plugins[plugin_name]
            if hasattr(plugin_instance, 'on_reload'):
                try:
                    await plugin_instance.on_reload()
                    self.logger.debug(f"已调用插件的 on_reload 方法: {plugin_name}")
                except Exception as e:
                    self.logger.warning(f"调用插件 on_reload 方法失败: {e}")
            
            # 先卸载插件
            await self.unload_plugin(plugin_name)
            
            # 等待一段时间确保完全卸载
            await asyncio.sleep(0.1)
            
            # 重新加载插件
            success = await self._load_plugin_file(plugin_file)
            
            if success:
                self.logger.info(f"插件重新加载成功: {plugin_name}")
                return True
            else:
                self.logger.error(f"插件重新加载失败: {plugin_name}")
                return False
                
        except Exception as e:
            self.logger.error(f"重新加载插件 {plugin_name} 时出错: {e}", exc_info=True)
            return False
    
    async def unload_plugin(self, plugin_name: str) -> bool:
        """
        卸载指定插件
        
        Args:
            plugin_name: 插件名称（模块名）
            
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
            if plugin_name in self.plugin_file_paths:
                self.loaded_files.discard(str(self.plugin_file_paths[plugin_name].absolute()))
                del self.plugin_file_paths[plugin_name]
            
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
            
            if not listeners:
                del self.event_listeners[event_name]
    
    async def load_plugin(self, plugin_name: str) -> bool:
        """
        加载指定插件
        
        Args:
            plugin_name: 插件名称（模块名，如 whitelist_audit.whitelist_audit）
            
        Returns:
            bool: 加载是否成功
        """
        try:
            # 查找对应的插件文件
            plugin_file = None
            for file_path in self.plugin_dir.rglob("*.py"):
                if file_path.name.startswith("_"):
                    continue
                
                relative_path = file_path.relative_to(self.plugin_dir)
                file_module_name = str(relative_path).replace('.py', '').replace(os.sep, '.')
                
                if file_module_name == plugin_name:
                    plugin_file = file_path
                    break
            
            if not plugin_file:
                self.logger.error(f"插件文件未找到: {plugin_name}")
                return False
            
            return await self._load_plugin_file(plugin_file)
            
        except Exception as e:
            self.logger.error(f"加载插件 {plugin_name} 时出错: {e}", exc_info=True)
            return False
    
    async def scan_and_reload_changed(self) -> Dict[str, bool]:
        """
        扫描插件目录，重新加载发生变化的插件
        
        Returns:
            Dict[str, bool]: 重新加载结果 {插件名: 是否成功}
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
                
                # 这里需要记录文件的修改时间，简化实现：总是重新加载
                results[plugin_name] = await self.reload_plugin(plugin_name)
            else:
                # 新插件，加载它
                results[plugin_name] = await self.load_plugin(plugin_name)
        
        return results
    
    # ============ 命令和事件相关接口 ============
    
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
                    import inspect
                    if inspect.iscoroutinefunction(listener):
                        await listener(*args, **kwargs)
                    else:
                        listener(*args, **kwargs)
            except Exception as e:
                self.logger.error(f"触发事件 {event_name} 时出错: {e}", exc_info=True)
    
    def unregister_command(self, command_name: str) -> bool:
        """
        注销指定的命令
        
        Args:
            command_name: 命令名称
            
        Returns:
            bool: 是否成功注销
        """
        try:
            if command_name in self.command_handlers:
                del self.command_handlers[command_name]
                self.logger.debug(f"已注销命令: {command_name}")
                return True
            return False
        except Exception as e:
            self.logger.error(f"注销命令失败: {e}")
            return False
    
    def update_command(self, command_name: str, **updates) -> bool:
        """
        更新命令的属性
        
        Args:
            command_name: 命令名称
            **updates: 要更新的属性（如description, cooldown等）
            
        Returns:
            bool: 是否成功更新
        """
        try:
            if command_name in self.command_handlers:
                self.command_handlers[command_name].update(updates)
                self.logger.debug(f"已更新命令: {command_name}")
                return True
            return False
        except Exception as e:
            self.logger.error(f"更新命令失败: {e}")
            return False
    
    def remove_event_listener(self, event_name: str, listener: Callable) -> bool:
        """
        移除指定事件的监听器
        
        Args:
            event_name: 事件名称
            listener: 监听器函数
            
        Returns:
            bool: 是否成功移除
        """
        try:
            if event_name in self.event_listeners:
                if listener in self.event_listeners[event_name]:
                    self.event_listeners[event_name].remove(listener)
                    
                    # 如果没有监听器了，删除整个事件
                    if not self.event_listeners[event_name]:
                        del self.event_listeners[event_name]
                    
                    return True
            return False
        except Exception as e:
            self.logger.error(f"移除事件监听器失败: {e}")
            return False
    
    def has_event_listener(self, event_name: str) -> bool:
        """
        检查是否有指定事件的监听器
        
        Args:
            event_name: 事件名称
            
        Returns:
            bool: 是否有监听器
        """
        return event_name in self.event_listeners and len(self.event_listeners[event_name]) > 0
    
    def get_registered_events(self) -> Dict[str, int]:
        """获取所有已注册的事件及其监听器数量"""
        return {
            event_name: len(listeners)
            for event_name, listeners in self.event_listeners.items()
        }
    
    # ============ 插件信息查询接口 ============
    
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
            "enabled": plugin.enabled,
            "dependencies": getattr(plugin, 'dependencies', [])
        }
    
    def list_plugins(self) -> List[Dict[str, Any]]:
        """列出所有已加载插件的信息"""
        return [
            self.get_plugin_info(name)
            for name in self.plugins.keys()
        ]
    
    def find_plugin_by_name(self, search_name: str) -> Optional[BotPlugin]:
        """
        通过插件文件名或插件名称查找插件
        
        Args:
            search_name: 插件文件名或插件名称
            
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
    
    def get_registered_commands(self) -> Dict[str, Dict[str, Any]]:
        """获取所有已注册的命令"""
        return {
            cmd_name: {
                'names': cmd_info.get('names', []),
                'admin_only': cmd_info.get('admin_only', False),
                'description': cmd_info.get('description', ''),
                'cooldown': cmd_info.get('cooldown', 0)
            }
            for cmd_name, cmd_info in self.command_handlers.items()
        }
    
    def get_all_plugin_info(self) -> List[Dict[str, Any]]:
        """获取所有插件的完整信息"""
        result = []
        for name, plugin in self.plugins.items():
            info = {
                'file_name': name,
                'name': plugin.name,
                'version': plugin.version,
                'author': plugin.author,
                'description': plugin.description,
                'enabled': plugin.enabled,
                'dependencies': getattr(plugin, 'dependencies', []),
                'config': getattr(plugin, 'config', {})
            }
            result.append(info)
        return result
    
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
                lines.append(f"    状态: {'可用' if plugin.enabled else '禁用'}")
        
        return "\n".join(lines)
    
    def get_plugin_config(self, plugin_name: str) -> Optional[Dict]:
        """
        获取插件的配置字典
        
        Args:
            plugin_name: 插件名称
            
        Returns:
            插件配置或None
        """
        plugin = self.get_plugin(plugin_name)
        if plugin:
            return getattr(plugin, 'config', {})
        return None
    
    def set_plugin_config(self, plugin_name: str, config: Dict) -> bool:
        """
        设置插件的配置字典
        
        Args:
            plugin_name: 插件名称
            config: 新配置
            
        Returns:
            是否设置成功
        """
        plugin = self.get_plugin(plugin_name)
        if plugin:
            try:
                plugin.config = config
                self.logger.debug(f"已设置插件{plugin_name}的配置")
                return True
            except Exception as e:
                self.logger.error(f"设置插件配置失败: {e}")
        return False
    
    def call_plugin_method(self, plugin_name: str, method_name: str, *args, **kwargs) -> Optional[Any]:
        """
        调用插件中的特定方法
        
        Args:
            plugin_name: 插件名称
            method_name: 方法名称
            *args: 位置参数
            **kwargs: 关键字参数
            
        Returns:
            方法的返回值或None
        """
        plugin = self.get_plugin(plugin_name)
        if not plugin:
            self.logger.warning(f"插件未找到: {plugin_name}")
            return None
        
        try:
            method = getattr(plugin, method_name, None)
            if callable(method):
                return method(*args, **kwargs)
            else:
                self.logger.warning(f"方法不存在或不可调用: {method_name}")
                return None
        except Exception as e:
            self.logger.error(f"调用插件方法失败: {e}", exc_info=True)
            return None
    
    def search_plugins_by_author(self, author: str) -> List[BotPlugin]:
        """
        按作者搜索插件
        
        Args:
            author: 作者名称
            
        Returns:
            匹配的插件列表
        """
        result = []
        author_lower = author.lower()
        for plugin in self.plugins.values():
            if author_lower in plugin.author.lower():
                result.append(plugin)
        return result
    
    def get_plugin_dependencies(self, plugin_name: str) -> List[str]:
        """
        获取插件的依赖插件列表
        
        Args:
            plugin_name: 插件名称
            
        Returns:
            依赖的插件列表
        """
        plugin = self.get_plugin(plugin_name)
        if plugin and hasattr(plugin, 'dependencies'):
            return plugin.dependencies
        return []
    
    async def reload_config(self, old_config: Dict, new_config: Dict):
        """通知所有插件配置已重新加载"""
        for plugin in self.plugins.values():
            try:
                await plugin.on_config_reload(old_config, new_config)
            except Exception as e:
                self.logger.error(f"插件配置重新加载失败: {e}", exc_info=True)
    
    def get_server_logs(self, lines: int = 50) -> List[str]:
        """
        获取MC服务端日志（来自内存缓冲区）
        
        Args:
            lines: 获取的日志行数，默认50行
            
        Returns:
            日志行列表，最新的日志在后
        """
        if not self.qq_server:
            self.logger.debug("QQ服务器实例未初始化，无法获取服务端日志")
            return []
        
        try:
            # 检查服务器是否在运行
            if not hasattr(self.qq_server, 'server_process') or not self.qq_server.server_process:
                self.logger.debug("服务器进程未运行")
                return []
                
            # 检查进程状态
            if (self.qq_server.server_process.poll() is not None):
                self.logger.debug("服务器进程已停止")
                return []
                
            return self.qq_server.get_recent_logs(lines)
        except Exception as e:
            self.logger.error(f"获取服务端日志失败: {e}")
            return []
    
    def get_latest_server_log(self) -> Optional[str]:
        """
        获取最新的MC服务端日志行
        
        Returns:
            最新的日志行，如果无日志返回None
            
        示例:
            latest = plugin_manager.get_latest_server_log()
            if latest:
                print(f"最新日志: {latest}")
        """
        logs = self.get_server_logs(1)
        return logs[0] if logs else None
    
    def search_server_logs(self, keyword: str, lines: int = 100) -> List[str]:
        """
        搜索MC服务端日志中包含指定关键字的行
        
        Args:
            keyword: 搜索关键字
            lines: 搜索范围（最近的N行日志），默认100行
            
        Returns:
            匹配的日志行列表
            
        示例:
            error_logs = plugin_manager.search_server_logs("ERROR", 100)
            for log in error_logs:
                print(log)
        """
        try:
            all_logs = self.get_server_logs(lines)
            keyword_lower = keyword.lower()
            return [log for log in all_logs if keyword_lower in log.lower()]
        except Exception as e:
            self.logger.error(f"搜索服务端日志失败: {e}")
            return []
    
    def get_server_logs_info(self) -> Dict[str, Any]:
        """
        获取MC服务端日志系统的详细信息
        
        Returns:
            包含日志统计信息的字典
            
        示例:
            info = plugin_manager.get_server_logs_info()
            print(f"当前日志行数: {info['current_lines']}")
            print(f"最大容量: {info['max_lines']}")
            print(f"使用率: {info['usage_percent']}%")
        """
        if not self.qq_server:
            return {
                'current_lines': 0,
                'max_lines': 0,
                'usage_percent': 0.0,
                'memory_usage_kb': 0.0
            }
        
        try:
            current_lines = len(self.qq_server.server_logs)
            max_lines = self.qq_server.server_logs.maxlen or 100
            usage_percent = (current_lines / max_lines * 100) if max_lines > 0 else 0
            
            # 估算内存使用
            memory_usage_kb = current_lines * 0.15  # 每行约150字节
            
            return {
                'current_lines': current_lines,
                'max_lines': max_lines,
                'usage_percent': usage_percent,
                'memory_usage_kb': memory_usage_kb,
                'status': 'running' if self.qq_server.server_process and self.qq_server.server_process.poll() is None else 'stopped'
            }
        except Exception as e:
            self.logger.error(f"获取日志信息失败: {e}")
            return {}
    
    def clear_server_logs(self) -> bool:
        """
        清空服务端日志缓冲区
        
        注意：这只清空内存中的日志，不影响磁盘上的日志文件
        
        Returns:
            清空是否成功
            
        示例:
            if plugin_manager.clear_server_logs():
                print("日志已清空")
        """
        if not self.qq_server:
            return False
        
        try:
            self.qq_server.server_logs.clear()
            self.logger.info("服务端日志缓冲区已清空")
            return True
        except Exception as e:
            self.logger.error(f"清空日志失败: {e}")
            return False

    def get_server_status(self) -> Dict[str, Any]:
        """
        获取MC服务器运行状态
        
        Returns:
            服务器状态信息字典
            
        示例:
            status = plugin_manager.get_server_status()
            print(f"服务器运行中: {status['is_running']}")
            print(f"进程PID: {status['pid']}")
        """
        if not self.qq_server:
            return {'is_running': False, 'error': 'QQ服务器实例未初始化'}
        
        try:
            process = self.qq_server.server_process
            is_running = process and process.poll() is None
            
            status = {
                'is_running': is_running,
                'pid': process.pid if is_running else None,
                'return_code': process.returncode if not is_running and process else None,
                'log_file': self.qq_server.log_file_path,
                'is_stopping': self.qq_server.server_stopping
            }
            
            return status
        except Exception as e:
            self.logger.error(f"获取服务器状态失败: {e}")
            return {'is_running': False, 'error': str(e)}
    
    def is_server_running(self) -> bool:
        """
        快速检查服务器是否运行
        
        Returns:
            服务器是否正在运行
        """
        status = self.get_server_status()
        return status.get('is_running', False)