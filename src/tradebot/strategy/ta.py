"""Streaming technical indicators beyond EMA/RSI/ATR: MACD, ADX, Bollinger, momentum, volume
spike, bar position, rolling support/resistance with breakouts, engulfing and double top/bottom.
All incremental (one bar per update), None until warm, no lookahead, finite-input guard.
`IndicatorSet` bundles them per symbol and exposes a flat snapshot for the AI filter and the
confluence strategy (spec docs/superpowers/specs/2026-09-15-indicators-and-confluence-design.md)."""
from __future__ import annotations

from collections import deque
from typing import Optional

from tradebot.strategy.indicators import ATR, EMA, RSI, _check_finite, _check_period
from tradebot.types import Candle


class MACD:
    """12/26 EMA gap minus a 9-EMA signal line. `hist_turned` is +1 when the histogram rose this
    bar after falling, -1 when it fell after rising, else 0."""

    def __init__(self, fast: int = 12, slow: int = 26, signal: int = 9):
        if fast >= slow:
            raise ValueError("MACD fast period must be < slow period")
        self.fast, self.slow, self.signal_ema = EMA(fast), EMA(slow), EMA(signal)
        self.macd: Optional[float] = None
        self.signal: Optional[float] = None
        self.hist: Optional[float] = None
        self.hist_turned = 0
        self._prev_hist: Optional[float] = None
        self._prev_delta: Optional[float] = None

    @property
    def ready(self) -> bool:
        return self.hist is not None

    def update(self, close: float) -> Optional[float]:
        f, s = self.fast.update(close), self.slow.update(close)
        if f is None or s is None:
            return None
        self.macd = f - s
        sig = self.signal_ema.update(self.macd)
        if sig is None:
            return None
        self.signal = sig
        hist = self.macd - sig
        delta = None if self._prev_hist is None else hist - self._prev_hist
        self.hist_turned = 0
        if delta is not None and self._prev_delta is not None:
            if delta > 0 >= self._prev_delta:
                self.hist_turned = 1
            elif delta < 0 <= self._prev_delta:
                self.hist_turned = -1
        self._prev_hist, self._prev_delta, self.hist = hist, delta, hist
        return hist


class ADX:
    """Wilder's ADX with DI+ and DI-. Ready after 2 * period bars (one period to seed the smoothed
    DM/TR, one more to seed the ADX average)."""

    def __init__(self, period: int = 14):
        self.period = _check_period(period)
        self.value: Optional[float] = None
        self.di_plus: Optional[float] = None
        self.di_minus: Optional[float] = None
        self._prev: Optional[Candle] = None
        self._tr_s = self._dmp_s = self._dmm_s = None  # Wilder-smoothed sums
        self._seed: list = []
        self._dx_seed: list = []

    @property
    def ready(self) -> bool:
        return self.value is not None

    def update(self, c: Candle) -> Optional[float]:
        for x in (c.high, c.low, c.close):
            _check_finite("ADX", x)
        if self._prev is None:
            self._prev = c
            return None
        p = self._prev
        up, down = c.high - p.high, p.low - c.low
        dmp = up if up > down and up > 0 else 0.0
        dmm = down if down > up and down > 0 else 0.0
        tr = max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close))
        self._prev = c
        if self._tr_s is None:
            self._seed.append((tr, dmp, dmm))
            if len(self._seed) < self.period:
                return None
            self._tr_s = sum(t for t, _, _ in self._seed)
            self._dmp_s = sum(a for _, a, _ in self._seed)
            self._dmm_s = sum(b for _, _, b in self._seed)
        else:
            n = self.period
            self._tr_s = self._tr_s - self._tr_s / n + tr
            self._dmp_s = self._dmp_s - self._dmp_s / n + dmp
            self._dmm_s = self._dmm_s - self._dmm_s / n + dmm
        if self._tr_s == 0:
            self.di_plus = self.di_minus = 0.0
            dx = 0.0
        else:
            self.di_plus = 100.0 * self._dmp_s / self._tr_s
            self.di_minus = 100.0 * self._dmm_s / self._tr_s
            denom = self.di_plus + self.di_minus
            dx = 0.0 if denom == 0 else 100.0 * abs(self.di_plus - self.di_minus) / denom
        if self.value is None:
            self._dx_seed.append(dx)
            if len(self._dx_seed) < self.period:
                return None
            self.value = sum(self._dx_seed) / self.period
        else:
            self.value = (self.value * (self.period - 1) + dx) / self.period
        return self.value


class Bollinger:
    def __init__(self, period: int = 20, mult: float = 2.0):
        self.period, self.mult = _check_period(period), mult
        self._w: deque = deque(maxlen=period)
        self.mid = self.upper = self.lower = self.pct_b = None

    @property
    def ready(self) -> bool:
        return self.mid is not None

    def update(self, close: float) -> Optional[float]:
        _check_finite("Bollinger", close)
        self._w.append(close)
        if len(self._w) < self.period:
            return None
        n = self.period
        mean = sum(self._w) / n
        var = sum((x - mean) ** 2 for x in self._w) / n
        sd = var ** 0.5
        self.mid, self.upper, self.lower = mean, mean + self.mult * sd, mean - self.mult * sd
        width = self.upper - self.lower
        self.pct_b = 0.5 if width == 0 else (close - self.lower) / width
        return self.pct_b


