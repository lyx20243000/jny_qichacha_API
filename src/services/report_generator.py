"""报告生成服务 - 生成 Markdown 内容并通过 Coze 文档服务输出 PDF 报告"""

import logging
import math
import os
import uuid
from datetime import datetime
from html import escape
from coze_coding_dev_sdk import DocumentGenerationClient
from storage.s3.s3_storage import S3SyncStorage

logger = logging.getLogger(__name__)

# 数据可信度标记映射
RELIABILITY_MAP = {
    "green": "\U0001f7e2",  # 🟢
    "yellow": "\U0001f7e1",  # 🟡
    "red": "\U0001f534",    # 🔴
}

# 维度中文名
DIMENSION_NAMES = {
    "industry": "行业",
    "operation": "企业经营",
    "finance": "财务",
    "credit": "信用",
}

REPORT_PRIMARY_BLUE = "#1664d9"
REPORT_DARK_BLUE = "#123b75"
REPORT_LIGHT_BLUE = "#eaf4ff"
REPORT_LINE_BLUE = "#b9d7ff"
REPORT_TEXT_GRAY = "#5f6b7a"
INVALID_REPORT_TITLE_CHARS = set('/\\:*?"<>|\r\n\t')


def _build_pdf_report_title(enterprise_name: str) -> str:
    """Build a readable PDF title while avoiding common filename-invalid characters."""
    clean_name = "".join(
        " " if ch in INVALID_REPORT_TITLE_CHARS else ch
        for ch in str(enterprise_name or "").strip()
    )
    clean_name = " ".join(clean_name.split())
    clean_name = clean_name[:80].strip() or "企业"
    return f"{clean_name} 分析报告"


def _svg_text(value, max_length: int = 80) -> str:
    """转义 SVG 文本，避免企业名称中的特殊字符破坏图形。"""
    text = str(value or "").strip()
    if max_length and len(text) > max_length:
        text = text[: max_length - 1] + "…"
    return escape(text, quote=True)


def _html_text(value, max_length: int = 0) -> str:
    """转义 HTML 文本。"""
    text = str(value or "").strip()
    if max_length and len(text) > max_length:
        text = text[: max_length - 1] + "…"
    return escape(text, quote=True)


def _resolve_enterprise_identity(enterprise_name: str, scoring_result: dict) -> tuple[str, str]:
    """从评分结果中解析企业名称和统一社会信用代码。"""
    profile = scoring_result.get("enterprise_profile") or {}
    if isinstance(profile, str) or not isinstance(profile, dict):
        profile = {}

    display_name = (
        profile.get("企业名称")
        or profile.get("company_name")
        or enterprise_name
        or "未获取"
    )
    social_credit_code = (
        profile.get("统一社会信用代码")
        or profile.get("社会信用代码")
        or profile.get("credit_code")
        or profile.get("social_credit_code")
        or "未获取"
    )
    return str(display_name), str(social_credit_code)


def _generate_report_cover_svg(
    enterprise_name: str,
    social_credit_code: str,
    total_score,
    final_grade: str,
    assessment_goal: str,
) -> str:
    """生成蓝白风格报告头图，模拟正式 KYB 报告首页视觉。"""
    generated_at = datetime.now().strftime("%Y-%m-%d")
    name = _svg_text(enterprise_name, 42)
    credit_code = _svg_text(social_credit_code, 30)
    goal = _svg_text(assessment_goal, 28)
    grade = _svg_text(final_grade, 6)
    score = _svg_text(total_score, 8)

    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 720 220" width="720" height="220">
  <rect x="0" y="0" width="720" height="220" rx="0" fill="#ffffff"/>
  <rect x="0" y="0" width="720" height="14" fill="{REPORT_PRIMARY_BLUE}"/>
  <rect x="0" y="14" width="720" height="72" fill="{REPORT_LIGHT_BLUE}"/>
  <rect x="34" y="36" width="4" height="30" fill="{REPORT_PRIMARY_BLUE}"/>
  <text x="48" y="51" font-size="20" font-weight="700" fill="{REPORT_DARK_BLUE}">企业综合评估报告</text>
  <text x="48" y="72" font-size="11" fill="{REPORT_TEXT_GRAY}">Enterprise KYB Assessment Report</text>
  <rect x="568" y="32" width="118" height="28" rx="14" fill="{REPORT_PRIMARY_BLUE}"/>
  <text x="627" y="51" text-anchor="middle" font-size="12" font-weight="700" fill="#ffffff">内部评估版</text>
  <text x="34" y="122" font-size="22" font-weight="700" fill="#18283d">{name}</text>
  <text x="34" y="149" font-size="12" fill="{REPORT_TEXT_GRAY}">统一社会信用代码：{credit_code}</text>
  <rect x="34" y="174" width="154" height="30" fill="#f7fbff" stroke="{REPORT_LINE_BLUE}"/>
  <rect x="188" y="174" width="112" height="30" fill="#f7fbff" stroke="{REPORT_LINE_BLUE}"/>
  <rect x="300" y="174" width="236" height="30" fill="#f7fbff" stroke="{REPORT_LINE_BLUE}"/>
  <rect x="536" y="174" width="150" height="30" fill="#f7fbff" stroke="{REPORT_LINE_BLUE}"/>
  <text x="48" y="193" font-size="12" fill="{REPORT_TEXT_GRAY}">总分</text>
  <text x="92" y="194" font-size="15" font-weight="700" fill="{REPORT_PRIMARY_BLUE}">{score}</text>
  <text x="204" y="193" font-size="12" fill="{REPORT_TEXT_GRAY}">评级</text>
  <text x="246" y="194" font-size="15" font-weight="700" fill="{REPORT_PRIMARY_BLUE}">{grade}</text>
  <text x="316" y="193" font-size="12" fill="{REPORT_TEXT_GRAY}">评估目标：{goal}</text>
  <text x="552" y="193" font-size="12" fill="{REPORT_TEXT_GRAY}">生成日期：{generated_at}</text>
