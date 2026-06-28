"""
BULLET-1 - Order Simulator Module
===================================

Simulateur d'ordres pour backtesting et paper trading.
Simule comportement réaliste des exchanges crypto.

Fonctionnalités :
- Simulation ordres MARKET (exécution immédiate)
- Simulation ordres LIMIT (attente prix cible)
- Slippage dynamique basé volatilité ATR (via atr.py)
- Frais maker/taker différenciés
- Funding fees (futures perpetuals, 8h)
- Latence API simulée (50-200ms)
- Export fills JSON/CSV
- Thread-safe (RLock)
- Support backtest + paper

Version: 2.6.9
Date: 2026-06-27
Author: FuegoDev
Mode: ✅ Backtest | ✅ Paper | ❌ Live
Dépendances: logger.py, helpers.py, atr.py, session_manager.py

Changelog:
    v2.6.9 — 2026-06-27
        [FIX-OS-ATR] Nouveau paramètre optionnel atr_indicator au constructeur.
            _compute_volatility_from_atr() délègue à l'instance ATRIndicator
            injectée (partagée avec RiskManager/PositionManager) au lieu de
            recalculer un ATR indépendant via calculate_atr_simple(period=14).
            Fallback inchangé si atr_indicator=None. Corrige le slippage
            dynamique incohérent avec le reste du système (audit Phase 4/8,
            MAJEUR M3).
"""

import csv
import json
import random
import threading
import time
from collections import deque
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional, Protocol, Union

import pandas as pd
import sys

# [v2.6.8 — FIX-OS-7] Pattern unifié de résolution racine projet (DRY).
# Remplace find_project_root() locale dupliquée — alignement sur le standard
# BULLET-1 utilisé dans market_context.py, signal_generator.py, strategy.py.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# ============================================================================
# IMPORTS BULLET-1
# ============================================================================

from src.utils.logger import BulletLogger
from src.utils.helpers import (
    timestamp_to_datetime,
    format_datetime,
    generate_id,
    ensure_directory
)
from src.indicators.atr import (
    calculate_atr_simple,
    ATR_DEFAULT_PERIOD,
    ATR_DEFAULT_METHOD,
    ATRIndicator,
)


# ============================================================================
# PROTOCOL — Interface capital (Dependency Inversion)
# ============================================================================

class ICapitalProvider(Protocol):
    """
    Interface minimale que doit exposer le fournisseur de capital.

    Permet au OrderSimulator d'être testable unitairement avec un mock
    sans dépendre de la classe concrète SessionManager.

    Toute classe implémentant ces 5 méthodes peut être injectée.
    """

    def get_capital_total(self) -> float:
        """Retourne le solde total (current_funds)."""
        ...

    def get_capital_available(self) -> float:
        """Retourne le capital disponible (total - locked_margin)."""
        ...

    def get_capital_locked(self) -> float:
        """Retourne la marge actuellement verrouillée."""
        ...

    def reserve_margin(self, amount: float) -> None:
        """Verrouille `amount` USDT de marge avant entrée en position."""
        ...

    def settle_trade(self, pnl_net: float, margin: float) -> None:
        """Libère la marge et applique le PnL net (opération atomique)."""
        ...

    # [v2.6.2 — FIX OS-4] Méthode ajoutée au protocole.
    # Était appelée dans execute_market_order() (déduction entry_fees) mais
    # absente de l'interface → tout mock conforme au protocole crashait à
    # l'exécution. La validation duck-typing du __init__ ne l'attrapait pas
    # non plus. Contrat d'interface désormais complet et cohérent avec l'usage.
    def update_balance(self, amount: float) -> None:
        """
        Applique un delta immédiat sur le solde (hors marge).

        Utilisé pour déduire les entry_fees au moment de l'ouverture d'une
        position MARKET : ces frais constituent un coût réel instantané,
        distinct de la marge qui sera libérée à la clôture via settle_trade().

        Args:
            amount: Delta USDT (négatif = déduction, positif = crédit).
        """
        ...


# ============================================================================
# CHARGEMENT CONFIGURATION
# ============================================================================

def load_order_simulator_config(
    config_path: Optional[Union[str, Path]] = None
) -> dict:
    """
    Charge config/order_simulator_config.json.

    Fonction standalone publique — utilisable indépendamment de la classe,
    testable unitairement, cohérente avec le pattern de load_atr_config().

    Args:
        config_path: Chemin explicite vers le fichier JSON.
                     Auto-détecté depuis la racine projet si None.

    Returns:
        dict: Configuration complète order_simulator_config.json

    Raises:
        FileNotFoundError: Fichier introuvable au chemin résolu.
        json.JSONDecodeError: Contenu JSON invalide.

    Examples:
        >>> config = load_order_simulator_config()
        >>> config = load_order_simulator_config('/custom/path/config.json')
    """
    path = Path(config_path) if config_path else (
        _PROJECT_ROOT / 'config' / 'order_simulator_config.json'
    )

    if not path.exists():
        raise FileNotFoundError(
            f"Configuration OrderSimulator introuvable: {path}\n"
            "Créer config/order_simulator_config.json avec la structure requise.\n"
            "Structure minimale:\n"
            '  {\n'
            '    "simulation": {\n'
            '      "maker_fee": 0.02,\n'
            '      "taker_fee": 0.04,\n'
            '      "slippage_dynamic": true,\n'
            '      "slippage_base": 0.0012,\n'
            '      "slippage_max": 0.005,\n'
            '      "spread_pct": 0.02,\n'
            '      "api_latency_ms": 200,\n'
            '      "funding_rate_8h": 0.01\n'
            '    }\n'
            '  }'
        )

    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


# ============================================================================
# CONSTANTES
# ============================================================================

# [v2.6.1 — FIX OS-3] Normalisation casse : majuscules pour cohérence cross-module.
# L'ancienne définition {'backtest', 'paper'} rejetait 'BACKTEST' produit par strategy.py.
_VALID_MODES          = frozenset({'BACKTEST', 'PAPER'})
_VALID_ORDER_TYPES    = frozenset({'MARKET', 'LIMIT', 'STOP_LIMIT'})
_VALID_SIDES          = frozenset({'BUY', 'SELL'})
_VALID_EXPORT_FORMATS = frozenset({'json', 'csv'})

