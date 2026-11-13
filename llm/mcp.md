---
title: Model Context Protocol (MCP)
section: llm
order: 25
phase: "Phase 4 · Agents"
summary: "How MCP connects LLM apps to tools and data: hosts, clients, servers, primitives, transports, JSON-RPC flow, OAuth for remote servers and the security risks."
---

The Model Context Protocol (MCP) is an open protocol that standardizes how an LLM application discovers and calls external tools and reads external context. Anthropic introduced it in November 2024. During 2025 the major model providers, IDEs, agent frameworks and SaaS vendors adopted it. MCP is integration plumbing, not a model capability. The model still does ordinary [tool calling]({{ '/llm/tool-calling/' | relative_url }}). MCP standardizes the connection between your application and whatever exposes the tools, so one GitHub or Postgres integration works in every MCP-capable host.

> **Gotcha:** the spec is versioned by date and still moving. Revisions up to `2025-11-25` open a stateful session with an `initialize` handshake. The `2026-07-28` revision (current as of late 2026) makes every request self-describing. Many deployed servers still speak the older revisions, so this note covers both.

## The problem: N×M integrations

```text
 Without a protocol: N x M adapters    With MCP: N + M implementations

  App A ──┬── GitHub adapter            App A ─┐          ┌─ GitHub server
          └── Postgres adapter          App B ─┼── MCP ───┼─ Postgres server
  App B ──┬── GitHub adapter            App C ─┘          └─ Slack server
          └── Postgres adapter
  App C ──┴── ... (every pair again)
```

Before MCP, every agent product wrote its own adapter for every tool. MCP does for agent tools what the Language Server Protocol did for editors: each tool vendor ships one server, and each application implements one client.

## Architecture: host, client, server

```text
┌──────────── Host (IDE, chat app, your agent service) ─────────────┐
│   LLM  ◄──►  orchestrator: consent, routing, context assembly     │
│                │                 │                 │              │
│           MCP client        MCP client        MCP client          │
└────────────────┼─────────────────┼─────────────────┼──────────────┘
                 │ stdio           │ HTTPS           │ HTTPS
          ┌──────▼──────┐   ┌──────▼──────┐   ┌──────▼──────┐
          │ git server  │   │ issue       │   │ your orders │
          │ (local proc)│   │ tracker     │   │ API server  │
          └─────────────┘   └─────────────┘   └─────────────┘
```

- **Host**: the application the user interacts with. It owns the model, the conversation, user consent and the decision about which tool results enter the context.
- **Client**: a connector inside the host that talks to exactly one server.
- **Server**: a process or service that exposes capabilities. It never sees the full conversation or the other servers. That isolation is deliberate.

The model never speaks MCP. The host converts MCP tool definitions into the provider's tool schema, turns the model's tool calls into `tools/call`, and feeds results back as tool-result messages.

## Primitives

| Primitive | Controlled by | Methods | Example |
|---|---|---|---|
| Tools | Model | `tools/list`, `tools/call` | `search_issues`, `run_query` |
| Resources | Application | `resources/list`, `resources/read`, `resources/templates/list` | `file:///repo/README.md`, a DB schema |
| Prompts | User | `prompts/list`, `prompts/get` | A `/review-pr` slash command |

