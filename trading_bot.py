#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TradingBot COMPLET — Trading 212 Invest
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Actifs    : Matières premières + Forex ETF + Crypto ETF (10 actifs)
Stratégie : EMA 20/50 + RSI 14 + MACD adapté par catégorie, bougies 15min CLÔTURÉES
Risque    : Stop-loss par catégorie, circuit breaker -5%/jour, max 3 positions simultanées
Broker    : Trading 212 Invest (fractions, 0 commission)
"""

# ============================================================
# SECTION 1 — IMPORTS & CONFIG
# ============================================================

import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
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
# PARAMÈTRES DE SIGNAL PAR CATÉGORIE D'ACTIF
# ============================================================
#
# Matières premières (GLD/SLV/USO/UNG) : MACD standard 12/26/9, RSI seuil moyen
# Forex ETF (FXE/UUP/FXB/FXY) : MACD lent 24/52/18 (instruments très peu volatils,
#   le MACD standard génère trop de faux croisements sur 15min), RSI strict
# Crypto ETF (BITO/IBIT) : MACD rapide 8/17/9 (forte volatilité, captures les
#   moves intraday), RSI achat relevé à 55 car les crypto trendent fort

SIGNAL_PARAMS: dict[str, dict] = {
    "commodities": {
        "rsi_buy":    48,   # légèrement au-dessus de 45 pour plus d'entrées
        "rsi_sell":   65,
        "macd_fast":  12,
        "macd_slow":  26,
        "macd_sig":    9,
        "vol_mult":  1.3,   # volume signal > 1.3× EMA20(volume)
        "sell_min":    2,   # critères baissiers requis sur 3 pour vendre
    },
    "forex": {
        "rsi_buy":    45,
        "rsi_sell":   62,   # plus bas : forex ETF atteignent rarement RSI 65
        "macd_fast":  24,   # MACD lent adapté aux faibles amplitudes
        "macd_slow":  52,
        "macd_sig":   18,
        "vol_mult":  1.2,   # forex ETFs ont des spikes de volume moins marqués
        "sell_min":    2,
    },
    "crypto": {
        "rsi_buy":    55,   # crypto trendent fort, RSI 55 capture les tendances
        "rsi_sell":   72,   # seuil de survente élevé (crypto peut rester suracheté)
        "macd_fast":   8,   # MACD rapide pour instruments très volatils
        "macd_slow":  17,
        "macd_sig":    9,
        "vol_mult":  1.4,   # volume confirmation plus strict sur crypto
        "sell_min":    2,
    },
}

# ============================================================
# ACTIFS — Ticker Yahoo Finance + config par catégorie
# ============================================================

ASSETS: dict[str, dict] = {
    # ── Matières premières ───────────────────────────────────
    "GLD":  {"t212": "GLD_US_EQ",  "category": "🪙 Matières premières", "stop": 0.03, "pct": 0.15, "params": "commodities"},
    "SLV":  {"t212": "SLV_US_EQ",  "category": "🪙 Matières premières", "stop": 0.03, "pct": 0.15, "params": "commodities"},
    "USO":  {"t212": "USO_US_EQ",  "category": "🪙 Matières premières", "stop": 0.04, "pct": 0.12, "params": "commodities"},
    "UNG":  {"t212": "UNG_US_EQ",  "category": "🪙 Matières premières", "stop": 0.05, "pct": 0.10, "params": "commodities"},

    # ── Forex ETF ────────────────────────────────────────────
    "FXE":  {"t212": "FXE_US_EQ",  "category": "💱 Forex",              "stop": 0.02, "pct": 0.15, "params": "forex"},
    "UUP":  {"t212": "UUP_US_EQ",  "category": "💱 Forex",              "stop": 0.02, "pct": 0.15, "params": "forex"},
    "FXB":  {"t212": "FXB_US_EQ",  "category": "💱 Forex",              "stop": 0.02, "pct": 0.10, "params": "forex"},
    "FXY":  {"t212": "FXY_US_EQ",  "category": "💱 Forex",              "stop": 0.02, "pct": 0.10, "params": "forex"},

    # ── Crypto ETF ───────────────────────────────────────────
    "BITO": {"t212": "BITO_US_EQ", "category": "₿ Crypto",              "stop": 0.07, "pct": 0.08, "params": "crypto"},
    "IBIT": {"t212": "IBIT_US_EQ", "category": "₿ Crypto",              "stop": 0.07, "pct": 0.08, "params": "crypto"},
}

# ---- Paramètres techniques fixes ----
EMA_SHORT = 20
EMA_LONG  = 50
RSI_PERIOD = 14
DATA_INTERVAL = "15m"
DATA_PERIOD   = "5d"    # 5j × 26 barres = 130 barres, suffisant pour EMA50
DAILY_PERIOD  = "60d"   # pour le filtre de tendance journalière

# ---- Gestion du risque ----
DAILY_LOSS_LIMIT_PCT = float(os.getenv("DAILY_LOSS_LIMIT_PCT", "0.05"))
MIN_ORDER_EUR    = 1.0
MAX_OPEN_POSITIONS = 3  # positions simultanées maximum

# ---- Constantes T212 ----
T212_RATE_SLEEP: float = 0.4
T212_TIMEOUT: int = 15
T212_RETRY_DELAYS: tuple = (2, 4)

# Sentinelle : distingue "erreur API" de "ressource absente (404)"
_API_ERROR = object()

# ---- Fichiers de stockage ----
TRADES_FILE        = Path("trades.json")
DAILY_PNL_FILE     = Path("daily_pnl.json")
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
    """Écriture atomique : temp file + rename pour éviter la corruption."""
    tmp = path.with_suffix(".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(path)
    except Exception as e:
        logger.error("Erreur écriture %s : %s", path, e)
        tmp.unlink(missing_ok=True)


def save_trade(trade: dict) -> None:
    trades = load_json(TRADES_FILE, [])
    trades.append(trade)
    save_json(TRADES_FILE, trades)


def load_daily_pnl() -> dict:
    data = load_json(DAILY_PNL_FILE, {})
    today = str(datetime.now(ZoneInfo("America/New_York")).date())
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
    return symbol in load_json(DISABLED_ASSETS_FILE, [])


def disable_asset(symbol: str) -> None:
    disabled = load_json(DISABLED_ASSETS_FILE, [])
    if symbol not in disabled:
        disabled.append(symbol)
        save_json(DISABLED_ASSETS_FILE, disabled)
        logger.warning("%s désactivé (rejeté par T212)", symbol)
        send_telegram(
            f"⚠️ <b>{symbol} désactivé</b>\n"
            "Actif non disponible sur votre compte Trading 212.\n"
            "Vérifiez que cet ETF est accessible dans l'application."
        )


def send_telegram(text: str) -> None:
    """Envoie un message Telegram avec 2 retries (délais 2s, 4s)."""
    if not TELEGRAM_BOT_TOKEN or not CHAT_ID:
        return
    if len(text) > 4096:
        text = text[:4080] + "\n<i>[tronqué]</i>"
    for attempt, delay in enumerate([0, 2, 4]):
        if delay:
            time.sleep(delay)
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
                timeout=10,
            )
            if resp.ok:
                return
            logger.warning("Telegram tentative %d/3 : %s", attempt + 1, resp.text[:100])
        except Exception as e:
            logger.error("Telegram tentative %d/3 erreur : %s", attempt + 1, e)


# ============================================================
# SECTION 3 — SERVICE TRADING 212
# ============================================================

def _t212_request(method: str, url: str, body: Optional[dict] = None) -> object:
    """
    Appel T212 avec retry automatique (2s, 4s).
    Retourne : dict/list (succès) | None (404) | _API_ERROR (échec persistant).
    """
    headers = {"Authorization": T212_API_KEY}
    if body is not None:
        headers["Content-Type"] = "application/json"

    last_exc = None
    for attempt, delay in enumerate([0, *T212_RETRY_DELAYS]):
        if delay:
            time.sleep(delay)
        try:
            resp = requests.request(
                method, url, headers=headers, json=body, timeout=T212_TIMEOUT,
            )
            if resp.status_code == 404:
                return None
            if resp.status_code == 429:
                logger.warning("T212 rate limit — retry %d/%d",
                               attempt + 1, len(T212_RETRY_DELAYS) + 1)
                last_exc = "rate_limit"
                continue
            resp.raise_for_status()
            time.sleep(T212_RATE_SLEEP)
            return resp.json()
        except Exception as e:
            logger.warning("T212 %s tentative %d/%d : %s",
                           url, attempt + 1, len(T212_RETRY_DELAYS) + 1, e)
            last_exc = e

    logger.error("T212 %s échec après %d tentatives : %s",
                 url, len(T212_RETRY_DELAYS) + 1, last_exc)
    return _API_ERROR


def t212_get(endpoint: str) -> object:
    return _t212_request("GET", f"{T212_BASE}{endpoint}")


def t212_post(endpoint: str, body: dict) -> object:
    return _t212_request("POST", f"{T212_BASE}{endpoint}", body=body)


def get_account() -> dict:
    data = t212_get("/api/v0/equity/account/cash")
    if data is _API_ERROR or not data:
        return {}
    return {
        "portfolio_value": float(data.get("total", 0)),
        "buying_power":    float(data.get("free", 0)),
        "cash":            float(data.get("cash", 0)),
        "invested":        float(data.get("invested", 0)),
        "ppl":             float(data.get("ppl", 0)),
    }


def get_position(symbol: str) -> Optional[dict]:
    """
    Retourne la position ouverte ou None (pas de position).
    Lève RuntimeError si T212 est inaccessible.
    """
    data = t212_get(f"/api/v0/equity/portfolio/{ASSETS[symbol]['t212']}")
    if data is _API_ERROR:
        raise RuntimeError(f"T212 inaccessible pour {symbol}")
    if not data or float(data.get("quantity", 0)) == 0:
        return None
    avg_price     = float(data.get("averagePrice", 0))
    current_price = float(data.get("currentPrice", avg_price))
    qty           = float(data.get("quantity", 0))
    ppl           = float(data.get("ppl", 0))
    plpc = ((current_price - avg_price) / avg_price) if avg_price else 0
    return {
        "symbol":         symbol,
        "qty":            qty,
        "avg_entry_price": avg_price,
        "current_price":  current_price,
        "unrealized_pl":  ppl,
        "unrealized_plpc": plpc,
        "market_value":   qty * current_price,
    }


def get_all_positions() -> list[dict]:
    """Un seul appel T212 pour toutes les positions — utilisé dans run_strategy()."""
    data = t212_get("/api/v0/equity/portfolio")
    if data is _API_ERROR or not data:
        return []
    result = []
    for p in data:
        avg_price     = float(p.get("averagePrice", 0))
        current_price = float(p.get("currentPrice", avg_price))
        qty           = float(p.get("quantity", 0))
        plpc = ((current_price - avg_price) / avg_price) if avg_price else 0
        raw    = p.get("ticker", "")
        symbol = raw.replace("_US_EQ", "").replace("_EQ", "")
        result.append({
            "symbol":         symbol,
            "qty":            qty,
            "avg_entry_price": avg_price,
            "current_price":  current_price,
            "unrealized_pl":  float(p.get("ppl", 0)),
            "unrealized_plpc": plpc,
        })
    return result


def place_buy_order(symbol: str, amount_eur: float, current_price: float) -> bool:
    if amount_eur < MIN_ORDER_EUR:
        logger.warning("%s montant trop faible : %.2f€", symbol, amount_eur)
        return False
    quantity = round(amount_eur / current_price, 6)
    result = t212_post("/api/v0/equity/orders", {
        "ticker":       ASSETS[symbol]["t212"],
        "quantity":     quantity,
        "type":         "MARKET",
        "timeValidity": "DAY",
    })
    if result is _API_ERROR:
        logger.error("ACHAT %s échoué : T212 inaccessible", symbol)
        return False
    if result:
        logger.info("ACHAT %s : %.6f actions (%.2f€)", symbol, quantity, amount_eur)
        return True
    disable_asset(symbol)
    return False


def close_position(symbol: str) -> bool:
    try:
        position = get_position(symbol)
    except RuntimeError:
        logger.error("close_position %s : T212 inaccessible", symbol)
        return False
    if not position:
        return True
    result = t212_post("/api/v0/equity/orders", {
        "ticker":       ASSETS[symbol]["t212"],
        "quantity":     position["qty"],
        "type":         "MARKET",
        "timeValidity": "DAY",
    })
    if result is _API_ERROR:
        logger.error("VENTE %s échouée : T212 inaccessible", symbol)
        return False
    if result:
        logger.info("VENTE %s : %.6f actions", symbol, position["qty"])
        return True
    return False


def is_market_open() -> bool:
    now = datetime.now(ZoneInfo("America/New_York"))
    if now.weekday() >= 5:
        return False
    open_t  = now.replace(hour=9,  minute=30, second=0, microsecond=0)
    close_t = now.replace(hour=16, minute=0,  second=0, microsecond=0)
    return open_t <= now <= close_t


# ============================================================
# SECTION 4 — ANALYSE TECHNIQUE
# ============================================================

def fetch_ohlcv(symbol: str) -> Optional[pd.DataFrame]:
    """Données 15min — on garde +1 barre pour l'exclusion de la bougie incomplète."""
    try:
        df = yf.download(
            symbol, period=DATA_PERIOD, interval=DATA_INTERVAL,
            progress=False, auto_adjust=True,
        )
        if df.empty or len(df) < EMA_LONG + 1:
            logger.warning("%s : données insuffisantes (%d barres)", symbol, len(df))
            return None
        return df
    except Exception as e:
        logger.error("fetch_ohlcv %s : %s", symbol, e)
        return None


