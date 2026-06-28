# BULLET-1 — Trading Strategy: Complete Technical Documentation

> **Author:** FuegoDev | **Version:** 2.1 | **Exchange:** Binance Futures | **Pair:** BTC/USDT  
> **Timeframe:** 5m / 15m | **Leverage:** 20× | **Margin:** Isolated | **Live Capital:** 50 USDT

---

## 1. Architectural Overview

BULLET-1 is an algorithmic trading system for crypto perpetual futures, built around the **Uncertainty Candle Enhanced** strategy. The pipeline is structured in three layers:

```
┌──────────────────────────────────────────────────────────────────────┐
│                          engine.py  (v2.2.2)                         │
│           Single orchestrator — sole permitted entry point           │
└──────────────┬──────────────────┬───────────────────────────────────┘
               │                  │                         │
               ▼                  ▼                         ▼
  ┌─────────────────┐  ┌──────────────────────┐  ┌──────────────────┐
  │ OHLCVDataEngine │  │   TradingEngine       │  │ AnalyticsEngine  │
  │  (data layer)   │  │   (v2.8.2)            │  │  (v2.1.5)        │
  └─────────────────┘  └──────────────────────┘  └──────────────────┘
```

### Data Pipeline

```
CSV/SQLite → DataLoader (v2.4.1)
           → DataValidator (structural checks, gap detection, deduplication)
           → DataProcessor (cleaning, normalization)
           → OHLCVDataEngine (session slicing, candle-by-candle iteration)
```

### Trading Pipeline (per closed candle)

```
SessionManager
    └── [closed candle N]
          ├── MarketContextCapture  → 7-indicator snapshot (ATR, Trend, Volume,
          │                           Momentum, Volatility, Structure, Regime)
          ├── Strategy (v2.2.11)
          │     ├── SignalGenerator (v2.4.6) → 11-step pipeline
          │     └── RiskManager (v2.4.1)    → sizing + SL/TP + market validation
          ├── OrderSimulator (v2.6.8)        → realistic fill + fees + funding
          └── PositionManager (v2.6.5)       → trailing stop + net PnL
```

---

## 2. Core Strategy: Uncertainty Candle Enhanced

### 2.1 Fundamental Principle

The strategy exploits **market indecision** materialized by a candle with a small body and long wicks (doji or near-doji). After this equilibrium moment, the next candle performs a **directional breakout** beyond the uncertainty candle's range. The strategy can trade in the **opposite direction** of the breakout (mode `normal` — contrarian) or in the **same direction** (mode `reverse` — momentum-following).

```
Uncertainty Candle          Breakout Candle              Signal Emitted
       │                          │
  High ─┤ ← upper wick ≥20%      │ Close > Prev High  →  SHORT (normal)
       │                          │                    →  LONG  (reverse)
  Body ─┤ ← body < 33%           │
       │                          │ Close < Prev Low   →  LONG  (normal)
  Low  ─┤ ← lower wick ≥20%      │                    →  SHORT (reverse)
```

### 2.2 Uncertainty Candle Detection

**Metrics computed by `UncertaintyCandleIndicator` (v2.2.2):**

```python
candle_range = high - low
body = abs(close - open)
body_pct = body / candle_range × 100

upper_wick = high - max(open, close)
lower_wick = min(open, close) - low
upper_wick_pct = upper_wick / candle_range × 100
lower_wick_pct = lower_wick / candle_range × 100
```

**Qualification criteria:**

| Criterion | Default Threshold | Purpose |
|---|---|---|
| `body_pct` | `< 33%` | Small body = buyer/seller equilibrium |
| `upper_wick_pct` | `≥ 20%` | High-end price rejection |
| `lower_wick_pct` | `≥ 20%` | Low-end price rejection |
| `body_min` | `≥ 10 USDT` | Anti-micro-noise filter |
| `candle_range_max` | `≤ 10,000 USDT` | Anti-flash-crash anomaly filter |
| `require_both_wicks` | `True` | Both wicks mandatory |

**Doji type classification:**

| Type | `body_pct` | `upper_wick_pct` | `lower_wick_pct` | Meaning |
|---|---|---|---|---|
| `perfect_doji` | `< 1%` | `≥ 20%` | `≥ 20%` | Perfect equilibrium |
| `long_legged_doji` | `< 15%` | `≥ 35%` | `≥ 35%` | Strong indecision |
| `dragonfly_doji` | `< 15%` | `< 5%` | `≥ 45%` | Clear seller rejection |
| `gravestone_doji` | `< 15%` | `≥ 45%` | `< 5%` | Clear buyer rejection |
| `standard_uncertainty` | `< 33%` | `≥ 20%` | `≥ 20%` | Standard indecision |

