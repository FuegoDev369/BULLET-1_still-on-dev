# BULLET-1 — Changelog

Traçabilité de toutes les évolutions significatives du projet.
Format : `[VERSION] YYYY-MM-DD — Description`

---

## [2.2.0] 2026-04-21 — Migration données CSV → SQLite

### Motivation
Le système CSV (`data/historical/**/*.csv`) présentait plusieurs limitations :
pas de requêtes partielles, pas de déduplication native, pas de métadonnées
intégrées, téléchargements redondants en cas de re-run, incompatible avec un
futur support multi-paires sans explosion du nombre de fichiers.

### Fichiers créés

| Fichier | Description |
|---|---|
| `src/data/db_manager.py` v1.0.0 | Couche d'accès SQLite : schéma, insert, query, métadonnées |
| `data/migrate_csv_to_db.py` v1.0.0 | Script one-time : importe les CSV existants vers la DB |
| `data/download_data_v3.0.py` v3.0.0 | Remplace `download_data_multi_exchange_v2.3.py` — sauvegarde SQLite + mode incrémental |
| `CHANGELOG.md` | Ce fichier |

### Fichiers modifiés

| Fichier | Avant | Après | Nature du changement |
|---|---|---|---|
| `src/data/data_loader.py` | v2.4.1 (CSV) | v3.0.0 (SQLite) | Réécriture complète du backend, interface publique identique |
| `src/backtesting/ohlcv_data_engine.py` | v2.2.3 | v2.3.0 | Hash CSV → empreinte DB, gestion d'erreurs mise à jour |
| `config/config.json` | `backtesting.data_path` (CSV) | `data.db_path` (SQLite) | Remplacement du chemin de données |

### Schéma SQLite (`bullet1_market_data.db`)

```
ohlcv    (id, exchange, symbol, timeframe, timestamp[ms], open, high, low, close, volume)
         UNIQUE(exchange, symbol, timeframe, timestamp)
         INDEX(exchange, symbol, timeframe, timestamp)

datasets (id, exchange, symbol, timeframe, first_ts, last_ts, candle_count, source, updated_at)
         UNIQUE(exchange, symbol, timeframe)
```

### Rétrocompatibilité

L'interface publique de `DataLoader` est identique — aucun changement requis
dans les modules appelants (`ohlcv_data_engine`, `engine`, `trading_engine`).
Seul le paramètre `csv_path` (présent dans `load()`) est supprimé — il n'était
pas utilisé en dehors des tests unitaires.

### Migration des données existantes

```bash
# Importer les CSV existants (one-time)
python data/migrate_csv_to_db.py

# Vérifier les datasets disponibles
python data/download_data_v3.0.py  # Menu → État DB
```

### Fichiers legacy conservés

`data/historical/BTC-USDT/5min.csv` et `15min.csv` sont conservés jusqu'à
validation complète de la migration. Ils peuvent ensuite être archivés ou supprimés.

---

## [2.1.0] 2026-03-15 — Phase 1 quasi-complète (Session 8)

Dernière session avant migration SQLite. Tous les modules substantiels de
la Phase 1 implémentés :
`order_simulator` v2.6.8, `metrics` v2.2.7, `engine` v2.2.2,
`trading_engine` v2.8.2, `analytics_engine` v2.1.5, `report_generator` v2.2.3.

---

*© BULLET-1 Project — FuegoDev — 2026*

---

## [2.2.1] 2026-04-21 — Lisibilité DB + main.py/backtest.py

### Timestamps SQLite — Lisibilité

**Contexte :** Les timestamps dans la table `ohlcv` sont stockés en Unix
millisecondes (entier, ex: `1698797700000`). C'est le format standard SQLite
pour les séries temporelles (indexation O(log N), 8 bytes, sans ambiguïté
de timezone). Mais ce format est illisible directement par un humain.

**Solution ajoutée :**
- `datasets_readable` VIEW dans `db_manager.py` — convertit automatiquement
  `first_ts` / `last_ts` / `updated_at` en dates texte UTC (`YYYY-MM-DD HH:MM`).
  Accessible depuis DB Browser for SQLite : `SELECT * FROM datasets_readable;`
- `data/db_status.py` — script CLI qui affiche l'état complet de la base
  avec dates lisibles, durées, nombre de bougies et point de reprise
  pour les mises à jour incrémentales.

