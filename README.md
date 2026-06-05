# MSMP_QQBot

[夸克下载链接](https://pan.quark.cn/s/ae82cd4320dc?pwd=CyXJ)

<img width="2560" height="1528" alt="gui" src="https://github.com/user-attachments/assets/82610b08-8850-476a-94db-d3a63948ce64" />

支持我的世界1.21.9+新加入的服务端管理协议MSMP和RCON的QQ机器人（1.21.9以下版本可单独使用RCON）。支持QQ启动/停止服务器，查询在线人数及玩家ID、服务器状态、执行命令等功能

支持多服务器管理，GUI界面，配置文件可自动热重载（有修改保存就自动重载）

拥有强大的自定义服务端监听器和自定义指令监听器，详见[wiki](https://github.com/intellectmind/MSMP_QQBot/wiki)

----------------------------------------------------------------------------------------------------------

## 使用说明
#### 启动方式1：独立启动我的世界服务端后启动MSMP_QQBot（即外部接入），此方式部分功能将不可用
#### 启动方式2（推荐）：使用`#start`命令启动或者使用QQ机器人（start命令）启动服务端，此时MSMP_QQBot控制台会捕获服务端控制台输出，并且仍然支持向服务端输入命令  
      
#### window用户：直接下载 [夸克下载链接](https://pan.quark.cn/s/ae82cd4320dc?pwd=CyXJ) ，双击运行即可  
#### Linux等其它用户可下载源代码运行  

> 启动命令`start.bat`参考`"G:\jdk-21.0.5\bin\java.exe" -Xmx8G -jar paper-1.21.10-69.jar nogui`，可以不用加UTF-8编码这些  

----------------------------------------------------------------------------------------------------------

## 外置插件

下载插件后直接放入plugins文件夹即可

[可在此下载插件](https://github.com/intellectmind/MSMP_QQBot-Plugins)

[MSMP_QQBot-插件开发者文档](https://github.com/intellectmind/MSMP_QQBot/wiki/MSMP_QQBot-插件开发者文档)

----------------------------------------------------------------------------------------------------------

## 命令说明

### QQ命令

注：管理员支持私聊使用所有命令，无管理员权限则只支持群内  

```
【基础命令】
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
• plugins / 插件 / /plugins
  查看已加载的插件及其命令

【管理员专属命令】
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
• reload_plugin / 重载插件 / /reload_plugin
  重新加载指定插件
• unload_plugin / 卸载插件 / /unload_plugin
  卸载指定插件
• load_plugin / 加载插件 / /load_plugin
  加载指定插件

【直接命令执行】
• !<命令>
  使用 ! 前缀直接执行服务器命令
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

日志管理命令 (使用 # 前缀):
  #log_status      - 显示日志开关状态
  #toggle_mc_log   - 开启/禁用 MC服务端日志输出
  #toggle_bot_log  - 开启/禁用 MSMP_QQBot日志输出
  #mute_log <关键词>   - 禁用包含指定关键词的日志
  #unmute_log <关键词> - 启用包含指定关键词的日志

日志归档命令 (使用 # 前缀):
  #archive_logs    - 执行日志归档操作
  #archive_stats   - 查看日志归档统计信息

连接管理命令 (使用 # 前缀):
  #connection status - 查看连接管理器状态
  #reconnect       - 重新连接所有服务 (MSMP和RCON)
  #reconnect_msmp  - 重新连接MSMP
  #reconnect_rcon  - 重新连接RCON

服务器管理命令 (使用 # 前缀):
  #start           - 启动Minecraft服务器
  #stop            - 停止Minecraft服务器
  #kill            - 强制杀死服务器进程(不保存数据,紧急用)
  #server_status   - 查看服务器进程状态

插件管理命令 (使用 # 前缀):
  #plugins         - 查看已加载的插件及其命令
  #load_plugin <插件名>  - 加载指定插件
  #unload_plugin <插件名> - 卸载指定插件
  #reload_plugin <插件名> - 重新加载指定插件(热重载)

服务器查询命令 (使用 # 前缀):
  #list            - 查看在线玩家列表
  #tps             - 查看服务器TPS(每秒刻数)性能
  #rules           - 查看服务器游戏规则和设置
  #sysinfo         - 查看系统信息 (CPU、内存、硬盘、网络)
  #disk            - 查看硬盘使用情况
  #process         - 查看Java进程信息
  #network         - 查看网络信息和实时带宽
  #listeners       - 查看所有自定义消息监听规则

Minecraft命令 (无 # 前缀):
  直接输入任意Minecraft命令将转发到服务器
  示例: list
        say Hello everyone!
        give @a diamond
```

----------------------------------------------------------------------------------------------------------

WebSocket反向连接示例  

<img width="653" height="728" alt="image" src="https://github.com/user-attachments/assets/5d3627b1-d886-45a6-8450-1bad5a7c5b17" />
