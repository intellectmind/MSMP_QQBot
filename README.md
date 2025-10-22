# MSMP_QQBot

支持我的世界1.21.9+新加入的服务端管理协议MSMP和RCON的QQ机器人（1.21.9以下版本可单独使用RCON）。支持QQ启动/停止服务器，查询在线人数及玩家ID、服务器状态、执行命令等功能

配置文件可自动热重载（有修改保存就自动重载）

拥有强大的自定义服务端日志监听器，详见[wiki](https://github.com/intellectmind/MSMP_QQBot/wiki/custom_listeners)

----------------------------------------------------------------------------------------------------------

## 使用说明
#### 启动方式1：独立启动我的世界服务端后启动MSMP_QQBot（即外部接入）
#### 启动方式2（推荐）：使用`#start`命令启动或者使用QQ机器人（start命令）启动服务端，此时MSMP_QQBot控制台会捕获服务端控制台输出，并且仍然支持向服务端输入命令  
      
#### window用户：直接下载`releases`中的`MSMP_QQBot.exe`和`config.yml`，修改配置后，双击运行即可  
#### Linux等其它用户可下载源代码运行  

> 注意：`start_script`和`working_directory`的路径不能使用`\`，而是用`/`，参考："G:/1.21.9/start.bat"  
> 启动命令`start.bat`参考`"G:\jdk-21.0.5\bin\java.exe" -Xmx8G -jar paper-1.21.10-69.jar nogui`，可以不用加UTF-8编码这些  

----------------------------------------------------------------------------------------------------------

## 命令说明

### QQ命令

注：管理员支持私聊使用所有命令，无管理员权限则只支持群内  

```
基础命令:
• list / 在线列表 / 玩家列表
  查看在线玩家列表
• tps / /tps / 服务器tps
  查看服务器TPS(每秒刻数)性能
• rules / 规则 / /rules
  查看服务器游戏规则和设置
• status / 状态 / /status
  查看服务器状态
• help / 帮助 / /help
  显示帮助信息

管理员命令:
• stop / 停止 / 关闭
  停止Minecraft服务器
• start / 启动 / 开启
  启动Minecraft服务器
• reload / 重载 / /reload
  重新加载配置文件
• log / 日志 / /log
  查看最近20条的服务器日志
• reconnect / 重连 / /reconnect
  重新连接所有服务(MSMP和RCON)
• reconnect_msmp / 重连msmp / /reconnect_msmp
  重新连接MSMP服务
• reconnect_rcon / 重连rcon / /reconnect_rcon
  重新连接RCON服务
• kill / force-stop / 强制停止
  强制中止Minecraft服务器进程(不保存数据)
• crash / crash-report / 崩溃报告
  获取最新的服务器崩溃报告
• sysinfo / 系统信息 / /sysinfo
  查看服务器系统信息(CPU、内存、硬盘、网络)
• disk / 硬盘 / /disk
  查看服务器硬盘详细使用情况
• process / 进程 / /process
  查看Java进程运行信息
• network / 网络 / /network
  查看网络信息和实时带宽速度
• listeners / 监听规则 / /listeners
  查看所有自定义消息监听规则

直接命令执行:
• !<命令>
  管理员可使用 ! 前缀直接执行服务器命令,需启用RCON
  示例: !say Hello 或 !give @a diamond
```

### MSMP_QQBot控制台命令

```
系统命令 (使用 # 前缀):
  #status          - 查看系统连接状态  
  #reload          - 重新加载配置文件  
  #logs            - 显示日志文件信息  
  #help            - 显示此帮助信息  
  #exit            - 退出程序  
  #logstats        - 查看日志系统统计信息  

日志开关命令 (使用 # 前缀):  
  #toggle_mc_log   - 开启/禁用 MC服务端日志输出  
  #toggle_bot_log  - 开启/禁用 MSMP_QQBot日志输出  
  #log_status      - 显示日志开关状态
  #mute_log <关键词>   - 禁用包含指定关键词的日志
  #unmute_log <关键词> - 启用包含指定关键词的日志

服务器管理命令 (使用 # 前缀):
  #start           - 启动Minecraft服务器
  #stop            - 停止Minecraft服务器
  #kill            - 强制杀死服务器进程(不保存数据,紧急用)
  #server_status   - 查看服务器进程状态

服务器查询命令 (使用 # 前缀):
  #list            - 查看在线玩家列表
  #tps             - 查看服务器TPS性能
  #rules           - 查看服务器游戏规则

系统监控命令 (使用 # 前缀):
  #sysinfo         - 查看系统信息 (CPU、内存、硬盘、网络)
  #disk            - 查看硬盘使用情况
  #process         - 查看Java进程信息
  #network         - 查看网络信息和实时带宽

连接管理命令 (使用 # 前缀):
  #reconnect       - 重新连接所有服务 (MSMP和RCON)
  #reconnect_msmp  - 重新连接MSMP
  #reconnect_rcon  - 重新连接RCON

其他命令 (使用 # 前缀):
  #listeners       - 查看自定义消息监听规则

Minecraft命令 (无 # 前缀):
  直接输入任意Minecraft命令将转发到服务器
  示例: list
        say Hello everyone!
        give @a diamond

日志开关示例:
  #toggle_mc_log   - 禁用MC服务端日志
  #toggle_bot_log  - 禁用Bot日志
  #log_status      - 查看所有日志状态
  #mute_log ERROR  - 禁用包含 ERROR 的日志
  #unmute_log ERROR - 启用包含 ERROR 的日志
```

----------------------------------------------------------------------------------------------------------

WebSocket反向连接示例  

<img width="653" height="728" alt="image" src="https://github.com/user-attachments/assets/5d3627b1-d886-45a6-8450-1bad5a7c5b17" />

----------------------------------------------------------------------------------------------------------

#### 带注释完整config.yml

```
# Minecraft Server Management Protocol (MSMP) 配置
msmp:
  # 是否启用MSMP（推荐：功能最完整，需版本1.21.9+，需关闭management-server-tls）
  enabled: false
  # MSMP服务器地址
  host: localhost
  # MSMP端口 (需要在服务端配置文件中设置 management-server-port)
  port: 21111
  # MSMP认证令牌 (需要在服务端配置文件中设置 management-server-secret)
  password: your_msmp_password_here

# RCON连接配置
rcon:
  # 是否启用RCON（与MSMP同时启用时优先走MSMP通道，版本1.21.9以下可单独使用这个）
  enabled: false
  # RCON服务器地址
  host: localhost
  # RCON端口
  port: 25575
  # RCON密码
  password: your_rcon_password_here

# WebSocket反向连接配置
websocket:
  # WebSocket监听端口 (反向WS连接到此端口)
  port: 8080
  # 鉴权令牌 (可选)
  token: ""
  # 是否启用鉴权
  auth_enabled: false

# QQ机器人配置
qq:
  # 允许使用机器人的QQ群号列表
  groups:
    - 123456789  # 群号
    - 234567891   # 可以添加/删除更多群号

  # QQ管理员列表 (可以使用start/stop命令的用户)
  admins:
    - 123456789  # 管理员QQ
    - 987654321  # 可以添加/删除更多管理员QQ

  # 欢迎新成员消息
  welcome_new_members: false
  welcome_message: "欢迎新成员加入！输入 help 查看可用命令"

# 服务器启动配置
server:
  # 服务器启动脚本路径 (支持.bat/.sh文件)，路径不能使用\，而是用/，不用加UTF-8编码这些
  start_script: "G:/1.21.9/start.bat"
  # 工作目录 (可选，不填则使用脚本所在目录)，路径不能使用\，而是用/
  working_directory: ""
  # 服务器启动超时时间（秒）
  startup_timeout: 300

# 命令配置
commands:
  # TPS命令配置 - 群内使用tps命令时执行的指令，可根据服务器类型自定义
  tps_command: tps

  # 基础命令开关配置，管理员不受此限制,始终可以使用所有命令
  enabled_commands:
    list: true    # 玩家列表命令
    tps: true     # TPS查询命令
    rules: true   # 规则查询命令
    status: true  # 状态查询命令
    help: true    # 帮助命令

  # 是否开放管理员命令，开放后非管理员也可以使用管理员命令，管理员不受此影响
  enabled_admin_commands:
    start: false          # 启动服务器命令
    stop: false           # 停止服务器命令
    kill: false           # 强制停止服务器命令
    reload: false         # 重载配置命令
    log: false            # 查看服务器日志命令
    reconnect: false      # 重连所有服务命令
    reconnect_msmp: false # 重连MSMP命令
    reconnect_rcon: false # 重连RCON命令
    crash: false          # 崩溃报告命令
    sysinfo: false        # 系统信息命令
    disk: false           # 磁盘信息命令
    process: false        # 进程信息命令
    network: false        # 网络信息命令
    listeners: false      # 监听规则命令

# 通知配置
notifications:
  # 是否发送服务器事件通知（启动/关闭）
  server_events: true
  # 是否发送玩家事件通知（加入/离开）
  player_events: true
  # 是否在控制台显示详细消息日志
  log_messages: false
  # 需搭配chunkmonitor插件使用，并启用控制台（https://github.com/intellectmind/ChunkMonitor）
  chunk_monitor:
    # 是否启用区块监控通知
    enabled: true
    # 是否向管理员发送私聊通知
    notify_admins: true
    # 是否向QQ群发送通知
    notify_groups: true

# 高级配置
advanced:
  # MSMP重连间隔（秒）
  reconnect_interval: 300
  # 心跳间隔（秒）
  heartbeat_interval: 30
  # 命令冷却时间（秒）
  command_cooldown: 3
  # 最大消息长度
  max_message_length: 2500
  # 玩家列表缓存时间（秒）
  player_list_cache_ttl: 5
  # 最大服务器日志行数
  max_server_logs: 100

# 定时任务配置
scheduled_tasks:
  # 是否启用定时任务
  enabled: false
  
  # 定时启动服务器
  auto_start:
    # 是否启用定时启动
    enabled: false
    # 启动时间列表 (24小时制 HH:MM)
    times:
      - "08:00"    # 早上8点启动
      - "18:00"    # 下午6点启动，可添加更多/删除
    # 启动前通知 (秒数，0=不通知)
    pre_notify_seconds: 300  # 提前5分钟通知
    # 通知消息
    notify_message: "服务器将在 {countdown} 秒后启动，请做好准备"
  
  # 定时关闭服务器
  auto_stop:
    # 是否启用定时关闭
    enabled: false
    # 关闭时间列表 (24小时制 HH:MM)
    times:
      - "12:00"    # 中午12点关闭
      - "23:59"    # 晚上11:59关闭，可添加更多/删除
    # 关闭前警告 (秒数，0=不通知)
    warning_before_seconds: 600  # 提前10分钟警告
    # 第一次警告消息
    first_warning: "服务器将在 {countdown} 秒后关闭，请保存游戏"
    # 第二次警告消息 (关闭前1分钟)
    second_warning: "服务器即将在 1 分钟后关闭"
    # 立即关闭消息
    immediate_message: "服务器正在关闭"

  # 定时重启服务器
  auto_restart:
    # 是否启用定时重启
    enabled: false
    # 重启时间列表 (24小时制 HH:MM)
    times:
      - "04:00"    # 凌晨4点重启
      - "16:00"    # 下午4点重启，可添加更多/删除
    # 重启前警告 (秒数，0=不通知)
    warning_before_seconds: 600  # 提前10分钟警告
    # 第一次警告消息
    first_warning: "服务器将在 {countdown} 秒后重启，请保存游戏"
    # 第二次警告消息 (重启前1分钟)
    second_warning: "服务器即将在 1 分钟后重启"
    # 立即重启消息
    immediate_message: "服务器正在重启"
    # 关闭后重启前等待时间 (秒数)
    wait_before_startup: 10
    # 重启成功消息
    restart_success_message: "服务器已重启，欢迎回来！"

# 调试模式
debug: false

# ============================================================
# 自定义服务端消息监听规则配置
# ============================================================
# 这个功能允许你通过正则表达式监听服务器日志
# 匹配成功时可以向QQ群发送消息或向服务端执行指令
custom_listeners:
  # 是否启用自定义监听功能
  enabled: false
  
  # 监听规则列表 - 可以添加无限个规则 
  rules:
    # 示例1: 玩家加入游戏的高级通知
    - name: "player_join_advanced"
      description: "玩家加入游戏时发送智能通知"
      enabled: true
      pattern: "(\\w+) joined the game"
      case_sensitive: false
      trigger_limit: 0        # 0表示无限制
      trigger_cooldown: 0     # 冷却时间（秒），0表示无冷却
      daily_limit: 0          # 每日限制，0表示无限制
      conditions:
        - type: "time_range"
          params:
            start: "08:00"
            end: "23:00"
        - type: "player_online"
          params:
            require: true
      qq_message: |
        玩家 {upper(group1)} 加入了游戏！
        服务器状态: TPS {server_tps} | 在线: {player_count}人
        时间: {time} | 规则: {rule_name}
        今日第 {trigger_today} 次玩家加入
      server_command: "say 欢迎 {upper(group1)} 加入游戏！当前在线: {player_count} 人"

    # 示例2: 服务器错误监控
    - name: "error_monitor"
      description: "监控服务器错误并通知管理员"
      enabled: true
      pattern: "\\[ERROR\\].*?(Exception|Error|Crash|Failed)"
      case_sensitive: false
      trigger_limit: 0
      trigger_cooldown: 300   # 5分钟冷却
      daily_limit: 5          # 每天最多5次
      conditions:
        - type: "server_tps"
          params:
            min_tps: 5
            max_tps: 20
      qq_message: |
        服务器错误告警！
        错误内容: {substr(match, 0, 100)}
        发生时间: {timestamp}
        当前TPS: {server_tps}
        在线玩家: {player_count}人
      server_command: "say 检测到服务器错误，请查看控制台日志"

    # 示例3: 玩家聊天关键词监控
    - name: "chat_keyword_alert"
      description: "监控玩家聊天中的关键词"
      enabled: true
      pattern: "<(\\w+)>.*?(作弊|外挂|bug|漏洞|hack|cheat)"
      case_sensitive: false
      trigger_limit: 0
      trigger_cooldown: 30    # 30秒冷却
      daily_limit: 0
      qq_message: |
        聊天关键词告警
        玩家: {group1}
        内容: {substr(match, 0, 50)}
        时间: {time}
      server_command: ""

    # 示例4: 服务器性能告警
    - name: "performance_alert"
      description: "服务器性能下降告警"
      enabled: true
      pattern: "Can't keep up!.*"
      case_sensitive: false
      trigger_limit: 3
      trigger_cooldown: 600   # 10分钟冷却
      daily_limit: 0
      conditions:
        - type: "memory_usage"
          params:
            max_usage: 90
      qq_message: |
        服务器性能告警！
        问题: {match}
        时间: {timestamp}
        当前TPS: {server_tps}
        内存: {memory_usage}%
        建议: {if(memory_usage > 80, '考虑重启释放内存', '检查插件性能')}
      server_command: "save-all"

    # 示例5: 玩家死亡通知
    - name: "player_death_smart"
      description: "玩家死亡时发送智能通知"
      enabled: true
      pattern: "(\\w+) (was slain by|was shot by|fell|drowned|burned|blown up|died)"
      case_sensitive: false
      trigger_limit: 0
      trigger_cooldown: 10
      daily_limit: 0
      qq_message: |
        玩家死亡事件
        玩家: {group1}
        原因: {replace(match, '^\\w+ ', '')}
        时间: {time}
        统计: 今日第 {trigger_today} 次死亡
      server_command: ""
```

----------------------------------------------------------------------------------------------------------
