# BULLET-1 — Vision, Plan & Roadmap Complète

> **Document de référence du projet**
> Version : 1.1 — Mise à jour : 2026-05-02
> Auteur : FuegoDev × Claude (Anthropic)
> Statut actuel : **Phase 3 — Optimisation Multi-Régimes (en cours)**

---

## Changelog du document

| Version | Date | Changement |
|---|---|---|
| 1.0 | 2026-04-29 | Création initiale — taxonomie 3 régimes |
| 1.1 | 2026-05-02 | Découverte majeure : taxonomie révisée 5 régimes, résultats OOS BULL documentés, notes sur levier/ATR |

---

## 1. Contexte & Vision

### 1.1 Ce qu'est BULLET-1

BULLET-1 est un bot de trading algorithmique pour les marchés futures crypto (BTC/USDT, 5m, Binance Futures). Ce n'est pas un bot statique — c'est un système intelligent, autonome et adaptatif. La métaphore : un **sniper** qui sait quand entrer, comment, avec quelle arme, quelle stratégie, quel plan de sortie.

### 1.2 La vision finale — Architecture 5 niveaux

```
Marché en temps réel
      │
      ▼
NIVEAU 1 — Détecteur de Régime Macro
  BULL_TREND / BULL_EXPLOSIVE / BEAR / BEAR_PANIC / RANGE
  Signal discriminant : ratio ATR(7)/ATR(21) + ADX + vitesse
      │
      ▼
NIVEAU 2 — Micro-Contexte (heures → jours)
  Momentum / Squeeze / Structure / Divergences
  Modules : momentum.py, volatility.py, structure.py
      │
      ▼
NIVEAU 3 — Config Selector Dynamique
  Régime × Micro-contexte → Config optimale
  Base de données : regime_configs.json (5 entrées)
  Smooth switching : confirmation multi-périodes
      │
      ▼
NIVEAU 4 — Context-Aware Entry Filter
  Conditions indicateurs à l'entrée de chaque trade
  Winners vs losers analysis → score de confiance contextuel
      │
      ▼
NIVEAU 5 — Engine d'Exécution BULLET-1
  Config sélectionnée + filtres contextuels actifs
  Backtest / Paper / Live
```

### 1.3 Hypothèse centrale (à valider en Phase 4)

Pour des configurations données, la fréquence des trades gagnants est significativement supérieure quand certains indicateurs réunissent des conditions spécifiques au moment de l'entrée.

```
Trade_score = signal_qualité × contexte_multiplicateur
contexte_multiplicateur ∈ [0.0, 1.0]
→ Si score < seuil → trade ignoré même si signal valide
```

---

## 2. Taxonomie des régimes — 5 régimes (v1.1)

> **Découverte empirique — 2026-05-02**
> L'hypothèse initiale à 3 régimes (BULL/BEAR/RANGE) est insuffisante.
> Les tests OOS BTC/USDT 2024 montrent que P1 BULL et P6 BULL nécessitent
> des configurations **opposées** sur des paramètres fondamentaux.

### 2.1 Définition des 5 régimes

**BULL_TREND**
Hausse progressive, tendancielle (ex: P1 ETF Rally). Vitesse ~+1%/j, ATR stable.
Signal ID : `ATR(7)/ATR(21) < 1.3` + ADX croissant
Config : 4-normal | ATR21 | mult=2.0 | volume=OFF

**BULL_EXPLOSIVE**
Hausse verticale, parabolique (ex: P6 Election Rally). Vitesse ~+1.5%/j, ATR en forte expansion.
Signal ID : `ATR(7)/ATR(21) > 1.5` + bougies >2×ATR
Config : 1-normal | ATR7 | mult=3.0 | volume=ON

**BEAR** *(subdivision probable — à confirmer)*
Baisse progressive (ex: P2 correction post-ATH). À optimiser sur P2.

**BEAR_PANIC** *(hypothèse — à confirmer)*
Chute brutale, liquidations en cascade (ex: P4 Black Monday). Si P2 et P4 divergent comme P1 et P6, deux configs BEAR seront nécessaires.

**RANGE**
Consolidation sans direction (ex: P3 post-halving, P5 pré-élection). À optimiser sur P3.

### 2.2 Pourquoi BULL_TREND ≠ BULL_EXPLOSIVE

| Paramètre | BULL_TREND (P1) | BULL_EXPLOSIVE (P6) |
|---|---|---|
| `configuration_name` | `4-normal (</<)` | `1-normal (>/>)` — **opposé** |
| `atr_parameters.period` | `21` (lent) | `7` (rapide) |
| `atr_mode.base_multiplier` | `2.0` (serré) | `3.0` (large) |
| `volume_confirmation` | `False` | `True` |
| Sharpe train | 2.404 | 1.703 |
| OOS croisé | ❌ -2.167 sur P6 | ⚠️ 4 trades sur P1 |

Ce ne sont pas deux ajustements de paramètres — c'est une logique de signal différente.

### 2.3 Notes importantes sur les paramètres

**Levier :** 10×/15×/20× donnent un Sharpe quasi-identique. Mais le DD double avec le levier. Le bon critère est Sharpe/DD, pas le PnL brut. **Levier 20× déconseillé en standard.**

