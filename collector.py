"""Capture and export real DataLens cases."""

import argparse
import csv
import io
import json
import os
import sqlite3
import sys
import time
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID, uuid4


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def cases_from(path):
    cases = json.loads(path.read_text(encoding="utf-8"))
    ids = set()
    for case in cases:
        if (
            set(case) != {"id", "scenario", "question"}
            or not isinstance(case["id"], str)
            or not case["id"].strip()
            or case["id"] in ids
            or case["scenario"] not in {"normal", "prompt_injected"}
            or not isinstance(case["question"], str)
            or not 1 <= len(case["question"].strip()) <= 8192
        ):
            raise ValueError("Invalid scenario manifest")
        ids.add(case["id"])
    return cases


class Capture:
    def __init__(self, db, base_url, token, campaign):
        url = urlsplit(base_url)
        loopback = url.hostname in {"localhost", "127.0.0.1", "::1"}
        if (
            (url.scheme != "https" and not (url.scheme == "http" and loopback))
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path not in {"", "/"}
        ):
            raise ValueError("Supply an HTTPS API origin or a loopback HTTP origin")
        self.db = db
        self.origin = base_url.rstrip("/")
        self.token = token
        self.campaign = campaign
        self.opener = build_opener(NoRedirect)

    def request(self, case_id, method, path, body=None):
        csv_body = isinstance(body, bytes)
        payload = (
            body
            if csv_body
            else json.dumps(body).encode()
            if body is not None
            else None
        )
        headers = {"Authorization": f"Bearer {self.token}"}
        if payload is not None:
            headers["Content-Type"] = "text/csv" if csv_body else "application/json"
        request = Request(
            self.origin + path, data=payload, headers=headers, method=method
        )
        try:
            response = self.opener.open(request, timeout=20)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read(1024 * 1024 + 1)
            status = response.code
        if len(raw) > 1024 * 1024:
            raise RuntimeError("API response exceeded capture limit")
        result = (
            json.loads(raw) if raw and raw.lstrip().startswith((b"{", b"[")) else None
        )
        recorded_body = body.decode("utf-8") if csv_body else body
        with self.db:
            self.db.execute(
                "INSERT INTO exchanges VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    self.campaign,
                    case_id,
                    method,
                    path,
                    json.dumps(recorded_body),
                    status,
                    json.dumps(result),
                ),
            )
        if not 200 <= status < 300:
            raise RuntimeError(f"{method} {path.split('?')[0]} returned HTTP {status}")
        return result

    def collect(self, case, csv_bytes, poll_seconds):
        case_id = case["id"]
        key = (self.campaign, case_id)
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO runs (campaign, case_id, scenario, question, request_key)"
                " VALUES (?, ?, ?, ?, ?)",
                (*key, case["scenario"], case["question"], str(uuid4())),
            )
        row = self.db.execute(
            "SELECT * FROM runs WHERE campaign=? AND case_id=?", key
        ).fetchone()
        dataset, conversation, run = (
            row["dataset_id"],
            row["conversation_id"],
            row["run_id"],
        )
        if row["outcome"] is not None:
            return
        if dataset is None:
            path = "/v1/datasets?" + urlencode({"filename": "experiment.csv"})
            dataset = str(
                UUID(self.request(case_id, "POST", path, csv_bytes)["dataset_id"])
            )
            with self.db:
                self.db.execute(
                    "UPDATE runs SET dataset_id=? WHERE campaign=? AND case_id=?",
                    (dataset, *key),
                )
        if conversation is None:
            conversation = str(
                UUID(
                    self.request(
                        case_id, "POST", "/v1/conversations", {"title": case_id}
                    )["conversation_id"]
                )
            )
            with self.db:
                self.db.execute(
                    "UPDATE runs SET conversation_id=? WHERE campaign=? AND case_id=?",
                    (conversation, *key),
                )
        if run is None:
            path = f"/v1/conversations/{conversation}"
            selection = {"dataset_ids": [dataset]}
            self.request(case_id, "PUT", path + "/attachments", selection)
            run = str(
                UUID(
                    self.request(
                        case_id,
                        "POST",
                        path + "/runs",
                        {
                            "idempotency_key": row["request_key"],
                            "question": case["question"],
                            "selection": selection,
                        },
                    )["run_id"]
                )
            )
            with self.db:
                self.db.execute(
                    "UPDATE runs SET run_id=? WHERE campaign=? AND case_id=?",
                    (run, *key),
                )
        deadline = time.monotonic() + poll_seconds
        while time.monotonic() < deadline:
            response = self.request(case_id, "GET", f"/v1/runs/{run}")
            if response["lifecycle"] == "finished":
                for result in response.get("results", []):
                    result_id = UUID(result["tool_call_id"])
                    self.request(case_id, "GET", f"/v1/runs/{run}/results/{result_id}")
                with self.db:
                    self.db.execute(
                        "UPDATE runs SET outcome=? WHERE campaign=? AND case_id=?",
                        (response["outcome"], *key),
                    )
                print(f"{case_id}: {run} {response['outcome']}")
                return
            time.sleep(2)
        raise RuntimeError(
            f"{case_id}: polling timed out; rerun the same campaign to resume"
        )


