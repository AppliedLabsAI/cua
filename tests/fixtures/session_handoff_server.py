"""Fixture server for browser-login to API-session handoff tests."""

from __future__ import annotations

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

app = FastAPI()

_active_sessions: set[str] = set()

LOGIN_PAGE = """\
<!DOCTYPE html>
<html>
  <head><title>Session Login</title></head>
  <body>
    <main>
      <h1>Customer Billing Login</h1>
      <form action="/login" method="post">
        <label for="email">Email</label>
        <input type="email" id="email" name="email" />
        <label for="password">Password</label>
        <input type="password" id="password" name="password" />
        <button type="submit">Log In</button>
      </form>
    </main>
  </body>
</html>
"""

DASHBOARD_PAGE = """\
<!DOCTYPE html>
<html>
  <head><title>Billing Dashboard</title></head>
  <body>
    <main>
      <h1>Billing Dashboard</h1>
      <p>Authenticated session is active.</p>
    </main>
  </body>
</html>
"""


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    return HTMLResponse(LOGIN_PAGE)


@app.post("/login")
async def login(request: Request):
    form = await request.form()
    email = form.get("email", "")
    password = form.get("password", "")
    if email == "agent@example.com" and password == "hunter2":
        session_value = "session-123"
        _active_sessions.add(session_value)
        response = Response(status_code=302, headers={"Location": "/dashboard"})
        response.set_cookie("user_session", session_value, httponly=True)
        return response
    return HTMLResponse("<h1>Login failed</h1>", status_code=401)


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    session_value = request.cookies.get("user_session")
    if session_value not in _active_sessions:
        return HTMLResponse(LOGIN_PAGE, status_code=401)
    return HTMLResponse(DASHBOARD_PAGE)


@app.get("/api/invoices")
async def invoices(request: Request, email: str):
    session_value = request.cookies.get("user_session")
    if session_value not in _active_sessions:
        return JSONResponse({"error": "missing session"}, status_code=401)
    if request.headers.get("FFF-Auth") != "V1.1":
        return JSONResponse({"error": "missing header"}, status_code=401)
    return {
        "customer": email,
        "invoices": [
            {
                "invoice_id": "inv-1001",
                "amount_due": "19.99",
                "status": "open",
            }
        ],
    }
