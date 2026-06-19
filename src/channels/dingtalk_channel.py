"""
DingTalk Channel — 钉钉长连接常驻 Channel

使用钉钉开放平台 Stream 模式 SDK (dingtalk-stream) 建立长连接，
接收机器人单聊或群聊 @ 消息，解析用户文本后调用项目内现有智能体
对话函数生成回复，再通过钉钉机器人消息接口回复到原会话。

功能特性:
- 基于 WebSocket 长连接，无需公网 IP
- 支持单聊和群聊 @ 消息接收
- 消息去重处理，避免重复响应
- 错误日志记录与异常处理
- 自动清理过期消息 ID 缓存
"""

import asyncio
import json
import logging
import os
import re
import threading
import time
from typing import Optional, Set

import dingtalk_stream
from dingtalk_stream import DingTalkStreamClient, Credential, AckMessage

from agents.agent import build_agent
from coze_coding_utils.runtime_ctx.context import new_context
from services.enterprise_analysis_runner import (
    run_enterprise_analysis_sync,
    should_use_fixed_enterprise_runner,
)

logger = logging.getLogger(__name__)
ANALYSIS_START_NOTICE = "收到，开始分析，预计约 5 分钟后出分析结果。"

# ──────────────────────────── 环境变量 ────────────────────────────

DINGTALK_CLIENT_ID = os.getenv("DINGTALK_CLIENT_ID", "")
DINGTALK_CLIENT_SECRET = os.getenv("DINGTALK_CLIENT_SECRET", "")

# 消息去重缓存配置
MAX_DEDUP_CACHE_SIZE = 10000  # 最大缓存消息 ID 数量
DEDUP_CACHE_CLEAN_INTERVAL = 300  # 清理间隔（秒）


# ──────────────────────────── 工具函数 ────────────────────────────


def _strip_at_mention(text: str) -> str:
    """去掉钉钉群聊消息中 @机器人 的内容。

    钉钉 @ 机器人时，消息内容可能包含:
    - @ 用户名（在文本中）
    - 需要根据 sender_staff_id 判断是否是机器人自己
    """
    # 钉钉 @ 格式: @用户名 或 @{用户名}
    # 去掉 @ 符号及相关用户名引用
    cleaned = re.sub(r"@\{[^}]+\}", "", text)  # @{用户名} 格式
    cleaned = re.sub(r"@[^\s]+", "", cleaned)  # @用户名 格式
    # 去掉多余空白
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    return cleaned


def _call_agent(user_text: str, conversation_id: str) -> str:
    """调用项目内智能体生成回复。

    使用 conversation_id 作为 thread_id，确保同一会话的上下文连续。
    """
    try:
        if should_use_fixed_enterprise_runner({"messages": [("user", user_text)]}):
            logger.info("DingTalk request routed to fixed enterprise runner")
            return run_enterprise_analysis_sync(user_input=str(user_text).strip())

        ctx = new_context(method="dingtalk_channel")
        agent = build_agent(ctx=ctx)

        config = {
            "configurable": {
                "thread_id": f"dingtalk_{conversation_id}",
            },
            "context": ctx,
        }

        result = agent.invoke(
            {"messages": [("user", user_text)]},
            config=config,
        )

        # 从结果中提取最后一条 AI 消息
        messages = result.get("messages", [])
        for msg in reversed(messages):
            if msg.type == "ai" and msg.content:
                return msg.content

        return "抱歉，暂时无法生成回复。"

    except Exception as e:
        logger.error(f"Agent call failed: {e}", exc_info=True)
        return f"智能体调用失败: {e}"


# ──────────────────────────── 消息处理器 ────────────────────────────


