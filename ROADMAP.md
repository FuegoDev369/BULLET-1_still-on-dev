# BULLET-1 — Vision, Plan & Roadmap Complète

> **Document de référence du projet**
> Version : 1.2 — Mise à jour : 2026-05-04
> Auteur : FuegoDev × Claude (Anthropic)
> Statut actuel : **Phase 3 — Optimisation Multi-Régimes (en cours)**

---

## Changelog du document

| Version | Date | Changement |
|---|---|---|
| 1.0 | 2026-04-29 | Création initiale — taxonomie 3 régimes |
| 1.1 | 2026-05-02 | Découverte majeure : taxonomie révisée 5 régimes, résultats OOS BULL documentés |
| 1.2 | 2026-05-04 | BEAR P2 terminé : NO-TRADE ZONE confirmé, config défensive documentée, passage à RANGE |

---

## 1. Contexte & Vision

### 1.1 Ce qu'est BULLET-1

BULLET-1 est un bot de trading algorithmique pour les marchés futures crypto (BTC/USDT, 5m, Binance Futures). Ce n'est pas un bot statique — c'est un système intelligent, autonome et adaptatif.

La métaphore : **un sniper**. Pas une mitrailleuse. Un opérateur de précision qui sait quand entrer, comment, avec quelle arme, quelle stratégie, quel plan de sortie — et **quand ne PAS tirer**.

### 1.2 La vision finale — Architecture 5 niveaux

```
Marché en temps réel
      │
      ▼
NIVEAU 1 — Détecteur de Régime Macro
  BULL_TREND / BULL_EXPLOSIVE / BEAR / BEAR_PANIC / RANGE
  Signal BULL  : ratio ATR(7)/ATR(21)
  Signal BEAR  : vitesse de chute + volume + structure
  Signal RANGE : ADX faible + Variance Ratio proche de 1.0
      │
      ▼
NIVEAU 2 — Micro-Contexte (heures → jours)
  Momentum / Squeeze / Structure / Divergences
  Modules : momentum.py, volatility.py, structure.py
      │
      ▼
NIVEAU 3 — Config Selector Dynamique
  BULL_TREND     → config active (4-normal, ATR21)
  BULL_EXPLOSIVE → config active (1-normal, ATR7)
  BEAR           → NO-TRADE (capital préservé)
  BEAR_PANIC     → NO-TRADE (capital préservé)
  RANGE          → config active (à définir Phase 3C)
      │
      ▼
NIVEAU 4 — Context-Aware Entry Filter
  Conditions indicateurs à l'entrée de chaque trade
  Score de confiance contextuel [0.0, 1.0]
      │
      ▼
NIVEAU 5 — Engine d'Exécution BULLET-1
  Config sélectionnée + filtres contextuels actifs
  Backtest / Paper / Live
```

### 1.3 Hypothèse centrale (à valider Phase 4)

Pour des configurations données, la fréquence des trades gagnants est significativement supérieure quand certains indicateurs réunissent des conditions spécifiques au moment de l'entrée.

```
Trade_score = signal_qualité × contexte_multiplicateur [0.0, 1.0]
→ Si score < seuil → trade ignoré même si signal valide
```

---

## 2. Taxonomie des régimes — 5 régimes

> **Principe empirique :** La taxonomie est dictée par les données, pas par des
> suppositions. Chaque régime est défini par ses caractéristiques observées et
> par la réponse de la stratégie à ces caractéristiques.

### 2.1 Les 5 régimes

**BULL_TREND** ✅ Optimisé
Hausse progressive, tendancielle. Ex : P1 ETF Rally (+74% en 73j).
Signal ID : `ATR(7)/ATR(21) < 1.3` + ADX croissant
Config : `4-normal` | ATR21 | mult=2.0 | volume=OFF
Sharpe train : 2.404

**BULL_EXPLOSIVE** ✅ Optimisé
Hausse verticale, parabolique. Ex : P6 Election Rally (+59% en 40j).
Signal ID : `ATR(7)/ATR(21) > 1.5` + bougies >2×ATR
Config : `1-normal` | ATR7 | mult=3.0 | volume=ON
Sharpe train : 1.703

**BEAR** ✅ Testé — NO-TRADE ZONE
Baisse progressive. Ex : P2 correction post-ATH (-23.4% en 48j).
**0/8 configurations profitables.** Cause structurelle (voir §2.3).
Décision : pas de trading en régime BEAR.

