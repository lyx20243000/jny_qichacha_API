"""评分引擎 - 加载评分规则、计算加权得分、红线判断、评级确定"""

import json
import os
import logging
from typing import Any

logger = logging.getLogger(__name__)

SCORING_RULES_PATH = "assets/scoring_rules.json"
TRUE_VALUES = {"true", "1", "yes", "y", "是", "有", "存在", "触发"}
FALSE_VALUES = {"false", "0", "no", "n", "否", "无", "不存在", "未发现", "未查询到", "未触发"}


def _load_scoring_rules() -> dict:
    """加载评分规则配置"""
    workspace = os.getenv("COZE_WORKSPACE_PATH", "/workspace/projects")
    path = os.path.join(workspace, SCORING_RULES_PATH)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _calculate_dimension_score(items: list[dict], scores: dict[str, Any]) -> tuple[float, float]:
    """计算某个维度的得分和满分

    Returns:
        (实际得分, 满分)
    """
    total_score = 0.0
    total_max = 0.0
    for item in items:
        item_id = item["id"]
        max_score = item["max_score"]
        total_max += max_score

        if item_id in scores:
            item_score = scores[item_id]
            if isinstance(item_score, dict):
                total_score += float(item_score.get("score", 0))
            else:
                total_score += float(item_score)
        # 如果该评分项没有评分数据，不加分

    return total_score, total_max


def calculate_weighted_total(
    dimension_scores: dict[str, float],
    dimension_max_scores: dict[str, float],
    weights: dict[str, float],
) -> float:
    """计算加权总分（0-100分制）

    Args:
        dimension_scores: 各维度实际得分 {"industry": 27, "operation": 15, ...}
        dimension_max_scores: 各维度满分 {"industry": 32, "operation": 40, ...}
        weights: 各维度权重 {"industry": 0.30, "operation": 0.30, ...}

    Returns:
        加权总分 (0-100)
    """
    weighted_total = 0.0
    for dim, weight in weights.items():
        dim_score = dimension_scores.get(dim, 0)
        dim_max = dimension_max_scores.get(dim, 1)
        if dim_max > 0:
            ratio = dim_score / dim_max  # 该维度得分率 0~1
            weighted_total += ratio * weight * 100

    return round(weighted_total, 2)


def determine_grade(total_score: float, grade_thresholds: dict[str, int]) -> str:
    """根据总分确定评级

    Args:
        total_score: 加权总分 (0-100)
        grade_thresholds: 评级阈值 {"A": 80, "B": 60, "C": 40, "D": 20, "E": 0}

    Returns:
        评级字母 (A/B/C/D/E)
    """
    # 按阈值从高到低排序
    sorted_grades = sorted(grade_thresholds.items(), key=lambda x: x[1], reverse=True)
    for grade, threshold in sorted_grades:
        if total_score >= threshold:
            return grade
    return "E"


def _parse_bool_value(value: Any) -> bool:
    """严格解析红线布尔字段，避免 bool("false") 误判为 True。"""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in TRUE_VALUES:
            return True
        if normalized in FALSE_VALUES:
            return False
        return False
    return False


def _parse_number_value(value: Any) -> float:
    """解析红线数值字段，无法解析时按 0 处理。"""
    if value in (None, "", [], {}):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        logger.warning("红线数值字段无法解析，已按 0 处理: %r", value)
        return 0.0


