# BULLET-1 — Audit du Moteur de Backtesting

> **Session d'audit :** 10 — 2026-04-22  
> **Version auditée :** v2.2.2 → v2.2.3  
> **Auditeur :** FuegoDev + Claude (Anthropic)  
> **Statut final :** ✅ Moteur validé — 2 bugs corrigés, 6 points fiables confirmés

---

## 1. Contexte & Périmètre

### Objectif

Avant d'engager la Phase 2 (Optimizer) et la Phase 3 (Paper Trading), il est
impératif de garantir que les résultats produits par le moteur de backtesting
sont **fiables, exacts et reproductibles**. Un bug dans le backtesting conduirait
à optimiser sur de mauvaises métriques et invaliderait toutes les décisions
stratégiques qui en découlent.

### Données du backtest d'audit

| Paramètre | Valeur |
|---|---|
| Paire | BTC/USDT |
| Timeframe | 5m |
| Période | 2024-01-01 → 2024-01-31 |
| Capital initial | 100 USDT |
| Levier | 10× |
| Configuration | 8-reverse |
| Trailing stop | ATR |
| Breakout mode | permissive |
| R/R ratio | 2.0 |
| Total trades analysés | 42 (3 sessions) |

---

## 2. Méthode d'audit

L'audit a été conduit en deux étapes :

1. **Analyse statique** — Lecture du code source des modules critiques :
   `trading_engine.py`, `order_simulator.py`, `metrics.py`, `position_manager.py`
2. **Vérification empirique** — Recalcul indépendant de chaque métrique depuis
   les fichiers JSON de trades bruts (`session_XXX_trades.json`), puis comparaison
   avec les valeurs reportées dans `metrics.json` et `summary.txt`

### Modules inspectés

| Module | Version | Rôle |
|---|---|---|
| `trading_engine.py` | v2.8.2 | Orchestration session, construction trade_record |
| `order_simulator.py` | v2.6.8 | Simulation SL/TP, slippage, frais, funding |
| `position_manager.py` | v2.6.5 | Trailing stop, PnL net, protection 1R |
| `metrics.py` | v2.2.7 | Calcul Sharpe, Sortino, drawdown, win rate |
| `session_manager.py` | v2.5.9 | Capital, sessions, limites journalières |

---

## 3. Points validés ✅

### 3.1 Calcul PnL — Exact

**Méthode :** Recalcul indépendant de `pnl_gross` et `pnl_net` sur les 42 trades.

```
pnl_gross = (exit_price - entry_price) × size     [LONG]
pnl_gross = (entry_price - exit_price) × size     [SHORT]
pnl_net   = pnl_gross - entry_fees - exit_fees - funding_fees
```

**Résultat :** 0 écart > 0.02 USDT sur 42 trades. ✅

---

### 3.2 Ratio Risk/Reward — Respecté

**Méthode :** Vérification que `|TP - entry| / |entry - SL| ≈ 2.0` sur tous les trades.

**Résultat :** 0 trade avec écart > 0.15 (arrondi naturel sur prix BTC). ✅

---

### 3.3 Sizing des positions — Cohérent

**Méthode :** Vérification des trois équations de sizing :

```
notional = collateral × leverage
size     = notional / entry_price
```

**Résultat :** 0 écart > 0.5 USDT sur notional, 0 écart > 0.0001 BTC sur size. ✅

---

### 3.4 Continuité du capital entre sessions — Correcte

**Méthode :** Vérification que `capital_fin(session N) = capital_début(session N+1)`.

| Transition | Capital sortant | Capital entrant | Écart |
|---|---|---|---|
| S1 → S2 | 94.60 USDT | 94.60 USDT | 0.00 ✅ |
| S2 → S3 | 95.90 USDT | 95.90 USDT | 0.00 ✅ |

**Résultat :** Capital transmis fidèlement entre sessions, sans reset non
désiré ni perte. ✅

---

### 3.5 Exits SL/TP — Cohérence prix

**Méthode :**
- Pour les exits **sans trailing** : `exit_price ≈ initial_SL` (±slippage)
- Pour les exits **avec trailing** : `exit_price ≈ final_trailing_SL` (±slippage)

**Résultat — distribution des écarts SL exit vs final trailing SL :**

| Metric | Valeur |
|---|---|
| Écart minimum | 0.001% |
| Écart maximum | 0.131% |
| Écart moyen | 0.065% |
| Trades dans ±0.5% | 39/39 (100%) |

L'écart résiduel correspond exactement au slippage ATR dynamique simulé
(`slippage_base = 0.12%`, `slippage_max = 0.5%`). ✅

> **Note importante :** Le premier audit avait signalé 36 "issues" car il
> comparait le prix de sortie au SL *initial*, sans tenir compte du trailing
> stop. Après correction de la méthode d'audit (comparaison au SL *final*),
> aucune anomalie n'a été détectée.

