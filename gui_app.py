import ast
import asyncio
import json
import copy
import hashlib
import logging
import locale
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import yaml
from version import APP_NAME, APP_VERSION

try:
    import psutil
except ImportError:
    psutil = None

try:
    import flet as ft
except ImportError as exc:
    raise SystemExit("未安装 Flet，请先执行: python -m pip install \"flet[all]\"") from exc


APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
BUNDLED_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))
CONFIG_PATH = APP_DIR / "config.yml"
PLUGIN_DIR = APP_DIR / "plugins"
DEFAULT_SERVER_CONFIG_DIR = APP_DIR / "servers"
TERMINAL_COMMANDS_PATH = APP_DIR / "gui_terminal_commands.json"
APP_ICON_PATH = Path(__file__).resolve().parent / "assets" / "app_icon.ico"
SERVER_REQUIRED_SECTIONS = (
    "msmp",
    "rcon",
    "qq",
    "server",
    "commands",
    "notifications",
    "advanced",
    "scheduled_tasks",
    "custom_commands",
    "custom_listeners",
)
PLUGIN_REPO_API = "https://api.github.com/repos/intellectmind/MSMP_QQBot-Plugins/contents"
PLUGIN_REPO_HTML = "https://github.com/intellectmind/MSMP_QQBot-Plugins"
PLUGIN_RAW_BASE = "https://raw.githubusercontent.com/intellectmind/MSMP_QQBot-Plugins/main"
ANSI_COLOR_PATTERN = re.compile(r"\x1b\[([0-9;]*)m")
ANSI_CONTROL_PATTERN = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
MC_COLOR_MAP = {
    "0": "#000000",
    "1": "#0000aa",
    "2": "#00aa00",
    "3": "#00aaaa",
    "4": "#aa0000",
    "5": "#aa00aa",
    "6": "#ffaa00",
    "7": "#aaaaaa",
    "8": "#555555",
    "9": "#5555ff",
    "a": "#55ff55",
    "b": "#55ffff",
    "c": "#ff5555",
    "d": "#ff55ff",
    "e": "#ffff55",
    "f": "#ffffff",
}
ANSI_COLOR_MAP = {
    "30": "#111111",
    "31": "#ff5555",
    "32": "#55ff55",
    "33": "#ffff55",
    "34": "#5555ff",
    "35": "#ff55ff",
    "36": "#55ffff",
    "37": "#dddddd",
    "90": "#777777",
    "91": "#ff7777",
    "92": "#77ff77",
    "93": "#ffff77",
    "94": "#7777ff",
    "95": "#ff77ff",
    "96": "#77ffff",
    "97": "#ffffff",
}


def _ensure_runtime_resources():
    """PyInstaller onefile 首次运行时，把内置默认资源复制到 exe 同目录。"""
    if not getattr(sys, "frozen", False):
        return

    bundled_config = BUNDLED_DIR / "config.yml"
    if not CONFIG_PATH.exists() and bundled_config.exists():
        shutil.copy2(bundled_config, CONFIG_PATH)

CONFIG_HELP = {
    "name": "服务器名称。可在 QQ 命令中作为服务器选择器使用，例如 start server1。",
    "debug": "调试模式。开启后输出更详细日志，排查问题时使用，正常运行建议关闭。",
    "websocket": "全局 OneBot 反向 WebSocket 监听配置，只保留在 config.yml，不写入单个服务器配置。",
    "websocket.port": "WebSocket 监听端口，QQ 机器人客户端反向连接到此端口。",
    "websocket.token": "WebSocket 鉴权令牌。启用鉴权后，客户端请求需要携带相同 token。",
    "websocket.auth_enabled": "是否启用 WebSocket 鉴权。公网或多人环境建议开启。",
    "msmp": "Minecraft Server Management Protocol 连接配置，适用于新版服务端远程管理。",
    "msmp.enabled": "是否启用 MSMP。启用后优先使用 MSMP 查询状态、玩家列表和执行管理命令。",
    "msmp.host": "MSMP 服务端地址。通常是 localhost，远程服务端填写对应 IP 或域名。",
    "msmp.port": "MSMP 端口，需要和服务端 management-server-port 保持一致。",
    "msmp.password": "MSMP 认证令牌，需要和服务端 management-server-secret 保持一致。",
    "rcon": "RCON 连接配置，适用于旧版本或需要执行传统控制台命令的场景。",
    "rcon.enabled": "是否启用 RCON。多服务器命令路由会使用每个服务器自己的 RCON 配置。",
    "rcon.host": "RCON 服务端地址。通常是 localhost，远程服务端填写对应 IP 或域名。",
    "rcon.port": "RCON 端口，需要和 server.properties 的 rcon.port 一致。",
    "rcon.password": "RCON 密码，需要和 server.properties 的 rcon.password 一致。",
    "qq": "该 MC 服务器对应的 QQ 群、管理员和欢迎消息配置。",
    "qq.groups": "该服务器响应的 QQ 群号列表。命令未指定服务器时，会按群号选择对应服务器。",
    "qq.admins": "该服务器管理员 QQ 列表。管理员可以执行 start、stop、插件管理等管理命令。",
    "qq.welcome_new_members": "是否在该服务器对应群内欢迎新成员。",
    "qq.welcome_message": "欢迎消息模板，支持 {at} @新成员、{user_id}、{group_id}。",
    "server": "服务器本地启动与进程守护配置。",
    "server.start_script": "服务器启动脚本路径，支持 .bat/.cmd/.sh。路径使用 /，不要写编码参数。",
    "server.working_directory": "工作目录。留空则使用启动脚本所在目录；路径使用 /。",
    "server.startup_timeout": "服务器启动超时时间，单位秒。超过后仍未启动成功会按失败处理。",
    "server.auto_restart_on_crash": "服务器异常停止后是否自动启动。手动 kill 指令不受此项影响。",
    "server.crash_restart_delay": "异常停止后的重启延迟，单位秒。",
    "server.log_idle_restart_enabled": "GUI 显示用开关。开启后使用日志假死超时秒数；关闭后保存为 0。",
    "server.log_idle_restart_timeout": "MC 日志多久没更新就强制 kill 并重启，单位秒；0 表示关闭假死检测。",
    "server_config_dir": "多服务器独立配置目录。默认读取 servers/*.yml，每个文件是一台 MC 服务器配置。",
    "commands": "QQ 命令行为配置，包括 TPS 命令、正则解析和命令开关。",
    "commands.tps_command": "群内使用 tps 命令时真正发送到 MC 控制台的指令，可按服务器类型调整。",
    "commands.tps_regex": "从服务器返回文本中提取 TPS 数值的 Python 正则表达式。",
    "commands.tps_group_index": "TPS 正则捕获组索引，从 1 开始。用于选择第几个括号捕获的数值。",
    "commands.tps_show_raw_output": "TPS 查询结果是否显示原始服务器返回值。true 显示解析值和原始文本，false 只显示解析值。",
    "commands.enabled_commands": "基础命令开关。管理员不受此限制，普通用户按这里控制是否可用。",
    "commands.enabled_commands.list": "玩家列表命令开关。",
    "commands.enabled_commands.tps": "TPS 查询命令开关。",
    "commands.enabled_commands.rules": "规则查询命令开关。",
    "commands.enabled_commands.status": "服务器状态命令开关。",
    "commands.enabled_commands.help": "帮助命令开关。",
    "commands.enabled_admin_commands": "是否向非管理员开放管理员命令。管理员始终可以使用管理员命令。",
    "commands.enabled_admin_commands.start": "是否允许普通成员使用启动服务器命令。管理员始终可用。",
    "commands.enabled_admin_commands.stop": "是否允许普通成员使用停止服务器命令。管理员始终可用。",
    "commands.enabled_admin_commands.kill": "是否允许普通成员使用强制停止服务器命令。管理员始终可用。",
    "commands.enabled_admin_commands.reload": "是否允许普通成员使用重载配置命令。管理员始终可用。",
    "commands.enabled_admin_commands.log": "是否允许普通成员查看服务器日志。管理员始终可用。",
    "commands.enabled_admin_commands.reconnect": "是否允许普通成员重连所有服务。管理员始终可用。",
    "commands.enabled_admin_commands.reconnect_msmp": "是否允许普通成员重连 MSMP。管理员始终可用。",
    "commands.enabled_admin_commands.reconnect_rcon": "是否允许普通成员重连 RCON。管理员始终可用。",
    "commands.enabled_admin_commands.crash": "是否允许普通成员查看崩溃报告。管理员始终可用。",
    "commands.enabled_admin_commands.sysinfo": "是否允许普通成员查看系统信息。管理员始终可用。",
    "commands.enabled_admin_commands.disk": "是否允许普通成员查看磁盘信息。管理员始终可用。",
    "commands.enabled_admin_commands.process": "是否允许普通成员查看进程信息。管理员始终可用。",
    "commands.enabled_admin_commands.network": "是否允许普通成员查看网络信息。管理员始终可用。",
    "commands.enabled_admin_commands.listeners": "是否允许普通成员查看监听规则。管理员始终可用。",
    "notifications": "通知开关配置，例如服务端启动关闭、玩家进出服、日志消息、区块监控。",
    "notifications.server_events": "是否发送服务器事件通知，例如启动、关闭、崩溃重启。",
    "notifications.player_events": "是否发送玩家加入、离开等玩家事件通知。",
    "notifications.log_messages": "是否在控制台显示详细消息日志。",
    "notifications.chunk_monitor": "ChunkMonitor 插件通知配置，需要服务端安装并启用 ChunkMonitor 控制台输出。",
    "notifications.chunk_monitor.enabled": "是否启用区块监控通知。",
    "notifications.chunk_monitor.notify_admins": "区块监控告警是否私聊发送给管理员。",
    "notifications.chunk_monitor.notify_groups": "区块监控告警是否发送到该服务器对应 QQ 群。",
    "advanced": "高级运行参数，影响重连、心跳、冷却、消息长度和缓存。",
    "advanced.reconnect_interval": "MSMP/RCON 重连间隔，单位秒。",
    "advanced.heartbeat_interval": "心跳间隔，单位秒。",
    "advanced.command_cooldown": "命令冷却时间，单位秒。防止同一用户高频刷命令。",
    "advanced.max_message_length": "机器人单条消息最大长度，超出会截断或分段处理。",
    "advanced.player_list_cache_ttl": "玩家列表缓存时间，单位秒。降低频繁 list 查询对服务端的压力。",
    "advanced.max_server_logs": "内存中最多保留的服务器日志行数。",
    "scheduled_tasks": "定时启动、停止、重启和通知任务配置。",
    "scheduled_tasks.enabled": "是否启用定时任务总开关。",
    "scheduled_tasks.auto_start": "定时启动服务器配置。",
    "scheduled_tasks.auto_start.enabled": "是否启用定时启动。",
    "scheduled_tasks.auto_start.times": "启动时间列表，24 小时制 HH:MM，可配置多个时间。",
    "scheduled_tasks.auto_start.weekdays": "执行星期列表，0=周一，1=周二，直到 6=周日。",
    "scheduled_tasks.auto_start.pre_notify_seconds": "启动前通知秒数，0 表示不提前通知。",
    "scheduled_tasks.auto_start.notify_message": "启动前通知消息，支持 {countdown} 秒数占位符。",
    "scheduled_tasks.auto_stop": "定时关闭服务器配置。",
    "scheduled_tasks.auto_stop.enabled": "是否启用定时关闭。",
    "scheduled_tasks.auto_stop.times": "关闭时间列表，24 小时制 HH:MM，可配置多个时间。",
    "scheduled_tasks.auto_stop.weekdays": "执行星期列表，0=周一，1=周二，直到 6=周日。",
    "scheduled_tasks.auto_stop.warning_before_seconds": "关闭前警告秒数，0 表示不提前警告。",
    "scheduled_tasks.auto_stop.first_warning": "第一次关闭警告消息，支持 {countdown}。",
    "scheduled_tasks.auto_stop.second_warning": "关闭前 1 分钟提示消息。",
    "scheduled_tasks.auto_stop.immediate_message": "立即关闭时发送的消息。",
    "scheduled_tasks.auto_restart": "定时重启服务器配置。",
    "scheduled_tasks.auto_restart.enabled": "是否启用定时重启。",
    "scheduled_tasks.auto_restart.times": "重启时间列表，24 小时制 HH:MM，可配置多个时间。",
    "scheduled_tasks.auto_restart.weekdays": "执行星期列表，0=周一，1=周二，直到 6=周日。",
    "scheduled_tasks.auto_restart.warning_before_seconds": "重启前警告秒数，0 表示不提前警告。",
    "scheduled_tasks.auto_restart.first_warning": "第一次重启警告消息，支持 {countdown}。",
    "scheduled_tasks.auto_restart.second_warning": "重启前 1 分钟提示消息。",
    "scheduled_tasks.auto_restart.immediate_message": "立即重启时发送的消息。",
    "scheduled_tasks.auto_restart.wait_before_startup": "停止后再次启动前等待时间，单位秒。",
    "scheduled_tasks.auto_restart.restart_success_message": "重启成功后发送的消息。",
    "custom_commands": "自定义 QQ 指令配置。可根据群消息自动发群消息、私聊或执行服务器命令。",
    "custom_commands.enabled": "是否启用自定义指令功能。",
    "custom_commands.rules": "自定义指令规则列表，可以添加多个规则。",
    "custom_commands.rules.name": "指令唯一标识符，用于日志记录和管理。",
    "custom_commands.rules.description": "指令描述，展示给管理员理解规则用途。",
    "custom_commands.rules.enabled": "是否启用该规则。",
    "custom_commands.rules.admin_only": "是否仅管理员可触发该自定义指令。",
    "custom_commands.rules.pattern": "用于匹配群消息的 Python 正则表达式。",
    "custom_commands.rules.case_sensitive": "正则匹配是否大小写敏感。",
    "custom_commands.rules.trigger_limit": "全局触发次数限制，0 表示无限制。",
    "custom_commands.rules.trigger_cooldown": "触发冷却时间，单位秒，防止频繁触发。",
    "custom_commands.rules.daily_limit": "每天触发次数限制，0 表示无限制。",
    "custom_commands.rules.group_message": "触发时发送到群的消息，空表示不发送。",
    "custom_commands.rules.server_command": "触发时执行的服务器命令，空表示不执行。",
    "custom_commands.rules.private_message": "触发时私聊发送给触发者的消息，空表示不发送。",
    "custom_commands.rules.conditions": "自定义指令执行条件列表，当前作为预留/扩展字段。",
    "custom_listeners": "自定义服务端日志监听规则配置。通过正则监听 MC 日志，匹配后发 QQ 消息或执行服务端命令。",
    "custom_listeners.enabled": "是否启用自定义监听功能。",
    "custom_listeners.rules": "监听规则列表，可以添加多个规则。",
    "custom_listeners.rules.name": "规则唯一标识符，用于日志记录和管理。",
    "custom_listeners.rules.description": "规则描述，展示规则用途。",
    "custom_listeners.rules.enabled": "是否启用该监听规则。",
    "custom_listeners.rules.pattern": "用于匹配服务器日志的 Python 正则表达式。",
    "custom_listeners.rules.case_sensitive": "正则匹配是否大小写敏感。",
    "custom_listeners.rules.trigger_limit": "全局触发次数限制，0 表示无限制。",
    "custom_listeners.rules.trigger_cooldown": "触发冷却时间，单位秒，0 表示无冷却。",
    "custom_listeners.rules.daily_limit": "每日触发次数限制，0 表示无限制。",
    "custom_listeners.rules.conditions": "执行条件列表。支持 time_range、player_online、server_tps、memory_usage、repeat_interval、weekday。",
    "custom_listeners.rules.conditions.type": "条件类型，例如 time_range、player_online、server_tps、memory_usage、repeat_interval、weekday。",
    "custom_listeners.rules.conditions.params": "条件参数对象，不同条件类型需要不同参数。",
    "custom_listeners.rules.conditions.params.start": "time_range 开始时间，格式 HH:MM。",
    "custom_listeners.rules.conditions.params.end": "time_range 结束时间，格式 HH:MM。",
    "custom_listeners.rules.conditions.params.require": "player_online 条件是否要求有玩家在线，true=需要在线，false=需要无人在线。",
    "custom_listeners.rules.conditions.params.min_tps": "server_tps 条件最小 TPS。",
    "custom_listeners.rules.conditions.params.max_tps": "server_tps 条件最大 TPS。",
    "custom_listeners.rules.conditions.params.max_usage": "memory_usage 条件最大内存使用率百分比。",
    "custom_listeners.rules.conditions.params.interval": "repeat_interval 条件最小触发间隔，单位秒。",
    "custom_listeners.rules.conditions.params.weekdays": "weekday 条件允许触发的星期列表，0=周一，6=周日。",
    "custom_listeners.rules.qq_message": "匹配时发送到 QQ 群的消息。qq_message 和 server_command 至少配置一个。",
    "custom_listeners.rules.server_command": "匹配时向服务端执行的指令。qq_message 和 server_command 至少配置一个。",
    "plugins": "插件相关配置和插件管理入口。",
}

PLUGIN_CONFIG_HELP = {
    "enabled": "是否在当前目标服务器启用此插件。关闭后该服务器不会执行此插件命令和事件。",
    "max_bindings_per_qq": "每个 QQ 账号在当前服务器最多能绑定的 Minecraft 游戏 ID 数量。",
    "verify_timeout": "绑定验证码有效时间，单位秒。",
    "verify_code_length": "绑定验证码数字长度。",
    "chat_message_pattern": "从 MC 日志中识别玩家聊天消息的正则表达式。",
    "features": "MC 与 QQ 消息同步的功能开关和目标群配置。",
    "features.mc_auto_sync_to_qq.enabled": "是否自动把 MC 玩家聊天同步到 QQ 群。",
    "features.mc_auto_sync_to_qq.group_ids": "自动同步到 QQ 的群号列表；当前多服务器模式下通常优先使用服务器配置里的 qq.groups。",
    "features.mc_manual_sync_to_qq.enabled": "是否允许玩家在 MC 内用 qq 指令主动发送消息到 QQ。",
    "features.mc_manual_sync_to_qq.group_ids": "MC 主动发送到 QQ 的目标群号列表。",
    "features.qq_manual_to_mc.enabled": "是否允许 QQ 用户用 mc 指令发送消息到 MC。",
    "features.qq_manual_to_mc.group_ids": "允许 QQ 转发到 MC 的群号列表。",
    "message_format.mc_auto_to_qq": "MC 自动同步到 QQ 时的消息模板，支持 {player} 和 {message}。",
    "message_format.mc_manual_to_qq": "MC 玩家主动同步到 QQ 时的消息模板，支持 {player} 和 {message}。",
    "message_format.qq_manual_to_mc": "QQ 消息发送到 MC 时的消息模板，支持 {nickname} 和 {message}。",
    "qq_commands.mc_command_prefix": "QQ群内发送到 MC 的命令前缀。",
    "mc_commands.qq_command_prefix": "MC 游戏内发送到 QQ 的命令前缀。",
    "blacklist.players": "禁止同步的 MC 玩家名列表。",
    "blacklist.users": "禁止同步的 QQ 用户号列表。",
    "ai_api_url": "白名单审核 AI 接口地址。",
    "ai_api_key": "白名单审核 AI 接口密钥。",
    "ai_model": "白名单审核使用的 AI 模型名称。",
    "allowed_groups": "允许使用白名单审核的 QQ 群列表；当前多服务器模式下建议用服务器配置里的 qq.groups 控制。",
    "cooldown_seconds": "白名单审核失败或冷却后的重试间隔，单位秒。",
    "pass_score": "白名单审核及格分数。",
    "question_count": "白名单审核题目数量。",
    "ai_timeout": "AI 请求超时时间，单位秒。",
    "answer_timeout": "每道审核题目的答题超时时间，单位秒。",
    "use_ai_questions": "是否使用 AI 动态生成审核题目；关闭后使用 default_questions。",
    "max_whitelist_per_qq": "每个 QQ 账号在当前服务器最多能绑定的白名单游戏 ID 数量。",
    "question_prompt": "AI 生成和评分白名单审核题目的提示词模板。",
    "default_questions": "关闭 AI 出题或 AI 出题失败时使用的默认题库。",
    "custom_whitelist_commands.add_command": "审核通过后执行的添加白名单命令模板，支持 {player}。",
    "custom_whitelist_commands.remove_command": "移除白名单时执行的命令模板，支持 {player}。",
    "custom_whitelist_commands.list_command": "查询服务器白名单列表的命令。",
    "custom_whitelist_commands.on_command": "开启服务器白名单的命令。",
    "custom_whitelist_commands.off_command": "关闭服务器白名单的命令。",
    "custom_whitelist_commands.reload_command": "重载服务器白名单的命令。",
    "allowed_dimensions": "区块工具允许操作的维度列表，例如 overworld、nether、end。",
    "require_confirmation": "执行危险区块删除操作前是否需要二次确认。",
    "backup_before_delete": "删除区块前是否自动备份。",
    "confirmation_timeout": "删除确认等待时间，单位秒。",
}


