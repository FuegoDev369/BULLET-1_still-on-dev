"""
BULLET-1 - Volume Indicator Module v2.4.3 (SIMPLIFIED ARCHITECTURE)
====================================================================

Author: FuegoDev
Version: 2.4.3
Date: 2026-03-15
"""

import pandas as pd
import numpy as np
from typing import Optional, Dict, Literal, Tuple, Deque
from enum import Enum
from pathlib import Path
from collections import deque
import time
import json
import sys

# [v2.4.2 — FIX-PATH-6] Résolution racine projet : pattern direct unifié.
# Remplace find_project_root() locale dupliquée dans ~10 modules (DRY).
# Calcul en 1 ligne depuis __file__ — sans fonction nommée, sans import.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.utils.logger import BulletLogger

#: Version du module — utilisée dans le log __init__ pour cohérence automatique.
_VERSION = "2.4.3"  # [v2.4.3 — FIX-VOL-2]
# ENUMS ET CONSTANTES

class Direction(Enum):
    """Direction du trade pour éviter erreurs de typo."""
    LONG = 'long'
    SHORT = 'short'

class VolumeTrend(Enum):
    """Tendance du volume."""
    INCREASING = 'increasing'
    DECREASING = 'decreasing'
    NEUTRAL = 'neutral'

class ExecutionMode(Enum):
    """Mode d'exécution du système."""
    BACKTEST = 'backtest'
    PAPER = 'paper'
    LIVE = 'live'

# Opérateurs de comparaison
COMPARISON_OPERATORS = {
    '>': lambda x, y: x > y,
    '<': lambda x, y: x < y,
    '>=': lambda x, y: x >= y,
    '<=': lambda x, y: x <= y,
    '==': lambda x, y: x == y,
    '!=': lambda x, y: x != y
}

# Constantes par défaut
DEFAULT_LOOKBACK_PERIOD = 20
DEFAULT_SPIKE_THRESHOLD = 2.0
DEFAULT_MIN_VOLUME = 0.01
DEFAULT_CACHE_SIZE = 500  # Rolling buffer size pour modes paper/live
MAX_NAN_PERCENTAGE = 5.0  # % maximum de NaN acceptable
MAX_LATENCY_MS = 50.0  # Latence maximale pour mode temps réel
# CLASSE VOLUME INDICATOR v2.4.0 (SIMPLIFIED ARCHITECTURE)