### main.py v1.1.0 et backtest.py v1.1.0

Implémentation fonctionnelle pour l'état actuel de BULLET-1 :

| Fichier | Avant | Après |
|---|---|---|
| `main.py` | Stub TODO | Implémenté — appelle `Engine().run()` en mode backtest |
| `backtest.py` | Stub TODO | Implémenté — wrapper direct pour lancement backtest |

- Mode `backtest` : fonctionnel — lance `Engine` complet
- Mode `paper`    : stub documenté (Phase 3 non disponible)
- Mode `live`     : stub documenté (Phase 4 non disponible)
- Gestion d'erreurs : `FileNotFoundError`, `ValueError`, `KeyboardInterrupt`
- Les deux scripts seront mis à jour à mesure de l'avancement des phases


---

## [2.2.2] 2026-04-22 — Correctifs critiques (Session 9 suite)

### Bug fix — `get_project_root()` (helpers.py)

**Symptôme :** `python main.py backtest` échouait avec
`config/volume_config.json introuvable` malgré la présence du fichier.

**Cause :** Le fallback de `get_project_root()` remontait 2 niveaux depuis
`src/utils/helpers.py` (`current.parent.parent = src/`) au lieu de 3
(`current.parent.parent.parent = BULLET-1/`). La fonction cherchait donc
les configs dans `src/config/` au lieu de `config/`.

**Fix :** `return current.parent.parent.parent` dans le fallback.

**Portée :** Tous les modules qui appellent `get_project_root()` bénéficient
du fix (engine.py, data_loader.py, helpers.py interne, etc.).

### Amélioration — Lisibilité table `datasets`

**Contexte :** La table `datasets` stockait `first_ts` et `last_ts` en
Unix ms (ex: `1698797700000`), format interne illisible directement.

**Solution :** Ajout de deux colonnes TEXT dans la table `datasets` :
- `first_date_utc` — ex: `"2023-11-01 00:00"` (UTC)
- `last_date_utc`  — ex: `"2024-03-11 23:45"` (UTC)

Ces colonnes sont renseignées automatiquement à chaque `insert_candles()`.
Les colonnes `first_ts`/`last_ts` sont conservées pour les calculs internes
(fingerprint, arithmétique de durée).

La VIEW `datasets_readable` utilise maintenant ces colonnes directement.

**Sur ta DB existante** — re-migrer pour peupler les nouvelles colonnes :
```bash
python data/migrate_csv_to_db.py
# Puis vérifier :
python data/db_status.py
```


---

## [2.2.3] 2026-04-22 — Correctifs métriques de performance (Session 10)

### Contexte

Après le premier backtest end-to-end réussi, un audit complet du moteur de
backtesting a été conduit (voir `AUDIT_BACKTEST.md`). Deux bugs dans la couche
de reporting ont été identifiés et corrigés. Le moteur de trading lui-même
(calcul PnL, SL/TP, sizing, trailing, funding) a été validé sans anomalie.

### Bug #1 — `pnl_pct` calculé sur `collateral` au lieu de `capital_before`

**Gravité : ÉLEVÉE** — Tous les ratios de risque annualisés étaient faux.

**Cause :** Dans `trading_engine.py`, `pnl_pct` était calculé comme
`pnl_net / collateral × 100`. Avec `collateral_percentage = 10%`, le
résultat était gonflé d'un facteur ×10 (= levier effectif).

**Exemple concret :**
```
pnl_net = +0.449 USDT sur capital = 100 USDT
AVANT : pnl_pct = 0.449 / 10.0 × 100 = +4.49%   ← FAUX
APRÈS : pnl_pct = 0.449 / 100.0 × 100 = +0.449%  ← CORRECT
```

