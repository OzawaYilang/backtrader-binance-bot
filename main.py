#!/usr/bin/env python3
from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

import time
import backtrader as bt
import datetime as dt
import ccxtbt
import ccxt
import time
from datetime import datetime

from functools import wraps
from ccxtbt import CCXTStore
from config import BINANCE, ENV, PRODUCTION, COIN_TARGET, COIN_REFER, DEBUG

from dataset.dataset import CustomDataset
from sizer.percent import FullMoney
from strategies.basic_rsi import BasicRSI
from utils import print_trade_analysis, print_sqn, send_telegram_message

from backtrader.metabase import MetaParams
from backtrader.utils.py3 import with_metaclass
from ccxt.base.errors import NetworkError, ExchangeError


class CCXTStoreFutures(ccxtbt.CCXTStore):

    def __init__(self, exchange, currency, config, retries, debug=False, sandbox=False):
        super().__init__(exchange, currency, config, retries, debug, sandbox)
        self.exchange = getattr(ccxt, exchange)(config)
        if sandbox:
            self.exchange.set_sandbox_mode(True)
        self.currency = currency
        self.retries = retries
        self.debug = debug
        balance = self.exchange.fapiPrivateV2GetBalance() if 'secret' in config else 0
        try:
            if balance == 0 or not balance['free'][currency]:
                self._cash = 0
            else:
                self._cash = balance['free'][currency]
        except KeyError:  # never funded or eg. all USD exchanged
            self._cash = 0
        try:
            if balance == 0 or not balance['total'][currency]:
                self._value = 0
            else:
                self._value = balance['total'][currency]
        except KeyError:
            self._value = 0

    def get_wallet_balance(self, currency, params=None):
        balance = self.exchange.fapiPrivateV2GetBalance(params)
        return balance

    def get_balance(self):
        balance = self.exchange.fapiPrivateV2GetBalance()

        cash = balance['free'][self.currency]
        value = balance['total'][self.currency]
        # Fix if None is returned
        self._cash = cash if cash else 0
        self._value = value if value else 0

    def create_order(self, symbol, order_type, side, amount, price, params):
        # returns the order
        return self.exchange.fapiPrivatePostOrder(symbol=symbol, type=order_type, side=side,
                                          amount=amount, price=price, params=params)

    def cancel_order(self, order_id, symbol):
        return self.exchange.fapiPrivateDeleteOrder(order_id, symbol)

    def fetch_trades(self, symbol):
        return self.exchange.fapiPrivateGetUserTrades(symbol)

    def fetch_ohlcv(self, symbol, timeframe, since, limit, params={}):
        if self.debug:
            print('Fetching: {}, TF: {}, Since: {}, Limit: {}'.format(symbol, timeframe, since, limit))
        return self.exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=limit, params=params)

    def fetch_order(self, oid, symbol):
        return self.exchange.fetch_order(oid, symbol)

    def fetch_open_orders(self, symbol=None):
        if symbol is None:
            return self.exchange.fetchOpenOrders()
        else:
            return self.exchange.fetchOpenOrders(symbol)


def main():
    cerebro = bt.Cerebro(quicknotify=True)
    ccxtbt.CCXTStore = CCXTStoreFutures
    if ENV == PRODUCTION:  # Live trading with Binance
        broker_config = \
            {
                'apiKey': BINANCE.get("key"),
                'secret': BINANCE.get("secret"),
                'nonce': lambda: str(int(time.time() * 1000)),
                'enableRateLimit': True,
                'hostname': 'fapi.binance.com',
                'rateLimit': 10,
                'timeout': 5000,
                'verbose': 'false'
            }

        store = CCXTStore(exchange='binance', currency=COIN_REFER, config=broker_config, retries=10, debug=DEBUG)

        broker_mapping = \
            {
                'order_types': {
                    bt.Order.Market: 'market',
                    bt.Order.Limit: 'limit',
                    bt.Order.Stop: 'stop-loss',
                    bt.Order.StopLimit: 'stop limit'
                },
                'mappings': {
                    'closed_order': {
                        'key': 'status',
                        'value': 'closed'
                    },
                    'canceled_order': {
                        'key': 'status',
                        'value': 'canceled'
                    }
                }
            }

        broker = store.getbroker(broker_mapping=broker_mapping)
        cerebro.setbroker(broker)

        hist_start_date = dt.datetime.utcnow() - dt.timedelta(minutes=30000)
        data = store.getdata(
            dataname='%s/%s' % (COIN_TARGET, COIN_REFER),
            name='%s%s' % (COIN_TARGET, COIN_REFER),
            timeframe=bt.TimeFrame.Minutes,
            fromdate=hist_start_date,
            compression=30,
            ohlcv_limit=99999
        )

        # Add the feed
        cerebro.adddata(data)

    else:  # Backtesting with CSV file
        data = CustomDataset(
            name=COIN_TARGET,
            dataname="dataset/binance_nov_18_mar_19_btc.csv",
            timeframe=bt.TimeFrame.Minutes,
            fromdate=dt.datetime(2018, 9, 20),
            todate=dt.datetime(2019, 3, 13),
            nullvalue=0.0
        )

        cerebro.resampledata(data, timeframe=bt.TimeFrame.Minutes, compression=30)

        broker = cerebro.getbroker()
        broker.setcommission(commission=0.001, name=COIN_TARGET)  # Simulating exchange fee
        broker.setcash(100000.0)
        cerebro.addsizer(FullMoney)

    # Analyzers to evaluate trades and strategies
    # SQN = Average( profit / risk ) / StdDev( profit / risk ) x SquareRoot( number of trades )
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name="ta")
    cerebro.addanalyzer(bt.analyzers.SQN, _name="sqn")

    # Include Strategy
    cerebro.addstrategy(BasicRSI)

    # Starting backtrader bot
    initial_value = cerebro.broker.getvalue()
    print('Starting Portfolio Value: %.2f' % initial_value)
    result = cerebro.run()

    # Print analyzers - results
    final_value = cerebro.broker.getvalue()
    print('Final Portfolio Value: %.2f' % final_value)
    print('Profit %.3f%%' % ((final_value - initial_value) / initial_value * 100))
    print_trade_analysis(result[0].analyzers.ta.get_analysis())
    print_sqn(result[0].analyzers.sqn.get_analysis())

    if DEBUG:
        cerebro.plot()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("finished.")
        time = dt.datetime.now().strftime("%d-%m-%y %H:%M")
        send_telegram_message("Bot finished by user at %s" % time)
    except Exception as err:
        send_telegram_message("Bot finished with error: %s" % err)
        print("Finished with error: ", err)
        raise
