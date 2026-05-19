#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TradingBot — Bot de trading actif sur matières premières
Actifs    : GLD (Or), SLV (Argent), USO (Pétrole), UNG (Gaz naturel)
Stratégie : EMA 20/50 + RSI 14 + MACD sur données horaires
Broker    : Alpaca — supporte les fractions d'actions (fonctionne dès 10$)
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

# ---- Actifs tradés (matières premières ETF) ----
ASSETS = [
    "GLD",  # Or
    "SLV",  # Argent
    "USO",  # Pétrole brut
    "UNG",  # Gaz naturel
]

# ---- Paramètres des indicateurs (données horaires) ----
EMA_SHORT = 20          # EMA 20 heures ≈ 3 jours de trading
EMA_LONG = 50           # EMA 50 heures ≈ 7 jours de trading
RSI_PERIOD = 14
RSI_BUY_MAX = 45        # Achète si RSI < 45 (zone de survente relative)
RSI_SELL_MIN = 65       # Vend si RSI > 65 (zone de surachat)
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL_PERIOD = 9
DATA_INTERVAL = "1h"    # Données horaires → plus de signaux qu'en journalier
DATA_PERIOD = "60d"     # 60 jours d'historique (limite yfinance pour 1h)

# ---- Gestion du risque ----
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "0.03"))
MAX_POSITION_PCT = float(os.getenv("MAX_POSITION_PCT", "0.20"))
DAILY_LOSS_LIMIT_PCT = float(os.getenv("DAILY_LOSS_LIMIT_PCT", "0.05"))
MIN_ORDER_USD = 5.0     # Montant minimum par ordre (limite Alpaca)

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
# SECTION 3 — SERVICE ALPACA
# ============================================================

trading_client = TradingClient(
    api_key=ALPACA_API_KEY,
    secret_key=ALPACA_SECRET_KEY,
    paper=ALPACA_PAPER,
)


def get_account() -> dict:
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
    try:
        pos = trading_client.get_open_position(symbol)
        return {
            "symbol": symbol,
            "qty": float(pos.qty),
            "avg_entry_price": float(pos.avg_entry_price),
            "current_price": float(pos.current_price),
            "unrealized_pl": float(pos.unrealized_pl),
            "unrealized_plpc": float(pos.unrealized_plpc),
            "market_value": float(pos.market_value),
        }
    except Exception:
        return None


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


def place_order_notional(symbol: str, side: str, amount_usd: float) -> Optional[dict]:
    """
    Ordre en dollars (notional) — permet les fractions d'actions.
    Fonctionne avec n'importe quel montant >= MIN_ORDER_USD.
    """
    if amount_usd < MIN_ORDER_USD:
        logger.warning("Montant trop faible pour %s : %.2f$ (min %.2f$)", symbol, amount_usd, MIN_ORDER_USD)
        return None
    try:
        order = trading_client.submit_order(
            MarketOrderRequest(
                symbol=symbol,
                notional=round(amount_usd, 2),
                side=OrderSide.BUY if side == "BUY" else OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
            )
        )
        logger.info("Ordre %s %s %.2f$ soumis (id=%s)", side, symbol, amount_usd, order.id)
        return {"id": str(order.id), "symbol": symbol, "side": side, "notional": amount_usd}
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
# SECTION 4 — ANALYSE TECHNIQUE (données horaires)
# ============================================================

def fetch_ohlcv(symbol: str) -> Optional[pd.DataFrame]:
    """Données horaires sur 60 jours via yfinance."""
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
    """Calcule tous les indicateurs sur données horaires."""
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
    """
    Achat si :
    - Pas déjà en position sur cet actif
    - EMA20 > EMA50 (tendance horaire haussière)
    - RSI < 45 (pas encore suracheté)
    - MACD vient de croiser à la hausse (déclencheur)
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
    Vente si :
    - RSI > 65 (suracheté), ou
    - EMA20 < EMA50 (retournement de tendance), ou
    - MACD croisement baissier
    """
    if not has_position:
        return False
    return (
        signals["rsi"] > RSI_SELL_MIN
        or signals["trend_bearish"]
        or signals["macd_bearish_cross"]
    )


