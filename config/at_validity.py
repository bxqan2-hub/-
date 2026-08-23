# -*- coding: utf-8 -*-
"""账号 Access Token 定时有效性检测配置。"""

from config.env_loader import apply_env_overrides


# WebUI 启动后开启轻量 AT 有效性检测；首次检测在一个完整周期后执行。
AT_VALIDITY_AUTO_CHECK_ENABLED = True

# 定时检测周期（分钟）。账号页顶部可直接修改并热加载。
AT_VALIDITY_CHECK_INTERVAL_MINUTES = 360


apply_env_overrides(
    globals(),
    {
        "AT_VALIDITY_AUTO_CHECK_ENABLED": "bool",
        "AT_VALIDITY_CHECK_INTERVAL_MINUTES": "int",
    },
)
