"""
BULLET-1 - Session Manager Module
==================================

Gestion des sessions de trading de x jours avec reset capital configurable.
Module CORE pour backtesting et live/paper trading.

Version: 2.5.9
Date: 2026-03-13
Author: FuegoDev

Dependencies: helpers.py, logger.py, config_loader.py, day_trades_manager.py

Thread Safety: RLock sur toutes les mutations de capital + flags blocage.
"""

import os
import csv
import json
import uuid
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple
from copy import deepcopy
from enum import Enum
import sys

# [v2.5.6 — FIX-PATH-6] Résolution racine projet : pattern direct unifié.
# Remplace find_project_root() locale dupliquée dans ~10 modules (DRY).
# Calcul en 1 ligne depuis __file__ — sans fonction nommée, sans import.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# BULLET-1 imports
from src.utils.helpers import (
    format_datetime,
    ensure_directory,
    write_json,
    generate_id,
    get_project_root
)
from src.utils.logger import BulletLogger

#: Version du module — utilisée dans les exports JSON (save_all_summaries)
_VERSION = "2.5.9"


class SessionStatus(Enum):
    """Session status enumeration."""
    ACTIVE  = "active"
    ENDED   = "ended"
    PAUSED  = "paused"   # Future feature


class SessionManager:
    """
    Manages trading sessions with configurable capital reset.

    Responsabilités:
    - Session lifecycle management (create, end, validate)
    - Capital tracking — SEULE source de vérité
      · current_funds  : solde total (USDT)
      · _locked_margin : marge réservée par les positions ouvertes
      · capital_available = current_funds - _locked_margin
    - Session limits enforcement (max_loss, max_gain)
    - Daily limits coordination (via DayTradesManager)
    - Trade validation and storage
    - Metrics calculation and export

    Capital API :
        reserve_margin(amount)         → Avant entrée position
        release_margin(amount)         → Après fermeture (sans PnL)
        update_balance(amount)         → Ajout/soustraction PnL net seul
        settle_trade(pnl_net, margin)  → Atomique : release + update
        get_capital_available()        → current_funds - _locked_margin
        get_capital_total()            → current_funds
        get_capital_locked()           → _locked_margin

    Timezone Handling :
        All datetime objects are UTC-aware. Naive datetimes assumed UTC.

    Priority Hierarchy:
        1. CRITICAL : Session max_loss with safety margin → Force close
        2. TACTICAL : Daily limits reached → Block trades until midnight
        3. STRATEGIC: Session limits reached → End session

    Thread Safety:
        Toutes mutations du capital protégées par self._lock (RLock).
        Les opérations de session (create, end) restent single-threaded
        par convention (appelées depuis engine.py en séquence).

    Examples:
        >>> session_mgr = SessionManager(config)
        >>>
        >>> session = session_mgr.create_session(
        ...     1,
        ...     datetime(2025, 5, 1, tzinfo=timezone.utc),
        ...     datetime(2025, 5, 10, tzinfo=timezone.utc)
        ... )
        >>>
        >>> # Avant ouverture position
        >>> session_mgr.reserve_margin(50.0)
        >>>
        >>> # Après fermeture position (atomique)
        >>> session_mgr.settle_trade(pnl_net=3.20, margin=50.0)
        >>>
        >>> should_end, reason = session_mgr.should_end_session()
        >>> summary = session_mgr.end_session(reason='completed')
    """

    # [v2.5.1 — FIX SM-1 / PM-1] Champs alignés sur la convention unifiée cross-module.
    # Anciens noms : 'side', 'sl_price', 'tp_price' (nommage position_manager v2.6.0)
    # Nouveaux noms : 'direction', 'stop_loss', 'take_profit' (alignement strategy + order_simulator)
    REQUIRED_TRADE_FIELDS = [
        'position_id',
        'direction',    # était 'side'
        'entry_price',
        'size',
        'stop_loss',    # était 'sl_price'
        'take_profit'   # était 'tp_price'
    ]

    def __init__(self, config: dict):
        """Initialize SessionManager with validated config."""
        self.logger = BulletLogger()
        self.config  = config

        # Session parameters
        session_config = config['session_management']
        self.trades_period_days = session_config['trades_period_days']
        self.reset_capital      = session_config['reset_capital_between_sessions']
        self.max_loss_pct       = session_config['max_loss_per_session_pct']
        self.max_gain_pct       = session_config['max_gain_per_session_pct']

        # Edge cases config
        edge_config = session_config.get('edge_cases', {})
        self.grace_period_hours          = edge_config.get('grace_period_hours', 4)
        self.max_loss_safety_margin_pct  = edge_config.get('max_loss_safety_margin_pct', 20.0)
        self.allow_grace_period          = edge_config.get('allow_grace_period', True)
        self.force_close_on_critical     = edge_config.get('force_close_on_critical', True)
        self.log_edge_cases              = edge_config.get('log_edge_cases', True)

        # Initial capital
        # [v2.5.1 — FIX SM-1] Normalisation de la casse AVANT comparaison.
        # L'ancienne implémentation comparait mode == 'backtest' (minuscules),
        # alors que strategy.py stocke 'BACKTEST' (majuscules).
        # Résultat silencieux : le bloc else était atteint → initial_capital = 100.0 USDT
        # quelle que soit la valeur configurée. Bug critique, aucune erreur émise.
        mode = config['general']['mode'].upper()
        if mode == 'BACKTEST':
            self.initial_capital = config['capital']['initial_capital_backtest']
        elif mode in ('PAPER', 'LIVE'):
            self.initial_capital = config['capital']['initial_capital_live']
        else:
            raise ValueError(
                f"Mode invalide: '{config['general']['mode']}'. "
                f"Valeurs acceptées : BACKTEST, PAPER, LIVE. "
                f"Vérifiez config['general']['mode']."
            )

        # Session state
        self.current_session: Optional[Dict[str, Any]]        = None
        self.sessions_summary: List[Dict[str, Any]]           = []
        self.current_session_trades: List[Dict[str, Any]]     = []
        self.block_new_trades                                  = False

        # ── Capital lock interne ──────────────────────────────────────────────
        # Marge totale réservée par les positions ouvertes.
        # Séparé de current_funds pour éviter toute ambiguïté.
        self._locked_margin: float = 0.0

        # Thread-safety sur toutes les mutations capital
        self._lock = threading.RLock()

        # Save paths
        self.auto_save_path   = session_config.get(
            'auto_save_trades_path',
            'results/backtests/sessions/trades/'
        )
        self.summary_save_path = session_config.get(
            'session_summary_save_path',
            'results/backtests/sessions/summaries/'
        )

        # Daily limits
        daily_config              = session_config.get('daily_limits', {})
        self.daily_limits_enabled = daily_config.get('enabled', False)

        if self.daily_limits_enabled:
            from src.core.day_trades_manager import DayTradesManager
            self.day_manager = DayTradesManager(daily_config, self.logger)
            self.logger.info(
                f"SessionManager initialized: period={self.trades_period_days}d, "
                f"reset_capital={self.reset_capital}, "
                f"max_loss={self.max_loss_pct}%, max_gain={self.max_gain_pct}% | "
                f"Daily limits ENABLED"
            )
        else:
            self.day_manager = None
            self.logger.info(
                f"SessionManager initialized: period={self.trades_period_days}d, "
                f"reset_capital={self.reset_capital}, "
                f"max_loss={self.max_loss_pct}%, max_gain={self.max_gain_pct}% | "
                f"Daily limits DISABLED"
            )

        if mode == 'BACKTEST' and not self.reset_capital:
            self.logger.warning(
                "BACKTEST mode with reset_capital=False. "
                "Consider reset_capital=True for fair session comparison."
            )

    # =========================================================================
    # GESTION CAPITAL — API PUBLIQUE 
    # SessionManager est le SEUL maître de l'état du capital.
    # =========================================================================

    def reserve_margin(self, amount: float) -> None:
        """
        Réserve une marge pour une position en cours d'ouverture.

        Doit être appelé par l'engine avant toute entrée en position.
        Lève une exception si les fonds disponibles sont insuffisants.

        Args:
            amount: Montant à verrouiller (USDT, > 0)

        Raises:
            RuntimeError: Aucune session active.
            ValueError: Montant invalide (≤ 0).
            RuntimeError: Fonds disponibles insuffisants.

        Examples:
            >>> session_mgr.reserve_margin(50.0)
        """
        if amount <= 0:
            raise ValueError(f"reserve_margin: montant invalide ({amount})")

        if self.current_session is None:
            raise RuntimeError("reserve_margin: aucune session active")

        with self._lock:
            available = self._get_capital_available_unsafe()
            if available < amount:
                raise RuntimeError(
                    f"Fonds insuffisants: requis {amount:.2f}, "
                    f"disponible {available:.2f}"
                )
            self._locked_margin += amount

        self.logger.debug(
            f"Margin reserved: +{amount:.2f} | "
            f"Total locked: {self._locked_margin:.2f} | "
            f"Available: {self.get_capital_available():.2f}"
        )

    def release_margin(self, amount: float) -> None:
        """
        Libère une marge après fermeture de position (sans PnL).

        Préférer settle_trade() pour les fermetures réelles (atomique).
        Utiliser release_margin() seul uniquement pour annulation d'ordre
        ou gestion d'erreur.

        Args:
            amount: Montant à libérer (USDT, > 0)

        Raises:
            RuntimeError: Aucune session active.
            ValueError: Montant invalide (≤ 0).

        Examples:
            >>> session_mgr.release_margin(50.0)   # Annulation d'ordre
        """
        if amount <= 0:
            raise ValueError(f"release_margin: montant invalide ({amount})")

        if self.current_session is None:
            raise RuntimeError("release_margin: aucune session active")

        with self._lock:
            self._locked_margin = max(0.0, self._locked_margin - amount)

        self.logger.debug(
            f"Margin released: -{amount:.2f} | "
            f"Total locked: {self._locked_margin:.2f}"
        )

    def update_balance(self, amount: float) -> None:
        """
        Applique un delta de PnL net au solde de la session courante.

        Ajoute `amount` (positif = gain, négatif = perte).
        Préférer settle_trade() pour les fermetures réelles.

        Args:
            amount: Delta PnL net (USDT). Peut être négatif.

        Raises:
            RuntimeError: Aucune session active.

        Examples:
            >>> session_mgr.update_balance(3.20)    # Gain
            >>> session_mgr.update_balance(-2.50)   # Perte
        """
        if self.current_session is None:
            raise RuntimeError("update_balance: aucune session active")

        with self._lock:
            old_balance = self.current_session['current_funds']
            new_balance = old_balance + amount
            self.current_session['current_funds'] = new_balance

        if self.day_manager and self.day_manager.current_day:
            self.day_manager.update_day_balance(new_balance)

        self.logger.debug(
            f"Balance updated: {old_balance:.2f} → {new_balance:.2f} "
            f"({amount:+.2f}) | Session {self.current_session['session_n']}"
        )

    def settle_trade(self, pnl_net: float, margin: float) -> None:
        """
        Opération atomique : libère la marge ET applique le PnL net.

        C'est le point d'entrée principal pour toute fermeture de position.
        Garantit l'absence de race condition entre release et update.

        Args:
            pnl_net: PnL net après fees (USDT, positif ou négatif)
            margin:  Marge à libérer (USDT, > 0)

        Raises:
            RuntimeError: Aucune session active.
            ValueError: Marge invalide (≤ 0).

        Examples:
            >>> # Position fermée avec gain de 3.20 USDT, marge de 50 USDT
            >>> session_mgr.settle_trade(pnl_net=3.20, margin=50.0)
            >>>
            >>> # Position fermée avec perte de 2.50 USDT
            >>> session_mgr.settle_trade(pnl_net=-2.50, margin=50.0)
        """
        if margin <= 0:
            raise ValueError(f"settle_trade: marge invalide ({margin})")

        if self.current_session is None:
            raise RuntimeError("settle_trade: aucune session active")

        with self._lock:
            # Release margin
            self._locked_margin = max(0.0, self._locked_margin - margin)

            # Apply PnL
            old_balance = self.current_session['current_funds']
            new_balance = old_balance + pnl_net
            self.current_session['current_funds'] = new_balance

        if self.day_manager and self.day_manager.current_day:
            self.day_manager.update_day_balance(new_balance)

        self.logger.debug(
            f"Trade settled: margin released={margin:.2f}, pnl_net={pnl_net:+.2f} | "
            f"Balance: {old_balance:.2f} → {new_balance:.2f} | "
            f"Locked: {self._locked_margin:.2f}"
        )

    # ── Accesseurs capital (thread-safe) ─────────────────────────────────────

    def get_capital_available(self) -> float:
        """
        Retourne le capital disponible (non réservé par des positions).

        capital_available = current_funds - _locked_margin

        Returns:
            float: Capital libre en USDT

        Raises:
            RuntimeError: Aucune session active.
        """
        if self.current_session is None:
            raise RuntimeError("get_capital_available: aucune session active")

        with self._lock:
            return self._get_capital_available_unsafe()

    def get_capital_total(self) -> float:
        """
        Retourne le solde total de la session (fonds actuels).

        Returns:
            float: current_funds (USDT)

        Raises:
            RuntimeError: Aucune session active.
        """
        return self.get_current_balance()

    def get_capital_locked(self) -> float:
        """
        Retourne la marge actuellement verrouillée par les positions.

        Returns:
            float: _locked_margin (USDT)
        """
        with self._lock:
            return self._locked_margin

    def _get_capital_available_unsafe(self) -> float:
        """
        Calcule le capital disponible sans acquérir le lock.

        À n'appeler QUE depuis un bloc `with self._lock`.
        """
        return self.current_session['current_funds'] - self._locked_margin

    # =========================================================================
    # GESTION BALANCE HISTORIQUE (conservé pour rétrocompatibilité)
    # =========================================================================

    def update_session_balance(self, new_balance: float) -> None:
        """
        Met à jour le solde à une valeur absolue.

        Méthode conservée pour rétrocompatibilité avec engine.py v1.
        Préférer update_balance(delta) pour les nouvelles intégrations.

        Args:
            new_balance: Nouveau solde absolu (USDT)

        Raises:
            RuntimeError: Aucune session active.
        """
        if self.current_session is None:
            raise RuntimeError("update_session_balance: aucune session active")

        with self._lock:
            old_balance = self.current_session['current_funds']
            self.current_session['current_funds'] = new_balance

        if self.day_manager and self.day_manager.current_day:
            self.day_manager.update_day_balance(new_balance)

        delta = new_balance - old_balance
        self.logger.debug(
            f"Session {self.current_session['session_n']} balance: "
            f"{old_balance:.2f} → {new_balance:.2f} ({delta:+.2f})"
        )

    def get_current_balance(self) -> float:
        """
        Retourne le solde courant de la session active.

        Returns:
            float: current_funds (USDT)

        Raises:
            RuntimeError: Aucune session active.
        """
        if self.current_session is None:
            raise RuntimeError("get_current_balance: aucune session active")
        with self._lock:
            return self.current_session['current_funds']

    # =========================================================================
    # GESTION SESSIONS
    # =========================================================================

    def _ensure_utc(self, dt: datetime) -> datetime:
        """
        Ensure datetime is UTC-aware.

        Naive datetimes are assumed UTC (backtest CSV standard).
        Non-UTC aware datetimes are converted to UTC with warning.

        Args:
            dt: Datetime to normalize

        Returns:
            UTC-aware datetime
        """
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        elif dt.tzinfo != timezone.utc:
            self.logger.warning(
                f"Non-UTC timezone detected: {dt.tzinfo}. "
                f"Converting to UTC. Timestamp: {dt.isoformat()}"
            )
            return dt.astimezone(timezone.utc)
        else:
            return dt

    def get_initial_funds(self, session_n: int) -> float:
        """
        Calculate initial funds for session n based on reset_capital mode.

        If reset_capital=True  → Always return initial_capital
        If reset_capital=False → Return previous session's final_funds
        """
        if session_n < 1:
            raise ValueError(f"session_n must be >= 1, got {session_n}")

        if self.reset_capital:
            initial_funds = self.initial_capital
            self.logger.debug(f"Session {session_n} capital (reset): {initial_funds:.2f}")
        else:
            if session_n == 1:
                initial_funds = self.initial_capital
                self.logger.debug(f"Session 1 capital (initial): {initial_funds:.2f}")
            else:
                if len(self.sessions_summary) < session_n - 1:
                    raise ValueError(
                        f"Cannot get initial funds for session {session_n}: "
                        f"session {session_n - 1} not completed"
                    )
                initial_funds = self.sessions_summary[session_n - 2]['final_funds']
                self.logger.debug(f"Session {session_n} capital (from prev): {initial_funds:.2f}")

        return initial_funds

    def create_session(
        self,
        session_n: int,
        start_date: datetime,
        end_date: datetime
    ) -> Dict[str, Any]:
        """
        Create new session with normalized UTC-aware dates.

        Réinitialise également _locked_margin à 0.

        [v2.5.6 — FIX-SM-2] Sécurisation : si current_session est dans un état
        ENDED (session terminée normalement mais non nettoyée suite à une exception
        dans end_session()), on force le nettoyage avant de créer la nouvelle session
        plutôt que de lever une ValueError. Cela évite un blocage irréversible du
        pipeline multi-sessions en cas d'erreur non-fatale à la fin d'une session.
        Les sessions dans l'état ACTIVE sont toujours refusées (comportement inchangé).

        Args:
            session_n:  Session number (1-based)
            start_date: Session start (UTC-aware or naive assumed UTC)
            end_date:   Session end (UTC-aware or naive assumed UTC)

        Returns:
            Created session dict with UTC-aware dates

        Raises:
            ValueError: If session still ACTIVE or invalid parameters
        """
        if self.current_session is not None:
            current_status = self.current_session.get('status')
            if current_status == SessionStatus.ACTIVE.value:
                raise ValueError(
                    f"Cannot create session {session_n}: "
                    f"Session {self.current_session['session_n']} still active"
                )
            # [v2.5.6 — FIX-SM-2] Session ENDED mais non nettoyée → reset forcé.
            # Cas possible si end_session() a levé une exception après
            # current_session['status'] = ENDED mais avant current_session = None.
            self.logger.warning(
                f"[FIX-SM-2] create_session({session_n}): session précédente "
                f"({self.current_session['session_n']}) dans état "
                f"'{current_status}' non nettoyée. Reset forcé avant création."
            )
            with self._lock:
                self._locked_margin   = 0.0
                self.block_new_trades = False
            self.current_session_trades = []
            self.current_session        = None

        # Normalize dates to UTC-aware
        start_date = self._ensure_utc(start_date)
        end_date   = self._ensure_utc(end_date)

        if start_date >= end_date:
            raise ValueError("start_date must be < end_date")

        initial_funds = self.get_initial_funds(session_n)

        session = {
            'session_id':    str(uuid.uuid4()),   # [v2.5.3] Identifiant unique de session
            'session_n':     session_n,
            'start_date':    start_date,
            'end_date':      end_date,
            'initial_funds': initial_funds,
            'current_funds': initial_funds,
            'status':        SessionStatus.ACTIVE.value,
            'end_reason':    None,
            'created_at':    datetime.now(timezone.utc)
        }

        with self._lock:
            self.current_session     = session
            self._locked_margin      = 0.0   # Reset marge à chaque nouvelle session
            self.block_new_trades    = False

        self.current_session_trades  = []

        if self.day_manager:
            # [v2.5.4 — FIX-SM-BUG1] Reset DayTradesManager AVANT start_new_day().
            # Sans ce reset, days_summary s'accumule entre sessions :
            # session 2 hérite des jours de session 1, save_days_summary() sauvegarde
            # N*period_days jours au lieu de period_days → reporting corrompu.
            self.day_manager.reset()
            self.day_manager.start_new_day(start_date, initial_funds)

        self.logger.log_session_start(
            session_n  = session_n,
            capital    = initial_funds,
            start_date = format_datetime(start_date, "%Y-%m-%d"),
            end_date   = format_datetime(end_date, "%Y-%m-%d")
        )

        return session

    def add_trade(self, trade: Dict[str, Any]) -> None:
        """
        Add trade to current session with strict validation.

        Rejects if trading blocked by session or daily limits.

        Args:
            trade: Trade dict with required fields

        Raises:
            RuntimeError: If no active session or trading blocked
            ValueError: If required fields missing
        """
        if self.current_session is None:
            raise RuntimeError("Cannot add trade: no active session")

        if not self.is_trading_allowed():
            raise RuntimeError(
                f"Trading BLOCKED. Session or daily limits reached. "
                f"session_blocked={self.block_new_trades}, "
                f"day_blocked="
                f"{self.day_manager.is_trading_blocked if self.day_manager else False}"
            )

        missing_fields = [f for f in self.REQUIRED_TRADE_FIELDS if f not in trade]
        if missing_fields:
            raise ValueError(f"Trade missing required fields: {missing_fields}")

        if self.current_session.get('status') != SessionStatus.ACTIVE.value:
            self.logger.warning(
                f"Adding trade to non-active session "
                f"{self.current_session['session_n']}"
            )

        self.current_session_trades.append(trade)

        if self.day_manager and self.day_manager.current_day:
            self.day_manager.register_trade()

        self.logger.debug(
            f"Trade added: {trade.get('position_id', 'unknown')} "
            f"to session {self.current_session['session_n']}"
        )

    # =========================================================================
    # CALCULS & LIMITES
    # =========================================================================

    def calculate_session_pnl_pct(self) -> float:
        """Calculate current session PnL percentage."""
        if self.current_session is None:
            raise RuntimeError("Cannot calculate PnL: no active session")

        initial = self.current_session['initial_funds']
        current = self.current_session['current_funds']

        if initial == 0:
            self.logger.critical("Initial funds is 0, cannot calculate PnL %")
            # [v2.5.1 — FIX SM-1] Normalisation casse pour cohérence
            if self.config['general']['mode'].upper() == 'LIVE':
                raise ValueError("Initial funds cannot be 0 in live mode")
            return 0.0

        return ((current - initial) / initial) * 100

    def should_end_session(
        self,
        has_open_position: bool = False,
        open_position_data: Optional[Dict[str, Any]] = None
    ) -> Tuple[bool, Optional[str]]:
        """
        Check if session should end based on limits hierarchy.

        Priority 1 (CRITICAL): Session max_loss critical → Force close
        Priority 2 (TACTICAL): Daily limits → Block trades
        Priority 3 (STRATEGIC): Session limits → End session

        Returns:
            (should_end: bool, reason: str | None)
        """
        if self.current_session is None:
            raise RuntimeError("Cannot check limits: no active session")

        session_pnl_pct = self.calculate_session_pnl_pct()

        # PRIORITY 1: Critical session max_loss
        if session_pnl_pct <= -self.max_loss_pct:
            if has_open_position and open_position_data and self.force_close_on_critical:
                sl_impact_pct    = self._calculate_sl_impact_pct(open_position_data)
                projected_pnl    = session_pnl_pct + sl_impact_pct
                safety_threshold = -self.max_loss_pct * (1 + self.max_loss_safety_margin_pct / 100)

                if projected_pnl <= safety_threshold:
                    if self.log_edge_cases:
                        self.logger.critical(
                            f"SESSION MAX LOSS CRITICAL: {session_pnl_pct:.2f}% "
                            f"(projected: {projected_pnl:.2f}%) - FORCING CLOSURE"
                        )
                    return True, 'max_loss_critical'

            if has_open_position and open_position_data:
                if self.log_edge_cases:
                    self.logger.warning(
                        f"Session max loss reached ({session_pnl_pct:.2f}%) "
                        f"but position open - waiting close"
                    )
                return False, 'waiting_position_close_loss'

            self.logger.warning(f"Session max loss reached: {session_pnl_pct:.2f}%")
            return True, 'max_loss_reached'

        # PRIORITY 2: Daily limits
        if self.day_manager and self.day_manager.current_day:
            should_stop_day, day_reason = self.day_manager.should_stop_trading_today()

            if should_stop_day:
                self.logger.warning(f"Daily limit reached: {day_reason}")
                self.day_manager.block_trading(day_reason)
                with self._lock:
                    self.block_new_trades = True
                return False, f'daily_{day_reason}'

        # PRIORITY 3: Session max_gain
        if session_pnl_pct >= self.max_gain_pct:
            if has_open_position:
                if self.log_edge_cases:
                    self.logger.info(
                        f"Session max gain reached ({session_pnl_pct:.2f}%) "
                        f"but position open - waiting close"
                    )
                with self._lock:
                    self.block_new_trades = True
                return False, 'waiting_position_close_gain'

            self.logger.info(f"Session max gain reached: {session_pnl_pct:.2f}%")
            return True, 'max_gain_reached'

        return False, None

    def _calculate_sl_impact_pct(self, position_data: Dict[str, Any]) -> float:
        """
        Calcule l'impact PnL (en %) si le SL est touché, fees comprises.

        [v2.5.9 — FIX-SM-5] L'ancienne implémentation ignorait les fees :
            - fees_entry : déjà payées à l'ouverture, récupérées depuis
              position_data.get('fees_paid', 0.0)
            - fees_exit  : estimées au taker (size × sl_price × taker_fee_rate)
        Sans ces fees, sl_loss_pct était sous-estimé → max_loss_critical
        se déclenchait trop tard, exposant le capital à un dépassement du
        seuil de sécurité.

        Fallback gracieux : si les champs fees sont absents (position_data
        fourni par un module plus ancien), le comportement pre-fix est conservé.
        """
        # [v2.5.1 — FIX PM-1] sl_price → stop_loss : alignement convention cross-module
        sl_price    = position_data['stop_loss']
        entry_price = position_data['entry_price']
        size        = position_data['size']

        # Perte brute au SL
        sl_distance = abs(sl_price - entry_price)
        sl_loss     = sl_distance * size

        # [v2.5.9 — FIX-SM-5] Fees : entry déjà payées + exit estimées au taker
        fees_entry = position_data.get('fees_paid', 0.0) or 0.0
        taker_fee_rate = (
            self.config.get('position', {}).get('taker_fee', 0.0) or 0.0
        )
        fees_exit  = size * sl_price * taker_fee_rate

        total_loss     = sl_loss + fees_entry + fees_exit
        sl_loss_pct    = (total_loss / self.current_session['initial_funds']) * 100
        return -sl_loss_pct

    def check_midnight_transition(
        self,
        current_time: datetime,
        has_open_position: bool = False
    ) -> Tuple[bool, Optional[str]]:
        """Check and handle midnight transition to next day."""
        if not self.day_manager:
            return False, None

        if self.current_session is None:
            raise RuntimeError("Cannot check midnight: no active session")

        should_transition = self.day_manager.should_transition_to_next_day(current_time)

        if should_transition:
            self._advance_to_next_day()
            return True, 'day_transition'

        return False, None

    def _advance_to_next_day(self) -> None:
        """Advance to next day (internal)."""
        if not self.day_manager or not self.day_manager.current_day:
            return

        if self.current_session is None:
            raise RuntimeError("Cannot advance day: no active session")

        day_summary = self.day_manager.end_day(reason='midnight_transition')

        self.logger.info(
            f"Day ended: {day_summary['date'].strftime('%Y-%m-%d')} | "
            f"PnL: {day_summary['daily_pnl']:.2f} ({day_summary['daily_pnl_pct']:+.2f}%)"
        )

        next_date = day_summary['date'] + timedelta(days=1)

        if next_date <= self.current_session['end_date']:
            current_balance = self.current_session['current_funds']
            self.day_manager.start_new_day(next_date, current_balance)

            session_pnl_pct = self.calculate_session_pnl_pct()

            if -self.max_loss_pct < session_pnl_pct < self.max_gain_pct:
                with self._lock:
                    self.block_new_trades = False
                if self.day_manager.is_trading_blocked:
                    self.day_manager.unblock_trading()

                self.logger.info(
                    f"New day: {next_date.strftime('%Y-%m-%d')} | "
                    f"Balance: {current_balance:.2f} | Trading ENABLED"
                )
            else:
                # [v2.5.9 — FIX-SM-9] block_new_trades NON resetté si max_gain
                # déjà atteint (pnl >= max_gain_pct). L'ancienne implémentation
                # loggait BLOCKED mais ne positionnait pas explicitement le flag
                # dans ce bloc — laissant le flag potentiellement faux si le
                # passage à minuit survenait après un block par Priority 3.
                with self._lock:
                    self.block_new_trades = True
                self.logger.warning(
                    f"New day: {next_date.strftime('%Y-%m-%d')} | "
                    f"Balance: {current_balance:.2f} | Trading BLOCKED "
                    f"(session pnl={session_pnl_pct:.2f}%)"
                )
        else:
            self.logger.info("Session period complete")

    def check_session_expiry(
        self,
        current_time: datetime,
        has_open_position: bool = False,
        position_entry_time: Optional[datetime] = None
    ) -> Tuple[bool, Optional[str]]:
        """Check if session has expired (duration exceeded)."""
        if self.current_session is None:
            raise RuntimeError("Cannot check expiry: no active session")

        # [v2.5.9 — FIX-SM-7] Normalisation UTC avant soustraction.
        # Sans ce guard, un current_time naïf - start_date UTC-aware (ou vice-versa)
        # lève TypeError en Python. _ensure_utc assume UTC si naïf (standard backtest).
        current_time = self._ensure_utc(current_time)
        if position_entry_time is not None:
            position_entry_time = self._ensure_utc(position_entry_time)

        session_duration = current_time - self.current_session['start_date']
        max_duration     = timedelta(days=self.trades_period_days)

        if session_duration >= max_duration:
            if has_open_position and position_entry_time:
                position_age   = current_time - position_entry_time
                grace_duration = timedelta(hours=self.grace_period_hours)

                if self.force_close_on_critical and position_age >= grace_duration:
                    if self.log_edge_cases:
                        self.logger.critical("Session expired - FORCING CLOSURE")
                    return True, 'force_close_position'

                if position_age < grace_duration and self.allow_grace_period:
                    if self.log_edge_cases:
                        self.logger.warning("Session expired but grace period active")
                    return False, 'grace_period'

                if self.log_edge_cases:
                    self.logger.warning("Session expired with open position")
                return True, 'force_close_position'

            self.logger.info(f"Session expired: {session_duration} >= {max_duration}")
            return True, 'session_expired'

        return False, None

    def check_data_exhaustion(
        self,
        current_index: int,
        total_candles: int
    ) -> Tuple[bool, Optional[str]]:
        """
        Check if CSV data exhausted (backtest).

        [v2.5.8 — FIX-SM-4] Correction off-by-one.
        L'ancienne condition `current_index >= total_candles - 1` se déclenchait
        sur la DERNIÈRE candle (index = total_candles - 1), avant que step() la
        traite → la dernière bougie était systématiquement ignorée.
        La condition corrigée `current_index >= total_candles` ne peut être vraie
        qu'APRÈS la dernière candle (index total_candles n'existe pas dans le
        DataFrame 0-based), ce qui garantit que toutes les bougies sont traitées.
        """
        # [v2.5.8 — FIX-SM-4] >= total_candles (et non total_candles - 1)
        if current_index >= total_candles:
            self.logger.info(f"End of CSV data: {current_index + 1}/{total_candles}")
            return True, 'data_exhausted'
        return False, None

    def end_session(self, reason: str = 'completed') -> Dict[str, Any]:
        """
        End current session and calculate metrics.

        Inclut un avertissement si _locked_margin > 0 à la clôture
        (indicateur de position non fermée correctement).

        Returns:
            session_summary dict
        """
        if self.current_session is None:
            raise RuntimeError("Cannot end session: no active session")

        # Avertissement si marge non libérée (position restée ouverte ?)
        if self._locked_margin > 0:
            self.logger.warning(
                f"end_session: _locked_margin={self._locked_margin:.2f} > 0 à la clôture. "
                f"Une position est peut-être restée ouverte sans fermeture propre."
            )

        # End current day if active
        if self.day_manager and self.day_manager.current_day:
            day_summary = self.day_manager.end_day(reason='session_ended')
            self.logger.info(
                f"Last day ended: {day_summary['date'].strftime('%Y-%m-%d')} | "
                f"PnL: {day_summary['daily_pnl']:.2f}"
            )

        # [v2.5.5 — FEAT] Remplissage des jours inactifs (sans candles / sans trades).
        # Cause : should_transition_to_next_day() est déclenché par les candles CSV.
        # Si un jour n'a aucune candle (weekend, gap de données), la transition minuit
        # n'est jamais déclenchée → end_day()/start_new_day() jamais appelés →
        # jours absents de days_summary → total_days < trades_period_days.
        # Correction : on injecte des jours vides pour chaque date manquante entre
        # le dernier jour enregistré et session_end_date (exclusif).
        if self.day_manager:
            session_end_date = self.current_session['end_date'].date()
            last_balance     = self.current_session['current_funds']
            recorded_dates   = {
                s['date'].date() if hasattr(s['date'], 'date') else s['date']
                for s in self.day_manager.days_summary
            }

            # Itérer sur toutes les dates de la session
            cursor = self.current_session['start_date'].date()
            while cursor < session_end_date:
                if cursor not in recorded_dates:
                    # Jour sans candles — injecter un jour vide dans days_summary
                    day_dt = datetime(
                        cursor.year, cursor.month, cursor.day,
                        tzinfo=timezone.utc
                    )
                    empty_day = {
                        'date':             day_dt,
                        'starting_balance': round(last_balance, 8),
                        'ending_balance':   round(last_balance, 8),
                        'daily_pnl':        0.0,
                        'daily_pnl_pct':    0.0,
                        'trades_count':     0,
                        'end_reason':       'no_data',
                        'ended_at':         datetime.now(timezone.utc),
                    }
                    self.day_manager.days_summary.append(empty_day)
                    self.logger.debug(
                        f"Inactive day injected: {cursor.isoformat()} "
                        f"(no candles / no data)"
                    )
                else:
                    # Mettre à jour last_balance depuis le jour enregistré
                    for s in self.day_manager.days_summary:
                        s_date = s['date'].date() if hasattr(s['date'], 'date') else s['date']
                        if s_date == cursor:
                            last_balance = s['ending_balance']
                            break

                cursor += timedelta(days=1)

            # Re-trier days_summary par date pour garantir l'ordre chronologique
            self.day_manager.days_summary.sort(
                key=lambda s: s['date'] if isinstance(s['date'], datetime)
                else datetime(s['date'].year, s['date'].month, s['date'].day, tzinfo=timezone.utc)
            )

        # Mark session as ended
        self.current_session['status']     = SessionStatus.ENDED.value
        self.current_session['end_reason'] = reason
        self.current_session['ended_at']   = datetime.now(timezone.utc)

        # Calculate final PnL
        self.current_session['final_funds'] = self.current_session['current_funds']
        initial = self.current_session['initial_funds']
        final   = self.current_session['final_funds']
        self.current_session['pnl']     = final - initial
        self.current_session['pnl_pct'] = self.calculate_session_pnl_pct()

        # Trade statistics
        trades = self.current_session_trades
        if len(trades) > 0:
            winning = [t for t in trades if t.get('pnl_net', 0) > 0]
            losing  = [t for t in trades if t.get('pnl_net', 0) < 0]

            self.current_session['total_trades']     = len(trades)
            self.current_session['winning_trades']   = len(winning)
            self.current_session['losing_trades']    = len(losing)
            self.current_session['win_rate']         = len(winning) / len(trades) * 100
            self.current_session['total_trade_pnl']  = sum(
                t.get('pnl_net', 0) for t in trades
            )
        else:
            self.current_session['total_trades']    = 0
            self.current_session['winning_trades']  = 0
            self.current_session['losing_trades']   = 0
            self.current_session['win_rate']        = 0.0
            self.current_session['total_trade_pnl'] = 0.0

        # Duration metrics 
        actual_duration = self.current_session['ended_at'] - self.current_session['start_date']
        actual_days     = actual_duration.days

        self.current_session['actual_days_traded'] = actual_days
        self.current_session['configured_days']    = self.trades_period_days
        self.current_session['duration_ratio']     = (
            actual_days / self.trades_period_days if self.trades_period_days > 0 else 0.0
        )

        is_premature = (
            actual_days < self.trades_period_days and
            reason in ['max_loss_reached', 'max_gain_reached', 'max_loss_critical']
        )
        self.current_session['is_premature']    = is_premature
        self.current_session['premature_reason'] = reason if is_premature else None

        if is_premature:
            self.logger.warning(
                f"Session PREMATURE: {actual_days}/{self.trades_period_days} days ({reason})"
            )

        # Save daily summaries
        if self.day_manager and self.day_manager.get_total_days() > 0:
            self.day_manager.save_days_summary(self.current_session['session_n'])

        # [v2.5.7 — FIX-SM-3] Création du summary et mise à jour de l'état AVANT la
        # persistance disque.
        #
        # BUG CORRIGÉ : avant ce fix, l'ordre était :
        #   1. _save_trades_to_disk()        ← pouvait lever une exception
        #   2. session_summary = {...}
        #   3. sessions_summary.append()     ← jamais atteint si (1) échoue
        #   4. current_session = None
        #
        # Conséquence : si _save_trades_to_disk() levait une TypeError (ex: type numpy
        # non sérialisable dans un trade_record contenant un champ imbriqué tel que
        # `origine_signal`), la machine d'état restait incohérente :
        #   - current_session non nettoyé (status='ended', non None)
        #   - sessions_summary vide
        # FIX-SM-2 (create_session) détectait l'état 'ended' et forçait le reset de
        # current_session, mais sessions_summary restait vide → get_initial_funds(N+1)
        # levait : "session N not completed" → crash du pipeline multi-sessions.
        #
        # CORRECTION : on finalise la machine d'état (summary + cleanup) AVANT la
        # persistance disque. La sauvegarde est désormais non-fatale : un échec est
        # loggé en ERROR mais n'interrompt plus le pipeline.

        # ── Création du summary ───────────────────────────────────────────────
        session_summary = {
            'session_id':        self.current_session['session_id'],  # [v2.5.3]
            'session_n':         self.current_session['session_n'],
            'start_date':        format_datetime(self.current_session['start_date'], "%Y-%m-%d"),
            'end_date':          format_datetime(self.current_session['end_date'], "%Y-%m-%d"),
            'initial_funds':     self.current_session['initial_funds'],
            # [v2.5.4 — FIX-SM-BUG2] Alias requis par analytics_engine.py (ligne ~307).
            # analytics_engine cherche session_summary.get('initial_capital') →
            # clé absente → None → fallback hardcodé 1_000.0 USDT.
            # initial_funds et initial_capital sont identiques : capital de départ session.
            'initial_capital':   self.current_session['initial_funds'],
            'final_funds':       self.current_session['final_funds'],
            'pnl':               self.current_session['pnl'],
            'pnl_pct':           self.current_session['pnl_pct'],
            'total_trades':      self.current_session['total_trades'],
            'winning_trades':    self.current_session.get('winning_trades', 0),
            'losing_trades':     self.current_session.get('losing_trades', 0),
            'win_rate':          self.current_session['win_rate'],
            'end_reason':        self.current_session['end_reason'],
            'actual_days_traded': self.current_session['actual_days_traded'],
            'configured_days':   self.current_session['configured_days'],
            'duration_ratio':    self.current_session['duration_ratio'],
            'is_premature':      self.current_session['is_premature'],
            'premature_reason':  self.current_session['premature_reason']
        }

        # ── Mise à jour de la machine d'état (atomique avant persistance disque) ──
        # Ordre garanti : append + cleanup se font TOUJOURS, même si la sauvegarde
        # disque échoue ensuite.
        self.sessions_summary.append(session_summary)

        # Capture les trades AVANT clear() pour la sauvegarde disque
        trades_to_save = list(self.current_session_trades)
        session_n_to_save = self.current_session['session_n']

        # Cleanup
        with self._lock:
            self._locked_margin   = 0.0   # Reset propre
            self.block_new_trades = False

        self.current_session_trades.clear()
        self.current_session  = None

        self.logger.log_session_end(
            session_n = session_summary['session_n'],
            pnl       = session_summary['pnl'],
            pnl_pct   = session_summary['pnl_pct'],
            trades    = session_summary['total_trades'],
            win_rate  = session_summary['win_rate'],
            reason    = reason
        )

        # ── Persistance disque (non-fatale pour le pipeline) ─────────────────
        # Un échec ici ne compromet plus la machine d'état : la session est déjà
        # marquée comme terminée et enregistrée dans sessions_summary.
        if len(trades_to_save) > 0:
            try:
                self._save_trades_to_disk(session_n_to_save, trades_to_save)
            except Exception as save_exc:
                self.logger.error(
                    f"[FIX-SM-3] Échec sauvegarde trades session {session_n_to_save} "
                    f"(non-fatal — pipeline continue) : "
                    f"{type(save_exc).__name__}: {save_exc}"
                )

        return session_summary

    # =========================================================================
    # PERSISTANCE & EXPORT
    # =========================================================================

    def _save_trades_to_disk(
        self,
        session_n: int,
        trades: List[Dict[str, Any]]
    ) -> None:
        """Save session trades to JSON with backup fallback.

        [v2.5.7 — FIX-SM-3] Sérialisation profonde ajoutée via _deep_serialize().
        L'ancienne implémentation ne convertissait que les datetime de premier niveau.
        Les champs imbriqués (ex: origine_signal contenant des types numpy retournés
        par les indicateurs techniques) levaient un TypeError dans json.dump, ce qui
        interrompait end_session() avant sessions_summary.append() — machine d'état
        incohérente → crash pipeline multi-sessions (FIX-SM-3).
        """
        project_root_path = get_project_root()
        save_path         = project_root_path / self.auto_save_path
        ensure_directory(save_path)

        filename        = f"session_{session_n:03d}_trades.json"
        filepath        = save_path / filename
        backup_filepath = save_path / f"session_{session_n:03d}_trades.backup.json"

        # [v2.5.7 — FIX-SM-3] Sérialisation profonde : convertit récursivement tous
        # les types non-sérialisables (numpy scalaires, datetime imbriqués, Enum, etc.)
        # en types Python natifs JSON-compatibles.
        trades_serializable = [self._deep_serialize(trade) for trade in trades]

        data = {
            'session_n':    session_n,
            'saved_at':     format_datetime(datetime.now(timezone.utc), "%Y-%m-%d %H:%M:%S"),
            'total_trades': len(trades_serializable),
            'trades':       trades_serializable
        }

        try:
            write_json(data, filepath, indent=2)
            self.logger.info(f"Session {session_n} trades saved: {filepath}")
        except Exception as primary_error:
            self.logger.critical(f"Failed to save trades: {primary_error}")
            try:
                write_json(data, backup_filepath, indent=2)
                self.logger.warning(f"Trades saved to BACKUP: {backup_filepath}")
            except Exception as backup_error:
                self.logger.critical(
                    f"CRITICAL: Cannot save trades!\n"
                    f"Primary: {primary_error}\nBackup: {backup_error}"
                )
                raise Exception(f"CRITICAL: Cannot save session {session_n} trades")

    def _deep_serialize(self, obj: Any) -> Any:
        """
        Convertit récursivement un objet en types Python natifs JSON-compatibles.

        [v2.5.7 — FIX-SM-3] Nécessaire pour sérialiser des champs imbriqués tels
        que `origine_signal` (snapshot marché) qui peuvent contenir des types numpy
        ou pandas retournés par les indicateurs techniques.

        Conversions appliquées :
            datetime (aware/naïf)          → str ISO-8601
            numpy.integer (int8/16/32/64)  → int
            numpy.floating (float32/64)    → float (None si NaN/Inf)
            numpy.bool_                    → bool
            numpy.ndarray                  → list (récursif)
            pandas.Timestamp               → str ISO-8601
            pandas.NA / numpy.nan          → None
            Enum                           → .value
            dict                           → dict (récursif sur valeurs)
            list / tuple                   → list (récursif)
            str / int / float / bool / None → inchangé

        Args:
            obj: Valeur à convertir (type quelconque).

        Returns:
            Valeur JSON-serializable native Python.
        """
        import math

        # ── Types natifs Python — retour direct ──────────────────────────────
        if obj is None or isinstance(obj, (bool, str)):
            return obj

        if isinstance(obj, int):
            return obj

        if isinstance(obj, float):
            # NaN et Inf ne sont pas JSON-serializable
            return None if (math.isnan(obj) or math.isinf(obj)) else obj

        # ── datetime / Timestamp ─────────────────────────────────────────────
        if isinstance(obj, datetime):
            if obj.tzinfo is None:
                obj = obj.replace(tzinfo=timezone.utc)
            return obj.strftime("%Y-%m-%d %H:%M:%S")

        # ── numpy types (optionnel : numpy peut ne pas être installé) ────────
        try:
            import numpy as np

            if isinstance(obj, np.bool_):
                return bool(obj)

            if isinstance(obj, np.integer):
                return int(obj)

            if isinstance(obj, np.floating):
                v = float(obj)
                return None if (math.isnan(v) or math.isinf(v)) else v

            if isinstance(obj, np.ndarray):
                return [self._deep_serialize(x) for x in obj.tolist()]

            # np.nan est en réalité un float Python — déjà couvert ci-dessus
            # mais on garde ce guard pour np.nan explicite
            if obj is np.nan:
                return None

        except ImportError:
            pass

        # ── pandas types ─────────────────────────────────────────────────────
        try:
            import pandas as pd

            if isinstance(obj, pd.Timestamp):
                if obj.tzinfo is None:
                    obj = obj.tz_localize('UTC')
                return obj.strftime("%Y-%m-%d %H:%M:%S")

            if obj is pd.NA or obj is pd.NaT:
                return None

        except ImportError:
            pass

        # ── Enum ──────────────────────────────────────────────────────────────
        from enum import Enum as _Enum
        if isinstance(obj, _Enum):
            return self._deep_serialize(obj.value)

        # ── Structures récursives ────────────────────────────────────────────
        if isinstance(obj, dict):
            return {k: self._deep_serialize(v) for k, v in obj.items()}

        if isinstance(obj, (list, tuple)):
            return [self._deep_serialize(item) for item in obj]

        # ── Fallback ultime : str() pour tout type inconnu ───────────────────
        # Garantit que json.dump ne lèvera jamais de TypeError.
        try:
            return str(obj)
        except Exception:
            return None

    def get_all_sessions_summary(self) -> List[Dict[str, Any]]:
        """Get all session summaries (copy)."""
        return self.sessions_summary.copy()

    def get_current_session(self) -> Optional[Dict[str, Any]]:
        """Get current session (deepcopy)."""
        return deepcopy(self.current_session) if self.current_session else None

    def get_session_summary(self, session_n: int) -> Optional[Dict[str, Any]]:
        """Get specific session summary (deepcopy)."""
        for summary in self.sessions_summary:
            if summary['session_n'] == session_n:
                return deepcopy(summary)
        return None

    def is_trading_allowed(self) -> bool:
        """Check if trading is allowed (not blocked)."""
        with self._lock:
            if self.block_new_trades:
                return False
            if self.day_manager and self.day_manager.is_trading_blocked:
                return False
            return True

    def save_all_summaries(self) -> None:
        """Save all session summaries to JSON."""
        if len(self.sessions_summary) == 0:
            self.logger.warning("No sessions to save")
            return

        project_root_path = get_project_root()
        save_path         = project_root_path / self.summary_save_path
        ensure_directory(save_path)

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename  = f"all_sessions_{timestamp}.json"
        filepath  = save_path / filename

        try:
            data = {
                'saved_at':             format_datetime(datetime.now(timezone.utc), "%Y-%m-%d %H:%M:%S"),
                'total_sessions':       len(self.sessions_summary),
                'reset_capital_mode':   self.reset_capital,
                'daily_limits_enabled': self.daily_limits_enabled,
                'version':              _VERSION,  # [v2.5.9 — FIX-SM-6] était '2.5.2' hardcodé
                'sessions':             self.sessions_summary
            }
            write_json(data, filepath, indent=2)
            self.logger.info(f"Session summaries saved: {filepath}")
        except Exception as e:
            self.logger.error(f"Failed to save summaries: {e}")

    def save_summaries_csv(self, filepath: Optional[Path] = None) -> None:
        """Export session summaries to CSV."""
        if len(self.sessions_summary) == 0:
            self.logger.warning("No sessions to save (CSV)")
            return

        if filepath is None:
            project_root_path = get_project_root()
            save_path         = project_root_path / self.summary_save_path
            ensure_directory(save_path)
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            filepath  = save_path / f"all_sessions_{timestamp}.csv"

        fieldnames = [
            'session_n', 'start_date', 'end_date', 'initial_funds', 'final_funds',
            'pnl', 'pnl_pct', 'total_trades', 'winning_trades', 'losing_trades',
            'win_rate', 'end_reason', 'actual_days_traded', 'configured_days',
            'duration_ratio', 'is_premature', 'premature_reason'
        ]

        try:
            with open(filepath, 'w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                for summary in self.sessions_summary:
                    row = {field: summary.get(field, '') for field in fieldnames}
                    writer.writerow(row)
            self.logger.info(f"Session summaries saved (CSV): {filepath}")
        except Exception as e:
            self.logger.error(f"Failed to save CSV: {e}")

    def reset(self) -> None:
        """Reset SessionManager (for tests)."""
        with self._lock:
            self.current_session  = None
            self._locked_margin   = 0.0
            self.block_new_trades = False

        self.sessions_summary.clear()
        self.current_session_trades.clear()

        if self.day_manager:
            self.day_manager.reset()

        self.logger.info("SessionManager reset")

    def __repr__(self) -> str:
        balance   = self.current_session['current_funds'] if self.current_session else 0.0
        available = self.get_capital_available() if self.current_session else 0.0
        return (
            f"SessionManager(session={self.current_session['session_n'] if self.current_session else None}, "
            f"balance={balance:.2f}, available={available:.2f}, "
            f"locked={self._locked_margin:.2f})"
        )

# FIN DU MODULE