**Fix :** Ajout du champ `capital_before` (capital total capturé au moment
de l'ouverture du trade) dans le `trade_record`. `pnl_pct` est désormais
calculé sur cette base réelle.

**Impact :** `metrics._extract_returns_pct()` utilise `capital_before` en
priorité — les calculs Sharpe/Sortino sont maintenant basés sur les vrais
% returns.

| Fichier | Changement |
|---|---|
| `src/backtesting/trading_engine.py` | Ajout `capital_before` dans `trade_record` à l'ouverture |
| `src/backtesting/trading_engine.py` | `pnl_pct = pnl_net / capital_before × 100` à la fermeture |
| `src/backtesting/trading_engine.py` | Logger `log_trade_close` corrigé pour cohérence |

### Bug #2 — Annualisation Sharpe/Sortino sur périodes < 30 jours

**Gravité : MOYENNE** — Valeurs absurdes et non interprétables reportées.

**Cause :** Sur des sessions de 2–5 jours, `sqrt(trades_per_year)` atteignait
×44 ou plus, rendant le Sharpe annualisé statistiquement non significatif.

**Exemple concret :**
```
Session 1 : 11 trades en 2 jours
trades_per_year = 11 / 2 × 365 = 2007  →  sqrt(2007) = 44.8
Sharpe brut = -1.19
AVANT : Sharpe annualisé = -1.19 × 44.8 = -53.37  ← absurde
APRÈS : 0.00 avec sharpe_note = "insufficient_period (< 30d)"
```

**Fix :** Guard `if elapsed_days < _CAGR_MIN_DAYS: return 0.0` dans
`calculate_sharpe_ratio()` et `calculate_sortino_ratio()`. Cohérent avec le
guard existant sur le CAGR (`_CAGR_MIN_DAYS = 30`).

**Nouveau champ `sharpe_note`** dans `metrics.json` (identique à `cagr_note`) :
- `None` si période ≥ 30 jours (Sharpe calculable)
- `"insufficient_period (< 30d)"` si période trop courte
- `"no_trades"` si aucun trade

| Fichier | Changement |
|---|---|
| `src/backtesting/metrics.py` | Guard `elapsed_days < 30` dans `calculate_sharpe_ratio()` |
| `src/backtesting/metrics.py` | Guard identique dans `calculate_sortino_ratio()` |
| `src/backtesting/metrics.py` | Champ `sharpe_note` dans `calculate_all_metrics()` |
| `src/backtesting/metrics.py` | Fallback `sharpe_note: 'no_trades'` |
| `src/backtesting/metrics.py` | Affichage `N/A (...)` dans `summary.txt` |

### Nouveau fichier créé

| Fichier | Description |
|---|---|
| `AUDIT_BACKTEST.md` | Rapport d'audit complet du moteur de backtesting |

### Validation cross-plateforme

Les corrections ont été validées par comparaison directe :
- Machine d'audit (Linux/Python 3.12) → résultats de référence
- Machine de production (Android 15 / Termux / Python 3.13) → résultats identiques
- 42 trades identiques bit-à-bit entre les deux runs
- 18/18 vérifications automatisées passées ✅

### Métriques non impactées

Total PnL, Win Rate, Profit Factor, Drawdown, capital final — **inchangés**.
Les corrections n'ont touché que la couche de reporting (% returns, ratios
annualisés), pas la logique de trading.


---

## [2.3.0] 2026-04-23 — Phase 2 : Optimizer (Session 11)

### Nouveaux fichiers

| Fichier | Description |
|---|---|
| `src/backtesting/optimizer.py` v1.0.0 | Moteur grid search Phase 2 |
| `optimize.py` v1.0.0 | CLI d'entrée pour l'optimisation |

### Fichiers modifiés

| Fichier | Changement |
|---|---|
| `config/config.json` | Section `optimization` remplacée par version calibrée Phase 2 |
| `main.py` v1.2.0 | Mode `optimize` ajouté (`python main.py optimize`) |

### Architecture Optimizer

**Grid search séquentiel** (Android/Termux — pas de multiprocessing).

**Grille par défaut :**
- 8 configurations stratégie (1-normal → 8-reverse)
- SL offset : 0.4%, 0.8%, 1.2%
- Levier : 5×, 10×, 15×
- Trailing stop : atr, percentage
- Min quality score : 8, 11, 14
- **Total : 432 runs** (~86 min sur Android, base 12s/run)

**Métriques agrégées cross-sessions** : tous les trades de toutes les sessions
d'un run sont poolés dans une seule instance `Metrics` → Sharpe/Sortino
calculables si la période totale ≥ 30 jours.

**Score primaire** : `sharpe_ratio` si période ≥ 30j, sinon `profit_factor`.
Cohérent avec le guard de metrics.py (`_CAGR_MIN_DAYS = 30`).

