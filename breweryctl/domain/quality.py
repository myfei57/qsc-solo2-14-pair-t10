"""成品终检指标与放行判定规则。

判定逻辑保持为无副作用的纯函数：输入终检指标、规格上下限与工艺偏差，
输出放行/复检/扣留建议与逐条依据。服务层负责取数与落库，
这里只回答“按这份化验单和这份工艺记录，结论是什么、为什么”。
"""

from __future__ import annotations

from typing import Any

from ..core.errors import ValidationError
from ..core.validators import require_number, require_text
from .models import ReleaseStatus

# 单项指标的判定结果
METRIC_PASS = "pass"
METRIC_BORDERLINE = "borderline"
METRIC_FAIL = "fail"
METRIC_MISSING = "missing"

# 工艺记录核查结果
PROCESS_OK = "ok"
PROCESS_DEVIATION = "deviation"
PROCESS_BLOCKER = "blocker"

DEFAULT_BORDERLINE_RATIO = 0.2


def build_metric_spec(
    name: str,
    lower: float | None,
    upper: float | None,
    *,
    critical: bool = False,
    unit: str = "",
    borderline_ratio: float | None = None,
) -> dict[str, Any]:
    """构造一条指标规格：上下限至少给一个，临界不合格项不允许让步放行。"""

    clean_name = require_text(name, field="name", max_length=60)
    low = _optional_limit(lower, "lower")
    high = _optional_limit(upper, "upper")
    if low is None and high is None:
        raise ValidationError("指标规格必须至少给出一个限值", metric=clean_name)
    if low is not None and high is not None and low > high:
        raise ValidationError("指标下限不能大于上限", metric=clean_name, lower=low, upper=high)
    ratio = DEFAULT_BORDERLINE_RATIO if borderline_ratio is None else require_number(
        borderline_ratio, field="borderline_ratio", minimum=0.0, maximum=1.0
    )
    return {
        "name": clean_name,
        "lower": low,
        "upper": high,
        "critical": bool(critical),
        "unit": unit.strip() if isinstance(unit, str) else "",
        "borderline_ratio": ratio,
    }


def default_spec_metrics() -> list[dict[str, Any]]:
    """啤酒成品终检的默认指标带。

    卫生/安全类（微生物、浊度）标记为 critical，超限直接扣留且不允许让步；
    理化类（酒精度、原麦汁浓度、CO₂、苦味值、pH、真发度）允许在复检确认后
    走让步审批。
    """

    return [
        build_metric_spec("alcohol_abv", 4.5, 5.5, unit="%vol"),
        build_metric_spec("original_extract", 11.0, 13.0, unit="°P"),
        build_metric_spec("co2", 0.45, 0.70, unit="%"),
        build_metric_spec("ibu", 25.0, 45.0, unit="IBU"),
        build_metric_spec("ph", 4.0, 4.6, critical=True),
        build_metric_spec("apparent_attenuation", 75.0, 85.0, unit="%"),
        build_metric_spec("turbidity", None, 0.9, critical=True, unit="EBC"),
        build_metric_spec("microbial", None, 0.0, critical=True, unit="定性"),
    ]


