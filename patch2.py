#!/usr/bin/env python3
"""Патч v2 — regex-замены, без heredoc."""
import re, subprocess

# ─── config.py ───────────────────────────────────────────────────────────────
with open('/opt/bot/config.py', encoding='utf-8') as f:
    txt = f.read()

replacements = [
    (r'(SMART_TP_TIGHT_TRAIL_PCT\s*=\s*float\(os\.getenv\("SMART_TP_TIGHT_TRAIL_PCT",\s*)"7\.0"',
     r'\g<1>"10.0"'),
    (r'(TRAIL_STAGE2_PCT\s*=\s*float\(os\.getenv\("TRAIL_STAGE2_PCT",\s*)"10\.0"',
     r'\g<1>"17.0"'),
    (r'(TRAIL_STAGE3_PCT\s*=\s*float\(os\.getenv\("TRAIL_STAGE3_PCT",\s*)"7\.5"',
     r'\g<1>"12.0"'),
    (r'(TRAILING_STOP_PCT\s*=\s*float\(os\.getenv\("TRAILING_STOP_PCT",\s*)"9\.0"',
     r'\g<1>"11.0"'),
    (r'(DCA_DROP_TRIGGER_PCT\s*=\s*float\(os\.getenv\("DCA_DROP_TRIGGER_PCT",\s*)"8"',
     r'\g<1>"10"'),
    (r'(DCA_PULLBACK_WAIT_PCT\s*=\s*float\(os\.getenv\("DCA_PULLBACK_WAIT_PCT",\s*)"10"',
     r'\g<1>"13"'),
    (r'(DCA_SMART_REENTRY_PULLBACK_PCT\s*=\s*float\(os\.getenv\("DCA_SMART_REENTRY_PULLBACK_PCT",\s*)"4"',
     r'\g<1>"7"'),
    (r'(DCA_ADAPTIVE_FAST_MOVE_PCT\s*=\s*float\(os\.getenv\("DCA_ADAPTIVE_FAST_MOVE_PCT",\s*)"4"',
     r'\g<1>"6"'),
    (r'(PROFIT_PROTECT_DROP_PCT\s*=\s*float\(os\.getenv\("PROFIT_PROTECT_DROP_PCT",\s*)"5\.0"',
     r'\g<1>"8.0"'),
    (r'(SCALP_TRAIL_PCT\s*=\s*float\(os\.getenv\("SCALP_TRAIL_PCT",\s*)"4\.0"',
     r'\g<1>"7.0"'),
    (r'(SCALP_MAX_ATR_PCT\s*=\s*float\(os\.getenv\("SCALP_MAX_ATR_PCT",\s*)"5\.5"',
     r'\g<1>"8.0"'),
    (r'(FAST_REENTRY_PULLBACK_PCT\s*=\s*float\(os\.getenv\("FAST_REENTRY_PULLBACK_PCT",\s*)"4\.0"',
     r'\g<1>"7.0"'),
]

changed = 0
for pattern, repl in replacements:
    new_txt, n = re.subn(pattern, repl, txt)
    name = re.search(r'\((\w+)', pattern).group(1)
    if n:
        txt = new_txt
        changed += n
        print(f'  [OK]   {name}')
    else:
        print(f'  [MISS] {name}  -- проверь вручную')

# WHALE_* параметры
if 'WHALE_BALANCE_POLL_SEC' not in txt:
    anchor = 'SMART_EARLY_MIN_TON'
    pos = txt.find(anchor)
    if pos != -1:
        eol = txt.find('\n', pos)
        whale_block = (
            '\n\n    # -- On-chain analyse whale balances (tonapi.io free) --\n'
            '    WHALE_BALANCE_POLL_SEC  = int(os.getenv("WHALE_BALANCE_POLL_SEC",  "300"))\n'
            '    WHALE_TOP_N             = int(os.getenv("WHALE_TOP_N",              "25"))\n'
            '    WHALE_MIN_GRINCH        = float(os.getenv("WHALE_MIN_GRINCH",  "100000"))'
        )
        txt = txt[:eol] + whale_block + txt[eol:]
        print('  [OK]   WHALE_* блок вставлен')
        changed += 1
    else:
        print('  [MISS] якорь SMART_EARLY_MIN_TON не найден')
else:
    print('  [SKIP] WHALE_* уже есть')

with open('/opt/bot/config.py', 'w', encoding='utf-8') as f:
    f.write(txt)
print(f'config.py: {changed} изменений')

# ─── wallet_tracker.py ───────────────────────────────────────────────────────
with open('/opt/bot/wallet_tracker.py', encoding='utf-8') as f:
    wt = f.read()

wt_changed = 0

# 1. Поля в __init__
if '_on_chain_balances' not in wt:
    wt = wt.replace(
        '        self._load()',
        '        self._on_chain_balances: dict = {}\n'
        '        self._last_balance_poll: float = 0.0\n'
        '        self._load()',
        1
    )
    wt_changed += 1
    print('  [OK]   __init__ поля добавлены')
else:
    print('  [SKIP] __init__ поля уже есть')