**Signal strength score (`signal_strength`):**

```
signal_strength = (1 - body_pct/body_max_pct) × 0.5
                + ((upper_wick_pct + lower_wick_pct) / (2 × 100)) × 0.5
→ Range [0.0 → 1.0]
```

### 2.3 Signal Generation Pipeline (11 Steps)

```
[Step 1]   DATA GUARD
           len(candles) ≥ volume_lookback (20)?
           → NO: NONE signal / 'insufficient_data'

[Step 2]   UNCERTAINTY CANDLE DETECTION
           UncertaintyCandleIndicator.detect(current_candle)
           → NO: NONE signal / 'no_uncertainty_candle'

[Step 3]   PREVIOUS CANDLE RETRIEVAL
           previous_candle = candles.iloc[-2]
           (requires len(candles) ≥ 2)

[Step 4]   BREAKOUT DETECTION
           STRICT mode     : close_current > high_prev  → UP
                             close_current < low_prev   → DOWN
           PERMISSIVE mode : high_current > high_prev   → UP
                             low_current < low_prev     → DOWN
           Both?  → BOTH → NONE signal / 'double_breakout_ambiguous'
           None?  → NONE signal / 'no_breakout'

[Step 5]   DIRECTION DETERMINATION
           NORMAL  : UP → SHORT + short_operator
                     DOWN → LONG + long_operator
           REVERSE : UP → LONG + long_operator
                     DOWN → SHORT + short_operator

[Step 6]   VOLUME LEVEL-1 VALIDATION (MANDATORY)
           avg_volume = SMA(volume, lookback=20)
           volume_ratio = current_volume / avg_volume
           Operator '>': current_volume > avg_volume → confirmed
           Operator '<': current_volume < avg_volume → confirmed
           → Not confirmed: NONE signal / 'volume_not_confirmed_X'

[Step 7]   TREND FILTER (optional, configurable)
           TrendIndicator → EMA50/EMA200 → trend ∈ {bullish, bearish, neutral, sideways}
           LONG  & trend == bearish  → rejected (if allow_counter_trend=False)
           SHORT & trend == bullish  → rejected
           neutral/sideways          → always accepted

[Step 8]   VOLUME LEVEL-2 VALIDATION (optional)
           Mode 'basic'       : volume_ratio ≥ min_ratio (1.1 default)
           Mode 'directional' : volume_ratio ≥ min_ratio
                                + candle direction == breakout direction
           Mode 'advanced'    : VolumeIndicator.is_volume_confirmation()
                                + volume trend check (increasing/neutral)

[Step 9]   CONFIDENCE SCORE (0 → 100)
           ┌─────────────────────────────────────────┬──────────┐
           │ Factor                                   │ Max pts  │
           ├─────────────────────────────────────────┼──────────┤
           │ Uncertainty candle quality (body+wicks)  │   35 pts │
           │ Volume LEVEL-1 ratio                     │   30 pts │
           │ Breakout amplitude vs prev candle range  │   20 pts │
           │ Trend alignment                          │   10 pts │
           │ Volume LEVEL-2 confirmed                 │    5 pts  │
           └─────────────────────────────────────────┴──────────┘

[Step 10]  ENTRY PRICE
           entry_price = close of current candle (execution on next candle)

[Step 11]  SIGNAL EMISSION
           Returns dict {side, confidence, entry_price, indicators, ...}
           + Persisted in _signals_history (deque maxlen=10,000)
```

### 2.4 The 8 Strategy Configurations

| Config | `logic_direction` | `short_op` | `long_op` | Breakout UP | Breakout DOWN |
|---|---|---|---|---|---|
| `1-normal` | normal | `>` | `>` | SHORT if vol↑ | LONG if vol↑ |
| `2-normal` | normal | `>` | `<` | SHORT if vol↑ | LONG if vol↓ |
| `3-normal` | normal | `<` | `>` | SHORT if vol↓ | LONG if vol↑ |
| `4-normal` | normal | `<` | `<` | SHORT if vol↓ | LONG if vol↓ |
| `5-reverse` | reverse | `<` | `>` | LONG if vol↑ | SHORT if vol↓ |
| `6-reverse` | reverse | `>` | `<` | LONG if vol↓ | SHORT if vol↑ |
| `7-reverse` | reverse | `<` | `<` | LONG if vol↓ | SHORT if vol↓ |
| **`8-reverse`** | **reverse** | **`>`** | **`>`** | **LONG if vol↑** | **SHORT if vol↑** |

