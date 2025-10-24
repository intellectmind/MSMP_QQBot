import logging
import os
import gzip
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Set, Dict, Any
import asyncio


class AdvancedLogFilter(logging.Filter):
    """高级日志过滤器"""
    
    def __init__(self):
        super().__init__()
        # 黑名单配置
        self.disabled_loggers: Set[str] = set()
        self.disabled_keywords: Set[str] = set()
        self.disabled_patterns: List[tuple] = []
        
        # 敏感信息脱敏配置
        self.sensitive_keywords = {
            'password': '***',
            'token': '***',
            'secret': '***',
            'auth': '***',
            'key': '***'
        }
        
        # 日志级别过滤
        self.min_level = logging.DEBUG
        self.logger_levels = {}
    
    def filter(self, record: logging.LogRecord) -> bool:
        """过滤日志记录"""
        
        # 1. 检查logger是否被禁用
        if record.name in self.disabled_loggers:
            return False
        
        # 2. 检查日志级别
        if record.levelno < self.min_level:
            return False
        
        # 检查特定logger的级别
        if record.name in self.logger_levels:
            if record.levelno < self.logger_levels[record.name]:
                return False
        
        # 3. 检查关键词黑名单
        message = record.getMessage()
        for keyword in self.disabled_keywords:
            if keyword.lower() in message.lower():
                return False
        
        # 4. 检查特定组合（logger + keyword）
        for logger_name, keyword in self.disabled_patterns:
            if logger_name in record.name and keyword.lower() in message.lower():
                return False
        
        # 5. 脱敏敏感信息
        record.msg = self._sanitize_message(record.msg)
        if record.args:
            record.args = self._sanitize_args(record.args)
        
        return True
    
    def _sanitize_message(self, message: str) -> str:
        """脱敏消息中的敏感信息"""
        for keyword, replacement in self.sensitive_keywords.items():
            import re
            pattern = rf'({keyword})["\']?\s*[:=]\s*["\']?[^"\'\s,;}}]+["\']?'
            message = re.sub(pattern, rf'\1={replacement}', message, flags=re.IGNORECASE)
        return message
    
    def _sanitize_args(self, args) -> tuple:
        """脱敏参数中的敏感信息"""
        if isinstance(args, dict):
            sanitized = {}
            for key, value in args.items():
                if any(keyword in key.lower() for keyword in self.sensitive_keywords.keys()):
                    sanitized[key] = '***'
                else:
                    sanitized[key] = value
            return sanitized
        return args
    
    # 管理方法
    def disable_logger(self, logger_name: str):
        """禁用特定logger"""
        self.disabled_loggers.add(logger_name)
    
    def enable_logger(self, logger_name: str):
        """启用特定logger"""
        self.disabled_loggers.discard(logger_name)
    
    def disable_keyword(self, keyword: str):
        """禁用包含关键词的日志"""
        self.disabled_keywords.add(keyword)
    
    def enable_keyword(self, keyword: str):
        """启用包含关键词的日志"""
        self.disabled_keywords.discard(keyword)
    
    def set_logger_level(self, logger_name: str, level: int):
        """设置特定logger的最小级别"""
        self.logger_levels[logger_name] = level
    
    def set_global_level(self, level: int):
        """设置全局最小级别"""
        self.min_level = level
    
    def is_logger_disabled(self, logger_name: str) -> bool:
        """检查logger是否被禁用"""
        return logger_name in self.disabled_loggers
    
    def is_keyword_disabled(self, keyword: str) -> bool:
        """检查关键词是否被禁用"""
        return keyword in self.disabled_keywords
    
    def get_status(self) -> dict:
        """获取过滤器状态"""
        return {
            'disabled_loggers': list(self.disabled_loggers),
            'disabled_keywords': list(self.disabled_keywords),
            'global_level': logging.getLevelName(self.min_level),
            'logger_levels': {k: logging.getLevelName(v) for k, v in self.logger_levels.items()},
            'sensitive_keywords': list(self.sensitive_keywords.keys())
        }


