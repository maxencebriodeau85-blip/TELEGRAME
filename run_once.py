#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Point d'entrée GitHub Actions.
Exécute la stratégie une fois et quitte.
"""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from trading_bot import (
    TRADES_FILE,
    DISABLED_ASSETS_FILE,
    ASSETS,
    T212_DEMO,
    load_daily_pnl,
    save_json,
    run_strategy,
    daily_summary,
    is_market_open,
    send_telegram,
    get_account,
)

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
)
logger = logging.getLogger("RunOnce")

if __name__ == "__main__":
    # Initialise les fichiers si absents
    for path, default in [(TRADES_FILE, []), (DISABLED_ASSETS_FILE, [])]:
        if not path.exists():
            save_json(path, default)
    load_daily_pnl()

    now_ny = datetime.now(ZoneInfo("America/New_York"))
    mode = "🧪 DEMO" if T212_DEMO else "💰 RÉEL"

    if not is_market_open():
        logger.info("Marché fermé — analyse ignorée")
    elif now_ny.hour == 16 and 5 <= now_ny.minute < 25:
        # Résumé journalier à 16h05 NY
        daily_summary()
    else:
        # Scan normal — envoie un message de statut
        account = get_account()
        send_telegram(
            f"🔍 <b>Scan en cours…</b> [{mode}]\n"
            f"Heure NY : {now_ny.strftime('%H:%M')} | "
            f"{len(ASSETS)} actifs analysés\n"
            f"Portfolio : {account.get('portfolio_value', 0):.2f} € | "
            f"Disponible : {account.get('buying_power', 0):.2f} €"
        )
        run_strategy()
