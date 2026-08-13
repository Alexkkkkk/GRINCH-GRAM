#!/usr/bin/env python3
"""
Одноразовый фикс: убирает дублирующиеся ID в grid_state.json.
Запустить на VPS: docker exec bot-bot-1 python3 /usr/src/app/fix_grid_ids.py
"""

import json, collections, shutil, time, os

STATE_FILE = "/app/data/grid_state.json"
BACKUP = STATE_FILE + f".bak.{int(time.time())}"

with open(STATE_FILE) as f:
    state = json.load(f)

levels = state.get("levels", [])
ids = [l["id"] for l in levels]
counter = collections.Counter(ids)
dupes = {i for i, c in counter.items() if c > 1}

print(f"Всего уровней: {len(levels)}")
print(f"Дублирующиеся ID: {dupes}")

if not dupes:
    print("Дублей нет — ничего делать не нужно.")
    exit(0)

# Бэкап
shutil.copy2(STATE_FILE, BACKUP)
print(f"Бэкап: {BACKUP}")

# Для каждого дублирующегося ID оставить только первый, остальным назначить новый уникальный ID
seen = set()
all_ids = {l["id"] for l in levels}
max_idle = max((l["id"] for l in levels if l["id"] <= -2000), default=-2000)
next_new = max_idle - 1  # уйти ещё глубже в idle-диапазон

for lv in levels:
    if lv["id"] in dupes:
        if lv["id"] not in seen:
            seen.add(lv["id"])  # первый — оставляем как есть
            print(f"  Оставляем: id={lv['id']} side={lv['side']} status={lv['status']}")
        else:
            # Переименовываем дубль
            old_id = lv["id"]
            while next_new in all_ids:
                next_new -= 1
            print(
                f"  Переименовываем дубль: id={old_id} → {next_new} side={lv['side']} status={lv['status']}"
            )
            lv["id"] = next_new
            all_ids.add(next_new)
            next_new -= 1

# Проверка
new_ids = [l["id"] for l in levels]
assert len(new_ids) == len(set(new_ids)), "Дубли остались!"

state["levels"] = levels
tmp = STATE_FILE + ".tmp"
with open(tmp, "w") as f:
    json.dump(state, f, ensure_ascii=False, indent=2)
os.replace(tmp, STATE_FILE)
print(f"\n✅ Готово — {len(levels)} уровней, дублей больше нет.")