- **Tools** are functions the model may invoke. A definition has `name`, `description`, `inputSchema` (JSON Schema), plus optional `title`, `outputSchema` and `annotations` (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`). Annotations are hints from the server, and the spec says to treat them as untrusted unless the server is trusted.
- **Resources** are readable data addressed by URI. The host decides whether to attach them.
- **Prompts** are server-provided templates the user invokes explicitly.

The client can also offer features to the server:

- **Elicitation**: the server asks the user, through the host, for structured input such as a confirmation or a missing field.
- **Sampling**: the server asks the host's model for a completion, so the server needs no API key of its own. Deprecated in `2026-07-28`. Call a provider API directly instead.
- **Roots**: the client tells the server which directories it may touch. Deprecated in `2026-07-28` in favor of passing paths as tool arguments or configuration.

Tool results carry `content` blocks (`text`, `image`, `audio`, `resource_link`, embedded `resource`), optional `structuredContent` that matches `outputSchema`, and `isError`. Errors use two channels. A **protocol error**, such as an unknown tool or malformed params, is a JSON-RPC `error`. A **tool execution error**, such as a bad date or an upstream 500, is a normal result with `isError: true`, so the model can read it and correct itself.

## Message flow

MCP messages are JSON-RPC 2.0. A request has an `id`, a `method` and `params`. A response echoes the `id` and carries either `result` or `error`. A notification has no `id` and must never be answered.

Under the handshake revisions (`2025-11-25` and earlier), a connection works like this:

```text
client → initialize {protocolVersion, capabilities, clientInfo}
server ← result {protocolVersion, capabilities, serverInfo, instructions}
client → notifications/initialized
client → tools/list, tools/call ...  (state lives on the connection)
```

The `2026-07-28` revision removes the handshake and protocol-level sessions. Every request carries its version and capabilities in `_meta`. `server/discover` returns supported versions and capabilities, and every result has a `resultType`:

{% raw %}
```json
{"jsonrpc": "2.0", "id": 7, "method": "tools/call",
 "params": {"name": "search_issues", "arguments": {"query": "refund bug"},
   "_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
             "io.modelcontextprotocol/clientCapabilities": {"elicitation": {}},
             "io.modelcontextprotocol/clientInfo": {"name": "agent", "version": "1.4"}}}}

{"jsonrpc": "2.0", "id": 7,
 "result": {"resultType": "complete", "isError": false,
            "content": [{"type": "text", "text": "3 open issues: ..."}]}}
```
{% endraw %}

In the stateless revision, servers no longer send requests to clients. If a tool needs user input or an LLM completion, the server answers with `resultType: "input_required"` and a map of `inputRequests`. The client collects the answers and retries the original call, under a new request id, with `inputResponses` and the opaque `requestState` the server returned. The spec calls this multi round-trip requests (MRTR). Long-lived change notifications such as `notifications/tools/list_changed` arrive on a `subscriptions/listen` stream. State that must span calls, like a browser session or a cart, uses explicit handles that the model passes back as tool arguments.

## Transports

**stdio.** The host launches the server as a subprocess and exchanges newline-delimited JSON-RPC over stdin and stdout. stderr is free for logs. Credentials come from environment variables, not OAuth. stdio fits local tools such as the filesystem, git or a local database. The server runs with the user's privileges.

**Streamable HTTP.** The server exposes one endpoint such as `https://api.example.com/mcp`. The client POSTs every message to it, accepting `application/json` or `text/event-stream`. The server replies with one JSON object or with an SSE stream scoped to that request: progress notifications, then the final response. Requests carry an `MCP-Protocol-Version` header, and in `2026-07-28` also `Mcp-Method` and `Mcp-Name`, so gateways can route without parsing the body. Earlier revisions had `Mcp-Session-Id` sessions and a GET stream for server-initiated messages. Dropping them lets servers scale behind a plain load balancer. The original HTTP+SSE transport (`2024-11-05`) is deprecated.

| | stdio | Streamable HTTP |
|---|---|---|
| Deployment | Subprocess per host | Shared service |
| Users | One local user | Many, multi-tenant |
| Auth | Env vars, OS user | OAuth 2.1 bearer tokens |
| Scaling | N/A | Stateless replicas (2026-07-28) |
| Main risks | Full local privileges, supply chain | Token handling, DNS rebinding, SSRF |

Local HTTP servers must validate the `Origin` header and bind to `127.0.0.1`. Otherwise a malicious web page can reach them through DNS rebinding.

## In practice

### A minimal server with the Python SDK (illustrative)

The official Python SDK (`mcp`) builds a server from decorated functions. It generates the input schema from type hints and the description from the docstring. Version 1 called the class `FastMCP` (`from mcp.server.fastmcp import FastMCP`). Version 2 renames it `MCPServer` and keeps the same decorator style. Check the version you install.

```python
from mcp.server import MCPServer   # v1: from mcp.server.fastmcp import FastMCP

mcp = MCPServer("orders")

@mcp.tool()
def search_orders(status: str | None = None, limit: int = 10) -> list[dict]:
    """Search the current customer's orders, newest first. Max 50 results."""
    return repo.search(status=status, limit=min(limit, 50))

@mcp.resource("schema://orders")
def orders_schema() -> str:
    """Column names and types of the orders table."""
    return repo.describe_table("orders")

@mcp.prompt()
def refund_review(order_id: str) -> str:
    """Review a refund request against policy."""
    return f"Review order {order_id} against the refund policy. List violations."
```