**Mode silencieux** : pendant l'optimisation, `analytics_engine_config.json`
est temporairement remplacé pour désactiver HTML/charts.
Restauration garantie dans le bloc `finally` — safe même sur Ctrl+C.

**Sorties** dans `results/optimization/` :
- `optimization_results_TIMESTAMP.json` — tous les résultats
- `optimization_report_TIMESTAMP.txt` — rapport texte lisible
- `best_config_TIMESTAMP.json` — meilleure config prête à l'emploi

### Usage

```bash
# Lancement complet (432 runs avec config par défaut)
python optimize.py
# ou
python main.py optimize

# Test rapide (sous-ensemble)
# Modifier config.json → "strategy_configurations": ["8-reverse", "1-normal"]
```

### Validation

5/5 tests unitaires passés :
- `_build_grid()` : product cartésien correct
- `OptimizationResult.primary_score` : sharpe vs profit_factor selon période
- `_rank_results()` : tri + gestion invalides
- `to_dict()` : sérialisation JSON
- `_CONFIG_MAP` : 8 configurations cohérentes


---

## [2.4.0] 2026-04-25 — Optimizer v2.0.0 — Extension multi-phases (Session 12)

### Contexte

Extension de l'optimiseur pour couvrir les paramètres des indicateurs externes
(ATR, Trend, UncertaintyCandle) et les toggles avancés de la stratégie (breakout,
trend_filter, volume_confirmation, trailing ATR progressif).

### Architecture — 3 phases séquentielles

| Phase | Contenu | Runs | Durée estimée Android |
|---|---|---|---|
| 2A | 8 configs × SL × levier × trailing × quality | 432 | ~86 min |
| 2B | Indicateurs externes (ATR/Trend/UC) | 162 | ~32 min |
| 2C | Toggles avancés + trailing ATR progressif | 144 | ~28 min |
| **Total** | | **738** | **~146 min (~2.5h)** |

Chaque phase utilise la meilleure config de la phase précédente comme base.

### Fichiers modifiés

| Fichier | Version | Changement |
|---|---|---|
| `src/backtesting/optimizer.py` | v2.0.0 | Réécriture complète |
| `optimize.py` | v2.0.0 | Support `--phase [2a\|2b\|2c\|all]` |
| `config/config.json` | — | Section `optimization` restructurée en 3 phases |
| `main.py` | v1.3.0 | Mode `optimize` redirige vers Phase 2A |

### Nouvelles fonctionnalités

**Support des configs externes :** Nouveau champ `"config_file"` dans
`parameters_to_optimize` → permet d'optimiser des paramètres dans
`atr_config.json`, `trend_config.json`, `uncertainty_candle_config.json`.
Chaque fichier est patché avant le run et restauré dans le `finally`.

**Skipping conditionnel :** Nouveau champ `"condition"` dans un paramètre :
`{"param": "strategy.volume_confirmation.enabled", "value": true}` →
le paramètre est ignoré si la condition n'est pas remplie. Évite les combos
sans sens (ex: `volume.mode` quand `volume.enabled=false`).

**Stages progressive_tightening corrélés :**
Quand `progressive_tightening=True`, les stages sont auto-calculés
proportionnellement au `base_multiplier` actuel :
```
base_multiplier=2.0 → stages=[1.70, 1.30, 0.90, 0.50]
base_multiplier=2.5 → stages=[2.12, 1.62, 1.12, 0.62]
base_multiplier=3.0 → stages=[2.55, 1.95, 1.35, 0.75]
```

**Déduplication déterministe :** Algorithme product-complet → forcer `None`
sur les params conditionnels non actifs → déduplication par signature.
Garantit l'exactitude des combinaisons uniques quel que soit l'ordre des params.

### Paramètres optimisés

**Phase 2A** — `config.json` :
- `stop_loss_offset_pct` : [0.4, 0.8, 1.2]%
- `leverage` : [5, 10, 15]×
- `trailing_stop_type` : [atr, hybrid]
- `min_quality_score` : [8, 11, 14]

**Phase 2B** — Configs externes :
- `uncertainty_candle_config.json → detection.body_max_pct` : [10, 20, 30]
- `atr_config.json → atr_parameters.period` : [7, 14, 21]
- `atr_config.json → atr_parameters.base_multiplier` : [2.0, 2.5, 3.0]
- `trend_config.json → moving_averages.slope_calculation_periods` : [3, 5]
- `trend_config.json → trend_detection.trend_strength_threshold` : [0.002, 0.004, 0.006]

