import yaml
import os
from typing import List, Dict, Optional

class ConfigValidationError(Exception):
    """配置验证错误"""
    pass

class ConfigManager:
    def __init__(self, config_path: str = "config.yml"):
        self.config_path = config_path
        self.config = {}
        self.load_config()
        # 验证配置
        errors = self.validate_config()
        if errors:
            raise ConfigValidationError("配置验证失败:\n" + "\n".join(f"  - {e}" for e in errors))
    
    def load_config(self):
        """加载配置文件"""
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, 'r', encoding='utf-8') as f:
                    self.config = yaml.safe_load(f) or {}
            except yaml.YAMLError as e:
                raise ConfigValidationError(f"配置文件YAML格式错误: {e}")
            except Exception as e:
                raise ConfigValidationError(f"读取配置文件失败: {e}")
        else:
            # 创建默认配置
            self.config = self.get_default_config()
            self.save_config()
    
    def save_config(self):
        """保存配置文件"""
        try:
            with open(self.config_path, 'w', encoding='utf-8') as f:
                yaml.dump(self.config, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
        except Exception as e:
            raise ConfigValidationError(f"保存配置文件失败: {e}")
    
    def get_default_config(self) -> Dict:
        """获取默认配置"""
        return {
            'msmp': {
                'enabled': True,
                'host': 'localhost',
                'port': 21111,
                'password': 'your_msmp_password_here'
            },
            'rcon': {
                'enabled': True,
                'host': 'localhost',
                'port': 25575,
                'password': 'your_rcon_password_here'
            },
            'websocket': {
                'port': 8080,
                'token': '',
                'auth_enabled': False
            },
            'qq': {
                'groups': [123456789],
                'admins': [123456789],
                'welcome_new_members': False,
                'welcome_message': '欢迎新成员加入!输入 help 查看可用命令'
            },
            'server': {
                'start_script': '',
                'working_directory': '',
                'startup_timeout': 300
            },
            'commands': {
                'tps_command': 'tps'
            },
            'notifications': {
                'server_events': True,
                'player_events': True,
                'log_messages': False
            },
            'advanced': {
                'reconnect_interval': 300,
                'heartbeat_interval': 30,
                'command_cooldown': 3,
                'max_message_length': 2500,
                'player_list_cache_ttl': 5
            },
            'debug': False
        }
    
    def validate_config(self) -> List[str]:
        """验证配置文件"""
        errors = []
        
        # 检查至少启用一种连接方式
        msmp_enabled = self.is_msmp_enabled()
        rcon_enabled = self.is_rcon_enabled()
        
        if not msmp_enabled and not rcon_enabled:
            errors.append("必须至少启用MSMP或RCON其中一种连接方式")
        
        # 验证MSMP配置(如果启用)
        if msmp_enabled:
            if not self.get_msmp_host():
                errors.append("MSMP host 未配置")
            
            msmp_port = self.get_msmp_port()
            if not (1024 <= msmp_port <= 65535):
                errors.append(f"MSMP端口 {msmp_port} 无效(应在1024-65535之间)")
            
            if not self.get_msmp_password():
                errors.append("MSMP password 未配置")
            elif self.get_msmp_password() == 'your_msmp_password_here':
                errors.append("MSMP password 仍使用默认值,请修改为实际密码")
        
        # 验证RCON配置(如果启用)
        if rcon_enabled:
            if not self.get_rcon_host():
                errors.append("RCON host 未配置")
            
            rcon_port = self.get_rcon_port()
            if not (1024 <= rcon_port <= 65535):
                errors.append(f"RCON端口 {rcon_port} 无效(应在1024-65535之间)")
            
            if not self.get_rcon_password():
                errors.append("RCON password 未配置")
            elif self.get_rcon_password() == 'your_rcon_password_here':
                errors.append("RCON password 仍使用默认值,请修改为实际密码")
        
        # 验证WebSocket配置
        ws_port = self.get_ws_port()
        if not (1024 <= ws_port <= 65535):
            errors.append(f"WebSocket端口 {ws_port} 无效(应在1024-65535之间)")
        
        if msmp_enabled and ws_port == self.get_msmp_port():
            errors.append(f"WebSocket端口不能与MSMP端口相同 ({ws_port})")
        
        if rcon_enabled and ws_port == self.get_rcon_port():
            errors.append(f"WebSocket端口不能与RCON端口相同 ({ws_port})")
        
        # 验证QQ配置
        if not self.get_qq_groups():
            errors.append("至少需要配置一个QQ群")
        
        for group_id in self.get_qq_groups():
            if not isinstance(group_id, int) or group_id <= 0:
                errors.append(f"无效的QQ群号: {group_id}")
        
        if not self.get_qq_admins():
            errors.append("至少需要配置一个管理员QQ号")
        
        for admin_id in self.get_qq_admins():
            if not isinstance(admin_id, int) or admin_id <= 0:
                errors.append(f"无效的管理员QQ号: {admin_id}")
        
        # 验证服务器配置(如果配置了启动脚本)
        start_script = self.get_server_start_script()
        if start_script:
            if not os.path.exists(start_script):
                # 只是警告,不算错误
                pass
            elif not (start_script.endswith('.bat') or start_script.endswith('.sh')):
                errors.append(f"服务器启动脚本格式不支持: {start_script}(仅支持.bat或.sh)")
        
        return errors
    
    def reload(self):
        """重新加载配置"""
        self.load_config()
        errors = self.validate_config()
        if errors:
            raise ConfigValidationError("配置验证失败:\n" + "\n".join(f"  - {e}" for e in errors))
    
    # MSMP配置
    def is_msmp_enabled(self) -> bool:
        return self.config.get('msmp', {}).get('enabled', True)
    
    def get_msmp_host(self) -> str:
        return self.config.get('msmp', {}).get('host', 'localhost')
    
    def get_msmp_port(self) -> int:
        return self.config.get('msmp', {}).get('port', 21111)
    
    def get_msmp_password(self) -> str:
        return self.config.get('msmp', {}).get('password', '')
    
    # RCON配置
    def is_rcon_enabled(self) -> bool:
        return self.config.get('rcon', {}).get('enabled', False)
    
    def get_rcon_host(self) -> str:
        return self.config.get('rcon', {}).get('host', 'localhost')
    
    def get_rcon_port(self) -> int:
        return self.config.get('rcon', {}).get('port', 25575)
    
    def get_rcon_password(self) -> str:
        return self.config.get('rcon', {}).get('password', '')
    
    # WebSocket配置
    def get_ws_port(self) -> int:
        return self.config.get('websocket', {}).get('port', 8080)
    
    def get_websocket_token(self) -> str:
        return self.config.get('websocket', {}).get('token', '')
    
    def is_websocket_auth_enabled(self) -> bool:
        return self.config.get('websocket', {}).get('auth_enabled', False)
    
    # QQ群配置
    def get_qq_groups(self) -> List[int]:
        return self.config.get('qq', {}).get('groups', [])
    
    def get_qq_admins(self) -> List[int]:
        return self.config.get('qq', {}).get('admins', [])
    
    def is_admin(self, user_id: int) -> bool:
        return user_id in self.get_qq_admins()
    
    def is_welcome_new_members_enabled(self) -> bool:
        return self.config.get('qq', {}).get('welcome_new_members', False)
    
    def get_welcome_message(self) -> str:
        return self.config.get('qq', {}).get('welcome_message', '欢迎新成员加入!输入 help 查看可用命令')
    
    def is_log_messages_enabled(self) -> bool:
        return self.config.get('notifications', {}).get('log_messages', False)
    
    # 服务器配置
    def get_server_start_script(self) -> str:
        return self.config.get('server', {}).get('start_script', '')
    
    def get_server_working_directory(self) -> str:
        return self.config.get('server', {}).get('working_directory', '')
    
    def get_server_startup_timeout(self) -> int:
        return self.config.get('server', {}).get('startup_timeout', 300)
    
    # 命令配置 (新增)
    def get_tps_command(self) -> str:
        """获取TPS命令配置"""
        return self.config.get('commands', {}).get('tps_command', 'tps')
    
    # 通知配置
    def is_server_event_notify_enabled(self) -> bool:
        return self.config.get('notifications', {}).get('server_events', True)
    
    def is_player_event_notify_enabled(self) -> bool:
        return self.config.get('notifications', {}).get('player_events', True)
    
    # 高级配置
    def get_reconnect_interval(self) -> int:
        return self.config.get('advanced', {}).get('reconnect_interval', 300)
    
    def get_heartbeat_interval(self) -> int:
        return self.config.get('advanced', {}).get('heartbeat_interval', 30)
    
    def get_command_cooldown(self) -> int:
        return self.config.get('advanced', {}).get('command_cooldown', 3)
    
    def get_max_message_length(self) -> int:
        return self.config.get('advanced', {}).get('max_message_length', 500)
    
    # 其他配置
    def is_debug_mode(self) -> bool:
        return self.config.get('debug', False)
    
    def to_dict(self) -> Dict:
        """获取配置字典(隐藏敏感信息)"""
        safe_config = self.config.copy()
        if 'msmp' in safe_config and 'password' in safe_config['msmp']:
            safe_config['msmp']['password'] = '***'
        if 'rcon' in safe_config and 'password' in safe_config['rcon']:
            safe_config['rcon']['password'] = '***'
        if 'websocket' in safe_config and 'token' in safe_config['websocket']:
            safe_config['websocket']['token'] = '***' if safe_config['websocket']['token'] else ''
        return safe_config