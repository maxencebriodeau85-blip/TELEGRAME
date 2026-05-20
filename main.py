#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
InvestBot — Bot Telegram d'investissement personnel
Démarrage : python main.py
"""

# ============================================================
# SECTION 1 — IMPORTS & CONFIG
# ============================================================

import asyncio
import json
import logging
import os
import time
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Optional

import xml.etree.ElementTree as ET

import anthropic
import requests
import yfinance as yf
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv
from telegram import Bot, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# Chargement des variables d'environnement
load_dotenv()

# --- Paramètres de configuration ---
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID: int = int(os.getenv("CHAT_ID", "0"))
ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
TIMEZONE: str = os.getenv("TIMEZONE", "Europe/Paris")
DIGEST_HOUR: int = int(os.getenv("DIGEST_HOUR", "8"))
DIGEST_MINUTE: int = int(os.getenv("DIGEST_MINUTE", "0"))
ALERT_CHECK_INTERVAL: int = int(os.getenv("ALERT_CHECK_INTERVAL_MINUTES", "30"))
DEBUG: bool = os.getenv("DEBUG", "false").lower() == "true"

# --- Chemins des fichiers de stockage ---
ALERTES_FILE = Path("alertes.json")
WATCHLIST_FILE = Path("watchlist.json")

# --- Tickers préconfigurés ---
DEFAULT_WATCHLIST = ["CW8.PA", "SPY", "^FCHI"]
TICKERS_FR = {
    "CW8": "CW8.PA",
    "CAC40": "^FCHI",
    "CAC": "^FCHI",
    "SP500": "SPY",
    "OR": "GLD",
    "GOLD": "GLD",
    "EURUSD": "EURUSD=X",
}

# --- Sources RSS ---
RSS_SOURCES = [
    {"name": "Les Échos", "url": "https://www.lesechos.fr/rss/rss_finance.xml"},
    {"name": "BFM Bourse", "url": "https://www.bfmtv.com/rss/economie/"},
    {"name": "Reuters FR", "url": "https://feeds.reuters.com/reuters/frBusinessNews"},
    {"name": "Investing.com", "url": "https://fr.investing.com/rss/news.rss"},
]

# --- Configuration du logging ---
log_level = logging.DEBUG if DEBUG else logging.INFO
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=log_level,
)
logger = logging.getLogger("InvestBot")

# Validation de la configuration au démarrage
if not TELEGRAM_BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN manquant dans le fichier .env")
if not CHAT_ID:
    raise ValueError("CHAT_ID manquant dans le fichier .env")
if not ANTHROPIC_API_KEY:
    logger.warning("ANTHROPIC_API_KEY manquant — la commande /analyse sera indisponible")


# ============================================================
# SECTION 2 — SERVICES (market, rss, claude_ai, storage)
# ============================================================

# ---- Stockage JSON ----

def load_json(path: Path, default) -> dict | list:
    """Charge un fichier JSON ou retourne la valeur par défaut."""
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.error("Erreur lecture %s : %s", path, e)
    return default


def save_json(path: Path, data: dict | list) -> None:
    """Sauvegarde des données en JSON."""
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except IOError as e:
        logger.error("Erreur écriture %s : %s", path, e)


def load_watchlist() -> list[str]:
    return load_json(WATCHLIST_FILE, list(DEFAULT_WATCHLIST))


def save_watchlist(wl: list[str]) -> None:
    save_json(WATCHLIST_FILE, wl)


def load_alertes() -> list[dict]:
    return load_json(ALERTES_FILE, [])


def save_alertes(alertes: list[dict]) -> None:
    save_json(ALERTES_FILE, alertes)


def init_storage() -> None:
    """Crée les fichiers de stockage s'ils n'existent pas."""
    if not WATCHLIST_FILE.exists():
        save_watchlist(list(DEFAULT_WATCHLIST))
        logger.info("watchlist.json initialisé avec les valeurs par défaut")
    if not ALERTES_FILE.exists():
        save_alertes([])
        logger.info("alertes.json initialisé vide")


# ---- Service marché (yfinance) ----

def resolve_ticker(ticker: str) -> str:
    """Résout les alias FR vers les symboles yfinance."""
    return TICKERS_FR.get(ticker.upper(), ticker.upper())


async def fetch_quote(ticker: str) -> Optional[dict]:
    """
    Récupère les données de cours pour un ticker via yfinance.
    Retente une fois après 3 secondes en cas d'échec.
    """
    ticker = resolve_ticker(ticker)

    for attempt in range(2):
        try:
            loop = asyncio.get_event_loop()
            info = await loop.run_in_executor(None, lambda: yf.Ticker(ticker).info)

            # Détection ticker invalide
            if not info or info.get("regularMarketPrice") is None:
                # Parfois le prix est dans "currentPrice"
                price = info.get("currentPrice") or info.get("navPrice")
                if not price:
                    return None
            else:
                price = info.get("regularMarketPrice")

            prev_close = info.get("regularMarketPreviousClose") or info.get("previousClose", 0)
            change = price - prev_close if prev_close else 0
            change_pct = (change / prev_close * 100) if prev_close else 0

            return {
                "ticker": ticker,
                "name": info.get("longName") or info.get("shortName", ticker),
                "price": price,
                "currency": info.get("currency", ""),
                "change": change,
                "change_pct": change_pct,
                "volume": info.get("regularMarketVolume") or info.get("volume", 0),
                "week52_high": info.get("fiftyTwoWeekHigh"),
                "week52_low": info.get("fiftyTwoWeekLow"),
                "market_cap": info.get("marketCap"),
                "pe_ratio": info.get("trailingPE"),
                "eps": info.get("trailingEps"),
                "beta": info.get("beta"),
                "dividend_yield": info.get("dividendYield"),
                "debt_equity": info.get("debtToEquity"),
                "revenue": info.get("totalRevenue"),
            }
        except Exception as e:
            logger.warning("Tentative %d/2 échouée pour %s : %s", attempt + 1, ticker, e)
            if attempt == 0:
                await asyncio.sleep(3)

    return None


def format_quote(data: dict) -> str:
    """Formate un cours boursier en HTML pour Telegram."""
    arrow = "📈" if data["change"] >= 0 else "📉"
    sign = "+" if data["change"] >= 0 else ""
    currency = data["currency"] or ""

    lines = [
        f"{arrow} <b>{data['name']}</b> (<code>{data['ticker']}</code>)",
        f"Prix : <b>{data['price']:.2f} {currency}</b>",
        f"Variation : <b>{sign}{data['change']:.2f} {currency} ({sign}{data['change_pct']:.2f}%)</b>",
    ]

    if data.get("volume"):
        vol = data["volume"]
        vol_str = f"{vol/1_000_000:.1f}M" if vol >= 1_000_000 else f"{vol:,}"
        lines.append(f"Volume : {vol_str}")

    if data.get("week52_high") and data.get("week52_low"):
        lines.append(
            f"52 sem. : {data['week52_low']:.2f} — {data['week52_high']:.2f} {currency}"
        )

    if data.get("market_cap"):
        mc = data["market_cap"]
        mc_str = f"{mc/1_000_000_000:.1f}Md" if mc >= 1_000_000_000 else f"{mc/1_000_000:.0f}M"
        lines.append(f"Capitalisation : {mc_str} {currency}")

    return "\n".join(lines)


# ---- Service RSS ----

def _parse_rss(url: str, source_name: str, keyword: Optional[str]) -> list[dict]:
    """Parse un flux RSS via requests + xml.etree.ElementTree (sans feedparser)."""
    results = []
    try:
        resp = requests.get(url, timeout=10, headers={"User-Agent": "InvestBot/1.0"})
        resp.raise_for_status()
        root = ET.fromstring(resp.content)

        # Namespaces courants dans les flux RSS/Atom
        ns = {
            "atom": "http://www.w3.org/2005/Atom",
            "media": "http://search.yahoo.com/mrss/",
        }

        # Format RSS 2.0
        items = root.findall(".//item")
        # Format Atom
        if not items:
            items = root.findall(".//atom:entry", ns)

        for item in items[:10]:
            title_el = item.find("title") or item.find("atom:title", ns)
            link_el = item.find("link") or item.find("atom:link", ns)
            pub_el = item.find("pubDate") or item.find("published") or item.find("atom:published", ns)

            title = (title_el.text or "").strip() if title_el is not None else ""
            # Pour Atom, le lien est dans l'attribut href
            if link_el is not None:
                link = link_el.get("href") or (link_el.text or "").strip()
            else:
                link = ""
            published = (pub_el.text or "").strip()[:16] if pub_el is not None else ""

            if not title:
                continue
            if keyword and keyword.lower() not in title.lower():
                continue

            results.append({
                "title": title,
                "link": link,
                "published": published,
                "source": source_name,
            })
    except Exception as e:
        logger.warning("Erreur RSS %s : %s", source_name, e)

    return results


async def fetch_news(keyword: Optional[str] = None, max_items: int = 5) -> list[dict]:
    """Agrège les news des 4 flux RSS, filtre par mot-clé si fourni."""
    articles = []
    loop = asyncio.get_event_loop()

    for source in RSS_SOURCES:
        items = await loop.run_in_executor(
            None, lambda s=source: _parse_rss(s["url"], s["name"], keyword)
        )
        articles.extend(items)

    # Déduplication basique par titre
    seen = set()
    unique = []
    for a in articles:
        if a["title"] not in seen:
            seen.add(a["title"])
            unique.append(a)

    return unique[:max_items]


def format_news(articles: list[dict]) -> str:
    """Formate une liste d'articles en HTML pour Telegram."""
    if not articles:
        return "Aucune actualité trouvée."

    lines = ["🗞️ <b>Actualités économiques</b>\n"]
    for i, a in enumerate(articles, 1):
        title = a["title"][:120] + "…" if len(a["title"]) > 120 else a["title"]
        lines.append(
            f"{i}. <a href=\"{a['link']}\">{title}</a>\n"
            f"   <i>{a['source']}</i> — {a.get('published', '')[:16]}"
        )
    return "\n\n".join(lines)


