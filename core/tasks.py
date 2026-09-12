import traceback
from utils.logger import setup_logger
from utils.config import get_config, get_userData
from utils import norm
from core.msg_builder import build_message, build_message_with_openai
from core.browser import get_browser
from playwright.sync_api import Response, TimeoutError as PlaywrightTimeoutError
import time

config = get_config()
logger = setup_logger(level=config.get("logLevel", "Info"))
userIDDict = {}

CONVERSATION_ITEM_SELECTOR = ".conversationConversationItemwrapper"
CONVERSATION_TITLE_SELECTOR = ".conversationConversationItemtitle"
CONVERSATION_LIST_SELECTOR = ".conversationConversationListwrapper"
CHAT_EDITOR_SELECTOR = ".messageEditorimChatEditorContainer"


def handle_response(response: Response):
    """
    只监听你要的那个接口响应
    """
    global userIDDict
    # 精准匹配目标接口 URL
    if "aweme/v1/web/im/user/info" in response.url:
        # print(f"URL: {response.url}")
        # print(f"状态码: {response.status}")
        try:
            # 获取接口返回的 JSON 数据
            json_data = response.json()
            # print("\n📦 响应 JSON 数据：")
            # print(json.dumps(json_data, indent=4, ensure_ascii=False))
            for item in json_data.get("data", []):
                short_id = item.get("short_id")  # short_id
                unique_id = item.get("unique_id")  # unique_id
                sec_uid = item.get("sec_uid", "")  # sec_uid 可能不存在，提供默认值为空字符串
                nickname = norm(item.get("nickname"))  # 昵称
                remark_name = norm(item.get("remark_name", nickname))  #  备注名，如果没有则使用昵称
                userIDDict[remark_name] = [short_id, unique_id, sec_uid, nickname, remark_name]
        except Exception as e:
            tb = traceback.extract_tb(e.__traceback__)
            last = tb[-1]
            print(f"解析响应失败: {e}")
            print(f"文件: {last.filename}, 行号: {last.lineno}, 函数: {last.name}")


def retry_operation(name, operation, retries=3, delay=2, *args, **kwargs):
    """
    通用的重试逻辑
    :param name: 操作名称（用于日志记录）
    :param operation: 要执行的异步操作
    :param retries: 最大重试次数
    :param delay: 每次重试之间的延迟（秒）
    :param args: 传递给操作的参数
    :param kwargs: 传递给操作的关键字参数
    """
    for attempt in range(retries):
        try:
            return operation(*args, **kwargs)
        except Exception as e:
            if attempt < retries - 1:
                logger.warning(f"{name} 失败，正在重试第 {attempt + 1} 次，错误：{e}")
                time.sleep(delay)
            else:
                logger.error(f"{name} 失败，已达到最大重试次数，错误：{e}")
                raise

def checkTargetName(targetName, targets):
    """检查targetName是否为目标
    """

    targetSymbol = None

    targetName = norm(targetName)

    if targetName in userIDDict:
        matched = next((v for v in userIDDict[targetName] if v and v in targets), None)
        if matched is not None:
            targetSymbol = matched
    else:
        if targetName in targets:
            targetSymbol = targetName
    return targetSymbol


