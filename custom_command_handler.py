import re
import logging
import asyncio
import time
import datetime
from typing import List, Dict, Any, Optional, Callable
from dataclasses import dataclass

@dataclass
class CustomCommand:
    """自定义指令"""
    name: str
    pattern: str  # 正则表达式
    enabled: bool = True
    admin_only: bool = False  # 是否仅管理员可用
    description: str = ""
    case_sensitive: bool = False
    trigger_limit: int = 0  # 触发次数限制 (0=无限制)
    trigger_cooldown: int = 0  # 冷却时间(秒)
    daily_limit: int = 0  # 每日触发限制
    
    # 执行动作
    group_message: str = ""  # 发送到群的消息
    server_command: str = ""  # 执行的服务器命令
    private_message: str = ""  # 发送到触发者的私聊消息
    
    # 高级功能
    conditions: List[Dict[str, Any]] = None  # 执行条件
    
    def __post_init__(self):
        if self.conditions is None:
            self.conditions = []
        self._compile_pattern()
        self.trigger_history = {
            'match_count': 0,
            'last_match_time': 0,
            'last_trigger_time': 0,
            'trigger_times_today': 0,
            'last_reset_date': ''
        }
    
    def _compile_pattern(self):
        """编译正则表达式"""
        flags = 0 if self.case_sensitive else re.IGNORECASE
        try:
            self.compiled_pattern = re.compile(self.pattern, flags)
        except re.error as e:
            raise ValueError(f"指令 '{self.name}' 的正则表达式语法错误: {e}")
    
    def match(self, text: str) -> Optional[re.Match]:
        """检查文本是否匹配指令"""
        if not self.enabled:
            return None
        return self.compiled_pattern.search(text)


