"""成品放行判定规则：指标分级、工艺偏差与让步边界。"""

from __future__ import annotations

import unittest

from breweryctl.domain.models import ReleaseStatus
from breweryctl.domain.quality import (
    METRIC_FAIL,
    METRIC_MISSING,
    METRIC_PASS,
    METRIC_BORDERLINE,
    PROCESS_BLOCKER,
    PROCESS_DEVIATION,
    concession_allowed,
    default_spec_metrics,
    evaluate,
)

SPECS = default_spec_metrics()

PASSING_METRICS = {
    "alcohol_abv": 5.0,
    "original_extract": 12.0,
    "co2": 0.58,
    "ibu": 35.0,
    "ph": 4.3,
    "apparent_attenuation": 80.0,
    "turbidity": 0.5,
    "microbial": 0.0,
}


class EvaluateTest(unittest.TestCase):
    def test_all_metrics_in_band_releases(self) -> None:
        result = evaluate(SPECS, dict(PASSING_METRICS), [])
        self.assertEqual(ReleaseStatus.RELEASED.value, result["suggestion"])
        self.assertTrue(all(item["result"] == METRIC_PASS for item in result["metric_results"]))
        self.assertEqual([], result["reasons"])

    def test_missing_metric_holds(self) -> None:
        metrics = dict(PASSING_METRICS)
        del metrics["ibu"]
        result = evaluate(SPECS, metrics, [])
        self.assertEqual(ReleaseStatus.HELD.value, result["suggestion"])
        names = [item["name"] for item in result["metric_results"] if item["result"] == METRIC_MISSING]
        self.assertIn("ibu", names)

    def test_non_critical_failure_holds_but_concession_possible(self) -> None:
        metrics = dict(PASSING_METRICS, ibu=55.0)
        result = evaluate(SPECS, metrics, [])
        self.assertEqual(ReleaseStatus.HELD.value, result["suggestion"])
        failed = [item for item in result["metric_results"] if item["result"] == METRIC_FAIL]
        self.assertEqual(["ibu"], [item["name"] for item in failed])
        self.assertTrue(concession_allowed(result))

    def test_critical_failure_blocks_concession(self) -> None:
        metrics = dict(PASSING_METRICS, ph=5.1)
        result = evaluate(SPECS, metrics, [])
        self.assertEqual(ReleaseStatus.HELD.value, result["suggestion"])
        self.assertFalse(concession_allowed(result))

        micro = dict(PASSING_METRICS, microbial=1.0)
        micro_result = evaluate(SPECS, micro, [])
        self.assertEqual(ReleaseStatus.HELD.value, micro_result["suggestion"])
        self.assertFalse(concession_allowed(micro_result))

    def test_borderline_value_triggers_retest(self) -> None:
        # IBU 带 25~45，20% 边界带为 ±4；42.0 贴上限
        metrics = dict(PASSING_METRICS, ibu=42.0)
        result = evaluate(SPECS, metrics, [])
        self.assertEqual(ReleaseStatus.RETEST.value, result["suggestion"])
        borderline = [
            item["name"] for item in result["metric_results"] if item["result"] == METRIC_BORDERLINE
        ]
        self.assertEqual(["ibu"], borderline)
        # 复检建议状态下谈不上让步
        self.assertFalse(concession_allowed(result))

    def test_process_deviation_triggers_retest(self) -> None:
        findings = [
            {"name": "hop_window", "result": PROCESS_DEVIATION, "message": "1 次酒花错过窗口"}
        ]
        result = evaluate(SPECS, dict(PASSING_METRICS), findings)
        self.assertEqual(ReleaseStatus.RETEST.value, result["suggestion"])

    def test_process_blocker_holds_even_when_metrics_pass(self) -> None:
        findings = [
            {"name": "open_alarms", "result": PROCESS_BLOCKER, "message": "严重告警未关闭"}
        ]
        result = evaluate(SPECS, dict(PASSING_METRICS), findings)
        self.assertEqual(ReleaseStatus.HELD.value, result["suggestion"])
        self.assertFalse(concession_allowed(result))

    def test_concession_requires_actual_held_failure(self) -> None:
        passing = evaluate(SPECS, dict(PASSING_METRICS), [])
        self.assertFalse(concession_allowed(passing))


if __name__ == "__main__":
    unittest.main()