---

### 3.6 Funding fees — Correctement appliquées

**Méthode :** Vérification que les funding fees sont prélevées uniquement
lors du franchissement des cycles 8h (00:00, 08:00, 16:00 UTC).

**Résultat :**

| Indicateur | Valeur |
|---|---|
| Trades avec funding fee | 10/42 |
| Trades sans funding fee | 32/42 |
| Montant funding moyen | ~0.019 USDT |

Seuls les trades dont la durée traverse un cycle de 8h reçoivent une
funding fee. Les 32 trades fermés à l'intérieur d'un cycle sont exempts. ✅

---

### 3.7 Cohérence `exit_reason` / `pnl_net` / `is_winner`

| Règle vérifiée | Résultat |
|---|---|
| TP exit → `pnl_net > 0` toujours | ✅ 3/3 TP positifs |
| `is_winner=True` → `pnl_net > 0` toujours | ✅ 0 contradiction |
| SL exit → peut être positif (trailing en profit) | ✅ Trade 1 : SL exit avec pnl_net = +0.45 USDT |

---

## 4. Bugs identifiés et corrigés 🔧

### Bug #1 — `pnl_pct` calculé sur le collateral (Gravité : ÉLEVÉE)

**Fichier :** `src/backtesting/trading_engine.py`

**Symptôme :**
```
Trade 1 — pnl_net = +0.449 USDT sur capital = 100 USDT
pnl_pct AVANT : +4.49%   ← calculé sur collateral (10 USDT)
pnl_pct APRÈS :  +0.45%  ← calculé sur capital_before (100 USDT)
```

**Cause :**
```python
# AVANT (bug)
'pnl_pct': round((pnl_net / collateral) * 100, 2)
# collateral = 10% du capital → résultat gonflé ×10 (= levier effectif)
```

**Impact :** `pnl_pct` alimentait `metrics._extract_returns_pct()` qui le
consommait comme base du Sharpe/Sortino. Tous les ratios de risque annualisés
étaient faux par un facteur ×10.

**Fix appliqué :** 3 changements dans `trading_engine.py` :
1. Capture de `capital_before = session_manager.get_capital_total()` au moment
   de l'ouverture de chaque trade
2. `pnl_pct = pnl_net / capital_before × 100` à la fermeture
3. Logger `log_trade_close` mis à jour pour cohérence d'affichage

**Vérification post-fix :**

| Session | `pnl_pct` 1er trade AVANT | `pnl_pct` APRÈS | Facteur |
|---|---|---|---|
| S1 | +4.49% | +0.449% | ×10.0 ✅ |
| S2 | +1.41% | +0.141% | ×10.0 ✅ |
| S3 | −3.64% | −0.364% | ×10.0 ✅ |

Écart max de recalcul indépendant : **0.000000%** — correction exacte.

---

### Bug #2 — Annualisation Sharpe/Sortino sur périodes courtes (Gravité : MOYENNE)

**Fichier :** `src/backtesting/metrics.py`

**Symptôme :**
```
Session 1 — 11 trades en 2 jours
trades_per_year = 11 / 2 × 365 = 2007
sqrt(2007)      = 44.8
Sharpe brut     = -1.19
Sharpe annualisé AVANT = -1.19 × 44.8 = -53.37  ← absurde
Sharpe annualisé APRÈS = 0.00 (guard < 30j)      ← correct
```

**Cause :** La formule d'annualisation `sharpe_raw × sqrt(trades_per_year)`
est mathématiquement valide sur des longues séries, mais produit des valeurs
sans signification statistique sur des sessions < 30 jours (trop peu de
points pour une loi des grands nombres).

**Impact :** Sharpe de `-52.60` au lieu de `0.00` (non calculable) pour
Session 1, `-14.40` au lieu de `0.00` pour Session 3. Ces valeurs
auraient rendu l'Optimizer de Phase 2 incapable de comparer les configurations.

**Fix appliqué :** 5 changements dans `metrics.py` :
1. Guard `if elapsed_days < _CAGR_MIN_DAYS: return 0.0` dans `calculate_sharpe_ratio()`
2. Guard identique dans `calculate_sortino_ratio()`
3. Champ `sharpe_note` dans `calculate_all_metrics()` (identique à `cagr_note`)
4. `sharpe_note: 'no_trades'` dans le fallback sans trades
5. Affichage `N/A (insufficient_period (< 30d))` dans `summary.txt`

**Vérification post-fix :**

| Session | Elapsed | Sharpe AVANT | Sharpe APRÈS | sharpe_note |
|---|---|---|---|---|
| S1 | 2.0j | -52.60 | 0.00 ✅ | `insufficient_period (< 30d)` |
| S2 | 0.3j | 0.00 | 0.00 ✅ | `insufficient_period (< 30d)` |
| S3 | 4.8j | -14.40 | 0.00 ✅ | `insufficient_period (< 30d)` |

