# -*- coding: utf-8 -*-
from typing import Any, Dict, List


PLATFORM_KNOWLEDGE_BASE: List[Dict[str, Any]] = [
    {
        "platform": "MTK",
        "capabilities": ["硬件解码", "多媒体播放", "广告插播"],
        "risks": [
            {"feature": "投屏恢复", "risk": "广告打断后投屏恢复延迟", "severity": "P1"},
            {"feature": "状态切换", "risk": "并发切换时状态同步不稳定", "severity": "P1"},
        ],
    },
    {
        "platform": "RK3566",
        "capabilities": ["GPU渲染", "硬件视频解码", "多任务调度"],
        "risks": [
            {"feature": "广告打断", "risk": "高码率视频下解码资源竞争", "severity": "P1"},
            {"feature": "投屏", "risk": "投屏与广告并发可能出现黑屏闪断", "severity": "P1"},
        ],
    },
    {
        "platform": "Amlogic",
        "capabilities": ["视频播放", "多媒体混流", "基础投屏"],
        "risks": [
            {"feature": "异常恢复", "risk": "弱网场景重连后音画同步漂移", "severity": "P2"},
            {"feature": "权限控制", "risk": "多终端同步权限校验遗漏概率较高", "severity": "P2"},
        ],
    },
]


def get_platform_knowledge_base() -> List[Dict[str, Any]]:
    return PLATFORM_KNOWLEDGE_BASE

