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
    load_daily_pnl,
    save_json,
    run_strategy,
    daily_summary,
    is_market_open,
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
        (RISK_STATS_FILE, {"win_streak": 0, "last_date": None}),
    ]:
        if not path.exists():
            save_json(path, default)
    load_daily_pnl()

    now_ny = datetime.now(ZoneInfo("America/New_York"))

    if not is_market_open():
        logger.info("Marché fermé — analyse ignorée")
    elif now_ny.hour == 16 and 5 <= now_ny.minute < 25:
        # Résumé journalier — envoyé une seule fois (fenêtre 16h05–16h24 NY)
        logger.info("Envoi du résumé journalier")
        daily_summary()
    else:
        # Analyse normale — les trades/SL génèrent leurs propres messages Telegram
        run_strategy()
