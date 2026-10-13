# Lancement permanent en démo

Le code est préparé pour **un serveur Linux avec Docker Compose 2.24+**, un seul
worker et un volume persistant. Aucun hébergement n'est créé par ce dépôt.
L'environnement de développement Codex ne constitue pas un hébergement permanent.

## Préparer le serveur

1. Copier la version corrigée du dépôt sur le serveur. Arrêter les anciens bots
   et désactiver le workflow de trading dans GitHub Actions **avant** le lancement.
   La version locale de `.github/workflows/trading_bot.yml` n'exécute que les tests ;
   elle ne remplace le workflow distant qu'après publication sur GitHub.
2. Installer Python 3.12 et créer `.venv`, puis installer `requirements.lock`.
3. Construire l'image :

   ```bash
   python3.12 -m venv .venv
   .venv/bin/python -m pip install -r requirements.lock
   ./scripts/build-image.sh
   ```

   Le script télécharge les wheels depuis PyPI avec vérification TLS puis les
   installe hors réseau dans l'image. Le dossier `.wheelhouse/` est ignoré par Git.
   Préparer les wheels sur Linux, avec la même architecture que le serveur Docker.
   Ne pas réutiliser les wheels d'une autre architecture.
4. Créer un `.env` privé à partir de `.env.example`, permissions `chmod 600 .env`,
   puis remplir `T212_API_KEY`, `T212_API_KEY_ID`, `TELEGRAM_BOT_TOKEN`, `CHAT_ID`.
   Utiliser les clés du **compte démo** Trading 212. Ne jamais les publier dans Git
   ou les transmettre dans la conversation. Anthropic n'est pas requis pour le
   worker de trading. Compose impose `T212_DEMO=true` et `TEST_TRADE=false`.

## Vérifier puis démarrer

```bash
docker compose run --rm trading-bot python preflight.py
```

Cette vérification lit l'identité Telegram, le compte démo Trading 212 et les
bougies Yahoo Finance ; aucun ordre ni message n'est envoyé. Une erreur doit être
résolue avant de poursuivre. Les destinations principales sont `api.telegram.org`,
`demo.trading212.com`, `query1.finance.yahoo.com`, `query2.finance.yahoo.com` et
`fc.yahoo.com` (Yahoo peut aussi utiliser des domaines de consentement).

Si des positions ou un historique existent déjà, migrer **l'état à jour du bot
arrêté** vers le volume `/data` avant le démarrage. Les JSON suivis dans le dépôt
peuvent être périmés : ne pas les copier aveuglément. Préserver `trades.json`,
`daily_pnl.json`, `positions_meta.json`, `risk_stats.json`, `bot_control.json`,
`disabled_assets.json` et `decisions.log`. Le conteneur utilise l'UID/GID 10001.

```bash
docker compose up -d
docker compose ps
docker compose logs --tail=50 trading-bot
```

Envoyer `/status` au bot depuis le compte Telegram autorisé pour vérifier le
fonctionnement connecté. Les ordres sont possibles sur le **compte démo** pendant
les heures de marché. Une session hors marché ne valide pas leur exécution.

Le volume `bot-state` conserve l'état et le conteneur redémarre après un crash.
Un cycle bloqué plus de 240 secondes termine le processus pour permettre son
redémarrage. Les erreurs ordinaires sont réessayées au cycle suivant. Le healthcheck
vérifie un cycle réussi de moins de cinq minutes ; il ne certifie pas que chaque
API est disponible. Docker ne redémarre pas automatiquement un conteneur uniquement
parce qu'il est `unhealthy` : surveiller cet état chez l'hébergeur.

Arrêt : `docker compose down` (conserve le volume). **Ne pas utiliser `down -v`**
pour une simple mise à jour. Ne lancer qu'un worker pour le compte et le token ;
le verrou local ne protège pas contre une autre machine ou GitHub Actions.

## Ordre non confirmé

Si `order_uncertain` apparaît dans `bot_control.json`, tous les POST broker sont
bloqués. Le verrou persiste après redémarrage, même après `/resume`. Arrêter le
worker, vérifier dans Trading 212 les ordres et positions, réconcilier l'état,
puis retirer ce champ uniquement lorsque la situation est établie. Cela évite de
rejouer un ordre dont la réponse a été perdue. Un accusé de réception d'ordre ne
constitue pas une garantie de prix d'exécution ; contrôler les exécutions en démo.

## Ce qui est validé

Les tests hors connexion couvrent les signaux, le dimensionnement, les erreurs
d'ordres, la persistance, le verrou de processus, le heartbeat et l'arrêt propre :

```bash
.venv/bin/python -m unittest discover -s tests -v
docker build --network=none --target test -t telegrame-demo:test .
```

Ils ne démontrent pas la rentabilité. Le backtest historique ne représente pas
la stratégie adaptative actuelle : une comparaison hors échantillon, sur les
mêmes actifs avec frais et glissement, puis une observation en démo, restent
nécessaires avant toute évaluation de performance. Ce déploiement refuse le réel.
