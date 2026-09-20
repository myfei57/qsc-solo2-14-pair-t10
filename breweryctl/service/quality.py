"""成品放行应用服务：终检录入、判定、扣留与让步接收审批。"""

from __future__ import annotations

from typing import Any

from ..core.clock import Clock, format_moment
from ..core.errors import ConflictError, NotFoundError, SequenceError, ValidationError
from ..core.ids import new_id
from ..core.validators import require_text
from ..domain.alarms import AlarmCenter
from ..domain.audit import AuditLog
from ..domain.models import BatchStage, InspectionRound, ReleaseDecision, ReleaseStatus
from ..domain.quality import (
    EVIDENCE_BLOCK,
    MAX_RETEST_ROUNDS,
    QualitySpecRegistry,
    VERDICT_HOLD,
    VERDICT_RELEASE,
    VERDICT_RETEST,
    evaluate_release,
    gather_process_evidence,
)
from ..persistence.store import FileStore, merge_documents
from .brewing import BrewingService

RELEASES = "release_decisions"

# 让步接收的有权批准角色
CONCESSION_APPROVER_ROLES = ("qa_manager", "plant_manager")

# 终态后不再允许任何操作的状态
_FROZEN = (ReleaseStatus.RELEASED.value, ReleaseStatus.REJECTED.value)


