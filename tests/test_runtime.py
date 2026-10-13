import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone

import test_trading
from test_trading import bot
import healthcheck
import preflight
import pandas as pd
import numpy as np


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.control = Path(self.temp.name) / 'bot_control.json'
        patcher = patch.object(bot, 'BOT_CONTROL_FILE', self.control)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_position_normalization_does_not_need_market_bars(self):
        with patch.object(bot, 't212_get', return_value={'quantity':2, 'averagePrice':100,'currentPrice':102,'ppl':4}):
            position = bot.get_position('PHAU')
        self.assertEqual(position['qty'], 2)
        self.assertEqual(position['unrealized_plpc'], .02)

    def test_real_signal_generation_contains_adaptive_assessment(self):
        close = 100 + np.linspace(0, 6, 100) + np.sin(np.arange(100))*.4
        bars = pd.DataFrame({'Close':close,'High':close+.5,'Low':close-.5,'Volume':1000},
                            index=pd.date_range('2026-01-01',periods=100,freq='15min'))
        with patch.object(bot,'fetch_ohlcv',return_value=bars),patch.object(bot,'_fetch_daily_trend',return_value='bullish'):
            signal = bot.get_signals('PHAU')
            self.assertIn('quality', signal['adaptive'])
            # Current unfinished bar must not affect decisions.
            bars.iloc[-1, bars.columns.get_loc('Close')] = 100000
            self.assertEqual(signal, bot.get_signals('PHAU'))

    def test_uncertain_order_blocks_next_cycle_and_restart(self):
        with patch.object(bot,'_t212_request',return_value=bot._API_ERROR) as request:
            self.assertIs(bot.t212_post('/orders',{'ticker':'PHAUl_EQ'}),bot._API_ERROR)
            self.assertIn('order_uncertain',json.loads(self.control.read_text()))
            self.assertIs(bot.t212_post('/orders',{'ticker':'PHAUl_EQ'}),bot._API_ERROR)
            self.assertEqual(request.call_count,1)

    def test_state_saved_before_request(self):
        with patch.object(bot,'save_bot_control',side_effect=OSError),patch.object(bot,'_t212_request') as request:
            with self.assertRaises(OSError):
                bot.t212_post('/orders',{})
            request.assert_not_called()

    def test_corrupt_state_never_defaults_to_empty_portfolio(self):
        self.control.write_text('{broken')
        with self.assertRaises(RuntimeError):
            bot.load_bot_control()

    def test_rejected_buy_does_not_increase_position_count(self):
        with patch.object(bot,'clear_position_meta'),patch.object(bot,'should_buy',return_value=True),\
             patch.object(bot,'check_correlation_allowed',return_value=(True,'')),\
             patch.object(bot,'_execute_buy',return_value=False):
            delta=bot._handle_asset('PHAU',{},None,{},1000,10000,0,'DEMO',0)
            self.assertEqual(delta,0)

    def test_rejected_sell_does_not_free_position_slot(self):
        with patch.object(bot,'update_max_price',return_value=100),\
             patch.object(bot,'check_trailing_stop',return_value=False),\
             patch.object(bot,'check_stop_loss',return_value=True),\
             patch.object(bot,'_execute_stop_loss',return_value=False):
            self.assertEqual(bot._handle_asset('PHAU',{}, {'current_price':90},{},0,0,0,'DEMO',1),0)

    def test_trailing_remains_armed_after_gain_drops_below_activation(self):
        with patch.object(bot,'load_positions_meta',return_value={'PHAU':{'max_price':105}}):
            self.assertTrue(bot.check_trailing_stop({'current_price':101,'avg_entry_price':100,'unrealized_plpc':.01},'PHAU'))

    def test_healthcheck_rejects_failed_stale_and_missing_heartbeat(self):
        path=Path(self.temp.name)/'heartbeat.json'
        self.assertFalse(healthcheck.healthy(path))
        now=datetime.now(timezone.utc)
        for status,age,expected in [('cycle_completed',0,True),('cycle_failed',0,False),('cycle_completed',301,False)]:
            path.write_text(json.dumps({'status':status,'updated_at':(now-timedelta(seconds=age)).isoformat()}))
            self.assertEqual(healthcheck.healthy(path,now),expected)

    def test_preflight_refuses_live_and_forced_trade(self):
        for values in ({'T212_DEMO':'false'},{'T212_DEMO':'true','TEST_TRADE':'true'}):
            with patch.dict(os.environ,values),self.assertRaises(ValueError):
                preflight.check_environment()

    def test_worker_process_lock_heartbeat_and_clean_shutdown(self):
        import subprocess
        import sys
        import time
        state = Path(self.temp.name) / 'worker'
        env = dict(os.environ, BOT_STATE_DIR=str(state), T212_DEMO='true', TEST_TRADE='false')
        # Run the real entrypoint/loop but substitute the network-bearing cycle.
        code = 'import continuous; continuous.cycle = lambda bot: None; continuous.main()'
        process = subprocess.Popen([sys.executable, '-c', code], env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 10
            while not (state/'heartbeat.json').exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertTrue(healthcheck.healthy(state/'heartbeat.json'))
            duplicate = subprocess.run([sys.executable, '-c', code], env=env, capture_output=True, timeout=10)
            self.assertNotEqual(duplicate.returncode, 0)
            self.assertIn('Un worker', duplicate.stderr.decode())
            process.terminate()
            process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0)
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate()
