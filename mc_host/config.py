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
  <title>Campfire</title>
  <style>
    *{margin:0;padding:0;box-sizing:border-box}
    body{font-family:ui-sans-serif,system-ui,sans-serif;background:#0b1220;color:#e7edf5;
         display:flex;align-items:center;justify-content:center;min-height:100vh}
    .card{background:#121a2b;border:1px solid #22304a;border-radius:16px;padding:36px;width:360px}
    .mark{width:56px;height:56px;margin:0 auto 16px;display:block}
    h2{color:#4ade80;margin-bottom:6px;text-align:center;letter-spacing:.04em}
    p{color:#8b9bb4;text-align:center;font-size:.92rem;margin-bottom:18px}
    input{width:100%;padding:12px;border-radius:10px;border:1px solid #2a3a55;
          background:#0b1220;color:#e7edf5;font-size:1rem;margin-bottom:14px}
    input:focus{outline:none;border-color:#4ade80}
    button{width:100%;padding:12px;border-radius:10px;border:none;
           background:#4ade80;color:#052e16;font-weight:700;font-size:1rem;cursor:pointer}
    button:hover{opacity:.92}
    .error{color:#fb7185;font-size:.9rem;margin-bottom:12px;text-align:center}
  </style>
</head>
<body>
  <div class="card">
    <svg class="mark" viewBox="0 0 64 64" aria-hidden="true">
      <path d="M32 6c-4 14 10 16 6 30-1 6-6 10-6 10s-5-4-6-10C22 22 36 20 32 6z" fill="#f5c16c"/>
      <path d="M32 20c-2 7 5 8 3 14-3-1-6-6-3-14z" fill="#fb7185"/>
      <rect x="10" y="40" width="44" height="8" rx="4" transform="rotate(-22 32 44)" fill="#8b5a2b"/>
      <rect x="10" y="46" width="44" height="8" rx="4" transform="rotate(22 32 50)" fill="#6b4220"/>
    </svg>
    <h2>Campfire</h2>
    <p>Dashboard login</p>
    {error}
    <form method="POST" action="/api/login">
      <input type="password" name="key" placeholder="Admin key" autofocus>
      <button type="submit">Open dashboard</button>
    </form>
  </div>
</body>
</html>"""