def _fetch_daily_trend(symbol: str) -> str:
    """
    Filtre de tendance de fond journalière : compare le dernier cours à l'EMA50 daily.
    Retourne 'bullish', 'bearish' ou 'unknown' (en cas d'erreur ou données insuffisantes).
    Les données journalières (60 barres) sont légères et rapides à télécharger.
    """
    try:
        df = yf.download(
            symbol, period=DAILY_PERIOD, interval="1d",
            progress=False, auto_adjust=True,
        )
        if df is None or df.empty or len(df) < 50:
            return "unknown"
        close     = df["Close"].squeeze()
        ema50_d   = float(close.ewm(span=50, adjust=False).mean().iloc[-1])
        price     = float(close.iloc[-1])
        return "bullish" if price > ema50_d else "bearish"
    except Exception as e:
        logger.warning("_fetch_daily_trend %s : %s", symbol, e)
        return "unknown"


def _volume_ok(df: pd.DataFrame, vol_mult: float) -> bool:
    """
    Filtre de volume : la dernière bougie clôturée doit dépasser vol_mult × EMA20(volume).
    Retourne True si pas de données de volume (forex ETFs) pour ne pas bloquer le signal.
    """
    try:
        volume = df["Volume"].squeeze().iloc[:-1]   # exclure bougie en cours
        if volume.empty or volume.sum() == 0:
            return True
        vol_ema = float(volume.ewm(span=20, adjust=False).mean().iloc[-1])
        if vol_ema == 0:
            return True
        return float(volume.iloc[-1]) >= vol_ema * vol_mult
    except Exception:
        return True