class DingTalkBotHandler(dingtalk_stream.ChatbotHandler):
    """钉钉机器人消息处理器，处理单聊和群聊 @ 消息。"""

    def __init__(
        self,
        dedup_cache: Optional[Set[str]] = None,
        dedup_lock: Optional[threading.Lock] = None
    ):
        super().__init__()
        # 消息去重缓存（注意：dedup_cache 可能是传入的空 set，需要显式判断 None）
        self._processed_msg_ids: Set[str] = dedup_cache if dedup_cache is not None else set()
        self._dedup_lock = dedup_lock if dedup_lock is not None else threading.Lock()
        self._last_clean_time = time.time()

    def _is_duplicate(self, msg_id: str) -> bool:
        """检查消息是否已处理（去重）。"""
        with self._dedup_lock:
            if msg_id in self._processed_msg_ids:
                return True
            self._processed_msg_ids.add(msg_id)

            # 当缓存超过最大限制时，立即清理（不依赖时间间隔）
            if len(self._processed_msg_ids) > MAX_DEDUP_CACHE_SIZE:
                # 保留最近的一半消息 ID（原地修改，保持共享引用）
                keep_count = MAX_DEDUP_CACHE_SIZE // 2
                items_to_keep = list(self._processed_msg_ids)[-keep_count:]
                self._processed_msg_ids.clear()
                self._processed_msg_ids.update(items_to_keep)
                logger.info(
                    f"Cleaned dedup cache (size exceeded), kept {keep_count} recent msg IDs"
                )
                self._last_clean_time = time.time()

            return False

    async def process(self, callback: dingtalk_stream.CallbackMessage):
        """处理接收到的机器人消息回调。"""
        try:
            # 解析消息
            incoming_message = dingtalk_stream.ChatbotMessage.from_dict(callback.data)

            msg_id = incoming_message.msg_id
            conversation_id = incoming_message.conversation_id or incoming_message.chat_id

            # 去重检查
            if self._is_duplicate(msg_id):
                logger.debug(f"Duplicate message ignored: {msg_id}")
                return AckMessage.STATUS_OK, "OK"

            # 获取消息内容
            msg_type = incoming_message.msg_type
            sender_staff_id = incoming_message.sender_staff_id
            sender_nick = incoming_message.sender_nick

            logger.info(
                f"Received DingTalk message: msg_id={msg_id}, "
                f"conversation_id={conversation_id}, msg_type={msg_type}, "
                f"sender={sender_nick}({sender_staff_id})"
            )

            # 目前仅处理文本消息
            if msg_type != "text":
                logger.info(f"Skipping non-text message type: {msg_type}")
                self.reply_markdown_card(
                    title="提示",
                    content="目前仅支持文本消息，请发送文字进行对话。",
                    incoming_message=incoming_message
                )
                return AckMessage.STATUS_OK, "OK"

            # 提取文本内容
            text_content = incoming_message.text.content
            if not text_content:
                logger.info("Empty text content, skipping.")
                return AckMessage.STATUS_OK, "OK"

            # 去掉 @ 机器人标记
            user_text = _strip_at_mention(text_content)
            if not user_text:
                logger.info("Empty text after stripping at-mention, skipping.")
                return AckMessage.STATUS_OK, "OK"

            logger.info(f"User text: {user_text[:200]}")

            # 在后台线程中调用智能体（避免阻塞长连接）
            # 钉钉要求 3 秒内响应，但智能体调用可能较慢，所以异步处理
            thread = threading.Thread(
                target=self._process_and_reply,
                args=(user_text, conversation_id, incoming_message),
                daemon=True,
            )
            thread.start()

            # 立即返回 ACK，表示已接收消息
            return AckMessage.STATUS_OK, "OK"

        except Exception as e:
            logger.error(f"Error processing DingTalk message: {e}", exc_info=True)
            return AckMessage.STATUS_OK, "OK"

    def _process_and_reply(
        self,
        user_text: str,
        conversation_id: str,
        incoming_message: dingtalk_stream.ChatbotMessage,
    ) -> None:
        """在后台线程中调用智能体并发送回复。

        注意：使用 reply_markdown_card 方法而非 reply_text，
        因为 reply_text 不支持私聊（私聊消息没有 session_webhook）。
        """
        try:
            self.reply_markdown_card(
                title="企业分析助手",
                content=ANALYSIS_START_NOTICE,
                incoming_message=incoming_message
            )

            reply_text = _call_agent(user_text, conversation_id)

            # 截断过长消息（钉钉单条消息上限约 20000 字符）
            if len(reply_text) > 19990:
                reply_text = reply_text[:19990] + "\n...(内容过长已截断)"

            # 使用 reply_markdown_card 发送回复（支持私聊和群聊）
            # reply_text 方法不支持私聊，私聊消息没有 session_webhook
            self.reply_markdown_card(
                title="企业分析助手",
                content=reply_text,
                incoming_message=incoming_message
            )

            logger.info(
                f"Reply sent for conversation_id={conversation_id}"
            )

        except Exception as e:
            logger.error(f"Failed to process and reply: {e}", exc_info=True)
            try:
                # 错误消息也用 markdown card 发送
                self.reply_markdown_card(
                    title="处理失败",
                    content=f"处理失败: {e}",
                    incoming_message=incoming_message
                )
            except Exception as send_err:
                logger.error(f"Failed to send error reply: {send_err}", exc_info=True)


# ──────────────────────────── 钉钉 Channel ────────────────────────────


