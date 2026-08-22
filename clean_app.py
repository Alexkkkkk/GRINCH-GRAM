with open("app.py", "r", encoding="utf-8") as f:
    content = f.read()

# 1. Remove DCA_REENTRY_COOLDOWN_SEC from restore loop
content = content.replace(
    '            ("DCA_REENTRY_COOLDOWN_SEC", lambda v: int(float(v))),\n            ("FAST_REENTRY_PULLBACK_PCT", float),',
    '            ("FAST_REENTRY_PULLBACK_PCT", float),',
)

# 2. Remove DCA AI-guard from protective_filters endpoint
content = content.replace(
    """    # ── DCA AI-guard: проверяем текущее состояние ────────────────────────────
    ai_signal = last_ai.get("ai_signal", "HOLD")
    ai_conf = float(last_ai.get("confidence", 0) or 0)
    dca_guard_active = ai_signal == "SELL" and ai_conf >= Config.DCA_AI_SELL_BLOCK_CONF

    return jsonify(
        {
            "cooldown": {
                "active": cooldown_active,
                "seconds_left": int(cd_left),
                "total_sec": cd_total,
                "pct": (
                    round((1 - cd_left / cd_total) * 100, 1) if cd_total > 0 else 100
                ),
            },
            "confluence": {
                "enabled": Config.CONFLUENCE_ENABLED,
                "active": confluence_active,
                "rsi": round(confluence_rsi, 2) if confluence_rsi is not None else None,
                "volume_ratio": (
                    round(confluence_vol_ratio, 2)
                    if confluence_vol_ratio is not None
                    else None
                ),
            },
            "dca_guard": {
                "enabled": True,
                "active": dca_guard_active,
                "ai_signal": ai_signal,
                "ai_confidence": ai_conf,
                "threshold": Config.DCA_AI_SELL_BLOCK_CONF,
            },
            "blocked_recent": blocked_recent,
        }
    )""",
    """    return jsonify(
        {
            "cooldown": {
                "active": cooldown_active,
                "seconds_left": int(cd_left),
                "total_sec": cd_total,
                "pct": (
                    round((1 - cd_left / cd_total) * 100, 1) if cd_total > 0 else 100
                ),
            },
            "confluence": {
                "enabled": Config.CONFLUENCE_ENABLED,
                "active": confluence_active,
                "rsi": round(confluence_rsi, 2) if confluence_rsi is not None else None,
                "volume_ratio": (
                    round(confluence_vol_ratio, 2)
                    if confluence_vol_ratio is not None
                    else None
                ),
            },
            "blocked_recent": blocked_recent,
        }
    )""",
)

# 3. Remove DCA block from status endpoint
old_dca_status = """            # DCA стратегия
            "dca_mode": Config.DCA_MODE,
            "dca_stake_ton": Config.DCA_STAKE_TON,
            "dca_target_profit_pct": Config.DCA_TARGET_PROFIT_PCT,
            "dca_drop_trigger_pct": Config.DCA_DROP_TRIGGER_PCT,
            "dca_pullback_wait_pct": Config.DCA_PULLBACK_WAIT_PCT,
            "dca_max_entries": Config.DCA_MAX_ENTRIES,
            # DCA улучшения (4 механизма)
            "dca_cascade_enabled": Config.DCA_CASCADE_ENABLED,
            "dca_cascade_level1_pct": Config.DCA_CASCADE_LEVEL1_PCT,
            "dca_cascade_level2_pct": Config.DCA_CASCADE_LEVEL2_PCT,
            "dca_smart_reentry_enabled": Config.DCA_SMART_REENTRY_ENABLED,
            "dca_smart_reentry_pullback_pct": Config.DCA_SMART_REENTRY_PULLBACK_PCT,
            "dca_smart_reentry_min_ai_conf": Config.DCA_SMART_REENTRY_MIN_AI_CONF,
            "dca_compound_enabled": Config.DCA_COMPOUND_ENABLED,
            "dca_compound_ratio": Config.DCA_COMPOUND_RATIO,
            "dca_compound_max_ton": Config.DCA_COMPOUND_MAX_TON,
            "dca_adaptive_trigger_enabled": Config.DCA_ADAPTIVE_TRIGGER_ENABLED,
            "dca_adaptive_fast_move_pct": Config.DCA_ADAPTIVE_FAST_MOVE_PCT,
            "dca_adaptive_fast_drop_pct": Config.DCA_ADAPTIVE_FAST_DROP_PCT,
            # Детектор крупных продаж
            "large_sell_dca_enabled": Config.LARGE_SELL_DCA_ENABLED,
            "large_sell_dca_ton": Config.LARGE_SELL_DCA_TON,
            "large_sell_min_ton": Config.LARGE_SELL_MIN_TON,
            "large_sell_cooldown_sec": Config.LARGE_SELL_COOLDOWN_SEC,
"""
new_dca_status = """            # Grid стратегия
            "grid_mode": Config.GRID_MODE,
            "grid_step_pct": Config.GRID_STEP_PCT,
            "grid_sell_levels": Config.GRID_SELL_LEVELS,
            "grid_buy_levels": Config.GRID_BUY_LEVELS,
"""
content = content.replace(old_dca_status, new_dca_status)

# 4. Remove dca_ai_sell_block_conf from status
content = content.replace(
    '            "loss_cooldown_sec": Config.LOSS_COOLDOWN_SEC,\n            "dca_ai_sell_block_conf": Config.DCA_AI_SELL_BLOCK_CONF,\n            "confluence_enabled": Config.CONFLUENCE_ENABLED,',
    '            "loss_cooldown_sec": Config.LOSS_COOLDOWN_SEC,\n            "confluence_enabled": Config.CONFLUENCE_ENABLED,',
)

