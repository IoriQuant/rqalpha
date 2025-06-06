# -*- coding: utf-8 -*-
# 版权所有 2019 深圳米筐科技有限公司（下称“米筐科技”）
#
# 除非遵守当前许可，否则不得使用本软件。
#
#     * 非商业用途（非商业用途指个人出于非商业目的使用本软件，或者高校、研究所等非营利机构出于教育、科研等目的使用本软件）：
#         遵守 Apache License 2.0（下称“Apache 2.0 许可”），
#         您可以在以下位置获得 Apache 2.0 许可的副本：http://www.apache.org/licenses/LICENSE-2.0。
#         除非法律有要求或以书面形式达成协议，否则本软件分发时需保持当前许可“原样”不变，且不得附加任何条件。
#
#     * 商业用途（商业用途指个人出于任何商业目的使用本软件，或者法人或其他组织出于任何目的使用本软件）：
#         未经米筐科技授权，任何个人不得出于任何商业目的使用本软件（包括但不限于向第三方提供、销售、出租、出借、转让本软件、
#         本软件的衍生产品、引用或借鉴了本软件功能或源代码的产品或服务），任何法人或其他组织不得出于任何目的使用本软件，
#         否则米筐科技有权追究相应的知识产权侵权责任。
#         在此前提下，对本软件的使用同样需要遵守 Apache 2.0 许可，Apache 2.0 许可与本许可冲突之处，以本许可为准。
#         详细的授权流程，请联系 public@ricequant.com 获取。

from datetime import datetime, date
from typing import Union, List, Sequence, Optional, Tuple

import six
import numpy as np
import pandas as pd

from rqalpha.const import INSTRUMENT_TYPE, TRADING_CALENDAR_TYPE, EXECUTION_PHASE
from rqalpha.utils import risk_free_helper, TimeRange, merge_trading_period
from rqalpha.data.trading_dates_mixin import TradingDatesMixin
from rqalpha.model.bar import BarObject, NANDict, PartialBarObject
from rqalpha.model.tick import TickObject
from rqalpha.model.instrument import Instrument
from rqalpha.model.order import ALGO_ORDER_STYLES
from rqalpha.utils.functools import lru_cache
from rqalpha.utils.datetime_func import convert_int_to_datetime, convert_date_to_int
from rqalpha.utils.typing import DateLike, StrOrIter
from rqalpha.interface import AbstractDataSource, AbstractPriceBoard
from rqalpha.core.execution_context import ExecutionContext
from line_profiler import LineProfiler