```bash
pip install "mcp[cli]"
mcp dev server.py                                  # run under the MCP Inspector
mcp run server.py --transport streamable-http      # serve at localhost:8000/mcp
```

### The wire protocol in stdlib Python

The toy below speaks the `2026-07-28` request shape over stdio. The script launches itself as the server subprocess. It shows discovery, listing, both error channels, version rejection and a multi round-trip confirmation.

{% raw %}
```python
import json
import subprocess
import sys

VERSION = "2026-07-28"
TOOLS = [
    {"name": "add", "description": "Add two integers.",
     "inputSchema": {"type": "object", "required": ["a", "b"],
                     "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}}},
     "annotations": {"readOnlyHint": True}},
    {"name": "drop_table", "description": "Drop a table after user confirmation.",
     "inputSchema": {"type": "object", "required": ["table"],
                     "properties": {"table": {"type": "string"}}},
     "annotations": {"destructiveHint": True}},
]


def ok(rid, result, kind="complete"):
    return {"jsonrpc": "2.0", "id": rid, "result": {"resultType": kind, **result}}


def err(rid, code, message):
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def text(s, is_error=False):
    return {"content": [{"type": "text", "text": s}], "isError": is_error}


def call_tool(rid, params):
    name, args = params.get("name"), params.get("arguments", {})
    if name == "add":
        if not all(isinstance(args.get(k), int) for k in ("a", "b")):
            return ok(rid, text("a and b must be integers", is_error=True))
        return ok(rid, text(str(args["a"] + args["b"])))
    if name == "drop_table":
        answer = params.get("inputResponses", {}).get("confirm")
        if answer is None:  # ask the user via the client, then expect a retry
            ask = {"method": "elicitation/create", "params": {
                "mode": "form", "message": f"Drop {args['table']}?",
                "requestedSchema": {"type": "object", "required": ["ok"],
                                    "properties": {"ok": {"type": "boolean"}}}}}
            return ok(rid, {"inputRequests": {"confirm": ask}}, kind="input_required")
        if answer["action"] == "accept" and answer["content"]["ok"]:
            return ok(rid, text(f"dropped {args['table']}"))
        return ok(rid, text("user declined", is_error=True))
    return err(rid, -32602, f"Unknown tool: {name}")


def handle(msg):
    if "id" not in msg:
        return None  # notification: never answered
    rid, method, params = msg["id"], msg["method"], msg.get("params", {})
    version = params.get("_meta", {}).get("io.modelcontextprotocol/protocolVersion")
    if version != VERSION:
        return err(rid, -32022, f"Unsupported protocol version {version}")
    if method == "server/discover":
        return ok(rid, {"supportedVersions": [VERSION], "capabilities": {"tools": {}}})
    if method == "tools/list":
        return ok(rid, {"tools": TOOLS})
    if method == "tools/call":
        return call_tool(rid, params)
    return err(rid, -32601, f"Method not found: {method}")


def serve():
    for line in sys.stdin:  # stdio framing: one JSON message per line
        reply = handle(json.loads(line))
        if reply is not None:  # stdout is the wire; logs must go to stderr
            print(json.dumps(reply), flush=True)


class Client:
    def __init__(self):
        cmd = [sys.executable, "-I", __file__, "--serve"]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, text=True)
        self.next_id = 0

    def request(self, method, params=None, version=VERSION):
        self.next_id += 1
        meta = {"io.modelcontextprotocol/protocolVersion": version,
                "io.modelcontextprotocol/clientCapabilities": {"elicitation": {}}}
        msg = {"jsonrpc": "2.0", "id": self.next_id, "method": method,
               "params": {**(params or {}), "_meta": meta}}
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        reply = json.loads(self.proc.stdout.readline())
        assert reply["id"] == self.next_id  # responses correlate by id
        return reply

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=5)


if __name__ == "__main__":
    if "--serve" in sys.argv:
        serve()
        sys.exit(0)
    c = Client()
    assert c.request("server/discover")["result"]["supportedVersions"] == [VERSION]
    names = [t["name"] for t in c.request("tools/list")["result"]["tools"]]
    assert names == ["add", "drop_table"]
    r = c.request("tools/call", {"name": "add", "arguments": {"a": 2, "b": 3}})
    assert r["result"]["content"][0]["text"] == "5" and not r["result"]["isError"]
    r = c.request("tools/call", {"name": "add", "arguments": {"a": "2", "b": 3}})
    assert r["result"]["isError"]  # execution error: shown to the model
    assert c.request("tools/call", {"name": "nope"})["error"]["code"] == -32602
    assert c.request("tools/frobnicate")["error"]["code"] == -32601
    assert c.request("tools/list", version="2024-11-05")["error"]["code"] == -32022
    call = {"name": "drop_table", "arguments": {"table": "orders"}}
    r = c.request("tools/call", call)["result"]
    assert r["resultType"] == "input_required" and "confirm" in r["inputRequests"]
    yes = {"confirm": {"action": "accept", "content": {"ok": True}}}
    r = c.request("tools/call", {**call, "inputResponses": yes})["result"]
    assert r["content"][0]["text"] == "dropped orders"
    c.close()
    print("ok")
```
{% endraw %}

