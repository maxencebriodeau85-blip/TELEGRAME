#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backtest.py — Backtesting de la stratégie TradingBot sur données yfinance 15min
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Usage :
  python backtest.py               → portfolio complet (10 actifs)
  python backtest.py --symbol GLD  → actif individuel
  python backtest.py --no-plot     → sans courbe ASCII

Stratégie testée (baseline originale) :
  ACHAT : EMA20 > EMA50 ET RSI < 45 ET MACD(12/26/9) croisement haussier
  VENTE : RSI > 65 OU EMA20 < EMA50 OU MACD croisement baissier
  Stop-loss : par actif (2–7 %)
  Pas de lookahead : signaux calculés sur bar[t], exécutés à l'ouverture de bar[t+1]

⚠️  yfinance limite les données 15min à 60 jours (~42 jours de trading, ~1 092 barres/actif).
    Les métriques de Sharpe/Sortino sont indicatives sur cette courte fenêtre.
"""

import argparse
import csv
import sys
from datetime import time as dtime
from math import sqrt

import numpy as np
import pandas as pd
import yfinance as yf

# ============================================================
# CONFIGURATION
# ============================================================

INITIAL_CAPITAL   = 10_000.0   # €
SLIPPAGE          = 0.0005     # 0.05 % par trade (Trading 212 0 commission)
DAILY_LOSS_LIMIT  = 0.05       # circuit breaker -5 % / jour
MAX_OPEN_POS      = 3          # positions simultanées max (portfolio)
MAX_PER_CAT       = 2          # par catégorie
DATA_PERIOD       = "60d"      # max yfinance 15min
DATA_INTERVAL     = "15m"

# Paramètres de signal — version baseline (uniformes sur tous les actifs)
SIG = {
    "rsi_buy":   45,
    "rsi_sell":  65,
    "macd_fast": 12,
    "macd_slow": 26,
    "macd_sig":   9,
}

ASSETS: dict[str, dict] = {
    "GLD":  {"cat": "Commodities", "stop": 0.04, "pct": 0.15},
    "SLV":  {"cat": "Commodities", "stop": 0.04, "pct": 0.15},
    "USO":  {"cat": "Commodities", "stop": 0.05, "pct": 0.12},
    "UNG":  {"cat": "Commodities", "stop": 0.05, "pct": 0.10},
    "FXE":  {"cat": "Forex",       "stop": 0.02, "pct": 0.15},
    "UUP":  {"cat": "Forex",       "stop": 0.02, "pct": 0.15},
    "FXB":  {"cat": "Forex",       "stop": 0.02, "pct": 0.10},
    "FXY":  {"cat": "Forex",       "stop": 0.02, "pct": 0.10},
    "BITO": {"cat": "Crypto",      "stop": 0.07, "pct": 0.08},
    "IBIT": {"cat": "Crypto",      "stop": 0.07, "pct": 0.08},
}

# Paires mutuellement exclusives dans le portfolio
CORR_EXCLUSIONS: list[frozenset] = [
    frozenset({"GLD", "SLV"}),
    frozenset({"BITO", "IBIT"}),
]


# ============================================================
# INDICATEURS (pure pandas/numpy — pas de pandas_ta nécessaire)
# ============================================================

def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def _rsi(s: pd.Series, period: int = 14) -> pd.Series:
    d    = s.diff()
    gain = d.clip(lower=0).ewm(alpha=1 / period, min_periods=period).mean()
    loss = (-d.clip(upper=0)).ewm(alpha=1 / period, min_periods=period).mean()
    rs   = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _macd(s: pd.Series, fast: int, slow: int, sig: int
          ) -> tuple[pd.Series, pd.Series]:
    line   = _ema(s, fast) - _ema(s, slow)
    signal = _ema(line, sig)
    return line, signal


def compute_signals(df: pd.DataFrame) -> pd.DataFrame:
    """
    Calcule EMA/RSI/MACD et génère les signaux d'achat/vente.

    Règle anti-lookahead : les raw signals sont calculés sur la bougie close[t],
    puis décalés d'une barre (.shift(1)) — l'exécution se fait donc au
    close de bar[t+1], soit la première bougie où on peut agir sur l'information.

    Le MACD cross détecte le passage entre bar[t-1] et bar[t], décalé de 1
    pour exécution au bar[t+1].
    """
    close = df["Close"]

    ema20 = _ema(close, 20)
    ema50 = _ema(close, 50)
    rsi   = _rsi(close, 14)
    macd_line, macd_sig = _macd(close, SIG["macd_fast"], SIG["macd_slow"], SIG["macd_sig"])

    # Croisements sur bar courant (comparaison bar[t-1] vs bar[t])
    bull_cross = (macd_line.shift(1) < macd_sig.shift(1)) & (macd_line > macd_sig)
    bear_cross = (macd_line.shift(1) > macd_sig.shift(1)) & (macd_line < macd_sig)

    raw_buy  = (ema20 > ema50) & (rsi < SIG["rsi_buy"]) & bull_cross
    raw_sell = (rsi > SIG["rsi_sell"]) | (ema20 < ema50) | bear_cross

    out = df.copy()
    out["ema20"]       = ema20
    out["ema50"]       = ema50
    out["rsi"]         = rsi
    out["buy_signal"]  = raw_buy.shift(1).fillna(False).astype(bool)
    out["sell_signal"] = raw_sell.shift(1).fillna(False).astype(bool)

    # Confluence score 0-3 (pour ordering dans le portfolio)
    out["confluence"] = (
        (ema20 > ema50).astype(int)
        + (rsi < SIG["rsi_buy"]).astype(int)
        + bull_cross.astype(int)
    ).shift(1).fillna(0)

    return out


# ============================================================
# MÉTRIQUES
# ============================================================

def calc_metrics(equity: pd.Series, trades: list[dict], symbol: str) -> dict:
    """
    Calcule toutes les métriques à partir de la courbe d'equity et du journal de trades.
    Sharpe/Sortino annualisés sur base de rendements journaliers (252 jours/an).
    """
    base: dict = {
        "symbol": symbol,
        "start_equity": float(equity.iloc[0]),
        "end_equity":   float(equity.iloc[-1]),
        "total_return": 0.0,
        "sharpe": 0.0, "sortino": 0.0,
        "max_dd_pct": 0.0, "max_dd_days": 0,
        "n_trades": 0, "win_rate": 0.0,
        "profit_factor": 0.0, "expectancy": 0.0,
        "gross_profit": 0.0, "gross_loss": 0.0,
    }

    if equity.empty or len(equity) < 2:
        return base

    base["total_return"] = (equity.iloc[-1] / equity.iloc[0] - 1) * 100

    # Rendements journaliers (resample à la journée)
    daily = equity.resample("1D").last().dropna()
    dr    = daily.pct_change().dropna()

    if len(dr) > 1 and dr.std() > 0:
        base["sharpe"] = round(float(dr.mean() / dr.std() * sqrt(252)), 3)

    neg = dr[dr < 0]
    if len(neg) > 0 and neg.std() > 0:
        base["sortino"] = round(float(dr.mean() / neg.std() * sqrt(252)), 3)

    # Max Drawdown
    peak = equity.cummax()
    dd   = (equity - peak) / peak
    base["max_dd_pct"] = round(float(dd.min()) * 100, 2)

    # Durée max du drawdown en jours
    in_dd = dd < 0
    max_dur, cur_start = 0, None
    for ts, v in dd.items():
        if v < 0:
            if cur_start is None:
                cur_start = ts
        elif cur_start is not None:
            dur = (ts - cur_start).days
            max_dur = max(max_dur, dur)
            cur_start = None
    if cur_start is not None:
        max_dur = max(max_dur, (dd.index[-1] - cur_start).days)
    base["max_dd_days"] = max_dur

    # Trade metrics
    if not trades:
        return base

    df_t  = pd.DataFrame(trades)
    pnl   = df_t["pnl_eur"]
    wins  = pnl[pnl > 0]
    loss  = pnl[pnl <= 0]

    base["n_trades"]     = len(df_t)
    base["win_rate"]     = round(len(wins) / len(pnl) * 100, 1)
    base["gross_profit"] = round(float(wins.sum()), 2)
    base["gross_loss"]   = round(float(loss.abs().sum()), 2)
    base["profit_factor"] = round(
        base["gross_profit"] / base["gross_loss"], 2
    ) if base["gross_loss"] > 0 else float("inf")
    base["expectancy"]   = round(float(pnl.mean()), 3)

    return base


def spy_buy_hold(start: pd.Timestamp, end: pd.Timestamp) -> float:
    """Rendement Buy & Hold SPY sur la même période. Retourne % ou 0.0 si erreur."""
    try:
        df = yf.download(
            "SPY", start=start.date(), end=end.date(),
            interval="1d", progress=False, auto_adjust=True,
        )
        if df.empty or len(df) < 2:
            return 0.0
        close = df["Close"].squeeze()
        return round((float(close.iloc[-1]) / float(close.iloc[0]) - 1) * 100, 2)
    except Exception:
        return 0.0


# ============================================================
# MOTEUR DE BACKTEST
# ============================================================

class BacktestEngine:
    """
    Simule la stratégie TradingBot sur données 15min yfinance.

    Modes :
    - run_single(symbol) → backtest d'un actif isolé avec capital entier
    - run_portfolio()    → backtest du portfolio (capital partagé, corrélations)
    """

    def __init__(
        self,
        symbols: list[str] | None = None,
        capital: float = INITIAL_CAPITAL,
        slippage: float = SLIPPAGE,
    ) -> None:
        self.symbols  = symbols or list(ASSETS.keys())
        self.capital  = capital
        self.slippage = slippage

        self._raw_data: dict[str, pd.DataFrame] = {}    # OHLCV après filtre NYSE
        self._signals:  dict[str, pd.DataFrame] = {}    # avec buy/sell_signal
        self.results:   dict[str, dict]         = {}    # métriques par actif + portfolio
        self.all_trades: list[dict]             = []    # journal complet
        self._equity:   dict[str, pd.Series]   = {}    # courbes d'equity

    # ------------------------------------------------------------------ #
    #  TÉLÉCHARGEMENT                                                     #
    # ------------------------------------------------------------------ #

    def _download_data(self) -> None:
        """
        Télécharge 60 jours de bougies 15min pour chaque actif,
        filtre sur les heures NYSE (9h30–16h00 ET, lun–ven),
        normalise les colonnes multi-index de yfinance.
        """
        print(f"\n  Téléchargement des données ({DATA_PERIOD} × 15min)…")
        for sym in self.symbols:
            try:
                raw = yf.download(
                    sym, period=DATA_PERIOD, interval=DATA_INTERVAL,
                    progress=False, auto_adjust=True,
                )
                if raw.empty:
                    print(f"  ⚠  {sym} : aucune donnée")
                    continue

                # yfinance peut retourner un MultiIndex — on l'aplatit
                if isinstance(raw.columns, pd.MultiIndex):
                    raw.columns = raw.columns.droplevel(1)

                # Normalisation timezone → America/New_York
                if raw.index.tzinfo is None:
                    raw.index = raw.index.tz_localize("America/New_York",
                                                       ambiguous="infer",
                                                       nonexistent="shift_forward")
                else:
                    raw.index = raw.index.tz_convert("America/New_York")

                # Filtre heures de marché NYSE
                mask = (
                    (raw.index.time >= dtime(9, 30))
                    & (raw.index.time < dtime(16, 0))
                    & (raw.index.dayofweek < 5)
                )
                raw = raw[mask].dropna(subset=["Close"])

                if len(raw) < 60:
                    print(f"  ⚠  {sym} : données insuffisantes ({len(raw)} barres)")
                    continue

                self._raw_data[sym]  = raw
                self._signals[sym]   = compute_signals(raw)
                print(f"  ✓  {sym} : {len(raw)} barres | "
                      f"{raw.index[0].strftime('%d/%m')} → {raw.index[-1].strftime('%d/%m')}")

            except Exception as e:
                print(f"  ✗  {sym} : erreur téléchargement — {e}")

    # ------------------------------------------------------------------ #
    #  BACKTEST INDIVIDUEL                                                #
    # ------------------------------------------------------------------ #

    def run_single(self, symbol: str) -> dict:
        """
        Backtest d'un actif seul avec INITIAL_CAPITAL dédié.
        Stop-loss vérifié sur le Low de chaque bougie (réaliste).
        """
        if symbol not in self._signals:
            return {}

        cfg   = ASSETS[symbol]
        sig   = self._signals[symbol]
        stop  = cfg["stop"]
        pct   = cfg["pct"]

        cash       = self.capital
        qty        = 0.0
        entry_p    = 0.0
        stop_p     = 0.0
        entry_ts   = None
        trades: list[dict] = []
        equity_pts: list[tuple] = []

        # Circuit breaker par jour
        day_start_val = self.capital
        cur_day       = None
        cb_active     = False

        for ts, row in sig.iterrows():
            bar_day = ts.date()

            if bar_day != cur_day:
                cur_day       = bar_day
                day_start_val = cash + qty * float(row["Close"])
                cb_active     = False

            price      = float(row["Close"])
            low        = float(row["Low"])
            open_p     = float(row["Open"])
            pos_val    = qty * price
            total_val  = cash + pos_val

            # Circuit breaker check
            if not cb_active and day_start_val > 0:
                day_loss = (total_val - day_start_val) / day_start_val
                if day_loss <= -DAILY_LOSS_LIMIT:
                    if qty > 0:
                        fill = price * (1 - self.slippage)
                        pnl  = (fill - entry_p) * qty
                        trades.append(_trade_record(
                            symbol, entry_ts, ts, entry_p, fill,
                            qty, pnl, "CIRCUIT_BREAKER", cfg["cat"],
                        ))
                        cash += qty * fill
                        qty   = 0.0
                    cb_active = True

            # Equity snapshot (avant ordre)
            equity_pts.append((ts, cash + qty * price))

            if cb_active:
                continue

            # ── 1. Stop-loss (vérifié sur le Low de la bougie) ───────
            if qty > 0 and low <= stop_p:
                # Le stop est touché : fill au stop_price ou à l'open si gap baissier
                fill = (stop_p if open_p > stop_p else open_p) * (1 - self.slippage)
                pnl  = (fill - entry_p) * qty
                trades.append(_trade_record(
                    symbol, entry_ts, ts, entry_p, fill,
                    qty, pnl, "STOP_LOSS", cfg["cat"],
                ))
                cash += qty * fill
                qty   = 0.0

            # ── 2. Signal de vente ───────────────────────────────────
            elif qty > 0 and bool(row["sell_signal"]):
                fill = price * (1 - self.slippage)
                pnl  = (fill - entry_p) * qty
                trades.append(_trade_record(
                    symbol, entry_ts, ts, entry_p, fill,
                    qty, pnl, "SELL_SIGNAL", cfg["cat"],
                ))
                cash += qty * fill
                qty   = 0.0

            # ── 3. Signal d'achat ────────────────────────────────────
            elif qty == 0 and bool(row["buy_signal"]):
                amount = min(cash * pct, cash)
                if amount >= 1.0:
                    fill   = price * (1 + self.slippage)
                    qty    = amount / fill
                    cash  -= qty * fill
                    entry_p = fill
                    stop_p  = fill * (1 - stop)
                    entry_ts = ts

        # Clôture forcée en fin de backtest
        if qty > 0:
            last_price = float(sig["Close"].iloc[-1])
            fill = last_price * (1 - self.slippage)
            pnl  = (fill - entry_p) * qty
            trades.append(_trade_record(
                symbol, entry_ts, sig.index[-1], entry_p, fill,
                qty, pnl, "END_OF_BACKTEST", cfg["cat"],
            ))
            cash += qty * fill

        equity = pd.Series(
            [v for _, v in equity_pts],
            index=pd.DatetimeIndex([ts for ts, _ in equity_pts]),
        )
        equity.iloc[-1] = cash   # valeur finale en cash pur

        metrics = calc_metrics(equity, trades, symbol)
        self.results[symbol]  = metrics
        self._equity[symbol]  = equity
        self.all_trades.extend(trades)
        return metrics

    # ------------------------------------------------------------------ #
    #  BACKTEST PORTFOLIO                                                 #
    # ------------------------------------------------------------------ #

    def run_portfolio(self) -> dict:
        """
        Backtest du portfolio : capital 10 000€ partagé entre tous les actifs.
        Respect des contraintes de corrélation et de la limite MAX_OPEN_POS.
        """
        # Timeline commune = union des index de tous les actifs disponibles
        common_idx = pd.DatetimeIndex([])
        for sym in self.symbols:
            if sym in self._signals:
                common_idx = common_idx.union(self._signals[sym].index)
        common_idx = common_idx.sort_values()

        if common_idx.empty:
            return {}

        cash = self.capital

        # open_positions: symbol → {qty, entry_p, stop_p, entry_ts}
        open_pos: dict[str, dict] = {}
        trades:   list[dict]      = []
        equity_pts: list[tuple]   = []

        day_start_val = self.capital
        cur_day       = None
        cb_active     = False

        for ts in common_idx:
            bar_day = ts.date()

            if bar_day != cur_day:
                cur_day       = bar_day
                pos_val       = sum(
                    p["qty"] * _price_at(self._signals[sym], ts)
                    for sym, p in open_pos.items()
                    if sym in self._signals
                )
                day_start_val = cash + pos_val
                cb_active     = False

            # Valeur portfolio
            pos_val   = sum(
                p["qty"] * _price_at(self._signals[sym], ts)
                for sym, p in open_pos.items()
                if sym in self._signals
            )
            total_val = cash + pos_val

            # Circuit breaker
            if not cb_active and day_start_val > 0:
                if (total_val - day_start_val) / day_start_val <= -DAILY_LOSS_LIMIT:
                    for sym, p in list(open_pos.items()):
                        price = _price_at(self._signals[sym], ts)
                        if price > 0:
                            fill = price * (1 - self.slippage)
                            pnl  = (fill - p["entry_p"]) * p["qty"]
                            trades.append(_trade_record(
                                sym, p["entry_ts"], ts, p["entry_p"],
                                fill, p["qty"], pnl, "CIRCUIT_BREAKER",
                                ASSETS[sym]["cat"],
                            ))
                            cash += p["qty"] * fill
                    open_pos.clear()
                    cb_active = True

            equity_pts.append((ts, cash + sum(
                p["qty"] * _price_at(self._signals[sym], ts)
                for sym, p in open_pos.items()
                if sym in self._signals
            )))

            if cb_active:
                continue

            # ── Traitement de chaque actif ────────────────────────────
            # Trier : positions ouvertes d'abord (SL/vente), puis achats par confluence
            symbols_ordered = sorted(
                [s for s in self.symbols if s in self._signals],
                key=lambda s: (
                    0 if s in open_pos else 1,
                    -float(_sig_val(self._signals[s], ts, "confluence") or 0),
                ),
            )

            for sym in symbols_ordered:
                sig = self._signals[sym]
                if ts not in sig.index:
                    continue

                row     = sig.loc[ts]
                price   = float(row["Close"])
                low_p   = float(row["Low"])
                open_p  = float(row["Open"])
                cfg     = ASSETS[sym]

                # ── Stop-loss ────────────────────────────────────────
                if sym in open_pos:
                    p      = open_pos[sym]
                    stop_p = p["stop_p"]
                    if low_p <= stop_p:
                        fill = (stop_p if open_p > stop_p else open_p) * (1 - self.slippage)
                        pnl  = (fill - p["entry_p"]) * p["qty"]
                        trades.append(_trade_record(
                            sym, p["entry_ts"], ts, p["entry_p"],
                            fill, p["qty"], pnl, "STOP_LOSS", cfg["cat"],
                        ))
                        cash += p["qty"] * fill
                        del open_pos[sym]
                        continue

                # ── Vente signal ─────────────────────────────────────
                if sym in open_pos and bool(row["sell_signal"]):
                    p    = open_pos[sym]
                    fill = price * (1 - self.slippage)
                    pnl  = (fill - p["entry_p"]) * p["qty"]
                    trades.append(_trade_record(
                        sym, p["entry_ts"], ts, p["entry_p"],
                        fill, p["qty"], pnl, "SELL_SIGNAL", cfg["cat"],
                    ))
                    cash += p["qty"] * fill
                    del open_pos[sym]
                    continue

                # ── Achat signal ─────────────────────────────────────
                if sym not in open_pos and bool(row["buy_signal"]):
                    if len(open_pos) >= MAX_OPEN_POS:
                        continue
                    if not _corr_allowed(sym, set(open_pos.keys())):
                        continue
                    if _count_cat(sym, open_pos) >= MAX_PER_CAT:
                        continue

                    amount = min(self.capital * cfg["pct"], cash)
                    if amount < 1.0:
                        continue
                    fill     = price * (1 + self.slippage)
                    qty      = amount / fill
                    cash    -= qty * fill
                    open_pos[sym] = {
                        "qty":      qty,
                        "entry_p":  fill,
                        "stop_p":   fill * (1 - cfg["stop"]),
                        "entry_ts": ts,
                    }

        # Clôture forcée fin de backtest
        if open_pos:
            last_ts = common_idx[-1]
            for sym, p in open_pos.items():
                price = _price_at(self._signals[sym], last_ts, last_known=True)
                fill  = price * (1 - self.slippage)
                pnl   = (fill - p["entry_p"]) * p["qty"]
                trades.append(_trade_record(
                    sym, p["entry_ts"], last_ts, p["entry_p"],
                    fill, p["qty"], pnl, "END_OF_BACKTEST", ASSETS[sym]["cat"],
                ))
                cash += p["qty"] * fill

        equity = pd.Series(
            [v for _, v in equity_pts],
            index=pd.DatetimeIndex([ts for ts, _ in equity_pts]),
        )

        metrics = calc_metrics(equity, trades, "PORTFOLIO")
        # Comptes par actif et catégorie
        df_t = pd.DataFrame(trades) if trades else pd.DataFrame()
        if not df_t.empty:
            metrics["trades_by_symbol"]   = df_t.groupby("symbol").size().to_dict()
            metrics["trades_by_category"] = df_t.groupby("category").size().to_dict()

        self.results["PORTFOLIO"] = metrics
        self._equity["PORTFOLIO"] = equity
        self.all_trades.extend(trades)
        return metrics

    # ------------------------------------------------------------------ #
    #  MÉTHODES PUBLIQUES                                                 #
    # ------------------------------------------------------------------ #

    def run(self, mode: str = "portfolio") -> None:
        """
        Lance le backtest complet.
        mode = 'portfolio' | 'single:<SYM>' | 'all'
        """
        self._download_data()

        if not self._raw_data:
            print("\n  ✗ Aucune donnée téléchargée. Vérifiez la connexion.")
            sys.exit(1)

        print()
        if mode == "portfolio":
            print("  ▶ Backtest portfolio…")
            self.run_portfolio()

        elif mode.startswith("single:"):
            sym = mode.split(":")[1].upper()
            if sym not in self._signals:
                print(f"  ✗ Symbole '{sym}' non disponible.")
                return
            print(f"  ▶ Backtest {sym}…")
            self.run_single(sym)

        elif mode == "all":
            print("  ▶ Backtest individuel de chaque actif…")
            for sym in self._signals:
                self.run_single(sym)
            print("  ▶ Backtest portfolio…")
            self.run_portfolio()

    def get_metrics(self) -> dict:
        return self.results

    def plot_equity(self, key: str = "PORTFOLIO") -> None:
        """Courbe d'equity en ASCII — compatible terminal et GitHub Actions."""
        if key not in self._equity:
            return
        equity = self._equity[key]
        if equity.empty:
            return
        title  = f"Equity — {key}  ({equity.index[0].strftime('%d/%m/%Y')} → {equity.index[-1].strftime('%d/%m/%Y')})"
        _ascii_chart(equity, title)

    def save_results(self, path: str = "backtest_results.csv") -> None:
        """Exporte tous les trades en CSV."""
        if not self.all_trades:
            print("  ⚠  Aucun trade à exporter.")
            return
        fields = ["symbol", "category", "entry_time", "entry_price",
                  "exit_time", "exit_price", "exit_reason",
                  "quantity", "pnl_eur", "pnl_pct", "hold_bars"]
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            w.writerows(self.all_trades)
        print(f"\n  ✓ Trades exportés → {path}  ({len(self.all_trades)} lignes)")

    def print_summary(self) -> None:
        """Tableau récapitulatif dans le terminal."""
        if not self.results:
            return

        # Période et SPY
        start_ts = end_ts = None
        for sym, eq in self._equity.items():
            if sym == "PORTFOLIO" or eq.empty:
                continue
            if start_ts is None or eq.index[0] < start_ts:
                start_ts = eq.index[0]
            if end_ts is None or eq.index[-1] > end_ts:
                end_ts = eq.index[-1]

        spy_ret = 0.0
        if start_ts and end_ts:
            spy_ret = spy_buy_hold(start_ts, end_ts)

        _print_table(self.results, spy_ret)

        # Avertissement actifs faibles
        weak = [
            sym for sym, m in self.results.items()
            if sym not in ("PORTFOLIO",) and isinstance(m.get("sharpe"), (int, float))
            and m["sharpe"] < 0.5
        ]
        if weak:
            print(f"\n  ⚠  Actifs avec Sharpe < 0.5 (candidats à désactivation) :")
            for sym in weak:
                m = self.results[sym]
                print(f"     {sym:5s} — Sharpe={m['sharpe']:.2f} | "
                      f"Return={m['total_return']:+.1f}% | MaxDD={m['max_dd_pct']:.1f}%")


