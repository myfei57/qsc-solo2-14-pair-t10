"""成品放行：终检化验单登记、规则判定与让步接收审批。

放行不再依赖组长“看一眼化验单”：

* 每次提交化验单都按终检规格逐项判定，并结合工艺记录（成熟度、清洗凭证、
  错过酒花窗口、未关闭告警等）给出 *放行/复检/扣留* 建议与依据；
* 正式放行只能由人在建议基础上拍板，系统拒绝与建议矛盾的放行
  （扣留必须走让步审批，复检未完成不能放行）；
* 让步接收必须由非申请人审批，全程写入不可变审计日志，事后可按批次翻出。
"""

from __future__ import annotations

from typing import Any

from ..core.clock import Clock, format_moment
from ..core.config import Settings
from ..core.errors import ConflictError, NotFoundError, SequenceError, ValidationError
from ..core.ids import new_id
from ..core.validators import require_number, require_text
from ..domain.alarms import AlarmCenter
from ..domain.audit import AuditLog
from ..domain.models import (
    BatchStage,
    ConcessionRequest,
    ConcessionStatus,
    LabReport,
    ReleaseDecision,
    ReleaseStatus,
)
from ..domain.qcspec import QcSpecRegistry
from ..domain.quality import (
    PROCESS_BLOCKER,
    PROCESS_DEVIATION,
    PROCESS_OK,
    evaluate,
    concession_allowed,
)
from ..persistence.store import FileStore, merge_documents
from .brewing import BrewingService

LAB_REPORTS = "lab_reports"
RELEASE_DECISIONS = "release_decisions"
CONCESSIONS = "concession_requests"

TERMINAL_RELEASE_STATES = (
    ReleaseStatus.RELEASED.value,
    ReleaseStatus.CONCESSION_RELEASED.value,
    ReleaseStatus.REJECTED.value,
)


