# specmcp

**Convert any OpenAPI spec into a working MCP server — no code required.**

specmcp runs a load → normalize → simplify → expose pipeline at startup and then answers MCP `tools/list` and `tools/call` requests by proxying the real upstream API. Give it a spec URL or file and a config file, and your LLM can call the API immediately.

```
specmcp serve --spec https://api.example.com/openapi.json
```

---

## Install

```sh
pip install specmcp
```

Requires Python 3.10+.

---

## Quickstart

### 1. Scaffold a config file

```sh
specmcp init --spec https://petstore3.swagger.io/api/v3/openapi.json
```

This writes `mcp.config.yaml` and `.env.example` to the current directory.

### 2. Start the server

```sh
specmcp serve --config mcp.config.yaml
```

The server runs over **stdio** by default, which is what Claude Desktop and most MCP clients expect. You should see something like:

```
specmcp serving 19 tools from https://petstore3.swagger.io/api/v3/openapi.json [transport=stdio]
```

### 3. Point your MCP client at it

In Claude Desktop's `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "petstore": {
      "command": "specmcp",
      "args": ["serve", "--config", "/path/to/mcp.config.yaml"]
    }
  }
}
```

---

## Config file reference

`mcp.config.yaml` controls everything: which spec to load, which operations to expose, auth credentials, timeouts, and more.

```yaml
version: "1"

spec:
  source: ./openapi.json          # path or URL

auth:
  myKey:
    type: apiKey
    in: header                    # header | query | cookie
    name: X-API-Key
    value_from: env(MY_API_KEY)   # env(VAR) or literal(value)

server:
  base_url_override: https://api.example.com   # override spec's servers[]
  include_operations:            # allowlist by operationId
    - listPets
    - createPet
  exclude_tags:
    - admin

dispatch:
  default_timeout_seconds: 30
  response_size_limit_bytes: 1048576   # 1 MiB
  text_truncate_bytes: 65536           # 64 KiB

simplify:
  truncate_description_chars: 400
  drop_spec_metadata: true
```

### Auth types

#### API key

```yaml
auth:
  myKey:
    type: apiKey
    in: header          # header | query | cookie
    name: X-API-Key
    value_from: env(MY_API_KEY)
```

#### Bearer token

```yaml
auth:
  myToken:
    type: bearer
    value_from: env(MY_BEARER_TOKEN)
```

#### OAuth 2.0 — Client Credentials

specmcp fetches a short-lived access token at the `token_url` and refreshes it automatically before expiry. No user interaction required.

```yaml
auth:
  myApi:
    type: oauth2_client_credentials
    token_url: https://auth.example.com/oauth/token
    client_id_from: env(MY_CLIENT_ID)
    client_secret_from: env(MY_CLIENT_SECRET)
    scopes:
      - read
      - write
```

#### OAuth 2.0 — Authorization Code + PKCE

For APIs that require user-delegated OAuth (GitHub, Google, Salesforce, etc.). Requires `--transport http` so specmcp can receive the IdP callback.

```yaml
auth:
  myOAuth:
    type: oauth2_authorization_code
    authorization_url: https://auth.example.com/oauth/authorize
    token_url: https://auth.example.com/oauth/token
    redirect_uri: http://localhost:8765/auth/callback
    client_id_from: env(MY_CLIENT_ID)
    client_secret_from: env(MY_CLIENT_SECRET)
    scopes:
      - read
      - write
