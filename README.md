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

Stop anytime with Ctrl+C; it will drain in-flight requests, print a final
summary, and exit. Traffic also stops automatically once `duration_seconds`
and/or `total_requests` (configured under `traffic:`) is reached.

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
