import asyncio
import logging

from loguru import logger

from embykeeper.log import formatter
from embykeeper.config import config
from embykeeper.apprise import AppriseStream

debug_logger = logger.bind(scheme="debugtool")
logger = logger.bind(scheme="notifier", nonotify=True)

stream_log = None
stream_msg = None
handler_log_id = None
handler_msg_id = None
change_handle_telegram = None
change_handle_notifier = None


async def _stop_notifier():
    global stream_log, stream_msg, handler_log_id, handler_msg_id

    if handler_log_id is not None:
        logger.remove(handler_log_id)
        handler_log_id = None
    if handler_msg_id is not None:
        logger.remove(handler_msg_id)
        handler_msg_id = None

    if stream_log:
        stream_log.close()
        await stream_log.join()
        stream_log = None
    if stream_msg:
        stream_msg.close()
        await stream_msg.join()
        stream_msg = None


def _handle_config_change(*args):
    async def _async():
        global stream_log, stream_msg

        await _stop_notifier()
        if config.notifier and config.notifier.enabled:
            streams = await start_notifier()
            if streams:
                stream_log, stream_msg = streams

    logger.debug("正在刷新 Telegram 消息通知.")
    asyncio.create_task(_async())


async def start_notifier():
    """消息通知初始化函数."""
    global stream_log, stream_msg, handler_log_id, handler_msg_id, change_handle_telegram, change_handle_notifier

    def _filter_log(record):
        notify = record.get("extra", {}).get("log", None)
        nonotify = record.get("extra", {}).get("nonotify", None)
        if (not nonotify) and (notify or record["level"].no == logging.ERROR):
            return True
        else:
            return False

    def _filter_msg(record):
        notify = record.get("extra", {}).get("msg", None)
        nonotify = record.get("extra", {}).get("nonotify", None)
        if (not nonotify) and notify:
            return True
        else:
            return False

    def _formatter(record):
        return "{level}#" + formatter(record)

    notifier = config.notifier
    if not notifier or not notifier.enabled:
        if not change_handle_notifier:
            change_handle_notifier = config.on_change("notifier", _handle_config_change)
        return None

    if notifier.method == "apprise":
        if not notifier.apprise_uri:
            logger.error("Apprise URI 未配置, 无法发送消息推送.")
            return None

        logger.info("关键消息将通过 Apprise 推送.")
        stream_log = AppriseStream(uri=notifier.apprise_uri)
        handler_log_id = logger.add(
            stream_log,
            format=_formatter,
            filter=_filter_log,
        )
        stream_msg = AppriseStream(uri=notifier.apprise_uri)
        handler_msg_id = logger.add(
            stream_msg,
            format=_formatter,
            filter=_filter_msg,
        )
        if not change_handle_notifier:
            change_handle_notifier = config.on_change("notifier", _handle_config_change)
        return stream_log, stream_msg

    # Default to telegram
    accounts = config.telegram.account
    account = None
    if isinstance(notifier.account, int):
        try:
            account = accounts[notifier.account - 1]
        except IndexError:
            pass
    elif isinstance(notifier.account, str):
        for a in accounts:
            if a.phone == notifier.account:
                account = a
                break

    if account:
        from .telegram.session import ClientsSession
        from .telegram.log import TelegramStream

        async with ClientsSession([account]) as clients:
            async for a, tg in clients:
                logger.info(f'计划任务的关键消息将通过 Embykeeper Bot 发送至 "{account.phone}" 账号.')
                break
            else:
                logger.error(f'无法连接到 "{account.phone}" 账号, 无法发送日志推送.')
                return None

        stream_log = TelegramStream(
            account=account,
            instant=config.notifier.immediately,
        )
        handler_log_id = logger.add(
            stream_log,
            format=_formatter,
            filter=_filter_log,
        )
        stream_msg = TelegramStream(
            account=account,
            instant=True,
        )
        handler_msg_id = logger.add(
            stream_msg,
            format=_formatter,
            filter=_filter_msg,
        )
        if not change_handle_telegram:
            change_handle_telegram = config.on_change("telegram.account", _handle_config_change)
        if not change_handle_notifier:
            change_handle_notifier = config.on_change("notifier", _handle_config_change)
        return stream_log, stream_msg
    else:
        logger.error(f"无法找到消息推送所配置的 Telegram 账号.")
        if not change_handle_notifier:
            change_handle_notifier = config.on_change("notifier", _handle_config_change)
        return None


async def debug_notifier():
    streams = await start_notifier()
    if streams:
        logger.info("以下是发送的日志:")
        debug_logger.bind(msg=True).info("这是一条用于测试的即时消息, 使用 debug_notify 触发 😉.")
        debug_logger.bind(log=True).info("这是一条用于测试的日志消息, 使用 debug_notify 触发 😉.")
        if config.notifier.method == "apprise":
            logger.info("已尝试发送, 请至 Apprise 配置的接收端查看.")
        elif config.notifier.method == "telegram":
            logger.info("已尝试发送, 请至 @embykeeper_bot 查看.")
        await asyncio.gather(*[stream.join() for stream in streams if stream])
    else:
        logger.error("您当前没有配置有效的日志通知 (未启用日志通知或未配置账号), 请检查配置文件.")