def calc_ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def calc_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain  = delta.clip(lower=0).ewm(alpha=1 / period, min_periods=period).mean()
    loss  = (-delta.clip(upper=0)).ewm(alpha=1 / period, min_periods=period).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def calc_macd(
    series: pd.Series, fast: int, slow: int, signal_p: int
) -> tuple[pd.Series, pd.Series]:
    macd_line = calc_ema(series, fast) - calc_ema(series, slow)
    return macd_line, calc_ema(macd_line, signal_p)


def get_signals(symbol: str, params_key: str = "commodities") -> Optional[dict]:
    """
    Calcule tous les indicateurs pour un actif.
    Retourne un dict incluant le score de confluence 0-5.

    Bougies 15min : on exclut systématiquement la dernière (encore en formation)
    pour éviter les faux croisements MACD mid-bar.

    La tendance journalière (EMA50 daily) est fetchée séquentiellement — légère
    (60 barres vs 130) et déjà parallélisée au niveau de _prefetch_signals().
    """
    params = SIGNAL_PARAMS[params_key]

    df          = fetch_ohlcv(symbol)
    daily_trend = _fetch_daily_trend(symbol)

    if df is None:
        return None

    # Exclure la bougie en cours (pas encore clôturée)
    close = df["Close"].squeeze().iloc[:-1]

    ema20      = calc_ema(close, EMA_SHORT)
    ema50      = calc_ema(close, EMA_LONG)
    rsi        = calc_rsi(close, RSI_PERIOD)
    macd_line, signal_line = calc_macd(
        close, params["macd_fast"], params["macd_slow"], params["macd_sig"]
    )

    price       = float(close.iloc[-1])
    ema20_now   = float(ema20.iloc[-1])
    ema50_now   = float(ema50.iloc[-1])
    rsi_now     = float(rsi.iloc[-1])
    macd_now    = float(macd_line.iloc[-1])
    signal_now  = float(signal_line.iloc[-1])
    macd_prev   = float(macd_line.iloc[-2])
    signal_prev = float(signal_line.iloc[-2])

    trend_bullish     = ema20_now > ema50_now
    trend_bearish     = ema20_now < ema50_now
    macd_bull_cross   = (macd_prev < signal_prev) and (macd_now > signal_now)
    macd_bear_cross   = (macd_prev > signal_prev) and (macd_now < signal_now)
    vol_ok            = _volume_ok(df, params["vol_mult"])

    # Score de confluence ACHAT 0-5 — sert à prioriser les signaux simultanés
    confluence_score = (
        int(trend_bullish)
        + int(macd_bull_cross)
        + int(rsi_now < params["rsi_buy"])
        + int(vol_ok)
        + int(daily_trend == "bullish")
    )

    return {
        "symbol":         symbol,
        "price":          price,
        "ema20":          ema20_now,
        "ema50":          ema50_now,
        "rsi":            rsi_now,
        "rsi_buy":        params["rsi_buy"],
        "rsi_sell":       params["rsi_sell"],
        "sell_min":       params["sell_min"],
        "macd":           macd_now,
        "macd_signal":    signal_now,
        "macd_bull_cross": macd_bull_cross,
        "macd_bear_cross": macd_bear_cross,
        "trend_bullish":  trend_bullish,
        "trend_bearish":  trend_bearish,
        "volume_ok":      vol_ok,
        "daily_trend":    daily_trend,
        "confluence_score": confluence_score,
    }


