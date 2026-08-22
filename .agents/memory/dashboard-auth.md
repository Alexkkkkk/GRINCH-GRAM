---
name: Dashboard login auth
description: How owner login/password gating works and the Socket.IO handshake bypass gotcha
---

# Owner dashboard auth (login/password)

Owner panel (`/` + owner `/api/*` + the status `/socket.io` stream) is gated behind a
session login. Credentials come from `ADMIN_USERNAME` / `ADMIN_PASSWORD` secrets, compared
with `hmac.compare_digest`. Public multi-user surface stays open: `/join`,
`/dashboard/<token>`, `/api/user/*`, `/api/platform/stats`, `/static/`.

**Why a global `before_request` guard is NOT enough:**
Flask-SocketIO's connection handshake bypasses `@app.before_request`. An unauthenticated
client could still open `/socket.io` and receive `status_update` (owner data). You MUST
enforce auth inside `@socketio.on("connect")` — return `False` to reject when
`_auth_configured() and not session.get("logged_in")`.
**How to apply:** any time you protect routes with a before_request guard, remember the
socket connect handler is a separate door — gate it explicitly too.

**Session secret:** never rely on the weak hardcoded `Config.SECRET_KEY` default
(`grinch-gram-secret-2024`) — forgeable cookies = login bypass. `_resolve_secret_key()`
in app.py uses env `SESSION_SECRET`/`SECRET_KEY`, else a random `token_hex(32)` persisted
to `.session_secret` (gitignored) so sessions survive restarts.

**Fail-open when unconfigured:** if `ADMIN_USERNAME`/`ADMIN_PASSWORD` are unset the guard
allows access, to avoid bricking the panel before secrets exist. Once both secrets are set
it becomes strict.

**Allowlist shape:** use exact paths + narrow trailing-slash prefixes (`/api/user/`,
`/dashboard/`, `/static/`), NOT bare `/api/user` — a bare prefix would also expose a future
`/api/useradmin`.