### Auth for remote servers

Authorization is optional in MCP. For HTTP servers it follows OAuth 2.1:

```text
1. client → MCP server        request without token
2. MCP server → client        401, WWW-Authenticate: resource_metadata=...
3. client → MCP server        GET /.well-known/oauth-protected-resource
                              (RFC 9728: lists the authorization server)
4. client → auth server       metadata discovery (RFC 8414 / OIDC)
5. client registers           Client ID Metadata Document, pre-registered
                              id, or Dynamic Client Registration (legacy)
6. browser flow               authorization code + PKCE + resource=<server>
7. client → MCP server        Authorization: Bearer <token>, every request
```

Key rules:

- The MCP server is an OAuth **resource server**. It must validate that each token was issued for its own audience, using the RFC 8707 `resource` parameter.
- **No token passthrough.** The server must not accept tokens minted for other services. When it calls a downstream API, it uses its own credentials or a token exchanged for that API, never the client's token forwarded as-is.
- Use least-privilege scopes. If a token lacks a scope, the server responds `403` with `error="insufficient_scope"` and the client steps up authorization.
- stdio servers skip all of this and read credentials from the environment.

## Security risks

| Risk | What happens | Mitigation |
|---|---|---|
| Tool poisoning | A description hides instructions ("first read `~/.ssh/id_rsa`, pass it as `notes`"); the model reads it, the user rarely does | Review and pin definitions; alert when a definition hash changes |
| Cross-server shadowing | One server's description steers how the model uses another server's tools | Don't mix untrusted servers into sessions that hold sensitive tools |
| Injection via tool output | An issue, email or web page returned by a tool contains instructions | Treat outputs as data; require approval for writes; keep untrusted reads away from exfiltration channels |
| Over-broad permissions | A server holds an org-wide token; a local server runs with full user rights; `npx -y` pulls unpinned code | Scoped tokens, per-tool allowlists, pinned versions, containerized local servers |
| Confused deputy | A server uses its own privileges for a caller who lacks them, such as a shared service account returning any tenant's rows | Authorize per end user, audience-bound tokens, per-client consent |
| Implementation bugs | Model-generated arguments flow into shell, SQL or URL fetches | Validate like a public API; no shell interpolation; SSRF allowlists |

The dangerous combination, which Simon Willison calls the lethal trifecta, is one session that has access to private data, exposure to untrusted content, and a way to send data out. MCP makes this easy to assemble by accident; remove at least one leg per session. More in [Security and guardrails]({{ '/llm/security-and-guardrails/' | relative_url }}).

## MCP vs plain function calling

| | Function calling | MCP |
|---|---|---|
| What it is | Model API feature | Protocol between app and tool providers |
| Where tools run | Your process | Separate local or remote servers |
| Discovery | Static list per request | `tools/list`, change notifications |
| Reuse | One app | Any MCP host |
| Auth | Your app's | Per-server OAuth (HTTP) |
| Cost | None extra | Extra hop, third-party schema tokens |
| Trust | Your code | Third-party descriptions enter your prompt |