class DataProxy(TradingDatesMixin):
    def __init__(self, data_source, price_board):
        # type: (AbstractDataSource, AbstractPriceBoard) -> None
        self._data_source = data_source
        self._price_board = price_board
        try:
            trading_calendars = data_source.get_trading_calendars()
        except NotImplementedError:
            # forward compatible
            trading_calendars = {TRADING_CALENDAR_TYPE.EXCHANGE: data_source.get_trading_calendar()}
        TradingDatesMixin.__init__(self, trading_calendars)

    def __getattr__(self, item):
        return getattr(self._data_source, item)

    def get_trading_minutes_for(self, order_book_id, dt):
        instrument = self.instruments(order_book_id)
        minutes = self._data_source.get_trading_minutes_for(instrument, dt)
        return [] if minutes is None else minutes

    def get_yield_curve(self, start_date, end_date, tenor=None):
        if isinstance(tenor, six.string_types):
            tenor = [tenor]
        return self._data_source.get_yield_curve(start_date, end_date, tenor)

    def get_risk_free_rate(self, start_date, end_date):
        tenors = risk_free_helper.get_tenors_for(start_date, end_date)
        # 为何取 start_date 当日的？表示 start_date 时借入资金、end_date 归还的成本
        _s = start_date if self.is_trading_date(start_date) else self.get_next_trading_date(start_date, n=1)
        yc = self._data_source.get_yield_curve(_s, _s)
        if yc is None or yc.empty:
            return np.nan
        yc = yc.iloc[0]
        for tenor in tenors[::-1]:
            rate = yc.get(tenor)
            if rate and not np.isnan(rate):
                return rate
        else:
            return np.nan

    def get_dividend(self, order_book_id):
        instrument = self.instruments(order_book_id)
        return self._data_source.get_dividend(instrument)

    def get_split(self, order_book_id):
        instrument = self.instruments(order_book_id)
        return self._data_source.get_split(instrument)

    def get_dividend_by_book_date(self, order_book_id, date):
        table = self._data_source.get_dividend(self.instruments(order_book_id))
        if table is None or len(table) == 0:
            return

        try:
            dates = table['book_closure_date']
        except ValueError:
            dates = table['ex_dividend_date']
            date = self.get_next_trading_date(date)

        dt = date.year * 10000 + date.month * 100 + date.day

        left_pos = dates.searchsorted(dt)
        right_pos = dates.searchsorted(dt, side="right")

        if left_pos >= right_pos:
            return None

        return table[left_pos: right_pos]

    def get_split_by_ex_date(self, order_book_id, date):
        df = self.get_split(order_book_id)
        if df is None or len(df) == 0:
            return

        dt = convert_date_to_int(date)
        pos = df['ex_date'].searchsorted(dt)
        if pos == len(df) or df['ex_date'][pos] != dt:
            return None

        return df['split_factor'][pos]

    @lru_cache(10240)
    def _get_prev_close(self, order_book_id, dt):
        instrument = self.instruments(order_book_id)
        prev_trading_date = self.get_previous_trading_date(dt)
        bar = self._data_source.history_bars(instrument, 1, '1d', 'close', prev_trading_date,
                                             skip_suspended=False, include_now=False, adjust_orig=dt)
        if bar is None or len(bar) < 1:
            return np.nan
        return bar[0]

    def get_prev_close(self, order_book_id, dt):
        return self._get_prev_close(order_book_id, dt.replace(hour=0, minute=0, second=0))

    @lru_cache(10240)
    def _get_prev_settlement(self, instrument, dt):
        bar = self._data_source.history_bars(
            instrument, 1, '1d', fields='prev_settlement', dt=dt, skip_suspended=False, adjust_orig=dt
        )
        if bar is None or len(bar) == 0:
            return np.nan
        return bar[0]

    @lru_cache(10240)
    def _get_settlement(self, instrument, dt):
        bar = self._data_source.history_bars(instrument, 1, '1d', 'settlement', dt, skip_suspended=False)
        if bar is None or len(bar) == 0:
            raise LookupError("'{}', dt={}".format(instrument.order_book_id, dt))
        return bar[0]

    def get_prev_settlement(self, order_book_id, dt):
        instrument = self.instruments(order_book_id)
        if instrument.type not in (INSTRUMENT_TYPE.FUTURE, INSTRUMENT_TYPE.OPTION):
            return np.nan
        return self._get_prev_settlement(instrument, dt)

    def get_settlement(self, instrument, dt):
        # type: (Instrument, datetime) -> float
        if instrument.type != INSTRUMENT_TYPE.FUTURE:
            raise LookupError("'{}', instrument_type={}".format(instrument.order_book_id, instrument.type))
        return self._get_settlement(instrument, dt)

    def get_settle_price(self, order_book_id, date):
        instrument = self.instruments(order_book_id)
        if instrument.type != 'Future':
            return np.nan
        return self._data_source.get_settle_price(instrument, date)

    @lru_cache(512)
    def get_bar(self, order_book_id: str, dt: date, frequency: str = '1d') -> BarObject:
        instrument = self.instruments(order_book_id)
        if dt is None:
            return BarObject(instrument, NANDict, dt)
        bar = self._data_source.get_bar(instrument, dt, frequency)
        if bar:
            return BarObject(instrument, bar)
        return BarObject(instrument, NANDict, dt)

    def get_open_auction_bar(self, order_book_id, dt):
        instrument = self.instruments(order_book_id)
        try:
            bar = self._data_source.get_open_auction_bar(instrument, dt)
        except NotImplementedError:
            # forward compatible
            tick = self.current_snapshot(order_book_id, "1d", dt)
            bar = {k: getattr(tick, k) for k in [
                "datetime", "open", "limit_up", "limit_down", "volume", "total_turnover"
            ]}
        return PartialBarObject(instrument, bar)
    
    def get_open_auction_volume(self, order_book_id, dt):
        instrument = self.instruments(order_book_id)
        volume = self._data_source.get_open_auction_volume(instrument, dt)
        return volume

    def history(self, order_book_id, bar_count, frequency, field, dt):
        data = self.history_bars(order_book_id, bar_count, frequency,
                                 ['datetime', field], dt, skip_suspended=False, adjust_orig=dt)
        if data is None:
            return None
        return pd.Series(data[field], index=[convert_int_to_datetime(t) for t in data['datetime']])

    def fast_history(self, order_book_id, bar_count, frequency, field, dt):
        return self.history_bars(order_book_id, bar_count, frequency, field, dt, skip_suspended=False,
                                 adjust_type='pre', adjust_orig=dt)

    def history_bars(self, order_book_id, bar_count, frequency, field, dt,
                     skip_suspended=True, include_now=False,
                     adjust_type='pre', adjust_orig=None):
        instrument = self.instrument(order_book_id)
        if adjust_orig is None:
            adjust_orig = dt
        """ lp = LineProfiler()
        lp_wrapper = lp(self._data_source.history_bars)
        r = lp_wrapper(instrument, bar_count, frequency, field, dt,
                                              skip_suspended=skip_suspended, include_now=include_now,
                                              adjust_type=adjust_type, adjust_orig=adjust_orig)
        lp.print_stats() """

        r =  self._data_source.history_bars(instrument, bar_count, frequency, field, dt,
                                              skip_suspended=skip_suspended, include_now=include_now,
                                              adjust_type=adjust_type, adjust_orig=adjust_orig)
        return r
    
    def history_bars_4_fast_bt(self, order_book_id, bar_count, frequency, fields, dt)->np.ndarray:
        """
        为快速回测开发的历史数据接口，
        为了快速出结果，因此不做复权处理，
        甚至不同周期的数据结构也不做统一转换，
        返回np.ndarray结构，时间格式为int64或unint64
        所有处理操作下沉到调用方，
        因为调用方可以在快速拿到所有原始数据之后利用多进程处理
        """
        
        instrument = self.instrument(order_book_id)
        """ lp = LineProfiler()
        lp_wrapper = lp(self._data_source.history_bars_4_fast_bt)
        r = lp_wrapper(instrument, bar_count, frequency, fields, dt)
        lp.print_stats() """
        r =  self._data_source.history_bars_4_fast_bt(
            instrument, bar_count, frequency, fields, dt
        )
        return r
    
    def get_dr(self, order_book_id)->pd.DataFrame:
        instrument = self.instrument(order_book_id)
        return self._data_source.get_dr(instrument)

    def history_ticks(self, order_book_id, count, dt):
        instrument = self.instruments(order_book_id)
        return self._data_source.history_ticks(instrument, count, dt)

    def current_snapshot(self, order_book_id, frequency, dt):

        def tick_fields_for(ins):
            _STOCK_FIELD_NAMES = [
                'datetime', 'open', 'high', 'low', 'last', 'volume', 'total_turnover', 'prev_close',
                'limit_up', 'limit_down'
            ]
            _FUTURE_FIELD_NAMES = _STOCK_FIELD_NAMES + ['open_interest', 'prev_settlement']

            if ins.type not in [INSTRUMENT_TYPE.FUTURE, INSTRUMENT_TYPE.OPTION]:
                return _STOCK_FIELD_NAMES
            else:
                return _FUTURE_FIELD_NAMES

        instrument = self.instruments(order_book_id)
        if frequency == '1d':
            bar = self._data_source.get_bar(instrument, dt, '1d')
            if not bar:
                return None
            d = {k: bar[k] for k in tick_fields_for(instrument) if k in bar.dtype.names}
            d["last"] = bar["open"] if ExecutionContext.phase() == EXECUTION_PHASE.OPEN_AUCTION else bar["close"]
            d['prev_close'] = self._get_prev_close(order_book_id, dt)
            return TickObject(instrument, d)

        return self._data_source.current_snapshot(instrument, frequency, dt)

    def available_data_range(self, frequency):
        return self._data_source.available_data_range(frequency)

    def get_futures_trading_parameters(self, order_book_id, dt):
        # type: (str, datetime.date) -> FuturesTradingParameters
        instrument = self.instruments(order_book_id)
        return self._data_source.get_futures_trading_parameters(instrument, dt)

    def get_merge_ticks(self, order_book_id_list, trading_date, last_dt=None):
        return self._data_source.get_merge_ticks(order_book_id_list, trading_date, last_dt)

    def is_suspended(self, order_book_id, dt, count=1):
        # type: (str, DateLike, int) -> Union[Sequence[bool], bool]
        if count == 1:
            return self._data_source.is_suspended(order_book_id, [dt])[0]

        trading_dates = self.get_n_trading_dates_until(dt, count)
        return self._data_source.is_suspended(order_book_id, trading_dates)

    def is_st_stock(self, order_book_id, dt, count=1):
        if count == 1:
            return self._data_source.is_st_stock(order_book_id, [dt])[0]

        trading_dates = self.get_n_trading_dates_until(dt, count)
        return self._data_source.is_st_stock(order_book_id, trading_dates)

    def get_tick_size(self, order_book_id):
        return self.instruments(order_book_id).tick_size()

    def get_last_price(self, order_book_id):
        # type: (str) -> float
        return float(self._price_board.get_last_price(order_book_id))

    def all_instruments(self, types, dt=None):
        # type: (List[INSTRUMENT_TYPE], Optional[datetime]) -> List[Instrument]
        li = []
        for i in self._data_source.get_instruments(types=types):
            if dt is None or i.listing_at(dt):
                li.append(i)
        return li
        # return [i for i in self._data_source.get_instruments(types=types) if dt is None or i.listing_at(dt)]

    @lru_cache(None)
    def instrument(self, sym_or_id):
        return next(iter(self._data_source.get_instruments(id_or_syms=[sym_or_id])), None)

    def instruments(self, sym_or_ids):
        # type: (StrOrIter) -> Union[None, Instrument, List[Instrument]]
        if isinstance(sym_or_ids, str):
            return next(iter(self._data_source.get_instruments(id_or_syms=[sym_or_ids])), None)
        else:
            return list(self._data_source.get_instruments(id_or_syms=sym_or_ids))

    def get_future_contracts(self, underlying, date):
        # type: (str, DateLike) -> List[str]
        return sorted(i.order_book_id for i in self.all_instruments(
            [INSTRUMENT_TYPE.FUTURE], date
        ) if i.underlying_symbol == underlying and not Instrument.is_future_continuous_contract(i.order_book_id))

    def get_trading_period(self, sym_or_ids, default_trading_period=None):
        # type: (StrOrIter, Optional[Sequence[TimeRange]]) -> List[TimeRange]
        trading_period = default_trading_period or []
        for instrument in self.instruments(sym_or_ids):
            trading_period.extend(instrument.trading_hours or [])
        return merge_trading_period(trading_period)

    def is_night_trading(self, sym_or_ids):
        # type: (StrOrIter) -> bool
        return any((instrument.trade_at_night for instrument in self.instruments(sym_or_ids)))

    def get_algo_bar(self, id_or_ins, order_style, dt):
        # type: (Union[str, Instrument], Union[*ALGO_ORDER_STYLES], datetime) -> Tuple[float, int]
        if not isinstance(order_style, ALGO_ORDER_STYLES):
            raise RuntimeError("get_algo_bar only support VWAPOrder and TWAPOrder")
        if not isinstance(id_or_ins, Instrument):
            id_or_ins = self.instrument(id_or_ins)
        if id_or_ins is None:
            return np.nan, 0
        # 存在一些有日线没分钟线的情况,如果不是缺了,通常都是因为volume为0,用日线先判断确认下
        day_bar = self.get_bar(order_book_id=id_or_ins.order_book_id, dt=dt, frequency="1d")
        if day_bar.volume == 0:
            return np.nan, 0
        bar = self._data_source.get_algo_bar(id_or_ins, order_style.start_min, order_style.end_min, dt)
        return (bar[order_style.TYPE], bar["volume"]) if bar else (np.nan, 0)