def check_red_lines(
    red_line_data: dict[str, Any], red_line_rules: list[dict]
) -> list[dict]:
    """检查红线触发情况

    Args:
        red_line_data: 红线字段数据 {"is_dishonest": False, "major_penalty_count": 0, ...}
        red_line_rules: 红线规则列表

    Returns:
        触发的红线列表 [{"field": ..., "action": ..., "description": ...}]
    """
    triggered = []
    for rule in red_line_rules:
        field = rule["field"]
        condition = rule["condition"]
        action = rule["action"]
        description = rule["description"]

        if field not in red_line_data:
            continue

        value = red_line_data[field]

        # 解析条件
        triggered_flag = False
        if condition.startswith(">= "):
            threshold = float(condition[3:])
            triggered_flag = _parse_number_value(value) >= threshold
        elif condition.startswith("<= "):
            threshold = float(condition[3:])
            triggered_flag = _parse_number_value(value) <= threshold
        elif condition.startswith("> "):
            threshold = float(condition[2:])
            triggered_flag = _parse_number_value(value) > threshold
        elif condition.startswith("< "):
            threshold = float(condition[2:])
            triggered_flag = _parse_number_value(value) < threshold
        elif condition.startswith("== "):
            expected = condition[3:].strip()
            if expected.lower() == "true":
                triggered_flag = _parse_bool_value(value)
            elif expected.lower() == "false":
                triggered_flag = not _parse_bool_value(value)
            else:
                triggered_flag = str(value) == expected

        if triggered_flag:
            triggered.append({
                "field": field,
                "action": action,
                "description": description,
                "value": value,
            })

    return triggered


def evaluate(scoring_result: dict) -> dict:
    """完整的评分评估入口

    Args:
        scoring_result: LLM输出的评分结果，格式如下:
        {
            "industry": {
                "industry_advancement": {"score": 4, "reliability": "green"},
                "industry_scale": {"score": 5, "reliability": "green"},
                ...
            },
            "operation": { ... },
            "finance": { ... },
            "credit": { ... },
            "red_line_data": {
                "is_dishonest": false,
                "major_penalty_count": 0,
                "has_abnormal_operation": false,
                "legal_person_restricted": false
            }
        }

    Returns:
        完整的评估结果
    """
    rules = _load_scoring_rules()
    weights = rules["weights"]
    grade_thresholds = rules["grade_thresholds"]
    red_line_rules = rules["red_lines"]
    scoring_items = rules["scoring_items"]

    # 1. 计算各维度得分
    dimension_scores = {}
    dimension_max_scores = {}
    dimension_details = {}

    for dim_key, items in scoring_items.items():
        dim_scores = {}
        if dim_key in scoring_result:
            dim_scores = scoring_result[dim_key]

        total_score, total_max = _calculate_dimension_score(items, dim_scores)
        dimension_scores[dim_key] = round(total_score, 2)
        dimension_max_scores[dim_key] = total_max

        # 收集各评分项详情
        details = []
        for item in items:
            item_id = item["id"]
            if item_id in dim_scores:
                score_data = dim_scores[item_id]
                if isinstance(score_data, dict):
                    details.append({
                        "id": item_id,
                        "name": item["name"],
                        "score": score_data.get("score", 0),
                        "max_score": item["max_score"],
                        "reliability": score_data.get("reliability", "yellow"),
                        "basis": score_data.get("basis", ""),
                    })
                else:
                    details.append({
                        "id": item_id,
                        "name": item["name"],
                        "score": float(score_data),
                        "max_score": item["max_score"],
                        "reliability": "yellow",
                        "basis": "",
                    })
            else:
                details.append({
                    "id": item_id,
                    "name": item["name"],
                    "score": 0,
                    "max_score": item["max_score"],
                    "reliability": "red",
                    "basis": "未获取到数据",
                })

        dimension_details[dim_key] = {
            "total_score": round(total_score, 2),
            "max_score": total_max,
            "score_ratio": round(total_score / total_max, 4) if total_max > 0 else 0,
            "items": details,
        }

    # 2. 计算加权总分
    weighted_total = calculate_weighted_total(dimension_scores, dimension_max_scores, weights)

    # 3. 确定原始评级
    raw_grade = determine_grade(weighted_total, grade_thresholds)

    # 4. 检查红线
    red_line_data = scoring_result.get("red_line_data", {})
    triggered_red_lines = check_red_lines(red_line_data, red_line_rules)

    # 5. 评级只跟分数走；风险项仅进入风险提示，不再覆盖评级。
    final_grade = raw_grade

    return {
        "weighted_total_score": weighted_total,
        "raw_grade": raw_grade,
        "final_grade": final_grade,
        "triggered_red_lines": triggered_red_lines,
        "dimensions": dimension_details,
        "weights": weights,
    }
