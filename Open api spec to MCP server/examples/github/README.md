# GitHub example

Exposes a curated subset of the GitHub REST API as MCP tools. Useful for
LLM agents that need to read and create issues, search repos, and review PRs.

## Setup

```bash
cp .env.example .env
# Edit .env — create a token at https://github.com/settings/tokens
# Required scopes: repo, read:user
export $(cat .env | xargs)
```

## Preview tools

```bash
specmcp inspect --config mcp.config.yaml
```

Note: the GitHub spec is large (~40 MB). The first `inspect` or `serve` run
will take a few seconds to download and resolve `$ref`s. Subsequent runs
are faster if you cache the spec locally.

## Run as MCP server

```bash
specmcp serve --config mcp.config.yaml
```

## Tools exposed (curated subset)

| Tool | Description |
|---|---|
| `get_repo` | Get a repository by owner/name |
| `list_my_repos` | List the authenticated user's repositories |
| `list_issues` | List issues for a repo (filterable by state) |
| `get_issue` | Get a single issue by number |
| `create_issue` | Create a new issue |
| `comment_on_issue` | Add a comment to an issue |
| `list_pull_requests` | List PRs for a repo |
| `get_pull_request` | Get a single PR by number |
| `search_repos` | Search GitHub repositories |
| `search_issues` | Search issues and PRs |

## Expanding coverage

Remove or modify `include_operations` in `mcp.config.yaml` to expose more
of the ~900 GitHub API endpoints. Use `specmcp inspect` to preview the
resulting tool list before serving.
