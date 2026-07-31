---
name: Work In Progress
description: Что делалось в прошлой сессии — незавершённые задачи и следующие шаги
---

## Последняя сессия — 31.07.2026

### Что было исправлено (код закоммичен и запушен на GitHub)

| Коммит | Файл | Что |
|--------|------|-----|
| 53dbcfa | db_store.py | `_normalize_trade_fields()` — алиасы polей (profit_ton/pct, close_price, avg_price, dca_entries_count) для всех будущих trades |
| 3871d8e | grid_trader.py | `_is_profitable_sell()` — правильная база затрат (не center_price_ton); `_execute_sell()` — консистентно |
| 8bb89a0 | app.py | Clamp DCA_DROP_TRIGGER_PCT → 1-25% при старте; автосброс 50%→10% с перезаписью в settings_store |

VPS cron подтягивает git pull каждые 3 мин → всё будет задеплоено автоматически.

### Что ещё нужно сделать (SSH был недоступен)

1. **Бэкфилл bot_trades** — 17+ исторических записей без profit_ton/close_price/avg_price.
   Скрипт готов (`/tmp/backfill_trades.py` — см. ниже). Запустить когда SSH восстановится:
   ```bash
   docker cp /tmp/backfill_trades.py bot-bot-1:/tmp/ && docker exec bot-bot-1 python3 /tmp/backfill_trades.py
   ```

2. **Groq API** — AuthenticationError. Пользователь должен сам обновить ключ в дашборде
   (Settings → Groq API Key). Код не требует правок.

3. **VPS_SSH_KEY** — пароль устарел, SSH не работает. Попросить пользователя обновить секрет.

### Бэкфилл-скрипт (для bot_trades)
```python
import os, json, psycopg2, psycopg2.extras
db_url = os.environ.get("EXTERNAL_DATABASE_URL") or os.environ.get("DATABASE_URL","")
conn = psycopg2.connect(db_url)
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
cur.execute("SELECT id, data FROM bot_trades")
rows = cur.fetchall()
updated = 0
for row in rows:
    d = dict(row["data"])
    changed = False
    if not d.get("profit_ton"): d["profit_ton"] = d.get("pnl") or 0.0; changed = True
    if not d.get("profit_pct"): d["profit_pct"] = d.get("nl_pct") or 0.0; changed = True
    if not d.get("close_price"): d["close_price"] = d.get("exit_price") or 0.0; changed = True
    if not d.get("avg_price"): d["avg_price"] = d.get("entry_price_ton") or 0.0; changed = True
    if not d.get("dca_entries_count"): d["dca_entries_count"] = d.get("merged_count") or 1; changed = True
    if changed:
        conn.cursor().execute("UPDATE bot_trades SET data=%s WHERE id=%s",
                              (json.dumps(d), row["id"]))
        updated += 1
conn.commit()
print(f"Done: {updated}/{len(rows)}")
conn.close()
```

### Ключевые баги и их статус

| # | Баг | Статус |
|---|-----|--------|
| Grid SELL пропускает ордера | `center_price_ton` после рецентровки завышал cost_ton | ✅ Исправлено |
| DCA_DROP_TRIGGER_PCT=50% | Блокировал DCA до -50% просадки | ✅ Исправлено (code+clamp) |
| bot_trades алиасы полей | future trades — исправлено; исторические — нужен бэкфилл | 🟡 Частично |
| trading_enabled конфликт | Синхронизирован (предыдущая сессия) | ✅ |
| Groq AuthenticationError | Требует обновления ключа пользователем | ⚠️ Нужны действия |
