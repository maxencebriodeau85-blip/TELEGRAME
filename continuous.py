"""Continuous worker: serialized cycles, heartbeat, and Linux single-instance lock."""
import fcntl
import json
import logging
import os
from pathlib import Path
import signal
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)


def cycle(bot, now=None):
    bot._config_loaded = False
    bot._config_cache = {}
    bot._FX_CACHE.clear()
    bot.poll_telegram_commands()
    now = now or datetime.now(ZoneInfo("Europe/London"))
    daily = bot.load_daily_pnl()
    if now.weekday() < 5 and (now.hour, now.minute) >= (16, 35) and not daily.get("summary_sent"):
        bot.daily_summary()
        # Reload to preserve changes made by the summary.
        daily = bot.load_daily_pnl()
        daily["summary_sent"] = True
        bot.save_json(bot.DAILY_PNL_FILE, daily)
    bot.run_strategy()  # Existing market, pause and risk checks apply.


def run_loop(bot, stop, heartbeat, interval=60):
    while not stop.is_set():
        started = time.monotonic()
        status = {"updated_at": datetime.now(ZoneInfo("UTC")).isoformat()}
        watchdog = threading.Timer(240, lambda: os._exit(1))
        watchdog.daemon = True
        watchdog.start()
        try:
            cycle(bot)
            status["status"] = "cycle_completed"
        except Exception as exc:
            # Avoid logging exception values, which may contain credentials in URLs.
            logger.error("Cycle échoué (%s), nouvel essai au prochain cycle", type(exc).__name__)
            status.update(status="cycle_failed", error_type=type(exc).__name__)
        finally:
            watchdog.cancel()
        status["updated_at"] = datetime.now(ZoneInfo("UTC")).isoformat()
        tmp = heartbeat.with_suffix(".tmp")
        tmp.write_text(json.dumps(status), encoding="utf-8")
        tmp.replace(heartbeat)
        stop.wait(max(0, interval - (time.monotonic() - started)))


def main():
    from dotenv import load_dotenv
    load_dotenv()
    from preflight import check_environment
    check_environment()
    # Default worker state is separate from versioned GitHub Actions state.
    state = Path(os.getenv("BOT_STATE_DIR", "data"))
    state.mkdir(parents=True, exist_ok=True)
    os.environ["BOT_STATE_DIR"] = str(state.resolve())
    with (state / "worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Un worker utilise déjà ce dossier d'état")
        import trading_bot as bot
        stop = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
        logger.info("Worker continu démarré (%s)", "DEMO" if bot.T212_DEMO else "LIVE")
        run_loop(bot, stop, state / "heartbeat.json")


if __name__ == "__main__":
    main()
