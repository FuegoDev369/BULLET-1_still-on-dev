"""
BULLET-1 - Strategy Module
===========================

Orchestrateur central de la stratégie de trading BULLET-1.
Module critique qui coordonne signal_generator et risk_manager pour prendre
les décisions de trading complètes.

Version: 2.2.11
Date: 2026-03-13
Author: FuegoDev
Mode: ✅ Backtest | ✅ Paper | ✅ Live
Dépendances: signal_generator.py, risk_manager.py, logger.py
"""

import json
import threading
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
import pandas as pd
import sys

#trouver la racine du projet (root)
# [v2.2.10 — FIX-PATH-6] Résolution racine projet : pattern direct unifié.
# Remplace find_project_root() locale dupliquée dans ~10 modules (DRY).
# Calcul en 1 ligne depuis __file__ — sans fonction nommée, sans import.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# IMPORTS BULLET-1
from src.utils.logger import BulletLogger
from src.core.signal_generator import SignalGenerator
from src.core.risk_manager import RiskManager

# CONSTANTES

# [v2.2.8] Casse unifiée en MAJUSCULES — alignement avec MODE_CONFIGS de logger.py
# (était : {'backtest', 'paper', 'live'} → KeyError garanti sur tout accès direct à MODE_CONFIGS)
_VALID_MODES = frozenset({'BACKTEST', 'PAPER', 'LIVE'})
_VALID_SIGNAL_TYPES = frozenset({'LONG', 'SHORT', 'NONE'})

# [v2.2.11 — FIX-STR-2] Seuil d'erreurs consécutives avant arrêt fatal.
# Si signal_generator ou risk_manager échouent N fois de suite sans succès
# intermédiaire, le backtest s'arrête plutôt que de produire des résultats
# silencieusement invalides (ex: modèle cassé, données corrompues).
_MAX_CONSECUTIVE_ERRORS = 10


# [v2.2.11 — FIX-STR-2] Exception levée quand les sous-modules échouent
# de façon répétée. Permet à trading_engine de la catcher et d'arrêter
# la session proprement au lieu de continuer avec des None silencieux.
class StrategyFatalError(RuntimeError):
    """Levée quand signal_generator ou risk_manager échouent trop souvent."""
    pass

# CLASSE PRINCIPALE - Strategy

