import re
import logging
import asyncio
import time
import datetime
from typing import List, Dict, Any, Optional, Callable
from dataclasses import dataclass, field
from enum import Enum
from collections import defaultdict


class ConditionType(Enum):
    """条件类型枚举"""
    TIME_RANGE = "time_range"
    PLAYER_ONLINE = "player_online"
    SERVER_TPS = "server_tps"
    MEMORY_USAGE = "memory_usage"
    CUSTOM_FUNCTION = "custom_function"
    REPEAT_INTERVAL = "repeat_interval"
    WEEKDAY = "weekday"


@dataclass
class Condition:
    """执行条件"""
    type: ConditionType
    params: Dict[str, Any] = field(default_factory=dict)
    
    def __post_init__(self):
        if self.params is None:
            self.params = {}


@dataclass
class TriggerHistory:
    """触发历史记录"""
    match_count: int = 0
    last_match_time: float = 0
    last_trigger_time: float = 0
    trigger_times_today: int = 0  # 今天触发次数
    last_reset_date: str = ""  # 最后一次重置的日期


class ListenerRule:
    """自定义监听规则"""

    def __init__(self, 
                 name: str,
                 pattern: str,
                 enabled: bool = True,
                 qq_message: str = "",
                 server_command: str = "",
                 description: str = "",
                 case_sensitive: bool = False,
                 trigger_limit: int = 0,
                 trigger_cooldown: int = 0,
                 daily_limit: int = 0,
                 conditions: List[Dict] = None,
                 logger: logging.Logger = None):
        
        self.name = name
        self.pattern = pattern
        self.enabled = enabled
        self.qq_message = qq_message
        self.server_command = server_command
        self.description = description
        self.case_sensitive = case_sensitive
        self.trigger_limit = trigger_limit
        self.trigger_cooldown = trigger_cooldown
        self.daily_limit = daily_limit
        self.conditions = self._parse_conditions(conditions or [])
        self.logger = logger or logging.getLogger(__name__)
        
        # 触发历史
        self.history = TriggerHistory()
        
        # 验证规则
        self._validate_rule()
        
        # 编译正则表达式
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            self.compiled_pattern = re.compile(pattern, flags)
        except re.error as e:
            raise ValueError(f"规则 '{name}' 的正则表达式语法错误: {e}")
    
    def _validate_rule(self):
        """验证规则有效性"""
        if not self.pattern:
            raise ValueError(f"规则 '{self.name}' 的 pattern 不能为空")
        
        if not self.qq_message and not self.server_command:
            raise ValueError(f"规则 '{self.name}' 的 qq_message 和 server_command 不能同时为空")
    
    def _parse_conditions(self, conditions_config: List[Dict]) -> List[Condition]:
        """解析条件配置"""
        conditions = []
        for cond_config in conditions_config:
            try:
                cond_type = ConditionType(cond_config.get('type'))
                params = cond_config.get('params', {})
                conditions.append(Condition(cond_type, params))
            except ValueError:
                logging.warning(f"未知的条件类型: {cond_config.get('type')}")
        return conditions
    
    def match(self, text: str) -> Optional[re.Match]:
        """检查文本是否匹配规则"""
        if not self.enabled:
            return None
        return self.compiled_pattern.search(text)
        
    def can_trigger(self, context: Dict[str, Any] = None) -> bool:
        """检查是否可以触发规则"""
        if not self.enabled:
            self.logger.debug(f"[{self.name}] 规则已禁用")
            return False
        
        # 检查全局触发限制
        if self.trigger_limit > 0 and self.history.match_count >= self.trigger_limit:
            self.logger.debug(f"[{self.name}] 达到触发次数限制: {self.history.match_count}/{self.trigger_limit}")
            return False
        
        # 检查冷却时间
        current_time = time.time()
        if (self.trigger_cooldown > 0 and 
            current_time - self.history.last_trigger_time < self.trigger_cooldown):
            self.logger.debug(f"[{self.name}] 冷却中，剩余: {self.trigger_cooldown - (current_time - self.history.last_trigger_time):.1f}s")
            return False
        
        # 检查每日限制
        today = datetime.date.today().isoformat()
        if self.daily_limit > 0:
            if today != self.history.last_reset_date:
                self.history.trigger_times_today = 0
                self.history.last_reset_date = today
            
            if self.history.trigger_times_today >= self.daily_limit:
                self.logger.debug(f"[{self.name}] 达到每日触发限制: {self.history.trigger_times_today}/{self.daily_limit}")
                return False
        
        # 检查执行条件
        if not self._check_conditions(context or {}):
            self.logger.debug(f"[{self.name}] 条件检查失败")
            return False
        
        self.logger.debug(f"[{self.name}] 通过所有检查，允许触发")
        return True
    
    def _check_conditions(self, context: Dict[str, Any]) -> bool:
        """检查所有执行条件"""
        for condition in self.conditions:
            if not self._evaluate_condition(condition, context):
                return False
        return True
    
    def _evaluate_condition(self, condition: Condition, context: Dict[str, Any]) -> bool:
        """评估单个条件"""
        try:
            if condition.type == ConditionType.TIME_RANGE:
                return self._check_time_range(condition.params)
            elif condition.type == ConditionType.PLAYER_ONLINE:
                return self._check_player_online(condition.params, context)
            elif condition.type == ConditionType.SERVER_TPS:
                return self._check_server_tps(condition.params, context)
            elif condition.type == ConditionType.MEMORY_USAGE:
                return self._check_memory_usage(condition.params, context)
            elif condition.type == ConditionType.REPEAT_INTERVAL:
                return self._check_repeat_interval(condition.params)
            elif condition.type == ConditionType.WEEKDAY:
                return self._check_weekday(condition.params)
            elif condition.type == ConditionType.CUSTOM_FUNCTION:
                return self._check_custom_function(condition.params, context)
        except Exception as e:
            logging.error(f"评估条件 {condition.type} 时出错: {e}")
            return False
        return True
    
    def _check_time_range(self, params: Dict[str, Any]) -> bool:
        """检查时间范围条件"""
        now = datetime.datetime.now().time()
        start_str = params.get('start', '00:00')
        end_str = params.get('end', '23:59')
        
        try:
            start_time = datetime.datetime.strptime(start_str, '%H:%M').time()
            end_time = datetime.datetime.strptime(end_str, '%H:%M').time()
            
            if start_time <= end_time:
                return start_time <= now <= end_time
            else:  # 跨天的情况
                return now >= start_time or now <= end_time
        except ValueError:
            logging.error(f"时间格式错误: start={start_str}, end={end_str}")
            return True
    
    def _check_player_online(self, params: Dict[str, Any], context: Dict[str, Any]) -> bool:
        """检查玩家在线条件"""
        require_online = params.get('require', True)
        player_count = context.get('player_count', 0)
        
        if require_online:
            return player_count > 0
        else:
            return player_count == 0
    
    def _check_server_tps(self, params: Dict[str, Any], context: Dict[str, Any]) -> bool:
        """检查服务器TPS条件"""
        min_tps = params.get('min_tps', 10.0)
        max_tps = params.get('max_tps', 20.0)
        server_tps = context.get('server_tps', 20.0)
        
        return min_tps <= server_tps <= max_tps
    
    def _check_memory_usage(self, params: Dict[str, Any], context: Dict[str, Any]) -> bool:
        """检查内存使用率条件"""
        max_usage = params.get('max_usage', 80.0)
        memory_usage = context.get('memory_usage', 0.0)
        
        return memory_usage <= max_usage
    
    def _check_repeat_interval(self, params: Dict[str, Any]) -> bool:
        """检查重复间隔条件（防止频繁触发）"""
        interval = params.get('interval', 300)  # 默认5分钟
        
        time_since_last = time.time() - self.history.last_trigger_time
        return time_since_last >= interval
    
    def _check_weekday(self, params: Dict[str, Any]) -> bool:
        """检查星期几条件"""
        allowed_weekdays = params.get('weekdays', [0, 1, 2, 3, 4, 5, 6])  # 0=周一
        today = datetime.datetime.now().weekday()
        
        return today in allowed_weekdays
    
    def _check_custom_function(self, params: Dict[str, Any], context: Dict[str, Any]) -> bool:
        """检查自定义函数条件"""
        return True
    
    def update_history(self, triggered: bool = True):
        """更新触发历史"""
        # 检查是否需要重置每日计数
        today = datetime.date.today().isoformat()
        if today != self.history.last_reset_date:
            self.history.trigger_times_today = 0
            self.history.last_reset_date = today
        
        self.history.match_count += 1
        self.history.last_match_time = time.time()
        
        if triggered:
            self.history.last_trigger_time = time.time()
            self.history.trigger_times_today += 1  # 先增加计数，再使用
    
    def format_message(self, 
                      match: re.Match, 
                      template: str, 
                      context: Dict[str, Any] = None) -> str:
        """格式化消息模板"""
        if not template:
            return template
        
        context = context or {}
        result = template
        
        # 基础占位符
        result = result.replace("{match}", match.group(0))
        result = result.replace("{match_full}", match.group(0))
        result = result.replace("{rule_name}", self.name)
        result = result.replace("{match_count}", str(self.history.match_count))
        result = result.replace("{trigger_today}", str(self.history.trigger_times_today))
        
        if self.history.last_match_time > 0:
            last_time = datetime.datetime.fromtimestamp(self.history.last_match_time)
            result = result.replace("{prev_match_time}", last_time.strftime('%Y-%m-%d %H:%M:%S'))
        else:
            result = result.replace("{prev_match_time}", "从未")
        
        # 系统信息占位符
        now = datetime.datetime.now()
        result = result.replace("{timestamp}", now.strftime('%Y-%m-%d %H:%M:%S'))
        result = result.replace("{date}", now.strftime('%Y-%m-%d'))
        result = result.replace("{time}", now.strftime('%H:%M:%S'))
        result = result.replace("{weekday}", ['周一', '周二', '周三', '周四', '周五', '周六', '周日'][now.weekday()])
        result = result.replace("{server_tps}", str(context.get('server_tps', 'N/A')))
        result = result.replace("{player_count}", str(context.get('player_count', 0)))
        result = result.replace("{memory_usage}", str(context.get('memory_usage', 'N/A')))
        
        # 正则表达式捕获组占位符
        for i, group in enumerate(match.groups()):
            if group is not None:
                result = result.replace(f"{{group{i+1}}}", group)
        
        # 函数式占位符处理
        result = self._process_functional_placeholders(result, match, context)
        
        return result
    
    def _process_functional_placeholders(self, text: str, match: re.Match, context: Dict[str, Any]) -> str:
        """处理函数式占位符"""
        import re as re_module
        
        func_pattern = r'\{(\w+)\((.*?)\)\}'
        
        def replace_function(match_func):
            func_name = match_func.group(1)
            args_str = match_func.group(2)
            
            args = []
            for arg in self._split_function_args(args_str):
                arg = arg.strip()
                if arg.startswith('"') and arg.endswith('"'):
                    args.append(arg[1:-1])
                elif arg.startswith("'") and arg.endswith("'"):
                    args.append(arg[1:-1])
                else:
                    args.append(self._resolve_variable(arg, match, context))
            
            result = self._execute_function(func_name, args)
            return str(result) if result is not None else ""
        
        return re_module.sub(func_pattern, replace_function, text)
    
    def _split_function_args(self, args_str: str) -> List[str]:
        """分割函数参数，处理嵌套括号"""
        args = []
        current_arg = ""
        bracket_depth = 0
        quote_char = None
        
        for char in args_str:
            if char in ['"', "'"] and bracket_depth == 0:
                if quote_char is None:
                    quote_char = char
                elif quote_char == char:
                    quote_char = None
                current_arg += char
            elif char == '(':
                bracket_depth += 1
                current_arg += char
            elif char == ')':
                bracket_depth -= 1
                current_arg += char
            elif char == ',' and bracket_depth == 0 and quote_char is None:
                args.append(current_arg.strip())
                current_arg = ""
            else:
                current_arg += char
        
        if current_arg.strip():
            args.append(current_arg.strip())
        
        return args
    
    def _resolve_variable(self, var_name: str, match: re.Match, context: Dict[str, Any]) -> str:
        """解析变量值"""
        group_match = re.match(r'^group(\d+)$', var_name)
        if group_match:
            group_index = int(group_match.group(1)) - 1
            if 0 <= group_index < len(match.groups()):
                return match.groups()[group_index] or ""
            return ""
        
        if var_name == "match":
            return match.group(0)
        
        if var_name in context:
            return str(context[var_name])
        
        system_vars = {
            "timestamp": datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            "rule_name": self.name,
            "match_count": str(self.history.match_count),
            "trigger_today": str(self.history.trigger_times_today)
        }
        if var_name in system_vars:
            return system_vars[var_name]
        
        return var_name
    
    def _execute_function(self, func_name: str, args: List[str]) -> Any:
        """执行函数"""
        func_map = {
            'upper': lambda x: str(x).upper(),
            'lower': lambda x: str(x).lower(),
            'substr': lambda s, start, end: str(s)[int(start):int(end)],
            'repeat': lambda s, n: str(s) * int(n),
            'replace': lambda s, old, new: str(s).replace(old, new),
            'if': lambda condition, true_val, false_val: true_val if condition else false_val,
            'length': lambda s: len(str(s)),
            'trim': lambda s: str(s).strip(),
            'contains': lambda s, substr: substr in str(s),
            'startsWith': lambda s, prefix: str(s).startswith(prefix),
            'endsWith': lambda s, suffix: str(s).endswith(suffix),
            'split': lambda s, sep: str(s).split(sep),
            'join': lambda sep, s: sep.join(s) if isinstance(s, list) else sep.join(str(s)),
        }
        
        if func_name in func_map:
            try:
                return func_map[func_name](*args)
            except Exception as e:
                logging.error(f"执行函数 {func_name} 时出错: {e}")
                return None
        
        logging.warning(f"未知函数: {func_name}")
        return None


