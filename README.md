# MSMP_QQBot

----------------------------------------------------------------------------------------------------------

## 使用说明
#### 支持独立启动我的世界服务端（可外部接入），或者使用QQ机器人启动服务端（此时MSMP_QQBot会捕获服务端控制台输出）  
#### window用户：直接下载`releases`中的exe文件，双击运行即可  
#### Linux等其它用户可下载源代码运行  
<img width="653" height="728" alt="image" src="https://github.com/user-attachments/assets/5d3627b1-d886-45a6-8450-1bad5a7c5b17" />

----------------------------------------------------------------------------------------------------------

#### 带注释完整config.yml

```
# Minecraft Server Management Protocol (MSMP) 配置
msmp:
  # 是否启用MSMP（推荐：功能最完整，需版本1.21.9+）
  enabled: true
  # MSMP服务器地址
  host: localhost
  # MSMP端口 (需要在服务端配置文件中设置 management-server-port)
  port: 21111
  # MSMP认证令牌 (需要在服务端配置文件中设置 management-server-secret)
  password: your_msmp_password_here

# RCON连接配置
rcon:
  # 是否启用RCON（备用方案，与MSMP同时启用时优先走MSMP通道，版本1.21.9以下可单独使用这个）
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
    - 234567891   # 可以添加更多群号

  # QQ管理员列表 (可以使用start/stop命令的用户)
  admins:
    - 123456789  # 管理员QQ
    - 987654321  # 可以添加更多管理员QQ

  # 欢迎新成员消息
  welcome_new_members: false
  welcome_message: "欢迎新成员加入！输入 help 查看可用命令"

# 服务器启动配置
server:
  # 服务器启动脚本路径 (支持.bat/.sh文件)
  start_script: "G:/1.21.9/start.bat"
  # 工作目录 (可选，不填则使用脚本所在目录)
  working_directory: ""
  # 服务器启动超时时间（秒）
  startup_timeout: 300

# 通知配置
notifications:
  # 是否发送服务器事件通知（启动/关闭）
  server_events: true
  # 是否发送玩家事件通知（加入/离开）
  player_events: true
  # 是否在控制台显示详细消息日志
  log_messages: false

# 高级配置
advanced:
  # MSMP重连间隔（秒）
  reconnect_interval: 300
  # 心跳间隔（秒）
  heartbeat_interval: 30
  # 命令冷却时间（秒）- 防止命令刷屏
  command_cooldown: 3
  # 最大消息长度
  max_message_length: 500
  # 玩家列表缓存时间（秒）
  player_list_cache_ttl: 5

# 调试模式
debug: false
```

----------------------------------------------------------------------------------------------------------
