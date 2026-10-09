"""Loopback-only GitHub App manifest setup and secure GitHub secret entry."""

from __future__ import annotations

import argparse
import html
import json
import secrets
import subprocess
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

REPOSITORY = "Karapsin/analytics_toolkit"


def gh(*args: str, data: str | None = None) -> str:
    result = subprocess.run(["gh", *args], input=data, text=True, capture_output=True, check=False)
    if result.returncode:
        # Never expose API response data (manifest conversions contain private keys).
        msg = "GitHub setup command failed. Verify gh authentication and permissions."
        raise RuntimeError(msg)
    return result.stdout


def store_secret(name: str, value: str) -> None:
    gh("secret", "set", name, "--repo", REPOSITORY, data=value)


def app_manifest(port: int) -> dict[str, object]:
    return {
        "name": "analytics-toolkit-agent-" + secrets.token_hex(3),
        "url": "https://github.com/" + REPOSITORY,
        "redirect_url": f"http://127.0.0.1:{port}/callback",
        "public": False,
        "hook_attributes": {"url": "https://github.com/" + REPOSITORY, "active": False},
        "default_permissions": {
            "contents": "write",
            "pull_requests": "write",
            "issues": "write",
            "checks": "write",
            "actions": "read",
            "workflows": "write",
        },
        "default_events": [],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    csrf = secrets.token_urlsafe(32)
    setup: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:
            pass  # URLs contain one-time authorization codes; do not log requests.

        def page(self, content: str, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(
                ("<!doctype html><title>GitHub agent setup</title>" + content).encode()
            )

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            if parsed.path == "/callback":
                if query.get("state") != [csrf] or not query.get("code"):
                    self.page("Invalid setup callback.", 400)
                    return
                code = query["code"][0]
                if not code.isalnum():
                    self.page("Invalid authorization code.", 400)
                    return
                try:
                    app = json.loads(
                        gh("api", f"app-manifests/{code}/conversions", "--method", "POST")
                    )
                    store_secret("AGENT_APP_PRIVATE_KEY", app["pem"])
                    gh(
                        "variable",
                        "set",
                        "AGENT_APP_ID",
                        "--repo",
                        REPOSITORY,
                        "--body",
                        str(app["id"]),
                    )
                    gh(
                        "variable",
                        "set",
                        "AGENT_APP_SLUG",
                        "--repo",
                        REPOSITORY,
                        "--body",
                        app["slug"],
                    )
                    setup["slug"] = app["slug"]
                except (RuntimeError, ValueError, KeyError):
                    self.page(
                        "App setup failed. Verify GitHub authentication; "
                        "no secrets were displayed.",
                        500,
                    )
                    return
                self.page(
                    "<p>App created; its private key is stored securely in GitHub.</p>"
                    '<p><a href="https://github.com/apps/'
                    f'{html.escape(app["slug"])}/installations/new">'
                    "Install the App on analytics_toolkit only</a></p>"
                    "<p>The private worker uses your saved ChatGPT login; no API key is needed.</p>"
                )
            else:
                port = self.server.server_address[1]
                manifest = app_manifest(port)
                self.page(
                    "<h1>Create repository GitHub agent</h1>"
                    "<p>Creates a private App for review, merge, and integration repair.</p>"
                    f'<form method="post" action="https://github.com/settings/apps/new?state={csrf}">'
                    '<input type="hidden" name="manifest" value="'
                    f'{html.escape(json.dumps(manifest), quote=True)}">'
                    "<button>Create GitHub App</button></form>"
                )

    server = HTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{server.server_address[1]}"
    print("Secure local setup: " + url, flush=True)
    webbrowser.open(url)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