</svg>"""


def _append_section_heading(lines: list[str], title: str) -> None:
    """追加 PDF 友好的大标题。"""
    lines.append(f"## {title}")
    lines.append("")


def _generate_radar_chart_svg(
    dimension_details: dict, dimension_names: dict
) -> str:
    """生成四维雷达图 SVG

    Args:
        dimension_details: 各维度评分详情
        dimension_names: 维度中文名映射

    Returns:
        SVG 字符串
    """
    dims = []
    for key in ["industry", "operation", "finance", "credit"]:
        if key in dimension_details:
            ratio = dimension_details[key].get("score_ratio", 0)
            name = dimension_names.get(key, key)
            dims.append({"name": name, "ratio": ratio})

    if not dims:
        return ""

    n = len(dims)
    cx, cy = 150, 150
    r = 120

    # 计算各轴角度（从顶部开始，顺时针）
    angles = [2 * math.pi * i / n - math.pi / 2 for i in range(n)]

    # 生成网格线（5层）
    grid_paths = []
    for level in range(1, 6):
        points = []
        lr = r * level / 5
        for angle in angles:
            x = cx + lr * math.cos(angle)
            y = cy + lr * math.sin(angle)
            points.append(f"{x:.1f},{y:.1f}")
        grid_paths.append(f'<polygon points="{" ".join(points)}" fill="none" stroke="#e0e0e0" stroke-width="0.5"/>')

    # 生成数据多边形
    data_points = []
    for i, dim in enumerate(dims):
        x = cx + r * dim["ratio"] * math.cos(angles[i])
        y = cy + r * dim["ratio"] * math.sin(angles[i])
        data_points.append(f"{x:.1f},{y:.1f}")

    data_polygon = f'<polygon points="{" ".join(data_points)}" fill="rgba(66,133,244,0.3)" stroke="#4285F4" stroke-width="2"/>'

    # 生成轴线和标签
    axes_and_labels = []
    for i, dim in enumerate(dims):
        ex = cx + r * math.cos(angles[i])
        ey = cy + r * math.sin(angles[i])
        axes_and_labels.append(
            f'<line x1="{cx}" y1="{cy}" x2="{ex:.1f}" y2="{ey:.1f}" stroke="#bdbdbd" stroke-width="0.5"/>'
        )
        # 标签位置偏移
        lx = cx + (r + 20) * math.cos(angles[i])
        ly = cy + (r + 20) * math.sin(angles[i])
        score_pct = f'{dim["ratio"]*100:.0f}%'
        axes_and_labels.append(
            f'<text x="{lx:.1f}" y="{ly:.1f}" text-anchor="middle" dominant-baseline="middle" font-size="12" fill="#333">{dim["name"]} {score_pct}</text>'
        )

    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 300" width="300" height="300">
    {"".join(grid_paths)}
    {data_polygon}
    {"".join(axes_and_labels)}
    </svg>"""

    return svg


def _append_list_section(lines: list[str], title: str, values: list | str | None) -> None:
    """向报告追加简短列表段落。"""
    if not values:
        return

    if isinstance(values, str):
        values = [values]

    _append_section_heading(lines, title)
    for value in values:
        if value:
            lines.append(f"- {value}")
    lines.append("")


def _format_triggered_risk(rule: dict) -> str:
    """将触发的风险项转成客户可读的重点关注风险。"""
    description = rule.get("description") or rule.get("field") or "风险项"
    value = rule.get("value")
    if value not in (None, ""):
        return f"{description}：当前值为 {value}，建议结合权威数据源进行人工复核。"
    return f"{description}：建议结合权威数据源进行人工复核。"


def _merge_triggered_risks(scoring_result: dict, evaluation_result: dict) -> None:
    """把风险触发项合并到 action_recommendation.key_risks。"""
    triggered = evaluation_result.get("triggered_red_lines") or []
    if not triggered:
        return

    recommendation = scoring_result.setdefault("action_recommendation", {})
    if isinstance(recommendation, str):
        recommendation = {"cooperation_advice": recommendation}
        scoring_result["action_recommendation"] = recommendation
    if not isinstance(recommendation, dict):
        return

    existing = recommendation.get("key_risks") or []
    if isinstance(existing, str):
        existing = [existing]

    seen = {str(item) for item in existing if item}
    for rule in triggered:
        text = _format_triggered_risk(rule)
        if text not in seen:
            existing.append(text)
            seen.add(text)
    recommendation["key_risks"] = existing


