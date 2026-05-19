#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TradingBot COMPLET — Trading 212 Invest
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Actifs    : Matières premières + Forex ETF + Crypto ETF (10 actifs)
Stratégie : EMA 20/50 + RSI 14 + MACD sur bougies 15 minutes
Risque    : Stop-loss et taille de position adaptés par catégorie
Broker    : Trading 212 Invest (fractions, 0 commission)
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

T212_BASE = "https://demo.trading212.com" if T212_DEMO else "https://live.trading212.com"

# ============================================================
# ACTIFS — Ticker Yahoo Finance + config par catégorie
# ============================================================
# Chaque actif a :
#   t212     : ticker dans Trading 212
#   category : groupe affiché dans le résumé
#   stop     : stop-loss en % (crypto plus large car plus volatile)
#   pct      : % max du portfolio à investir par position

ASSETS: dict[str, dict] = {
    # ── Matières premières ───────────────────────────────────
    "GLD":  {"t212": "GLD_US_EQ",  "category": "🪙 Matières premières", "stop": 0.03, "pct": 0.15},
    "SLV":  {"t212": "SLV_US_EQ",  "category": "🪙 Matières premières", "stop": 0.03, "pct": 0.15},
    "USO":  {"t212": "USO_US_EQ",  "category": "🪙 Matières premières", "stop": 0.04, "pct": 0.12},
    "UNG":  {"t212": "UNG_US_EQ",  "category": "🪙 Matières premières", "stop": 0.05, "pct": 0.10},

    # ── Forex ETF ────────────────────────────────────────────
    "FXE":  {"t212": "FXE_US_EQ",  "category": "💱 Forex",              "stop": 0.02, "pct": 0.15},
    "UUP":  {"t212": "UUP_US_EQ",  "category": "💱 Forex",              "stop": 0.02, "pct": 0.15},
    "FXB":  {"t212": "FXB_US_EQ",  "category": "💱 Forex",              "stop": 0.02, "pct": 0.10},
    "FXY":  {"t212": "FXY_US_EQ",  "category": "💱 Forex",              "stop": 0.02, "pct": 0.10},

    # ── Crypto ETF ───────────────────────────────────────────
    "BITO": {"t212": "BITO_US_EQ", "category": "₿ Crypto",              "stop": 0.07, "pct": 0.08},
    "IBIT": {"t212": "IBIT_US_EQ", "category": "₿ Crypto",              "stop": 0.07, "pct": 0.08},
}

# ---- Paramètres des indicateurs (bougies 15 min) ----
EMA_SHORT = 20
EMA_LONG = 50
RSI_PERIOD = 14
RSI_BUY_MAX = 45
RSI_SELL_MIN = 65
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL_PERIOD = 9
DATA_INTERVAL = "15m"
DATA_PERIOD = "60d"

# ---- Gestion du risque globale ----
DAILY_LOSS_LIMIT_PCT = float(os.getenv("DAILY_LOSS_LIMIT_PCT", "0.05"))
MIN_ORDER_EUR = 1.0

# ---- Fichiers de stockage ----
TRADES_FILE = Path("trades.json")
DAILY_PNL_FILE = Path("daily_pnl.json")
DISABLED_ASSETS_FILE = Path("disabled_assets.json")

# ---- Logging ----
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.DEBUG if DEBUG else logging.INFO,
)
logger = logging.getLogger("TradingBot")

if not T212_API_KEY:
    raise ValueError("T212_API_KEY requis dans le .env")
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


def is_asset_disabled(symbol: str) -> bool:
    """Vérifie si un actif a été désactivé suite à une erreur d'ordre."""
    disabled = load_json(DISABLED_ASSETS_FILE, [])
    return symbol in disabled


