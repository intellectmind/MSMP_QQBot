import socket
import struct
import logging
import re
from typing import Optional, List
from dataclasses import dataclass

@dataclass
class PlayerListInfo:
    """玩家列表信息"""
    current_players: int = 0
    max_players: int = 20
    player_names: List[str] = None
    
    def __init__(self):
        self.player_names = []
    
    def __str__(self):
        return f"PlayerListInfo{{current={self.current_players}, max={self.max_players}, players={', '.join(self.player_names)}}}"


class RCONClient:
    """Minecraft RCON客户端"""
    
    # RCON数据包类型
    SERVERDATA_AUTH = 3
    SERVERDATA_AUTH_RESPONSE = 2
    SERVERDATA_EXECCOMMAND = 2
    SERVERDATA_RESPONSE_VALUE = 0
    
    def __init__(self, host: str, port: int, password: str, logger: logging.Logger, timeout: int = 10):
        self.host = host
        self.port = port
        self.password = password
        self.logger = logger
        self.timeout = timeout
        self.socket = None
        self.authenticated = False
        self.request_id = 0
    
    def connect(self) -> bool:
        """连接到RCON服务器"""
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.settimeout(self.timeout)
            self.socket.connect((self.host, self.port))
            
            # 进行认证
            if self._authenticate():
                self.authenticated = True
                self.logger.info(f"已连接到RCON服务器 {self.host}:{self.port}")
                return True
            else:
                self.logger.error("RCON认证失败")
                self.close()
                return False
                
        except socket.timeout:
            self.logger.error(f"连接RCON服务器超时: {self.host}:{self.port}")
            return False
        except ConnectionRefusedError:
            self.logger.error(f"RCON服务器拒绝连接: {self.host}:{self.port}")
            return False
        except Exception as e:
            self.logger.error(f"连接RCON服务器失败: {e}")
            return False
    
    def _authenticate(self) -> bool:
        """执行RCON认证"""
        try:
            # 发送认证包
            self._send_packet(self.SERVERDATA_AUTH, self.password)
            
            # 接收认证响应
            response_id, response_type, _ = self._receive_packet()
            
            # 认证成功时request_id会匹配，失败时返回-1
            return response_id != -1
            
        except Exception as e:
            self.logger.error(f"RCON认证异常: {e}")
            return False
    
    def _send_packet(self, packet_type: int, payload: str) -> int:
        """发送RCON数据包"""
        if not self.socket:
            raise Exception("RCON未连接")
        
        self.request_id += 1
        request_id = self.request_id
        
        # 编码payload
        payload_bytes = payload.encode('utf-8')
        
        # 构建数据包: ID(4) + Type(4) + Payload + \x00\x00
        packet = struct.pack('<ii', request_id, packet_type) + payload_bytes + b'\x00\x00'
        
        # 添加长度前缀
        packet_length = len(packet)
        full_packet = struct.pack('<i', packet_length) + packet
        
        self.socket.sendall(full_packet)
        return request_id
    
    def _receive_packet(self) -> tuple:
        """接收RCON数据包"""
        if not self.socket:
            raise Exception("RCON未连接")
        
        # 读取长度
        length_data = self._recv_exact(4)
        packet_length = struct.unpack('<i', length_data)[0]
        
        # 读取完整数据包
        packet_data = self._recv_exact(packet_length)
        
        # 解析数据包
        request_id, response_type = struct.unpack('<ii', packet_data[:8])
        payload = packet_data[8:-2].decode('utf-8', errors='ignore')
        
        return request_id, response_type, payload
    
    def _recv_exact(self, size: int) -> bytes:
        """精确接收指定字节数"""
        data = b''
        while len(data) < size:
            chunk = self.socket.recv(size - len(data))
            if not chunk:
                raise Exception("连接已关闭")
            data += chunk
        return data
    
    def execute_command(self, command: str) -> Optional[str]:
        """执行RCON命令"""
        if not self.authenticated:
            raise Exception("RCON未认证")
        
        # 检查socket连接状态
        if not self.socket:
            raise Exception("RCON连接已关闭")
        
        try:
            # 设置较短的超时时间，避免长时间等待
            original_timeout = self.socket.gettimeout()
            self.socket.settimeout(5.0)  # 5秒超时
            
            # 发送命令
            self._send_packet(self.SERVERDATA_EXECCOMMAND, command)
            
            # 接收响应
            _, _, response = self._receive_packet()
            
            # 恢复原始超时设置
            self.socket.settimeout(original_timeout)
            
            return response
            
        except socket.timeout:
            self.logger.warning(f"执行RCON命令超时: {command}")
            # 超时时不关闭连接，让调用方决定
            return None
        except Exception as e:
            self.logger.warning(f"执行RCON命令失败: {e}")
            # 发生异常时关闭连接
            self.close()
            return None
    
    def get_player_list(self) -> PlayerListInfo:
        """获取玩家列表"""
        info = PlayerListInfo()
        
        try:
            # 执行list命令
            response = self.execute_command("list")
            
            if not response:
                return info
            
            self.logger.debug(f"RCON list响应: {response}")
            
            # 改进的解析逻辑，支持多种语言格式
            info = self._parse_list_response(response)
            
            return info
            
        except Exception as e:
            self.logger.error(f"获取RCON玩家列表失败: {e}", exc_info=True)
            return info
    
    def _parse_list_response(self, response: str) -> PlayerListInfo:
        """解析list命令响应，支持多种格式"""
        info = PlayerListInfo()
        
        # 移除颜色代码和多余空格
        cleaned_response = re.sub(r'[Â§&][0-9a-fk-orA-FK-OR]', '', response).strip()
        
        self.logger.debug(f"清理后的响应: {cleaned_response}")
        
        # ============ 第一步：解析在线人数和最大人数 ============
        
        # 模式1: 英文标准格式 "There are X of a max of Y players online"
        match = re.search(r'There are (\d+) of a max of (\d+) players online', cleaned_response, re.IGNORECASE)
        if match:
            info.current_players = int(match.group(1))
            info.max_players = int(match.group(2))
            self.logger.debug(f"匹配模式1: {info.current_players}/{info.max_players}")
        
        # 模式2: 英文变体格式 "There are X/Y players online"
        if not match:
            match = re.search(r'There are (\d+)/(\d+) players online', cleaned_response, re.IGNORECASE)
            if match:
                info.current_players = int(match.group(1))
                info.max_players = int(match.group(2))
                self.logger.debug(f"匹配模式2: {info.current_players}/{info.max_players}")
        
        # 模式3: 中文格式 "当前有 X 个玩家在线，最多 Y 人"
        if not match:
            match = re.search(r'当前有\s*(\d+)\s*个玩家在线.*?最多\s*(\d+)\s*人', cleaned_response)
            if match:
                info.current_players = int(match.group(1))
                info.max_players = int(match.group(2))
                self.logger.debug(f"匹配模式3: {info.current_players}/{info.max_players}")
        
        # 模式4: 中文简写格式 "玩家在线 X/Y"
        if not match:
            match = re.search(r'玩家在线\s*(\d+)/(\d+)', cleaned_response)
            if match:
                info.current_players = int(match.group(1))
                info.max_players = int(match.group(2))
                self.logger.debug(f"匹配模式4: {info.current_players}/{info.max_players}")
        
        # 模式5: 通用格式 "X of Y" 或 "X/Y"
        if not match:
            match = re.search(r'(\d+)\s*(?:of|/)\s*(\d+)', cleaned_response)
            if match:
                info.current_players = int(match.group(1))
                info.max_players = int(match.group(2))
                self.logger.debug(f"匹配模式5: {info.current_players}/{info.max_players}")
        
        # ============ 第二步：解析玩家名称列表 ============
        
        player_names = []
        
        # 特殊处理：某些服务器会输出 "服主在线: xxx" 和 "default: yyy" 这样的分类格式
        # 我们需要合并所有玩家名称
        all_names_parts = []
        
        # 模式：匹配 "服主在线: xxx" 或 "default: yyy" 等格式
        category_matches = re.findall(r'(?:服主在线|default|在线玩家|players)[:：]\s*([^\n:：]+)', cleaned_response)
        if category_matches:
            self.logger.debug(f"检测到分类格式，找到 {len(category_matches)} 个分类")
            all_names_parts.extend(category_matches)
        
        # 如果没有找到分类格式，尝试多个分隔符来定位玩家列表的起始位置
        if not all_names_parts:
            separators = [':', '：', 'online:', 'online：', '在线:']
            player_part = ""
            
            for sep in separators:
                if sep in cleaned_response:
                    # 找到分隔符后面的内容
                    parts = cleaned_response.split(sep, 1)
                    if len(parts) > 1:
                        player_part = parts[1].strip()
                        if player_part:
                            self.logger.debug(f"使用分隔符 '{sep}' 提取玩家列表: {player_part[:100]}")
                            all_names_parts.append(player_part)
                            break
            
            # 如果还没找到，尝试从最后一个数字后面提取
            if not all_names_parts:
                # 移除前面的数字和统计信息，保留玩家名称部分
                match = re.search(r'[\d/]+\s+(?:players?|玩家|人).*?:\s*(.+)', cleaned_response, re.IGNORECASE)
                if match:
                    player_part = match.group(1).strip()
                    self.logger.debug(f"从统计信息后提取玩家列表: {player_part[:100]}")
                    all_names_parts.append(player_part)
        
        # 处理玩家名称部分
        for player_part in all_names_parts:
            if not player_part or player_part in [" ", ".", "无", "none", "None"]:
                continue
            
            # 多个分隔符来分割玩家名称：逗号、空格、换行等
            # 首先尝试用逗号分割
            if ',' in player_part:
                raw_names = [name.strip() for name in player_part.split(',') if name.strip()]
                self.logger.debug(f"使用逗号分割，得到 {len(raw_names)} 个玩家")
            else:
                # 如果没有逗号，尝试用多个空格或特殊字符分割
                # 匹配连续的空格或其他分隔符
                raw_names = re.split(r'\s{2,}|,|;|，|；|\n', player_part)
                raw_names = [name.strip() for name in raw_names if name.strip()]
                self.logger.debug(f"使用正则分割，得到 {len(raw_names)} 个玩家")
            
            # 清理每个玩家名称
            for name in raw_names:
                # 移除特殊字符，保留字母、数字、下划线、中文、连字符
                # 允许更多字符以支持各种命名规范
                clean_name = re.sub(r'[\s\[\]\(\)\{\}<>\"\'`~!@#$%^&*|\\/?]+', '', name)
                
                # 过滤掉只包含特殊字符的项，以及已经添加过的重复项
                if clean_name and clean_name not in [':', '：', 'online', '在线'] and clean_name not in player_names:
                    player_names.append(clean_name)
                    self.logger.debug(f"清理玩家名称: '{name}' -> '{clean_name}'")
        
        # 检查是否没有解析到玩家名称但有在线人数
        if not player_names and info.current_players > 0:
            self.logger.warning(f"检测到有 {info.current_players} 个玩家在线，但无法解析玩家名称")
            self.logger.debug(f"原始响应内容: {cleaned_response}")
        
        info.player_names = player_names
        
        # ============ 第三步：数据一致性检查 ============
        
        # 如果从玩家名称列表解析出的数量不同，优先使用实际玩家名称数量
        if player_names:
            if len(player_names) != info.current_players:
                self.logger.debug(
                    f"玩家数量不一致: 数字解析={info.current_players}, "
                    f"名称解析={len(player_names)}, 使用实际名称数量"
                )
                info.current_players = len(player_names)
        elif info.current_players == 0:
            # 如果没有玩家名称且在线人数为0，这是正常的
            pass
        
        self.logger.debug(
            f"最终解析结果: 当前玩家={info.current_players}, "
            f"最大玩家={info.max_players}, 玩家列表={info.player_names}"
        )
        
        return info
    
    def stop_server(self) -> bool:
        """停止服务器"""
        try:
            response = self.execute_command("stop")
            return response is not None
        except Exception as e:
            self.logger.error(f"RCON停止服务器失败: {e}")
            return False
    
    def close(self):
        """关闭连接"""
        self.authenticated = False
        if self.socket:
            try:
                self.socket.close()
            except:
                pass
            self.socket = None
    
    def is_connected(self) -> bool:
        """检查是否已连接"""
        if not self.socket or not self.authenticated:
            return False
        
        try:
            # 尝试发送一个简单的命令来测试连接
            self.socket.settimeout(1)
            response = self.execute_command("list")
            self.socket.settimeout(self.timeout)
            return response is not None
        except:
            return False
    
    def __enter__(self):
        """上下文管理器入口"""
        self.connect()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """上下文管理器退出"""
        self.close()