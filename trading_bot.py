#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TradingBot COMPLET — Trading 212 Invest
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Actifs    : Matières premières + Actions (ETFs UCITS) + Crypto ETPs (10 actifs)
Stratégie : EMA 20/50 + RSI 14 + MACD adapté par catégorie, bougies 15min CLÔTURÉES
Risque    : ATR sizing, trailing stop, circuit breaker -5%/jour, corrélations inter-actifs
Broker    : Trading 212 Invest (fractions, 0 commission)
"""

# ============================================================
# SECTION 1 — IMPORTS & CONFIG
# ============================================================

import base64
import json
import logging
import os
import time
import yaml
from concurrent.futures import ThreadPoolExecutor, as_completed
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
yf.set_tz_cache_location("/tmp/yfinance")

# ---- Clés API ----
T212_API_KEY_ID: str = os.getenv("T212_API_KEY_ID", "")
T212_API_KEY: str = os.getenv("T212_API_KEY", "")
T212_DEMO: bool = os.getenv("T212_DEMO", "true").lower() == "true"
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID: str = os.getenv("CHAT_ID", "")
DEBUG: bool      = os.getenv("DEBUG", "false").lower() == "true"
TEST_TRADE: bool = os.getenv("TEST_TRADE", "false").lower() == "true"

T212_BASE = "https://demo.trading212.com" if T212_DEMO else "https://live.trading212.com"

# ============================================================
# PARAMÈTRES DE SIGNAL PAR CATÉGORIE
# ============================================================
#
# Crypto 8/17/9 : forte volatilité, MACD rapide capture les accélérations intraday
# Vente sell_min=2 : requiert 2/3 critères baissiers — évite les sorties prématurées

SIGNAL_PARAMS: dict[str, dict] = {
    "commodities": {
        "rsi_buy": 48, "rsi_sell": 65,
        "macd_fast": 12, "macd_slow": 26, "macd_sig": 9,
        "vol_mult": 1.3, "sell_min": 2,
    },
    "equity": {
        "rsi_buy": 50, "rsi_sell": 68,
        "macd_fast": 12, "macd_slow": 26, "macd_sig": 9,
        "vol_mult": 1.2, "sell_min": 2,
    },
    "crypto": {
        "rsi_buy": 55, "rsi_sell": 72,
        "macd_fast": 8, "macd_slow": 17, "macd_sig": 9,
        "vol_mult": 1.4, "sell_min": 2,
    },
}

# ============================================================
# PARAMÈTRES DE SIZING PAR CATÉGORIE (ATR-based)
# ============================================================
#
# Formule : position_value = (portfolio × risk_pct) / (ATR_14 × atr_mult) × prix
# Cap absolu = min(résultat, portfolio × max_pct)
#
# Interprétation : si le prix bouge de (ATR × atr_mult) contre nous,
# la perte est exactement risk_pct × portfolio.
#
# Matières premières/équité risk_pct modéré (0.8-1%)
# Crypto risk_pct plus élevé (1.5%) pour capter les amplitudes

SIZING_PARAMS: dict[str, dict] = {
    "commodities": {"risk_pct": 0.010, "atr_period": 14, "atr_mult": 2.0, "max_pct": 0.15},
    "equity":      {"risk_pct": 0.008, "atr_period": 14, "atr_mult": 2.0, "max_pct": 0.15},
    "crypto":      {"risk_pct": 0.015, "atr_period": 14, "atr_mult": 2.5, "max_pct": 0.08},
}

# ============================================================
# CONTRAINTES DE CORRÉLATION INTER-ACTIFS
# ============================================================
#
# Paires mutuellement exclusives : impossible de détenir les deux simultanément
# PHAU+PHAG : or+argent, corrélation historique > 0.80
# EQQQ+XNAS : même sous-jacent Nasdaq-100, doublon de risque
# BTCE+ETHE : crypto BTC+ETH, corrélation élevée

CORR_EXCLUSIONS: list[frozenset] = [
    frozenset({"PHAU", "PHAG"}),
    frozenset({"EQQQ", "XNAS"}),
    frozenset({"BTCE", "ETHE"}),
]
MAX_PER_CATEGORY = 2   # max positions simultanées par catégorie

# ============================================================
# ACTIFS
# ============================================================
# Note : _T212_TO_SYMBOL est construit APRÈS la définition de ASSETS (voir fin de cette section)

ASSETS: dict[str, dict] = {
    # Matières premières — ETCs physiques UCITS (LSE, prix en GBP)
    "PHAU": {"t212": "PHAUl_EQ", "yf": "PHAU.L", "ccy": "£", "category": "🪙 Matières premières", "stop": 0.03, "pct": 0.15, "params": "commodities"},
    "PHAG": {"t212": "PHAGl_EQ", "yf": "PHAG.L", "ccy": "£", "category": "🪙 Matières premières", "stop": 0.03, "pct": 0.15, "params": "commodities"},
    "CRUD": {"t212": "CRUDl_EQ", "yf": "CRUD.L", "ccy": "£", "category": "🪙 Matières premières", "stop": 0.04, "pct": 0.12, "params": "commodities"},
    "NGAS": {"t212": "NGASl_EQ", "yf": "NGAS.L", "ccy": "£", "category": "🪙 Matières premières", "stop": 0.05, "pct": 0.10, "params": "commodities"},
    # Actions — ETFs indiciels UCITS (LSE, prix en USD ou GBP selon ETF)
    "IUSA": {"t212": "IUSAl_EQ", "yf": "IUSA.L", "ccy": "$", "category": "📈 Actions",            "stop": 0.03, "pct": 0.15, "params": "equity"},
    "EQQQ": {"t212": "EQQQl_EQ", "yf": "EQQQ.L", "ccy": "$", "category": "📈 Actions",            "stop": 0.04, "pct": 0.12, "params": "equity"},
    "IWDA": {"t212": "IWDAl_EQ", "yf": "IWDA.L", "ccy": "$", "category": "📈 Actions",            "stop": 0.03, "pct": 0.12, "params": "equity"},
    "XNAS": {"t212": "XNASl_EQ", "yf": "XNAS.L", "ccy": "$", "category": "📈 Actions",            "stop": 0.04, "pct": 0.10, "params": "equity"},
    # Crypto — ETPs physiques UCITS (Xetra EUR / LSE USD)
    "BTCE": {"t212": "BTCEd_EQ", "yf": "BTCE.DE", "ccy": "€", "category": "₿ Crypto",             "stop": 0.07, "pct": 0.08, "params": "crypto"},
    "ETHE": {"t212": "ZETHd_EQ", "yf": "ZETH.DE",  "ccy": "€", "category": "₿ Crypto",             "stop": 0.08, "pct": 0.08, "params": "crypto"},
}

# Table inverse T212 ticker → symbole yfinance. Évite le parsing fragile par str.replace().
_T212_TO_SYMBOL: dict[str, str] = {cfg["t212"]: sym for sym, cfg in ASSETS.items()}

# ---- Paramètres techniques fixes ----
EMA_SHORT  = 20
EMA_LONG   = 50
RSI_PERIOD = 14
DATA_INTERVAL = "15m"
DATA_PERIOD   = "5d"
DAILY_PERIOD  = "3mo"   # ~65 jours ouvrés > 50 requis pour EMA50 daily ("60d" ne donne que ~43)

# ---- Gestion du risque ----
DAILY_LOSS_LIMIT_PCT    = float(os.getenv("DAILY_LOSS_LIMIT_PCT", "0.05"))
MIN_ORDER_EUR           = 1.0
MAX_OPEN_POSITIONS      = 3
TRAILING_ACTIVATION_PCT = 0.02   # le trailing stop s'active à +2% de gain non réalisé

# ---- Risk report ----
VAR_CONFIDENCE   = 0.95   # VaR 95%
VAR_HISTORY_DAYS = 20     # jours d'historique pour la VaR
CORR_ALERT_THRESHOLD = 0.70   # alerte si corrélation moy. portfolio > ce seuil

# ---- Constantes T212 ----
T212_RATE_SLEEP: float = 0.4
T212_TIMEOUT: int = 15
T212_RETRY_DELAYS: tuple = (2, 4)

_API_ERROR = object()

# ---- Fichiers de stockage ----
TRADES_FILE          = Path("trades.json")
DAILY_PNL_FILE       = Path("daily_pnl.json")
DISABLED_ASSETS_FILE = Path("disabled_assets.json")
POSITIONS_META_FILE  = Path("positions_meta.json")   # trailing stop tracking
RISK_STATS_FILE      = Path("risk_stats.json")        # win streak & historical risk
BOT_CONTROL_FILE     = Path("bot_control.json")       # pause/resume + first live order
DECISIONS_LOG_FILE   = Path("decisions.log")          # audit trail des décisions
CONFIG_FILE          = Path("config.yaml")            # paramètres mode live

# Circuit breaker progressif (LIVE uniquement — DEMO utilise DAILY_LOSS_LIMIT_PCT)
CB_PHASE1_PCT             = 0.03   # semaines 1-2 live : -3% par jour
CB_NORMAL_PCT             = 0.05   # après 3 semaines profitables : -5% par jour
PROFITABLE_WEEKS_TO_RELAX = 3      # semaines consécutives rentables pour assouplir
CUMULATIVE_DRAWDOWN_LIMIT = 0.15   # drawdown cumulé > -15% → arrêt complet
LIVE_PHASE1_DAYS          = 14     # durée de la phase stricte (jours calendaires)

# ---- Logging ----
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.DEBUG if DEBUG else logging.INFO,
)
logger = logging.getLogger("TradingBot")

if not T212_API_KEY or not T212_API_KEY_ID:
    raise ValueError("T212_API_KEY et T212_API_KEY_ID requis dans le .env")

# Basic auth : base64(KEY_ID:SECRET) — format officiel T212 API v0
_T212_AUTH = "Basic " + base64.b64encode(
    f"{T212_API_KEY_ID}:{T212_API_KEY}".encode()
).decode()
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
    """Écriture atomique via fichier temporaire — évite la corruption si GitHub Actions coupe."""
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
    data  = load_json(DAILY_PNL_FILE, {})
    today = str(datetime.now(ZoneInfo("Europe/London")).date())
    if data.get("date") != today:
        data = {"date": today, "start_value": None, "trades": 0, "pnl": 0.0, "opening_sent": False, "summary_sent": False}
        save_json(DAILY_PNL_FILE, data)
    return data


def update_daily_pnl(pnl_delta: float) -> None:
    data = load_daily_pnl()
    data["pnl"]    = round(data.get("pnl", 0.0) + pnl_delta, 2)
    data["trades"] = data.get("trades", 0) + 1
    save_json(DAILY_PNL_FILE, data)


# ---- Trailing stop metadata ----

def load_positions_meta() -> dict:
    return load_json(POSITIONS_META_FILE, {})


def save_positions_meta(meta: dict) -> None:
    save_json(POSITIONS_META_FILE, meta)


def init_position_meta(symbol: str, entry_price: float) -> None:
    """Initialise le suivi d'une position à l'ouverture."""
    meta = load_positions_meta()
    meta[symbol] = {
        "entry_price": entry_price,
        "max_price":   entry_price,
        "opened_at":   datetime.now(ZoneInfo("Europe/London")).isoformat(),
    }
    save_positions_meta(meta)


def update_max_price(symbol: str, current_price: float) -> float:
    """Met à jour max_price si le prix courant est plus élevé. Retourne max_price."""
    meta  = load_positions_meta()
    entry = meta.get(symbol, {})
    prev_max = entry.get("max_price", current_price)
    if current_price > prev_max:
        entry["max_price"] = current_price
        meta[symbol] = entry
        save_positions_meta(meta)
        return current_price
    return prev_max


def clear_position_meta(symbol: str) -> None:
    meta = load_positions_meta()
    if symbol in meta:
        meta.pop(symbol)
        save_positions_meta(meta)


# ---- Risk stats (win streak) ----

def load_risk_stats() -> dict:
    return load_json(RISK_STATS_FILE, {"win_streak": 0, "last_date": None})


def record_day_result(pnl: float) -> int:
    """
    Appelé en fin de journée (daily_summary) avec le P&L du jour.
    Met à jour le streak journalier et le suivi hebdomadaire (pour le CB progressif).
    """
    stats      = load_risk_stats()
    now_ldn    = datetime.now(ZoneInfo("Europe/London"))
    today      = str(now_ldn.date())

    if stats.get("last_date") != today:
        stats["win_streak"]       = stats.get("win_streak", 0) + 1 if pnl >= 0 else 0
        stats["last_date"]        = today
        stats["current_week_pnl"] = stats.get("current_week_pnl", 0.0) + pnl

        # Vendredi = clôture de semaine
        if now_ldn.weekday() == 4:
            history  = stats.get("weekly_results", [])
            week_pnl = stats["current_week_pnl"]
            history.append({"date": today, "pnl": round(week_pnl, 2)})
            stats["weekly_results"]   = history[-12:]  # 12 semaines d'historique
            stats["current_week_pnl"] = 0.0

            # Semaines consécutives rentables (pour assouplir le CB)
            streak_w = 0
            for w in reversed(stats["weekly_results"]):
                if w["pnl"] >= 0:
                    streak_w += 1
                else:
                    break
            stats["profitable_weeks"] = streak_w

        save_json(RISK_STATS_FILE, stats)

    return stats.get("win_streak", 0)


# ---- Divers ----

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
    """Envoi avec 2 retries (2s, 4s)."""
    if not TELEGRAM_BOT_TOKEN or not CHAT_ID:
        return
    if len(text) > 4096:
        cut = text[:4080].rfind('\n')
        text = (text[:cut] if cut > 0 else text[:4080]) + "\n<i>[tronqué]</i>"
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
            if resp.status_code == 403:
                logger.error("Telegram HTTP 403 Forbidden — vérifiez TELEGRAM_BOT_TOKEN")
                return
            logger.warning("Telegram tentative %d/3 : %s", attempt + 1, resp.text[:100])
        except Exception as e:
            logger.error("Telegram tentative %d/3 : %s", attempt + 1, e)


def _send_error_alert(msg: str) -> None:
    """Alerte Telegram pour erreurs critiques, throttlée à 1 message par 30 min.
    Évite le spam en cas de panne prolongée (GitHub Actions s'exécute toutes les 10 min).
    """
    ctrl = load_bot_control()
    last = ctrl.get("last_error_alert_ts", 0)
    if time.time() - last < 1800:
        return
    send_telegram(f"⚠️ <b>Erreur bot</b>\n{msg}")
    ctrl["last_error_alert_ts"] = time.time()
    save_bot_control(ctrl)


# ============================================================
# SECTION 2.5 — CONFIG, CONTRÔLE & AUDIT
# ============================================================

_config_cache: dict = {}
_config_loaded: bool = False


def load_config() -> dict:
    """Charge config.yaml une seule fois par run (cache module-level)."""
    global _config_cache, _config_loaded
    if _config_loaded:
        return _config_cache
    _config_loaded = True
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                _config_cache = yaml.safe_load(f) or {}
        except Exception as e:
            logger.warning("load_config : %s", e)
    return _config_cache


def load_bot_control() -> dict:
    return load_json(BOT_CONTROL_FILE, {
        "paused": False,
        "paused_at": None,
        "first_live_order_done": False,
        "calibration_live_start": None,
        "last_telegram_update_id": 0,
        "last_error_alert_ts": 0,
    })


def save_bot_control(ctrl: dict) -> None:
    save_json(BOT_CONTROL_FILE, ctrl)


def log_decision(symbol: str, action: str, reason: str, signals: Optional[dict] = None) -> None:
    """Écrit chaque décision dans decisions.log. Cap à 5 000 lignes si le fichier dépasse 1 Mo."""
    now = datetime.now(ZoneInfo("Europe/London")).isoformat()
    sig_str = ""
    if signals:
        sig_str = (
            f" | RSI={signals.get('rsi', 0):.1f}"
            f" EMA={'↗' if signals.get('trend_bullish') else '↘'}"
            f" MACD={'↗' if signals.get('macd_bull_cross') else ('↘' if signals.get('macd_bear_cross') else '-')}"
            f" score={signals.get('confluence_score', 0)}/5"
            f" daily={signals.get('daily_trend', '?')}"
        )
    line = f"{now} | {symbol:<6} | {action:<24} | {reason}{sig_str}\n"
    try:
        if DECISIONS_LOG_FILE.exists() and DECISIONS_LOG_FILE.stat().st_size > 1_000_000:
            with open(DECISIONS_LOG_FILE, "r", encoding="utf-8") as f:
                lines = f.readlines()
            with open(DECISIONS_LOG_FILE, "w", encoding="utf-8") as f:
                f.writelines(lines[-5000:])
        with open(DECISIONS_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception as e:
        logger.warning("log_decision : %s", e)


def poll_telegram_commands() -> None:
    """
    Appelé au début de chaque run GitHub Actions.
    Récupère les commandes /pause et /resume via getUpdates (offset pour éviter les doublons).
    Seul le CHAT_ID autorisé est traité — les autres messages sont ignorés silencieusement.
    """
    ctrl = load_bot_control()
    offset = ctrl.get("last_telegram_update_id", 0)
    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates",
            params={"offset": offset + 1, "timeout": 3, "limit": 20},
            timeout=8,
        )
        if not resp.ok:
            return
        changed = False
        for update in resp.json().get("result", []):
            update_id = update.get("update_id", 0)
            msg       = update.get("message", {})
            chat_id   = str(msg.get("chat", {}).get("id", ""))
            text      = msg.get("text", "").strip()
            msg_date  = msg.get("date", 0)  # Unix timestamp

            ctrl["last_telegram_update_id"] = max(ctrl.get("last_telegram_update_id", 0), update_id)
            changed = True

            # Ignorer les messages plus vieux de 10 min (évite de rejouer les commandes historiques)
            if time.time() - msg_date > 600:
                continue

            if chat_id != str(CHAT_ID):
                continue

            cmd = text.split()[0].lower() if text else ""
            if cmd == "/pause":
                ctrl["paused"] = True
                ctrl["paused_at"] = datetime.now(ZoneInfo("Europe/London")).isoformat()
                send_telegram(
                    "⏸️ <b>Bot en pause</b>\n"
                    "Stratégie suspendue — positions existantes conservées.\n"
                    "Envoyez <code>/resume</code> pour reprendre."
                )
                logger.info("Bot mis en pause via Telegram")
            elif cmd == "/resume":
                ctrl["paused"] = False
                ctrl["paused_at"] = None
                send_telegram("▶️ <b>Bot repris</b> — prochaine analyse dans ≤ 5 min.")
                logger.info("Bot repris via Telegram")
            elif cmd == "/status":
                mode = "🧪 DEMO" if T212_DEMO else "💰 RÉEL"
                account   = get_account()
                positions = get_all_positions() or []   # None (T212 down) → [] pour len()
                daily     = load_daily_pnl()
                bal       = f"{account['portfolio_value']:.2f}€" if account else "N/A"
                pnl       = daily.get("pnl", 0.0)
                paused_str = "⏸️ En pause" if ctrl.get("paused") else "▶️ Actif"
                send_telegram(
                    f"📊 <b>Status Bot {mode}</b>\n"
                    f"État : {paused_str}\n"
                    f"Portefeuille : {bal}\n"
                    f"P&L jour : {pnl:+.2f}€\n"
                    f"Positions : {len(positions)}/{len(ASSETS)}"
                )

        if changed:
            save_bot_control(ctrl)
    except Exception as e:
        logger.warning("poll_telegram_commands : %s", e)


def _calibration_factor() -> float:
    """
    Retourne 0.5 pendant les CALIBRATION_DAYS premiers jours en mode LIVE.
    Permet une phase de calibration prudente au démarrage du live.
    En DEMO : toujours 1.0.
    """
    if T212_DEMO:
        return 1.0
    ctrl = load_bot_control()
    cal_start = ctrl.get("calibration_live_start")
    if not cal_start:
        logger.info("Calibration live : aucun trade réel passé → sizing 50%")
        return 0.5
    try:
        start    = datetime.fromisoformat(cal_start).date()
        cal_days = load_config().get("trading", {}).get("calibration_days", 30)
        days     = (datetime.now(ZoneInfo("Europe/London")).date() - start).days
        factor   = 0.5 if days < cal_days else 1.0
        logger.info("Calibration live : J+%d/%d → sizing %.0f%%", days, cal_days, factor * 100)
        return factor
    except Exception as e:
        logger.warning("Calibration live : erreur de calcul (%s) → sizing 50% par précaution", e)
        return 0.5


def _get_circuit_breaker_pct() -> float:
    """
    Seuil de circuit breaker journalier selon la phase live :
    - Phase 1 (LIVE_PHASE1_DAYS premiers jours) : CB_PHASE1_PCT (-3%)
    - Après PROFITABLE_WEEKS_TO_RELAX semaines consécutives rentables : CB_NORMAL_PCT (-5%)
    - Sinon : CB_PHASE1_PCT (-3%) par défaut
    """
    stats      = load_risk_stats()
    live_start = stats.get("live_start_date")
    if not live_start:
        return CB_PHASE1_PCT

    try:
        start     = datetime.fromisoformat(live_start).date()
        days_live = (datetime.now(ZoneInfo("Europe/London")).date() - start).days
    except Exception:
        return CB_PHASE1_PCT

    if days_live < LIVE_PHASE1_DAYS:
        return CB_PHASE1_PCT

    cfg_weeks        = load_config().get("risk", {}).get("profitable_weeks_to_relax", PROFITABLE_WEEKS_TO_RELAX)
    profitable_weeks = stats.get("profitable_weeks", 0)
    pct = CB_NORMAL_PCT if profitable_weeks >= cfg_weeks else CB_PHASE1_PCT
    logger.info("Circuit breaker actif : -%.0f%% (semaines rentables : %d/%d)",
                pct * 100, profitable_weeks, cfg_weeks)
    return pct


# ============================================================
# SECTION 3 — SERVICE TRADING 212
# ============================================================

def _t212_request(method: str, url: str, body: Optional[dict] = None) -> object:
    """Retry automatique (2s, 4s). Retourne : json | None (404) | _API_ERROR."""
    headers = {"Authorization": _T212_AUTH}
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
                logger.warning("T212 rate limit — retry %d", attempt + 1)
                last_exc = "rate_limit"
                continue
            if not resp.ok:
                logger.warning("T212 %s %s → %d : %s", method, url, resp.status_code, resp.text[:500])
            resp.raise_for_status()
            time.sleep(T212_RATE_SLEEP)
            return resp.json()
        except Exception as e:
            logger.warning("T212 tentative %d/%d : %s", attempt + 1, len(T212_RETRY_DELAYS) + 1, e)
            last_exc = e
    logger.error("T212 %s échec : %s", url, last_exc)
    return _API_ERROR


def t212_get(endpoint: str) -> object:
    return _t212_request("GET", f"{T212_BASE}{endpoint}")


def t212_post(endpoint: str, body: dict) -> object:
    return _t212_request("POST", f"{T212_BASE}{endpoint}", body=body)




def get_account() -> dict:
    data = t212_get("/api/v0/equity/account/cash")
    if data is _API_ERROR or not data or "total" not in data:
        return {}
    return {
        "portfolio_value": float(data.get("total", 0)),
        "buying_power":    float(data.get("free", 0)),
        "cash":            float(data.get("cash", 0)),
        "invested":        float(data.get("invested", 0)),
        "ppl":             float(data.get("ppl", 0)),
    }


def get_position(symbol: str) -> Optional[dict]:
    """Lève RuntimeError si T212 est inaccessible (distingué de "pas de position")."""
    data = t212_get(f"/api/v0/equity/portfolio/{ASSETS[symbol]['t212']}")
    if data is _API_ERROR:
        raise RuntimeError(f"T212 inaccessible pour {symbol}")
    if not data or float(data.get("quantity", 0)) == 0:
        return None
    avg_price     = float(data.get("averagePrice", 0))
    current_price = float(data.get("currentPrice", avg_price))
    qty           = float(data.get("quantity", 0))
    plpc = ((current_price - avg_price) / avg_price) if avg_price else 0
    return {
        "symbol":          symbol,
        "qty":             qty,
        "avg_entry_price": avg_price,
        "current_price":   current_price,
        "unrealized_pl":   float(data.get("ppl", 0)),
        "unrealized_plpc": plpc,
        "market_value":    qty * current_price,
    }


def get_all_positions() -> Optional[list[dict]]:
    """Un seul appel T212 pour toutes les positions.
    Retourne None si T212 est inaccessible (distingué d'un portfolio vide → []).
    """
    data = t212_get("/api/v0/equity/portfolio")
    if data is _API_ERROR:
        return None  # erreur réseau — ne pas confondre avec portfolio vide
    if not data:
        return []
    result = []
    for p in data:
        t212_ticker = p.get("ticker", "")
        symbol = _T212_TO_SYMBOL.get(t212_ticker, "")
        if not symbol:
            continue  # actif inconnu de notre univers, ignorer
        avg_price     = float(p.get("averagePrice", 0))
        current_price = float(p.get("currentPrice", avg_price))
        qty           = float(p.get("quantity", 0))
        plpc = ((current_price - avg_price) / avg_price) if avg_price else 0
        result.append({
            "symbol":          symbol,
            "qty":             qty,
            "avg_entry_price": avg_price,
            "current_price":   current_price,
            "unrealized_pl":   float(p.get("ppl", 0)),
            "unrealized_plpc": plpc,
            "market_value":    qty * current_price,
        })
    return result


def place_buy_order(symbol: str, amount_eur: float, current_price: float) -> bool:
    if amount_eur < MIN_ORDER_EUR:
        logger.warning("%s montant trop faible : %.2f€", symbol, amount_eur)
        return False
    ccy = ASSETS[symbol]["ccy"]
    fx_to_eur = _get_fx_to_eur(ccy)             # 1 ccy = fx_to_eur EUR
    amount_ccy = amount_eur / fx_to_eur          # EUR → devise native de l'actif
    quantity = round(amount_ccy / current_price, 4)
    if not (quantity > 0):  # attrape aussi NaN
        logger.error("%s quantité calculée invalide (%.2f%s / %.4f) — ordre annulé",
                     symbol, amount_ccy, ccy, current_price)
        return False
    t212_ticker = ASSETS[symbol]["t212"]
    logger.info("ACHAT %s : body → ticker=%s quantity=%s (%.2f€ → %.2f%s / %.4f)",
                symbol, t212_ticker, quantity, amount_eur, amount_ccy, ccy, current_price)
    result   = t212_post("/api/v0/equity/orders/market", {
        "ticker": t212_ticker, "quantity": quantity,
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
    qty = round(position["qty"], 4)
    if not (qty > 0):
        logger.warning("close_position %s : qty nulle ou invalide (%.6f) — rien à vendre",
                       symbol, position["qty"])
        return True
    result = t212_post("/api/v0/equity/orders/market", {
        "ticker": ASSETS[symbol]["t212"], "quantity": -qty,
    })
    if result is _API_ERROR:
        logger.error("VENTE %s échouée : T212 inaccessible", symbol)
        return False
    if result:
        logger.info("VENTE %s : %.6f actions", symbol, position["qty"])
        return True
    return False


# Jours fériés UK (LSE fermée) 2025-2028 — à mettre à jour annuellement
_UK_BANK_HOLIDAYS: frozenset = frozenset({
    date(2025, 1, 1), date(2025, 4, 18), date(2025, 4, 21),
    date(2025, 5, 5), date(2025, 5, 26), date(2025, 8, 25),
    date(2025, 12, 25), date(2025, 12, 26),
    date(2026, 1, 1), date(2026, 4, 3), date(2026, 4, 6),
    date(2026, 5, 4), date(2026, 5, 25), date(2026, 8, 31),
    date(2026, 12, 25), date(2026, 12, 28),
    date(2027, 1, 1), date(2027, 3, 26), date(2027, 3, 29),
    date(2027, 5, 3), date(2027, 5, 31), date(2027, 8, 30),
    date(2027, 12, 27), date(2027, 12, 28),
    date(2028, 1, 3), date(2028, 4, 14), date(2028, 4, 17),
    date(2028, 5, 1), date(2028, 5, 29), date(2028, 8, 26),
    date(2028, 12, 25), date(2028, 12, 26),
})
_HOLIDAY_COVERAGE_END = date(2028, 12, 26)


def is_market_open() -> bool:
    now = datetime.now(ZoneInfo("Europe/London"))
    if now.weekday() >= 5:
        return False
    today = now.date()
    if (today - _HOLIDAY_COVERAGE_END).days > -60:
        logger.warning("_UK_BANK_HOLIDAYS expire bientôt — mettre à jour la liste pour %d+", today.year)
    if today in _UK_BANK_HOLIDAYS:
        logger.info("Jour férié UK — LSE fermée")
        return False
    return (
        now.replace(hour=8, minute=0, second=0, microsecond=0)
        <= now <=
        now.replace(hour=16, minute=30, second=0, microsecond=0)
    )


_FX_CACHE: dict[str, float] = {}

def _get_fx_to_eur(ccy: str) -> float:
    """1 unité de ccy = ? EUR. Mis en cache pour la durée du run."""
    if ccy == "€":
        return 1.0
    if ccy in _FX_CACHE:
        return _FX_CACHE[ccy]
    pairs = {"£": "GBPEUR=X", "$": "USDEUR=X"}
    sym = pairs.get(ccy)
    if not sym:
        _FX_CACHE[ccy] = 1.0
        return 1.0
    try:
        df = yf.Ticker(sym).history(period="2d", interval="1d", auto_adjust=True)
        rate = float(df["Close"].iloc[-1]) if not df.empty else 1.0
    except Exception:
        rate = 1.0
    if not (rate > 0):  # protège contre NaN / 0 (division par zéro dans place_buy_order)
        rate = 1.0
    _FX_CACHE[ccy] = rate
    logger.info("FX %s/EUR : %.4f", ccy, rate)
    return rate


# ============================================================
# SECTION 4 — ANALYSE TECHNIQUE
# ============================================================

def fetch_ohlcv(symbol: str) -> Optional[pd.DataFrame]:
    try:
        yf_sym = ASSETS[symbol]["yf"]
        ticker = yf.Ticker(yf_sym)
        df = ticker.history(period=DATA_PERIOD, interval=DATA_INTERVAL, auto_adjust=True)
        if df.empty or len(df) < EMA_LONG + 1:
            logger.warning("%s : données insuffisantes (%d barres)", symbol, len(df))
            return None
        return df
    except Exception as e:
        logger.error("fetch_ohlcv %s : %s", symbol, e)
        return None


def _fetch_daily_trend(symbol: str) -> str:
    """EMA50 journalière — filtre de tendance de fond. Retourne 'bullish'/'bearish'/'unknown'."""
    try:
        yf_sym = ASSETS[symbol]["yf"]
        ticker = yf.Ticker(yf_sym)
        df = ticker.history(period=DAILY_PERIOD, interval="1d", auto_adjust=True)
        if df is None or df.empty or len(df) < 50:
            return "unknown"
        close = df["Close"].squeeze()
        return "bullish" if float(close.iloc[-1]) > float(
            close.ewm(span=50, adjust=False).mean().iloc[-1]
        ) else "bearish"
    except Exception as e:
        logger.warning("_fetch_daily_trend %s : %s", symbol, e)
        return "unknown"


def _volume_ok(df: pd.DataFrame, vol_mult: float) -> bool:
    """
    Volume dernière bougie clôturée > vol_mult × EMA20(volume).
    Retourne False si données absentes (ne pas masquer le manque de données en signal positif).
    """
    try:
        volume = df["Volume"].squeeze().iloc[:-1]
        if volume.empty or volume.sum() == 0:
            return False  # données volume absentes → filtre échoue
        vol_ema = float(volume.ewm(span=20, adjust=False).mean().iloc[-1])
        if vol_ema == 0:
            return False
        return float(volume.iloc[-1]) >= vol_ema * vol_mult
    except Exception:
        return False  # erreur → filtre échoue prudemment


def calc_ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def calc_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain  = delta.clip(lower=0).ewm(alpha=1 / period, min_periods=period).mean()
    loss  = (-delta.clip(upper=0)).ewm(alpha=1 / period, min_periods=period).mean()
    rs    = gain / loss.replace(0, np.nan)
    rsi   = 100 - (100 / (1 + rs))
    if rsi.isna().any():
        logger.debug("RSI NaN (série plate ou hors-marché) — remplacé par 50")
        rsi = rsi.fillna(50.0)
    return rsi


def calc_macd(series: pd.Series, fast: int, slow: int, sig: int) -> tuple[pd.Series, pd.Series]:
    line = calc_ema(series, fast) - calc_ema(series, slow)
    return line, calc_ema(line, sig)


def calc_atr(df: pd.DataFrame, period: int = 14) -> float:
    """
    ATR (Average True Range) sur les bougies clôturées.
    Utilisé pour le sizing dynamique : stop_distance = ATR × atr_mult.
    """
    high  = df["High"].squeeze().iloc[:-1]
    low   = df["Low"].squeeze().iloc[:-1]
    close = df["Close"].squeeze().iloc[:-1]
    tr = pd.concat([
        (high - low),
        (high - close.shift(1)).abs(),
        (low  - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(span=period, adjust=False).mean()
    raw = float(atr.iloc[-1]) if not atr.empty else 0.0
    return raw if np.isfinite(raw) and raw > 0 else 0.0


def get_signals(symbol: str, params_key: str = "commodities") -> Optional[dict]:
    """
    Calcule tous les indicateurs pour un actif. Inclut ATR pour le sizing,
    tendance journalière (filtre de fond), volume et score de confluence 0-5.

    Bougie en cours exclue (iloc[:-1]) pour éviter les faux signaux mid-bar.
    """
    params = SIGNAL_PARAMS[params_key]
    sizing = SIZING_PARAMS[params_key]

    df          = fetch_ohlcv(symbol)
    daily_trend = _fetch_daily_trend(symbol)

    if df is None:
        return None

    close = df["Close"].squeeze().iloc[:-1]

    ema20      = calc_ema(close, EMA_SHORT)
    ema50      = calc_ema(close, EMA_LONG)
    rsi        = calc_rsi(close, RSI_PERIOD)
    macd_line, signal_line = calc_macd(
        close, params["macd_fast"], params["macd_slow"], params["macd_sig"]
    )
    atr_val    = calc_atr(df, sizing["atr_period"])

    if len(macd_line.dropna()) < 2 or len(signal_line.dropna()) < 2:
        logger.warning("%s : MACD insuffisant (données < 2 bougies valides)", symbol)
        return None

    price       = float(close.iloc[-1])
    ema20_now   = float(ema20.iloc[-1])
    ema50_now   = float(ema50.iloc[-1])
    rsi_now     = float(rsi.iloc[-1])
    macd_now    = float(macd_line.iloc[-1])
    signal_now  = float(signal_line.iloc[-1])
    macd_prev   = float(macd_line.iloc[-2])
    signal_prev = float(signal_line.iloc[-2])

    if not all(np.isfinite(v) for v in [price, ema20_now, ema50_now, rsi_now,
                                         macd_now, signal_now, macd_prev, signal_prev]):
        logger.warning("%s : indicateur(s) NaN/Inf — données insuffisantes ou corrompues", symbol)
        return None

    trend_bullish   = ema20_now > ema50_now
    trend_bearish   = ema20_now < ema50_now
    macd_bull_cross = (macd_prev < signal_prev) and (macd_now > signal_now)
    macd_bear_cross = (macd_prev > signal_prev) and (macd_now < signal_now)
    vol_ok          = _volume_ok(df, params["vol_mult"])

    macd_bullish = macd_now > signal_now   # momentum positif (inclut et survit au crossover)

    confluence_score = (
        int(trend_bullish)
        + int(macd_bullish)
        + int(rsi_now < params["rsi_buy"])
        + int(vol_ok)
        + int(daily_trend == "bullish")
    )

    return {
        "symbol":          symbol,
        "price":           price,
        "ema20":           ema20_now,
        "ema50":           ema50_now,
        "rsi":             rsi_now,
        "rsi_buy":         params["rsi_buy"],
        "rsi_sell":        params["rsi_sell"],
        "sell_min":        params["sell_min"],
        "macd":            macd_now,
        "macd_signal":     signal_now,
        "macd_bull_cross": macd_bull_cross,
        "macd_bullish":    macd_bullish,
        "macd_bear_cross": macd_bear_cross,
        "trend_bullish":   trend_bullish,
        "trend_bearish":   trend_bearish,
        "volume_ok":       vol_ok,
        "daily_trend":     daily_trend,
        "atr":             atr_val,
        "confluence_score": confluence_score,
    }


def _prefetch_signals(symbols: list[str]) -> dict[str, Optional[dict]]:
    """Télécharge 15min + daily pour tous les actifs en parallèle (5 threads)."""
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
# SECTION 5 — SIZING ATR + TRAILING STOP + CORRÉLATION
# ============================================================

def calculate_amount_atr(
    symbol: str,
    buying_power: float,
    portfolio_value: float,
    atr: float,
    current_price: float,
    cal: float = 1.0,
) -> float:
    """
    Sizing dynamique basé sur l'ATR.

    Formule : (portfolio × risk_pct) / (ATR × atr_mult) × prix = montant €
    Plafond  : min(résultat, portfolio × max_pct, buying_power)

    Si ATR = 0 ou prix = 0 → fallback sur le % fixe de l'actif.
    `cal` : facteur de calibration (0.5 pendant phase live initiale, 1.0 sinon).
    """
    sizing = SIZING_PARAMS[ASSETS[symbol]["params"]]

    if not (atr > 0) or not (current_price > 0):  # attrape aussi NaN (NaN > 0 == False)
        fallback = round(min(buying_power * ASSETS[symbol]["pct"] * cal, buying_power), 2)
        logger.warning("%s : ATR invalide (%.6f) → fallback sizing fixe %.2f€", symbol, atr, fallback)
        return fallback

    risk_amount    = portfolio_value * sizing["risk_pct"]
    stop_distance  = atr * sizing["atr_mult"]
    shares         = risk_amount / stop_distance
    position_value = shares * current_price * cal

    cap = min(
        portfolio_value * sizing["max_pct"] * cal,
        buying_power,
    )
    return round(min(position_value, cap), 2)


def check_trailing_stop(position: dict, symbol: str) -> bool:
    """
    Trailing stop — actif uniquement si unrealized_plpc >= +TRAILING_ACTIVATION_PCT.
    Prix de déclenchement : max_price_atteint × (1 - stop_loss_initial).
    Avant l'activation du trailing, c'est le stop-loss classique qui protège.
    """
    if position["unrealized_plpc"] < TRAILING_ACTIVATION_PCT:
        return False

    meta      = load_positions_meta()
    max_price = meta.get(symbol, {}).get("max_price", position["current_price"])
    stop_pct  = ASSETS[symbol]["stop"]
    stop_price = max_price * (1 - stop_pct)

    triggered = position["current_price"] <= stop_price
    if triggered:
        logger.info(
            "%s trailing stop : %.4f <= max_price=%.4f × (1-%.0f%%) = %.4f",
            symbol, position["current_price"], max_price, stop_pct * 100, stop_price,
        )
    return triggered


def check_correlation_allowed(symbol: str, positions_map: dict) -> tuple[bool, str]:
    """
    Vérifie que l'ouverture d'une position sur `symbol` respecte les règles de corrélation.
    Retourne (autorisé, raison_si_refus).

    Règles :
    1. Paires mutuellement exclusives (PHAU+PHAG, EQQQ+XNAS, BTCE+ETHE)
    2. Max MAX_PER_CATEGORY positions simultanées par catégorie
    """
    open_symbols    = {sym for sym in ASSETS if sym in positions_map}
    asset_category  = ASSETS[symbol]["category"]

    # Règle 1 : paires exclusives
    for group in CORR_EXCLUSIONS:
        if symbol in group:
            partners = group - {symbol}
            for partner in partners:
                if partner in open_symbols:
                    return False, f"corrélation élevée avec {partner} (déjà en position)"

    # Règle 2 : limite par catégorie
    same_cat = sum(
        1 for sym in open_symbols
        if ASSETS.get(sym, {}).get("category") == asset_category
    )
    if same_cat >= MAX_PER_CATEGORY:
        return False, f"limite {MAX_PER_CATEGORY} positions pour {asset_category}"

    return True, ""


# ============================================================
# SECTION 6 — MÉTRIQUES DE RISQUE (VaR, exposition, corrélation)
# ============================================================

def _fetch_historical_returns(symbols: list[str], days: int = 25) -> dict[str, pd.Series]:
    """
    Télécharge les rendements journaliers en parallèle pour le calcul de VaR
    et de corrélation portfolio. Appel groupé pour minimiser les requêtes réseau.
    """
    results: dict[str, pd.Series] = {}

    def _one(sym: str) -> Optional[pd.Series]:
        try:
            yf_sym = ASSETS[sym]["yf"]
            df = yf.Ticker(yf_sym).history(
                period=f"{days + 5}d", interval="1d", auto_adjust=True)
            if df is None or df.empty or len(df) < days:
                return None
            return df["Close"].squeeze().pct_change().dropna().tail(days)
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(_one, sym): sym for sym in symbols}
        for future in as_completed(futures):
            sym = futures[future]
            try:
                ret = future.result()
                if ret is not None:
                    results[sym] = ret
            except Exception:
                pass
    return results


def calculate_var_95(
    positions: list[dict],
    hist_returns: dict[str, pd.Series],
    portfolio_value: float,
) -> Optional[tuple[float, float]]:
    """
    VaR historique 95% sur VAR_HISTORY_DAYS jours de rendements journaliers.

    Retourne (var_pct, var_eur) ou None si données insuffisantes.

    Méthode : rendements journaliers pondérés par la valeur de marché de chaque position,
    puis 5e percentile de la distribution des rendements portfolio.
    """
    if not positions or not hist_returns:
        return None

    # Filtrer les positions dont on a l'historique
    valid = [p for p in positions if p["symbol"] in hist_returns]
    if not valid:
        return None

    total_value = sum(p.get("market_value", 0) for p in valid)
    if total_value == 0:
        return None

    n_days = VAR_HISTORY_DAYS
    port_returns = np.zeros(n_days)

    for p in valid:
        sym    = p["symbol"]
        weight = p.get("market_value", 0) / total_value
        ret    = hist_returns[sym].values
        k      = min(len(ret), n_days)
        port_returns[-k:] += weight * ret[-k:]

    var_pct = float(np.percentile(port_returns, (1 - VAR_CONFIDENCE) * 100))
    var_eur = var_pct * portfolio_value

    return round(var_pct * 100, 2), round(var_eur, 2)


def calculate_exposure(
    positions: list[dict], portfolio_value: float
) -> dict[str, float]:
    """Exposition par catégorie en % du portfolio total."""
    exposure: dict[str, float] = {}
    for p in positions:
        cat = ASSETS.get(p["symbol"], {}).get("category", "Autre")
        exposure[cat] = exposure.get(cat, 0) + p.get("market_value", 0)
    if portfolio_value > 0:
        return {k: round(v / portfolio_value * 100, 1) for k, v in exposure.items()}
    return {k: 0.0 for k in exposure}


def calculate_portfolio_correlation(
    positions: list[dict],
    hist_returns: dict[str, pd.Series],
) -> Optional[float]:
    """
    Corrélation moyenne par paires des positions ouvertes (sur 20j de rendements daily).
    Retourne None si < 2 positions ou données insuffisantes.
    Alerte dans le rapport si résultat > CORR_ALERT_THRESHOLD.
    """
    symbols = [p["symbol"] for p in positions if p["symbol"] in hist_returns]
    if len(symbols) < 2:
        return None

    ret_df = pd.DataFrame({sym: hist_returns[sym] for sym in symbols}).dropna()
    if ret_df.empty or len(ret_df) < 10:
        return None

    corr_matrix = ret_df.corr().values
    n = len(corr_matrix)
    mask = ~np.eye(n, dtype=bool)
    return round(float(corr_matrix[mask].mean()), 2)


# ============================================================
# SECTION 7 — STRATÉGIE & GESTION DU RISQUE
# ============================================================

def should_buy(signals: dict, has_position: bool, force: bool = False) -> bool:
    """
    Conditions d'achat :
    - REQUIS : EMA20 > EMA50 (15min haussier — pas d'achat en downtrend)
    - REQUIS : RSI <= seuil catégorie (pas suracheté)
    - REQUIS : daily_trend != bearish (gate macro dur)
    - ASSOUPLISSEMENT : MACD > signal OU volume ok OU daily bullish (1/3 suffit)
    Remplace le strict crossover MACD (ponctuel) par un momentum persistant + confirmations alternatives.
    """
    if force:
        return True
    if has_position:
        return False
    if signals["daily_trend"] == "unknown":
        logger.warning(
            "%s : daily_trend=unknown (yfinance HS ou données insuffisantes) — "
            "achat autorisé mais non confirmé sur le timeframe journalier",
            signals["symbol"],
        )
    # Les 3 conditions dures + au moins 1 confirmation supplémentaire
    extra = (
        int(signals["macd_bullish"])
        + int(signals["volume_ok"])
        + int(signals["daily_trend"] == "bullish")
    )
    return (
        signals["trend_bullish"]
        and signals["rsi"] <= signals["rsi_buy"]
        and signals["daily_trend"] != "bearish"
        and extra >= 1
    )


def should_sell(signals: dict, has_position: bool) -> bool:
    """
    Vente si sell_min critères baissiers réunis (défaut 2/3).
    Évite les sorties prématurées sur un seul indicateur momentané.
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
    for symbol in list(ASSETS.keys()):
        try:
            pos = get_position(symbol)
            if pos:
                close_position(symbol)
                clear_position_meta(symbol)
        except RuntimeError as e:
            logger.error("_close_all_positions %s : %s", symbol, e)


def check_daily_loss_limit() -> bool:
    account = get_account()
    if not account:
        return False

    portfolio_value = account["portfolio_value"]
    # Pas de garde portfolio_value <= 0 ici : en mode LIVE un compte à 0 doit
    # déclencher le circuit breaker (cum_dd -100%). Les gardes start==0 et peak>0
    # dans les blocs suivants évitent les divisions par zéro.

    # ── Drawdown cumulé (LIVE uniquement) ────────────────────────────────
    if not T212_DEMO:
        stats = load_risk_stats()
        peak  = stats.get("peak_portfolio_value")
        if peak is None or portfolio_value > peak:
            stats["peak_portfolio_value"] = portfolio_value
            save_json(RISK_STATS_FILE, stats)
            peak = portfolio_value

        if peak > 0:
            cum_dd    = (portfolio_value - peak) / peak
            cfg_limit = load_config().get("risk", {}).get(
                "cumulative_drawdown_limit", CUMULATIVE_DRAWDOWN_LIMIT
            )
            if cum_dd <= -cfg_limit:
                _close_all_positions()
                ctrl           = load_bot_control()
                ctrl["paused"] = True
                ctrl["paused_at"] = datetime.now(ZoneInfo("Europe/London")).isoformat()
                save_bot_control(ctrl)
                send_telegram(
                    f"🚨 <b>ARRÊT D'URGENCE — Drawdown cumulé</b>\n"
                    f"Pic : {peak:.2f} € → Actuel : {portfolio_value:.2f} €\n"
                    f"Drawdown : <b>{cum_dd*100:+.1f}%</b> (seuil -{cfg_limit*100:.0f}%)\n"
                    "Toutes positions clôturées. Bot suspendu.\n"
                    "⚠️ Intervention manuelle requise — envoyez /resume pour relancer."
                )
                logger.critical("ARRÊT D'URGENCE — drawdown cumulé %.1f%%", cum_dd * 100)
                return True

    # ── Circuit breaker journalier ────────────────────────────────────────
    daily = load_daily_pnl()
    if daily.get("start_value") is None:
        if portfolio_value > 0:
            daily["start_value"] = portfolio_value
            save_json(DAILY_PNL_FILE, daily)
        else:
            logger.warning("check_daily_loss_limit: portfolio_value=0 — circuit breaker différé au prochain run")
        return False

    start = daily.get("start_value", 0)
    if start == 0:
        return False

    loss_pct = (portfolio_value - start) / start
    cb_pct   = DAILY_LOSS_LIMIT_PCT if T212_DEMO else _get_circuit_breaker_pct()

    if loss_pct <= -cb_pct:
        logger.warning("Circuit breaker : %.2f%% (seuil %.0f%%)", loss_pct * 100, cb_pct * 100)
        _close_all_positions()
        send_telegram(
            f"⛔ <b>Circuit breaker</b> ({loss_pct*100:+.1f}%)\n"
            f"Seuil : -{cb_pct*100:.0f}% | Toutes positions clôturées.\n"
            "Trading suspendu jusqu'à demain."
        )
        return True

    return False


# ============================================================
# SECTION 8 — EXÉCUTION DES ORDRES
# ============================================================

def _execute_trailing_stop(
    symbol: str, signals: dict, position: dict, max_price: float, mode: str
) -> None:
    stop_pct    = ASSETS[symbol]["stop"]
    stop_price  = max_price * (1 - stop_pct)
    asset_cfg   = ASSETS[symbol]
    if close_position(symbol):
        pl = position["unrealized_pl"]
        update_daily_pnl(pl)
        clear_position_meta(symbol)
        log_decision(symbol, "TRAILING_STOP",
            f"PL={pl:+.2f}€ ({position['unrealized_plpc']*100:+.1f}%) "
            f"max={max_price:.4f} stop={stop_price:.4f}")
        ccy = asset_cfg["ccy"]
        send_telegram(
            f"📉 <b>TRAILING STOP {symbol}</b> [{mode}] {asset_cfg['category']}\n"
            f"P&L : <b>{pl:+.2f} €</b> ({position['unrealized_plpc']*100:+.1f}%)\n"
            f"Prix : {signals['price']:.4f} {ccy} | Max atteint : {max_price:.4f} {ccy}\n"
            f"Stop déclenché à {stop_price:.4f} {ccy} (-{stop_pct*100:.0f}% du max)"
        )
        save_trade({
            "symbol": symbol, "side": "SELL_TRAILING",
            "category": asset_cfg["category"],
            "price": signals["price"], "pl": pl,
            "max_price": max_price, "stop_price": stop_price,
            "timestamp": datetime.now(ZoneInfo("Europe/London")).isoformat(),
        })
        logger.info("TRAILING STOP %s — PL=%.2f€", symbol, pl)


def _execute_stop_loss(symbol: str, signals: dict, position: dict, mode: str) -> None:
    asset_cfg = ASSETS[symbol]
    stop_pct  = asset_cfg["stop"]
    logger.warning("%s stop-loss (%.2f%% / seuil %.0f%%)",
                   symbol, position["unrealized_plpc"] * 100, stop_pct * 100)
    if close_position(symbol):
        pl = position["unrealized_pl"]
        update_daily_pnl(pl)
        clear_position_meta(symbol)
        log_decision(symbol, "STOP_LOSS",
            f"PL={pl:+.2f}€ ({position['unrealized_plpc']*100:+.1f}%) seuil=-{stop_pct*100:.0f}%")
        send_telegram(
            f"🛑 <b>STOP-LOSS {symbol}</b> [{mode}] {asset_cfg['category']}\n"
            f"Perte : <b>{pl:+.2f} €</b> ({position['unrealized_plpc']*100:+.1f}%)\n"
            f"Seuil : -{stop_pct*100:.0f}% | Prix : {signals['price']:.4f} {asset_cfg['ccy']}"
        )
        save_trade({
            "symbol": symbol, "side": "SELL_STOPLOSS",
            "category": asset_cfg["category"],
            "price": signals["price"], "pl": pl,
            "timestamp": datetime.now(ZoneInfo("Europe/London")).isoformat(),
        })
        logger.info("STOP-LOSS %s — PL=%.2f€", symbol, pl)


def _execute_sell(symbol: str, signals: dict, position: dict, mode: str) -> None:
    asset_cfg = ASSETS[symbol]
    if close_position(symbol):
        pl = position["unrealized_pl"]
        update_daily_pnl(pl)
        clear_position_meta(symbol)
        log_decision(symbol, "VENTE_SIGNAL",
            f"PL={pl:+.2f}€ ({position['unrealized_plpc']*100:+.1f}%)", signals)
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
            f"Prix : {signals['price']:.4f} {asset_cfg['ccy']} | RSI : {signals['rsi']:.1f}\n"
            f"Raison : {' + '.join(reasons)}"
        )
        save_trade({
            "symbol": symbol, "side": "SELL",
            "category": asset_cfg["category"],
            "price": signals["price"], "pl": pl,
            "reason": " + ".join(reasons),
            "timestamp": datetime.now(ZoneInfo("Europe/London")).isoformat(),
        })
        logger.info("VENTE %s — PL=%.2f€", symbol, pl)


def _execute_buy(
    symbol: str, signals: dict,
    buying_power: float, portfolio_value: float,
    invested: float,
    positions_map: dict, mode: str,
) -> None:
    asset_cfg = ASSETS[symbol]
    stop_pct  = asset_cfg["stop"]

    # ── Garde capital live (mode LIVE uniquement) ──────────────────────────
    if not T212_DEMO:
        cfg        = load_config()
        live_limit = cfg.get("trading", {}).get("live_capital_limit")
        if live_limit is None:
            live_limit = 500
            logger.warning("live_capital_limit absent de config.yaml — fallback 500€")
        if invested >= live_limit:
            log_decision(symbol, "ACHAT_REJETÉ_LIMITE",
                f"capital live {live_limit:.0f}€ atteint (investi={invested:.2f}€)", signals)
            logger.info("%s : achat bloqué — limite capital live %.0f€", symbol, live_limit)
            return

    cal = 1.0
    if TEST_TRADE:
        amount = 2.0  # montant forcé pour test — 2€ pour dépasser la quantité minimale T212
    else:
        cal    = _calibration_factor()
        amount = calculate_amount_atr(
            symbol, buying_power, portfolio_value, signals["atr"], signals["price"], cal=cal
        )
    if place_buy_order(symbol, amount, signals["price"]):
        qty_approx  = amount / signals["price"]
        daily_emoji = {"bullish": "🌞", "bearish": "🌧️", "unknown": "❓"}.get(
            signals["daily_trend"], "❓"
        )
        # ATR sizing info
        sizing      = SIZING_PARAMS[asset_cfg["params"]]
        atr_stop_eur = portfolio_value * sizing["risk_pct"]
        send_telegram(
            f"📥 <b>ACHAT {symbol}</b> [{mode}] {asset_cfg['category']}\n"
            f"Montant : <b>{amount:.2f} €</b> (≈ {qty_approx:.4f} actions)\n"
            f"Prix : {signals['price']:.4f} {asset_cfg['ccy']} | RSI : {signals['rsi']:.1f}/{signals['rsi_buy']}\n"
            f"ATR : {signals['atr']:.4f} {asset_cfg['ccy']} | Risque max : {atr_stop_eur:.2f} €\n"
            f"EMA ✅ | MACD ↗ ✅ | Vol : {'✅' if signals['volume_ok'] else '⚠️'} | "
            f"Trend J : {daily_emoji}\n"
            f"Confluence : <b>{signals['confluence_score']}/5</b> | "
            f"Stop : -{stop_pct*100:.0f}% | Trailing dès +{TRAILING_ACTIVATION_PCT*100:.0f}%"
        )
        # ── Alerte premier ordre réel ──────────────────────────────────────
        if not T212_DEMO:
            ctrl = load_bot_control()
            if not ctrl.get("first_live_order_done"):
                send_telegram(
                    f"⚠️ <b>PREMIER ORDRE RÉEL</b>\n"
                    f"{symbol} ACHAT <b>{amount:.2f} €</b>\n"
                    "Vérifiez manuellement sur Trading 212 avant de continuer."
                )
                ctrl["first_live_order_done"]   = True
                ctrl["calibration_live_start"]  = ctrl.get("calibration_live_start") \
                                                  or datetime.now(ZoneInfo("Europe/London")).isoformat()
                save_bot_control(ctrl)
            # Marquer la date de démarrage live dans risk_stats pour le CB progressif
            stats = load_risk_stats()
            if not stats.get("live_start_date"):
                stats["live_start_date"] = datetime.now(ZoneInfo("Europe/London")).isoformat()
                save_json(RISK_STATS_FILE, stats)

        cal_note = f" [calibration {cal*100:.0f}%]" if cal < 1.0 else ""
        log_decision(symbol, "ACHAT_ORDRE",
            f"{amount:.2f}€ prix={signals['price']:.4f}{asset_cfg['ccy']}{cal_note}", signals)
        init_position_meta(symbol, signals["price"])
        save_trade({
            "symbol": symbol, "side": "BUY",
            "category": asset_cfg["category"],
            "price": signals["price"], "amount_eur": amount,
            "atr": signals["atr"],
            "confluence_score": signals["confluence_score"],
            "daily_trend": signals["daily_trend"],
            "calibration_factor": cal,
            "timestamp": datetime.now(ZoneInfo("Europe/London")).isoformat(),
        })
        logger.info("ACHAT %s — %.2f€ | ATR=%.4f | confluence=%d/5 | cal=%.0f%%",
                    symbol, amount, signals["atr"], signals["confluence_score"], cal * 100)


def _handle_asset(
    symbol: str,
    signals: dict,
    position: Optional[dict],
    positions_map: dict,
    buying_power: float,
    portfolio_value: float,
    invested: float,
    mode: str,
    open_count: int,
) -> int:
    """
    Gère un actif. Retourne le delta de positions : +1 achat, -1 vente/SL, 0 sinon.

    Ordre de priorité :
    1. Trailing stop (si gain >= +2% et prix retombé)
    2. Stop-loss classique
    3. Signal de vente (2/3 critères)
    4. Signal d'achat (vérification corrélation + limite positions)
    """
    has_position = position is not None

    if has_position:
        # Mise à jour du max_price pour le trailing stop
        max_price = update_max_price(symbol, position["current_price"])

        # 1. Trailing stop
        if check_trailing_stop(position, symbol):
            _execute_trailing_stop(symbol, signals, position, max_price, mode)
            return -1

        # 2. Stop-loss classique
        if check_stop_loss(position, symbol):
            _execute_stop_loss(symbol, signals, position, mode)
            return -1

        # 3. Vente (2/3 critères)
        if should_sell(signals, has_position):
            _execute_sell(symbol, signals, position, mode)
            return -1

    else:
        # Nettoyage si la position a été fermée externalement
        clear_position_meta(symbol)

        # 4. Achat
        if should_buy(signals, has_position):
            if open_count >= MAX_OPEN_POSITIONS:
                log_decision(symbol, "ACHAT_REJETÉ_MAX_POS",
                    f"MAX_OPEN_POSITIONS={MAX_OPEN_POSITIONS} atteint", signals)
                logger.info("%s : achat ignoré [score=%d/5] — limite %d positions",
                            symbol, signals["confluence_score"], MAX_OPEN_POSITIONS)
                return 0

            allowed, reason = check_correlation_allowed(symbol, positions_map)
            if not allowed:
                log_decision(symbol, "ACHAT_REJETÉ_CORR", reason, signals)
                logger.info("%s : achat bloqué — %s", symbol, reason)
                return 0

            _execute_buy(symbol, signals, buying_power, portfolio_value, invested, positions_map, mode)
            return 1

    logger.debug(
        "%s : %s | RSI=%.1f/%d | trend=%s | MACD↗=%s | vol=%s | daily=%s | score=%d/5",
        symbol, "position" if has_position else "attente",
        signals["rsi"], signals["rsi_buy"],
        signals["trend_bullish"], signals["macd_bull_cross"],
        signals["volume_ok"], signals["daily_trend"], signals["confluence_score"],
    )
    return 0


# ============================================================
# SECTION 9 — BOUCLE PRINCIPALE
# ============================================================

def run_strategy() -> None:
    """Appelé toutes les 10 min par GitHub Actions (ou par le scheduler local)."""
    logger.info("=== Analyse (%d actifs) ===", len(ASSETS))

    ctrl = load_bot_control()
    if ctrl.get("paused"):
        logger.info("Bot en pause (envoyez /resume via Telegram pour reprendre)")
        return

    # Bloquer TEST_TRADE en mode LIVE — évite des ordres non-signalisés avec argent réel
    if TEST_TRADE and not T212_DEMO:
        logger.critical("TEST_TRADE=true en mode LIVE — trading bloqué, incohérence de config")
        _send_error_alert(
            "TEST_TRADE=true détecté en mode LIVE\n"
            "Trading bloqué — désactivez TEST_TRADE dans le workflow pour reprendre."
        )
        return

    if not is_market_open():
        logger.info("Marché fermé")
        return

    if check_daily_loss_limit():
        return

    account = get_account()
    if not account:
        logger.error("Impossible de récupérer le compte T212")
        _send_error_alert("T212 inaccessible (account/cash) — run annulé")
        return

    buying_power    = account["buying_power"]
    portfolio_value = account["portfolio_value"]
    invested        = account.get("invested", 0)
    mode            = "🧪 DEMO" if T212_DEMO else "💰 RÉEL"

    raw_positions = get_all_positions()
    if raw_positions is None:
        logger.error("T212 inaccessible — positions non récupérables, run annulé")
        _send_error_alert("T212 inaccessible (portfolio) — run annulé pour éviter les achats en double")
        return
    positions_map = {p["symbol"]: p for p in raw_positions}
    open_count    = sum(1 for sym in ASSETS if sym in positions_map)

    active_symbols = [sym for sym in ASSETS if not is_asset_disabled(sym)]
    all_signals    = _prefetch_signals(active_symbols)

    # Alerte si yfinance renvoie None pour TOUS les actifs — distingue une panne yfinance
    # d'un simple "pas de signal" (ce dernier est normal, la panne ne l'est pas).
    if active_symbols and all(all_signals.get(sym) is None for sym in active_symbols):
        logger.error("yfinance inaccessible — aucun signal disponible pour %d actifs", len(active_symbols))
        _send_error_alert(
            f"yfinance inaccessible — 0/{len(active_symbols)} actifs ont renvoyé des données.\n"
            "Le run est ignoré jusqu'à la prochaine exécution."
        )
        return

    # ── Mode TEST_TRADE : forcer un achat de 1€ sur le premier actif sans position ──
    if TEST_TRADE:
        test_sym = next((s for s in active_symbols if s not in positions_map), None)
        if test_sym and all_signals.get(test_sym):
            logger.info("TEST_TRADE activé — achat forcé 2€ sur %s", test_sym)
            _execute_buy(test_sym, all_signals[test_sym], buying_power,
                         portfolio_value, invested, positions_map, mode)
        else:
            logger.warning("TEST_TRADE : aucun actif disponible sans position")
        return

    # Traiter d'abord les positions ouvertes (stop-loss / ventes),
    # puis les achats potentiels triés par confluence décroissante
    def _sort_key(sym: str) -> tuple:
        sig = all_signals.get(sym)
        if sig is None:
            return (2, 0)
        return (0, 0) if sym in positions_map else (1, -sig.get("confluence_score", 0))

    for symbol in sorted(active_symbols, key=_sort_key):
        signals = all_signals.get(symbol)
        if signals is None:
            logger.warning("%s : données indisponibles", symbol)
            continue
        delta = _handle_asset(
            symbol, signals, positions_map.get(symbol), positions_map,
            buying_power, portfolio_value, invested, mode, open_count,
        )
        open_count += delta
        if delta == 1:
            positions_map[symbol] = {"symbol": symbol}
            # Rafraîchir buying_power et invested — évite que le live_capital_limit
            # soit contourné si deux achats se déclenchent dans le même run.
            fresh = get_account()
            if fresh:
                buying_power = fresh["buying_power"]
                invested     = fresh.get("invested", 0)


def daily_summary() -> None:
    """
    Résumé 16h30 London enrichi :
    - P&L, positions, streak
    - VaR 95% historique 20j
    - Exposition par catégorie
    - Alerte corrélation portfolio
    """
    account    = get_account()
    account_ok = bool(account)            # {} (T212 down) est falsy
    _positions = get_all_positions()
    positions  = _positions if _positions is not None else []
    daily      = load_daily_pnl()
    mode       = "🧪 DEMO" if T212_DEMO else "💰 RÉEL"
    pnl        = daily.get("pnl", 0.0)
    trades_today = daily.get("trades", 0)
    # N'enregistrer le résultat que si des trades ont eu lieu — évite de comptabiliser
    # les jours sans trading (T212 down, pas de signal) comme des jours "profitables"
    # ce qui gonflerait profitable_weeks et détendrait prématurément le circuit breaker.
    streak = record_day_result(pnl) if trades_today > 0 else load_risk_stats().get("win_streak", 0)
    t212_ok = _positions is not None

    lines = [
        f"📊 <b>Résumé {datetime.now(ZoneInfo('Europe/London')).strftime('%d/%m/%Y')}</b> [{mode}]",
        "",
        f"💼 Portfolio : <b>{account.get('portfolio_value', 0):.2f} €</b>" + ("" if account_ok else " ⚠️"),
        f"💵 Disponible : {account.get('buying_power', 0):.2f} €",
        f"📈 Investi : {account.get('invested', 0):.2f} €",
        "" if account_ok else "⚠️ T212 inaccessible — données compte non récupérées",
        f"{'📈' if pnl >= 0 else '📉'} P&L du jour : <b>{pnl:+.2f} €</b>",
        f"🔄 Trades : {trades_today}" + ("" if trades_today > 0 else " (aucun — journée non comptabilisée)"),
        f"{'🏆' if streak > 0 else '💀'} Série gagnante : <b>{streak} jour(s)</b>",
        "" if t212_ok else "⚠️ T212 inaccessible — positions non récupérées",
        "",
    ]

    # Positions ouvertes groupées par catégorie
    if positions:
        by_cat: dict[str, list] = {}
        for p in positions:
            cat = ASSETS.get(p["symbol"], {}).get("category", "Autre")
            by_cat.setdefault(cat, []).append(p)
        lines.append("📋 <b>Positions ouvertes :</b>")
        for cat, pos_list in by_cat.items():
            lines.append(f"\n{cat}")
            for p in pos_list:
                arrow = "📈" if p["unrealized_pl"] >= 0 else "📉"
                meta  = load_positions_meta().get(p["symbol"], {})
                trail = f" | max={meta['max_price']:.4f}" if "max_price" in meta else ""
                lines.append(
                    f"  {arrow} <b>{p['symbol']}</b> | "
                    f"{p['avg_entry_price']:.4f}→{p['current_price']:.4f} {ASSETS.get(p['symbol'], {}).get('ccy', '')}{trail} | "
                    f"P&L : {p['unrealized_pl']:+.2f} € ({p['unrealized_plpc']*100:+.1f}%)"
                )
    else:
        lines.append("Aucune position ouverte ce soir")

    portfolio_value = account.get("portfolio_value", 0)

    # Métriques de risque (téléchargement parallèle des rendements historiques)
    if positions and portfolio_value > 0:
        symbols_held   = [p["symbol"] for p in positions if p["symbol"] in ASSETS]
        hist_returns   = _fetch_historical_returns(symbols_held, days=VAR_HISTORY_DAYS + 5)

        # Exposition par catégorie
        exposure = calculate_exposure(positions, portfolio_value)
        if exposure:
            lines.append("\n📊 <b>Exposition par catégorie :</b>")
            for cat, pct in exposure.items():
                bar = "█" * int(pct / 10) + "░" * (10 - int(pct / 10))
                lines.append(f"  {cat} : {pct:.1f}% {bar}")

        # VaR 95%
        var_result = calculate_var_95(positions, hist_returns, portfolio_value)
        if var_result:
            var_pct, var_eur = var_result
            lines.append(
                f"\n📉 <b>VaR 95% ({VAR_HISTORY_DAYS}j)</b> : "
                f"{var_pct:.2f}% (≈ {var_eur:.2f} €)"
            )

        # Corrélation portfolio
        avg_corr = calculate_portfolio_correlation(positions, hist_returns)
        if avg_corr is not None:
            corr_flag = "⚠️ ALERTE" if avg_corr > CORR_ALERT_THRESHOLD else "✅"
            lines.append(
                f"🔗 Corrélation moy. portfolio : {avg_corr:.2f} "
                f"(seuil {CORR_ALERT_THRESHOLD}) {corr_flag}"
            )
            if avg_corr > CORR_ALERT_THRESHOLD:
                lines.append(
                    "  → Positions corrélées : risque de pertes simultanées élevé."
                )

    # Actifs désactivés
    disabled = load_json(DISABLED_ASSETS_FILE, [])
    if disabled:
        lines.append(f"\n⚠️ Actifs désactivés : {', '.join(disabled)}")

    # Paramètres live : calibration + circuit breaker actif
    if not T212_DEMO:
        cal_factor = _calibration_factor()
        cb_pct     = _get_circuit_breaker_pct()
        lines.append(
            f"\n🎛 <b>Paramètres live</b>\n"
            f"Sizing : <b>{'50% (calibration)' if cal_factor < 1.0 else '100% (normal)'}</b> | "
            f"CB jour : <b>-{cb_pct*100:.0f}%</b>"
        )

    send_telegram("\n".join(lines))
    logger.info("Résumé journalier envoyé")


# ============================================================
# SECTION 10 — MAIN & SCHEDULER (exécution locale uniquement)
# ============================================================

def main() -> None:
    logger.info("Démarrage TradingBot — %s | %d actifs",
                "DEMO" if T212_DEMO else "RÉEL", len(ASSETS))

    all_files = [
        (TRADES_FILE, []), (DAILY_PNL_FILE, {}),
        (DISABLED_ASSETS_FILE, []), (POSITIONS_META_FILE, {}),
        (RISK_STATS_FILE, {
            "win_streak": 0, "last_date": None,
            "current_week_pnl": 0.0, "weekly_results": [],
            "profitable_weeks": 0, "peak_portfolio_value": None,
            "live_start_date": None,
        }),
    ]
    for f, default in all_files:
        if not f.exists():
            save_json(f, default)
    load_daily_pnl()

    account = get_account()
    mode    = "🧪 DEMO TRADING" if T212_DEMO else "💰 TRADING RÉEL"

    categories: dict[str, list] = {}
    for symbol, cfg in ASSETS.items():
        categories.setdefault(cfg["category"], []).append(symbol)

    send_telegram(
        f"🤖 <b>TradingBot démarré</b> — {mode}\n\n"
        + "\n".join(f"{cat} : {', '.join(syms)}" for cat, syms in categories.items())
        + f"\n\nSizing ATR | Trailing stop +{TRAILING_ACTIVATION_PCT*100:.0f}% | "
        f"Max {MAX_OPEN_POSITIONS} positions\n"
        f"Corrélation : PHAU≠PHAG, EQQQ≠XNAS, BTCE≠ETHE | Max {MAX_PER_CATEGORY}/catégorie\n\n"
        f"💼 Portfolio : <b>{account.get('portfolio_value', 0):.2f} €</b>\n"
        f"💵 Disponible : <b>{account.get('buying_power', 0):.2f} €</b>"
    )

    scheduler = BlockingScheduler(timezone="Europe/London")
    scheduler.add_job(poll_telegram_commands, "cron", day_of_week="mon-fri",
                      hour="7-17", minute="*/10", id="poll_commands")
    scheduler.add_job(run_strategy, "cron", day_of_week="mon-fri",
                      hour="8-16", minute="*/10", id="run_strategy")
    scheduler.add_job(daily_summary, "cron", day_of_week="mon-fri",
                      hour=16, minute=35, id="daily_summary")
    logger.info("Scheduler actif — 10 min / 8h–16h30 London / lun–ven")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("TradingBot arrêté proprement")
        send_telegram("🛑 <b>TradingBot arrêté</b>")


if __name__ == "__main__":
    main()
