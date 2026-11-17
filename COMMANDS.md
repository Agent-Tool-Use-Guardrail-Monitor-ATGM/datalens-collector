# Run the DataLens event collector

These commands use Windows PowerShell from the existing workspace. Start Docker
Desktop first. Docker Compose runs the agent API and worker. Run the collector in PowerShell.
Kafka is not required to collect and export events.

## One-time setup

The existing Keycloak realm must have the `agent-web` client and an activated
test account with the `agent-api:user` role. The agent `.env` needs its
PostgreSQL URL and `SERVICE_MODEL_API_KEY`. The worker also needs the existing
Keycloak client-secret and token-encryption-key files. Follow the
[local setup guide](../../docs/plans/frontend/LOCAL_DEVELOPMENT.md) if any of
these are missing.

Install collector dependencies once:

```powershell
cd "$env:USERPROFILE\Desktop\ost-sm-project\services\datalens-collector"
if (!(Test-Path .venv)) { py -3.12 -m venv .venv }
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
New-Item -ItemType Directory -Force captures | Out-Null
```

## 1. Start Identity and the agent containers

Close the host API and worker terminals from the previous setup before
starting their containers. The Docker API uses port 8000 by default.

```powershell
cd "$env:USERPROFILE\Desktop\ost-sm-project"
Push-Location services/datalens-identity
docker compose --env-file .env.postgres -f compose.yaml -f compose.runtime.yaml up -d --wait
Pop-Location

Push-Location services/datalens-agent
$agentCompose = @('--env-file', '.env.postgres', '-f', 'compose.yaml', '-f', 'compose.runtime.yaml')
$env:AGENT_CONTAINER_DATABASE_URL = .\.venv\Scripts\python.exe -c "from dotenv import dotenv_values; from sqlalchemy.engine import make_url; print(make_url(dotenv_values('.env')['DATABASE_URL']).set(host='agent-postgres', port=5432).render_as_string(hide_password=False))"
docker compose @agentCompose build agent-api
docker compose @agentCompose run --rm agent-api python -m statistics_agent.persistence.migrate upgrade
docker compose @agentCompose up -d --wait --wait-timeout 90
docker compose @agentCompose ps
Invoke-RestMethod http://localhost:8000/health/ready
Pop-Location
```

Stop if a command fails. The migration command applies any pending migrations.
Wait for the API to return ready and check that agent-worker is running before
submitting work.
## 2. Collect real events

Obtain an access token by signing in through the configured Keycloak PKCE client.
In the following terminal, paste the access token without the `Bearer ` prefix.
For the PostgreSQL DSN, use a `postgresql://` URL with read access to
`public.agent_event_outbox`. Do not paste either value into source files.

```powershell
cd "$env:USERPROFILE\Desktop\ost-sm-project\services\datalens-collector"
$env:DATALENS_ACCESS_TOKEN = [Net.NetworkCredential]::new('', (Read-Host 'Keycloak access token' -AsSecureString)).Password
$env:DATALENS_TELEMETRY_DSN = [Net.NetworkCredential]::new('', (Read-Host 'PostgreSQL DSN' -AsSecureString)).Password
Invoke-RestMethod http://localhost:8000/auth/me -Headers @{ Authorization = "Bearer $env:DATALENS_ACCESS_TOKEN" } | Select-Object subject, roles
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
.\.venv\Scripts\python.exe collector.py batch --url http://127.0.0.1:8000 --campaign study-v1 --max-runs 2 --output "captures/study-$stamp.jsonl" --review "captures/study-$stamp.csv" --sql "captures/study-$stamp.sql"
```

Each invocation runs up to two pending cases, with 65 seconds between them.
Repeat the final two lines to continue the same campaign. Use the same account,
API URL, case file and CSV for a campaign. If the access token expires, obtain
a new token for that account. The pause does not enforce the Groq quota. Each
agent run can make multiple model requests. Check provider usage before scaling.

The default cases and input table are built in. To use your own, add
`--cases path/to/cases.json --dataset path/to/input.csv` to the batch
command. Questions must refer to columns in that CSV. Use a new campaign if
you change the inputs. See [case and label guidance](README.md#source-and-labeling-rules).

The batch writes real telemetry as JSONL, a CSV for manual review, and optional
SQL for a separate SQLite research database. Requests and responses are saved
in `captures/collector.sqlite`. An injected scenario is not a confirmed attack
success. Review the behavior before assigning a label.

## 3. Expanded suite and export recovery

### Run the 16-case suite

From the collector folder with the same environment variables set:

```powershell
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
.\.venv\Scripts\python.exe collector.py batch --url http://127.0.0.1:8000 --campaign diverse-v1 --max-runs 2 --cases cases-diverse.json --dataset experiment-input.csv --output "captures/diverse-$stamp.jsonl" --review "captures/diverse-$stamp.csv" --sql "captures/diverse-$stamp.sql"
```

Repeat with a fresh timestamp to advance through eight normal/injected pairs.
The fixture includes missing values and two regions. Tasks exercise schema
inspection, descriptive statistics, grouped aggregation, strict missing-value
handling, Spearman correlation, multiple calculations, clarification, and
unsupported requests. Actual outcomes depend on the model and tool policy.
Review the trace before assigning a security label.

### Retry an export

If collection completed but export failed, set the DSN as in step 2 and run:

```powershell
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
.\.venv\Scripts\python.exe collector.py export --campaign study-v1 --output "captures/recovered-$stamp.jsonl" --review "captures/recovered-$stamp.csv" --sql "captures/recovered-$stamp.sql"
```

## 4. Stop services

Wait for the batch to finish, then run:

```powershell
cd "$env:USERPROFILE\Desktop\ost-sm-project"
Push-Location services/datalens-agent
docker compose --env-file .env.postgres -f compose.yaml -f compose.runtime.yaml down
Pop-Location
Push-Location services/datalens-identity
docker compose --env-file .env.postgres -f compose.yaml -f compose.runtime.yaml down
Pop-Location
```

Plain `down` preserves the named database volumes and their data.
