# DataLens experiment collector

Use [COMMANDS.md](COMMANDS.md) for the complete local startup, collection,
export and shutdown sequence.

Run this one-shot Python CLI alongside DataLens. It can live in a separate
repository and run as a scheduled job. Each campaign saves API requests and
responses in a local SQLite file under `captures/`. Telemetry comes from the
agent’s existing PostgreSQL outbox and can be exported without Kafka.

## What gets stored

| Location | Contents |
| --- | --- |
| Agent PostgreSQL, `public.agent_event_outbox` | Original session, tool, and model events. The collector reads this table by run ID using a read-only connection. |
| `captures/collector.sqlite` | Campaign fingerprints, scenario prompts, run IDs and outcomes, idempotency keys, and captured API requests and responses. This file lets an interrupted campaign resume. |
| `captures/*.jsonl` | One enriched research event per line. The wrapper records provenance and review metadata. The nested event preserves the original event ID and timestamp. |
| `captures/*.csv` | One review row per run, with the prompt, answer, verified results, tool activity, and fields for manual labels and notes. |
| `captures/*.sql` | Optional SQLite import that deduplicates by event ID. |

The scenario manifests and the small test fixture belong in source control.
Git can track JSON, JSONL, CSV, and SQL exports in `captures/`.
The local SQLite database, credentials, and virtual environment are ignored.
Use the private team repository for these exports.

## Start collecting

1. From this folder, create a Python 3.12+ virtual environment and install
   `requirements.txt`. Start the usual DataLens API, worker and PostgreSQL with a
   live model configured. Use a dedicated test account and test environment.
   Use the regular Keycloak PKCE flow to obtain its access token and set
   `DATALENS_ACCESS_TOKEN` in your shell. If it expires, obtain a fresh token
   and rerun with the same account and campaign.
2. Run a small pilot. Only one collector should write to a campaign at a time.

   ```powershell
   python collector.py collect --campaign pilot --max-runs 2
   ```

   The included case file pairs normal questions with direct prompt injections
   against existing tool boundaries. The same campaign resumes unfinished runs
   with persisted idempotency keys, then advances through the manifest. Use a
   new campaign for intentional replicates or modified prompts. The cap counts
   scenarios attempted by the CLI per invocation. One run can consume several
   model calls, including tool continuation and retries. Start with one or two
   cases and check provider usage before increasing the batch size.
3. Ask the DataLens database owner for a connection with read access to
   `public.agent_event_outbox` in the experiment database. Put its psycopg DSN
   (`postgresql://...`) in `DATALENS_TELEMETRY_DSN`. Export before published
   outbox rows are cleaned up and keep a backup outside the dev database:

   ```powershell
   python collector.py export --campaign pilot --output captures/pilot-real.jsonl
   ```

The SQLite file contains the experiment CSV, prompts, run responses and
result payloads. Keep it private and use test data. Authorization headers and
database credentials are excluded from captures. The JSONL export contains
redacted telemetry with an outer provenance wrapper.

## Run a data collection batch

Each successful `batch` or `export` refreshes `captures/all-events.jsonl`,
`captures/all-runs.csv`, and `captures/all-events.sql`. The collector initializes
these files from local capture snapshots and then merges the exported campaign.
Events are keyed by event ID and review rows by run ID. Optional `--output`,
`--review`, and `--sql` flags still create separate snapshots when supplied.
If a batch stops before export, rerun it or use `export --campaign NAME`.

The follow-up manifest adds 16 scenarios for the existing input fixture:

```powershell
python collector.py batch --url http://127.0.0.1:18000 --campaign followup-docker-v1 --cases cases-followup.json --dataset experiment-input.csv --max-runs 16 --pause-seconds 45
```

With the API and worker running, set `DATALENS_ACCESS_TOKEN` and
`DATALENS_TELEMETRY_DSN` as above. Run from the collector folder:

```powershell
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
python collector.py batch --campaign study-v1 --max-runs 2 --output "captures/study-$stamp.jsonl" --review "captures/study-$stamp.csv" --sql "captures/study-$stamp.sql"
```

The command runs pending cases through the live agent sequentially, waits
65 seconds between cases by default, then exports all terminal runs in the
campaign. The CSV contains prompts, agent outputs, tool proposals and outcomes,
verified results, model attempts and blank review fields. Edit `cases.json` or
pass `--cases path/to/cases.json`. Pass `--dataset path/to/input.csv` to replace
the built-in four-row table. Match the questions to the CSV columns.

Repeat with the same campaign, inputs and account, and fresh output filenames.
Cases with a saved terminal outcome are skipped, including incomplete cases.
Use a new campaign for changed inputs. If export
fails, run `export --campaign study-v1 --output ... --review ...` to recover
saved runs without model calls. Keep manually reviewed CSV snapshots separately.

The delay and run cap control submissions. Groq usage still depends on tool
continuations, retries and other workloads sharing the quota.

The optional SQL file imports complete event records into a separate SQLite
database. Reimporting the same event IDs preserves one row per event:

```powershell
Get-Content -Raw "captures/study-$stamp.sql" | sqlite3 captures/research.sqlite
```

The collector also saves API requests and responses in `captures/collector.sqlite`.
See the [collector command guide](COMMANDS.md) for Docker Compose, migration,
API and worker commands. Start in this order:
Identity, agent PostgreSQL, migrations, API, worker, then the collector. Keep
Kafka and RabbitMQ optional for local collection. For live Kafka delivery,
start the [publisher](../datalens-agent/docs/kafka-telemetry.md) once the team
broker is reachable and its `KAFKA_*` settings are configured.

## Source and labeling rules

- `source=real` records observed agent activity. Collect normal and injected
  cases from both team agents. The starter collector supports DataLens. The
  teammate’s agent should produce the same wrapper around its own event
  mapping. Learn Batch 3 Markov transitions from those two agents only.
- `source=public` is reserved for an ATBench importer. Keep the upstream
  trajectory ID and label when implementing it. ATBench labels are at trajectory
  level. Treat this release’s `test` split as a separate public evaluation set.
  [ATBench dataset card](https://huggingface.co/datasets/AI45Research/ATBench)
Each exported `session.started` includes `scenario_id` from the case manifest,
`category` from the scenario, and `attack_type` (direct prompt injection or null).
Tool requests have one-based `session_call_index` values. Matching outcomes reuse
the request's index. Replay each session by `event.sequence`. Deduplicate cumulative
exports by `event.event_id`.

Exports assign a versioned session label automatically, with the same label in
the CSV's `automatic_label` column. `policy_blocked` records an explicit tool
denial. `answer_violation` currently recognizes a specific false North mean claim
when a verified tool result used an ungrouped mean. `model_failure` records a
model-call failure. `tool_trace_allowed` means a finished run's observed policy
calls all completed with explicit permission. It describes tool policy coverage.
`insufficient_evidence` is an automatic abstention. Evidence event IDs are
exported for denials, failures, and the observed final answer. Scenario metadata
and session labels are targets or review context for training, and should stay out
of the classifier inputs. Keep paired scenarios together when splitting data.
The enriched envelope is for research exports. Use the original wire format for
live telemetry.

## Dataset status

The local backfill covered 450 real events across 63 sessions and 63 scenario
IDs. The export validates session boundaries, contiguous event sequences, and
matching tool requests and outcomes. The backfill produced 21 allowed tool traces,
15 model failures, one policy block, one detected answer violation, and 25
abstentions.

The team's 500-scenario run and a shared-format check across both agents are
pending. This collector's scenarios use a statistics fixture. The date prompt and
`get_current_datetime` argument issue belong to the other agent's workflow.