class LogArchiveManager:
    """日志归档管理器"""
    
    def __init__(self, log_dir: str = "logs", archive_dir: str = "logs/archive"):
        self.log_dir = Path(log_dir)
        self.archive_dir = Path(archive_dir)
        self.logger = logging.getLogger(__name__)
        
        # 创建归档目录
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        
        # 配置
        self.retention_days = 7
        self.compress_age_days = 1
        self.archive_patterns = [
            'msmp_qqbot.log.*',
            'error.log.*',
            'runtime.log.*'
        ]
    
    async def archive_old_logs(self) -> Dict[str, Any]:
        """归档旧日志文件"""
        try:
            now = datetime.now()
            archived_count = 0
            compressed_count = 0
            deleted_count = 0
            
            for log_file in self.log_dir.glob('*.log*'):
                if log_file.is_dir():
                    continue
                
                file_age = now - datetime.fromtimestamp(log_file.stat().st_mtime)
                file_size_mb = log_file.stat().st_size / (1024 * 1024)
                
                # 1. 压缩旧日志
                if file_age.days >= self.compress_age_days and not str(log_file).endswith('.gz'):
                    try:
                        await self._compress_file(log_file)
                        compressed_count += 1
                        self.logger.info(f"已压缩: {log_file.name} ({file_size_mb:.2f}MB)")
                    except Exception as e:
                        self.logger.error(f"压缩失败 {log_file.name}: {e}")
                
                # 2. 移动到归档目录
                if file_age.days >= 1:
                    try:
                        archive_path = self._get_archive_path(log_file)
                        shutil.move(str(log_file), str(archive_path))
                        archived_count += 1
                        self.logger.info(f"已归档: {log_file.name} -> {archive_path.name}")
                    except Exception as e:
                        self.logger.error(f"归档失败 {log_file.name}: {e}")
                
                # 3. 删除超期日志
                if file_age.days >= self.retention_days:
                    try:
                        log_file.unlink()
                        deleted_count += 1
                        self.logger.info(f"已删除: {log_file.name} (超期 {file_age.days} 天)")
                    except Exception as e:
                        self.logger.error(f"删除失败 {log_file.name}: {e}")
            
            # 记录统计
            if archived_count > 0 or compressed_count > 0 or deleted_count > 0:
                summary = f"日志归档完成 - 压缩: {compressed_count}, 归档: {archived_count}, 删除: {deleted_count}"
                self.logger.info(summary)
            
            return {
                'compressed': compressed_count,
                'archived': archived_count,
                'deleted': deleted_count,
                'timestamp': now.isoformat()
            }
            
        except Exception as e:
            self.logger.error(f"日志归档过程出错: {e}", exc_info=True)
            return None
    
    async def _compress_file(self, file_path: Path):
        """压缩单个文件"""
        gz_path = file_path.with_suffix(file_path.suffix + '.gz')
        
        def compress():
            with open(file_path, 'rb') as f_in:
                with gzip.open(gz_path, 'wb') as f_out:
                    shutil.copyfileobj(f_in, f_out)
        
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, compress)
        
        # 删除原文件
        file_path.unlink()
    
    def _get_archive_path(self, log_file: Path) -> Path:
        """生成归档文件路径"""
        mtime = datetime.fromtimestamp(log_file.stat().st_mtime)
        date_dir = self.archive_dir / mtime.strftime('%Y-%m-%d')
        date_dir.mkdir(parents=True, exist_ok=True)
        
        return date_dir / log_file.name
    
    def get_archive_stats(self) -> Dict[str, Any]:
        """获取归档统计信息"""
        stats = {
            'total_files': 0,
            'total_size_mb': 0,
            'by_date': {},
            'compressed_size_mb': 0
        }
        
        for file_path in self.archive_dir.rglob('*'):
            if file_path.is_file():
                stats['total_files'] += 1
                file_size_mb = file_path.stat().st_size / (1024 * 1024)
                stats['total_size_mb'] += file_size_mb
                
                # 按日期统计
                date_str = file_path.parent.name
                if date_str not in stats['by_date']:
                    stats['by_date'][date_str] = {'count': 0, 'size_mb': 0}
                
                stats['by_date'][date_str]['count'] += 1
                stats['by_date'][date_str]['size_mb'] += file_size_mb
                
                # 压缩文件大小
                if str(file_path).endswith('.gz'):
                    stats['compressed_size_mb'] += file_size_mb
        
        return stats
    
    async def cleanup_old_archives(self, days: int = 30) -> Dict[str, Any]:
        """清理超期的归档文件"""
        try:
            now = datetime.now()
            deleted_count = 0
            freed_space_mb = 0
            
            for file_path in self.archive_dir.rglob('*'):
                if file_path.is_file():
                    file_age = now - datetime.fromtimestamp(file_path.stat().st_mtime)
                    
                    if file_age.days >= days:
                        freed_space_mb += file_path.stat().st_size / (1024 * 1024)
                        file_path.unlink()
                        deleted_count += 1
            
            if deleted_count > 0:
                self.logger.info(f"已清理 {deleted_count} 个超期归档文件，释放 {freed_space_mb:.2f}MB 空间")
            
            return {'deleted': deleted_count, 'freed_space_mb': freed_space_mb}
            
        except Exception as e:
            self.logger.error(f"清理归档文件出错: {e}", exc_info=True)
            return None


