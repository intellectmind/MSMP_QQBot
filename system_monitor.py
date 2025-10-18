import psutil
import logging
import platform
import time
from typing import Dict, Any, Optional
from dataclasses import dataclass
from datetime import datetime, date

@dataclass
class SystemStats:
    """系统统计数据"""
    cpu_percent: float
    cpu_count: int
    cpu_freq: float
    memory_used: int
    memory_total: int
    memory_percent: float
    disk_used: int
    disk_total: int
    disk_percent: float
    disk_free: int
    net_sent: int
    net_recv: int
    net_sent_speed: float
    net_recv_speed: float
    boot_time: float
    timestamp: float


class SystemMonitor:
    """系统监控工具"""
    
    def __init__(self, logger: logging.Logger):
        self.logger = logger
        self.previous_net_stats = None
        self.previous_timestamp = None
        self.platform_type = platform.system()
        
        # 每日统计相关
        self.daily_net_stats = None  # 当天零点时的网络数据
        self.daily_reset_date = None  # 最后一次重置的日期
        
        # 立即初始化一次网络统计，避免第一次查询时速度为0
        try:
            net = psutil.net_io_counters()
            self.previous_net_stats = net
            self.previous_timestamp = time.time()
            
            # 初始化每日统计
            self.daily_net_stats = net
            self.daily_reset_date = date.today()
            self.logger.info(f"网络监控已初始化，每日统计重置日期: {self.daily_reset_date}")
        except Exception as e:
            self.logger.warning(f"初始化网络统计失败: {e}")
    
    def _check_daily_reset(self):
        """检查是否需要重置每日统计"""
        today = date.today()
        if self.daily_reset_date != today:
            try:
                net = psutil.net_io_counters()
                self.daily_net_stats = net
                self.daily_reset_date = today
                self.logger.info(f"每日网络统计已重置，新日期: {today}")
            except Exception as e:
                self.logger.error(f"重置每日统计失败: {e}")
    
    def get_system_stats(self) -> Optional[SystemStats]:
        """获取系统统计信息"""
        try:
            cpu_percent = psutil.cpu_percent(interval=1)
            cpu_count = psutil.cpu_count()
            cpu_freq = psutil.cpu_freq().current if psutil.cpu_freq() else 0
            
            memory = psutil.virtual_memory()
            disk = psutil.disk_usage('/')
            net = psutil.net_io_counters()
            current_timestamp = time.time()
            
            net_sent_speed = 0.0
            net_recv_speed = 0.0
            
            # 计算带宽速度
            if self.previous_net_stats is not None and self.previous_timestamp is not None:
                time_delta = current_timestamp - self.previous_timestamp
                
                if time_delta > 0:
                    sent_delta = net.bytes_sent - self.previous_net_stats.bytes_sent
                    recv_delta = net.bytes_recv - self.previous_net_stats.bytes_recv
                    
                    # 转换为 MB/s
                    net_sent_speed = (sent_delta / (1024 * 1024)) / time_delta
                    net_recv_speed = (recv_delta / (1024 * 1024)) / time_delta
                    
                    # 防止负数（可能是系统重启或网络重置）
                    if net_sent_speed < 0:
                        net_sent_speed = 0.0
                    if net_recv_speed < 0:
                        net_recv_speed = 0.0
            
            # 更新前一次的数据
            self.previous_net_stats = net
            self.previous_timestamp = current_timestamp
            
            boot_time = psutil.boot_time()
            
            stats = SystemStats(
                cpu_percent=cpu_percent,
                cpu_count=cpu_count,
                cpu_freq=cpu_freq,
                memory_used=memory.used,
                memory_total=memory.total,
                memory_percent=memory.percent,
                disk_used=disk.used,
                disk_total=disk.total,
                disk_percent=disk.percent,
                disk_free=disk.free,
                net_sent=net.bytes_sent,
                net_recv=net.bytes_recv,
                net_sent_speed=net_sent_speed,
                net_recv_speed=net_recv_speed,
                boot_time=boot_time,
                timestamp=current_timestamp
            )
            
            return stats
            
        except Exception as e:
            self.logger.error(f"获取系统统计信息失败: {e}")
            return None
    
    def get_daily_network_usage(self) -> tuple:
        """获取当天的网络使用量 (发送字节, 接收字节)"""
        try:
            # 检查是否需要重置
            self._check_daily_reset()
            
            net = psutil.net_io_counters()
            
            if self.daily_net_stats is None:
                return (0, 0)
            
            # 计算当天的使用量 = 当前值 - 当天零点的值
            daily_sent = net.bytes_sent - self.daily_net_stats.bytes_sent
            daily_recv = net.bytes_recv - self.daily_net_stats.bytes_recv
            
            # 防止负数
            if daily_sent < 0:
                daily_sent = 0
            if daily_recv < 0:
                daily_recv = 0
            
            return (daily_sent, daily_recv)
            
        except Exception as e:
            self.logger.error(f"获取每日网络使用量失败: {e}")
            return (0, 0)
    
    def format_bytes(self, bytes_value: int) -> str:
        """格式化字节数为可读格式"""
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if bytes_value < 1024.0:
                return f"{bytes_value:.2f} {unit}"
            bytes_value /= 1024.0
        return f"{bytes_value:.2f} PB"
    
    def format_speed(self, speed_mbps: float) -> str:
        """格式化网络速度为可读格式"""
        if speed_mbps < 0.01:
            speed_kbps = speed_mbps * 1024
            return f"{speed_kbps:.2f} KB/s"
        elif speed_mbps < 1000:
            return f"{speed_mbps:.2f} MB/s"
        else:
            speed_gbps = speed_mbps / 1024
            return f"{speed_gbps:.2f} GB/s"
    
    def get_detailed_cpu_info(self) -> Dict[str, Any]:
        """获取详细的CPU频率信息"""
        try:
            cpu_freq = psutil.cpu_freq(percpu=True)  # 获取每个核心的频率
            
            if cpu_freq:
                # 当前频率（所有核心）
                current_freqs = [freq.current for freq in cpu_freq]
                current_avg = sum(current_freqs) / len(current_freqs)
                
                # 最小频率
                min_freq = cpu_freq[0].min
                
                # 最大频率
                max_freq = cpu_freq[0].max
                
                return {
                    'current': current_avg,
                    'current_list': current_freqs,
                    'min': min_freq,
                    'max': max_freq,
                    'count': len(current_freqs)
                }
            else:
                return None
        except Exception as e:
            self.logger.debug(f"获取CPU频率信息失败: {e}")
            return None
    
    def get_uptime_string(self, boot_time: float) -> str:
        """获取系统运行时间字符串"""
        try:
            uptime_seconds = datetime.now().timestamp() - boot_time
            
            days = int(uptime_seconds // 86400)
            hours = int((uptime_seconds % 86400) // 3600)
            minutes = int((uptime_seconds % 3600) // 60)
            seconds = int(uptime_seconds % 60)
            
            if days > 0:
                return f"{days}天 {hours}小时 {minutes}分钟"
            elif hours > 0:
                return f"{hours}小时 {minutes}分钟"
            else:
                return f"{minutes}分钟 {seconds}秒"
                
        except Exception as e:
            self.logger.error(f"计算运行时间失败: {e}")
            return "未知"
    
    def format_system_info(self, stats: SystemStats) -> str:
        """格式化系统信息为字符串"""
        if not stats:
            return "无法获取系统信息"
        
        uptime = self.get_uptime_string(stats.boot_time)
        
        cpu_status = "高" if stats.cpu_percent > 80 else ("中" if stats.cpu_percent > 50 else "低")
        mem_status = "高" if stats.memory_percent > 80 else ("中" if stats.memory_percent > 50 else "低")
        disk_status = "高" if stats.disk_percent > 80 else ("中" if stats.disk_percent > 50 else "低")
        
        # 获取详细CPU信息
        cpu_info = self.get_detailed_cpu_info()
        
        # 获取每日流量
        daily_sent, daily_recv = self.get_daily_network_usage()
        daily_sent_str = self.format_bytes(daily_sent)
        daily_recv_str = self.format_bytes(daily_recv)
        
        # 获取累计流量（系统启动后）
        system_sent_str = self.format_bytes(stats.net_sent)
        system_recv_str = self.format_bytes(stats.net_recv)
        
        net_sent_speed = self.format_speed(stats.net_sent_speed)
        net_recv_speed = self.format_speed(stats.net_recv_speed)
        
        today_str = date.today().strftime('%Y-%m-%d')
        
        # 构建CPU信息字符串
        if cpu_info:
            cpu_freq_info = f"  当前: {cpu_info['current']:.2f} MHz (最小 {cpu_info['min']:.2f} MHz, 最大 {cpu_info['max']:.2f} MHz)\n"
            
            # 格式化每核心频率，每行显示4个核心
            core_freqs_str = "  每核心频率: "
            for i, freq in enumerate(cpu_info['current_list']):
                if i > 0 and i % 4 == 0:
                    core_freqs_str += "\n                   "
                core_freqs_str += f"{freq:.1f}MHz "
            core_freqs_str += "\n"
        else:
            cpu_freq_info = f"  当前: {stats.cpu_freq:.2f} MHz\n"
            core_freqs_str = ""
        
        message = (
            "系统监控信息\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"系统: {self.platform_type}\n"
            f"运行时间: {uptime}\n"
            f"更新时间: {datetime.fromtimestamp(stats.timestamp).strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            
            f"CPU信息:\n"
            f"  核心数: {stats.cpu_count}\n"
            f"{cpu_freq_info}"
            f"{core_freqs_str}"
            f"  使用率: {cpu_status} {stats.cpu_percent:.1f}%\n\n"
            
            f"内存信息:\n"
            f"  已用: {self.format_bytes(stats.memory_used)}\n"
            f"  总量: {self.format_bytes(stats.memory_total)}\n"
            f"  使用率: {mem_status} {stats.memory_percent:.1f}%\n\n"
            
            f"硬盘信息 (/):\n"
            f"  已用: {self.format_bytes(stats.disk_used)}\n"
            f"  可用: {self.format_bytes(stats.disk_free)}\n"
            f"  总量: {self.format_bytes(stats.disk_total)}\n"
            f"  使用率: {disk_status} {stats.disk_percent:.1f}%\n\n"
            
            f"网络流量 (今日 {today_str}):\n"
            f"  上传: {daily_sent_str}\n"
            f"  下载: {daily_recv_str}\n\n"
            
            f"网络流量 (系统启动后):\n"
            f"  上传: {system_sent_str}\n"
            f"  下载: {system_recv_str}\n\n"
            
            f"实时带宽:\n"
            f"  上传速度: {net_sent_speed}\n"
            f"  下载速度: {net_recv_speed}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )
        
        return message
    
    def get_process_info(self, process_name: str = "java") -> str:
        """获取特定进程的信息"""
        try:
            matching_processes = []
            
            for proc in psutil.process_iter(['pid', 'name', 'memory_percent', 'cpu_percent']):
                try:
                    if process_name.lower() in proc.info['name'].lower():
                        matching_processes.append(proc.info)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            
            if not matching_processes:
                return f"未找到进程: {process_name}"
            
            message = f"进程信息 ({process_name}):\n"
            message += "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            
            total_memory = 0
            for proc in matching_processes:
                memory_usage = proc['memory_percent']
                cpu_usage = proc['cpu_percent']
                total_memory += memory_usage
                
                memory_bytes = int(psutil.virtual_memory().total * memory_usage / 100)
                message += (
                    f"PID: {proc['pid']}\n"
                    f"  名称: {proc['name']}\n"
                    f"  内存: {memory_usage:.2f}% ({self.format_bytes(memory_bytes)})\n"
                    f"  CPU: {cpu_usage:.2f}%\n\n"
                )
            
            message += f"总内存占用: {total_memory:.2f}%\n"
            message += "━━━━━━━━━━━━━━━━━━━━━━━━━━"
            
            return message
            
        except Exception as e:
            self.logger.error(f"获取进程信息失败: {e}")
            return f"获取进程信息失败: {str(e)}"
    
    def get_disk_info(self, path: str = "/") -> str:
        """获取详细的磁盘信息"""
        try:
            disk = psutil.disk_usage(path)
            
            used_percent = disk.percent
            bar_length = 20
            used_bars = int(bar_length * used_percent / 100)
            empty_bars = bar_length - used_bars
            bar = "█" * used_bars + "░" * empty_bars
            
            message = (
                f"磁盘信息 ({path}):\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"总容量: {self.format_bytes(disk.total)}\n"
                f"已使用: {self.format_bytes(disk.used)}\n"
                f"可用: {self.format_bytes(disk.free)}\n"
                f"使用率: {disk.percent:.1f}%\n"
                f"[{bar}]\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━"
            )
            
            return message
            
        except Exception as e:
            self.logger.error(f"获取磁盘信息失败: {e}")
            return f"获取磁盘信息失败: {str(e)}"
    
    def get_network_info(self) -> str:
        """获取详细的网络信息"""
        try:
            stats = self.get_system_stats()
            
            if not stats:
                return "无法获取网络信息"
            
            # 获取每日流量
            daily_sent, daily_recv = self.get_daily_network_usage()
            daily_sent_str = self.format_bytes(daily_sent)
            daily_recv_str = self.format_bytes(daily_recv)
            
            # 获取累计流量
            system_sent_str = self.format_bytes(stats.net_sent)
            system_recv_str = self.format_bytes(stats.net_recv)
            
            net_sent_speed = self.format_speed(stats.net_sent_speed)
            net_recv_speed = self.format_speed(stats.net_recv_speed)
            
            net_if_stats = psutil.net_if_stats()
            today_str = date.today().strftime('%Y-%m-%d')
            
            message = "网络信息\n"
            message += "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            
            message += f"今日流量 ({today_str}):\n"
            message += f"  上传: {daily_sent_str}\n"
            message += f"  下载: {daily_recv_str}\n\n"
            
            message += f"累计流量 (系统启动后):\n"
            message += f"  上传: {system_sent_str}\n"
            message += f"  下载: {system_recv_str}\n\n"
            
            message += f"实时带宽:\n"
            message += f"  上传速度: {net_sent_speed}\n"
            message += f"  下载速度: {net_recv_speed}\n\n"
            
            message += "网卡状态:\n"
            for interface, stat in net_if_stats.items():
                status = "在线" if stat.isup else "离线"
                message += f"  {interface}: {status}\n"
            
            message += "━━━━━━━━━━━━━━━━━━━━━━━━━━"
            
            return message
            
        except Exception as e:
            self.logger.error(f"获取网络信息失败: {e}")
            return f"获取网络信息失败: {str(e)}"