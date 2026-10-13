import os
import unittest
from unittest.mock import patch, Mock
from datetime import datetime
from zoneinfo import ZoneInfo
import tempfile
from pathlib import Path
import threading
import numpy as np
import pandas as pd

# Local fixtures only; no network calls or orders are made by these tests.
os.environ.update(T212_API_KEY='test', T212_API_KEY_ID='test', TELEGRAM_BOT_TOKEN='test', CHAT_ID='1')
import trading_bot as bot
from adaptive_strategy import assess_market
import continuous


class StrategyTests(unittest.TestCase):
    def bars(self, close):
        return pd.DataFrame({'Close':close,'High':close+0.2,'Low':close-0.2},
                            index=pd.date_range('2026-01-01',periods=len(close),freq='15min'))

    def test_regimes_and_corrupt_data(self):
        self.assertTrue(assess_market(self.bars(np.linspace(100,110,80)))['entry_ok'])
        self.assertFalse(assess_market(self.bars(np.full(80,100.)))['entry_ok'])
        self.assertFalse(assess_market(self.bars(np.linspace(110,100,80)))['entry_ok'])
        bars=self.bars(np.linspace(100,110,80));bars.iloc[-1,0]=np.nan
        self.assertFalse(assess_market(bars)['entry_ok'])
        bars=self.bars(np.linspace(100,110,80));bars['High']+=10
        self.assertEqual(assess_market(bars)['regime'],'volatile')

    def test_rsi_rising_market_is_not_neutral(self):
        self.assertEqual(bot.calc_rsi(pd.Series(range(1,100))).iloc[-1],100)
        self.assertEqual(bot.calc_rsi(pd.Series([100.]*100)).iloc[-1],50)

    def test_stop_risk_budget_and_invalid_inputs(self):
        for atr in (.001,1,10):
            amount=bot.calculate_amount_atr('PHAU',10000,10000,atr,100)
            self.assertLessEqual(amount*.03,100)
            self.assertLessEqual(amount,1500)
        for atr in (0,float('nan'),float('inf')):
            self.assertEqual(bot.calculate_amount_atr('PHAU',10000,10000,atr,100),0)

    def test_adaptive_demo_gate(self):
        sig=dict(adaptive={'entry_ok':True},daily_trend='bullish',macd_bullish=True,rsi=60)
        with patch.object(bot,'T212_DEMO',True),patch.object(bot,'load_config',return_value={'strategy':{'adaptive_demo':True}}):
            self.assertTrue(bot.should_buy(sig,False))
            self.assertFalse(bot.should_buy(sig,True))
            sig['daily_trend']='unknown'
            self.assertFalse(bot.should_buy(sig,False))

    def test_cycle_refreshes_cache_and_polls_outside_market_hours(self):
        fake=Mock();fake.load_daily_pnl.return_value={'summary_sent':False}
        continuous.cycle(fake,datetime(2026,10,11,20,tzinfo=ZoneInfo('Europe/London')))
        fake.poll_telegram_commands.assert_called_once()
        fake.run_strategy.assert_called_once()
        fake.daily_summary.assert_not_called()
        self.assertFalse(fake._config_loaded)
        fake._FX_CACHE.clear.assert_called_once()

    def test_post_timeout_is_not_retried(self):
        import requests
        with patch.object(bot.requests,'request',side_effect=requests.Timeout),patch.object(bot.time,'sleep'):
            self.assertIs(bot._t212_request('POST','https://example.invalid',{}),bot._API_ERROR)
            self.assertEqual(bot.requests.request.call_count,1)

    def test_live_order_respects_remaining_capital(self):
        with patch.object(bot,'T212_DEMO',False),patch.object(bot,'TEST_TRADE',False),\
             patch.object(bot,'load_config',return_value={'trading':{'live_capital_limit':500}}),\
             patch.object(bot,'_calibration_factor',return_value=1),\
             patch.object(bot,'place_buy_order',return_value=False) as place:
            bot._execute_buy('PHAU',{'atr':1,'price':100},1000,10000,499,{},'LIVE')
            self.assertEqual(place.call_args.args[1],1)

    def test_adaptive_is_not_enabled_in_live(self):
        sig=dict(symbol='PHAU',adaptive={'entry_ok':True},daily_trend='bullish',
                 macd_bullish=True,volume_ok=True,trend_bullish=True,rsi=60,rsi_buy=48)
        with patch.object(bot,'T212_DEMO',False),patch.object(bot,'load_config',return_value={'strategy':{'adaptive_demo':True}}):
            self.assertFalse(bot.should_buy(sig,False))

    def test_worker_recovers_after_exception(self):
        stop=threading.Event()
        calls=[]
        def tick(_):
            calls.append(1)
            if len(calls)==1: raise RuntimeError('failure')
            stop.set()
        with tempfile.TemporaryDirectory() as d,patch.object(continuous,'cycle',side_effect=tick):
            continuous.run_loop(Mock(),stop,Path(d)/'heartbeat.json',interval=0)
            self.assertEqual(len(calls),2)
            self.assertIn('cycle_completed',(Path(d)/'heartbeat.json').read_text())


if __name__=='__main__': unittest.main()
