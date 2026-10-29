# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Project foundation: `uv` packaging, ruff, mypy (strict), pytest with an 85 % coverage gate,
  pre-commit hooks (ruff, mypy, Gitleaks).
- Manifest (`dr.yaml`) models with strict validation, `${VAR}` interpolation from the
  environment and `repository.env_file`, precise error messages that never echo values, and
  owner-only permission checks for the manifest, password file and env file.
- Annotated example manifest in `examples/dr.yaml`.
- Subprocess runner with mandatory timeouts, streaming pipes that report both exit codes, and
  secret redaction; structured logging (JSON off a TTY) with redaction; host-wide run lock.
- Error hierarchy mapped to documented exit codes.
- CLI skeleton with `--config`, `--json`, `--verbose` and `--version`.
- `drkit doctor`: checks the manifest, file permissions and dependencies (restic, Docker,
  Compose, git, and Ansible when configured), with fix hints and `--json` output.
- CI (lint, types, unit tests, build, Gitleaks), Dependabot, issue and pull request templates.
- `SECURITY.md`, `CONTRIBUTING.md`, MIT license and ADRs 0001–0003.

[Unreleased]: https://github.com/caiodassumpcao/drkit/commits/main
