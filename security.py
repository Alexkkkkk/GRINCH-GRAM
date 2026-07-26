"""
security.py — Многоуровневая защита Flask-приложения от атак.

Уровни:
  1. IP blacklist (постоянный + временный автобан)
  2. Rate limiting (скользящее окно, per-IP)
  3. Brute-force protection (блокировка логина)
  4. Scanner/bot detection (User-Agent)
  5. Security HTTP-headers

Вызывается из app.py: before_request / after_request / login-маршрут.
"""

import time
import threading
import logging
import os
import json
from collections import defaultdict, deque

log = logging.getLogger("security")

# ── Настройки ──────────────────────────────────────────────────────────────
RATE_WINDOW_SEC       = 60       # длина скользящего окна (секунд)
RATE_GENERAL_MAX      = 150      # запросов/окно для обычных путей
RATE_API_MAX          = 80       # запросов/окно для /api/*
RATE_STATIC_MAX       = 400      # /static/ — клиент грузит много ресурсов

LOGIN_MAX_ATTEMPTS    = 10       # неудачных попыток входа
LOGIN_LOCKOUT_SEC     = 900      # 15 мин блокировки после превышения

AUTO_BAN_THRESHOLD    = 600      # запросов/окно → временный бан
BAN_DURATION_SEC      = 6 * 3600 # 6 часов

_DATA_DIR        = os.environ.get("DATA_DIR", "data")
_BLACKLIST_FILE  = os.path.join(_DATA_DIR, "blocked_ips.json")

# ── Внутренние хранилища ───────────────────────────────────────────────────
_lock         = threading.Lock()
_ip_requests  = defaultdict(deque)   # ip → deque(timestamps)
_login_fails  = defaultdict(list)    # ip → [fail_timestamp, ...]
_auto_banned  = {}                    # ip → unban_timestamp
_perm_banned  = set()                 # постоянные баны (из файла)

# ── User-Agent фрагменты известных сканеров ────────────────────────────────
_BAD_UA_FRAGMENTS = [
    "masscan", "nmap", "nikto", "sqlmap", "zgrab", "zmap",
    "dirbuster", "gobuster", "wfuzz", "nuclei", "hydra",
    "metasploit", "burpsuite", "acunetix", "openvas",
    "python-requests/2.2", "python-requests/2.3", "python-requests/2.4",
    "python-requests/2.5", "python-requests/2.6",
    "go-http-client/1.1", "curl/7.29", "libwww-perl", "scrapy",
    "harvester", "subfinder", "amass", "shodan",
]

# ── Пути, которые точно НЕ нужны на боте (типичные цели сканеров) ─────────
_SCANNER_PATHS = {
    "/wp-login.php", "/wp-admin", "/xmlrpc.php", "/admin.php",
    "/.env", "/.git", "/config.php", "/phpmyadmin", "/.aws/credentials",
    "/actuator", "/api/v1/pod", "/.DS_Store", "/backup.zip",
    "/server-status", "/server-info", "/.htaccess", "/web.config",
    "/cgi-bin/", "/boaform/",
}


# ══════════════════════════════════════════════════════════════════════════
#  Инициализация
# ══════════════════════════════════════════════════════════════════════════
def _load_blacklist():
    global _perm_banned
    try:
        os.makedirs(_DATA_DIR, exist_ok=True)
        if os.path.exists(_BLACKLIST_FILE):
            with open(_BLACKLIST_FILE) as f:
                data = json.load(f)
            _perm_banned = set(data.get("ips", []))
            if _perm_banned:
                log.info("[Security] 🚫 Загружено %d заблокированных IP", len(_perm_banned))
    except Exception as e:
        log.warning("[Security] Не удалось загрузить чёрный список: %s", e)


def _save_blacklist():
    try:
        os.makedirs(_DATA_DIR, exist_ok=True)
        with open(_BLACKLIST_FILE, "w") as f:
            json.dump({"ips": sorted(_perm_banned)}, f, indent=2)
    except Exception as e:
        log.warning("[Security] Не удалось сохранить чёрный список: %s", e)


# ══════════════════════════════════════════════════════════════════════════
#  Вспомогательные функции
# ══════════════════════════════════════════════════════════════════════════
def get_client_ip() -> str:
    """Реальный IP клиента (nginx ставит X-Real-IP)."""
    from flask import request as _req
    ip = (
        _req.headers.get("X-Real-IP")
        or _req.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        or _req.remote_addr
        or "unknown"
    )
    return ip.strip()


def _is_banned(ip: str) -> bool:
    if ip in _perm_banned:
        return True
    ts = _auto_banned.get(ip)
    if ts:
        if time.time() < ts:
            return True
        del _auto_banned[ip]  # истёк
    return False


