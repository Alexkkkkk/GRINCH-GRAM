---
name: Work In Progress
description: Что делалось в прошлой сессии — незавершённые задачи и следующие шаги
---

## Последняя сессия — 03.08.2026 (полный аудит кода)

### Выполнено: полный аудит всех 38 Python-файлов (28 522 строк)

#### Метод:
- Синтаксическая проверка всех .py → нет ошибок ✅
- Проверка всех Config.* (149 атрибутов) — все валидны ✅
- Проверка всех db_store.* вызовов — все валидны ✅
- Проверка всех ExchangeClient.* — все валидны ✅
- Проверка всех AIEngine.* — все валидны ✅
- Проверка всех BrainFusion.* — все валидны ✅
- Проверка всех ExperienceManager.* — все валидны ✅
- wallet_tracker._seen — dict ✅
- dedust_client — _clean_addr_str, amount_nano cap ✅
- Ложные срабатывания: GridConfig.X совпадал с паттерном Config.X, self._grid_ai.X совпадал с _ai.X — исключены

#### Найдено и исправлено (3 бага):

1. ✅ **app.py:1015-1019** — operator precedence в `_check_admin_confirm()`
   - При `request.is_json=False` заголовок `X-Admin-Confirm` не проверялся (только form data)
   - Исправлено: явные скобки гарантируют проверку заголовка при любом Content-Type

2. ✅ **app.py:1159** — `except Exception: pass` после `save_open_trades`
   - Ошибки (напр. AttributeError) молча глотались
   - Исправлено: `except Exception as _e: log.warning(...)`

3. ✅ **db_store.py:1268** — аннотация `tuple[dict, list, set, float]` вместо `tuple[dict, list, dict, float]`
   - `wallets_load()` возвращает dict (не set) для `seen`
   - Исправлено: аннотация приведена к реальности

#### Низкоприоритетные (не исправлялись, не критичны):
- app.py:1965-1967, 1986-1988 — `_require_login()` дублирует before_request (мёртвый код, но безопасен)
- db_store.py:383 — `_pool_lock` + sleep при импорте — НЕ баг (при старте других потоков нет)
- db_store.py:891 — генератор `ai_examples_export_all` глотает ошибку (логирует), допустимо
- trader.py — 30+ `except Exception: pass` — НАМЕРЕННЫ (предотвращают краш цикла)

#### Деплой:
- Изменения закоммичены и запушены в GitHub (origin/main)
- VPS подхватит через cron `*/3 * * * *` → deploy.sh → docker rebuild
- SSH-секреты VPS (VPS_SSH_KEY, VPS_SSH_PASSWORD) недоступны в этом Replit-инстансе

### Текущее состояние бота (известно из сессии 01.08.2026):
- Позиция: -18.0% (-94 TON unrealized), stake=521.35 TON, amount=1101171.25 GRINCH
- ONLY_PROFIT_EXIT: активен, ждём возврата в плюс
- Grid: active=False

### Незакрытые вопросы:
1. **Telegram chat_id** — не настроен
2. **BUY no_funds** — нужен свободный TON (min 5 TON)
3. **VPS SSH** — пароль root не задан в секретах Replit → прямой деплой через SSH невозможен из Replit
