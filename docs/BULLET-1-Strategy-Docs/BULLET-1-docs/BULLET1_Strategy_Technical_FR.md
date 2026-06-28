# BULLET-1 — Stratégie de Trading : Documentation Technique Complète

> **Auteur :** FuegoDev | **Version :** 2.1 | **Exchange :** Binance Futures | **Paire :** BTC/USDT  
> **Timeframe :** 5m / 15m | **Levier :** 20× | **Marge :** Isolated | **Capital live :** 50 USDT

---

## 1. Vue d'ensemble architecturale

BULLET-1 est un système de trading algorithmique Futures perpétuels basé sur la stratégie **Uncertainty Candle Enhanced**. Le pipeline s'articule en trois couches :

```
┌──────────────────────────────────────────────────────────────────────┐
│                          engine.py  (v2.2.2)                         │
│              Orchestrateur unique — seul point d'entrée              │
└──────────────┬──────────────────┬───────────────────────────────────┘
               │                  │                         │
               ▼                  ▼                         ▼
  ┌─────────────────┐  ┌──────────────────────┐  ┌──────────────────┐
  │ OHLCVDataEngine │  │   TradingEngine       │  │ AnalyticsEngine  │
  │  (données)      │  │   (v2.8.2)            │  │  (v2.1.5)        │
  └─────────────────┘  └──────────────────────┘  └──────────────────┘
```

### Pipeline de données

```
CSV/SQLite → DataLoader (v2.4.1)
           → DataValidator (validation structurelle, gaps, doublons)
           → DataProcessor (nettoyage, normalisation)
           → OHLCVDataEngine (slicing sessionnel, itération candle-by-candle)
```

### Pipeline de trading (par candle fermée)

```
SessionManager
    └── [candle N fermée]
          ├── MarketContextCapture  → snapshot 7 indicateurs (ATR, Trend, Volume,
          │                           Momentum, Volatility, Structure, Regime)
          ├── Strategy (v2.2.11)
          │     ├── SignalGenerator (v2.4.6) → pipeline 11 étapes
          │     └── RiskManager (v2.4.1)    → sizing + SL/TP + validation
          ├── OrderSimulator (v2.6.8)        → fill réaliste + frais + funding
          └── PositionManager (v2.6.5)       → trailing stop + PnL net
```

---

## 2. Stratégie Centrale : Uncertainty Candle Enhanced

### 2.1 Principe fondamental

La stratégie exploite le phénomène d'**indécision de marché** matérialisé par une bougie à petit corps et longues mèches (doji ou quasi-doji). Après ce moment d'équilibre, la bougie suivante réalise une **cassure directionnelle** hors de la range de la bougie d'incertitude. La stratégie peut trader dans le **sens opposé** à la cassure (mode `normal` — contrariant) ou dans le **même sens** (mode `reverse` — suiveur de momentum).

```
Bougie d'incertitude        Bougie de cassure           Signal généré
       │                          │
  High ─┤ ← mèche haute ≥20%     │ Close > High préc.  →  SHORT (normal)
       │                          │                     →  LONG  (reverse)
  Body ─┤ ← corps < 33%          │
       │                          │ Close < Low préc.   →  LONG  (normal)
  Low  ─┤ ← mèche basse ≥20%     │                     →  SHORT (reverse)
```

### 2.2 Détection de la bougie d'incertitude

**Métriques calculées par `UncertaintyCandleIndicator` (v2.2.2) :**

```python
candle_range = high - low
body = abs(close - open)
body_pct = body / candle_range × 100

upper_wick = high - max(open, close)
lower_wick = min(open, close) - low
upper_wick_pct = upper_wick / candle_range × 100
lower_wick_pct = lower_wick / candle_range × 100
```

**Critères de qualification :**

| Critère | Seuil par défaut | Rôle |
|---|---|---|
| `body_pct` | `< 33%` | Corps petit = équilibre acheteurs/vendeurs |
| `upper_wick_pct` | `≥ 20%` | Rejet prix haut |
| `lower_wick_pct` | `≥ 20%` | Rejet prix bas |
| `body_min` | `≥ 10 USDT` | Filtre anti-micro-bruit |
| `candle_range_max` | `≤ 10 000 USDT` | Filtre anti-anomalie flash |
| `require_both_wicks` | `True` | Les deux mèches obligatoires |