def _append_action_recommendation(lines: list[str], scoring_result: dict) -> None:
    """追加面向业务决策的行动建议。"""
    recommendation = scoring_result.get("action_recommendation")
    if not recommendation:
        return

    _append_section_heading(lines, "行动建议")

    if isinstance(recommendation, str):
        lines.append(recommendation)
        lines.append("")
        return

    conclusion = recommendation.get("conclusion", "")
    next_action = recommendation.get("next_action", "")
    cooperation_advice = recommendation.get("cooperation_advice", "")

    if conclusion:
        lines.append(f"**综合建议：**{conclusion}")
        lines.append("")
    if next_action:
        lines.append(f"**建议动作：**{next_action}")
        lines.append("")
    if cooperation_advice:
        lines.append(f"**合作建议：**{cooperation_advice}")
        lines.append("")

    _append_list_section(lines, "重点关注风险", recommendation.get("key_risks"))


def _append_bottom_recommendation_sections(lines: list[str], scoring_result: dict) -> None:
    """将行动建议、重点风险和需补充资料统一放到报告底部。"""
    recommendation = scoring_result.get("action_recommendation")
    if not recommendation:
        return

    _append_action_recommendation(lines, scoring_result)
    if isinstance(recommendation, dict):
        _append_list_section(lines, "需补充资料", recommendation.get("required_materials"))


def _append_data_source_summary(lines: list[str], scoring_result: dict) -> None:
    """追加数据源分层说明。"""
    data_source_summary = scoring_result.get("data_source_summary")
    if not data_source_summary:
        return

    _append_section_heading(lines, "数据源分层说明")
    if isinstance(data_source_summary, str):
        lines.append(data_source_summary)
    elif isinstance(data_source_summary, dict):
        label_map = {
            "official_or_structured": "权威结构化数据",
            "public_disclosure": "公开披露资料",
            "search_or_media": "搜索与媒体信息",
            "inference": "间接推断说明",
            "official": "权威官方数据",
            "structured": "结构化数据",
            "public": "公开披露资料",
            "media": "媒体与搜索信息",
        }
        for label, text in data_source_summary.items():
            if text:
                display_label = label_map.get(str(label), str(label).replace("_", " ").strip())
                lines.append(f"- **{display_label}：**{text}")
    lines.append("")


def _append_enterprise_profile(lines: list[str], enterprise_name: str, scoring_result: dict) -> None:
    """在报告头部展示企业基础身份信息。"""
    display_name, social_credit_code = _resolve_enterprise_identity(enterprise_name, scoring_result)

    _append_section_heading(lines, "企业基础信息")
    lines.append("| 字段 | 内容 |")
    lines.append("|------|------|")
    lines.append(f"| 企业名称 | {display_name} |")
    lines.append(f"| 统一社会信用代码 | {social_credit_code} |")
    lines.append("")


def _normalize_green_power_fit(value) -> str:
    """绿电合作适配度只输出高适配/中适配，符合当前业务前提。"""
    if isinstance(value, dict):
        raw = str(value.get("level") or value.get("适配等级") or value.get("绿电合作适配度") or "")
    else:
        raw = str(value or "")
    if "高" in raw:
        return "高适配"
    return "中适配"


def _append_structured_assessment_section(
    lines: list[str],
    title: str,
    data,
    fallback: str = "",
) -> None:
    """追加 P0/P1 业务评估结构，支持字符串、列表和字典。"""
    if not data:
        if not fallback:
            return
        data = fallback

    _append_section_heading(lines, title)

    if isinstance(data, str):
        lines.append(data)
        lines.append("")
        return

    if isinstance(data, list):
        for item in data:
            if item:
                lines.append(f"- {item}")
        lines.append("")
        return

    if isinstance(data, dict):
        for key, value in data.items():
            if value in (None, "", [], {}):
                continue
            if isinstance(value, list):
                rendered = "；".join(str(item) for item in value if item)
            elif isinstance(value, dict):
                rendered = "；".join(f"{k}: {v}" for k, v in value.items() if v not in (None, "", [], {}))
            else:
                rendered = str(value)
            if rendered:
                lines.append(f"- **{key}：**{rendered}")
        lines.append("")


def _merge_supplementary_data_into_sections(scoring_result: dict) -> None:
    """将补充采集字段融入对应报告板块，避免客户看到内部 P1 章节。"""
    supplementary = scoring_result.get("supplementary_data_collection")
    if not isinstance(supplementary, dict):
        return

    merge_rules = {
        "股权穿透": ("related_party_risk", "股权穿透"),
        "资金压力": ("asset_equity_encumbrance_review", "资金压力"),
        "监管合规": ("administrative_operation_risk_review", "监管合规"),
        "经营真实性": ("performance_capability", "经营真实性"),
    }

    for source_key, (target_section, target_key) in merge_rules.items():
        value = supplementary.get(source_key)
        if value in (None, "", [], {}):
            continue

        section = scoring_result.get(target_section)
        if isinstance(section, str):
            section = {"综合判断": section}
            scoring_result[target_section] = section
        elif not isinstance(section, dict):
            section = {}
            scoring_result[target_section] = section

        section.setdefault(target_key, value)


