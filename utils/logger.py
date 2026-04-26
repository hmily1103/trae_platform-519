"""
统一日志管理：支持环境变量配置、日志轮转
"""
import logging
import os
from logging.handlers import RotatingFileHandler


def _get_log_config() -> dict:
    """从环境变量读取日志配置"""
    root = os.path.dirname(os.path.dirname(__file__))
    return {
        "log_dir": os.environ.get("LOG_DIR") or os.path.join(root, "logs"),
        "max_bytes": int(os.environ.get("LOG_MAX_BYTES", "10485760")),  # 10MB
        "backup_count": int(os.environ.get("LOG_BACKUP_COUNT", "5")),
    }


def setup_logger(name: str, log_file: str = None, level: int = logging.INFO) -> logging.Logger:
    """
    设置日志记录器，支持 RotatingFileHandler 轮转
    
    :param name: 日志记录器名称
    :param log_file: 日志文件路径（可选，支持 LOG_DIR 环境变量覆盖目录）
    :param level: 日志级别
    :return: Logger 实例
    """
    logger = logging.getLogger(name)
    logger.setLevel(level)
    
    # 避免重复添加 handler
    if logger.handlers:
        return logger
    
    # 控制台输出格式
    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_format = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    console_handler.setFormatter(console_format)
    logger.addHandler(console_handler)
    
    # 文件输出（如果指定了日志文件）
    if log_file:
        cfg = _get_log_config()
        # 若 LOG_DIR 已设置，可覆盖 log_file 的目录部分
        if os.environ.get("LOG_DIR"):
            log_dir = cfg["log_dir"]
            base_name = os.path.basename(log_file)
            log_file = os.path.join(log_dir, base_name)
        # 确保日志目录存在
        log_dir = os.path.dirname(log_file)
        if log_dir and not os.path.exists(log_dir):
            os.makedirs(log_dir, exist_ok=True)
        
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=cfg["max_bytes"],
            backupCount=cfg["backup_count"],
            encoding='utf-8'
        )
        file_handler.setLevel(level)
        file_format = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        file_handler.setFormatter(file_format)
        logger.addHandler(file_handler)
    
    return logger


# 创建平台主日志记录器（支持 LOG_DIR 环境变量）
_root = os.path.dirname(os.path.dirname(__file__))
_default_log_dir = os.path.join(_root, "logs")
_log_dir = os.environ.get("LOG_DIR") or _default_log_dir
platform_logger = setup_logger(
    'trae_platform',
    log_file=os.path.join(_log_dir, 'platform.log')
)

