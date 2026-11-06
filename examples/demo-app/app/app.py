"""Demo application: stores uploads in a volume and reports the health of its databases.

    GET  /health          200 when PostgreSQL and MySQL accept connections
    PUT  /uploads/<name>  store the request body
    GET  /uploads/<name>  return a stored file
    GET  /uploads/        list the stored files

Standard library only, so the stock Python image runs it as is.
"""

import os
import socket
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

UPLOADS = Path(os.environ.get("UPLOADS_DIR", "/data/uploads"))
DATABASES = (("db", 5432), ("mysql", 3306))


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/health":
            down = [host for host, port in DATABASES if not reachable(host, port)]
            if down:
                self.reply(HTTPStatus.SERVICE_UNAVAILABLE, f"down: {', '.join(down)}\n".encode())
            else:
                self.reply(HTTPStatus.OK, b"ok\n")
        elif self.path == "/uploads/":
            names = sorted(p.name for p in UPLOADS.iterdir() if p.is_file())
            self.reply(HTTPStatus.OK, "".join(f"{name}\n" for name in names).encode())
        elif (path := self.upload()) is not None and path.is_file():
            self.reply(HTTPStatus.OK, path.read_bytes())
        else:
            self.reply(HTTPStatus.NOT_FOUND, b"not found\n")

    def do_PUT(self) -> None:
        path = self.upload()
        if path is None:
            self.reply(HTTPStatus.BAD_REQUEST, b"PUT /uploads/<name>\n")
            return
        length = int(self.headers.get("Content-Length", "0"))
        path.write_bytes(self.rfile.read(length))
        self.reply(HTTPStatus.CREATED, b"stored\n")

    def upload(self) -> Path | None:
        name = self.path.removeprefix("/uploads/")
        if not self.path.startswith("/uploads/") or not name or "/" in name or name[0] == ".":
            return None
        return UPLOADS / name

    def reply(self, status: HTTPStatus, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def reachable(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


if __name__ == "__main__":
    UPLOADS.mkdir(parents=True, exist_ok=True)
    ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()  # noqa: S104 - in a container