# ---- Service Claude AI ----

async def analyse_ticker_ai(ticker: str) -> str:
    """Analyse complète d'un ticker via Claude Sonnet."""
    if not ANTHROPIC_API_KEY:
        return "❌ Clé ANTHROPIC_API_KEY non configurée dans le .env"

    ticker = resolve_ticker(ticker)

    # 1. Récupération des données fondamentales
    try:
        loop = asyncio.get_event_loop()
        yf_ticker = yf.Ticker(ticker)
        info = await loop.run_in_executor(None, lambda: yf_ticker.info)
        news_raw = await loop.run_in_executor(None, lambda: yf_ticker.news)
    except Exception as e:
        return f"❌ Impossible de récupérer les données pour <code>{ticker}</code> : {e}"

    if not info or not info.get("regularMarketPrice"):
        return f"❌ Ticker <code>{ticker}</code> introuvable ou sans données."

    price = info.get("regularMarketPrice") or info.get("currentPrice", "N/A")
    currency = info.get("currency", "")
    pe = info.get("trailingPE", "N/A")
    eps = info.get("trailingEps", "N/A")
    beta = info.get("beta", "N/A")
    mc = info.get("marketCap", 0)
    mc_str = f"{mc/1_000_000_000:.1f}Md {currency}" if mc else "N/A"
    div_yield = info.get("dividendYield")
    div_str = f"{div_yield*100:.2f}%" if div_yield else "Aucun"
    debt_eq = info.get("debtToEquity", "N/A")
    revenue = info.get("totalRevenue", 0)
    rev_str = f"{revenue/1_000_000_000:.1f}Md {currency}" if revenue else "N/A"
    name = info.get("longName") or info.get("shortName", ticker)
    sector = info.get("sector", "N/A")
    industry = info.get("industry", "N/A")

    # 2. Dernières actualités (3 max)
    news_lines = []
    for item in (news_raw or [])[:3]:
        news_lines.append(f"- {item.get('title', 'Sans titre')}")
    news_text = "\n".join(news_lines) if news_lines else "Aucune actualité récente disponible."

    # 3. Construction du prompt
    donnees = f"""
Ticker : {ticker}
Nom : {name}
Secteur : {sector} / {industry}
Prix actuel : {price} {currency}
P/E ratio : {pe}
EPS : {eps}
Beta : {beta}
Capitalisation : {mc_str}
Dividende : {div_str}
Dette/Equity : {debt_eq}
Chiffre d'affaires : {rev_str}

Actualités récentes :
{news_text}
"""

    system_prompt = (
        "Tu es un conseiller financier expert français. "
        "Analyse cette valeur pour un investisseur particulier français : "
        "20 ans, PEA ouvert, ETF CW8 en position principale, horizon long terme 10+ ans, "
        "profil croissance, également investi en crowdfunding immobilier (Bricks). "
        "Sois concis, factuel, en français. "
        "Termine toujours par un bloc VERDICT avec : "
        "Opportunité (1-5 ⭐), Risque (Faible/Moyen/Élevé), "
        "Adapté PEA (Oui/Non/Partiellement), Complémentaire CW8 (Oui/Non)."
    )

    user_prompt = f"Voici les données de la valeur à analyser :\n{donnees}"

    # 4. Appel Claude
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        loop = asyncio.get_event_loop()
        message = await loop.run_in_executor(
            None,
            lambda: client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1024,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            ),
        )
        raw_analysis = message.content[0].text
    except Exception as e:
        logger.error("Erreur Claude AI : %s", e)
        return f"❌ Erreur lors de l'analyse IA : {e}"

    # 5. Formatage de la réponse
    header = (
        f"📊 <b>Analyse {ticker}</b>\n\n"
        f"<b>Données clés</b>\n"
        f"Prix : {price} {currency} | P/E : {pe} | Beta : {beta} | MarketCap : {mc_str}\n\n"
    )

    full_response = header + raw_analysis
    # Troncature si nécessaire
    if len(full_response) > 4000:
        full_response = full_response[:3990] + "\n<i>[Message tronqué]</i>"

    return full_response