def _append_business_assessment_sections(lines: list[str], scoring_result: dict) -> None:
    """追加 P0/P1 业务结构，突出绿电合作准入判断。"""
    _merge_supplementary_data_into_sections(scoring_result)

    green_fit = scoring_result.get("green_power_fit") or scoring_result.get("green_power_cooperation_fit") or {}
    if not isinstance(green_fit, dict):
        green_fit = {"判断依据": green_fit}
    green_fit["适配等级"] = _normalize_green_power_fit(green_fit)
    green_fit.setdefault(
        "业务口径",
        "客户进入评估通常已存在绿电合作意向，因此仅区分高适配/中适配；重点判断落地路径和需补充资料。",
    )

    _append_structured_assessment_section(lines, "绿电合作适配度", green_fit)
    _append_structured_assessment_section(
        lines,
        "主体真实性核验",
        scoring_result.get("subject_verification"),
        "已完成企业名称和统一社会信用代码确认；仍建议人工复核登记状态、经营范围、注册地址与实际经营地址一致性。",
    )
    _append_structured_assessment_section(
        lines,
        "履约能力分析",
        scoring_result.get("performance_capability"),
        "需结合参保人数、招聘活跃度、招投标/中标记录、生产基地、客户订单、被执行记录等信息继续判断履约能力。",
    )
    _append_structured_assessment_section(
        lines,
        "关联方风险",
        scoring_result.get("related_party_risk"),
        "需关注控股股东、实际控制人、法定代表人及主要关联企业是否存在失信、限高、经营异常、行政处罚等风险外溢。",
    )


def _append_kyb_risk_sections(lines: list[str], scoring_result: dict) -> None:
    """追加专项 KYB 风险核查摘要，让报告更接近尽调报告。"""
    _merge_supplementary_data_into_sections(scoring_result)

    _append_structured_assessment_section(
        lines,
        "专项风险核查总览",
        scoring_result.get("kyb_risk_overview") or scoring_result.get("special_risk_review"),
    )
    _append_structured_assessment_section(
        lines,
        "法律诉讼核查",
        scoring_result.get("legal_litigation_review"),
    )
    _append_structured_assessment_section(
        lines,
        "行政经营风险核查",
        scoring_result.get("administrative_operation_risk_review"),
    )
    _append_structured_assessment_section(
        lines,
        "税务环保风险核查",
        scoring_result.get("tax_environment_risk_review"),
    )
    _append_structured_assessment_section(
        lines,
        "资产及股权负担核查",
        scoring_result.get("asset_equity_encumbrance_review"),
    )
    _append_structured_assessment_section(
        lines,
        "历史风险核查",
        scoring_result.get("history_risk_review"),
    )
    _append_structured_assessment_section(
        lines,
        "经营资质与土地信息",
        scoring_result.get("operation_qualification_land_review"),
    )


def _build_comprehensive_summary(
    evaluation_result: dict,
    scoring_result: dict,
    summary: str,
) -> str:
    """生成更完整的综合评价，避免报告开头只有一句空泛结论。"""
    final_grade = evaluation_result["final_grade"]
    total_score = evaluation_result["weighted_total_score"]
    dimensions = evaluation_result.get("dimensions", {})

    parts = [
        f"本次评估目标为{scoring_result.get('assessment_goal', '企业综合评估')}。"
        f"企业加权总分为 {total_score} 分，评级为 {final_grade}。"
    ]

    if summary:
        parts.append(summary)

    dim_sentences = []
    for dim_key in ["industry", "operation", "finance", "credit"]:
        dim_data = dimensions.get(dim_key, {})
        if not dim_data:
            continue
        dim_name = DIMENSION_NAMES.get(dim_key, dim_key)
        dim_score = dim_data.get("total_score", 0)
        dim_max = dim_data.get("max_score", 0)
        ratio = dim_data.get("score_ratio", 0)
        if ratio >= 0.8:
            level = "表现较强"
        elif ratio >= 0.6:
            level = "表现中等偏上"
        elif ratio >= 0.4:
            level = "表现一般"
        else:
            level = "存在明显短板"
        dim_sentences.append(f"{dim_name}维度得分 {dim_score}/{dim_max}，{level}")
    if dim_sentences:
        parts.append("；".join(dim_sentences) + "。")

    finance_notes = scoring_result.get("financial_assessment_notes") or []
    missing_financial_fields = scoring_result.get("missing_financial_fields") or []
    if isinstance(finance_notes, str):
        finance_notes = [finance_notes]
    if isinstance(missing_financial_fields, str):
        missing_financial_fields = [missing_financial_fields]
    if finance_notes or missing_financial_fields:
        note_text = "；".join(str(item) for item in finance_notes if item)
        missing_text = "、".join(str(item) for item in missing_financial_fields if item)
        if missing_text:
            parts.append(f"财务评估需谨慎，关键缺失项包括：{missing_text}。{note_text}")
        elif note_text:
            parts.append(note_text)

    recommendation = scoring_result.get("action_recommendation")
    if isinstance(recommendation, dict):
        conclusion = recommendation.get("conclusion")
        next_action = recommendation.get("next_action")
        if conclusion or next_action:
            parts.append(f"综合建议：{conclusion or ''}。建议动作：{next_action or ''}。")
    elif isinstance(recommendation, str):
        parts.append(f"综合建议：{recommendation}")

    return "\n\n".join(part for part in parts if part)


