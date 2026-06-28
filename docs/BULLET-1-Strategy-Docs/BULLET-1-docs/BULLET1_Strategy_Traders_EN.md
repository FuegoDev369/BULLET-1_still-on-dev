# BULLET-1 — Strategy: Crypto Trader's Guide

> **Pair:** BTC/USDT Perpetual Futures | **Exchange:** Binance | **TF:** 5 minutes | **Leverage:** 20×

---

## Strategy Overview

BULLET-1 trades around one central concept: the **uncertainty candle** (doji or near-doji). When the market hesitates — buyers and sellers in perfect equilibrium — the next candle reveals the true direction. That moment of revelation is exactly what BULLET-1 captures.

The strategy is called **Uncertainty Candle Enhanced** and runs on BTC/USDT Futures 5-minute charts with **reverse logic** (momentum mode) in its current configuration.

---

## The Uncertainty Candle — What Is It?

An uncertainty candle is a **doji or near-doji candle**: small body, long upper and lower wicks. It signals that neither bulls nor bears are in control at that moment.

```
         │  ← upper wick (≥ 20% of total range)
        ─┼─ ← small body (< 33% of total range)
         │  ← lower wick (≥ 20% of total range)
```

**Types recognized by BULLET-1:**

| Type | Market Reading |
|---|---|
| **Perfect Doji** | Maximum indecision, near-zero body |
| **Long-legged Doji** | Strong two-sided volatility, extreme uncertainty |
| **Dragonfly Doji** | Clear seller rejection — bears tried and failed |
| **Gravestone Doji** | Clear buyer rejection — bulls tried and failed |
| **Standard Uncertainty** | Near-doji with base criteria met |

---

## The Signal Mechanism

### Step 1 — Identify the Indecision Candle
BULLET-1 monitors each closed candle. If it shows a body < 33% of its range and both upper + lower wicks ≥ 20% each → **uncertainty candle validated**.

### Step 2 — Detect the Breakout
The **next candle** must exit the uncertainty candle's range:
- **BULLISH breakout** → price surpasses the previous High (permissive mode: current High > prev High)
- **BEARISH breakout** → price breaks below the previous Low

If both breakouts occur simultaneously → signal cancelled (ambiguity).

### Step 3 — Reverse Logic (current configuration)

In **reverse** mode, BULLET-1 trades IN THE DIRECTION of the breakout (momentum):

```
BULLISH breakout → LONG   (confirmed bull breakout)
BEARISH breakout → SHORT  (confirmed bear breakout)
```

> **Note:** The current `7-reverse` configuration requires volume BELOW average for both directions. This is an anti-FOMO filter: the best breakouts are often the quiet ones.

### Step 4 — Volume Confirmation (LEVEL-1, mandatory)

The breakout candle's volume must be **below average** (20-candle SMA).

```
volume_ratio = current_volume / SMA_volume_20
Config 7-reverse: volume_ratio < 1.0 required
```

### Step 5 — Trend Filter (optional — currently inactive)

An EMA50/EMA200 filter can reject counter-trend trades. In the current configuration, this filter is **disabled**: BULLET-1 trades both directions without trend constraints.

### Step 6 — Confidence Score (0 → 100)

Each signal receives a score calculated on 5 factors:

| Factor | Weight |
|---|---|
| Doji quality (body + wicks) | 35% |
| Breakout volume strength | 30% |
| Breakout amplitude | 20% |
| Trend alignment | 10% |
| LEVEL-2 volume confirmation | 5% |

---

## Position Management

### Entry
- **Entry price:** Close of the signal candle (execution on the next candle)
- **1 simultaneous position maximum** (no pyramiding)
- **Isolated margin**: if liquidated, only this position's collateral is lost

### Position Sizing
- **Collateral:** 10% of available capital
- **Leverage:** 20×
- **Notional exposure:** 200% of capital (= 10% × 20×)

Example on 100 USDT: 10 USDT collateral controls 200 USDT worth of BTC.

### Stop Loss & Take Profit

```
Stop Loss   : ±0.5% from entry price (adverse direction)
Take Profit : 2× the SL distance (Risk/Reward = 2.0)
```

