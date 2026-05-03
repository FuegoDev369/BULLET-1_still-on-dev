"""
BULLET-1 - Trend Indicators Module v2.5.2
=================================================================
Clean refactored version - Reduced from 1300 to ~650 lines
Functionality unchanged - Code clarity improved

Author: FuegoDev
Version: 2.5.2
Date: 2026-03-15
"""

import pandas as pd
import numpy as np
from enum import Enum
from typing import Optional, Literal, Tuple, Dict, Any
from dataclasses import dataclass
from pathlib import Path
import sys
import time
import json

# [v2.5.1 — FIX-PATH-6] Résolution racine projet : pattern direct unifié.
# Remplace find_project_root() locale dupliquée dans ~10 modules (DRY).
# Calcul en 1 ligne depuis __file__ — sans fonction nommée, sans import.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.utils.logger import BulletLogger

#: Version du module — utilisée dans les logs __init__ pour cohérence automatique.
_VERSION = "2.5.2"  # [v2.5.2 — FIX-TRD-2]

# --- Constants ---
TREND_CONFIG_PATH = 'config/trend_config.json'
MAIN_CONFIG_PATH = 'config/config.json'
DEFAULT_FAST_PERIOD = 50
DEFAULT_SLOW_PERIOD = 200
DEFAULT_MA_TYPE = 'ema'
DEFAULT_TREND_THRESHOLD = 0.002
DEFAULT_SLOPE_PERIODS = 10
DEFAULT_MIN_CROSS_SEPARATION = 0.005
QUALITY_SLOPE_THRESHOLD = 0.02
QUALITY_STRENGTH_MULTIPLIER = 10

# --- Enums ---
class Trend(Enum):
    BULLISH = 'bullish'
    BEARISH = 'bearish'
    NEUTRAL = 'neutral'

class CrossType(Enum):
    GOLDEN_CROSS = 'golden_cross'
    DEATH_CROSS = 'death_cross'
    NONE = 'none'

class TrendStrength(Enum):
    STRONG = 'strong'
    MODERATE = 'moderate'
    WEAK = 'weak'

@dataclass
class TrendMetrics:
    """Complete trend metrics."""
    trend: str
    quality_score: float  # 0-100
    strength: str
    fast_slope: float
    slow_slope: float
    ma_distance: float
    price_vs_fast: float
    price_vs_slow: float
    confluence_score: float

class ModeInconsistencyError(Exception):
    """Raised when modes differ between trend_config.json and config.json."""
    pass

