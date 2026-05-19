#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TradingBot — Bot de trading automatique sur signaux techniques
Actifs    : GLD (Or ETF), USO (Pétrole ETF)
Stratégie : EMA 50/200 + RSI 14 + MACD 12/26/9
Broker    : Alpaca (paper trading par défaut)
Démarrage : python trading_bot.py
"""

# ============================================================
# SECTION 1 — IMPORTS & CONFIG
# ============================================================

import json
import logging
import os
from datetime import datetime, date
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import requests
import yfinance as yf
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from apscheduler.schedulers.blocking import BlockingScheduler
from dotenv import load_dotenv

load_dotenv()

# ---- Clés API ----
ALPACA_API_KEY: str = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY: str = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_PAPER: bool = os.getenv("ALPACA_PAPER", "true").lower() == "true"
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID: str = os.getenv("CHAT_ID", "")
DEBUG: bool = os.getenv("DEBUG", "false").lower() == "true"

# ---- Actifs tradés ----
ASSETS = ["GLD", "USO"]  # Or (GLD), Pétrole (USO)

# ---- Paramètres des indicateurs ----
EMA_SHORT = 50
EMA_LONG = 200
RSI_PERIOD = 14
RSI_BUY_MAX = 50       # N'achète que si RSI < 50 (tendance non épuisée)
RSI_SELL_MIN = 70      # Vend si RSI > 70 (suracheté)
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL_PERIOD = 9

# ---- Gestion du risque ----
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "0.03"))            # Stop-loss -3%
MAX_POSITION_PCT = float(os.getenv("MAX_POSITION_PCT", "0.20"))      # Max 20% du portfolio
DAILY_LOSS_LIMIT_PCT = float(os.getenv("DAILY_LOSS_LIMIT_PCT", "0.05"))  # -5% = stop journée

# ---- Fichiers de stockage ----
TRADES_FILE = Path("trades.json")
DAILY_PNL_FILE = Path("daily_pnl.json")

# ---- Logging ----
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.DEBUG if DEBUG else logging.INFO,
)
logger = logging.getLogger("TradingBot")

# Validation de la configuration au démarrage
if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
    raise ValueError("ALPACA_API_KEY et ALPACA_SECRET_KEY requis dans le .env")
if not TELEGRAM_BOT_TOKEN or not CHAT_ID:
    raise ValueError("TELEGRAM_BOT_TOKEN et CHAT_ID requis dans le .env")


# ============================================================
# SECTION 2 — STOCKAGE & NOTIFICATIONS TELEGRAM
# ============================================================

def load_json(path: Path, default) -> dict | list:
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error("Erreur lecture %s : %s", path, e)
    return default


def save_json(path: Path, data) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error("Erreur écriture %s : %s", path, e)


def save_trade(trade: dict) -> None:
    trades = load_json(TRADES_FILE, [])
    trades.append(trade)
    save_json(TRADES_FILE, trades)


def load_daily_pnl() -> dict:
    """Charge le P&L du jour. Réinitialise automatiquement chaque matin."""
    data = load_json(DAILY_PNL_FILE, {})
    today = str(date.today())
    if data.get("date") != today:
        data = {"date": today, "start_value": None, "trades": 0, "pnl": 0.0}
        save_json(DAILY_PNL_FILE, data)
    return data


def update_daily_pnl(pnl_delta: float) -> None:
    data = load_daily_pnl()
    data["pnl"] = round(data.get("pnl", 0.0) + pnl_delta, 2)
    data["trades"] = data.get("trades", 0) + 1
    save_json(DAILY_PNL_FILE, data)


def send_telegram(text: str) -> None:
    """Envoie une notification Telegram (synchrone)."""
    if not TELEGRAM_BOT_TOKEN or not CHAT_ID:
        return
    if len(text) > 4096:
        text = text[:4080] + "\n<i>[tronqué]</i>"
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        if not resp.ok:
            logger.warning("Telegram error : %s", resp.text[:200])
    except Exception as e:
        logger.error("Erreur envoi Telegram : %s", e)


# ============================================================
# SECTION 3 — SERVICE ALPACA
# ============================================================

# Client Alpaca initialisé une seule fois
trading_client = TradingClient(
    api_key=ALPACA_API_KEY,
    secret_key=ALPACA_SECRET_KEY,
    paper=ALPACA_PAPER,
)


def get_account() -> dict:
    """Retourne les infos du compte (valeur portfolio, liquidités, buying power)."""
    try:
        acc = trading_client.get_account()
        return {
            "portfolio_value": float(acc.portfolio_value),
            "buying_power": float(acc.buying_power),
            "cash": float(acc.cash),
        }
    except Exception as e:
        logger.error("Erreur get_account : %s", e)
        return {}


def get_position(symbol: str) -> Optional[dict]:
    """Retourne la position ouverte pour un symbole, None si aucune."""
    try:
        pos = trading_client.get_open_position(symbol)
        return {
            "symbol": symbol,
            "qty": float(pos.qty),
            "avg_entry_price": float(pos.avg_entry_price),
            "current_price": float(pos.current_price),
            "unrealized_pl": float(pos.unrealized_pl),
            "unrealized_plpc": float(pos.unrealized_plpc),
        }
    except Exception:
        return None  # Pas de position ouverte = normal


def get_all_positions() -> list[dict]:
    try:
        return [
            {
                "symbol": p.symbol,
                "qty": float(p.qty),
                "avg_entry_price": float(p.avg_entry_price),
                "current_price": float(p.current_price),
                "unrealized_pl": float(p.unrealized_pl),
                "unrealized_plpc": float(p.unrealized_plpc),
            }
            for p in trading_client.get_all_positions()
        ]
    except Exception as e:
        logger.error("Erreur get_all_positions : %s", e)
        return []


def place_order(symbol: str, side: str, qty: int) -> Optional[dict]:
    """Soumet un ordre market BUY ou SELL."""
    try:
        order = trading_client.submit_order(
            MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.BUY if side == "BUY" else OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
            )
        )
        logger.info("Ordre %s %s x%d soumis (id=%s)", side, symbol, qty, order.id)
        return {"id": str(order.id), "symbol": symbol, "side": side, "qty": qty}
    except Exception as e:
        logger.error("Erreur ordre %s %s : %s", side, symbol, e)
        return None


def close_position(symbol: str) -> bool:
    try:
        trading_client.close_position(symbol)
        logger.info("Position %s fermée", symbol)
        return True
    except Exception as e:
        logger.error("Erreur fermeture %s : %s", symbol, e)
        return False


def is_market_open() -> bool:
    try:
        return trading_client.get_clock().is_open
    except Exception as e:
        logger.error("Erreur is_market_open : %s", e)
        return False


# ============================================================
# SECTION 4 — ANALYSE TECHNIQUE
# ============================================================

def fetch_ohlcv(symbol: str, period: str = "1y") -> Optional[pd.DataFrame]:
    """Télécharge les données journalières via yfinance."""
    try:
        df = yf.download(symbol, period=period, interval="1d", progress=False, auto_adjust=True)
        if df.empty or len(df) < EMA_LONG:
            logger.warning("%s : données insuffisantes (%d barres)", symbol, len(df))
            return None
        return df
    except Exception as e:
        logger.error("Erreur fetch_ohlcv %s : %s", symbol, e)
        return None


def calc_ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def calc_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, min_periods=period).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, min_periods=period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def calc_macd(series: pd.Series) -> tuple[pd.Series, pd.Series]:
    """Retourne (ligne MACD, ligne signal)."""
    macd_line = calc_ema(series, MACD_FAST) - calc_ema(series, MACD_SLOW)
    return macd_line, calc_ema(macd_line, MACD_SIGNAL_PERIOD)


def get_signals(symbol: str) -> Optional[dict]:
    """
    Calcule EMA50, EMA200, RSI14 et MACD sur les données journalières.
    Retourne un dict complet avec valeurs actuelles et booléens de signal.
    """
    df = fetch_ohlcv(symbol)
    if df is None:
        return None

    close = df["Close"].squeeze()  # Gère les DataFrames multi-niveaux de yfinance

    ema50 = calc_ema(close, EMA_SHORT)
    ema200 = calc_ema(close, EMA_LONG)
    rsi = calc_rsi(close, RSI_PERIOD)
    macd_line, signal_line = calc_macd(close)

    # Valeurs sur la dernière et avant-dernière bougie
    price = float(close.iloc[-1])
    ema50_now = float(ema50.iloc[-1])
    ema200_now = float(ema200.iloc[-1])
    rsi_now = float(rsi.iloc[-1])
    macd_now = float(macd_line.iloc[-1])
    signal_now = float(signal_line.iloc[-1])
    macd_prev = float(macd_line.iloc[-2])
    signal_prev = float(signal_line.iloc[-2])

    return {
        "symbol": symbol,
        "price": price,
        "ema50": ema50_now,
        "ema200": ema200_now,
        "rsi": rsi_now,
        "macd": macd_now,
        "macd_signal": signal_now,
        # Croisements MACD
        "macd_bullish_cross": (macd_prev < signal_prev) and (macd_now > signal_now),
        "macd_bearish_cross": (macd_prev > signal_prev) and (macd_now < signal_now),
        # Tendance globale
        "trend_bullish": ema50_now > ema200_now,
        "trend_bearish": ema50_now < ema200_now,
    }


# ============================================================
# SECTION 5 — STRATÉGIE
# ============================================================

def should_buy(signals: dict, has_position: bool) -> bool:
    """
    Achat si :
    - Pas déjà en position
    - EMA50 > EMA200 (tendance haussière confirmée)
    - RSI < 50 (pas encore suracheté, momentum disponible)
    - MACD vient de croiser à la hausse (signal de déclenchement)
    """
    if has_position:
        return False
    return (
        signals["trend_bullish"]
        and signals["rsi"] < RSI_BUY_MAX
        and signals["macd_bullish_cross"]
    )


def should_sell(signals: dict, has_position: bool) -> bool:
    """
    Vente si l'un des critères est rempli :
    - RSI > 70 (suracheté)
    - EMA50 < EMA200 (tendance inversée)
    - MACD croisement baissier (perte de momentum)
    """
    if not has_position:
        return False
    return (
        signals["rsi"] > RSI_SELL_MIN
        or signals["trend_bearish"]
        or signals["macd_bearish_cross"]
    )


def calculate_qty(price: float, buying_power: float) -> int:
    """Taille de position : MAX_POSITION_PCT % du buying power disponible."""
    if price <= 0 or buying_power <= 0:
        return 1
    qty = int((buying_power * MAX_POSITION_PCT) / price)
    return max(qty, 1)


# ============================================================
# SECTION 6 — GESTION DU RISQUE
# ============================================================

def check_stop_loss(position: dict) -> bool:
    """True si la perte latente dépasse STOP_LOSS_PCT."""
    return position["unrealized_plpc"] <= -STOP_LOSS_PCT


def check_daily_loss_limit() -> bool:
    """
    True si le portfolio a perdu plus de DAILY_LOSS_LIMIT_PCT depuis l'ouverture.
    Mémorise la valeur de départ au premier appel de la journée.
    """
    account = get_account()
    if not account:
        return False

    daily = load_daily_pnl()
    if daily.get("start_value") is None:
        daily["start_value"] = account["portfolio_value"]
        save_json(DAILY_PNL_FILE, daily)
        return False

    loss_pct = (account["portfolio_value"] - daily["start_value"]) / daily["start_value"]
    if loss_pct <= -DAILY_LOSS_LIMIT_PCT:
        logger.warning("Limite journalière atteinte : %.2f%%", loss_pct * 100)
        return True
    return False


# ============================================================
# SECTION 7 — BOUCLE PRINCIPALE
# ============================================================

def run_strategy() -> None:
    """
    Exécutée toutes les 15 minutes (9h30–16h00 heure NY, lun–ven).
    Pour chaque actif : vérifie stop-loss → signal vente → signal achat.
    """
    logger.info("=== Analyse des signaux ===")

    if not is_market_open():
        logger.info("Marché fermé — analyse ignorée")
        return

    if check_daily_loss_limit():
        send_telegram(
            "⛔ <b>Limite de perte journalière atteinte</b>\n"
            f"Trading suspendu pour aujourd'hui (seuil : -{DAILY_LOSS_LIMIT_PCT*100:.0f}%)"
        )
        return

    account = get_account()
    if not account:
        logger.error("Impossible de récupérer le compte Alpaca")
        return

    buying_power = account["buying_power"]
    portfolio_value = account["portfolio_value"]
    mode = "🧪 PAPER" if ALPACA_PAPER else "💰 RÉEL"

    for symbol in ASSETS:
        logger.info("Analyse %s...", symbol)

        signals = get_signals(symbol)
        if signals is None:
            logger.warning("Signaux indisponibles pour %s", symbol)
            continue

        position = get_position(symbol)
        has_position = position is not None

        # --- Stop-loss prioritaire ---
        if has_position and check_stop_loss(position):
            logger.warning("%s stop-loss : %.2f%%", symbol, position["unrealized_plpc"] * 100)
            if close_position(symbol):
                pl = position["unrealized_pl"]
                update_daily_pnl(pl)
                send_telegram(
                    f"🛑 <b>STOP-LOSS {symbol}</b> [{mode}]\n"
                    f"Perte : <b>{pl:+.2f} $</b> ({position['unrealized_plpc']*100:+.1f}%)\n"
                    f"Prix de sortie : {signals['price']:.2f} $"
                )
                save_trade({
                    "symbol": symbol, "side": "SELL_STOPLOSS",
                    "price": signals["price"], "pl": pl,
                    "timestamp": datetime.now().isoformat(),
                })
            continue

        # --- Signal de vente ---
        if has_position and should_sell(signals, has_position):
            if close_position(symbol):
                pl = position["unrealized_pl"]
                update_daily_pnl(pl)
                reasons = []
                if signals["rsi"] > RSI_SELL_MIN:
                    reasons.append(f"RSI={signals['rsi']:.1f}")
                if signals["trend_bearish"]:
                    reasons.append("EMA50 < EMA200")
                if signals["macd_bearish_cross"]:
                    reasons.append("MACD ↘")
                send_telegram(
                    f"📤 <b>VENTE {symbol}</b> [{mode}]\n"
                    f"P&L : <b>{pl:+.2f} $</b> ({position['unrealized_plpc']*100:+.1f}%)\n"
                    f"Prix : {signals['price']:.2f} $ | RSI : {signals['rsi']:.1f}\n"
                    f"Raison : {' + '.join(reasons)}"
                )
                save_trade({
                    "symbol": symbol, "side": "SELL",
                    "price": signals["price"], "pl": pl,
                    "reason": " + ".join(reasons),
                    "timestamp": datetime.now().isoformat(),
                })
                logger.info("VENTE %s — PL=%.2f$", symbol, pl)

        # --- Signal d'achat ---
        elif not has_position and should_buy(signals, has_position):
            qty = calculate_qty(signals["price"], buying_power)
            if place_order(symbol, "BUY", qty):
                cost = qty * signals["price"]
                send_telegram(
                    f"📥 <b>ACHAT {symbol}</b> [{mode}]\n"
                    f"Quantité : <b>{qty}</b> × {signals['price']:.2f} $ = <b>{cost:.2f} $</b>\n"
                    f"RSI : {signals['rsi']:.1f} | EMA50 > EMA200 ✅ | MACD ↗ ✅\n"
                    f"Portfolio : {portfolio_value:.2f} $"
                )
                save_trade({
                    "symbol": symbol, "side": "BUY",
                    "price": signals["price"], "qty": qty, "cost": cost,
                    "timestamp": datetime.now().isoformat(),
                })
                logger.info("ACHAT %s — qty=%d prix=%.2f$", symbol, qty, signals["price"])

        else:
            status = "position ouverte" if has_position else "pas de signal"
            logger.info(
                "%s : %s | RSI=%.1f | EMA_bull=%s | MACD_cross=%s",
                symbol, status, signals["rsi"],
                signals["trend_bullish"], signals["macd_bullish_cross"],
            )


def daily_summary() -> None:
    """Résumé envoyé chaque soir à 16h05 (après clôture NY)."""
    account = get_account()
    positions = get_all_positions()
    daily = load_daily_pnl()
    mode = "🧪 PAPER" if ALPACA_PAPER else "💰 RÉEL"

    pnl = daily.get("pnl", 0.0)
    pnl_emoji = "📈" if pnl >= 0 else "📉"

    lines = [
        f"📊 <b>Résumé du {datetime.now().strftime('%d/%m/%Y')}</b> [{mode}]",
        "",
        f"💼 Portfolio : <b>{account.get('portfolio_value', 0):.2f} $</b>",
        f"💵 Liquidités : {account.get('cash', 0):.2f} $",
        f"{pnl_emoji} P&L du jour : <b>{pnl:+.2f} $</b>",
        f"Trades exécutés : {daily.get('trades', 0)}",
        "",
    ]

    if positions:
        lines.append("📋 <b>Positions ouvertes :</b>")
        for p in positions:
            arrow = "📈" if p["unrealized_pl"] >= 0 else "📉"
            lines.append(
                f"{arrow} <b>{p['symbol']}</b> : {p['qty']} actions | "
                f"Entrée : {p['avg_entry_price']:.2f} $ | "
                f"P&L : {p['unrealized_pl']:+.2f} $ ({p['unrealized_plpc']*100:+.1f}%)"
            )
    else:
        lines.append("Aucune position ouverte ce soir")

    send_telegram("\n".join(lines))
    logger.info("Résumé journalier envoyé")


# ============================================================
# SECTION 8 — MAIN & SCHEDULER
# ============================================================

def main() -> None:
    logger.info("Démarrage TradingBot — mode %s", "PAPER" if ALPACA_PAPER else "RÉEL")
    logger.info("Actifs : %s | Stop-loss : %.0f%% | Position max : %.0f%%",
                ASSETS, STOP_LOSS_PCT * 100, MAX_POSITION_PCT * 100)

    # Initialise les fichiers de stockage
    if not TRADES_FILE.exists():
        save_json(TRADES_FILE, [])
    load_daily_pnl()

    # Message de démarrage Telegram
    account = get_account()
    mode = "🧪 PAPER TRADING" if ALPACA_PAPER else "💰 TRADING RÉEL"
    send_telegram(
        f"🤖 <b>TradingBot démarré</b> — {mode}\n\n"
        f"Actifs : <b>{', '.join(ASSETS)}</b>\n"
        f"Stratégie : EMA {EMA_SHORT}/{EMA_LONG} + RSI {RSI_PERIOD} + MACD\n"
        f"Stop-loss : {STOP_LOSS_PCT*100:.0f}% | "
        f"Position max : {MAX_POSITION_PCT*100:.0f}% du portfolio\n"
        f"Limite journalière : -{DAILY_LOSS_LIMIT_PCT*100:.0f}%\n\n"
        f"💼 Portfolio initial : <b>{account.get('portfolio_value', 0):.2f} $</b>"
    )

    # Planificateur — fonctionne sur l'heure de New York
    scheduler = BlockingScheduler(timezone="America/New_York")

    # Analyse toutes les 15 min, 9h30–15h59, lundi–vendredi
    scheduler.add_job(
        run_strategy,
        "cron",
        day_of_week="mon-fri",
        hour="9-15",
        minute="*/15",
        id="run_strategy",
        name="Analyse signaux",
    )

    # Résumé quotidien à 16h05 (après clôture Wall Street)
    scheduler.add_job(
        daily_summary,
        "cron",
        day_of_week="mon-fri",
        hour=16,
        minute=5,
        id="daily_summary",
        name="Résumé journalier",
    )

    logger.info("Scheduler actif — analyse toutes les 15 min (9h30-16h NY, lun-ven)")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("TradingBot arrêté proprement")
        send_telegram("🛑 <b>TradingBot arrêté</b>")


if __name__ == "__main__":
    main()