class Strategy:
    """
    Orchestrateur central de la stratégie de trading BULLET-1.
    
    Coordonne signal_generator et risk_manager pour produire des ordres
    de trading complets et validés.
    
    Architecture:
    - Découplage complet : strategy ne connaît que ses 2 dépendances directes
    - Thread-safe : toutes les opérations sur self._context sont protégées
    - Cache intelligent : évite recalculs inutiles sur mêmes candles
    - Validation multicouche : signal → risk → capital → conditions finales
    - Export complet : historique décisions en JSON/CSV
    
    Responsabilités:
    1. Orchestration : coordonne signal_generator + risk_manager
    2. Validation finale : capital, quality score, market conditions
    3. Contexte : maintient historique signaux et cache
    4. Statistiques : track décisions et performance
    5. Export : persiste décisions pour analyse
    
    Thread-safety:
        Toutes les opérations sur self._context et self._cache sont
        protégées par self._lock (RLock).
    
    Cache:
        Évite recalculs si même timestamp + même candle.
        Clé cache : f"{timestamp}_{candle['close']}"
    
    Attributes:
        logger (BulletLogger): Logger centralisé
        config (dict): Configuration complète BULLET-1
        mode (str): 'BACKTEST', 'PAPER', ou 'LIVE'
        signal_generator (SignalGenerator): Générateur de signaux
        risk_manager (RiskManager): Gestionnaire de risque
        min_quality_score (int): Score confiance minimum requis (0-100)
        _context (dict): Contexte stratégie (historique, stats)
        _cache (dict): Cache résultats analyse
        _lock (RLock): Lock thread-safety
    
    Examples:
        >>> from src.utils.config_loader import load_config
        >>> config = load_config()
        >>> 
        >>> signal_gen = SignalGenerator(config)
        >>> risk_mgr = RiskManager(config)
        >>> 
        >>> strategy = Strategy(config, signal_gen, risk_mgr, mode='backtest')
        >>> 
        >>> # Analyse candles
        >>> order = strategy.analyze(candles, current_balance=1000.0)
        >>> 
        >>> if order:
        ...     print(f"Signal: {order['direction']} @ {order['entry_price']}")
        ...     print(f"Size: {order['size']}, SL: {order['stop_loss']}")
        >>> 
        >>> # Stats
        >>> stats = strategy.get_statistics()
        >>> print(f"Signals générés: {stats['signals_generated']}")
    """
    
    def __init__(
        self,
        config: dict,
        signal_generator: SignalGenerator,
        risk_manager: RiskManager
    ):
        """
        Initialise la stratégie.
        
        Args:
            config: Configuration complète BULLET-1
            signal_generator: Instance SignalGenerator (injection)
            risk_manager: Instance RiskManager (injection)
        
        Raises:
            ValueError: Si mode absent/invalide, dépendances None,
                        ou clés config obligatoires manquantes
        
        Notes:
            - Injection de dépendances : strategy ne crée PAS ses dépendances
            - Thread-safe : peut être utilisé par plusieurs threads
            - Cache activé : améliore performance en backtest
            - mode extrait exclusivement via config['general']['mode']
            - min_quality_score contrôlé exclusivement via config['strategy']['min_quality_score']
        
        Examples:
            >>> config = load_config()
            >>> signal_gen = SignalGenerator(config)
            >>> risk_mgr = RiskManager(config)
            >>> 
            >>> strategy = Strategy(config, signal_gen, risk_mgr)        """
        # Validation mode (obligatoire — exclusivement via config)
        _mode = config.get('general', {}).get('mode')
        if not _mode:
            raise ValueError(
                "Configuration manquante : 'general.mode' est requis.\n"
                "➜ Vérifiez votre fichier de config et ajoutez :\n"
                '  "general": {\n'
                '      "mode": "BACKTEST"\n'
                '  }\n'
                "  (valeurs valides : 'BACKTEST', 'PAPER', 'LIVE')"
            )
        # [v2.2.8] Normalisation en majuscules avant validation et stockage.
        # Garantit que self.mode == BulletLogger.mode en casse (ex: 'BACKTEST').
        # Tolère les valeurs minuscules dans config.json ('backtest' → 'BACKTEST').
        _mode = _mode.upper()
        if _mode not in _VALID_MODES:
            raise ValueError(
                f"Invalid mode: '{_mode}'. Must be one of {_VALID_MODES}.\n"
                "➜ Vérifiez config['general']['mode'] dans votre fichier de config."
            )
        
        if signal_generator is None or risk_manager is None:
            raise ValueError(
                "signal_generator and risk_manager must not be None"
            )
        
        # Validation trading_pair (obligatoire — pas de fallback)
        _trading_pair = config.get('general', {}).get('trading_pair')
        if not _trading_pair:
            raise ValueError(
                "Configuration manquante : 'general.trading_pair' est requis.\n"
                "➜ Vérifiez votre fichier de config et ajoutez :\n"
                '  "general": {\n'
                '      "trading_pair": "BTC/USDT"\n'
                '  }'
            )
        
        # Logger
        self.logger = BulletLogger()
        
        # Configuration
        self.config = config
        self.mode = _mode
        self._trading_pair = _trading_pair
        
        # Dépendances (injection)
        self.signal_generator = signal_generator
        self.risk_manager = risk_manager
        
        # Score confiance minimum requis (obligatoire — exclusivement via config)
        # [v2.2.11 — FIX-STR-4] Validation renforcée : type numérique + plage [0, 100].
        _min_quality_score = config.get('strategy', {}).get('min_quality_score')
        if _min_quality_score is None:
            raise ValueError(
                "Configuration manquante : 'strategy.min_quality_score' est requis.\n"
                "➜ Vérifiez votre fichier de config et ajoutez :\n"
                '  "strategy": {\n'
                '      "min_quality_score": 60\n'
                '  }\n'
                "  (valeur recommandée : entre 40 et 80)"
            )
        if not isinstance(_min_quality_score, (int, float)):
            raise ValueError(
                f"'strategy.min_quality_score' doit être numérique, "
                f"reçu : {type(_min_quality_score).__name__} ({_min_quality_score!r})"
            )
        if not (0 <= _min_quality_score <= 100):
            raise ValueError(
                f"'strategy.min_quality_score' doit être dans [0, 100], "
                f"reçu : {_min_quality_score}"
            )
        self.min_quality_score = _min_quality_score
        
        # Thread-safety
        self._lock = threading.RLock()

        # [v2.2.11 — FIX-STR-2] Compteur d'erreurs consécutives des sous-modules.
        # Réinitialisé à 0 après chaque succès. Déclenche StrategyFatalError
        # si > _MAX_CONSECUTIVE_ERRORS pour éviter les runs silencieusement invalides.
        self._consecutive_errors: int = 0

        # Contexte stratégie
        self._context = {
            'last_signal': None,
            'last_signal_timestamp': None,
            'consecutive_same_direction': 0,
            'last_direction': None,
            'signals_history': [],
            'orders_history': [],
            'rejections_history': []
        }
        
        # Cache résultats (pour éviter recalculs)
        self._cache = {
            'last_cache_key': None,
            'last_result': None
        }
        
        # Statistiques
        self._stats = self._make_empty_stats()
        
        self.logger.info(
            f"Strategy initialized (mode: {self.mode}, "
            f"min_quality_score: {self.min_quality_score})"
        )
    
    # ========================================================================
    # MÉTHODE PRINCIPALE - ANALYZE
    # ========================================================================
    
    def analyze(
        self,
        candles: Union[pd.DataFrame, List[dict]],
        current_balance: float,
        prev_candle: Optional[dict] = None,
        force_recalculate: bool = False
    ) -> Optional[Dict[str, Any]]:
        """
        Analyse complète pour générer ordre de trading.
        
        Flow complet:
        1. Validation données entrée (candles, balance)
        2. Check cache (si non force_recalculate)
        3. Génération signal (via signal_generator)
        4. Validation signal (type, quality score)
        5. Calcul position params (via risk_manager)
        6. Validation finale (capital, market conditions)
        7. Construction ordre final
        8. Update contexte et stats
        9. Cache résultat
        
        Args:
            candles: Données historiques (DataFrame ou liste dicts)
                    Colonnes requises: open, high, low, close, volume, timestamp
                    Minimum: volume_lookback candles (ex: 20)
            current_balance: Capital disponible en USDT,doit venir de session_manager.py
            prev_candle: Bougie précédente (optionnel, pour validation market)
            force_recalculate: Force recalcul même si cache valide
        
        Returns:
            None si pas de signal ou signal rejeté
            Dict order si signal valide
        
        Raises:
            ValueError: Si candles invalides ou balance <= 0
        
        Notes:
            - Thread-safe : peut être appelé par plusieurs threads
            - Cache : évite recalcul si même timestamp/candle
            - Logging : toutes décisions loggées (NONE, LONG, SHORT, REJECTED)
            - Stats : toutes analyses comptabilisées
        
        Examples:
            >>> # Analyse simple
            >>> order = strategy.analyze(candles, balance=1000.0)
            >>> 
            >>> if order:
            ...     print(f"Trade: {order['direction']} @ {order['entry_price']}")
            ... else:
            ...     print("No signal ou signal rejeté")
            >>> 
            >>> # Avec prev_candle pour validation market
            >>> order = strategy.analyze(
            ...     candles,
            ...     balance=1000.0,
            ...     prev_candle=previous_candle,
            ...     force_recalculate=True
            ... )
        """
        # ── Étape 1: Validation entrée ──────────────────────────────────
        
        try:
            current_candle, candles_df = self._validate_and_prepare_input(
                candles, current_balance
            )
        except ValueError as e:
            self.logger.error(f"Input validation failed: {e}")
            with self._lock:
                self._stats['errors'] += 1
            return None
        
        # ── Étape 2: Check cache ────────────────────────────────────────
        
        if not force_recalculate:
            cached_result = self._check_cache(current_candle)
            if cached_result is not None:
                self.logger.debug("Returning cached result")
                with self._lock:
                    self._stats['cache_hits'] += 1
                return cached_result
        
        # ── Étape 3: Génération signal ──────────────────────────────────

        with self._lock:
            self._stats['total_analyzed'] += 1

        try:
            signal = self.signal_generator.generate_signal(candles_df, current_candle)
            # [v2.2.11 — FIX-STR-2] Succès → reset compteur erreurs consécutives.
            self._consecutive_errors = 0
        except Exception as e:
            self._consecutive_errors += 1
            self.logger.error(
                f"Signal generation error ({self._consecutive_errors}/"
                f"{_MAX_CONSECUTIVE_ERRORS}): {e}",
                exc_info=True
            )
            with self._lock:
                self._stats['errors'] += 1
            # [v2.2.11 — FIX-STR-2] Trop d'erreurs consécutives → arrêt fatal.
            if self._consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
                raise StrategyFatalError(
                    f"signal_generator a échoué {self._consecutive_errors} fois "
                    f"consécutivement. Vérifiez les données et la configuration."
                )
            return None
        
        # ── Étape 4: Validation signal ──────────────────────────────────
        
        # [FIX-2 v2.2.6] Suppression du check `signal is None` : generate_signal() retourne
        # toujours un dict (au minimum via _create_none_signal()), jamais None.
        # Le check mort masquait l'intention réelle et pouvait induire en erreur à la maintenance.
        # La ligne `reason` est nettoyée en conséquence (suppression de la branche `if signal`).
        if signal['side'] == 'NONE':
            reason = signal.get('reason', 'no_signal')
            self._log_rejection('NONE', reason, current_candle, None)
            return self._cache_and_return(current_candle, None)
        
        # Validation type signal
        if signal['side'] not in _VALID_SIGNAL_TYPES:
            self.logger.error(f"Invalid signal side: {signal['side']}")
            with self._lock:
                self._stats['errors'] += 1
            return None
        
        # Validation quality score
        if signal['confidence'] < self.min_quality_score:
            reason = (
                f"quality_too_low:{signal['confidence']}<{self.min_quality_score}"
            )
            self._log_rejection(signal['side'], reason, current_candle, signal)
            return self._cache_and_return(current_candle, None)
        
        # ── Étape 5: Calcul position parameters ─────────────────────────

        try:
            position_params = self._calculate_position_params(
                signal=signal,
                current_balance=current_balance,
                current_candle=current_candle
            )
            # [v2.2.11 — FIX-STR-2] Succès → reset compteur erreurs consécutives.
            self._consecutive_errors = 0
        except Exception as e:
            self._consecutive_errors += 1
            self.logger.error(
                f"Position params calculation error ({self._consecutive_errors}/"
                f"{_MAX_CONSECUTIVE_ERRORS}): {e}",
                exc_info=True
            )
            with self._lock:
                self._stats['errors'] += 1
            # [v2.2.11 — FIX-STR-2] Trop d'erreurs consécutives → arrêt fatal.
            if self._consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
                raise StrategyFatalError(
                    f"risk_manager.compute_position() a échoué {self._consecutive_errors} "
                    f"fois consécutivement. Vérifiez la configuration du risk manager."
                )
            return None
        
        # ── Étape 6: Validation finale (market conditions via risk_manager) ──
        #
        # [v2.2.7 — FIX-1] Step 6a (capital check) supprimé — dead code confirmé.
        # Condition `current_balance < position_params['collateral']` impossible :
        #   collateral = capital × (collateral_pct / 100)
        #   → collateral < capital tant que collateral_pct < 100% (toujours le cas).
        # Guard structurellement jamais déclenché → retiré sans perte de sécurité.
        # La vérification des limites de position (min/max notional) reste assurée
        # par risk_manager.validate_position_entry() ci-dessous.

        validation_result = self.risk_manager.validate_position_entry(
            signal=signal,
            candle=current_candle,
            capital=current_balance,
            prev_candle=prev_candle,
            historical_data=candles_df
        )
        
        is_valid, validation_reason = validation_result
        
        if not is_valid:
            reason = f"market_conditions:{validation_reason}"
            self._log_rejection(signal['side'], reason, current_candle, signal)
            return self._cache_and_return(current_candle, None)
        
        # ── Étape 7: Construction ordre final ───────────────────────────
        
        order = self._build_order(
            signal=signal,
            position_params=position_params,
            current_candle=current_candle
        )
        
        # ── Étape 8: Update contexte et stats ───────────────────────────
        
        self._update_context_after_signal(signal, order)
        
        with self._lock:
            self._stats['signals_generated'] += 1
            if signal['side'] == 'LONG':
                self._stats['signals_long'] += 1
            else:
                self._stats['signals_short'] += 1
        
        # ── Étape 9: Log et cache ───────────────────────────────────────
        
        self.logger.info(
            f"ORDER GENERATED: {order['direction']} @ {order['entry_price']:.2f} | "
            f"Size: {order['size']:.8f} | SL: {order['stop_loss']:.2f} | "
            f"TP: {order['take_profit']:.2f} | Quality: {order['quality_score']}"
        )
        
        return self._cache_and_return(current_candle, order)
    
    # ========================================================================
    # MÉTHODES PRIVÉES - HELPERS
    # ========================================================================
    
    def _validate_and_prepare_input(
        self,
        candles: Union[pd.DataFrame, List[dict]],
        current_balance: float
    ) -> Tuple[dict, pd.DataFrame]:
        """
        Valide et prépare les données d'entrée.
        
        Args:
            candles: Données candles
            current_balance: Capital disponible
        
        Returns:
            Tuple (current_candle dict, candles DataFrame)
        
        Raises:
            ValueError: Si données invalides
        """
        # Validation balance
        if current_balance <= 0:
            raise ValueError(f"Invalid balance: {current_balance}")
        
        # Conversion en DataFrame si nécessaire
        if isinstance(candles, list):
            if len(candles) == 0:
                raise ValueError("Empty candles list")
            candles_df = pd.DataFrame(candles)
        elif isinstance(candles, pd.DataFrame):
            if candles.empty:
                raise ValueError("Empty candles DataFrame")
            candles_df = candles
        else:
            raise ValueError(
                f"Invalid candles type: {type(candles)}. "
                "Expected DataFrame or list of dicts"
            )
        
        # Validation colonnes requises
        # [FIX-1 v2.2.6] 'timestamp' ajouté : signal_generator.generate_signal() l'exige
        # explicitement. Sans cette colonne, un ValueError était levé dans le bloc
        # try/except de l'étape 3 et avalé silencieusement, retournant None sans trace claire.
        required_cols = ['open', 'high', 'low', 'close', 'volume', 'timestamp']
        missing = [col for col in required_cols if col not in candles_df.columns]
        if missing:
            raise ValueError(f"Missing required columns: {missing}")
        
        # Récupérer candle courante (dernière)
        current_candle = candles_df.iloc[-1].to_dict()
        
        # Validation volume_lookback
        min_required = self.signal_generator.volume_lookback
        if len(candles_df) < min_required:
            raise ValueError(
                f"Insufficient candles: {len(candles_df)} < {min_required} required"
            )
        
        return current_candle, candles_df
    
    def _check_cache(self, current_candle: dict) -> Optional[Dict]:
        """
        Vérifie si résultat en cache pour cette candle.
        
        Args:
            current_candle: Candle à analyser
        
        Returns:
            None si pas de cache valide
            Dict order si cache trouvé
        """
        cache_key = self._generate_cache_key(current_candle)
        
        with self._lock:
            if self._cache['last_cache_key'] == cache_key:
                return deepcopy(self._cache['last_result'])
        
        return None
    
    def _generate_cache_key(self, candle: dict) -> str:
        """Génère clé de cache unique pour une candle."""
        timestamp = candle.get('timestamp', datetime.now(timezone.utc))
        close = candle['close']
        return f"{timestamp}_{close}"
    
    def _cache_and_return(
        self,
        current_candle: dict,
        result: Optional[Dict]
    ) -> Optional[Dict]:
        """
        Met en cache le résultat et le retourne.
        
        Args:
            current_candle: Candle analysée
            result: Résultat à cacher
        
        Returns:
            result (copie profonde si non None)
        """
        cache_key = self._generate_cache_key(current_candle)
        
        with self._lock:
            self._cache['last_cache_key'] = cache_key
            self._cache['last_result'] = deepcopy(result) if result else None
        
        return deepcopy(result) if result else None
    
    def _calculate_position_params(
        self,
        signal: dict,
        current_balance: float,
        current_candle: dict
    ) -> Dict[str, Any]:
        """
        Délègue le calcul complet des paramètres position à risk_manager.

        [v2.2.7 — FIX-2] Refactorisation : Strategy orchestre, elle ne calcule pas.
        Avant v2.2.7, cette méthode appelait directement calculate_position_size()
        puis calculate_sl_tp() — deux appels distincts, logique de calcul côté Strategy.
        Ces appels sont maintenant centralisés dans risk_manager.compute_position(),
        point d'entrée unique. Strategy se contente de transmettre les paramètres
        et de retourner le résultat.

        Args:
            signal: Signal généré (avec entry_price, side)
            current_balance: Capital disponible
            current_candle: Candle courante (conservé pour compatibilité signature)

        Returns:
            Dict avec: collateral, notional, size, stop_loss, take_profit,
                       risk, reward, rr_ratio, leverage

        Raises:
            Exception: Si calcul échoue (propagé depuis risk_manager)
        """
        # [v2.2.7 — FIX-2] Appel unique vers risk_manager.compute_position().
        # Remplace les anciens appels directs à calculate_position_size() +
        # calculate_sl_tp() qui dupliquaient une logique de calcul dans Strategy.
        return self.risk_manager.compute_position(
            capital=current_balance,
            side=signal['side'],
            entry_price=signal['entry_price']
        )
    
    def _build_order(
        self,
        signal: dict,
        position_params: dict,
        current_candle: dict
    ) -> Dict[str, Any]:
        """
        Construit ordre final complet.
        
        Args:
            signal: Signal généré
            position_params: Paramètres position (size, SL, TP, etc.)
            current_candle: Candle courante
        
        Returns:
            Dict ordre complet prêt pour exécution
        """
        order = {
            # Infos trading essentielles
            'symbol': self._trading_pair,
            'direction': signal['side'],
            'entry_price': signal['entry_price'],
            'entry_type': 'MARKET',
            'size': position_params['size'],
            'stop_loss': position_params['stop_loss'],
            'take_profit': position_params['take_profit'],
            
            # Infos capital et leverage
            'collateral': position_params['collateral'],
            'notional': position_params['notional'],
            'leverage': position_params['leverage'],
            
            # Qualité signal
            'quality_score': signal['confidence'],
            
            # [v2.2.9 — FIX S-2] UTC explicite pour cohérence chronologique des trades.
            # datetime.now() retournait l'heure locale, incohérent avec les timestamps
            # OHLCV UTC. Impacte la traçabilité des exports JSON/CSV.
            'timestamp': signal.get('timestamp', datetime.now(timezone.utc)),
            
            # Stratégie
            'configuration_name': self.signal_generator.configuration_name,
            
            # Raison
            'reason': signal.get('reason', 'signal_valid'),
            
            # Metadata complète
            'metadata': {
                'signal_confidence': signal['confidence'],
                'signal_reason': signal.get('reason', ''),
                'risk_reward_ratio': position_params['rr_ratio'],
                'risk': position_params['risk'],
                'reward': position_params['reward'],
                'sl_offset_pct': self.risk_manager.sl_offset_pct,
                'indicators': signal.get('indicators', {}),
                'mode': self.mode
            }
        }
        
        return order
    
    def _log_rejection(
        self,
        signal_type: str,
        reason: str,
        current_candle: dict,
        signal: Optional[dict]
    ):
        """
        Log signal rejeté et update stats.
        
        Args:
            signal_type: Type signal ('NONE', 'LONG', 'SHORT')
            reason: Raison rejet
            current_candle: Candle courante
            signal: Signal (ou None si NONE)
        """
        rejection_record = {
            'type': signal_type,
            'reason': reason,
            'timestamp': current_candle.get('timestamp', datetime.now(timezone.utc)),
            'price': current_candle['close'],
            'confidence': signal['confidence'] if signal else 0
        }
        
        with self._lock:
            self._context['rejections_history'].append(rejection_record)
            self._stats['signals_rejected'] += 1

            if signal_type == 'NONE':
                self._stats['signals_none'] += 1

        # [v2.2.11 — FIX-STR-3] Rejets non-NONE loggés en INFO (était DEBUG).
        # Les rejets NONE (pas de signal) restent en DEBUG car très fréquents.
        # Les rejets quality_too_low et market_conditions sont INFO : ils
        # indiquent que la stratégie a généré un signal mais l'a filtré —
        # information utile pour analyser les performances post-backtest.
        if signal_type == 'NONE':
            self.logger.debug(
                f"No signal | Reason: {reason} | Price: {current_candle['close']:.2f}"
            )
        else:
            self.logger.info(
                f"Signal rejected: {signal_type} | Reason: {reason} | "
                f"Price: {current_candle['close']:.2f} | "
                f"Confidence: {signal['confidence'] if signal else 'N/A'}"
            )
    
    def _update_context_after_signal(self, signal: dict, order: dict):
        """
        Met à jour contexte après génération signal valide.
        
        Args:
            signal: Signal généré
            order: Ordre construit
        """
        with self._lock:
            # Update last signal
            self._context['last_signal'] = deepcopy(signal)
            self._context['last_signal_timestamp'] = signal.get(
                'timestamp', datetime.now(timezone.utc)
            )
            
            # Track direction consecutive
            if self._context['last_direction'] == signal['side']:
                self._context['consecutive_same_direction'] += 1
            else:
                self._context['consecutive_same_direction'] = 1
            
            self._context['last_direction'] = signal['side']
            
            # Historique
            self._context['signals_history'].append(deepcopy(signal))
            self._context['orders_history'].append(deepcopy(order))
    
    @staticmethod
    def _make_empty_stats() -> Dict[str, int]:
        """Crée dict stats vide."""
        return {
            'total_analyzed': 0,
            'signals_generated': 0,
            'signals_long': 0,
            'signals_short': 0,
            'signals_none': 0,
            'signals_rejected': 0,
            'cache_hits': 0,
            'errors': 0
        }
    
    # ========================================================================
    # MÉTHODES PUBLIQUES - UTILS
    # ========================================================================
    
    def get_statistics(self) -> Dict[str, Any]:
        """
        Retourne statistiques complètes thread-safe.
        
        Returns:
            Dict avec stats + ratios calculés
        
        Examples:
            >>> stats = strategy.get_statistics()
            >>> print(f"Total analysées: {stats['total_analyzed']}")
            >>> print(f"Taux signaux: {stats['signal_rate_pct']:.2f}%")
            >>> print(f"Hit rate cache: {stats['cache_hit_rate_pct']:.2f}%")
        """
        with self._lock:
            stats = self._stats.copy()
            
            total = stats['total_analyzed']
            
            # Calcul ratios
            if total > 0:
                stats['signal_rate_pct'] = round(
                    (stats['signals_generated'] / total) * 100, 2
                )
                stats['rejection_rate_pct'] = round(
                    (stats['signals_rejected'] / total) * 100, 2
                )
                stats['none_rate_pct'] = round(
                    (stats['signals_none'] / total) * 100, 2
                )
                stats['cache_hit_rate_pct'] = round(
                    (stats['cache_hits'] / total) * 100, 2
                )
            else:
                stats['signal_rate_pct'] = 0.0
                stats['rejection_rate_pct'] = 0.0
                stats['none_rate_pct'] = 0.0
                stats['cache_hit_rate_pct'] = 0.0
            
            generated = stats['signals_generated']
            if generated > 0:
                stats['long_ratio_pct'] = round(
                    (stats['signals_long'] / generated) * 100, 2
                )
                stats['short_ratio_pct'] = round(
                    (stats['signals_short'] / generated) * 100, 2
                )
            else:
                stats['long_ratio_pct'] = 0.0
                stats['short_ratio_pct'] = 0.0
        
        return stats
    
    def get_context(self) -> Dict[str, Any]:
        """
        Retourne contexte stratégie complet thread-safe.
        
        Returns:
            Dict avec last_signal, consecutive_same_direction, historiques, etc.
        
        Examples:
            >>> context = strategy.get_context()
            >>> print(f"Dernier signal: {context['last_signal']}")
            >>> print(f"Consecutive: {context['consecutive_same_direction']}")
        """
        with self._lock:
            return deepcopy(self._context)
    
    def get_last_signal(self) -> Optional[Dict]:
        """Retourne dernier signal généré (copie thread-safe)."""
        with self._lock:
            if self._context['last_signal']:
                return deepcopy(self._context['last_signal'])
        return None
    
    def get_signals_history(self) -> List[Dict]:
        """Retourne historique signaux (copie thread-safe)."""
        with self._lock:
            return deepcopy(self._context['signals_history'])
    
    def get_orders_history(self) -> List[Dict]:
        """Retourne historique ordres (copie thread-safe)."""
        with self._lock:
            return deepcopy(self._context['orders_history'])
    
    def get_rejections_history(self) -> List[Dict]:
        """Retourne historique rejets (copie thread-safe)."""
        with self._lock:
            return deepcopy(self._context['rejections_history'])
    
    def reset_statistics(self):
        """Réinitialise statistiques."""
        with self._lock:
            self._stats = self._make_empty_stats()
        self.logger.info("Statistics reset")
    
    def reset_context(self):
        """Réinitialise contexte (sauf stats)."""
        with self._lock:
            self._context = {
                'last_signal': None,
                'last_signal_timestamp': None,
                'consecutive_same_direction': 0,
                'last_direction': None,
                'signals_history': [],
                'orders_history': [],
                'rejections_history': []
            }
        self.logger.info("Context reset")
    
    def reset_cache(self):
        """Réinitialise cache."""
        with self._lock:
            self._cache = {
                'last_cache_key': None,
                'last_result': None
            }
        self.logger.debug("Cache reset")
    
    def reset_session(self) -> None:
        """
        Réinitialise le contexte et le cache entre deux sessions de backtest.

        [v2.2.10 — FIX-STR-1] Résout l'accumulation inter-sessions :
        - _context['signals_history'] et 'orders_history' croissaient sans borne
          en mémoire sur un backtest multi-sessions (historique session N-1 polluait N).
        - _context['consecutive_same_direction'] portait l'état directionnel de la
          session précédente vers la suivante, faussant la logique de signal.
        - Le cache conservait une clé stale de la dernière candle de la session
          précédente, pouvant provoquer un faux cache-hit en début de session N.

        Les statistiques globales (_stats) sont intentionnellement CONSERVÉES :
        elles agrègent la performance de l'ensemble du backtest multi-sessions
        et sont consommées par EngineRunResult.strategy_stats après chaque session.

        Appelée par TradingEngine._reset_session_state() avant chaque run_session().

        Notes:
            - Thread-safe : protégée par self._lock (RLock).
            - Ne pas confondre avec reset_all() qui efface aussi les stats.
        """
        with self._lock:
            self._context = {
                'last_signal':                None,
                'last_signal_timestamp':      None,
                'consecutive_same_direction': 0,
                'last_direction':             None,
                'signals_history':            [],
                'orders_history':             [],
                'rejections_history':         [],
            }
            self._cache = {
                'last_cache_key': None,
                'last_result':    None,
            }
        # [v2.2.11 — FIX-STR-2] Reset compteur erreurs consécutives entre sessions.
        # Une session qui se termine avec des erreurs ne doit pas contaminer
        # le compteur de la session suivante.
        self._consecutive_errors = 0
        self.logger.info(
            "[reset_session] Context + cache + error counter réinitialisés "
            "pour nouvelle session (stats globales conservées)"
        )

    def reset_all(self):
        """Réinitialise tout (stats + contexte + cache)."""
        self.reset_statistics()
        self.reset_context()
        self.reset_cache()
        self.logger.info("Strategy reset (all)")
    
    # ========================================================================
    # EXPORT
    # ========================================================================
    
    def export_orders(
        self,
        output_path: Union[str, Path],
        fmt: str = 'json'
    ) -> Path:
        """
        Export historique ordres vers JSON ou CSV.
        
        Args:
            output_path: Chemin fichier sortie
            fmt: Format ('json' ou 'csv')
        
        Returns:
            Path fichier créé
        
        Examples:
            >>> # Export JSON
            >>> path = strategy.export_orders('results/orders.json')
            >>> 
            >>> # Export CSV
            >>> path = strategy.export_orders('results/orders.csv', fmt='csv')
        """
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        with self._lock:
            orders = deepcopy(self._context['orders_history'])
        
        if fmt == 'json':
            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump(orders, f, indent=2, ensure_ascii=False, default=str)
        
        elif fmt == 'csv':
            if orders:
                import csv
                keys = ['symbol', 'direction', 'entry_price', 'size',
                       'stop_loss', 'take_profit', 'quality_score', 'timestamp']
                
                with open(output_path, 'w', encoding='utf-8', newline='') as f:
                    writer = csv.DictWriter(f, fieldnames=keys, extrasaction='ignore')
                    writer.writeheader()
                    writer.writerows(orders)
        
        else:
            raise ValueError(f"Invalid format: {fmt}. Use 'json' or 'csv'")
        
        self.logger.info(f"Orders exported: {len(orders)} records → {output_path}")
        return output_path.resolve()
    
    def export_rejections(
        self,
        output_path: Union[str, Path],
        fmt: str = 'json'
    ) -> Path:
        """
        Export historique rejets vers JSON ou CSV.
        
        Args:
            output_path: Chemin fichier sortie
            fmt: Format ('json' ou 'csv')
        
        Returns:
            Path fichier créé
        """
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        with self._lock:
            rejections = deepcopy(self._context['rejections_history'])
        
        if fmt == 'json':
            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump(rejections, f, indent=2, ensure_ascii=False, default=str)
        
        elif fmt == 'csv':
            if rejections:
                import csv
                keys = ['type', 'reason', 'timestamp', 'price', 'confidence']
                
                with open(output_path, 'w', encoding='utf-8', newline='') as f:
                    writer = csv.DictWriter(f, fieldnames=keys)
                    writer.writeheader()
                    writer.writerows(rejections)
        
        else:
            raise ValueError(f"Invalid format: {fmt}. Use 'json' or 'csv'")
        
        self.logger.info(
            f"Rejections exported: {len(rejections)} records → {output_path}"
        )
        return output_path.resolve()
    
    # ========================================================================
    # INFO
    # ========================================================================
    
    def get_configuration_info(self) -> Dict[str, Any]:
        """Retourne infos configuration stratégie."""
        return {
            'version': '2.2.11',
            'mode': self.mode,
            'min_quality_score': self.min_quality_score,
            'signal_generator': self.signal_generator.get_configuration_details(),
            'risk_manager': {
                'leverage': self.risk_manager.leverage,
                'collateral_pct': self.risk_manager.collateral_pct,
                'rr_ratio': self.risk_manager.rr_ratio,
                'sl_offset_pct': self.risk_manager.sl_offset_pct
            }
        }
    
    def __repr__(self) -> str:
        """Représentation string."""
        return (
            f"Strategy(mode={self.mode}, "
            f"min_quality_score={self.min_quality_score}, "
            f"config={self.signal_generator.configuration_name})"
        )
# FIN DU MODULE