# ============================================================
# SECTION 3 — HANDLERS (cours, watchlist, alertes, news, analyse)
# ============================================================

# ---- Décorateur de sécurité ----

def authorized_only(func):
    """Refuse silencieusement toute interaction hors CHAT_ID autorisé."""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id if update.effective_user else None
        if user_id != CHAT_ID:
            logger.warning(
                "Accès refusé pour user_id=%s (autorisé : %s)", user_id, CHAT_ID
            )
            return  # Ignore silencieusement
        return await func(update, context, *args, **kwargs)
    return wrapper


# ---- Utilitaire d'envoi ----

async def send_message(bot: Bot, text: str, chat_id: int = None) -> None:
    """Envoie un message en HTML, tronqué si > 4096 caractères."""
    target = chat_id or CHAT_ID
    if len(text) > 4096:
        text = text[:4080] + "\n<i>[Message tronqué — 4096 car. max]</i>"
    await bot.send_message(chat_id=target, text=text, parse_mode=ParseMode.HTML)


# ---- /start ----

@authorized_only
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = (
        "👋 <b>Bienvenue sur InvestBot !</b>\n\n"
        "Je suis votre assistant personnel d'investissement.\n\n"
        "<b>Commandes disponibles :</b>\n\n"
        "📈 <b>Cours</b>\n"
        "  /cours [TICKER] — Cours d'une action/ETF\n"
        "  /cours AAPL MSFT CW8 — Plusieurs tickers\n\n"
        "📋 <b>Watchlist</b>\n"
        "  /watchlist show — Afficher ma watchlist\n"
        "  /watchlist add [TICKER] — Ajouter un ticker\n"
        "  /watchlist remove [TICKER] — Retirer un ticker\n\n"
        "🔔 <b>Alertes prix</b>\n"
        "  /alerte add [TICKER] [&gt;/&lt;] [PRIX] — Créer une alerte\n"
        "  /alerte list — Lister mes alertes\n"
        "  /alerte remove [ID] — Supprimer une alerte\n\n"
        "🗞️ <b>Actualités</b>\n"
        "  /news — Top 5 news économiques\n"
        "  /news [mot-clé] — Filtrer les news\n\n"
        "🤖 <b>Analyse IA</b>\n"
        "  /analyse [TICKER] — Analyse complète via Claude AI\n\n"
        "❓ /help — Aide détaillée"
    )
    await send_message(update.get_bot(), msg)


