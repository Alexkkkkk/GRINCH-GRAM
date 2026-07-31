---
name: Work In Progress
description: Что делалось в прошлой сессии — незавершённые задачи и следующие шаги
---

## Последняя сессия — 31.07.2026

### Что исправлено (4 коммита на GitHub, cron задеплоит на VPS автоматически)

| Коммит | Файл | Что |
|--------|------|-----|
| 53dbcfa | db_store.py | `_normalize_trade_fields()` — алиасы полей в trades_upsert/open_trades_save |
| 3871d8e | grid_trader.py | `_is_profitable_sell()` — правильная база затрат, не center_price_ton |
| 8bb89a0 | app.py | Clamp DCA_DROP_TRIGGER_PCT → 1-25% при старте, автосброс 50%→10% |
| 7df9b67 | trader.py | DCA entries_count после merge+restart: dca_index=merged_count в _merge_long_trades + max(dca_index, merged_count) при восстановлении |

### Что ещё нужно (SSH был недоступен)

1. **Бэкфилл bot_trades** — 17+ исторических записей без profit_ton/close_price/avg_price.
   Запустить когда SSH восстановится:
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

3. **SSH на VPS** — port 22 connection refused, sshd не запущен. Нужно перезапустить через панель хостинга.

### Статус всех багов из аудита 31.07

| Баг | Статус |
|-----|--------|
| Grid SELL L2 пропускает ордера | ✅ Исправлено |
| DCA_DROP_TRIGGER_PCT=50% | ✅ Исправлено |
| DCA entries_count сбрасывается после рестарта | ✅ Исправлено |
| bot_trades поля (profit_ton и др.) — future trades | ✅ Исправлено |
| bot_trades поля — исторические записи (бэкфилл) | 🟡 Нужен SSH |
| trading_enabled конфликт | ✅ Исправлено (прошлая сессия) |
| Groq AuthenticationError | ⚠️ Нужен рабочий ключ |
