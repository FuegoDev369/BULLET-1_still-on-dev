"""
Position Manager 

Features:
- Trailing stop: candle/atr/hybrid modes
- Protection 1R asymétrique
- Progressive tightening
- Calcul PnL Net (frais complets)
- Tracking trailing history
- ⚡ Cache ATR par timestamp (multi-positions)

Dependencies: logger, atr, helpers
Required by: engine, trading_bot
Mode: backtest | paper | live

version: 2.6.5
Auteur: FuegoDev
Date: 2026-03-13
"""

import pandas as pd
import numpy as np
import uuid  # [v2.6.1 — FIX PM-2] Pour position_id garanti unique (remplace timestamp float)
from typing import Optional, Dict, Any, Literal, Union, List, Tuple
from datetime import datetime, timezone  # [v2.6.2] Ajout timezone pour datetime.now(timezone.utc)
from collections import OrderedDict
from pathlib import Path
import sys

# trouvé la racine du projet
# [v2.6.4 — FIX-PATH-6] Résolution racine projet : pattern direct unifié.
# Remplace find_project_root() locale dupliquée dans ~10 modules (DRY).
# Calcul en 1 ligne depuis __file__ — sans fonction nommée, sans import.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
    
# BULLET-1 IMPORTS
from src.utils.logger import BulletLogger
from src.utils.helpers import safe_divide, clamp
from src.indicators.atr import ATRIndicator

#: Version du module — utilisée dans get_state() pour cohérence automatique
_VERSION = "2.6.5"
# [v2.6.0] Supprimé : from src.utils.config_loader import load_config, BulletConfig
# Raison : PM ne charge plus sa config lui-même — Dependency Injection pattern.
# C'est le module appelant qui charge la config
# via load_config() et la passe en paramètre. Chaque module reçoit.