**Phase 2C** — `config.json` toggles avancés :
- `strategy.signal_generator.breakout_detection_mode` : [permissive, strict]
- `strategy.trend_filter.enabled` : [false, true]
- `strategy.trend_filter.allow_counter_trend` : [false, true] *(si trend_filter=true)*
- `strategy.volume_confirmation.enabled` : [false, true]
- `strategy.volume_confirmation.mode` : [basic, directional, advanced] *(si enabled=true)*
- `strategy.trailing_stop.atr_mode.base_multiplier` : [2.0, 2.5, 3.0]
- `strategy.trailing_stop.atr_mode.progressive_tightening` : [false, true]

### Validation

8/8 tests unitaires passés :
- `_get_nested / _set_nested`
- `_compute_stages` — décroissant et proportionnel
- Phase 2A — produit cartésien sans overrides
- Phase 2B — `external_overrides` peuplés correctement
- Skipping conditionnel simple (volume)
- Double condition indépendante (trend × volume = 9 combos)
- `progressive_tightening` → stages injectés uniquement si `True`
- Grilles réelles 2B=162 runs, 2C=144 runs

### Usage

```bash
# Phase 2A (défaut — point de départ obligatoire)
python optimize.py --phase 2a

# Phase 2B (après avoir appliqué best_config_2A.json)
python optimize.py --phase 2b

# Phase 2C (après avoir appliqué best_config_2B.json)
python optimize.py --phase 2c

# Tout en séquentiel avec confirmation entre phases
python optimize.py --phase all
```


---

## [2.4.1] 2026-04-25 — Correctifs fiabilité Optimizer + sessions vides (Session 13)

### Contexte

Après le premier run complet de la Phase 2A (432 runs), un backtest direct
avec la meilleure config a produit des résultats radicalement différents de
ceux reportés par l'optimizer (PnL +3.05 vs -0.62, 26 trades vs 8 trades).
Audit complet effectué → 3 causes identifiées et corrigées.

---

### Bug #1 — `use_reset_capital=True` : conditions d'évaluation irréalistes (CRITIQUE)

**Cause :**
L'optimizer utilisait `use_reset_capital=True` : chaque session redémarre
à 100 USDT, indépendamment des sessions précédentes. Le backtest standard
utilise `reset_capital=False` : capital cumulatif entre sessions.

Résultat : l'optimizer évaluait les configurations dans un environnement
artificiel non représentatif du trading réel.

| | Optimizer (avant fix) | Backtest direct |
|---|---|---|
| `reset_capital` | True (artificiel) | False (réel) |
| Capital par session | 100 USDT fixe | Cumulatif |
| Trades Phase 2A best | 26 | 8 |
| PnL Phase 2A best | +3.0545 USDT | -0.6160 USDT |

**Fix :** `use_reset_capital = false` dans `config.json → optimization`.
L'optimizer évalue maintenant dans les mêmes conditions que le vrai backtest.
Les scores seront plus représentatifs de la réalité (potentiellement plus bas,
mais fiables).

**Impact :** La Phase 2A doit être relancée. Les résultats précédents
(Sharpe=1.222, best_config `1-normal`) ne sont pas fiables.

---

### Bug #2 — 1296 runs au lieu de 162 en Phase 2B (ÉLEVÉ)

**Cause :**
`optimize.py → _run_phase()` ne fixait pas `strategy_configurations` pour
les phases 2B et 2C. L'optimizer itérait sur les 8 configurations au lieu
de fixer la meilleure config de Phase 2A.

`1296 = 8 configs × 162 combinaisons` au lieu de `162 = 1 config × 162`

**Fix :** En Phase 2B et 2C, `strategy_configurations` est automatiquement
fixé à la configuration active dans `config.json` :
```python
if phase_key in ("2b", "2c"):
    best_config_name = cfg.get("strategy", {}).get("configuration_name")
    if best_config_name:
        cfg_modified["optimization"]["strategy_configurations"] = [best_config_name]
```

---

### Bug #3 — `Initial Capital: 0.00 USDT` sur sessions sans trades (MOYEN)