class VolumeIndicator:
    """
    Volume indicators for BULLET-1 trading system.
    
    Usage:
        Production: VolumeIndicator() → loads config/volume_config.json (required)
        Tests: VolumeIndicator(volume_config_dict={...}, load_volume_config=False)
    
    Modes:
        BACKTEST: add_volume_indicators(df) → batch processing
        PAPER/LIVE: update_incremental(candle) → O(1) incremental cache (<50ms)
    
    Config structure: See config/volume_config.json
    """
    
    @staticmethod
    def _load_volume_config(config_path: Optional[Path] = None) -> dict:
        """Charger la configuration depuis volume_config.json."""
        if config_path is None:
            # [v2.4.3 — FIX-VOL-1] Recalcul local via __file__ au lieu du global
            # _PROJECT_ROOT capturé à l'import. Stable quel que soit le répertoire
            # de travail (tests, CI). Identique à FIX-ATR-2.
            config_path = Path(__file__).resolve().parent.parent.parent / 'config' / 'volume_config.json'

        if not config_path.exists():
            raise FileNotFoundError(
                f"volume_config.json not found at {config_path}. Create config/volume_config.json or provide path."
            )

        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                vol_config = json.load(f)
        except json.JSONDecodeError as e:
            raise json.JSONDecodeError(
                f"Invalid JSON in {config_path}: {e.msg}",
                e.doc,
                e.pos
            )

        required_sections = ['general', 'indicators']
        missing_sections = [s for s in required_sections if s not in vol_config]

        if missing_sections:
            raise ValueError(
                f"Invalid volume_config.json structure. "
                f"Missing required sections: {missing_sections}\n"
                f"Required: {required_sections}"
            )

        return vol_config

    @staticmethod
    def _load_system_config(config_path: Optional[Path] = None) -> dict:
        """Charger la configuration système depuis config.json."""
        if config_path is None:
            # [v2.4.3 — FIX-VOL-1] Même correction que _load_volume_config.
            config_path = Path(__file__).resolve().parent.parent.parent / 'config' / 'config.json'

        if not config_path.exists():
            raise FileNotFoundError(
                f"System config file not found: {config_path}\n"
                f"Expected location: config/config.json"
            )

        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                system_config = json.load(f)
        except json.JSONDecodeError as e:
            raise json.JSONDecodeError(
                f"Invalid JSON in {config_path}: {e.msg}",
                e.doc,
                e.pos
            )

        return system_config
    
    @staticmethod
    def _extract_config_value(config_dict: dict, key: str, default: any = None) -> any:
        """Extraire valeur depuis dict de config avec structure {"value": ..., "description": ...}.."""
        if key not in config_dict:
            return default
        
        item = config_dict[key]
        
        # Extract value from config dict
        if isinstance(item, dict) and 'value' in item:
            return item['value']

        return item  # [v2.4.3 — FIX-VOL-5] ligne vide parasite supprimée
    
    def __init__(
        self,
        volume_config_path: Optional[Path] = None,
        volume_config_dict: Optional[dict] = None,
        load_volume_config: bool = True
    ):
        """Initialiser VolumeIndicator depuis volume_config.json UNIQUEMENT.."""
        self.logger = BulletLogger()
        # Load configuration
        
        if load_volume_config:
            # MODE PRODUCTION: Charge volume_config.json (OBLIGATOIRE)
            try:
                volume_config = self._load_volume_config(volume_config_path)
                self.logger.debug("volume_config.json loaded successfully")
            except FileNotFoundError as e:
                raise FileNotFoundError(
                    f"volume_config.json not found. Expected: config/volume_config.json\n"
                    f"Solution: Create config/volume_config.json or provide custom path.\n"
                    f"For tests: VolumeIndicator(volume_config_dict={{...}}, load_volume_config=False)"
                ) from e
            except Exception as e:
                self.logger.error(f"Error loading volume_config.json: {e}")
                raise
        else:
            # MODE TEST: Utilise volume_config_dict
            if volume_config_dict is None:
                raise ValueError(
                    "load_volume_config=False requires volume_config_dict parameter. "
                    "Usage: VolumeIndicator(volume_config_dict={'general': {...}, 'indicators': {...}}, load_volume_config=False)"
                )
            
            volume_config = volume_config_dict
            self.logger.debug("Using volume_config_dict (test mode)")
        # Validate mode consistency
        
        if load_volume_config:
            # MODE PRODUCTION: Vérifier cohérence avec config.json
            try:
                system_config = self._load_system_config()
                self.logger.debug("config.json loaded for mode validation")
                
                # Extraire mode système (config.json)
                try:
                    system_mode_str = system_config['general']['mode'].lower()
                except KeyError:
                    self.logger.warning(
                        "Cannot extract mode from config.json (missing 'general.mode'). "
                        "Skipping mode validation."
                    )
                    system_mode_str = None
                
                # Extraire mode volume (volume_config.json)
                volume_mode_str = self._extract_config_value(
                    volume_config.get('general', {}),
                    'mode',
                    default=None
                )
                
                # VALIDATION: Les deux modes doivent correspondre
                if system_mode_str is not None and volume_mode_str is not None:
                    if system_mode_str != volume_mode_str:
                        raise ValueError(
                            f"\n"
                            f"{'='*70}\n"
                            f"❌ FATAL ERROR: MODE INCONSISTENCY DETECTED\n"
                            f"{'='*70}\n"
                            f"\n"
                            f"Les modes configurés ne correspondent pas:\n"
                            f"\n"
                            f"  📄 config/config.json         : mode = '{system_mode_str}'\n"
                            f"  📄 config/volume_config.json  : mode = '{volume_mode_str}'\n"
                            f"\n"
                            f"PROBLÈME:\n"
                            f"  VolumeIndicator doit fonctionner dans le même mode que le système.\n"
                            f"  Cette incohérence peut causer des erreurs imprévisibles.\n"
                            f"\n"
                            f"SOLUTION:\n"
                            f"  Harmoniser les modes dans les deux fichiers.\n"
                            f"  Les valeurs possibles sont: 'backtest', 'paper', 'live'\n"
                            f"\n"
                            f"  Option 1: Modifier config/config.json\n"
                            f"    → Définir general.mode = '{volume_mode_str}'\n"
                            f"\n"
                            f"  Option 2: Modifier config/volume_config.json\n"
                            f"    → Définir general.mode.value = '{system_mode_str}'\n"
                            f"\n"
                            f"{'='*70}\n"
                        )
                    else:
                        self.logger.debug(
                            f"Mode validation OK: config.json and volume_config.json "
                            f"both have mode='{system_mode_str}'"
                        )
            
            except FileNotFoundError:
                self.logger.warning(
                    "config.json not found. Skipping mode validation. "
                    "This is acceptable in isolated test environments."
                )
        else:
            # MODE TEST: Pas de validation (environnement isolé)
            self.logger.debug("Mode validation skipped (test mode)")
        # Extract parameters
        
        # Extraire sections
        general = volume_config.get('general', {})
        indicators = volume_config.get('indicators', {})
        cache_config = volume_config.get('cache', {})
        validation_config = volume_config.get('validation', {})
        logging_config = volume_config.get('logging', {})
        
        # Mode d'exécution
        mode_str = self._extract_config_value(general, 'mode', default='backtest')
        self.mode = self._extract_mode_from_string(mode_str)
        
        # Paramètres indicateurs (OBLIGATOIRES)
        self.lookback_period = self._extract_config_value(
            indicators, 'volume_lookback_period'
        )
        
        if self.lookback_period is None:
            raise ValueError(
                "Missing required parameter: indicators.volume_lookback_period in volume_config"
            )
        
        # Paramètres indicateurs (OPTIONNELS avec défauts)
        self.spike_threshold = self._extract_config_value(
            indicators, 'volume_spike_threshold', DEFAULT_SPIKE_THRESHOLD
        )
        self.min_volume = self._extract_config_value(
            indicators, 'min_volume_threshold', DEFAULT_MIN_VOLUME
        )
        self.trend_short_period = self._extract_config_value(
            indicators, 'volume_trend_short_period', 5
        )
        self.trend_long_period = self._extract_config_value(
            indicators, 'volume_trend_long_period', 20
        )
        
        # Paramètres cache
        self.cache_buffer_size = self._extract_config_value(
            cache_config, 'buffer_size', DEFAULT_CACHE_SIZE
        )
        self.max_latency_ms = self._extract_config_value(
            cache_config, 'max_latency_ms', MAX_LATENCY_MS
        )
        
        # Paramètres validation
        self.max_nan_percentage = self._extract_config_value(
            validation_config, 'max_nan_percentage', MAX_NAN_PERCENTAGE
        )
        
        # Paramètres logging
        self.log_performance = self._extract_config_value(
            logging_config, 'log_performance', True
        )
        self.performance_log_interval = self._extract_config_value(
            logging_config, 'performance_log_interval', 100
        )
        
        self.logger.debug(
            f"Parameters extracted: lookback={self.lookback_period}, "
            f"spike_threshold={self.spike_threshold}, "
            f"cache_buffer_size={self.cache_buffer_size}"
        )
        # Validate parameters
        
        self._validate_parameters()
        # Initialize cache (paper/live modes)
        
        self._cache_initialized = False
        
        if self.mode in [ExecutionMode.PAPER, ExecutionMode.LIVE]:
            self._init_incremental_cache()
        
        self.logger.info(
            f"VolumeIndicator v{_VERSION} initialized: mode={self.mode.value}, "
            f"lookback={self.lookback_period}, spike_threshold={self.spike_threshold} sigma, "
            f"trend_periods=({self.trend_short_period}/{self.trend_long_period}), "
            f"cache={'ENABLED' if self.mode != ExecutionMode.BACKTEST else 'DISABLED'}"
        )
    
    def _extract_mode_from_string(self, mode_str: str) -> ExecutionMode:
        """Convertir string mode en ExecutionMode enum.."""
        mode_mapping = {
            'backtest': ExecutionMode.BACKTEST,
            'paper': ExecutionMode.PAPER,
            'live': ExecutionMode.LIVE
        }
        
        mode_lower = mode_str.lower()
        
        if mode_lower not in mode_mapping:
            raise ValueError(
                f"Invalid mode '{mode_str}'. "
                f"Expected one of: {list(mode_mapping.keys())}"
            )
        
        return mode_mapping[mode_lower]
    
    def _validate_parameters(self) -> None:
        """Valider tous les paramètres du module."""
        if self.lookback_period < 1:
            raise ValueError(
                f"lookback_period must be >= 1, got {self.lookback_period}"
            )
        if self.spike_threshold <= 0:
            raise ValueError(
                f"spike_threshold must be > 0, got {self.spike_threshold}"
            )
        if self.min_volume < 0:
            raise ValueError(
                f"min_volume must be >= 0, got {self.min_volume}"
            )
        if self.trend_short_period < 2:
            raise ValueError(
                f"trend_short_period must be >= 2, got {self.trend_short_period}"
            )
        if self.trend_long_period <= self.trend_short_period:
            raise ValueError(
                f"trend_long_period ({self.trend_long_period}) must be > "
                f"trend_short_period ({self.trend_short_period})"
            )
    
    def _init_incremental_cache(self) -> None:
        """Initialiser le cache incrémental pour modes PAPER/LIVE.."""
        self._volume_buffer: Deque[float] = deque(maxlen=self.cache_buffer_size)
        self._volume_sum: float = 0.0
        self._sma_cache: Optional[float] = None
        self._std_cache: Optional[float] = None
        
        # Buffers pour analyse tendance
        self._trend_short_buffer: Deque[float] = deque(maxlen=self.trend_short_period)
        self._trend_long_buffer: Deque[float] = deque(maxlen=self.trend_long_period)
        
        # Monitoring
        self._last_update_time: Optional[float] = None
        self._update_count: int = 0
        self._total_latency_ms: float = 0.0
        
        self.logger.debug(
            f"Incremental cache initialized: buffer_size={self.cache_buffer_size}, "
            f"lookback={self.lookback_period}"
        )
    
    def initialize_cache(self, historical_data: pd.DataFrame) -> None:
        """
        Warm up cache with historical data for PAPER/LIVE modes.
        
        Args: historical_data (DataFrame with \'volume\' column)
        Raises: ValueError if BACKTEST mode
        """
        if self.mode == ExecutionMode.BACKTEST:
            raise ValueError(
                "Cache initialization not needed for BACKTEST mode. "
                "Use add_volume_indicators() directly."
            )
        
        self._validate_volume_data(historical_data)
        
        # Remplir buffer avec données historiques (taille configurable depuis volume_config.json)
        volumes = historical_data['volume'].tail(self.cache_buffer_size).values
        
        for vol in volumes:
            if not np.isnan(vol):
                self._volume_buffer.append(vol)
                self._volume_sum += vol
                self._trend_short_buffer.append(vol)
                self._trend_long_buffer.append(vol)
        
        # Calculer SMA et STD initiaux
        if len(self._volume_buffer) >= self.lookback_period:
            lookback_volumes = list(self._volume_buffer)[-self.lookback_period:]
            self._sma_cache = np.mean(lookback_volumes)
            self._std_cache = np.std(lookback_volumes)
        
        self._cache_initialized = True
        
        # Format SMA avec gestion None
        sma_display = f"{self._sma_cache:.6f}" if self._sma_cache is not None else "N/A"
        
        self.logger.info(
            f"Cache initialized with {len(self._volume_buffer)} candles. "
            f"SMA={sma_display}, "
            f"Ready for incremental updates."
        )
    
    def update_incremental(self, new_candle: dict) -> dict:
        """
        Incremental O(1) update for PAPER/LIVE modes (<50ms).
        
        Args:
            new_candle: {'volume': float, 'open': float, 'close': float, 'timestamp': float (optional)}
        
        Returns:
            {'volume', 'volume_sma', 'volume_ratio', 'volume_zscore', 'volume_spike', 
             'volume_trend', 'latency_ms', 'timestamp'}
        
        Raises: ValueError if BACKTEST mode or cache not initialized
        """
        # VALIDATIONS
        
        if self.mode == ExecutionMode.BACKTEST:
            raise ValueError(
                "update_incremental() is only for PAPER/LIVE modes. "
                "Use add_volume_indicators() for BACKTEST mode."
            )
        
        if not self._cache_initialized:
            raise ValueError(
                "Cache not initialized. Call initialize_cache() first with "
                "historical data before using update_incremental()."
            )
        
        # Vérifier clés requises
        required_keys = ['volume', 'open', 'close']
        missing_keys = [k for k in required_keys if k not in new_candle]
        if missing_keys:
            raise ValueError(
                f"Missing required keys in new_candle: {missing_keys}"
            )
        # START PERFORMANCE MONITORING
        
        start_time = time.perf_counter()
        # EXTRACTION DONNÉES
        # [v2.4.3 — FIX-VOL-3] pd.to_numeric(errors='coerce') : cast sécurisé
        # qui retourne NaN si la valeur est None, chaîne non numérique, etc.
        # L'ancienne implémentation faisait float() PUIS np.isnan() : si la valeur
        # était None ou une chaîne, float() levait ValueError avant le check NaN.
        new_volume_raw = pd.to_numeric(new_candle['volume'], errors='coerce')
        new_open  = float(new_candle['open'])
        new_close = float(new_candle['close'])
        timestamp = new_candle.get('timestamp', time.time())

        if pd.isna(new_volume_raw):
            self.logger.warning("NaN/non-numeric volume detected, using last valid value")
            new_volume = float(self._volume_buffer[-1]) if self._volume_buffer else 0.0
        else:
            new_volume = float(new_volume_raw)

        if new_volume < 0:
            raise ValueError(f"Volume cannot be negative: {new_volume}")
        # UPDATE CACHE INCRÉMENTAL O(1)
        
        # 1. Update rolling buffer
        if len(self._volume_buffer) == self.cache_buffer_size:
            # Buffer plein : retirer oldest
            oldest_volume = self._volume_buffer[0]
            self._volume_sum -= oldest_volume
        
        self._volume_buffer.append(new_volume)
        self._volume_sum += new_volume
        
        # 2. Update trend buffers
        self._trend_short_buffer.append(new_volume)
        self._trend_long_buffer.append(new_volume)
        # CALCUL SMA INCRÉMENTAL O(1)
        
        if len(self._volume_buffer) >= self.lookback_period:
            # SMA incrémental: moyenne des N derniers volumes
            lookback_volumes = list(self._volume_buffer)[-self.lookback_period:]
            new_sma = np.mean(lookback_volumes)
            
            # Alternative optimisée (si on veut pure O(1)):
            # new_sma = self._sma_cache + (new_volume - oldest_volume) / self.lookback_period
            # Mais numpy.mean sur slice est ultra-rapide et plus précis
            
            self._sma_cache = new_sma
            
            # STD incrémental
            self._std_cache = np.std(lookback_volumes)
        else:
            # Pas assez de données : SMA simple
            self._sma_cache = self._volume_sum / len(self._volume_buffer)
            self._std_cache = 0.0
        # CALCUL INDICATEURS
        
        # Volume Ratio
        volume_ratio = new_volume / self._sma_cache if self._sma_cache > 0 else 1.0
        
        # Volume Z-Score
        volume_zscore = (
            (new_volume - self._sma_cache) / self._std_cache 
            if self._std_cache > 0 
            else 0.0
        )
        
        # Volume Spike
        volume_spike = volume_zscore > self.spike_threshold
        
        # Volume Trend
        volume_trend = self._calculate_trend_incremental()
        # MONITORING LATENCE
        
        end_time = time.perf_counter()
        latency_ms = (end_time - start_time) * 1000
        
        self._update_count += 1
        self._total_latency_ms += latency_ms
        self._last_update_time = timestamp
        # ASSERTION LATENCE < 50ms
        
        if latency_ms > self.max_latency_ms:
            self.logger.error(
                f"❌ LATENCY VIOLATION: {latency_ms:.2f}ms > {self.max_latency_ms}ms"
            )
            # En production, on pourrait raise une exception
            # Pour l'instant, on log seulement
        
        # Log performance (intervalle configurable depuis volume_config.json)
        if self.log_performance and self._update_count % self.performance_log_interval == 0:
            avg_latency = self._total_latency_ms / self._update_count
            self.logger.debug(
                f"Performance stats: avg_latency={avg_latency:.2f}ms, "
                f"updates={self._update_count}, "
                f"buffer_size={len(self._volume_buffer)}"
            )
        # RETOUR RÉSULTATS
        
        return {
            'volume': new_volume,
            'volume_sma': self._sma_cache,
            'volume_ratio': volume_ratio,
            'volume_zscore': volume_zscore,
            'volume_spike': volume_spike,
            'volume_trend': volume_trend,
            'latency_ms': latency_ms,
            'timestamp': timestamp
        }
    
    def _calculate_trend_incremental(self) -> str:
        """Calculer tendance volume de façon incrémentale.."""
        if (len(self._trend_short_buffer) < self.trend_short_period or 
            len(self._trend_long_buffer) < self.trend_long_period):
            return VolumeTrend.NEUTRAL.value
        
        short_ma = np.mean(self._trend_short_buffer)
        long_ma = np.mean(self._trend_long_buffer)
        
        if short_ma > long_ma * 1.1:
            return VolumeTrend.INCREASING.value
        elif short_ma < long_ma * 0.9:
            return VolumeTrend.DECREASING.value
        else:
            return VolumeTrend.NEUTRAL.value
    
    def get_cache_stats(self) -> Dict[str, any]:
        """Get cache statistics and performance metrics."""
        if self.mode == ExecutionMode.BACKTEST:
            raise ValueError("Cache stats not available in BACKTEST mode")
        
        if not self._cache_initialized:
            return {
                'initialized': False,
                'buffer_size': 0,
                'update_count': 0
            }
        
        avg_latency = (
            self._total_latency_ms / self._update_count 
            if self._update_count > 0 
            else 0.0
        )
        
        return {
            'initialized': True,
            'buffer_size': len(self._volume_buffer),
            'max_buffer_size': self.cache_buffer_size,  # [v2.4.3 — FIX-VOL-4] était DEFAULT_CACHE_SIZE hardcodé
            'sma_cache': self._sma_cache,
            'std_cache': self._std_cache,
            'update_count': self._update_count,
            'avg_latency_ms': avg_latency,
            'total_latency_ms': self._total_latency_ms,
            'last_update_time': self._last_update_time,
            'latency_compliant': avg_latency < self.max_latency_ms,
        }
    # MÉTHODES MODE BACKTEST (BATCH PROCESSING)
    
    def _validate_volume_data(self, data: pd.DataFrame) -> None:
        """Valider données volume avant calcul."""
        if len(data) == 0:
            raise ValueError("DataFrame is empty")
        
        if 'volume' not in data.columns:
            raise ValueError("Column 'volume' not found in DataFrame")
        
        negative_count = (data['volume'] < 0).sum()
        if negative_count > 0:
            self.logger.error(f"Found {negative_count} negative volumes")
            raise ValueError(
                f"Volume cannot be negative ({negative_count} violations)"
            )
        
        nan_count = data['volume'].isna().sum()
        if nan_count > 0:
            nan_pct = (nan_count / len(data)) * 100
            if nan_pct > self.max_nan_percentage:
                raise ValueError(
                    f"Too many NaN in volume: {nan_count} ({nan_pct:.1f}%) > "
                    f"{self.max_nan_percentage}%"
                )
            self.logger.warning(
                f"Found {nan_count} NaN in volume ({nan_pct:.1f}%), "
                f"will forward-fill"
            )
        
        low_volume_count = (data['volume'] < self.min_volume).sum()
        if low_volume_count > 0:
            low_volume_pct = (low_volume_count / len(data)) * 100
            self.logger.warning(
                f"Found {low_volume_count} candles with volume < "
                f"{self.min_volume} ({low_volume_pct:.1f}%)"
            )
    
    def _validate_data_length(
        self, 
        data: pd.DataFrame, 
        min_length: Optional[int] = None
    ) -> None:
        """Valider longueur suffisante des données."""
        min_length = min_length or self.lookback_period
        
        if len(data) < min_length:
            raise ValueError(
                f"Insufficient data: {len(data)} rows < {min_length} required. "
                f"Need at least {min_length} candles for "
                f"lookback_period={self.lookback_period}"
            )
    
    def add_volume_indicators(
        self, 
        data: pd.DataFrame,
        include_current: bool = True,
        add_trend: bool = True
    ) -> pd.DataFrame:
        """
        Add volume indicators to DataFrame (BACKTEST mode only).
        
        Args:
            data: DataFrame with 'volume', 'open', 'close'
            include_current: Include current candle in SMA (default: True)
            add_trend: Add 'volume_trend' column (default: True)
        
        Returns:
            DataFrame with: volume_sma, volume_ratio, volume_zscore, volume_spike, volume_trend
        
        Raises: ValueError if mode is PAPER/LIVE (use update_incremental instead)
        """
        if self.mode in [ExecutionMode.PAPER, ExecutionMode.LIVE]:
            raise ValueError(
                f"add_volume_indicators() is only for BACKTEST mode. "
                f"Current mode: {self.mode.value}. "
                f"Use update_incremental() for paper/live modes."
            )
        
        self._validate_volume_data(data)
        self._validate_data_length(data)
        
        df = data.copy()
        
        if df['volume'].isna().any():
            df['volume'] = df['volume'].ffill()
            self.logger.debug("Forward-filled NaN values in volume")
        
        if include_current:
            window = self.lookback_period
            shift_amount = 0
        else:
            window = self.lookback_period
            shift_amount = 1
        
        # 1. Volume SMA
        df['volume_sma'] = df['volume'].rolling(
            window=window, 
            min_periods=1
        ).mean()
        
        if shift_amount > 0:
            df['volume_sma'] = df['volume_sma'].shift(shift_amount)
        
        # 2. Volume Ratio
        df['volume_ratio'] = df['volume'] / df['volume_sma']
        df['volume_ratio'] = df['volume_ratio'].replace(
            [np.inf, -np.inf], np.nan
        ).fillna(1.0)
        
        # 3. Volume Z-Score
        volume_std = df['volume'].rolling(
            window=window, 
            min_periods=1
        ).std()
        
        if shift_amount > 0:
            volume_std = volume_std.shift(shift_amount)
        
        df['volume_zscore'] = (df['volume'] - df['volume_sma']) / volume_std
        df['volume_zscore'] = df['volume_zscore'].replace(
            [np.inf, -np.inf], np.nan
        ).fillna(0.0)
        
        # 4. Volume Spike
        df['volume_spike'] = df['volume_zscore'] > self.spike_threshold
        
        # 5. Volume Trend (optionnel)
        if add_trend:
            df = self._add_volume_trend(df)
        
        spike_count = df['volume_spike'].sum()
        spike_pct = (spike_count / len(df)) * 100
        
        self.logger.info(
            f"Volume indicators added (BACKTEST mode): {spike_count} spikes detected "
            f"({spike_pct:.1f}% of candles), include_current={include_current}"
        )
        
        return df
    
    def _add_volume_trend(self, df: pd.DataFrame) -> pd.DataFrame:
        """Ajouter analyse tendance volume (batch)."""
        short_ma = df['volume'].rolling(
            window=self.trend_short_period,
            min_periods=1
        ).mean()
        
        long_ma = df['volume'].rolling(
            window=self.trend_long_period,
            min_periods=1
        ).mean()
        
        df['volume_trend'] = VolumeTrend.NEUTRAL.value
        df.loc[short_ma > long_ma * 1.1, 'volume_trend'] = (
            VolumeTrend.INCREASING.value
        )
        df.loc[short_ma < long_ma * 0.9, 'volume_trend'] = (
            VolumeTrend.DECREASING.value
        )
        
        trend_counts = df['volume_trend'].value_counts()
        self.logger.debug(
            f"Volume trends: {trend_counts.to_dict()} "
            f"(short={self.trend_short_period}, long={self.trend_long_period})"
        )
        
        return df
    # MÉTHODES UTILITAIRES (TOUS MODES)
    
    def get_volume_trend(self, row: pd.Series) -> str:
        """Obtenir tendance volume pour une ligne."""
        if 'volume_trend' not in row.index:
            raise ValueError(
                "Column 'volume_trend' not found. "
                "Run add_volume_indicators(add_trend=True) first."
            )
        
        return str(row['volume_trend'])
    
    def is_volume_confirmation(
        self, 
        row: pd.Series, 
        direction: Direction,
        min_ratio: float = 1.2,
        check_trend: bool = False
    ) -> bool:
        """Check if volume confirms direction (LONG/SHORT)."""
        required_cols = ['volume_ratio', 'open', 'close']
        for col in required_cols:
            if col not in row.index:
                raise ValueError(
                    f"Column '{col}' not found. "
                    f"Run add_volume_indicators() first."
                )
        
        if check_trend and 'volume_trend' not in row.index:
            raise ValueError(
                "Column 'volume_trend' not found. "
                "Run add_volume_indicators(add_trend=True) or "
                "set check_trend=False."
            )
        
        volume_ok = row['volume_ratio'] >= min_ratio
        
        if not volume_ok:
            return False
        
        if direction == Direction.LONG:
            directional_ok = row['close'] > row['open']
        elif direction == Direction.SHORT:
            directional_ok = row['close'] < row['open']
        else:
            raise ValueError(
                f"Invalid direction: {direction}. "
                f"Must be Direction.LONG or Direction.SHORT."
            )
        
        if check_trend:
            trend = row['volume_trend']
            trend_ok = trend in [
                VolumeTrend.INCREASING.value, 
                VolumeTrend.NEUTRAL.value
            ]
        else:
            trend_ok = True
        
        is_confirmed = volume_ok and directional_ok and trend_ok
        
        if not is_confirmed:
            self.logger.debug(
                f"Volume NOT confirmed for {direction.value}: "
                f"volume_ok={volume_ok} (ratio={row['volume_ratio']:.2f}), "
                f"directional_ok={directional_ok}, "
                f"trend_ok={trend_ok if check_trend else 'N/A'}"
            )
        
        return is_confirmed
    
    def compare_volume(
        self,
        df: pd.DataFrame,
        operator: str = '>',
        reference_column: str = 'volume_sma',
        output_column: str = 'volume_condition'
    ) -> pd.DataFrame:
        """Comparer volume avec référence selon opérateur."""
        if 'volume' not in df.columns:
            raise ValueError("Column 'volume' not found in DataFrame")
        
        if reference_column not in df.columns:
            raise ValueError(
                f"Reference column '{reference_column}' not found"
            )
        
        if operator not in COMPARISON_OPERATORS:
            raise ValueError(
                f"Invalid operator '{operator}'. "
                f"Must be one of: {list(COMPARISON_OPERATORS.keys())}"
            )
        
        df_result = df.copy()
        
        compare_func = COMPARISON_OPERATORS[operator]
        df_result[output_column] = compare_func(
            df_result['volume'],
            df_result[reference_column]
        )
        
        true_count = df_result[output_column].sum()
        self.logger.debug(
            f"Volume comparison: volume {operator} {reference_column} "
            f"→ {true_count} True values"
        )
        
        return df_result
    
    def get_volume_stats(self, data: pd.DataFrame) -> Dict[str, float]:
        """
        Get volume statistics from DataFrame.
        
        Returns: {\'mean\', \'median\', \'std\', \'min\', \'max\', \'q25\', \'q75\', \'q90\', \'q95\'}
        """
        self._validate_volume_data(data)
        
        volumes = data['volume']
        
        return {
            'mean': float(volumes.mean()),
            'median': float(volumes.median()),
            'std': float(volumes.std()),
            'min': float(volumes.min()),
            'max': float(volumes.max()),
            'q25': float(volumes.quantile(0.25)),
            'q50': float(volumes.quantile(0.50)),  # ✅ P50
            'q75': float(volumes.quantile(0.75)),
            'q90': float(volumes.quantile(0.90)),  # ✅ P90 NOUVEAU
            'q95': float(volumes.quantile(0.95)),  # ✅ P95 NOUVEAU
            'count': len(volumes)
        }
    
    def is_volume_spike(self, row: pd.Series) -> bool:
        """Vérifier si pic de volume."""
        if 'volume_spike' not in row.index:
            raise ValueError(
                "Column 'volume_spike' not found. "
                "Run add_volume_indicators() first."
            )
        
        return bool(row['volume_spike'])
    
    def get_volume_percentile(
        self, 
        row: pd.Series, 
        data: pd.DataFrame
    ) -> float:
        """Calculer percentile du volume actuel."""
        if 'volume' not in row.index:
            raise ValueError("Column 'volume' not found in row")
        
        self._validate_volume_data(data)
        
        current_volume = row['volume']
        percentile = (data['volume'] < current_volume).sum() / len(data) * 100
        
        return float(percentile)
    
    def is_dry_volume(
        self, 
        row: pd.Series, 
        price_change_threshold: float = 0.003
    ) -> bool:
        """Détecter "dry volume" : volume élevé sans mouvement prix."""
        required_cols = ['volume_ratio', 'open', 'close']
        for col in required_cols:
            if col not in row.index:
                raise ValueError(f"Column '{col}' not found in row")
        
        high_volume = row['volume_ratio'] > 1.5
        
        if row['open'] != 0:
            price_change_pct = abs(
                (row['close'] - row['open']) / row['open']
            )
            low_price_change = price_change_pct < price_change_threshold
        else:
            low_price_change = False
        
        return high_volume and low_price_change