```

See [OAuth 2.0 Authorization Code flow](#oauth-20-authorization-code-flow) below for the full walkthrough.

---

## CLI reference

```
specmcp serve [OPTIONS]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--spec` / `-s` | *(from config)* | Path or URL to the OpenAPI spec |
| `--config` / `-c` | `mcp.config.yaml` | Config file path |
| `--transport` / `-t` | `stdio` | `stdio` or `http` |
| `--watch` / `-w` | `false` | Hot-reload spec/config on file change |
| `--verbose` / `-v` | `false` | DEBUG-level logging |
| `--management-port` | `8766` | Port for the management listener (HTTP transport only) |
| `--management-bind` | `loopback` | `loopback` or `all` |
| `--token-store` | `memory` | `memory` or `sqlite` (encrypted at rest) |
| `--token-store-path` | `~/.specmcp/tokens.db` | SQLite database path |
| `--token-store-key-env` | `SPECMCP_TOKEN_KEY` | Env var holding the encryption key |

Other commands:

```
specmcp init      # scaffold mcp.config.yaml + .env.example
specmcp inspect   # list all exposed tools without starting the server
specmcp validate  # lint the spec and config and exit with a status code
```

---

## HTTP transport

Start the server over HTTP/SSE instead of stdio:

```sh
specmcp serve --config mcp.config.yaml --transport http
```

The MCP server listens on `http://127.0.0.1:8765` by default.

```yaml
transport:
  http:
    host: "127.0.0.1"
    port: 8765
```

Use HTTP transport when you need:
- Multiple simultaneous MCP clients (one SSE connection each)
- OAuth 2.0 Authorization Code flows (requires an HTTP callback endpoint)
- Per-session auth tokens

---

## OAuth 2.0 Authorization Code flow

When `oauth2_authorization_code` is configured and the server is running with `--transport http`, the flow works like this:

1. **LLM calls a tool** — specmcp checks whether the session has a valid token.
2. **No token** — specmcp returns an `AuthRequired` error containing a `login_url`.
3. **Client presents the URL** — the user opens the URL in a browser.
4. **User authenticates** — the IdP redirects to specmcp's `/auth/callback`.
5. **Token stored** — specmcp exchanges the code for tokens and stores them.
6. **LLM retries** — the tool call proceeds with the access token in the `Authorization` header.

Subsequent calls use the stored token (silently refreshed before expiry).

### Start the HTTP server

```sh
specmcp serve \
  --config mcp.config.yaml \
  --transport http \
  --token-store sqlite \
  --token-store-key-env SPECMCP_TOKEN_KEY
```

Set the encryption key for the SQLite token store:

```sh
export SPECMCP_TOKEN_KEY="$(openssl rand -hex 32)"
```

### OAuth endpoints

| Route | Purpose |
|-------|---------|
| `GET /auth/login?nonce=<token>` | Redirect to the IdP authorization page |
| `GET /auth/callback?code=&state=` | Exchange the authorization code for tokens |
| `GET /auth/status?session=<id>` | Poll `{"authenticated": true\|false}` |
| `DELETE /auth/session/<id>` | Revoke a session's tokens (management endpoint) |

The management endpoint (`DELETE /auth/session/<id>`) runs on a dedicated port (default `8766`) bound to loopback only. Set `--management-bind all` and `management.management_token_from` in config to expose it externally with Bearer auth.

---

## Hot-reload (--watch)

```sh
specmcp serve --config mcp.config.yaml --watch
```

Watches the spec and config files for changes and atomically reloads the `ToolRegistry` without dropping the stdio connection. Useful when iterating on a spec. Requires `watchfiles`:

```sh
pip install watchfiles
```

> **Note:** changes to the `auth:` section of `mcp.config.yaml` are not picked up on hot-reload. A full restart is required for auth changes.

---

## Examples

The `examples/` directory contains ready-to-run configs:

| Example | Description |
|---------|-------------|
| `examples/petstore/` | Swagger Petstore — basic quickstart |
| `examples/github/` | GitHub REST API (bearer token) |
| `examples/stripe/` | Stripe API (API key) |
| `examples/oauth2-example/` | OAuth 2.0 Authorization Code + PKCE |

Run any example:

```sh
export GITHUB_TOKEN=ghp_...
specmcp serve --config examples/github/mcp.config.yaml
```

---

## How it works

Point specmcp at any OpenAPI spec and it instantly becomes an MCP server. Every operation in the spec becomes a callable tool — with a name, description, and typed arguments that the LLM can use directly.

specmcp handles all the translation automatically: it reads the spec, builds the tools, injects your credentials, and proxies calls to the real upstream API. No code generation, no wrappers to maintain. When the spec changes, restart specmcp (or use `--watch`) and the tools update automatically.

---

## License

Apache 2.0
