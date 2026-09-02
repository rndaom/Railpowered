"""Runtime paths, ports, and admin-panel credentials."""

from __future__ import annotations

import base64
import hashlib
import os

import installer

MC_DIR = installer.DATA_DIR
MC_PORT = 25565
WEB_PORT = int(os.environ.get("PORT", 8080))
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
ADMIN_KEY = os.environ.get("ADMIN_KEY", "")
ADMIN_KEY_FROM_ENV = bool(ADMIN_KEY)
if not ADMIN_KEY:
    ADMIN_KEY = base64.urlsafe_b64encode(os.urandom(18)).decode()
ADMIN_TOKEN = hashlib.sha256(ADMIN_KEY.encode()).hexdigest()[:32]
MC_PUBLIC_ADDRESS = os.environ.get("MC_PUBLIC_ADDRESS", "").strip()

LOGIN_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Admin Login</title>
  <style>
    *{margin:0;padding:0;box-sizing:border-box}
    body{font-family:'Segoe UI',system-ui,sans-serif;background:#1a1a2e;color:#eee;
         display:flex;align-items:center;justify-content:center;min-height:100vh}
    .card{background:#16213e;border-radius:8px;padding:32px;width:340px}
    h2{color:#4ecca3;margin-bottom:16px;text-align:center}
    input{width:100%;padding:10px;border-radius:6px;border:1px solid #333;
          background:#0f3460;color:#eee;font-size:1rem;margin-bottom:14px}
    input:focus{outline:none;border-color:#4ecca3}
    button{width:100%;padding:10px;border-radius:6px;border:none;
           background:#4ecca3;color:#111;font-weight:600;font-size:1rem;cursor:pointer}
    button:hover{opacity:.9}
    .error{color:#e94560;font-size:.9rem;margin-bottom:12px;text-align:center}
  </style>
</head>
<body>
  <div class="card">
    <h2>Admin Login</h2>
    {error}
    <form method="POST" action="/api/login">
      <input type="password" name="key" placeholder="Admin key" autofocus>
      <button type="submit">Login</button>
    </form>
  </div>
</body>
</html>"""