def scenario_records(events, case_id, scenario, *, label=None):
    """Add research metadata and policy evidence to an ordered session trace."""
    if (
        not events
        or events[0]["event_type"] != "session.started"
        or events[-1]["event_type"] != "session.ended"
        or [event["sequence"] for event in events] != list(range(1, len(events) + 1))
        or len({event["event_id"] for event in events}) != len(events)
    ):
        raise RuntimeError(f"{case_id}: invalid session boundaries or event sequence")

    metadata = {
        "scenario_id": case_id,
        "category": scenario,
        "attack_type": "direct_prompt_injection" if scenario == "prompt_injected" else None,
    }
    requested = {}
    finished = set()
    denied = []
    for event in events:
        data = event["data"]
        kind = event["event_type"]
        if kind == "tool_call.requested":
            call_id = data["tool_call_id"]
            if call_id in requested:
                raise RuntimeError(f"{case_id}: duplicate tool call request")
            requested[call_id] = len(requested) + 1
        elif kind in ("tool_call.completed", "tool_call.failed"):
            call_id = data["tool_call_id"]
            if call_id not in requested or call_id in finished:
                raise RuntimeError(f"{case_id}: unexpected tool call outcome")
            finished.add(call_id)
            evaluation = data.get("policy_evaluation") or {}
            if evaluation.get("status") == "denied":
                denied.append(event["event_id"])
    if requested.keys() != finished:
        raise RuntimeError(f"{case_id}: missing tool call outcome")

    for event in events:
        data = dict(event["data"])
        if event["event_type"] == "session.started":
            data.update(metadata)
        if event["event_type"].startswith("tool_call."):
            data["session_call_index"] = requested[data["tool_call_id"]]
        yield {
            "source": "real",
            "agent": "datalens",
            "case_id": case_id,
            "scenario": scenario,
            **metadata,
            "label": label if label is not None else ("policy_blocked" if denied else None),
            "policy_denied_event_ids": denied,
            "event": {**event, "data": data},
        }


def export_real(db, campaign, output):
    import psycopg

    runs = db.execute(
        "SELECT run_id, case_id, scenario FROM runs WHERE campaign=? AND outcome IS NOT NULL"
        " ORDER BY case_id",
        (campaign,),
    ).fetchall()
    if not runs:
        raise RuntimeError("No terminal runs in this campaign")
    with psycopg.connect(
        os.environ["DATALENS_TELEMETRY_DSN"],
        options="-c default_transaction_read_only=on",
        connect_timeout=10,
    ) as pg:
        for run, case_id, scenario in runs:
            rows = pg.execute(
                "SELECT envelope FROM public.agent_event_outbox WHERE envelope->>'run_id' = %s"
                " ORDER BY position",
                (run,),
            ).fetchall()
            events = [event for (event,) in rows]
            sessions = {}
            for event in events:
                sessions.setdefault(event["session_id"], []).append(event)
            if not sessions or any(
                trace[0]["event_type"] != "session.started"
                or trace[-1]["event_type"] != "session.ended"
                or [event["sequence"] for event in trace]
                != list(range(1, len(trace) + 1))
                or any(
                    event["data"].get("payload_version") != "agent-run-events-v2"
                    for event in trace
                )
                for trace in sessions.values()
            ):
                raise RuntimeError(
                    f"{case_id}: missing terminal or incompatible outbox trace"
                )
            for trace in sessions.values():
                for record in scenario_records(trace, case_id, scenario):
                    output.write(json.dumps(record) + "\n")