class PositionManager:
    """
    Gestionnaire positions avec trailing stop ultra-configurable et optimisations performance.
    
    ⚡  PERFORMANCE CRITICAL :
    ATR doit être pré-calculé et passé via atr_value parameter.
    Recalcul dynamique supprimé (trop lent pour backtest long terme).
    
    Responsabilités principales:
    - Tracking positions ouvertes/fermées avec metadata complète
    - Calcul PnL Net temps réel (PnL Brut - Frais Totaux)
    - Trailing stop triple mode (candle/atr/hybrid)
    - Protection 1R configurable asymétrique
    - Progressive tightening & volatility adaptation
    - Accumulation funding fees pour simulation réaliste
    - Cache ATR par timestamp (évite recalculs multiples)
    
    PnL Net Calculation:
        PnL Brut = (Exit - Entry) × Size
        Frais Totaux = entry_fees + exit_fees + funding_fees
        PnL Net = PnL Brut - Frais Totaux
    
    Attributes:
        logger: Logger centralisé
        config: Configuration validée (dict BULLET-1)
        atr: Instance ATRIndicator (si type='atr'/'hybrid')
        positions: Positions actives (OrderedDict)
        closed_positions: Historique positions fermées
        _atr_cache: Cache ATR par timestamp (performance)
    """
    
    def __init__(self, config: dict, atr_indicator: Optional[ATRIndicator] = None):
        # [v2.6.0] config: BulletConfig → config: dict
        # Le module appelant est responsable du chargement (Dependency Injection).
        """
        Initialiser PositionManager.
        
        Args:
            config: dict de configuration BULLET-1 (chargé et passé par le module appelant)
            atr_indicator: Instance ATRIndicator (requis si type='atr'/'hybrid')
        
        Raises:
            TypeError: Si config n'est pas un dict
            ValueError: Si type='atr'/'hybrid' mais ATR manquant
        """
        self.logger = BulletLogger()
        
        # [v2.6.0] Guard : isinstance(..., BulletConfig) → isinstance(..., dict)
        if not isinstance(config, dict):
            raise TypeError(
                f"config must be dict, got: {type(config).__name__}. "
                f"Use: pass a dict loaded by the caller (e.g. engine.py)"
            )
        
        self.config = config
        # [v2.6.0] dot-notation BulletConfig → bracket notation dict standard
        trailing = self.config['strategy']['trailing_stop']
        
        # [v2.6.0] trailing.type → trailing['type']
        if trailing['type'] in ['atr', 'hybrid']:
            if atr_indicator is None:
                raise ValueError(
                    f"ATRIndicator REQUIRED for trailing type '{trailing['type']}'"
                )
            self.atr = atr_indicator
        else:
            self.atr = None
        
        self.positions: OrderedDict[str, Dict[str, Any]] = OrderedDict()
        self.closed_positions: list = []
        
        # ⚡ Cache ATR par timestamp (performance)
        self._atr_cache: Dict[Any, float] = {}
        self._atr_cache_max_size = 50  # Limiter mémoire
        
        self.logger.info(
            f"PositionManager  OPTIMIZED initialized: "
            f"trailing={trailing['type']}, "
            f"1r_protection={trailing['protection_1r']['auto_activate']}, "
            f"progressive={trailing['atr_mode']['progressive_tightening']}, "
            f"volatility_adjust={trailing['volatility_adjustment']['enabled']}, "
            f"atr_cache_enabled=True"
        )
    
    # --- Helpers internes ---

    @staticmethod
    def _ensure_utc(dt: datetime) -> datetime:
        """
        Normalise un datetime en UTC-aware.

        [v2.6.5 — FIX-PM-4] Nécessaire pour éviter TypeError lors des
        comparaisons temporelles entre datetimes naïfs et UTC-aware.
        Naïf → assume UTC (convention backtest CSV). Non-UTC → converti.

        Args:
            dt: Datetime à normaliser.

        Returns:
            datetime UTC-aware.
        """
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    # --- Positions ---
    
    def open_position(self, position_data: Dict[str, Any]) -> str:
        """
        Ouvrir nouvelle position.
        
        Args:
            position_data: Dict avec données position
                Required: direction, entry_price, size, stop_loss, take_profit,
                         entry_time, collateral, leverage, entry_fees
        
        Returns:
            str: position_id unique
        
        Raises:
            ValueError: Si entry_fees manquant ou < 0
        """
        if 'entry_fees' not in position_data:
            raise ValueError(
                "position_data MUST include 'entry_fees' (from order_simulator)"
            )
        
        if position_data['entry_fees'] < 0:
            raise ValueError(f"entry_fees must be >= 0, got: {position_data['entry_fees']}")
        
        # [v2.6.1 — FIX PM-2] UUID hex pour position_id garanti unique.
        # L'ancienne implémentation utilisait f"pos_{entry_time.timestamp()}"
        # → collision possible si deux positions s'ouvrent dans la même seconde.
        # Format : lisible (timestamp ms) + unicité (hex court)
        ts_ms = int(position_data['entry_time'].timestamp() * 1000)
        position_id = f"pos_{ts_ms}_{uuid.uuid4().hex[:8]}"
        
        # [v2.6.1 — FIX PM-1] Renommage champs pour alignement cross-module :
        # side → direction | sl_price → stop_loss | tp_price → take_profit
        # initial_sl_price → initial_stop_loss
        # Convention unifiée avec strategy.py et order_simulator.py.
        position_data['initial_stop_loss'] = position_data['stop_loss']
        position_data['1r_protection_active'] = False
        position_data['position_id'] = position_id
        position_data['status'] = 'open'
        
        # [v2.6.0] dot-notation → bracket notation dict
        trailing = self.config['strategy']['trailing_stop']
        position_data['trailing_mode_switched'] = False
        position_data['current_trailing_mode'] = (
            # [v2.6.0] trailing.hybrid_mode.start_with / trailing.type → bracket notation
            trailing['hybrid_mode']['start_with'] if trailing['type'] == 'hybrid' else trailing['type']
        )
        position_data['volatility_state'] = 'normal'
        position_data['notional'] = position_data['entry_price'] * position_data['size']
        
        position_data['exit_fees'] = 0.0
        position_data['funding_fees'] = 0.0
        position_data['total_fees'] = position_data['entry_fees']
        position_data['trailing_history'] = []
        
        self.positions[position_id] = position_data
        
        self.logger.info(
            f"Position opened: {position_id}, {position_data['direction']}, "
            f"entry={position_data['entry_price']:.2f}, "
            f"sl={position_data['stop_loss']:.2f}, tp={position_data['take_profit']:.2f}, "
            f"size={position_data['size']:.6f}, fees={position_data['entry_fees']:.4f}"
        )
        
        return position_id
    
    def close_position(
        self, 
        position_id: str, 
        exit_price: float, 
        exit_time: datetime,
        reason: Literal['SL', 'TP', 'manual', 'session_end'],
        exit_fees: float,
        funding_fees: float = 0.0
    ) -> Dict[str, Any]:
        """
        Fermer position et calculer PnL Net.
        
        Formula: PnL Net = PnL Brut - (entry_fees + exit_fees + funding_fees)
        
        Args:
            position_id: ID position
            exit_price: Prix sortie
            exit_time: Timestamp sortie
            reason: 'SL', 'TP', 'manual', 'session_end'
            exit_fees: Frais sortie (requis)
            funding_fees: Frais funding cumulés (default=0)
        
        Returns:
            dict: Position fermée avec 'pnl', 'pnl_brut', 'total_fees', etc.
        
        Raises:
            ValueError: Si position non trouvé, prix invalide, ou frais < 0
        """
        if position_id not in self.positions:
            raise ValueError(f"Position not found: {position_id}")
        
        if exit_price <= 0:
            raise ValueError(f"Invalid exit_price: {exit_price}")
        
        if exit_fees < 0 or funding_fees < 0:
            raise ValueError("Fees must be >= 0")
        
        position = self.positions[position_id]

        # [v2.6.5 — FIX-PM-4] Normalisation UTC avant toute comparaison temporelle.
        # Sans ce guard, exit_time naïf vs entry_time UTC-aware (ou vice-versa)
        # lève TypeError. Même pattern que FIX-SM-7 dans session_manager.
        exit_time  = self._ensure_utc(exit_time)
        entry_time = self._ensure_utc(position['entry_time'])
        
        if exit_time < entry_time:
            raise ValueError("Exit time cannot be before entry time")
        
        if position['direction'] == 'SHORT':
            pnl_brut = (position['entry_price'] - exit_price) * position['size']
        else:
            pnl_brut = (exit_price - position['entry_price']) * position['size']
        
        # [v2.6.5 — FIX-PM-5] Funding fees : additionner accumulé + delta final.
        # L'ancienne logique max(param, accumulé) était incorrecte :
        #   - Si param=0 et accumulé>0 → correct par chance (max retourne accumulé)
        #   - Si param>0 et accumulé>0 → on prenait le MAX au lieu de la SOMME
        #     → sous-comptage ou sur-comptage selon les valeurs relatives.
        # Sémantique correcte : position['funding_fees'] = cumul via add_funding_fee(),
        # funding_fees (param) = delta final éventuel à la fermeture → SOMME.
        funding_fees_final = position.get('funding_fees', 0.0) + funding_fees
        total_fees = position['entry_fees'] + exit_fees + funding_fees_final
        pnl_net = pnl_brut - total_fees
        pnl_pct = (pnl_net / position['collateral']) * 100
        
        position['exit_price'] = exit_price
        position['exit_time'] = exit_time
        position['exit_reason'] = reason
        position['pnl_brut'] = round(pnl_brut, 2)
        position['exit_fees'] = round(exit_fees, 4)
        position['funding_fees_total'] = round(funding_fees_final, 4)
        position['total_fees'] = round(total_fees, 4)
        position['pnl'] = round(pnl_net, 2)
        position['pnl_pct'] = round(pnl_pct, 2)
        position['status'] = 'closed'
        position['duration_seconds'] = (exit_time - entry_time).total_seconds()
        
        closed_position = self.positions.pop(position_id)
        self.closed_positions.append(closed_position)
        
        self.logger.info(
            f"Position closed: {position_id}, {reason}, "
            f"pnl_brut={pnl_brut:.2f}, fees={total_fees:.4f}, "
            f"pnl_net={pnl_net:.2f} ({pnl_pct:.2f}%), "
            f"trailing_updates={len(position.get('trailing_history', []))}"
        )
        
        return closed_position
    
    def add_funding_fee(self, position_id: str, funding_fee: float, timestamp: datetime):
        """
        Accumuler funding fee pour simulation backtest réaliste.
        
        Args:
            position_id: ID position
            funding_fee: Montant funding (peut être négatif)
            timestamp: Timestamp calcul
        
        Raises:
            ValueError: Si position non trouvé
        """
        if position_id not in self.positions:
            raise ValueError(f"Position not found: {position_id}")
        
        position = self.positions[position_id]
        position['funding_fees'] += funding_fee
        position['total_fees'] += funding_fee
        
        self.logger.debug(
            f"Funding fee added: {position_id}, fee={funding_fee:.4f}, "
            f"total_funding={position['funding_fees']:.4f}"
        )
    
    # --- Trailing Stop ---
    
    def update_trailing_stop(
        self,
        position_id: str,
        candle: Dict[str, Any],
        atr_value: Optional[float] = None
    ) -> Tuple[Dict[str, Any], str]:
        """
        Mettre à jour trailing stop pour une position.
        
        ⚡  PERFORMANCE :
        - Mode candle : atr_value ignoré (pas besoin ATR)
        - Mode atr/hybrid : atr_value REQUIS (pré-calculé)
        - Cache automatique par timestamp (multi-positions)
        
        Workflow:
        1. Protection 1R (si activée)
        2. Calcul volatility state (si ATR disponible)
        3. Trailing selon type (candle/atr/hybrid)
        
        Args:
            position_id: ID position
            candle: Bougie courante
            atr_value: Valeur ATR pré-calculée (REQUIS si mode atr/hybrid)
        
        Returns:
            Tuple[position mise à jour, type d'update effectif]
        
        Raises:
            ValueError: Si position non trouvé
            ValueError: Si mode atr/hybrid et atr_value manquant
        """
        if position_id not in self.positions:
            raise ValueError(f"Position not found: {position_id}")

        position = self.positions[position_id]
        # [v2.6.0] dot-notation → bracket notation dict
        trailing = self.config['strategy']['trailing_stop']
        effective_update_type = 'none'
        # [v2.6.2 — FIX PM-3] timezone-aware fallback timestamp (datetime.now(timezone.utc))
        # pour cohérence avec le reste du système (évite naive vs aware mismatch)
        candle_timestamp = candle.get('timestamp', datetime.now(timezone.utc))
        
        # [v2.6.0] trailing.type → trailing['type']
        if trailing['type'] in ['atr', 'hybrid']:
            if atr_value is None:
                raise ValueError(
                    f"atr_value REQUIRED for trailing type '{trailing['type']}'. "
                    f"Pre-calculate ATR once: atr_series = atr.calculate_atr(data), "
                    f"then pass: atr_value=atr_series.iloc[i]. "
                    f"See docs/PERFORMANCE_GUIDE.md for details."
                )
            
            # Mettre en cache (évite recalculs si multi-positions même bougie)
            self._cache_atr_value(candle_timestamp, atr_value)

        # [v2.6.0] trailing.protection_1r.auto_activate → trailing['protection_1r']['auto_activate']
        if trailing['protection_1r']['auto_activate']:
            sl_before = position['stop_loss']
            position = self._apply_1r_protection_v2(position, candle)
            if position['stop_loss'] != sl_before:
                effective_update_type = '1r_protection'

        # [v2.6.0] trailing.volatility_adjustment.enabled → trailing['volatility_adjustment']['enabled']
        if (trailing['volatility_adjustment']['enabled']
            and self.atr is not None
            and atr_value is not None):
            position = self._calculate_volatility_state(position, candle, atr_value)

        sl_before_trailing = position['stop_loss']

        # [v2.6.0] trailing.type → trailing['type']
        if trailing['type'] == 'candle':
            position = self._update_trailing_candle_v2(position, candle)
            if position['stop_loss'] != sl_before_trailing:
                effective_update_type = 'candle'

        elif trailing['type'] == 'atr':
            position = self._update_trailing_atr_v2(position, candle, atr_value)
            if position['stop_loss'] != sl_before_trailing:
                effective_update_type = 'atr'

        elif trailing['type'] == 'hybrid':
            position, hybrid_sub_type = self._update_trailing_hybrid(position, candle, atr_value)
            if position['stop_loss'] != sl_before_trailing:
                effective_update_type = f'hybrid_{hybrid_sub_type}'

        if position['stop_loss'] != sl_before_trailing:
            self._record_trailing_update(
                position, sl_before_trailing, position['stop_loss'],
                effective_update_type, candle_timestamp, candle['close']
            )

        self.positions[position_id] = position
        return position, effective_update_type

    def update_all_trailing_stops(
        self,
        candle: Dict[str, Any],
        atr_value: Optional[float] = None
    ) -> List[Dict[str, Any]]:
        """
        Mettre à jour trailing stop pour toutes positions actives.
        
        ⚡  PERFORMANCE :
        Cache ATR automatique par timestamp.
        Si 3 positions actives, atr_value n'est mis en cache qu'une fois.
        
        Args:
            candle: Bougie courante
            atr_value: Valeur ATR pré-calculée (REQUIS si mode atr/hybrid)
        
        Returns:
            List[Dict]: Updates effectués avec old_sl, new_sl, update_type, etc.
        """
        updates = []

        for position_id in list(self.positions.keys()):
            try:
                old_sl = self.positions[position_id]['stop_loss']
                _, effective_type = self.update_trailing_stop(position_id, candle, atr_value)
                new_sl = self.positions[position_id]['stop_loss']

                if new_sl != old_sl:
                    updates.append({
                        'position_id': position_id,
                        'old_sl': round(old_sl, 2),
                        'new_sl': round(new_sl, 2),
                        'update_type': effective_type,
                        'volatility': self.positions[position_id].get('volatility_state', 'N/A'),
                        'trailing_mode': self.positions[position_id].get('current_trailing_mode', 'N/A')
                    })
            except Exception as e:
                self.logger.error(f"Failed to update trailing for {position_id}: {e}")

        return updates
    
    # --- ATR Cache (Performance) ---
    
    def _cache_atr_value(self, timestamp: Any, atr_value: float):
        """Mettre en cache valeur ATR pour éviter recalculs multiples."""
        if timestamp is None:
            return
        
        if timestamp not in self._atr_cache:
            self._atr_cache[timestamp] = atr_value
            
            # Limiter taille cache (FIFO)
            if len(self._atr_cache) > self._atr_cache_max_size:
                # [v2.6.5 — FIX-PM-6] min() peut lever TypeError si les clés
                # sont de types hétérogènes (datetime + int depuis candle mal formée).
                # Fallback : vider le cache entier si la comparaison échoue.
                # Le cache se recrée dynamiquement sans perte de données OHLCV.
                try:
                    oldest_timestamp = min(self._atr_cache.keys())
                    del self._atr_cache[oldest_timestamp]
                    self.logger.debug(f"ATR cache cleanup: removed {oldest_timestamp}")
                except TypeError:
                    self._atr_cache.clear()
                    self.logger.warning(
                        "ATR cache cleared: heterogeneous timestamp types detected. "
                        "Vérifiez que candle['timestamp'] est toujours du même type."
                    )
    
    def _get_cached_atr(self, timestamp: Any, atr_value: float) -> float:
        """
        Récupérer ATR du cache si disponible, sinon utiliser valeur fournie.
        
        Optimisation : Si plusieurs positions traitent la même bougie,
        ATR est mis en cache après première position.
        """
        if timestamp and timestamp in self._atr_cache:
            self.logger.debug(f"ATR cache hit: {timestamp}")
            return self._atr_cache[timestamp]
        
        # Cache miss : utiliser valeur fournie et cacher
        self._cache_atr_value(timestamp, atr_value)
        return atr_value
    
    # --- Trailing Candle ---
    
    def _update_trailing_candle_v2(
        self, position: Dict[str, Any], current_candle: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Trailing candle: SL suit high/low de bougie.
        
        ⚡  : Plus besoin historical_data (simplifié).
        Utilise toujours current_candle (mode use_previous_candle géré par engine).
        """
        if position['direction'] == 'SHORT':
            new_sl = current_candle['high']
            should_update = new_sl < position['stop_loss']
        else:
            new_sl = current_candle['low']
            should_update = new_sl > position['stop_loss']
        
        if should_update:
            old_sl = position['stop_loss']
            position['stop_loss'] = new_sl
            
            self.logger.debug(
                f"Trailing SL (candle) updated: {position['position_id']}, "
                f"{old_sl:.2f} → {new_sl:.2f}"
            )
        
        return position
    
    # --- Trailing ATR ---
    
    def _update_trailing_atr_v2(
        self, position: Dict[str, Any], candle: Dict[str, Any], atr_value: float
    ) -> Dict[str, Any]:
        """
        Trailing ATR: SL à distance dynamique ATR × multiplier.
        
        ⚡  : Utilise atr_value pré-calculé (pas de recalcul).
        """
        # [v2.6.0] dot-notation → bracket notation dict
        trailing = self.config['strategy']['trailing_stop']
        
        if self.atr is None:
            raise RuntimeError("ATR trailing requires ATRIndicator")
        
        # ⚡ Utiliser ATR pré-calculé
        multiplier = self._get_adaptive_multiplier(position, atr_value, candle)
        
        current_price = candle['close']
        base_distance = atr_value * multiplier
        # [v2.6.0] trailing.atr_mode.min/max_distance_pct → trailing['atr_mode']['...']
        min_distance = current_price * (trailing['atr_mode']['min_distance_pct'] / 100)
        max_distance = current_price * (trailing['atr_mode']['max_distance_pct'] / 100)
        distance = clamp(base_distance, min_distance, max_distance)
        
        if position['direction'] == 'SHORT':
            new_sl = current_price + distance
            should_update = new_sl < position['stop_loss']
        else:
            new_sl = current_price - distance
            should_update = new_sl > position['stop_loss']
        
        if should_update:
            old_sl = position['stop_loss']
            position['stop_loss'] = new_sl
            
            self.logger.debug(
                f"Trailing SL (ATR) updated: {position['position_id']}, "
                f"{old_sl:.2f} → {new_sl:.2f} "
                f"(ATR={atr_value:.2f}, mult={multiplier:.2f})"
            )
        
        return position
    
    def _get_adaptive_multiplier(
        self, position: Dict[str, Any], atr_value: float, candle: Dict[str, Any]
    ) -> float:
        """Calculer multiplicateur ATR adaptatif (progressive + volatility)."""
        # [v2.6.0] dot-notation → bracket notation dict
        trailing = self.config['strategy']['trailing_stop']
        # [v2.6.0] trailing.atr_mode.base_multiplier → trailing['atr_mode']['base_multiplier']
        base_multiplier = trailing['atr_mode']['base_multiplier']
        
        # [v2.6.0] trailing.atr_mode.progressive_tightening/.stages → bracket notation
        if trailing['atr_mode']['progressive_tightening'] and trailing['atr_mode']['stages']:
            progressive_multiplier = self._get_progressive_multiplier(position)
            
            # [v2.6.0] trailing.volatility_adjustment.enabled → bracket notation
            if trailing['volatility_adjustment']['enabled']:
                volatility_factor = self._get_volatility_factor(position)
                return progressive_multiplier * volatility_factor
            
            return progressive_multiplier
        
        elif trailing['volatility_adjustment']['enabled']:
            volatility_factor = self._get_volatility_factor(position)
            return base_multiplier * volatility_factor
        
        return base_multiplier
    
    def _get_progressive_multiplier(self, position: Dict[str, Any]) -> float:
        """Calculer multiplicateur selon profit ratio (progressive tightening)."""
        # [v2.6.0] dot-notation → bracket notation dict
        trailing = self.config['strategy']['trailing_stop']
        profit_ratio = self._calculate_unrealized_profit_ratio(position)
        
        sorted_stages = sorted(
            # [v2.6.0] trailing.atr_mode.stages → trailing['atr_mode']['stages']
            trailing['atr_mode']['stages'],
            # [v2.6.0] stage.profit_threshold → stage['profit_threshold']
            key=lambda s: s['profit_threshold'],
            reverse=False
        )
        
        # [v2.6.0] trailing.atr_mode.base_multiplier → bracket notation
        selected_multiplier = trailing['atr_mode']['base_multiplier']
        selected_threshold = None
        
        for stage in sorted_stages:
            # [v2.6.0] stage.profit_threshold/stage.multiplier → stage['...']
            if profit_ratio >= stage['profit_threshold']:
                selected_multiplier = stage['multiplier']
                selected_threshold = stage['profit_threshold']
            else:
                break
        
        if selected_threshold is not None:
            self.logger.debug(
                f"Progressive multiplier: profit={profit_ratio:.2f}R >= "
                f"{selected_threshold:.2f}R → mult={selected_multiplier:.2f}"
            )
        
        return selected_multiplier
    
    def _get_volatility_factor(self, position: Dict[str, Any]) -> float:
        """Calculer facteur d'ajustement selon volatilité."""
        # [v2.6.0] dot-notation → bracket notation dict
        trailing = self.config['strategy']['trailing_stop']
        volatility_state = position.get('volatility_state', 'normal')
        
        if volatility_state == 'high':
            # [v2.6.0] trailing.volatility_adjustment.multiplier_increase → bracket notation
            return trailing['volatility_adjustment']['multiplier_increase']
        return 1.0
    
    # --- Trailing Hybrid ---
    
    def _update_trailing_hybrid(
        self, position: Dict[str, Any], candle: Dict[str, Any], atr_value: float
    ) -> Tuple[Dict[str, Any], str]:
        """
        Mode hybride: ATR début, candle après seuil profit.
        
        ⚡  : Utilise atr_value pré-calculé.
        """
        trailing = self.config['strategy']['trailing_stop']  # [v2.6.0] dot-notation → bracket notation dict
        profit_ratio = self._calculate_unrealized_profit_ratio(position)
        
        # [v2.6.0] trailing.hybrid_mode.start_with → trailing['hybrid_mode']['start_with']
        current_mode = position.get('current_trailing_mode', trailing['hybrid_mode']['start_with'])
        new_mode = current_mode
        
        if current_mode == trailing['hybrid_mode']['start_with']:
            # [v2.6.0] trailing.hybrid_mode.switch_to_candle_after → bracket notation
            if profit_ratio >= trailing['hybrid_mode']['switch_to_candle_after']:
                new_mode = 'candle' if trailing['hybrid_mode']['start_with'] == 'atr' else 'atr'
        # [v2.6.0] trailing.hybrid_mode.allow_switch_back → bracket notation
        elif trailing['hybrid_mode']['allow_switch_back']:
            if profit_ratio < trailing['hybrid_mode']['switch_to_candle_after']:
                new_mode = trailing['hybrid_mode']['start_with']
        
        if new_mode != current_mode:
            self.logger.info(
                f"Hybrid mode switch: {position['position_id']}, {current_mode} → {new_mode}"
            )
            position['current_trailing_mode'] = new_mode
            position['trailing_mode_switched'] = True
        
        if new_mode == 'atr':
            position = self._update_trailing_atr_v2(position, candle, atr_value)
        else:
            position = self._update_trailing_candle_v2(position, candle)
        
        return position, new_mode
    
    # --- Protection 1R ---
    
    def _apply_1r_protection_v2(
        self, position: Dict[str, Any], candle: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Protection 1R: déplacer SL au breakeven après gain seuil."""
        if position.get('1r_protection_active', False):
            return position
        
        # [v2.6.0] dot-notation → bracket notation dict
        trailing = self.config['strategy']['trailing_stop']
        
        # [v2.6.0] trailing.protection_1r.asymmetric_mode → trailing['protection_1r']['asymmetric_mode']
        if trailing['protection_1r']['asymmetric_mode']:
            threshold = (
                # [v2.6.0] trailing.protection_1r.long/short_breakeven_threshold → bracket notation
                trailing['protection_1r']['long_breakeven_threshold']
                if position['direction'] == 'LONG'
                else trailing['protection_1r']['short_breakeven_threshold']
            )
        else:
            # [v2.6.0] trailing.protection_1r.min_profit_for_breakeven → bracket notation
            threshold = trailing['protection_1r']['min_profit_for_breakeven']
        
        initial_risk = abs(position['entry_price'] - position['initial_stop_loss'])
        current_price = candle['close']
        
        if position['direction'] == 'SHORT':
            unrealized_pnl = position['entry_price'] - current_price
        else:
            unrealized_pnl = current_price - position['entry_price']
        
        required_profit = initial_risk * threshold
        
        if unrealized_pnl >= required_profit:
            entry_price = position['entry_price']
            
            if position['direction'] == 'SHORT':
                is_improvement = entry_price < position['stop_loss']
            else:
                is_improvement = entry_price > position['stop_loss']
            
            if is_improvement:
                position['stop_loss'] = entry_price
                position['1r_protection_active'] = True
                
                self.logger.info(
                    f"Protection {threshold:.1f}R activated: {position['position_id']}, "
                    f"SL→breakeven ({entry_price:.2f})"
                )
        
        return position
    
    # --- Volatility State ---
    
    def _calculate_volatility_state(
        self, position: Dict[str, Any], candle: Dict[str, Any], atr_value: float
    ) -> Dict[str, Any]:
        """
        Calculer état volatilité (low/normal/high).
        
        ⚡  : Utilise atr_value pré-calculé.
        """
        # [v2.6.0] dot-notation → bracket notation dict
        trailing = self.config['strategy']['trailing_stop']
        
        if self.atr is None:
            raise RuntimeError("Volatility calculation requires ATRIndicator")
        
        # ⚡ Utiliser ATR pré-calculé
        current_price = candle['close']
        atr_pct = (atr_value / current_price) * 100
        
        # [v2.6.0] trailing.volatility_adjustment.atr_threshold_high/low → bracket notation
        if atr_pct > trailing['volatility_adjustment']['atr_threshold_high']:
            volatility_state = 'high'
        elif atr_pct < trailing['volatility_adjustment']['atr_threshold_low']:
            volatility_state = 'low'
        else:
            volatility_state = 'normal'
        
        old_state = position.get('volatility_state', 'normal')
        if volatility_state != old_state:
            self.logger.debug(
                f"Volatility state changed: {position['position_id']}, "
                f"{old_state} → {volatility_state} (ATR={atr_pct:.2f}%)"
            )
        
        position['volatility_state'] = volatility_state
        return position
    
    # --- Trailing History ---
    
    def _record_trailing_update(
        self, position: Dict, old_sl: float, new_sl: float,
        update_type: str, timestamp: datetime, current_price: float
    ):
        """Enregistrer update dans trailing_history."""
        if 'trailing_history' not in position:
            position['trailing_history'] = []

        # [v2.6.3] sl_distance_pct : distance entre new_sl et current_price en %.
        # Toujours positif si le SL est du bon côté du prix :
        #   LONG  → new_sl sous current_price : (current_price - new_sl) / current_price
        #   SHORT → new_sl au-dessus          : (new_sl - current_price) / current_price
        # Négatif = SL croisé (état anormal, ne devrait pas arriver en pratique).
        if current_price > 0:
            if position.get('direction') == 'SHORT':
                sl_distance_pct = round((new_sl - current_price) / current_price * 100, 4)
            else:
                sl_distance_pct = round((current_price - new_sl) / current_price * 100, 4)
        else:
            sl_distance_pct = 0.0

        position['trailing_history'].append({
            'timestamp':       timestamp,
            'old_sl':          round(old_sl, 2),
            'new_sl':          round(new_sl, 2),
            'update_type':     update_type,
            'volatility_state': position.get('volatility_state', 'normal'),
            'profit_ratio':    self._calculate_unrealized_profit_ratio(position),
            'current_price':   round(current_price, 2),
            # [v2.6.3] entry_price : répété dans chaque entrée pour rendre
            # l'historique auto-explicite sans lookup dans les champs parents.
            'entry_price':     round(position.get('entry_price', 0.0), 2),
            # [v2.6.3] sl_distance_pct : distance SL/prix courant en %.
            # Permet de mesurer le "serrage" du trailing sans recalcul externe.
            'sl_distance_pct': sl_distance_pct,
        })
    
    def get_trailing_history(self, position_id: str) -> List[Dict[str, Any]]:
        """
        Obtenir historique complet trailing pour une position.
        
        Args:
            position_id: ID position (active ou fermée)
        
        Returns:
            List[Dict]: Historique avec timestamp, old_sl, new_sl, update_type, etc.
        """
        if position_id in self.positions:
            return self.positions[position_id].get('trailing_history', []).copy()
        
        for pos in self.closed_positions:
            if pos['position_id'] == position_id:
                return pos.get('trailing_history', []).copy()
        
        self.logger.warning(f"Position not found: {position_id}")
        return []
    
    # --- PnL Calculation ---
    
    def calculate_unrealized_pnl(self, position_id: str, current_price: float) -> float:
        """
        Calculer PnL non réalisé BRUT (avant frais).
        
        Args:
            position_id: ID position
            current_price: Prix actuel
        
        Returns:
            float: PnL BRUT en USDT
        """
        if position_id not in self.positions:
            raise ValueError(f"Position not found: {position_id}")
        
        position = self.positions[position_id]
        
        if position['direction'] == 'SHORT':
            pnl = (position['entry_price'] - current_price) * position['size']
        else:
            pnl = (current_price - position['entry_price']) * position['size']
        
        return round(pnl, 2)
    
    def calculate_unrealized_pnl_net(self, position_id: str, current_price: float) -> float:
        """
        Calculer PnL non réalisé NET (après frais accumulés).
        
        Args:
            position_id: ID position
            current_price: Prix actuel
        
        Returns:
            float: PnL NET en USDT
        """
        if position_id not in self.positions:
            raise ValueError(f"Position not found: {position_id}")
        
        position = self.positions[position_id]
        pnl_brut = self.calculate_unrealized_pnl(position_id, current_price)
        fees_accumulated = position['entry_fees'] + position.get('funding_fees', 0.0)
        
        return round(pnl_brut - fees_accumulated, 2)
    
    def calculate_unrealized_pnl_pct(
        self, position_id: str, current_price: float, use_net: bool = True
    ) -> float:
        """
        Calculer PnL non réalisé en % (basé collateral).
        
        Args:
            position_id: ID position
            current_price: Prix actuel
            use_net: Si True, utilise PnL Net (default), sinon Brut
        
        Returns:
            float: PnL en %
        """
        if position_id not in self.positions:
            raise ValueError(f"Position not found: {position_id}")
        
        position = self.positions[position_id]
        
        if use_net:
            pnl = self.calculate_unrealized_pnl_net(position_id, current_price)
        else:
            pnl = self.calculate_unrealized_pnl(position_id, current_price)
        
        return round((pnl / position['collateral']) * 100, 2)
    
    def _calculate_unrealized_profit_ratio(self, position: Dict[str, Any]) -> float:
        """Calculer ratio profit en R (mouvement SL / risque initial)."""
        initial_risk = abs(position['entry_price'] - position['initial_stop_loss'])
        sl_movement  = abs(position['stop_loss']   - position['initial_stop_loss'])
        
        if position['direction'] == 'SHORT':
            unrealized_pnl = sl_movement if position['stop_loss'] < position['initial_stop_loss'] else 0.0
        else:
            unrealized_pnl = sl_movement if position['stop_loss'] > position['initial_stop_loss'] else 0.0
        
        return round(safe_divide(unrealized_pnl, initial_risk, 0.0), 2)
    
    # --- Getters & State Management ---
    
    def get_active_positions(self) -> Dict[str, Dict[str, Any]]:
        """Obtenir toutes positions actives."""
        return self.positions.copy()
    
    def has_active_position(self) -> bool:
        """Vérifier si position active existe."""
        return len(self.positions) > 0
    
    def get_position(self, position_id: str) -> Optional[Dict[str, Any]]:
        """Obtenir position spécifique."""
        return self.positions.get(position_id)
    
    def get_closed_positions(self) -> list:
        """Obtenir historique positions fermées."""
        return self.closed_positions.copy()
    
    def get_total_pnl(self) -> float:
        """Calculer PnL total (positions fermées, PnL Net)."""
        return sum(pos.get('pnl', 0.0) for pos in self.closed_positions)
    
    def get_win_rate(self) -> float:
        """Calculer win rate (positions fermées)."""
        if not self.closed_positions:
            return 0.0
        wins = sum(1 for pos in self.closed_positions if pos.get('pnl', 0) > 0)
        return round((wins / len(self.closed_positions)) * 100, 2)
    
    def get_atr_cache_stats(self) -> Dict[str, Any]:
        """Obtenir statistiques cache ATR (monitoring performance)."""
        return {
            'cache_size': len(self._atr_cache),
            'cache_max_size': self._atr_cache_max_size,
            'cache_usage_pct': (len(self._atr_cache) / self._atr_cache_max_size) * 100
        }
    
    def clear_atr_cache(self):
        """Vider cache ATR (utile entre sessions backtest)."""
        self._atr_cache.clear()
        self.logger.debug("ATR cache cleared")
    
    def reset(self):
        """Reset state (positions + historique + cache)."""
        self.positions.clear()
        self.closed_positions.clear()
        self._atr_cache.clear()
        self.logger.info("PositionManager reset")
    
    def get_state(self) -> Dict[str, Any]:
        """Obtenir state complet pour sauvegarde."""
        # [v2.6.0] dot-notation → bracket notation dict
        trailing = self.config['strategy']['trailing_stop']
        
        return {
            'positions': dict(self.positions),
            'closed_positions': self.closed_positions,
            'trailing_config': {
                # [v2.6.0] trailing.type/protection_1r/volatility_adjustment → bracket notation
                'type': trailing['type'],
                'protection_enabled': trailing['protection_1r']['auto_activate'],
                'protection_asymmetric': trailing['protection_1r']['asymmetric_mode'],
                'volatility_enabled': trailing['volatility_adjustment']['enabled']
            },
            'atr_cache_size': len(self._atr_cache),
            'version': _VERSION  # [v2.6.5 — FIX-PM-7] était '2.6.0' hardcodé
        }
    
    def restore_state(self, state: Dict[str, Any]):
        """Restaurer state depuis sauvegarde."""
        state_version = state.get('version', 'unknown')
        # [v2.6.5 — FIX-PM-7] Vérification par préfixe majeur au lieu d'une liste
        # figée manuellement. L'ancienne liste ['2.3.0', '2.5.1', '2.6.0', '2.6.1']
        # manquait '2.6.2', '2.6.3', '2.6.4' → WARNING erroné à chaque restore.
        # Toutes les versions 2.x sont considérées compatibles (même schéma de données).
        if not str(state_version).startswith('2.'):
            self.logger.warning(
                f"Restoring state from v{state_version} (current: {_VERSION}) — "
                f"schéma potentiellement incompatible."
            )
        
        self.positions = OrderedDict(state.get('positions', {}))
        self.closed_positions = state.get('closed_positions', [])
        
        for pos in self.positions.values():
            if 'trailing_history' not in pos:
                pos['trailing_history'] = []
        
        # Cache ATR pas restauré (sera recréé dynamiquement)
        self._atr_cache.clear()
        
        self.logger.info(
            f"State restored: {len(self.positions)} active, "
            f"{len(self.closed_positions)} closed"
        )

# FIN DU MODULE