def _prefetch_signals(symbols: list[str]) -> dict[str, Optional[dict]]:
    """Télécharge les données de tous les actifs en parallèle (5 threads max)."""
    results: dict[str, Optional[dict]] = {}
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {
            pool.submit(get_signals, sym, ASSETS[sym]["params"]): sym
            for sym in symbols
        }
        for future in as_completed(futures):
            sym = futures[future]
            try:
                results[sym] = future.result()
            except Exception as e:
                logger.error("Erreur signal %s : %s", sym, e)
                results[sym] = None
    return results


# ============================================================
# SECTION 5 — STRATÉGIE & GESTION DU RISQUE
# ============================================================

def should_buy(signals: dict, has_position: bool) -> bool:
    """
    Achat si toutes les conditions sont réunies :
    - EMA20 > EMA50 (tendance 15min haussière)
    - RSI < seuil de la catégorie (48 commodities / 45 forex / 55 crypto)
    - MACD croisement haussier sur bougie clôturée
    - Tendance journalière NON baissière (gate dur : on ne trade pas contre EMA50 daily)
    """
    if has_position:
        return False
    return (
        signals["trend_bullish"]
        and signals["rsi"] < signals["rsi_buy"]
        and signals["macd_bull_cross"]
        and signals["daily_trend"] != "bearish"
    )