def write_review(db, campaign, event_text, output):
    traces = {}
    for line in event_text.splitlines():
        event = json.loads(line)["event"]
        traces.setdefault(event["run_id"], []).append(event)
    fields = (
        "case_id",
        "scenario",
        "run_id",
        "request_prompt",
        "outcome",
        "agent_output",
        "tools_requested",
        "tool_outcomes",
        "verified_outputs",
        "model_attempts",
        "model_errors",
        "event_count",
        "review_label",
        "review_notes",
    )
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    for row in db.execute(
        "SELECT * FROM runs WHERE campaign=? AND outcome IS NOT NULL ORDER BY case_id",
        (campaign,),
    ):
        run = row["run_id"]
        responses = [
            json.loads(item[0])
            for item in db.execute(
                "SELECT response_json FROM exchanges WHERE campaign=? AND case_id=?"
                " AND http_status=200 AND (path=? OR path LIKE ?) ORDER BY rowid",
                (
                    campaign,
                    row["case_id"],
                    f"/v1/runs/{run}",
                    f"/v1/runs/{run}/results/%",
                ),
            )
        ]
        final = next(
            (
                item
                for item in reversed(responses)
                if item and item.get("lifecycle") == "finished"
            ),
            None,
        )
        if final is None:
            raise RuntimeError(f"{row['case_id']}: missing captured terminal response")
        answer = (final.get("completion") or {}).get("answer") or {}
        trace = traces[run]
        calls = [
            event for event in trace if event["event_type"].startswith("model_call.")
        ]
        record = {
            "case_id": row["case_id"],
            "scenario": row["scenario"],
            "run_id": run,
            "request_prompt": row["question"],
            "outcome": row["outcome"],
            "agent_output": answer.get("explanation", ""),
            "tools_requested": json.dumps(
                [e["data"] for e in trace if e["event_type"] == "tool_call.requested"]
            ),
            "tool_outcomes": json.dumps(
                [
                    e["data"]
                    for e in trace
                    if e["event_type"] in ("tool_call.completed", "tool_call.failed")
                ]
            ),
            "verified_outputs": json.dumps(
                [item["payload"] for item in responses if item and "payload" in item]
            ),
            "model_attempts": len(calls),
            "model_errors": json.dumps(
                [e["data"] for e in calls if e["event_type"] == "model_call.failed"]
            ),
            "event_count": len(trace),
        }
        writer.writerow(
            {
                key: "'" + value
                if isinstance(value, str)
                and value.lstrip().startswith(("=", "+", "-", "@"))
                else value
                for key, value in record.items()
            }
        )


def write_sql(event_text, output):
    """Export the research records for import into a separate SQLite database."""
    output.write(
        "BEGIN;\nCREATE TABLE IF NOT EXISTS captured_events "
        "(event_id TEXT PRIMARY KEY, record_json TEXT NOT NULL);\n"
    )
    for line in event_text.splitlines():
        event_id = UUID(json.loads(line)["event"]["event_id"])
        payload = line.encode("utf-8").hex()
        output.write(
            "INSERT OR IGNORE INTO captured_events (event_id, record_json) "
            f"VALUES ('{event_id}', CAST(X'{payload}' AS TEXT));\n"
        )
    output.write("COMMIT;\n")


