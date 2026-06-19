"""
Feishu Channel — 飞书长连接常驻 Channel

使用 lark-oapi Python SDK 的 WSClient（lark.ws.Client）建立飞书长连接，
订阅 im.message.receive_v1 消息事件，解析用户文本并去掉群聊 @机器人，
调用项目内现有智能体对话函数生成回复，再用 client.im.v1.message.create
以机器人身份把文本回复发送回原 chat_id。
"""

import asyncio
import io
import json
import logging
import os
import re
import tempfile
import threading
import time
from typing import Optional
from urllib.parse import urlparse

import lark_oapi as lark
from lark_oapi.api.im.v1 import *

from agents.agent import build_agent
from coze_coding_utils.runtime_ctx.context import new_context
from services.enterprise_analysis_runner import (
    run_enterprise_analysis_sync,
    should_use_fixed_enterprise_runner,
)

logger = logging.getLogger(__name__)
ANALYSIS_START_NOTICE = "收到，开始分析，预计约 5 分钟后出分析结果。"

# ──────────────────────────── 环境变量 ────────────────────────────

FEISHU_APP_ID = os.getenv("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "")

# ──────────────────────────── 工具函数 ────────────────────────────


def _strip_at_mention(text: str) -> str:
    """去掉飞书群聊消息中 @机器人 的 at 标签。

    飞书 at 标签格式: <at user_id="ou_xxx">@用户名</at>
    同时去掉多余空白和首尾空白。
    """
    cleaned = re.sub(r"<at\s+user_id=\"[^\"]*\">@[^<]*</at>", "", text)
    # 去掉因 at 标签移除产生的多余空白
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    return cleaned


def _extract_text_from_content(content: str) -> str:
    """从飞书消息 content JSON 中提取纯文本。

    content 示例:
    - 文本消息: {"text":"hello"}
    - 含 at:   {"text":"<at user_id=\"ou_xxx\">@Bot</at> hello"}
    """
    try:
        data = json.loads(content)
        raw_text = data.get("text", "")
        return _strip_at_mention(raw_text)
    except (json.JSONDecodeError, AttributeError):
        return content


def _call_agent(user_text: str, chat_id: str) -> str:
    """调用项目内智能体生成回复。

    使用 chat_id 作为 thread_id，确保同一会话的上下文连续。
    """
    try:
        if should_use_fixed_enterprise_runner({"messages": [("user", user_text)]}):
            logger.info("Feishu request routed to fixed enterprise runner")
            return run_enterprise_analysis_sync(user_input=str(user_text).strip())

        ctx = new_context(method="feishu_channel")
        agent = build_agent(ctx=ctx)

        config = {
            "configurable": {
                "thread_id": f"feishu_{chat_id}",
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


# ──────────────────────────── 飞书 Client ────────────────────────────


class FeishuChannel:
    """飞书长连接 Channel，常驻接收消息并智能回复。"""

    def __init__(
        self,
        app_id: Optional[str] = None,
        app_secret: Optional[str] = None,
    ):
        self.app_id = app_id or FEISHU_APP_ID
        self.app_secret = app_secret or FEISHU_APP_SECRET

        if not self.app_id or not self.app_secret:
            raise ValueError(
                "FEISHU_APP_ID 和 FEISHU_APP_SECRET 不能为空，"
                "请通过环境变量或构造参数传入。"
            )

        # 用于发送消息的 API Client
        self.api_client = (
            lark.Client.builder()
            .app_id(self.app_id)
            .app_secret(self.app_secret)
            .build()
        )

        # 事件处理器
        self.event_handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._on_message_receive)
            .build()
        )

        # WebSocket 长连接 Client
        self.ws_client = lark.ws.Client(
            self.app_id,
            self.app_secret,
            event_handler=self.event_handler,
            log_level=lark.LogLevel.INFO,
        )

        # 消息处理线程池（防止阻塞长连接）
        self._executor = None

        # 记录已处理的消息 ID，避免重复处理
        self._processed_msg_ids: set = set()
        self._processed_lock = threading.Lock()

    # ──────────── 事件处理 ────────────

    def _on_message_receive(self, data: lark.im.v1.P2ImMessageReceiveV1) -> None:
        """处理 im.message.receive_v1 事件。"""
        try:
            event = data.event
            if not event:
                logger.warning("Received event with empty event body, skipping.")
                return

            message = event.message
            sender = event.sender

            if not message:
                logger.warning("Received event with empty message, skipping.")
                return

            msg_id = message.message_id
            chat_id = message.chat_id
            msg_type = message.message_type
            content = message.content

            # 忽略自己发送的消息
            if sender and sender.sender_type == "app":
                logger.debug(f"Ignoring self-sent message: {msg_id}")
                return

            # 去重
            with self._processed_lock:
                if msg_id in self._processed_msg_ids:
                    logger.debug(f"Duplicate message ignored: {msg_id}")
                    return
                self._processed_msg_ids.add(msg_id)
                # 防止集合无限增长
                if len(self._processed_msg_ids) > 10000:
                    self._processed_msg_ids = set(list(self._processed_msg_ids)[-5000:])

            logger.info(
                f"Received message: msg_id={msg_id}, chat_id={chat_id}, "
                f"type={msg_type}, sender_id={sender.sender_id if sender else 'unknown'}"
            )

            # 目前仅处理文本消息
            if msg_type != "text":
                logger.info(f"Skipping non-text message type: {msg_type}")
                self._send_text_message(
                    chat_id, "目前仅支持文本消息，请发送文字进行对话。"
                )
                return

            # 提取纯文本（去掉 @机器人）
            user_text = _extract_text_from_content(content)
            if not user_text:
                logger.info("Empty text after stripping at-mention, skipping.")
                return

            logger.info(f"User text: {user_text[:200]}")

            # 在后台线程中调用智能体，避免阻塞长连接事件处理（3 秒超时限制）
            thread = threading.Thread(
                target=self._process_and_reply,
                args=(user_text, chat_id, msg_id),
                daemon=True,
            )
            thread.start()

        except Exception as e:
            logger.error(f"Error processing message event: {e}", exc_info=True)

    # ──────────── PDF 处理 ────────────

    @staticmethod
    def _extract_pdf_urls(text: str) -> list[str]:
        """从回复文本中提取所有 PDF URL。

        匹配模式:
        - 📄 [企业名称 分析报告](https://xxx.pdf)
        - 📄 **PDF报告链接：**https://xxx.pdf
        - 普通包含 .pdf 的 URL
        """
        # 匹配常见 PDF URL 模式
        patterns = [
            r'\[[^\]]*分析报告[^\]]*\]\((https?://[^\s\)]+\.pdf[^\s\)]*)\)',
            r'(?:PDF报告链接[：:]\s*)(https?://[^\s\)]+\.pdf[^\s\)]*)',
            r'(https?://[^\s\)"\']+\.(?:pdf|PDF)(?:\?[^\s\)"\']*)?)',
        ]
        urls = []
        for pattern in patterns:
            for match in re.finditer(pattern, text):
                url = match.group(1).rstrip(".,;，。；)")
                if url not in urls:
                    urls.append(url)
        return urls

    @staticmethod
    def _download_file(url: str, timeout: int = 60) -> tuple[io.BytesIO, str]:
        """下载文件到内存，返回 (file_stream, filename)。"""
        import urllib.request

        parsed = urlparse(url)
        # 从 URL 路径提取文件名
        path = parsed.path or "report.pdf"
        filename = os.path.basename(path)
        if not filename.lower().endswith(".pdf"):
            filename += ".pdf"

        req = urllib.request.Request(url, headers={"User-Agent": "FeishuChannel/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()

        logger.info(f"Downloaded file: {filename}, size={len(data)} bytes")
        return io.BytesIO(data), filename

    @staticmethod
    def _extract_pdf_report_name(text: str) -> str:
        """从 Agent 回复中提取中文 PDF 报告名称，用于渠道附件名展示。"""
        link_match = re.search(r'\[([^\]]*分析报告[^\]]*)\]\(https?://[^\s\)]+\.pdf[^\s\)]*\)', text)
        if link_match:
            name = link_match.group(1).strip()
            if not name.lower().endswith(".pdf"):
                name += ".pdf"
            return name

        match = re.search(r"PDF报告名称[：:]\s*\*\*?([^\n*]+)\*?", text)
        if not match:
            match = re.search(r"PDF报告名称[：:]\s*([^\n]+)", text)
        if not match:
            return ""
        name = match.group(1).strip().strip("*")
        if not name:
            return ""
        if not name.lower().endswith(".pdf"):
            name += ".pdf"
        return name

    def _send_file_message(self, chat_id: str, file_stream: io.BytesIO, filename: str) -> bool:
        """上传文件到飞书并发送文件消息。

        流程: 上传文件(im.v1.file.create) → 获取 file_key → 发送文件消息
        飞书要求 file_type: stream（普通文件）、pdf（PDF 文件）
        """
        try:
            # 上传文件
            file_stream.seek(0)
            create_file_req = (
                CreateFileRequest.builder()
                .request_body(
                    CreateFileRequestBody.builder()
                    .file_type("stream")
                    .file_name(filename)
                    .file(file_stream)
                    .build()
                )
                .build()
            )

            create_file_resp = self.api_client.im.v1.file.create(create_file_req)

            if not create_file_resp.success():
                logger.error(
                    f"Upload file failed: code={create_file_resp.code}, "
                    f"msg={create_file_resp.msg}, log_id={create_file_resp.get_log_id()}"
                )
                return False

            file_key = create_file_resp.data.file_key
            logger.info(f"File uploaded successfully: file_key={file_key}")

            # 发送文件消息
            request = (
                CreateMessageRequest.builder()
                .receive_id_type("chat_id")
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(chat_id)
                    .msg_type("file")
                    .content(json.dumps({"file_key": file_key}))
                    .build()
                )
                .build()
            )

            response: CreateMessageResponse = self.api_client.im.v1.message.create(request)

            if not response.success():
                logger.error(
                    f"Send file message failed: code={response.code}, "
                    f"msg={response.msg}, log_id={response.get_log_id()}"
                )
                return False

            logger.info(f"File message sent successfully to chat_id={chat_id}")
            return True

        except Exception as e:
            logger.error(f"Send file message exception: {e}", exc_info=True)
            return False

    def _process_and_reply(self, user_text: str, chat_id: str, msg_id: str) -> None:
        """在后台线程中调用智能体并发送回复（文本 + PDF 文件）。"""
        try:
            self._send_text_message(chat_id, ANALYSIS_START_NOTICE)
            reply_text = _call_agent(user_text, chat_id)

            # 发送文本回复
            self._send_text_message(chat_id, reply_text)

            # 检测并下载 PDF，发送文件消息
            pdf_urls = self._extract_pdf_urls(reply_text)
            preferred_pdf_name = self._extract_pdf_report_name(reply_text)
            for pdf_url in pdf_urls:
                try:
                    file_stream, filename = self._download_file(pdf_url)
                    if preferred_pdf_name:
                        filename = preferred_pdf_name
                    self._send_file_message(chat_id, file_stream, filename)
                except Exception as e:
                    logger.error(f"Failed to send PDF file ({pdf_url}): {e}", exc_info=True)
                    # PDF 文件发送失败不影响主流程，链接已在文本中

            logger.info(f"Reply sent for msg_id={msg_id} (text + {len(pdf_urls)} PDF(s))")
        except Exception as e:
            logger.error(f"Failed to process and reply: {e}", exc_info=True)
            self._send_text_message(chat_id, f"处理失败: {e}")

    # ──────────── 消息发送 ────────────

    def _send_text_message(self, chat_id: str, text: str) -> bool:
        """通过飞书 API 发送文本消息到指定 chat_id。"""
        try:
            # 截断过长消息（飞书单条消息上限约 4000 字符）
            if len(text) > 4000:
                text = text[:3990] + "\n...(内容过长已截断)"

            request = (
                CreateMessageRequest.builder()
                .receive_id_type("chat_id")
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(chat_id)
                    .msg_type("text")
                    .content(json.dumps({"text": text}))
                    .build()
                )
                .build()
            )

            response: CreateMessageResponse = self.api_client.im.v1.message.create(
                request
            )

            if not response.success():
                logger.error(
                    f"Send message failed: code={response.code}, "
                    f"msg={response.msg}, log_id={response.get_log_id()}"
                )
                return False

            logger.debug(f"Message sent successfully to chat_id={chat_id}")
            return True

        except Exception as e:
            logger.error(f"Send message exception: {e}", exc_info=True)
            return False

    # ──────────── 启动 ────────────

    # 重连配置
    RECONNECT_DELAY = 10  # 重连等待秒数
    MAX_RECONNECT_ATTEMPTS = 100  # 最大重连次数

    def start(self) -> None:
        """启动飞书长连接，阻塞当前线程。

        lark SDK 的 ws_client.start() 内部使用 run_until_complete 管理 event loop，
        因此必须在完全没有 asyncio loop 的线程中运行。

        添加外层重连循环，确保连接断开后自动重连。
        """
        logger.info("Starting Feishu Channel (WSClient long connection)...")
        logger.info(f"APP_ID: {self.app_id[:8]}***" if self.app_id else "APP_ID not set")

        reconnect_attempts = 0
        while True:
            try:
                logger.info("Establishing WebSocket connection...")
                self.ws_client.start()
                # 如果正常退出，不再重连
                logger.info("WebSocket connection closed normally.")
                break

            except KeyboardInterrupt:
                logger.info("Feishu Channel stopped by user.")
                break

            except Exception as e:
                reconnect_attempts += 1
                if reconnect_attempts >= self.MAX_RECONNECT_ATTEMPTS:
                    logger.error(
                        f"Feishu Channel exceeded max reconnect attempts ({self.MAX_RECONNECT_ATTEMPTS}), giving up."
                    )
                    raise

                logger.warning(
                    f"Feishu Channel connection lost (attempt {reconnect_attempts}/{self.MAX_RECONNECT_ATTEMPTS}): {e}"
                )
                logger.info(f"Reconnecting in {self.RECONNECT_DELAY} seconds...")
                time.sleep(self.RECONNECT_DELAY)

                # 重新创建 WebSocket Client
                try:
                    self.ws_client = lark.ws.Client(
                        self.app_id,
                        self.app_secret,
                        event_handler=self.event_handler,
                        log_level=lark.LogLevel.INFO,
                    )
                    logger.info("Recreated ws_client for reconnect.")
                except Exception as recreate_error:
                    logger.error(f"Failed to recreate ws_client: {recreate_error}")
                    continue


# ──────────────────────────── 入口 ────────────────────────────


def main() -> None:
    """启动 Feishu Channel 的入口函数。"""
    import dotenv

    # 尝试加载 .env 文件
    dotenv.load_dotenv()

    channel = FeishuChannel()
    channel.start()


if __name__ == "__main__":
    main()