On a LONG entry at 50,000 USDT:
- SL = 49,750 USDT (−0.5%)
- TP = 50,500 USDT (+1.0%)

### Trailing Stop (ATR mode)

BULLET-1 uses a **dynamic ATR-based trailing stop** that progressively tightens as the trade moves into profit:

| Profit Reached | Trailing Tightness |
|---|---|
| < 0.5R | ATR × 2.0 (wide — let it breathe) |
| ≥ 0.5R | ATR × 1.7 |
| ≥ 0.8R | ATR × 1.3 |
| ≥ 1.2R | ATR × 0.9 (tight — protect gains) |
| ≥ 1.6R | ATR × 0.5 (very tight — maximize) |

**Breakeven Protection:**
Once the trade reaches **1R of profit** (LONG) or **1.2R** (SHORT), the SL is automatically moved to entry price. The trade becomes **risk-free**.

---

## Safety Filters and Circuit Breakers

BULLET-1 validates market conditions before every entry. If any of these conditions is detected, the entry is blocked:

| Blocking Condition | Threshold |
|---|---|
| Spread too wide | > 0.75% |
| Insufficient liquidity | Volume < minimum threshold |
| Excessive opening gap | > 0.5% |
| Extreme wicks (potential manipulation) | Adverse wick > 1.5% |
| Extreme volatility (ATR) | ATR > 3.0% of price |
| Consecutive anomalies | > 10 → automatic stop |

**Daily limits:**
- Max daily loss: **−5%** of capital → bot stops for the day
- Max daily gain: **+10%** → bot stops for the day
- Max trades per day: **10**

**Per-session limits (10 days):**
- Max session loss: **−10%**
- Max session gain: **+25%**

---

## Backtesting Realism

BULLET-1 simulates real trading conditions to ensure backtest results are trustworthy:

| Simulated Element | Detail |
|---|---|
| Slippage | Dynamic — increases with ATR volatility |
| Maker/taker fees | Differentiated (standard Binance Futures) |
| Funding fees | 8-hour cycle (00:00 / 08:00 / 16:00 UTC) — real perp futures cost |
| API latency | 50 to 200 ms simulated |
| Look-ahead bias | Execution on next candle — no future data used |

---

## The 8 Testable Configurations

BULLET-1 implements 8 variants of the same strategy. The difference: trade direction based on breakout type AND volume condition.

| Config | Logic | Breakout UP | Breakout DOWN |
|---|---|---|---|
| 1-normal | Contrarian | SHORT if vol↑ | LONG if vol↑ |
| 2-normal | Contrarian | SHORT if vol↑ | LONG if vol↓ |
| 3-normal | Contrarian | SHORT if vol↓ | LONG if vol↑ |
| 4-normal | Contrarian | SHORT if vol↓ | LONG if vol↓ |
| 5-reverse | Momentum | LONG if vol↑ | SHORT if vol↓ |
| 6-reverse | Momentum | LONG if vol↓ | SHORT if vol↑ |
| **7-reverse** ✅ | **Momentum** | **LONG if vol↓** | **SHORT if vol↓** |
| 8-reverse | Momentum | LONG if vol↑ | SHORT if vol↑ |

Backtesting and the optimizer (Phase 2) will identify which configuration performs best on historical BTC data.

---

## Key Performance Metrics Tracked

| Metric | What It Measures |
|---|---|
| **Winrate** | % of winning trades |
| **Profit Factor** | Gains/Losses ratio — must be > 1.5 |
| **Max Drawdown** | Worst consecutive loss from a peak |
| **Sharpe Ratio** | Risk-adjusted return — must be > 1.0 |
| **Calmar Ratio** | Annual growth / Max Drawdown |
| **Avg Realized RR** | Effective RR (often differs from theoretical with trailing stop) |

---

## Capitalization Goal

| Parameter | Value |
|---|---|
| Starting capital (live) | 50 USDT |
| Target capital | 1,000 USDT |
| Expected growth | 20× initial capital |
| Roadmap | Backtest → Paper Trading → Live |

> ⚠️ The bot is not yet in live trading. Phase 1 (backtesting) at 75%, Phases 3 and 4 (paper + live) to be started.

---

*© BULLET-1 — FuegoDev — 2026*