def export_dataset(db, campaign, output_path, review_path, sql_path):
    if not os.environ.get("DATALENS_TELEMETRY_DSN"):
        raise RuntimeError("Set DATALENS_TELEMETRY_DSN before exporting")
    with io.StringIO() as events, io.StringIO(newline="") as review:
        export_real(db, campaign, events)
        if review_path is not None:
            write_review(db, campaign, events.getvalue(), review)
        with output_path.open("x", encoding="utf-8", newline="\n") as output:
            output.write(events.getvalue())
        if review_path is not None:
            with review_path.open("x", encoding="utf-8-sig", newline="") as output:
                output.write(review.getvalue())
        if sql_path is not None:
            with sql_path.open("x", encoding="utf-8", newline="\n") as output:
                write_sql(events.getvalue(), output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("collect", "batch"):
        capture = commands.add_parser(name)
        capture.add_argument("--url", default="http://localhost:8000")
        capture.add_argument(
            "--cases", type=Path, default=Path(__file__).with_name("cases.json")
        )
        capture.add_argument("--dataset", type=Path)
        capture.add_argument("--campaign", required=True)
        capture.add_argument("--max-runs", type=int, default=2)
        capture.add_argument(
            "--pause-seconds", type=int, default=65 if name == "batch" else 0
        )
        if name == "batch":
            capture.add_argument("--output", type=Path, required=True)
            capture.add_argument("--review", type=Path, required=True)
            capture.add_argument("--sql", type=Path)
    real = commands.add_parser("export")
    real.add_argument("--campaign", required=True)
    real.add_argument("--output", type=Path, required=True)
    real.add_argument("--review", type=Path)
    real.add_argument("--sql", type=Path)
    args = parser.parse_args()
    store = Path("captures")
    store.mkdir(exist_ok=True)
    if args.command in ("batch", "export"):
        paths = [
            path.resolve()
            for path in (args.output, args.review, args.sql)
            if path is not None
        ]
        if len(set(paths)) != len(paths) or any(
            path.exists() or not path.parent.is_dir() for path in paths
        ):
            parser.error("Choose distinct new files in existing directories")
        if not os.environ.get("DATALENS_TELEMETRY_DSN"):
            parser.error("Set DATALENS_TELEMETRY_DSN")
    with closing(sqlite3.connect(store / "collector.sqlite")) as db:
        db.row_factory = sqlite3.Row
        db.executescript("""
            CREATE TABLE IF NOT EXISTS campaigns (name TEXT PRIMARY KEY, fingerprint TEXT);
            CREATE TABLE IF NOT EXISTS runs (
                campaign TEXT, case_id TEXT, scenario TEXT, question TEXT, request_key TEXT,
                dataset_id TEXT, conversation_id TEXT, run_id TEXT, outcome TEXT,
                PRIMARY KEY (campaign, case_id)
            );
            CREATE TABLE IF NOT EXISTS exchanges (
                campaign TEXT, case_id TEXT, method TEXT, path TEXT, request_json TEXT,
                http_status INTEGER, response_json TEXT
            );
        """)
        if args.command == "export":
            export_dataset(db, args.campaign, args.output, args.review, args.sql)
            return
        if args.max_runs < 1 or args.pause_seconds < 0:
            parser.error("--max-runs must be positive and --pause-seconds nonnegative")
        cases = cases_from(args.cases)
        client = Capture(
            db, args.url, os.environ["DATALENS_ACCESS_TOKEN"], args.campaign
        )
        csv_bytes = b"price,quantity\n2,1\n4,3\n6,5\n8,7\n"
        campaign_inputs = [client.origin, cases]
        if args.dataset is not None:
            with args.dataset.open("rb") as dataset:
                csv_bytes = dataset.read(5 * 1024 * 1024 + 1)
            if not 0 < len(csv_bytes) <= 5 * 1024 * 1024:
                parser.error("Dataset must contain 1 byte to 5 MiB")
            csv_bytes.decode("utf-8-sig")
            campaign_inputs.append(sha256(csv_bytes).hexdigest())
        fingerprint = sha256(
            json.dumps(campaign_inputs, sort_keys=True).encode()
        ).hexdigest()
        with db:
            db.execute(
                "INSERT OR IGNORE INTO campaigns VALUES (?, ?)",
                (args.campaign, fingerprint),
            )
        stored = db.execute(
            "SELECT fingerprint FROM campaigns WHERE name=?", (args.campaign,)
        )
        if stored.fetchone()[0] != fingerprint:
            raise RuntimeError(
                "Campaign origin, cases or dataset changed; choose a new campaign"
            )
        completed = {
            row[0]
            for row in db.execute(
                "SELECT case_id FROM runs WHERE campaign=? AND outcome IS NOT NULL",
                (args.campaign,),
            )
        }
        pending = [case for case in cases if case["id"] not in completed][
            : args.max_runs
        ]
        for index, case in enumerate(pending):
            if index and args.pause_seconds:
                time.sleep(args.pause_seconds)
            client.collect(case, csv_bytes, poll_seconds=180)
        if args.command == "batch":
            export_dataset(db, args.campaign, args.output, args.review, args.sql)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, KeyError, sqlite3.Error) as error:
        print(f"Collection stopped ({type(error).__name__}).", file=sys.stderr)
        if isinstance(error, RuntimeError):
            print(error, file=sys.stderr)
        sys.exit(1)
