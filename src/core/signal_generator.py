"""
Signal Generator - Stratégie Uncertainty Candle Enhanced v2.4.6

Author: FuegoDev
Version: 2.4.6
Date: 2026-03-13
─────────────────────────────────────────────────────────────────────────────
"""

import csv
import json
import threading
from collections import deque
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple, Union
import sys

import pandas as pd

# ============================================================================
# RÉSOLUTION RACINE PROJET
# ============================================================================

# [v2.4.4 — FIX-PATH-6] Résolution racine projet : pattern direct unifié.
# Remplace find_project_root() locale dupliquée dans ~10 modules (DRY).
# Calcul en 1 ligne depuis __file__ — sans fonction nommée, sans import.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# ============================================================================
# IMPORTS BULLET-1
# ============================================================================

from src.utils.logger import BulletLogger
from src.indicators.uncertainty_candle import UncertaintyCandleIndicator
from src.indicators.volume import VolumeIndicator, Direction
from src.indicators.trend import TrendIndicator

# ============================================================================
# CONSTANTES
# ============================================================================

_VALID_LOGIC_DIRECTIONS: frozenset = frozenset({'normal', 'reverse'})
_VALID_EXPORT_FORMATS: frozenset = frozenset({'json', 'csv'})
_VALID_SIGNAL_SIDES: frozenset = frozenset({'LONG', 'SHORT', 'NONE'})

# Score confiance : bornes de chaque facteur (en points sur 100)
_CONFIDENCE_UNCERTAINTY_MAX: int = 35   # Qualité bougie d'incertitude
_CONFIDENCE_VOLUME_MAX: int = 30        # Ratio volume NIVEAU-1
_CONFIDENCE_BREAKOUT_MAX: int = 20      # Amplitude cassure
_CONFIDENCE_TREND_MAX: int = 10         # Alignement tendance
_CONFIDENCE_VOLUME_L2_MAX: int = 5      # Volume NIVEAU-2

# [v2.4.6 — FIX-SG-3] Taille maximale de l'historique des signaux par défaut.
# Évite la croissance illimitée de _signals_history sur les longs backtests.
# Configurable via config['strategy']['signal_generator']['max_signals_history'].
_DEFAULT_MAX_SIGNALS_HISTORY: int = 10_000


# ============================================================================
# SIGNAL GENERATOR
# ============================================================================

