# CLAUDE.md — BULLET-1
## Contexte automatique pour Claude Code / Claude Projects

---

## 🎯 PROJET
**Nom :** BULLET-1
**Auteur :** FuegoDev (FuegoDev369)
**Objectif :** Bot de trading algorithmique autonome sur crypto futures (BTC/USDT perpetuals, Binance)
**État actuel :** En développement actif — backtesting opérationnel, paper et live à implémenter
**Version courante :** 2.x (moteurs backtesting en v2.8.x)

---

## 🛠️ STACK TECHNIQUE
- **Langage :** Python 3.11+
- **Dépendances core :** pandas ≥ 2.0, numpy ≥ 1.24, pydantic ≥ 2.0, matplotlib ≥ 3.7
- **Base de données :** SQLite (`data/bullet1_market_data.db`) — données OHLCV historiques BTC/USDT
- **Exchange cible :** Binance (futures perpetuals, isolated margin)
- **Mode actif :** `backtest` (paper et live = stubs à implémenter)
- **Configuration :** JSON multi-fichiers dans `config/`
- **Logger :** `BulletLogger` (singleton custom dans `src/utils/logger.py`)

---

## 📁 ARCHITECTURE

```
BULLET-1/
├── config/                        → Configs JSON (une par module)
│   ├── config.json                → Config principale (paire, timeframe, capital, risk...)
│   ├── credentials.json           → Clés API (NE PAS modifier sans confirmation)
│   ├── atr_config.json            → Config ATRIndicator
│   ├── volume_config.json         → Config VolumeIndicator (OBLIGATOIRE)
│   ├── trend_config.json          → Config TrendIndicator
│   ├── uncertainty_candle_config.json → Config UncertaintyCandleIndicator
│   └── [autres configs indicateurs...]
│
├── data/
│   └── bullet1_market_data.db     → SQLite OHLCV historiques (BTC/USDT 5m)
│
├── src/
│   ├── backtesting/               → Pipeline de backtesting complet
│   │   ├── engine.py              → Orchestrateur principal (POINT D'ENTRÉE UNIQUE)
│   │   ├── trading_engine.py      → Boucle candle-par-candle + gestion positions
│   │   ├── ohlcv_data_engine.py   → Chargement + validation données OHLCV
│   │   ├── analytics_engine.py    → Génération rapports post-session
│   │   ├── metrics.py             → Calcul métriques (Sharpe, Sortino, Calmar, CAGR...)
│   │   ├── order_simulator.py     → Simulation ordres avec slippage + fees + funding
│   │   ├── report_generator.py    → Export HTML/Markdown/JSON/CSV
│   │   └── optimizer.py           → Optimisation paramètres stratégie
│   │
│   ├── core/                      → Logique métier trading
│   │   ├── strategy.py            → Orchestrateur stratégie (coordonne signal + risk)
│   │   ├── signal_generator.py    → Génération signaux bruts (uncertainty candle enhanced)
│   │   ├── risk_manager.py        → Position sizing + SL/TP + validation marché
│   │   ├── position_manager.py    → Cycle de vie positions (trailing stop inclus)
│   │   ├── session_manager.py     → Gestion capital + limites session/journalières
│   │   └── day_trades_manager.py  → Tracking trades journaliers + limites
│   │
│   ├── indicators/                → Indicateurs techniques (chacun avec config JSON)
│   │   ├── atr.py                 → ATR (trailing stop type='atr'/'hybrid')
│   │   ├── uncertainty_candle.py  → Bougie d'incertitude (signal principal)
│   │   ├── momentum.py            → Momentum
│   │   ├── regime.py              → Détection régime de marché
│   │   ├── structure.py           → Structure de marché (HH/HL/LH/LL)
│   │   ├── trend.py               → Filtre de tendance (MA rapide/lente)
│   │   ├── volatility.py          → Volatilité
│   │   └── volume.py              → Volume (OBLIGATOIRE)
│   │
│   ├── data/                      → Pipeline données
│   │   ├── db_manager.py          → Accès SQLite
│   │   ├── data_loader.py         → Chargement données
│   │   ├── data_processor.py      → Nettoyage + normalisation
│   │   └── data_validator.py      → Validation qualité données
│   │
│   ├── ml/
│   │   └── market_context.py      → Capture contexte marché à l'ouverture de position
│   │
│   ├── exchange/                  → Clients exchange (stubs à implémenter)
│   │   ├── base_client.py
│   │   ├── binance_client.py
│   │   └── paper_trading.py
│   │
│   └── utils/
│       ├── config_loader.py       → Chargement + validation Pydantic des configs
│       ├── logger.py              → BulletLogger (singleton)
│       └── helpers.py             → Utilitaires communs
│
├── backtest.py                    → Point d'entrée backtest CLI
├── optimize.py                    → Point d'entrée optimiseur
└── main.py                        → Point d'entrée principal
```