**BEAR_PANIC** ⏭️ Non testé (conclusion BEAR suffit)
Chute brutale, liquidations. Ex : P4 Black Monday (-26.5% en 46j).
Si BEAR progressif n'est pas tradeable, BEAR_PANIC l'est encore moins.
Décision : NO-TRADE ZONE par extension.

**RANGE** ⬜ À optimiser — PRIORITÉ ACTUELLE
Consolidation sans direction. Ex : P3 post-halving (±12.5% en 50j).
Terrain idéal pour la stratégie : breakouts après accumulation = signal
d'incertitude = pause avant vrai breakout directionnel.

### 2.2 Résultats OOS BULL croisés

| Config | Train | OOS croisé | Résultat OOS |
|---|---|---|---|
| P1-config (BULL_TREND) | Sharpe 2.404 | Testé sur P6 | ❌ Sharpe -2.167 |
| P6-config (BULL_EXPLOSIVE) | Sharpe 1.703 | Testé sur P1 | ⚠️ 4 trades seulement |

Conclusion : deux sous-régimes BULL distincts confirmés. Chacun nécessite sa propre config.

### 2.3 Pourquoi BEAR = NO-TRADE ZONE

La stratégie Uncertainty Candle Enhanced détecte des bougies d'hésitation
pour anticiper des breakouts. En régime BEAR :

