"""TLS certificates of a recovered host: restored from the backup, or issued again by certbot.

Which certificates exist, and for which names, is read from the enabled nginx configuration:
every ``ssl_certificate /etc/letsencrypt/live/<name>/…`` is a certificate to bring back, for
the ``server_name`` values of the server blocks that use it.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import structlog

from drkit import runner
from drkit.errors import CommandError

LETSENCRYPT = Path("/etc/letsencrypt")
NGINX_DIR = Path("/etc/nginx")
CERTBOT_TIMEOUT_S = 300.0
COMMAND_TIMEOUT_S = 60.0

_SERVER_BLOCK = re.compile(r"\bserver\s*\{")
_SERVER_NAME = re.compile(r"\bserver_name\s+([^;]+);")
_CERTIFICATE = re.compile(r"\bssl_certificate\s+\"?/etc/letsencrypt/live/([^/\s;\"]+)/")

log = structlog.get_logger(__name__)


def server_blocks(nginx_dir: Path = NGINX_DIR) -> Iterator[str]:
    """The ``server { … }`` blocks of the enabled configuration (comments removed)."""
    files = [nginx_dir / "nginx.conf"]
    for directory in ("conf.d", "sites-enabled"):
        if (nginx_dir / directory).is_dir():
            files += sorted((nginx_dir / directory).iterdir())
    for path in files:
        if not path.is_file():
            continue
        text = re.sub(r"#[^\n]*", "", path.read_text(encoding="utf-8", errors="replace"))
        for match in _SERVER_BLOCK.finditer(text):
            depth, end = 1, match.end()
            while depth and end < len(text):
                depth += {"{": 1, "}": -1}.get(text[end], 0)
                end += 1
            yield text[match.end() : end - 1]


def domains(block: str) -> list[str]:
    """Public names of a server block: no catch-all, wildcard, regex or local names."""
    names = [name for match in _SERVER_NAME.finditer(block) for name in match.group(1).split()]
    return [
        name
        for name in names
        if name not in ("_", "localhost", '""')
        and not name.startswith(("*", "~", "."))
        and not re.fullmatch(r"[\d.:]+", name)
    ]


def certificates(nginx_dir: Path = NGINX_DIR) -> dict[str, list[str]]:
    """Let's Encrypt certificate name → the domains of the server blocks that use it."""
    found: dict[str, list[str]] = {}
    for block in server_blocks(nginx_dir):
        for name in _CERTIFICATE.findall(block):
            names = found.setdefault(name, [])
            names += [d for d in domains(block) if d not in names]
    return found


def all_domains(nginx_dir: Path = NGINX_DIR) -> list[str]:
    """Every public name served by nginx (for the DNS checklist)."""
    names: list[str] = []
    for block in server_blocks(nginx_dir):
        names += [d for d in domains(block) if d not in names]
    return names


def reissue(email: str, nginx_dir: Path = NGINX_DIR, live_dir: Path = LETSENCRYPT) -> str:
    """Issue again, with certbot's standalone server, each certificate nginx needs.

    Certificates already present are kept. nginx is stopped first, since certbot needs
    port 80 and nginx cannot start until its certificates exist; it is started again by
    the next step.
    """
    missing = {
        name: names
        for name, names in certificates(nginx_dir).items()
        if not (live_dir / "live" / name / "fullchain.pem").exists()
    }
    if not missing:
        return "no certificate to issue"
    runner.run(["systemctl", "stop", "nginx"], timeout=COMMAND_TIMEOUT_S, check=False)
    issued = []
    for name, names in missing.items():
        if not names:
            raise CommandError(
                f"certificate {name!r} is used by nginx, but no server_name says for which domain",
                hint="set server_name in the server blocks that use it",
            )
        log.info("tls.reissue", certificate=name, domains=names)
        args = [
            *("certbot", "certonly", "--standalone", "--non-interactive", "--agree-tos"),
            *("--email", email, "--cert-name", name),
        ]
        for domain in names:
            args += ["-d", domain]
        result = runner.run(args, timeout=CERTBOT_TIMEOUT_S, check=False)
        if not result.ok:
            raise CommandError(
                f"certbot could not issue {name!r} ({', '.join(names)}): "
                f"{runner.failure_detail(result, max_lines=3)}",
                hint="the DNS records must already point to this server and port 80 be "
                "reachable from the internet; fix them and resume with --from-step 6",
            )
        issued.append(f"{name} ({', '.join(names)})")
    return f"{len(issued)} certificate(s) issued: {'; '.join(issued)}"


def missing_certificates_only(nginx_output: str) -> bool:
    """True if ``nginx -t`` failed only because Let's Encrypt files are not there yet."""
    errors = [line for line in nginx_output.splitlines() if "[emerg]" in line]
    return bool(errors) and all("/etc/letsencrypt/" in line for line in errors)
