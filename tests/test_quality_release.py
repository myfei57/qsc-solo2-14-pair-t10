"""成品放行：终检判定、复检、扣留、让步审批与结批联锁。"""

from __future__ import annotations

import copy
import unittest

from breweryctl.core.errors import (
    ConflictError,
    InterlockError,
    SequenceError,
    ValidationError,
)
from breweryctl.domain.models import ReleaseStatus
from breweryctl.domain.quality import (
    EVIDENCE_BLOCK,
    METRIC_FAIL,
    METRIC_PASS,
    METRIC_RETEST,
    evaluate_metric,
    evaluate_release,
)

from .helpers import PASSING_METRICS, brew_to_mature, make_app


class EvaluateMetricTest(unittest.TestCase):
    def setUp(self) -> None:
        self.metric = {
            "key": "ph",
            "label": "pH",
            "unit": "",
            "lower": 3.8,
            "upper": 4.6,
            "retest_band": 0.1,
            "critical": False,
        }

    def test_in_range_passes(self) -> None:
        self.assertEqual(METRIC_PASS, evaluate_metric(self.metric, 4.2)["result"])

    def test_retest_band_marks_retest(self) -> None:
        result = evaluate_metric(self.metric, 4.65)
        self.assertEqual(METRIC_RETEST, result["result"])

    def test_far_out_is_fail(self) -> None:
        self.assertEqual(METRIC_FAIL, evaluate_metric(self.metric, 5.2)["result"])

    def test_critical_metric_skips_retest_band(self) -> None:
        critical = dict(self.metric, critical=True)
        self.assertEqual(METRIC_FAIL, evaluate_metric(critical, 4.65)["result"])


class EvaluateReleaseTest(unittest.TestCase):
    def _spec(self) -> list[dict[str, object]]:
        return [
            {"key": "fg", "label": "FG", "unit": "", "lower": 1.006, "upper": 1.018, "retest_band": 0.002, "critical": False},
            {"key": "diacetyl", "label": "双乙酰", "unit": "", "lower": None, "upper": 0.15, "retest_band": 0.03, "critical": True},
        ]

    def test_all_pass_releases(self) -> None:
        result = evaluate_release(self._spec(), {"fg": 1.012, "diacetyl": 0.08}, [])
        self.assertEqual("release", result["verdict"])
        self.assertFalse(result["concession_allowed"])

    def test_marginal_metric_requests_retest(self) -> None:
        result = evaluate_release(self._spec(), {"fg": 1.019, "diacetyl": 0.08}, [])
        self.assertEqual("retest", result["verdict"])

    def test_retest_band_exhausted_becomes_hold_with_concession(self) -> None:
        result = evaluate_release(
            self._spec(), {"fg": 1.019, "diacetyl": 0.08}, [], round_number=3, max_retest_rounds=3
        )
        self.assertEqual("hold", result["verdict"])
        self.assertTrue(result["concession_allowed"])

    def test_non_critical_fail_allows_concession(self) -> None:
        result = evaluate_release(self._spec(), {"fg": 1.03, "diacetyl": 0.08}, [])
        self.assertEqual("hold", result["verdict"])
        self.assertTrue(result["concession_allowed"])

    def test_critical_fail_never_allows_concession(self) -> None:
        result = evaluate_release(self._spec(), {"fg": 1.012, "diacetyl": 0.3}, [])
        self.assertEqual("hold", result["verdict"])
        self.assertFalse(result["concession_allowed"])

    def test_safety_block_never_allows_concession(self) -> None:
        evidence = [
            {"check": "cip", "status": EVIDENCE_BLOCK, "message": "缺清洗凭证", "critical": True}
        ]
        result = evaluate_release(self._spec(), {"fg": 1.012, "diacetyl": 0.08}, evidence)
        self.assertEqual("hold", result["verdict"])
        self.assertFalse(result["concession_allowed"])

    def test_process_deviation_holds_and_allows_concession(self) -> None:
        evidence = [
            {
                "check": "hop_additions",
                "status": "deviation",
                "message": "2 次酒花错过投放窗口",
                "concession_allowed": True,
            }
        ]
        result = evaluate_release(self._spec(), {"fg": 1.012, "diacetyl": 0.08}, evidence)
        self.assertEqual("hold", result["verdict"])
        self.assertTrue(result["concession_allowed"])

    def test_missing_metric_raises(self) -> None:
        with self.assertRaises(ValidationError):
            evaluate_release(self._spec(), {"fg": 1.012}, [])


class QualityReleaseFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = make_app()
        self.quality = self.app.registry.quality
        self.brewing = self.app.registry.brewing
        self.batch_id, self.tank_id = brew_to_mature(self.app)

    def _submit(self, values: dict[str, float] | None = None, **kwargs: object) -> dict:
        return self.quality.submit_inspection(
            self.batch_id,
            values or copy.deepcopy(PASSING_METRICS),
            "qa-li",
            report_no="COA-1",
            **kwargs,
        )

    def test_passing_metrics_release_and_unlock_completion(self) -> None:
        decision = self._submit()
        self.assertEqual(ReleaseStatus.RELEASED.value, decision["status"])
        self.assertEqual("auto", decision["release_mode"])
        self.assertEqual(1, decision["rounds"][0]["round"])
        view = self.brewing.status(self.batch_id)
        self.assertEqual("released", view["release"]["status"])
        completed = self.brewing.complete_batch(self.batch_id, "qa-li")
        self.assertEqual("completed", completed["batch"]["stage"])

    def test_unreleased_batch_cannot_be_completed(self) -> None:
        with self.assertRaises(InterlockError):
            self.brewing.complete_batch(self.batch_id, "qa-li")

    def test_marginal_metric_triggers_retest_then_release(self) -> None:
        values = dict(PASSING_METRICS, fg=1.019)  # 超上限 0.001，落在复检带 0.002
        decision = self._submit(values)
        self.assertEqual(ReleaseStatus.RETEST.value, decision["status"])
        second = self.quality.submit_inspection(
            self.batch_id, PASSING_METRICS, "qa-li", report_no="COA-2"
        )
        self.assertEqual(ReleaseStatus.RELEASED.value, second["status"])
        self.assertEqual(2, second["round"])
        self.assertEqual([1, 2], [item["round"] for item in second["rounds"]])

    def test_manual_hold_before_inspection_is_rejected(self) -> None:
        from breweryctl.core.errors import NotFoundError

        with self.assertRaises(NotFoundError):
            self.quality.hold(self.batch_id, "化验单未到", "qa-li")

    def test_concession_workflow_for_non_critical_failure(self) -> None:
        values = dict(PASSING_METRICS, ph=5.0)  # 明显超限但非关键
        decision = self._submit(values)
        self.assertEqual(ReleaseStatus.HELD.value, decision["status"])
        with self.assertRaises(ConflictError):
            self.brewing.complete_batch(self.batch_id, "qa-li")

        requested = self.quality.request_concession(
            self.batch_id, "pH 偏高但感官正常", "qa-li", proposed_use="降级内部品鉴"
        )
        self.assertEqual(ReleaseStatus.CONCESSION_REQUESTED.value, requested["status"])

        with self.assertRaises(ValidationError):
            self.quality.approve_concession(
                self.batch_id, "boss", "foreman", note="同意"
            )
        with self.assertRaises(ConflictError):
            # 申请人不能自批
            self.quality.approve_concession(
                self.batch_id, "qa-li", "qa_manager", note="同意"
            )
        approved = self.quality.approve_concession(
            self.batch_id, "manager-wang", "qa_manager", note="限内部使用"
        )
        self.assertEqual(ReleaseStatus.RELEASED.value, approved["status"])
        self.assertEqual("concession", approved["release_mode"])
        concession = approved["concession"]
        self.assertEqual("approved", concession["status"])
        self.assertEqual("manager-wang", concession["approver"])
        completed = self.brewing.complete_batch(self.batch_id, "qa-li")
        self.assertEqual("completed", completed["batch"]["stage"])

    def test_reject_concession_returns_to_held(self) -> None:
        values = dict(PASSING_METRICS, ph=5.0)
        self._submit(values)
        self.quality.request_concession(self.batch_id, "偏差说明", "qa-li")
        rejected = self.quality.reject_concession(
            self.batch_id, "manager-wang", "qa_manager", note="不接受"
        )
        self.assertEqual(ReleaseStatus.HELD.value, rejected["status"])
        self.assertEqual("rejected", rejected["concession"]["status"])

    def test_critical_failure_cannot_request_concession(self) -> None:
        values = dict(PASSING_METRICS, diacetyl=0.3)
        decision = self._submit(values)
        self.assertEqual(ReleaseStatus.HELD.value, decision["status"])
        with self.assertRaises(ConflictError):
            self.quality.request_concession(self.batch_id, "想让步", "qa-li")

    def test_reject_goods_is_terminal(self) -> None:
        values = dict(PASSING_METRICS, diacetyl=0.3)
        self._submit(values)
        rejected = self.quality.reject_batch(self.batch_id, "双乙酰严重超标，判废", "qa-li")
        self.assertEqual(ReleaseStatus.REJECTED.value, rejected["status"])
        with self.assertRaises(SequenceError):
            self._submit()
        with self.assertRaises(SequenceError):
            self.quality.hold(self.batch_id, "再扣一次", "qa-li")

    def test_inspection_requires_mature_batch(self) -> None:
        app = make_app()
        from .helpers import create_batch

        young = create_batch(app)
        with self.assertRaises(SequenceError):
            app.registry.quality.submit_inspection(young, PASSING_METRICS, "qa-li")

    def test_audit_trail_records_every_step(self) -> None:
        values = dict(PASSING_METRICS, ph=5.0)
        self._submit(values)
        self.quality.request_concession(self.batch_id, "偏差", "qa-li")
        self.quality.approve_concession(self.batch_id, "manager-wang", "qa_manager", note="ok")
        export = self.brewing.audit_history(self.batch_id)
        actions = [entry["action"] for entry in export["entries"]]
        self.assertIn("quality.inspection_hold", actions)
        self.assertIn("quality.concession_requested", actions)
        self.assertIn("quality.concession_approved", actions)
        decision = self.quality.get_decision(self.batch_id)
        event_actions = [event["action"] for event in decision["events"]]
        self.assertEqual("concession.approved", event_actions[-1])

    def test_manual_hold_records_reason(self) -> None:
        self._submit(dict(PASSING_METRICS, fg=1.019))  # retest
        held = self.quality.hold(self.batch_id, "复检前化验单存疑", "qa-li")
        self.assertEqual(ReleaseStatus.HELD.value, held["status"])
        self.assertEqual("复检前化验单存疑", held["held_reason"])

    def test_process_deviation_routes_to_concession(self) -> None:
        brewing = self.brewing
        # 成熟批次上人为挂一条批次相关 warning 告警，模拟未关闭的工艺异常
        self.app.registry.alarms.raise_alarm(
            brewery_id=str(brewing._require_batch(self.batch_id)["brewery_id"]),
            source=f"hop:{self.batch_id}",
            severity="warning",
            code="hop_window_missed",
            message="酒花错过窗口",
            context={"batch_id": self.batch_id},
        )
        decision = self._submit()
        self.assertEqual(ReleaseStatus.HELD.value, decision["status"])
        approved = self.quality.request_concession(
            self.batch_id, "酒花漏投，风味评估可接受", "qa-li", proposed_use="正常灌装"
        )
        self.assertEqual(ReleaseStatus.CONCESSION_REQUESTED.value, approved["status"])
        approved = self.quality.approve_concession(
            self.batch_id, "manager-wang", "plant_manager", note="附感官评估记录"
        )
        self.assertEqual(ReleaseStatus.RELEASED.value, approved["status"])
        self.assertEqual("concession", approved["release_mode"])


if __name__ == "__main__":
    unittest.main()