# --- Main Class ---
class TrendIndicator:
    """
    Trend analysis for BULLET-1 trading system.
    
    Reads config from config/trend_config.json only.
    Validates mode consistency with config/config.json.
    Supports: backtest (batch), paper/live (real-time cache).
    """
    
    def __init__(self, config_file_path: Optional[str] = None, 
                 main_config_file_path: Optional[str] = None):
        """Init from config files. Validates mode consistency."""
        self.logger = BulletLogger()
        self.logger.info(f"Initializing TrendIndicator v{_VERSION}...")  # [FIX-TRD-2]
        
        # Load config
        cfg = self._load_config(
            config_file_path or TREND_CONFIG_PATH,
            main_config_file_path or MAIN_CONFIG_PATH
        )
        
        # Set attributes
        self.mode = cfg['mode']
        self.fast_period = cfg['fast_period']
        self.slow_period = cfg['slow_period']
        self.ma_type = cfg['ma_type']
        self.trend_threshold = cfg['trend_threshold']
        self.slope_periods = cfg['slope_periods']
        self.min_cross_separation = cfg['min_cross_separation']
        self.quality_slope_threshold = cfg.get('quality_slope_threshold', QUALITY_SLOPE_THRESHOLD)
        self.quality_strength_multiplier = cfg.get('quality_strength_multiplier', QUALITY_STRENGTH_MULTIPLIER)
        
        # Validate
        self._validate_params()
        
        # Init cache for paper/live
        if self.mode in ['paper', 'live']:
            self._init_ema_cache()
        else:
            self._ema_cache = None
            self._ema_fast_prev = self._ema_slow_prev = None
            self._is_warmup_complete = False
        
        self.logger.info(f"✅ TrendIndicator v{_VERSION} ready: mode={self.mode}, fast={self.fast_period}, slow={self.slow_period}")  # [FIX-TRD-2]
    
    def _load_config(self, trend_path: str, main_path: str) -> Dict[str, Any]:
        """Load and validate config from JSON files."""
        # [v2.5.2 — FIX-TRD-1] Recalcul local via __file__ au lieu du global
        # _PROJECT_ROOT capturé à l'import. Si le module est importé depuis un
        # contexte différent (tests, CI), _PROJECT_ROOT peut pointer au mauvais
        # endroit. Path(__file__) est stable quel que soit le répertoire de travail.
        # Identique à FIX-ATR-2 et FIX-VOL-1.
        _local_root = Path(__file__).resolve().parent.parent.parent
        trend_full = _local_root / trend_path
        main_full  = _local_root / main_path
        
        # Check files exist
        if not trend_full.exists():
            raise FileNotFoundError(f"Trend config not found: {trend_full}\nCreate: {trend_path}")
        if not main_full.exists():
            raise FileNotFoundError(f"Main config not found: {main_full}\nCreate: {main_path}")
        
        # Load JSON
        with open(trend_full) as f:
            trend_cfg = json.load(f)
        with open(main_full) as f:
            main_cfg = json.load(f)
        
        # Extract modes
        try:
            trend_mode = trend_cfg['general']['mode']
            main_mode = main_cfg['general']['mode']
        except KeyError as e:
            raise ValueError(f"Missing 'general.mode' in config: {e}")
        
        # Validate mode consistency
        enforce = trend_cfg.get('validation', {}).get('enforce_mode_consistency', True)
        if enforce and trend_mode != main_mode:
            msg = (
                f"\n{'='*70}\n"
                f"❌ MODE MISMATCH\n"
                f"{'='*70}\n"
                f"trend_config.json: '{trend_mode}'\n"
                f"config.json:       '{main_mode}'\n\n"
                f"Fix: Set same mode in both files.\n"
                f"Files: {trend_path}, {main_path}\n"
                f"{'='*70}\n"
            )
            self.logger.critical(msg)
            raise ModeInconsistencyError(msg)
        
        if not enforce and trend_mode != main_mode:
            self.logger.warning(f"⚠️ Mode mismatch ignored: trend={trend_mode}, main={main_mode}")
        
        # Extract params
        try:
            ma = trend_cfg['moving_averages']
            td = trend_cfg['trend_detection']
            qs = trend_cfg.get('quality_scoring', {})
            
            return {
                'mode': trend_mode,
                'ma_type': ma.get('type', DEFAULT_MA_TYPE),
                'fast_period': ma.get('fast_period', DEFAULT_FAST_PERIOD),
                'slow_period': ma.get('slow_period', DEFAULT_SLOW_PERIOD),
                'slope_periods': ma.get('slope_calculation_periods', DEFAULT_SLOPE_PERIODS),
                'trend_threshold': td.get('trend_strength_threshold', DEFAULT_TREND_THRESHOLD),
                'min_cross_separation': td.get('min_crossover_separation', DEFAULT_MIN_CROSS_SEPARATION),
                'quality_slope_threshold': qs.get('slope_threshold', QUALITY_SLOPE_THRESHOLD),
                'quality_strength_multiplier': qs.get('strength_multiplier', QUALITY_STRENGTH_MULTIPLIER)
            }
        except KeyError as e:
            raise ValueError(f"Invalid config structure: missing {e}")
    
    def _validate_params(self):
        """Validate configuration parameters."""
        checks = [
            (self.mode in ['backtest', 'paper', 'live'], f"Invalid mode: {self.mode}"),
            (self.fast_period >= 2, f"fast_period must be >= 2, got {self.fast_period}"),
            (self.slow_period > self.fast_period, f"slow_period must be > fast_period"),
            (self.ma_type in ['ema', 'sma'], f"ma_type must be 'ema' or 'sma', got {self.ma_type}"),
            (self.trend_threshold >= 0, f"trend_threshold must be >= 0"),
            (self.slope_periods >= 2, f"slope_periods must be >= 2"),
            (self.min_cross_separation >= 0, f"min_cross_separation must be >= 0")
        ]
        for check, msg in checks:
            if not check:
                raise ValueError(msg)
    
    def _init_ema_cache(self):
        """Init EMA cache for real-time mode."""
        self._ema_cache = {
            'fast': {'value': None, 'timestamp': None, 'alpha': 2 / (self.fast_period + 1)},
            'slow': {'value': None, 'timestamp': None, 'alpha': 2 / (self.slow_period + 1)}
        }
        self._ema_fast_prev = self._ema_slow_prev = None
        self._last_cross_type = CrossType.NONE.value
        self._is_warmup_complete = False
        # [v2.5.2 — FIX-TRD-3] self._warmup_price_buffer supprimé — était créé ici
        # mais jamais lu ni écrit par aucune méthode du module (code mort).
    
    # --- Real-time methods ---
    
    def initialize_warmup(self, historical_prices: pd.Series, force: bool = False):
        """Init cache with SMA warmup. Required for paper/live mode."""
        if self.mode == 'backtest':
            raise RuntimeError("Warmup not needed in backtest mode")
        if self._is_warmup_complete and not force:
            self.logger.warning("Warmup already done. Use force=True to reinit.")
            return
        if len(historical_prices) < self.slow_period:
            raise ValueError(f"Need {self.slow_period} prices, got {len(historical_prices)}")
        
        sma_fast = historical_prices.iloc[-self.fast_period:].mean()
        sma_slow = historical_prices.iloc[-self.slow_period:].mean()
        
        self._ema_cache['fast'].update({'value': sma_fast, 'timestamp': pd.Timestamp.now()})
        self._ema_cache['slow'].update({'value': sma_slow, 'timestamp': pd.Timestamp.now()})
        self._ema_fast_prev = sma_fast
        self._ema_slow_prev = sma_slow
        self._is_warmup_complete = True
        
        self.logger.info(f"Warmup done: EMA_fast={sma_fast:.2f}, EMA_slow={sma_slow:.2f}")
    
    def update_ema_incremental(self, new_price: float, ma_type: Literal['fast', 'slow']) -> float:
        """Update EMA incrementally O(1). Formula: EMA_new = α*price + (1-α)*EMA_old."""
        if self.mode == 'backtest':
            raise RuntimeError("Incremental update not available in backtest mode")
        if not self._is_warmup_complete:
            raise ValueError("Warmup not completed. Call initialize_warmup() first")
        
        cache = self._ema_cache[ma_type]
        new_ema = cache['alpha'] * new_price + (1 - cache['alpha']) * cache['value']
        cache.update({'value': new_ema, 'timestamp': pd.Timestamp.now()})
        return new_ema
    
    def detect_crossover_realtime(self, ema_fast_new: float, ema_slow_new: float) -> str:
        """Detect crossover in real-time (<10ms)."""
        if self._ema_fast_prev is None or self._ema_slow_prev is None:
            return CrossType.NONE.value
        
        distance = abs(ema_fast_new - ema_slow_new) / ema_slow_new
        
        if self._ema_fast_prev <= self._ema_slow_prev and ema_fast_new > ema_slow_new:
            if distance >= self.min_cross_separation:
                self.logger.info(f"GOLDEN CROSS: fast {self._ema_fast_prev:.2f}→{ema_fast_new:.2f}")
                return CrossType.GOLDEN_CROSS.value
        elif self._ema_fast_prev >= self._ema_slow_prev and ema_fast_new < ema_slow_new:
            if distance >= self.min_cross_separation:
                self.logger.info(f"DEATH CROSS: fast {self._ema_fast_prev:.2f}→{ema_fast_new:.2f}")
                return CrossType.DEATH_CROSS.value
        
        return CrossType.NONE.value
    
    def get_realtime_trend(self, new_price: float, timestamp: pd.Timestamp) -> Dict[str, Any]:
        """Analyze trend in real-time O(1). Returns full metrics dict."""
        if self.mode == 'backtest':
            raise RuntimeError("Use add_trend_indicators() in backtest mode")
        if not self._is_warmup_complete:
            raise ValueError("Warmup not completed")
        
        start = time.perf_counter()
        
        self._ema_fast_prev = self._ema_cache['fast']['value']
        self._ema_slow_prev = self._ema_cache['slow']['value']
        
        ema_fast = self.update_ema_incremental(new_price, 'fast')
        ema_slow = self.update_ema_incremental(new_price, 'slow')
        trend = detect_trend_simple(new_price, ema_fast, ema_slow, self.trend_threshold)
        cross = self.detect_crossover_realtime(ema_fast, ema_slow)
        quality = self._calc_realtime_quality(new_price, ema_fast, ema_slow, trend)
        
        elapsed_ms = (time.perf_counter() - start) * 1000
        if elapsed_ms > 10:
            self.logger.warning(f"Slow analysis: {elapsed_ms:.2f}ms")
        
        return {
            'trend': trend,
            'ema_fast': ema_fast,
            'ema_slow': ema_slow,
            'cross_type': cross,
            'quality_score': quality,
            'timestamp': timestamp,
            'processing_time_ms': elapsed_ms
        }
    
    def _calc_realtime_quality(self, price: float, ema_fast: float, ema_slow: float, trend: str) -> float:
        """Calculate quality score 0-100 for real-time (simplified, no slopes)."""
        if trend == Trend.NEUTRAL.value:
            return 0.0
        
        distance = abs(ema_fast - ema_slow) / ema_slow
        strength = min(distance * self.quality_strength_multiplier, 1.0)
        
        if trend == Trend.BULLISH.value:
            aligned = price > ema_fast and ema_fast > ema_slow
        else:
            aligned = price < ema_fast and ema_fast < ema_slow
        
        quality = 0.6 * strength + 0.4 * (1.0 if aligned else 0.5)
        return round(quality * 100, 2)
    
    # --- Batch/backtest methods ---
    
    def calculate_ema(self, df: pd.DataFrame, period: int, price_col: str = 'close') -> pd.Series:
        """Calculate EMA."""
        if price_col not in df.columns:
            raise ValueError(f"Column '{price_col}' not found")
        return df[price_col].ewm(span=period, adjust=False, min_periods=period).mean()
    
    def calculate_sma(self, df: pd.DataFrame, period: int, price_col: str = 'close') -> pd.Series:
        """Calculate SMA."""
        if price_col not in df.columns:
            raise ValueError(f"Column '{price_col}' not found")
        return df[price_col].rolling(window=period, min_periods=period).mean()
    
    def calculate_ma(self, df: pd.DataFrame, period: int, ma_type: Optional[str] = None, 
                     price_col: str = 'close') -> pd.Series:
        """Calculate moving average (EMA or SMA)."""
        ma_type = ma_type or self.ma_type
        return self.calculate_ema(df, period, price_col) if ma_type == 'ema' else self.calculate_sma(df, period, price_col)
    
    def add_trend_indicators(self, df: pd.DataFrame, price_col: str = 'close',
                            add_slope: bool = True, add_cross: bool = True, 
                            add_quality: bool = True) -> pd.DataFrame:
        """Add all trend indicators to DataFrame (vectorized batch processing)."""
        if df.empty:
            raise ValueError("DataFrame is empty")
        if price_col not in df.columns:
            raise ValueError(f"Column '{price_col}' not found")
        if len(df) < self.slow_period:
            raise ValueError(f"Need {self.slow_period} rows, got {len(df)}")
        
        df = df.copy()
        
        # Moving averages
        df['ma_fast'] = self.calculate_ma(df, self.fast_period, price_col=price_col)
        df['ma_slow'] = self.calculate_ma(df, self.slow_period, price_col=price_col)
        
        # Slopes
        if add_slope or add_quality:
            df['ma_fast_slope'] = self._calc_slope(df['ma_fast'], self.slope_periods)
            df['ma_slow_slope'] = self._calc_slope(df['ma_slow'], self.slope_periods)
        
        # Trend
        df['trend'] = self._identify_trend_vectorized(df, use_slope=(add_slope or add_quality))
        
        # Crossovers
        if add_cross:
            df['cross_type'] = self._detect_crossovers(df)
        
        # Quality and strength
        if add_quality:
            df['trend_quality'] = self._calc_quality_vectorized(df, price_col)
            df['trend_strength'] = self._calc_strength_vectorized(df)
        
        return df
    
    def _identify_trend_vectorized(self, df: pd.DataFrame, use_slope: bool = True) -> pd.Series:
        """Identify trend (vectorized)."""
        trend = pd.Series(Trend.NEUTRAL.value, index=df.index)
        fast_above = df['ma_fast'] > df['ma_slow']
        
        if use_slope and 'ma_fast_slope' in df.columns and 'ma_slow_slope' in df.columns:
            both_rising = (df['ma_fast_slope'] > 0) & (df['ma_slow_slope'] > 0)
            both_falling = (df['ma_fast_slope'] < 0) & (df['ma_slow_slope'] < 0)
            trend[fast_above & both_rising] = Trend.BULLISH.value
            trend[(~fast_above) & both_falling] = Trend.BEARISH.value
        else:
            ratio = df['ma_fast'] / df['ma_slow']
            trend[ratio > (1 + self.trend_threshold)] = Trend.BULLISH.value
            trend[ratio < (1 - self.trend_threshold)] = Trend.BEARISH.value
        
        trend[df['ma_fast'].isna() | df['ma_slow'].isna()] = Trend.NEUTRAL.value
        return trend
    
    def _detect_crossovers(self, df: pd.DataFrame) -> pd.Series:
        """Detect significant crossovers."""
        crosses = pd.Series(CrossType.NONE.value, index=df.index)
        fast_above = df['ma_fast'] > df['ma_slow']
        distance = (df['ma_fast'] - df['ma_slow']).abs() / df['ma_slow']
        fast_above_prev = fast_above.shift(1)
        
        golden = (fast_above_prev == False) & (fast_above == True) & (distance.shift(1) <= self.min_cross_separation)
        death = (fast_above_prev == True) & (fast_above == False) & (distance.shift(1) <= self.min_cross_separation)
        
        crosses[golden] = CrossType.GOLDEN_CROSS.value
        crosses[death] = CrossType.DEATH_CROSS.value
        return crosses
    
    def _calc_quality_vectorized(self, df: pd.DataFrame, price_col: str) -> pd.Series:
        """Calculate trend quality score 0-100 (vectorized)."""
        quality = pd.Series(0.0, index=df.index)
        strength = (df['ma_fast'] - df['ma_slow']).abs() / df['ma_slow']
        
        slope_bull = ((df['ma_fast_slope'] + df['ma_slow_slope']) / self.quality_slope_threshold).clip(0, 1)
        slope_bear = (-(df['ma_fast_slope'] + df['ma_slow_slope']) / self.quality_slope_threshold).clip(0, 1)
        
        price_align_bull = ((df[price_col] > df['ma_fast']) & (df['ma_fast'] > df['ma_slow'])).astype(float)
        price_align_bear = ((df[price_col] < df['ma_fast']) & (df['ma_fast'] < df['ma_slow'])).astype(float)
        
        is_bull = df['trend'] == Trend.BULLISH.value
        is_bear = df['trend'] == Trend.BEARISH.value
        
        quality[is_bull] = (
            0.4 * slope_bull[is_bull] +
            0.4 * (strength[is_bull] * self.quality_strength_multiplier).clip(0, 1) +
            0.2 * price_align_bull[is_bull]
        )
        
        quality[is_bear] = (
            0.4 * slope_bear[is_bear] +
            0.4 * (strength[is_bear] * self.quality_strength_multiplier).clip(0, 1) +
            0.2 * price_align_bear[is_bear]
        )
        
        return (quality * 100).clip(0, 100)
    
    def _calc_strength_vectorized(self, df: pd.DataFrame) -> pd.Series:
        """Calculate trend strength (vectorized)."""
        strength = pd.Series(TrendStrength.WEAK.value, index=df.index)
        distance = (df['ma_fast'] - df['ma_slow']).abs() / df['ma_slow']
        slopes_bull = (df['ma_fast_slope'] > 0.01) & (df['ma_slow_slope'] > 0.005)
        slopes_bear = (df['ma_fast_slope'] < -0.01) & (df['ma_slow_slope'] < -0.005)
        
        strong = (df['trend_quality'] > 70) & (distance > self.trend_threshold * 2) & (slopes_bull | slopes_bear)
        moderate = (df['trend_quality'] > 40) & (df['trend_quality'] <= 70)
        
        strength[strong] = TrendStrength.STRONG.value
        strength[moderate] = TrendStrength.MODERATE.value
        strength[df['trend'] == Trend.NEUTRAL.value] = TrendStrength.WEAK.value
        return strength
    
    def _calc_slope(self, series: pd.Series, periods: int) -> pd.Series:
        """Calculate slope over N periods."""
        prev = series.shift(periods)
        slope = (series - prev) / prev
        return slope.replace([np.inf, -np.inf], 0).fillna(0)
    
    # --- Getter methods ---
    
    def get_current_trend(self, row: pd.Series) -> str:
        """Get trend from row. Requires 'trend' column."""
        if 'trend' not in row.index:
            raise ValueError("Column 'trend' not found. Call add_trend_indicators() first")
        return row['trend']
    
    def get_crossover_type(self, row: pd.Series) -> str:
        """Get crossover type from row."""
        if 'cross_type' not in row.index:
            raise ValueError("Column 'cross_type' not found")
        return row['cross_type']
    
    def get_trend_with_quality(self, row: pd.Series) -> Tuple[str, float]:
        """Get trend and quality score (0-100) from row."""
        if 'trend_quality' not in row.index:
            raise ValueError("Column 'trend_quality' not found")
        return row['trend'], row['trend_quality']
    
    def get_trend_strength(self, row: pd.Series) -> str:
        """Get trend strength from row."""
        if 'trend_strength' not in row.index:
            raise ValueError("Column 'trend_strength' not found")
        return row['trend_strength']
    
    def get_ma_slope(self, row: pd.Series, ma_type: Literal['fast', 'slow']) -> float:
        """Get MA slope from row."""
        col = f'ma_{ma_type}_slope'
        if col not in row.index:
            raise ValueError(f"Column '{col}' not found")
        return row[col]
    
    def get_trend_confluence(self, row: pd.Series, price_col: str = 'close') -> float:
        """Calculate trend confluence score (0-1)."""
        trend = self.get_current_trend(row)
        if trend == Trend.NEUTRAL.value:
            return 0.0
        
        signals = 0
        checks = [
            (trend == Trend.BULLISH.value and row['ma_fast'] > row['ma_slow']) or
            (trend == Trend.BEARISH.value and row['ma_fast'] < row['ma_slow']),
            
            ('ma_fast_slope' in row.index) and (
                (trend == Trend.BULLISH.value and row['ma_fast_slope'] > 0) or
                (trend == Trend.BEARISH.value and row['ma_fast_slope'] < 0)
            ),
            
            ('ma_slow_slope' in row.index) and (
                (trend == Trend.BULLISH.value and row['ma_slow_slope'] > 0) or
                (trend == Trend.BEARISH.value and row['ma_slow_slope'] < 0)
            ),
            
            (price_col in row.index) and (
                (trend == Trend.BULLISH.value and row[price_col] > row['ma_fast']) or
                (trend == Trend.BEARISH.value and row[price_col] < row['ma_fast'])
            ),
            
            (price_col in row.index) and (
                (trend == Trend.BULLISH.value and row[price_col] > row['ma_slow']) or
                (trend == Trend.BEARISH.value and row[price_col] < row['ma_slow'])
            )
        ]
        
        return sum(checks) / len(checks)
    
    def get_complete_metrics(self, row: pd.Series, price_col: str = 'close') -> TrendMetrics:
        """Get all trend metrics from row."""
        trend = self.get_current_trend(row)
        quality = row.get('trend_quality', 0.0)
        strength = row.get('trend_strength', TrendStrength.WEAK.value)
        
        fast_slope = self.get_ma_slope(row, 'fast') if 'ma_fast_slope' in row.index else 0.0
        slow_slope = self.get_ma_slope(row, 'slow') if 'ma_slow_slope' in row.index else 0.0
        
        distance = 0.0
        if not pd.isna(row['ma_fast']) and not pd.isna(row['ma_slow']) and row['ma_slow'] != 0:
            distance = abs(row['ma_fast'] - row['ma_slow']) / row['ma_slow']
        
        price_vs_fast = price_vs_slow = 0.0
        if price_col in row.index and not pd.isna(row[price_col]):
            if not pd.isna(row['ma_fast']) and row['ma_fast'] != 0:
                price_vs_fast = (row[price_col] - row['ma_fast']) / row['ma_fast']
            if not pd.isna(row['ma_slow']) and row['ma_slow'] != 0:
                price_vs_slow = (row[price_col] - row['ma_slow']) / row['ma_slow']
        
        return TrendMetrics(
            trend=trend,
            quality_score=quality,
            strength=strength,
            fast_slope=fast_slope,
            slow_slope=slow_slope,
            ma_distance=distance,
            price_vs_fast=price_vs_fast,
            price_vs_slow=price_vs_slow,
            confluence_score=self.get_trend_confluence(row, price_col)
        )
    
    # --- Validation methods ---
    
    def validate_multi_timeframe_trend(self, df_higher: pd.DataFrame, df_current: pd.DataFrame,
                                      direction: Literal['LONG', 'SHORT'], 
                                      require_alignment: bool = True) -> bool:
        """Validate trend alignment across timeframes."""
        if 'trend' not in df_higher.columns or 'trend' not in df_current.columns:
            raise ValueError("Both DataFrames must have 'trend' column")
        
        htf = self.get_current_trend(df_higher.iloc[-1])
        ctf = self.get_current_trend(df_current.iloc[-1])
        
        if direction == 'LONG':
            if require_alignment:
                return htf == Trend.BULLISH.value and ctf == Trend.BULLISH.value
            return htf in [Trend.BULLISH.value, Trend.NEUTRAL.value] and ctf == Trend.BULLISH.value
        
        elif direction == 'SHORT':
            if require_alignment:
                return htf == Trend.BEARISH.value and ctf == Trend.BEARISH.value
            return htf in [Trend.BEARISH.value, Trend.NEUTRAL.value] and ctf == Trend.BEARISH.value
        
        raise ValueError(f"Invalid direction: {direction}")
    
    def validate_trend_filter(self, row: pd.Series, direction: Literal['LONG', 'SHORT'],
                             allow_counter_trend: bool = False, min_quality: float = 0.0) -> bool:
        """Validate if signal respects trend filter."""
        if allow_counter_trend:
            if min_quality > 0 and 'trend_quality' in row.index:
                return row['trend_quality'] >= min_quality
            return True
        
        trend = self.get_current_trend(row)
        
        if direction == 'LONG':
            valid = trend in [Trend.BULLISH.value, Trend.NEUTRAL.value]
        elif direction == 'SHORT':
            valid = trend in [Trend.BEARISH.value, Trend.NEUTRAL.value]
        else:
            raise ValueError(f"Invalid direction: {direction}")
        
        if min_quality > 0 and 'trend_quality' in row.index:
            return valid and row['trend_quality'] >= min_quality
        
        return valid
    
    def is_price_above_ma(self, row: pd.Series, ma_type: Literal['fast', 'slow'], 
                         price_col: str = 'close') -> bool:
        """Check if price is above MA."""
        ma_col = f'ma_{ma_type}'
        if price_col not in row.index or ma_col not in row.index:
            raise ValueError(f"Columns '{price_col}' and '{ma_col}' required")
        
        if pd.isna(row[price_col]) or pd.isna(row[ma_col]):
            return False
        return row[price_col] > row[ma_col]