def should_sell(signals: dict, has_position: bool) -> bool:
    """
    Vente si au moins sell_min critères baissiers sont réunis (défaut : 2/3).
    Réduit les sorties prématurées dues à un RSI bref ou un faux croisement MACD.
    """
    if not has_position:
        return False
    sell_score = (
        int(signals["trend_bearish"])
        + int(signals["rsi"] > signals["rsi_sell"])
        + int(signals["macd_bear_cross"])
    )
    return sell_score >= signals["sell_min"]


def check_stop_loss(position: dict, symbol: str) -> bool:
    return position["unrealized_plpc"] <= -ASSETS[symbol]["stop"]


def _close_all_positions() -> None:
    """Clôture toutes les positions — appelé par le circuit breaker."""
    for symbol in list(ASSETS.keys()):
        try:
            pos = get_position(symbol)
            if pos:
                close_position(symbol)
        except RuntimeError as e:
            logger.error("_close_all_positions %s : %s", symbol, e)


def check_daily_loss_limit() -> bool:
    """
    Vérifie la limite journalière.
    Si atteinte : clôture TOUTES les positions puis arrête le trading.
    """
    account = get_account()
    if not account:
        return False
    daily = load_daily_pnl()
    if daily.get("start_value") is None:
        daily["start_value"] = account["portfolio_value"]
        save_json(DAILY_PNL_FILE, daily)
        return False
    start = daily["start_value"]
    if start == 0:
        return False
    loss_pct = (account["portfolio_value"] - start) / start
    if loss_pct <= -DAILY_LOSS_LIMIT_PCT:
        logger.warning("Circuit breaker : %.2f%% — clôture de toutes les positions", loss_pct * 100)
        _close_all_positions()
        send_telegram(
            f"⛔ <b>Circuit breaker déclenché</b> ({loss_pct*100:+.1f}%)\n"
            f"Seuil : -{DAILY_LOSS_LIMIT_PCT*100:.0f}% | Toutes positions clôturées.\n"
            "Trading suspendu jusqu'à demain."
        )
        return True
    return False


