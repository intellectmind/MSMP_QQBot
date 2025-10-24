import time
import asyncio
import logging
from typing import Tuple, Optional, Dict, Any
from enum import Enum


class ConnectionStatus(Enum):
    """连接状态枚举"""
    UNKNOWN = "未知"
    CONNECTED = "已连接"
    DISCONNECTED = "已断开"
    CONNECTING = "连接中"
    ERROR = "错误"


class ConnectionCache:
    """连接状态缓存"""
    
    def __init__(self, ttl: int = 5):
        """
        Args:
            ttl: 缓存过期时间（秒）
        """
        self.ttl = ttl
        self.cache = {}  # {key: (status, timestamp)}
        self.lock = asyncio.Lock()
    
    async def get(self, key: str) -> Optional[ConnectionStatus]:
        """获取缓存的连接状态"""
        async with self.lock:
            if key in self.cache:
                status, timestamp = self.cache[key]
                # 检查缓存是否过期
                if time.time() - timestamp < self.ttl:
                    return status
                else:
                    del self.cache[key]
        return None
    
    async def set(self, key: str, status: ConnectionStatus):
        """设置连接状态缓存"""
        async with self.lock:
            self.cache[key] = (status, time.time())
    
    async def invalidate(self, key: str):
        """立即失效缓存"""
        async with self.lock:
            if key in self.cache:
                del self.cache[key]
    
    async def clear(self):
        """清空所有缓存"""
        async with self.lock:
            self.cache.clear()