def disable_asset(symbol: str) -> None:
    """Désactive un actif temporairement si Trading 212 rejette l'ordre."""
    disabled = load_json(DISABLED_ASSETS_FILE, [])
    if symbol not in disabled:
        disabled.append(symbol)
        save_json(DISABLED_ASSETS_FILE, disabled)
        logger.warning("%s désactivé (actif non disponible sur T212)", symbol)
        send_telegram(
            f"⚠️ <b>{symbol} désactivé</b>\n"
            "Actif non disponible sur votre compte Trading 212.\n"
            "Vérifiez que cet ETF est accessible dans l'application."
        )


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
    try:
        resp = requests.get(
            f"{T212_BASE}{endpoint}",
            headers={"Authorization": T212_API_KEY},
            timeout=15,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        time.sleep(0.4)
        return resp.json()
    except Exception as e:
        logger.error("T212 GET %s : %s", endpoint, e)
        return None


def t212_post(endpoint: str, body: dict) -> Optional[dict]:
    try:
        resp = requests.post(
            f"{T212_BASE}{endpoint}",
            headers={"Authorization": T212_API_KEY, "Content-Type": "application/json"},
            json=body,
            timeout=15,
        )
        resp.raise_for_status()
        time.sleep(0.4)
        return resp.json()
    except Exception as e:
        logger.error("T212 POST %s : %s", endpoint, e)
        return None


def get_account() -> dict:
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
    t212_ticker = ASSETS[symbol]["t212"]
    data = t212_get(f"/api/v0/equity/portfolio/{t212_ticker}")
    if not data or float(data.get("quantity", 0)) == 0:
        return None
    avg_price = float(data.get("averagePrice", 0))
    current_price = float(data.get("currentPrice", avg_price))
    qty = float(data.get("quantity", 0))
    ppl = float(data.get("ppl", 0))
    plpc = ((current_price - avg_price) / avg_price) if avg_price else 0
    return {
        "symbol": symbol,
        "qty": qty,
        "avg_entry_price": avg_price,
        "current_price": current_price,
        "unrealized_pl": ppl,
        "unrealized_plpc": plpc,
        "market_value": qty * current_price,
    }


def get_all_positions() -> list[dict]:
    data = t212_get("/api/v0/equity/portfolio")
    if not data:
        return []
    result = []
    for p in data:
        avg_price = float(p.get("averagePrice", 0))
        current_price = float(p.get("currentPrice", avg_price))
        qty = float(p.get("quantity", 0))
        plpc = ((current_price - avg_price) / avg_price) if avg_price else 0
        raw = p.get("ticker", "")
        symbol = raw.replace("_US_EQ", "").replace("_EQ", "")
        result.append({
            "symbol": symbol,
            "qty": qty,
            "avg_entry_price": avg_price,
            "current_price": current_price,
            "unrealized_pl": float(p.get("ppl", 0)),
            "unrealized_plpc": plpc,
        })
    return result


def place_buy_order(symbol: str, amount_eur: float, current_price: float) -> bool:
    """Achète pour `amount_eur` € d'un actif en fraction d'actions."""
    if amount_eur < MIN_ORDER_EUR:
        logger.warning("%s montant trop faible : %.2f€", symbol, amount_eur)
        return False
    t212_ticker = ASSETS[symbol]["t212"]
    quantity = round(amount_eur / current_price, 6)
    result = t212_post("/api/v0/equity/orders", {
        "ticker": t212_ticker,
        "quantity": quantity,
        "type": "MARKET",
        "timeValidity": "DAY",
    })
    if result:
        logger.info("ACHAT %s : %.6f actions (%.2f€)", symbol, quantity, amount_eur)
        return True
    # L'ordre a échoué — l'actif est probablement indisponible sur ce compte
    disable_asset(symbol)
    return False


def close_position(symbol: str) -> bool:
    """Vend la totalité de la position."""
    position = get_position(symbol)
    if not position:
        return True
    t212_ticker = ASSETS[symbol]["t212"]
    result = t212_post("/api/v0/equity/orders", {
        "ticker": t212_ticker,
        "quantity": position["qty"],
        "type": "MARKET",
        "timeValidity": "DAY",
    })
    if result:
        logger.info("VENTE %s : %.6f actions", symbol, position["qty"])
        return True
    return False


def is_market_open() -> bool:
    """NYSE : 9h30–16h00 ET, lundi–vendredi."""
    now = datetime.now(ZoneInfo("America/New_York"))
    if now.weekday() >= 5:
        return False
    open_t = now.replace(hour=9, minute=30, second=0, microsecond=0)
    close_t = now.replace(hour=16, minute=0, second=0, microsecond=0)
    return open_t <= now <= close_t


# ============================================================
# SECTION 4 — ANALYSE TECHNIQUE (yfinance, bougies 15 min)
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
        logger.error("fetch_ohlcv %s : %s", symbol, e)
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
    ema20_now, ema50_now = float(ema20.iloc[-1]), float(ema50.iloc[-1])
    rsi_now = float(rsi.iloc[-1])
    macd_now, signal_now = float(macd_line.iloc[-1]), float(signal_line.iloc[-1])
    macd_prev, signal_prev = float(macd_line.iloc[-2]), float(signal_line.iloc[-2])

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
# SECTION 5 — STRATÉGIE & GESTION DU RISQUE
# ============================================================

def should_buy(signals: dict, has_position: bool) -> bool:
    """
    Achat si :
    - Pas de position ouverte sur cet actif
    - EMA20 > EMA50 (tendance haussière)
    - RSI < 45 (pas suracheté)
    - MACD croisement haussier (déclencheur)
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
    - EMA20 < EMA50 (retournement), ou
    - MACD croisement baissier
    """
    if not has_position:
        return False
    return (
        signals["rsi"] > RSI_SELL_MIN
        or signals["trend_bearish"]
        or signals["macd_bearish_cross"]
    )


def check_stop_loss(position: dict, symbol: str) -> bool:
    """Stop-loss adapté par actif (crypto plus large que forex)."""
    stop = ASSETS[symbol]["stop"]
    return position["unrealized_plpc"] <= -stop


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


def calculate_amount(symbol: str, buying_power: float) -> float:
    """Montant à investir selon le % configuré par actif."""
    pct = ASSETS[symbol]["pct"]
    return round(buying_power * pct, 2)


# ============================================================
# SECTION 6 — BOUCLE PRINCIPALE
# ============================================================

def run_strategy() -> None:
    """Exécutée toutes les 5 min — analyse les 10 actifs."""
    logger.info("=== Analyse (%s actifs) ===", len(ASSETS))

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
        logger.error("Impossible de récupérer le compte T212")
        return

    buying_power = account["buying_power"]
    portfolio_value = account["portfolio_value"]
    mode = "🧪 DEMO" if T212_DEMO else "💰 RÉEL"

    for symbol, asset_cfg in ASSETS.items():
        if is_asset_disabled(symbol):
            continue

        logger.info("Analyse %s [%s]...", symbol, asset_cfg["category"])

        signals = get_signals(symbol)
        if signals is None:
            continue

        position = get_position(symbol)
        has_position = position is not None
        stop_pct = asset_cfg["stop"]

        # --- Stop-loss adapté par actif ---
        if has_position and check_stop_loss(position, symbol):
            logger.warning("%s stop-loss (%.2f%% / seuil %.0f%%)",
                           symbol, position["unrealized_plpc"] * 100, stop_pct * 100)
            if close_position(symbol):
                pl = position["unrealized_pl"]
                update_daily_pnl(pl)
                send_telegram(
                    f"🛑 <b>STOP-LOSS {symbol}</b> [{mode}] {asset_cfg['category']}\n"
                    f"Perte : <b>{pl:+.2f} €</b> ({position['unrealized_plpc']*100:+.1f}%)\n"
                    f"Seuil configuré : -{stop_pct*100:.0f}% | "
                    f"Prix : {signals['price']:.4f} $"
                )
                save_trade({
                    "symbol": symbol, "side": "SELL_STOPLOSS",
                    "category": asset_cfg["category"],
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
                    reasons.append("EMA20↘EMA50")
                if signals["macd_bearish_cross"]:
                    reasons.append("MACD↘")
                send_telegram(
                    f"📤 <b>VENTE {symbol}</b> [{mode}] {asset_cfg['category']}\n"
                    f"P&L : <b>{pl:+.2f} €</b> ({position['unrealized_plpc']*100:+.1f}%)\n"
                    f"Prix : {signals['price']:.4f} $ | RSI : {signals['rsi']:.1f}\n"
                    f"Raison : {' + '.join(reasons)}"
                )
                save_trade({
                    "symbol": symbol, "side": "SELL",
                    "category": asset_cfg["category"],
                    "price": signals["price"], "pl": pl,
                    "reason": " + ".join(reasons),
                    "timestamp": datetime.now().isoformat(),
                })
                logger.info("VENTE %s — PL=%.2f€", symbol, pl)

        # --- Signal d'achat ---
        elif not has_position and should_buy(signals, has_position):
            amount = calculate_amount(symbol, buying_power)
            if place_buy_order(symbol, amount, signals["price"]):
                qty_approx = amount / signals["price"]
                send_telegram(
                    f"📥 <b>ACHAT {symbol}</b> [{mode}] {asset_cfg['category']}\n"
                    f"Montant : <b>{amount:.2f} €</b> "
                    f"(≈ {qty_approx:.4f} actions)\n"
                    f"Prix : {signals['price']:.4f} $ | RSI : {signals['rsi']:.1f}\n"
                    f"EMA20 > EMA50 ✅ | MACD ↗ ✅ | Stop-loss : -{stop_pct*100:.0f}%\n"
                    f"Portfolio : {portfolio_value:.2f} €"
                )
                save_trade({
                    "symbol": symbol, "side": "BUY",
                    "category": asset_cfg["category"],
                    "price": signals["price"], "amount_eur": amount,
                    "timestamp": datetime.now().isoformat(),
                })
                logger.info("ACHAT %s — %.2f€", symbol, amount)

        else:
            logger.debug(
                "%s : %s | RSI=%.1f | trend=%s | MACD_cross=%s",
                symbol,
                "en position" if has_position else "attente signal",
                signals["rsi"], signals["trend_bullish"], signals["macd_bullish_cross"],
            )


def daily_summary() -> None:
    """Résumé complet envoyé chaque soir à 16h05."""
    account = get_account()
    positions = get_all_positions()
    daily = load_daily_pnl()
    mode = "🧪 DEMO" if T212_DEMO else "💰 RÉEL"
    pnl = daily.get("pnl", 0.0)

    lines = [
        f"📊 <b>Résumé {datetime.now().strftime('%d/%m/%Y')}</b> [{mode}]",
        "",
        f"💼 Portfolio : <b>{account.get('portfolio_value', 0):.2f} €</b>",
        f"💵 Disponible : {account.get('buying_power', 0):.2f} €",
        f"📈 Investi : {account.get('invested', 0):.2f} €",
        f"{'📈' if pnl >= 0 else '📉'} P&L du jour : <b>{pnl:+.2f} €</b>",
        f"🔄 Trades : {daily.get('trades', 0)}",
        "",
    ]

    if positions:
        # Groupement par catégorie
        by_category: dict[str, list] = {}
        for p in positions:
            cat = ASSETS.get(p["symbol"], {}).get("category", "Autre")
            by_category.setdefault(cat, []).append(p)

        lines.append("📋 <b>Positions ouvertes :</b>")
        for cat, pos_list in by_category.items():
            lines.append(f"\n{cat}")
            for p in pos_list:
                arrow = "📈" if p["unrealized_pl"] >= 0 else "📉"
                lines.append(
                    f"  {arrow} <b>{p['symbol']}</b> | "
                    f"{p['avg_entry_price']:.4f}→{p['current_price']:.4f} $ | "
                    f"P&L : {p['unrealized_pl']:+.2f} € ({p['unrealized_plpc']*100:+.1f}%)"
                )
    else:
        lines.append("Aucune position ouverte ce soir")

    # Actifs désactivés
    disabled = load_json(DISABLED_ASSETS_FILE, [])
    if disabled:
        lines.append(f"\n⚠️ Actifs désactivés : {', '.join(disabled)}")

    send_telegram("\n".join(lines))
    logger.info("Résumé journalier envoyé")


# ============================================================
# SECTION 7 — MAIN & SCHEDULER
# ============================================================

def main() -> None:
    logger.info("Démarrage TradingBot — %s | %d actifs",
                "DEMO" if T212_DEMO else "RÉEL", len(ASSETS))

    # Initialisation des fichiers
    for f, default in [(TRADES_FILE, []), (DAILY_PNL_FILE, {}), (DISABLED_ASSETS_FILE, [])]:
        if not f.exists():
            save_json(f, default)
    load_daily_pnl()

    account = get_account()
    mode = "🧪 DEMO TRADING" if T212_DEMO else "💰 TRADING RÉEL"

    # Regroupement des actifs par catégorie pour le message de démarrage
    categories: dict[str, list] = {}
    for symbol, cfg in ASSETS.items():
        categories.setdefault(cfg["category"], []).append(symbol)

    assets_text = "\n".join(
        f"{cat} : {', '.join(symbols)}"
        for cat, symbols in categories.items()
    )

    send_telegram(
        f"🤖 <b>TradingBot démarré</b> — {mode}\n\n"
        f"{assets_text}\n\n"
        f"Stratégie : EMA {EMA_SHORT}/{EMA_LONG} + RSI {RSI_PERIOD} + MACD (15min)\n"
        f"Analyse : toutes les 5 min | Résumé : 16h05 NY\n"
        f"Stop-loss : Matières 3-5% | Forex 2% | Crypto 7%\n\n"
        f"💼 Portfolio : <b>{account.get('portfolio_value', 0):.2f} €</b>\n"
        f"💵 Disponible : <b>{account.get('buying_power', 0):.2f} €</b>"
    )

    scheduler = BlockingScheduler(timezone="America/New_York")

    # Analyse toutes les 5 min, 9h30–16h, lun–ven
    scheduler.add_job(
        run_strategy,
        "cron",
        day_of_week="mon-fri",
        hour="9-15",
        minute="*/5",
        id="run_strategy",
        name="Analyse signaux",
    )

    # Résumé quotidien à 16h05
    scheduler.add_job(
        daily_summary,
        "cron",
        day_of_week="mon-fri",
        hour=16,
        minute=5,
        id="daily_summary",
        name="Résumé journalier",
    )

    logger.info("Scheduler actif — 5 min / 9h30–16h NY / lun–ven")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("TradingBot arrêté proprement")
        send_telegram("🛑 <b>TradingBot arrêté</b>")


if __name__ == "__main__":
    main()