---

## 5. Métriques stables — Intégrité confirmée

Ces métriques ne dépendent pas de `pnl_pct` et doivent être **bit-à-bit
identiques** avant et après les corrections. Vérification sur les résultats
produits par la machine de test (Termux/Android) :

| Métrique | S1 avant | S1 après | S2 avant | S2 après | S3 avant | S3 après |
|---|---|---|---|---|---|---|
| Total trades | 11 | 11 ✅ | 3 | 3 ✅ | 28 | 28 ✅ |
| Win rate % | 9.09 | 9.09 ✅ | 66.67 | 66.67 ✅ | 25.00 | 25.00 ✅ |
| Total PnL (USDT) | -5.39 | -5.39 ✅ | +1.31 | +1.31 ✅ | -4.84 | -4.84 ✅ |
| Profit factor | 0.08 | 0.08 ✅ | 5.59 | 5.59 ✅ | 0.45 | 0.45 ✅ |
| Max drawdown % | 5.81 | 5.81 ✅ | 0.30 | 0.30 ✅ | 5.55 | 5.55 ✅ |
| Capital final | 94.60 | 94.60 ✅ | 95.90 | 95.90 ✅ | 90.98 | 90.98 ✅ |

**Conclusion :** Les corrections n'ont modifié aucun calcul de trading.
Seule la couche de reporting (pnl_pct en %, ratios annualisés) a été corrigée.

---

## 6. Reproductibilité cross-plateforme

Le même backtest a été exécuté indépendamment sur deux environnements :

| Environnement | OS | Python | Résultat |
|---|---|---|---|
| Container audit | Linux (Ubuntu) | 3.12 | Référence |
| Machine de prod | Android 15 / Termux | 3.13 | Identique ✅ |

**Les 42 trades sont identiques** (mêmes entry/exit times, mêmes prix,
mêmes PnL) — le backtesting est **déterministe et reproductible**.

---

## 7. Limitations connues & points de vigilance

### 7.1 Sessions < 30 jours → Sharpe non calculé

Le guard de 30 jours signifie que sur les sessions 7-jours actuelles,
Sharpe et Sortino retournent `0.0` avec `sharpe_note: insufficient_period`.
**Ce comportement est intentionnel et correct.** Les ratios seront calculés :
- En Phase 2 (Optimizer) sur des backtests multi-sessions agrégés (≥ 30j)
- Lors de runs sur périodes plus longues (ex: 2024-01-01 → 2024-06-30)

### 7.2 CAGR non calculé sur sessions courtes

Identique au point 7.1 — `_CAGR_MIN_DAYS = 30` s'applique aussi au CAGR.
Normal et documenté.

### 7.3 Look-ahead bias — Confiance élevée, non prouvable formellement

Les timestamps d'entrée sont tous sur des frontières de 5 minutes, cohérent
avec une exécution à l'ouverture de la candle suivant le signal (T+1).
La protection look-ahead est documentée dans `trading_engine.py`
(`[FIX-LAB-2]`). Aucune violation détectée empiriquement.

### 7.4 Slippage simulé ≠ slippage réel

Le slippage est modélisé par `slippage_base = 0.12%` + ATR dynamique.
En conditions de marché extrêmes (liquidations en cascade, flash crash),
le slippage réel peut être significativement supérieur.
**Recommandation :** Tester avec `slippage_max = 1.0%` lors de l'optimisation.

---

## 8. Fichiers modifiés (v2.2.3)

| Fichier | Lignes modifiées | Nature |
|---|---|---|
| `src/backtesting/trading_engine.py` | ~920, ~1137, ~1219 | Bug #1 fix |
| `src/backtesting/metrics.py` | ~468, ~523, ~967, ~1021, ~1144 | Bug #2 fix |

---

## 9. Verdict final

| Composant | Statut |
|---|---|
| Calcul PnL (gross, net, fees) | ✅ Fiable |
| Sizing des positions | ✅ Fiable |
| Risk/Reward ratio | ✅ Fiable |
| Exits SL/TP avec trailing | ✅ Fiable |
| Funding fees | ✅ Fiable |
| Continuité du capital | ✅ Fiable |
| `exit_reason` / `is_winner` | ✅ Fiable |
| `pnl_pct` (% return par trade) | ✅ Corrigé en v2.2.3 |
| Sharpe / Sortino annualisés | ✅ Corrigé en v2.2.3 |
| Reproductibilité cross-plateforme | ✅ Confirmée |

**Le moteur de backtesting BULLET-1 est validé pour la Phase 2 (Optimizer).**

Les résultats qu'il produit sont fiables pour évaluer et comparer les 8
configurations de la stratégie Uncertainty Candle Enhanced.

---

*Audit réalisé lors de la Session 10 — 2026-04-22*  
*© BULLET-1 Project — FuegoDev — 2026*
