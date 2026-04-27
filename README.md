# imagegen-server

Queue-backed image generation service built around the `imagegen` OpenAI Responses flow.

## What it does

- Accepts image generation jobs over HTTP
- Stores jobs and runtime files under `~/.imagegen-server/`
- Runs one worker per configured API key
- Keeps each key at concurrency `1`
- Retries transient upstream failures with a fixed delay
- Exposes job status and generated files over HTTP

## Runtime data

All runtime data lives under `~/.imagegen-server/` by default:

- `app.db`
- `jobs/<job_id>/input`
- `jobs/<job_id>/output`
- `jobs/<job_id>/meta`
- `logs/server.log`

Override with `IMAGEGEN_SERVER_HOME` if needed.

## Required environment variables

```bash
export IMAGE_API_KEYS_JSON='[
  {"name":"key-a","api_key":"sk-..."},
  {"name":"key-b","api_key":"sk-..."}
]'
```

Optional variables:

```bash
export APP_HOST=127.0.0.1
export APP_PORT=8000
export IMAGEGEN_SERVER_HOME="$HOME/.imagegen-server"
export OPENAI_BASE_URL="https://api.example.com/v1"
export OPENAI_MODEL="gpt-5.5"
export OPENAI_IMAGE_TOOL_MODEL="gpt-image-2"
export JOB_MAX_RETRIES=2
export JOB_RETRY_DELAY_SECONDS=60
export JOB_TIMEOUT_SECONDS=600
```

## Install

Recommended local setup:

```bash
./scripts/bootstrap.sh
```

## Run

```bash
export IMAGE_API_KEYS_JSON='[{"name":"key-a","api_key":"sk-..."}]'
export OPENAI_BASE_URL='https://api.example.com/v1'
./scripts/start.sh
```

You can also keep a persistent environment file at `~/.imagegen-server/server.env`.
`scripts/start.sh` auto-loads it before starting the server.

Stop it with:

```bash
./scripts/stop.sh
```

## API

### Create a job

```bash
curl -X POST http://127.0.0.1:8000/jobs \
  -F 'prompt=A cinematic portrait under neon rain' \
  -F 'image_action=generate'
```

With references:

```bash
curl -X POST http://127.0.0.1:8000/jobs \
  -F 'prompt=Turn this into a premium anime illustration' \
  -F 'image_action=edit' \
  -F 'reference_images=@/absolute/path/ref1.png' \
  -F 'reference_images=@/absolute/path/ref2.jpg'
```

### Query a job

```bash
curl http://127.0.0.1:8000/jobs/<job_id>
```

### List jobs

```bash
curl 'http://127.0.0.1:8000/jobs?limit=20&offset=0'
```