class SignalGenerator:
    """
    Générateur de signaux de trading basé sur la stratégie Uncertainty Candle Enhanced.

    VERSION 2.4.2 — CONVENTION TRADING STANDARD (side au lieu de type)

    Stratégie (Flow complet):
    1.  Vérifier données historiques suffisantes
    2.  Identifier bougie d'incertitude (body < 33%, wicks >= 20%)
    3.  Récupérer bougie précédente
    4.  Détecter cassure selon mode:
        - STRICT:     close actuel dépasse high/low précédent
        - PERMISSIVE: high/low actuel dépasse high/low précédent
    5.  Appliquer logique normal/reverse → type signal + opérateur volume
    6.  Valider volume NIVEAU-1 (entry_logic) — TOUJOURS REQUIS
    7.  Appliquer filtre de tendance (optionnel)
    8.  Valider volume NIVEAU-2 (volume_confirmation) — optionnel
    9.  Calculer score de confiance (0-100)
    10. Déterminer prix d'entrée
    11. Émettre signal + persister dans l'historique

    Architecture v2.4.0:
    - VolumeIndicator() : charge automatiquement config/volume_config.json
    - TrendIndicator()  : charge automatiquement config/trend_config.json
    - Validation croisée des modes entre tous les fichiers config
    - Couplage réduit : pas de manipulation des configs internes
    - Fail-fast : erreurs détectées immédiatement au démarrage

    Thread-safety:
        self._lock (RLock) protège self.stats et self._signals_history.

    Export:
        Les signaux LONG/SHORT sont accumulés dans self._signals_history.
        Appelez export_signals() pour persister en JSON ou CSV.

    Score confiance:
        Entier 0-100 basé sur la confluence de 5 indicateurs :
        - Qualité bougie d'incertitude  : 0-35 pts
        - Ratio volume NIVEAU-1         : 0-30 pts
        - Amplitude cassure             : 0-20 pts
        - Alignement tendance           : 0-10 pts
        - Volume NIVEAU-2               :  0-5 pts
    """

    def __init__(self, config: dict) -> None:
        """
        Initialise le générateur de signaux.

        Args:
            config: Configuration complète du bot (config.json).

        Raises:
            ValueError: Si logic_direction n'est pas 'normal' ou 'reverse'.
            ModeIncoherenceError: Si les modes entre fichiers config diffèrent.
        """
        self.logger = BulletLogger()
        self.config = config

        strategy_config: dict = config['strategy']
        self.configuration_name: str = strategy_config['configuration_name']

        sg_config: dict = strategy_config.get('signal_generator', {})
        self.breakout_mode: str = sg_config.get('breakout_detection_mode', 'permissive')

        entry_config: dict = strategy_config['entry_logic']
        self.logic_direction: str = entry_config['logic_direction']
        self.short_operator: str = entry_config['for_short_case_comparison_operator']
        self.long_operator: str = entry_config['for_long_case_comparison_operator']
        # NOTE: volume_lookback sera synchronisé depuis VolumeIndicator après son init
        # (source de vérité unique = volume_config.json)

        self._validate_configuration(entry_config)

        trend_filter_config: dict = strategy_config.get('trend_filter', {})
        self.trend_filter_enabled: bool = trend_filter_config.get('enabled', True)
        self.allow_counter_trend: bool = trend_filter_config.get('allow_counter_trend', False)

        self.volume_confirmation_config: dict = strategy_config.get(
            'volume_confirmation',
            {'enabled': False}
        )

        # ── Indicateurs ( - AUTO-CONFIGURATION) ────────────────────────
        self.logger.info("Initializing indicators (v2.4.2 architecture)...")

        self.uncertainty_detector = UncertaintyCandleIndicator()
        self.body_max_pct: float = self.uncertainty_detector.body_max_pct
        self.wick_min_pct: float = self.uncertainty_detector.wick_min_pct

        self.volume_analyzer = VolumeIndicator()
        
        # Synchronisation volume_lookback depuis VolumeIndicator (source de vérité unique)
        # VolumeIndicator a chargé lookback_period depuis volume_config.json
        # On utilise cette valeur pour garantir cohérence avec les calculs internes
        self.volume_lookback: int = self.volume_analyzer.lookback_period
        
        self.trend_analyzer = TrendIndicator()

        self.logger.info("✅ All indicators initialized successfully (v2.4.2)")

        self._lock = threading.RLock()
        self.stats: Dict[str, int] = self._make_empty_stats()

        # [v2.4.6 — FIX-SG-3] Historique borné pour éviter la saturation mémoire
        # sur les longs backtests. Configurable via config.
        _max_hist = int(
            sg_config.get('max_signals_history', _DEFAULT_MAX_SIGNALS_HISTORY)
        )
        self._signals_history: deque = deque(maxlen=_max_hist)
        self._max_signals_history: int = _max_hist

        self.logger.info(
            f"SignalGenerator v2.4.2 initialized: "
            f"config={self.configuration_name}, "
            f"direction={self.logic_direction}, "
            f"breakout_mode={self.breakout_mode}"
        )

    def _validate_configuration(self, entry_config: dict) -> None:
        """Valide la configuration à l'initialisation."""
        if self.logic_direction not in _VALID_LOGIC_DIRECTIONS:
            raise ValueError(
                f"Invalid logic_direction='{self.logic_direction}'. "
                f"Must be one of: {sorted(_VALID_LOGIC_DIRECTIONS)}."
            )

        if 'require_volume_confirmation' in entry_config:
            if entry_config['require_volume_confirmation'] is False:
                self.logger.warning(
                    "DEPRECATED: 'require_volume_confirmation=False' is ignored. "
                    "Volume NIVEAU-1 is ALWAYS required."
                )

    def generate_signal(
        self,
        candles: pd.DataFrame,
        current_candle: dict
    ) -> Dict[str, Any]:
        """Génère un signal de trading."""
        with self._lock:
            self.stats['total_processed'] += 1

        required_columns = ['open', 'high', 'low', 'close', 'volume', 'timestamp']
        missing = [col for col in required_columns if col not in current_candle]
        if missing:
            raise ValueError(f"Missing required columns: {missing}")

        timestamp = current_candle['timestamp']
        if timestamp is None:
            raise ValueError("current_candle must contain a non-None 'timestamp'")

        # [v2.4.6 — FIX-SG-4] Garde-fou bougie non fermée (transition backtest → live).
        # En backtest toutes les bougies sont fermées. En live, ce guard détecte
        # un appel prématuré sur une bougie en cours de formation et le signale
        # explicitement plutôt que de produire un signal sur des données incomplètes.
        try:
            candle_ts = pd.Timestamp(timestamp, tz='UTC') if not hasattr(timestamp, 'tzinfo') else timestamp
            now_utc = pd.Timestamp.now(tz='UTC')
            if candle_ts > now_utc:
                self.logger.warning(
                    f"[FIX-SG-4] current_candle timestamp ({candle_ts}) est dans le futur "
                    f"(now={now_utc}). Bougie potentiellement non fermée — signal non fiable."
                )
        except Exception:
            pass  # Ne jamais bloquer la génération de signal sur une erreur de timestamp

        if len(candles) < self.volume_lookback:
            return self._create_none_signal(timestamp, current_candle['close'], 'insufficient_data')

        uncertainty_result = self.uncertainty_detector.detect(current_candle)
        if not uncertainty_result['is_uncertainty']:
            return self._create_none_signal(
                timestamp, current_candle['close'], 'no_uncertainty_candle',
                {'is_uncertainty': False}
            )

        with self._lock:
            self.stats['uncertainty_detected'] += 1

        cached_metrics = uncertainty_result

        # [FIX-2 v2.4.3] Guard mis à jour : < 2 au lieu de < 1.
        # iloc[-2] exige au minimum 2 candles dans le DataFrame.
        # L'ancien guard (< 1) laissait passer le cas len == 1,
        # provoquant un IndexError sur iloc[-2] — bug résiduel introduit par FIX-1.
        if len(candles) < 2:
            self.logger.warning("Not enough candles for previous_candle lookup")
            return self._create_none_signal(timestamp, current_candle['close'], 'no_previous_candle')

        # [FIX-1 v2.4.3] iloc[-2] au lieu de iloc[-1] : strategy.py passe candles_df
        # avec current_candle en dernière position (iloc[-1]). Utiliser iloc[-1] ici
        # revenait à comparer current_candle avec lui-même dans _detect_breakout(),
        # rendant toute détection de breakout structurellement impossible.
        previous_candle = candles.iloc[-2].to_dict()
        breakout_info = self._detect_breakout(current_candle, previous_candle)

        if breakout_info['breakout_type'] is None:
            return self._create_none_signal(
                timestamp, current_candle['close'], 'no_breakout',
                {'is_uncertainty': True, 'high_broken': breakout_info['high_broken'],
                 'low_broken': breakout_info['low_broken']}
            )

        if breakout_info['breakout_type'] == 'BOTH':
            with self._lock:
                self.stats['breakouts_double'] += 1
            return self._create_none_signal(
                timestamp, current_candle['close'], 'double_breakout_ambiguous',
                {'is_uncertainty': True, 'breakout_up': True, 'breakout_down': True}
            )

        with self._lock:
            if breakout_info['breakout_type'] == 'UP':
                self.stats['breakouts_up'] += 1
            else:
                self.stats['breakouts_down'] += 1

        signal_result = self._determine_signal_type(breakout_info)
        if signal_result is None:
            self.logger.error("logic_error: could not determine signal type")
            return self._create_none_signal(
                timestamp, current_candle['close'], 'logic_error',
                {'breakout_type': breakout_info['breakout_type']}
            )

        signal_type, volume_operator = signal_result

        volume_result = self._validate_volume(current_candle, candles, volume_operator)
        if volume_result is None:
            return self._create_none_signal(
                timestamp, current_candle['close'], 'invalid_average_volume',
                {'is_uncertainty': True, 'breakout': breakout_info['breakout_type']}
            )

        volume_confirmed, volume_ratio, candles_enriched = volume_result

        if not volume_confirmed:
            with self._lock:
                self.stats['volume_rejected'] += 1
            return self._create_none_signal(
                timestamp, current_candle['close'], f'volume_not_confirmed_{volume_operator}',
                {'is_uncertainty': True, 'breakout': breakout_info['breakout_type'],
                 'volume_confirmed': False, 'volume_ratio': round(volume_ratio, 2)}
            )

        with self._lock:
            self.stats['volume_confirmed'] += 1

        trend: Optional[str] = None
        if self.trend_filter_enabled:
            candles_with_trend = self.trend_analyzer.add_trend_indicators(candles)
            trend = self.trend_analyzer.get_current_trend(candles_with_trend.iloc[-1])

            if not self._validate_trend_alignment(signal_type, trend):
                with self._lock:
                    self.stats['trend_rejected'] += 1
                return self._create_none_signal(
                    timestamp, current_candle['close'], f'trend_filter_rejected_{trend}',
                    {'is_uncertainty': True, 'breakout': breakout_info['breakout_type'],
                     'volume_confirmed': volume_confirmed, 'trend': trend}
                )

        # FIX-SG-1 (v2.4.5): breakout_direction transmis comme source de vérité
        # pour la direction de marché (voir _validate_volume_confirmation).
        volume_level2_confirmed, vol_conf_details = self._validate_volume_confirmation(
            current_candle, candles_enriched, signal_type,
            breakout_direction=breakout_info['breakout_type']
        )

        if not volume_level2_confirmed:
            with self._lock:
                self.stats['volume_level2_rejected'] += 1
            return self._create_none_signal(
                timestamp, current_candle['close'],
                f"volume_level2_{vol_conf_details.get('rejection_reason', 'rejected')}",
                {'is_uncertainty': True, 'breakout': breakout_info['breakout_type'],
                 'volume_confirmed': volume_confirmed, 'trend': trend,
                 'volume_level2_details': vol_conf_details}
            )

        with self._lock:
            self.stats['volume_level2_confirmed'] += 1

        confidence: int = self._calculate_confidence(
            current_candle, previous_candle, volume_ratio,
            breakout_info['breakout_type'], trend, cached_metrics, volume_level2_confirmed
        )

        entry_price: float = self._determine_entry_price(signal_type, current_candle)

        with self._lock:
            if signal_type == 'LONG':
                self.stats['signals_long'] += 1
            else:
                self.stats['signals_short'] += 1

        self.logger.info(
            f"✅ Signal GENERATED: {signal_type}, confidence={confidence}/100, "
            f"entry={entry_price:.2f}"
        )

        signal: Dict[str, Any] = {
            'side': signal_type,
            'confidence': confidence,
            'reason': f'{self.logic_direction}_logic_applied',
            'entry_price': entry_price,
            'timestamp': timestamp,
            'indicators': {
                'is_uncertainty': True,
                'volume_confirmed': volume_confirmed,
                'volume_ratio': round(volume_ratio, 2) if not pd.isna(volume_ratio) else None,
                'volume_level2_confirmed': volume_level2_confirmed,
                'volume_level2_details': vol_conf_details,
                'breakout': breakout_info['breakout_type'],
                'breakout_mode': self.breakout_mode,
                'trend': trend,
                'body_pct': round(cached_metrics['body_pct'], 2),
                'wick_upper_pct': round(cached_metrics['upper_wick_pct'], 2),
                'wick_lower_pct': round(cached_metrics['lower_wick_pct'], 2),
                'doji_type': cached_metrics.get('doji_type', 'unknown'),
                'signal_strength': cached_metrics.get('signal_strength', 0.0)
            }
        }

        self._append_signal_history(signal)
        return signal

    def _detect_breakout(self, current_candle: dict, previous_candle: dict) -> Dict[str, Any]:
        """Détecte les cassures selon le mode configuré."""
        if self.breakout_mode == 'strict':
            high_broken = current_candle['close'] > previous_candle['high']
            low_broken = current_candle['close'] < previous_candle['low']
        else:
            high_broken = current_candle['high'] > previous_candle['high']
            low_broken = current_candle['low'] < previous_candle['low']

        if high_broken and low_broken:
            breakout_type = 'BOTH'
        elif high_broken:
            breakout_type = 'UP'
        elif low_broken:
            breakout_type = 'DOWN'
        else:
            breakout_type = None

        return {'high_broken': high_broken, 'low_broken': low_broken, 'breakout_type': breakout_type}

    def _determine_signal_type(self, breakout_info: Dict[str, Any]) -> Optional[Tuple[str, str]]:
        """Applique la logique normal/reverse."""
        breakout_type = breakout_info['breakout_type']
        if breakout_type is None or breakout_type == 'BOTH':
            return None

        if self.logic_direction == 'normal':
            return ('SHORT', self.short_operator) if breakout_type == 'UP' else ('LONG', self.long_operator)
        else:
            return ('LONG', self.long_operator) if breakout_type == 'UP' else ('SHORT', self.short_operator)

    def _validate_volume(
        self, current_candle: dict, candles: pd.DataFrame, volume_operator: str
    ) -> Optional[Tuple[bool, float, pd.DataFrame]]:
        """Valide le volume NIVEAU-1."""
        current_volume = current_candle['volume']

        try:
            candles_enriched = self.volume_analyzer.add_volume_indicators(
                candles, include_current=False, add_trend=True
            )
        except Exception as exc:
            self.logger.error(f"Volume enrichment failed: {exc}")
            return None

        last_row = candles_enriched.iloc[-1]
        avg_volume = last_row['volume_sma']

        if pd.isna(avg_volume) or avg_volume <= 0:
            return None

        volume_ratio = current_volume / avg_volume
        is_confirmed = current_volume > avg_volume if volume_operator == '>' else current_volume < avg_volume

        return (is_confirmed, volume_ratio, candles_enriched)

    def _validate_volume_confirmation(
        self,
        current_candle: dict,
        candles_enriched: pd.DataFrame,
        signal_type: str,
        breakout_direction: Optional[str] = None,
    ) -> Tuple[bool, Dict[str, Any]]:
        """
        Valide le volume NIVEAU-2.

        FIX-SG-1 (v2.4.5):
            En mode 'normal' (stratégie contrariante), signal et breakout sont
            structurellement opposés : SHORT sur breakout UP, LONG sur breakout DOWN.
            Utiliser signal_type comme proxy de la direction de marché rejetait
            structurellement 100% des signaux en mode 'directional' et 'advanced'.

            breakout_direction (ex: 'UP'/'DOWN') est désormais la source de vérité
            pour déterminer la direction réelle du marché (market_direction).
            Fallback sur signal_type si breakout_direction est None (rétrocompat).

            Zéro régression en mode 'reverse' : breakout UP → signal LONG →
            market_direction 'bullish' → identique au comportement précédent.

        Args:
            current_candle:    Dict OHLCV de la bougie courante.
            candles_enriched:  DataFrame enrichi par add_volume_indicators().
            signal_type:       'LONG' ou 'SHORT' (direction stratégique).
            breakout_direction: 'UP' ou 'DOWN' (direction réelle du breakout).
                                Passé depuis breakout_info['breakout_type'].
        """
        vol_conf = self.volume_confirmation_config
        if not vol_conf.get('enabled', False):
            return (True, {'mode': 'disabled'})

        mode = vol_conf.get('mode', 'basic')
        last_row = candles_enriched.iloc[-1]
        volume_ratio = current_candle['volume'] / last_row['volume_sma']

        # FIX-SG-1 : résolution de la direction de marché réelle.
        # breakout_direction reflète ce que le marché a fait (indépendant de la stratégie).
        # signal_type reflète ce qu'on fait en réponse (peut être opposé en mode 'normal').
        if breakout_direction is not None:
            market_direction = 'bullish' if breakout_direction == 'UP' else 'bearish'
        else:
            # Fallback rétrocompat : correct uniquement en mode 'reverse'.
            market_direction = 'bullish' if signal_type == 'LONG' else 'bearish'

        if mode == 'basic':
            min_ratio = vol_conf.get('basic', {}).get('min_ratio', 1.2)
            is_confirmed = volume_ratio >= min_ratio
            details = {'mode': 'basic', 'volume_ratio': round(volume_ratio, 2), 'min_ratio_required': min_ratio}
            if not is_confirmed:
                details['rejection_reason'] = f'ratio_below_{min_ratio}'
            return (is_confirmed, details)

        elif mode == 'directional':
            min_ratio = vol_conf.get('directional', {}).get('min_ratio', 1.2)
            require_matching = vol_conf.get('directional', {}).get('require_matching_candle', True)
            candle_direction = 'bullish' if current_candle['close'] > current_candle['open'] else 'bearish'
            volume_ok = volume_ratio >= min_ratio

            if require_matching:
                # FIX-SG-1 : comparer la bougie à market_direction (breakout),
                # pas à signal_type (qui peut être contrariant en mode 'normal').
                direction_ok = (candle_direction == market_direction)
            else:
                direction_ok = True

            is_confirmed = volume_ok and direction_ok
            details = {
                'mode': 'directional',
                'volume_ratio': round(volume_ratio, 2),
                'candle_direction': candle_direction,
                'market_direction': market_direction,
                'signal_type': signal_type,
            }
            if not is_confirmed:
                details['rejection_reason'] = (
                    f'ratio_below_{min_ratio}' if not volume_ok
                    else f'candle_{candle_direction}_not_matching_breakout_{breakout_direction}'
                )
            return (is_confirmed, details)

        elif mode == 'advanced':
            min_ratio = vol_conf.get('advanced', {}).get('min_ratio', 1.2)
            check_trend = vol_conf.get('advanced', {}).get('check_volume_trend', True)

            # FIX-SG-1 : direction_enum basée sur market_direction (breakout),
            # pas sur signal_type (is_volume_confirmation vérifie close vs open,
            # qui correspond à la direction du breakout, pas du signal contrariant).
            direction_enum = Direction.LONG if market_direction == 'bullish' else Direction.SHORT

            current_row = pd.Series({
                **current_candle,
                'volume_sma':   last_row['volume_sma'],
                'volume_ratio': volume_ratio,
                'volume_trend': last_row['volume_trend'],
            })

            try:
                is_confirmed = self.volume_analyzer.is_volume_confirmation(
                    row=current_row, direction=direction_enum,
                    min_ratio=min_ratio, check_trend=check_trend
                )
            except Exception as exc:
                self.logger.error(f"Volume confirmation (advanced) failed: {exc}")
                return (False, {'mode': 'advanced', 'rejection_reason': 'error'})

            details = {
                'mode': 'advanced',
                'volume_ratio': round(volume_ratio, 2),
                'volume_trend': current_row['volume_trend'],
                'signal_type': signal_type,
                'breakout_direction': breakout_direction,
                'market_direction': market_direction,
            }
            if not is_confirmed:
                details['rejection_reason'] = 'advanced_validation_failed'
            return (is_confirmed, details)

        return (False, {'mode': 'invalid', 'rejection_reason': f'invalid_mode_{mode}'})

    def _validate_trend_alignment(self, signal_type: str, trend: Optional[str]) -> bool:
        """Valide l'alignement signal/tendance."""
        if self.allow_counter_trend:
            return True
        if trend is None or trend in {'neutral', 'unknown', 'sideways'}:
            return True
        if signal_type == 'LONG' and trend == 'bullish':
            return True
        if signal_type == 'SHORT' and trend == 'bearish':
            return True
        return False

    def _calculate_confidence(
        self, current_candle: dict, previous_candle: dict, volume_ratio: float,
        breakout_type: str, trend: Optional[str], cached_metrics: Optional[Dict] = None,
        volume_level2_confirmed: Optional[bool] = None
    ) -> int:
        """Calcule le score de confiance (0-100)."""
        score = 0.0

        if cached_metrics:
            body_pct = cached_metrics['body_pct']
            wick_upper_pct = cached_metrics['upper_wick_pct']
            wick_lower_pct = cached_metrics['lower_wick_pct']
        else:
            metrics = self.uncertainty_detector.calculate_candle_metrics(current_candle)
            body_pct = metrics['body_pct']
            wick_upper_pct = metrics['upper_wick_pct']
            wick_lower_pct = metrics['lower_wick_pct']

        body_score = max(0.0, (self.body_max_pct - body_pct) / self.body_max_pct)
        avg_wick_pct = (wick_upper_pct + wick_lower_pct) / 2
        wick_score = min(1.0, avg_wick_pct / 40.0)
        score += (body_score * 0.5 + wick_score * 0.5) * _CONFIDENCE_UNCERTAINTY_MAX

        if not pd.isna(volume_ratio):
            volume_score = min(1.0, max(0.0, (volume_ratio - 1.0) / 1.0))
            score += volume_score * _CONFIDENCE_VOLUME_MAX

        prev_close = previous_candle['close']
        if breakout_type == 'UP':
            ref = previous_candle['high']
            compare = current_candle['close'] if self.breakout_mode == 'strict' else current_candle['high']
        else:
            ref = previous_candle['low']
            compare = current_candle['close'] if self.breakout_mode == 'strict' else current_candle['low']

        # [v2.4.6 — FIX-SG-2] Guard explicite prev_close = 0.
        # Une bougie corrompue avec close = 0 produirait une division par zéro
        # ou un breakout_amplitude infini, faussant le score de confiance.
        # Log WARNING pour faciliter le diagnostic des données corrompues.
        if not prev_close:
            self.logger.warning(
                f"[FIX-SG-2] previous_candle['close'] = {prev_close} invalide "
                f"— breakout_amplitude forcé à 0.0 (score confiance sous-estimé)."
            )
            breakout_amplitude = 0.0
        else:
            breakout_amplitude = abs(compare - ref) / prev_close
        score += min(1.0, breakout_amplitude / 0.01) * _CONFIDENCE_BREAKOUT_MAX

        if self.trend_filter_enabled and trend:
            score += _CONFIDENCE_TREND_MAX

        if volume_level2_confirmed:
            score += _CONFIDENCE_VOLUME_L2_MAX

        return min(100, round(int(score)))

    def _determine_entry_price(self, signal_type: str, current_candle: dict) -> float:
        """Détermine le prix d'entrée."""
        return current_candle['close']

    def export_signals(
        self, output_path: Union[str, Path], fmt: Literal['json', 'csv'] = 'json',
        signal_sides: Optional[List[str]] = None
    ) -> Path:
        """Exporte les signaux vers JSON ou CSV."""
        if fmt not in _VALID_EXPORT_FORMATS:
            raise ValueError(f"Invalid format: {fmt}")

        if signal_sides is None:
            signal_sides = ['LONG', 'SHORT']

        with self._lock:
            history_snapshot = [s for s in self._signals_history if s['side'] in signal_sides]

        output_path = Path(output_path).with_suffix(f'.{fmt}')
        output_path.parent.mkdir(parents=True, exist_ok=True)

        if fmt == 'json':
            self._export_json(output_path, history_snapshot)
        else:
            self._export_csv(output_path, history_snapshot)

        self.logger.info(f"Signals exported: {len(history_snapshot)} records → {output_path}")
        return output_path.resolve()

    def _export_json(self, path: Path, records: List[Dict]) -> None:
        """Exporte vers JSON."""
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump(records, fh, indent=2, ensure_ascii=False, default=str)

    def _export_csv(self, path: Path, records: List[Dict]) -> None:
        """Exporte vers CSV."""
        flat_cols = ['side', 'confidence', 'reason', 'entry_price', 'timestamp']
        with open(path, 'w', encoding='utf-8', newline='') as fh:
            writer = csv.DictWriter(fh, fieldnames=flat_cols + ['indicators_json'], extrasaction='ignore')
            writer.writeheader()
            for record in records:
                row = {col: record.get(col, '') for col in flat_cols}
                row['indicators_json'] = json.dumps(record.get('indicators', {}), ensure_ascii=False, default=str)
                writer.writerow(row)

    def _append_signal_history(self, signal: Dict) -> None:
        """Ajoute un signal à l'historique."""
        if signal['side'] not in {'LONG', 'SHORT'}:
            return

        record = deepcopy(signal)
        if isinstance(record.get('timestamp'), datetime):
            record['timestamp'] = record['timestamp'].isoformat()

        with self._lock:
            self._signals_history.append(record)

    def _create_none_signal(
        self, timestamp: datetime, price: float, reason: str, indicators: Optional[Dict] = None
    ) -> Dict:
        """Crée un signal NONE."""
        with self._lock:
            self.stats['signals_none'] += 1
        return {'side': 'NONE', 'confidence': 0, 'reason': reason,
                'timestamp': timestamp, 'indicators': indicators or {}}

    @staticmethod
    def _make_empty_stats() -> Dict[str, int]:
        """Initialise les statistiques."""
        return {
            'total_processed': 0, 'uncertainty_detected': 0,
            'breakouts_up': 0, 'breakouts_down': 0, 'breakouts_double': 0,
            'volume_confirmed': 0, 'volume_rejected': 0, 'trend_rejected': 0,
            'volume_level2_rejected': 0, 'volume_level2_confirmed': 0,
            'signals_long': 0, 'signals_short': 0, 'signals_none': 0
        }

    def get_statistics(self) -> Dict[str, Any]:
        """Retourne les statistiques enrichies."""
        with self._lock:
            stats = self.stats.copy()

        total = stats['total_processed']
        if total > 0:
            stats['uncertainty_rate_pct'] = round(stats['uncertainty_detected'] / total * 100, 2)
            stats['signal_rate_pct'] = round(
                (stats['signals_long'] + stats['signals_short']) / total * 100, 2
            )
        return stats

    def get_configuration_details(self) -> Dict[str, Any]:
        """Retourne les détails de configuration."""
        return {
            'version': '2.4.6', 'name': self.configuration_name,
            'logic_direction': self.logic_direction, 'breakout_mode': self.breakout_mode,
            'short_operator': self.short_operator, 'long_operator': self.long_operator,
            'body_max_pct': self.body_max_pct, 'wick_min_pct': self.wick_min_pct,
            'volume_lookback': self.volume_lookback,
            'trend_filter_enabled': self.trend_filter_enabled,
            'volume_level2_enabled': self.volume_confirmation_config.get('enabled', False),
            # [v2.4.6 — FIX-SG-3] Taille max historique signaux
            'max_signals_history': self._max_signals_history,
        }

    def get_signals_history(self) -> List[Dict]:
        """Retourne l'historique des signaux (copie, ordre chronologique)."""
        with self._lock:
            return list(self._signals_history)

    def reset_statistics(self) -> None:
        """Réinitialise les statistiques."""
        with self._lock:
            self.stats = self._make_empty_stats()

    def reset_signals_history(self) -> None:
        """Vide l'historique des signaux."""
        with self._lock:
            self._signals_history.clear()

    def reset_all(self) -> None:
        """Réinitialise tout."""
        with self._lock:
            self.stats = self._make_empty_stats()
            self._signals_history.clear()

# FIN DU MODULE