def generate_markdown_report(
    enterprise_name: str,
    evaluation_result: dict,
    scoring_result: dict,
    summary: str,
    qcc_data_summary: str = "",
) -> str:
    """生成 Markdown 格式的企业分析报告

    Args:
        enterprise_name: 企业名称
        evaluation_result: 评分引擎输出的评估结果
        scoring_result: LLM 输出的原始评分数据（含评分依据）
        summary: 综合评价文本
        qcc_data_summary: 企查查结构化数据摘要（可选）

    Returns:
        Markdown 格式的报告文本
    """
    final_grade = evaluation_result["final_grade"]
    total_score = evaluation_result["weighted_total_score"]
    dimensions = evaluation_result["dimensions"]
    weights = evaluation_result["weights"]
    _merge_triggered_risks(scoring_result, evaluation_result)

    # 报告头部
    display_name, social_credit_code = _resolve_enterprise_identity(enterprise_name, scoring_result)
    assessment_goal = scoring_result.get("assessment_goal", "企业综合评估")
    lines = []
    lines.append("# 企业综合评估报告")
    lines.append("")
    lines.append(f"**企业名称：**{display_name}")
    lines.append("")
    lines.append(f"**统一社会信用代码：**{social_credit_code}")
    lines.append("")
    lines.append(f"**总分：**{total_score} 分")
    lines.append("")
    lines.append(f"**评级：**{final_grade}")
    lines.append("")
    lines.append(f"**评估目标：**{assessment_goal}")
    lines.append("")
    lines.append(f"**生成日期：**{datetime.now().strftime('%Y-%m-%d')}")
    lines.append("")

    _append_enterprise_profile(lines, enterprise_name, scoring_result)

    # 综合评价
    _append_section_heading(lines, "综合评价")
    lines.append(_build_comprehensive_summary(evaluation_result, scoring_result, summary))
    lines.append("")
    lines.append("---")
    lines.append("")

    _append_list_section(lines, "财务评估说明", scoring_result.get("financial_assessment_notes"))
    _append_list_section(lines, "财务数据缺失项", scoring_result.get("missing_financial_fields"))
    _append_business_assessment_sections(lines, scoring_result)
    _append_kyb_risk_sections(lines, scoring_result)

    # 各维度详情
    for dim_key in ["industry", "operation", "finance", "credit"]:
        dim_name = DIMENSION_NAMES.get(dim_key, dim_key)
        dim_data = dimensions.get(dim_key, {})
        dim_score = dim_data.get("total_score", 0)
        dim_max = dim_data.get("max_score", 0)
        dim_items = dim_data.get("items", [])

        _append_section_heading(
            lines,
            f"{dim_name}评价（得分 {dim_score}/{dim_max}，权重 {weights.get(dim_key, 0)*100:.0f}%）",
        )

        for item in dim_items:
            reliability = RELIABILITY_MAP.get(item.get("reliability", "yellow"), "\U0001f7e1")
            score = item.get("score", 0)
            max_score = item.get("max_score", 0)
            lines.append(f"### {item['name']}（得分 {score}/{max_score}，可信度 {reliability}）")
            lines.append("")
            basis = item.get("basis", "")
            if basis:
                lines.append(f"评分依据：{basis}")
                lines.append("")

        # 维度综合评价（从 scoring_result 中获取）
        dim_summary_key = f"{dim_key}_summary"
        dim_summary_text = scoring_result.get(dim_summary_key, "")
        if dim_summary_text:
            lines.append(f"**{dim_name}综合评价：**{dim_summary_text}")
            lines.append("")

        lines.append("---")
        lines.append("")

    # 评分汇总表
    _append_section_heading(lines, "评分汇总表")
    lines.append("| 打分项 | 得分 | 满分 | 可信度 |")
    lines.append("|--------|------|------|--------|")

    for dim_key in ["industry", "operation", "finance", "credit"]:
        dim_data = dimensions.get(dim_key, {})
        dim_items = dim_data.get("items", [])
        for item in dim_items:
            reliability = RELIABILITY_MAP.get(item.get("reliability", "yellow"), "\U0001f7e1")
            lines.append(f"| {item['name']} | {item['score']} | {item['max_score']} | {reliability} |")

    lines.append("")

    # 企查查结构化数据仅供内部红线校验和评分依据参考，不再在报告中单独展示原始数据

    _append_bottom_recommendation_sections(lines, scoring_result)

    # 免责声明
    lines.append("---")
    lines.append("")
    _append_section_heading(lines, "免责声明")
    lines.append("1. 本报告基于公开网络信息自动生成，仅供参考")
    lines.append("2. 标注 \U0001f534 的数据项为缺失推算，仅供参考")
    lines.append("3. 不构成任何投资建议或商业决策依据")
    lines.append("4. 建议重大决策前结合实地考察和专业尽调")

    return "\n".join(lines)