# ============================================================
# HELPERS (fonctions pures, hors classe)
# ============================================================

def _trade_record(
    symbol: str, entry_ts, exit_ts, entry_p: float, exit_p: float,
    qty: float, pnl: float, reason: str, category: str,
) -> dict:
    """Construit un enregistrement de trade normalisé."""
    hold_bars = 0
    if entry_ts is not None and exit_ts is not None:
        try:
            hold_bars = int((exit_ts - entry_ts) / pd.Timedelta("15min"))
        except Exception:
            hold_bars = 0
    pnl_pct = (exit_p / entry_p - 1) * 100 if entry_p > 0 else 0.0
    return {
        "symbol":      symbol,
        "category":    category,
        "entry_time":  entry_ts,
        "entry_price": round(entry_p, 6),
        "exit_time":   exit_ts,
        "exit_price":  round(exit_p, 6),
        "exit_reason": reason,
        "quantity":    round(qty, 6),
        "pnl_eur":     round(pnl, 4),
        "pnl_pct":     round(pnl_pct, 3),
        "hold_bars":   hold_bars,
    }


def _price_at(sig: pd.DataFrame, ts: pd.Timestamp, last_known: bool = False) -> float:
    """Retourne le Close à l'instant ts, ou le dernier connu si last_known=True."""
    if ts in sig.index:
        return float(sig.loc[ts, "Close"])
    if last_known and not sig.empty:
        before = sig.index[sig.index <= ts]
        if not before.empty:
            return float(sig.loc[before[-1], "Close"])
    return 0.0


