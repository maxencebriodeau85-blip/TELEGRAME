# 🤖 InvestBot — Bot Telegram d'investissement personnel

Bot Telegram personnel pour suivre vos investissements : cours boursiers en temps réel, watchlist, alertes prix, actualités économiques et analyses IA via Claude.

---

## Fonctionnalités

| Commande | Description |
|---|---|
| `/start` | Message de bienvenue et liste des commandes |
| `/help` | Aide détaillée |
| `/cours [TICKER(S)]` | Cours actuel, variation, volume, 52 semaines |
| `/watchlist show\|add\|remove` | Gestion de votre liste de suivi |
| `/alerte add\|list\|remove` | Alertes de prix automatiques |
| `/news [mot-clé]` | Actualités économiques (Les Échos, BFM, Reuters…) |
| `/analyse [TICKER]` | Analyse fondamentale complète via Claude AI |

**Digest automatique** envoyé chaque matin à 8h00 : top 5 news + marchés + watchlist.

---

## Prérequis

- Python 3.11+
- Un compte Telegram et un bot créé via [@BotFather](https://t.me/BotFather)
- Une clé API Anthropic ([console.anthropic.com](https://console.anthropic.com))

---

## ⚠️ Sécurité — À lire impérativement

- **Ne jamais committer le fichier `.env`** — il contient vos tokens secrets
- Le fichier `.gitignore` doit exclure `.env` (vérifié automatiquement)
- Si votre token Telegram est compromis : allez sur [@BotFather](https://t.me/BotFather), utilisez `/revoke` sur votre bot, puis recopiez le nouveau token dans `.env`
- Le bot est verrouillé sur un seul `CHAT_ID` — toute commande d'un autre utilisateur est ignorée silencieusement

---

## Installation

### 1. Cloner le dépôt

```bash
git clone https://github.com/votre-username/investbot.git
cd investbot
```

### 2. Créer un environnement virtuel

```bash
python3.11 -m venv venv
source venv/bin/activate      # Linux/macOS
# ou
venv\Scripts\activate         # Windows
```

### 3. Installer les dépendances

```bash
pip install -r requirements.txt
```

### 4. Configurer le fichier `.env`

```bash
cp .env.example .env
```

Éditez ensuite `.env` avec vos valeurs :

```dotenv
TELEGRAM_BOT_TOKEN=votre_token_botfather_ici
CHAT_ID=2087052883
ANTHROPIC_API_KEY=votre_cle_anthropic_ici
TIMEZONE=Europe/Paris
DIGEST_HOUR=8
DIGEST_MINUTE=0
ALERT_CHECK_INTERVAL_MINUTES=30
DEBUG=false
```

**Comment obtenir votre CHAT_ID ?**
Envoyez un message à [@userinfobot](https://t.me/userinfobot) sur Telegram.

### 5. Lancer le bot

```bash
python main.py
```

---

## Configuration du `.env`

| Variable | Description | Valeur par défaut |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Token fourni par @BotFather | *obligatoire* |
| `CHAT_ID` | Votre identifiant Telegram (seul autorisé) | *obligatoire* |
| `ANTHROPIC_API_KEY` | Clé API pour les analyses IA | *obligatoire pour /analyse* |
| `TIMEZONE` | Fuseau horaire pour le digest | `Europe/Paris` |
| `DIGEST_HOUR` | Heure du digest quotidien | `8` |
| `DIGEST_MINUTE` | Minute du digest quotidien | `0` |
| `ALERT_CHECK_INTERVAL_MINUTES` | Fréquence de vérification des alertes | `30` |
| `DEBUG` | Active les logs détaillés | `false` |

---

## Alias de tickers supportés

| Alias | Ticker réel |
|---|---|
| `CW8` | `CW8.PA` |
| `CAC40` / `CAC` | `^FCHI` |
| `SP500` | `SPY` |
| `OR` / `GOLD` | `GLD` |
| `EURUSD` | `EURUSD=X` |

Tous les tickers Yahoo Finance standard sont également acceptés (`AAPL`, `MSFT`, `LVMH.PA`, etc.)

---

## Déploiement sur Railway (gratuit)

Railway permet d'héberger le bot gratuitement avec 500h/mois sur le plan Starter.

### Étapes

1. Créez un compte sur [railway.app](https://railway.app)
2. Cliquez sur **"New Project"** → **"Deploy from GitHub repo"**
3. Connectez votre dépôt GitHub contenant ce projet
4. Dans l'onglet **Variables**, ajoutez toutes les variables du `.env` :
   - `TELEGRAM_BOT_TOKEN`
   - `CHAT_ID`
   - `ANTHROPIC_API_KEY`
   - `TIMEZONE`
   - `DIGEST_HOUR`
   - `DIGEST_MINUTE`
   - `ALERT_CHECK_INTERVAL_MINUTES`
   - `DEBUG`
5. Railway détecte automatiquement Python et lance `python main.py`

> **Note :** Les fichiers `alertes.json` et `watchlist.json` sont créés automatiquement au démarrage. Sur Railway, ils seront perdus à chaque redéploiement (stockage éphémère). Pour persister les données, envisagez d'utiliser un volume Railway ou une base de données externe.

---

## Structure des fichiers

```
investbot/
├── main.py           # Bot complet en un seul fichier
├── requirements.txt  # Dépendances Python
├── .env.example      # Exemple de configuration (sans valeurs réelles)
├── .env              # Votre configuration (à ne jamais committer !)
├── .gitignore        # Doit inclure .env
├── alertes.json      # Créé automatiquement au démarrage
├── watchlist.json    # Créé automatiquement au démarrage
└── README.md         # Ce fichier
```

---

## Licence

Usage personnel uniquement. Ce bot n'est pas destiné à fournir des conseils financiers professionnels.