**Cause :**
`Metrics._make_empty_metrics()` était une `@staticmethod` sans accès à
`self.initial_capital`. La clé `initial_capital` était absente du dict
retourné → `analytics_engine` recevait `None` → fallback hardcodé à
`1000.0 USDT` puis affiché `0.00` dans `summary.txt`.

**Fix 1 — `metrics.py` :** `_make_empty_metrics(initial_capital)` accepte
maintenant le capital initial en paramètre. `calculate_all()` passe
`self.initial_capital` à l'appel.

**Fix 2 — `analytics_engine.py` :** Chaîne de fallback pour `initial_capital` :
1. `session_summary['initial_capital']` (source principale)
2. `session_summary['initial_funds']` (alias session_manager)
3. `trades[0]['capital_before']` (fallback trades)
4. `100.0` USDT (dernier recours)

Suppression du fallback hardcodé à `1000.0 USDT` qui masquait les vrais
problèmes d'initialisation.

---

### Fichiers modifiés

| Fichier | Lignes | Changement |
|---|---|---|
| `config/config.json` | optimization | `use_reset_capital: false` |
| `optimize.py` | `_run_phase()` | Fix strategy_configurations 2B/2C |
| `src/backtesting/metrics.py` | `_make_empty_metrics` | Paramètre `initial_capital` |
| `src/backtesting/analytics_engine.py` | ~296-340 | Chaîne fallback initial_capital |

### Validation

4/4 tests unitaires passés.

### Action requise

**Relancer la Phase 2A** avec la config corrigée (`use_reset_capital=False`) :
```bash
python optimize.py --phase 2a
```
Les résultats seront directement comparables aux backtests standard.


---

## [2.4.2] 2026-04-27 — Fix contamination trades orphelins (Session 14)

### Contexte

Après v2.4.1, le backtest direct avec la best config de Phase 2A produisait
**10 trades** alors que l'optimizer en reportait **30** — avec une config
identique et `use_reset_capital=False` confirmé. Audit complet effectué.

### Bug identifié — FIX-OPT-ORPHAN (CRITIQUE)

**Symptôme :** Optimizer Phase 2A déclare 30 trades / Sharpe=X pour la best
config. Backtest direct avec la même config : 10 trades / résultats différents.

**Cause racine :**
`Engine.run()` ne crée pas de fichier `session_XXX_trades.json` pour les
sessions sans trades (ex: sessions 4 et 7 qui avaient 0 signaux).

Si un backtest *précédent* avait créé `session_004_trades.json` et
`session_007_trades.json` avec des trades, ces fichiers restaient dans le
dossier `results/backtests/sessions/trades/`. L'optimizer les trouvait
lors de `_collect_trades()` et les incluait dans le total — ajoutant des
trades fantômes du backtest précédent au résultat du run actuel.

```
Avant purge : session_001..003..005..006 (run actuel, 10 trades)
            + session_004..007 (ANCIENS fichiers, 20 trades orphelins)
            = 30 trades (faux total)

Après purge : session_001..003..005..006 (run actuel, 10 trades)
            = 10 trades (correct)
```

**Fix :** Ajout de `_purge_trades_dir()` dans `optimizer.py`, appelée
dans `_run_single()` **avant** chaque `Engine.run()`. Supprime tous les
fichiers `session_*_trades.json` existants pour garantir que seuls les
fichiers du run actuel sont lus par `_collect_trades()`.

```python
# Dans _run_single(), avant Engine.run() :
self._purge_trades_dir()   # [FIX-OPT-ORPHAN]
```

### Fichier modifié

| Fichier | Changement |
|---|---|
| `src/backtesting/optimizer.py` | `_purge_trades_dir()` + appel dans `_run_single()` |

### Validation

4/4 tests unitaires passés :
- `_purge_trades_dir()` existe
- Supprime correctement les fichiers orphelins
- Appelée avant `Engine.run()` dans `_run_single()`
- Simulation contamination 30→10 trades : correctement éliminée

### Impact

**La Phase 2A doit être relancée** — les résultats précédents (Sharpe,
PnL, best config) étaient calculés sur des trades contaminés par des
fichiers orphelins. Les scores étaient artificiellement gonflés.

```bash
# Relancer Phase 2A avec le fix
python optimize.py --phase 2a
```

