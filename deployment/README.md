# Herbalife survey analyst agent

This repository runs the FlavorAI survey analyst: a FastAPI service that answers questions about
one survey using a single OpenAI model, read-only PostgreSQL access, and the Charts statistics API. Another
backend calls `POST /ask` and consumes its newline-delimited JSON (NDJSON) stream.

## Architecture and runtime constraints

```text
Calling backend -> HTTPS/Nginx -> 127.0.0.1:8000 -> FastAPI/LangGraph
                                                   |-> PostgreSQL (read-only)
                                                   |-> Charts statistics API
                                                   +-> OpenAI API
```

Production deliberately runs **one Uvicorn worker**. Survey scope is currently process-global,
agent runs are serialized by an in-process lock, and conversation checkpoints are in memory. More
workers would split thread state and require sticky routing; do not increase the worker count until
scope and checkpoints are moved to request-safe shared storage.

Every decision and synthesis turn uses `MODEL_NAME` with the same complete schema-bearing system
prompt. There is no tool-based model selector or full/lean prompt selector. The final permitted
turn uses that same model with tools unbound so it must synthesize from evidence already gathered.

## API contract

`GET /health` reports process health and whether the agent execution slot is busy. It does not
probe all external dependencies; use `scripts/preflight.py` to verify database access.

`POST /ask` and `DELETE /threads/{thread_id}` require `Authorization: Bearer <secret>`. The
calling backend reads the secret from `HERBALIFE_EXTERNAL_ACCESS_SECRET`; never send it to or
embed it in a browser application.

`POST /ask` accepts:

```json
{
  "prompt": "Compare the aroma scores of the tested products.",
  "survey_id": "SURVEY_UUID",
  "thread_id": "optional-conversation-id"
}
```

`prompt` and `survey_id` are required. Reuse `thread_id` for follow-ups about the same survey.
The `application/x-ndjson` response emits one JSON object per line:

| Event | Caller behavior |
|---|---|
| `status` | Optional progress information |
| `token` | Append text to the answer buffer |
| `reset` | Clear text from an intermediate tool-calling turn |
| `tool_result` | Optional tool progress metadata |
| `done` | Use `answer` as the authoritative result and retain `thread_id` |
| `error` | Treat the stream as failed, even though HTTP 200 may already be committed |

The calling backend must read incrementally and allow at least a 15-minute upstream timeout.
Read the durable log with `GET /threads/{thread_id}/messages?survey_id=SURVEY_UUID`; add
`&detailed=true` for the agent trace. To list every conversation with activity on a UTC date,
use the authenticated administrator endpoint `GET /admin/conversations?date=YYYY-MM-DD&detailed=true`.
Release completed conversations with
`DELETE /threads/{thread_id}`. LangGraph checkpoint state is lost on restart, but the SQLite
conversation log is persisted in the `conversation-data` volume. The checkpoint memory also grows
substantially if callers never delete threads.

## Configuration

Copy `.env.example` to `.env` locally. Production uses
`/opt/herbalife-agent/app.env`, created directly on the server with mode `0600`; CI never creates
or prints it.

Required variables:

| Variable | Purpose |
|---|---|
| `OPENAI_API_KEY` | OpenAI model access |
| `DATABASE_URL` | PostgreSQL DSN for a **read-only** role |
| `CHARTS_API_BASE_URL` | Survey-builder Charts API base URL |
| `CHARTS_STATS_EXTERNAL_ACCESS_SECRET` | Value sent as `x-charts-stats-secret` |
| `HERBALIFE_EXTERNAL_ACCESS_SECRET` | At least 32 characters; authenticates backend callers |

Other variables in `.env.example` tune models, caches, timeouts, and CORS. Never put secrets in
Git, Docker build arguments, Compose YAML, or CI logs.

## Local use

```bash
cp .env.example .env
# Supply valid development credentials. Generate the backend secret with: openssl rand -hex 32
docker compose up --build -d
docker compose ps
curl -fsS http://127.0.0.1:8000/health
docker compose exec flavorai python scripts/preflight.py
```

Or run in a virtual environment:

```bash
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
./start.sh
```

Test the stream with:

```bash
python scripts/ask_stream.py --health
python scripts/ask_stream.py "What products were tested?" --survey-id SURVEY_UUID
```

## Production deployment

Deployment assets are included in the repository:

- `Dockerfile`: non-root, single-worker image with a health check.
- `compose.production.yaml`: immutable image, loopback-only port, bounded logs, reduced privileges.
- `ops/deploy.sh`: health-gated deployment with rollback to the last recorded image.
- `.gitlab-ci.yml`: validation, rootless BuildKit image publishing, and SSH deployment.

The full Ubuntu/Hetzner, Docker, Nginx, Fail2ban, Certbot, deploy-user, and SSH runbook is
[`docs/hetzner-deployment.md`](docs/hetzner-deployment.md).

Create the production secret file once as root:

```bash
install -d -m 0750 -o deploy -g deploy /opt/herbalife-agent
install -m 0600 -o deploy -g deploy /dev/null /opt/herbalife-agent/app.env
sudo -u deploy nano /opt/herbalife-agent/app.env
```

Use `.env.example` as the key list. The container always listens on port 8000.

### GitLab CI/CD variables

The repository must be hosted or mirrored in GitLab with Container Registry enabled. Add protected
CI/CD variables:

| Variable | Type | Example |
|---|---|---|
| `SSH_PRIVATE_KEY` | File | Dedicated deploy-user private key |
| `SSH_KNOWN_HOSTS` | File | Verified ED25519 host-key line |
| `DEPLOY_HOST` | Variable | `herbalife-agent.gpisurveys.com` |
| `DEPLOY_PORT` | Variable | `22` |
| `DEPLOY_USER` | Variable | `deploy` |
| `DEPLOY_PATH` | Variable | `/opt/herbalife-agent` |
| `PRODUCTION_URL` | Variable | `https://herbalife-agent.gpisurveys.com` |

Create a GitLab deploy token named `gitlab-deploy-token` with `read_registry` only. GitLab exposes
it as `CI_DEPLOY_USER` and `CI_DEPLOY_PASSWORD`. The production job is manual on the default branch
until its rule is intentionally changed to `when: on_success`.

### Nginx

`/ask` is streaming HTTP (not WebSocket or SSE). The proxy must not buffer it:

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_connect_timeout 10s;
    proxy_send_timeout 900s;
    proxy_read_timeout 900s;
    proxy_buffering off;
    proxy_cache off;
    add_header X-Accel-Buffering no;
}
```

There is currently no built-in API authentication or rate limiting. Port 8000 is therefore bound
only to loopback. Protect Nginx with the existing backend gateway, a private network, or an IP
allow-list; do not expose `/ask` directly to the internet.

## Operations and performance

The SQLite conversation log is stored at `/app/data/conversation.sqlite3`, backed by the named
`herbalife-agent-conversation-data` volume in production. Do not use `docker compose down -v` or
remove that volume unless the log has been backed up.

```bash
cd /opt/herbalife-agent
docker compose --env-file .compose.env -f compose.production.yaml ps
docker compose --env-file .compose.env -f compose.production.yaml logs -f --tail=200 flavorai
curl -fsS http://127.0.0.1:8000/health
docker compose --env-file .compose.env -f compose.production.yaml exec flavorai python scripts/preflight.py
```

If preflight reports a PostgreSQL connection timeout, follow the
[private PostgreSQL timeout runbook](docs/hetzner-deployment.md#postgresql-connection-times-out-after-10-seconds).

Manual rollback uses an immutable registry tag:

```bash
./deploy.sh registry.gitlab.com/GROUP/PROJECT:KNOWN_GOOD_COMMIT_SHA
```

- Requests queue behind one execution lock. Adding workers is not currently a safe scale-out path.
- `MAX_PARALLEL_TOOL_CALLS` caps same-turn SQL/statistics batches inside one request; the legacy
  `MAX_PARALLEL_SQL_CALLS` value remains its fallback. Neither setting changes API concurrency.
- Inventory caches are process-local and are cleared by a restart.
- Keep the PostgreSQL role read-only and retain the database statement timeout.
- Monitor memory and require callers to delete completed threads.