def evaluate(
    spec_metrics: list[dict[str, Any]],
    lab_metrics: dict[str, float],
    process_findings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """按规格评估一次化验单，给出建议结论与依据。

    返回字段：

    * ``suggestion``：released / retest / held
    * ``metric_results``：逐项指标的判定与距离限值的余量
    * ``process_findings``：工艺记录核查结果
    * ``reasons``：人类可读的判定依据
    """

    if not isinstance(spec_metrics, list) or not spec_metrics:
        raise ValidationError("放行规格不能为空")

    metric_results: list[dict[str, Any]] = []
    missing: list[str] = []
    borderline: list[str] = []
    failed: list[dict[str, Any]] = []

    for spec in spec_metrics:
        result = _evaluate_metric(spec, lab_metrics)
        metric_results.append(result)
        if result["result"] == METRIC_MISSING:
            missing.append(result["name"])
        elif result["result"] == METRIC_BORDERLINE:
            borderline.append(result["name"])
        elif result["result"] == METRIC_FAIL:
            failed.append(result)

    findings = list(process_findings or [])
    blockers = [item for item in findings if item.get("result") == PROCESS_BLOCKER]
    deviations = [item for item in findings if item.get("result") == PROCESS_DEVIATION]

    reasons: list[str] = []
    for result in failed:
        reasons.append(
            f"指标 {result['name']} 不合格：实测 {result['actual']}，"
            f"限值 {_format_limits(result)}"
            + ("（关键指标）" if result.get("critical") else "")
        )
    for name in borderline:
        reasons.append(f"指标 {name} 贴近限值，建议复检确认")
    for name in missing:
        reasons.append(f"指标 {name} 缺少化验结果")
    for item in blockers:
        reasons.append(f"工艺硬性条件未满足：{item.get('message', item.get('name', ''))}")
    for item in deviations:
        reasons.append(f"工艺偏差：{item.get('message', item.get('name', ''))}")

    critical_failures = [item for item in failed if item.get("critical")]
    if critical_failures or blockers:
        suggestion = ReleaseStatus.HELD.value
    elif failed or missing:
        suggestion = ReleaseStatus.HELD.value
    elif borderline or deviations:
        suggestion = ReleaseStatus.RETEST.value
    else:
        suggestion = ReleaseStatus.RELEASED.value

    return {
        "suggestion": suggestion,
        "metric_results": metric_results,
        "process_findings": findings,
        "reasons": reasons,
    }


def concession_allowed(evaluation: dict[str, Any]) -> bool:
    """扣留结论是否允许申请让步接收：关键指标或工艺硬条件不合格则禁止。"""

    if evaluation.get("suggestion") != ReleaseStatus.HELD.value:
        return False
    for result in evaluation.get("metric_results", []):
        if result.get("result") == METRIC_FAIL and result.get("critical"):
            return False
    for finding in evaluation.get("process_findings", []):
        if finding.get("result") == PROCESS_BLOCKER:
            return False
    has_non_critical_failure = any(
        result.get("result") == METRIC_FAIL and not result.get("critical")
        for result in evaluation.get("metric_results", [])
    )
    return has_non_critical_failure


def _evaluate_metric(spec: dict[str, Any], lab_metrics: dict[str, float]) -> dict[str, Any]:
    name = require_text(spec.get("name"), field="name", max_length=60)
    raw = lab_metrics.get(name)
    base = {
        "name": name,
        "unit": spec.get("unit", ""),
        "lower": spec.get("lower"),
        "upper": spec.get("upper"),
        "critical": bool(spec.get("critical")),
        "actual": raw,
        "margin": None,
        "result": METRIC_MISSING,
    }
    if raw is None:
        return base
    value = require_number(raw, field=f"metric:{name}")
    base["actual"] = value
    lower = spec.get("lower")
    upper = spec.get("upper")
    ratio = float(spec.get("borderline_ratio", DEFAULT_BORDERLINE_RATIO) or 0.0)

    if lower is not None and value < lower:
        base["result"] = METRIC_FAIL
        base["margin"] = round(value - lower, 6)
        return base
    if upper is not None and value > upper:
        base["result"] = METRIC_FAIL
        base["margin"] = round(value - upper, 6)
        return base

    band = _borderline_band(spec)
    near_lower = lower is not None and value < lower + band
    near_upper = upper is not None and value > upper - band
    if near_lower or near_upper:
        base["result"] = METRIC_BORDERLINE
    else:
        base["result"] = METRIC_PASS
    if lower is not None:
        base["margin"] = round(value - lower, 6)
    elif upper is not None:
        base["margin"] = round(upper - value, 6)
    return base


def _borderline_band(spec: dict[str, Any]) -> float:
    ratio = float(spec.get("borderline_ratio", DEFAULT_BORDERLINE_RATIO) or 0.0)
    lower = spec.get("lower")
    upper = spec.get("upper")
    if lower is not None and upper is not None:
        return (upper - lower) * ratio
    limit = lower if lower is not None else upper
    return abs(float(limit)) * ratio


def _optional_limit(value: Any, field: str) -> float | None:
    if value is None:
        return None
    return require_number(value, field=field, minimum=-10_000.0, maximum=10_000.0)


def _format_limits(result: dict[str, Any]) -> str:
    lower = result.get("lower")
    upper = result.get("upper")
    unit = result.get("unit") or ""
    if lower is not None and upper is not None:
        return f"{lower}~{upper}{unit}"
    if lower is not None:
        return f"≥{lower}{unit}"
    return f"≤{upper}{unit}"