class FletGuiApp:
    def __init__(self, page: ft.Page):
        self.page = page
        self.config_data = {}
        self.config_comments = {}
        self.server_configs = []
        self.config_paths = []
        self.selected_config_path = []
        self.remote_plugins = {}
        self.remote_plugin_items = []
        self.selected_plugin = ""
        self.selected_plugin_type = ""
        self.selected_plugin_status = ""
        self.selected_server_index = 0
        self.terminal_server_index = 0
        self.plugin_server_index = 0
        self.active_view_index = 0
        self.active_terminal_tab = "msmp"
        self.bot_process = None
        self.bot_launch_pending = False
        self.bot_connection_confirmed = False
        self.bot_listener_confirmed = False
        self.bot_start_sequence = 0
        self.mc_processes = {}
        self.server_action_pending = {}
        self.bot_managed_server_key = ""
        self.bot_managed_server_keys = set()
        self.mc_log_tail_thread = None
        self.terminal_buffers = {"msmp": [], "mc": {}}
        self.terminal_shortcuts = {"msmp": [], "mc": []}
        self.terminal_recent_commands = {"msmp": [], "mc": []}
        self.terminal_queue = queue.Queue()
        self._closing = False
        self._close_started = False
        self._shutdown_done = False
        self.logger = logging.getLogger(__name__)

        self._configure_page()
        self._load_terminal_commands()
        self._build_controls()
        self._load_config()
        self._show_view(0)
        self.page.run_thread(self._drain_terminal_queue)
        self._start_mc_log_tailer()

    def _configure_page(self):
        self.page.title = "MSMP_QQBot Control Deck"
        self.page.theme_mode = ft.ThemeMode.DARK
        self.page.bgcolor = "#090d12"
        self.page.padding = 0
        self.page.window.width = 1320
        self.page.window.height = 860
        self.page.window.min_width = 1080
        self.page.window.min_height = 700
        if not getattr(sys, "frozen", False) and APP_ICON_PATH.exists():
            self.page.window.icon = str(APP_ICON_PATH)
        self.page.window.prevent_close = True
        self.page.window.on_event = self._on_window_event
        self.page.on_close = self._on_close
        self.page.theme = ft.Theme(
            color_scheme_seed="#f2ad3f",
            font_family="Segoe UI",
            use_material3=True,
        )

    def _safe_page_update(self) -> bool:
        if self._closing:
            return False
        try:
            self.page.update()
            return True
        except RuntimeError as exc:
            if "destroyed session" in str(exc).lower():
                self._closing = True
                return False
            raise
        except Exception as exc:
            message = str(exc).lower()
            if "destroyed session" in message or "session" in message and "destroy" in message:
                self._closing = True
                return False
            raise

    def _build_controls(self):
        self.status_text = ft.Text("配置待加载", color="#9ba3ad", size=12)
        self.title_text = ft.Text("Bot配置", size=28, weight=ft.FontWeight.W_700)
        self.subtitle_text = ft.Text("可视化管理 MSMP_QQBot", color="#9ba3ad")
        self.content = ft.Container(expand=True)

        self.rail = ft.NavigationRail(
            selected_index=0,
            bgcolor="#0f151f",
            min_width=96,
            group_alignment=-0.75,
            label_type=ft.NavigationRailLabelType.ALL,
            destinations=[
                ft.NavigationRailDestination(icon=ft.Icons.TUNE, label="Bot配置"),
                ft.NavigationRailDestination(icon=ft.Icons.TERMINAL, label="终端"),
                ft.NavigationRailDestination(icon=ft.Icons.EXTENSION, label="插件"),
                ft.NavigationRailDestination(icon=ft.Icons.DNS, label="服务器"),
                ft.NavigationRailDestination(icon=ft.Icons.INFO, label="关于"),
            ],
            on_change=lambda event: self._show_view(event.control.selected_index),
        )

        shell = ft.Row(
            [
                self.rail,
                ft.Container(
                    expand=True,
                    padding=24,
                    content=ft.Column(
                        [
                            ft.Row(
                                [
                                    ft.Column([self.title_text, self.subtitle_text], spacing=2, expand=True),
                                ]
                            ),
                            self.content,
                        ],
                        expand=True,
                    ),
                ),
            ],
            expand=True,
            spacing=0,
        )
        self.page.add(shell)

    def _show_view(self, index: int):
        view_defs = [
            ("Bot配置", "", self._config_view),
            ("终端", "", self._terminal_view),
            ("插件", "", self._plugins_view),
            ("服务器", "", self._servers_view),
            ("关于", "", self._about_view),
        ]
        if index < 0 or index >= len(view_defs):
            self.logger.warning("忽略无效GUI页面索引: %s", index)
            index = 0
            self.rail.selected_index = index
        self.active_view_index = index
        title, subtitle, view_factory = view_defs[index]
        self.title_text.value, self.subtitle_text.value = title, subtitle
        self.content.content = view_factory()
        self._safe_page_update()

    def _panel(self, content, padding=18, expand=False):
        return ft.Container(
            content=content,
            padding=padding,
            expand=expand,
            bgcolor="#121923",
            border_radius=22,
        )

    def _section_title(self, title, subtitle=""):
        lines = [ft.Text(title, size=18, weight=ft.FontWeight.W_700)]
        if subtitle:
            lines.append(ft.Text(subtitle, color="#8f98a6", size=12))
        return ft.Column(lines, spacing=2)

    def _metric_chip(self, label, value, color="#f6c56a"):
        return ft.Container(
            content=ft.Column(
                [
                    ft.Text(label, color="#8f98a6", size=11),
                    ft.Text(str(value), color=color, size=18, weight=ft.FontWeight.W_800),
                ],
                spacing=0,
            ),
            bgcolor="#0c1119",
            border_radius=16,
            padding=ft.Padding(14, 10, 14, 10),
        )

    def _settings_section(self, title, subtitle, controls):
        return ft.Container(
            content=ft.Column(
                [self._section_title(title, subtitle), *controls],
                spacing=10,
            ),
            bgcolor="#0c1119",
            border_radius=18,
            padding=16,
        )

    def _field_row(self, *controls):
        return ft.Row(list(controls), spacing=10)

    def _checkbox_wrap(self, keys):
        return ft.Row([self.server_fields[key] for key in keys], spacing=8, wrap=True)

    def _register_page_service(self, control):
        services = getattr(self.page, "services", None)
        if services is not None and control not in services:
            services.append(control)

    def _bind_toggle_auto_save(self, fields, handler):
        for key, control in fields.items():
            if isinstance(control, (ft.Switch, ft.Checkbox)):
                control.on_change = lambda event, field_key=key: handler(field_key, event)

    def _server_options(self):
        options = []
        for index, server in enumerate(self._servers()):
            name = self._server_key(index + 1, server)
            options.append(ft.DropdownOption(key=str(index), text=f"{index + 1}. {name}"))
        return options

    def _bounded_server_index(self, state_attr="selected_server_index"):
        servers = self._servers()
        current = int(getattr(self, state_attr, 0) or 0)
        if servers:
            current = min(max(current, 0), len(servers) - 1)
        else:
            current = 0
        setattr(self, state_attr, current)
        return current

    def _selected_server_for(self, state_attr="selected_server_index"):
        servers = self._servers()
        if not servers:
            return None, 0, ""
        index = self._bounded_server_index(state_attr)
        return servers[index], index, self._server_key(index + 1, servers[index])

    def _server_selector(self, label="目标服务器", width=260, state_attr="selected_server_index", on_change=None):
        servers = self._servers()
        index = self._bounded_server_index(state_attr)
        return ft.Dropdown(
            label=label,
            value=str(index) if servers else None,
            options=self._server_options(),
            width=width,
            dense=True,
            disabled=not servers,
            hint_text="暂无服务器配置",
            on_select=on_change or (lambda event: self._on_server_selector_changed(event, state_attr)),
        )

    def _on_server_selector_changed(self, event, state_attr="selected_server_index"):
        try:
            setattr(self, state_attr, int(event.control.value))
        except (TypeError, ValueError):
            setattr(self, state_attr, 0)
        self._bounded_server_index(state_attr)
        if self.active_view_index == 3 and hasattr(self, "server_list"):
            self._populate_servers(load_form=state_attr == "selected_server_index")
        if self.active_view_index == 2 and hasattr(self, "plugin_config_list"):
            self._populate_plugins(list(self.remote_plugins.values()))
            self._update_selected_plugin_detail()
            self._load_plugin_config_form(update=False)
        if self.active_view_index == 1 and hasattr(self, "mc_terminal"):
            self._render_terminal_buffer("mc")
            self._render_active_terminal_panel()
        self._safe_page_update()

    def _selected_server_prefix(self, state_attr="selected_server_index"):
        return self._selected_server_for(state_attr)[2]

    def _selected_server_command_selector(self, state_attr="selected_server_index"):
        servers = self._servers()
        if not servers:
            return ""
        return str(self._bounded_server_index(state_attr) + 1)

    def _args_start_with_server(self, args):
        parts = str(args or "").strip().split(maxsplit=1)
        if not parts:
            return False
        first = parts[0].lower()
        for index, server in enumerate(self._servers(), 1):
            names = {str(index), self._server_key(index, server).lower(), str(server.get("name", "")).lower()}
            if first in names:
                return True
        return False

    def _console_command_with_selected_server(self, command, state_attr="terminal_server_index"):
        raw = str(command or "").strip()
        if not raw.startswith("#"):
            return raw
        body = raw[1:].strip()
        name, _, args = body.partition(" ")
        server_aware_commands = {
            "list", "tps", "rules", "sysinfo", "disk", "process", "network", "listeners",
            "start", "stop", "kill", "log", "reconnect", "reconnect_msmp", "reconnect_rcon",
            "plugins", "load_plugin", "unload_plugin", "reload_plugin",
            "mc", "command",
        }
        if name.lower() not in server_aware_commands or self._args_start_with_server(args):
            return raw
        prefix = self._selected_server_command_selector(state_attr)
        if not prefix:
            return raw
        return f"#{name} {prefix}" + (f" {args.strip()}" if args.strip() else "")

    def _config_view(self):
        self.bot_fields = {
            "websocket.port": ft.TextField(label="WebSocket端口", hint_text=CONFIG_HELP["websocket.port"]),
            "websocket.token": ft.TextField(label="鉴权令牌", hint_text=CONFIG_HELP["websocket.token"], password=True, can_reveal_password=True),
            "websocket.auth_enabled": ft.Switch(label="启用WebSocket鉴权", on_change=lambda _: self._save_bot_config_form()),
        }
        self._populate_bot_config_form()
        bot_panel = self._panel(
            ft.Column(
                [
                    self._section_title("WebSocket连接", ""),
                    self._field_row(self.bot_fields["websocket.port"], self.bot_fields["websocket.token"]),
                    self.bot_fields["websocket.auth_enabled"],
                    ft.Row(
                        [
                            ft.FilledButton("应用并保存 Bot配置", icon=ft.Icons.SAVE, on_click=lambda _: self._save_bot_config_form()),
                            ft.OutlinedButton("重载 Bot配置", icon=ft.Icons.REFRESH, on_click=lambda _: self._load_config()),
                        ],
                        wrap=True,
                        spacing=10,
                    ),
                ],
                spacing=12,
            ),
            padding=22,
        )
        return ft.Column(
            [
                bot_panel,
            ],
            expand=True,
            spacing=16,
        )

    def _populate_bot_config_form(self):
        if not hasattr(self, "bot_fields"):
            return
        websocket = self.config_data.get("websocket") or {}
        self.bot_fields["websocket.port"].value = str(websocket.get("port", 8080))
        self.bot_fields["websocket.token"].value = websocket.get("token", "")
        self.bot_fields["websocket.auth_enabled"].value = bool(websocket.get("auth_enabled", False))

    def _apply_bot_config_form(self):
        if not hasattr(self, "bot_fields"):
            return False
        raw_port = str(self.bot_fields["websocket.port"].value or "").strip()
        try:
            port = int(raw_port or "8080")
        except ValueError:
            self._toast("WebSocket端口必须是数字")
            return False
        websocket = dict(self.config_data.get("websocket") or {})
        websocket["port"] = port
        websocket["token"] = self.bot_fields["websocket.token"].value or ""
        websocket["auth_enabled"] = bool(self.bot_fields["websocket.auth_enabled"].value)
        self.config_data = {"websocket": websocket}
        if hasattr(self, "config_list"):
            self._populate_config_list()
        return True

    def _save_bot_config_form(self):
        if self._apply_bot_config_form():
            self._save_config()

    def _configured_ws_port(self):
        websocket = self.config_data.get("websocket") or {}
        try:
            return int(websocket.get("port", 8080))
        except (TypeError, ValueError):
            return 8080

    def _validate_bot_start_config(self):
        websocket = self.config_data.get("websocket")
        if websocket is None:
            self.config_data["websocket"] = {"port": 8080, "token": "", "auth_enabled": False}
            return True, 8080, "Bot配置缺少 websocket 段，已使用默认端口 8080"
        if not isinstance(websocket, dict):
            return False, None, "Bot配置错误：websocket 必须是对象，请在 Bot配置页面修正"
        raw_port = websocket.get("port", 8080)
        try:
            port = int(raw_port)
        except (TypeError, ValueError):
            return False, None, f"Bot配置错误：websocket.port 必须是数字，当前值为 {raw_port!r}"
        if port < 1 or port > 65535:
            return False, None, f"Bot配置错误：websocket.port 必须在 1-65535 之间，当前为 {port}"
        return True, port, ""

    def _tcp_port_open(self, port, host="127.0.0.1", timeout=0.05):
        try:
            with socket.create_connection((host, int(port)), timeout=timeout):
                return True
        except OSError:
            return False
        except (TypeError, ValueError):
            return False

    def _terminal_view(self):
        self.msmp_terminal = ft.Column(expand=True, spacing=2, scroll=ft.ScrollMode.AUTO, auto_scroll=True)
        self.mc_terminal = ft.Column(expand=True, spacing=2, scroll=ft.ScrollMode.AUTO, auto_scroll=True)
        self._render_terminal_buffer("msmp")
        self._render_terminal_buffer("mc")
        self.msmp_input = ft.TextField(
            hint_text="输入 #plugins、#reload、#start 等 Bot 控制台命令",
            on_submit=self._submit_msmp,
            expand=True,
        )
        self.mc_input = ft.TextField(
            hint_text="输入 MC 控制台命令，将发送给选中的本地服务器",
            on_submit=self._submit_mc,
            expand=True,
        )
        self.terminal_server_selector = self._server_selector("MC目标服务器", state_attr="terminal_server_index")
        self.terminal_bot_toggle = ft.FilledButton(on_click=lambda _: self._toggle_bot_process())
        self.terminal_server_toggle = ft.OutlinedButton(on_click=lambda _: self._toggle_selected_server())
        self.terminal_server_force_stop = ft.OutlinedButton(
            "强制关闭服务器",
            icon=ft.Icons.DANGEROUS,
            on_click=lambda _: self._force_stop_local_process("terminal_server_index"),
        )
        self._refresh_terminal_action_buttons()
        self.terminal_tab_switch = ft.SegmentedButton(
            segments=[
                ft.Segment(value="msmp", icon=ft.Icons.TERMINAL, label=ft.Text("MSMP终端")),
                ft.Segment(value="mc", icon=ft.Icons.DNS, label=ft.Text("MC终端")),
            ],
            selected=[self.active_terminal_tab],
            allow_empty_selection=False,
            on_change=self._on_terminal_tab_changed,
        )
        self.msmp_terminal_panel = self._terminal_panel(
            "MSMP 终端",
            "#9dd6ff",
            self.msmp_terminal,
            self.msmp_input,
            "msmp",
        )
        self.mc_terminal_panel = self._terminal_panel(
            "MC 终端",
            "#b6f3a4",
            self.mc_terminal,
            self.mc_input,
            "mc",
        )
        self.terminal_body = ft.Column(
            [self.msmp_terminal_panel, self.mc_terminal_panel],
            expand=True,
            spacing=0,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        )
        self._sync_terminal_panel_visibility()
        return ft.Column(
            [
                ft.Row(
                    [
                        self.terminal_server_selector,
                        self.terminal_bot_toggle,
                        self.terminal_server_toggle,
                        self.terminal_server_force_stop,
                        self.terminal_tab_switch,
                    ],
                    spacing=10,
                    wrap=True,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                self.terminal_body,
            ],
            expand=True,
            spacing=14,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        )

    def _on_terminal_tab_changed(self, event):
        selected = list(event.control.selected or [])
        self.active_terminal_tab = selected[0] if selected else "msmp"
        if self.active_terminal_tab == "mc":
            self._render_terminal_buffer("mc")
        self._sync_terminal_panel_visibility()
        self._safe_page_update()

    def _render_active_terminal_panel(self):
        if not hasattr(self, "terminal_body"):
            return
        if hasattr(self, "msmp_terminal_panel") and hasattr(self, "mc_terminal_panel"):
            self._rebuild_terminal_panels()
            self._sync_terminal_panel_visibility()
            return
        self._sync_terminal_panel_visibility()

    def _rebuild_terminal_panels(self):
        self.msmp_terminal_panel = self._terminal_panel(
            "MSMP 终端",
            "#9dd6ff",
            self.msmp_terminal,
            self.msmp_input,
            "msmp",
        )
        self.mc_terminal_panel = self._terminal_panel(
            "MC 终端",
            "#b6f3a4",
            self.mc_terminal,
            self.mc_input,
            "mc",
        )
        if hasattr(self.terminal_body, "controls"):
            self.terminal_body.controls = [self.msmp_terminal_panel, self.mc_terminal_panel]

    def _sync_terminal_panel_visibility(self):
        self._refresh_terminal_action_buttons()
        if hasattr(self, "msmp_terminal_panel"):
            self.msmp_terminal_panel.visible = self.active_terminal_tab != "mc"
        if hasattr(self, "mc_terminal_panel"):
            self.mc_terminal_panel.visible = self.active_terminal_tab == "mc"

    def _terminal_panel(self, title, color, output, input_control, target):
        return self._panel(
            ft.Column(
                [
                    ft.Text(title, size=18, weight=ft.FontWeight.W_700, color=color),
                    ft.Container(
                        content=ft.SelectionArea(content=output),
                        expand=True,
                        bgcolor="#070a0f",
                        border_radius=16,
                        padding=12,
                    ),
                    self._terminal_input_bar(target, input_control),
                    self._terminal_command_strip(target),
                ],
                expand=True,
                spacing=10,
                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            ),
            expand=True,
        )

    def _terminal_input_bar(self, target, input_control):
        return ft.Row(
            [
                input_control,
                ft.FilledButton("发送", icon=ft.Icons.SEND, on_click=lambda _: self._submit_terminal_input(target)),
                ft.OutlinedButton("保存快捷", icon=ft.Icons.SAVE, on_click=lambda _: self._save_terminal_shortcut(target)),
            ],
            spacing=10,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

    def _terminal_command_strip(self, target):
        return ft.Container(
            content=ft.Column(
                [
                    self._terminal_command_row("快捷命令", target, self.terminal_shortcuts.get(target, []), removable=True),
                    self._terminal_command_row("最近使用", target, self.terminal_recent_commands.get(target, []), removable=False),
                ],
                spacing=8,
            ),
            padding=ft.Padding(12, 10, 12, 10),
            border_radius=16,
            bgcolor="#0c1119",
        )

    def _terminal_command_row(self, title, target, commands, removable=False):
        controls = [ft.Text(title, color="#8f98a6", size=12)]
        if not commands:
            controls.append(ft.Text("暂无", color="#596575", size=12))
        for command in commands[:20]:
            controls.append(self._terminal_command_button(target, command, removable))
        return ft.Row(controls, spacing=8, wrap=True, vertical_alignment=ft.CrossAxisAlignment.CENTER)

    def _terminal_command_button(self, target, command, removable=False):
        button = ft.TextButton(
            str(command),
            on_click=lambda _event, value=command: self._set_terminal_input(target, value),
        )
        if not removable:
            return ft.Container(content=button, bgcolor="#182232", border_radius=12, padding=ft.Padding(4, 0, 4, 0))
        return ft.Container(
            content=ft.Row(
                [
                    button,
                    ft.IconButton(
                        icon=ft.Icons.CLOSE,
                        icon_size=14,
                        tooltip="删除快捷命令",
                        on_click=lambda _event, value=command: self._remove_terminal_shortcut(target, value),
                    ),
                ],
                spacing=0,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            bgcolor="#182232",
            border_radius=12,
            padding=ft.Padding(4, 0, 0, 0),
        )

    def _load_terminal_commands(self):
        try:
            data = json.loads(TERMINAL_COMMANDS_PATH.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except Exception as exc:
            self.logger.warning("读取GUI终端命令记录失败: %s", exc)
            return
        if not isinstance(data, dict):
            return
        self.terminal_shortcuts = {
            target: self._clean_terminal_command_list((data.get("shortcuts") or {}).get(target, []))
            for target in ("msmp", "mc")
        }
        self.terminal_recent_commands = {
            target: self._clean_terminal_command_list((data.get("recent") or {}).get(target, []))
            for target in ("msmp", "mc")
        }

    def _save_terminal_commands(self):
        data = {
            "shortcuts": self.terminal_shortcuts,
            "recent": self.terminal_recent_commands,
        }
        try:
            TERMINAL_COMMANDS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            self._toast(f"保存终端命令记录失败: {exc}")

    def _clean_terminal_command_list(self, commands, limit=20):
        result = []
        seen = set()
        for item in commands or []:
            command = str(item or "").strip()
            if not command or command in seen:
                continue
            seen.add(command)
            result.append(command)
            if len(result) >= limit:
                break
        return result

    def _remember_terminal_command(self, target, command):
        command = str(command or "").strip()
        if not command:
            return
        recent = [item for item in self.terminal_recent_commands.get(target, []) if item != command]
        self.terminal_recent_commands[target] = [command, *recent][:20]
        self._save_terminal_commands()
        if self.active_view_index == 1 and self.active_terminal_tab == target and hasattr(self, "terminal_body"):
            self._render_active_terminal_panel()

    def _save_terminal_shortcut(self, target, command=None):
        command = str(command or self._terminal_input_value(target) or "").strip()
        if not command:
            self._toast("请输入命令后再保存快捷命令")
            return
        shortcuts = self.terminal_shortcuts.setdefault(target, [])
        if command in shortcuts:
            self._toast("快捷命令已存在")
            return
        self.terminal_shortcuts[target] = [command, *shortcuts][:20]
        self._save_terminal_commands()
        self._render_active_terminal_panel()
        self._toast("快捷命令已保存")
        self._safe_page_update()

    def _remove_terminal_shortcut(self, target, command):
        self.terminal_shortcuts[target] = [item for item in self.terminal_shortcuts.get(target, []) if item != command]
        self._save_terminal_commands()
        self._render_active_terminal_panel()
        self._toast("快捷命令已删除")
        self._safe_page_update()

    def _terminal_input_control(self, target):
        return self.mc_input if target == "mc" else self.msmp_input

    def _terminal_input_value(self, target):
        control = self._terminal_input_control(target)
        return str(getattr(control, "value", "") or "")

    def _set_terminal_input(self, target, command):
        control = self._terminal_input_control(target)
        control.value = str(command or "")
        self._safe_page_update()

    def _submit_terminal_input(self, target):
        command = self._terminal_input_value(target).strip()
        self._terminal_input_control(target).value = ""
        self._safe_page_update()
        if target == "mc":
            self._execute_mc_command(command)
        else:
            self._execute_msmp_command(command)

    def _refresh_terminal_action_buttons(self):
        if hasattr(self, "terminal_bot_toggle"):
            bot_running = self._is_bot_running()
            self.terminal_bot_toggle.content = "停止 Bot" if bot_running else "启动中..." if self.bot_launch_pending else "启动 Bot"
            self.terminal_bot_toggle.icon = ft.Icons.STOP if bot_running else ft.Icons.HOURGLASS_EMPTY if self.bot_launch_pending else ft.Icons.PLAY_ARROW
            self.terminal_bot_toggle.disabled = self.bot_launch_pending and not bot_running
        if hasattr(self, "terminal_server_toggle"):
            _server, _index, key = self._selected_server_for("terminal_server_index")
            self._clear_expired_server_pending()
            running = self._is_selected_terminal_server_running()
            pending = bool(self.server_action_pending.get(key))
            if pending:
                self.terminal_server_toggle.content = "处理中..."
                self.terminal_server_toggle.icon = ft.Icons.HOURGLASS_EMPTY
            else:
                self.terminal_server_toggle.content = "停止服务器" if running else "启动服务器"
                self.terminal_server_toggle.icon = ft.Icons.POWER_SETTINGS_NEW if running else ft.Icons.ROCKET_LAUNCH
            self.terminal_server_toggle.disabled = pending or not bool(key)
        if hasattr(self, "terminal_server_force_stop"):
            _server, _index, key = self._selected_server_for("terminal_server_index")
            running = self._is_selected_terminal_server_running()
            self.terminal_server_force_stop.disabled = not bool(key) or not running

    def _toggle_bot_process(self):
        if self._is_bot_running():
            self._stop_bot_process()
        else:
            self._start_bot_process()
        self._refresh_terminal_action_buttons()
        self._safe_page_update()

    def _toggle_selected_server(self):
        if self._is_selected_terminal_server_running():
            self._stop_local_process("terminal_server_index")
        else:
            self._start_selected_server("terminal_server_index")
        self._refresh_terminal_action_buttons()
        self._safe_page_update()

    def _is_selected_terminal_server_running(self):
        _server, _index, key = self._selected_server_for("terminal_server_index")
        return self._is_server_running(key)

    def _is_server_running(self, key):
        if not key:
            return False
        process = self.mc_processes.get(key)
        return bool(process and process.poll() is None) or (self._is_bot_running() and key in self.bot_managed_server_keys)

    def _set_server_action_pending(self, key, action, timeout=90):
        if not key:
            return
        if action:
            self.server_action_pending[key] = {"action": action, "expires_at": time.time() + timeout}
        else:
            self.server_action_pending.pop(key, None)
        self._refresh_terminal_action_buttons()

    def _clear_expired_server_pending(self):
        now = time.time()
        expired = [
            key
            for key, state in self.server_action_pending.items()
            if isinstance(state, dict) and float(state.get("expires_at") or 0) < now
        ]
        for key in expired:
            self.server_action_pending.pop(key, None)

    def _validate_server_start_config(self, server, index, key):
        if not isinstance(server, dict):
            return False, None, None, f"{key or '服务器'} 配置错误：顶层必须是对象"
        server_section = self._server_section(server)
        if not isinstance(server_section, dict):
            return False, None, None, f"{key} 配置错误：server 段必须是对象"
        script = str(server_section.get("start_script") or "").strip()
        if not script:
            return False, None, None, f"{key} 启动失败：未填写 server.start_script，请在服务器页面配置启动脚本"
        script_path = self._resolve_app_path(script)
        if not script_path.exists():
            return False, None, None, f"{key} 启动失败：启动脚本不存在: {script_path}"
        if not script_path.is_file():
            return False, None, None, f"{key} 启动失败：server.start_script 必须指向文件: {script_path}"
        suffix = script_path.suffix.lower()
        if suffix not in {".bat", ".cmd", ".sh"}:
            return False, None, None, f"{key} 启动失败：启动脚本仅支持 .bat/.cmd/.sh，当前为 {suffix or '无扩展名'}"
        configured_workdir = str(server_section.get("working_directory") or "").strip()
        workdir = self._resolve_app_path(configured_workdir) if configured_workdir else script_path.parent
        if not workdir.exists():
            return False, None, None, f"{key} 启动失败：工作目录不存在: {workdir}"
        if not workdir.is_dir():
            return False, None, None, f"{key} 启动失败：working_directory 必须指向文件夹: {workdir}"
        return True, script_path, workdir, ""

    def _plugins_view(self):
        self.plugin_list = ft.ListView(expand=True, spacing=6, auto_scroll=False)
        self.plugin_detail = ft.Text("选择插件查看状态。", color="#9ba3ad", selectable=True)
        self.plugin_config_path_text = ft.Text("配置文件: 未选择插件", color="#8f98a6", size=12, selectable=True)
        self.plugin_config_list = ft.ListView(expand=True, spacing=8, auto_scroll=False)
        self.plugin_config_fields = {}
        self.plugin_config_data = {}
        self.plugin_server_selector = self._server_selector("插件目标服务器", state_attr="plugin_server_index")
        self._populate_plugins(self.remote_plugin_items)
        self._load_plugin_config_form()
        return ft.Column(
            [
                ft.Row(
                    [
                        self.plugin_server_selector,
                        ft.FilledButton("刷新插件仓库", icon=ft.Icons.CLOUD_DOWNLOAD, on_click=lambda _: self._refresh_plugin_repo()),
                        ft.OutlinedButton("刷新本地插件", icon=ft.Icons.REFRESH, on_click=lambda _: self._refresh_local_plugins()),
                        ft.OutlinedButton("打开插件目录", icon=ft.Icons.FOLDER_OPEN, on_click=lambda _: self._open_plugin_dir()),
                    ],
                    spacing=10,
                ),
                ft.Row(
                    [
                        ft.Container(
                            width=340,
                            content=self._panel(
                                ft.Column(
                                    [
                                        self._section_title("插件列表", "选择插件后编辑当前服务器的独立配置"),
                                        self.plugin_list,
                                    ],
                                    spacing=12,
                                    expand=True,
                                ),
                                expand=True,
                            ),
                        ),
                        ft.Container(
                            width=360,
                            content=self._panel(
                                ft.Column(
                                    [
                                        ft.Text("插件操作", size=18, weight=ft.FontWeight.W_700),
                                        self.plugin_detail,
                                        ft.FilledButton("安装/更新到当前服务器", icon=ft.Icons.DOWNLOAD, on_click=lambda _: self._download_selected_plugin()),
                                        ft.OutlinedButton("从当前服务器卸载", icon=ft.Icons.REMOVE_CIRCLE_OUTLINE, on_click=lambda _: self._uninstall_plugin_from_selected_server()),
                                        ft.OutlinedButton("启用到当前服务器", on_click=lambda _: self._enable_plugin_for_selected_server()),
                                        ft.OutlinedButton("禁用当前服务器", on_click=lambda _: self._disable_plugin_for_selected_server()),
                                        ft.OutlinedButton("重载插件", on_click=lambda _: self._reload_selected_plugin()),
                                        ft.OutlinedButton("配置/文件位置", icon=ft.Icons.SETTINGS, on_click=lambda _: self._show_plugin_location()),
                                    ],
                                    spacing=10,
                                ),
                                expand=True,
                            ),
                        ),
                        self._panel(
                            ft.Column(
                                [
                                    self._section_title("插件配置", "按当前目标服务器保存到独立 config.json"),
                                    self.plugin_config_path_text,
                                    self.plugin_config_list,
                                    ft.Row(
                                        [
                                            ft.FilledButton("保存插件配置", icon=ft.Icons.SAVE, on_click=lambda _: self._save_plugin_config_form()),
                                            ft.OutlinedButton("重新加载", icon=ft.Icons.REFRESH, on_click=lambda _: self._load_plugin_config_form()),
                                        ],
                                        spacing=10,
                                        wrap=True,
                                    ),
                                ],
                                spacing=12,
                                expand=True,
                            ),
                            expand=True,
                        ),
                    ],
                    expand=True,
                    spacing=16,
                ),
            ],
            expand=True,
            spacing=14,
        )

    def _about_view(self):
        repo_url = "https://github.com/intellectmind/MSMP_QQBot"
        intro = (
            "MSMP_QQBot 是面向 Minecraft 服务器的 QQ 机器人控制面板，"
            "支持 OneBot 反向 WebSocket、MSMP/RCON 管理、多服务器配置、"
            "插件扩展、定时任务、通知和本地可视化 GUI。"
        )
        return ft.Column(
            [
                self._panel(
                    ft.Column(
                        [
                            ft.Text(APP_NAME, size=34, weight=ft.FontWeight.W_800, color="#f6c56a"),
                            ft.Text(intro, color="#c7ced8", size=15, selectable=True),
                            ft.Row(
                                [
                                    self._metric_chip("当前版本", APP_VERSION, "#9dd6ff"),
                                    self._metric_chip("配置模式", "Bot + 多服务器", "#b6f3a4"),
                                ],
                                spacing=12,
                            ),
                            ft.Container(height=1, bgcolor="#263142"),
                            ft.Text("GitHub 仓库", size=18, weight=ft.FontWeight.W_700),
                            ft.Text("intellectmind/MSMP_QQBot", color="#9ba3ad", selectable=True),
                            ft.TextButton(
                                "打开 GitHub 仓库",
                                icon=ft.Icons.OPEN_IN_NEW,
                                url=repo_url,
                            ),
                        ],
                        spacing=16,
                    ),
                    padding=28,
                ),
            ],
            spacing=16,
            scroll=ft.ScrollMode.AUTO,
        )

    def _servers_view(self):
        self.server_list = ft.ListView(expand=True, spacing=8, auto_scroll=False)
        self.server_fields = {
            "name": ft.TextField(label="服务器名称", hint_text=CONFIG_HELP["name"]),
            "msmp.enabled": ft.Switch(label="启用 MSMP"),
            "rcon.enabled": ft.Switch(label="启用 RCON"),
            "start_script": ft.TextField(label="启动脚本", hint_text=CONFIG_HELP["server.start_script"]),
            "working_directory": ft.TextField(label="工作目录", hint_text=CONFIG_HELP["server.working_directory"]),
            "server.startup_timeout": ft.TextField(label="启动超时(秒)", hint_text=CONFIG_HELP["server.startup_timeout"]),
            "server.auto_restart_on_crash": ft.Switch(label="崩溃自动重启"),
            "server.crash_restart_delay": ft.TextField(label="重启延迟(秒)", hint_text=CONFIG_HELP["server.crash_restart_delay"]),
            "server.log_idle_restart_enabled": ft.Switch(label="启用日志假死重启"),
            "server.log_idle_restart_timeout": ft.TextField(label="日志假死重启(秒)", hint_text=CONFIG_HELP["server.log_idle_restart_timeout"]),
            "msmp.host": ft.TextField(label="MSMP 地址", hint_text=CONFIG_HELP["msmp.host"]),
            "msmp.port": ft.TextField(label="MSMP 端口", hint_text=CONFIG_HELP["msmp.port"]),
            "msmp.password": ft.TextField(label="MSMP 密钥", hint_text=CONFIG_HELP["msmp.password"], password=True, can_reveal_password=True),
            "rcon.host": ft.TextField(label="RCON 地址", hint_text=CONFIG_HELP["rcon.host"]),
            "rcon.port": ft.TextField(label="RCON 端口", hint_text=CONFIG_HELP["rcon.port"]),
            "rcon.password": ft.TextField(label="RCON 密码", hint_text=CONFIG_HELP["rcon.password"], password=True, can_reveal_password=True),
            "qq.groups": ft.TextField(label="QQ群号列表", hint_text="多个群号用逗号或换行分隔", multiline=True, min_lines=2, max_lines=4),
            "qq.admins": ft.TextField(label="管理员QQ列表", hint_text="多个QQ用逗号或换行分隔", multiline=True, min_lines=2, max_lines=4),
            "qq.welcome_new_members": ft.Switch(label="欢迎新成员"),
            "qq.welcome_message": ft.TextField(label="欢迎消息", hint_text=CONFIG_HELP["qq.welcome_message"]),
            "commands.tps_command": ft.TextField(label="TPS命令", hint_text=CONFIG_HELP["commands.tps_command"]),
            "commands.tps_regex": ft.TextField(label="TPS正则", hint_text=CONFIG_HELP["commands.tps_regex"]),
            "commands.tps_group_index": ft.TextField(label="TPS捕获组", hint_text=CONFIG_HELP["commands.tps_group_index"]),
            "commands.tps_show_raw_output": ft.Switch(label="显示TPS原始输出"),
            "notifications.server_events": ft.Switch(label="服务器事件通知"),
            "notifications.player_events": ft.Switch(label="玩家事件通知"),
            "notifications.log_messages": ft.Switch(label="控制台详细消息日志"),
            "notifications.chunk_monitor.enabled": ft.Switch(label="启用区块监控"),
            "notifications.chunk_monitor.notify_admins": ft.Switch(label="区块监控私聊管理员"),
            "notifications.chunk_monitor.notify_groups": ft.Switch(label="区块监控发群"),
            "advanced.reconnect_interval": ft.TextField(label="重连间隔(秒)", hint_text=CONFIG_HELP["advanced.reconnect_interval"]),
            "advanced.heartbeat_interval": ft.TextField(label="心跳间隔(秒)", hint_text=CONFIG_HELP["advanced.heartbeat_interval"]),
            "advanced.command_cooldown": ft.TextField(label="命令冷却(秒)", hint_text=CONFIG_HELP["advanced.command_cooldown"]),
            "advanced.max_message_length": ft.TextField(label="最大消息长度", hint_text=CONFIG_HELP["advanced.max_message_length"]),
            "advanced.player_list_cache_ttl": ft.TextField(label="玩家列表缓存(秒)", hint_text=CONFIG_HELP["advanced.player_list_cache_ttl"]),
            "advanced.max_server_logs": ft.TextField(label="最大日志行数", hint_text=CONFIG_HELP["advanced.max_server_logs"]),
            "scheduled_tasks.enabled": ft.Switch(label="启用定时任务"),
            "scheduled_tasks.auto_start.enabled": ft.Switch(label="定时启动"),
            "scheduled_tasks.auto_start.times": ft.TextField(label="启动时间", hint_text="如 08:00,18:00"),
            "scheduled_tasks.auto_start.weekdays": ft.TextField(label="启动星期", hint_text="0=周一，6=周日，如 0,1,2,3,4"),
            "scheduled_tasks.auto_stop.enabled": ft.Switch(label="定时关闭"),
            "scheduled_tasks.auto_stop.times": ft.TextField(label="关闭时间", hint_text="如 12:00,23:59"),
            "scheduled_tasks.auto_stop.weekdays": ft.TextField(label="关闭星期", hint_text="0=周一，6=周日"),
            "scheduled_tasks.auto_restart.enabled": ft.Switch(label="定时重启"),
            "scheduled_tasks.auto_restart.times": ft.TextField(label="重启时间", hint_text="如 04:00"),
            "scheduled_tasks.auto_restart.weekdays": ft.TextField(label="重启星期", hint_text="0=周一，6=周日"),
        }
        for command_name in ("list", "tps", "rules", "status", "help"):
            self.server_fields[f"commands.enabled_commands.{command_name}"] = ft.Checkbox(label=command_name)
        for command_name in (
            "start", "stop", "kill", "reload", "log", "reconnect", "reconnect_msmp",
            "reconnect_rcon", "crash", "sysinfo", "disk", "process", "network", "listeners",
        ):
            self.server_fields[f"commands.enabled_admin_commands.{command_name}"] = ft.Checkbox(label=command_name)
        self._bind_toggle_auto_save(self.server_fields, self._on_server_toggle_changed)
        if not hasattr(self, "script_picker"):
            self.script_picker = ft.FilePicker()
            self.workdir_picker = ft.FilePicker()
            self._register_page_service(self.script_picker)
            self._register_page_service(self.workdir_picker)
        self.server_help_filter = ft.TextField(
            label="搜索字段说明",
            hint_text="输入 qq.welcome、tps、custom_listeners 等",
            icon=ft.Icons.SEARCH,
            on_change=lambda _: self._populate_server_help(),
        )
        self.server_help_list = ft.ListView(expand=True, spacing=8, auto_scroll=False)

        quick_form = ft.Column(
            [
                self._settings_section(
                    "基础信息",
                    "服务器名称和本地启动脚本",
                    [
                        self.server_fields["name"],
                        ft.Row(
                            [
                                self.server_fields["start_script"],
                                ft.IconButton(ft.Icons.FOLDER_OPEN, on_click=lambda _: self.page.run_task(self._pick_start_script)),
                            ],
                            spacing=8,
                        ),
                        ft.Row(
                            [
                                self.server_fields["working_directory"],
                                ft.IconButton(ft.Icons.FOLDER_OPEN, on_click=lambda _: self.page.run_task(self._pick_workdir)),
                            ],
                            spacing=8,
                        ),
                    ],
                ),
                self._settings_section(
                    "连接协议",
                    "MSMP/RCON 可分别启用，命令会按目标服务器连接配置执行",
                    [
                        ft.Row([self.server_fields["msmp.enabled"], self.server_fields["rcon.enabled"]], spacing=18, wrap=True),
                        self._field_row(self.server_fields["msmp.host"], self.server_fields["msmp.port"]),
                        self.server_fields["msmp.password"],
                        self._field_row(self.server_fields["rcon.host"], self.server_fields["rcon.port"]),
                        self.server_fields["rcon.password"],
                    ],
                ),
                self._settings_section(
                    "QQ权限与欢迎",
                    "每个服务器可绑定不同群和管理员；欢迎消息支持 {at}",
                    [
                        self._field_row(self.server_fields["qq.groups"], self.server_fields["qq.admins"]),
                        self.server_fields["qq.welcome_new_members"],
                        self.server_fields["qq.welcome_message"],
                    ],
                ),
                self._settings_section(
                    "启动守护",
                    "本地进程启动、崩溃重启和日志假死检测",
                    [
                        ft.Row(
                            [
                                self.server_fields["server.auto_restart_on_crash"],
                                self.server_fields["server.log_idle_restart_enabled"],
                            ],
                            spacing=18,
                            wrap=True,
                        ),
                        self._field_row(
                            self.server_fields["server.startup_timeout"],
                            self.server_fields["server.crash_restart_delay"],
                            self.server_fields["server.log_idle_restart_timeout"],
                        ),
                    ],
                ),
                self._settings_section(
                    "命令与TPS",
                    "基础命令、管理员命令普通成员开放和 TPS 解析规则",
                    [
                        self._field_row(self.server_fields["commands.tps_command"], self.server_fields["commands.tps_group_index"]),
                        self.server_fields["commands.tps_regex"],
                        self.server_fields["commands.tps_show_raw_output"],
                        ft.Text("基础命令", color="#8f98a6", size=12),
                        self._checkbox_wrap([f"commands.enabled_commands.{name}" for name in ("list", "tps", "rules", "status", "help")]),
                        ft.Text("向非管理员开放的管理员命令", color="#8f98a6", size=12),
                        self._checkbox_wrap([f"commands.enabled_admin_commands.{name}" for name in (
                            "start", "stop", "kill", "reload", "log", "reconnect", "reconnect_msmp",
                            "reconnect_rcon", "crash", "sysinfo", "disk", "process", "network", "listeners",
                        )]),
                    ],
                ),
                self._settings_section(
                    "通知",
                    "服务器事件、玩家事件、日志和 ChunkMonitor 告警",
                    [
                        ft.Row(
                            [
                                self.server_fields["notifications.server_events"],
                                self.server_fields["notifications.player_events"],
                                self.server_fields["notifications.log_messages"],
                            ],
                            spacing=18,
                            wrap=True,
                        ),
                        ft.Row(
                            [
                                self.server_fields["notifications.chunk_monitor.enabled"],
                                self.server_fields["notifications.chunk_monitor.notify_admins"],
                                self.server_fields["notifications.chunk_monitor.notify_groups"],
                            ],
                            spacing=18,
                            wrap=True,
                        ),
                    ],
                ),
                self._settings_section(
                    "高级参数",
                    "重连、心跳、冷却、消息长度和缓存",
                    [
                        self._field_row(self.server_fields["advanced.reconnect_interval"], self.server_fields["advanced.heartbeat_interval"], self.server_fields["advanced.command_cooldown"]),
                        self._field_row(self.server_fields["advanced.max_message_length"], self.server_fields["advanced.player_list_cache_ttl"], self.server_fields["advanced.max_server_logs"]),
                    ],
                ),
                self._settings_section(
                    "定时任务",
                    "用逗号填写多个时间或星期，例如 08:00,18:00 / 0,1,2,3,4",
                    [
                        self.server_fields["scheduled_tasks.enabled"],
                        ft.Row([self.server_fields["scheduled_tasks.auto_start.enabled"], self.server_fields["scheduled_tasks.auto_stop.enabled"], self.server_fields["scheduled_tasks.auto_restart.enabled"]], spacing=18, wrap=True),
                        self._field_row(self.server_fields["scheduled_tasks.auto_start.times"], self.server_fields["scheduled_tasks.auto_start.weekdays"]),
                        self._field_row(self.server_fields["scheduled_tasks.auto_stop.times"], self.server_fields["scheduled_tasks.auto_stop.weekdays"]),
                        self._field_row(self.server_fields["scheduled_tasks.auto_restart.times"], self.server_fields["scheduled_tasks.auto_restart.weekdays"]),
                    ],
                ),
            ],
            spacing=10,
        )

        yaml_editor = ft.Column(
            [
                self._section_title("高级 YAML / 自定义规则", "自定义命令、监听器和未覆盖字段可在这里手动编辑"),
                self._server_yaml_field(),
            ],
            spacing=10,
            expand=True,
        )
        self._populate_servers()
        self._populate_server_help(update=False)

        save_bar = ft.Container(
            content=ft.Row(
                [
                    ft.Column(
                        [
                            ft.Text("当前服务器配置", size=18, weight=ft.FontWeight.W_700, color="#f7c56b"),
                            ft.Text("修改表单或高级 YAML 后，点击右侧按钮写入 servers/*.yml。", color="#9ba3ad", size=12),
                        ],
                        spacing=2,
                        expand=True,
                    ),
                    ft.FilledButton(
                        "保存服务器配置",
                        icon=ft.Icons.SAVE,
                        on_click=lambda _: self._apply_server_form(),
                    ),
                ],
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.Padding(16, 14, 16, 14),
            border_radius=18,
            bgcolor="#1d2b3d",
            border=ft.Border(
                left=ft.BorderSide(1, "#f7c56b"),
                top=ft.BorderSide(1, "#f7c56b"),
                right=ft.BorderSide(1, "#f7c56b"),
                bottom=ft.BorderSide(1, "#f7c56b"),
            ),
        )

        form = ft.Column(
            [
                save_bar,
                quick_form,
                ft.Container(height=1, bgcolor="#263142"),
                yaml_editor,
                ft.Row(
                    [
                        ft.FilledButton("保存服务器配置", icon=ft.Icons.SAVE, on_click=lambda _: self._apply_server_form()),
                        ft.OutlinedButton("新增服务器", icon=ft.Icons.ADD, on_click=lambda _: self._add_server()),
                        ft.OutlinedButton("删除选中", icon=ft.Icons.DELETE, on_click=lambda _: self._remove_selected_server()),
                    ],
                    wrap=True,
                ),
                ft.Divider(color="#263142"),
                ft.Text("QQ 指令示例: start 1、stop server1、tps 2、插件命令 2 参数。服务器编号/名称放在命令参数最前面。", color="#9ba3ad"),
            ],
            spacing=10,
            scroll=ft.ScrollMode.AUTO,
        )
        help_panel = ft.Column(
            [
                self._section_title("字段说明", "按配置路径搜索，每个 YAML 子项都能查到用途"),
                self.server_help_filter,
                self.server_help_list,
            ],
            spacing=12,
            expand=True,
        )
        return ft.Row(
            [
                ft.Container(
                    width=360,
                    content=self._panel(
                        ft.Column(
                            [
                                ft.Row(
                                    [
                                        ft.Container(
                                            content=self._section_title("服务器列表", "选择要编辑或启动的服务器"),
                                            expand=True,
                                        ),
                                        ft.OutlinedButton(
                                            "新增服务器",
                                            icon=ft.Icons.ADD,
                                            on_click=lambda _: self._add_server(),
                                        ),
                                    ],
                                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                                    vertical_alignment=ft.CrossAxisAlignment.START,
                                ),
                                ft.Row(
                                    [
                                        ft.FilledButton(
                                            "启动选中",
                                            icon=ft.Icons.ROCKET_LAUNCH,
                                            on_click=lambda _: self._start_selected_server("selected_server_index"),
                                            expand=True,
                                        ),
                                        ft.OutlinedButton(
                                            "停止选中",
                                            icon=ft.Icons.POWER_SETTINGS_NEW,
                                            on_click=lambda _: self._stop_local_process("selected_server_index"),
                                            expand=True,
                                        ),
                                    ],
                                    spacing=10,
                                ),
                                ft.OutlinedButton(
                                    "强制关闭选中",
                                    icon=ft.Icons.DANGEROUS,
                                    on_click=lambda _: self._force_stop_local_process("selected_server_index"),
                                    width=320,
                                ),
                                self.server_list,
                            ],
                            spacing=12,
                            expand=True,
                        ),
                        expand=True,
                    ),
                ),
                self._panel(form, expand=True),
                ft.Container(width=380, content=self._panel(help_panel, expand=True)),
            ],
            expand=True,
            spacing=16,
        )

    def _server_yaml_field(self):
        self.server_yaml_editor = ft.TextField(
            label="高级 YAML",
            hint_text="常用配置请优先使用上方表单；这里用于 custom_commands/custom_listeners 和手动高级编辑。",
            multiline=True,
            min_lines=10,
            max_lines=18,
            border_color="#34445a",
        )
        return self.server_yaml_editor

    def _populate_server_help(self, update=True):
        if not hasattr(self, "server_help_list"):
            return
        query = (self.server_help_filter.value or "").strip().lower() if hasattr(self, "server_help_filter") else ""
        self.server_help_list.controls.clear()
        for key, description in sorted(CONFIG_HELP.items()):
            if key.startswith("websocket"):
                continue
            haystack = f"{key} {description}".lower()
            if query and query not in haystack:
                continue
            self.server_help_list.controls.append(self._help_card(key, description))
        if not self.server_help_list.controls:
            self.server_help_list.controls.append(ft.Text("没有匹配的字段说明。", color="#9ba3ad"))
        if update and self.page.controls:
            self._safe_page_update()

    def _help_card(self, key, description):
        return ft.Container(
            content=ft.Column(
                [
                    ft.Text(key, color="#f6c56a", size=12, weight=ft.FontWeight.W_700, selectable=True),
                    ft.Text(description, color="#c7ced8", size=12, selectable=True),
                ],
                spacing=4,
            ),
            bgcolor="#0c1119",
            border_radius=14,
            padding=ft.Padding(12, 10, 12, 10),
        )

    def _load_config(self):
        try:
            self.config_data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
            self.config_comments = self._parse_config_comments(CONFIG_PATH)
            self.server_configs = self._load_server_configs()
            self.status_text.value = str(CONFIG_PATH.resolve())
            self._populate_active_view()
            self._append_terminal("msmp", "配置已加载\n")
        except Exception as exc:
            self._toast(f"加载失败: {exc}")

    def _save_config(self):
        try:
            if CONFIG_PATH.exists():
                CONFIG_PATH.with_suffix(".yml.bak").write_text(CONFIG_PATH.read_text(encoding="utf-8"), encoding="utf-8")
            CONFIG_PATH.write_text(yaml.dump(self.config_data, allow_unicode=True, sort_keys=False), encoding="utf-8")
            self._append_terminal("msmp", "配置已保存\n")
            self._toast("配置已保存")
        except Exception as exc:
            self._toast(f"保存失败: {exc}")

    def _populate_active_view(self):
        if self.active_view_index == 0 and hasattr(self, "config_list"):
            self._populate_bot_config_form()
            self._populate_config_list()
        if self.active_view_index == 3 and hasattr(self, "server_list"):
            self.server_configs = self._load_server_configs()
            self._populate_servers()
        if self.page.controls:
            self._safe_page_update()

    def _flatten_config(self):
        rows = []

        def walk(value, path):
            rows.append((path, value))
            if isinstance(value, dict):
                for key, child in value.items():
                    walk(child, path + [key])
            elif isinstance(value, list):
                for index, child in enumerate(value):
                    walk(child, path + [index])

        for key, value in self.config_data.items():
            walk(value, [key])
        return rows

    def _populate_config_list(self):
        self.config_list.controls.clear()
        self.config_paths = self._flatten_config()
        for path, value in self.config_paths:
            label = ".".join(map(str, path))
            value_text = "{...}" if isinstance(value, dict) else f"[{len(value)}]" if isinstance(value, list) else str(value)
            self.config_list.controls.append(
                ft.Container(
                    content=ft.Row([ft.Text(label, expand=True), ft.Text(value_text, color="#9ba3ad")]),
                    padding=ft.Padding(12, 10, 12, 10),
                    border_radius=14,
                    bgcolor="#182232",
                    on_click=lambda _event, p=path: self._select_config_path(p),
                )
            )

    def _select_config_path(self, path):
        self.selected_config_path = path
        value = self._get_config_value(path)
        self.config_key.value = ".".join(map(str, path))
        self.config_comment.value = self._find_config_comment(path) or "暂无注释。可以在 config.yml 对该配置项上方添加 # 注释。"
        self.config_editor.value = yaml.dump(value, allow_unicode=True, sort_keys=False).strip()
        self._safe_page_update()

    def _apply_config_value(self):
        if not self.selected_config_path:
            self._toast("请先选择配置项")
            return
        try:
            self._set_config_value(self.selected_config_path, yaml.safe_load(self.config_editor.value or ""))
            self._populate_active_view()
            self._toast("已应用到配置树")
        except Exception as exc:
            self._toast(f"应用失败: {exc}")

    def _get_config_value(self, path):
        value = self.config_data
        for part in path:
            value = value[part]
        return value

    def _set_config_value(self, path, value):
        target = self.config_data
        for part in path[:-1]:
            target = target[part]
        target[path[-1]] = value

    def _find_config_comment(self, path):
        for end in range(len(path), 0, -1):
            normalized = [part for part in path[:end] if not isinstance(part, int)]
            key = ".".join(map(str, normalized))
            if key in self.config_comments:
                return self.config_comments[key]
            if key in CONFIG_HELP:
                return CONFIG_HELP[key]
        return ""

    def _parse_config_comments(self, path: Path):
        comments = {}
        pending = []
        stack = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return comments
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("#"):
                pending.append(stripped.lstrip("#").strip())
                continue
            if ":" not in stripped:
                continue
            indent = len(line) - len(line.lstrip(" "))
            key = stripped.split(":", 1)[0].strip().strip('"').strip("'")
            if key.startswith("- "):
                key = key[2:].strip()
            level = indent // 2
            stack = stack[:level]
            stack.append(key)
            if pending:
                comments[".".join(stack)] = "\n".join(pending)
                pending = []
        return comments

    def _populate_servers(self, load_form=True):
        if not hasattr(self, "server_list"):
            return
        servers = self._servers()
        self.server_list.controls.clear()
        for index, server in enumerate(servers):
            key = self._server_key(index + 1, server)
            process = self.mc_processes.get(key)
            running = process and process.poll() is None
            bot_managed = self._is_bot_running() and key in self.bot_managed_server_keys
            selected = index == self.selected_server_index
            server_section = self._server_section(server)
            self.server_list.controls.append(
                ft.Container(
                    content=ft.Column(
                        [
                            ft.Row(
                                [
                                    ft.Text(f"{index + 1}. {server.get('name') or f'server{index + 1}'}", size=17, weight=ft.FontWeight.W_700, expand=True),
                                    ft.Text(
                                        "本地运行" if running else "Bot托管" if bot_managed else "未运行",
                                        color="#b6f3a4" if running or bot_managed else "#9ba3ad",
                                    ),
                                ]
                            ),
                            ft.Text(server_section.get("start_script", "未配置启动脚本"), color="#c7ced8", selectable=True),
                            ft.Text(server_section.get("working_directory", "") or "工作目录: 自动使用脚本目录", color="#7f8996"),
                            ft.Text(f"配置文件: {server.get('_config_file', 'config.yml 内嵌配置')}", color="#7f8996", selectable=True),
                        ],
                        spacing=4,
                    ),
                    padding=14,
                    border_radius=18,
                    bgcolor="#24344d" if selected else "#182232",
                    on_click=lambda _event, i=index: self._select_server(i),
                )
            )
        if load_form:
            self._load_server_form()

    def _servers(self):
        if self.server_configs:
            return self.server_configs
        return []

    def _server_config_dir(self):
        configured = str(self.config_data.get("server_config_dir") or DEFAULT_SERVER_CONFIG_DIR).strip()
        path = Path(configured)
        if not path.is_absolute():
            path = CONFIG_PATH.resolve().parent / path
        return path

    def _load_server_configs(self):
        server_dir = self._server_config_dir()
        if not server_dir.exists():
            return []

        servers = []
        for path in sorted([*server_dir.glob("*.yml"), *server_dir.glob("*.yaml")]):
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                if not isinstance(data, dict):
                    self._append_terminal("msmp", f"跳过服务器配置，顶层不是对象: {path}\n")
                    continue
                missing_sections = [section for section in SERVER_REQUIRED_SECTIONS if not isinstance(data.get(section), dict)]
                if missing_sections:
                    self._append_terminal("msmp", f"跳过服务器配置，缺少完整配置段 {missing_sections}: {path}\n")
                    continue
                data.setdefault("name", path.stem)
                servers.append(self._attach_server_file(data, path))
            except Exception as exc:
                self._append_terminal("msmp", f"读取服务器配置失败 {path}: {exc}\n")
        return servers

    def _attach_server_file(self, server, path):
        data = dict(server)
        if path:
            data["_config_file"] = str(path)
        return data

    def _server_file_path(self, server, index):
        existing = server.get("_config_file")
        if existing:
            return Path(existing)
        name = str(server.get("name") or f"server{index + 1}").strip() or f"server{index + 1}"
        safe_name = "".join(char if char.isalnum() or char in ("-", "_") else "_" for char in name)
        safe_name = safe_name.strip("_") or f"server{index + 1}"
        return self._server_config_dir() / f"{safe_name}.yml"

    def _save_server_file(self, server, index):
        path = self._server_file_path(server, index)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {key: value for key, value in server.items() if not key.startswith("_")}
        path.write_text(yaml.dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
        server["_config_file"] = str(path)
        return path

    def _select_server(self, index):
        self.selected_server_index = index
        self._populate_servers()
        self._safe_page_update()

    def _load_server_form(self):
        if not hasattr(self, "server_fields"):
            return
        servers = self._servers()
        if not servers:
            self._clear_server_form()
            return
        self.selected_server_index = min(self.selected_server_index, len(servers) - 1)
        server = servers[self.selected_server_index]
        server_section = self._server_section(server)
        self.server_fields["name"].value = server.get("name", "")
        self.server_fields["start_script"].value = server_section.get("start_script", "")
        self.server_fields["working_directory"].value = server_section.get("working_directory", "")
        self.server_fields["server.startup_timeout"].value = str(server_section.get("startup_timeout", 300))
        self.server_fields["server.auto_restart_on_crash"].value = bool(server_section.get("auto_restart_on_crash", False))
        self.server_fields["server.crash_restart_delay"].value = str(server_section.get("crash_restart_delay", 10))
        try:
            log_idle_timeout = int(server_section.get("log_idle_restart_timeout", 0) or 0)
        except (TypeError, ValueError):
            log_idle_timeout = 0
        self.server_fields["server.log_idle_restart_enabled"].value = log_idle_timeout > 0
        self.server_fields["server.log_idle_restart_timeout"].value = str(log_idle_timeout)
        self._sync_log_idle_restart_field(update=False)
        for section in ("msmp", "rcon"):
            config = server.get(section) or {}
            self.server_fields[f"{section}.enabled"].value = bool(config.get("enabled", False))
            self.server_fields[f"{section}.host"].value = config.get("host", "")
            self.server_fields[f"{section}.port"].value = str(config.get("port", ""))
            self.server_fields[f"{section}.password"].value = config.get("password", "")
        qq_config = server.get("qq") or {}
        self.server_fields["qq.groups"].value = self._list_to_text(qq_config.get("groups", []))
        self.server_fields["qq.admins"].value = self._list_to_text(qq_config.get("admins", []))
        self.server_fields["qq.welcome_new_members"].value = bool(qq_config.get("welcome_new_members", False))
        self.server_fields["qq.welcome_message"].value = qq_config.get("welcome_message", "")

        commands = server.get("commands") or {}
        self.server_fields["commands.tps_command"].value = commands.get("tps_command", "tps")
        self.server_fields["commands.tps_regex"].value = commands.get("tps_regex", "")
        self.server_fields["commands.tps_group_index"].value = str(commands.get("tps_group_index", 1))
        self.server_fields["commands.tps_show_raw_output"].value = bool(commands.get("tps_show_raw_output", True))
        enabled_commands = commands.get("enabled_commands") or {}
        for name in ("list", "tps", "rules", "status", "help"):
            self.server_fields[f"commands.enabled_commands.{name}"].value = bool(enabled_commands.get(name, True))
        enabled_admin_commands = commands.get("enabled_admin_commands") or {}
        for name in (
            "start", "stop", "kill", "reload", "log", "reconnect", "reconnect_msmp",
            "reconnect_rcon", "crash", "sysinfo", "disk", "process", "network", "listeners",
        ):
            self.server_fields[f"commands.enabled_admin_commands.{name}"].value = bool(enabled_admin_commands.get(name, False))

        notifications = server.get("notifications") or {}
        self.server_fields["notifications.server_events"].value = bool(notifications.get("server_events", True))
        self.server_fields["notifications.player_events"].value = bool(notifications.get("player_events", False))
        self.server_fields["notifications.log_messages"].value = bool(notifications.get("log_messages", False))
        chunk_monitor = notifications.get("chunk_monitor") or {}
        self.server_fields["notifications.chunk_monitor.enabled"].value = bool(chunk_monitor.get("enabled", True))
        self.server_fields["notifications.chunk_monitor.notify_admins"].value = bool(chunk_monitor.get("notify_admins", True))
        self.server_fields["notifications.chunk_monitor.notify_groups"].value = bool(chunk_monitor.get("notify_groups", True))

        advanced = server.get("advanced") or {}
        for key, default in (
            ("reconnect_interval", 300),
            ("heartbeat_interval", 30),
            ("command_cooldown", 3),
            ("max_message_length", 2500),
            ("player_list_cache_ttl", 5),
            ("max_server_logs", 100),
        ):
            self.server_fields[f"advanced.{key}"].value = str(advanced.get(key, default))

        scheduled = server.get("scheduled_tasks") or {}
        self.server_fields["scheduled_tasks.enabled"].value = bool(scheduled.get("enabled", False))
        for task_name in ("auto_start", "auto_stop", "auto_restart"):
            task = scheduled.get(task_name) or {}
            self.server_fields[f"scheduled_tasks.{task_name}.enabled"].value = bool(task.get("enabled", False))
            self.server_fields[f"scheduled_tasks.{task_name}.times"].value = self._list_to_csv(task.get("times", []))
            self.server_fields[f"scheduled_tasks.{task_name}.weekdays"].value = self._list_to_csv(task.get("weekdays", []))
        if hasattr(self, "server_yaml_editor"):
            self.server_yaml_editor.value = yaml.dump(self._server_public_data(server), allow_unicode=True, sort_keys=False)

    def _clear_server_form(self):
        for control in getattr(self, "server_fields", {}).values():
            if hasattr(control, "value"):
                if isinstance(control, (ft.Switch, ft.Checkbox)):
                    control.value = False
                else:
                    control.value = ""
        if hasattr(self, "server_yaml_editor"):
            self.server_yaml_editor.value = ""
        self._sync_log_idle_restart_field(update=False)

    def _apply_server_form(self, refresh=True, notify=True):
        servers = self._servers()
        if not servers:
            self._toast("没有服务器配置，请先新增服务器")
            return False
        self.selected_server_index = min(self.selected_server_index, len(servers) - 1)
        old_server = servers[self.selected_server_index]
        old_config_file = old_server.get("_config_file")
        try:
            server = yaml.safe_load(self.server_yaml_editor.value or "{}") if hasattr(self, "server_yaml_editor") else dict(servers[self.selected_server_index])
        except Exception as exc:
            self._toast(f"服务器 YAML 解析失败: {exc}")
            return False
        if not isinstance(server, dict):
            self._toast("服务器 YAML 顶层必须是对象")
            return False
        if old_config_file:
            server["_config_file"] = old_config_file

        try:
            server["name"] = (self.server_fields["name"].value or "").strip() or server.get("name") or f"server{self.selected_server_index + 1}"
            if self._server_name_exists(server["name"], self.selected_server_index):
                self._toast(f"服务器名称已存在: {server['name']}")
                return False
            server_section = server.setdefault("server", {})
            if not isinstance(server_section, dict):
                self._toast("server 段必须是对象")
                return False
            server_section["start_script"] = self._config_path_value(self.server_fields["start_script"].value)
            server_section["working_directory"] = self._config_path_value(self.server_fields["working_directory"].value)
            server_section["startup_timeout"] = self._read_int_field("server.startup_timeout")
            server_section["auto_restart_on_crash"] = bool(self.server_fields["server.auto_restart_on_crash"].value)
            server_section["crash_restart_delay"] = self._read_int_field("server.crash_restart_delay")
            server_section["log_idle_restart_timeout"] = (
                self._read_int_field("server.log_idle_restart_timeout")
                if self.server_fields["server.log_idle_restart_enabled"].value
                else 0
            )

            for section in ("msmp", "rcon"):
                section_config = server.setdefault(section, {})
                if not isinstance(section_config, dict):
                    self._toast(f"{section} 段必须是对象")
                    return False
                section_config["enabled"] = bool(self.server_fields[f"{section}.enabled"].value)
                section_config["host"] = (self.server_fields[f"{section}.host"].value or "").strip()
                section_config["password"] = (self.server_fields[f"{section}.password"].value or "").strip()
                raw_port = (self.server_fields[f"{section}.port"].value or "").strip()
                if raw_port:
                    section_config["port"] = int(raw_port)
                else:
                    section_config.pop("port", None)

            qq_config = server.setdefault("qq", {})
            qq_config["groups"] = self._parse_int_list_value(self.server_fields["qq.groups"].value, "QQ群号列表")
            qq_config["admins"] = self._parse_int_list_value(self.server_fields["qq.admins"].value, "管理员QQ列表")
            qq_config["welcome_new_members"] = bool(self.server_fields["qq.welcome_new_members"].value)
            qq_config["welcome_message"] = (self.server_fields["qq.welcome_message"].value or "").strip()

            commands = server.setdefault("commands", {})
            commands["tps_command"] = (self.server_fields["commands.tps_command"].value or "").strip() or "tps"
            commands["tps_regex"] = (self.server_fields["commands.tps_regex"].value or "").strip()
            commands["tps_group_index"] = self._read_int_field("commands.tps_group_index")
            commands["tps_show_raw_output"] = bool(self.server_fields["commands.tps_show_raw_output"].value)
            commands["enabled_commands"] = {
                name: bool(self.server_fields[f"commands.enabled_commands.{name}"].value)
                for name in ("list", "tps", "rules", "status", "help")
            }
            commands["enabled_admin_commands"] = {
                name: bool(self.server_fields[f"commands.enabled_admin_commands.{name}"].value)
                for name in (
                    "start", "stop", "kill", "reload", "log", "reconnect", "reconnect_msmp",
                    "reconnect_rcon", "crash", "sysinfo", "disk", "process", "network", "listeners",
                )
            }

            notifications = server.setdefault("notifications", {})
            notifications["server_events"] = bool(self.server_fields["notifications.server_events"].value)
            notifications["player_events"] = bool(self.server_fields["notifications.player_events"].value)
            notifications["log_messages"] = bool(self.server_fields["notifications.log_messages"].value)
            chunk_monitor = notifications.setdefault("chunk_monitor", {})
            chunk_monitor["enabled"] = bool(self.server_fields["notifications.chunk_monitor.enabled"].value)
            chunk_monitor["notify_admins"] = bool(self.server_fields["notifications.chunk_monitor.notify_admins"].value)
            chunk_monitor["notify_groups"] = bool(self.server_fields["notifications.chunk_monitor.notify_groups"].value)

            advanced = server.setdefault("advanced", {})
            for key in (
                "reconnect_interval",
                "heartbeat_interval",
                "command_cooldown",
                "max_message_length",
                "player_list_cache_ttl",
                "max_server_logs",
            ):
                advanced[key] = self._read_int_field(f"advanced.{key}")

            scheduled = server.setdefault("scheduled_tasks", {})
            scheduled["enabled"] = bool(self.server_fields["scheduled_tasks.enabled"].value)
            for task_name in ("auto_start", "auto_stop", "auto_restart"):
                task = scheduled.setdefault(task_name, {})
                task["enabled"] = bool(self.server_fields[f"scheduled_tasks.{task_name}.enabled"].value)
                task["times"] = self._split_list_value(self.server_fields[f"scheduled_tasks.{task_name}.times"].value)
                task["weekdays"] = self._parse_int_list_value(self.server_fields[f"scheduled_tasks.{task_name}.weekdays"].value, f"{task_name} weekdays")
        except ValueError as exc:
            self._toast(str(exc))
            return False
        path = self._save_server_file(server, self.selected_server_index)
        self.server_configs = self._load_server_configs()
        if refresh:
            self._populate_servers()
            self._populate_active_view()
        else:
            if hasattr(self, "server_yaml_editor"):
                current = self.server_configs[self.selected_server_index] if self.server_configs else server
                self.server_yaml_editor.value = yaml.dump(self._server_public_data(current), allow_unicode=True, sort_keys=False)
            self._sync_log_idle_restart_field(update=False)
        if notify:
            self._append_terminal("msmp", f"服务器配置已保存: {path}\n")
        return True

    def _add_server(self):
        servers = self._servers()
        index = len(servers) + 1
        server = self._new_server_config(index)
        path = self._save_server_file(server, index - 1)
        self.server_configs = self._load_server_configs()
        self.selected_server_index = self._server_index_by_config_file(path, fallback=index - 1)
        self._populate_servers()
        self._safe_page_update()

    def _remove_selected_server(self):
        servers = self._servers()
        if not servers:
            return
        self.selected_server_index = min(max(self.selected_server_index, 0), len(servers) - 1)
        server = servers[self.selected_server_index]
        path = server.get("_config_file")
        try:
            if path and Path(path).exists():
                Path(path).unlink()
                self._append_terminal("msmp", f"已删除服务器配置文件: {path}\n")
            self.server_configs = self._load_server_configs()
            self.selected_server_index = min(max(0, self.selected_server_index - 1), max(len(self.server_configs) - 1, 0))
            self._populate_servers()
            self._safe_page_update()
            self._toast("服务器配置已删除")
        except Exception as exc:
            self._toast(f"删除服务器配置失败: {exc}")

    def _server_index_by_config_file(self, path, fallback=0):
        target = str(Path(path).resolve()) if path else ""
        for index, server in enumerate(self.server_configs):
            current = server.get("_config_file")
            if current and str(Path(current).resolve()) == target:
                return index
        return min(max(int(fallback or 0), 0), max(len(self.server_configs) - 1, 0))

    async def _pick_start_script(self):
        files = await self.script_picker.pick_files(allow_multiple=False)
        if files:
            self.server_fields["start_script"].value = self._config_path_value(files[0].path)
            self._safe_page_update()

    async def _pick_workdir(self):
        path = await self.workdir_picker.get_directory_path()
        if path:
            self.server_fields["working_directory"].value = self._config_path_value(path)
            self._safe_page_update()

    def _config_path_value(self, value):
        return str(value or "").strip().replace("\\", "/")

    def _server_section(self, server):
        section = server.get("server")
        return section if isinstance(section, dict) else {}

    def _server_name_exists(self, name, current_index):
        target = str(name or "").strip().lower()
        if not target:
            return False
        for index, server in enumerate(self._servers()):
            if index == current_index:
                continue
            if str(server.get("name") or "").strip().lower() == target:
                return True
        return False

    def _server_public_data(self, server):
        return {key: value for key, value in server.items() if not str(key).startswith("_")}

    def _get_nested(self, data, path, default=None):
        value = data
        for part in path.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value

    def _set_nested(self, data, path, value):
        parts = path.split(".")
        target = data
        for part in parts[:-1]:
            child = target.get(part)
            if not isinstance(child, dict):
                child = {}
                target[part] = child
            target = child
        target[parts[-1]] = value

    def _split_list_value(self, value):
        normalized = str(value or "").replace("，", ",").replace("\n", ",")
        return [item.strip() for item in normalized.split(",") if item.strip()]

    def _parse_int_list_value(self, value, field_name):
        result = []
        for item in self._split_list_value(value):
            try:
                result.append(int(item))
            except ValueError as exc:
                raise ValueError(f"{field_name} 只能填写数字列表") from exc
        return result

    def _list_to_text(self, values):
        if not isinstance(values, list):
            return ""
        return "\n".join(str(value) for value in values)

    def _list_to_csv(self, values):
        if not isinstance(values, list):
            return ""
        return ",".join(str(value) for value in values)

    def _set_field_value(self, key, value):
        if key in self.server_fields:
            self.server_fields[key].value = value

    def _on_server_toggle_changed(self, key, _event=None):
        if key == "server.log_idle_restart_enabled":
            self._sync_log_idle_restart_field(update=False)
        if self._apply_server_form(refresh=False, notify=False):
            self._toast("开关已自动保存")
        elif self.page.controls:
            self._safe_page_update()

    def _on_log_idle_restart_changed(self, _event=None):
        self._sync_log_idle_restart_field(update=True)

    def _sync_log_idle_restart_field(self, update=False):
        if not hasattr(self, "server_fields"):
            return
        enabled = bool(self.server_fields["server.log_idle_restart_enabled"].value)
        timeout_field = self.server_fields["server.log_idle_restart_timeout"]
        timeout_field.disabled = not enabled
        if enabled and not str(timeout_field.value or "").strip():
            timeout_field.value = "300"
        if enabled and str(timeout_field.value or "").strip() == "0":
            timeout_field.value = "300"
        if update and self.page.controls:
            self._safe_page_update()

    def _read_int_field(self, key):
        raw = str(self.server_fields[key].value or "").strip()
        if not raw:
            return 0
        try:
            return int(raw)
        except ValueError as exc:
            raise ValueError(f"{key} 必须是数字") from exc

    def _new_server_config(self, index):
        return {
            "name": f"server{index}",
            "msmp": {
                "enabled": False,
                "host": "localhost",
                "port": 21111 + index - 1,
                "password": "",
            },
            "rcon": {
                "enabled": True,
                "host": "localhost",
                "port": 25575 + index - 1,
                "password": "",
            },
            "server": {
                "start_script": "",
                "working_directory": "",
                "startup_timeout": 300,
                "auto_restart_on_crash": False,
                "crash_restart_delay": 10,
                "log_idle_restart_timeout": 0,
            },
            "qq": {
                "groups": [],
                "admins": [],
                "welcome_new_members": False,
                "welcome_message": "{at} 欢迎新成员加入！输入 help 查看可用命令",
            },
            "commands": {
                "tps_command": "tps",
                "tps_regex": r"TPS from last 1m, 5m, 15m:\s*([\d.]+)",
                "tps_group_index": 1,
                "tps_show_raw_output": True,
                "enabled_commands": {
                    "list": True,
                    "tps": True,
                    "rules": True,
                    "status": True,
                    "help": True,
                },
                "enabled_admin_commands": {
                    "start": False,
                    "stop": False,
                    "kill": False,
                    "reload": False,
                    "log": False,
                    "reconnect": False,
                    "reconnect_msmp": False,
                    "reconnect_rcon": False,
                    "crash": False,
                    "sysinfo": False,
                    "disk": False,
                    "process": False,
                    "network": False,
                    "listeners": False,
                },
            },
            "notifications": {
                "server_events": True,
                "player_events": False,
                "log_messages": False,
                "chunk_monitor": {
                    "enabled": True,
                    "notify_admins": True,
                    "notify_groups": True,
                },
            },
            "advanced": {
                "reconnect_interval": 300,
                "heartbeat_interval": 30,
                "command_cooldown": 3,
                "max_message_length": 2500,
                "player_list_cache_ttl": 5,
                "max_server_logs": 100,
            },
            "scheduled_tasks": {
                "enabled": False,
                "auto_start": {"enabled": False, "times": [], "weekdays": [], "pre_notify_seconds": 300, "notify_message": "服务器将在 {countdown} 秒后启动，请做好准备"},
                "auto_stop": {"enabled": False, "times": [], "weekdays": [], "warning_before_seconds": 600, "first_warning": "服务器将在 {countdown} 秒后关闭，请保存游戏", "second_warning": "服务器即将在 1 分钟后关闭", "immediate_message": "服务器正在关闭"},
                "auto_restart": {"enabled": False, "times": [], "weekdays": [], "warning_before_seconds": 600, "first_warning": "服务器将在 {countdown} 秒后重启，请保存游戏", "second_warning": "服务器即将在 1 分钟后重启", "immediate_message": "服务器正在重启", "wait_before_startup": 10, "restart_success_message": "服务器已重启，欢迎回来！"},
            },
            "custom_commands": {"enabled": False, "rules": []},
            "custom_listeners": {"enabled": False, "rules": []},
            "debug": False,
        }

    def _refresh_plugin_repo(self):
        self._toast("正在刷新插件仓库...")
        self._append_terminal("msmp", "正在从 GitHub 插件仓库获取插件列表...\n")
        self.page.run_thread(self._fetch_plugin_repo)

    def _fetch_plugin_repo(self):
        try:
            items = self._github_json(PLUGIN_REPO_API, timeout=15)
            if not isinstance(items, list):
                raise ValueError(f"GitHub API 返回异常: {items}")
            plugins = [item for item in items if item.get("name", "").endswith(".py") or item.get("type") == "dir"]
        except Exception as api_exc:
            self._append_terminal("msmp", f"GitHub API 获取失败，尝试网页兜底解析: {api_exc}\n")
            try:
                plugins = self._fetch_plugin_repo_from_html()
            except Exception as html_exc:
                self._toast(f"插件仓库刷新失败: {html_exc}")
                self._append_terminal("msmp", f"插件仓库刷新失败: API={api_exc}; HTML={html_exc}\n")
                return

        try:
            for plugin in plugins:
                metadata = self._fetch_remote_metadata_for_repo_item(plugin)
                plugin.update(metadata)
            self.remote_plugin_items = plugins
            self._populate_plugins(plugins)
            self._safe_page_update()
            self._toast(f"插件仓库已刷新，共 {len(plugins)} 个插件")
            self._append_terminal("msmp", f"插件仓库已刷新，共 {len(plugins)} 个插件\n")
        except Exception as exc:
            self._toast(f"插件仓库刷新失败: {exc}")
            self._append_terminal("msmp", f"插件仓库刷新失败: {exc}\n")

    def _github_request(self, url, timeout=15):
        headers = {
            "User-Agent": f"{APP_NAME}/{APP_VERSION}",
            "Accept": "application/vnd.github+json, text/html;q=0.9, */*;q=0.8",
        }
        return urllib.request.Request(url, headers=headers)

    def _github_read_text(self, url, timeout=15):
        return self._github_read_bytes(url, timeout=timeout).decode("utf-8", errors="replace")

    def _github_read_bytes(self, url, timeout=15):
        with urllib.request.urlopen(self._github_request(url, timeout), timeout=timeout) as response:
            return response.read()

    def _github_json(self, url, timeout=15):
        return json.loads(self._github_read_text(url, timeout=timeout))

    def _fetch_plugin_repo_from_html(self):
        html = self._github_read_text(PLUGIN_REPO_HTML, timeout=15)
        names = sorted(set(re.findall(r'href="/intellectmind/MSMP_QQBot-Plugins/blob/[^"]+/([^"/]+\.py)"', html)))
        if not names:
            names = sorted(set(re.findall(r'>([A-Za-z0-9_]+\.py)<', html)))
        if not names:
            raise ValueError("GitHub 页面中未解析到 .py 插件")
        return [
            {
                "name": name,
                "type": "file",
                "download_url": f"{PLUGIN_RAW_BASE}/{name}",
            }
            for name in names
        ]

    def _fetch_remote_metadata_for_repo_item(self, item):
        name = item.get("name", "")
        try:
            if item.get("type") == "dir":
                return self._fetch_remote_dir_metadata(name, item.get("url", ""))
            url = item.get("download_url") or f"{PLUGIN_RAW_BASE}/{name}"
            source = self._github_read_text(url, timeout=12)
            return self._parse_plugin_metadata_from_source(source, name)
        except Exception as exc:
            self._append_terminal("msmp", f"读取远程插件元信息失败 {name}: {exc}\n", update=False)
            return self._remote_plugin_metadata(name)

    def _populate_plugins(self, plugins):
        self._set_remote_plugin_items(plugins)
        if not self._plugin_view_active():
            return
        self.plugin_list.controls.clear()
        local_entries = self._local_plugin_entries()
        local_files = set(local_entries.keys())
        local_storage_names = {self._plugin_storage_name(name) for name in local_files}
        remote_names = set(self.remote_plugins.keys())
        remote_storage_names = {self._plugin_storage_name(name) for name in remote_names}
        for plugin in self.remote_plugin_items:
            name = plugin.get("name", "")
            storage_name = self._plugin_storage_name(name)
            status = "已安装" if storage_name in local_storage_names else "可下载"
            self._add_plugin_row(name, plugin.get("type", ""), status)
        for name in sorted(local_files - remote_names):
            if self._plugin_storage_name(name) in remote_storage_names:
                continue
            self._add_plugin_row(name, local_entries[name], "本地")

    def _set_remote_plugin_items(self, plugins):
        self.remote_plugin_items = list(plugins or [])
        self.remote_plugins = {
            plugin.get("name", ""): plugin
            for plugin in self.remote_plugin_items
            if plugin.get("name")
        }

    def _plugin_view_active(self):
        return (
            self.active_view_index == 2
            and hasattr(self, "plugin_list")
            and hasattr(self, "plugin_config_list")
        )

    def _refresh_local_plugins(self):
        self._populate_plugins(list(self.remote_plugin_items or self.remote_plugins.values()))
        if self._plugin_view_active():
            self._update_selected_plugin_detail()
            self._load_plugin_config_form(update=False)
        self._toast("本地插件列表已刷新")
        self._safe_page_update()

    def _local_plugin_exists(self, name):
        return self._local_plugin_source_path(name) is not None or (PLUGIN_DIR / str(name or "")).exists()

    def _local_plugin_source_path(self, name):
        storage_name = self._plugin_storage_name(name)
        direct_file = PLUGIN_DIR / f"{storage_name}.py"
        if direct_file.exists():
            return direct_file
        direct_dir = PLUGIN_DIR / storage_name
        package_file = direct_dir / f"{storage_name}.py"
        if package_file.exists():
            return package_file
        if direct_dir.exists():
            candidates = [
                child for child in direct_dir.rglob("*.py")
                if not child.name.startswith("_") and "__pycache__" not in child.parts
            ]
            return sorted(candidates, key=lambda item: len(item.parts))[0] if candidates else None
        raw_path = PLUGIN_DIR / str(name or "")
        return raw_path if raw_path.exists() and raw_path.suffix == ".py" else None

    def _plugin_metadata(self, name):
        metadata = {
            "display_name": self._plugin_storage_name(name),
            "version": "未知",
            "author": "未知",
            "description": "暂无描述",
        }
        source_path = self._local_plugin_source_path(name)
        if not source_path or not source_path.exists():
            return metadata
        try:
            tree = ast.parse(source_path.read_text(encoding="utf-8"))
        except Exception:
            return metadata

        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            class_values = self._class_literal_assignments(node)
            if {"name", "version", "author", "description"} & class_values.keys():
                metadata["display_name"] = str(class_values.get("name") or metadata["display_name"])
                metadata["version"] = str(class_values.get("version") or metadata["version"])
                metadata["author"] = str(class_values.get("author") or metadata["author"])
                metadata["description"] = str(class_values.get("description") or metadata["description"])
                return metadata
        return metadata

    def _class_literal_assignments(self, class_node):
        values = {}
        wanted = {"name", "version", "author", "description"}
        for statement in class_node.body:
            target_name = ""
            value_node = None
            if isinstance(statement, ast.Assign) and len(statement.targets) == 1 and isinstance(statement.targets[0], ast.Name):
                target_name = statement.targets[0].id
                value_node = statement.value
            elif isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
                target_name = statement.target.id
                value_node = statement.value
            if target_name not in wanted or value_node is None:
                continue
            try:
                value = ast.literal_eval(value_node)
            except Exception:
                continue
            if isinstance(value, str):
                values[target_name] = value
        return values

    def _remote_plugin_metadata(self, name):
        item = self.remote_plugins.get(name, {})
        metadata = {
            "display_name": self._plugin_storage_name(name),
            "version": "未知",
            "author": "未知",
            "description": "远程插件，下载后可读取本地元信息",
        }
        for key in ("name", "version", "author", "description"):
            if item.get(key):
                metadata["display_name" if key == "name" else key] = str(item[key])
        return metadata

    def _plugin_display_metadata(self, name):
        if self._local_plugin_exists(name):
            return self._plugin_metadata(name)
        return self._remote_plugin_metadata(name)

    def _plugin_server_install_status(self, name, base_status):
        local = self._local_plugin_exists(name)
        server_config = self._plugin_server_config_path(name)
        if local and server_config.exists() and self._plugin_server_disabled(name):
            return "当前服务器已卸载"
        if local and server_config.exists():
            return "当前服务器已安装"
        if local:
            return "本地已有，当前服务器默认启用"
        return base_status or "可下载"

    def _plugin_server_disabled(self, name):
        config = self._read_json_dict(self._plugin_server_config_path(name))
        return config.get("enabled") is False

    def _local_plugin_entries(self):
        """列出本地真实可加载插件，和插件管理器的去重规则保持一致。"""
        entries = {}
        if not PLUGIN_DIR.exists():
            return entries
        for path in sorted(PLUGIN_DIR.iterdir(), key=lambda item: item.name.lower()):
            if path.name.startswith("_") or path.name == "__pycache__":
                continue
            if path.is_file():
                if path.suffix != ".py":
                    continue
                package_entry = PLUGIN_DIR / path.stem / f"{path.stem}.py"
                if package_entry.exists():
                    continue
                entries[path.name] = "file"
            elif path.is_dir():
                package_entry = path / f"{path.name}.py"
                if package_entry.exists() or any(child.suffix == ".py" and not child.name.startswith("_") for child in path.rglob("*.py")):
                    entries[path.name] = "dir"
        return entries

    def _add_plugin_row(self, name, plugin_type, status):
        selected = name == self.selected_plugin
        metadata = self._plugin_display_metadata(name)
        server_status = self._plugin_server_install_status(name, status)
        description = metadata.get("description") or "暂无描述"
        if len(description) > 46:
            description = f"{description[:46]}..."
        self.plugin_list.controls.append(
            ft.Container(
                content=ft.Column(
                    [
                        ft.Row(
                            [
                                ft.Text(metadata["display_name"], expand=True, weight=ft.FontWeight.W_700),
                                ft.Text(f"v{metadata['version']}", color="#9dd6ff", size=12),
                                ft.Text(server_status, color="#f6c56a", size=12),
                            ],
                            spacing=8,
                        ),
                        ft.Text(f"文件: {name}  类型: {plugin_type}  作者: {metadata['author']}", color="#9ba3ad", size=12),
                        ft.Text(description, color="#7f8996", size=12),
                    ],
                    spacing=4,
                ),
                padding=ft.Padding(14, 12, 14, 12),
                border_radius=16,
                bgcolor="#24344d" if selected else "#182232",
                on_click=lambda _event, n=name, t=plugin_type, s=server_status: self._select_plugin(n, t, s),
            )
        )

    def _select_plugin(self, name, plugin_type, status):
        self.selected_plugin = name
        self.selected_plugin_type = plugin_type
        self.selected_plugin_status = status
        self._update_selected_plugin_detail()
        self._populate_plugins(list(self.remote_plugins.values()))
        self._load_plugin_config_form(update=False)
        self._safe_page_update()

    def _update_selected_plugin_detail(self):
        if not hasattr(self, "plugin_detail") or not self.selected_plugin:
            return
        metadata = self._plugin_display_metadata(self.selected_plugin)
        server_name = self._selected_server_prefix("plugin_server_index") or "default"
        root_config = self._plugin_root_config_path(self.selected_plugin)
        server_config = self._plugin_server_config_path(self.selected_plugin)
        local_path = self._local_plugin_source_path(self.selected_plugin) or (PLUGIN_DIR / self.selected_plugin)
        install_state = self._plugin_server_install_status(self.selected_plugin, self.selected_plugin_status)
        self.plugin_detail.value = "\n".join([
            f"插件: {metadata['display_name']}",
            f"文件/目录: {self.selected_plugin}",
            f"版本: {metadata['version']}",
            f"作者: {metadata['author']}",
            f"描述: {metadata['description']}",
            f"类型: {self.selected_plugin_type or '未知'}",
            f"当前服务器: {server_name}",
            f"安装状态: {install_state}",
            f"插件代码: {Path(local_path).resolve()}",
            f"根配置: {root_config.resolve()}",
            f"当前服务器配置: {server_config.resolve()}",
        ])

    def _load_plugin_config_form(self, update=True):
        if not hasattr(self, "plugin_config_list"):
            return
        self.plugin_config_fields = {}
        self.plugin_config_list.controls.clear()
        if not self.selected_plugin:
            self.plugin_config_data = {}
            self.plugin_config_path_text.value = "配置文件: 请先选择插件"
            self.plugin_config_list.controls.append(ft.Text("选择左侧插件后可编辑当前服务器的独立配置。", color="#9ba3ad"))
            if update and self.page.controls:
                self._safe_page_update()
            return

        config_path = self._plugin_server_config_path(self.selected_plugin)
        root_config_path = self._plugin_root_config_path(self.selected_plugin)
        config = self._default_plugin_config(self.selected_plugin)
        if root_config_path.exists():
            config = self._deep_merge(config, self._read_json_dict(root_config_path))
        if config_path.exists():
            config = self._deep_merge(config, self._read_json_dict(config_path))
        self.plugin_config_data = config
        server_name = self._selected_server_prefix("plugin_server_index") or "default"
        config_state = "已创建" if config_path.exists() else "未创建，保存后生成"
        self.plugin_config_path_text.value = (
            f"当前服务器: {server_name}\n"
            f"服务器配置: {config_path.resolve()} ({config_state})\n"
            f"默认根配置: {root_config_path.resolve()}"
        )

        if not config:
            self.plugin_config_list.controls.append(
                ft.Text("此插件暂未发现可视化配置。可保存后创建当前服务器的 config.json。", color="#9ba3ad")
            )
        else:
            self._render_plugin_config_node(config, [])

        if update and self.page.controls:
            self._safe_page_update()

    def _render_plugin_config_node(self, value, path):
        if isinstance(value, dict):
            if path:
                self.plugin_config_list.controls.append(
                    self._section_title(self._plugin_config_label(path), self._plugin_config_help(path))
                )
            for key, child in value.items():
                self._render_plugin_config_node(child, [*path, key])
            return

        label = self._plugin_config_label(path)
        help_text = self._plugin_config_help(path)
        key = ".".join(path)
        if isinstance(value, bool):
            control = ft.Switch(label=label, value=value, on_change=lambda _: self._save_plugin_config_form())
        elif isinstance(value, (list, dict)):
            control = ft.TextField(
                label=label,
                value=json.dumps(value, ensure_ascii=False, indent=2),
                multiline=True,
                min_lines=2,
                max_lines=8,
            )
        else:
            control = ft.TextField(
                label=label,
                value="" if value is None else str(value),
                multiline=isinstance(value, str) and len(value) > 80,
                min_lines=2 if isinstance(value, str) and len(value) > 80 else 1,
                max_lines=5 if isinstance(value, str) and len(value) > 80 else 1,
            )
        self.plugin_config_fields[key] = (path, control, type(value))
        self.plugin_config_list.controls.append(
            ft.Container(
                content=ft.Column(
                    [
                        control,
                        ft.Text(help_text or "暂无说明。", color="#7f8996", size=12, selectable=True),
                    ],
                    spacing=4,
                ),
                bgcolor="#0c1119",
                border_radius=16,
                padding=ft.Padding(12, 10, 12, 10),
            )
        )

    def _save_plugin_config_form(self):
        if not self.selected_plugin:
            self._toast("请先选择插件")
            return
        try:
            config = copy.deepcopy(self.plugin_config_data)
            for key, (path, control, value_type) in self.plugin_config_fields.items():
                value = self._plugin_control_value(control, value_type)
                self._set_nested_value(config, path, value)
            config_path = self._plugin_server_config_path(self.selected_plugin)
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
            self.plugin_config_data = config
            self.plugin_config_path_text.value = f"当前服务器配置: {config_path.resolve()}"
            self._append_terminal("msmp", f"插件配置已保存: {config_path.resolve()}\n")
            self._toast("插件配置已保存；如 Bot 正在运行，请重载插件或重启 Bot 生效")
            self._populate_plugins(list(self.remote_plugins.values()))
            self._update_selected_plugin_detail()
            self._load_plugin_config_form(update=False)
            self._safe_page_update()
        except Exception as exc:
            self._toast(f"保存插件配置失败: {exc}")

    def _plugin_control_value(self, control, value_type):
        if value_type is bool:
            return bool(control.value)
        raw = control.value
        if value_type is int:
            return int(str(raw or "0").strip())
        if value_type is float:
            return float(str(raw or "0").strip())
        if value_type in (list, dict):
            parsed = yaml.safe_load(raw or "null")
            if not isinstance(parsed, value_type):
                raise ValueError(f"{control.label} 必须是 {value_type.__name__}")
            return parsed
        return "" if raw is None else str(raw)

    def _plugin_config_label(self, path):
        return ".".join(path)

    def _plugin_config_help(self, path):
        key = ".".join(path)
        for end in range(len(path), 0, -1):
            candidate = ".".join(path[:end])
            if candidate in PLUGIN_CONFIG_HELP:
                return PLUGIN_CONFIG_HELP[candidate]
        return ""

    def _plugin_root_config_path(self, name):
        storage_name = self._plugin_storage_name(name)
        return PLUGIN_DIR / storage_name / "config.json"

    def _plugin_server_config_path(self, name, server_index=None):
        storage_name = self._plugin_storage_name(name)
        server_key = self._selected_server_storage_key("plugin_server_index", server_index)
        return PLUGIN_DIR / storage_name / "servers" / self._safe_storage_name(server_key) / "config.json"

    def _selected_server_storage_key(self, state_attr="selected_server_index", server_index=None):
        servers = self._servers()
        if not servers:
            return "default"
        if server_index is None:
            index = self._bounded_server_index(state_attr)
        else:
            index = min(max(int(server_index), 0), len(servers) - 1)
        server = servers[index]
        return str(server.get("_config_file") or server.get("name") or f"server{index + 1}")

    def _plugin_storage_name(self, name):
        raw = str(name or "").strip()
        if raw.endswith(".py"):
            raw = raw[:-3]
        if "." in raw:
            raw = raw.split(".", 1)[0]
        return self._safe_storage_name(raw or "unknown_plugin")

    def _safe_storage_name(self, value):
        text = str(value or "default").replace("\\", "/").strip().strip("/")
        text = text or "default"
        safe = "".join(char if char.isalnum() or char in ("-", "_", ".") else "_" for char in text)
        safe = safe.strip("._") or "default"
        if safe != text or len(safe) > 80:
            digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]
            safe = safe[:70].strip("._") or "default"
            return f"{safe}_{digest}"
        return safe

    def _read_json_dict(self, path):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _deep_merge(self, base, override):
        result = copy.deepcopy(base) if isinstance(base, dict) else {}
        if not isinstance(override, dict):
            return result
        for key, value in override.items():
            if isinstance(value, dict) and isinstance(result.get(key), dict):
                result[key] = self._deep_merge(result[key], value)
            else:
                result[key] = copy.deepcopy(value)
        return result

    def _set_nested_value(self, target, path, value):
        current = target
        for part in path[:-1]:
            current = current.setdefault(part, {})
        current[path[-1]] = value

    def _default_plugin_config(self, name):
        storage_name = self._plugin_storage_name(name)
        defaults = {
            "qq_mc_binding": {
                "enabled": True,
                "max_bindings_per_qq": 1,
                "verify_timeout": 300,
                "verify_code_length": 6,
                "chat_message_pattern": r".*\[Not Secure\]\s*<([^>]+)>\s*(.+)",
            },
            "mc_qq_sync": {
                "enabled": True,
                "features": {
                    "mc_auto_sync_to_qq": {"enabled": False, "group_ids": []},
                    "mc_manual_sync_to_qq": {"enabled": True, "group_ids": []},
                    "qq_manual_to_mc": {"enabled": True, "group_ids": []},
                },
                "message_format": {
                    "mc_auto_to_qq": "[MC] {player}: {message}",
                    "mc_manual_to_qq": "[MC] {player}: {message}",
                    "qq_manual_to_mc": "[QQ] {nickname}: {message}",
                },
                "qq_commands": {"mc_command_prefix": "mc"},
                "mc_commands": {"qq_command_prefix": "qq"},
                "blacklist": {"players": [], "users": []},
            },
            "whitelist_audit": {
                "enabled": True,
                "ai_api_url": "你的api接口",
                "ai_api_key": "your-api-key-here",
                "ai_model": "自己填模型",
                "allowed_groups": [],
                "cooldown_seconds": 3600,
                "pass_score": 60,
                "question_count": 10,
                "ai_timeout": 60,
                "answer_timeout": 180,
                "use_ai_questions": True,
                "max_whitelist_per_qq": 1,
                "question_prompt": "出{question_count}个我的世界服务器进服审核题目。",
                "default_questions": [],
                "custom_whitelist_commands": {
                    "add_command": "whitelist add {player}",
                    "remove_command": "whitelist remove {player}",
                    "list_command": "whitelist list",
                    "on_command": "whitelist on",
                    "off_command": "whitelist off",
                    "reload_command": "whitelist reload",
                },
            },
            "chunk_deleter": {
                "enabled": True,
                "allowed_dimensions": ["overworld", "nether", "end"],
                "require_confirmation": True,
                "backup_before_delete": True,
                "confirmation_timeout": 180,
            },
        }
        config = copy.deepcopy(defaults.get(storage_name, {}))
        config.setdefault("enabled", True)
        return config

    def _download_selected_plugin(self):
        if not self.selected_plugin:
            self._toast("请先选择插件")
            return
        plugin_name = self.selected_plugin
        server_index = self._bounded_server_index("plugin_server_index")
        self.page.run_thread(lambda: self._download_plugin_file(plugin_name, server_index))

    def _download_plugin_file(self, name, server_index=None):
        try:
            PLUGIN_DIR.mkdir(exist_ok=True)
            item = self.remote_plugins.get(name, {})
            local_exists = self._local_plugin_exists(name)
            local_version = self._plugin_metadata(name).get("version", "未知") if local_exists else "未知"
            file_payload = None
            remote_metadata = self._remote_plugin_metadata(name)

            if local_exists and not item:
                config_path = self._ensure_plugin_server_config(name, server_index)
                self._append_terminal("msmp", f"插件本地已存在，已安装到当前服务器: {name} (本地 v{local_version})\n")
                self._append_terminal("msmp", f"当前服务器配置: {config_path.resolve()}\n")
                self._refresh_plugin_ui_after_install(name)
                self._toast(f"插件已安装到当前服务器: {name}")
                return
            if not local_exists and not item:
                raise ValueError(f"未找到远程插件信息，请先刷新插件仓库: {name}")

            try:
                remote_metadata, file_payload = self._fetch_remote_plugin_package(name, item)
            except Exception as exc:
                if not local_exists:
                    raise
                self._append_terminal("msmp", f"远程版本检查失败，按本地插件安装到当前服务器: {exc}\n")

            if local_exists:
                remote_version = remote_metadata.get("version", "未知")
                if not self._is_remote_version_newer(remote_version, local_version):
                    config_path = self._ensure_plugin_server_config(name, server_index)
                    if remote_version == "未知":
                        self._append_terminal(
                            "msmp",
                            f"插件本地已存在，远程版本不可确认，跳过下载并安装到当前服务器: {name} "
                            f"(本地 v{local_version})\n",
                        )
                    else:
                        self._append_terminal(
                            "msmp",
                            f"插件本地已存在且无更新，跳过下载: {name} "
                            f"(本地 v{local_version}, 远程 v{remote_version})\n",
                        )
                    self._append_terminal("msmp", f"当前服务器配置: {config_path.resolve()}\n")
                    self._refresh_plugin_ui_after_install(name)
                    self._toast(f"插件已安装到当前服务器: {name}")
                    return

            if item.get("type") == "dir":
                self._download_plugin_dir(item["url"], PLUGIN_DIR / name)
            else:
                if file_payload is None:
                    url = item.get("download_url") or f"{PLUGIN_RAW_BASE}/{name}"
                    file_payload = self._github_read_bytes(url, timeout=20)
                target = self._local_plugin_source_path(name) if local_exists else PLUGIN_DIR / name
                temp_target = target.with_name(f".{target.name}.download_tmp")
                temp_target.write_bytes(file_payload)
                os.replace(temp_target, target)
            config_path = self._ensure_plugin_server_config(name, server_index)
            action = "插件已更新并安装到当前服务器" if local_exists else "插件已下载并安装到当前服务器"
            self._append_terminal("msmp", f"{action}: {name}\n")
            self._append_terminal("msmp", f"当前服务器配置: {config_path.resolve()}\n")
            self._refresh_plugin_ui_after_install(name)
            self._toast(f"{action}: {name}")
        except Exception as exc:
            self._toast(f"下载失败: {exc}")

    def _fetch_remote_plugin_package(self, name, item):
        if not item:
            return self._remote_plugin_metadata(name), None
        if item.get("type") == "dir":
            metadata = self._fetch_remote_dir_metadata(name, item.get("url", ""))
            return metadata, None

        url = item.get("download_url") or f"{PLUGIN_RAW_BASE}/{name}"
        payload = self._github_read_bytes(url, timeout=20)
        metadata = self._parse_plugin_metadata_from_source(payload.decode("utf-8", errors="replace"), name)
        return metadata, payload

    def _fetch_remote_dir_metadata(self, name, api_url):
        if not api_url:
            return self._remote_plugin_metadata(name)
        items = self._github_json(api_url, timeout=20)
        storage_name = self._plugin_storage_name(name)
        py_items = [item for item in items if item.get("type") == "file" and item.get("name", "").endswith(".py")]
        main_item = next((item for item in py_items if item.get("name") == f"{storage_name}.py"), None)
        main_item = main_item or (py_items[0] if py_items else None)
        if not main_item or not main_item.get("download_url"):
            return self._remote_plugin_metadata(name)
        source = self._github_read_text(main_item["download_url"], timeout=20)
        return self._parse_plugin_metadata_from_source(source, name)

    def _parse_plugin_metadata_from_source(self, source, fallback_name):
        metadata = {
            "display_name": self._plugin_storage_name(fallback_name),
            "version": "未知",
            "author": "未知",
            "description": "暂无描述",
        }
        try:
            tree = ast.parse(source)
        except Exception:
            return metadata
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            class_values = self._class_literal_assignments(node)
            if {"name", "version", "author", "description"} & class_values.keys():
                metadata["display_name"] = str(class_values.get("name") or metadata["display_name"])
                metadata["version"] = str(class_values.get("version") or metadata["version"])
                metadata["author"] = str(class_values.get("author") or metadata["author"])
                metadata["description"] = str(class_values.get("description") or metadata["description"])
                return metadata
        return metadata

    def _is_remote_version_newer(self, remote_version, local_version):
        if not remote_version or remote_version == "未知":
            return False
        if not local_version or local_version == "未知":
            return True
        return self._version_key(remote_version) > self._version_key(local_version)

    def _version_key(self, value):
        numbers = [int(part) for part in re.findall(r"\d+", str(value or ""))]
        while len(numbers) < 3:
            numbers.append(0)
        return tuple(numbers[:4] or [0, 0, 0])

    def _ensure_plugin_server_config(self, name, server_index=None):
        config_path = self._plugin_server_config_path(name, server_index)
        root_config_path = self._plugin_root_config_path(name)
        config = self._default_plugin_config(name)
        if root_config_path.exists():
            config = self._deep_merge(config, self._read_json_dict(root_config_path))
        if config_path.exists():
            config = self._deep_merge(config, self._read_json_dict(config_path))
        config["enabled"] = True
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        return config_path

    def _set_plugin_enabled_for_selected_server(self, enabled):
        if not self.selected_plugin:
            self._toast("请先选择插件")
            return None
        if not self._local_plugin_exists(self.selected_plugin):
            self._toast("插件尚未安装，请先安装/更新到当前服务器")
            return None
        config_path = self._plugin_server_config_path(self.selected_plugin)
        root_config_path = self._plugin_root_config_path(self.selected_plugin)
        config = self._default_plugin_config(self.selected_plugin)
        if root_config_path.exists():
            config = self._deep_merge(config, self._read_json_dict(root_config_path))
        if config_path.exists():
            config = self._deep_merge(config, self._read_json_dict(config_path))
        config["enabled"] = bool(enabled)
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        self.plugin_config_data = config
        self._populate_plugins(list(self.remote_plugins.values()))
        self._update_selected_plugin_detail()
        self._load_plugin_config_form(update=False)
        self._safe_page_update()
        return config_path

    def _refresh_plugin_ui_after_install(self, name):
        self.selected_plugin = name
        self._populate_plugins(list(self.remote_plugins.values()))
        if not self._plugin_view_active():
            return
        self._update_selected_plugin_detail()
        self._load_plugin_config_form(update=False)
        self._safe_page_update()

    def _download_plugin_dir(self, api_url, target_dir):
        target_dir = Path(target_dir)
        temp_dir = target_dir.with_name(f".{target_dir.name}.download_tmp")
        backup_dir = target_dir.with_name(f".{target_dir.name}.backup_tmp")
        for stale in (temp_dir, backup_dir):
            if stale.exists():
                self._remove_path(stale)
        self._download_plugin_dir_contents(api_url, temp_dir)
        try:
            if target_dir.exists():
                target_dir.replace(backup_dir)
            temp_dir.replace(target_dir)
            if backup_dir.exists():
                self._remove_path(backup_dir)
        except Exception:
            if target_dir.exists():
                self._remove_path(target_dir)
            if backup_dir.exists():
                backup_dir.replace(target_dir)
            raise

    def _remove_path(self, path):
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()

    def _download_plugin_dir_contents(self, api_url, target_dir):
        target_dir.mkdir(parents=True, exist_ok=True)
        items = self._github_json(api_url, timeout=20)
        for item in items:
            target = target_dir / item["name"]
            if item.get("type") == "dir":
                self._download_plugin_dir_contents(item["url"], target)
            elif item.get("download_url"):
                target.write_bytes(self._github_read_bytes(item["download_url"], timeout=20))

    def _uninstall_plugin_from_selected_server(self):
        if not self.selected_plugin:
            self._toast("请先选择插件")
            return
        if not self._local_plugin_exists(self.selected_plugin):
            self._toast("插件尚未安装，无法从当前服务器卸载")
            return
        try:
            config_path = self._plugin_server_config_path(self.selected_plugin)
            config = self._default_plugin_config(self.selected_plugin)
            root_config_path = self._plugin_root_config_path(self.selected_plugin)
            if root_config_path.exists():
                config = self._deep_merge(config, self._read_json_dict(root_config_path))
            if config_path.exists():
                config = self._deep_merge(config, self._read_json_dict(config_path))
            config["enabled"] = False
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
            self._append_terminal("msmp", f"插件已从当前服务器卸载: {self.selected_plugin}\n")
            self._append_terminal("msmp", f"当前服务器配置: {config_path.resolve()}\n")
            self._populate_plugins(list(self.remote_plugins.values()))
            self._update_selected_plugin_detail()
            self._load_plugin_config_form(update=False)
            self._safe_page_update()
            self._toast("已在当前服务器禁用插件；如 Bot 正在运行，请重载插件或重启 Bot 生效")
        except Exception as exc:
            self._toast(f"卸载当前服务器插件失败: {exc}")

    def _plugin_command(self, command):
        if not self.selected_plugin:
            self._toast("请先选择插件")
            return
        plugin_name = self.selected_plugin[:-3] if self.selected_plugin.endswith(".py") else self.selected_plugin
        self._send_bot_command(
            self._console_command_with_selected_server(
                f"#{command} {plugin_name}",
                "plugin_server_index",
            )
        )

    def _plugin_console_name(self):
        if not self.selected_plugin:
            return ""
        return self.selected_plugin[:-3] if self.selected_plugin.endswith(".py") else self.selected_plugin

    def _plugin_target_server_label(self):
        _server, _index, key = self._selected_server_for("plugin_server_index")
        return key or "default"

    def _send_plugin_bot_command(self, command):
        plugin_name = self._plugin_console_name()
        if not plugin_name:
            return False
        return self._send_bot_command(
            self._console_command_with_selected_server(
                f"#{command} {plugin_name}",
                "plugin_server_index",
            )
        )

    def _enable_plugin_for_selected_server(self):
        try:
            config_path = self._set_plugin_enabled_for_selected_server(True)
            if config_path is None:
                return
            server_label = self._plugin_target_server_label()
            self._append_terminal(
                "msmp",
                f"插件已启用到服务器 {server_label}: {self.selected_plugin}\n当前服务器配置: {config_path.resolve()}\n",
            )
            if self._is_bot_running() and self._send_plugin_bot_command("load_plugin"):
                self._toast(f"已启用 {self.selected_plugin} 到 {server_label}，并通知 Bot 加载")
            else:
                self._toast(f"已启用 {self.selected_plugin} 到 {server_label}；Bot 未运行或未接收命令时需启动/重载 Bot")
        except Exception as exc:
            self._toast(f"启用插件失败: {exc}")

    def _disable_plugin_for_selected_server(self):
        try:
            config_path = self._set_plugin_enabled_for_selected_server(False)
            if config_path is None:
                return
            server_label = self._plugin_target_server_label()
            self._append_terminal(
                "msmp",
                f"插件已在服务器 {server_label} 禁用: {self.selected_plugin}\n当前服务器配置: {config_path.resolve()}\n",
            )
            if self._is_bot_running() and self._send_plugin_bot_command("unload_plugin"):
                self._toast(f"已在 {server_label} 禁用 {self.selected_plugin}，并通知 Bot 卸载")
            else:
                self._toast(f"已在 {server_label} 禁用 {self.selected_plugin}；Bot 未运行或未接收命令时需启动/重载 Bot")
        except Exception as exc:
            self._toast(f"禁用插件失败: {exc}")

    def _reload_selected_plugin(self):
        if not self.selected_plugin:
            self._toast("请先选择插件")
            return
        if not self._local_plugin_exists(self.selected_plugin):
            self._toast("插件尚未安装，请先安装/更新到当前服务器")
            return
        server_label = self._plugin_target_server_label()
        if self._is_bot_running() and self._send_plugin_bot_command("reload_plugin"):
            self._toast(f"已向 Bot 发送重载插件命令: {self.selected_plugin} ({server_label})")
        else:
            self._toast("Bot 未运行，无法重载插件；请先启动 Bot")

    def _show_plugin_location(self):
        if not self.selected_plugin:
            self._toast("请先选择插件")
            return
        name = self.selected_plugin
        path = (PLUGIN_DIR / name).resolve()
        root_config = self._plugin_root_config_path(name)
        server_config = self._plugin_server_config_path(name)
        self._append_terminal("msmp", f"插件路径: {path}\n")
        self._append_terminal("msmp", f"根配置: {root_config.resolve()}\n")
        self._append_terminal("msmp", f"当前服务器配置: {server_config.resolve()}\n")
        self._open_path(server_config.parent if server_config.parent.exists() else (path.parent if path.is_file() else path))

    def _open_plugin_dir(self):
        PLUGIN_DIR.mkdir(exist_ok=True)
        self._open_path(PLUGIN_DIR.resolve())

    def _open_path(self, path):
        target = Path(path)
        if os.name == "nt" and target.exists():
            try:
                os.startfile(str(target))
            except Exception as exc:
                self._toast(f"打开路径失败: {exc}")
        else:
            self._append_terminal("msmp", f"路径: {path}\n")

    def _start_bot_process(self):
        if self.bot_process and self.bot_process.poll() is None:
            self._append_terminal("msmp", "Bot 已在运行\n")
            self._toast("Bot 已在运行，无需重复启动")
            self._refresh_terminal_action_buttons()
            return
        if self.bot_launch_pending:
            self._append_terminal("msmp", "Bot 正在启动中，请等待当前启动完成\n")
            self._toast("Bot 正在启动中，请稍等")
            return
        valid, port, message = self._validate_bot_start_config()
        if not valid:
            self._append_terminal("msmp", f"{message}\n")
            self._toast(message)
            self._refresh_terminal_action_buttons()
            return
        if message:
            self._append_terminal("msmp", f"{message}\n")
        if self._tcp_port_open(port):
            message = f"Bot启动失败：WebSocket端口 {port} 已被占用。请先停止旧实例，或修改 Bot配置里的 websocket.port"
            self._append_terminal("msmp", f"{message}\n")
            self._toast(message)
            self._refresh_terminal_action_buttons()
            return
        self.bot_start_sequence += 1
        start_sequence = self.bot_start_sequence
        self.bot_connection_confirmed = False
        self.bot_listener_confirmed = False
        self.bot_launch_pending = True
        self._append_terminal("msmp", f"启动 Bot，OneBot反向WS地址: ws://127.0.0.1:{port}\n")
        self._toast(f"正在启动 Bot：监听 ws://127.0.0.1:{port}")
        self._start_mc_log_tailer()
        self.page.run_thread(self._run_bot_process)
        self.page.run_thread(lambda: self._monitor_bot_startup(start_sequence, port))
        self._refresh_terminal_action_buttons()

    def _start_mc_log_tailer(self):
        if self.mc_log_tail_thread and self.mc_log_tail_thread.is_alive():
            return
        self.mc_log_tail_thread = threading.Thread(target=self._tail_mc_log_file, name="mc-log-tail", daemon=True)
        self.mc_log_tail_thread.start()

    def _tail_mc_log_file(self):
        log_dir = APP_DIR / "logs"
        tails = {}
        existing_at_start = set(log_dir.glob("mc_server*.log")) if log_dir.exists() else set()

        def close_tail(path):
            state = tails.pop(path, None)
            handle = state.get("file") if state else None
            if handle:
                try:
                    handle.close()
                except Exception:
                    pass

        def server_key_for_log(path):
            stem = path.stem
            if stem == "mc_server":
                return self.bot_managed_server_key or "unknown"
            prefix = "mc_server_"
            if stem.startswith(prefix):
                return stem[len(prefix):] or "unknown"
            return "unknown"

        while not self._closing:
            try:
                paths = sorted(log_dir.glob("mc_server*.log")) if log_dir.exists() else []
                active_paths = set(paths)
                for stale_path in list(tails):
                    if stale_path not in active_paths:
                        close_tail(stale_path)

                if not paths:
                    time.sleep(0.1)
                    continue

                read_any = False
                for log_path in paths:
                    state = tails.setdefault(log_path, {
                        "file": None,
                        "signature": None,
                        "opened_once": False,
                        "existed_at_start": log_path in existing_at_start,
                    })
                    stat = log_path.stat()
                    signature = (stat.st_ino, stat.st_ctime_ns, stat.st_size)
                    handle = state.get("file")
                    old_signature = state.get("signature")
                    needs_open = handle is None or not old_signature or old_signature[:2] != signature[:2]
                    if needs_open:
                        if handle:
                            handle.close()
                        handle = log_path.open("r", encoding="utf-8", errors="replace")
                        state["file"] = handle
                        state["signature"] = signature
                        if state["existed_at_start"] and not state["opened_once"]:
                            handle.seek(0, os.SEEK_END)
                        else:
                            handle.seek(0)
                        state["opened_once"] = True

                    lines_read = 0
                    while lines_read < 100:
                        line = handle.readline()
                        if not line:
                            break
                        server_key = server_key_for_log(log_path)
                        line = f"[{server_key}] {line}"
                        self.terminal_queue.put(("mc", line))
                        lines_read += 1
                        read_any = True
                    if lines_read:
                        state["signature"] = (signature[0], signature[1], log_path.stat().st_size)
                        continue

                    if handle.tell() > log_path.stat().st_size:
                        close_tail(log_path)
                        tails[log_path] = {
                            "file": None,
                            "signature": None,
                            "opened_once": False,
                            "existed_at_start": False,
                        }
                time.sleep(0.01 if read_any else 0.05)
            except Exception as exc:
                for path in list(tails):
                    close_tail(path)
                self.terminal_queue.put(("mc", f"[GUI] 跟随 MC 日志失败，稍后重试: {exc}\n"))
                time.sleep(1)

        for path in list(tails):
            close_tail(path)

    def _monitor_bot_startup(self, start_sequence, port):
        listen_deadline = time.time() + 8
        while not self._closing and self.bot_start_sequence == start_sequence and time.time() < listen_deadline:
            if self.bot_process and self.bot_process.poll() is not None:
                self.bot_launch_pending = False
                message = "Bot启动失败：进程已退出，WebSocket未保持监听，请查看上方错误日志"
                self.terminal_queue.put(("msmp", f"{message}。\n"))
                self.terminal_queue.put(("toast", message))
                self.terminal_queue.put(("servers", ""))
                return
            if self._tcp_port_open(port):
                self.bot_listener_confirmed = True
                self.bot_launch_pending = False
                self.terminal_queue.put(("msmp", f"WebSocket监听已就绪: ws://127.0.0.1:{port}\n"))
                self.terminal_queue.put(("toast", f"Bot WebSocket监听已就绪：ws://127.0.0.1:{port}"))
                self.terminal_queue.put(("servers", ""))
                break
            time.sleep(0.25)

        if self.bot_start_sequence != start_sequence or self._closing:
            return

        if not self.bot_listener_confirmed:
            self.bot_launch_pending = False
            message = f"Bot启动失败：{port} 端口未监听，Bot 未成功启动或已退出"
            self.terminal_queue.put(("msmp", f"{message}。\n"))
            self.terminal_queue.put(("toast", message))
            self.terminal_queue.put(("servers", ""))
            return

        connect_deadline = time.time() + 20
        while not self._closing and self.bot_start_sequence == start_sequence and time.time() < connect_deadline:
            if self.bot_process and self.bot_process.poll() is not None:
                self.bot_launch_pending = False
                message = "Bot启动失败：进程已退出，OneBot尚未连接"
                self.terminal_queue.put(("msmp", f"{message}。\n"))
                self.terminal_queue.put(("toast", message))
                self.terminal_queue.put(("servers", ""))
                return
            if self.bot_connection_confirmed:
                self.terminal_queue.put(("toast", "Bot 已启动并收到 OneBot 连接"))
                return
            time.sleep(0.5)

        if self.bot_start_sequence == start_sequence and not self.bot_connection_confirmed:
            message = (
                "Bot 已监听但尚未收到 OneBot 连接：请检查 NapCat/OneBot 反向WS地址 "
                f"ws://127.0.0.1:{port}，以及 token 是否一致"
            )
            self.terminal_queue.put(("msmp", f"{message}。\n"))
            self.terminal_queue.put(("toast", message))

    def _run_bot_process(self):
        try:
            command = [sys.executable, "--bot"] if getattr(sys, "frozen", False) else [sys.executable, "-u", str(APP_DIR / "main.py")]
            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUTF8"] = "1"
            self.bot_process = subprocess.Popen(
                command,
                cwd=str(APP_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.PIPE,
                text=True,
                bufsize=1,
                encoding="utf-8",
                errors="replace",
                env=env,
            )
            for line in self.bot_process.stdout:
                if "WebSocket服务器启动成功" in line or "server listening on" in line:
                    self.bot_listener_confirmed = True
                if "QQ机器人已连接" in line or "connection open" in line:
                    self.bot_connection_confirmed = True
                mc_line = self._mc_line_from_bot_stdout(line)
                if mc_line:
                    self.terminal_queue.put(("mc", mc_line))
                    continue
                self.terminal_queue.put(("msmp", line))
            exit_code = self.bot_process.wait()
            self.bot_connection_confirmed = False
            self.bot_listener_confirmed = False
            self.bot_launch_pending = False
            self.bot_managed_server_key = ""
            self.bot_managed_server_keys.clear()
            self.terminal_queue.put(("msmp", f"Bot进程已退出，退出码: {exit_code}\n"))
            self.terminal_queue.put(("toast", f"Bot 已退出，退出码: {exit_code}"))
            self.terminal_queue.put(("servers", ""))
        except Exception as exc:
            self.bot_connection_confirmed = False
            self.bot_listener_confirmed = False
            self.bot_launch_pending = False
            self.bot_managed_server_key = ""
            self.bot_managed_server_keys.clear()
            message = f"Bot启动失败：{exc}"
            self.terminal_queue.put(("msmp", f"{message}\n"))
            self.terminal_queue.put(("toast", message))
            self.terminal_queue.put(("servers", ""))

    def _stop_bot_process(self):
        if not self.bot_process or self.bot_process.poll() is not None:
            self.bot_launch_pending = False
            self._toast("Bot 未运行，无需停止")
            self._refresh_terminal_action_buttons()
            return
        self.bot_connection_confirmed = False
        self.bot_listener_confirmed = False
        self.bot_launch_pending = False
        self._send_bot_command("#exit")
        self._append_terminal("msmp", "已请求 Bot 退出\n")
        self._toast("已请求停止 Bot")
        self._refresh_terminal_action_buttons()

    def _send_bot_command(self, command, echo=True):
        if not self.bot_process or self.bot_process.poll() is not None or not self.bot_process.stdin:
            self._append_terminal("msmp", f"Bot未运行，无法发送: {command}\n")
            self._toast(f"Bot未运行，无法发送命令: {command}")
            return False
        try:
            self.bot_process.stdin.write(command.rstrip() + "\n")
            self.bot_process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            self._append_terminal("msmp", f"Bot命令发送失败，进程可能已退出: {exc}\n")
            self._toast(f"Bot命令发送失败: {exc}")
            return False
        if echo:
            self._append_terminal("msmp", f"> {command}\n")
        return True

    def _start_selected_server(self, state_attr="terminal_server_index"):
        servers = self._servers()
        if not servers:
            self._toast("启动服务器失败：没有服务器配置，请先在服务器页面新增配置")
            return
        server, index, key = self._selected_server_for(state_attr)
        if not server:
            self._toast("启动服务器失败：当前没有选中的服务器配置")
            return
        if state_attr == "selected_server_index":
            self.terminal_server_index = index
        valid, script_path, workdir, message = self._validate_server_start_config(server, index, key)
        if not valid:
            self._append_terminal("mc", f"[{key}] {message}\n")
            self._toast(message)
            return
        process = self.mc_processes.get(key)
        if process and process.poll() is None:
            self._append_terminal("mc", f"[{key}] 已在运行\n")
            self._toast(f"{key} 已在运行")
            self._set_server_action_pending(key, "")
            return
        self._append_terminal("mc", f"[{key}] 本地启动: {script_path}，工作目录: {workdir}\n")
        self._toast(f"正在启动服务器 {key}：{script_path.name}")
        self._set_server_action_pending(key, "start")
        self.page.run_thread(lambda: self._run_mc_process(key, str(script_path), str(workdir)))

    def _resolve_app_path(self, value):
        path = Path(str(value or "").strip().replace("\\", os.sep).replace("/", os.sep)).expanduser()
        if path.is_absolute():
            return path
        return APP_DIR / path

    def _run_mc_process(self, key, script, workdir):
        try:
            creationflags, startupinfo = self._hidden_process_options()
            process = subprocess.Popen(
                self._script_command(script),
                cwd=workdir,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.PIPE,
                bufsize=0,
                creationflags=creationflags,
                startupinfo=startupinfo,
            )
            self.mc_processes[key] = process
            self.server_action_pending.pop(key, None)
            self.terminal_queue.put(("toast", f"{key} 已启动，进程 PID: {process.pid}"))
            self.terminal_queue.put(("servers", ""))
            for raw_line in process.stdout:
                line = self._decode_process_output(raw_line)
                self.terminal_queue.put(("mc", f"[{key}] {line}"))
            exit_code = process.wait()
            if self.mc_processes.get(key) is process:
                self.mc_processes.pop(key, None)
            self.server_action_pending.pop(key, None)
            self.terminal_queue.put(("mc", f"[{key}] 进程已退出，返回码: {exit_code}\n"))
            self.terminal_queue.put(("toast", f"{key} 已退出，返回码: {exit_code}"))
        except Exception as exc:
            process = locals().get("process")
            if process is not None and self.mc_processes.get(key) is process:
                self.mc_processes.pop(key, None)
            self.server_action_pending.pop(key, None)
            message = f"{key} 启动失败：{exc}"
            self.terminal_queue.put(("mc", f"[{key}] {message}\n"))
            self.terminal_queue.put(("toast", message))
        finally:
            if key in self.server_action_pending and not self._is_server_running(key):
                self.server_action_pending.pop(key, None)
            self.terminal_queue.put(("servers", ""))

    def _decode_process_output(self, raw_line):
        if isinstance(raw_line, str):
            return raw_line
        data = bytes(raw_line or b"")
        encodings = ["utf-8", locale.getpreferredencoding(False), "oem", "gbk", "cp936"]
        for encoding in dict.fromkeys(item for item in encodings if item):
            try:
                return data.decode(encoding)
            except (LookupError, UnicodeDecodeError):
                continue
        return data.decode("utf-8", errors="replace")

    def _write_process_stdin(self, process, command):
        if not process or not process.stdin:
            return
        payload = (str(command).rstrip() + "\n").encode("utf-8")
        process.stdin.write(payload)
        process.stdin.flush()

    def _script_command(self, script):
        suffix = Path(script).suffix.lower()
        if os.name == "nt" and suffix in {".bat", ".cmd"}:
            return ["cmd.exe", "/c", script]
        if suffix == ".sh":
            return ["sh", script]
        return [script]

    def _hidden_process_options(self):
        if os.name != "nt":
            return 0, None
        creationflags = 0
        if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
            creationflags |= subprocess.CREATE_NEW_PROCESS_GROUP
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            creationflags |= subprocess.CREATE_NO_WINDOW
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        return creationflags, startupinfo

    def _wait_process_exit(self, process, timeout):
        if not process:
            return None
        try:
            return process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None
        except Exception:
            return process.poll()

    def _process_tree_snapshot(self, process):
        if psutil is None or not process:
            return []
        try:
            parent = psutil.Process(process.pid)
            return [parent, *parent.children(recursive=True)]
        except psutil.Error:
            return []

    def _terminate_psutil_processes(self, processes, timeout=3):
        alive = []
        for item in reversed(list(processes or [])):
            try:
                if item.is_running() and item.status() != psutil.STATUS_ZOMBIE:
                    item.terminate()
                    alive.append(item)
            except psutil.Error:
                pass
        if not alive:
            return
        _gone, still_alive = psutil.wait_procs(alive, timeout=timeout)
        for item in still_alive:
            try:
                item.kill()
            except psutil.Error:
                pass
        if still_alive:
            psutil.wait_procs(still_alive, timeout=timeout)

    def _terminate_process_tree(self, process, graceful_command=None, graceful_timeout=6, kill_timeout=3):
        if not process:
            return None
        tracked_processes = self._process_tree_snapshot(process)
        if process.poll() is not None:
            if tracked_processes:
                self._terminate_psutil_processes(tracked_processes[1:], timeout=kill_timeout)
            return process.returncode

        if graceful_command:
            try:
                self._write_process_stdin(process, graceful_command)
            except Exception:
                pass
            exit_code = self._wait_process_exit(process, graceful_timeout)
            if exit_code is not None:
                if tracked_processes:
                    self._terminate_psutil_processes(tracked_processes[1:], timeout=kill_timeout)
                return exit_code

        if psutil is not None:
            try:
                processes = tracked_processes or self._process_tree_snapshot(process)
                self._terminate_psutil_processes(processes, timeout=kill_timeout)
            except psutil.NoSuchProcess:
                pass
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass
        elif os.name == "nt":
            try:
                creationflags, startupinfo = self._hidden_process_options()
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=creationflags,
                    startupinfo=startupinfo,
                    check=False,
                )
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass
        else:
            try:
                process.kill()
            except Exception:
                pass

        return self._wait_process_exit(process, 1)

    def _stop_local_process(self, state_attr="terminal_server_index"):
        servers = self._servers()
        if not servers:
            self._toast("没有服务器配置")
            return
        server, index, key = self._selected_server_for(state_attr)
        if not server:
            self._toast("没有服务器配置")
            return
        if state_attr == "selected_server_index":
            self.terminal_server_index = index
        process = self.mc_processes.get(key)
        if process and process.poll() is None and process.stdin:
            try:
                self._write_process_stdin(process, "stop")
                self._set_server_action_pending(key, "stop", timeout=45)
                self._append_terminal("mc", f"[{key}] 已发送 stop，等待服务端关闭\n")
                self._toast(f"已向 {key} 发送 stop")
                return
            except Exception as exc:
                self._append_terminal("mc", f"[{key}] stop 发送失败，改为终止进程: {exc}\n")
                self._terminate_process_tree(process, graceful_command=None, graceful_timeout=0)
                self.mc_processes.pop(key, None)
                self._set_server_action_pending(key, "")
                self._toast(f"已终止 {key} 本地进程")
                return

        if process and process.poll() is None:
            self._terminate_process_tree(process, graceful_command=None, graceful_timeout=0)
            self.mc_processes.pop(key, None)
            self._set_server_action_pending(key, "")
            self._append_terminal("mc", f"[{key}] stdin不可用，已终止本地进程\n")
            self._toast(f"已终止 {key} 本地进程")
            return

        if self._is_bot_running():
            if self._send_bot_command(f"#stop {index + 1}"):
                self._set_server_action_pending(key, "stop", timeout=45)
                self._append_terminal("mc", f"[{key}] 已发送 Bot 托管停止命令\n")
                self._toast(f"已请求停止 {key}")
            return

        self._append_terminal("mc", f"[{key}] 未运行\n")
        self._set_server_action_pending(key, "")
        self._toast(f"{key} 未运行")

    def _force_stop_local_process(self, state_attr="terminal_server_index"):
        servers = self._servers()
        if not servers:
            self._toast("强制关闭失败：没有服务器配置")
            return
        server, index, key = self._selected_server_for(state_attr)
        if not server:
            self._toast("强制关闭失败：当前没有选中的服务器配置")
            return
        if state_attr == "selected_server_index":
            self.terminal_server_index = index
        process = self.mc_processes.get(key)
        if process and process.poll() is None:
            try:
                self._terminate_process_tree(process, graceful_command=None, graceful_timeout=0)
                self._append_terminal("mc", f"[{key}] 已强制关闭本地进程\n")
                self._toast(f"已强制关闭 {key} 本地进程")
            except Exception as exc:
                self._append_terminal("mc", f"[{key}] 强制关闭失败: {exc}\n")
                self._toast(f"{key} 强制关闭失败: {exc}")
            finally:
                self._set_server_action_pending(key, "")
                self.mc_processes.pop(key, None)
                self._refresh_terminal_action_buttons()
                self._safe_page_update()
            return

        if self._is_bot_running() and key in self.bot_managed_server_keys:
            selector = self._selected_server_command_selector(state_attr)
            if self._send_bot_command(f"#kill {selector}"):
                self._set_server_action_pending(key, "kill", timeout=20)
                self._append_terminal("mc", f"[{key}] 已发送 Bot 托管强制关闭命令\n")
                self._toast(f"已请求强制关闭 {key}")
            return

        self._append_terminal("mc", f"[{key}] 未运行，无法强制关闭\n")
        self._set_server_action_pending(key, "")
        self._toast(f"{key} 未运行，无法强制关闭")

    def _submit_msmp(self, event):
        command = event.control.value.strip()
        event.control.value = ""
        self._safe_page_update()
        self._execute_msmp_command(command)

    def _submit_mc(self, event):
        command = event.control.value.strip()
        event.control.value = ""
        self._safe_page_update()
        self._execute_mc_command(command)

    def _execute_msmp_command(self, command):
        command = str(command or "").strip()
        if command:
            self._remember_terminal_command("msmp", command)
            self._send_bot_command(self._console_command_with_selected_server(command, "terminal_server_index"))

    def _execute_mc_command(self, command):
        command = str(command or "").strip()
        if not command:
            return
        self._remember_terminal_command("mc", command)
        servers = self._servers()
        if not servers:
            message = f"没有服务器配置，无法发送MC命令: {command}"
            self._append_terminal("mc", f"{message}\n")
            self._toast(message)
            return
        server, index, key = self._selected_server_for("terminal_server_index")
        if not server:
            message = f"当前没有选中的服务器配置，无法发送MC命令: {command}"
            self._append_terminal("mc", f"{message}\n")
            self._toast(message)
            return
        process = self.mc_processes.get(key)
        if not process or process.poll() is not None or not process.stdin:
            if self._is_bot_running():
                selector = self._selected_server_command_selector("terminal_server_index")
                if self._send_bot_command(f"#mc {selector} {command}", echo=False):
                    self._append_terminal("mc", f"[{key}] > {command}\n")
                return
            message = f"{key} MC进程未运行且Bot未启动，无法发送: {command}"
            self._append_terminal("mc", f"[{key}] {message}\n")
            self._toast(message)
            return
        self._write_process_stdin(process, command)
        self._append_terminal("mc", f"[{key}] > {command}\n")

    def _is_bot_running(self):
        return bool(self.bot_process and self.bot_process.poll() is None)

    def _update_bot_managed_server_state(self, line):
        line_text = str(line or "")
        server_keys = [self._server_key(index + 1, server) for index, server in enumerate(self._servers())]

        def resolve_key(raw):
            candidate = str(raw or "").strip().strip("[]")
            if not candidate:
                return ""
            candidate_lower = candidate.lower()
            for key in server_keys:
                if key.lower() == candidate_lower:
                    return key
            for key in server_keys:
                if key and key in line_text:
                    return key
            return candidate

        start_patterns = [
            r"目标服务器:\s*(.+?)(?:\s|$)",
            r"(.+?)\s+服务器进程已创建",
            r"开始采集\s+(.+?)\s+服务器输出",
            r"Minecraft服务器日志\s+\[([^\]]+)\]",
        ]
        for pattern in start_patterns:
            match = re.search(pattern, line_text)
            if match:
                managed_key = resolve_key(match.group(1))
                if not managed_key:
                    return
                self.bot_managed_server_key = managed_key
                self.bot_managed_server_keys.add(managed_key)
                self.server_action_pending.pop(managed_key, None)
                self.terminal_queue.put(("servers", ""))
                return
        exit_patterns = [
            r"(.+?)\s+服务器进程退出",
            r"(.+?)\s+服务器输出采集结束",
            r"\[([^\]]+)\]\s+进程已退出",
        ]
        for pattern in exit_patterns:
            exit_match = re.search(pattern, line_text)
            if exit_match:
                stopped_key = resolve_key(exit_match.group(1))
                self.bot_managed_server_keys.discard(stopped_key)
                self.server_action_pending.pop(stopped_key, None)
                if self.bot_managed_server_key == stopped_key:
                    self.bot_managed_server_key = next(iter(self.bot_managed_server_keys), "")
                self.terminal_queue.put(("servers", ""))
                return

    def _mc_line_from_bot_stdout(self, line):
        tagged_match = re.search(r"\[MC Server\]\[([^\]]+)\]\s*(.*)", str(line or ""))
        if tagged_match:
            return f"[{tagged_match.group(1).strip()}] {tagged_match.group(2).rstrip()}\n"
        match = re.search(r"\[MC Server\]\s*(.*)", str(line or ""))
        if not match:
            return ""
        server_key = self.bot_managed_server_key or "unknown"
        return f"[{server_key}] {match.group(1).rstrip()}\n"

    def _server_key(self, index, server):
        name = str(server.get("name") or f"server{index}").strip()
        return name or f"server{index}"

    def _selected_server_key(self):
        servers = self._servers()
        if not servers:
            return "default"
        index = self._bounded_server_index("terminal_server_index")
        return self._server_key(index + 1, servers[index])

    def _mc_terminal_key_for_line(self, text):
        match = re.match(r"\[([^\]]+)\]", str(text or ""))
        if match:
            return match.group(1).strip() or "unknown"
        return "unknown"

    def _mc_terminal_buffer(self, key=None):
        buffers = self.terminal_buffers.setdefault("mc", {})
        if not isinstance(buffers, dict):
            buffers = {"default": list(buffers)}
            self.terminal_buffers["mc"] = buffers
        return buffers.setdefault(key or self._selected_server_key(), [])

    def _append_terminal(self, target, text, update=True):
        output = getattr(self, f"{target}_terminal", None)
        key = self._mc_terminal_key_for_line(text) if target == "mc" else None
        removed_count = self._append_terminal_buffer(target, text, key=key)
        if not output:
            return
        if target == "mc" and key != self._selected_server_key():
            return
        if removed_count:
            del output.controls[:removed_count]
        output.controls.append(self._terminal_line_control(target, text.rstrip("\n"), self._terminal_line_color(target, text)))
        if update:
            self._safe_page_update()

    def _append_terminal_buffer(self, target, text, key=None):
        color = self._terminal_line_color(target, text)
        if target == "mc":
            buffer = self._mc_terminal_buffer(key)
        else:
            buffer = self.terminal_buffers.setdefault(target, [])
        buffer.append((text.rstrip("\n"), color))
        removed_count = 0
        if len(buffer) > 800:
            removed_count = min(100, len(buffer))
            del buffer[:100]
        return removed_count

    def _terminal_line_color(self, target, text):
        return "#f7c56b" if "失败" in text or "错误" in text else "#b6f3a4" if target == "mc" else "#9dd6ff"

    def _render_terminal_buffer(self, target):
        output = getattr(self, f"{target}_terminal", None)
        if not output:
            return
        output.controls.clear()
        if target == "mc":
            buffer = self._mc_terminal_buffer()
        else:
            buffer = self.terminal_buffers.get(target, [])
        for text, color in buffer:
            output.controls.append(self._terminal_line_control(target, text, color))

    def _terminal_line_control(self, target, text, default_color):
        text = self._clean_terminal_text(text)
        if target != "mc":
            return ft.Text(text, color=default_color, font_family="Consolas")
        spans = self._terminal_color_spans(text, default_color)
        if len(spans) == 1 and spans[0][1] == default_color:
            return ft.Text(spans[0][0], color=default_color, font_family="Consolas")
        return ft.Text(
            "",
            spans=[
                ft.TextSpan(segment, style=ft.TextStyle(color=color))
                for segment, color in spans
                if segment
            ],
            font_family="Consolas",
        )

    def _clean_terminal_text(self, text):
        text = str(text or "").replace("\r", "")
        return ANSI_CONTROL_PATTERN.sub(
            lambda match: match.group(0) if ANSI_COLOR_PATTERN.fullmatch(match.group(0)) else "",
            text,
        )

    def _terminal_color_spans(self, text, default_color):
        spans = []
        current_color = default_color
        buffer = []
        index = 0

        def flush():
            if buffer:
                spans.append(("".join(buffer), current_color))
                buffer.clear()

        while index < len(text):
            char = text[index]
            if char == "§" and index + 1 < len(text):
                code = text[index + 1].lower()
                if code in MC_COLOR_MAP:
                    flush()
                    current_color = MC_COLOR_MAP[code]
                elif code == "r":
                    flush()
                    current_color = default_color
                index += 2
                continue

            ansi_match = ANSI_COLOR_PATTERN.match(text, index)
            if ansi_match:
                flush()
                codes = [code for code in ansi_match.group(1).split(";") if code] or ["0"]
                for code in codes:
                    if code in {"0", "39"}:
                        current_color = default_color
                    elif code in ANSI_COLOR_MAP:
                        current_color = ANSI_COLOR_MAP[code]
                index = ansi_match.end()
                continue

            buffer.append(char)
            index += 1

        flush()
        return spans or [("", default_color)]

    def _drain_terminal_queue(self):
        while not self._closing:
            changed = False
            servers_dirty = False
            processed = 0
            max_batch = 200
            try:
                first_item = self.terminal_queue.get(timeout=0.01)
            except queue.Empty:
                continue
            pending_items = [first_item]
            while processed < max_batch:
                if pending_items:
                    target, line = pending_items.pop(0)
                else:
                    try:
                        target, line = self.terminal_queue.get_nowait()
                    except queue.Empty:
                        break
                processed += 1
                if target == "servers":
                    servers_dirty = True
                    changed = True
                    continue
                if target == "toast":
                    self._toast(line, update=False)
                    changed = True
                    continue
                if target == "msmp":
                    self._update_bot_managed_server_state(line)
                self._append_terminal(target, line, update=False)
                changed = True
            if servers_dirty and self.active_view_index == 3 and hasattr(self, "server_list"):
                self._populate_servers(load_form=False)
            if servers_dirty and self.active_view_index == 1 and hasattr(self, "terminal_server_toggle"):
                self._refresh_terminal_action_buttons()
            if changed:
                if not self._safe_page_update():
                    return

    def _toast(self, message, update=True):
        if self._closing:
            return
        try:
            self.page.snack_bar = ft.SnackBar(ft.Text(message))
            self.page.snack_bar.open = True
            if update:
                self._safe_page_update()
        except Exception as exc:
            text = str(exc).lower()
            if "destroyed session" in text or "session" in text and "destroy" in text:
                self._closing = True
                return
            raise

    async def _on_window_event(self, event):
        event_type = getattr(event, "type", "")
        event_value = getattr(event_type, "value", str(event_type)).lower()
        if event_value != "close" and not event_value.endswith(".close"):
            return
        if self._close_started:
            return
        self._close_started = True
        try:
            self._toast("正在关闭：先停止本地服务器和 Bot，请稍等...", update=True)
        except Exception:
            pass
        await asyncio.to_thread(self._shutdown_for_close)
        try:
            self.page.window.prevent_close = False
            result = self.page.window.destroy()
            if asyncio.iscoroutine(result):
                await result
        finally:
            os._exit(0)

    def _shutdown_for_close(self):
        if self._shutdown_done:
            return
        self._shutdown_done = True
        self._closing = True
        for key, process in list(self.mc_processes.items()):
            if process and process.poll() is None:
                self._terminate_process_tree(process, graceful_command="stop", graceful_timeout=6)
            self.mc_processes.pop(key, None)
        self.server_action_pending.clear()
        if self.bot_process and self.bot_process.poll() is None:
            self._terminate_process_tree(self.bot_process, graceful_command="#exit", graceful_timeout=4)

    def _on_close(self, _event=None):
        self._shutdown_for_close()


def main(page: ft.Page):
    FletGuiApp(page)


def run_gui():
    _ensure_runtime_resources()
    os.chdir(APP_DIR)
    ft.run(main)


if __name__ == "__main__":
    if "--bot" in sys.argv:
        _ensure_runtime_resources()
        os.chdir(APP_DIR)
        from main import main as bot_main
        bot_main()
    else:
        run_gui()
