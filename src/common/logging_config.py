"""日志配置"""

import logging
import sys
from typing import Optional


def setup_logging(level: str = "INFO", format_type: str = "text") -> None:
    """
    配置日志系统

    Args:
        level: 日志级别（DEBUG, INFO, WARNING, ERROR, CRITICAL）
        format_type: 格式类型（text 或 json）

    Examples:
        >>> setup_logging("INFO", "text")
        >>> logger = logging.getLogger(__name__)
        >>> logger.info("Test log")  # doctest: +SKIP
    """
    log_level = getattr(logging, level.upper(), logging.INFO)

    if format_type == "json":
        # JSON 格式（便于机器解析）
        formatter = logging.Formatter(
            '{"time":"%(asctime)s","level":"%(levelname)s",'
            '"name":"%(name)s","message":"%(message)s"}'
        )
    else:
        # 文本格式（便于人类阅读）
        formatter = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    # 配置根日志记录器
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    # 移除现有处理器
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    # 添加控制台处理器
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)


def get_logger(name: str, level: Optional[str] = None) -> logging.Logger:
    """
    获取命名日志记录器

    Args:
        name: 日志记录器名称（通常使用 __name__）
        level: 可选的日志级别覆盖

    Returns:
        配置好的日志记录器

    Examples:
        >>> logger = get_logger(__name__)
        >>> logger.name  # doctest: +SKIP
        '__main__'
    """
    logger = logging.getLogger(name)
    if level:
        logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    return logger
