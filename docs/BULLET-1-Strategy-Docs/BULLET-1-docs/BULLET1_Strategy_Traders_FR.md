# BULLET-1 — Stratégie : Guide du Trader Crypto

> **Paire :** BTC/USDT Futures Perpétuels | **Exchange :** Binance | **TF :** 5 minutes | **Levier :** 20×

---

## Vue d'ensemble de la stratégie

BULLET-1 trade autour d'un concept central : la **bougie d'incertitude** (doji ou quasi-doji). Lorsque le marché hésite — acheteurs et vendeurs à l'équilibre parfait — la bougie suivante trahit la direction réelle. C'est ce moment de révélation que BULLET-1 capture.

La stratégie s'appelle **Uncertainty Candle Enhanced** et fonctionne sur BTC/USDT Futures 5 minutes avec une **logique reverse** (mode momentum) dans sa configuration actuelle.

---

## La Bougie d'Incertitude — Qu'est-ce que c'est ?

Une bougie d'incertitude est une **bougie doji ou quasi-doji** : petit corps, longues mèches haute et basse. Elle signale que ni les bulls ni les bears n'ont le contrôle du marché à cet instant.

```
         │  ← mèche haute (≥ 20% de la range totale)
        ─┼─ ← petit corps (< 33% de la range)
         │  ← mèche basse (≥ 20% de la range totale)
```

**Types reconnus par BULLET-1 :**

| Type | Lecture de marché |
|---|---|
| **Perfect Doji** | Indécision maximale, corps quasi nul |
| **Long-legged Doji** | Forte volatilité des deux côtés, incertitude extrême |
| **Dragonfly Doji** | Rejet vendeur net — les bears ont tenté mais ont échoué |
| **Gravestone Doji** | Rejet acheteur net — les bulls ont tenté mais ont échoué |
| **Standard Uncertainty** | Quasi-doji avec critères de base respectés |

---

## Le Mécanisme de Signal

### Étape 1 — Identifier la bougie d'indécision
BULLET-1 surveille chaque bougie fermée. Si elle présente un corps < 33% de sa range et des mèches haute + basse ≥ 20% chacune → **bougie d'incertitude validée**.

### Étape 2 — Détecter la cassure
La bougie **suivante** doit sortir de la range de la bougie d'incertitude :
- **Cassure HAUSSIÈRE** → le prix dépasse le High précédent (mode permissif : le High actuel > High précédent)
- **Cassure BAISSIÈRE** → le prix casse sous le Low précédent

Si les deux cassures se produisent simultanément → signal annulé (ambiguïté).

### Étape 3 — Logique Reverse (configuration actuelle)

En mode **reverse**, BULLET-1 trade DANS LE SENS de la cassure (momentum) :

```
Cassure HAUSSIÈRE → LONG   (breakout bull confirmé)
Cassure BAISSIÈRE → SHORT  (breakout bear confirmé)
```

> **Note :** La configuration actuelle `7-reverse` exige un volume INFÉRIEUR à la moyenne pour les deux. C'est un filtre anti-FOMO : les meilleurs breakouts sont souvent ceux qui se font dans la discrétion.

### Étape 4 — Confirmation Volume (NIVEAU-1, obligatoire)

Le volume de la bougie de cassure doit être **inférieur à la moyenne** (SMA 20 bougies).

```
volume_ratio = volume_actuel / SMA_volume_20
Configuration 7-reverse : volume_ratio < 1.0 requis
```

### Étape 5 — Filtre de Tendance (optionnel — inactif actuellement)

Un filtre EMA50/EMA200 peut rejeter les trades contre-tendance. Dans la configuration actuelle, ce filtre est **désactivé** : BULLET-1 trade dans les deux sens sans contrainte de tendance.

### Étape 6 — Score de Confiance (0 → 100)

Chaque signal reçoit un score calculé sur 5 facteurs :

| Facteur | Poids |
|---|---|
| Qualité du doji (corps + mèches) | 35% |
| Force du volume de cassure | 30% |
| Amplitude de la cassure | 20% |
| Alignement avec la tendance | 10% |
| Confirmation volume NIVEAU-2 | 5% |

---

## Gestion des Positions

### Entrée
- **Prix d'entrée :** Close de la bougie de signal (exécution sur la bougie suivante)
- **1 seule position simultanée** (pas de pyramiding)
- **Marge Isolated** : en cas de liquidation, seul le collateral de cette position est perdu

### Position Sizing
- **Collateral :** 10% du capital disponible
- **Levier :** 20×
- **Exposition notionnelle :** 200% du capital (= 10% × 20×)

Exemple sur 100 USDT : 10 USDT en collateral contrôle 200 USDT de BTC.

### Stop Loss & Take Profit

