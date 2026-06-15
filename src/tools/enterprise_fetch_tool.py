"""企业信息页面抓取工具 - 通过 fetch-url 抓取企查查/天眼查等页面完整内容（免费渠道）"""

from langchain.tools import tool
from coze_coding_dev_sdk.fetch import FetchClient
from coze_coding_utils.runtime_ctx.context import new_context
from coze_coding_utils.log.write_log import request_context


def _do_fetch_page(url: str) -> str:
    """执行页面抓取的公共逻辑"""
    ctx = request_context.get() or new_context(method="enterprise_fetch")
    client = FetchClient(ctx=ctx)

    response = client.fetch(url=url)

    if response.status_code != 0:
        return f"抓取页面失败，状态码: {response.status_code}，原因: {response.status_message}"

    text_parts = []
    for item in response.content:
        if item.type == "text":
            text_parts.append(item.text)

    if not text_parts:
        return f"页面「{response.title}」未提取到文本内容。"

    full_text = "\n".join(text_parts)
    # 限制长度避免返回过多内容
    if len(full_text) > 8000:
        full_text = full_text[:8000] + "\n...(内容已截断)"

    return f"页面标题: {response.title}\nURL: {response.url}\n\n{full_text}"


@tool
def fetch_enterprise_page(url: str) -> str:
    """抓取指定URL页面的完整文本内容。当搜索结果中发现企查查、天眼查、爱企查等包含企业详细信息的页面URL时，使用此工具抓取页面完整内容以获取更丰富的数据。参数url为要抓取的页面完整URL。"""
    return _do_fetch_page(url)