**Classification des types de doji :**

| Type | `body_pct` | `upper_wick_pct` | `lower_wick_pct` | Signification |
|---|---|---|---|---|
| `perfect_doji` | `< 1%` | `≥ 20%` | `≥ 20%` | Équilibre parfait |
| `long_legged_doji` | `< 15%` | `≥ 35%` | `≥ 35%` | Forte indécision |
| `dragonfly_doji` | `< 15%` | `< 5%` | `≥ 45%` | Rejet vendeur net |
| `gravestone_doji` | `< 15%` | `≥ 45%` | `< 5%` | Rejet acheteur net |
| `standard_uncertainty` | `< 33%` | `≥ 20%` | `≥ 20%` | Indécision standard |

**Score de force du signal (`signal_strength`) :**

```
signal_strength = (1 - body_pct/body_max_pct) × 0.5
                + ((upper_wick_pct + lower_wick_pct) / (2 × 100)) × 0.5
→ Range [0.0 → 1.0]
```

### 2.3 Pipeline de génération de signal (11 étapes)

```
[Étape 1]  GUARD DATA
           len(candles) ≥ volume_lookback (20) ?
           → NON : signal NONE / 'insufficient_data'

[Étape 2]  DÉTECTION UNCERTAINTY CANDLE
           UncertaintyCandleIndicator.detect(current_candle)
           → NON : signal NONE / 'no_uncertainty_candle'

[Étape 3]  RÉCUPÉRATION BOUGIE PRÉCÉDENTE
           previous_candle = candles.iloc[-2]
           (len(candles) ≥ 2 requis)

[Étape 4]  DÉTECTION CASSURE
           Mode STRICT     : close_current > high_prev  → UP
                             close_current < low_prev   → DOWN
           Mode PERMISSIVE : high_current > high_prev   → UP
                             low_current < low_prev     → DOWN
           Les deux ? → BOTH → signal NONE / 'double_breakout_ambiguous'
           Aucun ?    → signal NONE / 'no_breakout'

[Étape 5]  DÉTERMINATION DIRECTION
           NORMAL  : UP → SHORT + short_operator
                     DOWN → LONG + long_operator
           REVERSE : UP → LONG + long_operator
                     DOWN → SHORT + short_operator

[Étape 6]  VALIDATION VOLUME NIVEAU-1 (OBLIGATOIRE)
           avg_volume = SMA(volume, lookback=20)
           volume_ratio = current_volume / avg_volume
           Opérateur '>' : current_volume > avg_volume → confirmé
           Opérateur '<' : current_volume < avg_volume → confirmé
           → NON confirmé : signal NONE / 'volume_not_confirmed_X'

[Étape 7]  FILTRE TENDANCE (optionnel, configurable)
           TrendIndicator → EMA50/EMA200 → trend ∈ {bullish, bearish, neutral, sideways}
           LONG  & trend == bearish  → rejeté (si allow_counter_trend=False)
           SHORT & trend == bullish  → rejeté
           neutral/sideways          → toujours accepté

[Étape 8]  VALIDATION VOLUME NIVEAU-2 (optionnel)
           Mode 'basic'       : volume_ratio ≥ min_ratio (1.1 défaut)
           Mode 'directional' : volume_ratio ≥ min_ratio
                                + direction bougie == direction breakout
           Mode 'advanced'    : VolumeIndicator.is_volume_confirmation()
                                inclut check tendance volume (increasing/neutral)

[Étape 9]  SCORE DE CONFIANCE (0 → 100)
           ┌─────────────────────────────────────────┬──────────┐
           │ Facteur                                  │ Max pts  │
           ├─────────────────────────────────────────┼──────────┤
           │ Qualité incertitude (body + wicks)       │   35 pts │
           │ Ratio volume NIVEAU-1                    │   30 pts │
           │ Amplitude cassure vs range bougie préc.  │   20 pts │
           │ Alignement tendance                      │   10 pts │
           │ Volume NIVEAU-2 confirmé                 │    5 pts  │
           └─────────────────────────────────────────┴──────────┘

[Étape 10] PRIX D'ENTRÉE
           entry_price = close de la bougie courante (exécution candle suivante)

[Étape 11] ÉMISSION SIGNAL
           Retourne dict {side, confidence, entry_price, indicators, ...}
           + Persistance dans _signals_history (deque maxlen=10 000)
```