class Momentum:
    """Percent change of the close over n bars."""

    def __init__(self, n: int):
        self.n = _check_period(n)
        self._w: deque = deque(maxlen=n + 1)
        self.value: Optional[float] = None

    @property
    def ready(self) -> bool:
        return self.value is not None

    def update(self, close: float) -> Optional[float]:
        _check_finite("Momentum", close)
        self._w.append(close)
        if len(self._w) <= self.n:
            return None
        base = self._w[0]
        self.value = 0.0 if base == 0 else (close / base - 1.0) * 100.0
        return self.value


class VolumeSpike:
    """This bar's volume as a percent of the mean of the previous `period` bars' volumes."""

    def __init__(self, period: int = 20):
        self.period = _check_period(period)
        self._w: deque = deque(maxlen=period)
        self.value: Optional[float] = None

    @property
    def ready(self) -> bool:
        return self.value is not None

    def update(self, volume: float) -> Optional[float]:
        if len(self._w) == self.period:
            mean = sum(self._w) / self.period
            self.value = 0.0 if mean == 0 else volume / mean * 100.0
        self._w.append(volume)
        return self.value


def bar_position(c: Candle) -> float:
    """Where the close sits in the bar's range: 1 at the high, 0 at the low, 0.5 when the range is zero."""
    rng = c.high - c.low
    return 0.5 if rng <= 0 else (c.close - c.low) / rng


class RollingLevels:
    """Support and resistance = min low / max high of the previous `period` bars, excluding the
    current bar, so a close beyond them is a genuine break of prior structure."""

    def __init__(self, period: int = 100):
        self.period = _check_period(period)
        self._highs: deque = deque(maxlen=period)
        self._lows: deque = deque(maxlen=period)
        self.support = self.resistance = None
        self.breakout = self.breakdown = 0

    @property
    def ready(self) -> bool:
        return self.support is not None

    def update(self, c: Candle) -> None:
        if len(self._highs) == self.period:
            self.resistance, self.support = max(self._highs), min(self._lows)
            self.breakout = int(c.close > self.resistance)
            self.breakdown = int(c.close < self.support)
        self._highs.append(c.high)
        self._lows.append(c.low)


class Patterns:
    """Engulfing from the last two bars; double top/bottom from the last two confirmed pivot highs
    or lows (a pivot is the extreme of a 2*wing+1 window) within `tolerance_pct` of each other,
    separated by at least `min_gap` bars, confirmed when the close breaks the level between them."""

    def __init__(self, wing: int = 2, tolerance_pct: float = 0.3, min_gap: int = 5, lookback: int = 60):
        self.wing, self.tol, self.min_gap = wing, tolerance_pct / 100.0, min_gap
        self._bars: deque = deque(maxlen=lookback)
        self.engulfing = 0          # +1 bullish, -1 bearish
        self.double_top = 0
        self.double_bottom = 0
        self._pivot_highs: list = []   # (index, price)
        self._pivot_lows: list = []
        self._fired: dict = {}         # pivot pair -> already reported, so each pattern completes once
        self._i = -1

    def update(self, c: Candle) -> None:
        self._i += 1
        prev = self._bars[-1] if self._bars else None
        self._bars.append(c)
        self.engulfing = 0
        if prev is not None:
            prev_body = (min(prev.open, prev.close), max(prev.open, prev.close))
            if c.close > c.open and prev.close < prev.open and c.open <= prev_body[0] and c.close >= prev_body[1]:
                self.engulfing = 1
            elif c.close < c.open and prev.close > prev.open and c.open >= prev_body[1] and c.close <= prev_body[0]:
                self.engulfing = -1
        w = self.wing
        if len(self._bars) >= 2 * w + 1:
            mid = self._bars[-(w + 1)]
            window = list(self._bars)[-(2 * w + 1):]
            mid_i = self._i - w
            if all(mid.high >= b.high for b in window):
                self._pivot_highs.append((mid_i, mid.high))
            if all(mid.low <= b.low for b in window):
                self._pivot_lows.append((mid_i, mid.low))
        cutoff = self._i - len(self._bars)
        self._pivot_highs = [p for p in self._pivot_highs if p[0] >= cutoff][-4:]
        self._pivot_lows = [p for p in self._pivot_lows if p[0] >= cutoff][-4:]
        self.double_top = self._double(self._pivot_highs, c, top=True)
        self.double_bottom = self._double(self._pivot_lows, c, top=False)

    def _double(self, pivots: list, c: Candle, top: bool) -> int:
        """1 on the bar the close first breaks the neck between the last two matching pivots."""
        if len(pivots) < 2:
            return 0
        (i1, p1), (i2, p2) = pivots[-2], pivots[-1]
        if i2 - i1 < self.min_gap or abs(p1 - p2) > self.tol * max(p1, p2):
            return 0
        between = [b for k, b in enumerate(self._bars) if i1 <= self._i - (len(self._bars) - 1 - k) <= i2]
        if not between:
            return 0
        key = (top, i1, i2)
        if key in self._fired:
            return 0
        if top:
            neck = min(b.low for b in between)
            broke = c.close < neck
        else:
            neck = max(b.high for b in between)
            broke = c.close > neck
        if broke:
            self._fired = {key: True}  # only the newest pair can fire, so older keys are dead weight
        return int(broke)


