---
name: Security rate-limit thresholds
description: Правильные пороги security.py чтобы дашборд не банил самого владельца.
---

# Security Rate-Limit Thresholds

## Правило
RATE_API_MAX ≥ 300 req/60s, AUTO_BAN_THRESHOLD ≥ 500 req/60s.

**Why:** Дашборд поллит ~10 эндпоинтов каждые 5с (status, candles, filters, coin, wallet, performance, organism и др.) = ~120–150 req/min с одной вкладки. Лимит 80 банил VPN-IP владельца.

**How to apply:** При жалобе {"error":"Forbidden"} от самого владельца — сначала проверить rate-limit пороги, потом ban-лист.

## Правильные значения (2026-07-28)
- RATE_GENERAL_MAX = 300
- RATE_API_MAX = 300
- RATE_STATIC_MAX = 600
- AUTO_BAN_THRESHOLD = 500

## Симптом и фикс
Лог: "DDoS flood N req/60s" для VPN-IP владельца.
Снять бан: `security._auto_banned.pop(ip)` + `security._perm_banned.discard(ip)`.
Потом деплоить security.py с новыми порогами и пересобрать контейнер.