class QualityReleaseService:
    """把终检化验单与工艺记录变成有依据、可追溯的放行结论。"""

    def __init__(
        self,
        store: FileStore,
        clock: Clock,
        brewing: BrewingService,
        specs: QualitySpecRegistry,
        alarms: AlarmCenter,
        audit: AuditLog,
    ) -> None:
        self.store = store
        self.clock = clock
        self.brewing = brewing
        self.specs = specs
        self.alarms = alarms
        self.audit = audit
        self.decisions = store.collection(RELEASES)

    # ------------------------------------------------------------------ 终检

    def submit_inspection(
        self,
        batch_id: str,
        values: dict[str, Any],
        actor: str,
        *,
        laboratory: str | None = None,
        report_no: str | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        """录入一轮终检化验结果并自动给出放行 / 复检 / 扣留结论。"""

        clean_actor = require_text(actor, field="actor", max_length=60)
        if not isinstance(values, dict) or not values:
            raise ValidationError("终检指标数据不能为空", field="metrics")

        batch = self.brewing._require_batch(batch_id)
        if batch.get("stage") != BatchStage.MATURING.value:
            raise SequenceError(
                "批次尚未成熟，不能提交终检放行判定",
                batch_id=batch_id,
                stage=batch.get("stage"),
            )

        with self.store.locks.guard(f"release:{batch_id}"):
            document = self.decisions.get(batch_id)
            if document is not None and document.get("status") in _FROZEN:
                raise SequenceError(
                    "放行单已终态，不能再录入化验",
                    batch_id=batch_id,
                    status=document.get("status"),
                )

            recipe = self.brewing.recipes.get(str(batch["recipe_id"]))
            style = str(recipe.get("style", ""))
            spec = self.specs.resolve(str(batch["brewery_id"]), style)
            metrics_spec = list(spec.get("metrics", []))
            clean_values = self._clean_values(metrics_spec, values)

            view = self.brewing.status(batch_id)
            evidence = gather_process_evidence(
                batch,
                view,
                batch_open_alarms=self._batch_open_alarms(batch_id, str(batch["brewery_id"])),
            )
            round_number = int(document.get("round", 0)) + 1 if document else 1
            evaluation = evaluate_release(
                metrics_spec,
                clean_values,
                evidence,
                round_number=round_number,
                max_retest_rounds=MAX_RETEST_ROUNDS,
            )
            now = format_moment(self.clock.now())
            round_doc = InspectionRound(
                round=round_number,
                verdict=str(evaluation["verdict"]),
                metrics=list(evaluation["metrics"]),
                evidence=list(evaluation["evidence"]),
                submitted_by=clean_actor,
                submitted_at=now,
                laboratory=require_text(laboratory, field="laboratory", max_length=80)
                if laboratory
                else None,
                report_no=require_text(report_no, field="report_no", max_length=60)
                if report_no
                else None,
                note=note.strip() if isinstance(note, str) else "",
            ).to_doc()

            if document is None:
                document = ReleaseDecision(
                    id=new_id("release"),
                    batch_id=batch_id,
                    brewery_id=str(batch["brewery_id"]),
                    spec_id=str(spec["id"]),
                    spec_version=int(spec.get("current_version", 1)),
                    created_at=now,
                ).to_doc()

            status = {
                VERDICT_RELEASE: ReleaseStatus.RELEASED.value,
                VERDICT_RETEST: ReleaseStatus.RETEST.value,
                VERDICT_HOLD: ReleaseStatus.HELD.value,
            }[str(evaluation["verdict"])]

            document = merge_documents(
                document,
                [
                    ("status", status),
                    ("verdict", evaluation["verdict"]),
                    ("round", round_number),
                    ("rounds", list(document.get("rounds", [])) + [round_doc]),
                    ("held_reason", "；".join(evaluation["reasons"]) if status == ReleaseStatus.HELD.value else None),
                    ("held_by", clean_actor if status == ReleaseStatus.HELD.value else None),
                    ("held_at", now if status == ReleaseStatus.HELD.value else None),
                    ("released_by", clean_actor if status == ReleaseStatus.RELEASED.value else None),
                    ("released_at", now if status == ReleaseStatus.RELEASED.value else None),
                    ("release_mode", "auto" if status == ReleaseStatus.RELEASED.value else None),
                    ("updated_at", now),
                ],
            )
            events = list(document.get("events", []))
            events.append(
                {
                    "at": now,
                    "actor": clean_actor,
                    "action": f"inspection.submitted.{evaluation['verdict']}",
                    "detail": {
                        "round": round_number,
                        "report_no": round_doc["report_no"],
                        "concession_allowed": evaluation["concession_allowed"],
                        "reasons": evaluation["reasons"],
                    },
                }
            )
            document["events"] = events
            document = self.decisions.put(batch_id, document)

        self.audit.record(
            str(batch["brewery_id"]),
            batch_id,
            clean_actor,
            f"quality.inspection_{evaluation['verdict']}",
            {
                "round": round_number,
                "spec_id": spec["id"],
                "spec_version": spec.get("current_version"),
                "report_no": round_doc["report_no"],
                "reasons": evaluation["reasons"],
            },
        )
        if status == ReleaseStatus.HELD.value:
            self.alarms.raise_alarm(
                brewery_id=str(batch["brewery_id"]),
                source=f"quality:{batch_id}",
                severity="critical",
                code="release_held",
                message=f"批次 {batch.get('code')} 终检不合格，已扣留",
                context={"batch_id": batch_id, "round": round_number, "reasons": evaluation["reasons"]},
            )
        elif status == ReleaseStatus.RETEST.value:
            self.alarms.raise_alarm(
                brewery_id=str(batch["brewery_id"]),
                source=f"quality:{batch_id}",
                severity="warning",
                code="release_retest",
                message=f"批次 {batch.get('code')} 指标临界，安排复检",
                context={"batch_id": batch_id, "round": round_number},
            )
        return self.get_decision(batch_id)

    # ------------------------------------------------------------------ 扣留

    def hold(self, batch_id: str, reason: str, actor: str) -> dict[str, Any]:
        """质量员手动扣留（例如化验单存疑），必须写明原因。"""

        clean_actor = require_text(actor, field="actor", max_length=60)
        clean_reason = require_text(reason, field="reason", max_length=240)
        document = self._apply_transition(
            batch_id,
            allow_from=(
                ReleaseStatus.PENDING.value,
                ReleaseStatus.RETEST.value,
                ReleaseStatus.HELD.value,
                ReleaseStatus.CONCESSION_REQUESTED.value,
            ),
            to_status=ReleaseStatus.HELD.value,
            action="quality.hold",
            actor=clean_actor,
            detail={"reason": clean_reason},
            patch=[
                ("held_reason", clean_reason),
                ("held_by", clean_actor),
                ("concession", None),
            ],
        )
        return document

    # ------------------------------------------------------------ 让步接收

    def request_concession(
        self,
        batch_id: str,
        reason: str,
        requester: str,
        *,
        proposed_use: str = "",
    ) -> dict[str, Any]:
        """扣留后发起让步接收申请；只有允许让步的批次可以申请。"""

        clean_requester = require_text(requester, field="requester", max_length=60)
        clean_reason = require_text(reason, field="reason", max_length=240)
        clean_use = proposed_use.strip() if isinstance(proposed_use, str) else ""
        if len(clean_use) > 200:
            raise ValidationError("让步用途说明过长", length=len(clean_use))

        with self.store.locks.guard(f"release:{batch_id}"):
            document = self._require_decision(batch_id)
            if document.get("status") != ReleaseStatus.HELD.value:
                raise SequenceError(
                    "只有扣留状态的批次可以申请让步接收",
                    batch_id=batch_id,
                    status=document.get("status"),
                )
            latest = self._latest_round(document)
            if not self._concession_allowed(document, latest):
                raise ConflictError(
                    "该批次关键指标不合格或存在安全类记录，不允许让步接收",
                    batch_id=batch_id,
                )
            now = format_moment(self.clock.now())
            concession = {
                "reason": clean_reason,
                "proposed_use": clean_use,
                "requested_by": clean_requester,
                "requested_at": now,
                "status": "requested",
                "approver": None,
                "approver_role": None,
                "decided_at": None,
                "decision_note": None,
            }
            document = merge_documents(
                document,
                [
                    ("status", ReleaseStatus.CONCESSION_REQUESTED.value),
                    ("concession", concession),
                    ("updated_at", now),
                ],
            )
            document["events"] = list(document.get("events", [])) + [
                {
                    "at": now,
                    "actor": clean_requester,
                    "action": "concession.requested",
                    "detail": {"reason": clean_reason, "proposed_use": clean_use},
                }
            ]
            document = self.decisions.put(batch_id, document)

        batch = self.brewing._require_batch(batch_id)
        self.audit.record(
            str(batch["brewery_id"]),
            batch_id,
            clean_requester,
            "quality.concession_requested",
            {"reason": clean_reason, "proposed_use": clean_use},
        )
        self.alarms.raise_alarm(
            brewery_id=str(batch["brewery_id"]),
            source=f"quality:{batch_id}",
            severity="warning",
            code="concession_requested",
            message=f"批次 {batch.get('code')} 提出让步接收申请，等待审批",
            context={"batch_id": batch_id, "requested_by": clean_requester},
        )
        return self.get_decision(batch_id)

    def approve_concession(
        self,
        batch_id: str,
        approver: str,
        approver_role: str,
        *,
        note: str = "",
    ) -> dict[str, Any]:
        """质量负责人/厂长批准让步接收，批准后放行并全程留痕。"""

        clean_approver = require_text(approver, field="approver", max_length=60)
        clean_role = require_text(approver_role, field="approver_role", max_length=40)
        if clean_role not in CONCESSION_APPROVER_ROLES:
            raise ValidationError(
                "该角色无权批准让步接收",
                approver_role=clean_role,
                allowed=list(CONCESSION_APPROVER_ROLES),
            )
        clean_note = note.strip() if isinstance(note, str) else ""
        if len(clean_note) > 240:
            raise ValidationError("审批意见过长", length=len(clean_note))

        with self.store.locks.guard(f"release:{batch_id}"):
            document = self._require_decision(batch_id)
            if document.get("status") != ReleaseStatus.CONCESSION_REQUESTED.value:
                raise SequenceError(
                    "让步申请不在待审批状态",
                    batch_id=batch_id,
                    status=document.get("status"),
                )
            concession = dict(document.get("concession") or {})
            if concession.get("requested_by") == clean_approver:
                raise ConflictError("让步接收申请人不能批准自己的申请", approver=clean_approver)
            now = format_moment(self.clock.now())
            concession.update(
                {
                    "status": "approved",
                    "approver": clean_approver,
                    "approver_role": clean_role,
                    "decided_at": now,
                    "decision_note": clean_note,
                }
            )
            document = merge_documents(
                document,
                [
                    ("status", ReleaseStatus.RELEASED.value),
                    ("concession", concession),
                    ("released_by", clean_approver),
                    ("released_at", now),
                    ("release_mode", "concession"),
                    ("updated_at", now),
                ],
            )
            document["events"] = list(document.get("events", [])) + [
                {
                    "at": now,
                    "actor": clean_approver,
                    "action": "concession.approved",
                    "detail": {"approver_role": clean_role, "note": clean_note},
                }
            ]
            document = self.decisions.put(batch_id, document)

        batch = self.brewing._require_batch(batch_id)
        self.audit.record(
            str(batch["brewery_id"]),
            batch_id,
            clean_approver,
            "quality.concession_approved",
            {"approver_role": clean_role, "note": clean_note, "reason": concession.get("reason")},
        )
        return self.get_decision(batch_id)

    def reject_concession(
        self,
        batch_id: str,
        approver: str,
        approver_role: str,
        *,
        note: str,
    ) -> dict[str, Any]:
        """驳回让步申请，批次回到扣留状态。"""

        clean_approver = require_text(approver, field="approver", max_length=60)
        clean_role = require_text(approver_role, field="approver_role", max_length=40)
        if clean_role not in CONCESSION_APPROVER_ROLES:
            raise ValidationError(
                "该角色无权审批让步接收",
                approver_role=clean_role,
                allowed=list(CONCESSION_APPROVER_ROLES),
            )
        clean_note = require_text(note, field="note", max_length=240)

        with self.store.locks.guard(f"release:{batch_id}"):
            document = self._require_decision(batch_id)
            if document.get("status") != ReleaseStatus.CONCESSION_REQUESTED.value:
                raise SequenceError(
                    "让步申请不在待审批状态",
                    batch_id=batch_id,
                    status=document.get("status"),
                )
            concession = dict(document.get("concession") or {})
            now = format_moment(self.clock.now())
            concession.update(
                {
                    "status": "rejected",
                    "approver": clean_approver,
                    "approver_role": clean_role,
                    "decided_at": now,
                    "decision_note": clean_note,
                }
            )
            document = merge_documents(
                document,
                [
                    ("status", ReleaseStatus.HELD.value),
                    ("concession", concession),
                    ("updated_at", now),
                ],
            )
            document["events"] = list(document.get("events", [])) + [
                {
                    "at": now,
                    "actor": clean_approver,
                    "action": "concession.rejected",
                    "detail": {"approver_role": clean_role, "note": clean_note},
                }
            ]
            document = self.decisions.put(batch_id, document)

        batch = self.brewing._require_batch(batch_id)
        self.audit.record(
            str(batch["brewery_id"]),
            batch_id,
            clean_approver,
            "quality.concession_rejected",
            {"approver_role": clean_role, "note": clean_note},
        )
        return self.get_decision(batch_id)

    # ------------------------------------------------------------------ 报废

    def reject_batch(self, batch_id: str, reason: str, actor: str) -> dict[str, Any]:
        """扣留批次最终判废，终态不可恢复。"""

        clean_actor = require_text(actor, field="actor", max_length=60)
        clean_reason = require_text(reason, field="reason", max_length=240)
        with self.store.locks.guard(f"release:{batch_id}"):
            document = self._require_decision(batch_id)
            if document.get("status") not in (
                ReleaseStatus.HELD.value,
                ReleaseStatus.CONCESSION_REQUESTED.value,
                ReleaseStatus.RETEST.value,
            ):
                raise SequenceError(
                    "只有扣留、待让步审批或待复检的批次可以判废",
                    batch_id=batch_id,
                    status=document.get("status"),
                )
            now = format_moment(self.clock.now())
            document = merge_documents(
                document,
                [
                    ("status", ReleaseStatus.REJECTED.value),
                    ("reject_reason", clean_reason),
                    ("rejected_by", clean_actor),
                    ("rejected_at", now),
                    ("updated_at", now),
                ],
            )
            document["events"] = list(document.get("events", [])) + [
                {
                    "at": now,
                    "actor": clean_actor,
                    "action": "quality.batch_rejected",
                    "detail": {"reason": clean_reason},
                }
            ]
            document = self.decisions.put(batch_id, document)
        batch = self.brewing._require_batch(batch_id)
        self.audit.record(
            str(batch["brewery_id"]),
            batch_id,
            clean_actor,
            "quality.batch_rejected",
            {"reason": clean_reason},
        )
        self.alarms.raise_alarm(
            brewery_id=str(batch["brewery_id"]),
            source=f"quality:{batch_id}",
            severity="critical",
            code="release_rejected",
            message=f"批次 {batch.get('code')} 已判废：{clean_reason}",
            context={"batch_id": batch_id, "reason": clean_reason},
        )
        return self.get_decision(batch_id)

    # ------------------------------------------------------------------ 查询

    def get_decision(self, batch_id: str) -> dict[str, Any]:
        """返回批次放行单；尚未录入终检时返回待判定占位视图。"""

        document = self.decisions.get(batch_id)
        if document is None:
            batch = self.brewing._require_batch(batch_id)
            return {
                "batch_id": batch_id,
                "brewery_id": batch.get("brewery_id"),
                "status": ReleaseStatus.PENDING.value,
                "round": 0,
                "verdict": None,
                "rounds": [],
                "events": [],
            }
        return document

    def require_released(self, batch_id: str) -> dict[str, Any]:
        """结批联锁：未取得放行结论不允许完成批次。"""

        document = self.get_decision(batch_id)
        if document.get("status") != ReleaseStatus.RELEASED.value:
            from ..core.errors import InterlockError

            raise InterlockError(
                "批次尚未放行，禁止结批转入灌装",
                batch_id=batch_id,
                release_status=document.get("status"),
            )
        return document

    def list_decisions(self, status: str | None = None) -> list[dict[str, Any]]:
        items = self.decisions.all()
        if status:
            items = [item for item in items if item.get("status") == status]
        return sorted(items, key=lambda item: str(item.get("updated_at", "")), reverse=True)

    def preview(self, batch_id: str) -> dict[str, Any]:
        """录入化验单前先看工艺记录核对项。"""

        batch = self.brewing._require_batch(batch_id)
        view = self.brewing.status(batch_id)
        evidence = gather_process_evidence(
            batch,
            view,
            batch_open_alarms=self._batch_open_alarms(batch_id, str(batch["brewery_id"])),
        )
        recipe = self.brewing.recipes.get(str(batch["recipe_id"]))
        try:
            spec = self.specs.resolve(str(batch["brewery_id"]), str(recipe.get("style", "")))
            spec_view = {"id": spec["id"], "version": spec.get("current_version"), "metrics": spec.get("metrics")}
        except NotFoundError:
            spec_view = None
        return {
            "batch_id": batch_id,
            "stage": batch.get("stage"),
            "spec": spec_view,
            "evidence": evidence,
        }

    def summary(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for item in self.decisions.all():
            key = str(item.get("status"))
            counts[key] = counts.get(key, 0) + 1
        return {"decisions": len(self.decisions.all()), "by_status": counts}

    # ------------------------------------------------------------------ 内部

    def _apply_transition(
        self,
        batch_id: str,
        *,
        allow_from: tuple[str, ...],
        to_status: str,
        action: str,
        actor: str,
        detail: dict[str, Any],
        patch: list[tuple[str, Any]],
    ) -> dict[str, Any]:
        with self.store.locks.guard(f"release:{batch_id}"):
            document = self._require_decision(batch_id)
            if document.get("status") not in allow_from:
                raise SequenceError(
                    "放行单当前状态不允许该操作",
                    batch_id=batch_id,
                    status=document.get("status"),
                    required=list(allow_from),
                )
            now = format_moment(self.clock.now())
            document = merge_documents(
                document,
                [("status", to_status), ("updated_at", now), *patch],
            )
            document["events"] = list(document.get("events", [])) + [
                {"at": now, "actor": actor, "action": action, "detail": detail}
            ]
            document = self.decisions.put(batch_id, document)
        batch = self.brewing._require_batch(batch_id)
        self.audit.record(str(batch["brewery_id"]), batch_id, actor, action, detail)
        return self.get_decision(batch_id)

    def _require_decision(self, batch_id: str) -> dict[str, Any]:
        document = self.decisions.get(batch_id)
        if document is None:
            raise NotFoundError("批次还没有放行单，请先录入终检化验结果", batch_id=batch_id)
        return document

    def _latest_round(self, document: dict[str, Any]) -> dict[str, Any] | None:
        rounds = document.get("rounds") or []
        return rounds[-1] if rounds else None

    def _concession_allowed(self, document: dict[str, Any], latest_round: dict[str, Any] | None) -> bool:
        if latest_round is None:
            return False
        # 以最近一轮的证据与指标判定为准：关键项不合格不允许让步
        if any(item.get("critical") and item.get("result") == "fail" for item in latest_round.get("metrics", [])):
            return False
        # 安全类 / 联锁类记录缺失不允许让步；普通工艺偏差可以让步
        if any(
            item.get("status") == EVIDENCE_BLOCK and not item.get("concession_allowed")
            for item in latest_round.get("evidence", [])
        ):
            return False
        return True

    def _batch_open_alarms(self, batch_id: str, brewery_id: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for item in self.alarms.list_alarms(brewery_id=brewery_id):
            if item.get("status") == "resolved":
                continue
            source = str(item.get("source", ""))
            # 质量模块自己产生的扣留/复检告警是判定结果，不能反过来充当工艺证据
            if source.startswith("quality:"):
                continue
            context = item.get("context") or {}
            if context.get("batch_id") == batch_id or source.endswith(batch_id):
                result.append(item)
        return result

    def _clean_values(
        self, metrics_spec: list[dict[str, Any]], values: dict[str, Any]
    ) -> dict[str, float]:
        clean: dict[str, float] = {}
        for metric in metrics_spec:
            key = str(metric.get("key"))
            raw = values.get(key)
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                raise ValidationError(f"终检指标 {key} 缺少数值", field=key)
            clean[key] = float(raw)
        unknown = sorted(set(values.keys()) - set(clean.keys()))
        if unknown:
            raise ValidationError("存在规格之外的终检指标", unknown=unknown)
        return clean
