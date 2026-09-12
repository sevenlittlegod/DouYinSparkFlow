import os, sys
from enum import Enum
import json
import logging
from utils.logger import setup_logger
from utils import norm

logger = setup_logger(level=logging.DEBUG)

"""
是否启用调试模式
更详细的日志打印，浏览器操作可视化等
"""
DEBUG = True
config = None
userData = None


class Environment(Enum):
    GITHUBACTION = "GITHUB_ACTION"  # GitHub Action 运行
    LOCAL = "LOCAL"  # 本地代码运行
    PACKED = "PACKED"  # PyInstaller 打包运行

    def __str__(self):
        return self.value


def get_environment():
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Environment.PACKED
    elif os.getenv("GITHUB_ACTIONS") == "true":
        return Environment.GITHUBACTION
    else:
        return Environment.LOCAL


def get_config():
    """
    获取配置信息
    :return: 配置字典
    """
    global config

    if config:
        return config

    config = {
        "proxyAddress": os.getenv("PROXY_ADDRESS", ""),
        "messageTemplate": os.getenv(
            "MESSAGE_TEMPLATE",
            "[盖瑞]今日火花[加一]\\n—— [右边] 每日一言 [左边] ——\\n[API]",
        ),
        "hitokotoTypes": json.loads(
            os.getenv("HITOKOTO_TYPES", '["文学","影视","诗词","哲学"]')
        ),
        "browserTimeout": int(
            os.getenv("BROWSER_TIMEOUT", "120000")
        ),  # 浏览器操作超时时间，单位毫秒
        "friendListTimeout": int(
            os.getenv("FRIEND_LIST_WAIT_TIME", "2000")
        ),  # 好友列表加载超时时间，单位毫秒
        "taskRetryTimes": int(os.getenv("TASK_RETRY_TIMES", "3")),  # 任务重试次数
        "logLevel": os.getenv("LOG_LEVEL", "DEBUG"),  # 日志级别
    }

    return config


def sanitize_cookies(cookies):
    for cookie in cookies:
        if "sameSite" in cookie:
            cookie.pop("sameSite")  # 移除 sameSite 字段，Playwright 可能不支持该字段
    return cookies


def get_userData():
    """
    获取用户数据目录
    :return: 用户数据目录路径
    """
    global userData

    if userData:
        return userData

    try:
        tasks = json.loads(os.getenv("TASKS", "[]"))
    except json.JSONDecodeError:
        raise ValueError("TASKS 必须是有效的 JSON 数组，请重新检查任务配置。") from None
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("TASKS 必须是非空 JSON 数组；没有可执行账号，请先配置任务。")

    validated_users = []

    for index, task in enumerate(tasks, start=1):
        if not isinstance(task, dict):
            raise ValueError(f"TASKS 第 {index} 项必须是账号配置对象。")
        username = task.get("username", "未知用户")
        unique_id = task.get("unique_id")
        if not isinstance(unique_id, str) or not unique_id.strip():
            raise ValueError(f"TASKS 第 {index} 项缺少有效的 unique_id 字符串。")
        targets = task.get("targets")
        if not isinstance(targets, list) or not targets or any(
            not isinstance(target, str) or not norm(target) for target in targets
        ):
            raise ValueError(f"TASKS 第 {index} 项的 targets 必须是非空字符串数组。")
        cookies_key = f"cookies_{unique_id}".upper()
        cookies_str = os.getenv(cookies_key, "")
        if not cookies_str:
            raise ValueError(f"缺少 {cookies_key}，请配置对应账号的 Cookie JSON。")
        try:
            cookies = json.loads(cookies_str)
        except json.JSONDecodeError:
            raise ValueError(f"{cookies_key} 不是有效的 JSON，请重新导出 Cookie。") from None
        if not isinstance(cookies, list) or not cookies:
            raise ValueError(f"{cookies_key} 必须是非空 Cookie JSON 数组。")
        for cookie in cookies:
            if (
                not isinstance(cookie, dict)
                or not isinstance(cookie.get("name"), str)
                or not isinstance(cookie.get("value"), str)
                or not (
                    isinstance(cookie.get("url"), str) and cookie["url"]
                    or isinstance(cookie.get("domain"), str) and cookie["domain"]
                    and isinstance(cookie.get("path"), str) and cookie["path"]
                )
            ):
                raise ValueError(
                    f"{cookies_key} 中的 Cookie 格式不完整；请在 www.douyin.com/chat "
                    "登录后重新导出完整的 JSON 数组（含 name、value 和 domain/path 或 url）。"
                )

        validated_users.append(
            {
                "unique_id": unique_id,
                "username": username,
                "cookies": sanitize_cookies(cookies),
                "targets": [norm(t) for t in targets],
            }
        )

    userData = validated_users
    return userData
