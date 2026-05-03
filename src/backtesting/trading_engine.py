"""
BULLET-1 - Trading Engine
=========================

Orchestrateur principal du pipeline de trading BULLET-1.

Position dans l'architecture globale :
    ohlcv_data_engine  ──▶  trading_engine  ──▶  analytics_engine
         (données)          (orchestration)          (rapports)

Ce module est le chef d'orchestre : il ne calcule rien, il coordonne.
Chaque module délégué a une responsabilité unique et l'engine assure
la cohérence du flux entre eux.

Responsabilités principales :
1. Cycle de vie session  : create → run → end (via SessionManager)
2. Cycle de vie position : open → trailing → SL/TP → close (via PM + OS)
3. Signal → exécution   : Strategy → OrderSimulator → PositionManager
4. Gaps cross-modules   : pnl_net dans trade records, funding fees, ATR pré-calcul
5. Interface analytics  : EngineRunResult vers analytics_engine

Version: 2.8.2
Date: 2026-03-13
Author: FuegoDev
Mode: ✅ Backtest | ✅ Paper | ❌ Live (Live = sous-classe à implémenter)
Dépendances: strategy, session_manager, order_simulator, position_manager, logger, helpers
"""

from __future__ import annotations

# [v2.5.2 — FIX-TE-4] import threading supprimé — inutilisé.
# Le TE est mono-thread par design ; la thread-safety est délégée
# aux sous-modules (SessionManager, PositionManager, BulletLogger).
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Tuple
import pandas as pd
from pathlib import Path
import sys

#trouver la racine du projet 
# [v2.5.6 — FIX-PATH-6] Résolution racine projet : pattern direct unifié.
# Remplace find_project_root() locale dupliquée dans ~10 modules (DRY).
# Calcul en 1 ligne depuis __file__ — sans fonction nommée, sans import.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# BULLET-1 imports
from src.utils.logger import BulletLogger
from src.core.strategy import Strategy
from src.core.session_manager import SessionManager
from src.backtesting.order_simulator import OrderSimulator
from src.core.position_manager import PositionManager

# [v2.6.0 — FEAT-MC-1] Import MarketContextCapture.
# Optionnel au sens DI : si None, le trade_record est créé sans origine_signal.
# L'import est absolu pour respecter la convention BULLET-1 (src.*).
from src.ml.market_context import MarketContextCapture

# ============================================================================
# TYPES & ENUMS
# ============================================================================

class EngineState(Enum):
    """
    Machine d'état du TradingEngine.

    IDLE     : Aucune session active. En attente de create_session().
    RUNNING  : Session active, trading autorisé.
    PAUSED   : Session active, trading bloqué (limite session/journalière atteinte).
    STOPPED  : Engine arrêté définitivement suite à une erreur fatale. Nécessite réinstanciation.

    Transitions valides :
        IDLE → RUNNING       (create_session + begin_session)
        RUNNING → PAUSED     (limite atteinte)
        PAUSED → RUNNING     (minuit, nouvelle journée, limites réinitialisées)
        RUNNING|PAUSED → IDLE  (end_session normal — [FIX-TE-BUG3] était STOPPED)
        RUNNING|PAUSED → STOPPED  (end_session sur erreur fatale)
        STOPPED → (terminal)
    """
    IDLE    = auto()
    RUNNING = auto()
    PAUSED  = auto()
    STOPPED = auto()


class StepResult(Enum):
    """
    Résultat d'une itération step().

    Permet à l'appelant (ohlcv_data_engine) de comprendre ce qui s'est passé
    sans inspecter l'état interne de l'engine.
    """
    NO_SIGNAL          = auto()  # Pas de signal généré par la stratégie
    POSITION_OPENED    = auto()  # Nouvelle position ouverte
    POSITION_UPDATED   = auto()  # Trailing stop mis à jour, position maintenue
    POSITION_CLOSED_SL = auto()  # Position fermée sur Stop-Loss
    POSITION_CLOSED_TP = auto()  # Position fermée sur Take-Profit
    POSITION_CLOSED_SESSION = auto()  # Position fermée par fin de session
    TRADING_BLOCKED    = auto()  # Trading bloqué par limites (session ou journalières)
    SESSION_ENDED      = auto()  # Session terminée (tous motifs)
    DATA_EXHAUSTED     = auto()  # Données OHLCV épuisées


# ============================================================================
# CONFIGURATION
# ============================================================================

@dataclass
class EngineConfig:
    """
    Configuration comportementale du TradingEngine.

    Séparée de la config BULLET-1 principale pour respecter SRP :
    ces paramètres contrôlent le comportement de l'engine lui-même,
    pas les paramètres de trading.

    Attributes:
        min_candles_window:   Minimum de candles passées à Strategy.analyze().
                              Doit être >= signal_generator.volume_lookback.
        max_candles_window:   Maximum de candles en mémoire (performance backtest).
                              Évite l'accumulation mémoire sur longues périodes.
        accumulate_funding:   Si True, accumule les funding fees aux timestamps UTC
                              fixes (00:00, 08:00, 16:00) pendant la durée de vie
                              d'une position. Réaliste pour perpetual futures.
        close_position_on_session_end: Si True, force la fermeture de toute
                              position ouverte à la fin de session (au prix close
                              de la dernière candle). Recommandé = True pour
                              backtesting propre.
        log_every_n_candles:  Fréquence d'émission des logs de progression.
                              0 = désactivé.
    """
    min_candles_window: int = 50
    max_candles_window: int = 500
    accumulate_funding: bool = True
    close_position_on_session_end: bool = True
    log_every_n_candles: int = 100


# ============================================================================
# RÉSULTAT DE SESSION
# ============================================================================

@dataclass
class EngineRunResult:
    """
    Résultat complet d'une exécution run_session().

    Interface de sortie vers analytics_engine.
    Contient toutes les données nécessaires pour générer des rapports.

    Attributes:
        session_id:       Identifiant UUID unique de la session [v2.5.3]
        session_summary:  Résumé session (depuis SessionManager.end_session)
        trades:           Liste complète des trades fermés avec pnl_net
        closed_positions: Historique positions (depuis PositionManager)
        strategy_stats:   Statistiques de la stratégie (ratios, cache, etc.)
        simulator_stats:  Statistiques du simulateur (fees, slippage, etc.)
        step_results:     Comptage par type de StepResult sur la session
        errors:           Liste des erreurs non fatales survenues
        candles_processed: Nombre de candles traitées
    """
    session_id:         str                   # [v2.5.3] UUID unique — clé primaire analytics
    session_summary:    Dict[str, Any]
    trades:             List[Dict[str, Any]]
    closed_positions:   List[Dict[str, Any]]
    strategy_stats:     Dict[str, Any]
    simulator_stats:    Dict[str, Any]
    step_results:       Dict[str, int]
    errors:             List[str]
    candles_processed:  int = 0


# ============================================================================
# TRADING ENGINE
# ============================================================================

