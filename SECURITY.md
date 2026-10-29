# Security policy

drkit handles the most sensitive data on a server: database contents, application secrets and
the keys to its backups. Security reports are taken seriously and handled with priority.

## Supported versions

drkit is pre-1.0. Only the latest release receives fixes.

| Version | Supported |
|---|---|
| latest release | ✅ |
| older releases | ❌ |

## Reporting a vulnerability

**Do not open a public issue.** Report privately through
[GitHub Security Advisories](https://github.com/caiodassumpcao/drkit/security/advisories/new).

Please include:

- the affected version (`drkit --version`) and environment;
- a description of the issue and its impact;
- steps to reproduce, with any secrets redacted.

You can expect an acknowledgement within **3 business days** and a status update within
**10 business days**. Once a fix is released, the advisory is published with credit to the
reporter (unless you prefer to stay anonymous).

## Scope

In scope, for example:

- secrets (passwords, tokens, keys, credentials in URLs) leaking into logs, errors, reports or
  notifications;
- drkit accepting a manifest, password file or env file with unsafe permissions;
- command injection through manifest values other than the documented, trusted `hooks`;
- restores writing outside the intended paths.

Out of scope:

- `hooks` executing arbitrary commands: they are trusted input, run as root from a root-owned
  `0600` manifest, by design;
- vulnerabilities in restic, Docker or other external tools (report them upstream);
- loss of the restic password or storage credentials: these are the recovery keys and must be
  kept outside the server (see the README).