class CustomCommandHandler:
    """自定义指令处理器"""
    
    def __init__(self, config_manager, logger: logging.Logger):
        self.config_manager = config_manager
        self.logger = logger
        self.commands: List[CustomCommand] = []
        self._load_commands_from_config()
    
    def _load_commands_from_config(self):
        """从配置文件加载自定义指令"""
        try:
            if 'custom_commands' not in self.config_manager.config:
                self.logger.info("未配置自定义指令")
                return
            
            commands_config = self.config_manager.config.get('custom_commands', {})
            
            if not commands_config.get('enabled', False):
                self.logger.info("自定义指令已禁用")
                return
            
            rules = commands_config.get('rules', [])
            
            if not rules:
                self.logger.info("未配置任何自定义指令规则")
                return
            
            new_commands = []
            for rule_config in rules:
                try:
                    cmd = CustomCommand(
                        name=rule_config.get('name', f'cmd_{len(new_commands)}'),
                        pattern=rule_config.get('pattern', ''),
                        enabled=rule_config.get('enabled', True),
                        admin_only=rule_config.get('admin_only', False),
                        description=rule_config.get('description', ''),
                        case_sensitive=rule_config.get('case_sensitive', False),
                        trigger_limit=rule_config.get('trigger_limit', 0),
                        trigger_cooldown=rule_config.get('trigger_cooldown', 0),
                        daily_limit=rule_config.get('daily_limit', 0),
                        group_message=rule_config.get('group_message', ''),
                        server_command=rule_config.get('server_command', ''),
                        private_message=rule_config.get('private_message', ''),
                        conditions=rule_config.get('conditions', [])
                    )
                    new_commands.append(cmd)
                    self.logger.info(f"已加载自定义指令: {cmd.name} [{'启用' if cmd.enabled else '禁用'}]")
                
                except ValueError as e:
                    self.logger.error(f"加载自定义指令失败: {e}")
                    continue
            
            self.commands = new_commands
            self.logger.info(f"共加载 {len(self.commands)} 个自定义指令")
            
        except Exception as e:
            self.logger.error(f"加载自定义指令配置失败: {e}", exc_info=True)
    
    def reload_commands(self):
        """重新加载指令"""
        self.commands.clear()
        self._load_commands_from_config()
    
    async def process_group_message(self,
                                   message: str,
                                   user_id: int,
                                   group_id: int,
                                   websocket,
                                   server_executor: Optional[Callable] = None) -> bool:
        """
        处理群消息中的自定义指令
        
        Args:
            message: 群消息内容
            user_id: 用户ID
            group_id: 群ID
            websocket: WebSocket连接
            server_executor: 服务器命令执行器
            
        Returns:
            True 如果匹配了指令，False 否则
        """
        is_admin = self.config_manager.is_admin(user_id)
        
        for cmd in self.commands:
            # 检查是否启用
            if not cmd.enabled:
                continue
            
            # 检查管理员权限
            if cmd.admin_only and not is_admin:
                self.logger.debug(f"用户 {user_id} 无权限执行指令 {cmd.name}")
                continue
            
            # 检查正则匹配
            match = cmd.match(message)
            if not match:
                continue
            
            # 检查触发条件
            if not self._check_trigger_conditions(cmd, user_id):
                self.logger.debug(f"指令 {cmd.name} 触发条件不满足")
                continue
            
            self.logger.info(f"触发自定义指令: {cmd.name} (用户: {user_id}, 群: {group_id})")
            
            try:
                # 发送群消息
                if cmd.group_message:
                    formatted_msg = self._format_message(cmd.group_message, match, user_id)
                    await self._send_group_message(websocket, group_id, formatted_msg)
                
                # 发送私聊消息
                if cmd.private_message:
                    formatted_msg = self._format_message(cmd.private_message, match, user_id)
                    await self._send_private_message(websocket, user_id, formatted_msg)
                
                # 执行服务器命令
                if cmd.server_command and server_executor:
                    formatted_cmd = self._format_message(cmd.server_command, match, user_id)
                    await server_executor(formatted_cmd)
                
                # 更新触发历史
                self._update_trigger_history(cmd)
                
            except Exception as e:
                self.logger.error(f"执行自定义指令 {cmd.name} 失败: {e}", exc_info=True)
            
            return True  # 已处理此指令
        
        return False  # 未匹配任何指令
    
    def _check_trigger_conditions(self, cmd: CustomCommand, user_id: int) -> bool:
        """检查触发条件"""
        today = datetime.date.today().isoformat()
        
        # 检查全局触发限制
        if cmd.trigger_limit > 0 and cmd.trigger_history['match_count'] >= cmd.trigger_limit:
            self.logger.debug(f"指令 {cmd.name} 达到触发次数限制")
            return False
        
        # 检查冷却时间
        current_time = time.time()
        if (cmd.trigger_cooldown > 0 and 
            current_time - cmd.trigger_history['last_trigger_time'] < cmd.trigger_cooldown):
            self.logger.debug(f"指令 {cmd.name} 冷却中")
            return False
        
        # 检查每日限制
        if cmd.daily_limit > 0:
            if today != cmd.trigger_history['last_reset_date']:
                cmd.trigger_history['trigger_times_today'] = 0
                cmd.trigger_history['last_reset_date'] = today
            
            if cmd.trigger_history['trigger_times_today'] >= cmd.daily_limit:
                self.logger.debug(f"指令 {cmd.name} 达到每日限制")
                return False
        
        return True
    
    def _format_message(self, template: str, match: re.Match, user_id: int) -> str:
        """格式化消息模板"""
        result = template
        
        # 替换基本占位符
        result = result.replace("{match}", match.group(0))
        result = result.replace("{user_id}", str(user_id))
        result = result.replace("{timestamp}", datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        
        # 替换正则组占位符
        for i, group in enumerate(match.groups(), 1):
            if group is not None:
                result = result.replace(f"{{group{i}}}", group)
        
        return result
    
    def _update_trigger_history(self, cmd: CustomCommand):
        """更新触发历史"""
        today = datetime.date.today().isoformat()
        
        if today != cmd.trigger_history['last_reset_date']:
            cmd.trigger_history['trigger_times_today'] = 0
            cmd.trigger_history['last_reset_date'] = today
        
        cmd.trigger_history['match_count'] += 1
        cmd.trigger_history['last_match_time'] = time.time()
        cmd.trigger_history['last_trigger_time'] = time.time()
        cmd.trigger_history['trigger_times_today'] += 1
    
    async def _send_group_message(self, websocket, group_id: int, message: str):
        """发送群消息"""
        try:
            if not websocket or websocket.closed:
                self.logger.warning("WebSocket连接已关闭，无法发送群消息")
                return
            
            import json
            request = {
                "action": "send_group_msg",
                "params": {
                    "group_id": group_id,
                    "message": message,
                    "auto_escape": False
                }
            }
            await websocket.send(json.dumps(request))
        except Exception as e:
            self.logger.error(f"发送群消息失败: {e}")
    
    async def _send_private_message(self, websocket, user_id: int, message: str):
        """发送私聊消息"""
        try:
            if not websocket or websocket.closed:
                self.logger.warning("WebSocket连接已关闭，无法发送私聊")
                return
            
            import json
            request = {
                "action": "send_private_msg",
                "params": {
                    "user_id": user_id,
                    "message": message,
                    "auto_escape": False
                }
            }
            await websocket.send(json.dumps(request))
        except Exception as e:
            self.logger.error(f"发送私聊消息失败: {e}")
    
    def get_commands_info(self) -> str:
        """获取所有指令信息"""
        if not self.commands:
            return "未配置任何自定义指令"
        
        lines = ["自定义指令列表", "=" * 60]
        
        for i, cmd in enumerate(self.commands, 1):
            status = "[启用]" if cmd.enabled else "[禁用]"
            admin_tag = "[仅管理员]" if cmd.admin_only else "[所有用户]"
            lines.append(f"\n指令 {i}: {status} {admin_tag} {cmd.name}")
            
            if cmd.description:
                lines.append(f"  描述: {cmd.description}")
            
            lines.append(f"  正则: {cmd.pattern}")
            
            if cmd.trigger_limit > 0:
                lines.append(f"  触发限制: {cmd.trigger_limit} 次")
            
            if cmd.trigger_cooldown > 0:
                lines.append(f"  冷却时间: {cmd.trigger_cooldown} 秒")
            
            if cmd.daily_limit > 0:
                lines.append(f"  每日限制: {cmd.daily_limit} 次")
            
            if cmd.group_message:
                msg_preview = cmd.group_message[:50] + "..." if len(cmd.group_message) > 50 else cmd.group_message
                lines.append(f"  群消息: {msg_preview}")
            
            if cmd.private_message:
                msg_preview = cmd.private_message[:50] + "..." if len(cmd.private_message) > 50 else cmd.private_message
                lines.append(f"  私聊: {msg_preview}")
            
            if cmd.server_command:
                cmd_preview = cmd.server_command[:50] + "..." if len(cmd.server_command) > 50 else cmd.server_command
                lines.append(f"  服务器命令: {cmd_preview}")
        
        lines.append("\n" + "=" * 60)
        return "\n".join(lines)
