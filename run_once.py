#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Point d'entrée GitHub Actions.
Exécute la stratégie une fois et quitte — le scheduler est géré par GitHub Actions cron.
"""
import logging
from pathlib import Path

import sys

from trading_bot import (
    TRADES_FILE,
    DISABLED_ASSETS_FILE,
    POSITIONS_META_FILE,
    RISK_STATS_FILE,
    BOT_CONTROL_FILE,
    DAILY_PNL_FILE,
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
    _send_error_alert,
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
            "last_error_alert_ts": 0,
        }),
    ]:
        if not path.exists():
            save_json(path, default)
    load_daily_pnl()

    # Commandes Telegram en premier — même si T212 est down
    poll_telegram_commands()

    now_ldn = datetime.now(ZoneInfo("Europe/London"))

    if now_ldn.hour == 16 and 30 <= now_ldn.minute < 50:
        # Résumé journalier — fenêtre 16h30–16h49 London (après clôture LSE)
        # DOIT être testé AVANT la vérification T212 — daily_summary gère gracieusement
        # l'absence de données T212 (affiche "⚠️ T212 inaccessible").
        logger.info("Envoi du résumé journalier")
        daily_summary()

    elif not is_market_open():
        logger.info("Marché fermé — analyse ignorée")

    else:
        # Vérification connectivité T212 uniquement pendant les heures de trading.
        # Utilise sys.exit(1) pour que GitHub Actions marque le run comme échoué
        # si les clés API sont incorrectes ou le service indisponible.
        if not get_account():
            logger.error(
                "T212 inaccessible (mode=%s) — clés API incorrectes ou service down",
                "DEMO" if T212_DEMO else "LIVE",
            )
            _send_error_alert(
                f"T212 inaccessible (mode {'DEMO' if T212_DEMO else 'LIVE'})\n"
                "Vérifiez : T212_API_KEY, T212_API_KEY_ID, T212_DEMO"
            )
            sys.exit(1)

        # Message d'ouverture — premier run du jour pendant les heures de marché.
        # Déclenché sur flag plutôt que sur fenêtre horaire stricte (8h00-8h19)
        # pour résister aux retards GitHub Actions.
        daily = load_daily_pnl()
        if not daily.get("opening_sent"):
            mode = "🧪 DEMO" if T212_DEMO else "💰 RÉEL"
            account = get_account()
            bal = f"{account['portfolio_value']:.2f}€" if account else "N/A"
            _positions = get_all_positions()
            pos_str = f"{len(_positions)}" if _positions is not None else "?"
            send_telegram(
                f"🟢 Marché ouvert — Bot actif {mode}\n"
                f"Portefeuille : {bal} | Positions : {pos_str}/{len(ASSETS)} actifs"
            )
            daily["opening_sent"] = True
            save_json(DAILY_PNL_FILE, daily)
            logger.info("Message d'ouverture envoyé")

        run_strategy()
