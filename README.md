# llm-mcp-trafficgen

Continually generates chat traffic against one or more OpenAI-compatible LLM
gateways, giving each LLM tool access to one or more MCP servers.

## How it works

1. On startup, the app connects to every configured MCP server, calls
   `initialize` and `tools/list` (JSON-RPC 2.0 over a single HTTP POST per
   call, matching `mcp-code-sample.txt`), and converts the discovered tools
   into OpenAI-style `tools` schema entries.
2. For each configured LLM, it launches `concurrency` worker tasks that loop:
   pick a prompt, POST it to `.../chat/completions` (matching
   `llm-code-sample.txt`) with the full tool list attached, and pace
   themselves to the configured `requests_per_second`.
3. If the model responds with `tool_calls`, the app dispatches each call to
   the right MCP server via `tools/call`, feeds the result back as a `tool`
   message, and loops (up to `max_tool_call_rounds`) until the model returns
   a plain answer.
4. Every configured MCP server's tools are exposed to every configured LLM.
5. Results (latency, tool-call counts, errors) are logged to a JSONL file and
   summarized periodically to the console.

## Setup

Requires Python 3.11+ (or use Docker, below).

```bash
pip install -r requirements.txt
cp config.example.yaml config.yaml
cp prompts.example.txt prompts.txt   # or edit config.yaml to point elsewhere
export NAI_API_KEY=...
export MCP_CRM_API_KEY=...
```

Edit `config.yaml` to list your real LLM(s) and MCP server(s). API keys can
be given inline (`api_key: ...`) or, preferably, via an environment variable
name (`api_key_env: SOME_ENV_VAR`).

## Run

```bash
python -m trafficgen --config config.yaml
```

Stop anytime with Ctrl+C: it stops sending new requests, gives in-flight
requests up to 5 seconds to finish, prints a final summary, and exits. Press
Ctrl+C a second time to cancel in-flight requests immediately. Traffic also stops automatically once `duration_seconds`
and/or `total_requests` (configured under `traffic:`) is reached.

### Web UI

```bash
python -m trafficgen --ui --config config.yaml   # then open http://127.0.0.1:8080
```

A local page for editing the config as a form (LLMs, MCP servers, traffic,
prompts, output) instead of hand-editing YAML. "Save" validates the config
and writes it back to the `--config` file. "Save & start" also starts a run,
and the page shows live per-LLM stats and the log. Use `--port`/`--host` to
change where it listens. It binds to localhost by default, because anyone who
can reach it can edit the config and start traffic.

**Password:** set `TRAFFICGEN_UI_PASSWORD` to require a password. The browser
asks for it on first visit (leave the username blank, or type anything). Without
it, anyone who can reach the UI can edit the config, start traffic, and read
any inline `api_key` values; the server logs a warning at startup. The UI is
plain HTTP, so the password crosses the network unencrypted; put it behind an
HTTPS reverse proxy if that matters on your network.

Note: saving from the UI rewrites the YAML file, so comments in it are lost.
For API keys, use `api_key_env`; the UI shows whether each variable is set in
the environment the UI server was started from.

## Docker

```bash
cp config.example.yaml config.yaml        # if you haven't already
echo "NAI_API_KEY=..." >> .env            # keys named by api_key_env
echo "TRAFFICGEN_UI_PASSWORD=..." >> .env # password for the UI
docker compose up --build                 # UI at http://127.0.0.1:8080
```

The current directory is mounted at `/data` in the container, so the UI
reads and saves `./config.yaml`, prompts files are found next to it, and the
JSONL log is written here too. API keys come from `.env`, which is gitignored.
The UI is published on port 8080 on all of the host's network interfaces, so
others on the network can open `http://<host>:8080`. To keep it to the host
only, change the port mapping in `docker-compose.yml` to `127.0.0.1:8080:8080`.

To run traffic directly without the UI (Ctrl+C works the same as above):

```bash
docker compose run --rm trafficgen --config /data/config.yaml
```

## Config reference

See `config.example.yaml` for a fully commented example. Key sections:

- `llms`: list of LLM gateway entries (`base_url`, `chat_path`, `model`,
  `api_key`/`api_key_env`, `verify_ssl`, optional per-LLM
  `requests_per_second`/`concurrency` overrides).
- `mcp_servers`: list of MCP gateway entries (`base_url`,
  `api_key`/`api_key_env`, `verify_ssl`).
- `traffic`: global `requests_per_second`, `concurrency`, `duration_seconds`
  and/or `total_requests`, `max_tool_call_rounds`, `request_timeout_seconds`.
- `prompts`: `file` (one prompt per line) and/or inline `list`; a prompt is
  picked at random per request.
- `output`: JSONL `log_file` path and console `summary_interval_seconds`.

## Notes / assumptions

The MCP transport in `mcp-code-sample.txt` is a bare JSON-RPC POST per call
rather than the standard stdio or SSE-streaming MCP transports. This client:

- Sends `initialize`, then a best-effort `notifications/initialized` (some
  gateways may not need/accept it -- failure there is logged and ignored).
- Calls `tools/list` to discover tools, and `tools/call` to invoke them.
- If the gateway returns an `Mcp-Session-Id` response header (as MCP's
  standard "Streamable HTTP" transport does), it's captured and echoed back
  on subsequent calls; if the gateway is stateless, this is a no-op.

If your actual gateway deviates from this (different method names, required
session headers, etc.), the JSON-RPC plumbing lives entirely in
`trafficgen/mcp_client.py` and should be easy to adjust.