---

## 🔄 FLUX D'EXÉCUTION (BACKTESTING)

```
backtest.py
    └── Engine.run()
            ├── Phase 0 : load_config() → BulletConfig (Pydantic)
            ├── Phase 1 : validate_period_coherence() [durée % trades_period_days == 0]
            ├── Phase 2 : initialize_subsystems() [DI : 10 sous-modules instanciés]
            ├── Phase 3 : OHLCVDataEngine.load_and_validate() → DataFrame OHLCV
            ├── Phase 4 : Boucle sessions [1..N]
            │       └── TradingEngine.run_session()
            │               └── Boucle candles : strategy.analyze() → order_simulator → position_manager
            └── Phase 5 : AnalyticsEngine.generate_reports() → HTML/JSON/CSV
```

---

## ⚙️ CONFIGURATION CRITIQUE (config.json)
- **Paire :** BTC/USDT | **Timeframe :** 5m
- **Mode :** backtest | **Levier :** 20x | **Margin :** isolated
- **Capital initial :** 100 USDT (backtest) / 50 USDT (live)
- **Stratégie :** `uncertainty_candle_enhanced` (configuration `7-reverse`)
- **Risk/Reward :** 2.0 | **SL offset :** 0.5%
- **Session :** 10 jours | **Max perte session :** 10% | **Max gain :** 25%
- **Trailing stop type :** configurable (candle/atr/hybrid)

---

## 📏 CONVENTIONS DE CODE

### Pattern universel résolution racine projet
```python
# Utilisé dans TOUS les modules — NE PAS modifier
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
```

### Imports BULLET-1
```python
from src.utils.logger import BulletLogger      # Toujours ce logger, jamais print()
from src.utils.helpers import format_datetime, safe_divide, ensure_directory
from src.utils.config_loader import load_config, BulletConfig
```

### Style obligatoire
- Type hints OBLIGATOIRES sur toutes les fonctions
- Docstrings Google style OBLIGATOIRES
- Logging via `BulletLogger` uniquement — jamais `print()` ou `logging` standard
- Gestion d'erreurs complète : try/except avec logger approprié
- Thread-safety : RLock sur tout état mutable partagé
- Changelog inline dans les docstrings de module : `# [vX.Y.Z — REF] Description`

### Naming
- Variables/fonctions : snake_case
- Classes : PascalCase
- Constantes module : UPPER_SNAKE_CASE avec underscore préfixe (`_CONST`)
- Références de fix/feat : format `[vX.Y.Z — TYPE-MODULE-N]`

---

## ✅ RÈGLES DE TRAVAIL

1. **Lire avant de modifier** — Toujours lire le fichier complet avant toute modification
2. **Respecter la DI** — engine.py est le SEUL point d'instanciation des sous-modules
3. **Pas de breaking changes** sur les interfaces publiques sans confirmation explicite
4. **Tests sur données réelles** — utiliser `data/bullet1_market_data.db` pour valider
5. **Un module = une responsabilité** — ne pas créer de couplage entre modules non connectés
6. **Changelog obligatoire** — tout fix ou feat documenté dans la docstring du module

---

## 🚫 NE JAMAIS FAIRE
- Modifier `credentials.json` sans confirmation explicite
- Supprimer du code sans confirmation
- Contourner le pattern `_PROJECT_ROOT` unifié
- Utiliser `print()` — toujours `BulletLogger`
- Hardcoder des valeurs qui sont dans config.json
- Dupliquer la logique de cohérence de période (exclusivité engine.py)
- Ignorer les exceptions avec `except: pass`
- Créer des imports circulaires (respecter le graphe DI)

---

## 📋 COMMANDES UTILES
```bash
# Lancer un backtest
python backtest.py

# Lancer l'optimiseur
python optimize.py

# Inspecter la base de données
python -c "import sqlite3; conn=sqlite3.connect('data/bullet1_market_data.db'); print([r[0] for r in conn.execute(\"SELECT name FROM sqlite_master WHERE type='table'\").fetchall()])"
```

---

## 🗂️ DONNÉES DE MARCHÉ
- **Fichier :** `data/bullet1_market_data.db` (SQLite, ~52MB)
- **Contenu :** OHLCV historiques BTC/USDT 5m (données réelles)
- **Usage :** Source unique pour tous les backtests — ne pas modifier directement