def _render_html_kv_section(title: str, data, fallback: str = "") -> str:
    """渲染 HTML 信息块。"""
    if not data:
        data = fallback
    if not data:
        return ""

    body = []
    if isinstance(data, str):
        body.append(f"<p>{_html_text(data)}</p>")
    elif isinstance(data, list):
        items = "".join(f"<li>{_html_text(item)}</li>" for item in data if item)
        body.append(f"<ul>{items}</ul>")
    elif isinstance(data, dict):
        rows = []
        for key, value in data.items():
            if value in (None, "", [], {}):
                continue
            if isinstance(value, list):
                rendered = "；".join(str(item) for item in value if item)
            elif isinstance(value, dict):
                rendered = "；".join(f"{k}: {v}" for k, v in value.items() if v not in (None, "", [], {}))
            else:
                rendered = str(value)
            if rendered:
                rows.append(
                    f"<tr><th>{_html_text(key)}</th><td>{_html_text(rendered)}</td></tr>"
                )
        if rows:
            body.append(f"<table class=\"kv-table\">{''.join(rows)}</table>")
    else:
        body.append(f"<p>{_html_text(data)}</p>")

    if not body:
        return ""
    return f"""
<section class="report-section">
  <h2>{_html_text(title)}</h2>
  {''.join(body)}
</section>
"""


def _render_html_list_section(title: str, values) -> str:
    if not values:
        return ""
    if isinstance(values, str):
        values = [values]
    items = "".join(f"<li>{_html_text(item)}</li>" for item in values if item)
    if not items:
        return ""
    return f"""
<section class="report-section">
  <h2>{_html_text(title)}</h2>
  <ul>{items}</ul>
</section>
"""


def _render_dimension_html(dim_key: str, evaluation_result: dict, scoring_result: dict) -> str:
    dimensions = evaluation_result["dimensions"]
    weights = evaluation_result["weights"]
    dim_name = DIMENSION_NAMES.get(dim_key, dim_key)
    dim_data = dimensions.get(dim_key, {})
    dim_score = dim_data.get("total_score", 0)
    dim_max = dim_data.get("max_score", 0)
    dim_items = dim_data.get("items", [])

    cards = []
    for item in dim_items:
        reliability = RELIABILITY_MAP.get(item.get("reliability", "yellow"), "\U0001f7e1")
        cards.append(
            f"""
<div class="score-item">
  <div class="score-item-head">
    <strong>{_html_text(item.get('name', ''))}</strong>
    <span>{_html_text(item.get('score', 0))}/{_html_text(item.get('max_score', 0))} {reliability}</span>
  </div>
  <p>{_html_text(item.get('basis', ''))}</p>
</div>
"""
        )

    dim_summary = scoring_result.get(f"{dim_key}_summary", "")
    summary_html = f"<p class=\"dim-summary\">{_html_text(dim_summary)}</p>" if dim_summary else ""
    return f"""
<section class="report-section">
  <h2>{_html_text(dim_name)}评价</h2>
  <div class="section-meta">得分 {dim_score}/{dim_max}，权重 {weights.get(dim_key, 0) * 100:.0f}%</div>
  <div class="score-grid">{''.join(cards)}</div>
  {summary_html}
</section>
"""


def _build_html_download_script() -> str:
    return """
<script>
function downloadPdf() {
  window.print();
}
</script>
"""