def scroll_and_select_user(page, username, targets):
    """尝试滚动并查找用户名"""
    # 定义目标元素和滚动容器的选择器
    target_selector = CONVERSATION_ITEM_SELECTOR
    scrollable_friends_selector = CONVERSATION_LIST_SELECTOR

    # [修复] 使用模糊匹配 no-more-tip- 前缀，不再依赖精确哈希后缀
    # 同时增加文本匹配作为兜底
    # no_more_selector = 'xpath=//div[contains(@class, "no-more-tip-")]'
    # loading_selector = 'xpath=//div[contains(@class, "semi-spin")]'

    logger.debug(f"账号 {username} 开始查找目标好友列表")
    logger.debug(f"账号 {username} 目标好友列表: {targets}")

    found_targets = set()
    # [修改] 复制一份目标列表用于追踪进度
    remaining_targets = set(targets)

    # [修复] 新增：连续空滚动计数器（滚动后没有发现新好友的次数）
    empty_scroll_count = 0
    MAX_EMPTY_SCROLLS = 10  # 连续10次滚动没有新好友，认为到底了

    while True:
        # 查找所有目标元素
        target_elements = page.locator(target_selector).all()

        # [修复] 记录本轮循环前已发现的好友数，用于判断是否有新发现
        prev_found_count = len(found_targets)

        for element in target_elements:
            try:
                # 查找子元素 span，模糊匹配 class
                span = element.locator(CONVERSATION_TITLE_SELECTOR)
                targetName = span.inner_text()

                if targetName in found_targets:
                    continue  # 已处理过，跳过
                found_targets.add(targetName)

                logger.debug(f"账号 {username} 找到好友 {targetName}")

                targetSymbol = checkTargetName(targetName, targets)

                if targetSymbol:
                    element.click()

                    yield targetSymbol

                    # [修改] 标记已找到，如果全找到了直接退出
                    if targetSymbol in remaining_targets:
                        remaining_targets.remove(targetSymbol)
                    if len(remaining_targets) == 0:
                        logger.debug(f"账号 {username} 所有目标好友均已找到，停止搜索")
                        return
                    break
            except Exception as e:
                traceback.print_exc()
        else:
            # [修复] 检查本轮是否有新好友被发现
            new_found = len(found_targets) > prev_found_count
            if new_found:
                empty_scroll_count = 0  # 有新发现，重置计数器
            else:
                empty_scroll_count += 1  # 无新发现，递增计数器

            # [修复] 状态检测逻辑（多重兜底）

            # # 1. 检查是否到底（"没有更多了" —— 使用模糊类名匹配）
            # if page.locator(no_more_selector).count() > 0:
            #     logger.info(f"账号 {username} 检测到'没有更多了'标志，已到达底部")
            #     if len(remaining_targets) > 0:
            #         logger.warning(
            #             f"账号 {username} 搜索结束，仍有以下好友未找到: {remaining_targets}"
            #         )
            #     break

            # 2. [修复] 检查连续空滚动次数，防止死循环
            if empty_scroll_count >= MAX_EMPTY_SCROLLS:
                logger.warning(
                    f"账号 {username} 连续 {MAX_EMPTY_SCROLLS} 次滚动未发现新好友，判定已到达底部"
                )
                if len(remaining_targets) > 0:
                    logger.warning(
                        f"账号 {username} 搜索结束，仍有以下好友未找到: {remaining_targets}"
                    )
                break

            # 3. 检查是否正在加载
            # if page.locator(loading_selector).count() > 0:
            #     logger.debug(f"账号 {username} 列表正在加载中 (Loading)...")
            #     time.sleep(1.5)  # 给加载留点时间
            #     # 不 break，继续去滚动以触发后续内容

            # 4. 滚动容器
            scrollable_element = page.locator(
                scrollable_friends_selector
            ).element_handle()

            if scrollable_element:
                # [修复] 记录滚动前的 scrollTop，用于检测是否真的滚动了
                scroll_top_before = page.evaluate(
                    "(element) => element.scrollTop", scrollable_element
                )

                page.evaluate(
                    "(element) => element.scrollTop += 800", scrollable_element
                )

                # [修复] 检测滚动后的 scrollTop
                time.sleep(0.3)
                scroll_top_after = page.evaluate(
                    "(element) => element.scrollTop", scrollable_element
                )

                if scroll_top_before == scroll_top_after:
                    # scrollTop 没有变化，说明已经到底了
                    empty_scroll_count += 2  # 加速判定到底
                    logger.debug(
                        f"账号 {username} scrollTop 未变化 ({scroll_top_before})，可能已到底 (空滚动计数: {empty_scroll_count}/{MAX_EMPTY_SCROLLS})"
                    )
                else:
                    logger.debug(
                        f"账号 {username} 滚动好友列表以加载更多好友 (scrollTop: {scroll_top_before} -> {scroll_top_after})"
                    )

                time.sleep(1.5)
            else:
                logger.error(f"账号 {username} 未找到滚动容器，退出")
                break


def wait_for_chat_selector(page, selector, username):
    try:
        page.wait_for_selector(selector, timeout=config["browserTimeout"])
    except PlaywrightTimeoutError:
        raise RuntimeError(
            f"账号 {username} 的聊天页面元素加载超时。请先打开 https://www.douyin.com/chat "
            "确认仍已登录、完成可能出现的验证且可以手动聊天；如已掉线，请重新导出 Cookie。"
            "若手动聊天正常，可能是页面结构已变化，需要更新脚本。"
        ) from None


