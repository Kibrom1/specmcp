# Contributing to specmcp

Thank you for wanting to improve specmcp. This document covers how to set up a dev environment, run the test suite, and submit changes.

---

## Development setup

Requires Python 3.10 or later.

```bash
git clone https://github.com/specmcp/specmcp
cd specmcp
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

Verify everything works:

```bash
specmcp --version
pytest
```

---

## Running tests

```bash
# All unit tests (fast, no network)
pytest tests/unit/

# Integration tests against bundled spec corpus
pytest tests/integration/

# Full suite including slow corpus tests
pytest --run-corpus

# A specific test file
pytest tests/unit/runtime/test_oauth_handler.py -v
```

Tests use `pytest-asyncio` (auto mode), `respx` for mocking outbound HTTP, and `syrupy` for snapshot assertions. Update snapshots when you intentionally change output:

```bash
pytest --snapshot-update
```

---

## Code style

```bash
ruff check src/ tests/           # lint
ruff format src/ tests/          # format
mypy src/specmcp                 # type-check
```

All three must pass cleanly before opening a PR. The CI workflow runs them automatically.

---

## Project structure

```
src/specmcp/
├── cli/          — Typer commands (serve, init, inspect, validate, report-issue)
├── core/         — Pure pipeline: load → normalize → simplify → expose
├── auth/         — Auth injection + OAuth2 (bearer, apiKey, client_creds, auth code)
├── runtime/      — Async HTTP client, dispatcher, OAuth Starlette routes, session
├── config.py     — Pydantic Config model (mcp.config.yaml)
└── errors.py     — SpecmcpError hierarchy
```

See [CLAUDE.md](CLAUDE.md) for a deeper map including data flow and key design contracts.

---

## Adding a new auth scheme type

1. Add the config model to `config.py` (follow `ApiKeyAuthConfig`).
2. Add the new config to the `AuthSchemeConfig` union type alias.
3. Add an injection handler in `auth/injector.py` (add an entry to `_HANDLERS`).
4. Update `Config.scaffold()` in `config.py` to generate the YAML block.
5. Update `init.py` to detect and pass the scheme to `auth_scheme_list`.
6. Write tests under `tests/unit/test_auth.py`.

---

## Adding a new CLI command

1. Create `src/specmcp/cli/<command>.py` with a `@app.command("<name>")` function.
2. Import it in `src/specmcp/cli/__init__.py` so it is registered.
3. Follow the existing pattern: load config → run pipeline → handle `SpecmcpError` → emit output.
4. Add tests under `tests/unit/`.

---

## Pull request checklist

- [ ] `pytest` passes (all unit + integration tests).
- [ ] `ruff check` and `ruff format --check` pass.
- [ ] `mypy` passes with no new errors.
- [ ] New behaviour is covered by tests.
- [ ] Security-relevant changes (auth, credential handling, OAuth) include a brief threat-model note in the PR description.
- [ ] CHANGELOG.md updated under `## [Unreleased]` if the change is user-visible.

---

## Reporting bugs

Run `specmcp report-issue --spec your-spec.yaml` to produce a sanitized debug bundle (no credential values), then paste it into a [new GitHub issue](https://github.com/specmcp/specmcp/issues/new).

---

## Security disclosures

Please do not open a public GitHub issue for security vulnerabilities. Email the maintainers directly; details are in the repository's security policy (`SECURITY.md` if present, otherwise the GitHub Security tab).

---

## License

By contributing you agree that your changes will be licensed under the [Apache-2.0 License](LICENSE).
