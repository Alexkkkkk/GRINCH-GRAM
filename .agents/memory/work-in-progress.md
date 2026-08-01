---
name: Work In Progress
description: Что делалось в прошлой сессии — незавершённые задачи и следующие шаги
---

## Последняя сессия — 01.08.2026

### Что сделано

| Действие | Результат |
|----------|-----------|
| SSH на VPS восстановлен (VPS_SSH_KEY добавлен в Replit secrets) | ✅ |
| Grid rebuild с пирамидой v3 через аутентифицированный API | ✅ |
| Пирамида применилась: L1=159k, L5=122k, L9=86k GRINCH (×1.30→×0.70) | ✅ |
| center_price обновлён до актуальной 0.000408 TON | ✅ |
| paused_reason очищен — AI-менеджер активирует сетку когда режим изменится | ✅ |

### Текущее состояние VPS (01.08.2026 ~09:45)

- **DCA позиция**: 1,101,171 GRINCH, вложено 521.46 TON, текущая -14.6%, цель +24.66%
- **Сетка**: 9 sell уровней (все waiting), 5 buy уровней (no_funds — TON 2.295), active=False
- **ONLY_PROFIT_EXIT**: блокирует продажу (ждём возврата в плюс)
- **AI-Mgr**: режим RANGING — сетка пока не активируется автоматически

### Что ещё нужно

1. **Бэкфилл bot_trades** — 17+ исторических записей без profit_ton/close_price/avg_price (Task #3).
   ```bash
   docker exec bot-bot-1 python3 << 'EOF'
   import os,json,psycopg2,psycopg2.extras
   db=psycopg2.connect(os.environ.get("EXTERNAL_DATABASE_URL") or os.environ.get("DATABASE_URL",""))
   cur=db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
   cur.execute("SELECT trade_id,data FROM bot_trades")
   n=0
   for r in cur.fetchall():
       d=dict(r["data"]); c=False
       if not d.get("profit_ton"): d["profit_ton"]=d.get("pnl") or 0.0; c=True
       if not d.get("profit_pct"): d["profit_pct"]=d.get("pnl_pct") or 0.0; c=True
       if not d.get("close_price"): d["close_price"]=d.get("exit_price") or 0.0; c=True
       if not d.get("avg_price"): d["avg_price"]=d.get("entry_price_ton") or 0.0; c=True
       if not d.get("dca_entries_count"): d["dca_entries_count"]=d.get("merged_count") or 1; c=True
       if c: db.cursor().execute("UPDATE bot_trades SET data=%s WHERE trade_id=%s",(json.dumps(d),r["trade_id"])); n+=1
   db.commit(); print(f"Done {n}")
   EOF
   ```

2. **Groq API** — нужен рабочий ключ (AuthenticationError). Обновить через дашборд.

### Статус багов

| Баг | Статус |
|-----|--------|
| Grid pyramid weights не применялись | ✅ Исправлено (rebuild 01.08) |
| Grid SELL L2 пропускает ордера | ✅ |
| DCA_DROP_TRIGGER_PCT=50% | ✅ |
| DCA entries_count сбрасывается после рестарта | ✅ |
| bot_trades поля (profit_ton и др.) — future trades | ✅ |
| bot_trades поля — исторические записи (бэкфилл) | 🟡 Task #3 |
| trading_enabled конфликт | ✅ |
| Groq AuthenticationError | ⚠️ Нужен рабочий ключ |
