"""
BULLET-1 - Day Trades Manager Module
=====================================

Gestion des limites quotidiennes de trading (00h00 - 23h59).
Module CORE pour contrôle tactique intra-day.

Fonctionnalités :
- Tracking métriques par jour (PnL, trades count, balance)
- Limites quotidiennes (max_loss_day, max_gain_day, max_trades_day)
- Transitions automatiques jour suivant (minuit)
- Sauvegarde automatique résumés quotidiens vers JSON avec backup
- Validation stricte des transitions
- Support timezone UTC (mode BACKTEST)

Version: 2.2.2
Date: 2026-03-15
Author: FuegoDev
Dépendances: helpers.py (module 1), logger.py (module 3), config_loader.py (module 5)
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple
from copy import deepcopy
from enum import Enum
import sys

# [v2.2.2 — FIX-DTM-1] Pattern direct unifié BULLET-1 — remplace find_project_root().
# Même correction que FIX-ENG-6, FIX-AE-1, FIX-RG-4.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Imports depuis modules BULLET-1
from src.utils.helpers import (
    format_datetime,
    ensure_directory,
    write_json,
    get_project_root
)
from src.utils.logger import BulletLogger

#: Version du module — utilisée dans les exports JSON et les logs.
_VERSION = "2.2.2"  # [v2.2.2 — FIX-DTM-4]


# ============================================================================
# ENUMS
# ============================================================================

class DayStatus(Enum):
    """
    Statuts possibles d'un jour de trading.
    
    Attributes:
        ACTIVE: Jour en cours
        ENDED: Jour terminé
        BLOCKED: Jour bloqué (limites atteintes, attente minuit)
    """
    ACTIVE = "active"
    ENDED = "ended"
    BLOCKED = "blocked"


# ============================================================================
# CLASSE DAY TRADES MANAGER
# ============================================================================

class DayTradesManager:
    """
    Gestion des limites quotidiennes de trading (tracker léger).
    
    ⚠️ DESIGN: Single-threaded. Non thread-safe.
    
    🎯 RESPONSABILITÉ UNIQUE v2.2.1:
    DayTradesManager gère UNIQUEMENT les jours, leurs limites et métriques.
    Il ne gère PAS le capital (délégué à SessionManager).
    
    🌍 TIMEZONE v2.2.1 (MODE BACKTEST):
    Tous les datetime sont UTC-aware ou assumés UTC si naïfs.
    Validation automatique timezone sur tous les inputs.
    
    🧹 SIMPLICITÉ v2.2.1 (MODE BACKTEST):
    Zero code mort. Chaque paramètre config est utilisé à 100%.
    Carry-over automatique (pas de config nécessaire en BACKTEST).
    Pas de flag 'enabled' global (granularité avec max_X = None).
    
    📋 CONFIGURATION MODE BACKTEST (4 paramètres):
    - max_loss_per_day_pct : Perte max par jour % (None = désactivé)
    - max_gain_per_day_pct : Gain max par jour % (None = désactivé)
    - max_trades_per_day : Nombre max trades (None = illimité)
    - auto_save_days_path : Chemin sauvegarde JSON
    
    📋 FUTURS PARAMÈTRES PAPER/LIVE (NON IMPLÉMENTÉS):
    Ces paramètres seront ajoutés pour les modes PAPER/LIVE quand nécessaire :
    
    - reset_time (str): Heure reset quotidien (ex: '17:00:00' pour 17h EST)
      → Utile en LIVE si trading 24h sur marchés crypto
      → En BACKTEST: toujours minuit UTC (pas besoin config)
    
    - timezone (str): Timezone pour reset (ex: 'America/New_York', 'Europe/Paris')
      → Utile en LIVE/PAPER pour aligner sur timezone utilisateur
      → En BACKTEST: toujours UTC (données CSV standard)
    
    - grace_period_minutes (int): Minutes grace period autour minuit
      → Utile en LIVE si bot redémarre pile à minuit (éviter double transition)
      → En BACKTEST: timestamps déterministes (pas de redémarrage possible)
    
    - carry_over_open_positions (bool): Autoriser transition si position ouverte
      → Utile en LIVE/PAPER: si False, attendre fermeture avant minuit
      → En BACKTEST: toujours True (timestamps déterministes, transition obligatoire)
    
    Concepts clés :
    - Day : Période de 00h00 UTC à 23h59 UTC (changement de date pure)
    - starting_balance : Capital au DÉBUT du jour (= current_funds session)
    - current_balance : Capital ACTUEL du jour (mis à jour après chaque opération)
    - daily_pnl : PnL du jour = current_balance - starting_balance (calculé à la demande)
    - daily_pnl_pct : PnL % du jour (calculé à la demande via property)
    - trades_count : Nombre de trades exécutés dans la journée
    
    🔄 WORKFLOW MODE BACKTEST v2.2.1:
    1. SessionManager démarre jour via start_new_day(date, balance)
    2. Engine itère candles → should_transition_to_next_day(timestamp)
    3. Si transition → _transition_new_day(new_date, balance)
    4. OrderSimulator met à jour balance → update_day_balance()
    5. Strategy enregistre trade → register_trade() (retourne bool)
    6. SessionManager vérifie limites → should_stop_trading_today() (QUERY)
    7. Si limite atteinte → block_trading(reason) (COMMAND)
    8. Fin session → end_day()
    
    Features v2.2.1:
    - 🧹 Zero code mort (carry_over, enabled supprimés)
    - ✨ Config ultra-simple (4 paramètres, tous utilisés)
    - ✨ Timezone UTC explicite (100% conformité BACKTEST)
    - ✨ Détection minuit simplifiée (comparaison dates)
    - ✨ Interface should_transition_to_next_day() pour BACKTEST
    - ✨ Méthode privée _transition_new_day()
    - ✨ Validation timezone automatique
    - ✨ Calcul PnL optimisé (properties)
    - ✨ Séparation CQRS (query vs command)
    - ✨ Sauvegarde avec backup automatique
    - ✨ Documentation futurs besoins PAPER/LIVE
    
    Attributes:
        config (dict): Configuration daily_limits
        logger (BulletLogger): Logger centralisé
        max_loss_per_day_pct (float|None): Perte max par jour % (None = désactivé)
        max_gain_per_day_pct (float|None): Gain max par jour % (None = désactivé)
        max_trades_per_day (int|None): Nombre max trades par jour (None = illimité)
        timezone (timezone): Timezone UTC (mode BACKTEST)
        current_day (dict|None): Jour actif courant
        days_summary (list): Résumés de tous les jours
        auto_save_path (str): Chemin sauvegarde résumés quotidiens
    
    Examples:
        >>> # MODE BACKTEST - Initialisation (simple)
        >>> daily_config = {
        ...     'max_loss_per_day_pct': 3.0,
        ...     'max_gain_per_day_pct': 10.0,
        ...     'max_trades_per_day': 5,
        ...     'auto_save_days_path': 'results/backtests/sessions/days/'
        ... }
        >>> day_mgr = DayTradesManager(daily_config, logger)
        >>> 
        >>> # Démarrer jour (UTC)
        >>> day_mgr.start_new_day(
        ...     datetime(2025, 5, 1, tzinfo=timezone.utc),
        ...     100.0
        ... )
        >>> 
        >>> # Boucle candles (BACKTEST)
        >>> for candle in candles:
        ...     if day_mgr.should_transition_to_next_day(candle['timestamp']):
        ...         day_mgr._transition_new_day(candle['timestamp'], balance)
        >>> 
        >>> # Mise à jour balance
        >>> day_mgr.update_day_balance(103.5)
        >>> 
        >>> # Enregistrer trade
        >>> if day_mgr.register_trade():
        ...     print("Trade enregistré")
        >>> 
        >>> # Vérifier limites
        >>> should_stop, reason = day_mgr.should_stop_trading_today()
        >>> if should_stop:
        ...     day_mgr.block_trading(reason)
    """
    
    def __init__(self, config: dict, logger: Optional[BulletLogger] = None):
        """
        Initialiser DayTradesManager.
        
        MODE BACKTEST v2.2.1:
        Configuration ultra-simple, zero code mort.
        Chaque paramètre est utilisé à 100%.
        
        Args:
            config: Configuration daily_limits avec 4 paramètres:
                - max_loss_per_day_pct (float|None): Perte max % (None = désactivé)
                - max_gain_per_day_pct (float|None): Gain max % (None = désactivé)
                - max_trades_per_day (int|None): Max trades (None = illimité)
                - auto_save_days_path (str): Chemin sauvegarde JSON
            logger: Logger centralisé (optionnel, créé si non fourni)
        
        Raises:
            ValueError: Si configuration invalide
        
        Examples:
            >>> # Configuration minimale BACKTEST
            >>> daily_config = {
            ...     'max_loss_per_day_pct': 3.0,
            ...     'max_gain_per_day_pct': 10.0,
            ...     'max_trades_per_day': 5,
            ...     'auto_save_days_path': 'results/backtests/sessions/days/'
            ... }
            >>> day_mgr = DayTradesManager(daily_config, logger)
            
            >>> # Désactiver une limite (None)
            >>> daily_config = {
            ...     'max_loss_per_day_pct': 3.0,
            ...     'max_gain_per_day_pct': None,  # Pas de limite gain
            ...     'max_trades_per_day': None,    # Illimité
            ...     'auto_save_days_path': 'results/backtests/sessions/days/'
            ... }
        """
        self.logger = logger if logger else BulletLogger()
        self.config = config
        
        # ====================================================================
        # Paramètres limites quotidiennes (None = désactivé/illimité)
        # ====================================================================
        
        self.max_loss_per_day_pct = config.get('max_loss_per_day_pct', 5.0)
        self.max_gain_per_day_pct = config.get('max_gain_per_day_pct', 15.0)
        self.max_trades_per_day = config.get('max_trades_per_day', None)
        
        # ====================================================================
        # Timezone UTC pour mode BACKTEST
        # ====================================================================
        
        self.timezone = timezone.utc
        
        # ====================================================================
        # État jours
        # ====================================================================
        
        self.current_day: Optional[Dict[str, Any]] = None
        self.days_summary: List[Dict[str, Any]] = []
        
        # ====================================================================
        # Chemin sauvegarde
        # ====================================================================
        
        self.auto_save_path = config.get(
            'auto_save_days_path',
            'results/backtests/sessions/days/'
        )
        
        # ====================================================================
        # Logger initialisation
        # ====================================================================
        
        self.logger.info(
            f"DayTradesManager v2.2.1 (BACKTEST) initialized: "
            f"max_loss={self.max_loss_per_day_pct if self.max_loss_per_day_pct else 'disabled'}%, "
            f"max_gain={self.max_gain_per_day_pct if self.max_gain_per_day_pct else 'disabled'}%, "
            f"max_trades={self.max_trades_per_day if self.max_trades_per_day else 'unlimited'}, "
            f"timezone=UTC"
        )
    
    # ========================================================================
    # HELPERS TIMEZONE
    # ========================================================================
    
    def _ensure_utc(self, dt: datetime) -> datetime:
        """
        Assurer qu'un datetime est UTC-aware.
        
        MODE BACKTEST:
        - Si datetime naïf → Assumer UTC (standard backtest CSV)
        - Si datetime aware mais non-UTC → Logger warning (mais accepter)
        - Retourner toujours UTC-aware datetime
        
        Args:
            dt: Datetime à valider
        
        Returns:
            datetime: Datetime UTC-aware
        
        Examples:
            >>> # Naïf → assumé UTC
            >>> dt_utc = self._ensure_utc(datetime(2025, 5, 1, 12, 0))
            >>> print(dt_utc.tzinfo)
            UTC
            
            >>> # Déjà UTC → inchangé
            >>> dt_utc = self._ensure_utc(datetime(2025, 5, 1, 12, 0, tzinfo=timezone.utc))
        """
        if dt.tzinfo is None:
            # Naïf → Assumer UTC (standard backtest)
            return dt.replace(tzinfo=timezone.utc)
        elif dt.tzinfo != timezone.utc:
            # Non-UTC → Logger warning (mais accepter pour compatibilité)
            self.logger.warning(
                f"⚠️ Non-UTC timezone detected: {dt.tzinfo}. "
                f"BACKTEST mode expects UTC timestamps. "
                f"Timestamp: {dt.isoformat()}"
            )
            return dt.astimezone(timezone.utc)
        else:
            # Déjà UTC
            return dt
    
    def _normalize_to_date(self, dt: datetime) -> datetime:
        """
        Normaliser datetime à date pure UTC (00:00:00).
        
        Args:
            dt: Datetime à normaliser
        
        Returns:
            datetime: Date normalisée (00:00:00 UTC)
        
        Examples:
            >>> dt = datetime(2025, 5, 1, 14, 30, 45, tzinfo=timezone.utc)
            >>> normalized = self._normalize_to_date(dt)
            >>> print(normalized)
            2025-05-01 00:00:00+00:00
        """
        dt_utc = self._ensure_utc(dt)
        return datetime(
            dt_utc.year,
            dt_utc.month,
            dt_utc.day,
            tzinfo=timezone.utc
        )
    
    # ========================================================================
    # PROPERTIES - CALCUL À LA DEMANDE
    # ========================================================================
    
    @property
    def daily_pnl(self) -> float:
        """
        Calculer PnL quotidien à la demande.
        
        Returns:
            float: PnL quotidien en USDT
        """
        if self.current_day is None:
            return 0.0
        
        return self.current_day['current_balance'] - self.current_day['starting_balance']
    
    @property
    def daily_pnl_pct(self) -> float:
        """
        Calculer PnL % quotidien à la demande.
        
        Returns:
            float: PnL quotidien en %
        """
        if self.current_day is None or self.current_day['starting_balance'] == 0:
            return 0.0
        
        return (self.daily_pnl / self.current_day['starting_balance']) * 100
    
    @property
    def trades_today(self) -> int:
        """
        Obtenir nombre de trades aujourd'hui.
        
        Returns:
            int: Nombre de trades
        """
        return self.current_day['trades_count'] if self.current_day else 0
    
    @property
    def is_trading_blocked(self) -> bool:
        """
        Vérifier si trading est bloqué aujourd'hui.
        
        Returns:
            bool: True si bloqué
        """
        return (
            self.current_day is not None and 
            self.current_day['status'] == DayStatus.BLOCKED.value
        )
    
    @property
    def current_date(self) -> Optional[datetime]:
        """
        Obtenir date du jour actif (UTC).
        
        Returns:
            datetime | None: Date du jour actif (UTC-aware)
        """
        return self.current_day['date'] if self.current_day else None
    
    # ========================================================================
    # CRÉATION & GESTION JOURS
    # ========================================================================
    
    def start_new_day(
        self,
        date: datetime,
        starting_balance: float
    ) -> Dict[str, Any]:
        """
        Démarrer nouveau jour avec capital de départ.
        
        Cette méthode est appelée par SessionManager:
        - Au début de la session (premier jour)
        - À chaque transition minuit (si session continue)
        
        Args:
            date: Date du jour (datetime UTC, heure ignorée normalisée à 00:00)
            starting_balance: Capital au début du jour
        
        Returns:
            dict: Jour créé avec tous les champs
        
        Raises:
            ValueError: Si jour déjà actif ou paramètres invalides
        
        Examples:
            >>> # Avec timezone UTC explicite (recommandé)
            >>> day = day_mgr.start_new_day(
            ...     date=datetime(2025, 5, 1, tzinfo=timezone.utc),
            ...     starting_balance=100.0
            ... )
            
            >>> # Avec datetime naïf (assumé UTC pour backtest)
            >>> day = day_mgr.start_new_day(
            ...     date=datetime(2025, 5, 1, 14, 30),  # Heure ignorée
            ...     starting_balance=100.0
            ... )
        """
        # Vérifier pas de jour déjà actif
        if (self.current_day is not None and 
            self.current_day.get('status') == DayStatus.ACTIVE.value):
            raise ValueError(
                f"Cannot start new day: "
                f"Day {self.current_day['date'].strftime('%Y-%m-%d')} still active. "
                f"Call end_day() first."
            )
        
        # Vérifier balance positive
        if starting_balance < 0:
            raise ValueError(
                f"starting_balance cannot be negative: {starting_balance}"
            )

        # [v2.2.2 — FIX-DTM-5] Warning si solde de départ nul.
        # Solde zéro accepté (bug non-bloquant) mais anormal en backtest :
        # peut signifier capital épuisé ou erreur d'appel dans SessionManager.
        if starting_balance == 0.0:
            self.logger.warning(
                "⚠️  start_new_day: starting_balance=0.0 — "
                "capital épuisé ou erreur d'appel ? "
                "daily_pnl_pct retournera 0.0 (division par zéro évitée)."
            )
        
        # Normaliser date à UTC 00:00:00
        day_date = self._normalize_to_date(date)
        
        # Créer jour
        day = {
            'date': day_date,
            'starting_balance': starting_balance,
            'current_balance': starting_balance,
            'trades_count': 0,
            'status': DayStatus.ACTIVE.value,
            'created_at': datetime.now(timezone.utc),
            'ended_at': None,
            'end_reason': None
        }
        
        # Stocker comme jour courant
        self.current_day = day
        
        # Logger démarrage
        self.logger.info(
            f"📅 Day started: {day_date.strftime('%Y-%m-%d')} UTC | "
            f"Starting balance: {starting_balance:.2f} USDT | "
            f"Status: ACTIVE"
        )
        
        return day
    
    def update_day_balance(self, new_balance: float):
        """
        Mettre à jour le solde du jour courant.
        
        Cette méthode est appelée par SessionManager après chaque
        mise à jour de session_balance.
        
        Args:
            new_balance: Nouveau solde du compte
        
        Raises:
            RuntimeError: Si aucun jour actif
        """
        if self.current_day is None:
            raise RuntimeError("Cannot update balance: no active day")
        
        old_balance = self.current_day['current_balance']
        self.current_day['current_balance'] = new_balance
        
        delta = new_balance - old_balance
        delta_sign = "+" if delta >= 0 else ""
        
        self.logger.debug(
            f"Day {self.current_day['date'].strftime('%Y-%m-%d')} balance updated: "
            f"{old_balance:.2f} → {new_balance:.2f} USDT ({delta_sign}{delta:.2f}) | "
            f"Daily PnL: {self.daily_pnl:.2f} USDT ({self.daily_pnl_pct:+.2f}%)"
        )
    
    def register_trade(self) -> bool:
        """
        Enregistrer un trade (incrémenter compteur).
        
        Valide la limite avant d'incrémenter.
        
        Returns:
            bool: True si trade enregistré, False si limite atteinte
        
        Raises:
            RuntimeError: Si aucun jour actif
        """
        if self.current_day is None:
            raise RuntimeError("Cannot register trade: no active day")
        
        # Vérifier limite AVANT d'incrémenter
        if self.max_trades_per_day is not None:
            if self.current_day['trades_count'] >= self.max_trades_per_day:
                self.logger.warning(
                    f"⚠️  Cannot register trade: daily limit reached "
                    f"({self.max_trades_per_day} trades)"
                )
                return False
        
        self.current_day['trades_count'] += 1
        
        self.logger.debug(
            f"Trade registered for day {self.current_day['date'].strftime('%Y-%m-%d')}: "
            f"count={self.current_day['trades_count']}"
            f"{f'/{self.max_trades_per_day}' if self.max_trades_per_day else ''}"
        )
        
        return True
    
    # ========================================================================
    # INTERFACE BACKTEST - DÉTECTION TRANSITION JOUR
    # ========================================================================
    
    def should_transition_to_next_day(self, current_timestamp: datetime) -> bool:
        """
        Vérifier si transition vers jour suivant nécessaire (BACKTEST).
        
        MODE BACKTEST:
        Simple comparaison de dates (current_date != day_date).
        Conforme à 100% aux spécifications BACKTEST.
        
        Carry-over AUTOMATIQUE et OBLIGATOIRE:
        En mode BACKTEST, les timestamps sont déterministes (candles CSV).
        Si position ouverte à 23:59 et prochain candle à 00:00, on DOIT
        transitionner immédiatement. Le carry-over est automatique.
        
        FUTURES MODES (PAPER/LIVE - NON IMPLÉMENTÉS):
        En mode PAPER/LIVE, cette méthode sera étendue pour:
        - Vérifier grace_period (éviter double transition si bot redémarre)
        - Vérifier carry_over_open_positions (bloquer transition si position ouverte)
        - Comparer avec reset_time configurable (différent de minuit)
        - Support multi-timezone (conversion timezone utilisateur)
        
        Args:
            current_timestamp: Timestamp actuel (candle CSV)
        
        Returns:
            bool: True si changement de jour détecté
        
        Examples:
            >>> # Dans engine backtest (iteration candles)
            >>> for candle in candles:
            ...     if day_mgr.should_transition_to_next_day(candle['timestamp']):
            ...         # Changement de jour détecté
            ...         day_mgr._transition_new_day(
            ...             candle['timestamp'],
            ...             current_balance
            ...         )
        """
        if self.current_day is None:
            return False
        
        # Simple comparaison dates (BACKTEST spec)
        current_timestamp_utc = self._ensure_utc(current_timestamp)
        current_date = current_timestamp_utc.date()
        day_date = self.current_day['date'].date()
        
        return current_date != day_date
    
    def _transition_new_day(
        self,
        new_date: datetime,
        current_balance: float
    ):
        """
        Effectuer transition vers nouveau jour (privée).
        
        Cette méthode est appelée par Engine/SessionManager après détection
        changement de jour via should_transition_to_next_day().
        
        Workflow:
        1. Terminer jour courant (end_day)
        2. Démarrer nouveau jour (start_new_day)
        
        Args:
            new_date: Date du nouveau jour (datetime UTC)
            current_balance: Balance actuelle (= nouveau starting_balance)
        
        Examples:
            >>> # Appelé par engine après détection changement jour
            >>> if day_mgr.should_transition_to_next_day(candle['timestamp']):
            ...     day_mgr._transition_new_day(
            ...         candle['timestamp'],
            ...         session_mgr.current_funds
            ...     )
        """
        old_date = self.current_day['date'] if self.current_day else None
        
        # Terminer jour courant
        if self.current_day is not None:
            self.end_day(reason='midnight_transition')
        
        # Démarrer nouveau jour
        self.start_new_day(new_date, current_balance)
        
        # Logger transition
        if old_date:
            self.logger.info(
                f"🕐 Day transition completed: "
                f"{old_date.strftime('%Y-%m-%d')} → "
                f"{self.current_day['date'].strftime('%Y-%m-%d')} UTC"
            )
    
    # ========================================================================
    # SÉPARATION CQRS - QUERY vs COMMAND
    # ========================================================================
    
    def should_stop_trading_today(self) -> Tuple[bool, Optional[str]]:
        """
        Vérifier si trading doit s'arrêter aujourd'hui (limites atteintes).
        
        QUERY PURE - Pas de side effect, ne modifie PAS l'état.
        Principe CQRS respecté.
        
        Returns:
            tuple: (should_stop: bool, reason: str | None)
                - (True, 'max_daily_loss')
                - (True, 'max_daily_gain')
                - (True, 'max_daily_trades')
                - (False, None)
        
        Raises:
            RuntimeError: Si aucun jour actif
        
        Examples:
            >>> # Vérifier limites (QUERY)
            >>> should_stop, reason = day_mgr.should_stop_trading_today()
            >>> if should_stop:
            ...     # Bloquer explicitement (COMMAND)
            ...     day_mgr.block_trading(reason)
        """
        if self.current_day is None:
            raise RuntimeError("Cannot check limits: no active day")
        
        current_pnl_pct = self.daily_pnl_pct
        current_trades = self.current_day['trades_count']
        
        # ====================================================================
        # LIMITE MAX LOSS QUOTIDIEN
        # ====================================================================
        
        if self.max_loss_per_day_pct is not None:
            if current_pnl_pct <= -self.max_loss_per_day_pct:
                self.logger.warning(
                    f"⚠️  Daily max loss limit: {current_pnl_pct:.2f}% "
                    f"<= {-self.max_loss_per_day_pct}%"
                )
                return True, 'max_daily_loss'
        
        # ====================================================================
        # LIMITE MAX GAIN QUOTIDIEN
        # ====================================================================
        
        if self.max_gain_per_day_pct is not None:
            if current_pnl_pct >= self.max_gain_per_day_pct:
                self.logger.info(
                    f"✅ Daily max gain limit: {current_pnl_pct:.2f}% "
                    f">= {self.max_gain_per_day_pct}%"
                )
                return True, 'max_daily_gain'
        
        # ====================================================================
        # LIMITE MAX TRADES QUOTIDIEN
        # ====================================================================
        
        if self.max_trades_per_day is not None:
            if current_trades >= self.max_trades_per_day:
                self.logger.info(
                    f"ℹ️  Daily max trades limit: {current_trades} "
                    f">= {self.max_trades_per_day}"
                )
                return True, 'max_daily_trades'
        
        # ====================================================================
        # Limites non atteintes
        # ====================================================================
        
        return False, None
    
    def block_trading(self, reason: str):
        """
        Bloquer le trading explicitement (COMMAND).
        
        Args:
            reason: Raison du blocage
        
        Raises:
            RuntimeError: Si aucun jour actif
        """
        if self.current_day is None:
            raise RuntimeError("Cannot block: no active day")
        
        self.current_day['status'] = DayStatus.BLOCKED.value
        
        self.logger.warning(
            f"🚫 Trading blocked for day {self.current_day['date'].strftime('%Y-%m-%d')}: "
            f"{reason}"
        )
    
    def unblock_trading(self):
        """
        Débloquer le trading (COMMAND).
        
        Raises:
            RuntimeError: Si aucun jour actif
        """
        if self.current_day is None:
            raise RuntimeError("Cannot unblock: no active day")
        
        self.current_day['status'] = DayStatus.ACTIVE.value
        
        self.logger.info(
            f"✅ Trading unblocked for day {self.current_day['date'].strftime('%Y-%m-%d')}"
        )
    
    # ========================================================================
    # TERMINAISON JOUR
    # ========================================================================
    
    def end_day(self, reason: str = 'completed') -> Dict[str, Any]:
        """
        Terminer jour courant et calculer métriques.
        
        Args:
            reason: Raison fin jour
        
        Returns:
            dict: Résumé jour avec métriques complètes
        
        Raises:
            RuntimeError: Si aucun jour actif
        """
        if self.current_day is None:
            raise RuntimeError("Cannot end day: no active day")
        
        # Marquer comme terminé
        self.current_day['status'] = DayStatus.ENDED.value
        self.current_day['end_reason'] = reason
        self.current_day['ended_at'] = datetime.now(timezone.utc)
        
        # Calculer PnL via properties
        final_pnl = self.daily_pnl
        final_pnl_pct = self.daily_pnl_pct
        
        # Créer résumé jour
        day_summary = {
            'date': self.current_day['date'],
            'starting_balance': self.current_day['starting_balance'],
            'ending_balance': self.current_day['current_balance'],
            'daily_pnl': final_pnl,
            'daily_pnl_pct': final_pnl_pct,
            'trades_count': self.current_day['trades_count'],
            'end_reason': self.current_day['end_reason'],
            'ended_at': self.current_day['ended_at']
        }
        
        # Ajouter à l'historique
        self.days_summary.append(day_summary)
        
        # Logger fin jour
        self.logger.info(
            f"📅 Day ended: {day_summary['date'].strftime('%Y-%m-%d')} UTC | "
            f"PnL: {day_summary['daily_pnl']:.2f} USDT ({day_summary['daily_pnl_pct']:+.2f}%) | "
            f"Trades: {day_summary['trades_count']} | "
            f"Reason: {reason}"
        )
        
        # Reset current_day
        self.current_day = None
        
        return day_summary
    
    # ========================================================================
    # CALCUL MÉTRIQUES
    # ========================================================================
    
    def get_daily_stats(self) -> Dict[str, Any]:
        """
        Obtenir statistiques du jour courant.
        
        Returns:
            dict: Statistiques complètes du jour
        
        Raises:
            RuntimeError: Si aucun jour actif
        """
        if self.current_day is None:
            raise RuntimeError("Cannot get stats: no active day")
        
        return {
            'date': self.current_day['date'].strftime('%Y-%m-%d'),
            'starting_balance': self.current_day['starting_balance'],
            'current_balance': self.current_day['current_balance'],
            'daily_pnl': self.daily_pnl,
            'daily_pnl_pct': self.daily_pnl_pct,
            'trades_count': self.current_day['trades_count'],
            'status': self.current_day['status']
        }
    
    # ========================================================================
    # GETTERS - IMMUTABILITÉ
    # ========================================================================
    
    def get_current_day(self) -> Optional[Dict[str, Any]]:
        """
        Obtenir jour courant (deepcopy pour immutabilité).
        
        Returns:
            dict | None: Jour courant (deepcopy) ou None
        """
        return deepcopy(self.current_day) if self.current_day else None
    
    def get_all_days_summary(self) -> List[Dict[str, Any]]:
        """
        Obtenir résumé de tous les jours (deepcopy pour immutabilité).
        
        Returns:
            list: Liste résumés jours (deepcopy)
        """
        return deepcopy(self.days_summary)
    
    def get_total_days(self) -> int:
        """
        Obtenir nombre total de jours terminés.
        
        Returns:
            int: Nombre de jours dans l'historique
        """
        return len(self.days_summary)
    
    # ========================================================================
    # SAUVEGARDE - BACKUP AUTOMATIQUE
    # ========================================================================
    
    def save_days_summary(self, session_n: int):
        """
        Sauvegarder résumés quotidiens vers JSON avec backup automatique.
        
        Args:
            session_n: Numéro de la session (>= 1)
        
        Raises:
            ValueError: Si session_n invalide
            Exception: Si échec sauvegarde critique
        """
        if len(self.days_summary) == 0:
            self.logger.warning("No days to save")
            return
        
        if session_n < 1:
            raise ValueError(f"session_n must be >= 1, got {session_n}")
        
        project_root = get_project_root()
        save_path = project_root / self.auto_save_path
        
        # Créer dossier si nécessaire
        ensure_directory(save_path)
        
        # Fichiers principal et backup
        filename = f"session_{session_n:03d}_days.json"
        filepath = save_path / filename
        backup_filename = f"session_{session_n:03d}_days.backup.json"
        backup_filepath = save_path / backup_filename
        
        # Préparer données JSON
        days_serializable = []
        
        for day in self.days_summary:
            day_copy = deepcopy(day)
            
            # Convertir datetime en string
            for key, value in day_copy.items():
                if isinstance(value, datetime):
                    day_copy[key] = format_datetime(value, "%Y-%m-%d %H:%M:%S")
            
            days_serializable.append(day_copy)
        
        # Données complètes
        data = {
            'session_n': session_n,
            'saved_at': format_datetime(datetime.now(timezone.utc), "%Y-%m-%d %H:%M:%S"),
            'total_days': len(days_serializable),
            'timezone': 'UTC',
            'version': _VERSION,  # [v2.2.2 — FIX-DTM-4] était '2.2.1' hardcodé
            'days': days_serializable
        }
        
        # Sauvegarde avec backup automatique
        try:
            write_json(data, filepath, indent=2)
            
            self.logger.info(
                f"💾 Session {session_n} days saved: {filepath} "
                f"({len(days_serializable)} days)"
            )
        
        except Exception as primary_error:
            self.logger.critical(
                f"🚨 CRITICAL: Failed to save days to {filepath}: {primary_error}"
            )
            
            try:
                # Tentative backup
                write_json(data, backup_filepath, indent=2)
                
                self.logger.warning(
                    f"⚠️  Days saved to BACKUP: {backup_filepath} "
                    f"({len(days_serializable)} days)"
                )
            
            except Exception as backup_error:
                self.logger.critical(
                    f"🚨🚨🚨 CRITICAL FAILURE: Cannot save days!\n"
                    f"Primary: {primary_error}\n"
                    f"Backup: {backup_error}\n"
                    f"DATA LOSS IMMINENT - {len(days_serializable)} days"
                )
                
                raise Exception(
                    f"CRITICAL: Cannot save session {session_n} days. "
                    f"Manual intervention required."
                )
    
    # ========================================================================
    # UTILITAIRES
    # ========================================================================
    
    def reset(self):
        """
        Reset complet DayTradesManager (pour tests).
        """
        self.current_day = None
        self.days_summary.clear()
        
        self.logger.info(f"DayTradesManager v{_VERSION} reset")  # [v2.2.2 — FIX-DTM-4]


# ============================================================================
# FIN DU MODULE
# ============================================================================