def _trend(fast: Optional[float], slow: Optional[float]) -> int:
    if fast is None or slow is None or fast == slow:
        return 0
    return 1 if fast > slow else -1


INDICATOR_INT_PARAMS = ("ema_fast", "ema_slow", "rsi_period", "atr_period", "macd_fast", "macd_slow", "macd_signal",
                        "adx_period", "bb_period", "volume_period", "levels_period")
INDICATOR_FLOAT_PARAMS = ("bb_mult", "pattern_tolerance_pct")


def validate_indicator_params(params: Optional[dict]) -> dict:
    """Strict check of the `strategy.indicators` section: known keys only, whole positive periods,
    non-negative floats. Raises ValueError at load time rather than inside the first bar."""
    p = dict(params or {})
    unknown = set(p) - set(INDICATOR_INT_PARAMS) - set(INDICATOR_FLOAT_PARAMS)
    if unknown:
        raise ValueError(f"strategy.indicators has unknown keys {sorted(unknown)}")
    out: dict = {}
    for k in INDICATOR_INT_PARAMS:
        if k in p:
            v = p[k]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v != int(v) or v < 1:
                raise ValueError(f"strategy.indicators.{k} must be a whole number >= 1, got {v!r}")
            out[k] = int(v)
    for k in INDICATOR_FLOAT_PARAMS:
        if k in p:
            v = p[k]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
                raise ValueError(f"strategy.indicators.{k} must be a number >= 0, got {v!r}")
            out[k] = float(v)
    return out


class IndicatorSet:
    """One bundle per symbol. `snapshot()` is a flat dict of floats/ints with stable keys."""

    def __init__(self, params: Optional[dict] = None):
        p = validate_indicator_params(params)
        self.ema_fast, self.ema_slow = EMA(int(p.get("ema_fast", 20))), EMA(int(p.get("ema_slow", 50)))
        self.rsi = RSI(int(p.get("rsi_period", 14)))
        self.atr = ATR(int(p.get("atr_period", 14)))
        self.macd = MACD(int(p.get("macd_fast", 12)), int(p.get("macd_slow", 26)), int(p.get("macd_signal", 9)))
        self.adx = ADX(int(p.get("adx_period", 14)))
        self.bb = Bollinger(int(p.get("bb_period", 20)), float(p.get("bb_mult", 2.0)))
        self.mom = {n: Momentum(n) for n in (1, 5, 60)}
        self.vol = VolumeSpike(int(p.get("volume_period", 20)))
        self.levels = RollingLevels(int(p.get("levels_period", 100)))
        self.patterns = Patterns(tolerance_pct=float(p.get("pattern_tolerance_pct", 0.3)))
        self._last: Optional[Candle] = None

    @property
    def ready(self) -> bool:
        return all(x.ready for x in (self.ema_slow, self.rsi, self.atr, self.macd, self.adx, self.bb, self.mom[60],
                                     self.vol, self.levels))

    def update(self, c: Candle) -> None:
        self.ema_fast.update(c.close)
        self.ema_slow.update(c.close)
        self.rsi.update(c.close)
        self.atr.update(c)
        self.macd.update(c.close)
        self.adx.update(c)
        self.bb.update(c.close)
        for m in self.mom.values():
            m.update(c.close)
        self.vol.update(c.volume)
        self.levels.update(c)
        self.patterns.update(c)
        self._last = c

    def snapshot(self) -> dict:
        def f(x):
            return None if x is None else round(float(x), 4)
        if self._last is None:
            return {}
        return {
            "ema20": f(self.ema_fast.value), "ema50": f(self.ema_slow.value),
            "trend": _trend(self.ema_fast.value, self.ema_slow.value) if self.ema_slow.ready else 0,
            "macd_hist": f(self.macd.hist), "macd_turned": self.macd.hist_turned,
            "mom_1": f(self.mom[1].value), "mom_5": f(self.mom[5].value), "mom_60": f(self.mom[60].value),
            "adx": f(self.adx.value), "di_plus": f(self.adx.di_plus), "di_minus": f(self.adx.di_minus),
            "rsi": f(self.rsi.value), "pct_b": f(self.bb.pct_b), "bb_upper": f(self.bb.upper), "bb_lower": f(self.bb.lower),
            "atr": f(self.atr.value), "volume_spike_pct": f(self.vol.value), "bar_position": round(bar_position(self._last), 4),
            "support": f(self.levels.support), "resistance": f(self.levels.resistance),
            "breakout": self.levels.breakout, "breakdown": self.levels.breakdown,
            "engulfing": self.patterns.engulfing, "double_top": self.patterns.double_top,
            "double_bottom": self.patterns.double_bottom,
        }