> **Active config (config.json):** `7-reverse` — LONG if vol↓ / SHORT if vol↓  
> **Default config described in README:** `8-reverse`

---

## 3. Risk Management

### 3.1 Position Sizing

```
collateral   = capital × collateral_pct / 100       [10% of capital]
notional     = collateral × leverage                 [× 20 = 200% of capital]
size (BTC)   = notional / entry_price
```

**Example:** Capital = 100 USDT, Entry = 50,000 USDT/BTC  
→ collateral = 10 USDT | notional = 200 USDT | size = 0.004 BTC

### 3.2 Stop Loss & Take Profit

```
LONG:
    SL = entry × (1 - sl_offset_pct/100)    [entry × 0.995  → -0.5%]
    risk = entry - SL
    TP = entry + risk × rr_ratio             [entry + risk × 2.0]

SHORT:
    SL = entry × (1 + sl_offset_pct/100)    [entry × 1.005  → +0.5%]
    risk = SL - entry
    TP = entry - risk × rr_ratio
```

**Active parameters:** `sl_offset_pct = 0.5%` | `risk_reward_ratio = 2.0`

### 3.3 Market Condition Validation

`RiskManager` filters entries with abnormal conditions:

| Condition | Threshold | Rejection Reason |
|---|---|---|
| Max spread | 0.75% | Spread too wide |
| Min volume | 1.0 (ratio) | Insufficient liquidity |
| Max gap | 0.5% | Excessive opening gap |
| Max wick ratio | 3.5 | Potential manipulation |
| Max wick pct | 1.5% | Adverse wick too long (directional) |
| Max ATR pct | 3.0% | Extreme volatility |
| Max consecutive anomalies | 10 | Circuit breaker |

**Directional wick logic (v2.4.0):**
- LONG: only the LOWER wick is penalized — upper wick is favorable
- SHORT: only the UPPER wick is penalized — lower wick is favorable
- Absolute guard: `max(upper, lower) / close > max_wick_pct × 1.5` → systematic rejection

### 3.4 Trailing Stop

**Three available modes:**

| Mode | Logic |
|---|---|
| `candle` | SL follows the High/Low of the previous candle |
| `atr` | SL = reference_price ± ATR × multiplier |
| `hybrid` | Starts as `atr`, switches to `candle` after 1R profit |

**ATR mode (active) with progressive tightening:**

| Profit Reached | ATR Multiplier |
|---|---|
| < 0.5R | 2.0 (base) |
| ≥ 0.5R | 1.7 |
| ≥ 0.8R | 1.3 |
| ≥ 1.2R | 0.9 |
| ≥ 1.6R | 0.5 |

**1R Protection (asymmetric breakeven):**
- LONG: activate if profit ≥ 1.0R → SL moved up to breakeven
- SHORT: activate if profit ≥ 1.2R → SL moved down to breakeven

---

## 4. Session Management

```
Session = rolling time window of N days (default: 10 days)

Daily limits:
    max_loss_per_day    = -5% of capital
    max_gain_per_day    = +10% of capital
    max_trades_per_day  = 10

Per-session limits:
    max_loss_per_session = -10% of capital
    max_gain_per_session = +25% of capital

Circuit breaker:
    force_close_on_critical = True
    reset_capital_between_sessions = False (cumulative gains)
```

---

## 5. Realistic Simulation (Backtesting)

### 5.1 OrderSimulator (v2.6.8)

| Component | Implementation |
|---|---|
| **Slippage** | Dynamic, ATR-based — increases with volatility |
| **Maker fees** | Configurable (Binance Futures: 0.02% maker) |
| **Taker fees** | Configurable (Binance Futures: 0.05% taker) |
| **Funding fees** | 8-hour cycle — charged at 00:00 / 08:00 / 16:00 UTC |
| **API latency** | Simulated 50–200 ms (random) |
| **Look-ahead bias** | Execution on candle N+1 (absolute protection) |
| **Spread** | Applied on every order |

### 5.2 Net PnL

