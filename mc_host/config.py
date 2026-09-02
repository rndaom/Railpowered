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
  <title>Powered Rail</title>
  <style>
    *{margin:0;padding:0;box-sizing:border-box}
    body{font-family:ui-sans-serif,system-ui,sans-serif;background:#16110c;color:#f4ead6;
         display:flex;align-items:center;justify-content:center;min-height:100vh;
         background-image:radial-gradient(900px 420px at 50% -10%, #4a3214 0%, transparent 58%)}
    .card{background:#221910;border:1px solid #453526;border-radius:16px;padding:36px;width:360px}
    .mark{width:56px;height:56px;margin:0 auto 16px;display:block}
    h2{color:#e4b23a;margin-bottom:6px;text-align:center;letter-spacing:.04em}
    p{color:#b09a7c;text-align:center;font-size:.92rem;margin-bottom:18px}
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
    <h2>Powered Rail</h2>
    <p>Dashboard login</p>
    {error}
    <form method="POST" action="/api/login">
      <input type="password" name="key" placeholder="Admin key" autofocus>
      <button type="submit">Open dashboard</button>
    </form>
  </div>
</body>
</html>"""