```
Stop Loss   : ±0.5% du prix d'entrée (direction adverse)
Take Profit : 2× la distance du SL (Risk/Reward = 2.0)
```

Sur une entrée LONG à 50 000 USDT :
- SL = 49 750 USDT (−0.5%)
- TP = 50 500 USDT (+1.0%)

### Trailing Stop (mode ATR)

BULLET-1 utilise un **trailing stop dynamique basé sur l'ATR** qui se resserre progressivement au fur et à mesure que le trade entre en profit :

| Profit réalisé | Serrage du trailing |
|---|---|
| < 0.5R | ATR × 2.0 (large — laisser respirer) |
| ≥ 0.5R | ATR × 1.7 |
| ≥ 0.8R | ATR × 1.3 |
| ≥ 1.2R | ATR × 0.9 (serré — protéger les gains) |
| ≥ 1.6R | ATR × 0.5 (très serré — maximiser) |

**Protection Breakeven :**
Dès que le trade atteint **1R de profit** (LONG) ou **1.2R** (SHORT), le SL est automatiquement remonté/rabaissé au prix d'entrée. Le trade devient **risque-zéro**.

---

## Filtres de Sécurité et Circuit Breakers

BULLET-1 valide les conditions de marché avant chaque entrée. Si l'une de ces conditions est détectée, l'entrée est bloquée :

| Condition bloquante | Seuil |
|---|---|
| Spread trop large | > 0.75% |
| Liquidité insuffisante | Volume < seuil minimum |
| Gap d'ouverture excessif | > 0.5% |
| Mèches extrêmes (manipulation potentielle) | Mèche adverse > 1.5% |
| Volatilité extrême (ATR) | ATR > 3.0% du prix |
| Anomalies consécutives | > 10 → arrêt automatique |

**Limites journalières :**
- Perte journalière max : **−5%** du capital → arrêt du bot pour la journée
- Gain journalier max : **+10%** → arrêt du bot pour la journée
- Trades max par jour : **10**

**Limites par session (10 jours) :**
- Perte session max : **−10%**
- Gain session max : **+25%**

---

## Réalisme du Backtesting

BULLET-1 simule les conditions réelles de trading pour que les résultats de backtest soient fiables :

| Élément simulé | Détail |
|---|---|
| Slippage | Dynamique — augmente avec la volatilité ATR |
| Frais maker/taker | Différenciés (Binance Futures standard) |
| Funding fees | Cycle 8h (00h / 08h / 16h UTC) — coût réel des Futures perp |
| Latence API | 50 à 200 ms simulés |
| Look-ahead bias | Exécution sur la bougie suivante — aucune donnée future utilisée |

---

## Les 8 Configurations Testables

BULLET-1 implémente 8 variantes de la même stratégie. La différence : la direction du trade selon la cassure ET la condition de volume.

| Config | Logique | Cassure UP | Cassure DOWN |
|---|---|---|---|
| 1-normal | Contrariant | SHORT si vol↑ | LONG si vol↑ |
| 2-normal | Contrariant | SHORT si vol↑ | LONG si vol↓ |
| 3-normal | Contrariant | SHORT si vol↓ | LONG si vol↑ |
| 4-normal | Contrariant | SHORT si vol↓ | LONG si vol↓ |
| 5-reverse | Momentum | LONG si vol↑ | SHORT si vol↓ |
| 6-reverse | Momentum | LONG si vol↓ | SHORT si vol↑ |
| **7-reverse** ✅ | **Momentum** | **LONG si vol↓** | **SHORT si vol↓** |
| 8-reverse | Momentum | LONG si vol↑ | SHORT si vol↑ |

Le backtesting et l'optimiseur (Phase 2) permettront d'identifier quelle configuration performe le mieux sur les données historiques BTC.

---

## Métriques de Performance Surveillées

| Métrique | Ce qu'elle mesure |
|---|---|
| **Winrate** | % de trades gagnants |
| **Profit Factor** | Rapport gains/pertes — doit être > 1.5 |
| **Max Drawdown** | Pire perte consécutive depuis un pic |
| **Sharpe Ratio** | Rendement ajusté au risque — doit être > 1.0 |
| **Calmar Ratio** | Croissance annuelle / Max Drawdown |
| **RR Moyen réalisé** | RR effectif (souvent différent du RR théorique avec trailing stop) |

---

## Objectif de Capitalisation

| Paramètre | Valeur |
|---|---|
| Capital de départ (live) | 50 USDT |
| Capital cible | 1 000 USDT |
| Croissance attendue | 20× le capital initial |
| Chemin | Backtest → Paper Trading → Live |

> ⚠️ Le bot n'est pas encore en live trading. Phase 1 (backtesting) à 75%, Phases 3 et 4 (paper + live) à démarrer.

---

*© BULLET-1 — FuegoDev — 2026*