```
PnL_gross  = (exit_price - entry_price) × size × direction
total_fees = entry_fees + exit_fees + cumulative_funding_fees
PnL_net    = PnL_gross - total_fees
```

---

## 6. Technical Indicators (Context Layer)

| Module | Indicators | Role in Strategy |
|---|---|---|
| `atr.py` (v2.3.4) | EMA-smoothed ATR, spike/crash detection | Slippage, trailing stop, sizing |
| `volume.py` (v2.4.3) | Volume SMA, ratio, direction, trend | LEVEL-1 and LEVEL-2 validation |
| `trend.py` (v2.5.2) | EMA50/200, crossovers, trend quality | Trend filter (Step 7) |
| `momentum.py` (v2.1.2) | RSI, MACD, ROC, Stoch RSI, Williams %R, CMF, MFI, OBV | Market context snapshot |
| `structure.py` (v2.1.2) | Session VWAP, Price Z-Score, Swing H/L, BOS, CHoCH, Camarilla Pivots | Market context |
| `volatility.py` (v2.1.2) | Bollinger Bands, Keltner Channels, Squeeze, Realized Vol, Chandelier Exit | Market context |
| `regime.py` (v2.1.2) | ADX (+DI/-DI), Variance Ratio (Lo-MacKinlay) | Market context |

**Market regimes detected by `regime.py`:**

| Regime | ADX | Variance Ratio |
|---|---|---|
| `TRENDING_BULLISH` | ≥ 20, +DI > -DI | — |
| `TRENDING_BEARISH` | ≥ 20, -DI > +DI | — |
| `RANGING_MOMENTUM` | < 20 | VR > 1.05 |
| `RANGING_MEANREV` | < 20 | VR < 0.95 |
| `RANGING_NEUTRAL` | < 20 | 0.95 ≤ VR ≤ 1.05 |
| `TRANSITIONING` | around 20 | mixed |

---

## 7. MarketContext Capture

At each trade, `MarketContextCapture` (v2.1.2) records a **7-indicator snapshot** attached to the trade record, enabling **post-backtest analysis** of the correlation between market regime and performance:

```python
snapshot = {
    'atr':        ATRIndicator.get_current_atr(),
    'trend':      TrendIndicator.get_trend_metrics(),
    'volume':     VolumeIndicator.get_volume_stats(),
    'momentum':   MomentumIndicator.get_full_snapshot(),
    'volatility': VolatilityIndicator.get_snapshot(),
    'structure':  StructureIndicator.get_snapshot(),
    'regime':     RegimeIndicator.get_regime_snapshot(),
}
```

---

## 8. Performance Metrics

Calculated by `metrics.py` (v2.2.7) after each backtest:

| Metric | Description |
|---|---|
| **Winrate** | % of winning trades |
| **Profit Factor** | Total gains / Total losses |
| **Avg realized RR** | Average effective RR of trades |
| **Sharpe Ratio** | (Return - RF) / Standard deviation |
| **Sortino Ratio** | Sharpe on downside only |
| **Calmar Ratio** | CAGR / Max Drawdown |
| **CAGR** | Compound Annual Growth Rate (if duration ≥ 30d) |
| **Max Drawdown** | Max loss from a peak (absolute + relative) |

---

## 9. Quick Reference — Key Parameters

```json
{
  "general":   { "timeframe": "5m", "trading_pair": "BTC/USDT" },
  "position":  { "leverage": 20, "collateral_percentage": 10.0 },
  "risk_management": { "risk_reward_ratio": 2.0, "stop_loss_offset_pct": 0.5 },
  "strategy":  {
    "configuration_name": "7-reverse",
    "entry_logic": {
      "logic_direction": "reverse",
      "for_short_case_comparison_operator": "<",
      "for_long_case_comparison_operator":  "<"
    },
    "trailing_stop": { "type": "atr", "atr_mode": { "base_multiplier": 2.0 } }
  }
}
```

---

## 10. Development Status

| Phase | Content | Progress |
|---|---|---|
| **Phase 1** | Infrastructure + Backtesting (41 modules) | **75.6%** ✅ |
| **Phase 2** | Optimizer (grid search, walk-forward) | 0% 🔒 |
| **Phase 3** | Paper Trading (Binance API) | 0% 🔒 |
| **Phase 4** | Live Trading (50 USDT → 1,000 USDT) | 0% 🔒 |

> ⚠️ **Do not use in live trading before full validation of Phases 1 through 3.**

---

*© BULLET-1 — FuegoDev — 2026*
