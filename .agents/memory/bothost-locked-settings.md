---
name: Bothost locked settings
description: Параметры Dockerfile и main.py, зафиксированные для Bothost — НЕЛЬЗЯ менять без явного разрешения пользователя.
---

# ⛔ Bothost-locked settings — НЕ МЕНЯТЬ

Эти настройки выверены по docs.bothost.ru, протестированы в продакшне и РАБОТАЮТ.
Любое изменение ломает деплой на Bothost. Пользователь явно запретил их менять.

## Dockerfile — заблокированные параметры

| Параметр | Значение | Причина |
|---|---|---|
| `WORKDIR` | `/usr/src/app` | Bothost bind-монтирует `/app` с Git при деплое — всё что собрано в `/app` исчезает |
| `DATA_DIR` | `/app/data` | Единственная персистентная директория Bothost (переживает redeploy) |
| `PORT` | `${PORT:-3000}` через shell-форму | Bothost передаёт порт через env `PORT`; хардкод ломает прокси |
| `CMD` | shell-форма (не exec-форма `[]`) | Только shell-форма раскрывает `$PORT` |
| `--worker-class gthread` | обязателен | Flask-SocketIO в threading-режиме требует gthread; без него SocketIO не работает |
| `LOW_MEMORY_MODE=1` | всегда включён | Bothost лимит ~255MB RAM; без этого контейнер получает OOM/SIGKILL |
| `HEALTHCHECK` | `localhost:${PORT:-3000}/health` | Bothost nginx роутит трафик только после 200 от /health |

## main.py — заблокированные параметры

```python
port = int(os.environ.get("PORT", 3000))  # НЕ хардкодить 5000 или любой другой порт
socketio.run(app, host="0.0.0.0", port=port, ...)
```

**Why:** Пользователь явно сказал "никогда не меняй эти настройки". Все они зафиксированы после изучения docs.bothost.ru и успешного деплоя.

**How to apply:** Перед любым редактированием Dockerfile или main.py — проверить этот файл. Если нужно что-то изменить — СПРОСИТЬ пользователя явно.