def _sig_val(sig: pd.DataFrame, ts: pd.Timestamp, col: str):
    """Retourne sig[col] à ts ou None."""
    if ts in sig.index:
        return sig.loc[ts, col]
    return None


def _corr_allowed(symbol: str, open_syms: set) -> bool:
    for group in CORR_EXCLUSIONS:
        if symbol in group and group - {symbol} & open_syms:
            return False
    return True


def _count_cat(symbol: str, open_pos: dict) -> int:
    cat = ASSETS[symbol]["cat"]
    return sum(1 for s in open_pos if ASSETS.get(s, {}).get("cat") == cat)


# ============================================================
# AFFICHAGE ASCII
# ============================================================

def _ascii_chart(equity: pd.Series, title: str, width: int = 72, height: int = 18) -> None:
    """
    Courbe d'equity en ASCII pur — pas de matplotlib.
    Utilise des blocs Unicode pour dessiner la ligne.
    """
    n    = len(equity)
    idx  = np.linspace(0, n - 1, min(width, n), dtype=int)
    vals = equity.iloc[idx].values
    ts   = equity.index[idx]

    lo, hi = vals.min(), vals.max()
    pad    = max((hi - lo) * 0.05, 1.0)
    lo    -= pad
    hi    += pad
    rang   = hi - lo

    def row_of(v: float) -> int:
        return max(0, min(height - 1, int((hi - v) / rang * (height - 1))))

    grid = [[" "] * width for _ in range(height)]

    # Ligne de référence du capital initial
    start_row = row_of(equity.iloc[0])
    for x in range(width):
        grid[start_row][x] = "·"

    # Tracé de la courbe
    prev_r = None
    for x, v in enumerate(vals):
        r = row_of(v)
        if prev_r is not None and abs(prev_r - r) > 1:
            lo_r, hi_r = min(prev_r, r), max(prev_r, r)
            for rr in range(lo_r + 1, hi_r):
                grid[rr][x] = "│"
        grid[r][x] = "█"
        prev_r = r

    # Impression
    W = width + 14
    print(f"\n  ╔{'═' * W}╗")
    print(f"  ║  {title:<{W - 2}}║")
    print(f"  ╠{'═' * W}╣")

    for r in range(height):
        v      = hi - r * rang / (height - 1)
        marker = "→" if r == start_row else " "
        label  = f"{v:>10,.0f}€ {marker}│"
        print(f"  ║ {label}{''.join(grid[r])}│ ║")

    print(f"  ║  {' ' * 13}└{'─' * width}┘{' ' * 1}║")

    # Étiquettes de dates
    d_start = ts[0].strftime("%d/%m")
    d_mid   = ts[len(ts) // 2].strftime("%d/%m")
    d_end   = ts[-1].strftime("%d/%m")
    half    = width // 2 - len(d_start)
    rest    = width - width // 2 - len(d_mid)
    dateline = f"{d_start}{' ' * half}{d_mid}{' ' * (rest - 2)}{d_end}"
    print(f"  ║  {' ' * 14}{dateline}{' ' * (W - 14 - len(dateline))}║")
    print(f"  ╚{'═' * W}╝")


def _print_table(results: dict, spy_ret: float) -> None:
    """Tableau récapitulatif formaté."""
    cols = ["Symbol", "Return%", "Sharpe", "Sortino", "MaxDD%",
            "MaxDD J", "Trades", "Win%", "PF", "Expect.€"]
    w    = [10, 9, 8, 8, 8, 8, 7, 7, 6, 9]
    sep  = "─" * (sum(w) + len(w) * 3 + 1)

    def row(vals: list) -> str:
        return "│ " + " │ ".join(str(v).rjust(w[i]) for i, v in enumerate(vals)) + " │"

    print(f"\n  {sep}")
    print(f"  │ " + " │ ".join(c.center(w[i]) for i, c in enumerate(cols)) + " │")
    print(f"  {sep}")

    # Actifs individuels d'abord
    for sym, m in sorted(results.items()):
        if sym == "PORTFOLIO":
            continue
        _pf = m.get("profit_factor", 0)
        pf  = f"{_pf:.2f}" if _pf != float("inf") else "∞"
        vals = [
            sym,
            f"{m['total_return']:+.2f}%",
            f"{m['sharpe']:.2f}",
            f"{m['sortino']:.2f}",
            f"{m['max_dd_pct']:.1f}%",
            str(m["max_dd_days"]),
            str(m["n_trades"]),
            f"{m['win_rate']:.0f}%",
            pf,
            f"{m['expectancy']:+.2f}",
        ]
        # Signalement des Sharpe < 0.5
        flag = " ⚠" if m["sharpe"] < 0.5 else "  "
        print(f"  {row(vals)}{flag}")

    # Séparateur portfolio
    print(f"  {sep}")

    if "PORTFOLIO" in results:
        m   = results["PORTFOLIO"]
        _pf = m.get("profit_factor", 0)
        pf  = f"{_pf:.2f}" if _pf != float("inf") else "∞"
        vals = [
            "PORTFOLIO",
            f"{m['total_return']:+.2f}%",
            f"{m['sharpe']:.2f}",
            f"{m['sortino']:.2f}",
            f"{m['max_dd_pct']:.1f}%",
            str(m["max_dd_days"]),
            str(m["n_trades"]),
            f"{m['win_rate']:.0f}%",
            pf,
            f"{m['expectancy']:+.2f}",
        ]
        print(f"  {row(vals)}")

    # SPY Buy & Hold
    spy_vals = [
        "SPY B&H",
        f"{spy_ret:+.2f}%",
        "—", "—", "—", "—", "—", "—", "—", "—",
    ]
    print(f"  {row(spy_vals)}")
    print(f"  {sep}\n")

    # Trades par catégorie (portfolio)
    if "PORTFOLIO" in results:
        m = results["PORTFOLIO"]
        if "trades_by_category" in m:
            print("  Trades par catégorie (portfolio) :")
            for cat, n in m["trades_by_category"].items():
                print(f"    {cat:14s} : {n} trades")
        if "trades_by_symbol" in m:
            print("\n  Trades par actif (portfolio) :")
            for sym, n in sorted(m["trades_by_symbol"].items()):
                sr = results.get(sym, {})
                flag = " ← Sharpe < 0.5 — candidat à désactivation" \
                       if results.get(sym, {}).get("sharpe", 1) < 0.5 else ""
                print(f"    {sym:5s} : {n} trades{flag}")


# ============================================================
# POINT D'ENTRÉE
# ============================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backtest de la stratégie TradingBot (yfinance 15min, 60j)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--symbol", "-s",
        default="all",
        help="Symbole unique (ex: GLD), 'all' pour tous individuels+portfolio, "
             "'portfolio' pour le portfolio seul (défaut: all)",
    )
    parser.add_argument(
        "--capital", "-c",
        type=float, default=INITIAL_CAPITAL,
        help=f"Capital initial en € (défaut: {INITIAL_CAPITAL:.0f})",
    )
    parser.add_argument(
        "--no-plot", action="store_true",
        help="Désactiver la courbe d'equity ASCII",
    )
    parser.add_argument(
        "--no-csv", action="store_true",
        help="Ne pas exporter backtest_results.csv",
    )
    args = parser.parse_args()

    print("━" * 60)
    print("  TradingBot — Backtesting Engine")
    print(f"  Capital : {args.capital:,.0f} € | Slippage : {SLIPPAGE*100:.2f}%")
    print(f"  Période : {DATA_PERIOD} | Intervalle : {DATA_INTERVAL}")
    print(f"  Circuit breaker : -{DAILY_LOSS_LIMIT*100:.0f}%/jour")
    print("━" * 60)

    sym = args.symbol.upper()
    if sym in ASSETS:
        mode = f"single:{sym}"
    elif sym in ("ALL", "PORTFOLIO"):
        mode = sym.lower()
    else:
        print(f"  ✗ Symbole inconnu : {sym}")
        print(f"  Symboles disponibles : {', '.join(ASSETS.keys())} | all | portfolio")
        sys.exit(1)

    engine = BacktestEngine(capital=args.capital)
    engine.run(mode=mode)
    engine.print_summary()

    if not args.no_csv:
        engine.save_results()

    if not args.no_plot:
        if mode == "all":
            for sym in list(ASSETS.keys()) + ["PORTFOLIO"]:
                if sym in engine._equity:
                    engine.plot_equity(sym)
        elif mode.startswith("single:"):
            sym = mode.split(":")[1]
            engine.plot_equity(sym)
        else:
            engine.plot_equity("PORTFOLIO")


if __name__ == "__main__":
    main()
