"""SG Transport Pulse command-platform wrapper.

Keeps the existing backend, APIs and business logic intact while adding the shared
application shell, Command Centre, Settings Centre and authentication-ready login UI.
"""
from pathlib import Path
from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
import app as core

app = core.app
HERE = Path(__file__).resolve().parent

class CommandShellMiddleware(BaseHTTPMiddleware):
    EXCLUDE = {"/login", "/command", "/settings"}
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        if request.url.path in self.EXCLUDE:
            return response
        ctype = response.headers.get("content-type", "")
        if "text/html" not in ctype.lower() or getattr(response, "body_iterator", None) is None:
            return response
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk)
        body = b"".join(chunks)
        try:
            html = body.decode("utf-8")
        except UnicodeDecodeError:
            return Response(body, status_code=response.status_code, headers=dict(response.headers), media_type="text/html")
        if "/design-system.css" not in html:
            html = html.replace("</head>", '<link rel="stylesheet" href="/design-system.css">\n</head>', 1)
        if "/nav-registry.js" not in html:
            html = html.replace("</body>", '<script src="/nav-registry.js"></script>\n</body>', 1)
        headers = dict(response.headers); headers.pop("content-length", None)
        return HTMLResponse(html, status_code=response.status_code, headers=headers)

app.add_middleware(CommandShellMiddleware)

@app.get("/command", response_class=HTMLResponse)
async def command_platform_page():
    return HTMLResponse((HERE / "command.html").read_text(encoding="utf-8"))

@app.get("/login", response_class=HTMLResponse)
async def login_platform_page():
    return HTMLResponse((HERE / "login.html").read_text(encoding="utf-8"))

@app.get("/settings", response_class=HTMLResponse)
async def settings_platform_page():
    return HTMLResponse((HERE / "settings.html").read_text(encoding="utf-8"))

@app.get("/api/auth/status")
async def auth_platform_status():
    return JSONResponse({"configured": False, "provider": None})