class ConnectionManager:
    """统一的连接管理器 - 集中管理所有连接状态和操作"""
    
    def __init__(self, logger: logging.Logger, cache_ttl: int = 5):
        self.logger = logger
        self.cache = ConnectionCache(ttl=cache_ttl)
        self._shutdown_mode = False
        self._shutdown_lock = asyncio.Lock()
        
        # 连接相关
        self.msmp_client = None
        self.rcon_client = None
        self.config_manager = None
        
        # 统一的状态管理
        self._msmp_status = {
            'enabled': False,
            'connected': False,
            'authenticated': False,
            'shutdown_mode': False
        }
        
        self._rcon_status = {
            'enabled': False,
            'connected': False,
            'authenticated': False,
            'shutdown_mode': False
        }
        
        # 重连配置
        self.max_reconnect_attempts = 3
        self.reconnect_delay_base = 2
        self.max_reconnect_delay = 300
    
    async def set_clients(self, msmp_client, rcon_client, config_manager):
        """设置客户端实例并初始化状态"""
        self.msmp_client = msmp_client
        self.rcon_client = rcon_client
        self.config_manager = config_manager
        
        # 初始化状态
        self._msmp_status['enabled'] = self.config_manager.is_msmp_enabled() if config_manager else False
        self._rcon_status['enabled'] = self.config_manager.is_rcon_enabled() if config_manager else False
    
    # ============ 统一的状态管理方法 ============
    
    async def update_msmp_status(self):
        """更新MSMP状态"""
        if not self.msmp_client or not self._msmp_status['enabled']:
            self._msmp_status.update({
                'connected': False,
                'authenticated': False
            })
            return
        
        # 检查关闭模式
        if self._shutdown_mode or self._msmp_status['shutdown_mode']:
            self._msmp_status.update({
                'connected': False,
                'authenticated': False
            })
            return
        
        try:
            # 实际检查连接状态
            is_connected = (hasattr(self.msmp_client, 'is_connected') and 
                           self.msmp_client.is_connected())
            is_authenticated = (hasattr(self.msmp_client, 'is_authenticated') and 
                               self.msmp_client.is_authenticated())
            
            self._msmp_status.update({
                'connected': is_connected,
                'authenticated': is_authenticated
            })
        except Exception as e:
            self.logger.debug(f"更新MSMP状态失败: {e}")
            self._msmp_status.update({
                'connected': False,
                'authenticated': False
            })
    
    async def update_rcon_status(self):
        """更新RCON状态"""
        if not self.rcon_client or not self._rcon_status['enabled']:
            self._rcon_status.update({
                'connected': False,
                'authenticated': False
            })
            return
        
        # 检查关闭模式
        if self._shutdown_mode or self._rcon_status['shutdown_mode']:
            self._rcon_status.update({
                'connected': False,
                'authenticated': False
            })
            return
        
        try:
            # 实际检查连接状态
            is_connected = (hasattr(self.rcon_client, 'is_connected') and 
                           self.rcon_client.is_connected())
            
            self._rcon_status.update({
                'connected': is_connected,
                'authenticated': is_connected  # RCON连接即认证
            })
        except Exception as e:
            self.logger.debug(f"更新RCON状态失败: {e}")
            self._rcon_status.update({
                'connected': False,
                'authenticated': False
            })
    
    async def update_all_status(self):
        """更新所有连接状态"""
        await self.update_msmp_status()
        await self.update_rcon_status()
    
    # ============ 状态查询方法 ============
    
    async def is_msmp_connected(self) -> bool:
        """检查MSMP是否连接（带缓存）"""
        cached = await self.cache.get("msmp_connected")
        if cached is not None:
            return cached == ConnectionStatus.CONNECTED
        
        await self.update_msmp_status()
        is_connected = self._msmp_status['connected']
        
        status = ConnectionStatus.CONNECTED if is_connected else ConnectionStatus.DISCONNECTED
        await self.cache.set("msmp_connected", status)
        
        return is_connected
    
    async def is_rcon_connected(self) -> bool:
        """检查RCON是否连接（带缓存）"""
        cached = await self.cache.get("rcon_connected")
        if cached is not None:
            return cached == ConnectionStatus.CONNECTED
        
        await self.update_rcon_status()
        is_connected = self._rcon_status['connected']
        
        status = ConnectionStatus.CONNECTED if is_connected else ConnectionStatus.DISCONNECTED
        await self.cache.set("rcon_connected", status)
        
        return is_connected
    
    async def get_connection_status(self) -> Dict[str, Any]:
        """获取完整的连接状态"""
        await self.update_all_status()
        
        return {
            'msmp_enabled': self._msmp_status['enabled'],
            'msmp_connected': self._msmp_status['connected'],
            'msmp_authenticated': self._msmp_status['authenticated'],
            'rcon_enabled': self._rcon_status['enabled'],
            'rcon_connected': self._rcon_status['connected'],
            'rcon_authenticated': self._rcon_status['authenticated'],
            'shutdown_mode': self._shutdown_mode,
            'cache_ttl': self.cache.ttl,
            'cache_size': len(self.cache.cache)
        }
    
    async def get_detailed_status(self) -> Dict[str, Any]:
        """获取详细状态信息"""
        status = await self.get_connection_status()
        
        # 添加MSMP详细状态
        if self.msmp_client and hasattr(self.msmp_client, 'get_detailed_status'):
            try:
                status['msmp_details'] = self.msmp_client.get_detailed_status()
            except Exception as e:
                status['msmp_details'] = f"获取详细状态失败: {e}"
        
        return status
    
    # ============ 关闭模式管理 ============
    
    async def set_shutdown_mode(self):
        """设置关闭模式"""
        if self._shutdown_mode:
            return
        
        async with self._shutdown_lock:
            self._shutdown_mode = True
            self._msmp_status['shutdown_mode'] = True
            self._rcon_status['shutdown_mode'] = True
            
            self.logger.debug("连接管理器进入关闭模式")
            
            # 立即停止所有活动连接
            await self._stop_all_activities()
            
            # 清空缓存
            await self.cache.clear()
    
    async def reset_shutdown_mode(self):
        """重置关闭模式"""
        if not self._shutdown_mode:
            return
        
        async with self._shutdown_lock:
            self._shutdown_mode = False
            self._msmp_status['shutdown_mode'] = False
            self._rcon_status['shutdown_mode'] = False
            
            self.logger.debug("连接管理器关闭模式已重置")
            
            # 重新初始化状态
            if self.config_manager:
                self._msmp_status['enabled'] = self.config_manager.is_msmp_enabled()
                self._rcon_status['enabled'] = self.config_manager.is_rcon_enabled()
            
            # 清空缓存
            await self.cache.clear()
            
            self.logger.info("连接管理器状态已完全重置")
    
    async def _stop_all_activities(self):
        """停止所有连接活动"""
        # 停止MSMP活动
        if self.msmp_client:
            try:
                # 设置MSMP客户端的关闭模式
                if hasattr(self.msmp_client, 'set_shutdown_mode'):
                    self.msmp_client.set_shutdown_mode()
                
                # 取消重连尝试
                if hasattr(self.msmp_client, 'reconnecting'):
                    self.msmp_client.reconnecting = False
                
                # 关闭连接
                if hasattr(self.msmp_client, 'close'):
                    await asyncio.wait_for(self.msmp_client.close(), timeout=2.0)
            except Exception as e:
                self.logger.debug(f"停止MSMP活动时出错: {e}")
        
        # 停止RCON活动
        if self.rcon_client:
            try:
                # 关闭RCON连接
                if hasattr(self.rcon_client, 'close'):
                    self.rcon_client.close()
            except Exception as e:
                self.logger.debug(f"停止RCON活动时出错: {e}")
    
    # ============ 连接操作 ============
    
    async def connect_all(self) -> Dict[str, bool]:
        """连接所有启用的服务"""
        if self._shutdown_mode:
            self.logger.warning("关闭模式中，跳过连接")
            return {'msmp': False, 'rcon': False}
        
        results = {}
        
        # 连接MSMP
        if self.config_manager and self.config_manager.is_msmp_enabled() and self.msmp_client:
            results['msmp'] = await self._connect_msmp()
        else:
            results['msmp'] = False
        
        # 连接RCON
        if self.config_manager and self.config_manager.is_rcon_enabled() and self.rcon_client:
            results['rcon'] = await self._connect_rcon()
        else:
            results['rcon'] = False
        
        # 清空缓存，强制重新检测
        await self.cache.clear()
        
        return results
    
    async def disconnect_all(self):
        """断开所有连接"""
        await self.set_shutdown_mode()  # 使用统一的关闭模式
    
    async def immediate_shutdown(self):
        """立即关闭所有连接"""
        await self.set_shutdown_mode()
    
    # ============ 重连操作 ============
    
    async def reconnect_all(self) -> Dict[str, bool]:
        """重新连接所有服务"""
        if self._shutdown_mode:
            self.logger.warning("关闭模式中，跳过重连")
            return {'msmp': False, 'rcon': False}
        
        self.logger.info("开始重新连接所有服务...")
        
        # 重置关闭模式
        await self.reset_shutdown_mode()
        
        # 等待一段时间让端口释放
        await asyncio.sleep(3)
        
        # 重新连接
        return await self.connect_all()
    
    async def reconnect_msmp(self) -> bool:
        """重新连接MSMP"""
        if self._shutdown_mode:
            self.logger.warning("关闭模式中，跳过MSMP重连")
            return False
        
        if not self.config_manager or not self.config_manager.is_msmp_enabled() or not self.msmp_client:
            self.logger.warning("MSMP未启用或客户端未初始化")
            return False
        
        self.logger.info("重新连接MSMP...")
        
        # 重置关闭模式
        await self.reset_shutdown_mode()
        
        # 等待
        await asyncio.sleep(2)
        
        # 重新连接
        return await self._connect_msmp()
    
    async def reconnect_rcon(self) -> bool:
        """重新连接RCON"""
        if self._shutdown_mode:
            self.logger.warning("关闭模式中，跳过RCON重连")
            return False
        
        if not self.config_manager or not self.config_manager.is_rcon_enabled() or not self.rcon_client:
            self.logger.warning("RCON未启用或客户端未初始化")
            return False
        
        self.logger.info("重新连接RCON...")
        
        # 重置关闭模式
        await self.reset_shutdown_mode()
        
        # 等待
        await asyncio.sleep(1)
        
        # 重新连接
        return await self._connect_rcon()
    
    # ============ 内部连接方法 ============
    
    async def _connect_msmp(self) -> bool:
        """内部MSMP连接方法"""
        try:
            if not self.msmp_client:
                return False
            
            # 检查是否已经连接
            if await self.is_msmp_connected():
                self.logger.debug("MSMP已连接，无需重复连接")
                return True
            
            self.logger.info("连接MSMP服务器...")
            
            # 使用同步方法连接（因为MSMPClient在后台线程运行）
            if hasattr(self.msmp_client, 'connect_sync'):
                self.msmp_client.connect_sync()
            else:
                # 异步连接
                await self.msmp_client.connect()
            
            # 等待连接建立
            await asyncio.sleep(3)
            
            if await self.is_msmp_connected():
                self.logger.info("MSMP连接成功")
                await self.cache.invalidate("msmp_connected")
                return True
            else:
                self.logger.warning("MSMP连接失败")
                return False
                
        except Exception as e:
            self.logger.error(f"MSMP连接异常: {e}")
            return False
    
    async def _connect_rcon(self) -> bool:
        """内部RCON连接方法"""
        try:
            if not self.rcon_client:
                return False
            
            # 检查是否已经连接
            if await self.is_rcon_connected():
                self.logger.debug("RCON已连接，无需重复连接")
                return True
            
            self.logger.info("连接RCON服务器...")
            
            # RCON是同步连接
            loop = asyncio.get_event_loop()
            success = await loop.run_in_executor(None, self.rcon_client.connect)
            
            if success:
                self.logger.info("RCON连接成功")
                await self.cache.invalidate("rcon_connected")
                return True
            else:
                self.logger.warning("RCON连接失败")
                return False
                
        except Exception as e:
            self.logger.error(f"RCON连接异常: {e}")
            return False
        
    async def is_any_connected(self) -> bool:
        """检查是否有任何连接可用"""
        if self._shutdown_mode:
            return False
        return await self.is_msmp_connected() or await self.is_rcon_connected()
    
    async def get_preferred_client(self) -> Tuple[Optional[str], Optional[object]]:
        """获取优先客户端（MSMP优先于RCON）"""
        if self._shutdown_mode:
            return None, None
        
        # 检查MSMP
        if self.config_manager and self.config_manager.is_msmp_enabled():
            if await self.is_msmp_connected():
                return 'msmp', self.msmp_client
        
        # 检查RCON
        if self.config_manager and self.config_manager.is_rcon_enabled():
            if await self.is_rcon_connected():
                return 'rcon', self.rcon_client
        
        return None, None
    
    async def get_client_for_command(self, command: str) -> Tuple[Optional[str], Optional[object]]:
        """根据命令类型获取合适的客户端"""
        if self._shutdown_mode:
            return None, None
        
        # TPS命令强制使用RCON
        if command.lower() in ['tps', '/tps']:
            if await self.is_rcon_connected():
                return 'rcon', self.rcon_client
            return None, None
        
        # 管理命令优先使用MSMP
        if command.lower().startswith(('allowlist', 'ban', 'op', 'gamerule', 'serversettings')):
            if await self.is_msmp_connected():
                return 'msmp', self.msmp_client
        
        # 默认使用优先客户端
        return await self.get_preferred_client()
    
    async def ensure_connected(self) -> Tuple[Optional[str], Optional[object]]:
        """确保至少有一个连接活跃"""
        # 检查是否在关闭模式
        if self._shutdown_mode:
            self.logger.debug("关闭模式中，跳过连接检查")
            return None, None
        
        client_type, client = await self.get_preferred_client()
        
        if client:
            return client_type, client
        
        # 检查关闭模式
        if self._shutdown_mode:
            return None, None
        
        # 尝试自动重连
        self.logger.warning("检测到连接断开，开始自动重连...")
        await self._auto_reconnect()
        
        # 重连后再次获取
        return await self.get_preferred_client()
    
    async def _auto_reconnect(self):
        """自动重连所有客户端"""
        # 检查关闭模式
        if self._shutdown_mode:
            self.logger.debug("关闭模式中，跳过自动重连")
            return
        
        reconnect_tasks = []
        
        if self.config_manager.is_msmp_enabled() and self.msmp_client:
            reconnect_tasks.append(self._reconnect_msmp())
        
        if self.config_manager.is_rcon_enabled() and self.rcon_client:
            reconnect_tasks.append(self._reconnect_rcon())
        
        if reconnect_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*reconnect_tasks, return_exceptions=True),
                    timeout=20.0
                )
            except asyncio.TimeoutError:
                self.logger.warning("自动重连超时")
    
    async def _reconnect_msmp(self):
        """重连 MSMP 客户端"""
        try:
            if await self.is_msmp_connected():
                return True
            
            for attempt in range(self.max_reconnect_attempts):
                try:
                    delay = min(
                        self.reconnect_delay_base * (2 ** attempt),
                        self.max_reconnect_delay
                    )
                    
                    if attempt > 0:
                        self.logger.info(f"MSMP 重连尝试 {attempt + 1}/{self.max_reconnect_attempts}，等待 {delay} 秒...")
                        await asyncio.sleep(delay)
                    
                    self.logger.debug("正在重连 MSMP...")
                    
                    # 直接调用连接方法
                    if hasattr(self.msmp_client, 'connect_sync'):
                        self.msmp_client.connect_sync()
                    else:
                        await self.msmp_client.connect()
                    
                    await asyncio.sleep(2)
                    
                    if await self.is_msmp_connected():
                        self.logger.info("MSMP 重连成功")
                        await self.cache.invalidate("msmp_connected")
                        return True
                    
                except Exception as e:
                    self.logger.debug(f"MSMP 重连尝试 {attempt + 1} 失败: {e}")
                    continue
            
            self.logger.warning("MSMP 重连失败")
            return False
            
        except Exception as e:
            self.logger.error(f"MSMP 重连异常: {e}")
            return False

    async def _reconnect_rcon(self):
        """重连 RCON 客户端"""
        try:
            if await self.is_rcon_connected():
                return True
            
            for attempt in range(self.max_reconnect_attempts):
                try:
                    delay = min(
                        self.reconnect_delay_base * (2 ** attempt),
                        self.max_reconnect_delay
                    )
                    
                    if attempt > 0:
                        self.logger.info(f"RCON 重连尝试 {attempt + 1}/{self.max_reconnect_attempts}，等待 {delay} 秒...")
                        await asyncio.sleep(delay)
                    
                    self.logger.debug("正在重连 RCON...")
                    
                    # 直接调用连接方法
                    loop = asyncio.get_event_loop()
                    success = await loop.run_in_executor(None, self.rcon_client.connect)
                    
                    if success:
                        self.logger.info("RCON 重连成功")
                        await self.cache.invalidate("rcon_connected")
                        return True
                    
                except Exception as e:
                    self.logger.debug(f"RCON 重连尝试 {attempt + 1} 失败: {e}")
                    continue
            
            self.logger.warning("RCON 重连失败")
            return False
            
        except Exception as e:
            self.logger.error(f"RCON 重连异常: {e}")
            return False
    
    # ============ 服务器启动后的连接 ============
    
    async def connect_after_server_start(self, delay: int = 5) -> Dict[str, bool]:
        """服务器启动后连接所有服务"""
        if self._shutdown_mode:
            self.logger.warning(f"连接管理器处于关闭模式，跳过服务器启动后连接 (shutdown_mode={self._shutdown_mode})")
            return {'msmp': False, 'rcon': False}
        
        self.logger.info(f"等待{delay}秒后连接服务器...")
        await asyncio.sleep(delay)
        
        self.logger.info(f"开始连接服务器 (shutdown_mode={self._shutdown_mode})")
        return await self.connect_all()
    
    async def invalidate_all_caches(self):
        """失效所有缓存"""
        await self.cache.clear()