# 5. Remove DCA config update block
old_dca_update = """    # DCA стратегия
    if "dca_mode" in data:
        new_dca = bool(data["dca_mode"])
        if new_dca != Config.DCA_MODE:
            if trader.open_trades:
                return (
                    jsonify(
                        {
                            "ok": False,
                            "message": "Нельзя переключить DCA при открытых сделках.",
                        }
                    ),
                    409,
                )
            Config.DCA_MODE = new_dca
            # Сброс DCA-состояния при смене режима
            trader.dca_wait_pullback = False
            trader.dca_peak_price = 0.0
            trader.dca_last_buy_price = 0.0
            trader.dca_entries_count = 0
            trader.dca_total_stake = 0.0
            trader.log(f"🔄 DCA режим {'включён' if new_dca else 'выключен'}", "INFO")
    if (v := num("dca_stake_ton", 1, 10000)) is not None:
        Config.DCA_STAKE_TON = v
    if (v := num("dca_target_profit_pct", 1, 200)) is not None:
        Config.DCA_TARGET_PROFIT_PCT = v
    if (v := num("dca_drop_trigger_pct", 1, 90)) is not None:
        Config.DCA_DROP_TRIGGER_PCT = v
    if (v := num("dca_pullback_wait_pct", 5, 90)) is not None:
        Config.DCA_PULLBACK_WAIT_PCT = v
    if (v := num("dca_max_entries", 1, 50)) is not None:
        Config.DCA_MAX_ENTRIES = int(v)
    # DCA улучшения
    if "dca_cascade_enabled" in data:
        Config.DCA_CASCADE_ENABLED = bool(data["dca_cascade_enabled"])
    if (v := num("dca_cascade_level1_pct", 5, 100)) is not None:
        Config.DCA_CASCADE_LEVEL1_PCT = v
    if (v := num("dca_cascade_level2_pct", 5, 100)) is not None:
        Config.DCA_CASCADE_LEVEL2_PCT = v
    if "dca_smart_reentry_enabled" in data:
        Config.DCA_SMART_REENTRY_ENABLED = bool(data["dca_smart_reentry_enabled"])
    if (v := num("dca_smart_reentry_pullback_pct", 1, 90)) is not None:
        Config.DCA_SMART_REENTRY_PULLBACK_PCT = v
    if (v := num("dca_smart_reentry_min_ai_conf", 10, 100)) is not None:
        Config.DCA_SMART_REENTRY_MIN_AI_CONF = v
    if "dca_compound_enabled" in data:
        Config.DCA_COMPOUND_ENABLED = bool(data["dca_compound_enabled"])
    if (v := num("dca_compound_ratio", 0.1, 10)) is not None:
        Config.DCA_COMPOUND_RATIO = v
    if (v := num("dca_compound_max_ton", 10, 10000)) is not None:
        Config.DCA_COMPOUND_MAX_TON = v
    if "dca_adaptive_trigger_enabled" in data:
        Config.DCA_ADAPTIVE_TRIGGER_ENABLED = bool(data["dca_adaptive_trigger_enabled"])
    if (v := num("dca_adaptive_fast_move_pct", 1, 50)) is not None:
        Config.DCA_ADAPTIVE_FAST_MOVE_PCT = v
    if (v := num("dca_adaptive_fast_drop_pct", 1, 50)) is not None:
        Config.DCA_ADAPTIVE_FAST_DROP_PCT = v
    # Детектор крупных продаж
    if "large_sell_dca_enabled" in data:
        Config.LARGE_SELL_DCA_ENABLED = bool(data["large_sell_dca_enabled"])
    if (v := num("large_sell_dca_ton", 10, 10000)) is not None:
        Config.LARGE_SELL_DCA_TON = v
    if (v := num("large_sell_min_ton", 1, 5000)) is not None:
        Config.LARGE_SELL_MIN_TON = v
    if (v := num("large_sell_cooldown_sec", 60, 86400)) is not None:
        Config.LARGE_SELL_COOLDOWN_SEC = int(v)
"""
new_dca_update = """    # Grid стратегия
    if "grid_mode" in data:
        new_grid = bool(data["grid_mode"])
        if new_grid != Config.GRID_MODE:
            Config.GRID_MODE = new_grid
            trader.log(f"🔄 Grid режим {'включён' if new_grid else 'выключен'}", "INFO")
    if (v := num("grid_step_pct", 1.0, 50.0)) is not None:
        Config.GRID_STEP_PCT = v
    if (v := num("grid_sell_levels", 1, 100)) is not None:
        Config.GRID_SELL_LEVELS = int(v)
    if (v := num("grid_buy_levels", 1, 100)) is not None:
        Config.GRID_BUY_LEVELS = int(v)
"""
content = content.replace(old_dca_update, new_dca_update)

# 6. Remove DCA_REENTRY_COOLDOWN_SEC from config export
content = content.replace(
    '                "DCA_REENTRY_COOLDOWN_SEC": Config.DCA_REENTRY_COOLDOWN_SEC,\n                "FAST_REENTRY_MIN_CONF": Config.FAST_REENTRY_MIN_CONF,',
    '                "FAST_REENTRY_MIN_CONF": Config.FAST_REENTRY_MIN_CONF,',
)

# 7. Remove DCA_AI_SELL_BLOCK_CONF from config export
content = content.replace(
    '                "DCA_AI_SELL_BLOCK_CONF": Config.DCA_AI_SELL_BLOCK_CONF,\n            }\n        )\n\n    return jsonify({"ok": True, "message": "Настройки обновлены"})',
    '            }\n        )\n\n    return jsonify({"ok": True, "message": "Настройки обновлены"})',
)

with open("app.py", "w", encoding="utf-8") as f:
    f.write(content)

print("app.py cleaned")