class TradingEngine:
    """
    Orchestrateur principal du pipeline de trading BULLET-1.

    Coordonne Strategy, SessionManager, OrderSimulator et PositionManager
    en respectant la séparation stricte des responsabilités de chaque module.

    Architecture décisionnelle :
        Strategy     → Génère l'ordre (quoi, où, avec quelle taille)
        OrderSimulator → Exécute l'ordre (prix réel avec slippage + fees)
        PositionManager → Gère la position ouverte (trailing, PnL)
        SessionManager  → Gère le capital et les limites de session

    L'engine ne prend AUCUNE décision de trading. Il orchestre.

    Flux par candle (step) :
        1. Vérifications de fin de session (limites, expiry, données)
        2. Si position ouverte :
              a. Update trailing stop (avec ATR pré-calculé)
              b. Check SL/TP hit → fermer si atteint
        3. Check transition minuit (limites journalières)
        4. Si trading autorisé et pas de position :
              a. Fenêtre candles → strategy.analyze()
              b. Si signal → order_simulator.execute_market_order()
              c. Si fill → position_manager.open_position()
              d. session_manager.add_trade()
        5. [FIX-TE-2] Accumulation funding fees si timestamps UTC traversés

    Gaps cross-modules corrigés :
        [FIX-TE-1] pnl_net mis à jour dans le trade_record de SessionManager
                   après chaque fermeture de position.
        [FIX-TE-2] Funding fees accumulées par l'engine aux timestamps fixes UTC.
        [FIX-TE-3] ATR pré-calculé une fois par candle et transmis au PM.

    Thread-safety :
        Cette classe est conçue pour un usage single-threaded par session.
        Chaque module délégué gère sa propre thread-safety interne.

    Examples:
        >>> engine = TradingEngine(
        ...     config=config,
        ...     strategy=strategy,
        ...     session_manager=session_mgr,
        ...     order_simulator=order_sim,
        ...     position_manager=position_mgr,
        ... )
        >>>
        >>> result = engine.run_session(
        ...     session_n=1,
        ...     start_date=datetime(2025, 1, 1, tzinfo=timezone.utc),
        ...     end_date=datetime(2025, 1, 31, tzinfo=timezone.utc),
        ...     candles=df_ohlcv,
        ... )
        >>>
        >>> # Résultat disponible pour analytics_engine
        >>> print(f"PnL: {result.session_summary['pnl']:.2f}")
        >>> print(f"Trades: {len(result.trades)}")
    """

    def __init__(
        self,
        config: dict,
        strategy: Strategy,
        session_manager: SessionManager,
        order_simulator: OrderSimulator,
        position_manager: PositionManager,
        engine_config: Optional[EngineConfig] = None,
        market_context: Optional[MarketContextCapture] = None,
    ):
        """
        Initialise le TradingEngine.

        Utilise l'injection de dépendances : toutes les instances sont créées
        à l'extérieur et passées ici. L'engine ne crée AUCUN sous-module.

        Args:
            config:           Configuration complète BULLET-1
            strategy:         Instance Strategy (signal_generator + risk_manager injectés)
            session_manager:  Instance SessionManager (source de vérité capital)
            order_simulator:  Instance OrderSimulator (exécution ordres)
            position_manager: Instance PositionManager (gestion trailing + PnL)
            engine_config:    Configuration comportementale de l'engine.
                              Si None, utilise les valeurs par défaut.
            market_context:   [v2.6.0] Instance MarketContextCapture (optionnelle).
                              Si fournie, capture l'état du marché à l'ouverture
                              de chaque position (origine_signal dans trade_record).
                              Si None, le trade_record est créé sans origine_signal
                              (rétro-compatible — comportement identique à v2.5.x).

        Raises:
            TypeError:  Si une dépendance est None ou du mauvais type.
            ValueError: Si config invalide ou mode non supporté.
        """
        # ── Validation dépendances ────────────────────────────────────────────
        self._validate_dependencies(
            config, strategy, session_manager, order_simulator, position_manager
        )

        # ── Attributs fondamentaux ────────────────────────────────────────────
        self.config           = config
        self.strategy         = strategy
        self.session_manager  = session_manager
        self.order_simulator  = order_simulator
        self.position_manager = position_manager
        self.engine_config    = engine_config or EngineConfig()
        self.logger           = BulletLogger()

        # [v2.6.0 — FEAT-MC-1] MarketContextCapture — optionnel par design DI.
        # None = comportement rétro-compatible (aucun origine_signal dans les trades).
        self.market_context: Optional[MarketContextCapture] = market_context

        # ── Mode opérationnel (normalisé) ────────────────────────────────────
        self.mode = config['general']['mode'].upper()

        # ── Machine d'état ───────────────────────────────────────────────────
        self._state = EngineState.IDLE

        # ── État interne de session ───────────────────────────────────────────
        # Référence au trade_record actif dans session_manager.current_session_trades
        # Permet la mise à jour pnl_net en place (FIX-TE-1).
        self._active_trade_record: Optional[Dict[str, Any]] = None

        # Position ID courante (une seule position simultanée — mono-asset)
        self._active_position_id: Optional[str] = None

        # [FIX-LAB-1] Ordre différé — signal généré au close de la candle N,
        # exécuté au open de la candle N+1 (prix de marché réellement accessible).
        self._pending_order: Optional[Dict[str, Any]] = None

        # Timestamp de la dernière candle traitée (déterminisme, anti-boucle)
        self._last_candle_timestamp: Optional[Any] = None

        # Funding fees : dernier timestamp de collecte par position
        self._last_funding_check: Optional[datetime] = None

        # Compteurs session pour EngineRunResult
        self._step_results: Dict[str, int] = {}
        self._session_errors: List[str] = []
        self._candles_processed: int = 0

        # [v2.5.2 — VIGILANCE-1] Flag anti-spam pour le warning ATR manquant.
        # _get_atr_value() est appelé à chaque candle avec position ouverte.
        # Sans ce flag, un trailing type='atr'/'hybrid' sans ATRIndicator injecté
        # produirait des milliers de warnings identiques sur un long backtest.
        # Le warning est émis UNE FOIS à l'init (ci-dessous) + une fois en session.
        self._warned_missing_atr: bool = False

        # ── Contrôle cohérence ATR à l'initialisation ────────────────────────
        trailing_type = (
            self.config
            .get('strategy', {})
            .get('trailing_stop', {})
            .get('type', 'candle')
        )
        if trailing_type in ('atr', 'hybrid') and position_manager.atr is None:
            self.logger.error(
                f"[VIGILANCE-1] TradingEngine init: trailing type='{trailing_type}' "
                "mais position_manager.atr est None. "
                "Le trailing stop sera INOPÉRANT sur toutes les sessions. "
                "Injectez une instance ATRIndicator dans PositionManager avant "
                "de lancer run_session()."
            )
            self._warned_missing_atr = True   # Pas de répétition en session

        self.logger.info(
            f"TradingEngine initialized "
            f"(mode={self.mode}, "
            f"trailing={trailing_type}, "
            f"window=[{self.engine_config.min_candles_window},"
            f"{self.engine_config.max_candles_window}], "
            f"funding={self.engine_config.accumulate_funding})"
        )

    # =========================================================================
    # API PUBLIQUE — POINT D'ENTRÉE PRINCIPAL
    # =========================================================================

    def run_session(
        self,
        session_n: int,
        start_date: datetime,
        end_date: datetime,
        candles: pd.DataFrame,
        warmup_candles: Optional[pd.DataFrame] = None,
    ) -> EngineRunResult:
        """
        Exécute une session de trading complète sur les données OHLCV.

        Flux complet :
            1. Créer la session (SessionManager)
            2. Itérer sur chaque candle via step()
            3. Fermer la position ouverte si nécessaire (close_position_on_session_end)
            4. Terminer la session (SessionManager)
            5. Retourner EngineRunResult vers analytics_engine

        Args:
            session_n:      Numéro de session (1-based, séquentiel)
            start_date:     Date de début (UTC-aware recommandé)
            end_date:       Date de fin (UTC-aware recommandé)
            candles:        DataFrame OHLCV pour la période de session réelle.
                            Colonnes requises : open, high, low, close, volume, timestamp
                            Les candles doivent être triées chronologiquement (asc).
            warmup_candles: [v2.6.1 — FEAT-WARMUP] Bougies de préchauffage optionnelles
                            (antérieures à start_date). Concaténées en tête de `candles`
                            AVANT la boucle step() pour pré-alimenter candles_window.
                            Elles ne passent JAMAIS dans step() : session management,
                            day transitions, signaux et positions — rien n'est déclenché
                            sur ces candles. Cela permet à MCCapture d'avoir une fenêtre
                            >= min_candles dès le premier trade de la session.

        Returns:
            EngineRunResult avec toutes les données de session.

        Raises:
            RuntimeError: Si engine en état STOPPED (non réutilisable).
            ValueError:   Si candles invalides ou session_n incohérent.

        Notes:
            - La session est créée ET terminée dans cette méthode.
            - En cas d'exception inattendue, end_session est appelé en finally
              pour garantir la propreté de l'état interne.
        """
        if self._state == EngineState.STOPPED:
            raise RuntimeError(
                "TradingEngine en état STOPPED. Créez une nouvelle instance."
            )

        # ── Validation données ────────────────────────────────────────────────
        self._validate_candles(candles)

        # ── [v2.6.1 — FEAT-WARMUP] Pré-alimentation de la fenêtre glissante ──
        # Les warmup_candles sont concaténées en tête du DataFrame d'itération.
        # L'offset warmup_offset marque la frontière : step() n'est appelé
        # qu'à partir de l'index warmup_offset (première candle de session réelle).
        # Les candles de warmup alimentent candles_window naturellement via
        # le calcul iloc[window_start : i + 1], sans jamais déclencher de logique
        # de trading, session ou day management.
        if warmup_candles is not None and not warmup_candles.empty:
            warmup_offset = len(warmup_candles)
            candles_full  = pd.concat(
                [warmup_candles, candles], ignore_index=True
            )
            self.logger.debug(
                f"[FEAT-WARMUP] {warmup_offset} bougies warmup pré-chargées "
                f"(skip step) | session réelle : {len(candles)} bougies"
            )
        else:
            warmup_offset = 0
            candles_full  = candles

        # ── Réinitialisation état session ─────────────────────────────────────
        self._reset_session_state()

        total_candles = len(candles)          # Compteur sur session réelle uniquement
        end_reason = 'completed'

        # ── Création session ──────────────────────────────────────────────────
        self.session_manager.create_session(session_n, start_date, end_date)
        self._state = EngineState.RUNNING

        self.logger.log_separator('INFO', '=', 80)
        self.logger.info(
            f"TradingEngine — Session {session_n} démarrée "
            f"({total_candles} candles{f' + {warmup_offset} warmup' if warmup_offset else ''})"
        )

        try:
            # ── Boucle principale candle-par-candle ───────────────────────────
            for i, (_, row) in enumerate(candles_full.iterrows()):

                # [FEAT-WARMUP] Skip silencieux des candles de préchauffage.
                # Elles sont dans candles_full pour alimenter candles_window
                # mais ne doivent jamais déclencher step() (session management,
                # signaux, positions). current_candle_index est recalé sur la
                # session réelle (i - warmup_offset) pour check_data_exhaustion.
                if i < warmup_offset:
                    continue

                current_candle = row.to_dict()

                # ── [v2.8.2 — FIX-LAB-2] Deux fenêtres distinctes selon l'usage ──
                #
                # AVANT (lookahead bias) :
                #   candles_window = candles_full.iloc[window_start : i + 1]
                #   → iloc[-1] = candle i (close "futur" connu à l'avance)
                #   → strategy.analyze() recevait la bougie en cours de formation
                #
                # APRÈS (correct) :
                #   candles_for_signal = candles_full.iloc[window_start : i]
                #   → iloc[-1] = candle i-1 (dernière bougie FERMÉE)
                #   → strategy.analyze() ne voit jamais le close de la bougie courante
                #
                #   candles_for_exec = candles_full.iloc[window_start : i + 1]
                #   → inclut la bougie i pour les calculs de contexte/ATR post-signal
                #   → utilisé par _execute_pending_order() et _check_sl_tp()
                #
                # current_candle (la bougie i) est passé séparément à step() pour :
                #   - l'exécution du pending_order au open (FIX-LAB-1, déjà correct)
                #   - le check SL/TP sur high/low de la bougie courante
                #   - les guards de session (timestamp, expiry)
                # Il n'est JAMAIS inclus dans la fenêtre passée à strategy.analyze().
                window_start       = max(0, i - self.engine_config.max_candles_window + 1)
                candles_for_signal = candles_full.iloc[window_start : i]          # exclut i ← FIX-LAB-2
                candles_for_exec   = candles_full.iloc[window_start : i + 1]      # inclut i (ATR, context)

                # Index dans la session réelle (pour check_data_exhaustion)
                session_candle_index = i - warmup_offset

                result, end_reason = self.step(
                    candles_for_signal=candles_for_signal,
                    candles_for_exec=candles_for_exec,
                    current_candle_index=session_candle_index,
                    total_candles=total_candles,
                    current_candle=current_candle,
                )

                self._increment_step_result(result)
                self._candles_processed += 1

                # Log progression
                self._maybe_log_progress(i, total_candles)

                # Fin de session détectée par step()
                if result in (StepResult.SESSION_ENDED, StepResult.DATA_EXHAUSTED):
                    break

            # ── Fermeture position ouverte à fin de session ───────────────────
            if (self.engine_config.close_position_on_session_end
                    and self._active_position_id is not None):
                last_candle = candles.iloc[-1].to_dict()
                self._force_close_position(
                    exit_price=last_candle['close'],
                    exit_time=self._ensure_utc_datetime(last_candle.get('timestamp')),
                    candles_data=candles,
                )
                end_reason = 'session_end_force_close'

        except Exception as exc:
            # Erreur inattendue — on tente de terminer proprement
            error_msg = f"Erreur inattendue en session {session_n}: {exc}"
            self.logger.exception(error_msg)
            self._session_errors.append(error_msg)
            end_reason = 'error'

        finally:
            # ── Fin de session ────────────────────────────────────────────────
            # Toujours exécuté — garantit la cohérence de SessionManager.
            # [FIX-TE-BUG1] _finalize_session retourne (summary, trades_snapshot).
            # Les trades sont capturés AVANT end_session() pour éviter le clear().
            # [FIX-TE-BUG2] capital_snapshot capturé AVANT end_session() pour
            # éviter RuntimeError sur get_capital_total() post-fermeture session.
            session_summary, trades_snapshot, capital_snapshot = self._finalize_session(end_reason)

            # [v2.5.6 — FIX-TE-7] Garantie de retour à IDLE après toute session.
            # _finalize_session() met _state = IDLE dans son chemin normal.
            # Ce guard couvre le cas exceptionnel où _finalize_session() échoue
            # AVANT d'atteindre la ligne self._state = EngineState.IDLE,
            # laissant l'engine dans un état RUNNING ou PAUSED qui bloquerait
            # la session suivante (run_session() vérifierait STOPPED uniquement,
            # mais step() exigerait RUNNING/PAUSED — état incohérent).
            if self._state not in (EngineState.IDLE, EngineState.STOPPED):
                self.logger.warning(
                    f"[FIX-TE-7] État engine non-IDLE après fin de session "
                    f"({self._state.name}) — forcé à IDLE."
                )
                self._state = EngineState.IDLE

        return self._build_run_result(session_summary, trades_snapshot, capital_snapshot)

    def step(
        self,
        candles_for_signal: pd.DataFrame,
        current_candle_index: int,
        total_candles: int,
        current_candle: Optional[Dict[str, Any]] = None,
        candles_for_exec: Optional[pd.DataFrame] = None,
    ) -> Tuple[StepResult, str]:
        """
        Traite une candle unique dans le contexte de la session active.

        Méthode centrale : peut être appelée directement par ohlcv_data_engine
        pour un contrôle fin du pipeline (tests, mode pas-à-pas).

        [v2.8.2 — FIX-LAB-2] Deux fenêtres distinctes selon l'usage :
            candles_for_signal : fenêtre SANS la bougie courante (i exclus).
                                 Passée à strategy.analyze() — son iloc[-1]
                                 est la dernière bougie FERMÉE.
            candles_for_exec   : fenêtre AVEC la bougie courante (i inclus).
                                 Utilisée pour ATR, context, SL/TP check.
                                 Si None : identique à candles_for_signal
                                 (rétrocompatibilité appels directs).

        Ordre d'évaluation (priorité décroissante) :
            1. Data exhaustion → SESSION_ENDED immédiat
            2. Session expiry → end si position fermée, grace period si ouverte
            2.5 Exécution ordre différé au open (FIX-LAB-1) → si pending_order
            3. Si position ouverte → trailing + SL/TP check
            4. Limites session/journalières → PAUSED ou SESSION_ENDED
            5. Si trading autorisé → signal → stocké comme pending_order
            6. Midnight transition (journalières uniquement)
            7. Funding fees (si position ouverte)

        Args:
            candles_window:       Fenêtre glissante de candles (max_candles_window)
            current_candle_index: Index 0-based dans le DataFrame complet
            total_candles:        Nombre total de candles dans la session
            current_candle:       Candle courante (dict). Si None, utilise la
                                  dernière ligne de candles_window.

        Returns:
            Tuple[StepResult, str] : (résultat, raison en string)

        Raises:
            RuntimeError: Si appelé sans session active (state != RUNNING/PAUSED).
        """
        if self._state not in (EngineState.RUNNING, EngineState.PAUSED):
            raise RuntimeError(
                f"step() appelé en état {self._state.name}. "
                "Une session active est requise."
            )

        # [v2.8.2 — FIX-LAB-2] Résolution des deux fenêtres.
        # candles_for_exec = candles_for_signal si non fourni (rétrocompatibilité).
        if candles_for_exec is None:
            candles_for_exec = candles_for_signal

        # Candle courante (dernière de la fenêtre d'EXÉCUTION si non fournie)
        if current_candle is None:
            current_candle = candles_for_exec.iloc[-1].to_dict()

        candle_ts = current_candle.get('timestamp')

        # ── 1. Data exhaustion ────────────────────────────────────────────────
        exhausted, exhausted_reason = self.session_manager.check_data_exhaustion(
            current_candle_index, total_candles
        )
        if exhausted:
            return StepResult.DATA_EXHAUSTED, exhausted_reason

        # ── 2. Session expiry ─────────────────────────────────────────────────
        current_time = self._ensure_utc_datetime(candle_ts)
        has_position = self._active_position_id is not None
        position_entry_time = None

        if has_position:
            pos = self.position_manager.get_position(self._active_position_id)
            position_entry_time = pos.get('entry_time') if pos else None

        expired, expiry_reason = self.session_manager.check_session_expiry(
            current_time,
            has_open_position=has_position,
            position_entry_time=position_entry_time,
        )

        if expired and expiry_reason == 'force_close_position' and has_position:
            pos = self.position_manager.get_position(self._active_position_id)
            if pos:
                self._force_close_position(
                    exit_price=current_candle['close'],
                    exit_time=current_time,
                    candles_data=candles_for_exec,       # [FIX-LAB-2] fenêtre exécution
                )
            return StepResult.SESSION_ENDED, 'force_close_session_expired'

        if expired and expiry_reason == 'session_expired':
            return StepResult.SESSION_ENDED, expiry_reason

        # ── 3. Gestion position ouverte ───────────────────────────────────────
        if has_position:
            # 3a. Trailing stop update (ATR pré-calculé depuis fenêtre exécution)
            atr_value = self._get_atr_value(candles_for_exec, current_candle)  # [FIX-LAB-2]
            self._update_trailing_stop(current_candle, atr_value)

            # 3b. Check SL/TP
            sl_tp_result = self._check_sl_tp(
                current_candle=current_candle,
                candles_window=candles_for_exec,         # [FIX-LAB-2] fenêtre exécution
                current_time=current_time,
            )
            if sl_tp_result is not None:
                return sl_tp_result

            # 3c. Funding fees (FIX-TE-2)
            if self.engine_config.accumulate_funding:
                self._accumulate_funding_fees(current_time)

            # [VIGILANCE-2] Note d'architecture — midnight transition avec position ouverte :
            # check_midnight_transition() est appelé au bloc 6 (sans position) ou ici
            # implicitement via le step suivant (la position est maintenue, on revient
            # sur ce bloc à la candle suivante). La transition de minuit ne débloque
            # le trading que lorsqu'il n'y a plus de position, ce qui est intentionnel :
            # la position en cours reste ouverte sans interruption, et les nouvelles
            # ouvertures seront ré-évaluées à la prochaine candle sans position.
            return StepResult.POSITION_UPDATED, 'trailing_updated'

        # ── 4. Limites session / journalières ─────────────────────────────────
        # [VIGILANCE-3] has_open_position=False est intentionnel ici :
        # Ce bloc n'est atteint que lorsqu'aucune position n'est ouverte (le bloc
        # has_position ci-dessus a renvoyé en return). Passer False est donc
        # sémantiquement correct et permet à SessionManager de déclencher la
        # Priority 1 (max_loss critical) sans calcul d'impact SL projeté, puisque
        # aucune position ne peut aggraver la perte.
        should_end, limit_reason = self.session_manager.should_end_session(
            has_open_position=False,
        )

        if should_end:
            # [FIX-TE-8] Purge du pending_order si les limites mettent fin à la session.
            # Sans cette purge, l'ordre différé resterait en mémoire et serait tenté
            # lors d'un éventuel appel step() suivant, alors que la session est terminée.
            if self._pending_order is not None:
                self.logger.debug(
                    "[FIX-TE-8] Ordre différé annulé : limite session atteinte avant exécution."
                )
                self._pending_order = None
            return StepResult.SESSION_ENDED, limit_reason or 'limits_reached'

        if self._state == EngineState.PAUSED:
            # Midnight transition (peut débloquer le trading)
            transitioned, _ = self.session_manager.check_midnight_transition(
                current_time, has_open_position=False
            )
            if transitioned and self.session_manager.is_trading_allowed():
                self._state = EngineState.RUNNING
            return StepResult.TRADING_BLOCKED, 'session_paused'

        if not self.session_manager.is_trading_allowed():
            self._state = EngineState.PAUSED
            return StepResult.TRADING_BLOCKED, 'trading_not_allowed'

        # ── 4.5 Exécution ordre différé au open de la candle courante ─────────
        # [FIX-LAB-1] Le signal a été généré au close de la candle précédente et
        # stocké dans _pending_order. On exécute maintenant au prix open de cette
        # candle — premier prix de marché réellement accessible après le signal.
        #
        # [FIX-TE-8] Déplacé ICI (après bloc 4) pour garantir que les limites
        # session/journalières sont vérifiées AVANT toute ouverture de position.
        # L'ancienne position (bloc 2.5) permettait d'ouvrir une position même
        # quand le SessionManager aurait refusé le trading à ce step.
        #
        # Cas défensif : si une position est déjà ouverte alors qu'un ordre est
        # en attente (état théoriquement impossible en mono-position), l'ordre est
        # annulé proprement pour éviter toute incohérence d'état.
        if self._pending_order is not None:
            if has_position:
                # Incohérence — on purge l'ordre différé sans l'exécuter.
                self.logger.warning(
                    "[FIX-LAB-1] Ordre différé annulé : position déjà active "
                    f"({self._active_position_id}). État nettoyé."
                )
                self._pending_order = None
            else:
                pending = self._pending_order
                self._pending_order = None   # one-shot : cleared avant tentative d'exécution
                exec_result = self._execute_pending_order(
                    order=pending,
                    current_candle=current_candle,
                    candles_window=candles_for_exec,     # [FIX-LAB-2] fenêtre exécution
                    current_time=current_time,
                )
                if exec_result[0] == StepResult.POSITION_OPENED:
                    return exec_result
                # Échec (capital insuffisant, erreur PM...) : log déjà émis dans
                # _execute_pending_order, on continue le step normalement.

        # ── 5. Génération signal et ouverture position ────────────────────────
        # [FIX-LAB-2] candles_for_signal (exclut i) est passé à _try_open_position
        # qui le transmet à strategy.analyze(). candles_for_exec (inclut i) est
        # réservé aux calculs post-signal (ATR, context, exécution).
        if len(candles_for_signal) < self.engine_config.min_candles_window:
            # Pas assez de données pour la stratégie — on attend
            return StepResult.NO_SIGNAL, 'insufficient_candles'

        available_balance = self.session_manager.get_capital_available()
        open_result = self._try_open_position(
            candles_window=candles_for_signal,           # [FIX-LAB-2] signal uniquement
            current_candle=current_candle,
            current_time=current_time,
            available_balance=available_balance,
        )

        # ── 6. Midnight transition ────────────────────────────────────────────
        self.session_manager.check_midnight_transition(
            current_time, has_open_position=(self._active_position_id is not None)
        )

        return open_result

    # =========================================================================
    # MÉTHODES PRIVÉES — OUVERTURE POSITION
    # =========================================================================

    def _try_open_position(
        self,
        candles_window: pd.DataFrame,
        current_candle: Dict[str, Any],
        current_time: datetime,
        available_balance: float,
    ) -> Tuple[StepResult, str]:
        """
        Tente d'ouvrir une position si la stratégie génère un signal.

        Flow :
            1. strategy.analyze() → order ou None
            2. order_simulator.execute_market_order() → fill
            3. position_manager.open_position() → position_id
            4. session_manager.add_trade() → enregistrement initial
            5. Stocker référence trade_record (pour FIX-TE-1)

        Args:
            candles_window:    Fenêtre candles pour Strategy.analyze()
            current_candle:    Candle courante (dict)
            current_time:      Timestamp UTC de la candle
            available_balance: Capital disponible (depuis SessionManager)

        Returns:
            (StepResult.POSITION_OPENED, reason) si position ouverte
            (StepResult.NO_SIGNAL, reason) sinon
        """
        # ── Analyse stratégie ─────────────────────────────────────────────────
        try:
            prev_candle = (
                candles_window.iloc[-2].to_dict()
                if len(candles_window) >= 2
                else None
            )

            order = self.strategy.analyze(
                candles=candles_window,
                current_balance=available_balance,
                prev_candle=prev_candle,
                force_recalculate=False,
            )
        except Exception as exc:
            msg = f"Strategy.analyze() a échoué: {exc}"
            self.logger.error(msg, exc_info=True)
            self._session_errors.append(msg)
            return StepResult.NO_SIGNAL, 'strategy_error'

        if order is None:
            return StepResult.NO_SIGNAL, 'no_signal'

        # ── [FIX-LAB-1] Différer l'exécution au open de la candle suivante ───
        # Le signal est confirmé sur le close de la candle courante. En réalité,
        # ce prix n'est plus accessible à l'instant où le signal est détecté.
        # On stocke l'ordre et on exécutera au open de la prochaine candle dans
        # le bloc 2.5 de step(), via _execute_pending_order().
        self._pending_order = order
        self.logger.debug(
            f"[FIX-LAB-1] Signal différé : {order['direction']} @ signal_price="
            f"{order['entry_price']:.2f} → exécution au open de la prochaine candle."
        )
        return StepResult.NO_SIGNAL, 'signal_deferred'

    def _execute_pending_order(
        self,
        order: Dict[str, Any],
        current_candle: Dict[str, Any],
        candles_window: pd.DataFrame,
        current_time: datetime,
    ) -> Tuple[StepResult, str]:
        """
        Exécute un ordre différé au prix open de la candle courante.

        [FIX-LAB-1] Appelé depuis step() bloc 2.5. L'ordre a été généré par
        strategy.analyze() à la candle précédente (close) et stocké dans
        _pending_order. On exécute maintenant au open de cette candle — premier
        prix de marché réellement accessible post-signal.

        Flow identique à l'ancienne 2ème moitié de _try_open_position(),
        avec current_price=current_candle['open'] au lieu de ['close'].

        Args:
            order:          Ordre stocké depuis la candle précédente
            current_candle: Candle courante (contient le prix open d'exécution)
            candles_window: Fenêtre courante (inclut la candle d'exécution — ATR à jour)
            current_time:   Timestamp UTC de la candle d'exécution

        Returns:
            (StepResult.POSITION_OPENED, reason) si succès
            (StepResult.NO_SIGNAL, reason) si échec (non fatal)
        """
        # ── Exécution ordre MARKET au open de la candle courante ─────────────
        try:
            fill = self.order_simulator.execute_market_order(
                order=order,
                current_price=current_candle['open'],    # [FIX-LAB-1] open, pas close
                current_candle=current_candle,
                historical_data=candles_window,
            )
        except RuntimeError as exc:
            # Capital insuffisant — non fatal
            self.logger.warning(f"[FIX-LAB-1] Ordre différé rejeté (capital): {exc}")
            return StepResult.NO_SIGNAL, 'insufficient_capital'
        except Exception as exc:
            msg = f"[FIX-LAB-1] execute_market_order() a échoué sur ordre différé: {exc}"
            self.logger.error(msg, exc_info=True)
            self._session_errors.append(msg)
            return StepResult.NO_SIGNAL, 'order_error'

        # ── Ouverture position ────────────────────────────────────────────────
        position_data = self._build_position_data(order, fill, current_time)

        try:
            position_id = self.position_manager.open_position(position_data)
        except Exception as exc:
            msg = f"[FIX-LAB-1] PositionManager.open_position() a échoué: {exc}"
            self.logger.error(msg, exc_info=True)
            self._session_errors.append(msg)
            try:
                self.session_manager.release_margin(order['collateral'])
                self.session_manager.update_balance(fill['entry_fees'])
            except Exception as release_exc:
                self.logger.critical(
                    f"Impossible de libérer la marge après échec PM: {release_exc}"
                )
            return StepResult.NO_SIGNAL, 'position_open_error'

        self._active_position_id = position_id

        # ── Enregistrement dans SessionManager ───────────────────────────────
        # [FIX-TE-1] Référence mutable au trade_record pour mise à jour post-close.
        # [v2.7.0 — FIX-TE-2] SL/TP recalculés sur fill_price (prix réel).
        _sl_tp_recalc = position_data.pop('_sl_tp_recalculated', None)
        _sl_recalc    = _sl_tp_recalc['sl_price'] if _sl_tp_recalc else order['stop_loss']
        _tp_recalc    = _sl_tp_recalc['tp_price'] if _sl_tp_recalc else order['take_profit']

        trade_record = {
            'position_id': position_id,
            'trading_pair':   self.config.get('general', {}).get('trading_pair', ''),
            'timeframe':      self.config.get('general', {}).get('timeframe', ''),
            'direction':   order['direction'],
            'entry_price': fill['fill_price'],
            'size':        fill['filled_size'],
            # [v2.7.0 — FIX-TE-2] SL/TP recalculés sur fill_price (prix réel post-slippage)
            'stop_loss':   _sl_recalc,
            'take_profit': _tp_recalc,
            'collateral':  order['collateral'],
            'notional':    order['notional'],
            'leverage':    order['leverage'],
            'entry_fees':  fill['entry_fees'],
            'entry_time':  current_time,
            'quality_score': order.get('quality_score', 0),
            'configuration_name': order.get('configuration_name', ''),
            # [FIX-TE-PCT] capital_before : capital total au moment de l'ouverture.
            # Base correcte pour pnl_pct et pour metrics._extract_returns_pct().
            # Distinct de 'collateral' (fraction du capital, gonflait pnl_pct × levier).
            'capital_before': round(self.session_manager.get_capital_total(), 4),
            # Champs mis à jour à la fermeture (FIX-TE-1)
            'exit_price':  None,
            'exit_time':   None,
            'exit_reason': None,
            'exit_fees':   None,
            'pnl_gross':   None,
            'pnl_net':     None,
            'pnl_pct':     None,
            'is_winner':   None,
        }

        try:
            self.session_manager.add_trade(trade_record)
        except RuntimeError as exc:
            self.logger.warning(f"add_trade() bloqué: {exc}")

        self._active_trade_record = trade_record

        # [v2.6.0 — FEAT-MC-1] Capture contexte marché (non fatale si échec).
        # La fenêtre passée est celle de la candle d'exécution (open), ce qui
        # est plus précis que la fenêtre du signal (close candle précédente).
        if self.market_context is not None:
            try:
                origine_signal = self.market_context.capture(
                    candles_window=candles_window,
                    entry_time=current_time,
                )
                if origine_signal is not None:
                    trade_record['origine_signal'] = origine_signal
                    self.logger.debug(
                        f"[FEAT-MC-1] origine_signal capturé pour {position_id} "
                        f"({len(str(origine_signal))} chars)"
                    )
                else:
                    self.logger.debug(
                        f"[FEAT-MC-1] capture() → None pour {position_id} "
                        f"(fenêtre insuffisante ou indicateurs indisponibles)"
                    )
            except Exception as mc_exc:
                self.logger.error(
                    f"[FEAT-MC-1] Erreur dans market_context.capture() pour "
                    f"{position_id} (non-fatal) : {type(mc_exc).__name__}: {mc_exc}",
                    exc_info=True,
                )

        self._last_funding_check = current_time

        self.logger.log_trade_open(
            trade_id=position_id,
            side=order['direction'],
            entry_price=fill['fill_price'],
            size=fill['filled_size'],
            sl_price=_sl_recalc,
            tp_price=_tp_recalc,
        )

        return StepResult.POSITION_OPENED, 'position_opened'

    def _build_position_data(
        self,
        order: Dict[str, Any],
        fill: Dict[str, Any],
        entry_time: datetime,
    ) -> Dict[str, Any]:
        """
        Construit le dict position_data attendu par PositionManager.open_position().

        Fusionne les données de l'ordre (stratégie) avec les données du fill
        (prix réels post-slippage, fees réels).

        [v2.7.0 — FIX-TE-2] SL et TP recalculés sur fill['fill_price'] (prix réel
        post-slippage) et non sur order['stop_loss']/order['take_profit'] (calculés
        par risk_manager sur signal['entry_price'] théorique).
        Avec slippage > 0, fill_price ≠ entry_price → SL/TP décalés par rapport
        au prix d'entrée réel → risk/reward effectif faussé.
        Exemple : LONG, entry théorique=100 000, fill=100 100 (+0.1% slippage),
        SL=98 000 (théorique). Avant fix : risk effectif=2 100 au lieu de 2 000.
        Après fix : SL et TP repositionnés symétriquement autour de fill_price,
        RR effectif = RR configuré, comportement conforme aux attentes du backtest.

        Args:
            order:      Ordre généré par Strategy
            fill:       Fill retourné par OrderSimulator
            entry_time: Timestamp UTC de l'entrée

        Returns:
            Dict complet conforme à l'interface PositionManager.open_position()
        """
        # [v2.7.0 — FIX-TE-2] Recalcul SL/TP sur le prix réel d'exécution.
        # order['stop_loss'] et order['take_profit'] sont issus de
        # risk_manager.compute_position(entry_price=signal['entry_price']).
        # Le slippage appliqué dans execute_market_order() déplace le prix
        # d'entrée réel → on recalcule ici avec le fill_price pour garantir
        # que le RR effectif correspond exactement au RR configuré.
        sl_tp = self.strategy.risk_manager.calculate_sl_tp(
            side=order['direction'],
            entry_price=fill['fill_price'],
        )

        return {
            # Prix réels d'exécution (post-slippage)
            'entry_price': fill['fill_price'],
            'entry_time':  entry_time,

            # Paramètres position
            'direction':   order['direction'],
            'size':        order['size'],
            # [v2.7.0 — FIX-TE-2] SL/TP recalculés sur fill_price (prix réel)
            'stop_loss':   sl_tp['sl_price'],
            'take_profit': sl_tp['tp_price'],
            'collateral':  order['collateral'],
            'leverage':    order['leverage'],
            'notional':    order['notional'],

            # Frais d'entrée réels (depuis le fill)
            # entry_fees REQUIS par PositionManager.open_position()
            'entry_fees': fill['entry_fees'],

            # Méta
            'symbol':    order.get('symbol', ''),
            'order_id':  fill.get('order_id', ''),

            # [v2.7.0 — FIX-TE-2] SL/TP recalculés exposés pour réutilisation
            # dans trade_record (_try_open_position) sans double appel RiskManager.
            '_sl_tp_recalculated': sl_tp,
        }

    # =========================================================================
    # MÉTHODES PRIVÉES — SL/TP CHECK & FERMETURE
    # =========================================================================

    def _check_sl_tp(
        self,
        current_candle: Dict[str, Any],
        candles_window: pd.DataFrame,
        current_time: datetime,
    ) -> Optional[Tuple[StepResult, str]]:
        """
        Vérifie si le SL ou TP de la position active a été atteint.

        Délègue la logique de détection à OrderSimulator.check_sl_tp()
        et la fermeture à _close_position().

        Args:
            current_candle:  Candle courante avec high/low
            candles_window:  Fenêtre historique pour calcul ATR du slippage SL
            current_time:    Timestamp UTC courant

        Returns:
            (StepResult, reason) si position fermée
            None si position maintenue
        """
        if self._active_position_id is None:
            return None

        position = self.position_manager.get_position(self._active_position_id)
        if position is None:
            # Incohérence d'état — nettoyer
            self.logger.error(
                f"Position {self._active_position_id} introuvable dans PM. "
                "Réinitialisation état engine."
            )
            self._active_position_id = None
            self._active_trade_record = None
            return None

        # Délégation détection + exécution SL/TP à OrderSimulator
        try:
            close_fill = self.order_simulator.check_sl_tp(
                position=position,
                current_candle=current_candle,
                historical_data=candles_window,
            )
        except Exception as exc:
            msg = f"OrderSimulator.check_sl_tp() a échoué: {exc}"
            self.logger.error(msg, exc_info=True)
            self._session_errors.append(msg)
            return None

        if close_fill is None:
            return None  # SL/TP non atteint sur cette candle

        # ── Déterminer reason et prix de fermeture ────────────────────────────
        is_sl = close_fill.get('exit_type') == 'STOP_LIMIT'
        close_reason = 'SL' if is_sl else 'TP'
        exit_price = close_fill['fill_price']

        # [v2.5.2 — FIX-TE-BUG2] Sauvegarder position_id AVANT _close_position().
        # _close_position() exécute self._active_position_id = None (nettoyage état),
        # ce qui rendait l'expression `self._active_position_id or 'unknown'`
        # systématiquement égale à 'unknown' dans log_trade_close ci-dessous.
        # Tous les logs de fermeture perdaient leur identifiant de trade.
        closed_position_id = self._active_position_id  # snapshot avant reset

        # ── Fermer la position dans PositionManager ───────────────────────────
        # settle_trade() a déjà été appelé dans execute_limit_order()
        # On ferme dans PM pour mettre à jour l'état interne (pnl, fees, etc.)
        self._close_position(
            close_fill=close_fill,
            close_reason=close_reason,
            exit_price=exit_price,
            exit_time=current_time,
        )

        step_result = (
            StepResult.POSITION_CLOSED_SL if is_sl
            else StepResult.POSITION_CLOSED_TP
        )

        # [FIX-TE-PCT] pnl_pct log : utilise capital_before si disponible,
        # sinon collateral comme fallback défensif.
        _log_capital_ref = (
            self._active_trade_record.get('capital_before')
            if self._active_trade_record is not None
            else None
        ) or position.get('collateral', 1.0)
        self.logger.log_trade_close(
            trade_id=closed_position_id or 'unknown',  # [FIX-TE-BUG2] snapshot valide
            exit_price=exit_price,
            pnl=close_fill.get('pnl_net', 0.0),
            pnl_pct=close_fill.get('pnl_net', 0.0) / _log_capital_ref * 100,
            reason=close_reason,
        )

        return step_result, close_reason

    def _close_position(
        self,
        close_fill: Dict[str, Any],
        close_reason: str,
        exit_price: float,
        exit_time: datetime,
    ) -> None:
        """
        Ferme la position active dans PositionManager et met à jour le trade record.

        [FIX-TE-1] Met à jour pnl_net, exit_price, exit_fees dans le trade_record
        de SessionManager. Sans cette mise à jour, end_session() calcule
        winning_trades=0 et win_rate=0% pour toutes les sessions.

        Args:
            close_fill:   Fill de fermeture (depuis OrderSimulator)
            close_reason: 'SL', 'TP', 'manual', 'session_end'
            exit_price:   Prix de fermeture réel
            exit_time:    Timestamp UTC de fermeture
        """
        if self._active_position_id is None:
            return

        position_id = self._active_position_id

        # ── Fermeture dans PositionManager ────────────────────────────────────
        try:
            closed_position = self.position_manager.close_position(
                position_id=position_id,
                exit_price=exit_price,
                exit_time=exit_time,
                reason=close_reason,
                exit_fees=close_fill.get('exit_fees', close_fill.get('fees', 0.0)),
                funding_fees=self.position_manager.positions.get(
                    position_id, {}
                ).get('funding_fees', 0.0),
            )
        except Exception as exc:
            self.logger.error(
                f"PositionManager.close_position() a échoué pour {position_id}: {exc}",
                exc_info=True,
            )
            closed_position = None

        # ── [FIX-TE-1] Mise à jour trade_record dans SessionManager ──────────
        # Le trade_record est le même objet dict que celui stocké dans
        # session_manager.current_session_trades (référence mutable).
        # En le mettant à jour ici, end_session() verra pnl_net correct.
        if self._active_trade_record is not None:
            pnl_net   = close_fill.get('pnl_net', 0.0)
            pnl_gross = close_fill.get('pnl_gross', 0.0)
            exit_fees = close_fill.get('exit_fees', close_fill.get('fees', 0.0))
            collateral = self._active_trade_record.get('collateral', 1.0)

            # ── [v2.5.5] Trailing history — sérialisation propre pour JSON ───
            # closed_position contient trailing_history (List[Dict]) peuplé par
            # PositionManager._record_trailing_update() à chaque tick de mise à
            # jour du SL dynamique. Les timestamps datetime sont convertis en
            # ISO string ici pour garantir la sérialisabilité JSON en aval
            # (session_manager._save_trades_to_disk).
            trailing_history_serializable = []
            if closed_position:
                for entry in closed_position.get('trailing_history', []):
                    entry_copy = dict(entry)
                    ts = entry_copy.get('timestamp')
                    if isinstance(ts, datetime):
                        entry_copy['timestamp'] = ts.strftime('%Y-%m-%d %H:%M:%S')
                    trailing_history_serializable.append(entry_copy)

            self._active_trade_record.update({
                'exit_price':          round(exit_price, 2),
                'exit_time':           exit_time,
                'exit_reason':         close_reason,
                'exit_fees':           round(exit_fees, 4),
                'pnl_gross':           round(pnl_gross, 4),
                'pnl_net':             round(pnl_net, 4),
                # [FIX-TE-PCT] pnl_pct sur capital_before (capital total à l'ouverture).
                # Avant (bug) : pnl_net / collateral → résultat gonflé par le levier
                #               (collateral = 10% du capital → pnl_pct × 10).
                # Après (fix) : pnl_net / capital_before → return réel sur capital engagé.
                # Alimente metrics._extract_returns_pct() via 'capital_before' (priorité 2)
                # ou via 'return_pct' injecté par metrics.add_trade() (priorité 1).
                'pnl_pct':             round(
                    (pnl_net / self._active_trade_record.get('capital_before', collateral)) * 100, 4
                ) if collateral else 0.0,
                # [v2.6.0 — FEAT-MC-1] is_winner : vrai si le trade est profitable net.
                # Calculé à la fermeture uniquement (pnl_net inconnu à l'ouverture).
                # Critère : pnl_net > 0 (après tous les frais : entry, exit, funding).
                'is_winner':           pnl_net > 0,
                # [v2.5.5 — FEAT] funding_fees_total : frais de financement cumulés
                # sur la durée de la position (débités aux timestamps 00h/08h/16h UTC).
                # Déjà intégré dans pnl_net via position_manager.close_position(),
                # exposé ici pour transparence et pour calculate_total_fees() dans metrics.
                # 0.0 si la position n'a traversé aucun timestamp de funding.
                'funding_fees':        round(
                    closed_position.get('funding_fees_total', 0.0)
                    if closed_position else 0.0,
                    4
                ),
                # ── Trailing stop tracking ─────────────────────────────────────
                # trailing_history : parcours complet du SL dynamique depuis
                #   l'ouverture jusqu'à la clôture (chaque tick de mise à jour).
                #   Chaque entrée : {timestamp, old_sl, new_sl, update_type,
                #                    volatility_state, profit_ratio, current_price}
                # trailing_mode_final : mode actif au moment de la clôture.
                #   En mode hybrid, reflète le sous-mode effectif au moment du SL/TP.
                'trailing_history':    trailing_history_serializable,
                'trailing_mode_final': (
                    closed_position.get('current_trailing_mode', 'unknown')
                    if closed_position else 'unknown'
                ),
            })

        # ── Reset état interne ────────────────────────────────────────────────
        self._active_position_id  = None
        self._active_trade_record = None
        self._last_funding_check  = None

        self.logger.debug(f"Position {position_id} fermée ({close_reason})")

    def _force_close_position(
        self,
        exit_price: float,
        exit_time: datetime,
        candles_data: pd.DataFrame,
    ) -> None:
        """
        Force la fermeture de la position active (fin de session, grace period expirée).

        Calcule les fees taker (fermeture forcée = market order)
        et appelle settle_trade() manuellement car on contourne OrderSimulator.

        Args:
            exit_price:   Prix de fermeture forcée (close de la dernière candle)
            exit_time:    Timestamp UTC de fermeture
            candles_data: Données OHLCV pour le calcul des funding fees finaux
        """
        if self._active_position_id is None:
            return

        position_id = self._active_position_id
        position = self.position_manager.get_position(position_id)

        if position is None:
            self.logger.warning(
                f"Force close: position {position_id} introuvable dans PM"
            )
            self._active_position_id  = None
            self._active_trade_record = None
            return

        self.logger.warning(
            f"Force close position {position_id} @ {exit_price:.2f} "
            f"(session_end)"
        )

        # ── [v2.5.2 — FIX-TE-BUG5] Funding fees finaux avant la fermeture ────
        # _accumulate_funding_fees() modifie DEUX états distincts :
        #   1. position_manager.positions[id]['funding_fees'] (accumulation PM)
        #   2. session_manager.update_balance(-fee) pour chaque période (SM balance)
        # Ces deux mutations doivent précéder le calcul de pnl_net et settle_trade()
        # pour que les valeurs soient cohérentes. On wrap dans un try-except pour
        # éviter qu'une erreur partielle bloque la fermeture de la position.
        if self.engine_config.accumulate_funding and self._last_funding_check:
            try:
                self._accumulate_funding_fees(exit_time)
            except Exception as exc:
                # Funding partiellement appliqué : on log mais on poursuit.
                # settle_trade() utilisera les funding_fees déjà accumulés dans PM.
                self.logger.error(
                    f"Funding accumulation partielle lors de force_close "
                    f"({position_id}): {exc}. Fermeture poursuivie."
                )

        # ── Calcul fees de sortie (taker — fermeture forcée = market order) ────
        # [v2.5.3 — FIX-TE-5] Corrigé maker_fee → taker_fee.
        # Toute fermeture forcée (fin de session, grace period, limite atteinte)
        # est exécutée au marché : taker fee s'applique systématiquement.
        notional   = exit_price * position['size']
        exit_fees  = notional * self.order_simulator.taker_fee

        # Calcul PnL
        if position['direction'] == 'LONG':
            pnl_gross = (exit_price - position['entry_price']) * position['size']
        else:
            pnl_gross = (position['entry_price'] - exit_price) * position['size']

        funding_fees_accumulated = position.get('funding_fees', 0.0)
        pnl_net = pnl_gross - exit_fees - position.get('entry_fees', 0.0) - funding_fees_accumulated
        # settle_pnl exclut entry_fees (déduites à l'ENTRY via update_balance)
        # et funding (déduites via update_balance dans _accumulate_funding_fees).
        # settle_trade ne règle que : libération marge + PnL brut - exit_fees.
        settle_pnl = pnl_gross - exit_fees

        # ── Règlement capital via SessionManager ──────────────────────────────
        try:
            self.session_manager.settle_trade(
                pnl_net=settle_pnl,
                margin=position['collateral'],
            )
        except Exception as exc:
            # Erreur settle : on log CRITICAL mais on ferme quand même la position
            # dans PM pour éviter un état fantôme (position "ouverte" sans capital réservé).
            self.logger.critical(
                f"settle_trade() force_close a échoué ({position_id}): {exc}. "
                "La position sera fermée dans PM mais l'état capital peut être incohérent."
            )

        # Créer un fill synthétique pour la cohérence
        synthetic_fill = {
            'fill_price': exit_price,
            'exit_fees':  exit_fees,
            'pnl_gross':  pnl_gross,
            'pnl_net':    pnl_net,
            'exit_type':  'manual',
        }

        self._close_position(
            close_fill=synthetic_fill,
            close_reason='session_end',
            exit_price=exit_price,
            exit_time=exit_time,
        )

    # =========================================================================
    # MÉTHODES PRIVÉES — TRAILING STOP & ATR
    # =========================================================================

    def _update_trailing_stop(
        self,
        current_candle: Dict[str, Any],
        atr_value: Optional[float],
    ) -> None:
        """
        Met à jour le trailing stop de la position active.

        [FIX-TE-3] ATR pré-calculé une fois par candle et passé à
        PositionManager — évite les recalculs multiples en cas de
        positions multiples (actuellement une seule, mais architecture future-proof).

        Args:
            current_candle: Candle courante
            atr_value:      Valeur ATR pré-calculée (None si mode candle)
        """
        if self._active_position_id is None:
            return

        try:
            self.position_manager.update_trailing_stop(
                position_id=self._active_position_id,
                candle=current_candle,
                atr_value=atr_value,
            )
        except ValueError as exc:
            # ATR manquant en mode atr/hybrid — log mais ne pas crasher
            self.logger.error(
                f"Trailing stop update a échoué (ATR?): {exc}"
            )
        except Exception as exc:
            self.logger.error(
                f"Trailing stop update a échoué: {exc}", exc_info=True
            )

    def _get_atr_value(
        self,
        candles_window: pd.DataFrame,
        current_candle: Dict[str, Any],
    ) -> Optional[float]:
        """
        Retourne la valeur ATR courante via l'instance ATRIndicator
        déjà injectée dans PositionManager.

        [v2.5.1 — FIX-TE-3] Correction d'une duplication de calcul.
        L'ancienne implémentation importait et appelait calculate_atr_simple()
        directement, recréant un calcul ATR parallèle indépendant de l'instance
        ATRIndicator configurée dans PositionManager. Deux conséquences :
          1. Doublon de calcul (performance inutilement dégradée).
          2. Incohérence potentielle si ATRIndicator utilise une config différente
             de ATR_DEFAULT_PERIOD / ATR_DEFAULT_METHOD.
        La correction délègue à self.position_manager.atr, instance unique
        et source de vérité pour tout calcul ATR du pipeline trailing.

        Retourne None si :
          - Le mode trailing est 'candle' (ATR non requis par PositionManager)
          - position_manager.atr est None (non injecté à l'init)
          - La série ATR retourne NaN ou une valeur invalide

        Args:
            candles_window: Fenêtre OHLCV courante (passée à atr.calculate_atr)
            current_candle: Non utilisé — conservé pour stabilité de signature

        Returns:
            float: Valeur ATR scalaire (dernière bougie) ou None
        """
        trailing_type = (
            self.config
            .get('strategy', {})
            .get('trailing_stop', {})
            .get('type', 'candle')
        )

        # Mode candle : PositionManager n'a pas besoin d'ATR
        if trailing_type == 'candle':
            return None

        # [v2.5.2 — FIX-TE-BUG3] Délégation à l'instance injectée dans PM
        # — pas de recalcul parallèle, pas de nouvel import atr.py ici.
        if self.position_manager.atr is None:
            if not self._warned_missing_atr:
                # [VIGILANCE-1] Warning émis UNE SEULE FOIS par session pour éviter
                # le spam de milliers de lignes identiques sur un backtest long.
                # L'erreur initiale à l'init couvre déjà l'alerte de configuration.
                self.logger.error(
                    f"_get_atr_value: trailing type='{trailing_type}' mais "
                    "position_manager.atr est None — trailing stop ATR/HYBRID inopérant. "
                    "Ce message ne sera plus répété pour cette session."
                )
                self._warned_missing_atr = True
            return None

        try:
            atr_series = self.position_manager.atr.calculate_atr(candles_window)
            atr_value  = atr_series.iloc[-1]

            if pd.isna(atr_value) or atr_value <= 0:
                self.logger.debug(
                    f"ATR: valeur invalide ({atr_value}) sur "
                    f"{len(candles_window)} candles — retour None"
                )
                return None

            return float(atr_value)

        except Exception as exc:
            self.logger.warning(f"ATR pré-calcul échoué: {exc}")
            return None

    # =========================================================================
    # MÉTHODES PRIVÉES — FUNDING FEES
    # =========================================================================

    def _accumulate_funding_fees(self, current_time: datetime) -> None:
        """
        [FIX-TE-2] Accumule les funding fees pour la position active.

        Réalité Binance perpetual futures :
        - Funding toutes les 8h : 00:00, 08:00, 16:00 UTC
        - Frais = notional × funding_rate_8h
        - Débiteur ou créditeur selon direction (simplifié : toujours débiteur ici)

        Détecte les timestamps UTC fixes traversés depuis le dernier check
        et accumule les frais correspondants via position_manager.add_funding_fee().

        Args:
            current_time: Timestamp UTC de la candle courante
        """
        if self._active_position_id is None or self._last_funding_check is None:
            return

        position = self.position_manager.get_position(self._active_position_id)
        if position is None:
            return

        FUNDING_HOURS = (0, 8, 16)
        last_check = self._last_funding_check

        # Construire la liste des timestamps de funding entre last_check et current_time
        funding_timestamps = []
        cursor = last_check.replace(hour=0, minute=0, second=0, microsecond=0)

        while cursor <= current_time:
            for hour in FUNDING_HOURS:
                ft = cursor.replace(hour=hour)
                if last_check < ft <= current_time:
                    funding_timestamps.append(ft)
            cursor += timedelta(days=1)

        for ft in funding_timestamps:
            notional = position.get('notional', 0.0)
            if notional <= 0:
                continue

            funding_fee = notional * self.order_simulator.funding_rate_8h

            try:
                self.position_manager.add_funding_fee(
                    position_id=self._active_position_id,
                    funding_fee=funding_fee,
                    timestamp=ft,
                )
                # Déduire du capital (coût réel immédiat)
                self.session_manager.update_balance(-funding_fee)

                self.logger.debug(
                    f"Funding fee: {self._active_position_id}, "
                    f"ts={ft.strftime('%Y-%m-%d %H:%M')}, "
                    f"fee={funding_fee:.4f} USDT"
                )
            except Exception as exc:
                self.logger.warning(f"add_funding_fee a échoué: {exc}")

        if funding_timestamps:
            self._last_funding_check = current_time

    # =========================================================================
    # MÉTHODES PRIVÉES — CYCLE DE VIE SESSION
    # =========================================================================

    def _finalize_session(
        self, end_reason: str
    ) -> Tuple[Dict[str, Any], List[Dict[str, Any]], Dict[str, Any]]:
        """
        Termine la session et collecte le résumé.

        Appelé dans le bloc finally de run_session() pour garantir
        la propreté de l'état même en cas d'erreur.

        [v2.5.2 — FIX-TE-BUG1] Les trades sont capturés EN PREMIER, AVANT
        l'appel à end_session() qui déclenche current_session_trades.clear()
        (SessionManager.end_session(), ligne ~983). Sans cette capture, le
        snapshot trades passé à _build_run_result() était systématiquement vide
        → analytics_engine recevait 0 trade, win_rate=0%, reporting brisé.

        [v2.5.5 — FIX-TE-BUG2] Le snapshot capital est capturé EN PREMIER,
        AVANT end_session() qui met current_session=None. Sans cette capture,
        order_simulator.get_statistics() appelait get_capital_total() sur un
        SessionManager sans session active → RuntimeError systématique.

        Args:
            end_reason: Raison de fin de session

        Returns:
            Tuple[session_summary, trades_snapshot, capital_snapshot] :
            - session_summary    : dict depuis SessionManager.end_session()
            - trades_snapshot    : copie des trades AVANT le clear de end_session()
            - capital_snapshot   : dict capital capturé AVANT end_session()
        """
        # ── [FIX-TE-BUG1] Snapshot trades AVANT end_session() ────────────────
        trades_snapshot: List[Dict[str, Any]] = list(
            self.session_manager.current_session_trades
        )

        # ── [FIX-TE-BUG2] Snapshot capital AVANT end_session() ───────────────
        # end_session() met current_session=None en ligne ~987 de session_manager.py.
        # order_simulator.get_statistics() appelle get_capital_total() qui délègue
        # à get_current_balance() — lève RuntimeError si current_session is None.
        # On capture les trois valeurs ici, pendant que la session est encore active.
        try:
            capital_snapshot: Dict[str, Any] = {
                'capital_total':     self.session_manager.get_capital_total(),
                'capital_available': self.session_manager.get_capital_available(),
                'capital_locked':    self.session_manager.get_capital_locked(),
            }
        except Exception as exc:
            self.logger.warning(f"Capital snapshot failed (non-fatal): {exc}")
            capital_snapshot = {
                'capital_total': 0.0,
                'capital_available': 0.0,
                'capital_locked': 0.0,
            }

        try:
            session_summary = self.session_manager.end_session(reason=end_reason)
            # [v2.5.5 — FIX-TE-BUG3] STOPPED → IDLE pour permettre la réutilisation
            # de l'instance sur plusieurs sessions consécutives.
            # STOPPED était un état terminal irréversible : run_session() le refuse
            # dès l'entrée, bloquant toutes les sessions après la première.
            # IDLE est l'état correct entre deux sessions — aucune session active,
            # mais l'instance est prête à en démarrer une nouvelle.
            self._state = EngineState.IDLE
            return session_summary, trades_snapshot, capital_snapshot
        except Exception as exc:
            self.logger.critical(
                f"end_session() a échoué: {exc}", exc_info=True
            )
            # Retourner un résumé minimal pour éviter de crasher analytics_engine
            return (
                {
                    'end_reason': f'finalization_error: {exc}',
                    'pnl': 0.0,
                    'pnl_pct': 0.0,
                    'total_trades': 0,
                    'win_rate': 0.0,
                },
                trades_snapshot,
                capital_snapshot,
            )

    def _reset_session_state(self) -> None:
        """Réinitialise l'état interne de session avant chaque run_session()."""
        self._active_position_id    = None
        self._active_trade_record   = None
        self._pending_order         = None   # [FIX-LAB-1] Ordre différé éventuel annulé entre sessions
        self._last_funding_check    = None
        self._last_candle_timestamp = None
        self._step_results          = {}
        self._session_errors        = []
        self._candles_processed     = 0
        # [VIGILANCE-1] Le flag anti-spam ATR est réinitialisé entre sessions
        # pour qu'une nouvelle session bénéficie d'un warning si ATR est toujours absent.
        # Ne pas réinitialiser ici si l'avertissement a déjà été émis à l'init
        # (cas où atr is None depuis le départ — le message init suffit).
        if self.position_manager.atr is not None:
            self._warned_missing_atr = False

        # [v2.5.6 — FIX-STR-1] Reset contexte + cache de Strategy entre sessions.
        # Sans ce reset, signals_history et orders_history s'accumulent en mémoire
        # sur toute la durée du backtest multi-sessions, et consecutive_same_direction
        # porte l'état directionnel de la session N-1 vers N.
        # Les stats globales sont conservées dans Strategy (voir reset_session()).
        self.strategy.reset_session()

        # [v2.8.2 — FIX-TE-9] Utilisation de reset_session() (nouveau contrat public
        # introduit par FIX-RM-5) au lieu de reset_anomaly_counter().
        # reset_session() regroupe tous les resets d'état inter-sessions du RiskManager
        # en une seule méthode explicite — plus extensible si d'autres compteurs
        # sont ajoutés dans de futures versions du RiskManager.
        self.strategy.risk_manager.reset_session()

    # =========================================================================
    # MÉTHODES PRIVÉES — CONSTRUCTION RÉSULTAT
    # =========================================================================

    def _build_run_result(
        self,
        session_summary: Dict[str, Any],
        trades_snapshot: List[Dict[str, Any]],
        capital_snapshot: Dict[str, Any],
    ) -> EngineRunResult:
        """
        Construit EngineRunResult pour analytics_engine.

        Agrège toutes les données de session en une structure propre :
        session_summary, trades complets, positions fermées, statistiques.

        [v2.5.2 — FIX-TE-BUG1] trades_snapshot est désormais fourni par
        _finalize_session(), capturé AVANT que SessionManager.end_session()
        n'appelle current_session_trades.clear(). Ce paramètre remplace
        la lecture post-clear qui retournait systématiquement une liste vide.

        [v2.5.5 — FIX-TE-BUG2] capital_snapshot est fourni par
        _finalize_session(), capturé AVANT end_session() qui met
        current_session=None. Ce snapshot est injecté dans simulator_stats
        pour éviter le RuntimeError de get_capital_total() post-fermeture.

        Args:
            session_summary:  Résumé retourné par SessionManager.end_session()
            trades_snapshot:  Copie des trades capturée avant end_session()
            capital_snapshot: Dict capital capturé avant end_session()

        Returns:
            EngineRunResult complet
        """
        # Récupérer les stats de base de l'OrderSimulator (sans appel SessionManager)
        simulator_stats = self.order_simulator.get_statistics_without_capital()
        # Enrichir avec le snapshot capital capturé avant end_session()
        simulator_stats.update(capital_snapshot)

        return EngineRunResult(
            session_id        = session_summary.get('session_id', ''),  # [v2.5.3]
            session_summary   = session_summary,
            trades            = trades_snapshot,     # [FIX-TE-BUG1] snapshot pré-clear
            closed_positions  = self.position_manager.get_closed_positions(),
            strategy_stats    = self.strategy.get_statistics(),
            simulator_stats   = simulator_stats,     # [FIX-TE-BUG2] snapshot pré-close
            step_results      = self._step_results.copy(),
            errors            = self._session_errors.copy(),
            candles_processed = self._candles_processed,
        )

    # =========================================================================
    # MÉTHODES PRIVÉES — UTILITAIRES
    # =========================================================================

    def _validate_dependencies(
        self,
        config: dict,
        strategy: Strategy,
        session_manager: SessionManager,
        order_simulator: OrderSimulator,
        position_manager: PositionManager,
    ) -> None:
        """
        Valide que toutes les dépendances sont présentes et du bon type.

        Raises:
            TypeError:  Si une dépendance est None ou du mauvais type.
            ValueError: Si config manquante ou mode invalide.
        """
        if not isinstance(config, dict):
            raise TypeError(f"config doit être un dict, reçu: {type(config)}")

        required_config_keys = ['general', 'capital', 'session_management', 'strategy']
        missing = [k for k in required_config_keys if k not in config]
        if missing:
            raise ValueError(f"Config manquante — clés requises: {missing}")

        mode = config.get('general', {}).get('mode', '').upper()
        if mode not in ('BACKTEST', 'PAPER', 'LIVE'):
            raise ValueError(
                f"Mode invalide: '{mode}'. Valeurs acceptées: BACKTEST, PAPER, LIVE"
            )

        deps = {
            'strategy':         (strategy, Strategy),
            'session_manager':  (session_manager, SessionManager),
            'order_simulator':  (order_simulator, OrderSimulator),
            'position_manager': (position_manager, PositionManager),
        }

        for name, (obj, expected_type) in deps.items():
            if obj is None:
                raise TypeError(f"{name} ne peut pas être None")
            if not isinstance(obj, expected_type):
                raise TypeError(
                    f"{name} doit être une instance de {expected_type.__name__}, "
                    f"reçu: {type(obj).__name__}"
                )

    def _validate_candles(self, candles: pd.DataFrame) -> None:
        """
        Valide le DataFrame OHLCV avant de démarrer une session.

        Raises:
            ValueError: Si données invalides ou colonnes manquantes.
        """
        if not isinstance(candles, pd.DataFrame):
            raise ValueError(f"candles doit être un DataFrame, reçu: {type(candles)}")

        if candles.empty:
            raise ValueError("candles est vide")

        required_cols = ['open', 'high', 'low', 'close', 'volume', 'timestamp']
        missing = [c for c in required_cols if c not in candles.columns]
        if missing:
            raise ValueError(f"Colonnes manquantes dans candles: {missing}")

        if len(candles) < self.engine_config.min_candles_window:
            raise ValueError(
                f"Données insuffisantes: {len(candles)} candles < "
                f"min_candles_window={self.engine_config.min_candles_window}"
            )

    def _ensure_utc_datetime(self, ts: Any) -> datetime:
        """
        Convertit tout timestamp en datetime UTC-aware.

        Fallback sur datetime.now(UTC) si conversion impossible.

        Args:
            ts: Timestamp (int ms, datetime naïf, datetime aware, string ISO)

        Returns:
            datetime UTC-aware
        """
        if ts is None:
            return datetime.now(timezone.utc)

        if isinstance(ts, datetime):
            if ts.tzinfo is None:
                return ts.replace(tzinfo=timezone.utc)
            return ts.astimezone(timezone.utc)

        try:
            from src.utils.helpers import timestamp_to_datetime
            return timestamp_to_datetime(ts)
        except Exception:
            return datetime.now(timezone.utc)

    def _increment_step_result(self, result: StepResult) -> None:
        """Incrémente le compteur du StepResult dans _step_results."""
        key = result.name
        self._step_results[key] = self._step_results.get(key, 0) + 1

    def _maybe_log_progress(self, candle_index: int, total_candles: int) -> None:
        """Émet un log de progression tous les N candles (configurable)."""
        n = self.engine_config.log_every_n_candles
        if n <= 0:
            return

        if candle_index > 0 and candle_index % n == 0:
            pct = (candle_index / total_candles) * 100
            has_pos = self._active_position_id is not None
            try:
                balance = self.session_manager.get_capital_total()
            except RuntimeError:
                balance = 0.0

            self.logger.debug(
                f"Progress: {candle_index}/{total_candles} "
                f"({pct:.1f}%) | "
                f"Balance: {balance:.2f} USDT | "
                f"Position: {'OPEN' if has_pos else 'NONE'}"
            )

    # =========================================================================
    # API PUBLIQUE — ACCESSEURS D'ÉTAT
    # =========================================================================

    @property
    def state(self) -> EngineState:
        """État courant du moteur (lecture seule)."""
        return self._state

    @property
    def has_active_position(self) -> bool:
        """True si une position est actuellement ouverte."""
        return self._active_position_id is not None

    def get_active_position(self) -> Optional[Dict[str, Any]]:
        """
        Retourne la position active courante (copie).

        Returns:
            Dict position ou None si aucune position ouverte.
        """
        if self._active_position_id is None:
            return None
        return self.position_manager.get_position(self._active_position_id)

    def get_engine_status(self) -> Dict[str, Any]:
        """
        Retourne un snapshot complet de l'état de l'engine.

        Utile pour monitoring, tests, et debugging.

        Returns:
            Dict avec state, position, capital, session info.
        """
        status: Dict[str, Any] = {
            'state':               self._state.name,
            'mode':                self.mode,
            'candles_processed':   self._candles_processed,
            'has_active_position': self.has_active_position,
            'active_position_id':  self._active_position_id,
            'error_count':         len(self._session_errors),
        }

        # Snapshot capital (si session active)
        try:
            status['capital_total']     = self.session_manager.get_capital_total()
            status['capital_available'] = self.session_manager.get_capital_available()
            status['capital_locked']    = self.session_manager.get_capital_locked()
        except RuntimeError:
            status['capital_total']     = None
            status['capital_available'] = None
            status['capital_locked']    = None

        # Snapshot session (si session active)
        try:
            session = self.session_manager.get_current_session()
            if session:
                status['session_n']       = session.get('session_n')
                status['session_status']  = session.get('status')
        except Exception:
            pass

        return status

    def reset(self) -> None:
        """
        Réinitialise l'engine à l'état IDLE depuis n'importe quel état.

        [v2.5.6 — FIX-TE-7] Permet la récupération d'un engine en état STOPPED
        ou dans tout état incohérent sans nécessiter une réinstanciation complète.

        Cas d'usage :
            - Engine en état STOPPED après une erreur fatale dans run_session()
            - Réinitialisation explicite entre des runs de test
            - Récupération suite à une interruption externe (signal OS, timeout)

        Comportement :
            - Réinitialise tout l'état interne de session (position, trades, etc.)
            - Force _state = IDLE
            - Ne réinitialise PAS les dépendances injectées (strategy, session_manager, etc.)
            - Ne crée PAS de nouvelle session — appeler run_session() après reset().

        Notes :
            - Cette méthode est idempotente : appeler reset() en état IDLE est sans effet.
            - Thread-safety : à appeler depuis le thread principal uniquement.

        Examples:
            >>> engine.run_session(...)  # Lève RuntimeError → engine en état STOPPED
            >>> engine.reset()           # Récupération sans réinstanciation
            >>> engine.run_session(...)  # Nouvelle tentative
        """
        old_state = self._state.name
        self._reset_session_state()
        self._state = EngineState.IDLE
        self.logger.info(
            f"TradingEngine.reset() : état '{old_state}' → IDLE | "
            f"État interne de session nettoyé."
        )

    def __repr__(self) -> str:
        return (
            f"TradingEngine("
            f"state={self._state.name}, "
            f"mode={self.mode}, "
            f"position={'open' if self.has_active_position else 'none'}, "
            f"candles_processed={self._candles_processed})"
        )


# ============================================================================
# FIN DU MODULE
# ============================================================================
