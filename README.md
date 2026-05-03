# BULLET-1

> **Bot de trading algorithmique crypto Futures — Backtesting ultra-réaliste & Live Trading**

---

## Table des matières

- [Vue d'ensemble](#vue-densemble)
- [Architecture globale](#architecture-globale)
- [Stratégie — Uncertainty Candle Enhanced](#stratégie--uncertainty-candle-enhanced)
- [Modules & responsabilités](#modules--responsabilités)
- [Stack technique](#stack-technique)
- [Installation](#installation)
- [Configuration](#configuration)
- [Usage](#usage)
- [Rapports & métriques](#rapports--métriques)
- [État du développement](#état-du-développement)
- [Roadmap](#roadmap)
- [Sécurité](#sécurité)
- [Auteur](#auteur)

---

## Vue d'ensemble

**BULLET-1** est un système complet de trading algorithmique sur Futures crypto (BTC/USDT — Binance), conçu autour de trois objectifs stratégiques :

1. **Backtesting haute fidélité** — simulation qui reproduit fidèlement les conditions réelles d'un exchange (slippage dynamique, frais maker/taker différenciés, funding fees 8h, latence API simulée, protection contre le look-ahead bias).
2. **Stratégie originale** — *Uncertainty Candle Enhanced*, pipeline de génération de signaux en 11 étapes, déclinée en 8 configurations testables indépendamment.
3. **Architecture évolutive** — progression séquentielle de 66 modules couvrant l'infrastructure, le backtesting, l'optimisation, le paper trading et le live trading.

| Propriété | Valeur |
|---|---|
| Version | 2.1 |
| Exchange cible | Binance Futures |
| Paire par défaut | BTC/USDT |
| Timeframe par défaut | 15 minutes |
| Capital de départ (live) | 50 USDT |
| Levier | 10× (configurable 1–125) |
| Mode de marge | Isolated |
| Auteur | FuegoDev |
| Statut | ⏳ Phase 1 en cours (75.6%) |

---

## Architecture globale

BULLET-1 est structuré en **pipeline séquentiel à trois niveaux** :

```
┌─────────────────────────────────────────────────────────────┐
│                        engine.py                            │
│              (Orchestrateur unique — point d'entrée)        │
└───────────────┬─────────────────┬───────────────────────────┘
                │                 │                 │
                ▼                 ▼                 ▼
   ┌────────────────┐  ┌──────────────────┐  ┌─────────────────┐
   │ OHLCVDataEngine│  │  TradingEngine   │  │ AnalyticsEngine │
   │  (données)     │  │  (orchestration) │  │  (rapports)     │
   └────────────────┘  └──────────────────┘  └─────────────────┘
```

### Pipeline de données

```
data_loader → data_validator → data_processor → OHLCVDataEngine
```

### Pipeline de trading (par session)

```
SessionManager
    └── [boucle candle par candle]
          ├── MarketContextCapture  → snapshot 7 indicateurs
          ├── Strategy
          │     ├── SignalGenerator → détection Uncertainty Candle (11 étapes)
          │     └── RiskManager    → sizing + SL/TP + validation marché
          ├── OrderSimulator        → exécution simulée (slippage, frais, funding)
          └── PositionManager       → trailing stop + calcul PnL net
```

### Structure du projet

```
BULLET-1/
├── config/                          # 15 fichiers de configuration JSON
│   ├── config.json                  # Configuration maître
│   ├── atr_config.json
│   ├── logger_config.json
│   ├── uncertainty_candle_config.json
│   ├── volume_config.json
│   ├── trend_config.json
│   ├── momentum_config.json
│   ├── structure_config.json
│   ├── volatility_config.json
│   ├── regime_config.json
│   ├── order_simulator_config.json
│   ├── analytics_engine_config.json
│   ├── data_processor_config.json
│   ├── data_validator_config.json
│   └── credentials.json             # ⚠️ Exclu du dépôt (.gitignore)
├── data/
│   ├── historical/BTC-USDT/
│   │   ├── 5min.csv                 # ~1.9 MB de données historiques
│   │   └── 15min.csv                # ~841 KB de données historiques
│   └── download_data_multi_exchange_v2.3.py
├── src/
│   ├── backtesting/                 # Moteur de backtesting complet
│   ├── core/                        # Logique de trading (Strategy, Risk, Sessions)
│   ├── data/                        # Chargement, validation, traitement OHLCV
│   ├── exchange/                    # Clients exchange (Binance, paper trading)
│   ├── indicators/                  # 8 indicateurs techniques
│   ├── ml/                          # Capture de contexte marché (7 indicateurs)
│   ├── notifications/               # Discord, Email
│   ├── trading/                     # TradingBot (live/paper)
│   └── utils/                       # Logger, ConfigLoader, Helpers
├── main.py                          # Point d'entrée CLI (backtest / paper / live)
└── backtest.py                      # Script de lancement backtest dédié
```

---

## Stratégie — Uncertainty Candle Enhanced

### Principe central

La stratégie repose sur la détection d'une **bougie d'incertitude** (indécision du marché) suivie d'une **cassure directionnelle** validée par le volume. Le signal est émis dans le sens **opposé** à la cassure (mode `normal`) ou dans le **même sens** (mode `reverse`).

### Détection de la bougie d'incertitude

Une bougie est qualifiée d'incertitude si elle respecte simultanément ces critères :

| Critère | Valeur par défaut |
|---|---|
| Corps | `body_pct < 33%` de la range totale |
| Mèche supérieure | `upper_wick_pct ≥ 20%` |
| Mèche inférieure | `lower_wick_pct ≥ 20%` |
| Corps minimum | `≥ 10 USDT` (filtre anti-bruit) |
| Range maximum | `≤ 10 000 USDT` (filtre anti-anomalie) |

**Types de bougies détectés :**

| Type | Description |
|---|---|
| `perfect_doji` | Corps quasi nul, mèches symétriques — incertitude maximale |
| `long_legged_doji` | Corps < 15%, mèches ≥ 35% haut et bas |
| `dragonfly_doji` | Grande mèche basse, sans mèche haute — rejet vendeur |
| `gravestone_doji` | Grande mèche haute, sans mèche basse — rejet acheteur |
| `standard_uncertainty` | Critères de base respectés |

### Pipeline de génération de signal (11 étapes)

```
[1] Vérification données suffisantes (≥ volume_lookback bougies)
[2] Détection bougie d'incertitude (UncertaintyCandleIndicator)
[3] Récupération bougie précédente (candles.iloc[-2])
[4] Détection cassure (mode strict ou permissive)
[5] Détermination type de signal (normal/reverse → LONG/SHORT)
[6] Validation volume NIVEAU-1 (obligatoire)
[7] Filtre de tendance (optionnel, configurable)
[8] Validation volume NIVEAU-2 (optionnel — modes basic/directional/advanced)
[9] Calcul score de confiance (0→100, 5 facteurs)
[10] Détermination prix d'entrée (close de la bougie courante)
[11] Émission signal + persistance dans _signals_history
```

**Score de confiance (0 → 100) :**

| Facteur | Points max |
|---|---|
| Qualité bougie d'incertitude (corps + mèches) | 35 pts |
| Ratio volume NIVEAU-1 | 30 pts |
| Amplitude de la cassure | 20 pts |
| Alignement tendance | 10 pts |
| Volume NIVEAU-2 confirmé | 5 pts |

### Les 8 configurations

| Nom | `logic_direction` | `short_op` | `long_op` | Logique cassure → signal |
|---|---|---|---|---|
| `1-normal` | `normal` | `>` | `>` | UP→SHORT(vol↑) / DOWN→LONG(vol↑) |
| `2-normal` | `normal` | `>` | `<` | UP→SHORT(vol↑) / DOWN→LONG(vol↓) |
| `3-normal` | `normal` | `<` | `>` | UP→SHORT(vol↓) / DOWN→LONG(vol↑) |
| `4-normal` | `normal` | `<` | `<` | UP→SHORT(vol↓) / DOWN→LONG(vol↓) |
| `5-reverse` | `reverse` | `<` | `>` | UP→LONG(vol↑) / DOWN→SHORT(vol↓) |
| `6-reverse` | `reverse` | `>` | `<` | UP→LONG(vol↓) / DOWN→SHORT(vol↑) |
| `7-reverse` | `reverse` | `<` | `<` | UP→LONG(vol↓) / DOWN→SHORT(vol↓) |
| `8-reverse` | `reverse` | `>` | `>` | UP→LONG(vol↑) / DOWN→SHORT(vol↑) |

> `vol↑` = volume courant **supérieur** à la moyenne | `vol↓` = volume courant **inférieur** à la moyenne

Configuration active par défaut : **`8-reverse`**

---

## Modules & responsabilités

### `src/backtesting/` — Moteur de backtesting

| Module | Version | Rôle |
|---|---|---|
| `engine.py` | v2.2.2 | Orchestrateur global — seul point d'instanciation autorisé |
| `trading_engine.py` | v2.8.2 | Orchestrateur session : coordonne Strategy, OrderSimulator, PositionManager |
| `ohlcv_data_engine.py` | — | Pipeline données OHLCV — chargement, slicing, itération |
| `order_simulator.py` | v2.6.8 | Simulation ordres MARKET/LIMIT — slippage ATR, frais, funding, latence |
| `metrics.py` | v2.2.7 | Calcul métriques de performance (Sharpe, Calmar, Sortino, CAGR, drawdown…) |
| `analytics_engine.py` | v2.1.5 | Consomme EngineRunResult, reconstruit equity curve, orchestre les rapports |
| `report_generator.py` | v2.2.3 | Génération rapports HTML / MD / JSON / CSV / TXT + graphiques matplotlib |
| `optimizer.py` | — | Optimisation des paramètres de stratégie (Phase 2) |

**Réalisme du backtesting :**
- Slippage dynamique calculé sur la volatilité ATR courante
- Frais maker/taker différenciés (configurable)
- Funding fees Futures perpétuels (cycle 8h)
- Latence API simulée (50–200 ms)
- Protection anti look-ahead bias (exécution candle suivante)
- Spread appliqué sur chaque ordre

### `src/core/` — Logique de trading

| Module | Version | Rôle |
|---|---|---|
| `strategy.py` | v2.2.11 | Orchestrateur stratégie — coordonne SignalGenerator et RiskManager |
| `signal_generator.py` | v2.4.6 | Implémente le pipeline Uncertainty Candle Enhanced (11 étapes) |
| `risk_manager.py` | v2.3.3 | Position sizing (collateral+levier), calcul SL/TP, validation conditions marché |
| `session_manager.py` | v2.5.9 | Sessions x-jours glissantes, limites journalières, reset capital configurable |
| `position_manager.py` | v2.6.5 | Trailing stop (candle/ATR/hybrid), protection 1R, PnL net, cache ATR |
| `day_trades_manager.py` | — | Comptabilité journalière des trades (limites quotidiennes) |

**Paramètres de risque clés :**
- Risk/Reward ratio : 2.0 (configurable)
- SL offset : 0.8% au-delà du niveau technique
- Positions simultanées max : 1
- Trailing stop modes : `candle`, `atr`, `hybrid`
- Circuit breaker : arrêt automatique sur erreurs répétées

### `src/indicators/` — Indicateurs techniques

| Module | Version | Indicateurs calculés |
|---|---|---|
| `uncertainty_candle.py` | v2.2.2 | Détection doji, body_pct, wick ratios, signal_strength |
| `volume.py` | v2.4.3 | Volume SMA, ratio, direction, tendance (increasing/neutral/decreasing) |
| `trend.py` | v2.5.2 | EMA, SMA, crossovers, tendance globale (bullish/bearish/neutral/sideways) |
| `atr.py` | v2.3.4 | ATR (EMA-smoothed), trailing stop dynamique, détection spikes/crashes |
| `momentum.py` | v2.1.2 | RSI, MACD, ROC z-scored, Stochastic RSI, Williams %R, CMF, MFI, OBV, divergences |
| `structure.py` | v2.1.2 | VWAP sessionnel, Price Z-Score, Swing H/L, BOS, CHoCH, Pivots Camarilla |
| `volatility.py` | v2.1.2 | Bollinger Bands, Keltner Channels, Squeeze, Realized Volatility, Chandelier Exit |
| `regime.py` | v2.1.2 | ADX (+DI/-DI), Variance Ratio (Lo-MacKinlay), régime composite |

### `src/ml/` — Capture de contexte marché

| Module | Version | Rôle |
|---|---|---|
| `market_context.py` | v2.1.2 | `MarketContextCapture` — snapshot des 7 indicateurs (ATR, Trend, Volume, Momentum, Volatility, Structure, Regime) à chaque trade |

Le snapshot de contexte est attaché à chaque trade record, permettant une analyse post-backtest de la corrélation entre les conditions de marché et la performance.

### `src/data/` — Pipeline de données

| Module | Version | Rôle |
|---|---|---|
| `data_loader.py` | v2.4.1 | Chargement CSV OHLCV, parsing timestamps vectorisé, validation préliminaire |
| `data_validator.py` | — | Validation structurelle et qualité des données (gaps, doublons, valeurs manquantes) |
| `data_processor.py` | — | Nettoyage, normalisation, enrichissement des colonnes OHLCV |

### `src/utils/` — Infrastructure transversale

| Module | Version | Rôle |
|---|---|---|
| `logger.py` | v2.3.2 | `BulletLogger` — singleton thread-safe, 7 fichiers de log distincts, rotation 10 MB |
| `config_loader.py` | v2.3.6 | `BulletConfig` — chargement + validation Pydantic des 15 fichiers JSON |
| `helpers.py` | — | Fonctions utilitaires partagées (timestamps, JSON I/O, filesystem, IDs) |
| `error_handler.py` | — | Gestion centralisée des erreurs (Phase 4) |
| `state_manager.py` | — | Persistance d'état pour le live trading (Phase 4) |
| `performance_monitor.py` | — | Monitoring performance en temps réel (Phase 3) |
| `validator.py` | — | Validation des entrées runtime (Phase 4) |

### `src/exchange/` — Clients exchange

| Module | Rôle |
|---|---|
| `base_client.py` | Interface abstraite commune à tous les clients |
| `binance_client.py` | Client Binance Futures (Phase 3) |
| `paper_trading.py` | Client paper trading — simulation ordres sans capital réel (Phase 3) |

### `src/notifications/` — Alertes

| Module | Rôle |
|---|---|
| `discord_notifier.py` | Notifications Discord (ouverture/fermeture trades, alertes) |
| `email_notifier.py` | Notifications email |

---

## Stack technique

| Domaine | Librairie |
|---|---|
| Données & calcul | `pandas`, `numpy` |
| Validation config | `pydantic` |
| Visualisation | `matplotlib` (backend `Agg`) |
| Logging | `logging` + `RotatingFileHandler` |
| Concurrence | `threading`, `RLock` |
| Données optionnelles | `psutil`, `tqdm` |
| Exchange (Phase 3+) | `ccxt` / Binance API |

**Environnement cible :** Python 3.10+ — compatible Termux (Android) et Linux standard.

---

## Données historiques

BULLET-1 utilise une base de données **SQLite** (`data/bullet1_market_data.db`)
pour stocker les données OHLCV. Ce format remplace les fichiers CSV depuis la v2.2.

### Avantages SQLite vs CSV
- Requêtes temporelles indexées (O(log N) vs lecture complète)
- Déduplication native (UNIQUE constraint)
- Métadonnées intégrées (table `datasets`)
- Mode incrémental : ne télécharge que les nouvelles bougies
- Un seul fichier, compatible Termux/Android

### Télécharger des données

```bash
python data/download_data_v3.0.py
```

Interface interactive avec 5 modes :
- **Quick Start** — téléchargement rapide avec valeurs par défaut
- **Advanced** — configuration complète (paire, timeframe, dates, exchange)
- **Mise à jour** — incrémental (nouvelles bougies uniquement)
- **Favoris** — configurations sauvegardées
- **État DB** — liste les datasets disponibles

### Migrer des CSV existants (one-time)

Si vous avez des fichiers CSV dans `data/historical/`, importez-les :

```bash
python data/migrate_csv_to_db.py
```

### Structure des données

```
data/
├── bullet1_market_data.db    ← Base SQLite (source unique de données)
├── migrate_csv_to_db.py      ← Script migration one-time
├── download_data_v3.0.py     ← Downloader interactif
└── historical/               ← Legacy CSV (peut être archivé post-migration)
    └── BTC-USDT/
        ├── 5min.csv
        └── 15min.csv
```

### Configuration de la DB

Dans `config/config.json` :
```json
"data": {
    "db_path": "data/bullet1_market_data.db"
}
```

Le chemin est relatif à la racine du projet. La DB est créée automatiquement
au premier lancement.

---


```bash
# 1. Cloner le dépôt
git clone <repo_url> BULLET-1
cd BULLET-1

# 2. Créer et activer l'environnement virtuel
python -m venv venv_bullet1
source venv_bullet1/bin/activate   # Linux/macOS/Termux
# venv_bullet1\Scripts\activate   # Windows

# 3. Installer les dépendances
pip install pandas numpy pydantic matplotlib tqdm psutil

# (optionnel — pour les tests)
pip install pytest pytest-cov

# 4. Configurer les credentials
cp config/credentials.json.template config/credentials.json
# Éditer config/credentials.json avec vos clés API Binance
```

> **Termux (Android) :** `pip install` peut nécessiter l'option `--break-system-packages` selon la version de pip installée.

---

## Configuration

Toutes les configurations sont dans le dossier `config/`. Le fichier maître est `config/config.json`.

### Paramètres critiques — `config/config.json`

```jsonc
{
  "general": {
    "mode": "backtest",          // "backtest" | "paper" | "live"
    "exchange": "binance",
    "trading_pair": "BTC/USDT",
    "timeframe": "15m"
  },

  "capital": {
    "initial_capital_backtest": 100.0,
    "initial_capital_live": 50.0,    // Capital de départ live (USDT)
    "target_capital_live": 1000.0    // Objectif de capitalisation
  },

  "session_management": {
    "trades_period_days": 7,
    "reset_capital_between_sessions": false,  // false en LIVE, true en optimisation
    "max_loss_per_session_pct": 5.0,
    "max_gain_per_session_pct": 15.0,
    "daily_limits": {
      "enabled": true,
      "max_loss_per_day_pct": 3.0,
      "max_gain_per_day_pct": 5.0,
      "max_trades_per_day": 8
    }
  },

  "position": {
    "leverage": 10,
    "margin_mode": "isolated",
    "collateral_percentage": 10.0,    // % du capital utilisé comme collateral
    "max_simultaneous_positions": 1
  },

  "risk_management": {
    "risk_reward_ratio": 2.0,
    "stop_loss_offset_pct": 0.8
  },

  "strategy": {
    "configuration_name": "8-reverse",   // Parmi 1-normal à 8-reverse
    "entry_logic": {
      "logic_direction": "reverse",
      "for_short_case_comparison_operator": ">",
      "for_long_case_comparison_operator": ">"
    }
  }
}
```

### Fichiers de configuration secondaires

| Fichier | Contenu |
|---|---|
| `atr_config.json` | Période ATR (7), multiplicateur trailing stop (2.5), thread-safety RLock |
| `uncertainty_candle_config.json` | Seuils body/wick pour la détection de doji |
| `volume_config.json` | Paramètres de la SMA volume et niveaux de confirmation |
| `trend_config.json` | Périodes EMA/SMA et seuils de classification de tendance |
| `momentum_config.json` | Paramètres RSI, MACD, Stochastic, etc. |
| `structure_config.json` | VWAP, Swing H/L, BOS/CHoCH |
| `volatility_config.json` | Bollinger Bands, Keltner, Squeeze |
| `regime_config.json` | ADX, Variance Ratio |
| `order_simulator_config.json` | Frais maker/taker, slippage, funding rate |
| `analytics_engine_config.json` | Formats de rapport activés, chemins de sortie |
| `logger_config.json` | Niveaux de log, rotation, rétention 30 jours |
| `credentials.json` | Clés API exchange — **jamais commité** |

---

## Usage

```bash
# Activer l'environnement
source venv_bullet1/bin/activate

# Lancer un backtest
python main.py backtest

# Lancer en mode paper trading (Phase 3)
python main.py paper

# Lancer en mode live (Phase 4)
python main.py live

# Script backtest dédié
python backtest.py

# Télécharger des données historiques
python data/download_data_multi_exchange_v2.3.py
```

```bash
# Lancer les tests (quand disponibles)
pytest tests/ -v
pytest tests/ --cov=src --cov-report=html
pytest tests/ -m critical
```

---

## Rapports & métriques

Le module `analytics_engine` → `report_generator` génère automatiquement après chaque backtest :

**Formats de sortie :**
- `HTML` — rapport interactif avec styles CSS
- `Markdown` — rapport lisible en texte structuré
- `JSON` — données brutes exportables
- `CSV` — trades + equity curve ligne par ligne
- `TXT` — résumé texte brut

**Graphiques générés (matplotlib) :**
- Courbe d'equity
- Courbe de drawdown
- Évolution du trailing stop

**Métriques calculées (`metrics.py`) :**
- Winrate, profit factor, average RR réalisé
- Sharpe Ratio, Sortino Ratio, Calmar Ratio
- CAGR (durée minimum 30 jours)
- Max drawdown absolu et relatif
- Statistiques par session et par journée

**Sorties structurelles :**
```
results/
└── backtests/
    └── sessions/
        ├── trades/       # Détail de chaque trade
        ├── summaries/    # Résumé de chaque session
        └── days/         # Statistiques journalières
logs/
├── bullet1.log
├── trading.log
├── errors.log
├── backtest.log
└── sessions.log
```

---

## État du développement

**Dernière mise à jour :** 2026-04-21 | **Session :** 9

### Progression globale : 31/66 modules (46.97%)

| Phase | Description | Modules | Complétés | Progression |
|---|---|---|---|---|
| **Phase 1** | Infrastructure + Backtesting | 1–41 | 31/41 | **75.6%** ✅ |
| **Phase 2** | Optimisation des paramètres | 42–45 | 0/4 | 0% 🔒 |
| **Phase 3** | Paper Trading | 46–60 | 0/15 | 0% 🔒 |
| **Phase 4** | Live Trading | 61–66 | 0/6 | 0% 🔒 |

> **Note :** La Phase 1 est fonctionnellement complète à ~95%. Tous les modules
> substantiels sont implémentés. La migration SQLite (Session 9) est un prérequis
> à la validation end-to-end de la Phase 1.

### Modules Phase 1 complétés ✅

`helpers.py` · `logger.py` · `config_loader.py` · `data_loader.py` (v3.0 SQLite) ·
`data_validator.py` · `data_processor.py` · `db_manager.py` · `uncertainty_candle.py` ·
`volume.py` · `trend.py` · `atr.py` · `momentum.py` · `structure.py` · `volatility.py` ·
`regime.py` · `risk_manager.py` · `position_manager.py` · `session_manager.py` ·
`day_trades_manager.py` · `signal_generator.py` · `strategy.py` · `market_context.py` ·
`order_simulator.py` · `ohlcv_data_engine.py` · `metrics.py` · `engine.py` ·
`trading_engine.py` · `analytics_engine.py` · `report_generator.py`

### Prochaines étapes

1. **Migration données** — `python data/migrate_csv_to_db.py`
2. **Validation end-to-end** — `python main.py backtest`
3. **Phase 2** — Optimiseur (grid search + walk-forward)

---


### Progression globale : 31/66 modules (46.97%)

| Phase | Description | Modules | Complétés | Progression |
|---|---|---|---|---|
| **Phase 1** | Infrastructure + Backtesting | 1–41 | 31/41 | **75.6%** ✅ |
| **Phase 2** | Optimisation des paramètres | 42–45 | 0/4 | 0% 🔒 |
| **Phase 3** | Paper Trading | 46–60 | 0/15 | 0% 🔒 |
| **Phase 4** | Live Trading | 61–66 | 0/6 | 0% 🔒 |

### Modules Phase 1 complétés ✅

`helpers.py` · `logger.py` · `config_loader.py` · `data_loader.py` · `data_validator.py` · `data_processor.py` · `uncertainty_candle.py` · `volume.py` · `trend.py` · `atr.py` · `risk_manager.py` · `position_manager.py` · `session_manager.py` · `signal_generator.py` · `strategy.py`

+ leurs fichiers de tests associés (modules 2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22, 24, 26, 29, 31)

+ **Checkpoint Bloc 4** validé ✅

### Prochain module à implémenter

**Module 32 — `order_simulator.py`** *(Criticité ⭐⭐ IMPORTANT)*

Puis la séquence : 33 → 34 → 35 → **36 (`engine.py`)** → 37 → 38 → 39 → 40 → 41

> Le module 36 (`engine.py`) est le module le plus critique de la Phase 1 — il dépend de l'ensemble des 35 modules précédents.

---

## Roadmap

### Phase 1 — Infrastructure + Backtesting *(75.6%)*
- [x] Utilitaires fondamentaux (helpers, logger, config_loader)
- [x] Pipeline de données (data_loader, validator, processor)
- [x] Indicateurs techniques (uncertainty_candle, volume, trend, atr)
- [x] Indicateurs avancés (momentum, structure, volatility, regime)
- [x] Logique de risque (risk_manager, position_manager)
- [x] Gestion des sessions (session_manager, day_trades_manager)
- [x] Génération de signaux (signal_generator, strategy)
- [ ] **Simulateur d'ordres** (order_simulator — Module 32)
- [ ] **Métriques de performance** (metrics — Module 34)
- [ ] **Moteur principal** (engine — Module 36)
- [ ] **Génération de rapports** (report_generator — Module 38)
- [ ] Scripts de lancement (backtest.py, main.py v1)

### Phase 2 — Optimisation des paramètres *(0%)*
- [ ] Optimizer (grid search / bayesian)
- [ ] Script `optimize_parameters.py`
- [ ] Script `compare_configurations.py` (comparaison des 8 configs)

### Phase 3 — Paper Trading *(0%)*
- [ ] Client Binance Futures (base_client, binance_client)
- [ ] Paper trading engine
- [ ] Performance monitor temps réel
- [ ] Notifications (Discord, Email)
- [ ] TradingBot complet

### Phase 4 — Live Trading *(0%)*
- [ ] Error handler production-grade
- [ ] State manager (persistance crash recovery)
- [ ] Validator runtime
- [ ] Déploiement live avec capital réel (50 USDT → 1000 USDT)

---

## Sécurité

- Les credentials API ne sont **jamais commités** — `credentials.json` est dans `.gitignore`
- **Circuit breaker** automatique sur erreurs répétées (configurable)
- **Limites par session** obligatoires : perte max 5% / gain max 15% par session
- **Limites journalières** : max 3% de perte, max 5% de gain, max 8 trades/jour
- **Thread-safety** : `RLock` sur toutes les mutations d'état critique (capital, positions, sessions)
- **Validation Pydantic** : toute la configuration est typée et validée au démarrage
- **Positions maximum** : 1 position simultanée (configurable)
- Levier maximum recommandé : **10×** (ne pas dépasser sans ajustement du `collateral_percentage`)

> ⚠️ **Avertissement** : ce système est en cours de développement. Ne pas l'utiliser en live trading avant la validation complète des Phases 1 à 3 et des tests en paper trading.

---

## Auteur

**FuegoDev**

- Version courante : `2.1`
- Date de création : 2026-01-11
- Dernière mise à jour des modules : 2026-03-15

---

*© BULLET-1 Project — 2026*
