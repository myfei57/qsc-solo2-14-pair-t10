"""控制台 HTTP 接口：路由、错误映射与页面分发。"""

from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request

from .helpers import StepClock, complete_batch, create_batch, make_app


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = make_app(clock=StepClock())
        self.app.server.start()
        host, port = self.app.server.address
        self.base = f"http://{host}:{port}"
        self.thread = threading.Thread(target=self.app.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.app.server.stop()
        self.thread.join(timeout=5)
        self.app.close()

    def call(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_state_and_pages(self) -> None:
        status, overview = self.call("GET", "/api/state")
        self.assertEqual(200, status)
        self.assertIn("banner", overview)
        self.assertIn("release", overview)
        status, pages = self.call("GET", "/api/pages")
        self.assertEqual(5, len(pages["pages"]))
        self.assertGreaterEqual(len(pages["routes"]), 60)

    def test_sequence_error_maps_to_conflict(self) -> None:
        batch_id = create_batch(self.app)
        status, payload = self.call(
            "POST", f"/api/batches/{batch_id}/charge", {"grain_kg": 220, "actor": "api"}
        )
        self.assertEqual(409, status)
        self.assertEqual("sequence_violation", payload["error"])

    def test_unknown_route_returns_not_found(self) -> None:
        status, payload = self.call("GET", "/api/does-not-exist")
        self.assertEqual(404, status)
        self.assertEqual("not_found", payload["error"])

    def test_static_pages_are_served(self) -> None:
        with urllib.request.urlopen(self.base + "/mash", timeout=10) as response:
            html = response.read().decode("utf-8")
        self.assertIn("糖化控制", html)
        with urllib.request.urlopen(self.base + "/static/app.js", timeout=10) as response:
            script = response.read().decode("utf-8")
        self.assertIn("initMashPage", script)


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


class ReleaseApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = make_app(clock=StepClock())
        self.app.server.start()
        host, port = self.app.server.address
        self.base = f"http://{host}:{port}"
        self.thread = threading.Thread(target=self.app.server.serve_forever, daemon=True)
        self.thread.start()
        self.batch_id = create_batch(self.app)
        complete_batch(self.app, self.batch_id)

    def tearDown(self) -> None:
        self.app.server.stop()
        self.thread.join(timeout=5)
        self.app.close()

    def call(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_release_and_concession_flow_over_http(self) -> None:
        # 关键指标不合格：扣留且不允许让步
        critical = [dict(item) for item in PASSING_METRICS]
        for item in critical:
            if item["name"] == "ph":
                item["value"] = 5.2
        status, view = self.call(
            "POST", f"/api/batches/{self.batch_id}/lab-report",
            {"metrics": critical, "actor": "lab-li"},
        )
        self.assertEqual(200, status)
        self.assertEqual("held", view["decision"]["status"])

        status, payload = self.call(
            "POST", f"/api/batches/{self.batch_id}/concession",
            {"actor": "qa-zhao", "reason": "试试", "proposed_disposition": "降级"},
        )
        self.assertEqual(409, status)
        self.assertEqual("conflict", payload["error"])

        # 非关键指标不合格：扣留 → 让步申请 → 他人审批 → 让步放行
        non_critical = [dict(item) for item in PASSING_METRICS]
        for item in non_critical:
            if item["name"] == "ph":
                item["value"] = 4.3
            if item["name"] == "ibu":
                item["value"] = 55.0
        status, view = self.call(
            "POST", f"/api/batches/{self.batch_id}/lab-report",
            {"metrics": non_critical, "actor": "lab-li"},
        )
        self.assertEqual("held", view["decision"]["status"])
        status, view = self.call(
            "POST", f"/api/batches/{self.batch_id}/concession",
            {"actor": "qa-zhao", "reason": "苦味偏高客户接受", "proposed_disposition": "定向销售"},
        )
        self.assertEqual(200, status)
        concession_id = view["concessions"][-1]["id"]

        status, payload = self.call(
            "POST", f"/api/quality/concessions/{concession_id}/review",
            {"approver": "qa-zhao", "approve": True, "note": "自批"},
        )
        self.assertEqual(409, status)

        status, view = self.call(
            "POST", f"/api/quality/concessions/{concession_id}/review",
            {"approver": "manager-qian", "approve": True, "note": "同意，限定渠道"},
        )
        self.assertEqual(200, status)
        self.assertEqual("concession_released", view["decision"]["status"])

        status, view = self.call("GET", f"/api/batches/{self.batch_id}/release")
        self.assertEqual(200, status)
        actions = set(view["audit"]["actions"])
        self.assertIn("qc.lab_submitted", actions)
        self.assertIn("qc.concession_released", actions)

    def test_queue_and_decisions_endpoints(self) -> None:
        status, payload = self.call("GET", "/api/quality/queue")
        self.assertEqual(200, status)
        self.assertIn("by_status", payload)
        status, payload = self.call("GET", "/api/quality/decisions")
        self.assertEqual(200, status)
        self.assertEqual([], payload["decisions"])
