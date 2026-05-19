#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TradingBot — Bot de trading actif connecté à Trading 212
Actifs    : GLD (Or), SLV (Argent), USO (Pétrole), UNG (Gaz naturel)
Stratégie : EMA 20/50 + RSI 14 + MACD sur données horaires
Broker    : Trading 212 Invest (API officielle)
Démarrage : python trading_bot.py
"""

# ============================================================
# SECTION 1 — IMPORTS & CONFIG
# ============================================================

import json
import logging
import os
import time
from datetime import datetime, date
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
import yfinance as yf
from apscheduler.schedulers.blocking import BlockingScheduler
from dotenv import load_dotenv

load_dotenv()

# ---- Clés API ----
T212_API_KEY: str = os.getenv("T212_API_KEY", "")
T212_DEMO: bool = os.getenv("T212_DEMO", "true").lower() == "true"
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID: str = os.getenv("CHAT_ID", "")
DEBUG: bool = os.getenv("DEBUG", "false").lower() == "true"

# ---- URL Trading 212 (demo ou live selon T212_DEMO) ----
T212_BASE = "https://demo.trading212.com" if T212_DEMO else "https://live.trading212.com"

# ---- Actifs : ticker Yahoo Finance → ticker Trading 212 ----
ASSETS = {
    "GLD": "GLD_US_EQ",   # Or
    "SLV": "SLV_US_EQ",   # Argent
    "USO": "USO_US_EQ",   # Pétrole brut
    "UNG": "UNG_US_EQ",   # Gaz naturel
}

# ---- Paramètres des indicateurs (données horaires) ----
EMA_SHORT = 20
EMA_LONG = 50
RSI_PERIOD = 14
RSI_BUY_MAX = 45
RSI_SELL_MIN = 65
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL_PERIOD = 9
DATA_INTERVAL = "1h"
DATA_PERIOD = "60d"

# ---- Gestion du risque ----
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "0.03"))
MAX_POSITION_PCT = float(os.getenv("MAX_POSITION_PCT", "0.20"))
DAILY_LOSS_LIMIT_PCT = float(os.getenv("DAILY_LOSS_LIMIT_PCT", "0.05"))
MIN_ORDER_EUR = 1.0  # Trading 212 accepte dès 1€

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

if not T212_API_KEY:
    raise ValueError("T212_API_KEY requis dans le .env (Paramètres → API dans Trading 212)")
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
    if not TELEGRAM_BOT_TOKEN or not CHAT_ID:
        return
    if len(text) > 4096:
        text = text[:4080] + "\n<i>[tronqué]</i>"
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        if not resp.ok:
            logger.warning("Telegram error : %s", resp.text[:200])
    except Exception as e:
        logger.error("Erreur Telegram : %s", e)


# ============================================================
# SECTION 3 — SERVICE TRADING 212
# ============================================================

def t212_get(endpoint: str) -> Optional[dict | list]:
    """Appel GET à l'API Trading 212."""
    try:
        resp = requests.get(
            f"{T212_BASE}{endpoint}",
            headers={"Authorization": T212_API_KEY},
            timeout=15,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        time.sleep(0.5)  # Respect du rate limit Trading 212
        return resp.json()
    except Exception as e:
        logger.error("T212 GET %s : %s", endpoint, e)
        return None


def t212_post(endpoint: str, body: dict) -> Optional[dict]:
    """Appel POST à l'API Trading 212."""
    try:
        resp = requests.post(
            f"{T212_BASE}{endpoint}",
            headers={"Authorization": T212_API_KEY, "Content-Type": "application/json"},
            json=body,
            timeout=15,
        )
        resp.raise_for_status()
        time.sleep(0.5)
        return resp.json()
    except Exception as e:
        logger.error("T212 POST %s : %s", endpoint, e)
        return None


def get_account() -> dict:
    """Retourne le solde et le buying power du compte."""
    data = t212_get("/api/v0/equity/account/cash")
    if not data:
        return {}
    return {
        "portfolio_value": float(data.get("total", 0)),
        "buying_power": float(data.get("free", 0)),
        "cash": float(data.get("cash", 0)),
        "invested": float(data.get("invested", 0)),
        "ppl": float(data.get("ppl", 0)),
    }


def get_position(symbol: str) -> Optional[dict]:
    """Retourne la position ouverte pour un symbole, None si aucune."""
    t212_ticker = ASSETS.get(symbol, f"{symbol}_US_EQ")
    data = t212_get(f"/api/v0/equity/portfolio/{t212_ticker}")
    if not data or data.get("quantity", 0) == 0:
        return None
    avg_price = float(data.get("averagePrice", 0))
    current_price = float(data.get("currentPrice", avg_price))
    qty = float(data.get("quantity", 0))
    ppl = float(data.get("ppl", 0))
    plpc = ((current_price - avg_price) / avg_price) if avg_price else 0
    return {
        "symbol": symbol,
        "t212_ticker": t212_ticker,
        "qty": qty,
        "avg_entry_price": avg_price,
        "current_price": current_price,
        "unrealized_pl": ppl,
        "unrealized_plpc": plpc,
        "market_value": qty * current_price,
    }


def get_all_positions() -> list[dict]:
    """Retourne toutes les positions ouvertes."""
    data = t212_get("/api/v0/equity/portfolio")
    if not data:
        return []
    positions = []
    for p in data:
        avg_price = float(p.get("averagePrice", 0))
        current_price = float(p.get("currentPrice", avg_price))
        qty = float(p.get("quantity", 0))
        plpc = ((current_price - avg_price) / avg_price) if avg_price else 0
        # Convertit le ticker T212 → ticker standard
        raw_ticker = p.get("ticker", "")
        symbol = raw_ticker.replace("_US_EQ", "").replace("_EQ", "")
        positions.append({
            "symbol": symbol,
            "qty": qty,
            "avg_entry_price": avg_price,
            "current_price": current_price,
            "unrealized_pl": float(p.get("ppl", 0)),
            "unrealized_plpc": plpc,
        })
    return positions


def place_buy_order(symbol: str, amount_eur: float, current_price: float) -> Optional[dict]:
    """
    Achète pour `amount_eur` euros d'un actif (fraction d'actions).
    Calcule la quantité à partir du prix actuel yfinance.
    """
    if amount_eur < MIN_ORDER_EUR:
        logger.warning("Montant trop faible : %.2f€ (min %.2f€)", amount_eur, MIN_ORDER_EUR)
        return None

    t212_ticker = ASSETS.get(symbol, f"{symbol}_US_EQ")
    # Calcul de la quantité fractionnaire
    quantity = round(amount_eur / current_price, 6)

    body = {
        "ticker": t212_ticker,
        "quantity": quantity,
        "type": "MARKET",
        "timeValidity": "DAY",
    }
    result = t212_post("/api/v0/equity/orders", body)
    if result:
        logger.info("ACHAT %s : %.6f actions (%.2f€)", symbol, quantity, amount_eur)
    return result


def close_position(symbol: str) -> bool:
    """Vend la totalité de la position sur un symbole."""
    position = get_position(symbol)
    if not position:
        return True  # Déjà fermée

    t212_ticker = ASSETS.get(symbol, f"{symbol}_US_EQ")
    body = {
        "ticker": t212_ticker,
        "quantity": position["qty"],
        "type": "MARKET",
        "timeValidity": "DAY",
    }
    result = t212_post("/api/v0/equity/orders", body)
    if result:
        logger.info("VENTE %s : %.6f actions fermées", symbol, position["qty"])
        return True
    return False


def is_market_open() -> bool:
    """
    Vérifie si le marché US est ouvert (9h30–16h00 ET, lundi–vendredi).
    Trading 212 n'a pas d'endpoint dédié — on utilise l'heure NY.
    """
    now = datetime.now(ZoneInfo("America/New_York"))
    if now.weekday() >= 5:  # Samedi ou dimanche
        return False
    open_time = now.replace(hour=9, minute=30, second=0, microsecond=0)
    close_time = now.replace(hour=16, minute=0, second=0, microsecond=0)
    return open_time <= now <= close_time


# ============================================================
# SECTION 4 — ANALYSE TECHNIQUE (données horaires yfinance)
# ============================================================

def fetch_ohlcv(symbol: str) -> Optional[pd.DataFrame]:
    try:
        df = yf.download(
            symbol,
            period=DATA_PERIOD,
            interval=DATA_INTERVAL,
            progress=False,
            auto_adjust=True,
        )
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
    macd_line = calc_ema(series, MACD_FAST) - calc_ema(series, MACD_SLOW)
    return macd_line, calc_ema(macd_line, MACD_SIGNAL_PERIOD)


def get_signals(symbol: str) -> Optional[dict]:
    df = fetch_ohlcv(symbol)
    if df is None:
        return None

    close = df["Close"].squeeze()
    ema20 = calc_ema(close, EMA_SHORT)
    ema50 = calc_ema(close, EMA_LONG)
    rsi = calc_rsi(close, RSI_PERIOD)
    macd_line, signal_line = calc_macd(close)

    price = float(close.iloc[-1])
    ema20_now = float(ema20.iloc[-1])
    ema50_now = float(ema50.iloc[-1])
    rsi_now = float(rsi.iloc[-1])
    macd_now = float(macd_line.iloc[-1])
    signal_now = float(signal_line.iloc[-1])
    macd_prev = float(macd_line.iloc[-2])
    signal_prev = float(signal_line.iloc[-2])

    return {
        "symbol": symbol,
        "price": price,
        "ema20": ema20_now,
        "ema50": ema50_now,
        "rsi": rsi_now,
        "macd": macd_now,
        "macd_signal": signal_now,
        "macd_bullish_cross": (macd_prev < signal_prev) and (macd_now > signal_now),
        "macd_bearish_cross": (macd_prev > signal_prev) and (macd_now < signal_now),
        "trend_bullish": ema20_now > ema50_now,
        "trend_bearish": ema20_now < ema50_now,
    }


# ============================================================
# SECTION 5 — STRATÉGIE
# ============================================================

def should_buy(signals: dict, has_position: bool) -> bool:
    if has_position:
        return False
    return (
        signals["trend_bullish"]
        and signals["rsi"] < RSI_BUY_MAX
        and signals["macd_bullish_cross"]
    )


def should_sell(signals: dict, has_position: bool) -> bool:
    if not has_position:
        return False
    return (
        signals["rsi"] > RSI_SELL_MIN
        or signals["trend_bearish"]
        or signals["macd_bearish_cross"]
    )


def calculate_amount(buying_power: float) -> float:
    """Montant à investir : MAX_POSITION_PCT % du buying power."""
    return round(buying_power * MAX_POSITION_PCT, 2)


# ============================================================
# SECTION 6 — GESTION DU RISQUE
# ============================================================

def check_stop_loss(position: dict) -> bool:
    return position["unrealized_plpc"] <= -STOP_LOSS_PCT


def check_daily_loss_limit() -> bool:
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
    """Exécutée toutes les 30 min — analyse et passe les ordres si signal."""
    logger.info("=== Analyse des signaux ===")

    if not is_market_open():
        logger.info("Marché fermé")
        return

    if check_daily_loss_limit():
        send_telegram(
            f"⛔ <b>Limite journalière atteinte</b> (-{DAILY_LOSS_LIMIT_PCT*100:.0f}%)\n"
            "Trading suspendu jusqu'à demain."
        )
        return

    account = get_account()
    if not account:
        logger.error("Impossible de récupérer le compte Trading 212")
        return

    buying_power = account["buying_power"]
    portfolio_value = account["portfolio_value"]
    mode = "🧪 DEMO" if T212_DEMO else "💰 RÉEL"

    for symbol in ASSETS:
        logger.info("Analyse %s...", symbol)

        signals = get_signals(symbol)
        if signals is None:
            continue

        position = get_position(symbol)
        has_position = position is not None

        # --- Stop-loss ---
        if has_position and check_stop_loss(position):
            logger.warning("%s stop-loss (%.2f%%)", symbol, position["unrealized_plpc"] * 100)
            if close_position(symbol):
                pl = position["unrealized_pl"]
                update_daily_pnl(pl)
                send_telegram(
                    f"🛑 <b>STOP-LOSS {symbol}</b> [{mode}]\n"
                    f"Perte : <b>{pl:+.2f} €</b> ({position['unrealized_plpc']*100:+.1f}%)\n"
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
                    reasons.append("EMA20 < EMA50")
                if signals["macd_bearish_cross"]:
                    reasons.append("MACD ↘")
                send_telegram(
                    f"📤 <b>VENTE {symbol}</b> [{mode}]\n"
                    f"P&L : <b>{pl:+.2f} €</b> ({position['unrealized_plpc']*100:+.1f}%)\n"
                    f"Prix : {signals['price']:.2f} $ | RSI : {signals['rsi']:.1f}\n"
                    f"Raison : {' + '.join(reasons)}"
                )
                save_trade({
                    "symbol": symbol, "side": "SELL",
                    "price": signals["price"], "pl": pl,
                    "reason": " + ".join(reasons),
                    "timestamp": datetime.now().isoformat(),
                })
                logger.info("VENTE %s — PL=%.2f€", symbol, pl)

        # --- Signal d'achat ---
        elif not has_position and should_buy(signals, has_position):
            amount = calculate_amount(buying_power)
            if place_buy_order(symbol, amount, signals["price"]):
                qty_approx = amount / signals["price"]
                send_telegram(
                    f"📥 <b>ACHAT {symbol}</b> [{mode}]\n"
                    f"Montant : <b>{amount:.2f} €</b>\n"
                    f"Prix : {signals['price']:.2f} $ "
                    f"(≈ {qty_approx:.4f} actions)\n"
                    f"RSI : {signals['rsi']:.1f} | EMA20 > EMA50 ✅ | MACD ↗ ✅\n"
                    f"Portfolio : {portfolio_value:.2f} €"
                )
                save_trade({
                    "symbol": symbol, "side": "BUY",
                    "price": signals["price"], "amount_eur": amount,
                    "timestamp": datetime.now().isoformat(),
                })
                logger.info("ACHAT %s — %.2f€", symbol, amount)

        else:
            logger.info(
                "%s : %s | RSI=%.1f | EMA_bull=%s | MACD_cross=%s",
                symbol,
                "en position" if has_position else "pas de signal",
                signals["rsi"], signals["trend_bullish"], signals["macd_bullish_cross"],
            )


def daily_summary() -> None:
    """Résumé journalier envoyé à 16h05 (après clôture Wall Street)."""
    account = get_account()
    positions = get_all_positions()
    daily = load_daily_pnl()
    mode = "🧪 DEMO" if T212_DEMO else "💰 RÉEL"
    pnl = daily.get("pnl", 0.0)

    lines = [
        f"📊 <b>Résumé {datetime.now().strftime('%d/%m/%Y')}</b> [{mode}]",
        "",
        f"💼 Portfolio : <b>{account.get('portfolio_value', 0):.2f} €</b>",
        f"💵 Liquidités : {account.get('cash', 0):.2f} €",
        f"{'📈' if pnl >= 0 else '📉'} P&L du jour : <b>{pnl:+.2f} €</b>",
        f"Trades exécutés : {daily.get('trades', 0)}",
        "",
    ]

    if positions:
        lines.append("📋 <b>Positions ouvertes :</b>")
        for p in positions:
            arrow = "📈" if p["unrealized_pl"] >= 0 else "📉"
            lines.append(
                f"{arrow} <b>{p['symbol']}</b> | "
                f"{p['avg_entry_price']:.2f} → {p['current_price']:.2f} $ | "
                f"P&L : {p['unrealized_pl']:+.2f} € ({p['unrealized_plpc']*100:+.1f}%)"
            )
    else:
        lines.append("Aucune position ouverte ce soir")

    send_telegram("\n".join(lines))
    logger.info("Résumé journalier envoyé")


# ============================================================
# SECTION 8 — MAIN & SCHEDULER
# ============================================================

def main() -> None:
    logger.info("Démarrage TradingBot Trading 212 — %s", "DEMO" if T212_DEMO else "RÉEL")

    if not TRADES_FILE.exists():
        save_json(TRADES_FILE, [])
    load_daily_pnl()

    account = get_account()
    mode = "🧪 DEMO TRADING" if T212_DEMO else "💰 TRADING RÉEL"

    send_telegram(
        f"🤖 <b>TradingBot démarré</b> — {mode}\n\n"
        f"Actifs : <b>{', '.join(ASSETS.keys())}</b>\n"
        f"Stratégie : EMA {EMA_SHORT}/{EMA_LONG} + RSI {RSI_PERIOD} + MACD (horaire)\n"
        f"Stop-loss : {STOP_LOSS_PCT*100:.0f}% | "
        f"Position max : {MAX_POSITION_PCT*100:.0f}%\n"
        f"Broker : Trading 212 ({'Demo' if T212_DEMO else 'Live'})\n\n"
        f"💼 Portfolio : <b>{account.get('portfolio_value', 0):.2f} €</b>\n"
        f"💵 Disponible : <b>{account.get('buying_power', 0):.2f} €</b>"
    )

    scheduler = BlockingScheduler(timezone="America/New_York")

    scheduler.add_job(
        run_strategy,
        "cron",
        day_of_week="mon-fri",
        hour="9-15",
        minute="*/30",
        id="run_strategy",
        name="Analyse signaux",
    )

    scheduler.add_job(
        daily_summary,
        "cron",
        day_of_week="mon-fri",
        hour=16,
        minute=5,
        id="daily_summary",
        name="Résumé journalier",
    )

    logger.info("Scheduler actif — analyse toutes les 30 min (9h30–16h NY, lun–ven)")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("TradingBot arrêté")
        send_telegram("🛑 <b>TradingBot arrêté</b>")


if __name__ == "__main__":
    main()