def calculate_amount(symbol: str, buying_power: float) -> float:
    return round(buying_power * ASSETS[symbol]["pct"], 2)


# ============================================================
# SECTION 6 — EXÉCUTION DES ORDRES
# ============================================================

def _execute_stop_loss(symbol: str, signals: dict, position: dict, mode: str) -> None:
    asset_cfg = ASSETS[symbol]
    stop_pct  = asset_cfg["stop"]
    logger.warning("%s stop-loss (%.2f%% / seuil %.0f%%)",
                   symbol, position["unrealized_plpc"] * 100, stop_pct * 100)
    if close_position(symbol):
        pl = position["unrealized_pl"]
        update_daily_pnl(pl)
        send_telegram(
            f"🛑 <b>STOP-LOSS {symbol}</b> [{mode}] {asset_cfg['category']}\n"
            f"Perte : <b>{pl:+.2f} €</b> ({position['unrealized_plpc']*100:+.1f}%)\n"
            f"Seuil : -{stop_pct*100:.0f}% | Prix : {signals['price']:.4f} $"
        )
        save_trade({
            "symbol": symbol, "side": "SELL_STOPLOSS",
            "category": asset_cfg["category"],
            "price": signals["price"], "pl": pl,
            "timestamp": datetime.now().isoformat(),
        })


def _execute_sell(symbol: str, signals: dict, position: dict, mode: str) -> None:
    asset_cfg = ASSETS[symbol]
    if close_position(symbol):
        pl = position["unrealized_pl"]
        update_daily_pnl(pl)
        reasons = []
        if signals["rsi"] > signals["rsi_sell"]:
            reasons.append(f"RSI={signals['rsi']:.1f}")
        if signals["trend_bearish"]:
            reasons.append("EMA20↘EMA50")
        if signals["macd_bear_cross"]:
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


