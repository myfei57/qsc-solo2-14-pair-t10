"""成品质量放行：质量规格与基于终检指标、工艺记录的判定引擎。"""

from __future__ import annotations

from typing import Any, Iterable

from ..core.clock import Clock, format_moment
from ..core.errors import ConflictError, NotFoundError, ValidationError
from ..core.ids import new_id
from ..core.validators import require_int, require_number, require_text
from ..persistence.store import FileStore, merge_documents
from .models import BatchStage, QualityMetric, QualitySpec

QUALITY_SPECS = "quality_specs"

DEFAULT_STYLE = "*"

# 判定结论
VERDICT_RELEASE = "release"
VERDICT_RETEST = "retest"
VERDICT_HOLD = "hold"

# 单项指标判定
METRIC_PASS = "pass"
METRIC_RETEST = "retest"
METRIC_FAIL = "fail"

# 工艺记录检查项
EVIDENCE_PASS = "pass"
EVIDENCE_DEVIATION = "deviation"
EVIDENCE_BLOCK = "block"

MAX_RETEST_ROUNDS = 3
MISSED_HOPS_DEVIATION_LIMIT = 0


class QualitySpecRegistry:
    """按工厂、酒种维护放行质量规格。"""

    def __init__(self, store: FileStore, clock: Clock) -> None:
        self.store = store
        self.clock = clock
        self.specs = store.collection(QUALITY_SPECS)

    def define(
        self,
        brewery_id: str,
        style: str,
        metrics: Iterable[dict[str, Any]],
    ) -> dict[str, Any]:
        """建立一个质量规格（初始版本）。"""

        clean_brewery = require_text(brewery_id, field="brewery_id", max_length=64)
        clean_style = (style or DEFAULT_STYLE)
        clean_style = require_text(clean_style, field="style", max_length=40)
        normalized = self._normalize_metrics(metrics)
        if self._find(clean_brewery, clean_style) is not None:
            raise ConflictError("该工厂酒种的质量规格已存在", brewery_id=clean_brewery, style=clean_style)
        now = format_moment(self.clock.now())
        spec_id = new_id("spec")
        content = self._version_content(normalized, version=1, now=now)
        document = QualitySpec(
            id=spec_id,
            brewery_id=clean_brewery,
            style=clean_style,
            metrics=content["metrics"],
            current_version=1,
            created_at=now,
            updated_at=now,
            versions=[content],
        ).to_doc()
        return self.specs.put(spec_id, document)

    def revise(self, spec_id: str, metrics: Iterable[dict[str, Any]]) -> dict[str, Any]:
        """基于当前版本创建新版本，历史版本保持不变。"""

        normalized = self._normalize_metrics(metrics)

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            version_number = int(document.get("current_version", 1)) + 1
            now = format_moment(self.clock.now())
            content = self._version_content(normalized, version=version_number, now=now)
            document["current_version"] = version_number
            document["metrics"] = content["metrics"]
            document["updated_at"] = now
            document["versions"] = list(document.get("versions", [])) + [content]
            return document

        return self.specs.update(spec_id, mutate)

    def get(self, spec_id: str) -> dict[str, Any]:
        return self.specs.require(spec_id, label="质量规格")

    def list_specs(self, brewery_id: str | None = None) -> list[dict[str, Any]]:
        items = self.specs.all()
        if brewery_id:
            items = [item for item in items if item.get("brewery_id") == brewery_id]
        return sorted(items, key=lambda item: (str(item.get("brewery_id")), str(item.get("style"))))

    def resolve(self, brewery_id: str, style: str) -> dict[str, Any]:
        """先精确匹配酒种，找不到再回退到该工厂的默认规格。"""

        document = self._find(brewery_id, style) or self._find(brewery_id, DEFAULT_STYLE)
        if document is None:
            raise NotFoundError(
                "没有适用的质量规格", brewery_id=brewery_id, style=style
            )
        return document

    def version(self, spec: dict[str, Any], version_number: int) -> dict[str, Any]:
        for item in spec.get("versions", []):
            if int(item.get("version", 0)) == version_number:
                return dict(item)
        raise NotFoundError(
            "质量规格版本不存在", spec_id=spec.get("id"), version=version_number
        )

    def seed_default(self, brewery_id: str, style: str) -> dict[str, Any] | None:
        """工厂初始化时写入默认啤酒放行规格，已存在则跳过。"""

        if self._find(brewery_id, DEFAULT_STYLE) is not None:
            return None
        return self.define(brewery_id, DEFAULT_STYLE, default_metrics(style))

    def _find(self, brewery_id: str, style: str) -> dict[str, Any] | None:
        matches = self.specs.find(
            lambda item: item.get("brewery_id") == brewery_id and item.get("style") == style
        )
        return matches[0] if matches else None

    def _version_content(self, metrics: list[dict[str, Any]], version: int, now: str) -> dict[str, Any]:
        return {"version": version, "metrics": metrics, "created_at": now}

    def _normalize_metrics(self, metrics: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in metrics:
            if not isinstance(raw, dict):
                raise ValidationError("质量指标必须是对象", metric=raw)
            key = require_text(raw.get("key"), field="metric.key", max_length=40)
            if key in seen:
                raise ValidationError("质量指标键重复", key=key)
            seen.add(key)
            lower = raw.get("lower")
            upper = raw.get("upper")
            lower_value = (
                None if lower is None else require_number(lower, field=f"{key}.lower", minimum=-1000.0, maximum=10_000.0)
            )
            upper_value = (
                None if upper is None else require_number(upper, field=f"{key}.upper", minimum=-1000.0, maximum=10_000.0)
            )
            if lower_value is None and upper_value is None:
                raise ValidationError("质量指标至少需要一个上下限", key=key)
            if lower_value is not None and upper_value is not None and lower_value > upper_value:
                raise ValidationError("质量指标下限不能高于上限", key=key, lower=lower_value, upper=upper_value)
            band = require_number(
                raw.get("retest_band", 0.0), field=f"{key}.retest_band", minimum=0.0, maximum=1000.0
            )
            raw_unit = raw.get("unit", "")
            unit = raw_unit.strip() if isinstance(raw_unit, str) else ""
            if len(unit) > 20:
                raise ValidationError(f"{key}.unit 长度不能超过 20", key=key)
            normalized.append(
                QualityMetric(
                    key=key,
                    label=require_text(raw.get("label", key), field=f"{key}.label", max_length=60),
                    unit=unit,
                    lower=lower_value,
                    upper=upper_value,
                    retest_band=band,
                    critical=bool(raw.get("critical", False)),
                ).to_doc()
            )
        if not normalized:
            raise ValidationError("质量规格至少需要一个指标")
        return normalized


def default_metrics(style: str = "") -> list[dict[str, Any]]:
    """内置的啤酒终检放行指标集。"""

    return [
        {"key": "fg", "label": "发酵度（终比重）", "unit": "SG", "lower": 1.006, "upper": 1.018, "retest_band": 0.002},
        {"key": "abv", "label": "酒精度", "unit": "%vol", "lower": 3.5, "upper": 6.5, "retest_band": 0.2},
        {"key": "ph", "label": "pH", "unit": "", "lower": 3.8, "upper": 4.6, "retest_band": 0.1},
        {"key": "co2", "label": "二氧化碳", "unit": "vol", "lower": 2.2, "upper": 2.8, "retest_band": 0.1},
        {"key": "ibu", "label": "苦味值", "unit": "IBU", "lower": 20.0, "upper": 50.0, "retest_band": 2.0},
        {"key": "turbidity", "label": "浊度", "unit": "EBC", "lower": None, "upper": 40.0, "retest_band": 5.0},
        {
            "key": "diacetyl",
            "label": "双乙酰",
            "unit": "mg/L",
            "lower": None,
            "upper": 0.15,
            "retest_band": 0.03,
            "critical": True,
        },
    ]


def evaluate_metric(metric: dict[str, Any], value: float) -> dict[str, Any]:
    """判定单个指标：合格、复检带内、不合格。"""

    lower = metric.get("lower")
    upper = metric.get("upper")
    band = float(metric.get("retest_band", 0.0))
    in_spec = (lower is None or value >= float(lower)) and (upper is None or value <= float(upper))
    if in_spec:
        result = METRIC_PASS
    else:
        near = False
        if lower is not None and value < float(lower):
            near = value >= float(lower) - band
        if upper is not None and value > float(upper):
            near = value <= float(upper) + band
        if band > 0 and near and not bool(metric.get("critical", False)):
            result = METRIC_RETEST
        else:
            result = METRIC_FAIL
    return {
        "key": metric.get("key"),
        "label": metric.get("label"),
        "unit": metric.get("unit"),
        "value": value,
        "lower": lower,
        "upper": upper,
        "retest_band": band,
        "critical": bool(metric.get("critical", False)),
        "result": result,
    }


def evaluate_release(
    metrics_spec: list[dict[str, Any]],
    values: dict[str, float],
    evidence: list[dict[str, Any]],
    *,
    round_number: int = 1,
    max_retest_rounds: int = MAX_RETEST_ROUNDS,
) -> dict[str, Any]:
    """纯函数：终检指标 + 工艺记录 → 放行 / 复检 / 扣留。

    ``evidence`` 项形如 ``{"check": ..., "status": pass|deviation|block,
    "concession_allowed": bool, "message": ...}``。
    """

    results: list[dict[str, Any]] = []
    missing: list[str] = []
    for metric in metrics_spec:
        key = str(metric.get("key"))
        value = values.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            missing.append(key)
            continue
        results.append(evaluate_metric(metric, float(value)))
    if missing:
        raise ValidationError("终检指标数据不完整", missing=missing)

    failed = [item for item in results if item["result"] == METRIC_FAIL]
    retestable = [item for item in results if item["result"] == METRIC_RETEST]
    blocks = [item for item in evidence if item.get("status") == EVIDENCE_BLOCK]
    deviations = [item for item in evidence if item.get("status") == EVIDENCE_DEVIATION]
    # 工艺偏差（漏投酒花、未解除的批次告警等）同样属于不符合，需让步处置
    concession_deviations = [item for item in deviations if item.get("concession_allowed")]

    critical_fail = any(item.get("critical") for item in failed) or any(
        item.get("critical") for item in blocks
    )
    rounds_used_up = round_number >= max_retest_rounds

    if critical_fail:
        # 关键指标不合格或安全类记录缺失：直接扣留，不允许让步
        verdict = VERDICT_HOLD
        concession_allowed = False
    elif blocks:
        # 其他工艺联锁未满足：扣留
        verdict = VERDICT_HOLD
        concession_allowed = False
    elif failed:
        # 非关键指标超限：扣留，但可申请让步接收
        verdict = VERDICT_HOLD
        concession_allowed = True
    elif retestable and not rounds_used_up:
        # 指标落在复检带且复检次数未到上限：安排复检
        verdict = VERDICT_RETEST
        concession_allowed = False
    elif retestable and rounds_used_up:
        # 复检轮次耗尽仍在临界带：按扣留处理，允许让步
        verdict = VERDICT_HOLD
        concession_allowed = True
    elif concession_deviations:
        # 终检合格但工艺有偏差：让步接收审批后才能放行
        verdict = VERDICT_HOLD
        concession_allowed = True
    else:
        # 指标全部合格、工艺记录齐全：放行
        verdict = VERDICT_RELEASE
        concession_allowed = False

    reasons = [f"指标 {item['key']} 不合格" for item in failed]
    reasons.extend(str(item.get("message", item.get("check"))) for item in blocks)
    reasons.extend(f"指标 {item['key']} 位于复检带" for item in retestable)
    reasons.extend(f"工艺偏差：{item.get('message', item.get('check'))}" for item in concession_deviations)

    return {
        "verdict": verdict,
        "concession_allowed": concession_allowed,
        "metrics": results,
        "evidence": evidence,
        "deviations": deviations,
        "reasons": reasons,
        "round": round_number,
    }


def gather_process_evidence(
    batch: dict[str, Any],
    status_view: dict[str, Any],
    *,
    batch_open_alarms: list[dict[str, Any]] | None = None,
    tank_certificate_id: str | None = None,
    missed_hops_deviation_limit: int = MISSED_HOPS_DEVIATION_LIMIT,
) -> list[dict[str, Any]]:
    """从批次状态视图汇总放行前必须核对的工艺记录。"""

    evidence: list[dict[str, Any]] = []
    stage = str(batch.get("stage"))

    def add(check: str, status: str, message: str, *, concession_allowed: bool = False, critical: bool = False) -> None:
        evidence.append(
            {
                "check": check,
                "status": status,
                "message": message,
                "concession_allowed": concession_allowed,
                "critical": critical,
            }
        )

    if stage != BatchStage.MATURING.value:
        add("stage", EVIDENCE_BLOCK, f"批次尚未成熟（当前 {stage}），不能申请放行")
        return evidence

    certificate_id = tank_certificate_id or batch.get("cip_certificate_id")
    if certificate_id:
        add("cip_certificate", EVIDENCE_PASS, f"清洗凭证 {certificate_id} 有效")
    else:
        add("cip_certificate", EVIDENCE_BLOCK, "缺少转罐清洗凭证", critical=True)

    open_alarms = [
        item
        for item in (batch_open_alarms or [])
        if str(item.get("severity")) in ("warning", "critical")
    ]
    critical_alarms = [
        item for item in open_alarms if str(item.get("severity")) == "critical"
    ]
    if critical_alarms:
        codes = sorted({str(item.get("code")) for item in critical_alarms})
        add(
            "critical_alarms",
            EVIDENCE_BLOCK,
            f"存在 {len(critical_alarms)} 条未解除的关键告警：{', '.join(codes)}",
            critical=True,
        )
    elif open_alarms:
        add(
            "open_alarms",
            EVIDENCE_DEVIATION,
            f"存在 {len(open_alarms)} 条未解除的告警，让步接收需说明",
            concession_allowed=True,
        )
    else:
        add("alarms", EVIDENCE_PASS, "无未解除告警")

    hops = status_view.get("hops") or {}
    missed = int(hops.get("missed", 0))
    if missed <= missed_hops_deviation_limit:
        add("hop_additions", EVIDENCE_PASS, f"酒花全部按窗口投加（漏投 {missed}）")
    else:
        add(
            "hop_additions",
            EVIDENCE_DEVIATION,
            f"{missed} 次酒花错过投放窗口",
            concession_allowed=True,
        )

    tank_view = status_view.get("tank") or {}
    tank_doc = tank_view.get("tank") if isinstance(tank_view, dict) else None
    maturation_days = tank_doc.get("maturation_days") if isinstance(tank_doc, dict) else None
    if maturation_days is None:
        add("maturation", EVIDENCE_BLOCK, "缺少成熟工艺记录")
    else:
        add("maturation", EVIDENCE_PASS, f"成熟 {maturation_days} 天")

    return evidence