**ATR trailing mult :** La range [0.5, 1.0, 1.5] est inadaptée — avec SL=1.0%, mult=0.5 génère des sorties prématurées sur ~100% des trades. **Range correcte pour futures optimisations : [1.5, 2.0, 2.5, 3.0, 3.5].**

### 2.4 Timeline 2024 — Base empirique

```
P1  BULL_TREND      2024-01-01 → 2024-03-14  73j  +74.5%  Optimisé ✅
P2  BEAR            2024-03-14 → 2024-05-01  48j  -23.4%  À optimiser ⬜
P3  RANGE           2024-05-01 → 2024-06-20  50j  ±12.5%  À optimiser ⬜
P4  BEAR_PANIC?     2024-06-20 → 2024-08-05  46j  -26.5%  OOS BEAR ⬜
P5  RANGE           2024-08-05 → 2024-11-05  92j  ±16%    OOS RANGE ⬜
P6  BULL_EXPLOSIVE  2024-11-05 → 2024-12-15  40j  +59%    Optimisé ✅
P7  BEAR            2024-12-15 → 2024-12-31  16j  -14.5%  Trop court
```

---

## 3. Infrastructure construite

### 3.1 Modules complétés ✅

`data_loader.py` v3.0.0 · `db_manager.py` v1.0.0 · `data_validator.py` · `data_processor.py` · `uncertainty_candle.py` v2.2.2 · `volume.py` v2.4.3 · `trend.py` v2.5.2 · `atr.py` v2.3.4 · `momentum.py` v2.1.2 · `structure.py` v2.1.2 · `volatility.py` v2.1.2 · `regime.py` v2.1.2 · `risk_manager.py` v2.3.3 · `position_manager.py` v2.6.5 · `session_manager.py` v2.5.9 · `signal_generator.py` v2.4.6 · `strategy.py` v2.2.11 · `market_context.py` v2.1.2 · `order_simulator.py` v2.6.8 · `metrics.py` v2.2.7 · `engine.py` v2.2.2 · `trading_engine.py` v2.8.2 · `analytics_engine.py` v2.1.5 · `report_generator.py` v2.2.3 · `optimizer.py` v2.0.0

### 3.2 Données disponibles

`bullet1_market_data.db` — BTC/USDT 5m+15m — 2023-11-01 → 2026-03-31 ✅

### 3.3 Configurations optimisées

**BULL_TREND** (P1 train, Sharpe=2.404) :
`4-normal` | qs=20 | lev=10× | SL=1.0% | trail=atr | mult=2.0 | progressive=OFF | breakout=permissive | trend=OFF | vol=OFF | atr_period=21 | body_max=10%

**BULL_EXPLOSIVE** (P6 train, Sharpe=1.703) :
`1-normal` | qs=20 | lev=15× | SL=1.0% | trail=hybrid | mult=3.0 | progressive=ON | vol=ON | atr_period=7 | body_max=10%

---

## 4. Roadmap détaillée

### PHASE 3 — Optimisation Multi-Régimes (EN COURS)

#### 3A — Audit modules contextuels (PRIORITÉ 1 — non encore fait)

| Module | Risque principal |
|---|---|
| `regime.py` | Faux signaux + ne distingue pas encore BULL_TREND/EXPLOSIVE |
| `momentum.py` | Divergences sur données insuffisantes |
| `structure.py` | Anchoring VWAP sur longues périodes |
| `volatility.py` | Squeeze trop sensible sur 5m |
| `market_context.py` | 5 `pass` → chemins non testés |

Critère : `regime.py` doit classifier les 7 phases 2024 avec accord ≥ 70% (±3 jours sur transitions) **et** calculer `ATR(7)/ATR(21)` pour distinguer les sous-régimes BULL.

#### 3B — Données : ✅ disponibles

#### 3C — Optimisations par régime

| Étape | Train | Régime | OOS | Statut |
|---|---|---|---|---|
| ✅ | P1 : 01/01 → 03/14 | BULL_TREND | P6 ❌ | Terminé |
| ✅ | P6 : 11/05 → 12/15 | BULL_EXPLOSIVE | P1 ⚠️ | Terminé |
| ⬜ | P2 : 03/14 → 05/01 | BEAR | P4 | **PROCHAIN** |
| ⬜ | P4 : 06/20 → 08/05 | BEAR_PANIC? | P2 | OOS BEAR |
| ⬜ | P3 : 05/01 → 06/20 | RANGE | P5 | Après BEAR |
| ⬜ | P5 : 08/05 → 11/05 | RANGE | P3 | OOS RANGE |

Procédure pour chaque régime :
```bash
# 1. Modifier start_date/end_date dans config.json
# 2. Lancer 2A+2B+2C (~9h total)
python optimize.py --phase 2a
python optimize.py --phase 2b
python optimize.py --phase 2c
# 3. Valider backtest direct (doit matcher l'optimizer)
# 4. Modifier dates → période OOS → python main.py backtest
# 5. Évaluer : Sharpe_OOS / Sharpe_TRAIN ≥ 0.7 ? → robuste
```

