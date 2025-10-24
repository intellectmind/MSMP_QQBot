import asyncio
import logging
import datetime
import time
from typing import Callable, List, Optional, Dict, Any
from dataclasses import dataclass


@dataclass
class ScheduledTask:
    """定时任务"""
    task_id: str
    task_type: str  # 'start' 或 'stop' 或 'restart'
    scheduled_time: str  # HH:MM 格式
    weekdays: List[int]  # 执行的星期 [0=周一, 6=周日]
    last_executed: Optional[float] = None
    enabled: bool = True


class ScheduledTaskManager:
    """定时任务管理器 - 支持星期配置"""
    
    WEEKDAY_NAMES = {
        0: '周一', 1: '周二', 2: '周三', 3: '周四',
        4: '周五', 5: '周六', 6: '周日'
    }
    
    def __init__(self, config_manager, qq_server, logger: logging.Logger):
        self.config_manager = config_manager
        self.qq_server = qq_server
        self.logger = logger
        
        self.tasks: List[ScheduledTask] = []
        self.running = False
        self.scheduler_task = None
        
        # 回调函数
        self.on_start_callback: Optional[Callable] = None
        self.on_stop_callback: Optional[Callable] = None
        self.on_restart_callback: Optional[Callable] = None
        self.on_notify_callback: Optional[Callable] = None
        
        self._load_tasks_from_config()
    
    def _load_tasks_from_config(self):
        """从配置文件加载定时任务"""
        try:
            scheduled_config = self.config_manager.config.get('scheduled_tasks', {})
            
            if not scheduled_config.get('enabled', False):
                self.logger.info("定时任务已禁用")
                return
            
            # 加载自动启动任务
            auto_start_config = scheduled_config.get('auto_start', {})
            if auto_start_config.get('enabled', False):
                start_times = auto_start_config.get('times', [])
                start_weekdays = auto_start_config.get('weekdays', [0, 1, 2, 3, 4, 5, 6])
                
                for i, time_str in enumerate(start_times):
                    task = ScheduledTask(
                        task_id=f"auto_start_{i}",
                        task_type='start',
                        scheduled_time=time_str,
                        weekdays=start_weekdays,
                        enabled=True
                    )
                    self.tasks.append(task)
                    weekday_names = [self.WEEKDAY_NAMES[d] for d in start_weekdays]
                    self.logger.info(f"已加载自动启动任务: {time_str} ({','.join(weekday_names)})")
            
            # 加载自动停止任务
            auto_stop_config = scheduled_config.get('auto_stop', {})
            if auto_stop_config.get('enabled', False):
                stop_times = auto_stop_config.get('times', [])
                stop_weekdays = auto_stop_config.get('weekdays', [0, 1, 2, 3, 4, 5, 6])
                
                for i, time_str in enumerate(stop_times):
                    task = ScheduledTask(
                        task_id=f"auto_stop_{i}",
                        task_type='stop',
                        scheduled_time=time_str,
                        weekdays=stop_weekdays,
                        enabled=True
                    )
                    self.tasks.append(task)
                    weekday_names = [self.WEEKDAY_NAMES[d] for d in stop_weekdays]
                    self.logger.info(f"已加载自动停止任务: {time_str} ({','.join(weekday_names)})")
            
            # 加载自动重启任务
            auto_restart_config = scheduled_config.get('auto_restart', {})
            if auto_restart_config.get('enabled', False):
                restart_times = auto_restart_config.get('times', [])
                restart_weekdays = auto_restart_config.get('weekdays', [0, 1, 2, 3, 4, 5, 6])
                
                for i, time_str in enumerate(restart_times):
                    task = ScheduledTask(
                        task_id=f"auto_restart_{i}",
                        task_type='restart',
                        scheduled_time=time_str,
                        weekdays=restart_weekdays,
                        enabled=True
                    )
                    self.tasks.append(task)
                    weekday_names = [self.WEEKDAY_NAMES[d] for d in restart_weekdays]
                    self.logger.info(f"已加载自动重启任务: {time_str} ({','.join(weekday_names)})")
            
            self.logger.info(f"共加载 {len(self.tasks)} 个定时任务")
            
        except Exception as e:
            self.logger.error(f"加载定时任务配置失败: {e}", exc_info=True)
    
    def set_start_callback(self, callback: Callable):
        """设置启动回调函数"""
        self.on_start_callback = callback
    
    def set_stop_callback(self, callback: Callable):
        """设置停止回调函数"""
        self.on_stop_callback = callback
    
    def set_restart_callback(self, callback: Callable):
        """设置重启回调函数"""
        self.on_restart_callback = callback
    
    def set_notify_callback(self, callback: Callable):
        """设置通知回调函数"""
        self.on_notify_callback = callback
    
    def start(self):
        """启动定时任务管理器"""
        if self.running:
            self.logger.warning("定时任务管理器已在运行")
            return
        
        self.running = True
        self.logger.info("定时任务管理器已启动")
        
        self.scheduler_task = asyncio.create_task(self._scheduler_loop())
    
    def stop(self):
        """停止定时任务管理器"""
        self.running = False
        
        if self.scheduler_task and not self.scheduler_task.done():
            self.scheduler_task.cancel()
        
        self.logger.info("定时任务管理器已停止")
    
    async def _scheduler_loop(self):
        """定时任务调度循环"""
        try:
            self.logger.info("定时任务调度循环已启动")
            
            while self.running:
                try:
                    current_time = datetime.datetime.now()
                    current_weekday = current_time.weekday()  # 0=周一, 6=周日
                    current_hour_minute = current_time.strftime("%H:%M")
                    current_timestamp = time.time()
                    
                    for task in self.tasks:
                        if not task.enabled:
                            continue
                        
                        # 检查星期是否匹配
                        if current_weekday not in task.weekdays:
                            continue
                        
                        # 检查是否到达定时时间
                        if task.scheduled_time == current_hour_minute:
                            # 防止重复执行 (同一分钟内只执行一次)
                            if task.last_executed is not None:
                                time_since_last = current_timestamp - task.last_executed
                                if time_since_last < 60:
                                    continue
                            
                            # 执行任务
                            await self._execute_task(task, current_time)
                            task.last_executed = current_timestamp
                    
                    # 检查即将到达的任务，发送提前通知
                    await self._check_upcoming_tasks(current_time, current_weekday)
                    
                    # 每10秒检查一次
                    await asyncio.sleep(10)
                    
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self.logger.error(f"定时任务调度出错: {e}", exc_info=True)
                    await asyncio.sleep(10)
        
        except asyncio.CancelledError:
            self.logger.info("定时任务调度循环已取消")
        except Exception as e:
            self.logger.error(f"定时任务调度循环异常: {e}", exc_info=True)
    
    async def _check_upcoming_tasks(self, current_time: datetime.datetime, current_weekday: int):
        """检查即将到达的任务并发送提前通知"""
        try:
            scheduled_config = self.config_manager.config.get('scheduled_tasks', {})
            
            for task in self.tasks:
                if not task.enabled:
                    continue
                
                # 检查星期是否匹配
                if current_weekday not in task.weekdays:
                    continue
                
                # 解析计划时间
                try:
                    task_hour, task_minute = map(int, task.scheduled_time.split(':'))
                    task_datetime = current_time.replace(hour=task_hour, minute=task_minute, second=0, microsecond=0)
                except ValueError:
                    continue
                
                # 计算距离任务执行的时间
                time_until_task = (task_datetime - current_time).total_seconds()
                
                if task.task_type == 'start':
                    config = scheduled_config.get('auto_start', {})
                    pre_notify = config.get('pre_notify_seconds', 300)
                    
                    if 0 <= time_until_task < pre_notify and time_until_task > pre_notify - 10:
                        await self._send_notify(
                            task,
                            config.get('notify_message', '服务器将在 {countdown} 秒后启动'),
                            int(time_until_task)
                        )
                
                elif task.task_type == 'stop':
                    config = scheduled_config.get('auto_stop', {})
                    warning_before = config.get('warning_before_seconds', 600)
                    
                    if 0 <= time_until_task < warning_before and time_until_task > warning_before - 10:
                        await self._send_notify(
                            task,
                            config.get('first_warning', '服务器将在 {countdown} 秒后关闭'),
                            int(time_until_task)
                        )
                    
                    elif 50 <= time_until_task < 70:
                        await self._send_notify(
                            task,
                            config.get('second_warning', '服务器即将在 1 分钟后关闭'),
                            60
                        )
                
                elif task.task_type == 'restart':
                    config = scheduled_config.get('auto_restart', {})
                    warning_before = config.get('warning_before_seconds', 600)
                    
                    if 0 <= time_until_task < warning_before and time_until_task > warning_before - 10:
                        await self._send_notify(
                            task,
                            config.get('first_warning', '服务器将在 {countdown} 秒后重启'),
                            int(time_until_task)
                        )
                    
                    elif 50 <= time_until_task < 70:
                        await self._send_notify(
                            task,
                            config.get('second_warning', '服务器即将在 1 分钟后重启'),
                            60
                        )
        
        except Exception as e:
            self.logger.error(f"检查即将到达的任务出错: {e}", exc_info=True)
    
    async def _execute_task(self, task: ScheduledTask, current_time: datetime.datetime):
        """执行定时任务"""
        try:
            weekday_name = self.WEEKDAY_NAMES[current_time.weekday()]
            self.logger.info(f"执行定时任务: {task.task_id} ({task.task_type}) - {task.scheduled_time} ({weekday_name})")
            
            if task.task_type == 'start':
                if (self.qq_server.server_process and 
                    self.qq_server.server_process.poll() is None):
                    self.logger.warning(f"服务器已在运行,跳过启动任务")
                    await self._send_notify(task, "服务器已在运行,无需启动", 0)
                    return
                
                self.logger.info(f"执行启动任务: {task.scheduled_time}")
                
                if self.on_start_callback:
                    await self.on_start_callback(task)
                else:
                    self.logger.warning("启动回调函数未设置")
            
            elif task.task_type == 'stop':
                if not (self.qq_server.server_process and 
                        self.qq_server.server_process.poll() is None):
                    self.logger.warning(f"服务器未运行,跳过停止任务")
                    await self._send_notify(task, "服务器未运行,无需停止", 0)
                    return
                
                self.logger.info(f"执行停止任务: {task.scheduled_time}")
                
                if self.on_stop_callback:
                    await self.on_stop_callback(task)
                else:
                    self.logger.warning("停止回调函数未设置")
            
            elif task.task_type == 'restart':
                if not (self.qq_server.server_process and 
                        self.qq_server.server_process.poll() is None):
                    self.logger.warning(f"服务器未运行,跳过重启任务")
                    await self._send_notify(task, "服务器未运行,无需重启", 0)
                    return
                
                self.logger.info(f"执行重启任务: {task.scheduled_time}")
                
                if self.on_stop_callback:
                    await self.on_stop_callback(task)
                
                scheduled_config = self.config_manager.config.get('scheduled_tasks', {})
                restart_config = scheduled_config.get('auto_restart', {})
                
                wait_time = restart_config.get('wait_before_startup', 10)
                self.logger.info(f"等待 {wait_time} 秒后重启服务器...")
                await asyncio.sleep(wait_time)
                
                if self.on_restart_callback:
                    await self.on_restart_callback(task)
                else:
                    self.logger.warning("重启回调函数未设置")
        
        except Exception as e:
            self.logger.error(f"执行定时任务失败: {e}", exc_info=True)
    
    async def _send_notify(self, task: ScheduledTask, message_template: str, countdown: int):
        """发送通知"""
        try:
            message = message_template.replace('{countdown}', str(countdown))
            
            if self.on_notify_callback:
                await self.on_notify_callback(task, message)
            
            self.logger.info(f"[通知] {message}")
        
        except Exception as e:
            self.logger.error(f"发送通知失败: {e}")
    
    def reload_tasks_from_config(self):
        """重新加载配置中的定时任务"""
        self.tasks.clear()
        self._load_tasks_from_config()
        self.logger.info("定时任务已重新加载")
    
    def list_tasks(self) -> str:
        """列出所有定时任务"""
        if not self.tasks:
            return "未配置任何定时任务"
        
        message = "定时任务列表\n" + "=" * 50 + "\n"
        
        start_tasks = [t for t in self.tasks if t.task_type == 'start']
        stop_tasks = [t for t in self.tasks if t.task_type == 'stop']
        restart_tasks = [t for t in self.tasks if t.task_type == 'restart']
        
        if start_tasks:
            message += "启动任务:\n"
            for task in start_tasks:
                status = "启用" if task.enabled else "禁用"
                weekday_names = [self.WEEKDAY_NAMES[d] for d in task.weekdays]
                message += f"  • {task.scheduled_time} [{status}] ({','.join(weekday_names)})\n"
        
        if stop_tasks:
            message += "\n停止任务:\n"
            for task in stop_tasks:
                status = "启用" if task.enabled else "禁用"
                weekday_names = [self.WEEKDAY_NAMES[d] for d in task.weekdays]
                message += f"  • {task.scheduled_time} [{status}] ({','.join(weekday_names)})\n"
        
        if restart_tasks:
            message += "\n重启任务:\n"
            for task in restart_tasks:
                status = "启用" if task.enabled else "禁用"
                weekday_names = [self.WEEKDAY_NAMES[d] for d in task.weekdays]
                message += f"  • {task.scheduled_time} [{status}] ({','.join(weekday_names)})\n"
        
        message += "=" * 50
        return message
    
    def disable_task(self, task_id: str) -> bool:
        """禁用指定任务"""
        for task in self.tasks:
            if task.task_id == task_id:
                task.enabled = False
                self.logger.info(f"已禁用任务: {task_id}")
                return True
        return False
    
    def enable_task(self, task_id: str) -> bool:
        """启用指定任务"""
        for task in self.tasks:
            if task.task_id == task_id:
                task.enabled = True
                self.logger.info(f"已启用任务: {task_id}")
                return True
        return False