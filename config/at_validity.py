# -*- coding: utf-8 -*-
"""账号 Access Token 定时有效性检测配置。"""

from config.env_loader import apply_env_overrides


# WebUI 启动后开启轻量 AT 有效性检测；首次检测在一个完整周期后执行。
AT_VALIDITY_AUTO_CHECK_ENABLED = True

# 定时检测周期（分钟）。账号页顶部可直接修改并热加载。
AT_VALIDITY_CHECK_INTERVAL_MINUTES = 360

# AT 检测使用独立队列，只请求会话接口，不进入套餐或 0 元试用判断。
AT_VALIDITY_WORKERS = 5
AT_VALIDITY_QUEUE_LIMIT = 500
AT_VALIDITY_REQUEST_ATTEMPTS = 5
AT_VALIDITY_RETRY_DELAY = 1.0
AT_VALIDITY_MIN_INTERVAL = 0.4
AT_VALIDITY_JITTER = 0.2


apply_env_overrides(
    globals(),
    {
        "AT_VALIDITY_AUTO_CHECK_ENABLED": "bool",
        "AT_VALIDITY_CHECK_INTERVAL_MINUTES": "int",
        "AT_VALIDITY_WORKERS": "int",
        "AT_VALIDITY_QUEUE_LIMIT": "int",
        "AT_VALIDITY_REQUEST_ATTEMPTS": "int",
        "AT_VALIDITY_RETRY_DELAY": "float",
        "AT_VALIDITY_MIN_INTERVAL": "float",
        "AT_VALIDITY_JITTER": "float",
    },
)
