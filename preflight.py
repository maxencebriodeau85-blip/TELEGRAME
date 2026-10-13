"""Read-only validation of demo credentials and external services. Never sends orders/messages."""
import argparse
import base64
import os
import sys

REQUIRED = ('T212_API_KEY', 'T212_API_KEY_ID', 'TELEGRAM_BOT_TOKEN', 'CHAT_ID')


def check_environment():
    missing = [name for name in REQUIRED if not os.getenv(name)]
    if missing:
        raise ValueError('Variables manquantes : ' + ', '.join(missing))
    if os.getenv('T212_DEMO', 'true').lower() != 'true':
        raise ValueError('Ce déploiement nécessite T212_DEMO=true')
    if os.getenv('TEST_TRADE', 'false').lower() != 'false':
        raise ValueError('Ce déploiement nécessite TEST_TRADE=false')
    try:
        chat_id = int(os.environ['CHAT_ID'])
        if chat_id == 0:
            raise ValueError
    except ValueError:
        raise ValueError('CHAT_ID doit être un entier non nul') from None


def check_services():
    import requests
    import yfinance as yf
    token = os.environ['TELEGRAM_BOT_TOKEN']
    auth = base64.b64encode((os.environ['T212_API_KEY_ID'] + ':' + os.environ['T212_API_KEY']).encode()).decode()
    checks = (
        ('Telegram getMe', 'https://api.telegram.org/bot' + token + '/getMe', {}),
        ('Trading 212 compte démo', 'https://demo.trading212.com/api/v0/equity/account/cash',
         {'Authorization': 'Basic ' + auth}),
    )
    success = True
    for label, url, headers in checks:
        try:
            response = requests.get(url, headers=headers, timeout=15)
            data = response.json() if response.ok else {}
            valid = bool(data.get('ok')) if label.startswith('Telegram') else 'total' in data
            ok = response.ok and valid
            print(f'{label}: ' + ('OK' if ok else f'échec HTTP {response.status_code} ou réponse invalide'))
            success &= ok
        except Exception as exc:
            print(f'{label}: {type(exc).__name__}')
            success = False
    try:
        yf.set_tz_cache_location('/tmp/yfinance')
        bars = yf.Ticker('PHAU.L').history(period='5d', interval='15m', raise_errors=True)
        ok = len(bars) >= 61
        print('Yahoo PHAU.L: ' + ('OK' if ok else 'données insuffisantes'))
        success &= ok
    except Exception as exc:
        print('Yahoo PHAU.L: ' + type(exc).__name__)
        success = False
    return success


def main():
    from dotenv import load_dotenv
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--offline', action='store_true', help='Vérifier uniquement la configuration')
    args = parser.parse_args()
    try:
        check_environment()
    except ValueError as exc:
        print(str(exc))
        return 1
    print('Configuration démo : OK')
    return 0 if args.offline or check_services() else 1


if __name__ == '__main__':
    sys.exit(main())
