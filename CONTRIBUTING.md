# Contributing

Thanks for your interest in drkit! This guide covers the development workflow. The technical
source of truth is the [design specification](docs/DESIGN.md): changes in behavior must update
it (or add an [ADR](docs/adr/)) in the same pull request.

## Setup

Requirements: Linux (or WSL2), Python 3.12, [uv](https://docs.astral.sh/uv/), Docker with
Compose v2 and restic ≥ 0.17 for integration tests.

```bash
git clone https://github.com/caiodassumpcao/drkit.git && cd drkit
uv sync
uv run pre-commit install
```

The pre-commit hooks run ruff (lint + format), mypy and [Gitleaks](https://github.com/gitleaks/gitleaks).
Do not bypass them with `--no-verify`; fix the cause instead.

## Checks

Every commit must pass:

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy                    # strict mode, src and tests
uv run pytest                  # unit tests, coverage gate at 85 %
uv run pytest -m integration   # needs Docker
```

## Code guidelines

- Type hints everywhere; `mypy --strict` must stay clean.
- No `print`: user output goes through `rich`, logs through `structlog`.
- External commands only through `drkit.runner` (argument lists, mandatory timeout, secret
  redaction). Never `shell=True`, except for the documented manifest hooks.
- Expected failures raise a subclass of `drkit.errors.DrkitError` with an actionable message
  (what failed + how to fix it), which maps to a documented exit code.
- Never put secret values in messages, logs or exceptions.
- Tests that need Docker or external services are marked `@pytest.mark.integration`.

## Commits and pull requests

- [Conventional Commits](https://www.conventionalcommits.org/), imperative mood, subject up to
  72 characters, no trailing period. Explain the *why* in the body when it is not obvious.
  Types: `feat`, `fix`, `refactor`, `test`, `docs`, `ci`, `build`, `chore`, `perf`.
  Example: `feat(postgres): stream pg_dump into restic via stdin`.
- Atomic commits: one topic per commit, each one passing the checks above.
- Signed commits are expected.
- Pull requests follow the [template](.github/pull_request_template.md), update
  [`CHANGELOG.md`](CHANGELOG.md) under *Unreleased* and need a green CI.