#### 3D — regime_configs.json

```json
{
  "version": "1.0",
  "regimes": {
    "BULL_TREND":    { "config": {}, "train_sharpe": 2.404, "oos_sharpe": -2.167, "detection": "ATR(7)/ATR(21) < 1.3" },
    "BULL_EXPLOSIVE":{ "config": {}, "train_sharpe": 1.703, "oos_sharpe": null,   "detection": "ATR(7)/ATR(21) > 1.5" },
    "BEAR":          { "config": {}, "train_sharpe": null,  "oos_sharpe": null,   "detection": "À définir" },
    "BEAR_PANIC":    { "config": {}, "train_sharpe": null,  "oos_sharpe": null,   "detection": "À définir (hypothèse)" },
    "RANGE":         { "config": {}, "train_sharpe": null,  "oos_sharpe": null,   "detection": "À définir" }
  }
}
```

---

### PHASE 4 — Context-Aware Entry Filter

#### 4A — Script d'analyse post-trade (winners vs losers)

`src/analysis/trade_context_analyzer.py` — extrait le snapshot `MarketContextCapture` à chaque `entry_time`, compare les ~50 features pour winners vs losers, calcule l'importance de chaque feature.

À lancer après Phase 3C complète (~100+ trades analysables).

#### 4B — Phase 2D optimizer (context guards)

Tester des filtres contextuels dans la grille d'optimisation. Contrainte : `min_trades_per_session ≥ 1.5` en moyenne.

#### 4C — Context Score dynamique

```python
# src/core/context_scorer.py
class ContextScorer:
    def score(self, snapshot: Dict) -> float:
        """Retourne un multiplicateur de confiance [0.0, 1.0]."""
```

---

### PHASE 5 — Détecteur de Régime Temps Réel

Calibration de `regime.py` + signal `ATR(7)/ATR(21)` sur les 7 phases 2024. Multi-timeframe (5m + 15m). Smooth switching avec confirmation ≥ 3 sessions consécutives + confiance ≥ 0.65 avant de changer de config.

---

### PHASE 6 — Simulation switching dynamique

Simuler le Config Selector sur 2024 complet. Comparer config fixe vs switching dynamique. Métriques : Sharpe annuel, switchings erronés, DD aux transitions.

---

### PHASE 7 — Paper Trading

Prérequis : Phases 3-6 terminées + `binance_client.py`, `paper_trading.py`, `trading_bot.py` implémentés. Durée minimale : 30 jours paper.

---

### PHASE 8 — Live Trading

Après ≥ 60 jours paper, WR ≥ 40%, PF ≥ 1.2, DD ≤ 20%. Capital initial 50 USDT → target 1000 USDT.

---

## 5. Statut actuel — 2026-05-02

```
Phase 1 — Infrastructure + Backtesting     ✅ COMPLÈTE + AUDITÉE
Phase 2 — Optimisation initiale            ✅ COMPLÈTE + VALIDÉE

Phase 3 — Optimisation Multi-Régimes       🔄 EN COURS
  3A. Audit modules contextuels             ⬜ PRIORITÉ 1 (non fait)
  3B. Données                               ✅ DB 2023-2026 disponible
  3C. BULL_TREND    (P1 train | P6 OOS)    ✅ Terminé
      BULL_EXPLOSIVE(P6 train | P1 OOS)    ✅ Terminé
      BEAR          (P2 train | P4 OOS)    ⬜ PROCHAINE ÉTAPE
      RANGE         (P3 train | P5 OOS)    ⬜ Après BEAR
  3D. regime_configs.json                   ⬜ Après 3C complet

Phase 4 — Context-Aware Filter             ⬜ Après Phase 3
Phase 5 — Détecteur Régime RT              ⬜ Après Phase 4
Phase 6 — Simulation switching             ⬜ Après Phase 5
Phase 7 — Paper Trading                    ⬜ Après Phase 6
Phase 8 — Live Trading                     ⬜ Après Phase 7
```

---

## 6. Principes de développement

1. **Rien sans audit** — chaque module critique est testé avant intégration.
2. **Reproductibilité** — optimizer = backtest direct (tolérance < 0.05 USDT).
3. **Réalisme** — `use_reset_capital=False`, slippage simulé, frais réels.
4. **Android** — séquentiel, RAM ≤ 1024 MB, pas de multiprocessing.
5. **Traçabilité** — chaque config documentée avec train + OOS.
6. **Taxonomie empirique** — les régimes sont définis par les données, pas par des suppositions. Si BEAR se subdivise comme BULL, on adapte.

---

## 7. Prochaine action

**BEAR — P2 (2024-03-14 → 2024-05-01)**

```bash
# config.json
"start_date": "2024-03-14",
"end_date":   "2024-05-01"

python optimize.py --phase 2a   # ~8h
python optimize.py --phase 2b   # ~30min
python optimize.py --phase 2c   # ~25min

# Puis OOS sur P4
"start_date": "2024-06-20",
"end_date":   "2024-08-05"
python main.py backtest
```

---

*FuegoDev × Claude — Mis à jour à chaque évolution majeure du projet.*