def _execute_buy(
    symbol: str, signals: dict, buying_power: float, portfolio_value: float, mode: str
) -> None:
    asset_cfg = ASSETS[symbol]
    stop_pct  = asset_cfg["stop"]
    amount    = calculate_amount(symbol, buying_power)
    if place_buy_order(symbol, amount, signals["price"]):
        qty_approx   = amount / signals["price"]
        daily_emoji  = {"bullish": "🌞", "bearish": "🌧️", "unknown": "❓"}.get(
            signals["daily_trend"], "❓"
        )
        vol_emoji    = "✅" if signals["volume_ok"] else "⚠️"
        send_telegram(
            f"📥 <b>ACHAT {symbol}</b> [{mode}] {asset_cfg['category']}\n"
            f"Montant : <b>{amount:.2f} €</b> (≈ {qty_approx:.4f} actions)\n"
            f"Prix : {signals['price']:.4f} $ | RSI : {signals['rsi']:.1f}/{signals['rsi_buy']}\n"
            f"EMA ✅ | MACD ↗ ✅ | Volume : {vol_emoji} | Trend J : {daily_emoji}\n"
            f"Confluence : <b>{signals['confluence_score']}/5</b> | "
            f"Stop-loss : -{stop_pct*100:.0f}%\n"
            f"Portfolio : {portfolio_value:.2f} €"
        )
        save_trade({
            "symbol": symbol, "side": "BUY",
            "category": asset_cfg["category"],
            "price": signals["price"], "amount_eur": amount,
            "confluence_score": signals["confluence_score"],
            "daily_trend": signals["daily_trend"],
            "timestamp": datetime.now().isoformat(),
        })
        logger.info("ACHAT %s — %.2f€ | confluence=%d/5", symbol, amount, signals["confluence_score"])


def _handle_asset(
    symbol: str,
    signals: dict,
    position: Optional[dict],
    buying_power: float,
    portfolio_value: float,
    mode: str,
    open_count: int,
) -> int:
    """
    Gère un actif. Retourne le delta de positions ouvertes :
    +1 si achat, -1 si vente ou stop-loss, 0 sinon.
    """
    has_position = position is not None

    # 1. Stop-loss — priorité absolue
    if has_position and check_stop_loss(position, symbol):
        _execute_stop_loss(symbol, signals, position, mode)
        return -1

    # 2. Vente (2/3 critères requis par défaut)
    if has_position and should_sell(signals, has_position):
        _execute_sell(symbol, signals, position, mode)
        return -1

    # 3. Achat — uniquement si sous la limite de positions simultanées
    if not has_position and should_buy(signals, has_position):
        if open_count >= MAX_OPEN_POSITIONS:
            logger.info(
                "%s : signal ACHAT ignoré [confluence=%d/5] — limite %d positions atteinte",
                symbol, signals["confluence_score"], MAX_OPEN_POSITIONS,
            )
            return 0
        _execute_buy(symbol, signals, buying_power, portfolio_value, mode)
        return 1

    logger.debug(
        "%s : %s | RSI=%.1f/%d | trend=%s | MACD↗=%s | vol=%s | daily=%s | score=%d/5",
        symbol,
        "position" if has_position else "attente",
        signals["rsi"], signals["rsi_buy"],
        signals["trend_bullish"], signals["macd_bull_cross"],
        signals["volume_ok"], signals["daily_trend"],
        signals["confluence_score"],
    )
    return 0


# ============================================================
# SECTION 7 — BOUCLE PRINCIPALE
# ============================================================

def run_strategy() -> None:
    """Point d'entrée de l'analyse — appelé par GitHub Actions toutes les 20 min."""
    logger.info("=== Analyse (%d actifs) ===", len(ASSETS))

    if not is_market_open():
        logger.info("Marché fermé")
        return

    if check_daily_loss_limit():
        return

    account = get_account()
    if not account:
        logger.error("Impossible de récupérer le compte T212")
        return

    buying_power    = account["buying_power"]
    portfolio_value = account["portfolio_value"]
    mode            = "🧪 DEMO" if T212_DEMO else "💰 RÉEL"

    # Un seul appel T212 pour toutes les positions existantes
    raw_positions  = get_all_positions()
    positions_map  = {p["symbol"]: p for p in raw_positions}
    open_count     = sum(1 for sym in ASSETS if sym in positions_map)

    # Téléchargement parallèle (5 threads) : 15min + daily par actif
    active_symbols = [sym for sym in ASSETS if not is_asset_disabled(sym)]
    all_signals    = _prefetch_signals(active_symbols)

    # Ordre de traitement :
    # 1. Actifs en position ouverte d'abord (stop-loss / vente prioritaires)
    # 2. Parmi les signaux d'achat, trier par confluence décroissante
    #    → si on atteint MAX_OPEN_POSITIONS, les meilleurs signaux sont traités en premier
    def _sort_key(sym: str) -> tuple:
        sig = all_signals.get(sym)
        if sig is None:
            return (2, 0)
        if sym in positions_map:
            return (0, 0)   # positions ouvertes → prioritaires
        return (1, -sig.get("confluence_score", 0))

    ordered = sorted(active_symbols, key=_sort_key)

    for symbol in ordered:
        signals  = all_signals.get(symbol)
        if signals is None:
            logger.warning("%s : données indisponibles, actif ignoré", symbol)
            continue
        position = positions_map.get(symbol)
        delta    = _handle_asset(
            symbol, signals, position,
            buying_power, portfolio_value, mode, open_count,
        )
        open_count += delta