- Les bougies d'incertitude = pauses **avant continuation baissière**
- Le breakout s'inverse rapidement → LONGs stoppés, SHORTs trop tardifs
- Résultat P2 : best PF = **0.357** (perd 2.8× ce qu'il gagne), PnL = -18.76 USDT

C'est une limitation **structurelle**, pas un problème de paramètres.
Aucun réglage (Phase 2A, 2B, 2C) n'a changé cette réalité.

**Ne pas perdre en BEAR = conserver le capital = alpha réel.**
C'est l'approche des meilleures stratégies trend-following professionnelles.

**Config défensive #05** (documentée, non déployée en prod) :
`7-reverse` + `trend_filter=ON` + `allow_counter_trend=False` + `mult=2.5`
→ Réduit les pertes de 68% (PnL -5.91 vs -18.76, DD 9.44% vs 21.18%)
→ Toujours négatif — uniquement pour contexte de transition si besoin

### 2.4 Notes importantes sur les paramètres

**Levier :** En BEAR, l'optimizer a sélectionné 20× — c'est un **artefact**.
Avec PF < 0.5, 20× amplifie les pertes. L'optimizer choisit 20× parce que
peu de trades = moins de pertes absolues statistiquement. Ne pas utiliser.
**Règle générale : le levier optimal = celui qui maximise Sharpe/DD, pas PnL brut.**

**ATR trailing mult :** Range [0.5, 1.0, 1.5] inadaptée avec SL=1.0%.
**Range correcte pour futures optimisations : [1.5, 2.0, 2.5, 3.0, 3.5].**

### 2.5 Timeline 2024 — État d'avancement

```
P1  BULL_TREND      2024-01-01 → 2024-03-14  73j  +74.5%   ✅ Optimisé (Sharpe 2.404)
P2  BEAR            2024-03-14 → 2024-04-23  40j  -23.4%   ✅ Testé — NO-TRADE ZONE
P3  RANGE           2024-05-01 → 2024-06-20  50j  ±12.5%   ⬜ PROCHAINE ÉTAPE
P4  BEAR_PANIC      2024-06-20 → 2024-08-05  46j  -26.5%   ⏭️  NO-TRADE par extension
P5  RANGE (OOS)     2024-08-05 → 2024-11-05  92j  ±16%     ⬜ OOS pour RANGE
P6  BULL_EXPLOSIVE  2024-11-05 → 2024-12-15  40j  +59%     ✅ Optimisé (Sharpe 1.703)
P7  BEAR            2024-12-15 → 2024-12-31  16j  -14.5%   ⏭️  NO-TRADE par extension
```

---

## 3. Infrastructure construite

### 3.1 Modules complétés ✅

`data_loader.py` v3.0.0 · `db_manager.py` v1.0.0 · `data_validator.py` ·
`data_processor.py` · `uncertainty_candle.py` v2.2.2 · `volume.py` v2.4.3 ·
`trend.py` v2.5.2 · `atr.py` v2.3.4 · `momentum.py` v2.1.2 · `structure.py`
v2.1.2 · `volatility.py` v2.1.2 · `regime.py` v2.1.2 · `risk_manager.py`
v2.3.3 · `position_manager.py` v2.6.5 · `session_manager.py` v2.5.9 ·
`signal_generator.py` v2.4.6 · `strategy.py` v2.2.11 · `market_context.py`
v2.1.2 · `order_simulator.py` v2.6.8 · `metrics.py` v2.2.7 · `engine.py`
v2.2.2 · `trading_engine.py` v2.8.2 · `analytics_engine.py` v2.1.5 ·
`report_generator.py` v2.2.3 · `optimizer.py` v2.0.0

### 3.2 Données disponibles

`bullet1_market_data.db` — BTC/USDT 5m+15m — 2023-11-01 → 2026-03-31 ✅

### 3.3 Base de configs par régime (état actuel)

| Régime | Config | Sharpe train | OOS | Statut prod |
|---|---|---|---|---|
| BULL_TREND | `4-normal` qs=20 lev=10× SL=1.0% trail=atr mult=2.0 atr_period=21 body_max=10% | 2.404 | ❌ P6 | Active |
| BULL_EXPLOSIVE | `1-normal` qs=20 lev=15× SL=1.0% trail=hybrid mult=3.0 atr_period=7 body_max=10% | 1.703 | ⚠️ P1 | Active |
| BEAR | — | 0/8 profitable | — | **NO-TRADE** |
| BEAR_PANIC | — | Non testé | — | **NO-TRADE** |
| RANGE | À optimiser | — | — | En cours |

---

## 4. Roadmap détaillée

### PHASE 3 — Optimisation Multi-Régimes (EN COURS)

#### 3A — Audit modules contextuels (PRIORITÉ 1 — non encore fait)

| Module | Risque principal |
|---|---|
| `regime.py` | Ne distingue pas encore BULL_TREND/EXPLOSIVE (ratio ATR7/ATR21 à ajouter) |
| `momentum.py` | Divergences sur données insuffisantes |
| `structure.py` | Anchoring VWAP sur longues périodes |
| `volatility.py` | Squeeze trop sensible sur 5m |
| `market_context.py` | 5 `pass` → chemins non testés |

Critère validation `regime.py` : classifie les 7 phases 2024 avec accord
≥ 70% (±3j sur transitions) ET calcule `ATR(7)/ATR(21)` pour distinguer
BULL_TREND vs BULL_EXPLOSIVE.

#### 3B — Données : ✅ disponibles

#### 3C — Optimisations par régime

| Étape | Train | Régime | OOS | Statut |
|---|---|---|---|---|
| ✅ | P1 : 01/01 → 03/14 | BULL_TREND | P6 ❌ | Terminé |
| ✅ | P6 : 11/05 → 12/15 | BULL_EXPLOSIVE | P1 ⚠️ | Terminé |
| ✅ | P2 : 03/14 → 04/23 | BEAR | — | NO-TRADE ZONE |
| ⏭️ | P4 : 06/20 → 08/05 | BEAR_PANIC | — | Skippé par extension |
| **⬜** | **P3 : 05/01 → 06/20** | **RANGE** | **P5** | **PROCHAINE ÉTAPE** |
| ⬜ | P5 : 08/05 → 11/05 | RANGE OOS | P3 | Après P3 |

**Procédure RANGE P3 :**

```bash
# 1. Modifier config.json
"start_date": "2024-05-01",
"end_date":   "2024-06-20"    # 50j → 5 sessions × 10j

# 2. Optimiser
python optimize.py --phase 2a   # ~8h
python optimize.py --phase 2b   # ~30min
python optimize.py --phase 2c   # ~25min

# 3. Valider backtest direct (doit matcher l'optimizer)
python main.py backtest

# 4. OOS sur P5 (92j — test de robustesse le plus long)
"start_date": "2024-08-05",
"end_date":   "2024-11-05"
python main.py backtest
```

**Pourquoi RANGE est prometteur :**
La stratégie Uncertainty Candle capte des breakouts après hésitation.
En RANGE, les bougies d'incertitude précèdent de vrais breakouts du range
→ c'est le signal le plus propre possible pour cette stratégie.

#### 3D — Constitution de regime_configs.json

```json
{
  "version": "1.0",
  "BULL_TREND":     { "config": {...}, "sharpe": 2.404, "action": "trade" },
  "BULL_EXPLOSIVE": { "config": {...}, "sharpe": 1.703, "action": "trade" },
  "BEAR":           { "config": null,  "sharpe": null,  "action": "no_trade",
                      "reason": "0/8 configurations profitables — limitation structurelle" },
  "BEAR_PANIC":     { "config": null,  "sharpe": null,  "action": "no_trade",
                      "reason": "Extension de la conclusion BEAR" },
  "RANGE":          { "config": null,  "sharpe": null,  "action": "à définir" }
}
```

---

### PHASE 4 — Context-Aware Entry Filter

`src/analysis/trade_context_analyzer.py` — Extrait le snapshot
`MarketContextCapture` à chaque `entry_time`, compare les ~50 features
pour winners vs losers, calcule l'importance de chaque feature.

À lancer après Phase 3C complète (≥100 trades analysables).

Extension optimizer : Phase 2D avec context guards.
Context Score dynamique : `src/core/context_scorer.py`

---

### PHASE 5 — Détecteur de Régime Temps Réel

Calibration `regime.py` + signal `ATR(7)/ATR(21)` sur les 7 phases 2024.
Multi-timeframe (5m + 15m). Smooth switching : confirmation ≥ 3 sessions
+ confiance ≥ 0.65 avant de changer de config.

**Cas BEAR :** Quand BEAR détecté → Config Selector retourne `None` →
Engine ne génère aucun trade (protection capital automatique).

---

### PHASE 6 — Simulation switching dynamique

Simuler le Config Selector sur 2024 complet. Comparer :
- Config fixe toute l'année
- Switching dynamique 5 régimes

Métriques : Sharpe annuel, periods non-tradées (BEAR), switchings erronés,
DD aux transitions.

---

### PHASE 7 — Paper Trading

Prérequis : Phases 3-6 terminées + modules exchange implémentés.
Durée minimale : 30 jours. Validation : WR ≥ 40%, PF ≥ 1.2, DD ≤ 20%.

---

### PHASE 8 — Live Trading

Après ≥ 60 jours paper validés. Capital initial 50 USDT → target 1000 USDT.

---

## 5. Statut actuel — 2026-05-04

```
Phase 1 — Infrastructure + Backtesting       ✅ COMPLÈTE + AUDITÉE
Phase 2 — Optimisation initiale              ✅ COMPLÈTE + VALIDÉE

Phase 3 — Optimisation Multi-Régimes         🔄 EN COURS
  3A. Audit modules contextuels               ⬜ PRIORITÉ 1 (non fait)
  3B. Données                                 ✅ DB 2023-2026 disponible
  3C. BULL_TREND    (P1)                      ✅ Sharpe 2.404
      BULL_EXPLOSIVE(P6)                      ✅ Sharpe 1.703
      BEAR          (P2)                      ✅ NO-TRADE ZONE confirmé
      BEAR_PANIC    (P4)                      ⏭️  NO-TRADE par extension
      RANGE         (P3 train | P5 OOS)       ⬜ PROCHAINE ÉTAPE
  3D. regime_configs.json                     ⬜ Après P3 RANGE

Phase 4 — Context-Aware Filter               ⬜ Après Phase 3
Phase 5 — Détecteur Régime RT                ⬜ Après Phase 4
Phase 6 — Simulation switching               ⬜ Après Phase 5
Phase 7 — Paper Trading                      ⬜ Après Phase 6
Phase 8 — Live Trading                       ⬜ Après Phase 7
```

### Stubs Phase 7 (basse priorité actuellement)

`base_client.py` · `binance_client.py` · `paper_trading.py` ·
`trading_bot.py` · `error_handler.py` · `state_manager.py` ·
`performance_monitor.py` · `validator.py` ·
`discord_notifier.py` · `email_notifier.py`

---

## 6. Principes de développement

1. **Rien sans audit** — chaque module critique testé avant intégration.
2. **Reproductibilité** — optimizer = backtest direct (tolérance < 0.05 USDT).
3. **Réalisme** — `use_reset_capital=False`, slippage simulé, frais réels.
4. **Android** — séquentiel, RAM ≤ 1024 MB, pas de multiprocessing.
5. **Traçabilité** — chaque config documentée avec train + OOS.
6. **Taxonomie empirique** — les régimes sont définis par les données.
7. **Discipline de capital** — ne pas trader = ne pas perdre = alpha en BEAR.

---

## 7. Prochaine action immédiate

**RANGE — P3 (2024-05-01 → 2024-06-20)**

```bash
# config.json
"start_date": "2024-05-01",
"end_date":   "2024-06-20"

python optimize.py --phase 2a
python optimize.py --phase 2b
python optimize.py --phase 2c

# Puis OOS sur P5 (92j — test de robustesse maximal)
"start_date": "2024-08-05",
"end_date":   "2024-11-05"
python main.py backtest
```

---

*FuegoDev × Claude — Mis à jour à chaque évolution majeure du projet.*
