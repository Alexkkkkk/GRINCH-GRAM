---
name: Work In Progress
description: Что делалось в прошлой сессии — незавершённые задачи и следующие шаги
---

## Последняя сессия — 01.08.2026

### Что исправлено (1 коммит 930a597, задеплоен на VPS docker cp + restart)

28 проблем из аудита безопасности и конкурентности:

| # | Файл | Что исправлено |
|---|------|----------------|
| 1/6 | trader.py | _close_trade: membership check под _ot_lock (RLock) |
| 5 | trader.py | Удалён хардкодный TON_USD=2.44, используем stale-кэш или pnl=0 |
| 6 | trader.py | open_trades read после _ot_lock exit → snapshot под _ot_lock |
| 7 | trader.py | DCA self-heal в get_status() — модификация только под _ot_lock |
| 3 | app.py | /api/user/deposit требует admin session |
| 4/20 | app.py | isfinite + type guard на amount в deposit и withdraw |
| 10 | grid_trader.py | ZeroDivisionError guard (current_price=0) в _execute_buy и _execute_dca |
| 12 | app.py | str(e) убран из всех grid API error responses |
| 13 | deposit_monitor.py | LIKE-коллизия: дополнительная проверка startswith |
| 21 | wallet_tracker.py | не заменять _on_chain_balances при полном сбое API |
| 35 | wallet_tracker.py | JSON backup error логируется вместо подавления |
| 25 | analytics_buffer.py | полный ISO timestamp вместо HH:MM:SS |
| 27 | brain_fusion.py | has_position threshold из Config вместо хардкод 100 |
| 23 | coin_info.py | cache stampede protection via _fetching_keys |
| 23 | price_feed.py | cache stampede protection via _fetching_keys |
| 26 | experience_manager.py | streak считает только closed-сделки с real pnl |
| 29/30 | alerts.py | ротация файла при >5MB + обрезка до 4096 для Telegram |
| 32 | organism.py | randint range guard при малом n |
| 34 | ai_advisor.py | _persist_history вынесен из-под _lock |

### Что НЕ исправлено из аудита (низкий приоритет или требуют архитектурных решений)

| # | Проблема | Почему не исправлено |
|---|----------|----------------------|
| 2 | Нет shared balance lock между DCA и Grid | Архитектурное изменение, требует отдельного рефакторинга |
| 11 | UUID-token в URL → утечка в Referer | Дизайн-решение, требует изменения всего user dashboard |
| 14 | SELECT FOR UPDATE в deposit_monitor | lt-идемпотентность уже есть, полноценный FOR UPDATE требует рефакторинга |
| 15/16 | AI data leakage / threshold mismatch | ML-рефакторинг, не срочно |
| 17 | experience_manager обрезает по 1000 | Не критично для текущего объёма |
| 18 | grid_trader build_grid/tick/save не всегда под lock | Частично защищено |
| 19 | SocketIO при пустых ADMIN_* | Уже обработано в on_connect |
| 24 | coin_info пара по ликвидности без адреса | Уже фильтруется по baseToken.address |
| 28 | ai_advisor JSON parser | Уже корректно (rfind для последней "}")  |
| 36 | db_store JSONB типы | Требует анализа конкретных полей |

### Прежние открытые задачи (из сессии 31.07)

1. **Бэкфилл bot_trades** — скрипт готов в прошлом WIP, нужен SSH. SSH теперь работает.
   ```bash
   docker exec bot-bot-1 python3 << 'EOF'
   import os,json,psycopg2,psycopg2.extras
   db=psycopg2.connect(os.environ.get("EXTERNAL_DATABASE_URL") or os.environ.get("DATABASE_URL",""))
   cur=db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
   cur.execute("SELECT id,data FROM bot_trades")
   n=0
   for r in cur.fetchall():
       d=dict(r["data"]); c=False
       if not d.get("profit_ton"): d["profit_ton"]=d.get("pnl") or 0.0; c=True
       if not d.get("profit_pct"): d["profit_pct"]=d.get("pnl_pct") or 0.0; c=True
       if not d.get("close_price"): d["close_price"]=d.get("exit_price") or 0.0; c=True
       if not d.get("avg_price"): d["avg_price"]=d.get("entry_price_ton") or 0.0; c=True
       if not d.get("dca_entries_count"): d["dca_entries_count"]=d.get("merged_count") or 1; c=True
       if c: db.cursor().execute("UPDATE bot_trades SET data=%s WHERE id=%s",(json.dumps(d),r["id"])); n+=1
   db.commit(); print(f"Done {n}")
   EOF
   ```

2. **Groq API** — нужен рабочий ключ (AuthenticationError). Обновить через дашборд.