# ---- /help ----

@authorized_only
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = (
        "📖 <b>Aide InvestBot</b>\n\n"
        "<b>/cours [TICKER(S)]</b>\n"
        "Affiche le cours actuel, variation, volume et 52 semaines.\n"
        "Alias disponibles : CW8, CAC40, SP500, OR, EURUSD\n"
        "Exemple : <code>/cours CW8 AAPL MSFT</code>\n\n"
        "<b>/watchlist show|add|remove</b>\n"
        "Gère votre liste de suivi personnelle.\n"
        "Exemple : <code>/watchlist add NVDA</code>\n\n"
        "<b>/alerte add [TICKER] [&gt;/&lt;] [PRIX]</b>\n"
        "Crée une alerte déclenchée quand le prix dépasse ou passe sous le seuil.\n"
        "Exemple : <code>/alerte add CW8 &gt; 500</code>\n\n"
        "<b>/alerte list</b> — Liste toutes vos alertes actives avec leur ID.\n\n"
        "<b>/alerte remove [ID]</b> — Supprime l'alerte numéro ID.\n\n"
        "<b>/news [mot-clé]</b>\n"
        "Récupère les dernières actualités économiques (Les Échos, BFM, Reuters…)\n"
        "Exemple : <code>/news inflation</code>\n\n"
        "<b>/analyse [TICKER]</b>\n"
        "Analyse fondamentale + actualités + avis IA (Claude Sonnet).\n"
        "Exemple : <code>/analyse LVMH</code>\n\n"
        "<i>Un digest automatique est envoyé chaque matin à 8h00.</i>"
    )
    await send_message(update.get_bot(), msg)