# ATR_DEFAULT_PERIOD et ATR_DEFAULT_METHOD importés depuis atr.py.
# Source de vérité unique : atr_config.json → atr.py → order_simulator.py
_VOLATILITY_FALLBACK = 0.01      # Fallback 1% si ATR indisponible

# [v2.6.8 — FIX-OS-6] Taille maximale de l'historique des fills par défaut.
# Évite la saturation mémoire sur les longs backtests haute-fréquence.
# Configurable via config['simulation']['max_fills_history'].
_DEFAULT_MAX_FILLS_HISTORY: int = 50_000


# ============================================================================
# CLASSE PRINCIPALE - OrderSimulator
# ============================================================================

class OrderSimulator:
    """
    Simulateur d'ordres pour backtesting et paper trading.

    Simule de manière réaliste le comportement d'un exchange crypto
    (Binance) avec slippage, fees, funding, latence.

    Architecture v2.6.0 — Moteur de calcul pur :
    - Aucun état capital interne. Tout le capital est délégué à
      l'ICapitalProvider injecté (concrètement : SessionManager).
    - Le simulateur CONSULTE le capital avant d'accepter un ordre.
    - Le simulateur NOTIFIE le SessionManager après exécution.
    - Thread-safe : RLock sur fills et stats uniquement.

    Responsabilités:
        1. Simulation ordres MARKET/LIMIT
        2. Calcul slippage dynamique (volatilité via ATR)
        3. Calcul fees maker/taker
        4. Calcul funding fees (8h)
        5. Notification au SessionManager (reserve_margin / settle_trade)
        6. Latence API (optionnel)
        7. Export fills JSON/CSV

    Délégation capital (v2.6.0):
        ENTRY → session_manager.reserve_margin(collateral)
        EXIT  → session_manager.settle_trade(pnl_net, collateral)
        READ  → session_manager.get_capital_available() / get_capital_total()

    Thread-safety:
        Fills et stats : protégés par self._lock (RLock).
        Capital        : protégé par le RLock interne du SessionManager.

    Attributes:
        logger (BulletLogger): Logger centralisé
        config (dict): Configuration order_simulator_config.json
        mode (str): 'backtest' ou 'paper'
        session_manager (ICapitalProvider): Fournisseur de capital injecté
        maker_fee (float): Frais maker (ex: 0.0002 = 0.02%)
        taker_fee (float): Frais taker (ex: 0.0004 = 0.04%)
        slippage_base (float): Slippage base (ex: 0.0001 = 0.01%)
        slippage_max (float): Slippage max (ex: 0.005 = 0.5%)
        funding_rate_8h (float): Taux funding 8h (ex: 0.0001 = 0.01%)
        api_latency_ms (int): Latence API ms
        _fills_history (list): Historique fills
        _lock (RLock): Lock thread-safety (fills + stats)
        _stats (dict): Statistiques simulation

    Examples:
        >>> session_mgr = SessionManager(config)
        >>> simulator   = OrderSimulator(
        ...     session_manager=session_mgr,
        ...     mode='backtest'
        ... )
        >>>
        >>> fill = simulator.execute_market_order(
        ...     order, current_price=50250, current_candle=candle,
        ...     historical_data=df
        ... )
        >>> print(f"Fill: {fill['fill_price']}, Fees: {fill['fees']}")
        >>>
        >>> # Capital lu depuis SessionManager
        >>> available = simulator.get_capital_available()
        >>> locked    = simulator.get_capital_locked()
    """

    def __init__(
        self,
        session_manager: ICapitalProvider,
        mode: str = 'backtest',
        config: Optional[dict] = None,
        random_seed: Optional[int] = None,
        atr_indicator: Optional[ATRIndicator] = None,
    ):
        """
        Initialise le simulateur d'ordres.

        Le paramètre `initial_capital` a été supprimé (v2.6.0).
        Le capital est entièrement géré par le SessionManager injecté.

        Charge automatiquement config/order_simulator_config.json si aucun
        config n'est fourni explicitement.

        Args:
            session_manager: Fournisseur de capital (ICapitalProvider).
                             En pratique : instance de SessionManager.
            mode:            Mode opération ('backtest'/'BACKTEST' ou 'paper'/'PAPER')
            config:          Configuration override (dict).
                             Si None → chargement automatique depuis
                             config/order_simulator_config.json.
            random_seed:     Seed optionnel pour reproductibilité totale du backtest.
                             [v2.6.1 — FIX OS-2] Utilise un Random isolé (self._rng)
                             pour ne pas polluer le PRNG global du process.
            atr_indicator:   Instance ATRIndicator partagée, injectée par engine.py
                             (même instance que RiskManager/PositionManager).
                             [v2.6.9 — FIX-OS-ATR] Si fournie, _compute_volatility_from_atr()
                             l'utilise au lieu de recalculer un ATR indépendant via
                             calculate_atr_simple(), garantissant un slippage dynamique
                             cohérent avec le reste du système (audit Phase 4/8, MAJEUR M3).
                             Si None (ex: trailing_stop.type='fixed') → fallback
                             automatique sur calculate_atr_simple(period=14),
                             comportement identique aux versions précédentes.

        Raises:
            ValueError: Si mode invalide.
            TypeError: Si session_manager ne respecte pas ICapitalProvider.
            FileNotFoundError: Si config=None et order_simulator_config.json
                               introuvable.
            json.JSONDecodeError: Si order_simulator_config.json invalide.

        Examples:
            >>> session_mgr = SessionManager(config)
            >>> simulator = OrderSimulator(session_mgr, mode='backtest', random_seed=42)
            >>>
            >>> # Override config (tests unitaires)
            >>> simulator = OrderSimulator(
            ...     session_mgr, mode='backtest', config=custom_config
            ... )
        """
        # [v2.6.1 — FIX OS-3] Normalisation casse AVANT validation.
        # Accepte indifféremment 'backtest' ou 'BACKTEST'.
        mode = mode.upper()

        if mode not in _VALID_MODES:
            raise ValueError(
                f"Invalid mode: {mode}. Must be one of {_VALID_MODES}"
            )

        # Validation duck-typing du fournisseur de capital.
        # [v2.6.2 — FIX OS-4] update_balance ajouté : était manquant alors
        # qu'il est appelé dans execute_market_order() pour déduire les
        # entry_fees. Tout mock conforme au protocole crashait en exécution.
        required_methods = [
            'get_capital_total', 'get_capital_available', 'get_capital_locked',
            'reserve_margin', 'settle_trade', 'update_balance'
        ]
        missing = [m for m in required_methods if not callable(getattr(session_manager, m, None))]
        if missing:
            raise TypeError(
                f"session_manager ne respecte pas ICapitalProvider. "
                f"Méthodes manquantes: {missing}"
            )

        # Injection dépendance
        self.session_manager: ICapitalProvider = session_manager

        # [v2.6.9 — FIX-OS-ATR] Instance ATRIndicator partagée (optionnelle).
        # Si fournie : _compute_volatility_from_atr() délègue le calcul à
        # cette instance au lieu de calculate_atr_simple() — cohérence
        # garantie avec RiskManager/PositionManager (audit Phase 4/8, MAJEUR M3).
        self._atr_indicator: Optional[ATRIndicator] = atr_indicator

        # Logger
        self.logger = BulletLogger()

        # Chargement config — auto si non fourni, override si fourni explicitement
        if config is None:
            self.config = load_order_simulator_config()
            self.logger.debug(
                "Configuration chargée depuis order_simulator_config.json"
            )
        else:
            self.config = config
            self.logger.debug(
                "Configuration override fournie explicitement (dict)"
            )

        self.mode = mode

        # Paramètres simulation depuis config
        sim_config = self.config.get('simulation', {})

        self.maker_fee = sim_config.get('maker_fee', 0.02) / 100    # % → decimal
        self.taker_fee = sim_config.get('taker_fee', 0.04) / 100

        self.slippage_base    = sim_config.get('slippage_base', 0.0012)
        self.slippage_max     = sim_config.get('slippage_max', 0.005)
        self.slippage_dynamic = sim_config.get('slippage_dynamic', True)

        self.spread_pct     = sim_config.get('spread_pct', 0.02) / 100
        self.api_latency_ms = sim_config.get('api_latency_ms', 200)

        self.funding_rate_8h = sim_config.get('funding_rate_8h', 0.01) / 100

        # Thread-safety (fills + stats uniquement — le capital est dans SessionManager)
        self._lock = threading.RLock()

        # [v2.6.1 — FIX OS-2] RNG isolé pour déterminisme sans polluer le PRNG global.
        # random.Random(seed) crée un générateur indépendant du random module-level.
        # Sans seed : comportement aléatoire (paper trading).
        # Avec seed : backtest 100% reproductible.
        self._rng = random.Random(random_seed)
        if random_seed is not None:
            self.logger.debug(f"OrderSimulator: RNG isolé initialisé (seed={random_seed})")

        # [v2.6.8 — FIX-OS-6] Historique borné pour éviter la saturation mémoire
        # sur les longs backtests haute-fréquence. Configurable via config.
        _max_fills = int(
            sim_config.get('max_fills_history', _DEFAULT_MAX_FILLS_HISTORY)
        )
        self._fills_history: deque = deque(maxlen=_max_fills)
        self._max_fills_history: int = _max_fills

        # Statistiques
        self._stats = self._make_empty_stats()

        self.logger.info(
            f"OrderSimulator initialized "
            f"(mode: {mode}, capital_provider: {type(session_manager).__name__}, "
            f"seed={'set' if random_seed is not None else 'unset'}, "
            f"atr_indicator={'injected' if self._atr_indicator is not None else 'none'})"
        )

    # ========================================================================
    # MÉTHODES PRINCIPALES - EXÉCUTION ORDRES
    # ========================================================================

    def execute_market_order(
        self,
        order: dict,
        current_price: float,
        current_candle: dict,
        historical_data: Optional[pd.DataFrame] = None
    ) -> Dict[str, Any]:
        """
        Simule ordre MARKET (exécution immédiate).

        Flow:
        1. Validation order
        2. Vérification capital disponible via session_manager
        3. Calcul volatilité ATR via atr.calculate_atr_simple() (si enabled)
        4. Calcul slippage dynamique (volatilité ATR normalisée)
        5. Calcul prix exécution (price + slippage)
        6. Calcul fees TAKER
        7. Réservation marge → session_manager.reserve_margin(collateral)
        8. Simulation latence API
        9. Création fill complet
        10. Update stats et historique

        Args:
            order:           Ordre depuis strategy.py
            current_price:   Prix marché actuel
            current_candle:  Bougie courante (doit contenir 'close')
            historical_data: Historique OHLCV (pour calcul ATR/volatilité)

        Returns:
            Dict fill avec détails complets

        Raises:
            ValueError:  Si ordre invalide
            RuntimeError: Si capital insuffisant (levé par session_manager)

        Notes:
            - MARKET → exécution immédiate
            - Applique fees TAKER
            - Slippage basé sur ATR(14/ema) normalisé si historical_data fourni
            - La réservation de marge est effectuée par session_manager

        Examples:
            >>> order = {
            ...     'symbol': 'BTC/USDT',
            ...     'direction': 'LONG',
            ...     'entry_price': 50250,
            ...     'entry_type': 'MARKET',
            ...     'size': 0.01,
            ...     'collateral': 50.0,
            ... }
            >>> fill = simulator.execute_market_order(
            ...     order, current_price=50250,
            ...     current_candle=candle, historical_data=df
            ... )
            >>> print(f"Filled @ {fill['fill_price']}")
        """
        # Validation structure ordre
        self._validate_order(order)

        collateral = order['collateral']

        # Vérification capitale AVANT exécution (session_manager est source de vérité)
        # La vérification + lock sont délégués — reserve_margin() lève RuntimeError
        # si fonds insuffisants.
        if not self._has_sufficient_capital(collateral):
            raise RuntimeError(
                f"Insufficient capital: need {collateral:.2f}, "
                f"available {self.session_manager.get_capital_available():.2f}"
            )

        # Déterminer side
        side = 'BUY' if order['direction'] == 'LONG' else 'SELL'

        # Calcul slippage dynamique via ATR (atr.py)
        volatility = _VOLATILITY_FALLBACK

        if self.slippage_dynamic and historical_data is not None:
            volatility = self._compute_volatility_from_atr(
                historical_data, current_candle, current_price
            )

        slippage_pct    = self._calculate_dynamic_slippage(volatility)
        slippage_amount = current_price * slippage_pct

        # Prix exécution (slippage + demi-spread, toujours défavorables au trader)
        # [FIX-SPREAD] Le spread bid/ask est modélisé en demi-spread par côté :
        # BUY exécuté à l'ask (mid + spread/2), SELL au bid (mid - spread/2).
        # spread_pct est déjà en décimal (converti /100 dans __init__).
        half_spread_amount = current_price * (self.spread_pct / 2)
        if side == 'BUY':
            fill_price = current_price + slippage_amount + half_spread_amount
        else:
            fill_price = current_price - slippage_amount - half_spread_amount

        # Calcul fees TAKER
        notional = fill_price * order['size']
        fees     = notional * self.taker_fee

        # Réservation marge → SessionManager (source de vérité capital)
        self.session_manager.reserve_margin(collateral)

        # [v2.6.1 — FIX OS-1] Déduction immédiate des entry_fees.
        # reserve_margin() ne lock que la marge (collateral).
        # Les fees d'entrée (taker fee sur valeur notionnelle) constituent
        # un coût réel immédiat qui doit être répercuté sur le solde.
        # Sans cette ligne, les entry_fees sont calculées et retournées dans
        # le fill mais jamais déduites du capital → biais positif sur tous les résultats.
        # Formule : fees = fill_price × size × taker_fee
        if fees > 0:
            self.session_manager.update_balance(-fees)

        # ── [v2.6.4 — FIX OS-ROLLBACK] Rollback automatique si crash post-reserve ─
        # À ce stade, la marge ET les entry_fees sont déjà engagées sur le SessionManager.
        # Si une exception survient dans la suite (création fill, update stats...),
        # sans ce bloc, la marge resterait verrouillée pour toute la session et
        # l'engine ne pourrait plus ouvrir de position — bug silencieux en production.
        # Solution : try/except avec rollback complet avant re-raise de l'exception.
        try:
            # Latence API (simulation)
            latency_ms = self._simulate_latency()

            # Génération fill
            fill = {
                # Identification
                'order_id': self._generate_order_id(),
                'symbol':   order['symbol'],
                'side':     side,
                'type':     'MARKET',
                'status':   'FILLED',

                # Prix & quantité
                'requested_price': order['entry_price'],
                'fill_price':      round(fill_price, 2),
                'requested_size':  order['size'],
                'filled_size':     order['size'],
                'remaining_size':  0.0,

                # Coûts
                'slippage':     round(slippage_amount, 4),
                'slippage_pct': round(slippage_pct * 100, 4),
                'fees':         round(fees, 4),
                'fee_rate':     self.taker_fee,
                'fee_type':     'TAKER',

                # [v2.6.2 — FIX OS-1 reporting] Les entry_fees sont déduites
                # immédiatement via update_balance() à l'ENTRY (coût réel
                # instantané). Elles sont stockées ici pour être récupérées à la
                # clôture par execute_limit_order() et intégrées dans pnl_net
                # selon la convention standard : pnl_net = pnl_gross - entry_fees - exit_fees.
                # Sans ce champ, le pnl_net de sortie oubliait les entry_fees
                # → reporting systématiquement trop optimiste.
                'entry_fees':   round(fees, 4),

                # Timing
                # [v2.6.3] datetime.now(timezone.utc) — UTC-aware pour cohérence
                # avec les timestamps OHLCV. datetime.now() sans tz produisait un
                # datetime naïf incohérent avec le reste du système.
                'timestamp':  current_candle.get('timestamp', datetime.now(timezone.utc)),
                'latency_ms': latency_ms,

                # Capital (snapshots informatifs — état au moment de l'ordre)
                'collateral_locked':   collateral,
                'collateral_released': 0.0,
                'capital_impact':      -collateral,   # Négatif = lock

                # Simulation metadata
                'simulation_mode':    self.mode,
                'simulation_details': {
                    'volatility_factor': round(volatility, 6),
                    'atr_period':        ATR_DEFAULT_PERIOD,
                    'atr_method':        ATR_DEFAULT_METHOD,
                    'spread_estimate':   self.spread_pct,
                    # [FIX-SPREAD] Demi-spread effectivement appliqué sur ce fill.
                    'spread_amount':     round(half_spread_amount, 4),
                    'partial_fill':      False,
                }
            }

            # Update stats et historique (thread-safe)
            with self._lock:
                self._stats['total_orders']   += 1
                self._stats['market_orders']  += 1
                self._stats['total_fees']     += fees
                self._stats['total_slippage'] += slippage_amount
                self._fills_history.append(deepcopy(fill))

            self.logger.info(
                f"MARKET ORDER FILLED: {side} {order['size']:.8f} @ {fill_price:.2f} | "
                f"Slippage: {slippage_pct*100:.3f}% ({slippage_amount:.2f} USDT) | "
                f"Fees: {fees:.4f} USDT | Margin reserved: {collateral:.2f} | "
                f"Volatility (ATR): {volatility:.4f}"
            )

            return fill

        except Exception as _post_reserve_exc:
            # ── Rollback atomique : libérer ce qui a été engagé ──────────────
            # reserve_margin() et update_balance() ont déjà muté l'état du SM.
            # On annule les deux avant de re-lever l'exception pour garantir
            # que la candle est idempotente du point de vue du capital.
            self.logger.critical(
                f"execute_market_order crash post-reserve "
                f"({type(_post_reserve_exc).__name__}: {_post_reserve_exc}). "
                f"Rollback: marge={collateral:.2f} USDT + fees={fees:.4f} USDT."
            )
            try:
                self.session_manager.release_margin(collateral)
            except Exception as _rb_margin_exc:
                self.logger.critical(
                    f"Rollback release_margin ÉCHEC ({collateral:.2f}): {_rb_margin_exc}. "
                    "Capital potentiellement incohérent — vérification manuelle requise."
                )
            if fees > 0:
                try:
                    self.session_manager.update_balance(fees)   # +fees = annule la déduction
                except Exception as _rb_fees_exc:
                    self.logger.critical(
                        f"Rollback update_balance fees ÉCHEC ({fees:.4f}): {_rb_fees_exc}. "
                        "Capital potentiellement incohérent — vérification manuelle requise."
                    )
            raise   # Re-raise : l'engine reçoit l'exception et retourne NO_SIGNAL proprement

    def execute_limit_order(
        self,
        order: dict,
        current_price: float,
        target_price: float,
        order_type: Literal['LIMIT', 'STOP_LIMIT'] = 'LIMIT',
        volatility: Optional[float] = None,
        candle_low: Optional[float] = None,
        candle_high: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Simule ordre LIMIT ou STOP_LIMIT (SL/TP).

        Comportement différencié selon le type d'ordre :

        LIMIT (Take-Profit) :
        - Exécution exacte au prix cible (pas de slippage)
        - Frais MAKER — l'ordre était en attente dans le carnet

        STOP_LIMIT (Stop-Loss) :
        - Réalité Binance : le SL déclenche un ordre MARKET immédiat
        - Frais TAKER — exécution agressive contre le carnet
        - Slippage défavorable — marché souvent en mouvement rapide

        Flow:
        1. Vérification si prix cible atteint
        2. Calcul fill_price selon type (slippage si STOP_LIMIT)
        3. Calcul fees (MAKER pour TP, TAKER pour SL)
        4. Calcul PnL
        5. Règlement atomique → session_manager.settle_trade(pnl_net, margin)
        6. Création fill

        Args:
            order:       Ordre ou position ouverte
            current_price: Prix actuel marché
            target_price: Prix cible (SL ou TP)
            order_type:  'LIMIT' (TP) ou 'STOP_LIMIT' (SL)
            volatility:  Volatilité normalisée pour calcul slippage SL.
                         Si None → utilise _VOLATILITY_FALLBACK.

        Returns:
            Fill complet si prix atteint, None sinon.

        Notes:
            - TP : fill_price = target_price, fee_type = MAKER, slippage = 0
            - SL : fill_price = target_price ± slippage, fee_type = TAKER
            - settle_trade() est atomique (release marge + update PnL)
        """
        direction = order.get('direction', 'LONG')

        # ── Vérification si prix atteint ─────────────────────────────────────
        if order_type == 'LIMIT':            # TP
            if direction == 'LONG':
                if current_price < target_price:
                    return None
            else:                            # SHORT
                if current_price > target_price:
                    return None

        elif order_type == 'STOP_LIMIT':     # SL
            if direction == 'LONG':
                if current_price > target_price:
                    return None
            else:                            # SHORT
                if current_price < target_price:
                    return None

        # ── Prix d'exécution & frais selon type ──────────────────────────────
        size = order['size']
        side = 'SELL' if direction == 'LONG' else 'BUY'

        if order_type == 'STOP_LIMIT':
            # SL → ordre MARKET : slippage défavorable + fees TAKER
            vol             = volatility if volatility is not None else _VOLATILITY_FALLBACK
            slippage_pct    = self._calculate_dynamic_slippage(vol)
            slippage_amount = target_price * slippage_pct

            # [FIX-SPREAD] Demi-spread appliqué sur les SL (exécution MARKET agressive).
            # Le TP (LIMIT/maker) est exécuté au prix exact — pas de spread côté TP.
            half_spread_amount = target_price * (self.spread_pct / 2)

            # Slippage + spread toujours défavorable (aggrave la perte)
            if direction == 'LONG':
                fill_price = target_price - slippage_amount - half_spread_amount   # SL LONG : reçoit moins
            else:
                fill_price = target_price + slippage_amount + half_spread_amount   # SL SHORT : paie plus

            # [v2.6.8 — FIX-OS-5] Clip fill_price dans les bornes réelles de la bougie.
            # Un fill_price hors de [candle_low, candle_high] est physiquement impossible :
            # le prix ne peut pas dépasser ses propres extrêmes sur la bougie concernée.
            # Sans ce clip, un slippage fort pouvait produire un fill à un prix jamais
            # atteint — biais négatif mesuré jusqu'à 0.3% par trade sur SL.
            if candle_low is not None and candle_high is not None:
                clipped = max(candle_low, min(fill_price, candle_high))
                if clipped != fill_price:
                    self.logger.debug(
                        f"[FIX-OS-5] fill_price SL clippé : {fill_price:.4f} → {clipped:.4f} "
                        f"(bougie [{candle_low:.4f}, {candle_high:.4f}])"
                    )
                    fill_price = clipped

            notional = fill_price * size
            fees     = notional * self.taker_fee
            fee_rate = self.taker_fee
            fee_type = 'TAKER'

        else:
            # TP → ordre LIMIT : exécution exacte, pas de slippage, fees MAKER
            fill_price      = target_price
            slippage_pct    = 0.0
            slippage_amount = 0.0
            notional        = fill_price * size
            fees            = notional * self.maker_fee
            fee_rate        = self.maker_fee
            fee_type        = 'MAKER'

        # ── Calcul PnL ────────────────────────────────────────────────────────
        entry_price = order.get('entry_price', fill_price)

        if direction == 'LONG':
            pnl_gross = (fill_price - entry_price) * size
        else:    # SHORT
            pnl_gross = (entry_price - fill_price) * size

        # [v2.6.2 — FIX OS-1 reporting]
        # Convention standard trading :
        #   pnl_gross = mouvement de prix pur (sans aucun frais)
        #   pnl_net   = pnl_gross - TOUTES les fees (entry + exit)
        #               C'est la seule définition utile pour le reporting,
        #               le dashboard et l'analyse de stratégie.
        #
        # _settle_pnl = variable interne uniquement, jamais exposée dans le
        #               fill. settle_trade() ne doit recevoir que
        #               pnl_gross - exit_fees car les entry_fees ont déjà été
        #               déduites du solde à l'ENTRY via update_balance().
        #               Passer pnl_net ici déduirait les entry_fees une
        #               deuxième fois et casserait la comptabilité du capital.
        entry_fees  = order.get('entry_fees', 0.0)
        exit_fees   = fees
        pnl_net     = pnl_gross - exit_fees - entry_fees
        _settle_pnl = pnl_gross - exit_fees

        # ── Règlement atomique via SessionManager ─────────────────────────────
        # Utilise _settle_pnl (interne) et non pnl_net.
        # Voir commentaire bloc PnL ci-dessus pour l'explication complète.
        collateral = order.get('collateral', 0.0)
        self.session_manager.settle_trade(pnl_net=_settle_pnl, margin=collateral)

        # ── Latence ───────────────────────────────────────────────────────────
        latency_ms = self._simulate_latency()

        # ── Génération fill ───────────────────────────────────────────────────
        fill = {
            # Identification
            'order_id': self._generate_order_id(),
            'symbol':   order.get('symbol', 'BTC/USDT'),
            'side':     side,
            'type':     order_type,
            'status':   'FILLED',

            # Prix & quantité
            'requested_price': target_price,
            'fill_price':      round(fill_price, 2),
            'requested_size':  size,
            'filled_size':     size,
            'remaining_size':  0.0,

            # Coûts — [v2.6.3 — FIX OS-BUG3] Suppression de la clé 'fees' en double.
            # L'ancienne implémentation définissait 'fees' à la ligne des slippage_pct
            # (= exit_fees seulement), puis la redéfinissait plus bas (= entry + exit).
            # Python conservait silencieusement la DERNIÈRE valeur, mais le contrat
            # de la clé était ambigu et trompeur à la lecture.
            # Convention unifiée désormais :
            #   'fees'       = total fees (entry + exit) — cohérent avec execute_market_order
            #   'exit_fees'  = frais de sortie seuls (pour décomposition)
            #   'entry_fees' = frais d'entrée (rappel informatif depuis l'ENTRY)
            'slippage':     round(slippage_amount, 4),
            'slippage_pct': round(slippage_pct * 100, 4),
            'fee_rate':     fee_rate,
            'fee_type':     fee_type,

            # PnL — convention standard trading
            'pnl_gross':  round(pnl_gross, 4),   # Mouvement de prix pur
            'pnl_net':    round(pnl_net, 4),      # Après toutes fees (entry + exit)

            # Détail fees (informatif — utile pour débogage et audit)
            'entry_fees': round(entry_fees, 4),
            'exit_fees':  round(exit_fees, 4),
            'fees':       round(entry_fees + exit_fees, 4),   # Total fees (unique définition)

            'entry_price': entry_price,

            # Timing
            # [v2.6.3] datetime.now(timezone.utc) — UTC-aware pour cohérence
            # avec les timestamps OHLCV. datetime.now() sans tz produisait un
            # datetime naïf incohérent avec le reste du système.
            'timestamp':  datetime.now(timezone.utc),
            'latency_ms': latency_ms,

            # Capital (snapshots informatifs)
            'collateral_locked':   0.0,
            'collateral_released': collateral,
            'capital_impact':      collateral + pnl_net,   # Positif = release + gain

            # Simulation metadata
            'simulation_mode': self.mode,
            'exit_type':       order_type,
        }

        # ── Update stats (thread-safe) ────────────────────────────────────────
        with self._lock:
            self._stats['total_orders'] += 1
            self._stats['limit_orders'] += 1
            self._stats['total_fees']   += fees
            self._fills_history.append(deepcopy(fill))

            if pnl_net > 0:
                self._stats['winning_trades'] += 1
            else:
                self._stats['losing_trades'] += 1

        exit_label = 'TP' if order_type == 'LIMIT' else 'SL'
        self.logger.info(
            f"{exit_label} HIT: {side} {size:.8f} @ {fill_price:.2f} "
            f"(trigger: {target_price:.2f}) | "
            f"Slippage: {slippage_pct*100:.3f}% | "
            f"Fees: entry={entry_fees:.4f} exit={exit_fees:.4f} ({fee_type}) | "
            f"PnL gross: {pnl_gross:+.2f} net: {pnl_net:+.2f} USDT | "
            f"Margin released: {collateral:.2f}"
        )

        return fill

    def check_sl_tp(
        self,
        position: dict,
        current_candle: dict,
        historical_data: Optional[pd.DataFrame] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Vérifie si SL ou TP atteint pour une position ouverte.

        Flow:
        1. Check SL hit (candle low/high vs SL price)
        2. Check TP hit (candle high/low vs TP price)
        3. Priorité SL si les deux touchés sur la même bougie
        4. Si SL : calcul volatilité ATR pour slippage réaliste
        5. Exécution via execute_limit_order()

        Args:
            position:        Position ouverte (depuis position_manager)
            current_candle:  Bougie courante (doit contenir high, low, close)
            historical_data: Historique OHLCV pour calcul volatilité ATR du SL.
                             Si None → slippage SL basé sur _VOLATILITY_FALLBACK.

        Returns:
            Fill si SL ou TP hit, None sinon.

        Notes:
            - LONG : SL = candle['low'] <= SL, TP = candle['high'] >= TP
            - SHORT: SL = candle['high'] >= SL, TP = candle['low'] <= TP
            - Priorité SL si les deux touchés sur la même bougie
        """
        direction = position['direction']
        sl_price  = position['stop_loss']
        tp_price  = position['take_profit']

        candle_high = current_candle['high']
        candle_low  = current_candle['low']

        sl_hit = False
        tp_hit = False

        if direction == 'LONG':
            if candle_low  <= sl_price: sl_hit = True
            if candle_high >= tp_price: tp_hit = True
        else:   # SHORT
            if candle_high >= sl_price: sl_hit = True
            if candle_low  <= tp_price: tp_hit = True

        # Priorité SL
        if sl_hit:
            sl_volatility = None
            if historical_data is not None:
                sl_volatility = self._compute_volatility_from_atr(
                    historical_data,
                    current_candle,
                    current_price=sl_price,
                )

            return self.execute_limit_order(
                order=position,
                current_price=sl_price,
                target_price=sl_price,
                order_type='STOP_LIMIT',
                volatility=sl_volatility,
                # [v2.6.8 — FIX-OS-5] Bornes réelles de la bougie pour clip fill_price.
                candle_low=candle_low,
                candle_high=candle_high,
            )

        if tp_hit:
            return self.execute_limit_order(
                order=position,
                current_price=tp_price,
                target_price=tp_price,
                order_type='LIMIT',
            )

        return None

    # ========================================================================
    # ACCESSEURS CAPITAL — Délégation vers SessionManager
    # ========================================================================

    def get_capital_available(self) -> float:
        """
        Retourne le capital disponible (délégué au SessionManager).

        Returns:
            float: session_manager.get_capital_available()
        """
        return self.session_manager.get_capital_available()

    def get_capital_locked(self) -> float:
        """
        Retourne la marge verrouillée (délégué au SessionManager).

        Returns:
            float: session_manager.get_capital_locked()
        """
        return self.session_manager.get_capital_locked()

    def get_capital_total(self) -> float:
        """
        Retourne le solde total (délégué au SessionManager).

        Returns:
            float: session_manager.get_capital_total()
        """
        return self.session_manager.get_capital_total()

    def _has_sufficient_capital(self, required: float) -> bool:
        """Vérifie si capital disponible >= requis (via SessionManager)."""
        return self.session_manager.get_capital_available() >= required

    # ========================================================================
    # CALCULS - SLIPPAGE & VOLATILITÉ ATR
    # ========================================================================

    def _compute_volatility_from_atr(
        self,
        historical_data: pd.DataFrame,
        current_candle: dict,
        current_price: float
    ) -> float:
        """
        Calcule la volatilité normalisée à partir de l'ATR.

        [v2.6.9 — FIX-OS-ATR] Délègue en priorité à l'instance ATRIndicator
        injectée (self._atr_indicator), partagée avec RiskManager et
        PositionManager, pour garantir un slippage dynamique cohérent avec
        le reste du système (audit Phase 4/8, MAJEUR M3). Si aucune instance
        n'est injectée (trailing_stop.type='fixed' par exemple), fallback
        sur atr.calculate_atr_simple(period=ATR_DEFAULT_PERIOD) — comportement
        identique aux versions précédentes.

        Formule:
            volatility = ATR(period, method) / current_price

        Args:
            historical_data: DataFrame OHLCV (colonnes: high, low, close)
            current_candle:  Bougie courante (pour fallback prix)
            current_price:   Prix courant pour normalisation

        Returns:
            float: Volatilité normalisée (ex: 0.012 = 1.2%)
                   Retourne _VOLATILITY_FALLBACK si données insuffisantes.
        """
        try:
            if self._atr_indicator is not None:
                # Source de vérité unique : même instance que RiskManager/
                # PositionManager — même période, même méthode de smoothing.
                atr_series = self._atr_indicator.calculate_atr(historical_data)
            else:
                atr_series = calculate_atr_simple(
                    historical_data,
                    period=ATR_DEFAULT_PERIOD,
                    method=ATR_DEFAULT_METHOD
                )

            atr_value = atr_series.iloc[-1]

            if pd.isna(atr_value) or atr_value <= 0:
                self.logger.debug(
                    f"ATR NaN ou nul (len={len(historical_data)}, "
                    f"source={'ATRIndicator' if self._atr_indicator is not None else 'calculate_atr_simple'}), "
                    f"fallback volatilité: {_VOLATILITY_FALLBACK}"
                )
                return _VOLATILITY_FALLBACK

            ref_price = current_candle.get('close', current_price)
            if ref_price <= 0:
                return _VOLATILITY_FALLBACK

            return atr_value / ref_price

        except Exception as e:
            self.logger.warning(
                f"ATR computation failed: {e} — "
                f"fallback volatilité: {_VOLATILITY_FALLBACK}"
            )
            return _VOLATILITY_FALLBACK

    def _calculate_dynamic_slippage(self, volatility: float) -> float:
        """
        Calcule le slippage dynamique basé sur la volatilité ATR.

        Formule:
            slippage = slippage_base × (1 + volatility × 1.5)
            slippage = min(slippage, slippage_max)

        Args:
            volatility: Volatilité normalisée (ex: 0.012 = 1.2%)

        Returns:
            float: Slippage en decimal (ex: 0.00122 = 0.122%)
        """
        _MULTIPLIER = 1.5
        slippage    = self.slippage_base * (1 + volatility * _MULTIPLIER)
        return min(slippage, self.slippage_max)

    def calculate_funding_fees(
        self,
        position: dict,
        open_timestamp: datetime,
        close_timestamp: datetime,
    ) -> float:
        """
        Calcule les funding fees réels pour une position (futures perpetuals).

        Réalité Binance : les funding fees sont basés sur les timestamps UTC
        fixes traversés (00:00, 08:00, 16:00) et non sur la durée.

        Règle de déclenchement :
            open_timestamp < funding_time <= close_timestamp

        Args:
            position:        Position avec clé 'notional' (valeur en USDT)
            open_timestamp:  Datetime d'ouverture (naive=UTC ou timezone-aware)
            close_timestamp: Datetime de fermeture (naive=UTC ou timezone-aware)

        Returns:
            float: Montant total funding fees (USDT), arrondi à 4 décimales.

        Raises:
            ValueError: Si close_timestamp <= open_timestamp.

        Examples:
            >>> position = {'notional': 500.0}
            >>> fees = simulator.calculate_funding_fees(
            ...     position,
            ...     open_timestamp=datetime(2026, 1, 1, 7, 55),
            ...     close_timestamp=datetime(2026, 1, 1, 8, 5),
            ... )
            >>> print(f"Funding: {fees:.4f} USDT")   # → 0.0500 USDT
        """
        notional = position.get('notional', 0.0)
        if notional <= 0:
            return 0.0

        def _to_utc_naive(dt: datetime) -> datetime:
            if dt.tzinfo is not None:
                return dt.astimezone(timezone.utc).replace(tzinfo=None)
            return dt

        open_dt  = _to_utc_naive(open_timestamp)
        close_dt = _to_utc_naive(close_timestamp)

        if close_dt <= open_dt:
            raise ValueError(
                f"close_timestamp ({close_dt}) doit être postérieur à "
                f"open_timestamp ({open_dt})"
            )

        FUNDING_HOURS = (0, 8, 16)
        periods       = 0
        current_day   = open_dt.replace(hour=0, minute=0, second=0, microsecond=0)

        while current_day <= close_dt:
            for hour in FUNDING_HOURS:
                funding_time = current_day.replace(hour=hour)
                if open_dt < funding_time <= close_dt:
                    periods += 1
            current_day += timedelta(days=1)

        if periods == 0:
            return 0.0

        fees = notional * self.funding_rate_8h * periods

        self.logger.debug(
            f"Funding fees: {periods} période(s) × "
            f"{self.funding_rate_8h*100:.4f}% × {notional:.2f} = {fees:.4f} USDT"
        )

        return round(fees, 4)

    # ========================================================================
    # HELPERS
    # ========================================================================

    def _validate_order(self, order: dict) -> None:
        """Valide la structure d'un ordre."""
        required_fields = ['symbol', 'direction', 'size', 'collateral']
        missing = [f for f in required_fields if f not in order]

        if missing:
            raise ValueError(f"Missing required order fields: {missing}")

        if order['size'] <= 0:
            raise ValueError(f"Invalid size: {order['size']}")

        if order['collateral'] <= 0:
            raise ValueError(f"Invalid collateral: {order['collateral']}")

    def _generate_order_id(self) -> str:
        """Génère un ID ordre unique."""
        return f"SIM_{generate_id()}"

    def _simulate_latency(self) -> int:
        """
        Simule la latence API (pas de vraie attente).

        [v2.6.1 — FIX OS-2] Utilise self._rng (Random isolé) au lieu de
        random.randint() (PRNG global). Garantit le déterminisme du backtest
        sans affecter le comportement aléatoire d'autres composants du process.
        """
        if self.api_latency_ms > 0:
            return self._rng.randint(
                max(1, self.api_latency_ms - 100),
                self.api_latency_ms + 100
            )
        return 0

    @staticmethod
    def _make_empty_stats() -> Dict[str, Union[int, float]]:
        """Crée un dict stats vide."""
        return {
            'total_orders':   0,
            'market_orders':  0,
            'limit_orders':   0,
            'winning_trades': 0,
            'losing_trades':  0,
            'total_fees':     0.0,
            'total_slippage': 0.0,
        }

    # ========================================================================
    # EXPORT & STATS
    # ========================================================================

    def export_fills(
        self,
        output_path: Union[str, Path],
        fmt: str = 'json'
    ) -> Path:
        """
        Export fills vers JSON ou CSV.

        Args:
            output_path: Chemin fichier sortie
            fmt:         Format ('json' ou 'csv')

        Returns:
            Path fichier créé

        Raises:
            ValueError: Format non supporté

        Examples:
            >>> path = simulator.export_fills('results/fills.json')
            >>> print(f"Exported: {path}")
        """
        if fmt not in _VALID_EXPORT_FORMATS:
            raise ValueError(
                f"Invalid format: '{fmt}'. "
                f"Must be one of {_VALID_EXPORT_FORMATS}"
            )

        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        with self._lock:
            fills = deepcopy(self._fills_history)

        if fmt == 'json':
            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump(fills, f, indent=2, ensure_ascii=False, default=str)

        elif fmt == 'csv':
            if fills:
                keys = [
                    'order_id', 'symbol', 'side', 'type', 'status',
                    'fill_price', 'filled_size', 'fees', 'timestamp'
                ]
                with open(output_path, 'w', encoding='utf-8', newline='') as f:
                    writer = csv.DictWriter(f, fieldnames=keys, extrasaction='ignore')
                    writer.writeheader()
                    writer.writerows(fills)

        self.logger.info(
            f"Fills exported: {len(fills)} records → {output_path}"
        )
        return output_path.resolve()

    def get_statistics(self) -> Dict[str, Any]:
        """
        Retourne les statistiques de simulation avec ratios calculés.

        Les snapshots capital sont lus en temps réel depuis le SessionManager.

        Returns:
            Dict stats enrichi (win_rate, avg_slippage, capital snapshot)

        Examples:
            >>> stats = simulator.get_statistics()
            >>> print(f"Total fees: {stats['total_fees']:.2f} USDT")
            >>> print(f"Win rate: {stats['win_rate_pct']:.1f}%")
        """
        with self._lock:
            stats = self._stats.copy()

        total_trades = stats['winning_trades'] + stats['losing_trades']

        stats['win_rate_pct'] = (
            round((stats['winning_trades'] / total_trades) * 100, 2)
            if total_trades > 0 else 0.0
        )

        stats['avg_slippage'] = (
            round(stats['total_slippage'] / stats['market_orders'], 4)
            if stats['market_orders'] > 0 else 0.0
        )

        # Capital lu depuis la source de vérité
        stats['capital_total']     = self.get_capital_total()
        stats['capital_available'] = self.get_capital_available()
        stats['capital_locked']    = self.get_capital_locked()

        return stats

    def get_statistics_without_capital(self) -> Dict[str, Any]:
        """
        Retourne les statistiques de simulation SANS lire le capital.

        [v2.5.5 — FIX-TE-BUG2] Variante de get_statistics() sûre à appeler
        après end_session(), quand current_session=None dans SessionManager.
        Les champs capital (capital_total, capital_available, capital_locked)
        sont absents du dict retourné — ils doivent être fournis séparément
        via un capital_snapshot capturé avant end_session().

        Returns:
            Dict stats (win_rate, avg_slippage) sans champs capital.
        """
        with self._lock:
            stats = self._stats.copy()

        total_trades = stats['winning_trades'] + stats['losing_trades']

        stats['win_rate_pct'] = (
            round((stats['winning_trades'] / total_trades) * 100, 2)
            if total_trades > 0 else 0.0
        )

        stats['avg_slippage'] = (
            round(stats['total_slippage'] / stats['market_orders'], 4)
            if stats['market_orders'] > 0 else 0.0
        )

        # Pas d'accès SessionManager — capital_snapshot injecté par l'appelant
        return stats

    def get_fills_history(self) -> List[Dict]:
        """Retourne l'historique fills (copie thread-safe, ordre chronologique)."""
        with self._lock:
            return list(deepcopy(self._fills_history))

    def reset_statistics(self) -> None:
        """Réinitialise les statistiques."""
        with self._lock:
            self._stats = self._make_empty_stats()
        self.logger.info("Statistics reset")

    def reset_fills_history(self) -> None:
        """Vide l'historique fills."""
        with self._lock:
            self._fills_history.clear()
        self.logger.info("Fills history reset")

    def reset_all(self) -> None:
        """Réinitialise tout (stats + fills)."""
        self.reset_statistics()
        self.reset_fills_history()
        self.logger.info("OrderSimulator reset (all)")

    def __repr__(self) -> str:
        try:
            available = self.get_capital_available()
        except RuntimeError:
            available = 0.0
        return (
            f"OrderSimulator(mode={self.mode}, "
            f"capital_provider={type(self.session_manager).__name__}, "
            f"capital_available={available:.2f}, "
            f"fills={len(self._fills_history)})"
        )

# FIN DU MODULE