class ReleaseService:
    """串起规格、化验单、工艺记录、判定结论与让步审批。"""

    def __init__(
        self,
        store: FileStore,
        settings: Settings,
        clock: Clock,
        brewing: BrewingService,
        specs: QcSpecRegistry,
        alarms: AlarmCenter,
        audit: AuditLog,
    ) -> None:
        self.store = store
        self.settings = settings
        self.clock = clock
        self.brewing = brewing
        self.specs = specs
        self.alarms = alarms
        self.audit = audit
        self.reports = store.collection(LAB_REPORTS)
        self.decisions = store.collection(RELEASE_DECISIONS)
        self.concessions = store.collection(CONCESSIONS)

    # ------------------------------------------------------------------ 规格

    def define_spec(
        self,
        brewery_id: str,
        style: str,
        metrics: list[dict[str, Any]],
        actor: str,
    ) -> dict[str, Any]:
        spec = self.specs.define(brewery_id, style, metrics)
        self.audit.record(
            brewery_id,
            None,
            actor,
            "qc.spec_defined",
            {"spec_id": spec["id"], "style": spec["style"], "version": spec["version"]},
        )
        return spec

    def list_specs(self, brewery_id: str | None = None) -> list[dict[str, Any]]:
        return self.specs.list_specs(brewery_id)

    # --------------------------------------------------------------- 化验单

    def submit_lab_report(
        self,
        batch_id: str,
        metrics: list[dict[str, Any]],
        actor: str,
        sampled_at: str | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        """登记一次终检化验并立即重新判定；批次必须已完成成熟。"""

        batch = self._require_completed_batch(batch_id)
        clean_actor = require_text(actor, field="actor", max_length=60)
        normalized = self._normalize_lab_metrics(metrics)
        decision = self._get_or_create_decision(batch)
        round_no = int(decision.get("round", 0)) + 1
        now = format_moment(self.clock.now())
        report = LabReport(
            id=new_id("lab"),
            batch_id=batch_id,
            brewery_id=str(batch["brewery_id"]),
            round=round_no,
            metrics=normalized,
            sampled_at=sampled_at or now,
            submitted_at=now,
            submitted_by=clean_actor,
            note=note.strip() if isinstance(note, str) else "",
        )
        saved_report = self.reports.put(report.id, report.to_doc())
        evaluation = self._evaluate(batch, {item["name"]: item["value"] for item in normalized})
        updated = merge_documents(
            decision,
            [
                ("round", round_no),
                ("lab_report_id", saved_report["id"]),
                ("suggested", evaluation["suggestion"]),
                ("metric_results", evaluation["metric_results"]),
                ("process_findings", evaluation["process_findings"]),
                ("reasons", evaluation["reasons"]),
                ("updated_at", now),
            ],
        )
        # 已经终判（放行/拒收）的批次不得再塞化验单
        if updated.get("status") in TERMINAL_RELEASE_STATES:
            raise ConflictError(
                "批次已有终判结论，不能再登记化验单",
                batch_id=batch_id,
                status=updated.get("status"),
            )
        updated = self._apply_suggested_state(updated, evaluation["suggestion"], now)
        saved_decision = self.decisions.put(str(decision["id"]), updated)
        self.audit.record(
            str(batch["brewery_id"]),
            batch_id,
            clean_actor,
            "qc.lab_submitted",
            {
                "report_id": saved_report["id"],
                "round": round_no,
                "suggested": evaluation["suggestion"],
            },
        )
        if evaluation["suggestion"] == ReleaseStatus.HELD.value:
            self.alarms.raise_alarm(
                brewery_id=str(batch["brewery_id"]),
                source=f"qc:{batch_id}",
                severity="critical",
                code="qc_batch_held",
                message=f"批次 {batch.get('code')} 终检不合格，已扣留待判",
                context={"batch_id": batch_id, "round": round_no},
            )
        return self.decision_view(batch_id)

    def lab_reports(self, batch_id: str) -> list[dict[str, Any]]:
        self.brewing._require_batch(batch_id)
        return sorted(
            self.reports.find(lambda item: item.get("batch_id") == batch_id),
            key=lambda item: int(item.get("round", 0)),
        )

    # --------------------------------------------------------------- 判定流

    def release(self, batch_id: str, actor: str, note: str = "") -> dict[str, Any]:
        """正式放行：规则建议为放行且工作状态为待判时允许。

        扣留批次必须走让步审批；复检未关闭（retest/held）不能直接放行。
        """

        batch = self._require_completed_batch(batch_id)
        decision = self._require_decision(batch_id)
        self._require_open(decision)
        if decision.get("suggested") != ReleaseStatus.RELEASED.value:
            raise ConflictError(
                "规则判定不建议放行，请先复检或走让步审批",
                batch_id=batch_id,
                suggested=decision.get("suggested"),
                reasons=decision.get("reasons", []),
            )
        if decision.get("status") != ReleaseStatus.PENDING.value:
            raise ConflictError(
                "批次仍有未关闭的复检/扣留，不能放行",
                batch_id=batch_id,
                status=decision.get("status"),
            )
        return self._finalize(
            batch,
            decision,
            ReleaseStatus.RELEASED.value,
            disposition="正常放行",
            actor=actor,
            action="qc.released",
            note=note,
        )

    def hold(self, batch_id: str, actor: str, reason: str) -> dict[str, Any]:
        """人工扣留（例如留样异常、调查未结）。"""

        batch = self._require_completed_batch(batch_id)
        decision = self._get_or_create_decision(batch)
        self._require_open(decision)
        clean_reason = require_text(reason, field="reason", max_length=240)
        now = format_moment(self.clock.now())
        reasons = list(decision.get("reasons", []))
        reasons.append(f"人工扣留：{clean_reason}")
        updated = merge_documents(
            decision,
            [
                ("status", ReleaseStatus.HELD.value),
                ("suggested", ReleaseStatus.HELD.value),
                ("reasons", reasons),
                ("updated_at", now),
            ],
        )
        saved = self.decisions.put(str(decision["id"]), updated)
        self.alarms.raise_alarm(
            brewery_id=str(batch["brewery_id"]),
            source=f"qc:{batch_id}",
            severity="critical",
            code="qc_batch_held",
            message=f"批次 {batch.get('code')} 被人工扣留：{clean_reason}",
            context={"batch_id": batch_id},
        )
        self.audit.record(
            str(batch["brewery_id"]), batch_id, actor, "qc.held", {"reason": clean_reason}
        )
        return self.decision_view(batch_id)

    def reject(self, batch_id: str, actor: str, reason: str) -> dict[str, Any]:
        """拒收/报废，终判不可撤销。"""

        batch = self._require_completed_batch(batch_id)
        decision = self._require_open_decision(batch_id)
        clean_reason = require_text(reason, field="reason", max_length=240)
        return self._finalize(
            batch,
            decision,
            ReleaseStatus.REJECTED.value,
            disposition=clean_reason,
            actor=actor,
            action="qc.rejected",
            note=clean_reason,
        )

    # ----------------------------------------------------------- 让步接收

    def request_concession(
        self,
        batch_id: str,
        actor: str,
        reason: str,
        proposed_disposition: str,
    ) -> dict[str, Any]:
        """为扣留批次发起让步接收申请；关键指标不合格不允许让步。"""

        batch = self._require_completed_batch(batch_id)
        decision = self._require_open_decision(batch_id)
        clean_reason = require_text(reason, field="reason", max_length=240)
        clean_disposition = require_text(
            proposed_disposition, field="proposed_disposition", max_length=120
        )
        if decision.get("status") != ReleaseStatus.HELD.value:
            raise ConflictError(
                "只有扣留状态的批次才能申请让步接收",
                batch_id=batch_id,
                status=decision.get("status"),
            )
        evaluation = {
            "suggestion": decision.get("suggested"),
            "metric_results": decision.get("metric_results", []),
            "process_findings": decision.get("process_findings", []),
        }
        if not concession_allowed(evaluation):
            raise ConflictError(
                "关键指标不合格或工艺硬条件未满足，禁止让步接收",
                batch_id=batch_id,
                reasons=decision.get("reasons", []),
            )
        existing = self.concessions.find(
            lambda item: item.get("batch_id") == batch_id
            and item.get("status") == ConcessionStatus.PENDING.value
        )
        if existing:
            raise ConflictError("该批次已有待审批的让步申请", concession_id=existing[0]["id"])
        now = format_moment(self.clock.now())
        request = ConcessionRequest(
            id=new_id("conc"),
            batch_id=batch_id,
            brewery_id=str(batch["brewery_id"]),
            decision_id=str(decision["id"]),
            reason=clean_reason,
            deviation_summary=list(decision.get("reasons", [])),
            proposed_disposition=clean_disposition,
            requested_by=require_text(actor, field="actor", max_length=60),
            requested_at=now,
        )
        saved = self.concessions.put(request.id, request.to_doc())
        self.audit.record(
            str(batch["brewery_id"]),
            batch_id,
            request.requested_by,
            "qc.concession_requested",
            {"concession_id": saved["id"], "reason": clean_reason},
        )
        return self.decision_view(batch_id)

    def review_concession(
        self,
        concession_id: str,
        approver: str,
        approve: bool,
        note: str,
    ) -> dict[str, Any]:
        """审批让步申请：驳回则维持扣留，批准后才可让步放行。"""

        document = self.concessions.get(concession_id)
        if document is None:
            raise NotFoundError("让步申请不存在", concession_id=concession_id)
        if document.get("status") != ConcessionStatus.PENDING.value:
            raise ConflictError(
                "让步申请已经审批",
                concession_id=concession_id,
                status=document.get("status"),
            )
        clean_approver = require_text(approver, field="approver", max_length=60)
        clean_note = require_text(note, field="note", max_length=240)
        if clean_approver == document.get("requested_by"):
            raise ConflictError(
                "让步接收必须由申请人之外的负责人审批",
                requested_by=document.get("requested_by"),
                approver=clean_approver,
            )

        now = format_moment(self.clock.now())
        new_status = ConcessionStatus.APPROVED.value if approve else ConcessionStatus.REJECTED.value
        updated = merge_documents(
            document,
            [
                ("status", new_status),
                ("reviewed_by", clean_approver),
                ("reviewed_at", now),
                ("review_note", clean_note),
            ],
        )
        saved = self.concessions.put(concession_id, updated)
        self.audit.record(
            str(saved["brewery_id"]),
            str(saved["batch_id"]),
            clean_approver,
            "qc.concession_reviewed",
            {"concession_id": concession_id, "approved": approve, "note": clean_note},
        )
        if not approve:
            return self.decision_view(str(saved["batch_id"]))

        decision = self.decisions.require(str(saved["decision_id"]), label="放行判定")
        batch = self._require_completed_batch(str(saved["batch_id"]))
        self._require_open(decision)
        return self._finalize(
            batch,
            decision,
            ReleaseStatus.CONCESSION_RELEASED.value,
            disposition=str(saved["proposed_disposition"]),
            actor=clean_approver,
            action="qc.concession_released",
            note=clean_note,
            concession_id=concession_id,
        )

    def concessions_for(self, batch_id: str) -> list[dict[str, Any]]:
        return sorted(
            self.concessions.find(lambda item: item.get("batch_id") == batch_id),
            key=lambda item: str(item.get("requested_at", "")),
        )

    def list_concessions(self, status: str | None = None) -> list[dict[str, Any]]:
        items = self.concessions.all()
        if status:
            allowed = [item.value for item in ConcessionStatus]
            if status not in allowed:
                raise ValidationError("审批状态不合法", status=status, allowed=allowed)
            items = [item for item in items if item.get("status") == status]
        return sorted(items, key=lambda item: str(item.get("requested_at", "")), reverse=True)

    # --------------------------------------------------------------- 查询口

    def decision_view(self, batch_id: str) -> dict[str, Any]:
        """批次放行档案：判定 + 历次化验单 + 让步审批，事后可整体翻出。"""

        batch = self.brewing._require_batch(batch_id)
        recipe = self.brewing.recipes.get(str(batch["recipe_id"]))
        decision = self.decisions.find(lambda item: item.get("batch_id") == batch_id)
        document = decision[0] if decision else None
        return {
            "batch": {
                "id": batch.get("id"),
                "code": batch.get("code"),
                "stage": batch.get("stage"),
                "style": recipe.get("style"),
                "recipe_version": batch.get("recipe_version"),
                "completed_at": batch.get("completed_at"),
            },
            "decision": document,
            "lab_reports": self.lab_reports(batch_id),
            "concessions": self.concessions_for(batch_id),
            "audit": self.audit.export_batch(batch_id),
        }

    def list_decisions(self, status: str | None = None) -> list[dict[str, Any]]:
        items = self.decisions.all()
        if status:
            allowed = [item.value for item in ReleaseStatus]
            if status not in allowed:
                raise ValidationError("放行状态不合法", status=status, allowed=allowed)
            items = [item for item in items if item.get("status") == status]
        return sorted(items, key=lambda item: str(item.get("updated_at", "")), reverse=True)

    def queue(self) -> dict[str, Any]:
        """放行工作台计数。"""

        items = self.decisions.all()
        counts: dict[str, int] = {}
        for item in items:
            key = str(item.get("status"))
            counts[key] = counts.get(key, 0) + 1
        return {
            "total": len(items),
            "by_status": counts,
            "pending_concessions": len(self.list_concessions(ConcessionStatus.PENDING.value)),
        }

    # --------------------------------------------------------------- 内部方法

    def _evaluate(self, batch: dict[str, Any], lab_metrics: dict[str, float]) -> dict[str, Any]:
        recipe = self.brewing.recipes.get(str(batch["recipe_id"]))
        spec = self.specs.require_for_batch(
            str(batch["brewery_id"]), str(recipe.get("style"))
        )
        findings = self._process_findings(batch)
        evaluation = evaluate(list(spec.get("metrics", [])), lab_metrics, findings)
        evaluation["spec_id"] = spec["id"]
        evaluation["spec_version"] = spec.get("version")
        return evaluation

    def _process_findings(self, batch: dict[str, Any]) -> list[dict[str, Any]]:
        """从工艺记录中提取放行前必须核查的事实。"""

        findings: list[dict[str, Any]] = []
        batch_id = str(batch["id"])

        tank_id = batch.get("tank_id")
        if tank_id:
            certificate = self.brewing.tanks.cip.certificate_for(str(tank_id))
            if certificate is None:
                findings.append(
                    {
                        "name": "cip_certificate",
                        "result": PROCESS_BLOCKER,
                        "message": "发酵罐缺少有效 CIP 清洗凭证",
                    }
                )
            else:
                findings.append(
                    {
                        "name": "cip_certificate",
                        "result": PROCESS_OK,
                        "message": f"清洗凭证 {certificate.get('id')} 有效",
                    }
                )

        hop_summary = self.brewing.hops.summary(batch_id)
        missed = int(hop_summary.get("missed", 0))
        if missed:
            findings.append(
                {
                    "name": "hop_window",
                    "result": PROCESS_DEVIATION,
                    "message": f"有 {missed} 次酒花错过投放窗口",
                }
            )

        active_alarms = self.alarms.list_alarms(
            status="active", brewery_id=str(batch["brewery_id"])
        )
        batch_alarms = [
            item
            for item in active_alarms
            if item.get("context", {}).get("batch_id") == batch_id
            # 放行模块自己发的扣留通知是判定输出，不能反过来当作工艺输入自锁
            and not str(item.get("source", "")).startswith("qc:")
        ]
        unresolved = [item for item in batch_alarms if item.get("severity") == "critical"]
        if unresolved:
            findings.append(
                {
                    "name": "open_alarms",
                    "result": PROCESS_BLOCKER,
                    "message": f"批次有 {len(unresolved)} 条未关闭的严重告警",
                }
            )
        elif batch_alarms:
            findings.append(
                {
                    "name": "open_alarms",
                    "result": PROCESS_DEVIATION,
                    "message": f"批次有 {len(batch_alarms)} 条未关闭告警",
                }
            )
        return findings

    def _normalize_lab_metrics(
        self, metrics: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        if not isinstance(metrics, list) or not metrics:
            raise ValidationError("化验指标必须是非空数组", field="metrics")
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in metrics:
            if not isinstance(item, dict):
                raise ValidationError("化验指标必须是对象", field="metrics")
            name = require_text(item.get("name"), field="metric.name", max_length=60)
            if name in seen:
                raise ValidationError("化验指标重复登记", metric=name)
            seen.add(name)
            value = require_number(item.get("value"), field=f"metric:{name}")
            normalized.append(
                {
                    "name": name,
                    "value": value,
                    "unit": str(item.get("unit", "") or ""),
                }
            )
        return normalized

    def _finalize(
        self,
        batch: dict[str, Any],
        decision: dict[str, Any],
        status: str,
        disposition: str,
        actor: str,
        action: str,
        note: str,
        concession_id: str | None = None,
    ) -> dict[str, Any]:
        clean_actor = require_text(actor, field="actor", max_length=60)
        now = format_moment(self.clock.now())
        patch: list[tuple[str, Any]] = [
            ("status", status),
            ("decided_by", clean_actor),
            ("decided_at", now),
            ("released_at", now),
            ("disposition", disposition),
            ("updated_at", now),
        ]
        if concession_id is not None:
            patch.append(("concession_id", concession_id))
        if note:
            reasons = list(decision.get("reasons", []))
            reasons.append(f"终判备注（{clean_actor}）：{note.strip()}")
            patch.append(("reasons", reasons))
        updated = merge_documents(decision, patch)
        self.decisions.put(str(decision["id"]), updated)
        self.audit.record(
            str(batch["brewery_id"]),
            str(batch["id"]),
            clean_actor,
            action,
            {"status": status, "disposition": disposition, "concession_id": concession_id},
        )
        return self.decision_view(str(batch["id"]))

    def _apply_suggested_state(
        self, decision: dict[str, Any], suggestion: str, now: str
    ) -> dict[str, Any]:
        """把建议落到工作状态。

        * 建议放行 → ``pending``：待质量负责人正式拍板；
        * 建议复检 → ``retest``，但已扣留的批次不因复检转好而自动解锁；
        * 建议扣留 → ``held``。

        ``status`` 是当前工作流状态，``suggested`` 才是规则引擎的建议，
        只有显式放行/让步放行/拒收才进入终判。
        """

        status = decision.get("status")
        if status in TERMINAL_RELEASE_STATES:
            return decision
        if suggestion == ReleaseStatus.HELD.value:
            status = ReleaseStatus.HELD.value
        elif suggestion == ReleaseStatus.RETEST.value:
            if status != ReleaseStatus.HELD.value:
                status = ReleaseStatus.RETEST.value
        else:
            if status != ReleaseStatus.HELD.value:
                status = ReleaseStatus.PENDING.value
        return merge_documents(decision, [("status", status), ("updated_at", now)])

    def _require_completed_batch(self, batch_id: str) -> dict[str, Any]:
        batch = self.brewing._require_batch(batch_id)
        if batch.get("stage") != BatchStage.COMPLETED.value:
            raise SequenceError(
                "批次尚未完成成熟，不能进入成品放行",
                batch_id=batch_id,
                stage=batch.get("stage"),
            )
        return batch

    def _get_or_create_decision(self, batch: dict[str, Any]) -> dict[str, Any]:
        existing = self.decisions.find(lambda item: item.get("batch_id") == batch["id"])
        if existing:
            return existing[0]
        now = format_moment(self.clock.now())
        decision = ReleaseDecision(
            id=new_id("rel"),
            batch_id=str(batch["id"]),
            brewery_id=str(batch["brewery_id"]),
            created_at=now,
            updated_at=now,
        )
        return self.decisions.put(decision.id, decision.to_doc())

    def _require_decision(self, batch_id: str) -> dict[str, Any]:
        items = self.decisions.find(lambda item: item.get("batch_id") == batch_id)
        if not items:
            raise ConflictError("批次还没有化验单，无法放行", batch_id=batch_id)
        return items[0]

    def _require_open_decision(self, batch_id: str) -> dict[str, Any]:
        decision = self._require_decision(batch_id)
        self._require_open(decision)
        return decision

    def _require_open(self, decision: dict[str, Any]) -> None:
        if decision.get("status") in TERMINAL_RELEASE_STATES:
            raise ConflictError(
                "放行结论已终判，不可再变更",
                batch_id=decision.get("batch_id"),
                status=decision.get("status"),
            )
