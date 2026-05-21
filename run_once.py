#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Point d'entrée GitHub Actions.
Exécute la stratégie une fois et quitte — le scheduler est géré par GitHub Actions cron.
"""
import logging
from pathlib import Path

from trading_bot import (
    TRADES_FILE,
    DISABLED_ASSETS_FILE,
    POSITIONS_META_FILE,
    RISK_STATS_FILE,
    BOT_CONTROL_FILE,
    ASSETS,
    T212_DEMO,
    load_daily_pnl,
    save_json,
    run_strategy,
    daily_summary,
    is_market_open,
    send_telegram,
    get_account,
    get_all_positions,
    poll_telegram_commands,
)
from datetime import datetime
from zoneinfo import ZoneInfo

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
)
logger = logging.getLogger("RunOnce")

if __name__ == "__main__":
    # Initialise les fichiers de stockage si absents
    for path, default in [
        (TRADES_FILE, []),
        (DISABLED_ASSETS_FILE, []),
        (POSITIONS_META_FILE, {}),
        (RISK_STATS_FILE, {
            "win_streak": 0, "last_date": None,
            "current_week_pnl": 0.0, "weekly_results": [],
            "profitable_weeks": 0, "peak_portfolio_value": None,
            "live_start_date": None,
        }),
        (BOT_CONTROL_FILE, {
            "paused": False, "paused_at": None,
            "first_live_order_done": False,
            "calibration_live_start": None,
            "last_telegram_update_id": 0,
        }),
    ]:
        if not path.exists():
            save_json(path, default)
    load_daily_pnl()

    now_ny = datetime.now(ZoneInfo("America/New_York"))

    # Traiter les commandes Telegram (/pause, /resume, /status) à chaque run
    poll_telegram_commands()

    if not is_market_open():
        logger.info("Marché fermé — analyse ignorée")
    elif now_ny.hour == 16 and 5 <= now_ny.minute < 25:
        # Résumé journalier — envoyé une seule fois (fenêtre 16h05–16h24 NY)
        logger.info("Envoi du résumé journalier")
        daily_summary()
    elif now_ny.hour == 9 and 30 <= now_ny.minute < 50:
        # Message d'ouverture — envoyé une seule fois à l'ouverture du marché
        mode = "🧪 DEMO" if T212_DEMO else "💰 RÉEL"
        account = get_account()
        bal = f"{account['portfolio_value']:.2f}€" if account else "N/A"
        positions = get_all_positions()
        send_telegram(
            f"🟢 Marché ouvert — Bot actif {mode}\n"
            f"Portefeuille : {bal} | Positions : {len(positions)}/{len(ASSETS)} actifs"
        )
        run_strategy()
    else:
        # Analyse normale — les trades/SL génèrent leurs propres messages Telegram
        run_strategy()