class DingTalkChannel:
    """钉钉长连接 Channel，常驻接收消息并智能回复。"""

    def __init__(
        self,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
    ):
        self.client_id = client_id or DINGTALK_CLIENT_ID
        self.client_secret = client_secret or DINGTALK_CLIENT_SECRET

        if not self.client_id or not self.client_secret:
            raise ValueError(
                "DINGTALK_CLIENT_ID 和 DINGTALK_CLIENT_SECRET 不能为空，"
                "请通过环境变量或构造参数传入。"
            )

        # 消息去重缓存（共享给 Handler）
        self._dedup_cache: Set[str] = set()

        # 创建凭证和客户端
        self.credential = Credential(self.client_id, self.client_secret)
        self.client = DingTalkStreamClient(self.credential)

        # 注册消息处理器
        self.handler = DingTalkBotHandler(dedup_cache=self._dedup_cache)
        self.client.register_callback_handler(
            dingtalk_stream.ChatbotMessage.TOPIC,
            self.handler
        )

        logger.info("DingTalk Channel initialized successfully")

    # 重连配置
    RECONNECT_DELAY = 10  # 重连等待秒数
    MAX_RECONNECT_ATTEMPTS = 100  # 最大重连次数（足够大，实际不会限制）

    def start(self) -> None:
        """启动钉钉长连接，阻塞当前线程。

        dingtalk-stream SDK 的 start_forever() 会自动管理连接和重连，
        但在极端情况下可能退出，因此添加外层重连循环确保服务持续运行。
        """
        logger.info("Starting DingTalk Channel (Stream Mode)...")
        logger.info(
            f"CLIENT_ID: {self.client_id[:8]}***"
            if self.client_id else "CLIENT_ID not set"
        )

        reconnect_attempts = 0
        while True:
            try:
                logger.info("Establishing WebSocket connection...")
                # start_forever() 会阻塞直到连接关闭或出错
                # SDK 内部自动处理重连和心跳
                self.client.start_forever()
                # 如果正常退出（如被停止），则不再重连
                logger.info("WebSocket connection closed normally.")
                break

            except KeyboardInterrupt:
                logger.info("DingTalk Channel stopped by user.")
                break

            except Exception as e:
                reconnect_attempts += 1
                if reconnect_attempts >= self.MAX_RECONNECT_ATTEMPTS:
                    logger.error(
                        f"DingTalk Channel exceeded max reconnect attempts ({self.MAX_RECONNECT_ATTEMPTS}), giving up."
                    )
                    raise

                logger.warning(
                    f"DingTalk Channel connection lost (attempt {reconnect_attempts}/{self.MAX_RECONNECT_ATTEMPTS}): {e}"
                )
                logger.info(f"Reconnecting in {self.RECONNECT_DELAY} seconds...")
                time.sleep(self.RECONNECT_DELAY)

                # 重新创建客户端和注册处理器
                try:
                    self.client = DingTalkStreamClient(self.credential)
                    self.client.register_callback_handler(
                        dingtalk_stream.ChatbotMessage.TOPIC,
                        self.handler
                    )
                    logger.info("Recreated DingTalkStreamClient for reconnect.")
                except Exception as recreate_error:
                    logger.error(f"Failed to recreate client: {recreate_error}")
                    # 继续尝试下一次重连
                    continue

    async def start_async(self) -> None:
        """异步启动钉钉长连接（适用于需要异步控制的场景）。"""
        logger.info("Starting DingTalk Channel (Async Stream Mode)...")
        reconnect_attempts = 0
        while True:
            try:
                logger.info("Establishing WebSocket connection (async)...")
                await self.client.start()
                logger.info("WebSocket connection closed normally (async).")
                break

            except asyncio.CancelledError:
                logger.info("DingTalk Channel async start cancelled.")
                break

            except Exception as e:
                reconnect_attempts += 1
                if reconnect_attempts >= self.MAX_RECONNECT_ATTEMPTS:
                    logger.error(
                        f"DingTalk Channel async exceeded max reconnect attempts ({self.MAX_RECONNECT_ATTEMPTS}), giving up."
                    )
                    raise

                logger.warning(
                    f"DingTalk Channel async connection lost (attempt {reconnect_attempts}/{self.MAX_RECONNECT_ATTEMPTS}): {e}"
                )
                logger.info(f"Reconnecting in {self.RECONNECT_DELAY} seconds...")
                await asyncio.sleep(self.RECONNECT_DELAY)

                # 重新创建客户端
                try:
                    self.client = DingTalkStreamClient(self.credential)
                    self.client.register_callback_handler(
                        dingtalk_stream.ChatbotMessage.TOPIC,
                        self.handler
                    )
                    logger.info("Recreated DingTalkStreamClient for async reconnect.")
                except Exception as recreate_error:
                    logger.error(f"Failed to recreate client: {recreate_error}")
                    continue


# ──────────────────────────── 入口 ────────────────────────────


def main() -> None:
    """启动 DingTalk Channel 的入口函数。"""
    import dotenv

    # 尝试加载 .env 文件
    dotenv.load_dotenv()

    # 确保日志目录存在
    log_dir = "/app/work/logs/bypass"
    os.makedirs(log_dir, exist_ok=True)

    # 配置日志 - 写入线上日志系统监控的路径
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[
            logging.StreamHandler(),  # 输出到 stdout（会被 main.py 捕获）
            logging.FileHandler(
                os.path.join(log_dir, "dingtalk_channel.log"),
                encoding="utf-8"
            ),
        ]
    )

    # 设置 dingtalk_stream SDK 的日志级别
    logging.getLogger("dingtalk_stream").setLevel(logging.DEBUG)

    channel = DingTalkChannel()
    channel.start()


if __name__ == "__main__":
    main()