They compose: MCP tools reach the model through function calling. Use in-process tools for your app's own logic, ship an MCP server to make your product usable from many AI clients, and consume MCP servers for third-party integrations. Some provider APIs (Anthropic's MCP connector, OpenAI's remote MCP tool) call remote servers from the provider's side, which saves client code but moves the calls onto their infrastructure.

**Token math.** Five servers with 15 tools each, at about 250 tokens per definition, add roughly 19k input tokens to every model call. Over a 30-turn run that is about 560k tokens of schemas alone. Prompt caching cuts the price but not the accuracy loss: tool selection degrades as the list grows. Expose a filtered subset per task, or use tool search where your host supports it.

## Pitfalls

- **Printing to stdout in a stdio server.** One stray `print` corrupts the stream. Symptoms are parse errors or "connection closed" in the host. Log to stderr.
- **Name collisions.** Two servers both expose `search`. Hosts prefix names, for example `mcp__github__search` in the Claude Agent SDK. Don't rely on `serverInfo.name` being unique.
- **Version skew.** A `2026-07-28` client meets a `2025-06-18` server. Clients fall back to `initialize` on a non-modern error. Pin SDK versions and test both eras.
- **Hidden session state.** Code that keys state by connection breaks behind a load balancer and violates the stateless revision. Use explicit handles.
- **Huge results.** Paginate, truncate with a hint, or return `resource_link`s.
- **Trusting annotations.** `readOnlyHint: true` is a claim, not enforcement. Gate destructive actions in the host.
- **Slow tools.** Long calls hit host timeouts. Emit progress notifications, or use the tasks extension for durable long-running work.

## Checklist

- Inventory every server: owner, version pin, transport, credentials, scopes.
- Review tool descriptions as you would code from a stranger. Alert on changes.
- Require human confirmation for destructive or externally visible actions.
- Never let one session combine private data, untrusted input and outbound channels.
- Remote servers: OAuth 2.1 with PKCE, audience-checked tokens, no passthrough, `Origin` validation.
- Keep the tool count per request small, and trace every `tools/call` with arguments, latency and result size ([Observability and tracing]({{ '/llm/observability-and-tracing/' | relative_url }})).

## Interview questions

**What problem does MCP solve, and how does it differ from function calling?** It replaces N×M custom integrations with one protocol. Function calling is how a model asks for a tool; MCP is how the host discovers and invokes tools hosted elsewhere.

**Host, client, server?** The host is the LLM app that owns the model, the user and consent. A client is one connection inside the host. A server exposes tools, resources and prompts and is isolated from the conversation and from other servers.

**Tools vs resources vs prompts?** Tools are model-controlled actions. Resources are application-controlled data addressed by URI. Prompts are user-invoked templates. The split tells the host who should decide to use each one.

**stdio or Streamable HTTP?** stdio for local, single-user tools launched by the host, with credentials from the environment. Streamable HTTP for shared, multi-tenant services with OAuth. After `2026-07-28` these servers are stateless and scale like any HTTP API.

**How should a remote MCP server handle tokens?** As an OAuth resource server. Advertise the authorization server through protected resource metadata, accept only tokens whose audience is this server, never forward the client's token downstream, and challenge with `insufficient_scope` for step-up.

**What is tool poisoning, and how do you defend against it?** Malicious instructions hidden in tool metadata that the model obeys. Defend by reviewing and pinning definitions, alerting on changes, isolating untrusted servers, and gating sensitive actions with approval.

**An agent reads public GitHub issues and can push to private repos through MCP. What's wrong?** It combines untrusted input, private data and an exfiltration path. An injected issue can make the agent leak private code. Split the capabilities across sessions or require approval for writes.

**Why did the 2026 revision go stateless?** Session-bound servers needed sticky routing. With per-request `_meta`, explicit state handles and mirrored headers, servers scale horizontally and gateways can route requests.

## Further reading

- Model Context Protocol specification, revisions `2026-07-28` and `2025-11-25` (modelcontextprotocol.io), including the security best practices page.
- Anthropic, "Introducing the Model Context Protocol" (November 2024).
- JSON-RPC 2.0 specification.
- RFC 9728 (OAuth 2.0 Protected Resource Metadata) and RFC 8707 (Resource Indicators for OAuth 2.0).
- Simon Willison, "The lethal trifecta for AI agents" (2025).