class LogManager:
    """统一的日志管理器"""
    
    def __init__(self, config_manager=None):
        self.config_manager = config_manager
        self.log_filter = AdvancedLogFilter()
        self.archive_manager = LogArchiveManager()
        self.logger = logging.getLogger(__name__)
        
        # 默认禁用的日志器
        self._setup_default_filters()
    
    def _setup_default_filters(self):
        """设置默认过滤器"""
        # 默认禁用一些噪音日志器
        noisy_loggers = ['websockets', 'asyncio']
        for logger_name in noisy_loggers:
            self.log_filter.disable_logger(logger_name)
    
    def setup_logging(self, debug_mode: bool = False):
        """设置完整的日志系统"""
        log_dir = "logs"
        os.makedirs(log_dir, exist_ok=True)
        
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.DEBUG if debug_mode else logging.INFO)
        
        # 清理现有处理器
        for handler in root_logger.handlers[:]:
            root_logger.removeHandler(handler)
        
        # 主日志文件 - 按大小轮转
        main_handler = logging.handlers.RotatingFileHandler(
            os.path.join(log_dir, 'msmp_qqbot.log'),
            maxBytes=10*1024*1024,
            backupCount=5,
            encoding='utf-8'
        )
        main_handler.setLevel(logging.DEBUG)
        main_handler.addFilter(self.log_filter)
        
        # 错误日志文件
        error_handler = logging.handlers.RotatingFileHandler(
            os.path.join(log_dir, 'error.log'),
            maxBytes=5*1024*1024,
            backupCount=3,
            encoding='utf-8'
        )
        error_handler.setLevel(logging.ERROR)
        error_handler.addFilter(self.log_filter)
        
        # 控制台处理器
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        console_handler.addFilter(self.log_filter)
        
        # 运行日志 - 按天轮转
        runtime_handler = logging.handlers.TimedRotatingFileHandler(
            os.path.join(log_dir, 'runtime.log'),
            when='midnight',
            interval=1,
            backupCount=7,
            encoding='utf-8'
        )
        runtime_handler.setLevel(logging.INFO)
        runtime_handler.addFilter(self.log_filter)
        
        # 日志格式
        detailed_formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s'
        )
        simple_formatter = logging.Formatter(
            '%(asctime)s - %(levelname)s - %(message)s'
        )
        
        main_handler.setFormatter(detailed_formatter)
        error_handler.setFormatter(detailed_formatter)
        console_handler.setFormatter(simple_formatter)
        runtime_handler.setFormatter(simple_formatter)
        
        root_logger.addHandler(main_handler)
        root_logger.addHandler(error_handler)
        root_logger.addHandler(console_handler)
        root_logger.addHandler(runtime_handler)
        
        self.logger.info("日志系统初始化完成")
    
    # 日志过滤管理方法
    def toggle_mc_server_log(self) -> bool:
        """切换MC服务端日志输出"""
        if self.log_filter.is_keyword_disabled('[MC Server]'):
            self.log_filter.enable_keyword('[MC Server]')
            self.logger.info("MC服务端日志已启用")
            return True
        else:
            self.log_filter.disable_keyword('[MC Server]')
            self.logger.info("MC服务端日志已禁用")
            return False
    
    def toggle_bot_log(self) -> bool:
        """切换MSMP_QQBot日志输出"""
        bot_loggers = [
            '__main__',
            'config_manager',
            'qq_bot_server',
            'msmp_client',
            'rcon_client',
            'command_handler',
            'custom_listener'
        ]
        
        all_disabled = all(self.log_filter.is_logger_disabled(name) for name in bot_loggers)
        
        if all_disabled:
            for logger_name in bot_loggers:
                self.log_filter.enable_logger(logger_name)
            self.logger.info("MSMP_QQBot日志已启用")
            return True
        else:
            for logger_name in bot_loggers:
                self.log_filter.disable_logger(logger_name)
            self.logger.info("MSMP_QQBot日志已禁用")
            return False
    
    def mute_keyword(self, keyword: str) -> bool:
        """禁用包含指定关键词的日志"""
        if self.log_filter.is_keyword_disabled(keyword):
            return False
        self.log_filter.disable_keyword(keyword)
        self.logger.info(f"已禁用包含 '{keyword}' 的日志")
        return True
    
    def unmute_keyword(self, keyword: str) -> bool:
        """启用包含指定关键词的日志"""
        if not self.log_filter.is_keyword_disabled(keyword):
            return False
        self.log_filter.enable_keyword(keyword)
        self.logger.info(f"已启用包含 '{keyword}' 的日志")
        return True
    
    def get_log_status(self) -> Dict[str, Any]:
        """获取日志状态信息"""
        filter_status = self.log_filter.get_status()
        
        mc_status = "禁用" if self.log_filter.is_keyword_disabled('[MC Server]') else "启用"
        
        bot_loggers = [
            '__main__', 'config_manager', 'qq_bot_server', 
            'msmp_client', 'rcon_client', 'command_handler', 'custom_listener'
        ]
        all_disabled = all(self.log_filter.is_logger_disabled(name) for name in bot_loggers)
        bot_status = "禁用" if all_disabled else "启用"
        
        return {
            'mc_server_log': mc_status,
            'bot_log': bot_status,
            'disabled_keywords': list(self.log_filter.disabled_keywords),
            'disabled_loggers': list(self.log_filter.disabled_loggers),
            'global_level': filter_status['global_level']
        }
    
    def get_logs_info(self) -> str:
        """获取日志文件信息"""
        log_dir = "logs"
        if not os.path.exists(log_dir):
            return "日志目录不存在"
        
        lines = ["日志文件信息:", "=" * 50]
        files_found = False
        
        for file in os.listdir(log_dir):
            if file.endswith('.log'):
                files_found = True
                file_path = os.path.join(log_dir, file)
                size = os.path.getsize(file_path)
                mtime = time.strftime('%Y-%m-%d %H:%M:%S', 
                                    time.localtime(os.path.getmtime(file_path)))
                
                if size > 1024*1024:
                    size_str = f"{size/(1024*1024):.2f} MB"
                elif size > 1024:
                    size_str = f"{size/1024:.2f} KB"
                else:
                    size_str = f"{size} B"
                
                lines.append(f"{file:20} {size_str:>10}  修改: {mtime}")
        
        if not files_found:
            lines.append("没有日志文件")
        
        lines.append("=" * 50)
        return "\n".join(lines)
    
    # 归档管理方法
    async def archive_logs(self) -> Dict[str, Any]:
        """执行日志归档"""
        return await self.archive_manager.archive_old_logs()
    
    def get_archive_stats(self) -> Dict[str, Any]:
        """获取归档统计信息"""
        return self.archive_manager.get_archive_stats()
    
    async def cleanup_archives(self, days: int = 30) -> Dict[str, Any]:
        """清理超期归档"""
        return await self.archive_manager.cleanup_old_archives(days)