def _auto_ban(ip: str, reason: str):
    _auto_banned[ip] = time.time() + BAN_DURATION_SEC
    log.warning("[Security] 🚫 Автобан %s на %dч — %s", ip, BAN_DURATION_SEC // 3600, reason)


# ══════════════════════════════════════════════════════════════════════════
#  Основная проверка (before_request)
# ══════════════════════════════════════════════════════════════════════════
def check_request():
    """
    Вызывается из @app.before_request.
    Возвращает None если запрос разрешён,
    либо Flask-response при блокировке.
    """
    from flask import request as _req, jsonify

    ip   = get_client_ip()
    path = _req.path or "/"
    now  = time.time()

    with _lock:
        # ── 1. Чёрный список ────────────────────────────────────────────
        if _is_banned(ip):
            return jsonify({"error": "Forbidden"}), 403

        # ── 2. Путь — известная цель сканера ────────────────────────────
        for sp in _SCANNER_PATHS:
            if path.lower().startswith(sp):
                _perm_banned.add(ip)
                _save_blacklist()
                log.warning("[Security] 🚫 Бан %s — попытка доступа к %s", ip, path)
                return jsonify({"error": "Not Found"}), 404

        # ── 3. User-Agent сканера ────────────────────────────────────────
        ua = (_req.headers.get("User-Agent") or "").lower()
        for frag in _BAD_UA_FRAGMENTS:
            if frag in ua:
                _perm_banned.add(ip)
                _save_blacklist()
                log.warning("[Security] 🚫 Бан %s — сканер UA: %.60s", ip, ua)
                return jsonify({"error": "Forbidden"}), 403

        # ── 4. Rate limiting (скользящее окно) ──────────────────────────
        q = _ip_requests[ip]
        while q and now - q[0] > RATE_WINDOW_SEC:
            q.popleft()
        q.append(now)
        count = len(q)

        # Автобан при флуде
        if count > AUTO_BAN_THRESHOLD:
            _auto_ban(ip, f"DDoS flood {count} req/{RATE_WINDOW_SEC}s")
            return jsonify({"error": "Too Many Requests"}), 429

        # Лимит по типу пути
        if path.startswith("/static/"):
            limit = RATE_STATIC_MAX
        elif path.startswith("/api/"):
            limit = RATE_API_MAX
        else:
            limit = RATE_GENERAL_MAX

        if count > limit:
            log.info("[Security] ⚠️  Rate limit %s — %d/%d req", ip, count, limit)
            from flask import make_response
            resp = make_response(jsonify({"error": "Too Many Requests",
                                          "retry_after": RATE_WINDOW_SEC}), 429)
            resp.headers["Retry-After"] = str(RATE_WINDOW_SEC)
            return resp

    return None  # пропускаем


# ══════════════════════════════════════════════════════════════════════════
#  Брутфорс-защита логина
# ══════════════════════════════════════════════════════════════════════════
def check_login_allowed(ip: str) -> bool:
    """True → вход разрешён. False → IP заблокирован."""
    now = time.time()
    with _lock:
        if _is_banned(ip):
            return False
        recent = [t for t in _login_fails.get(ip, []) if now - t < LOGIN_LOCKOUT_SEC]
        return len(recent) < LOGIN_MAX_ATTEMPTS


def record_login_fail(ip: str):
    """Фиксирует неудачную попытку; банит при превышении порога."""
    now = time.time()
    with _lock:
        recent = [t for t in _login_fails.get(ip, []) if now - t < LOGIN_LOCKOUT_SEC]
        recent.append(now)
        _login_fails[ip] = recent
        count = len(recent)
        log.info("[Security] 🔑 Неудачный вход %s (%d/%d)", ip, count, LOGIN_MAX_ATTEMPTS)
        if count >= LOGIN_MAX_ATTEMPTS:
            _auto_ban(ip, f"brute-force: {count} login fails")


def record_login_success(ip: str):
    """Сбрасывает счётчик неудачных попыток после успешного входа."""
    with _lock:
        _login_fails.pop(ip, None)


# ══════════════════════════════════════════════════════════════════════════
#  Security HTTP-headers (after_request)
# ══════════════════════════════════════════════════════════════════════════
def add_security_headers(response):
    h = response.headers
    h.setdefault("X-Content-Type-Options",  "nosniff")
    h.setdefault("X-Frame-Options",         "SAMEORIGIN")
    h.setdefault("X-XSS-Protection",        "1; mode=block")
    h.setdefault("Referrer-Policy",         "strict-origin-when-cross-origin")
    h.setdefault("Permissions-Policy",      "geolocation=(), microphone=(), camera=()")
    # Убираем «рекламу» технологии
    h["Server"] = "nginx"
    return response


# ══════════════════════════════════════════════════════════════════════════
#  Управление банами (API для дашборда)
# ══════════════════════════════════════════════════════════════════════════
def ban_ip(ip: str):
    with _lock:
        _perm_banned.add(ip)
        _save_blacklist()
    log.warning("[Security] 🚫 Ручной перманентный бан: %s", ip)


def unban_ip(ip: str):
    with _lock:
        _perm_banned.discard(ip)
        _auto_banned.pop(ip, None)
        _login_fails.pop(ip, None)
        _save_blacklist()
    log.info("[Security] ✅ Бан снят: %s", ip)


def get_stats() -> dict:
    now = time.time()
    with _lock:
        active_auto = {ip: ts for ip, ts in _auto_banned.items() if now < ts}
        top_ips = sorted(
            ((ip, len(q)) for ip, q in _ip_requests.items() if q),
            key=lambda x: -x[1]
        )[:10]
        lockouts = [
            ip for ip, fails in _login_fails.items()
            if len([t for t in fails if now - t < LOGIN_LOCKOUT_SEC]) >= LOGIN_MAX_ATTEMPTS
        ]
        return {
            "perm_banned":    len(_perm_banned),
            "perm_banned_ips": sorted(_perm_banned),
            "auto_banned":    len(active_auto),
            "auto_banned_ips": [
                {"ip": ip, "unban_in": int(ts - now)}
                for ip, ts in sorted(active_auto.items(), key=lambda x: x[1])
            ],
            "tracked_ips":    len(_ip_requests),
            "login_lockouts": len(lockouts),
            "locked_ips":     lockouts,
            "top_ips":        [{"ip": ip, "req": n} for ip, n in top_ips],
        }


# ── Загружаем чёрный список при импорте ───────────────────────────────────
_load_blacklist()
log.info("[Security] ✅ Модуль безопасности инициализирован")
