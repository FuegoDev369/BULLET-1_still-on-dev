"""
BULLET-1 - Engine (Orchestrateur Principal)
============================================

Orchestrateur de plus haut niveau du système de backtesting BULLET-1.

Ce module est le SEUL point d'entrée autorisé à :
    - Charger la configuration globale (via config_loader.py)
    - Instancier et connecter les trois sous-moteurs
    - Posséder la boucle globale de sessions
    - Valider la cohérence de période (durée ÷ trades_period_days)

Il ne contient AUCUNE logique métier.

Position dans l'architecture :
    engine.py
    ├── ohlcv_data_engine.py   → Pipeline données OHLCV
    ├── trading_engine.py      → Simulation sessions de trading
    └── analytics_engine.py   → Génération rapports & analyses

Version: 2.2.2
Date: 2026-03-13
Author: FuegoDev
Mode: ✅ Backtest | ⚠️ Paper (partiel) | ❌ Live (sous-classe à implémenter)
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

# ============================================================================
# RÉSOLUTION RACINE DU PROJET
# ============================================================================

# [v2.2.2 — FIX-ENG-6] Pattern direct unifié BULLET-1 — remplace _find_project_root().
# L'ancienne fonction cherchait des marqueurs (.git, pyproject.toml…) sur le
# filesystem, comportement fragile en CI sans .git ou en environnement packagé.
# Pattern direct identique à trading_engine, market_context, signal_generator,
# order_simulator, risk_manager. Aucun import circulaire avec helpers.py.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# ============================================================================
# IMPORTS BULLET-1
# ============================================================================

from src.utils.logger import BulletLogger
from src.utils.helpers import format_datetime, get_project_root, timestamp_to_datetime
from src.utils.config_loader import load_config, BulletConfig

from src.backtesting.ohlcv_data_engine import OHLCVDataEngine
from src.backtesting.trading_engine import TradingEngine, EngineRunResult, EngineConfig
from src.backtesting.analytics_engine import AnalyticsEngine

# Dépendances du TradingEngine — instanciées par engine.py via DI
from src.core.strategy import Strategy
from src.core.signal_generator import SignalGenerator
from src.core.risk_manager import RiskManager
from src.core.session_manager import SessionManager
from src.backtesting.order_simulator import OrderSimulator
from src.core.position_manager import PositionManager
from src.indicators.atr import ATRIndicator

# [v2.1.5 — FEAT-MC-1] MarketContextCapture : capture de l'état du marché
# à l'instant t de l'ouverture d'une position (ML / analyses externes).
from src.ml.market_context import MarketContextCapture


# ============================================================================
# CONSTANTES
# ============================================================================

#: Sections de configuration requises pour engine.py (validation fail-fast)
_REQUIRED_CONFIG_SECTIONS: Tuple[str, ...] = (
    "general",
    "session_management",
    "capital",
    "position",
    "risk_management",
    "strategy",
    "backtesting",
)

#: Clés critiques requises dans backtesting (section + clé)
_REQUIRED_BACKTESTING_KEYS: Tuple[Tuple[str, str], ...] = (
    ("backtesting", "start_date"),
    ("backtesting", "end_date"),
    ("session_management", "trades_period_days"),
)


# ============================================================================
# HELPERS MODULE-LEVEL
# ============================================================================

def _parse_timeframe_minutes(timeframe: str) -> int:
    """
    Convertit un timeframe string en minutes entier.

    [v2.1.6 — FIX-ENG-3] Utilisé par _extract_session_slice pour calculer
    le delta de warmup. Dupliqué depuis ohlcv_data_engine._timeframe_to_minutes
    pour éviter tout import circulaire (engine → ohlcv_data_engine → engine).

    Returns 0 si le format est inconnu (warmup désactivé silencieusement).
    """
    tf = timeframe.strip().lower()
    _KNOWN: dict = {
        '1m': 1, '3m': 3, '5m': 5, '10m': 10, '15m': 15,
        '30m': 30, '45m': 45,
        '1h': 60, '2h': 120, '3h': 180, '4h': 240, '6h': 360,
        '8h': 480, '12h': 720,
        '1d': 1440, '3d': 4320, '1w': 10080,
    }
    if tf in _KNOWN:
        return _KNOWN[tf]
    for suffix, factor in (('m', 1), ('h', 60), ('d', 1440), ('w', 10080)):
        if tf.endswith(suffix):
            try:
                return int(tf[:-len(suffix)]) * factor
            except ValueError:
                pass
    return 0   # Format inconnu → warmup désactivé silencieusement


# ============================================================================
# EXCEPTIONS SPÉCIFIQUES
# ============================================================================

class EngineConfigurationError(ValueError):
    """
    Levée quand la configuration fournie à engine.py est invalide.

    Sous-classe de ValueError pour interopérabilité avec load_config
    et les validations Pydantic.
    """
    pass


class EnginePeriodCoherenceError(EngineConfigurationError):
    """
    Levée quand la durée totale du backtest n'est pas un multiple
    exact de trades_period_days.

    Responsabilité EXCLUSIVE de engine.py — cette validation ne doit
    exister nulle part ailleurs dans le système.
    """
    pass


class EngineDataError(RuntimeError):
    """
    Levée quand les données OHLCV sont invalides au niveau orchestration
    (dataset vide, couverture insuffisante, etc.).
    """
    pass


# ============================================================================
# CLASSE PRINCIPALE
# ============================================================================

class Engine:
    """
    Orchestrateur principal du système de backtesting BULLET-1.

    Responsabilités exclusives :
        1. Chargement et distribution centralisée de la configuration
        2. Validation de cohérence de la période de backtest
        3. Injection de dépendances et ordre d'initialisation
        4. Orchestration du pipeline OHLCV
        5. Segmentation des sessions et boucle d'exécution trading
        6. Agrégation et transmission vers analytics
        7. Cycle de vie global et gestion des erreurs fatales
        8. Logging d'observabilité architecturale

    Ce module NE contient AUCUNE logique de :
        - Calcul de signal ou de position
        - Gestion du capital
        - Analyse de bougie individuelle
        - Génération de rapport

    Extensibilité (design forward-compatible) :
        - Boucle de sessions prête pour parallélisation (résultats collectés, puis analytics)
        - Interface abstraite pour futur mode temps réel (hook _on_session_complete)
        - Architecture sans état caché entre sessions

    Thread-safety :
        Engine est conçu pour un usage single-threaded.
        Chaque sous-moteur gère sa propre thread-safety interne.

    Examples:
        >>> engine = Engine()
        >>> engine.run()

        # Avec chemins de configuration personnalisés :
        >>> engine = Engine(
        ...     config_path="config/my_config.json",
        ...     credentials_path="config/credentials.json"
        ... )
        >>> engine.run()
    """

    def __init__(
        self,
        config_path: Optional[str] = None,
        credentials_path: Optional[str] = None,
    ) -> None:
        """
        Initialise l'Engine sans charger la configuration.

        La configuration est chargée paresseusement au premier appel de run().
        Cela permet de valider les chemins fournis sans démarrer le pipeline.

        Args:
            config_path:       Chemin vers config.json.
                               Si None, utilise config/config.json (défaut).
            credentials_path:  Chemin vers credentials.json.
                               Si None, utilise config/credentials.json (défaut).
                               Requis uniquement en mode live/paper.
        """
        self._config_path      = config_path
        self._credentials_path = credentials_path

        # Logger disponible immédiatement (singleton — ne dépend pas de la config)
        self.logger = BulletLogger()

        # État interne — tous None jusqu'à l'initialisation dans run()
        self._bullet_config:     Optional[BulletConfig]      = None
        self._config_dict:       Optional[Dict[str, Any]]    = None
        self._ohlcv_engine:      Optional[OHLCVDataEngine]   = None
        self._trading_engine:    Optional[TradingEngine]     = None
        self._analytics_engine:  Optional[AnalyticsEngine]   = None

        # Résultats intermédiaires (lifecycle)
        self._all_session_results: List[EngineRunResult]     = []
        self._all_analytics_paths: List[Dict[str, Any]]      = []
        # [v2.2.2 — FIX-ENG-9] Compteur de sessions vides (data_slice.empty).
        # Exposé dans le log de fin de boucle pour traçabilité.
        self._sessions_skipped: int = 0

        self.logger.info("Engine created — awaiting run()")

    # =========================================================================
    # API PUBLIQUE — POINT D'ENTRÉE UNIQUE
    # =========================================================================

    def run(self) -> List[Dict[str, Any]]:
        """
        Lance le pipeline complet de backtesting BULLET-1.

        Séquence d'exécution déterministe :
            0. Chargement et validation de la configuration
            1. Validation de cohérence de la période
            2. Instanciation ordonnée des sous-moteurs (DI)
            3. Pipeline OHLCV (chargement + validation)
            4. Boucle de sessions de trading
            5. Phase analytics (génération rapports)

        Returns:
            Liste des dictionnaires de chemins retournés par AnalyticsEngine
            pour chaque session. Chaque dict contient les clés :
            session_dir, html, markdown, text, json, csv, errors.

        Raises:
            EngineConfigurationError:   Configuration invalide ou manquante.
            EnginePeriodCoherenceError: Durée backtest non multiple de trades_period_days.
            EngineDataError:            Dataset OHLCV invalide ou insuffisant.
            RuntimeError:               Erreur fatale inattendue dans un sous-moteur.

        Notes:
            - En cas d'erreur fatale, log CRITICAL et re-raise immédiat.
            - Le bloc finally garantit l'émission du log de statut final
              et de la durée totale, même en cas d'exception.
        """
        start_ts = time.monotonic()
        status   = "FAILED"

        self.logger.log_separator('INFO', '=', 80)
        self.logger.info("BULLET-1 ENGINE — DÉMARRAGE")
        self.logger.info(f"   Timestamp : {format_datetime(datetime.now(timezone.utc))}")
        self.logger.log_separator('INFO', '=', 80)

        try:
            # ── Phase 0 : Configuration ───────────────────────────────────────
            self._phase_load_config()

            # ── Phase 1 : Validation cohérence de période ─────────────────────
            self._phase_validate_period_coherence()

            # ── Phase 2 : Initialisation DI ───────────────────────────────────
            self._phase_initialize_subsystems()

            # ── Phase 3 : Pipeline OHLCV ──────────────────────────────────────
            ohlcv_df = self._phase_run_data_pipeline()

            # ── Phase 4 : Boucle sessions trading ─────────────────────────────
            self._phase_run_session_loop(ohlcv_df)

            # ── Phase 5 : Analytics ───────────────────────────────────────────
            self._phase_run_analytics()

            status = "SUCCESS"
            return self._all_analytics_paths

        except (
            EngineConfigurationError,
            EnginePeriodCoherenceError,
            EngineDataError,
        ) as exc:
            self.logger.critical(f"❌ ERREUR FATALE ENGINE : {type(exc).__name__}: {exc}")
            raise

        except Exception as exc:
            self.logger.exception(
                f"❌ ERREUR INATTENDUE ENGINE : {type(exc).__name__}: {exc}"
            )
            raise RuntimeError(
                f"Engine: erreur inattendue — {type(exc).__name__}: {exc}"
            ) from exc

        finally:
            elapsed   = time.monotonic() - start_ts
            emoji     = "✅" if status == "SUCCESS" else "❌"
            sessions  = len(self._all_session_results)

            self.logger.log_separator('INFO', '=', 80)
            self.logger.info(f"{emoji} BULLET-1 ENGINE — STATUT FINAL : {status}")
            self.logger.info(f"   Sessions traitées : {sessions}")
            self.logger.info(f"   Durée totale      : {elapsed:.2f}s")
            self.logger.log_separator('INFO', '=', 80)

    # =========================================================================
    # PHASE 0 — CHARGEMENT ET VALIDATION DE LA CONFIGURATION
    # =========================================================================

    def _phase_load_config(self) -> None:
        """
        Charge, valide et distribue la configuration centrale.

        Comportement fail-fast : toute anomalie lève une exception immédiatement.
        config_loader.py est la SEULE dépendance autorisée pour accéder à config.json.

        Post-conditions :
            self._bullet_config est une BulletConfig Pydantic validée.
            self._config_dict   est un dict Python standard prêt pour injection.

        Raises:
            EngineConfigurationError: Si config.json est absent, invalide,
                                      ou si des sections requises manquent.
        """
        self.logger.log_separator('INFO', '-', 60)
        self.logger.info("PHASE 0 — Chargement configuration")

        try:
            self._bullet_config = load_config(
                config_path=self._config_path,
                credentials_path=self._credentials_path,
                validate=True,
            )
        except FileNotFoundError as exc:
            raise EngineConfigurationError(
                f"Fichier de configuration introuvable : {exc}"
            ) from exc
        except Exception as exc:
            raise EngineConfigurationError(
                f"Échec chargement configuration : {type(exc).__name__}: {exc}"
            ) from exc

        # Convertir en dict Python standard (injection vers sous-moteurs)
        # model_dump() de Pydantic v2 garantit la sérialisation complète et propre.
        self._config_dict = self._bullet_config.model_dump()

        # Validation de présence des sections requises dans le dict résultant
        self._validate_required_config_sections()

        mode    = self._config_dict['general']['mode']
        pair    = self._config_dict['general']['trading_pair']
        tf      = self._config_dict['general']['timeframe']
        start   = self._config_dict['backtesting']['start_date']
        end     = self._config_dict['backtesting']['end_date']
        period  = self._config_dict['session_management']['trades_period_days']

        self.logger.info("✅ Configuration chargée et validée")
        self.logger.info(f"   Mode            : {mode.upper()}")
        self.logger.info(f"   Paire           : {pair} | Timeframe : {tf}")
        self.logger.info(f"   Période backtest: {start} → {end}")
        self.logger.info(f"   Durée session   : {period} jour(s)")

    def _validate_required_config_sections(self) -> None:
        """
        Vérifie que toutes les sections de configuration requises sont présentes.

        Cette validation est une garde supplémentaire après load_config().
        Elle protège engine.py contre des configurations partiellement invalides
        qui auraient échappé à Pydantic.

        Raises:
            EngineConfigurationError: Si une ou plusieurs sections manquent.
        """
        assert self._config_dict is not None

        missing_sections = [
            section for section in _REQUIRED_CONFIG_SECTIONS
            if section not in self._config_dict
        ]

        if missing_sections:
            raise EngineConfigurationError(
                f"Sections de configuration requises manquantes : "
                f"{missing_sections}. "
                f"Vérifiez config.json et sa compatibilité avec config_loader v2.3.2."
            )

        missing_keys = [
            f"config['{section}']['{key}']"
            for section, key in _REQUIRED_BACKTESTING_KEYS
            if not str(self._config_dict.get(section, {}).get(key) or "").strip()
        ]

        if missing_keys:
            raise EngineConfigurationError(
                f"Clés de configuration requises manquantes ou vides :\n"
                + "\n".join(f"  • {k}" for k in missing_keys)
            )

    # =========================================================================
    # PHASE 1 — VALIDATION COHÉRENCE DE PÉRIODE (RESPONSABILITÉ EXCLUSIVE)
    # =========================================================================

    def _phase_validate_period_coherence(self) -> None:
        """
        Valide que la durée totale du backtest est un multiple exact
        de trades_period_days.

        CETTE VALIDATION EXISTE UNIQUEMENT DANS engine.py.
        Aucun sous-moteur ne doit dupliquer cette logique.

        Exemple :
            start = 2025-01-01, end = 2025-03-01 → 59 jours
            trades_period_days = 10 → 59 % 10 = 9 ≠ 0 → ERREUR

            start = 2025-01-01, end = 2025-03-11 → 69 jours → non multiple
            start = 2025-01-01, end = 2025-02-11 → 41 jours → non multiple
            start = 2025-01-01, end = 2025-04-01 → 90 jours
            trades_period_days = 10 → 90 % 10 = 0 → ✅

        Raises:
            EnginePeriodCoherenceError: Si la durée n'est pas un multiple exact.
            EngineConfigurationError:   Si les dates sont invalides ou incohérentes.
        """
        assert self._config_dict is not None

        self.logger.log_separator('INFO', '-', 60)
        self.logger.info("PHASE 1 — Validation cohérence de période")

        start_str  = self._config_dict['backtesting']['start_date']
        end_str    = self._config_dict['backtesting']['end_date']
        period_days = int(self._config_dict['session_management']['trades_period_days'])

        # Parsing des dates (YYYY-MM-DD — format imposé par BacktestingConfig)
        try:
            start_dt = datetime.strptime(start_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            end_dt   = datetime.strptime(end_str,   "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError as exc:
            raise EngineConfigurationError(
                f"Format de date invalide dans config.json (attendu YYYY-MM-DD) : {exc}"
            ) from exc

        if end_dt <= start_dt:
            raise EngineConfigurationError(
                f"La date de fin ({end_str}) doit être strictement postérieure "
                f"à la date de début ({start_str})."
            )

        total_days = (end_dt - start_dt).days

        if total_days < period_days:
            raise EnginePeriodCoherenceError(
                f"La durée totale du backtest ({total_days} jour(s)) est inférieure "
                f"à trades_period_days ({period_days} jour(s)). "
                f"Impossible de créer au moins une session complète. "
                f"Augmentez la période ou réduisez trades_period_days dans config.json."
            )

        remainder = total_days % period_days

        if remainder != 0:
            nb_complete      = total_days // period_days
            closest_multiple = nb_complete * period_days
            suggestion_end   = (start_dt + timedelta(days=closest_multiple)).strftime("%Y-%m-%d")

            raise EnginePeriodCoherenceError(
                f"La durée totale du backtest ({total_days} jour(s)) n'est pas un multiple "
                f"exact de trades_period_days ({period_days} jour(s)). "
                f"Reste : {remainder} jour(s). "
                f"Corrigez backtesting.end_date dans config.json. "
                f"Suggestion : end_date = '{suggestion_end}' "
                f"({nb_complete} session(s) complète(s) × {period_days} jour(s))."
            )

        nb_sessions = total_days // period_days

        self.logger.info(
            f"✅ Cohérence de période validée : "
            f"{total_days} jour(s) ÷ {period_days} = {nb_sessions} session(s)"
        )
        self.logger.info(f"   Période : {start_str} → {end_str}")

    # =========================================================================
    # PHASE 2 — INJECTION DE DÉPENDANCES & INITIALISATION ORDONNÉE
    # =========================================================================

    def _phase_initialize_subsystems(self) -> None:
        """
        Instancie tous les sous-moteurs dans le bon ordre selon le graphe DI.

        Ordre d'instanciation (dépendances décroissantes) :
            1. OHLCVDataEngine  — aucune dépendance inter-module
            2. SessionManager   — source de vérité capital
            3. SignalGenerator  — génère les signaux bruts
            4. RiskManager      — calcule le risque
            5. Strategy         — reçoit 3 et 4 par injection
            6. OrderSimulator   — exécute les ordres
            7. ATRIndicator     — requis si trailing type='atr'/'hybrid'
            8. PositionManager  — reçoit 7 par injection
            9. TradingEngine    — reçoit 2, 5, 6, 8 par injection
           10. AnalyticsEngine  — sans dépendance (stateless, config autonome)

        Chaque module reçoit UNIQUEMENT ce dont il a strictement besoin.
        Aucun état global n'est créé ici.

        Raises:
            EngineConfigurationError: Si l'instanciation d'un sous-moteur échoue.
        """
        assert self._config_dict is not None

        self.logger.log_separator('INFO', '-', 60)
        self.logger.info("PHASE 2 — Initialisation des sous-moteurs (DI)")

        cfg = self._config_dict  # Alias local — dict plain, pas de référence circulaire

        # [v2.1.4 — FIX-ENG-5] Validation des configs indicateurs AVANT instanciation.
        # UncertaintyCandleIndicator, VolumeIndicator, TrendIndicator et ATRIndicator
        # chargent leurs propres fichiers JSON au moment de leur __init__().
        # Sans cette validation préalable, une config absente ou corrompue déclenche
        # une exception dans la phase 2 avec un message peu lisible (TypeError/KeyError
        # perdu dans la stack d'instanciation). On valide ici, fail-fast, avec un
        # message clair indiquant le fichier manquant et la solution.
        self._phase_validate_indicator_configs()

        try:
            # ── 1. OHLCVDataEngine ───────────────────────────────────────────
            self.logger.debug("  [1/10] OHLCVDataEngine...")
            self._ohlcv_engine = OHLCVDataEngine(config=cfg)

            # ── 2. SessionManager — source de vérité capital ─────────────────
            self.logger.debug("  [2/10] SessionManager...")
            session_manager = SessionManager(config=cfg)

            # ── 3. SignalGenerator — génère les signaux bruts ────────────────
            self.logger.debug("  [3/10] SignalGenerator...")
            signal_generator = SignalGenerator(config=cfg)

            # ── 4. RiskManager — calcule le risque ───────────────────────────
            self.logger.debug("  [4/10] RiskManager...")
            risk_manager = RiskManager(config=cfg)

            # ── 5. Strategy — reçoit ses dépendances par injection ───────────
            self.logger.debug("  [5/10] Strategy (DI)...")
            strategy = Strategy(
                config=cfg,
                signal_generator=signal_generator,
                risk_manager=risk_manager,
            )

            # ── 6. OrderSimulator — exécute les ordres ───────────────────────
            self.logger.debug("  [6/10] OrderSimulator...")
            order_simulator = OrderSimulator(
                session_manager=session_manager,
                mode=cfg['general']['mode'],
                config=cfg,
            )

            # ── 7. ATRIndicator — requis si trailing type='atr'/'hybrid' ─────
            self.logger.debug("  [7/10] ATRIndicator...")
            trailing_type = cfg['strategy']['trailing_stop']['type']
            atr_indicator = ATRIndicator(config=cfg) if trailing_type in ('atr', 'hybrid') else None

            # ── 8. PositionManager — reçoit atr_indicator par injection ──────
            self.logger.debug("  [8/10] PositionManager...")
            position_manager = PositionManager(
                config=cfg,
                atr_indicator=atr_indicator,
            )

            # ── 9. TradingEngine — reçoit ses dépendances par injection ──────
            self.logger.debug("  [9/10] TradingEngine (DI)...")

            # [v2.1.4 — FIX-ENG-2] EngineConfig hydraté depuis config.json.
            # Avant ce fix, EngineConfig était instancié avec ses valeurs par défaut
            # codées en dur dans trading_engine.py, rendant ses paramètres impossibles
            # à configurer sans modifier le code source.
            # La section 'engine_config' est optionnelle : si absente, les défauts
            # de EngineConfig s'appliquent (rétro-compatibilité garantie).
            ec_raw: dict = cfg.get('engine_config', {})
            _ec_defaults = EngineConfig()   # Instance de référence pour les valeurs par défaut
            engine_config = EngineConfig(
                min_candles_window             = ec_raw.get(
                    'min_candles_window',            _ec_defaults.min_candles_window),
                max_candles_window             = ec_raw.get(
                    'max_candles_window',            _ec_defaults.max_candles_window),
                accumulate_funding             = ec_raw.get(
                    'accumulate_funding',            _ec_defaults.accumulate_funding),
                close_position_on_session_end  = ec_raw.get(
                    'close_position_on_session_end', _ec_defaults.close_position_on_session_end),
                log_every_n_candles            = ec_raw.get(
                    'log_every_n_candles',           _ec_defaults.log_every_n_candles),
            )

            if ec_raw:
                self.logger.info(
                    f"   EngineConfig (depuis config.json) : "
                    f"min_window={engine_config.min_candles_window}, "
                    f"max_window={engine_config.max_candles_window}, "
                    f"funding={engine_config.accumulate_funding}, "
                    f"close_on_end={engine_config.close_position_on_session_end}, "
                    f"log_n={engine_config.log_every_n_candles}"
                )
            else:
                self.logger.debug(
                    "   EngineConfig : section 'engine_config' absente de config.json — "
                    "valeurs par défaut de EngineConfig appliquées."
                )

            # [v2.1.6 — FEAT-MC-2] Feature flag : market_context.enabled
            # Configurable depuis config['engine_config']['market_context']['enabled'].
            # Si false (ou clé absente) → market_context = None → les trades
            # s'enregistrent sans origine_signal, comportement identique à l'avant
            # l'implémentation de MarketContextCapture.
            # Si true → instanciation normale (comportement v2.1.5).
            _mc_cfg     = ec_raw.get('market_context', {})
            _mc_enabled = str(_mc_cfg.get('enabled', 'true')).strip().lower() == 'true'

            if not _mc_enabled:
                self.logger.info(
                    "   [9b/10] MarketContextCapture désactivé "
                    "(engine_config.market_context.enabled=false) — "
                    "les trades n'auront pas d'origine_signal."
                )
                market_context = None
            else:
                # [v2.1.5 — FEAT-MC-1] Instanciation de MarketContextCapture.
                # L'instance ATRIndicator (déjà créée à l'étape 7) est RÉUTILISÉE :
                # cohérence de configuration garantie, zéro surcoût de calcul.
                # Toute erreur d'initialisation est absorbée (non-fatale) : le backtest
                # continue sans capture de contexte marché plutôt que de s'interrompre.
                self.logger.debug("  [9b/10] MarketContextCapture (DI)...")
                try:
                    market_context = MarketContextCapture(
                        config=cfg,
                        atr_indicator=atr_indicator,
                    )
                    mc_status = market_context.get_status()
                    self.logger.info(
                        f"   MarketContextCapture : "
                        f"{mc_status['ready_count']}/{mc_status['total_count']} indicateurs | "
                        f"atr={'injected' if mc_status['atr_injected'] else 'none'} | "
                        f"min_candles={mc_status['min_candles']}"
                    )
                    if mc_status['init_errors']:
                        self.logger.warning(
                            f"   ⚠️  {len(mc_status['init_errors'])} erreur(s) d'init "
                            f"MarketContextCapture — capture partielle possible"
                        )
                except Exception as mc_init_exc:
                    self.logger.error(
                        f"   ❌ MarketContextCapture init failed (non-fatal) : "
                        f"{type(mc_init_exc).__name__}: {mc_init_exc}. "
                        f"Les trades n'auront pas d'origine_signal."
                    )
                    market_context = None

            self._trading_engine = TradingEngine(
                config=cfg,
                strategy=strategy,
                session_manager=session_manager,
                order_simulator=order_simulator,
                position_manager=position_manager,
                engine_config=engine_config,
                market_context=market_context,
            )

            # ── 10. AnalyticsEngine — stateless, config autonome ────────────
            self.logger.debug("  [10/10] AnalyticsEngine...")
            self._analytics_engine = AnalyticsEngine()

        except (TypeError, ValueError) as exc:
            raise EngineConfigurationError(
                f"Échec d'initialisation d'un sous-moteur : "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        except Exception as exc:
            raise EngineConfigurationError(
                f"Erreur inattendue lors de l'initialisation : "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        self.logger.info("✅ Tous les sous-moteurs initialisés avec succès")

    # =========================================================================
    # PHASE 2 — VALIDATION CONFIGS INDICATEURS (sous-routine)
    # =========================================================================

    def _phase_validate_indicator_configs(self) -> None:
        """
        Valide l'existence et la lisibilité des fichiers JSON de configuration
        des indicateurs AVANT leur instanciation.

        [v2.1.4 — FIX-ENG-5] Les indicateurs (UncertaintyCandleIndicator,
        VolumeIndicator, TrendIndicator, ATRIndicator) chargent leurs configs
        dans leur __init__(). Un fichier absent ou corrompu échoue avec une
        exception cryptique perdue dans la phase 2. Ce hook valide fail-fast
        avec un message clair et actionnable.

        Configs validées :
            - config/uncertainty_candle_config.json  (WARNING si absent — fallback dispo)
            - config/volume_config.json              (ERREUR FATALE — obligatoire)
            - config/trend_config.json               (WARNING si absent — fallback dispo)
            - config/atr_config.json                 (ERREUR FATALE si trailing=atr/hybrid)

        La validation ATR est conditionnelle au mode trailing configuré.

        Raises:
            EngineConfigurationError: Si un fichier obligatoire est absent ou invalide.
        """
        assert self._config_dict is not None

        project_root = get_project_root()
        config_dir   = project_root / 'config'
        mode         = self._config_dict['general']['mode'].upper()
        trailing_type = (
            self._config_dict.get('strategy', {})
            .get('trailing_stop', {})
            .get('type', 'candle')
        )

        # ── 1. uncertainty_candle_config.json — WARNING si absent (fallback DEFAULT_CONFIG) ──
        uc_path = config_dir / 'uncertainty_candle_config.json'
        if not uc_path.exists():
            self.logger.warning(
                f"⚠️  config/uncertainty_candle_config.json introuvable. "
                f"UncertaintyCandleIndicator utilisera DEFAULT_CONFIG (valeurs figées). "
                f"Créez {uc_path} pour un contrôle fin des paramètres."
            )
        else:
            try:
                with open(uc_path, 'r', encoding='utf-8') as f:
                    json.load(f)
                self.logger.debug(f"   ✅ uncertainty_candle_config.json — OK")
            except Exception as exc:
                raise EngineConfigurationError(
                    f"config/uncertainty_candle_config.json est invalide (JSON corrompu) : {exc}\n"
                    f"Chemin : {uc_path}"
                ) from exc

        # ── 2. volume_config.json — OBLIGATOIRE (VolumeIndicator lève FileNotFoundError) ──
        vol_path = config_dir / 'volume_config.json'
        if not vol_path.exists():
            raise EngineConfigurationError(
                f"config/volume_config.json introuvable.\n"
                f"Ce fichier est OBLIGATOIRE pour VolumeIndicator (v2.4.0+).\n"
                f"Chemin attendu : {vol_path}\n"
                f"Créez ce fichier avant de lancer le backtest."
            )
        try:
            with open(vol_path, 'r', encoding='utf-8') as f:
                json.load(f)
            self.logger.debug(f"   ✅ volume_config.json — OK")
        except Exception as exc:
            raise EngineConfigurationError(
                f"config/volume_config.json est invalide (JSON corrompu) : {exc}\n"
                f"Chemin : {vol_path}"
            ) from exc

        # ── 3. trend_config.json — WARNING si absent (TrendIndicator a des defaults internes) ──
        trend_path = config_dir / 'trend_config.json'
        if not trend_path.exists():
            self.logger.warning(
                f"⚠️  config/trend_config.json introuvable. "
                f"TrendIndicator utilisera ses paramètres par défaut internes "
                f"(MA rapide=50, MA lente=200, type=EMA). "
                f"Créez {trend_path} pour personnaliser le filtre de tendance."
            )
        else:
            try:
                with open(trend_path, 'r', encoding='utf-8') as f:
                    json.load(f)
                self.logger.debug(f"   ✅ trend_config.json — OK")
            except Exception as exc:
                raise EngineConfigurationError(
                    f"config/trend_config.json est invalide (JSON corrompu) : {exc}\n"
                    f"Chemin : {trend_path}"
                ) from exc

        # ── 4. atr_config.json — OBLIGATOIRE si trailing type est 'atr' ou 'hybrid' ──
        atr_path = config_dir / 'atr_config.json'
        if trailing_type in ('atr', 'hybrid'):
            if not atr_path.exists():
                raise EngineConfigurationError(
                    f"config/atr_config.json introuvable.\n"
                    f"Ce fichier est OBLIGATOIRE car trailing_stop.type='{trailing_type}'.\n"
                    f"Chemin attendu : {atr_path}\n"
                    f"Créez ce fichier ou changez trailing_stop.type en 'candle'."
                )
            try:
                with open(atr_path, 'r', encoding='utf-8') as f:
                    json.load(f)
                self.logger.debug(f"   ✅ atr_config.json — OK (trailing={trailing_type})")
            except Exception as exc:
                raise EngineConfigurationError(
                    f"config/atr_config.json est invalide (JSON corrompu) : {exc}\n"
                    f"Chemin : {atr_path}"
                ) from exc
        else:
            # Mode candle : atr_config.json optionnel
            if not atr_path.exists():
                self.logger.debug(
                    f"   atr_config.json absent — non requis (trailing='{trailing_type}')"
                )
            else:
                self.logger.debug(f"   ✅ atr_config.json — présent (trailing='{trailing_type}')")

        self.logger.info("   ✅ Configs indicateurs validées")

    # =========================================================================
    # PHASE 3 — PIPELINE OHLCV
    # =========================================================================

    def _phase_run_data_pipeline(self) -> pd.DataFrame:
        """
        Orchestre le chargement et la validation des données OHLCV.

        Ce module DÉLÈGUE entièrement à OHLCVDataEngine.load_and_validate().
        Il effectue uniquement des vérifications de niveau orchestration :
            - DataFrame non vide
            - Couverture de la période configurée

        Returns:
            pd.DataFrame: DataFrame OHLCV complet, validé, prêt pour la boucle.

        Raises:
            EngineDataError: Si le dataset est vide, insuffisant, ou ne couvre
                             pas la période configurée.
        """
        assert self._ohlcv_engine  is not None
        assert self._config_dict   is not None

        self.logger.log_separator('INFO', '-', 60)
        self.logger.info("PHASE 3 — Pipeline de données OHLCV")
        data_start_ts = time.monotonic()

        try:
            ohlcv_df = self._ohlcv_engine.load_and_validate()
        except (FileNotFoundError, ValueError, RuntimeError) as exc:
            raise EngineDataError(
                f"Échec du pipeline OHLCV : {type(exc).__name__}: {exc}"
            ) from exc
        except Exception as exc:
            raise EngineDataError(
                f"Erreur inattendue dans le pipeline OHLCV : "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        data_elapsed = time.monotonic() - data_start_ts

        # Validation niveau orchestration : dataset non vide
        if ohlcv_df is None or ohlcv_df.empty:
            raise EngineDataError(
                "Le pipeline OHLCV a retourné un DataFrame vide. "
                "Vérifiez le fichier CSV source et la plage de dates configurée."
            )

        # Validation NaN sur colonnes critiques (niveau orchestration — non-exhaustif)
        ohlcv_cols = [c for c in ("open", "high", "low", "close", "volume") if c in ohlcv_df.columns]
        nan_count  = int(ohlcv_df[ohlcv_cols].isna().sum().sum()) if ohlcv_cols else 0
        if nan_count > 0:
            raise EngineDataError(
                f"Le DataFrame OHLCV contient {nan_count} valeur(s) NaN "
                f"sur les colonnes critiques {ohlcv_cols} après traitement. "
                f"Vérifiez la qualité des données source."
            )

        # Validation couverture de période
        self._validate_dataset_coverage(ohlcv_df)

        self.logger.info(
            f"✅ Pipeline OHLCV terminé — {len(ohlcv_df):,} bougies chargées "
            f"en {data_elapsed:.2f}s"
        )

        return ohlcv_df

    def _validate_dataset_coverage(self, ohlcv_df: pd.DataFrame) -> None:
        """
        Vérifie que le dataset OHLCV couvre toute la période configurée.

        Nous effectuons une vérification de niveau orchestration : la première
        et la dernière timestamp du DataFrame doivent être au plus à une tolérance
        d'un jour par rapport aux dates configurées.

        Args:
            ohlcv_df: DataFrame OHLCV retourné par OHLCVDataEngine.

        Raises:
            EngineDataError: Si la couverture est insuffisante.
        """
        assert self._config_dict is not None

        if "timestamp" not in ohlcv_df.columns:
            # Avertissement — ne bloque pas si timestamp absent (cas de test)
            self.logger.warning(
                "⚠️  Colonne 'timestamp' absente du DataFrame OHLCV — "
                "validation de couverture de période ignorée."
            )
            return

        start_str   = self._config_dict['backtesting']['start_date']
        end_str     = self._config_dict['backtesting']['end_date']

        # [v2.2.2 — FIX-ENG-8] Tolérance adaptée au timeframe configuré.
        # L'ancienne valeur fixe timedelta(days=1) autorisait silencieusement
        # 1 440 bougies manquantes sur un timeframe 1m — aucune alerte émise.
        # Nouvelle logique : tolerance = max(1 bougie du timeframe, 1 jour).
        #   → timeframes fins (≤1h)  : quelques minutes/secondes de tolérance
        #   → timeframes journaliers+ : 1 jour (comportement inchangé)
        # Fallback sur 1 jour si timeframe inconnu (dégradation gracieuse).
        tf_str      = (self._config_dict or {}).get('general', {}).get('timeframe', '')
        tf_minutes  = _parse_timeframe_minutes(tf_str)
        if tf_minutes > 0:
            tolerance = max(timedelta(minutes=tf_minutes), timedelta(days=1))
        else:
            tolerance = timedelta(days=1)   # Fallback : timeframe inconnu
        self.logger.debug(
            f"[FIX-ENG-8] Tolérance couverture dataset : {tolerance} "
            f"(timeframe='{tf_str}', tf_minutes={tf_minutes})"
        )

        try:
            config_start = datetime.strptime(start_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            config_end   = datetime.strptime(end_str,   "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            # Déjà validé en phase 1 — cas impossible ici
            return

        # Récupération première/dernière timestamp du dataset
        raw_first = ohlcv_df["timestamp"].iloc[0]
        raw_last  = ohlcv_df["timestamp"].iloc[-1]

        try:
            df_start = timestamp_to_datetime(raw_first)
            df_end   = timestamp_to_datetime(raw_last)
        except (ValueError, TypeError) as exc:
            self.logger.warning(
                f"⚠️  Impossible de parser les timestamps OHLCV "
                f"pour validation de couverture : {exc}"
            )
            return

        # Vérification couverture début
        if df_start > config_start + tolerance:
            raise EngineDataError(
                f"Le dataset OHLCV commence le {df_start.strftime('%Y-%m-%d')} "
                f"mais la période configurée débute le {start_str}. "
                f"Le dataset ne couvre pas le début de la période. "
                f"Vérifiez le fichier CSV source."
            )

        # Vérification couverture fin
        if df_end < config_end - tolerance:
            raise EngineDataError(
                f"Le dataset OHLCV se termine le {df_end.strftime('%Y-%m-%d')} "
                f"mais la période configurée se termine le {end_str}. "
                f"Le dataset ne couvre pas la fin de la période. "
                f"Vérifiez le fichier CSV source."
            )

        self.logger.debug(
            f"[Coverage] Dataset : {df_start.strftime('%Y-%m-%d')} → "
            f"{df_end.strftime('%Y-%m-%d')} ✅ couvre "
            f"{start_str} → {end_str}"
        )

    # =========================================================================
    # PHASE 4 — SEGMENTATION & BOUCLE DE SESSIONS TRADING
    # =========================================================================

    def _phase_run_session_loop(self, ohlcv_df: pd.DataFrame) -> None:
        """
        Divise la période de backtest en sessions et exécute chaque session.

        Responsabilités de cette phase :
            - Calculer les bornes temporelles de chaque session
            - Extraire la tranche OHLCV correspondante
            - Appeler TradingEngine.run_session() session par session
            - Maintenir un compteur de sessions (1-based)
            - Collecter les EngineRunResult dans self._all_session_results

        Le TradingEngine reste ignorant du scope global.
        La boucle globale appartient EXCLUSIVEMENT à engine.py.

        Args:
            ohlcv_df: DataFrame OHLCV complet validé par la phase 3.

        Raises:
            RuntimeError: Si TradingEngine lève une exception fatale.
        """
        assert self._trading_engine is not None
        assert self._config_dict    is not None

        self.logger.log_separator('INFO', '-', 60)
        self.logger.info("PHASE 4 — Boucle de sessions trading")

        start_str    = self._config_dict['backtesting']['start_date']
        end_str      = self._config_dict['backtesting']['end_date']
        period_days  = int(self._config_dict['session_management']['trades_period_days'])

        # Parsing — déjà validé en phase 1, pas de risque ici
        start_dt = datetime.strptime(start_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end_dt   = datetime.strptime(end_str,   "%Y-%m-%d").replace(tzinfo=timezone.utc)

        total_days   = (end_dt - start_dt).days
        nb_sessions  = total_days // period_days  # Multiple exact garanti par phase 1

        self.logger.info(
            f"   {nb_sessions} session(s) de {period_days} jour(s) à exécuter "
            f"sur {total_days} jour(s)"
        )

        sessions_elapsed_total = 0.0
        self._all_session_results = []

        for session_n in range(1, nb_sessions + 1):
            session_start_dt, session_end_dt = self._compute_session_bounds(
                global_start=start_dt,
                session_n=session_n,
                period_days=period_days,
            )

            self.logger.log_separator('INFO', '-', 60)
            self.logger.info(
                f"SESSION {session_n}/{nb_sessions} — "
                f"{session_start_dt.strftime('%Y-%m-%d')} → "
                f"{session_end_dt.strftime('%Y-%m-%d')}"
            )

            # Extraction de la tranche OHLCV pour cette session
            data_slice = self._extract_session_slice(
                ohlcv_df=ohlcv_df,
                session_start=session_start_dt,
                session_end=session_end_dt,
            )

            if data_slice.empty:
                self.logger.warning(
                    f"⚠️  Session {session_n} : aucune bougie dans la tranche "
                    f"{session_start_dt.strftime('%Y-%m-%d')} → "
                    f"{session_end_dt.strftime('%Y-%m-%d')}. Session ignorée."
                )
                # [v2.2.2 — FIX-ENG-9] Compteur sessions vides — tracé dans résumé final.
                self._sessions_skipped += 1
                continue

            # [v2.1.6 — FEAT-WARMUP] Extraction bougies de préchauffage
            warmup_slice = self._extract_warmup_slice(
                ohlcv_df=ohlcv_df,
                session_start=session_start_dt,
            )

            self.logger.info(f"   Bougies session : {len(data_slice):,}"
                             + (f" + {len(warmup_slice)} warmup" if not warmup_slice.empty else ""))

            # Exécution de la session via TradingEngine
            session_ts = time.monotonic()
            try:
                result: EngineRunResult = self._trading_engine.run_session(
                    session_n=session_n,
                    start_date=session_start_dt,
                    end_date=session_end_dt,
                    candles=data_slice,
                    warmup_candles=warmup_slice if not warmup_slice.empty else None,
                )
            except RuntimeError as exc:
                # Engine en état STOPPED — non récupérable sans reset explicite
                self.logger.critical(
                    f"❌ Session {session_n} : TradingEngine en état STOPPED — "
                    f"arrêt immédiat du pipeline : {exc}"
                )
                raise
            except Exception as exc:
                # Erreur fatale dans TradingEngine — propagation
                self.logger.exception(
                    f"❌ Session {session_n} : erreur fatale dans TradingEngine : {exc}"
                )
                raise RuntimeError(
                    f"Session {session_n} a échoué de façon inattendue : "
                    f"{type(exc).__name__}: {exc}"
                ) from exc

            session_elapsed = time.monotonic() - session_ts
            sessions_elapsed_total += session_elapsed

            # Validation du contrat : session_id est la clé primaire attendue par analytics
            if not result.session_id:
                self.logger.warning(
                    f"⚠️  Session {session_n} : EngineRunResult.session_id absent ou vide. "
                    f"AnalyticsEngine utilisera un identifiant horodaté."
                )

            # Collecte du résultat
            self._all_session_results.append(result)

            # Hook extensibilité — point d'extension pour futures fonctionnalités
            # (ex: notification temps réel, écriture intermédiaire, parallélisme)
            self._on_session_complete(session_n=session_n, result=result)

            # Log de progression avec métriques de session
            trades_count = len(result.trades) if result.trades else 0
            pnl          = result.session_summary.get('pnl', 0.0) if result.session_summary else 0.0
            self.logger.info(
                f"✅ Session {session_n}/{nb_sessions} terminée — "
                f"{trades_count} trade(s) | PnL : {pnl:+.4f} USDT | "
                f"durée : {session_elapsed:.2f}s"
            )

        self.logger.log_separator('INFO', '-', 60)
        self.logger.info(
            f"✅ Boucle de sessions terminée — "
            f"{len(self._all_session_results)}/{nb_sessions} session(s) collectée(s) | "
            f"{self._sessions_skipped} ignorée(s) (données vides) | "    # [FIX-ENG-9]
            f"durée trading : {sessions_elapsed_total:.2f}s"
        )

    def _compute_session_bounds(
        self,
        global_start: datetime,
        session_n: int,
        period_days: int,
    ) -> Tuple[datetime, datetime]:
        """
        Calcule les bornes temporelles UTC d'une session.

        La segmentation est purement arithmétique et déterministe.
        Aucune logique métier n'est appliquée ici.

        Args:
            global_start: Date de début globale du backtest (UTC-aware).
            session_n:    Numéro de session (1-based).
            period_days:  Durée d'une session en jours.

        Returns:
            Tuple (session_start, session_end), tous deux UTC-aware.
        """
        offset_start = (session_n - 1) * period_days
        offset_end   = session_n       * period_days

        session_start = global_start + timedelta(days=offset_start)
        session_end   = global_start + timedelta(days=offset_end)

        return session_start, session_end

    def _extract_session_slice(
        self,
        ohlcv_df: pd.DataFrame,
        session_start: datetime,
        session_end: datetime,
    ) -> pd.DataFrame:
        """
        Extrait la tranche OHLCV correspondant à une session (session réelle uniquement).

        Filtre par timestamp avec bornes [session_start, session_end).
        Les bougies de warmup (avant session_start) sont gérées séparément
        par _extract_warmup_slice() et transmises via warmup_candles à run_session.

        Args:
            ohlcv_df:      DataFrame OHLCV complet (inclut warmup si chargé).
            session_start: Début de session réel (UTC-aware).
            session_end:   Fin de session (UTC-aware).

        Returns:
            pd.DataFrame: Bougies de la session [session_start, session_end).
                          Index réinitialisé (drop=True).
        """
        if "timestamp" not in ohlcv_df.columns:
            self.logger.warning(
                "⚠️  Colonne 'timestamp' absente — extraction par position ignorée. "
                "Le DataFrame complet est transmis à chaque session."
            )
            return ohlcv_df.reset_index(drop=True)

        ts_col = ohlcv_df["timestamp"]
        try:
            if pd.api.types.is_datetime64_any_dtype(ts_col):
                ts_utc = pd.to_datetime(ts_col, utc=True)
            elif pd.api.types.is_integer_dtype(ts_col) or pd.api.types.is_float_dtype(ts_col):
                ts_utc = pd.to_datetime(ts_col, unit='ms', utc=True)
            else:
                ts_utc = pd.to_datetime(ts_col, utc=True)
        except Exception as exc:
            self.logger.warning(
                f"⚠️  Impossible de normaliser les timestamps : {exc}. "
                "DataFrame complet transmis."
            )
            return ohlcv_df.reset_index(drop=True)

        ts_start = pd.Timestamp(session_start)
        ts_end   = pd.Timestamp(session_end)

        mask = (ts_utc >= ts_start) & (ts_utc < ts_end)
        return ohlcv_df.loc[mask].reset_index(drop=True)

    def _extract_warmup_slice(
        self,
        ohlcv_df: pd.DataFrame,
        session_start: datetime,
    ) -> pd.DataFrame:
        """
        Extrait les bougies de préchauffage antérieures à session_start.

        [v2.1.6 — FEAT-WARMUP] Ces bougies alimentent candles_window dans
        TradingEngine sans jamais passer dans step() (session management,
        signaux et positions sont strictement isolés de ces candles).

        Le nombre de bougies demandées est calculé depuis :
            config['engine_config']['market_context_min_candles'] × timeframe

        Si le CSV ne remonte pas assez loin, retourne ce qui est disponible
        (graceful degradation — pas d'erreur fatale).

        Args:
            ohlcv_df:      DataFrame OHLCV complet.
            session_start: Début de session réel (UTC-aware).

        Returns:
            pd.DataFrame: Bougies [warmup_start, session_start).
                          Vide si aucune bougie disponible avant session_start.
        """
        if "timestamp" not in ohlcv_df.columns:
            return pd.DataFrame()

        # Calcul de la borne warmup_start
        ec           = (self._config_dict or {}).get('engine_config', {})
        warmup_n     = int(ec.get('market_context_min_candles', 300))
        tf_str       = (self._config_dict or {}).get('general', {}).get('timeframe', '')
        tf_minutes   = _parse_timeframe_minutes(tf_str)

        if warmup_n <= 0 or tf_minutes <= 0:
            return pd.DataFrame()

        warmup_start = session_start - timedelta(minutes=warmup_n * tf_minutes)

        ts_col = ohlcv_df["timestamp"]
        try:
            if pd.api.types.is_datetime64_any_dtype(ts_col):
                ts_utc = pd.to_datetime(ts_col, utc=True)
            elif pd.api.types.is_integer_dtype(ts_col) or pd.api.types.is_float_dtype(ts_col):
                ts_utc = pd.to_datetime(ts_col, unit='ms', utc=True)
            else:
                ts_utc = pd.to_datetime(ts_col, utc=True)
        except Exception:
            return pd.DataFrame()

        ts_warmup = pd.Timestamp(warmup_start)
        ts_start  = pd.Timestamp(session_start)

        mask = (ts_utc >= ts_warmup) & (ts_utc < ts_start)
        warmup_df = ohlcv_df.loc[mask].reset_index(drop=True)

        if warmup_df.empty:
            self.logger.warning(
                f"[FEAT-WARMUP] Aucune bougie disponible avant "
                f"{session_start.strftime('%Y-%m-%d')} dans le CSV. "
                f"Les premiers trades peuvent manquer d'origine_signal."
            )
        else:
            available = len(warmup_df)
            self.logger.debug(
                f"[FEAT-WARMUP] {available}/{warmup_n} bougies warmup disponibles "
                f"({warmup_start.strftime('%Y-%m-%d')} → "
                f"{session_start.strftime('%Y-%m-%d')})"
            )

        return warmup_df

    # =========================================================================
    # PHASE 5 — ANALYTICS
    # =========================================================================

    def _phase_run_analytics(self) -> None:
        """
        Transmet chaque résultat de session à AnalyticsEngine pour reporting.

        Comportement :
            - Les erreurs d'analytics sont absorbées par AnalyticsEngine lui-même
              (generate_reports ne lève jamais d'exception).
            - engine.py log un WARNING si des erreurs analytics sont retournées,
              mais ne bloque PAS l'exécution des sessions suivantes.
            - Aucune transformation analytique n'est effectuée ici.

        Post-conditions :
            self._all_analytics_paths contient un dict de paths par session.
        """
        assert self._analytics_engine is not None

        self.logger.log_separator('INFO', '-', 60)
        self.logger.info("PHASE 5 — Génération des rapports analytics")

        self._all_analytics_paths = []
        analytics_errors_total    = 0

        for i, result in enumerate(self._all_session_results, start=1):
            session_id = getattr(result, 'session_id', f'session_{i}')

            self.logger.info(
                f"   Analytics session {i}/{len(self._all_session_results)} "
                f"(id={session_id})..."
            )

            try:
                paths = self._analytics_engine.generate_reports(result)
            except Exception as exc:
                # Defensive catch — AnalyticsEngine ne devrait jamais lever,
                # mais on garantit la résilience de la boucle dans tous les cas.
                self.logger.exception(
                    f"⚠️  Analytics session {i} : exception inattendue (non-fatale) : {exc}"
                )
                paths = {
                    'session_dir': None,
                    'html': None, 'markdown': None, 'text': None,
                    'json': None, 'csv': None,
                    'errors': [f"Exception inattendue : {type(exc).__name__}: {exc}"],
                }

            # Rapport des erreurs analytics non-fatales
            analytics_errors = paths.get('errors', [])
            if analytics_errors:
                analytics_errors_total += len(analytics_errors)
                self.logger.warning(
                    f"⚠️  Session {i} : {len(analytics_errors)} erreur(s) analytics "
                    f"(non-fatales) : {analytics_errors}"
                )

            session_dir = paths.get('session_dir')
            if session_dir:
                self.logger.info(f"   ✅ Rapports session {i} → {session_dir}")

            self._all_analytics_paths.append(paths)

        self.logger.info(
            f"✅ Phase analytics terminée — "
            f"{len(self._all_analytics_paths)} session(s) | "
            f"{analytics_errors_total} erreur(s) analytics totale(s)"
        )

    # =========================================================================
    # HOOK D'EXTENSIBILITÉ
    # =========================================================================

    def _on_session_complete(
        self,
        session_n: int,
        result: EngineRunResult,
    ) -> None:
        """
        Hook appelé après chaque session terminée.

        Conçu pour extension future sans modification de la boucle principale :
            - Mode temps réel : notification immédiate
            - Exécution parallèle : soumission à un pool
            - Architecture distribuée : envoi vers message queue
            - Analytics en streaming : génération à chaud

        Dans la version courante (backtesting séquentiel), ce hook ne fait rien.
        Les sous-classes peuvent le surcharger sans toucher à run().

        Args:
            session_n: Numéro de session (1-based).
            result:    Résultat complet retourné par TradingEngine.
        """
        pass  # Extension point — intentionnellement vide


# ============================================================================
# POINT D'ENTRÉE CLI
# ============================================================================

def main() -> None:
    """
    Point d'entrée principal pour exécution CLI.

    Usage :
        python engine.py
        python engine.py --config config/my_config.json

    Retourne le code de sortie 0 en succès, 1 en échec.
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="BULLET-1 Backtesting Engine",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Exemples :\n"
            "  python engine.py\n"
            "  python engine.py --config config/my_config.json\n"
            "  python engine.py --config config/my_config.json "
            "--credentials config/credentials.json"
        ),
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Chemin vers config.json (défaut : config/config.json)",
    )
    parser.add_argument(
        "--credentials",
        type=str,
        default=None,
        help="Chemin vers credentials.json (défaut : config/credentials.json)",
    )

    args = parser.parse_args()

    engine = Engine(
        config_path=args.config,
        credentials_path=args.credentials,
    )

    try:
        engine.run()
        sys.exit(0)
    except (
        EngineConfigurationError,
        EnginePeriodCoherenceError,
        EngineDataError,
        RuntimeError,
    ):
        sys.exit(1)


if __name__ == "__main__":
    main()

# FIN DU MODULE