def generate_html_report(
    enterprise_name: str,
    evaluation_result: dict,
    scoring_result: dict,
    summary: str,
    qcc_data_summary: str = "",
) -> str:
    """生成单页 HTML 报告。页面内置下载 PDF 按钮，打印样式保持内容一致。"""
    final_grade = evaluation_result["final_grade"]
    total_score = evaluation_result["weighted_total_score"]
    dimensions = evaluation_result["dimensions"]
    _merge_triggered_risks(scoring_result, evaluation_result)
    _merge_supplementary_data_into_sections(scoring_result)

    display_name, social_credit_code = _resolve_enterprise_identity(enterprise_name, scoring_result)
    assessment_goal = scoring_result.get("assessment_goal", "企业综合评估")
    comprehensive_summary = _build_comprehensive_summary(evaluation_result, scoring_result, summary)

    dimension_cards = []
    for key in ["industry", "operation", "finance", "credit"]:
        dim = dimensions.get(key, {})
        dimension_cards.append(
            f"""
<div class="metric-card">
  <span>{_html_text(DIMENSION_NAMES.get(key, key))}</span>
  <strong>{_html_text(dim.get('total_score', 0))}/{_html_text(dim.get('max_score', 0))}</strong>
</div>
"""
        )

    profile_section = _render_html_kv_section(
        "企业基础信息",
        {"企业名称": display_name, "统一社会信用代码": social_credit_code},
    )
    business_sections = "".join(
        [
            _render_html_kv_section("绿电合作适配度", scoring_result.get("green_power_fit")),
            _render_html_kv_section("主体真实性核验", scoring_result.get("subject_verification")),
            _render_html_kv_section("履约能力分析", scoring_result.get("performance_capability")),
            _render_html_kv_section("关联方风险", scoring_result.get("related_party_risk")),
        ]
    )
    kyb_sections = "".join(
        [
            _render_html_kv_section("专项风险核查总览", scoring_result.get("kyb_risk_overview") or scoring_result.get("special_risk_review")),
            _render_html_kv_section("法律诉讼核查", scoring_result.get("legal_litigation_review")),
            _render_html_kv_section("行政经营风险核查", scoring_result.get("administrative_operation_risk_review")),
            _render_html_kv_section("税务环保风险核查", scoring_result.get("tax_environment_risk_review")),
            _render_html_kv_section("资产及股权负担核查", scoring_result.get("asset_equity_encumbrance_review")),
            _render_html_kv_section("历史风险核查", scoring_result.get("history_risk_review")),
            _render_html_kv_section("经营资质与土地信息", scoring_result.get("operation_qualification_land_review")),
        ]
    )
    dimension_sections = "".join(
        _render_dimension_html(key, evaluation_result, scoring_result)
        for key in ["industry", "operation", "finance", "credit"]
    )

    recommendation = scoring_result.get("action_recommendation")
    recommendation_sections = ""
    if isinstance(recommendation, dict):
        recommendation_sections += _render_html_kv_section(
            "行动建议",
            {
                "综合建议": recommendation.get("conclusion", ""),
                "建议动作": recommendation.get("next_action", ""),
                "合作建议": recommendation.get("cooperation_advice", ""),
            },
        )
        recommendation_sections += _render_html_list_section("重点关注风险", recommendation.get("key_risks"))
        recommendation_sections += _render_html_list_section("需补充资料", recommendation.get("required_materials"))
    elif recommendation:
        recommendation_sections += _render_html_kv_section("行动建议", str(recommendation))

    finance_sections = (
        _render_html_list_section("财务评估说明", scoring_result.get("financial_assessment_notes"))
        + _render_html_list_section("财务数据缺失项", scoring_result.get("missing_financial_fields"))
    )

    generated_at = datetime.now().strftime("%Y-%m-%d")
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_html_text(display_name)} 企业综合评估报告</title>
  <style>
    :root {{
      --blue: #1664d9;
      --blue-dark: #123b75;
      --blue-light: #eaf4ff;
      --line: #d9e6f7;
      --text: #18283d;
      --muted: #5f6b7a;
      --bg: #f4f8fc;
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif; line-height: 1.65; }}
    .toolbar {{ position: sticky; top: 0; z-index: 10; display: flex; justify-content: flex-end; gap: 12px; padding: 14px 24px; background: rgba(255,255,255,.94); border-bottom: 1px solid var(--line); backdrop-filter: blur(8px); }}
    .toolbar button {{ border: 0; background: var(--blue); color: #fff; padding: 10px 16px; border-radius: 6px; font-size: 14px; cursor: pointer; }}
    .page {{ max-width: 1080px; margin: 28px auto 56px; background: #fff; box-shadow: 0 18px 45px rgba(18,59,117,.11); }}
    .cover {{ padding: 34px 42px 28px; border-top: 12px solid var(--blue); background: linear-gradient(180deg, var(--blue-light), #fff 45%); }}
    .cover-label {{ color: var(--blue); font-weight: 700; letter-spacing: 0; }}
    .cover h1 {{ margin: 10px 0 6px; font-size: 30px; line-height: 1.25; letter-spacing: 0; }}
    .cover .sub {{ color: var(--muted); }}
    .summary-strip {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 1px; margin-top: 28px; border: 1px solid var(--line); background: var(--line); }}
    .summary-cell {{ background: #f8fbff; padding: 14px 16px; }}
    .summary-cell span {{ display: block; color: var(--muted); font-size: 13px; }}
    .summary-cell strong {{ display: block; margin-top: 4px; color: var(--blue-dark); font-size: 18px; }}
    main {{ padding: 0 42px 42px; }}
    .report-section {{ break-inside: avoid; margin-top: 28px; padding-top: 2px; }}
    .report-section h2 {{ margin: 0 0 14px; padding: 9px 14px; border-left: 5px solid var(--blue); background: var(--blue-light); color: var(--blue-dark); font-size: 18px; letter-spacing: 0; }}
    .section-meta {{ color: var(--muted); margin: -4px 0 12px; }}
    .lead {{ font-size: 15px; white-space: pre-line; }}
    .metric-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-top: 18px; }}
    .metric-card {{ border: 1px solid var(--line); border-radius: 6px; padding: 14px; background: #fbfdff; }}
    .metric-card span {{ display: block; color: var(--muted); }}
    .metric-card strong {{ display: block; margin-top: 4px; color: var(--blue); font-size: 20px; }}
    .kv-table {{ width: 100%; border-collapse: collapse; table-layout: fixed; border: 1px solid var(--line); }}
    .kv-table th {{ width: 180px; text-align: left; vertical-align: top; background: #f7fbff; color: var(--blue-dark); }}
    .kv-table th, .kv-table td {{ border-bottom: 1px solid var(--line); padding: 10px 12px; word-break: break-word; }}
    .score-grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }}
    .score-item {{ border: 1px solid var(--line); border-radius: 6px; padding: 12px; background: #fff; }}
    .score-item-head {{ display: flex; justify-content: space-between; gap: 12px; color: var(--blue-dark); }}
    .score-item p {{ margin: 8px 0 0; color: var(--muted); }}
    .dim-summary {{ padding: 12px; background: #fbfdff; border: 1px solid var(--line); }}
    .disclaimer {{ color: var(--muted); font-size: 13px; }}
    @media (max-width: 760px) {{
      .page {{ margin: 0; box-shadow: none; }}
      .cover, main {{ padding-left: 18px; padding-right: 18px; }}
      .summary-strip, .metric-grid, .score-grid {{ grid-template-columns: 1fr; }}
      .kv-table th {{ width: 120px; }}
    }}
    @media print {{
      body {{ background: #fff; }}
      .toolbar {{ display: none; }}
      .page {{ margin: 0; box-shadow: none; }}
      .cover {{ border-top: 8px solid var(--blue); }}
      .report-section {{ page-break-inside: avoid; }}
      a {{ color: inherit; text-decoration: none; }}
    }}
  </style>
</head>
<body>
  <div class="toolbar"><button onclick="downloadPdf()">下载 PDF</button></div>
  <article class="page">
    <header class="cover">
      <div class="cover-label">企业综合评估报告</div>
      <h1>{_html_text(display_name)}</h1>
      <div class="sub">统一社会信用代码：{_html_text(social_credit_code)} ｜ 生成日期：{generated_at}</div>
      <div class="summary-strip">
        <div class="summary-cell"><span>总分</span><strong>{_html_text(total_score)} 分</strong></div>
        <div class="summary-cell"><span>评级</span><strong>{_html_text(final_grade)}</strong></div>
        <div class="summary-cell"><span>评估目标</span><strong>{_html_text(assessment_goal)}</strong></div>
        <div class="summary-cell"><span>报告类型</span><strong>KYB 评估</strong></div>
      </div>
    </header>
    <main>
      <section class="report-section">
        <h2>综合评价</h2>
        <div class="lead">{_html_text(comprehensive_summary)}</div>
        <div class="metric-grid">{''.join(dimension_cards)}</div>
      </section>
      {profile_section}
      {finance_sections}
      {business_sections}
      {kyb_sections}
      {dimension_sections}
      {recommendation_sections}
      <section class="report-section disclaimer">
        <h2>免责声明</h2>
        <ol>
          <li>本报告基于公开网络信息自动生成，仅供参考。</li>
          <li>数据缺失或推算项仅供业务初筛参考。</li>
          <li>本报告不构成任何投资建议或商业决策依据。</li>
          <li>重大决策前建议结合实地考察和专业尽调。</li>
        </ol>
      </section>
    </main>
  </article>
  {_build_html_download_script()}
</body>
</html>"""


def _build_s3_storage() -> S3SyncStorage:
    """构造 Coze 对象存储客户端。"""
    access_key = (
        os.getenv("COZE_BUCKET_ACCESS_KEY")
        or os.getenv("COZE_TOS_ACCESS_KEY")
        or os.getenv("AWS_ACCESS_KEY_ID")
        or "coze"
    )
    secret_key = (
        os.getenv("COZE_BUCKET_SECRET_KEY")
        or os.getenv("COZE_TOS_SECRET_KEY")
        or os.getenv("AWS_SECRET_ACCESS_KEY")
        or "coze"
    )
    bucket_name = os.getenv("COZE_BUCKET_NAME", "")
    region = os.getenv("COZE_BUCKET_REGION") or os.getenv("AWS_REGION") or "cn-beijing"
    return S3SyncStorage(access_key=access_key, secret_key=secret_key, bucket_name=bucket_name, region=region)


def _upload_html_report(html_content: str, enterprise_name: str) -> str:
    """上传 HTML 报告并返回签名 URL。"""
    safe_stem = "".join(ch if ch.isalnum() else "_" for ch in enterprise_name)[:80] or "enterprise"
    file_name = f"enterprise_report_{safe_stem}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}.html"
    storage = _build_s3_storage()
    key = storage.upload_file(
        file_content=html_content.encode("utf-8"),
        file_name=file_name,
        content_type="text/html; charset=utf-8",
    )
    expire_time = int(os.getenv("REPORT_HTML_SIGN_EXPIRE_SECONDS", "604800"))
    return storage.generate_presigned_url(key=key, expire_time=expire_time)


def generate_pdf_report(
    enterprise_name: str,
    evaluation_result: dict,
    scoring_result: dict,
    summary: str,
    qcc_data_summary: str = "",
) -> str:
    """生成 Markdown 报告并通过 Coze 文档服务转为 PDF

    Args:
        enterprise_name: 企业名称
        evaluation_result: 评分引擎输出的评估结果
        scoring_result: LLM 输出的原始评分数据
        summary: 综合评价文本
        qcc_data_summary: 企查查结构化数据摘要（可选）

    Returns:
        PDF 报告 URL
    """
    markdown_content = generate_markdown_report(
        enterprise_name, evaluation_result, scoring_result, summary, qcc_data_summary
    )
    display_name, _ = _resolve_enterprise_identity(enterprise_name, scoring_result)
    title = _build_pdf_report_title(display_name)
    client = DocumentGenerationClient()
    url = client.create_pdf_from_markdown(markdown_content, title)
    logger.info("PDF report generated for %s, URL: %s", display_name, url)
    return url
