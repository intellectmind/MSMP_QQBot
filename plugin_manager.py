import os
import sys
import logging
import importlib.util
import asyncio
import inspect
import json
import hashlib
from pathlib import Path
from typing import Dict, List, Any, Optional, Callable, Set
from abc import ABC, abstractmethod


class BotPlugin(ABC):
    """插件基类 - 所有插件必须继承此类"""
    
    # 插件元数据
    name: str = "Unknown Plugin"
    version: str = "2.0.0"
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
        self.loaded_file_mtimes: Dict[str, float] = {}
        self.plugin_file_paths: Dict[str, Path] = {}
        self.plugin_dependencies: Dict[str, List[str]] = {}
        self.event_listener_timeout = 10.0
        self._server_log_cursors: Dict[Any, Dict[str, Any]] = {}
        
        self.plugin_dir.mkdir(exist_ok=True)
        if str(self.plugin_dir.absolute()) not in sys.path:
            sys.path.insert(0, str(self.plugin_dir.absolute()))
        
        self.logger.info(f"插件管理器已初始化, 插件目录: {self.plugin_dir.absolute()}")

    def _runtime_for_target_server(self, target_server: Optional[Dict[str, Any]] = None):
        if not self.qq_server:
            return None
        if target_server and hasattr(self.qq_server, '_runtime_for_server'):
            return self.qq_server._runtime_for_server(target_server)
        if hasattr(self.qq_server, '_runtime_for_key'):
            runtime = self.qq_server._runtime_for_key(getattr(self.qq_server, 'active_server_key', ''))
            if runtime:
                return runtime
        runtimes = getattr(self.qq_server, 'server_runtimes', {}) or {}
        running = [item for item in runtimes.values() if item.process and item.process.poll() is None]
        return running[-1] if running else None

    def get_running_server_configs(self) -> List[Dict[str, Any]]:
        """获取可轮询日志的服务器配置，包含 Bot 托管运行和外部 latest.log 日志源。"""
        if not self.qq_server:
            return []
        servers: List[Dict[str, Any]] = []
        seen_keys: Set[str] = set()
        if hasattr(self.qq_server, 'get_running_server_configs'):
            for server in self.qq_server.get_running_server_configs() or []:
                key = self.get_server_key(server)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                servers.append(dict(server or {}))
        else:
            runtime = self._runtime_for_target_server()
            if runtime and runtime.process and runtime.process.poll() is None:
                key = self.get_server_key(runtime.config)
                seen_keys.add(key)
                servers.append(dict(runtime.config or {}))

        for server in self.get_configured_servers():
            key = self.get_server_key(server)
            if key in seen_keys:
                continue
            latest_log = self._server_latest_log_path(server)
            if latest_log and latest_log.exists() and latest_log.is_file():
                seen_keys.add(key)
                servers.append(dict(server or {}))
        return servers
    
    async def load_plugins(self):
        """扫描并加载所有插件（包括子目录）"""
        if not self.plugin_dir.exists():
            self.logger.warning(f"插件目录不存在: {self.plugin_dir}")
            return
        
        plugin_files = self._discover_plugin_files()
        
        if not plugin_files:
            self.logger.info("未发现任何插件")
            return
        
        self.logger.info(f"发现 {len(plugin_files)} 个插件文件")
        
        pending = list(plugin_files)
        while pending:
            next_pending = []
            loaded_this_round = 0
            for plugin_file in pending:
                if await self._load_plugin_file(plugin_file):
                    loaded_this_round += 1
                else:
                    next_pending.append(plugin_file)

            if not next_pending:
                break

            if loaded_this_round == 0:
                skipped = ", ".join(str(path.relative_to(self.plugin_dir)) for path in next_pending)
                self.logger.error(f"以下插件加载失败或依赖无法满足，已跳过: {skipped}")
                break

            pending = next_pending

    def _discover_plugin_files(self) -> List[Path]:
        """扫描插件文件；同名根插件和子目录插件同时存在时优先加载子目录版本。"""
        plugin_files = []
        for plugin_file in self.plugin_dir.rglob("*.py"):
            if plugin_file.name.startswith("_") or plugin_file.name == "__init__.py":
                continue
            if plugin_file.parent == self.plugin_dir:
                package_entry = self.plugin_dir / plugin_file.stem / f"{plugin_file.stem}.py"
                if package_entry.exists():
                    self.logger.warning(
                        "跳过重复根插件 %s，优先加载子目录插件 %s",
                        plugin_file,
                        package_entry
                    )
                    continue
            plugin_files.append(plugin_file)
        return sorted(plugin_files)

    def _module_name_for_file(self, plugin_file: Path) -> str:
        """把插件文件路径转换为稳定模块名。"""
        relative_path = plugin_file.relative_to(self.plugin_dir)
        return str(relative_path).replace('.py', '').replace(os.sep, '.')

    def get_available_plugin_files(self) -> Dict[str, Path]:
        """获取可加载插件文件，已应用和自动加载一致的去重规则。"""
        return {
            self._module_name_for_file(plugin_file): plugin_file
            for plugin_file in self._discover_plugin_files()
        }

    def find_available_plugin_file(self, search_name: str) -> Optional[str]:
        """按模块名、文件名或插件显示名查找可加载插件模块名。"""
        normalized = str(search_name or '').strip()
        if normalized.endswith('.py'):
            normalized = normalized[:-3]
        search_lower = normalized.lower()
        if not search_lower:
            return None

        available = self.get_available_plugin_files()
        for module_name, plugin_file in available.items():
            if search_lower in (module_name.lower(), plugin_file.stem.lower()):
                return module_name

        for module_name, plugin_file in available.items():
            try:
                spec = importlib.util.spec_from_file_location(module_name, plugin_file)
                if not spec or not spec.loader:
                    continue
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                plugin_class = self._find_plugin_class(module)
                if not plugin_class:
                    continue
                display_name = getattr(plugin_class, 'name', '') or ''
                if search_lower in display_name.lower() or search_lower in module_name.lower():
                    return module_name
            except Exception as e:
                self.logger.debug(f"探测插件 {module_name} 显示名失败: {e}")
        return None
    
    async def _load_plugin_file(self, plugin_file: Path) -> bool:
        """加载单个插件文件"""
        module_name = None
        try:
            # 生成模块名：将文件路径转换为模块路径
            # 例如: plugins/whitelist_audit/whitelist_audit.py -> whitelist_audit.whitelist_audit
            module_name = self._module_name_for_file(plugin_file)
            
            self.logger.info(f"正在加载插件: {module_name} (文件: {plugin_file})")

            if module_name in self.plugins:
                self.logger.info(f"插件 {module_name} 已加载，重新加载前先卸载旧实例")
                await self.unload_plugin(module_name)
            
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
                await self._unload_plugin_module(module_name)
                return False
            
            # 实例化插件
            plugin_instance = plugin_class(self.logger.getChild(f"plugin.{module_name}"))
            
            # 检查依赖关系
            if hasattr(plugin_instance, 'dependencies') and plugin_instance.dependencies:
                missing_deps = self._check_dependencies(plugin_instance.dependencies)
                if missing_deps:
                    self.logger.warning(
                        f"插件 {module_name} 等待依赖: {', '.join(missing_deps)}"
                    )
                    await self._unload_plugin_module(module_name)
                    return False
            
            # 调用插件的 on_load 方法
            success = await asyncio.wait_for(
                plugin_instance.on_load(self),
                timeout=self.event_listener_timeout
            )
            
            if success:
                self.plugins[module_name] = plugin_instance
                self.plugin_modules[module_name] = module
                self.plugin_file_paths[module_name] = plugin_file
                file_path = str(plugin_file.absolute())
                self.loaded_files.add(file_path)
                self.loaded_file_mtimes[file_path] = plugin_file.stat().st_mtime
                self.logger.info(
                    f"插件加载成功: {plugin_instance.name} v{plugin_instance.version} "
                    f"(作者: {plugin_instance.author})"
                )
                return True
            else:
                self.logger.error(f"插件加载失败: {module_name}")
                await self._cleanup_failed_plugin_load(module_name, plugin_instance)
                await self._unload_plugin_module(module_name)
                return False
        
        except Exception as e:
            self.logger.error(f"加载插件 {plugin_file.name} 时出错: {e}", exc_info=True)
            if module_name:
                try:
                    failed_instance = locals().get('plugin_instance')
                    await self._cleanup_failed_plugin_load(module_name, failed_instance)
                    await self._unload_plugin_module(module_name)
                except Exception:
                    pass
            return False

    async def _cleanup_failed_plugin_load(self, module_name: str, plugin_instance: Optional[BotPlugin] = None):
        """清理 on_load 失败时已经注册的命令、事件和运行时资源。"""
        if plugin_instance:
            try:
                await asyncio.wait_for(
                    plugin_instance.on_unload(),
                    timeout=self.event_listener_timeout
                )
            except Exception as e:
                self.logger.debug(f"清理加载失败插件 {module_name} 的 on_unload 失败: {e}")
        self._cleanup_plugin_handlers(module_name)
    
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

    def _get_dependent_plugins(self, plugin_name: str, recursive: bool = False) -> List[str]:
        """获取依赖指定插件的已加载插件，递归模式按依赖链由近到远排序。"""
        direct_dependents = []
        for loaded_name, plugin in self.plugins.items():
            if loaded_name == plugin_name:
                continue
            dependencies = getattr(plugin, 'dependencies', []) or []
            if plugin_name in dependencies:
                direct_dependents.append(loaded_name)

        if not recursive:
            return direct_dependents

        ordered = []
        visited = set()

        def visit(name: str):
            for dependent in self._get_dependent_plugins(name, recursive=False):
                if dependent in visited:
                    continue
                visited.add(dependent)
                ordered.append(dependent)
                visit(dependent)

        visit(plugin_name)
        return ordered

    def _get_unload_order(self) -> List[str]:
        """获取全量卸载顺序，保证依赖方先于被依赖方卸载。"""
        ordered = []
        visited = set()

        def visit(plugin_name: str):
            if plugin_name in visited:
                return
            visited.add(plugin_name)
            for dependent in self._get_dependent_plugins(plugin_name, recursive=False):
                visit(dependent)
            ordered.append(plugin_name)

        for plugin_name in list(self.plugins.keys()):
            visit(plugin_name)
        return ordered
    
    async def unload_plugins(self):
        """卸载所有插件"""
        for module_name in self._get_unload_order():
            plugin = self.plugins.get(module_name)
            if not plugin:
                continue
            try:
                await asyncio.wait_for(
                    plugin.on_unload(),
                    timeout=self.event_listener_timeout
                )
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
        self.loaded_file_mtimes.clear()
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
                    await asyncio.wait_for(
                        plugin_instance.on_reload(),
                        timeout=self.event_listener_timeout
                    )
                    self.logger.debug(f"已调用插件的 on_reload 方法: {plugin_name}")
                except Exception as e:
                    self.logger.warning(f"调用插件 on_reload 方法失败: {e}")
            
            dependent_plugins = self._get_dependent_plugins(plugin_name, recursive=True)
            dependent_files = {
                dependent: self.plugin_file_paths.get(dependent)
                for dependent in dependent_plugins
            }
            for dependent in reversed(dependent_plugins):
                self.logger.info(f"插件 {dependent} 依赖 {plugin_name}，重载前先卸载依赖插件")
                await self.unload_plugin(dependent, unload_dependents=False)

            # 先卸载插件
            await self.unload_plugin(plugin_name, unload_dependents=False)
            
            # 等待一段时间确保完全卸载
            await asyncio.sleep(0.1)
            
            # 重新加载插件
            success = await self._load_plugin_file(plugin_file)
            
            if success:
                self.logger.info(f"插件重新加载成功: {plugin_name}")
                for dependent in dependent_plugins:
                    dependent_file = dependent_files.get(dependent)
                    if not dependent_file or not dependent_file.exists():
                        self.logger.warning(f"依赖插件文件缺失，跳过重载: {dependent}")
                        continue
                    if await self._load_plugin_file(dependent_file):
                        self.logger.info(f"依赖插件重载成功: {dependent}")
                    else:
                        self.logger.error(f"依赖插件重载失败: {dependent}")
                return True
            else:
                self.logger.error(f"插件重新加载失败: {plugin_name}")
                return False
                
        except Exception as e:
            self.logger.error(f"重新加载插件 {plugin_name} 时出错: {e}", exc_info=True)
            return False
    
    async def unload_plugin(self, plugin_name: str, unload_dependents: bool = True) -> bool:
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

            if unload_dependents:
                for dependent in self._get_dependent_plugins(plugin_name):
                    self.logger.info(f"插件 {dependent} 依赖 {plugin_name}，先卸载依赖插件")
                    await self.unload_plugin(dependent, unload_dependents=True)
            
            plugin_instance = self.plugins[plugin_name]
            
            unload_success = True
            try:
                await asyncio.wait_for(
                    plugin_instance.on_unload(),
                    timeout=self.event_listener_timeout
                )
            except Exception as e:
                unload_success = False
                self.logger.error(f"插件 {plugin_name} on_unload 执行失败，继续清理引用: {e}", exc_info=True)
            
            # 清理插件注册的命令和事件监听器
            self._cleanup_plugin_handlers(plugin_name)
            
            # 移除插件引用
            del self.plugins[plugin_name]
            
            # 卸载模块
            await self._unload_plugin_module(plugin_name)
            
            # 从已加载文件列表中移除
            if plugin_name in self.plugin_file_paths:
                file_path = str(self.plugin_file_paths[plugin_name].absolute())
                self.loaded_files.discard(file_path)
                self.loaded_file_mtimes.pop(file_path, None)
                del self.plugin_file_paths[plugin_name]
            
            self.logger.info(f"插件已卸载: {plugin_name}")
            return unload_success
            
        except Exception as e:
            self.logger.error(f"卸载插件 {plugin_name} 时出错: {e}", exc_info=True)
            return False
    
    def _cleanup_plugin_handlers(self, plugin_name: str):
        """清理插件注册的命令和事件监听器"""
        # 清理命令处理器
        commands_to_remove = []
        for cmd_name, handler_info in self.command_handlers.items():
            handler_func = handler_info.get('handler')
            handler_module = self._callable_module(handler_func)
            if handler_module.startswith(plugin_name):
                commands_to_remove.append(cmd_name)
        
        for cmd_name in commands_to_remove:
            del self.command_handlers[cmd_name]
            self.logger.debug(f"已清理插件命令: {cmd_name}")
        
        # 清理事件监听器
        for event_name, listeners in list(self.event_listeners.items()):
            listeners_to_remove = []
            for listener in listeners:
                listener_module = self._callable_module(listener)
                if listener_module.startswith(plugin_name):
                    listeners_to_remove.append(listener)
            
            for listener in listeners_to_remove:
                listeners.remove(listener)
                self.logger.debug(f"已清理插件事件监听器: {event_name}")
            
            if not listeners:
                del self.event_listeners[event_name]

    @staticmethod
    def _callable_module(callback: Callable) -> str:
        """兼容函数、绑定方法和可调用对象，获取其定义模块。"""
        if not callback:
            return ""
        func = getattr(callback, "__func__", callback)
        module = getattr(func, "__module__", "")
        if module:
            return str(module)
        owner = getattr(callback, "__self__", None)
        if owner:
            return str(getattr(owner.__class__, "__module__", ""))
        return str(getattr(callback.__class__, "__module__", ""))
    
    async def load_plugin(self, plugin_name: str) -> bool:
        """
        加载指定插件
        
        Args:
            plugin_name: 插件名称（模块名，如 whitelist_audit.whitelist_audit）
            
        Returns:
            bool: 加载是否成功
        """
        try:
            if plugin_name in self.plugins:
                return await self.reload_plugin(plugin_name)

            # 查找对应的插件文件
            plugin_file = self.get_available_plugin_files().get(plugin_name)
            
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
        
        available_files = self.get_available_plugin_files()
        available_paths = {str(path.absolute()) for path in available_files.values()}

        for plugin_name, plugin_file in available_files.items():
            file_path = str(plugin_file.absolute())
            current_mtime = plugin_file.stat().st_mtime
            
            # 检查文件是否已加载且是否发生变化
            if file_path in self.loaded_files:
                if current_mtime > self.loaded_file_mtimes.get(file_path, 0):
                    results[plugin_name] = await self.reload_plugin(plugin_name)
            else:
                # 新插件，加载它
                results[plugin_name] = await self.load_plugin(plugin_name)

        for plugin_name, plugin_file in list(self.plugin_file_paths.items()):
            file_path = str(plugin_file.absolute())
            if file_path not in available_paths:
                self.logger.info(f"插件文件已删除或被去重规则排除，卸载插件: {plugin_name}")
                results[plugin_name] = await self.unload_plugin(plugin_name)
        
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

        normalized_names = {str(name).lower() for name in names}
        if command_name in self.command_handlers:
            existing = self.command_handlers[command_name]
            self.logger.error(
                "插件命令注册冲突: %s 已由 %s 注册，跳过新的注册",
                command_name,
                self._callable_module(existing.get("handler")) or "unknown"
            )
            return

        for existing_name, existing in self.command_handlers.items():
            existing_aliases = {str(name).lower() for name in existing.get("names", [])}
            conflict_aliases = normalized_names & existing_aliases
            if conflict_aliases:
                self.logger.error(
                    "插件命令别名冲突: %s 与已注册命令 %s 冲突，跳过 %s",
                    ", ".join(sorted(conflict_aliases)),
                    existing_name,
                    command_name
                )
                return
        
        self.command_handlers[command_name] = {
            "handler": handler,
            "names": names,
            "normalized_names": normalized_names,
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

        if listener in self.event_listeners[event_name]:
            self.logger.debug(f"事件监听器已存在，跳过重复注册: {event_name}")
            return
        
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
        
        target_server = kwargs.get("target_server")
        listener_tasks = []
        for listener in list(self.event_listeners[event_name]):
            if not self.is_callable_enabled_for_server(listener, target_server):
                continue
            listener_tasks.append(self._trigger_event_listener(event_name, listener, args, kwargs))
        if listener_tasks:
            await asyncio.gather(*listener_tasks)

    async def _trigger_event_listener(self, event_name: str, listener: Callable, args: tuple, kwargs: Dict[str, Any]):
        """触发单个事件监听器，隔离超时和异常。"""
        try:
            if not callable(listener):
                return

            call_args = self._event_args_for_listener(listener, args, kwargs)
            call_kwargs = self._event_kwargs_for_listener(listener, kwargs)
            if inspect.iscoroutinefunction(listener):
                await asyncio.wait_for(
                    listener(*call_args, **call_kwargs),
                    timeout=self.event_listener_timeout
                )
            else:
                await asyncio.wait_for(
                    asyncio.to_thread(listener, *call_args, **call_kwargs),
                    timeout=self.event_listener_timeout
                )
        except asyncio.TimeoutError:
            self.logger.error(
                f"触发事件 {event_name} 超时: {getattr(listener, '__name__', repr(listener))}"
            )
        except Exception as e:
            self.logger.error(f"触发事件 {event_name} 时出错: {e}", exc_info=True)

    def _event_args_for_listener(self, listener: Callable, args: tuple, kwargs: Dict[str, Any]) -> tuple:
        """按监听器签名截断事件位置参数，避免旧插件因新增事件参数报错。"""
        if not args:
            return ()
        try:
            signature = inspect.signature(listener)
            parameters = list(signature.parameters.values())
            if any(param.kind == inspect.Parameter.VAR_POSITIONAL for param in parameters):
                return args
            positional_capacity = sum(
                1
                for param in parameters
                if param.kind in (
                    inspect.Parameter.POSITIONAL_ONLY,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD
                ) and param.name not in kwargs
            )
            return args[:positional_capacity]
        except (TypeError, ValueError):
            return ()

    def _event_kwargs_for_listener(self, listener: Callable, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        """按监听器签名过滤事件 kwargs，保持旧插件兼容。"""
        if not kwargs:
            return {}
        try:
            signature = inspect.signature(listener)
            parameters = signature.parameters.values()
            if any(param.kind == inspect.Parameter.VAR_KEYWORD for param in parameters):
                return kwargs
            accepted = {
                name
                for name, param in signature.parameters.items()
                if param.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
            }
            return {key: value for key, value in kwargs.items() if key in accepted}
        except (TypeError, ValueError):
            return {}
    
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

    # ============ 多服务器插件存储接口 ============

    def get_active_server_config(self) -> Dict[str, Any]:
        """获取当前活跃服务器配置。"""
        if self.qq_server:
            return getattr(self.qq_server, 'active_server_config', None) or {}
        return {}

    def get_configured_servers(self) -> List[Dict[str, Any]]:
        """获取全部服务器配置，供插件按服务器拆分数据文件。"""
        config_manager = getattr(self.qq_server, 'config_manager', None) if self.qq_server else None
        if config_manager and hasattr(config_manager, 'get_servers'):
            try:
                servers = config_manager.get_servers()
                return list(servers) if isinstance(servers, list) else []
            except Exception as e:
                self.logger.debug(f"获取服务器配置列表失败: {e}")
        active = self.get_active_server_config()
        return [active] if active else []

    def get_server_key(self, target_server: Optional[Dict[str, Any]] = None) -> str:
        """获取服务器稳定标识，优先使用独立配置文件路径。"""
        server = target_server or self.get_active_server_config()
        return str((server or {}).get('_config_file') or (server or {}).get('name') or 'default')

    def get_server_by_key(self, server_key: str) -> Dict[str, Any]:
        """按服务器标识查找服务器配置。"""
        for server in self.get_configured_servers():
            if self.get_server_key(server) == str(server_key):
                return server
        active = self.get_active_server_config()
        if active and self.get_server_key(active) == str(server_key):
            return active
        return {}

    def get_plugin_storage_name(self, plugin_name: str) -> str:
        """将插件模块名转换为插件存储目录名。"""
        raw = str(plugin_name or '').strip()
        if raw.endswith('.py'):
            raw = raw[:-3]
        if '.' in raw:
            raw = raw.split('.', 1)[0]
        return self._safe_path_name(raw or 'unknown_plugin')

    def get_plugin_server_dir(self, plugin_name: str, target_server: Optional[Dict[str, Any]] = None,
                              create: bool = True) -> Path:
        """返回插件在目标服务器下的独立目录。"""
        plugin_dir = self.plugin_dir / self.get_plugin_storage_name(plugin_name)
        server_dir = plugin_dir / "servers" / self._safe_path_name(self.get_server_key(target_server))
        if create:
            server_dir.mkdir(parents=True, exist_ok=True)
        return server_dir

    def get_plugin_server_file(self, plugin_name: str, filename: str,
                               target_server: Optional[Dict[str, Any]] = None,
                               create_parent: bool = True) -> Path:
        """返回插件在目标服务器下的独立文件路径。"""
        return self.get_plugin_server_dir(plugin_name, target_server, create_parent) / filename

    def get_plugin_server_dir_by_key(self, plugin_name: str, server_key: str,
                                     create: bool = True) -> Path:
        """按服务器标识返回插件独立目录。"""
        plugin_dir = self.plugin_dir / self.get_plugin_storage_name(plugin_name)
        server_dir = plugin_dir / "servers" / self._safe_path_name(server_key)
        if create:
            server_dir.mkdir(parents=True, exist_ok=True)
        return server_dir

    def get_plugin_server_file_by_key(self, plugin_name: str, server_key: str, filename: str,
                                      create_parent: bool = True) -> Path:
        """按服务器标识返回插件独立文件路径。"""
        return self.get_plugin_server_dir_by_key(plugin_name, server_key, create_parent) / filename

    def is_callable_enabled_for_server(self, callback: Callable, target_server: Optional[Dict[str, Any]] = None) -> bool:
        """判断回调所属插件在目标服务器是否启用。"""
        plugin_name = self.get_plugin_name_for_callable(callback)
        if not plugin_name:
            return True
        return self.is_plugin_enabled_for_server(plugin_name, target_server)

    def get_plugin_name_for_callable(self, callback: Callable) -> str:
        """根据回调模块名反查已加载插件名。"""
        module_name = self._callable_module(callback)
        if not module_name:
            return ""
        for plugin_name in self.plugins.keys():
            if module_name == plugin_name or module_name.startswith(f"{plugin_name}."):
                return plugin_name
            storage_name = self.get_plugin_storage_name(plugin_name)
            if module_name == storage_name or module_name.startswith(f"{storage_name}."):
                return plugin_name
        return ""

    def is_plugin_enabled_for_server(self, plugin_name: str, target_server: Optional[Dict[str, Any]] = None) -> bool:
        """读取服务器独立 config.json 的 enabled 开关；未配置时默认启用。"""
        try:
            path = self.get_plugin_server_file(plugin_name, "config.json", target_server, create_parent=False)
            if not path.exists():
                return True
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("enabled") is False:
                return False
        except Exception as exc:
            self.logger.warning(f"读取插件服务器启用状态失败 {plugin_name}: {exc}")
        return True

    def set_plugin_enabled_for_server(
        self,
        plugin_name: str,
        target_server: Optional[Dict[str, Any]],
        enabled: bool
    ) -> Path:
        """写入服务器独立插件启用开关，保留该服务器已有插件配置。"""
        path = self.get_plugin_server_file(plugin_name, "config.json", target_server, create_parent=True)
        data: Dict[str, Any] = {}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
            except Exception as exc:
                self.logger.warning(f"读取服务器插件配置失败，将重建 {path}: {exc}")
        data["enabled"] = bool(enabled)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def _safe_path_name(self, value: str) -> str:
        """把服务器标识转换为 Windows/Linux 都安全的短目录名。"""
        text = str(value or 'default').replace('\\', '/').strip().strip('/')
        text = text or 'default'
        safe = ''.join(char if char.isalnum() or char in ('-', '_', '.') else '_' for char in text)
        safe = safe.strip('._') or 'default'
        if safe != text or len(safe) > 80:
            digest = hashlib.sha1(text.encode('utf-8')).hexdigest()[:8]
            safe = safe[:70].strip('._') or 'default'
            return f"{safe}_{digest}"
        return safe
    
    async def reload_config(self, old_config: Dict, new_config: Dict):
        """通知所有插件配置已重新加载"""
        for plugin in list(self.plugins.values()):
            try:
                await asyncio.wait_for(
                    plugin.on_config_reload(old_config, new_config),
                    timeout=self.event_listener_timeout
                )
            except asyncio.TimeoutError:
                self.logger.error(f"插件配置重新加载超时: {plugin.name}")
            except Exception as e:
                self.logger.error(f"插件配置重新加载失败: {e}", exc_info=True)
    
    def _read_server_latest_log_tail(
        self,
        lines: int = 50,
        target_server: Optional[Dict[str, Any]] = None
    ) -> List[str]:
        """读取外部启动服务器的 logs/latest.log 尾部日志。"""
        try:
            latest_log = self._server_latest_log_path(target_server)
            if not latest_log.exists() or not latest_log.is_file():
                return []

            return self._tail_text_file(latest_log, max(1, int(lines or 50)))
        except Exception as e:
            self.logger.debug(f"读取 latest.log 失败: {e}")
            return []

    def _server_latest_log_path(self, target_server: Optional[Dict[str, Any]] = None) -> Path:
        config_manager = getattr(self.qq_server, 'config_manager', None) if self.qq_server else None
        working_dir = ""
        if config_manager and hasattr(config_manager, 'get_server_working_directory'):
            working_dir = config_manager.get_server_working_directory(target_server)
        if not working_dir:
            server = target_server or {}
            server_section = server.get('server') if isinstance(server.get('server'), dict) else {}
            working_dir = str(
                server_section.get('working_directory') or server.get('working_directory') or ''
            ).strip().replace('\\', os.sep).replace('/', os.sep)
            if not working_dir:
                start_script = str(
                    server_section.get('start_script') or server.get('start_script') or ''
                ).strip().replace('\\', os.sep).replace('/', os.sep)
                working_dir = os.path.dirname(start_script) if start_script else ''
        return Path(working_dir) / "logs" / "latest.log" if working_dir else Path()

    def _tail_text_file(self, path: Path, lines: int, block_size: int = 8192) -> List[str]:
        """从文本文件尾部读取指定行数，避免大日志全量读入内存。"""
        chunks = []
        newline_count = 0
        with path.open('rb') as f:
            f.seek(0, os.SEEK_END)
            position = f.tell()
            while position > 0 and newline_count <= lines:
                read_size = min(block_size, position)
                position -= read_size
                f.seek(position)
                chunk = f.read(read_size)
                chunks.append(chunk)
                newline_count += chunk.count(b'\n')

        data = b''.join(reversed(chunks))
        return data.decode('utf-8', errors='replace').splitlines()[-lines:]

    def get_incremental_server_logs(
        self,
        consumer_id: str,
        max_lines: int = 1000,
        target_server: Optional[Dict[str, Any]] = None
    ) -> List[str]:
        """按消费者和服务器返回新增日志；首次调用只建立基线，不回放旧日志。"""
        if not self.qq_server:
            return []
        cursor_key = (str(consumer_id or "default"), self.get_server_key(target_server))
        try:
            process = (
                self.qq_server.get_server_process(target_server)
                if hasattr(self.qq_server, 'get_server_process')
                else getattr(self.qq_server, 'server_process', None)
            )
            if not process or process.poll() is not None:
                return self._read_incremental_latest_log(cursor_key, max_lines, target_server)

            logs = self.qq_server.get_recent_logs(max(1, int(max_lines or 1000)), target_server)
            return self._slice_incremental_memory_logs(cursor_key, logs, max_lines)
        except Exception as e:
            self.logger.error(f"获取增量服务端日志失败: {e}")
            return []

    def _slice_incremental_memory_logs(self, cursor_key, logs, max_lines: int) -> List[str]:
        if not isinstance(logs, list) or not logs:
            return []
        logs = [line for line in logs if isinstance(line, str)]
        if not logs:
            return []

        cursor = self._server_log_cursors.get(cursor_key)
        last_hash = cursor.get("last_hash") if cursor and cursor.get("source") == "memory" else ""
        latest_hash = self._log_cursor_hash(logs[-1])
        if not last_hash:
            self._server_log_cursors[cursor_key] = {"source": "memory", "last_hash": latest_hash}
            return []

        start = 0
        for index in range(len(logs) - 1, -1, -1):
            if self._log_cursor_hash(logs[index]) == last_hash:
                start = index + 1
                break
        new_logs = logs[start:]
        self._server_log_cursors[cursor_key] = {"source": "memory", "last_hash": latest_hash}
        return new_logs[-max(1, int(max_lines or 1000)):]

    def _read_incremental_latest_log(self, cursor_key, max_lines: int,
                                     target_server: Optional[Dict[str, Any]]) -> List[str]:
        latest_log = self._server_latest_log_path(target_server)
        if not latest_log.exists() or not latest_log.is_file():
            self._server_log_cursors.pop(cursor_key, None)
            return []

        stat = latest_log.stat()
        path_text = str(latest_log.resolve())
        cursor = self._server_log_cursors.get(cursor_key)
        if not cursor or cursor.get("source") != "file" or cursor.get("path") != path_text:
            self._server_log_cursors[cursor_key] = {
                "source": "file",
                "path": path_text,
                "offset": stat.st_size,
                "mtime": stat.st_mtime,
            }
            return []

        offset = int(cursor.get("offset") or 0)
        if stat.st_size < offset:
            offset = 0
        if stat.st_size == offset:
            return []

        with latest_log.open('rb') as handle:
            handle.seek(offset)
            data = handle.read()
            new_offset = handle.tell()
        self._server_log_cursors[cursor_key] = {
            "source": "file",
            "path": path_text,
            "offset": new_offset,
            "mtime": stat.st_mtime,
        }
        return data.decode('utf-8', errors='replace').splitlines()[-max(1, int(max_lines or 1000)):]

    def _log_cursor_hash(self, line: str) -> str:
        return hashlib.sha1(str(line or "").encode("utf-8", errors="replace")).hexdigest()

    def get_server_logs(self, lines: int = 50, target_server: Optional[Dict[str, Any]] = None) -> List[str]:
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
            process = (
                self.qq_server.get_server_process(target_server)
                if hasattr(self.qq_server, 'get_server_process')
                else getattr(self.qq_server, 'server_process', None)
            )
            # 检查服务器是否在运行
            if not process:
                self.logger.debug("服务器进程未运行")
                return self._read_server_latest_log_tail(lines, target_server)
                
            # 检查进程状态
            if process.poll() is not None:
                self.logger.debug("服务器进程已停止")
                return self._read_server_latest_log_tail(lines, target_server)
                
            return self.qq_server.get_recent_logs(lines, target_server)
        except Exception as e:
            self.logger.error(f"获取服务端日志失败: {e}")
            return []
    
    def get_latest_server_log(self, target_server: Optional[Dict[str, Any]] = None) -> Optional[str]:
        """
        获取最新的MC服务端日志行
        
        Returns:
            最新的日志行，如果无日志返回None
            
        示例:
            latest = plugin_manager.get_latest_server_log()
            if latest:
                print(f"最新日志: {latest}")
        """
        logs = self.get_server_logs(1, target_server)
        return logs[0] if logs else None
    
    def search_server_logs(self, keyword: str, lines: int = 100, target_server: Optional[Dict[str, Any]] = None) -> List[str]:
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
            all_logs = self.get_server_logs(lines, target_server)
            keyword_lower = keyword.lower()
            return [log for log in all_logs if keyword_lower in log.lower()]
        except Exception as e:
            self.logger.error(f"搜索服务端日志失败: {e}")
            return []
    
    def get_server_logs_info(self, target_server: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
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
            runtime = self._runtime_for_target_server(target_server)
            if target_server and not runtime:
                logs = []
                process = None
            else:
                logs = runtime.logs if runtime else self.qq_server.server_logs
                process = runtime.process if runtime else getattr(self.qq_server, 'server_process', None)
            current_lines = len(logs)
            max_lines = getattr(logs, 'maxlen', None) or 100
            usage_percent = (current_lines / max_lines * 100) if max_lines > 0 else 0
            
            # 估算内存使用
            memory_usage_kb = current_lines * 0.15  # 每行约150字节
            
            return {
                'current_lines': current_lines,
                'max_lines': max_lines,
                'usage_percent': usage_percent,
                'memory_usage_kb': memory_usage_kb,
                'status': 'running' if process and process.poll() is None else 'stopped'
            }
        except Exception as e:
            self.logger.error(f"获取日志信息失败: {e}")
            return {}
    
    def clear_server_logs(self, target_server: Optional[Dict[str, Any]] = None) -> bool:
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
            runtime = self._runtime_for_target_server(target_server)
            if target_server and not runtime:
                self.logger.debug("目标服务器没有运行时日志缓冲区，跳过清空")
                return False
            logs = runtime.logs if runtime else self.qq_server.server_logs
            logs.clear()
            self.logger.info("服务端日志缓冲区已清空")
            return True
        except Exception as e:
            self.logger.error(f"清空日志失败: {e}")
            return False

    def get_server_status(self, target_server: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
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
            runtime = self._runtime_for_target_server(target_server)
            if target_server and hasattr(self.qq_server, 'get_server_process'):
                process = self.qq_server.get_server_process(target_server)
            else:
                process = runtime.process if runtime else self.qq_server.server_process
            is_running = bool(process and process.poll() is None)
            
            status = {
                'is_running': is_running,
                'pid': process.pid if is_running else None,
                'return_code': process.returncode if not is_running and process else None,
                'log_file': runtime.log_file_path if runtime else (
                    None if target_server else self.qq_server.log_file_path
                ),
                'is_stopping': runtime.stopping if runtime else (
                    False if target_server else self.qq_server.server_stopping
                )
            }
            
            return status
        except Exception as e:
            self.logger.error(f"获取服务器状态失败: {e}")
            return {'is_running': False, 'error': str(e)}
    
    def is_server_running(self, target_server: Optional[Dict[str, Any]] = None) -> bool:
        """
        快速检查服务器是否运行
        
        Returns:
            服务器是否正在运行
        """
        status = self.get_server_status(target_server)
        return status.get('is_running', False)
