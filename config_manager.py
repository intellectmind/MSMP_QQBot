import yaml
import os
import re
import threading
import time
import logging
import hashlib
import copy
from typing import List, Dict, Optional, Callable, Any
from pathlib import Path

class ConfigValidationError(Exception):
    """配置验证错误"""
    pass

class ConfigManager:
    SERVER_REQUIRED_SECTIONS = (
        'msmp',
        'rcon',
        'qq',
        'server',
        'commands',
        'notifications',
        'advanced',
        'scheduled_tasks',
        'custom_commands',
        'custom_listeners',
    )

    def __init__(self, config_path: str = "config.yml"):
        self.config_path = config_path
        self.config = {}
        self.load_config()
        
        # 验证配置
        errors = self.validate_config()
        if errors:
            raise ConfigValidationError("配置验证失败:\n" + "\n".join(f"  - {e}" for e in errors))
        
        # 配置热重载相关
        self._reload_callbacks: List[Callable] = []
        self._file_monitor_thread: Optional[threading.Thread] = None
        self._stop_monitor = False
        self._last_config_hash = self._get_config_hash()
        self._reload_lock = threading.RLock()
        self._event_loop = None
        self.logger = logging.getLogger(__name__)

    def set_event_loop(self, loop):
        """设置用于异步配置重载回调的主事件循环。"""
        self._event_loop = loop
    
    def _get_config_hash(self) -> str:
        """获取配置文件的哈希值，用于检测文件变化"""
        digest = hashlib.md5()
        try:
            with open(self.config_path, 'rb') as f:
                digest.update(f.read())
        except Exception:
            return ""

        try:
            server_dir = self.get_server_config_dir()
            if server_dir.exists():
                for path in sorted([*server_dir.glob("*.yml"), *server_dir.glob("*.yaml")]):
                    digest.update(str(path).encode('utf-8'))
                    digest.update(path.read_bytes())
        except Exception:
            pass

        return digest.hexdigest()
    
    def register_reload_callback(self, callback: Callable):
        """注册配置重载回调函数
        
        Args:
            callback: 可以是同步或异步函数，签名: func(old_config: dict, new_config: dict)
        """
        if callback not in self._reload_callbacks:
            self._reload_callbacks.append(callback)
            self.logger.debug(f"已注册配置重载回调: {callback.__name__}")
    
    def unregister_reload_callback(self, callback: Callable):
        """注销配置重载回调函数"""
        if callback in self._reload_callbacks:
            self._reload_callbacks.remove(callback)
            self.logger.debug(f"已注销配置重载回调: {callback.__name__}")
    
    def start_file_monitor(self, check_interval: int = 2):
        """启动配置文件监控线程
        
        Args:
            check_interval: 检查间隔（秒），默认2秒
        """
        if self._file_monitor_thread and self._file_monitor_thread.is_alive():
            self.logger.warning("配置监控线程已在运行")
            return
        
        self._stop_monitor = False
        self._file_monitor_thread = threading.Thread(
            target=self._monitor_config_file,
            args=(check_interval,),
            daemon=True,
            name="ConfigMonitor"
        )
        self._file_monitor_thread.start()
        self.logger.info(f"配置文件监控已启动 (检查间隔: {check_interval}秒)")
    
    def stop_file_monitor(self):
        """停止配置文件监控"""
        self._stop_monitor = True
        if self._file_monitor_thread and self._file_monitor_thread.is_alive():
            self._file_monitor_thread.join(timeout=5)
            self.logger.info("配置文件监控已停止")
    
    def _monitor_config_file(self, check_interval: int):
        """监控配置文件变化的线程函数"""
        while not self._stop_monitor:
            try:
                current_hash = self._get_config_hash()
                if current_hash and current_hash != self._last_config_hash:
                    self.logger.info("检测到配置文件变化，准备重载...")
                    if self.reload():
                        self.logger.info("配置已成功重载")
                    else:
                        self.logger.error("配置重载失败，已恢复为上一个版本")
                
                time.sleep(check_interval)
            except Exception as e:
                self.logger.error(f"配置监控线程出错: {e}")
                time.sleep(check_interval)
    
    def load_config(self):
        """加载配置文件"""
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, 'r', encoding='utf-8') as f:
                    self.config = yaml.safe_load(f) or {}
                self.config['_server_files'] = self._load_server_files()
            except yaml.YAMLError as e:
                raise ConfigValidationError(f"配置文件YAML格式错误: {e}")
            except Exception as e:
                raise ConfigValidationError(f"读取配置文件失败: {e}")
        else:
            self.config = self.get_default_config()
            self.save_config()
            self.config['_server_files'] = self._load_server_files()
    
    def save_config(self):
        """保存配置文件"""
        try:
            with open(self.config_path, 'w', encoding='utf-8') as f:
                yaml.dump(self._public_config(), f, allow_unicode=True, default_flow_style=False, sort_keys=False)
        except Exception as e:
            raise ConfigValidationError(f"保存配置文件失败: {e}")

    def _public_config(self) -> Dict[str, Any]:
        """过滤运行期内部字段，避免写回 config.yml。"""
        return {
            key: value for key, value in self.config.items()
            if not str(key).startswith('_')
        }
    
    def get_default_config(self) -> Dict:
        """获取默认配置"""
        return {
            'websocket': {
                'port': 8080,
                'token': '',
                'auth_enabled': False
            },
            'debug': False
        }
    
    def validate_config(self) -> List[str]:
        """验证配置文件"""
        errors = []

        ws_port = self.get_ws_port()
        if not (1024 <= ws_port <= 65535):
            errors.append(f"WebSocket端口 {ws_port} 无效(应在1024-65535之间)")

        seen_server_names = set()
        for index, server in enumerate(self.get_servers(), 1):
            errors.extend(self._validate_server_config(server, index, seen_server_names, ws_port))

        return errors

    def _validate_server_config(self, server: Dict[str, Any], index: int, seen_server_names: set, ws_port: int) -> List[str]:
        """验证单个独立服务器配置文件。"""
        errors = []
        source = server.get('_config_file') or f"servers[{index}]"

        try:
            name = str(server.get('name') or f'server{index}').strip()
            if name.lower() in seen_server_names:
                errors.append(f"服务器名称重复: {name}")
            seen_server_names.add(name.lower())

            msmp_config = server.get('msmp') or {}
            rcon_config = server.get('rcon') or {}
            msmp_enabled = bool(msmp_config.get('enabled', False))
            rcon_enabled = bool(rcon_config.get('enabled', False))

            if not msmp_enabled and not rcon_enabled:
                errors.append(f"{source} 必须至少启用MSMP或RCON其中一种连接方式")

            qq_config = server.get('qq') or {}
            groups = qq_config.get('groups') or []
            admins = qq_config.get('admins') or []
            if not groups:
                errors.append(f"{source} 至少需要配置一个QQ群")
            for group_id in groups:
                if not isinstance(group_id, int) or group_id <= 0:
                    errors.append(f"{source} 无效的QQ群号: {group_id}")
            if not admins:
                errors.append(f"{source} 至少需要配置一个管理员QQ号")
            for admin_id in admins:
                if not isinstance(admin_id, int) or admin_id <= 0:
                    errors.append(f"{source} 无效的管理员QQ号: {admin_id}")

            if msmp_enabled:
                msmp_port = int(msmp_config.get('port') or 0)
                if not msmp_config.get('host'):
                    errors.append(f"{source} MSMP host 未配置")
                if not (1024 <= msmp_port <= 65535):
                    errors.append(f"{source} MSMP端口 {msmp_port} 无效(应在1024-65535之间)")
                if msmp_port == ws_port:
                    errors.append(f"{source} MSMP端口不能与WebSocket端口相同 ({ws_port})")
                if not msmp_config.get('password'):
                    errors.append(f"{source} MSMP password 未配置")

            if rcon_enabled:
                rcon_port = int(rcon_config.get('port') or 0)
                if not rcon_config.get('host'):
                    errors.append(f"{source} RCON host 未配置")
                if not (1024 <= rcon_port <= 65535):
                    errors.append(f"{source} RCON端口 {rcon_port} 无效(应在1024-65535之间)")
                if rcon_port == ws_port:
                    errors.append(f"{source} RCON端口不能与WebSocket端口相同 ({ws_port})")
                if not rcon_config.get('password'):
                    errors.append(f"{source} RCON password 未配置")

            server_section = server.get('server', {}) if isinstance(server.get('server'), dict) else {}
            server_script = str(server_section.get('start_script') or '').strip()
            if server_script and not server_script.lower().endswith(('.bat', '.cmd', '.sh')):
                errors.append(f"{source} 启动脚本格式不支持: {server_script}(仅支持.bat/.cmd/.sh)")

            commands_config = server.get('commands') or {}
            enabled_commands = commands_config.get('enabled_commands', {})
            for cmd_name, enabled in enabled_commands.items():
                if not isinstance(enabled, bool):
                    errors.append(f"{source} 基础命令 {cmd_name} 的启用状态必须是布尔值")

            enabled_admin_commands = commands_config.get('enabled_admin_commands', {})
            for cmd_name, enabled in enabled_admin_commands.items():
                if not isinstance(enabled, bool):
                    errors.append(f"{source} 管理员命令 {cmd_name} 的普通成员开放状态必须是布尔值")

            errors.extend(self._validate_tps_config(commands_config, source))

            custom_listeners = server.get('custom_listeners') or {}
            if custom_listeners.get('enabled', False):
                for i, rule in enumerate(custom_listeners.get('rules', [])):
                    errors.extend(self._validate_listener_rule(rule, i))
        except Exception as e:
            errors.append(f"{source} 验证失败: {e}")

        return errors
    
    def _validate_custom_listeners(self) -> List[str]:
        """验证自定义监听器配置"""
        errors = []
        
        if not self.is_custom_listeners_enabled():
            return errors
        
        try:
            rules = self.get_custom_listener_rules()
            
            if not rules:
                return errors
            
            for i, rule in enumerate(rules):
                rule_errors = self._validate_listener_rule(rule, i)
                errors.extend(rule_errors)
            
        except Exception as e:
            errors.append(f"验证自定义监听器配置时出错: {e}")
        
        return errors
    
    def _validate_tps_config(self, commands_config: Optional[Dict[str, Any]] = None, source: str = "commands") -> List[str]:
        """验证TPS相关配置"""
        errors = []
        
        try:
            commands_config = commands_config or {}
            
            # 验证 tps_regex
            tps_regex = commands_config.get('tps_regex', '')
            if tps_regex:
                try:
                    re.compile(tps_regex)
                except re.error as e:
                    errors.append(f"{source} TPS正则表达式语法错误: {e}")
            else:
                errors.append(f"{source} TPS正则表达式不能为空")
            
            # 验证 tps_group_index
            tps_group_index = commands_config.get('tps_group_index', 1)
            if not isinstance(tps_group_index, int):
                errors.append(f"{source} tps_group_index 必须是整数,当前值: {tps_group_index}")
            elif tps_group_index < 1:
                errors.append(f"{source} tps_group_index 必须大于等于1,当前值: {tps_group_index}")
            elif tps_group_index > 10:
                errors.append(f"{source} tps_group_index 过大(建议不超过10),当前值: {tps_group_index}")
            
            # 验证 tps_show_raw_output
            tps_show_raw = commands_config.get('tps_show_raw_output', True)
            if not isinstance(tps_show_raw, bool):
                errors.append(f"{source} tps_show_raw_output 必须是布尔值,当前值: {tps_show_raw}")
            
        except Exception as e:
            errors.append(f"{source} 验证TPS配置时出错: {e}")
        
        return errors
    
    def _validate_listener_rule(self, rule: Dict, index: int) -> List[str]:
        """验证单个监听器规则"""
        errors = []
        
        rule_name = rule.get('name', f'rule_{index}')
        
        if not rule.get('pattern'):
            errors.append(f"监听规则 '{rule_name}' 的 pattern 不能为空")
        
        qq_message = rule.get('qq_message', '')
        server_command = rule.get('server_command', '')
        
        if not qq_message and not server_command:
            errors.append(f"监听规则 '{rule_name}' 的 qq_message 和 server_command 不能同时为空")
        
        return errors
    
    def reload(self, notify_callbacks: bool = True) -> bool:
        """重载配置文件
        
        Args:
            notify_callbacks: 是否通知所有回调函数
            
        Returns:
            True 如果重载成功，False 如果失败
        """
        with self._reload_lock:
            try:
                old_config = self.config.copy()
                
                self.load_config()
                
                errors = self.validate_config()
                if errors:
                    self.config = old_config
                    raise ConfigValidationError("新配置验证失败:\n" + "\n".join(f"  - {e}" for e in errors))
                
                self._last_config_hash = self._get_config_hash()
                
                if notify_callbacks:
                    self._trigger_reload_callbacks(old_config, self.config)
                
                return True
                
            except ConfigValidationError as e:
                self.logger.error(f"配置重载失败: {e}")
                return False
            except Exception as e:
                self.logger.error(f"配置重载异常: {e}", exc_info=True)
                return False
    
    def _trigger_reload_callbacks(self, old_config: Dict, new_config: Dict):
        """触发所有注册的重载回调函数"""
        import asyncio
        
        for callback in self._reload_callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    try:
                        loop = self._event_loop or asyncio.get_event_loop()
                        try:
                            running_loop = asyncio.get_running_loop()
                        except RuntimeError:
                            running_loop = None

                        if running_loop is loop:
                            loop.create_task(callback(old_config, new_config))
                        elif loop.is_running():
                            future = asyncio.run_coroutine_threadsafe(
                                callback(old_config, new_config),
                                loop
                            )
                            future.result(timeout=10)
                        else:
                            loop.run_until_complete(callback(old_config, new_config))
                    except RuntimeError:
                        asyncio.run(callback(old_config, new_config))
                else:
                    callback(old_config, new_config)
                    
            except Exception as e:
                self.logger.error(f"执行配置重载回调函数失败: {e}", exc_info=True)
    
    # ============ MSMP配置 ============
    def is_msmp_enabled(self) -> bool:
        return self.config.get('msmp', {}).get('enabled', False)
    
    def get_msmp_host(self) -> str:
        return self.config.get('msmp', {}).get('host', 'localhost')
    
    def get_msmp_port(self) -> int:
        return self.config.get('msmp', {}).get('port', 21111)
    
    def get_msmp_password(self) -> str:
        return self.config.get('msmp', {}).get('password', '')
    
    # ============ RCON配置 ============
    def is_rcon_enabled(self) -> bool:
        return self.config.get('rcon', {}).get('enabled', False)
    
    def get_rcon_host(self) -> str:
        return self.config.get('rcon', {}).get('host', 'localhost')
    
    def get_rcon_port(self) -> int:
        return self.config.get('rcon', {}).get('port', 25575)
    
    def get_rcon_password(self) -> str:
        return self.config.get('rcon', {}).get('password', '')
    
    # ============ WebSocket配置 ============
    def get_ws_port(self) -> int:
        return self.config.get('websocket', {}).get('port', 8080)
    
    def get_websocket_token(self) -> str:
        return self.config.get('websocket', {}).get('token', '')
    
    def is_websocket_auth_enabled(self) -> bool:
        return self.config.get('websocket', {}).get('auth_enabled', False)
    
    # ============ QQ群配置 ============
    def get_qq_groups(self) -> List[int]:
        groups = []
        for server in self.get_servers():
            groups.extend((server.get('qq') or {}).get('groups', []))
        return sorted({group for group in groups if isinstance(group, int)})
    
    def get_qq_admins(self) -> List[int]:
        admins = []
        for server in self.get_servers():
            admins.extend((server.get('qq') or {}).get('admins', []))
        return sorted({admin for admin in admins if isinstance(admin, int)})
    
    def is_admin(self, user_id: int) -> bool:
        return user_id in self.get_qq_admins()

    def is_server_admin(self, user_id: int, server: Optional[Dict[str, Any]] = None) -> bool:
        if not server:
            return self.is_admin(user_id)
        return user_id in ((server.get('qq') or {}).get('admins', []))
    
    def is_welcome_new_members_enabled(self) -> bool:
        return False
    
    def get_welcome_message(self) -> str:
        return '{at} 欢迎加入！输入 help 查看可用命令'

    def get_server_welcome_config(self, group_id: int) -> Dict[str, Any]:
        matching_configs = [
            (server.get('qq') or {})
            for server in self.get_servers()
            if group_id in ((server.get('qq') or {}).get('groups') or [])
        ]
        for qq_config in matching_configs:
            if qq_config.get('welcome_new_members', False):
                return {
                    'enabled': True,
                    'message': qq_config.get('welcome_message', '{at} 欢迎加入！输入 help 查看可用命令'),
                }
        return {
            'enabled': False,
            'message': '{at} 欢迎加入！输入 help 查看可用命令',
        }
    
    def _notifications_config(self, server: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if server:
            notifications = server.get('notifications')
            if isinstance(notifications, dict):
                return notifications
        notifications = self.config.get('notifications', {})
        return notifications if isinstance(notifications, dict) else {}

    def is_log_messages_enabled(self, server: Optional[Dict[str, Any]] = None) -> bool:
        return self._notifications_config(server).get('log_messages', False)
    
    # ============ 服务器配置 ============
    def _server_runtime_config(self, server: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if server:
            server_config = server.get('server')
            if isinstance(server_config, dict):
                return server_config
        server_config = self.config.get('server', {})
        return server_config if isinstance(server_config, dict) else {}

    def get_server_start_script(self, server: Optional[Dict[str, Any]] = None) -> str:
        return self._resolve_runtime_path(self._server_runtime_config(server).get('start_script', ''))
    
    def get_server_working_directory(self, server: Optional[Dict[str, Any]] = None) -> str:
        runtime_config = self._server_runtime_config(server)
        working_dir = self._resolve_runtime_path(runtime_config.get('working_directory', ''))
        if working_dir:
            return working_dir
        start_script = self.get_server_start_script(server)
        return os.path.dirname(start_script) if start_script else ''

    def _resolve_runtime_path(self, value: Any) -> str:
        path = str(value or '').strip().replace('\\', os.sep).replace('/', os.sep)
        if not path:
            return ''
        if os.path.isabs(path):
            return path
        return os.path.abspath(path)
    
    def get_server_startup_timeout(self, server: Optional[Dict[str, Any]] = None) -> int:
        return self._server_runtime_config(server).get('startup_timeout', 300)
    
    def is_auto_restart_on_crash_enabled(self, server: Optional[Dict[str, Any]] = None) -> bool:
        """是否启用崩溃后自动重启"""
        return self._server_runtime_config(server).get('auto_restart_on_crash', False)

    def get_crash_restart_delay(self, server: Optional[Dict[str, Any]] = None) -> int:
        """获取异常停止重启延迟(秒)"""
        return self._server_runtime_config(server).get('crash_restart_delay', 10)

    def get_log_idle_restart_timeout(self, server: Optional[Dict[str, Any]] = None) -> int:
        """获取日志空闲重启超时时间(秒),0表示关闭"""
        return self._server_runtime_config(server).get('log_idle_restart_timeout', 0)

    def get_servers(self) -> List[Dict[str, Any]]:
        """获取多服务器配置。servers/*.yml 必须是完整配置文件结构。"""
        file_servers = self.config.get('_server_files', [])
        if isinstance(file_servers, list) and file_servers:
            return [dict(server) for server in file_servers if isinstance(server, dict)]
        return []

    def get_server_config_dir(self) -> Path:
        """获取独立服务器配置目录。"""
        configured = str(self.config.get('server_config_dir') or 'servers').strip()
        path = Path(configured)
        if not path.is_absolute():
            path = Path(self.config_path).resolve().parent / path
        return path

    def _load_server_files(self) -> List[Dict[str, Any]]:
        """从 servers/*.yml 加载完整服务器配置文件。"""
        server_dir = self.get_server_config_dir()
        if not server_dir.exists():
            return []

        servers = []
        for path in sorted([*server_dir.glob("*.yml"), *server_dir.glob("*.yaml")]):
            try:
                data = yaml.safe_load(path.read_text(encoding='utf-8')) or {}
                if not isinstance(data, dict):
                    raise ConfigValidationError(f"{path} 顶层必须是对象")
                missing_sections = [
                    section for section in self.SERVER_REQUIRED_SECTIONS
                    if not isinstance(data.get(section), dict)
                ]
                if missing_sections:
                    raise ConfigValidationError(
                        f"{path} 不是完整服务器配置，缺少配置段: {', '.join(missing_sections)}"
                    )
                data.setdefault('name', path.stem)
                data['_config_file'] = str(path)
                servers.append(data)
            except ConfigValidationError:
                raise
            except yaml.YAMLError as e:
                raise ConfigValidationError(f"服务器配置文件YAML格式错误 {path}: {e}")
            except Exception as e:
                raise ConfigValidationError(f"读取服务器配置文件失败 {path}: {e}")
        return servers

    def resolve_server(self, selector: str = "") -> Optional[Dict[str, Any]]:
        """按编号或名称解析服务器配置。编号从 1 开始。"""
        servers = self.get_servers()
        selector = str(selector or "").strip()
        if not selector:
            return dict(servers[0]) if servers else None

        if selector.isdigit():
            index = int(selector) - 1
            if 0 <= index < len(servers):
                return dict(servers[index])
            return None

        selector_lower = selector.lower()
        for server in servers:
            name = str(server.get('name', '')).lower()
            if name == selector_lower:
                return dict(server)
        return None

    def resolve_server_for_group(self, group_id: int) -> Optional[Dict[str, Any]]:
        """按QQ群号解析默认服务器。"""
        for server in self.get_servers():
            if group_id in ((server.get('qq') or {}).get('groups', [])):
                return dict(server)
        return None

    def resolve_servers_for_context(self, user_id: int = 0, group_id: int = 0, is_private: bool = False) -> List[Dict[str, Any]]:
        """按当前群聊/私聊上下文获取有权限访问的服务器列表。"""
        servers = []
        for server in self.get_servers():
            qq_config = server.get('qq') or {}
            if is_private:
                if user_id in (qq_config.get('admins') or []):
                    servers.append(dict(server))
            elif group_id and group_id in (qq_config.get('groups') or []):
                servers.append(dict(server))
        return servers

    # ============ 命令配置 ============
    def get_tps_command(self) -> str:
        return self.config.get('commands', {}).get('tps_command', 'tps')

    def get_tps_regex(self) -> str:
        """获取TPS正则表达式"""
        return self.config.get('commands', {}).get('tps_regex', 'TPS from last 1m, 5m, 15m:\\s*([\\d.]+)')

    def get_tps_group_index(self) -> int:
        """获取TPS正则表达式的捕获组索引"""
        return self.config.get('commands', {}).get('tps_group_index', 1)

    def is_tps_raw_output_enabled(self) -> bool:
        """是否输出原始TPS返回值"""
        return self.config.get('commands', {}).get('tps_show_raw_output', True)

    def is_command_enabled(self, command_name: str) -> bool:
        """检查基础命令是否启用（管理员不受此限制）"""
        enabled_commands = self.config.get('commands', {}).get('enabled_commands', {})
        return enabled_commands.get(command_name, True)

    def is_admin_command_enabled(self, command_name: str) -> bool:
        """检查管理员命令是否向普通成员开放（管理员不受此限制）"""
        enabled_admin_commands = self.config.get('commands', {}).get('enabled_admin_commands', {})
        return enabled_admin_commands.get(command_name, False)

    def can_use_command(self, user_id: int, command_name: str, is_admin_command: bool = False) -> bool:
        """检查用户是否可以使用命令
        Args:
            user_id: 用户ID
            command_name: 命令名称
            is_admin_command: 是否为管理员命令
        Returns:
            bool: 是否可以使用
        """
        is_admin = self.is_admin(user_id)
        
        if is_admin_command:
            # 管理员命令：管理员始终可用，非管理员需要检查是否启用
            if is_admin:
                return True
            else:
                return self.is_admin_command_enabled(command_name)
        else:
            # 基础命令：管理员始终可用，非管理员需要检查是否启用
            if is_admin:
                return True
            else:
                return self.is_command_enabled(command_name)

    def get_enabled_commands(self) -> Dict[str, bool]:
        return self.config.get('commands', {}).get('enabled_commands', {
            'list': True,
            'tps': True,
            'rules': True,
            'status': True,
            'help': True
        })

    def get_enabled_admin_commands(self) -> Dict[str, bool]:
        """获取向普通成员开放的管理员命令列表。"""
        return self.config.get('commands', {}).get('enabled_admin_commands', {
            'start': False,
            'stop': False,
            'kill': False,
            'reload': False,
            'log': False,
            'reconnect': False,
            'reconnect_msmp': False,
            'reconnect_rcon': False,
            'crash': False,
            'sysinfo': False,
            'disk': False,
            'process': False,
            'network': False,
            'listeners': False
        })
    
    # ============ 通知配置 ============
    def is_server_event_notify_enabled(self, server: Optional[Dict[str, Any]] = None) -> bool:
        return self._notifications_config(server).get('server_events', True)
    
    def is_player_event_notify_enabled(self, server: Optional[Dict[str, Any]] = None) -> bool:
        return self._notifications_config(server).get('player_events', False)
    
    # ============ 区块监控配置 ============
    def get_chunk_monitor_config(self, server: Optional[Dict[str, Any]] = None) -> Dict[str, bool]:
        config = self._notifications_config(server).get('chunk_monitor', {
            'enabled': False,
            'notify_admins': True,
            'notify_groups': True
        })
        return config if isinstance(config, dict) else {}

    def is_chunk_monitor_enabled(self, server: Optional[Dict[str, Any]] = None) -> bool:
        return self.get_chunk_monitor_config(server).get('enabled', False)

    def should_notify_admins_on_chunk_monitor(self, server: Optional[Dict[str, Any]] = None) -> bool:
        return self.get_chunk_monitor_config(server).get('notify_admins', True)

    def should_notify_groups_on_chunk_monitor(self, server: Optional[Dict[str, Any]] = None) -> bool:
        return self.get_chunk_monitor_config(server).get('notify_groups', True)
    
    # ============ 高级配置 ============
    def get_reconnect_interval(self) -> int:
        return self.config.get('advanced', {}).get('reconnect_interval', 300)
    
    def get_heartbeat_interval(self) -> int:
        return self.config.get('advanced', {}).get('heartbeat_interval', 30)
    
    def get_command_cooldown(self) -> int:
        return self.config.get('advanced', {}).get('command_cooldown', 3)
    
    def get_max_message_length(self) -> int:
        return self.config.get('advanced', {}).get('max_message_length', 2500)
    
    def get_max_server_logs(self, server: Optional[Dict[str, Any]] = None) -> int:
        """获取最大服务器日志行数"""
        if server:
            return (server.get('advanced') or {}).get(
                'max_server_logs',
                self.config.get('advanced', {}).get('max_server_logs', 100)
            )
        return self.config.get('advanced', {}).get('max_server_logs', 100)
    
    def get_player_list_cache_ttl(self) -> int:
        """获取玩家列表缓存时间（秒）"""
        return self.config.get('advanced', {}).get('player_list_cache_ttl', 5)
    
    # ============ 自定义监听器配置 ============
    def is_custom_listeners_enabled(self) -> bool:
        return self.config.get('custom_listeners', {}).get('enabled', False)
    
    def get_custom_listener_rules(self) -> List[Dict]:
        return self.config.get('custom_listeners', {}).get('rules', [])
    
    def get_custom_listener_rule(self, rule_name: str) -> Optional[Dict]:
        rules = self.get_custom_listener_rules()
        for rule in rules:
            if rule.get('name') == rule_name:
                return rule
        return None
    
    def add_custom_listener_rule(self, rule: Dict):
        if 'custom_listeners' not in self.config:
            self.config['custom_listeners'] = {'enabled': False, 'rules': []}
        
        if 'rules' not in self.config['custom_listeners']:
            self.config['custom_listeners']['rules'] = []
        
        self.config['custom_listeners']['rules'].append(rule)
        self.save_config()
    
    def remove_custom_listener_rule(self, rule_name: str) -> bool:
        rules = self.get_custom_listener_rules()
        
        for i, rule in enumerate(rules):
            if rule.get('name') == rule_name:
                rules.pop(i)
                self.save_config()
                return True
        
        return False
    
    def enable_custom_listeners(self):
        if 'custom_listeners' not in self.config:
            self.config['custom_listeners'] = {'enabled': True, 'rules': []}
        else:
            self.config['custom_listeners']['enabled'] = True
        self.save_config()
    
    def disable_custom_listeners(self):
        if 'custom_listeners' not in self.config:
            self.config['custom_listeners'] = {'enabled': False, 'rules': []}
        else:
            self.config['custom_listeners']['enabled'] = False
        self.save_config()
    
    # ============ 自定义命令配置 ============
    def is_custom_commands_enabled(self) -> bool:
        return self.config.get('custom_commands', {}).get('enabled', False)
    
    def get_custom_command_rules(self) -> List[Dict]:
        return self.config.get('custom_commands', {}).get('rules', [])
    
    def get_custom_command_rule(self, rule_name: str) -> Optional[Dict]:
        rules = self.get_custom_command_rules()
        for rule in rules:
            if rule.get('name') == rule_name:
                return rule
        return None
    
    def add_custom_command_rule(self, rule: Dict):
        if 'custom_commands' not in self.config:
            self.config['custom_commands'] = {'enabled': False, 'rules': []}
        
        if 'rules' not in self.config['custom_commands']:
            self.config['custom_commands']['rules'] = []
        
        self.config['custom_commands']['rules'].append(rule)
        self.save_config()
    
    def remove_custom_command_rule(self, rule_name: str) -> bool:
        rules = self.get_custom_command_rules()
        
        for i, rule in enumerate(rules):
            if rule.get('name') == rule_name:
                rules.pop(i)
                self.save_config()
                return True
        
        return False
    
    def enable_custom_commands(self):
        if 'custom_commands' not in self.config:
            self.config['custom_commands'] = {'enabled': True, 'rules': []}
        else:
            self.config['custom_commands']['enabled'] = True
        self.save_config()
    
    def disable_custom_commands(self):
        if 'custom_commands' not in self.config:
            self.config['custom_commands'] = {'enabled': False, 'rules': []}
        else:
            self.config['custom_commands']['enabled'] = False
        self.save_config()
    
    # ============ 定时任务配置 ============
    def is_scheduled_tasks_enabled(self) -> bool:
        return self.config.get('scheduled_tasks', {}).get('enabled', False)
    
    def get_auto_start_config(self) -> Dict:
        return self.config.get('scheduled_tasks', {}).get('auto_start', {
            'enabled': False,
            'times': [],
            'weekdays': [],
            'pre_notify_seconds': 300,
            'notify_message': '服务器将在 {countdown} 秒后启动，请做好准备'
        })
    
    def get_auto_stop_config(self) -> Dict:
        return self.config.get('scheduled_tasks', {}).get('auto_stop', {
            'enabled': False,
            'times': [],
            'weekdays': [],
            'warning_before_seconds': 600,
            'first_warning': '服务器将在 {countdown} 秒后关闭，请保存游戏',
            'second_warning': '服务器即将在 1 分钟后关闭',
            'immediate_message': '服务器正在关闭'
        })
    
    def get_auto_restart_config(self) -> Dict:
        return self.config.get('scheduled_tasks', {}).get('auto_restart', {
            'enabled': False,
            'times': [],
            'weekdays': [],
            'warning_before_seconds': 600,
            'first_warning': '服务器将在 {countdown} 秒后重启，请保存游戏',
            'second_warning': '服务器即将在 1 分钟后重启',
            'immediate_message': '服务器正在重启',
            'wait_before_startup': 10,
            'restart_success_message': '服务器已重启，欢迎回来！'
        })
    
    def is_auto_start_enabled(self) -> bool:
        return self.get_auto_start_config().get('enabled', False)
    
    def is_auto_stop_enabled(self) -> bool:
        return self.get_auto_stop_config().get('enabled', False)
    
    def is_auto_restart_enabled(self) -> bool:
        return self.get_auto_restart_config().get('enabled', False)
    
    # ============ 其他配置 ============
    def is_debug_mode(self) -> bool:
        return self.config.get('debug', False)
    
    def set_debug_mode(self, enabled: bool):
        self.config['debug'] = enabled
        self.save_config()
    
    def to_dict(self) -> Dict:
        """获取配置字典(隐藏敏感信息)"""
        safe_config = copy.deepcopy(self.config)
        if 'websocket' in safe_config and 'token' in safe_config['websocket']:
            safe_config['websocket']['token'] = '***' if safe_config['websocket']['token'] else ''
        for server in safe_config.get('_server_files', []) or []:
            if not isinstance(server, dict):
                continue
            for section in ('msmp', 'rcon'):
                section_config = server.get(section)
                if isinstance(section_config, dict) and section_config.get('password'):
                    section_config['password'] = '***'
        return safe_config
    
    def get_config_status(self) -> str:
        """获取配置状态信息"""
        lines = ["=" * 60, "配置状态信息", "=" * 60]
        
        servers = self.get_servers()
        lines.append("\n【Bot连接配置】")
        lines.append(f"  WebSocket: 端口 {self.get_ws_port()}")
        if self.is_websocket_auth_enabled():
            lines.append(f"    - 认证: 已启用")
        
        lines.append("\n【QQ配置】")
        lines.append(f"  群号数: {len(self.get_qq_groups())}")
        lines.append(f"  管理员数: {len(self.get_qq_admins())}")
        
        lines.append("\n【服务器配置】")
        lines.append(f"  独立服务器配置数: {len(servers)}")
        for index, server in enumerate(servers, 1):
            name = server.get('name') or f'server{index}'
            msmp = server.get('msmp') or {}
            rcon = server.get('rcon') or {}
            qq = server.get('qq') or {}
            server_section = server.get('server') or {}
            commands = server.get('commands') or {}
            advanced = server.get('advanced') or {}
            enabled_commands = commands.get('enabled_commands') or {}
            enabled_admin = commands.get('enabled_admin_commands') or {}
            lines.append(f"  {index}. {name}")
            lines.append(f"    - 配置文件: {server.get('_config_file', '未知')}")
            lines.append(f"    - MSMP: {'启用' if msmp.get('enabled') else '禁用'} {msmp.get('host', 'localhost')}:{msmp.get('port', '-')}")
            lines.append(f"    - RCON: {'启用' if rcon.get('enabled') else '禁用'} {rcon.get('host', 'localhost')}:{rcon.get('port', '-')}")
            lines.append(f"    - QQ群: {len(qq.get('groups') or [])} 个，管理员: {len(qq.get('admins') or [])} 个")
            lines.append(f"    - 启动脚本: {server_section.get('start_script') or '未配置'}")
            lines.append(f"    - 启动超时: {server_section.get('startup_timeout', 300)}秒")
            lines.append(f"    - 基础命令: {sum(1 for v in enabled_commands.values() if v)}/{len(enabled_commands)}")
            lines.append(f"    - 管理员命令向普通成员开放: {sum(1 for v in enabled_admin.values() if v)}/{len(enabled_admin)}")
            lines.append(f"    - 命令冷却: {advanced.get('command_cooldown', 3)}秒，最大日志: {advanced.get('max_server_logs', 100)}行")
        
        lines.append("=" * 60)
        return "\n".join(lines)
