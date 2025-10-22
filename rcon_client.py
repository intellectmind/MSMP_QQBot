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
        """解析list命令响应"""
        info = PlayerListInfo()
        
        # 移除颜色代码和多余空格
        cleaned_response = re.sub(r'§[0-9a-fk-or]', '', response).strip()
        
        self.logger.debug(f"清理后的响应: {cleaned_response}")
        
        # 尝试多种解析模式
        
        # 模式1: 英文标准格式 "There are X of a max of Y players online: player1, player2"
        match = re.search(r'There are (\d+) of a max of (\d+) players online', cleaned_response, re.IGNORECASE)
        if match:
            info.current_players = int(match.group(1))
            info.max_players = int(match.group(2))
        
        # 模式2: 英文变体格式 "There are X/Y players online: player1, player2"
        if not match:
            match = re.search(r'There are (\d+)/(\d+) players online', cleaned_response, re.IGNORECASE)
            if match:
                info.current_players = int(match.group(1))
                info.max_players = int(match.group(2))
        
        # 模式3: 中文格式 "当前有 X 个玩家在线，最多 Y 人: player1, player2"
        if not match:
            match = re.search(r'当前有\s*(\d+)\s*个玩家在线.*?最多\s*(\d+)\s*人', cleaned_response)
            if match:
                info.current_players = int(match.group(1))
                info.max_players = int(match.group(2))
        
        # 模式4: 中文简写格式 "X/Y players online: player1, player2"
        if not match:
            match = re.search(r'(\d+)/(\d+)\s*玩家在线', cleaned_response)
            if match:
                info.current_players = int(match.group(1))
                info.max_players = int(match.group(2))
        
        # 模式5: 通用格式，查找 "X of Y" 或 "X/Y" 模式
        if not match:
            match = re.search(r'(\d+)\s*(?:of|/)\s*(\d+)', cleaned_response)
            if match:
                info.current_players = int(match.group(1))
                info.max_players = int(match.group(2))
        
        # 解析玩家名称列表
        player_names = []
        if ":" in cleaned_response:
            # 提取冒号后的内容
            player_part = cleaned_response.split(":", 1)[1].strip()
            
            if player_part and player_part not in [" ", "."]:  # 避免空列表的情况
                # 按逗号分割玩家名，并清理每个名称
                raw_names = [name.strip() for name in player_part.split(",") if name.strip()]
                
                # 进一步清理每个玩家名（移除可能的多余字符）
                for name in raw_names:
                    clean_name = re.sub(r'[^a-zA-Z0-9_-]', '', name)
                    if clean_name:
                        player_names.append(clean_name)
        
        info.player_names = player_names
        
        # 如果从玩家名称列表解析出的数量与数字解析的不一致，以实际名称为准
        if player_names and len(player_names) != info.current_players:
            self.logger.debug(f"玩家数量不一致: 数字解析={info.current_players}, 名称解析={len(player_names)}")
            info.current_players = len(player_names)
        
        self.logger.debug(f"解析结果: 当前玩家={info.current_players}, 最大玩家={info.max_players}, 玩家列表={info.player_names}")
        
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