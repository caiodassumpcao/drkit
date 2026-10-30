from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from drkit import runner
from drkit.errors import CommandError
from drkit.restore import tls
from drkit.runner import CommandResult

SITE = """
# shop.example.com is the store
server {
    listen 80;
    server_name shop.example.com www.shop.example.com;   # both names
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name shop.example.com www.shop.example.com;
    ssl_certificate /etc/letsencrypt/live/shop.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/shop.example.com/privkey.pem;
    location / {
        proxy_pass http://127.0.0.1:8080;
    }
}
"""

API = """
server {
    listen 443 ssl;
    server_name api.example.com *.api.example.com ~^(.+)\\.example\\.com$;
    ssl_certificate "/etc/letsencrypt/live/api/fullchain.pem";
}
server {
    listen 80 default_server;
    server_name _ localhost 127.0.0.1;
}
"""


@pytest.fixture
def nginx(tmp_path: Path) -> Path:
    (tmp_path / "sites-enabled").mkdir()
    (tmp_path / "sites-available").mkdir()
    (tmp_path / "conf.d").mkdir()
    (tmp_path / "nginx.conf").write_text("http { include sites-enabled/*; }\n")
    (tmp_path / "sites-enabled" / "shop").write_text(SITE)
    (tmp_path / "conf.d" / "api.conf").write_text(API)
    # Not enabled: never issued, never in the DNS checklist.
    (tmp_path / "sites-available" / "old").write_text(
        "server { server_name old.example.com; "
        "ssl_certificate /etc/letsencrypt/live/old/fullchain.pem; }"
    )
    return tmp_path


def test_certificates_and_their_domains(nginx: Path) -> None:
    assert tls.certificates(nginx) == {
        "api": ["api.example.com"],
        "shop.example.com": ["shop.example.com", "www.shop.example.com"],
    }


def test_all_domains(nginx: Path) -> None:
    assert tls.all_domains(nginx) == [
        "api.example.com",
        "shop.example.com",
        "www.shop.example.com",
    ]


def test_no_nginx_configuration(tmp_path: Path) -> None:
    assert tls.certificates(tmp_path) == {}


class TestReissue:
    @pytest.fixture
    def calls(self, monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
        calls: list[list[str]] = []

        def run(args: Sequence[str], **kwargs: Any) -> CommandResult:
            calls.append(list(args))
            code = 1 if "fail.example.com" in args else 0
            return CommandResult(tuple(args), code, "", "Challenge failed for domain", 0.0)

        monkeypatch.setattr(runner, "run", run)
        return calls

    def test_missing_certificates_are_issued(
        self, nginx: Path, tmp_path: Path, calls: list[list[str]]
    ) -> None:
        live = tmp_path / "letsencrypt"
        (live / "live" / "api").mkdir(parents=True)
        (live / "live" / "api" / "fullchain.pem").write_text("cert")

        detail = tls.reissue("ops@example.com", nginx, live)

        assert detail == (
            "1 certificate(s) issued: shop.example.com (shop.example.com, www.shop.example.com)"
        )
        assert calls == [
            ["systemctl", "stop", "nginx"],
            [
                *("certbot", "certonly", "--standalone", "--non-interactive", "--agree-tos"),
                *("--email", "ops@example.com", "--cert-name", "shop.example.com"),
                *("-d", "shop.example.com", "-d", "www.shop.example.com"),
            ],
        ]

    def test_nothing_to_issue(self, tmp_path: Path, calls: list[list[str]]) -> None:
        assert tls.reissue("ops@example.com", tmp_path, tmp_path) == "no certificate to issue"
        assert calls == []

    def test_certbot_failure(self, tmp_path: Path, calls: list[list[str]]) -> None:
        (tmp_path / "nginx.conf").write_text(
            "server { server_name fail.example.com; "
            "ssl_certificate /etc/letsencrypt/live/fail/fullchain.pem; }"
        )
        with pytest.raises(CommandError, match="certbot could not issue 'fail'") as exc:
            tls.reissue("ops@example.com", tmp_path, tmp_path)
        assert exc.value.hint is not None
        assert "--from-step 6" in exc.value.hint

    def test_certificate_without_a_domain(self, tmp_path: Path, calls: list[list[str]]) -> None:
        (tmp_path / "nginx.conf").write_text(
            "server { server_name _; ssl_certificate /etc/letsencrypt/live/x/fullchain.pem; }"
        )
        with pytest.raises(CommandError, match="no server_name says for which domain"):
            tls.reissue("ops@example.com", tmp_path, tmp_path)


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        (
            'nginx: [emerg] cannot load certificate "/etc/letsencrypt/live/shop/fullchain.pem"\n'
            "nginx: configuration file /etc/nginx/nginx.conf test failed",
            True,
        ),
        ('nginx: [emerg] unknown directive "proxy_passs" in /etc/nginx/sites-enabled/shop', False),
        ("", False),
    ],
)
def test_missing_certificates_only(output: str, expected: bool) -> None:
    assert tls.missing_certificates_only(output) is expected
