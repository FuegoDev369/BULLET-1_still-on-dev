# BULLET-1 — How Does It Work? A Beginner's Guide

---

## What Is BULLET-1?

BULLET-1 is an **automatic trading robot**. It buys and sells Bitcoin on its own, 24 hours a day, without you needing to watch the markets. It follows a precise strategy based on mathematical rules — not intuition or emotions.

It trades Bitcoin **Futures** (BTC/USDT) on Binance. Futures are a type of financial contract that lets you bet on the price going UP or DOWN — so you can profit even when the market falls.

---

## The Core Idea — Waiting for the Right Moment

Imagine a scale with two sides. When both sides are perfectly balanced — neither buyers nor sellers dominating — a decision is coming. The scale is about to tip one way.

In trading, this moment of balance appears as a **special candle** on the chart: a candle with a tiny body in the middle and long wicks above and below. This is called a **doji**.

```
         │   ← Long upper wick
       ══╪══ ← Tiny body (market is hesitating)
         │   ← Long lower wick
```

BULLET-1 spots these candles and watches closely for what happens next.

---

## The Strategy — Explained Simply

### 1️⃣ BULLET-1 finds a hesitation candle

The robot scans every 5-minute candle. If it finds one with:
- A **very small body** (less than a third of its total height)
- **Long wicks** both above AND below

→ It says: *"The market is undecided here. I'll watch the next candle closely."*

### 2️⃣ It waits for the breakout

The next candle will break out of that hesitation zone:
- It moves above the previous candle → **breakout to the UPSIDE**
- It moves below the previous candle → **breakout to the DOWNSIDE**

### 3️⃣ It follows the move (current mode)

In the current configuration, BULLET-1 follows the breakout direction:
- **Upside breakout → it buys (LONG)**
- **Downside breakout → it sells short (SHORT)**

> 🎯 It's like jumping on a train already in motion, at exactly the right moment.

### 4️⃣ It checks the volume

Before entering, the robot also checks the **volume** (the number of transactions happening). In its current config, it prefers to enter when volume is **calmer than usual**. This is an anti-FOMO filter.

### 5️⃣ It calculates its confidence

The robot gives each opportunity a score from 0 to 100. The higher the score, the better the signal quality. It only enters if conditions are good enough.

---

## How Does It Manage Trades?

### How much does it risk?

The robot uses **10% of its capital** per trade, but with **20× leverage**. That means with 10 USDT of margin, it controls 200 USDT of Bitcoin.

> ⚠️ Leverage amplifies gains... but also losses. That's why the robot has strict automatic stops.

### Stop Loss (loss protection)

As soon as it enters, the robot places a **Stop Loss** at 0.5% from the entry price. If the market moves 0.5% in the wrong direction, the trade closes automatically. Controlled loss.

### Take Profit (profit target)

The robot aims for a **1% gain** (twice the possible loss). If the market moves 1% in the right direction, the trade closes with profit.

### Trailing Stop (keeping gains)

If the trade is winning, the robot doesn't stay fixed on its initial target. It follows the move with a **dynamic stop** that tightens as profit grows. Result: it can capture larger moves if the market keeps going.

### Breakeven Protection (zero risk)

Once the trade reaches a certain profit level, the robot **moves its Stop Loss to the entry price**. From that point, the trade can no longer lose — the worst case is it closes at zero.

---

## Automatic Safeguards

BULLET-1 is designed to never lose control, even in chaotic markets:

| Situation | What the robot does |
|---|---|
| Market too chaotic (extreme volatility) | It waits — it doesn't trade |
| It has lost 5% today | It stops until tomorrow |
| It has gained 10% today | It stops — gains are protected |
| 10 trades already done today | It stops — discipline first |
| 10-day session down -10% | Full session stop |

---

## The Robot's Journey

BULLET-1 is still under development. It follows 4 steps before going live:

```
Phase 1 ✅ (75% complete)
Backtesting — testing the strategy on historical price data
↓
Phase 2 🔒 (coming soon)
Optimization — finding the best settings
↓
Phase 3 🔒 (coming soon)
Paper Trading — trading without real money to test in live conditions
↓
Phase 4 🔒 (final goal)
Live Trading — with real 50 USDT, target: 1,000 USDT
```

---

## In Summary — BULLET-1's Strategy

```
Market hesitates (doji candle)
        ↓
Next candle breaks in one direction
        ↓
BULLET-1 follows the move (with low volume)
        ↓
Enters with SL at -0.5% and TP at +1.0%
        ↓
Trailing stop protects and maximizes gains
        ↓
Daily limits stop the robot if needed
```

---

> 💡 **The key point:** BULLET-1 doesn't predict the market. It waits for a precise signal, enters with strict rules, and exits with discipline — whether in profit or loss.

---

*© BULLET-1 — FuegoDev — 2026*