# FONCTIONS UTILITAIRES GLOBALES

def calculate_volume_sma(
    data: pd.DataFrame, 
    period: int = DEFAULT_LOOKBACK_PERIOD,
    include_current: bool = True
) -> pd.Series:
    """Calculer SMA volume (fonction standalone)."""
    if 'volume' not in data.columns:
        raise ValueError("Column 'volume' not found")
    
    volume_sma = data['volume'].rolling(window=period, min_periods=1).mean()
    
    if not include_current:
        volume_sma = volume_sma.shift(1)
    
    return volume_sma

def is_volume_spike_simple(
    current_volume: float, 
    volume_sma: float, 
    threshold: float = 1.5
) -> bool:
    """Détection simple de pic volume."""
    if volume_sma == 0 or np.isnan(volume_sma):
        return False
    
    return current_volume >= (threshold * volume_sma)

def compare_volume_simple(
    current_volume: float,
    reference_volume: float,
    operator: str = '>'
) -> bool:
    """Comparer deux valeurs de volume."""
    if operator not in COMPARISON_OPERATORS:
        raise ValueError(f"Invalid operator '{operator}'")
    
    compare_func = COMPARISON_OPERATORS[operator]
    return compare_func(current_volume, reference_volume)
# METADATA MODULE

__all__ = [
    'VolumeIndicator',
    'Direction',
    'VolumeTrend',
    'ExecutionMode',
    'calculate_volume_sma',
    'is_volume_spike_simple',
    'compare_volume_simple',
    'DEFAULT_LOOKBACK_PERIOD',
    'DEFAULT_SPIKE_THRESHOLD',
    'DEFAULT_CACHE_SIZE',
    'MAX_LATENCY_MS',
]

# FIN DU MODULE