# --- Standalone functions ---

def calculate_ema_simple(prices: pd.Series, period: int) -> pd.Series:
    """Calculate EMA (simple)."""
    return prices.ewm(span=period, adjust=False, min_periods=period).mean()

def calculate_sma_simple(prices: pd.Series, period: int) -> pd.Series:
    """Calculate SMA (simple)."""
    return prices.rolling(window=period, min_periods=period).mean()

def detect_trend_simple(price: float, ma_fast: float, ma_slow: float, threshold: float = 0.005) -> str:
    """Detect trend (simple)."""
    if any(pd.isna(x) or x is None for x in [price, ma_fast, ma_slow]) or ma_slow == 0:
        return Trend.NEUTRAL.value
    
    ratio = ma_fast / ma_slow
    if ratio > (1 + threshold):
        return Trend.BULLISH.value
    elif ratio < (1 - threshold):
        return Trend.BEARISH.value
    return Trend.NEUTRAL.value

def calculate_trend_confluence(ma_fast: float, ma_slow: float, price: float,
                               fast_slope: float, slow_slope: float, trend: str) -> float:
    """Calculate trend confluence (simple)."""
    if trend == Trend.NEUTRAL.value:
        return 0.0
    
    signals = 0
    checks = [
        (trend == Trend.BULLISH.value and ma_fast > ma_slow) or (trend == Trend.BEARISH.value and ma_fast < ma_slow),
        (trend == Trend.BULLISH.value and fast_slope > 0) or (trend == Trend.BEARISH.value and fast_slope < 0),
        (trend == Trend.BULLISH.value and slow_slope > 0) or (trend == Trend.BEARISH.value and slow_slope < 0),
        (trend == Trend.BULLISH.value and price > ma_fast) or (trend == Trend.BEARISH.value and price < ma_fast),
        (trend == Trend.BULLISH.value and price > ma_slow) or (trend == Trend.BEARISH.value and price < ma_slow)
    ]
    return sum(checks) / len(checks)

# --- Metadata ---
__all__ = [
    'TrendIndicator', 'Trend', 'CrossType', 'TrendStrength', 'TrendMetrics',
    'ModeInconsistencyError', 'calculate_ema_simple', 'calculate_sma_simple',
    'detect_trend_simple', 'calculate_trend_confluence'
]

# FIN DU MODULE