### 2.4 Les 8 configurations de stratégie

| Config | `logic_direction` | `short_op` | `long_op` | Breakout UP | Breakout DOWN |
|---|---|---|---|---|---|
| `1-normal` | normal | `>` | `>` | SHORT si vol↑ | LONG si vol↑ |
| `2-normal` | normal | `>` | `<` | SHORT si vol↑ | LONG si vol↓ |
| `3-normal` | normal | `<` | `>` | SHORT si vol↓ | LONG si vol↑ |
| `4-normal` | normal | `<` | `<` | SHORT si vol↓ | LONG si vol↓ |
| `5-reverse` | reverse | `<` | `>` | LONG si vol↑ | SHORT si vol↓ |
| `6-reverse` | reverse | `>` | `<` | LONG si vol↓ | SHORT si vol↑ |
| `7-reverse` | reverse | `<` | `<` | LONG si vol↓ | SHORT si vol↓ |
| **`8-reverse`** | **reverse** | **`>`** | **`>`** | **LONG si vol↑** | **SHORT si vol↑** |

> **Configuration active (config.json) :** `7-reverse` — LONG si vol↓ / SHORT si vol↓  
> **Configuration par défaut décrite dans README :** `8-reverse`

---

## 3. Gestion du Risque

### 3.1 Position Sizing

```
collateral   = capital × collateral_pct / 100       [10% du capital]
notional     = collateral × leverage                 [× 20 = 200% du capital]
size (BTC)   = notional / entry_price
```

**Exemple :** Capital = 100 USDT, Entry = 50 000 USDT/BTC  
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

**Paramètres actifs :** `sl_offset_pct = 0.5%` | `risk_reward_ratio = 2.0`

### 3.3 Validation des conditions de marché

Le `RiskManager` filtre les entrées avec conditions anormales :

| Condition | Seuil | Rejet |
|---|---|---|
| Spread max | 0.75% | Spread trop large |
| Volume minimum | 1.0 (ratio) | Liquidité insuffisante |
| Gap max | 0.5% | Gap d'ouverture excessif |
| Wick ratio max | 3.5 | Manipulation potentielle |
| Wick pct max | 1.5% | Mèche adverse trop longue (directional) |
| ATR pct max | 3.0% | Volatilité extrême |
| Anomalies consécutives max | 10 | Circuit breaker |

**Logique directionnelle des wicks (v2.4.0) :**
- LONG : seule la mèche BASSE (upper_wick) est pénalisante → mèche haute favorable
- SHORT : seule la mèche HAUTE est pénalisante → mèche basse favorable
- Garde-fou absolu : `max(upper, lower) / close > max_wick_pct × 1.5` → rejet systématique

### 3.4 Trailing Stop

**Trois modes disponibles :**

| Mode | Logique |
|---|---|
| `candle` | SL suit le High/Low de la bougie précédente |
| `atr` | SL = prix_référence ± ATR × multiplicateur |
| `hybrid` | Démarre en `atr`, bascule vers `candle` après 1R de profit |

**Mode ATR (actif) avec progressive tightening :**

| Profit atteint | Multiplicateur ATR |
|---|---|
| < 0.5R | 2.0 (base) |
| ≥ 0.5R | 1.7 |
| ≥ 0.8R | 1.3 |
| ≥ 1.2R | 0.9 |
| ≥ 1.6R | 0.5 |

**Protection 1R (breakeven asymétrique) :**
- LONG : activate si profit ≥ 1.0R → SL remonté au breakeven
- SHORT : activate si profit ≥ 1.2R → SL rabaissé au breakeven

---

## 4. Gestion des Sessions

```
Session = fenêtre temporelle glissante de N jours (défaut: 10 jours)

Limites journalières :
    max_loss_per_day    = -5% du capital
    max_gain_per_day    = +10% du capital
    max_trades_per_day  = 10

Limites par session :
    max_loss_per_session = -10% du capital
    max_gain_per_session = +25% du capital

Circuit breaker :
    force_close_on_critical = True
    reset_capital_between_sessions = False (cumul des gains)
```

---

## 5. Simulation Réaliste (Backtesting)

### 5.1 OrderSimulator (v2.6.8)

