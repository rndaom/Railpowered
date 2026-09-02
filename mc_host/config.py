"""Runtime paths, ports, and admin-panel credentials."""

from __future__ import annotations

import base64
import hashlib
import os
from collections.abc import Mapping

import installer

ADMIN_KEY_FILE = ".admin-key"


def resolve_public_address(env: Mapping[str, str] | None = None) -> str:
    """Join address: explicit override, else Railway's TCP proxy host:port."""
    environ = os.environ if env is None else env
    override = str(environ.get("MC_PUBLIC_ADDRESS", "")).strip()
    if override and not _unevaluated_template(override):
        return override
    domain = str(environ.get("RAILWAY_TCP_PROXY_DOMAIN", "")).strip()
    port = str(environ.get("RAILWAY_TCP_PROXY_PORT", "")).strip()
    if domain and port:
        return f"{domain}:{port}"
    return ""


def resolve_admin_key(
    data_dir: str, env: Mapping[str, str] | None = None
) -> tuple[str, bool]:
    """Use ADMIN_KEY when set; otherwise reuse or create a key on the volume."""
    environ = os.environ if env is None else env
    env_key = str(environ.get("ADMIN_KEY", "")).strip()
    if env_key and not _unevaluated_template(env_key):
        return env_key, True
    path = os.path.join(data_dir, ADMIN_KEY_FILE)
    try:
        with open(path, encoding="utf-8") as handle:
            stored = handle.read().strip()
        if stored:
            return stored, False
    except OSError:
        pass
    key = base64.urlsafe_b64encode(os.urandom(18)).decode()
    try:
        os.makedirs(data_dir, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(key)
    except OSError:
        pass
    return key, False


def _unevaluated_template(value: str) -> bool:
    return value.startswith("${{") and "}}" in value


def resolve_web_port(
    env: Mapping[str, str] | None = None, minecraft_port: int = 25565
) -> int:
    """Dashboard port. Ignore Railway PORT when it collides with Minecraft."""
    environ = os.environ if env is None else env
    for key in ("WEB_PORT", "PORT"):
        raw = str(environ.get(key, "")).strip()
        if not raw.isdigit():
            continue
        port = int(raw)
        if 1 <= port <= 65535 and port != minecraft_port:
            return port
    return 8080


MC_DIR = installer.DATA_DIR
MC_PORT = 25565
WEB_PORT = resolve_web_port()
MAX_MEMORY = os.environ.get("MC_MAX_MEMORY", "1G")
MIN_MEMORY = os.environ.get("MC_MIN_MEMORY", "512M")
IDLE_TIMEOUT = int(os.environ.get("IDLE_TIMEOUT", "600"))
AUTO_START = os.environ.get("AUTO_START", "false").lower() == "true"
TEMPLATE_DIR = os.environ.get("TEMPLATE_DIR", "/server/templates")
if not os.path.isdir(TEMPLATE_DIR):
    _local_templates = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates"
    )
    if os.path.isdir(_local_templates):
        TEMPLATE_DIR = _local_templates
ADMIN_KEY, ADMIN_KEY_FROM_ENV = resolve_admin_key(installer.DATA_DIR)
ADMIN_TOKEN = hashlib.sha256(ADMIN_KEY.encode()).hexdigest()[:32]
MC_PUBLIC_ADDRESS = resolve_public_address()

LOGIN_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Railpowered</title>
  <style>
    *{margin:0;padding:0;box-sizing:border-box}
    body{font-family:ui-sans-serif,system-ui,sans-serif;background:#16110c;color:#f4ead6;
         display:flex;align-items:center;justify-content:center;min-height:100vh;
         background-image:radial-gradient(900px 420px at 50% -10%, #4a3214 0%, transparent 58%)}
    .card{background:#221910;border:1px solid #453526;border-radius:16px;padding:36px;width:360px}
    .mark{width:56px;height:56px;margin:0 auto 16px;display:block}
    h2{color:#e4b23a;margin-bottom:6px;text-align:center;letter-spacing:.04em}
    p{color:#b09a7c;text-align:center;font-size:.92rem;margin-bottom:18px}
    .hint{font-size:.8rem;margin-top:14px;margin-bottom:0}
    input{width:100%;padding:12px;border-radius:10px;border:1px solid #453526;
          background:#16110c;color:#f4ead6;font-size:1rem;margin-bottom:14px}
    input:focus{outline:none;border-color:#e4b23a}
    button{width:100%;padding:12px;border-radius:10px;border:none;
           background:#e4b23a;color:#2a1c08;font-weight:700;font-size:1rem;cursor:pointer}
    button:hover{opacity:.92}
    .error{color:#e24a3c;font-size:.9rem;margin-bottom:12px;text-align:center}
  </style>
</head>
<body>
  <div class="card">
    <svg class="mark" viewBox="0 0 64 64" aria-hidden="true">
      <rect x="8" y="14" width="48" height="7" rx="1.5" fill="#6b4423"/>
      <rect x="8" y="28.5" width="48" height="7" rx="1.5" fill="#5a381c"/>
      <rect x="8" y="43" width="48" height="7" rx="1.5" fill="#6b4423"/>
      <rect x="18" y="10" width="6" height="44" rx="1.5" fill="#e4b23a"/>
      <rect x="40" y="10" width="6" height="44" rx="1.5" fill="#e4b23a"/>
      <rect x="29" y="26" width="6" height="16" rx="1" fill="#3d2a1a"/>
      <circle cx="32" cy="24" r="6" fill="#e24a3c"/>
      <circle cx="32" cy="24" r="3" fill="#ffd27a"/>
    </svg>
    <h2>Railpowered</h2>
    <p>Dashboard login</p>
    {error}
    <form method="POST" action="/api/login">
      <input type="password" name="key" placeholder="Admin key" autofocus>
      <button type="submit">Open dashboard</button>
    </form>
    <p class="hint">On Railway this is the ADMIN_KEY you set when you deployed.</p>
  </div>
</body>
</html>"""
