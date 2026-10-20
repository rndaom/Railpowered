"""Static dashboard files (web/), page templates and HTTP security headers."""

from __future__ import annotations

import html
import os
import re

from . import config

CSP = (
    "default-src 'self'; img-src 'self' data:; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; script-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "DENY",
}
STATIC_CACHE = "public, max-age=300"

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".webmanifest": "application/manifest+json",
    ".txt": "text/plain; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
}
STATIC_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

LOGIN_ERRORS = {
    "1": "That key didn't match. Check it and try again.",
    "rate": "Too many attempts. Wait a few minutes and try again.",
}

FALLBACK_LOGIN = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Railpowered</title>
</head>
<body>
<main>
<h1>Railpowered</h1>
<p>{{error}}</p>
<form method="post" action="/login">
<label for="key">Admin key</label>
<input id="key" type="password" name="key" autocomplete="current-password" required autofocus>
<button type="submit">Open dashboard</button>
</form>
<p>Railpowered accepts the
<a href="https://www.minecraft.net/eula">Minecraft EULA</a> on your behalf.</p>
</main>
</body>
</html>
"""

FALLBACK_INDEX = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Railpowered</title>
</head>
<body>
<main>
<h1>Railpowered</h1>
<p>The dashboard files (web/) are missing from this image. The server and its
API still work; rebuild the image to restore the dashboard.</p>
<form method="post" action="/logout"><button type="submit">Log out</button></form>
</main>
</body>
</html>
"""


def web_dir() -> str | None:
    return config.resolve_web_dir()


def _inside(base: str, path: str) -> bool:
    base_real = os.path.realpath(base)
    real = os.path.realpath(path)
    return real != base_real and os.path.commonpath([base_real, real]) == base_real


def static_file(relative: str) -> tuple[str, str] | None:
    """(absolute path, content type) for /static/<relative>, or None.

    Only `<name>` or `assets/<name>` with a strict name and a known extension.
    """
    parts = relative.split("/")
    if len(parts) == 2 and parts[0] == "assets":
        name = parts[1]
    elif len(parts) == 1:
        name = parts[0]
    else:
        return None
    if not STATIC_NAME.match(name) or ".." in name:
        return None
    content_type = CONTENT_TYPES.get(os.path.splitext(name)[1].lower())
    if content_type is None:
        return None
    base = web_dir()
    if base is None:
        return None
    path = os.path.join(base, *parts)
    if not _inside(base, path) or not os.path.isfile(path):
        return None
    return path, content_type


def favicon() -> tuple[str, str] | None:
    for candidate in ("favicon.ico", "favicon.svg", "assets/favicon.svg", "assets/logo.svg"):
        found = static_file(candidate)
        if found:
            return found
    return None


def _read_page(name: str) -> str | None:
    base = web_dir()
    if base is None:
        return None
    path = os.path.join(base, name)
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return None


def login_page(error: str | None) -> str:
    page = _read_page("login.html") or FALLBACK_LOGIN
    message = LOGIN_ERRORS.get(error or "", "")
    return page.replace("{{error}}", html.escape(message))


def index_page() -> str:
    return _read_page("index.html") or FALLBACK_INDEX