| Composante | Implémentation |
|---|---|
| **Slippage** | Dynamique basé sur ATR courant — augmente avec la volatilité |
| **Frais maker** | Configurables (Binance Futures : 0.02% maker) |
| **Frais taker** | Configurables (Binance Futures : 0.05% taker) |
| **Funding fees** | Cycle 8h — prélevé à 00:00 / 08:00 / 16:00 UTC |
| **Latence API** | Simulée 50–200 ms (aléatoire) |
| **Look-ahead bias** | Exécution sur la bougie N+1 (protection absolue) |
| **Spread** | Appliqué sur chaque ordre |

### 5.2 PnL Net

```
PnL_brut   = (exit_price - entry_price) × size × direction
frais_total = entry_fees + exit_fees + funding_fees_cumul
PnL_net    = PnL_brut - frais_total
```

---

## 6. Indicateurs Techniques (couche de contexte)

| Module | Indicateurs | Rôle dans la stratégie |
|---|---|---|
| `atr.py` (v2.3.4) | ATR EMA-smoothed, spike/crash detection | Slippage, trailing stop, sizing |
| `volume.py` (v2.4.3) | Volume SMA, ratio, direction, tendance | Validation NIVEAU-1 et NIVEAU-2 |
| `trend.py` (v2.5.2) | EMA50/200, crossovers, trend quality | Filtre de tendance (Étape 7) |
| `momentum.py` (v2.1.2) | RSI, MACD, ROC, Stoch RSI, Williams %R, CMF, MFI, OBV | Contexte marché (snapshot) |
| `structure.py` (v2.1.2) | VWAP sessionnel, Price Z-Score, Swing H/L, BOS, CHoCH, Pivots Camarilla | Contexte marché |
| `volatility.py` (v2.1.2) | Bollinger Bands, Keltner Channels, Squeeze, Realized Vol, Chandelier Exit | Contexte marché |
| `regime.py` (v2.1.2) | ADX (+DI/-DI), Variance Ratio (Lo-MacKinlay) | Contexte marché |

**Régimes de marché détectés par `regime.py` :**

| Régime | ADX | Variance Ratio |
|---|---|---|
| `TRENDING_BULLISH` | ≥ 20, +DI > -DI | — |
| `TRENDING_BEARISH` | ≥ 20, -DI > +DI | — |
| `RANGING_MOMENTUM` | < 20 | VR > 1.05 |
| `RANGING_MEANREV` | < 20 | VR < 0.95 |
| `RANGING_NEUTRAL` | < 20 | 0.95 ≤ VR ≤ 1.05 |
| `TRANSITIONING` | autour de 20 | mixte |

---

## 7. MarketContext Capture

À chaque trade, `MarketContextCapture` (v2.1.2) enregistre un **snapshot de 7 indicateurs** attaché au record de trade. Cela permet une **analyse post-backtest** de la corrélation entre régime de marché et performance :

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

## 8. Métriques de Performance

Calculées par `metrics.py` (v2.2.7) après chaque backtest :

| Métrique | Description |
|---|---|
| **Winrate** | % trades gagnants |
| **Profit Factor** | Gains totaux / Pertes totales |
| **Avg RR réalisé** | RR moyen effectif des trades |
| **Sharpe Ratio** | (Rendement - RF) / Écart-type |
| **Sortino Ratio** | Sharpe sur downside uniquement |
| **Calmar Ratio** | CAGR / Max Drawdown |
| **CAGR** | Taux de croissance annuel composé (si durée ≥ 30j) |
| **Max Drawdown** | Perte max depuis un pic (absolu + relatif) |

---

## 9. Configuration Rapide des Paramètres Clés

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

## 10. Statut de Développement

| Phase | Contenu | Progression |
|---|---|---|
| **Phase 1** | Infrastructure + Backtesting (41 modules) | **75.6%** ✅ |
| **Phase 2** | Optimiseur (grid search, walk-forward) | 0% 🔒 |
| **Phase 3** | Paper Trading (Binance API) | 0% 🔒 |
| **Phase 4** | Live Trading (50 USDT → 1 000 USDT) | 0% 🔒 |

> ⚠️ **Ne pas utiliser en live avant validation complète des Phases 1 à 3.**

---

*© BULLET-1 — FuegoDev — 2026*