def calculate_notional(buying_power: float) -> float:
    """Montant en dollars à investir : MAX_POSITION_PCT % du buying power."""
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
    """
    Exécutée toutes les 30 minutes (9h30–16h NY, lun–ven).
    Données horaires → signaux plus fréquents que le bot journalier.
    """
    logger.info("=== Analyse des signaux (horaire) ===")

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
        return

    buying_power = account["buying_power"]
    portfolio_value = account["portfolio_value"]
    mode = "🧪 PAPER" if ALPACA_PAPER else "💰 RÉEL"

    for symbol in ASSETS:
        logger.info("Analyse %s...", symbol)

        signals = get_signals(symbol)
        if signals is None:
            continue

        position = get_position(symbol)
        has_position = position is not None

        # --- Stop-loss ---
        if has_position and check_stop_loss(position):
            logger.warning("%s stop-loss déclenché (%.2f%%)", symbol, position["unrealized_plpc"] * 100)
            if close_position(symbol):
                pl = position["unrealized_pl"]
                update_daily_pnl(pl)
                send_telegram(
                    f"🛑 <b>STOP-LOSS {symbol}</b> [{mode}]\n"
                    f"Perte : <b>{pl:+.2f} $</b> ({position['unrealized_plpc']*100:+.1f}%)\n"
                    f"Prix : {signals['price']:.2f} $"
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
            notional = calculate_notional(buying_power)
            if place_order_notional(symbol, "BUY", notional):
                send_telegram(
                    f"📥 <b>ACHAT {symbol}</b> [{mode}]\n"
                    f"Montant investi : <b>{notional:.2f} $</b>\n"
                    f"Prix unitaire : {signals['price']:.2f} $ "
                    f"(≈ {notional/signals['price']:.4f} actions)\n"
                    f"RSI : {signals['rsi']:.1f} | EMA20 > EMA50 ✅ | MACD ↗ ✅\n"
                    f"Portfolio : {portfolio_value:.2f} $"
                )
                save_trade({
                    "symbol": symbol, "side": "BUY",
                    "price": signals["price"], "notional": notional,
                    "timestamp": datetime.now().isoformat(),
                })
                logger.info("ACHAT %s — %.2f$", symbol, notional)

        else:
            logger.info(
                "%s : %s | RSI=%.1f | EMA_bull=%s | MACD_cross=%s",
                symbol,
                "position ouverte" if has_position else "pas de signal",
                signals["rsi"], signals["trend_bullish"], signals["macd_bullish_cross"],
            )


def daily_summary() -> None:
    """Résumé quotidien envoyé à 16h05 (clôture Wall Street)."""
    account = get_account()
    positions = get_all_positions()
    daily = load_daily_pnl()
    mode = "🧪 PAPER" if ALPACA_PAPER else "💰 RÉEL"
    pnl = daily.get("pnl", 0.0)

    lines = [
        f"📊 <b>Résumé {datetime.now().strftime('%d/%m/%Y')}</b> [{mode}]",
        "",
        f"💼 Portfolio : <b>{account.get('portfolio_value', 0):.2f} $</b>",
        f"💵 Liquidités : {account.get('cash', 0):.2f} $",
        f"{'📈' if pnl >= 0 else '📉'} P&L du jour : <b>{pnl:+.2f} $</b>",
        f"Trades exécutés : {daily.get('trades', 0)}",
        "",
    ]

    if positions:
        lines.append("📋 <b>Positions ouvertes :</b>")
        for p in positions:
            arrow = "📈" if p["unrealized_pl"] >= 0 else "📉"
            lines.append(
                f"{arrow} <b>{p['symbol']}</b> | "
                f"Entrée : {p['avg_entry_price']:.2f} $ → {p['current_price']:.2f} $ | "
                f"P&L : {p['unrealized_pl']:+.2f} $ ({p['unrealized_plpc']*100:+.1f}%)"
            )
    else:
        lines.append("Aucune position ouverte ce soir")

    send_telegram("\n".join(lines))


# ============================================================
# SECTION 8 — MAIN & SCHEDULER
# ============================================================

def main() -> None:
    logger.info("Démarrage TradingBot — %s", "PAPER" if ALPACA_PAPER else "RÉEL")

    if not TRADES_FILE.exists():
        save_json(TRADES_FILE, [])
    load_daily_pnl()

    account = get_account()
    mode = "🧪 PAPER TRADING" if ALPACA_PAPER else "💰 TRADING RÉEL"

    send_telegram(
        f"🤖 <b>TradingBot démarré</b> — {mode}\n\n"
        f"Actifs : <b>{', '.join(ASSETS)}</b>\n"
        f"Données : horaires (1h) — signaux plus fréquents\n"
        f"Stratégie : EMA {EMA_SHORT}/{EMA_LONG} + RSI {RSI_PERIOD} + MACD\n"
        f"Stop-loss : {STOP_LOSS_PCT*100:.0f}% | "
        f"Position max : {MAX_POSITION_PCT*100:.0f}%\n"
        f"Fractions d'actions : ✅ (fonctionne dès {MIN_ORDER_USD:.0f}$)\n\n"
        f"💼 Portfolio : <b>{account.get('portfolio_value', 0):.2f} $</b>"
    )

    scheduler = BlockingScheduler(timezone="America/New_York")

    # Analyse toutes les 30 min, 9h30–16h, lundi–vendredi
    scheduler.add_job(
        run_strategy,
        "cron",
        day_of_week="mon-fri",
        hour="9-15",
        minute="*/30",
        id="run_strategy",
        name="Analyse signaux horaires",
    )

    # Résumé à 16h05 chaque soir de semaine
    scheduler.add_job(
        daily_summary,
        "cron",
        day_of_week="mon-fri",
        hour=16,
        minute=5,
        id="daily_summary",
        name="Résumé journalier",
    )

    logger.info("Scheduler actif — analyse toutes les 30 min (9h30-16h NY)")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("TradingBot arrêté proprement")
        send_telegram("🛑 <b>TradingBot arrêté</b>")


if __name__ == "__main__":
    main()