def do_user_task(browser, username, cookies, targets, on_result=None):
    if not targets:
        raise ValueError(f"账号 {username} 没有配置目标好友。")
    submitted_targets = set()

    def record_result(target, status, reason=None):
        if on_result is not None:
            on_result(target, status, reason)

    def record_unattempted(reason):
        for target in targets:
            if target not in submitted_targets:
                record_result(target, "not_attempted", reason)

    try:
        context = browser.new_context()  # 每个任务使用独立的上下文
    except Exception:
        record_unattempted("login_or_page_unavailable")
        raise
    try:
        try:
            context.set_default_navigation_timeout(config["browserTimeout"])
            context.set_default_timeout(config["browserTimeout"])
            page = context.new_page()
            page.on("response", handle_response)
        except Exception:
            record_unattempted("login_or_page_unavailable")
            raise

        try:
            context.add_cookies(cookies)
        except Exception:
            record_unattempted("cookie_load_failed")
            # Playwright 的原始参数错误可能包含 Cookie 内容，不将它带入日志。
            raise ValueError(
                f"账号 {username} 的 Cookie 无法载入浏览器，请重新导出完整的 Cookie JSON。"
            ) from None

        try:
            retry_operation(
                "打开抖音网页聊天页面",
                page.goto,
                retries=config["taskRetryTimes"],
                delay=5,
                url="https://www.douyin.com/chat",
            )
            time.sleep(5)
            wait_for_chat_selector(page, CONVERSATION_LIST_SELECTOR, username)
        except Exception:
            record_unattempted("login_or_page_unavailable")
            raise

        logger.debug(f"账号 {username} 开始发送消息")
        for target in scroll_and_select_user(page, username, targets):
            # 不重发已尝试的目标，避免同一轮重复提交。
            if target in submitted_targets:
                continue
            try:
                wait_for_chat_selector(page, CHAT_EDITOR_SELECTOR, username)
                chat_input = page.locator(CHAT_EDITOR_SELECTOR)
            except Exception:
                record_result(target, "failed", "editor_unavailable")
                raise
            try:
                message = build_message()
                lines = message.split("\\n")
            except Exception:
                record_result(target, "failed", "message_build_failed")
                raise
            try:
                for index, line in enumerate(lines):
                    chat_input.type(line)
                    if index < len(lines) - 1:
                        chat_input.press("Shift+Enter")
            except Exception:
                record_result(target, "failed", "message_input_failed")
                raise

            logger.debug(f"账号 {username} 准备给好友 {target} 提交消息")
            # 发送只尝试一次；失败或结果不明时停止，避免自动重复发送。
            record_result(target, "unknown", "submission_in_progress")
            try:
                chat_input.press("Enter")
            except Exception:
                record_result(target, "unknown", "submission_uncertain")
                raise
            submitted_targets.add(target)
            record_result(target, "submitted_unverified")
            logger.info(
                f"账号 {username} 已对好友 {target} 按下发送键；是否送达请在抖音聊天中确认。"
            )
            time.sleep(2)

        missing_targets = set(targets) - submitted_targets
        if missing_targets:
            for target in sorted(missing_targets):
                record_result(target, "missing", "target_not_found")
            raise RuntimeError(
                f"账号 {username} 有 {len(missing_targets)} 个目标未提交消息："
                f"{', '.join(sorted(missing_targets))}。"
                "请检查好友是否出现在网页会话列表，以及备注、昵称或抖音号是否与 targets 一致。"
                "已提交的目标不会在本轮自动重发。"
            )
    finally:
        context.close()


def runTasks(report=None):
    # 先校验全部配置，再启动浏览器，防止空任务或错误配置被报告为成功。
    userData = get_userData()
    playwright, browser = get_browser()
    try:
        # 检查是否启用多任务和任务数量
        # 创建信号量以限制并发任务数量
        logger.info("开始执行任务")
        logger.debug(f"当前配置如下：")
        logger.debug(f"消息模板: {config.get('messageTemplate', '未找到消息模板')}")
        logger.debug(f"一言类型: {config['hitokotoTypes']}")
        for user in userData:
            logger.debug(
                f"用户: {user.get('username', '未知用户')}, 目标好友: {user['targets']}"
            )

        for user in userData:
            cookies = user["cookies"]
            targets = user["targets"]
            username = user.get("username", "未知用户")
            logger.info(f"开始处理账号 {username}")
            # 创建任务
            if report is None:
                do_user_task(browser, username, cookies, targets)
            else:
                do_user_task(
                    browser,
                    username,
                    cookies,
                    targets,
                    on_result=lambda target, status, reason: report.update_target(
                        user["unique_id"], target, status, reason
                    ),
                )
            logger.info(f"账号 {username} 任务完成")
    finally:
        # 关闭浏览器实例
        browser.close()

        playwright.stop()