class CustomMessageListener:
    """自定义消息监听器"""

    def __init__(self, config_manager, logger: logging.Logger):
        self.config_manager = config_manager
        self.logger = logger
        self.rules: List[ListenerRule] = []
        self.context_providers: List[Callable] = []
        self.rule_stats: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"total": 0, "errors": 0})
        
        self._register_default_context_providers()
        self._load_rules_from_config()
    
    def _register_default_context_providers(self):
        """注册默认的上下文提供器"""
        self.context_providers.append(self._get_system_context)
    
    def _get_system_context(self) -> Dict[str, Any]:
        """获取系统上下文信息"""
        try:
            context = {
                'timestamp': time.time(),
                'server_tps': 20.0,
                'player_count': 0,
                'memory_usage': 0.0,
            }
            return context
        except Exception as e:
            self.logger.error(f"获取系统上下文失败: {e}")
            return {}
    
    def _load_rules_from_config(self):
        """从配置文件加载规则"""
        try:
            rules_config = self.config_manager.config.get('custom_listeners', {}).get('rules', [])
            
            if not rules_config:
                self.logger.info("未配置自定义监听规则")
                self.rules = []
                return
            
            new_rules = []
            for rule_config in rules_config:
                try:
                    rule = ListenerRule(
                        name=rule_config.get('name', f'rule_{len(new_rules)}'),
                        pattern=rule_config.get('pattern', ''),
                        enabled=rule_config.get('enabled', True),
                        qq_message=rule_config.get('qq_message', ''),
                        server_command=rule_config.get('server_command', ''),
                        description=rule_config.get('description', ''),
                        case_sensitive=rule_config.get('case_sensitive', False),
                        trigger_limit=rule_config.get('trigger_limit', 0),
                        trigger_cooldown=rule_config.get('trigger_cooldown', 0),
                        daily_limit=rule_config.get('daily_limit', 0),
                        conditions=rule_config.get('conditions', []),
                        logger=self.logger
                    )
                    new_rules.append(rule)
                    self.logger.info(f"已加载监听规则: {rule.name} [{'可用' if rule.enabled else '禁用'}]")
                    
                except ValueError as e:
                    self.logger.error(f"加载监听规则失败: {e}")
                    continue
            
            self.rules = new_rules
            self.logger.info(f"共加载 {len(self.rules)} 个自定义监听规则")
            
        except Exception as e:
            self.logger.error(f"加载自定义监听规则失败: {e}", exc_info=True)
    
    def reload_rules(self):
        """重新加载规则"""
        self.rules.clear()
        self._load_rules_from_config()
    
    def register_context_provider(self, provider: Callable[[], Dict[str, Any]]):
        """注册上下文提供器"""
        self.context_providers.append(provider)
    
    def get_context(self) -> Dict[str, Any]:
        """获取完整的上下文信息"""
        context = {}
        for provider in self.context_providers:
            try:
                provider_context = provider()
                if provider_context:
                    context.update(provider_context)
            except Exception as e:
                self.logger.error(f"上下文提供器执行失败: {e}")
        return context
    
    async def process_message(self, 
                         log_line: str,
                         websocket,
                         group_ids: List[int],
                         server_executor=None,
                         context=None) -> List[str]:
        """
        处理消息并匹配规则
        """
        matched_rules = []
        
        # 优先使用传入的context，如果没有才生成
        if context is None:
            context = self.get_context()
        else:
            # 合并传入的context和系统context
            sys_context = self._get_system_context()
            sys_context.update(context)
            context = sys_context
        
        for rule in self.rules:
            try:
                match = rule.match(log_line)
                if match:
                    rule.update_history(triggered=False)
                    self.rule_stats[rule.name]["total"] += 1
                    
                    if rule.can_trigger(context):
                        matched_rules.append(rule.name)
                        
                        self.logger.info(f"消息匹配规则: {rule.name}, 内容: {log_line[:100]}")
                        
                        if rule.qq_message:
                            qq_message = rule.format_message(match, rule.qq_message, context)
                            await self._send_qq_messages(websocket, group_ids, qq_message)
                        
                        if rule.server_command:
                            server_command = rule.format_message(match, rule.server_command, context)
                            if server_executor:
                                await self._execute_server_command(server_executor, server_command)
                        
                        rule.update_history(triggered=True)
                    
            except Exception as e:
                self.logger.error(f"处理规则 {rule.name} 时出错: {e}", exc_info=True)
                self.rule_stats[rule.name]["errors"] += 1
                continue
        
        return matched_rules
    
    async def _send_qq_messages(self, websocket, group_ids: List[int], message: str):
        """向QQ群发送消息"""
        if not websocket or websocket.closed:
            self.logger.warning("无法发送QQ消息: WebSocket连接已关闭")
            return
        
        try:
            import json
            
            for group_id in group_ids:
                request = {
                    "action": "send_group_msg",
                    "echo": f"listener_msg_{int(time.time() * 1000)}",
                    "params": {
                        "group_id": group_id,
                        "message": message,
                        "auto_escape": False
                    }
                }
                
                await websocket.send(json.dumps(request))
                self.logger.debug(f"已向群 {group_id} 发送消息")
                
        except Exception as e:
            self.logger.error(f"发送QQ消息失败: {e}", exc_info=True)
    
    async def _execute_server_command(self, server_executor, command: str):
        """执行服务器命令"""
        try:
            self.logger.info(f"执行监听触发的服务器命令: {command}")
            
            if asyncio.iscoroutinefunction(server_executor):
                await server_executor(command)
            else:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, server_executor, command)
            
            self.logger.info(f"服务器命令执行完成: {command}")
            
        except Exception as e:
            self.logger.error(f"执行服务器命令失败: {e}", exc_info=True)
    
    def get_rules_info(self) -> str:
        """获取所有规则的信息"""
        if not self.rules:
            return "未配置任何监听规则"
        
        lines = ["自定义监听规则列表", "=" * 10]
        
        for i, rule in enumerate(self.rules, 1):
            status = "[启用]" if rule.enabled else "[禁用]"
            case_info = "[大小写敏感]" if rule.case_sensitive else "[忽略大小写]"
            
            lines.append(f"\n规则 {i}: {status} {case_info} {rule.name}")
            
            if rule.description:
                lines.append(f"  描述: {rule.description}")
            
            lines.append(f"  正则: {rule.pattern}")
            
            if rule.trigger_limit > 0:
                lines.append(f"  触发限制: {rule.trigger_limit} 次 (已触发 {rule.history.match_count} 次)")
            
            if rule.trigger_cooldown > 0:
                lines.append(f"  冷却时间: {rule.trigger_cooldown} 秒")
            
            if rule.daily_limit > 0:
                lines.append(f"  每日限制: {rule.daily_limit} 次 (今天已触发 {rule.history.trigger_times_today} 次)")
            
            if rule.conditions:
                lines.append("  执行条件:")
                for condition in rule.conditions:
                    lines.append(f"    - {condition.type.value}: {condition.params}")
            
            if rule.qq_message:
                msg_preview = rule.qq_message[:50] + "..." if len(rule.qq_message) > 50 else rule.qq_message
                lines.append(f"  QQ消息: {msg_preview}")
            
            if rule.server_command:
                cmd_preview = rule.server_command[:50] + "..." if len(rule.server_command) > 50 else rule.server_command
                lines.append(f"  服务器命令: {cmd_preview}")
            
            lines.append(f"  统计: 匹配 {rule.history.match_count} 次")
            if rule.history.last_match_time > 0:
                last_time = datetime.datetime.fromtimestamp(rule.history.last_match_time)
                lines.append(f"  最后匹配: {last_time.strftime('%Y-%m-%d %H:%M:%S')}")
        
        lines.append("\n" + "=" * 80)
        lines.append("占位符说明:")
        lines.append("  {timestamp} - 当前时间")
        lines.append("  {date} - 日期")
        lines.append("  {time} - 时间")
        lines.append("  {weekday} - 星期几")
        lines.append("  {server_tps} - 服务器TPS")
        lines.append("  {player_count} - 在线人数")
        lines.append("  {memory_usage} - 内存使用率")
        lines.append("  {rule_name} - 规则名称")
        lines.append("  {match_count} - 总匹配次数")
        lines.append("  {trigger_today} - 今天触发次数")
        lines.append("  {prev_match_time} - 上次匹配时间")
        lines.append("  {group1}, {group2}... - 正则捕获组")
        lines.append("\n函数式占位符:")
        lines.append("  {upper(text)} - 转大写")
        lines.append("  {lower(text)} - 转小写")
        lines.append("  {substr(text, start, end)} - 截取子串")
        lines.append("  {replace(text, old, new)} - 替换文本")
        lines.append("  {if(condition, true, false)} - 条件判断")
        lines.append("  {length(text)} - 文本长度")
        lines.append("  {contains(text, substr)} - 检查包含")
        lines.append("  {trim(text)} - 去除空格")
        lines.append("=" * 80)
        
        return "\n".join(lines)
    
    def get_rule_stats(self) -> Dict[str, Any]:
        """获取规则统计信息"""
        stats = {
            'total_rules': len(self.rules),
            'enabled_rules': len([r for r in self.rules if r.enabled]),
            'total_matches': sum(r.history.match_count for r in self.rules),
            'rules': {}
        }
        
        for rule in self.rules:
            stats['rules'][rule.name] = {
                'enabled': rule.enabled,
                'match_count': rule.history.match_count,
                'trigger_count': rule.history.trigger_times_today,
                'last_match_time': rule.history.last_match_time,
                'last_trigger_time': rule.history.last_trigger_time,
                'errors': self.rule_stats[rule.name]['errors']
            }
        
        return stats