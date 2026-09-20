"""成品放行工作流：化验单判定、放行/扣留/复检与让步审批留痕。"""

from __future__ import annotations



import unittest

from breweryctl.core.errors import ConflictError, SequenceError, ValidationError

from .helpers import StepClock, complete_batch, create_batch, make_app

PASSING_METRICS = [
    {"name": "alcohol_abv", "value": 5.0},
    {"name": "original_extract", "value": 12.0},
    {"name": "co2", "value": 0.58},
    {"name": "ibu", "value": 35.0},
    {"name": "ph", "value": 4.3},
    {"name": "apparent_attenuation", "value": 80.0},
    {"name": "turbidity", "value": 0.5},
    {"name": "microbial", "value": 0.0},
]


def metrics(**overrides: float) -> list[dict[str, float | str]]:
    values = {item["name"]: item["value"] for item in PASSING_METRICS}
    values.update(overrides)
    return [{"name": name, "value": value} for name, value in values.items()]


class ReleaseWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = StepClock()
        self.app = make_app(clock=self.clock)
        self.release = self.app.registry.release
        self.batch_id = create_batch(self.app)
        complete_batch(self.app, self.batch_id)

    def test_release_requires_completed_batch(self) -> None:
        fresh = create_batch(self.app)
        with self.assertRaises(SequenceError):
            self.release.submit_lab_report(fresh, PASSING_METRICS, "lab")

    def test_passing_report_can_be_released_and_audited(self) -> None:
        view = self.release.submit_lab_report(self.batch_id, PASSING_METRICS, "lab-li")
        self.assertEqual("pending", view["decision"]["status"])
        self.assertEqual("released", view["decision"]["suggested"])

        finalized = self.release.release(self.batch_id, "qc-wang", "符合标准")
        decision = finalized["decision"]
        self.assertEqual("released", decision["status"])
        self.assertEqual("qc-wang", decision["decided_by"])
        self.assertIsNotNone(decision["released_at"])

        audit = finalized["audit"]
        actions = set(audit["actions"])
        self.assertIn("qc.lab_submitted", actions)
        self.assertIn("qc.released", actions)

    def test_release_without_lab_report_is_rejected(self) -> None:
        with self.assertRaises(ConflictError):
            self.release.release(self.batch_id, "qc-wang")

    def test_non_critical_failure_holds_and_blocks_direct_release(self) -> None:
        view = self.release.submit_lab_report(
            self.batch_id, metrics(ibu=55.0), "lab-li"
        )
        self.assertEqual("held", view["decision"]["status"])
        with self.assertRaises(ConflictError):
            self.release.release(self.batch_id, "qc-wang")

    def test_critical_failure_cannot_be_conceded(self) -> None:
        self.release.submit_lab_report(self.batch_id, metrics(ph=5.2), "lab-li")
        with self.assertRaises(ConflictError):
            self.release.request_concession(
                self.batch_id, "qa-zhao", "口感可接受", "降级为调味酒"
            )

    def test_concession_requires_separate_approver(self) -> None:
        self.release.submit_lab_report(self.batch_id, metrics(ibu=55.0), "lab-li")
        self.release.request_concession(
            self.batch_id, "qa-zhao", "苦味略高，客户可接受", "定向渠道销售"
        )
        pending = self.release.list_concessions("pending")
        self.assertEqual(1, len(pending))
        concession_id = pending[0]["id"]

        # 申请人不能自批
        with self.assertRaises(ConflictError):
            self.release.review_concession(concession_id, "qa-zhao", True, "同意")

        finalized = self.release.review_concession(
            concession_id, "manager-qian", True, "限定渠道并加贴标识"
        )
        self.assertEqual("concession_released", finalized["decision"]["status"])
        self.assertEqual(concession_id, finalized["decision"]["concession_id"])
        self.assertEqual("manager-qian", finalized["decision"]["decided_by"])

        audit_actions = finalized["audit"]["actions"]
        self.assertIn("qc.concession_requested", audit_actions)
        self.assertIn("qc.concession_reviewed", audit_actions)
        self.assertIn("qc.concession_released", audit_actions)

    def test_concession_rejection_keeps_batch_held(self) -> None:
        self.release.submit_lab_report(self.batch_id, metrics(ibu=55.0), "lab-li")
        self.release.request_concession(self.batch_id, "qa-zhao", "理由", "降级")
        concession_id = self.release.list_concessions("pending")[0]["id"]
        view = self.release.review_concession(concession_id, "manager-qian", False, "不同意")
        self.assertEqual("held", view["decision"]["status"])
        self.assertEqual("rejected", view["concessions"][0]["status"])
        with self.assertRaises(ConflictError):
            self.release.review_concession(concession_id, "manager-qian", True, "再次同意")

    def test_retest_path_releases_after_clean_report(self) -> None:
        # IBU 带 25~45，20% 边界带为 ±4；42.0 贴上限触发复检
        view = self.release.submit_lab_report(
            self.batch_id, metrics(ibu=42.0), "lab-li"
        )
        self.assertEqual("retest", view["decision"]["status"])
        with self.assertRaises(ConflictError):
            self.release.release(self.batch_id, "qc-wang")

        self.clock.advance(120)
        view = self.release.submit_lab_report(self.batch_id, metrics(ibu=35.0), "lab-li")
        self.assertEqual("pending", view["decision"]["status"])
        self.assertEqual(2, len(view["lab_reports"]))
        finalized = self.release.release(self.batch_id, "qc-wang")
        self.assertEqual("released", finalized["decision"]["status"])

    def test_retest_can_escalate_to_hold_then_concession(self) -> None:
        self.release.submit_lab_report(self.batch_id, metrics(ibu=42.0), "lab-li")
        # 复检确认不合格：扣留，随后可走让步
        view = self.release.submit_lab_report(
            self.batch_id, metrics(ibu=48.0), "lab-li"
        )
        self.assertEqual("held", view["decision"]["status"])
        self.release.request_concession(self.batch_id, "qa-zhao", "复检苦味偏高", "定向销售")
        concession_id = self.release.list_concessions("pending")[0]["id"]
        finalized = self.release.review_concession(
            concession_id, "manager-qian", True, "同意让步"
        )
        self.assertEqual("concession_released", finalized["decision"]["status"])

    def test_manual_hold_is_recorded(self) -> None:
        self.release.submit_lab_report(self.batch_id, PASSING_METRICS, "lab-li")
        view = self.release.hold(self.batch_id, "qc-wang", "留样气味异常，待调查")
        self.assertEqual("held", view["decision"]["status"])
        with self.assertRaises(ConflictError):
            self.release.release(self.batch_id, "qc-wang")

    def test_reject_is_terminal_and_audited(self) -> None:
        self.release.submit_lab_report(self.batch_id, metrics(ph=5.2), "lab-li")
        view = self.release.reject(self.batch_id, "manager-qian", "微生物相关风险，报废")
        self.assertEqual("rejected", view["decision"]["status"])
        with self.assertRaises(ConflictError):
            self.release.hold(self.batch_id, "qc-wang", "再想想")
        with self.assertRaises(ConflictError):
            self.release.submit_lab_report(self.batch_id, PASSING_METRICS, "lab-li")

    def test_duplicate_metric_in_report_is_invalid(self) -> None:
        duplicated = PASSING_METRICS + [{"name": "ibu", "value": 30.0}]
        with self.assertRaises(ValidationError):
            self.release.submit_lab_report(self.batch_id, duplicated, "lab-li")

    def test_unresolved_process_alarm_blocks_concession(self) -> None:
        batch = self.app.registry.brewing._require_batch(self.batch_id)
        self.app.registry.alarms.raise_alarm(
            brewery_id=str(batch["brewery_id"]),
            source=f"ferment:{batch.get('tank_id')}",
            severity="critical",
            code="pressure_overrun",
            message="发酵超压联锁动作",
            context={"batch_id": self.batch_id},
        )
        self.release.submit_lab_report(self.batch_id, metrics(ibu=55.0), "lab-li")
        with self.assertRaises(ConflictError):
            self.release.request_concession(self.batch_id, "qa-zhao", "理由", "降级")
        # 工艺告警处置闭环后可以重新判定
        alarms = [
            item
            for item in self.app.registry.alarms.list_alarms(status="active")
            if not str(item.get("source", "")).startswith("qc:")
        ]
        for alarm in alarms:
            self.app.registry.alarms.acknowledge(str(alarm["id"]), "op")
            self.app.registry.alarms.resolve(str(alarm["id"]), "op", "已检修")
        view = self.release.submit_lab_report(self.batch_id, metrics(ibu=55.0), "lab-li")
        findings = {item["name"]: item["result"] for item in view["decision"]["process_findings"]}
        self.assertNotIn("open_alarms", findings)


if __name__ == "__main__":
    unittest.main()