# 2. Метод _poll_whale_balances + get_whale_hold_score
if '_poll_whale_balances' not in wt:
    whale_code = (
        '    def _poll_whale_balances(self):\n'
        '        """Проверяет on-chain GRINCH-баланс топ-N кошельков (tonapi.io)."""\n'
        '        try:\n'
        '            jetton_addr = Config.GRINCH_TOKEN_ADDRESS\n'
        '            top_n = getattr(Config, "WHALE_TOP_N", 25)\n'
        '            min_g = getattr(Config, "WHALE_MIN_GRINCH", 100000)\n'
        '            if not jetton_addr:\n'
        '                return\n'
        '            with self._lock:\n'
        '                wallets_copy = dict(self.wallets)\n'
        '            ranked = sorted(\n'
        '                wallets_copy.items(),\n'
        '                key=lambda x: x[1].get("grinch_bought", 0) + x[1].get("grinch_sold", 0),\n'
        '                reverse=True,\n'
        '            )[:top_n]\n'
        '            new_bal = {}\n'
        '            for addr, _ in ranked:\n'
        '                if not addr or addr == "\u2014":\n'
        '                    continue\n'
        '                try:\n'
        '                    url = "https://tonapi.io/v2/accounts/" + addr + "/jettons/" + jetton_addr\n'
        '                    r = _HTTP.get(url, timeout=8)\n'
        '                    if r.status_code in (404, 422):\n'
        '                        new_bal[addr] = 0.0\n'
        '                        continue\n'
        '                    r.raise_for_status()\n'
        '                    raw = r.json().get("balance", "0") or "0"\n'
        '                    new_bal[addr] = int(raw) / 1e9\n'
        '                except Exception:\n'
        '                    pass\n'
        '            with self._lock:\n'
        '                self._on_chain_balances = new_bal\n'
        '                self._last_balance_poll = time.time()\n'
        '            whales = sum(1 for v in new_bal.values() if v >= min_g)\n'
        '            logger.debug("[WalletTracker] on-chain: %d кошельков, %d китов", len(new_bal), whales)\n'
        '        except Exception as e:\n'
        '            logger.debug("[WalletTracker] _poll_whale_balances: %s", e)\n'
        '\n'
        '    def get_whale_hold_score(self) -> dict:\n'
        '        """whale_hold_score [-1..+1]: >0 киты держат, <0 вышли."""\n'
        '        with self._lock:\n'
        '            balances = dict(self._on_chain_balances)\n'
        '            last_poll = self._last_balance_poll\n'
        '        if not balances or time.time() - last_poll > 600:\n'
        '            return {"whale_hold_score": 0.0, "whale_count": 0,\n'
        '                    "whale_grinch_total": 0.0, "whale_data_age_sec": 9999}\n'
        '        min_g = getattr(Config, "WHALE_MIN_GRINCH", 100000)\n'
        '        whale_addrs = [a for a, v in balances.items() if v >= min_g]\n'
        '        whale_g = sum(balances[a] for a in whale_addrs)\n'
        '        total_g = sum(balances.values())\n'
        '        max_p = max(balances.values()) * len(balances) if balances else 1\n'
        '        score = max(-1.0, min(1.0, (whale_g / max_p) * 2 - 1))\n'
        '        return {\n'
        '            "whale_hold_score": round(score, 3),\n'
        '            "whale_count": len(whale_addrs),\n'
        '            "whale_grinch_total": round(whale_g / 1e6, 3),\n'
        '            "total_tracked_grinch": round(total_g / 1e6, 3),\n'
        '            "whale_data_age_sec": round(time.time() - last_poll),\n'
        '        }\n'
        '\n'
        '    def get_signal(self):'
    )
    wt = wt.replace('    def get_signal(self):', whale_code, 1)
    wt_changed += 1
    print('  [OK]   _poll_whale_balances + get_whale_hold_score вставлены')
else:
    print('  [SKIP] _poll_whale_balances уже есть')

# 3. Вызов поллера в _loop()
if 'WHALE_BALANCE_POLL_SEC' not in wt:
    old_loop_line = '                self._poll_once()\n                self.last_error = None'
    new_loop_line = (
        '                self._poll_once()\n'
        '                _wp_interval = getattr(Config, "WHALE_BALANCE_POLL_SEC", 300)\n'
        '                if time.time() - self._last_balance_poll >= _wp_interval:\n'
        '                    self._poll_whale_balances()\n'
        '                self.last_error = None'
    )
    if old_loop_line in wt:
        wt = wt.replace(old_loop_line, new_loop_line, 1)
        wt_changed += 1
        print('  [OK]   _loop() поллер добавлен')
    else:
        print('  [MISS] якорь _loop() не найден')
else:
    print('  [SKIP] _loop() поллер уже есть')

# 4. whale_hold_score в return get_signal()
if '"whale_hold_score"' not in wt:
    old_ret = '            "early_buy_ton": round(cur_net, 2),\n        }'
    new_ret = (
        '            "early_buy_ton": round(cur_net, 2),\n'
        '            **self.get_whale_hold_score(),\n'
        '        }'
    )
    if old_ret in wt:
        wt = wt.replace(old_ret, new_ret, 1)
        wt_changed += 1
        print('  [OK]   whale_hold_score в get_signal()')
    else:
        print('  [MISS] return get_signal() якорь не найден')
else:
    print('  [SKIP] whale_hold_score уже есть')

with open('/opt/bot/wallet_tracker.py', 'w', encoding='utf-8') as f:
    f.write(wt)
print(f'wallet_tracker.py: {wt_changed} изменений')

# Синтаксис
r1 = subprocess.run(['python3', '-m', 'py_compile', '/opt/bot/config.py'], capture_output=True, text=True)
r2 = subprocess.run(['python3', '-m', 'py_compile', '/opt/bot/wallet_tracker.py'], capture_output=True, text=True)
print('syntax config.py:', 'OK' if r1.returncode == 0 else 'FAIL: ' + r1.stderr)
print('syntax wallet_tracker.py:', 'OK' if r2.returncode == 0 else 'FAIL: ' + r2.stderr)
