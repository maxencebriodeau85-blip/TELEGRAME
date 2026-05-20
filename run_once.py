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
    load_daily_pnl,
    save_json,
    run_strategy,
    daily_summary,
    is_market_open,
    send_telegram,
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
    for path, default in [(TRADES_FILE, []), (DISABLED_ASSETS_FILE, [])]:
        if not path.exists():
            save_json(path, default)
    load_daily_pnl()

    now_ny = datetime.now(ZoneInfo("America/New_York"))

    # Résumé journalier si on est pile à 16h05 NY
    if now_ny.hour == 16 and 5 <= now_ny.minute < 25:
        logger.info("Envoi du résumé journalier")
        daily_summary()
    else:
        # Analyse normale
        run_strategy()
