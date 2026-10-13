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

## Hébergement

Pour le worker de trading continu, suivre [DEPLOYMENT.md](DEPLOYMENT.md).
Un hébergement permanent est nécessaire ; vérifier le tarif du fournisseur choisi.
Les étapes Railway ci-dessous décrivent le bot de consultation historique.

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

## Worker de trading continu

Le guide complet de déploiement est dans [DEPLOYMENT.md](DEPLOYMENT.md).

Le `Procfile` lance désormais `python continuous.py` (trading), et non `main.py`
(bot de consultation). Python 3.11+ sur Linux est requis. Pour démarrer :

```bash
source .venv/bin/activate
python continuous.py
```

Configurer de manière sécurisée `T212_API_KEY`, `T212_API_KEY_ID`,
`TELEGRAM_BOT_TOKEN`, `CHAT_ID` et `T212_DEMO=true`. Ne pas lancer en parallèle
le workflow GitHub Actions, `run_once.py`, `trading_bot.py` ou `main.py` avec le
même compte/token : commandes consommées par plusieurs processus et ordres
concurrents possibles. Le verrou protège seulement les workers partageant le
même dossier sur la même machine.

Le worker vérifie les commandes toutes les minutes et exécute la stratégie
pendant les heures de marché définies dans `is_market_open`. Il reste actif
hors marché, sans acheter. Une exception de cycle est journalisée par son type
et le prochain cycle est tenté. `data/heartbeat.json` indique la fin du dernier
cycle et les exceptions ; `cycle_completed` ne prouve ni la connectivité du
broker ni l'exécution d'ordres. Arrêt propre avec SIGTERM/SIGINT.

L'état est conservé dans `data/` par défaut ; définir `BOT_STATE_DIR` vers un
volume persistant chez l'hébergeur. Avant migration, arrêter les anciens jobs
et copier leurs fichiers d'état JSON et `decisions.log` dans ce volume : ne pas
redémarrer avec un historique vide si des positions sont ouvertes. Configurer
un seul worker, un redémarrage automatique après crash et une surveillance du
heartbeat chez l'hébergeur. Un environnement de développement cloud ne garantit
pas un hébergement permanent ; les processus ne survivent pas à sa fermeture.

### Stratégie adaptative expérimentale

`strategy.adaptive_demo: true` active en démo un classement par efficacité de
tendance et momentum normalisé par volatilité. Les achats requièrent une tendance
journalière haussière, un momentum MACD positif, un RSI entre 40 et 68 et un
régime intraday en tendance. Les marchés sans direction, données invalides et
volatilités extrêmes sont écartés. Le score est une heuristique explicable, pas
une probabilité de gain ni un modèle entraîné. En réel, les conditions d'entrée
historiques restent utilisées. Les sorties et plafonds existants sont conservés.

Le dimensionnement couvre désormais la distance du stop fixe et celle de l'ATR,
sans fallback d'achat sur ATR invalide. Un stop géré par le bot ne garantit pas
le prix de sortie : gaps, indisponibilité réseau et frais peuvent augmenter la
perte. Les POST broker ne sont pas rejoués automatiquement après erreur ambiguë ;
vérifier l'état broker avant toute nouvelle tentative manuelle.

Validation locale : `.venv/bin/python -m unittest discover -s tests -v`.
Les tests n'envoient aucun ordre ni message. Le backtest historique existant utilise
d'autres actifs et règles : il ne valide **pas** cette stratégie adaptative.
Mesurer rendement net, drawdown, rotation et comparaison au buy-and-hold sur
les mêmes actifs, hors échantillon, puis en démo avant d'envisager le réel.
Aucun gain ou avantage de performance n'est établi par les tests unitaires.