# ---- /cours ----

@authorized_only
async def cmd_cours(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args:
        await send_message(
            update.get_bot(),
            "Usage : <code>/cours [TICKER]</code> ou <code>/cours AAPL MSFT CW8</code>"
        )
        return

    parts = []
    for ticker in args:
        data = await fetch_quote(ticker)
        if data:
            parts.append(format_quote(data))
        else:
            parts.append(f"❌ Ticker <code>{ticker.upper()}</code> introuvable.")

    await send_message(update.get_bot(), "\n\n".join(parts))


# ---- /watchlist ----

@authorized_only
async def cmd_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    bot = update.get_bot()

    if not args:
        await send_message(
            bot,
            "Usage : <code>/watchlist show</code> | "
            "<code>/watchlist add TICKER</code> | "
            "<code>/watchlist remove TICKER</code>"
        )
        return

    action = args[0].lower()
    wl = load_watchlist()

    if action == "show":
        if not wl:
            await send_message(bot, "Votre watchlist est vide.")
            return
        await send_message(bot, "⏳ Récupération des cours en cours…")
        parts = [f"📋 <b>Watchlist ({len(wl)} valeurs)</b>\n"]
        for ticker in wl:
            data = await fetch_quote(ticker)
            if data:
                parts.append(format_quote(data))
            else:
                parts.append(f"❌ <code>{ticker}</code> — données indisponibles")
        await send_message(bot, "\n\n".join(parts))

    elif action == "add":
        if len(args) < 2:
            await send_message(bot, "Usage : <code>/watchlist add TICKER</code>")
            return
        ticker = resolve_ticker(args[1])
        if ticker in wl:
            await send_message(bot, f"<code>{ticker}</code> est déjà dans votre watchlist.")
        else:
            wl.append(ticker)
            save_watchlist(wl)
            await send_message(bot, f"✅ <code>{ticker}</code> ajouté à la watchlist.")

    elif action == "remove":
        if len(args) < 2:
            await send_message(bot, "Usage : <code>/watchlist remove TICKER</code>")
            return
        ticker = resolve_ticker(args[1])
        if ticker not in wl:
            await send_message(bot, f"<code>{ticker}</code> n'est pas dans votre watchlist.")
        else:
            wl.remove(ticker)
            save_watchlist(wl)
            await send_message(bot, f"🗑️ <code>{ticker}</code> retiré de la watchlist.")

    else:
        await send_message(
            bot, "Action inconnue. Utilisez <code>show</code>, <code>add</code> ou <code>remove</code>."
        )


# ---- /alerte ----

@authorized_only
async def cmd_alerte(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    bot = update.get_bot()

    if not args:
        await send_message(
            bot,
            "Usage :\n"
            "  <code>/alerte add TICKER &gt; PRIX</code>\n"
            "  <code>/alerte list</code>\n"
            "  <code>/alerte remove ID</code>"
        )
        return

    action = args[0].lower()
    alertes = load_alertes()

    if action == "add":
        # Attendu : /alerte add CW8 > 500
        if len(args) < 4:
            await send_message(
                bot, "Usage : <code>/alerte add TICKER &gt; PRIX</code>"
            )
            return

        ticker = resolve_ticker(args[1])
        condition = args[2]
        if condition not in (">", "<"):
            await send_message(bot, "La condition doit être <code>&gt;</code> ou <code>&lt;</code>.")
            return

        try:
            price = float(args[3])
        except ValueError:
            await send_message(bot, "Le prix doit être un nombre valide.")
            return

        new_id = max((a["id"] for a in alertes), default=0) + 1
        alerte = {
            "id": new_id,
            "ticker": ticker,
            "condition": condition,
            "price": price,
            "active": True,
            "created_at": datetime.now().isoformat(),
        }
        alertes.append(alerte)
        save_alertes(alertes)

        symbol = "&gt;" if condition == ">" else "&lt;"
        await send_message(
            bot,
            f"🔔 Alerte #{new_id} créée : <code>{ticker}</code> {symbol} <b>{price}</b>"
        )

    elif action == "list":
        actives = [a for a in alertes if a.get("active", True)]
        if not actives:
            await send_message(bot, "Aucune alerte active.")
            return

        lines = ["🔔 <b>Alertes actives</b>\n"]
        for a in actives:
            symbol = "&gt;" if a["condition"] == ">" else "&lt;"
            lines.append(
                f"#{a['id']} — <code>{a['ticker']}</code> {symbol} <b>{a['price']}</b>"
            )
        await send_message(bot, "\n".join(lines))

    elif action == "remove":
        if len(args) < 2:
            await send_message(bot, "Usage : <code>/alerte remove ID</code>")
            return

        try:
            alert_id = int(args[1])
        except ValueError:
            await send_message(bot, "L'ID doit être un entier.")
            return

        before = len(alertes)
        alertes = [a for a in alertes if a["id"] != alert_id]
        if len(alertes) == before:
            await send_message(bot, f"Alerte #{alert_id} introuvable.")
        else:
            save_alertes(alertes)
            await send_message(bot, f"🗑️ Alerte #{alert_id} supprimée.")

    else:
        await send_message(
            bot, "Action inconnue. Utilisez <code>add</code>, <code>list</code> ou <code>remove</code>."
        )


# ---- /news ----

@authorized_only
async def cmd_news(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyword = " ".join(context.args) if context.args else None
    bot = update.get_bot()

    await send_message(bot, "⏳ Récupération des actualités…")
    articles = await fetch_news(keyword=keyword, max_items=5)
    await send_message(bot, format_news(articles))


# ---- /analyse ----

@authorized_only
async def cmd_analyse(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    bot = update.get_bot()

    if not args:
        await send_message(bot, "Usage : <code>/analyse TICKER</code>\nExemple : <code>/analyse LVMH</code>")
        return

    ticker = args[0]
    await send_message(bot, f"⏳ Analyse de <code>{ticker}</code> en cours… (peut prendre 15-20s)")

    result = await analyse_ticker_ai(ticker)
    await send_message(bot, result)


# ============================================================
# SECTION 4 — SCHEDULER JOBS
# ============================================================

async def job_check_alertes(bot: Bot) -> None:
    """Vérifie les alertes prix actives et envoie une notification si déclenchée."""
    alertes = load_alertes()
    actives = [a for a in alertes if a.get("active", True)]

    if not actives:
        return

    triggered = []
    for alerte in actives:
        try:
            data = await fetch_quote(alerte["ticker"])
            if not data:
                continue

            prix_actuel = data["price"]
            seuil = alerte["price"]
            condition = alerte["condition"]

            if (condition == ">" and prix_actuel > seuil) or \
               (condition == "<" and prix_actuel < seuil):
                triggered.append((alerte, prix_actuel))
                # Désactive l'alerte après déclenchement
                alerte["active"] = False

        except Exception as e:
            logger.error("Erreur vérification alerte #%s : %s", alerte["id"], e)

    if triggered:
        save_alertes(alertes)
        for alerte, prix_actuel in triggered:
            symbol = "&gt;" if alerte["condition"] == ">" else "&lt;"
            msg = (
                f"🚨 <b>Alerte déclenchée !</b>\n\n"
                f"<code>{alerte['ticker']}</code> {symbol} {alerte['price']}\n"
                f"Prix actuel : <b>{prix_actuel:.2f}</b>\n"
                f"(Alerte #{alerte['id']})"
            )
            await send_message(bot, msg)
            logger.info("Alerte #%s déclenchée pour %s", alerte["id"], alerte["ticker"])


async def job_digest_quotidien(bot: Bot) -> None:
    """Envoie le digest quotidien du matin : news + marchés + watchlist."""
    logger.info("Envoi du digest quotidien")
    now = datetime.now().strftime("%A %d %B %Y")
    parts = [f"☀️ <b>Digest du matin — {now}</b>\n"]

    # 1. Top 5 news
    try:
        articles = await fetch_news(max_items=5)
        parts.append("🗞️ <b>Actualités du matin</b>\n")
        for i, a in enumerate(articles, 1):
            title = a["title"][:100] + "…" if len(a["title"]) > 100 else a["title"]
            parts.append(f"{i}. <a href=\"{a['link']}\">{title}</a> — <i>{a['source']}</i>")
    except Exception as e:
        logger.error("Erreur news digest : %s", e)
        parts.append("🗞️ Actualités indisponibles")

    parts.append("")

    # 2. Marchés principaux
    marches = ["^FCHI", "SPY", "QQQ", "GLD", "EURUSD=X"]
    marches_lines = ["📊 <b>Marchés</b>\n"]
    for ticker in marches:
        data = await fetch_quote(ticker)
        if data:
            arrow = "📈" if data["change"] >= 0 else "📉"
            sign = "+" if data["change"] >= 0 else ""
            name = data["name"][:25]
            marches_lines.append(
                f"{arrow} {name} : <b>{data['price']:.2f}</b> ({sign}{data['change_pct']:.2f}%)"
            )
        else:
            marches_lines.append(f"❓ {ticker} indisponible")
    parts.append("\n".join(marches_lines))
    parts.append("")

    # 3. Watchlist
    wl = load_watchlist()
    wl_lines = ["📋 <b>Watchlist</b>\n"]
    for ticker in wl:
        data = await fetch_quote(ticker)
        if data:
            arrow = "📈" if data["change"] >= 0 else "📉"
            sign = "+" if data["change"] >= 0 else ""
            name = data["name"][:25]
            wl_lines.append(
                f"{arrow} {name} : <b>{data['price']:.2f}</b> ({sign}{data['change_pct']:.2f}%)"
            )
        else:
            wl_lines.append(f"❓ {ticker} indisponible")
    parts.append("\n".join(wl_lines))

    full_msg = "\n".join(parts)
    await send_message(bot, full_msg)
    logger.info("Digest quotidien envoyé")


# ============================================================
# SECTION 5 — MAIN & BOT SETUP
# ============================================================

def setup_scheduler(application: Application) -> AsyncIOScheduler:
    """Configure et démarre le planificateur APScheduler."""
    scheduler = AsyncIOScheduler(timezone=TIMEZONE)

    # Job digest quotidien (heure configurable via .env)
    scheduler.add_job(
        func=lambda: asyncio.ensure_future(job_digest_quotidien(application.bot)),
        trigger=CronTrigger(hour=DIGEST_HOUR, minute=DIGEST_MINUTE, timezone=TIMEZONE),
        id="digest_quotidien",
        name="Digest quotidien",
        replace_existing=True,
    )
    logger.info("Digest quotidien planifié à %02d:%02d (%s)", DIGEST_HOUR, DIGEST_MINUTE, TIMEZONE)

    # Job vérification des alertes
    scheduler.add_job(
        func=lambda: asyncio.ensure_future(job_check_alertes(application.bot)),
        trigger="interval",
        minutes=ALERT_CHECK_INTERVAL,
        id="check_alertes",
        name="Vérification alertes",
        replace_existing=True,
    )
    logger.info("Vérification alertes planifiée toutes les %d minutes", ALERT_CHECK_INTERVAL)

    scheduler.start()
    return scheduler


async def post_init(application: Application) -> None:
    """Callback appelé après l'initialisation du bot."""
    init_storage()
    setup_scheduler(application)
    logger.info("InvestBot démarré. CHAT_ID autorisé : %s", CHAT_ID)

    # Message de démarrage
    try:
        await send_message(
            application.bot,
            "🤖 <b>InvestBot démarré !</b>\n"
            f"Digest quotidien à <b>{DIGEST_HOUR:02d}h{DIGEST_MINUTE:02d}</b> ({TIMEZONE})\n"
            f"Vérification alertes toutes les <b>{ALERT_CHECK_INTERVAL} min</b>\n\n"
            "Tapez /start pour voir les commandes disponibles."
        )
    except Exception as e:
        logger.warning("Impossible d'envoyer le message de démarrage : %s", e)


def main() -> None:
    """Point d'entrée principal du bot."""
    logger.info("Démarrage d'InvestBot…")

    # Construction de l'application
    application = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # Enregistrement des handlers de commandes
    handlers = [
        CommandHandler("start", cmd_start),
        CommandHandler("help", cmd_help),
        CommandHandler("cours", cmd_cours),
        CommandHandler("watchlist", cmd_watchlist),
        CommandHandler("alerte", cmd_alerte),
        CommandHandler("news", cmd_news),
        CommandHandler("analyse", cmd_analyse),
    ]
    for handler in handlers:
        application.add_handler(handler)

    logger.info("Handlers enregistrés : %d commandes", len(handlers))

    # Démarrage du polling
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