def daily_summary() -> None:
    """Résumé complet envoyé chaque soir à 16h05 NY."""
    account   = get_account()
    positions = get_all_positions()
    daily     = load_daily_pnl()
    mode      = "🧪 DEMO" if T212_DEMO else "💰 RÉEL"
    pnl       = daily.get("pnl", 0.0)

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

    disabled = load_json(DISABLED_ASSETS_FILE, [])
    if disabled:
        lines.append(f"\n⚠️ Actifs désactivés : {', '.join(disabled)}")

    send_telegram("\n".join(lines))
    logger.info("Résumé journalier envoyé")


# ============================================================
# SECTION 8 — MAIN & SCHEDULER (exécution locale uniquement)
# ============================================================

def main() -> None:
    logger.info("Démarrage TradingBot — %s | %d actifs",
                "DEMO" if T212_DEMO else "RÉEL", len(ASSETS))

    for f, default in [(TRADES_FILE, []), (DAILY_PNL_FILE, {}), (DISABLED_ASSETS_FILE, [])]:
        if not f.exists():
            save_json(f, default)
    load_daily_pnl()

    account = get_account()
    mode    = "🧪 DEMO TRADING" if T212_DEMO else "💰 TRADING RÉEL"

    categories: dict[str, list] = {}
    for symbol, cfg in ASSETS.items():
        categories.setdefault(cfg["category"], []).append(symbol)

    assets_text = "\n".join(
        f"{cat} : {', '.join(syms)}" for cat, syms in categories.items()
    )

    params_text = "\n".join(
        f"{k} → RSI achat &lt;{v['rsi_buy']} | MACD {v['macd_fast']}/{v['macd_slow']}/{v['macd_sig']}"
        for k, v in SIGNAL_PARAMS.items()
    )

    send_telegram(
        f"🤖 <b>TradingBot démarré</b> — {mode}\n\n"
        f"{assets_text}\n\n"
        f"<b>Paramètres par catégorie :</b>\n{params_text}\n\n"
        f"Filtre tendance : EMA50 journalière | Volume : EMA20×1.2–1.4\n"
        f"Max positions : {MAX_OPEN_POSITIONS} | Stop-loss : Matières 3–5% | Forex 2% | Crypto 7%\n\n"
        f"💼 Portfolio : <b>{account.get('portfolio_value', 0):.2f} €</b>\n"
        f"💵 Disponible : <b>{account.get('buying_power', 0):.2f} €</b>"
    )

    scheduler = BlockingScheduler(timezone="America/New_York")

    scheduler.add_job(
        run_strategy, "cron",
        day_of_week="mon-fri", hour="9-15", minute="5,25,45",
        id="run_strategy", name="Analyse signaux",
    )
    scheduler.add_job(
        daily_summary, "cron",
        day_of_week="mon-fri", hour=16, minute=5,
        id="daily_summary", name="Résumé journalier",
    )

    logger.info("Scheduler actif — 20 min / 9h30–16h NY / lun–ven")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("TradingBot arrêté proprement")
        send_telegram("🛑 <b>TradingBot arrêté</b>")


if __name__ == "__main__":
    main()
