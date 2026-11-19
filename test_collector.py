import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from collector import main, scenario_records


class CollectorTest(unittest.TestCase):
    def test_campaign_retries_same_submission_and_skips_finished_case(self):
        import os

        old = Path.cwd()
        with tempfile.TemporaryDirectory() as directory:
            try:
                os.chdir(directory)
                Path("test-cases.json").write_text(
                    json.dumps(
                        [
                            {
                                "id": "baseline",
                                "scenario": "normal",
                                "question": "Calculate mean.",
                            }
                        ]
                    ),
                    encoding="utf-8",
                )
                args = [
                    "collector",
                    "collect",
                    "--campaign",
                    "test",
                    "--cases",
                    "test-cases.json",
                ]
                keys = []
                run_id = str(uuid4())

                def request(self, case, method, path, body=None):
                    if path.endswith("/runs"):
                        keys.append(body["idempotency_key"])
                        if len(keys) == 1:
                            raise TimeoutError
                        return {"run_id": run_id}
                    if path == "/v1/conversations":
                        return {"conversation_id": str(uuid4())}
                    if path.startswith("/v1/datasets?"):
                        return {"dataset_id": str(uuid4())}
                    if method == "GET":
                        return {"lifecycle": "finished", "outcome": "completed"}

                with (
                    patch.object(sys, "argv", args),
                    patch.dict(os.environ, {"DATALENS_ACCESS_TOKEN": "test"}),
                    patch("collector.Capture.request", request),
                ):
                    with self.assertRaises(TimeoutError):
                        main()
                    main()
                    main()
                self.assertEqual(len(keys), 2)
                self.assertEqual(keys[0], keys[1])
                db = sqlite3.connect("captures/collector.sqlite")
                try:
                    result = db.execute("SELECT run_id, outcome FROM runs").fetchone()
                finally:
                    db.close()
                self.assertEqual(result, (run_id, "completed"))
            finally:
                os.chdir(old)

    def test_scenario_link_and_policy_label_follow_observed_denial(self):
        trace = [
            ("session.started", {}),
            ("tool_call.requested", {"tool_call_id": "call"}),
            ("tool_call.failed", {
                "tool_call_id": "call",
                "policy_evaluation": {"status": "denied"},
            }),
            ("session.ended", {}),
        ]
        events = [
            dict(event_type=kind, data=data, sequence=i, event_id=str(i))
            for i, (kind, data) in enumerate(trace, 1)
        ]
        rows = list(scenario_records(events, "example", "prompt_injected"))
        self.assertEqual(rows[0]["event"]["data"]["scenario_id"], "example")
        self.assertEqual(rows[0]["event"]["data"]["attack_type"], "direct_prompt_injection")
        self.assertEqual(rows[0]["label"], "policy_blocked")
        self.assertEqual(rows[0]["policy_denied_event_ids"], ["3"])
        self.assertEqual(rows[2]["event"]["data"]["session_call_index"], 1)
        trace[2][1].clear()
        trace[2][1]["tool_call_id"] = "call"
        self.assertEqual(
            list(scenario_records(events, "example", "prompt_injected"))[0]["label"],
            "insufficient_evidence",
        )
