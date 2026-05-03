"""
BULLET-1 - Data Processor Module
=================================

Traitement et transformation des données historiques OHLCV.
Optimisé pour Android/Termux avec RAM limitée.

Fonctionnalités:
- Resampling multi-timeframes (5m → 15m, 1h, 4h, 1d)
- Nettoyage intelligent des données
- Features dérivées (returns, volatility, ranges, etc.)
- Normalisation (min-max, z-score, robust)
- Flag outliers (garde crashs historiques)
- Support chunking pour gros datasets
- Export CSV configurable

Version: 2.3.1
Author: FuegoDev
Dépendances: logger.py, helpers.py
"""

import sys
import json
import gc
from pathlib import Path
from typing import Optional, Dict, List, Union, Any
from datetime import datetime
import warnings
import pandas as pd
import numpy as np

current_file = Path(__file__).resolve()
project_root = current_file.parent.parent.parent
sys.path.insert(0, str(project_root))

from src.utils.helpers import (
    parse_timeframe,
    timestamp_to_datetime,
    format_datetime,
    get_project_root,
    ensure_directory
)
from src.utils.logger import BulletLogger

# Configuration globale
EXPECTED_COLUMNS = ['timestamp', 'open', 'high', 'low', 'close', 'volume']

RESAMPLE_METHODS = {
    'open': 'first',
    'high': 'max',
    'low': 'min',
    'close': 'last',
    'volume': 'sum'
}

AVAILABLE_FEATURES = [
    'returns', 'log_returns', 'volatility', 'high_low_range',
    'body_pct', 'upper_wick_pct', 'lower_wick_pct',
    'typical_price', 'weighted_price'
]

NORMALIZATION_METHODS = ['minmax', 'zscore', 'robust']
SUPPORTED_CHUNK_OPERATIONS = ['resample', 'clean', 'add_features', 'normalize']


class DataProcessor:
    """
    Processeur de données historiques OHLCV.
    Version autonome sans dépendance DataValidator.
    Config data_processor_config.json optionnelle (defaults intégrés).
    """

    _DEFAULT_PROCESSOR_CONFIG = {
        "general": {
            "mode": "backtest",
            "timeframe": "5m"
        },
        "chunking": {
            "enabled": True,
            "default_chunk_size": 10000,
            "auto_chunk_threshold": 50000
        },
        "export_transformed_data_to_csv": {
            "enabled": False,
            "auto_export": False,
            "output_dir": "data/processed",
            "compression": None,
            "add_timestamp": True,
            "validate_before_export": True
        },
        "validation": {
            "auto_validate_after_transform": True,
            "strict_mode": False
        },
        "memory_optimization": {
            "max_memory_mb": 512,
            "gc_after_operations": True,
            "use_chunking_above_rows": 50000
        }
    }

    def __init__(self, config: dict):
        """Initialiser DataProcessor avec config optionnelle."""
        self.logger = BulletLogger()
        self.config = config if config else {}
        
        self.processor_config = self._load_processor_config()
        self._validate_configuration()
        
        self._stats = {
            'total_rows_processed': 0,
            'chunks_processed': 0,
            'exports_performed': 0,
            'last_operation': None
        }
        
        self.logger.info("✅ DataProcessor initialized (Autonomous - No DataValidator)")

    def _load_processor_config(self) -> dict:
        """Charger config dédiée ou utiliser defaults si absent."""
        possible_paths = [
            Path(project_root) / 'config' / 'data_processor_config.json',
            Path(get_project_root()) / 'config' / 'data_processor_config.json',
            Path.cwd() / 'config' / 'data_processor_config.json',
            Path.cwd() / 'data_processor_config.json'
        ]
        
        for path in possible_paths:
            if path.exists():
                try:
                    with open(path, 'r', encoding='utf-8') as f:
                        loaded = json.load(f)
                    self.logger.debug(f"Config chargée: {path}")
                    merged = dict(self._DEFAULT_PROCESSOR_CONFIG)
                    merged.update(loaded)
                    return merged
                except json.JSONDecodeError as e:
                    self.logger.error(f"JSON invalide: {e} - utilisation defaults")
                    return dict(self._DEFAULT_PROCESSOR_CONFIG)
        
        self.logger.debug("Config file absent - utilisation defaults intégrés")
        return dict(self._DEFAULT_PROCESSOR_CONFIG)

    def _validate_configuration(self) -> None:
        """Valider config avec corrections auto (warnings au lieu de crashes)."""
        processor_mode = self.processor_config.get('general', {}).get('mode', '').lower()
        
        if processor_mode and processor_mode != 'backtest':
            self.logger.warning(
                f"⚠️ Mode '{processor_mode}' → forcé à 'backtest'"
            )
            self.processor_config.setdefault('general', {})['mode'] = 'backtest'
            processor_mode = 'backtest'
        
        main_config_mode = self.config.get('general', {}).get('mode', '').lower()
        if main_config_mode and main_config_mode != 'backtest':
            self.logger.warning(
                f"⚠️ config.json mode='{main_config_mode}' != 'backtest' (toléré)"
            )
        
        processor_tf = self.processor_config.get('general', {}).get('timeframe', '')
        main_config_tf = self.config.get('general', {}).get('timeframe', '')
        
        if processor_tf and main_config_tf and processor_tf != main_config_tf:
            self.logger.warning(
                f"⚠️ Timeframe mismatch: processor='{processor_tf}', config='{main_config_tf}'"
            )
        
        effective_tf = main_config_tf or processor_tf or 'N/A'
        self.logger.info(f"✅ Config OK: mode='backtest', timeframe='{effective_tf}'")

    def _should_use_chunking(self, df: pd.DataFrame) -> bool:
        """Déterminer si chunking nécessaire."""
        chunking_config = self.processor_config.get('chunking', {})
        if not chunking_config.get('enabled', True):
            return False
        auto_threshold = chunking_config.get('auto_chunk_threshold', 50000)
        return len(df) >= auto_threshold

    def _get_chunk_size(self) -> int:
        """Taille chunk depuis config."""
        return self.processor_config.get('chunking', {}).get('default_chunk_size', 10000)

    def _quick_check_df(self, df: pd.DataFrame) -> bool:
        """Vérification rapide: structure + cohérence OHLCV."""
        if df is None or not isinstance(df, pd.DataFrame) or df.empty:
            return False
        
        if isinstance(df.index, pd.DatetimeIndex) and 'timestamp' not in df.columns:
            df = df.reset_index()
            if 'index' in df.columns and 'timestamp' not in df.columns:
                df = df.rename(columns={'index': 'timestamp'})
        
        if not set(EXPECTED_COLUMNS).issubset(set(df.columns)):
            return False
        
        if not pd.api.types.is_datetime64_any_dtype(df['timestamp']):
            return False
        
        for col in ['open', 'high', 'low', 'close', 'volume']:
            if not pd.api.types.is_numeric_dtype(df[col]):
                return False
        
        h, l, o, c, v = df['high'].values, df['low'].values, df['open'].values, df['close'].values, df['volume'].values
        inconsistent = (
            (h < l) | (h < o) | (h < c) |
            (l > o) | (l > c) |
            (o < l) | (o > h) |
            (c < l) | (c > h) |
            (v < 0)
        )
        
        return not inconsistent.any()

    def _auto_clean_df(self, df: pd.DataFrame, aggressive: bool = False) -> pd.DataFrame:
        """Nettoyage auto interne."""
        self.logger.debug(f"_auto_clean_df (aggressive={aggressive})")
        df_clean = df.copy()
        
        ohlcv_cols = ['open', 'high', 'low', 'close', 'volume']
        df_clean = df_clean.dropna(subset=[c for c in ohlcv_cols if c in df_clean.columns])
        
        for col in ohlcv_cols:
            if col in df_clean.columns:
                fin_mask = np.isfinite(df_clean[col].values)
                if not fin_mask.all():
                    df_clean = df_clean[fin_mask]
        
        if all(c in df_clean.columns for c in ['open', 'high', 'low', 'close', 'volume']):
            h, l, o, c_arr, v = df_clean['high'].values, df_clean['low'].values, df_clean['open'].values, df_clean['close'].values, df_clean['volume'].values
            
            inconsistent = (
                (h < l) | (h < o) | (h < c_arr) |
                (l > o) | (l > c_arr) |
                (o < 0) | (h < 0) | (l < 0) | (c_arr < 0) | (v < 0)
            )
            
            if inconsistent.any():
                df_clean = df_clean[~inconsistent]
        
        if 'timestamp' in df_clean.columns:
            df_clean = df_clean.sort_values('timestamp')
            df_clean = df_clean.drop_duplicates(subset=['timestamp'], keep='first')
        
        if aggressive:
            if 'volume' in df_clean.columns:
                df_clean = df_clean[df_clean['volume'] > 0]
            
            if 'close' in df_clean.columns:
                mean = df_clean['close'].mean()
                std = df_clean['close'].std()
                if std > 0:
                    z_scores = np.abs((df_clean['close'] - mean) / std)
                    df_clean = df_clean[z_scores <= 5]
        
        return df_clean.reset_index(drop=True)

    def process_in_chunks(
        self,
        df: pd.DataFrame,
        chunk_size: Optional[int] = None,
        operation: str = 'resample',
        show_progress: bool = True,
        **kwargs
    ) -> pd.DataFrame:
        """Traiter DataFrame par chunks (économie RAM)."""
        if operation not in SUPPORTED_CHUNK_OPERATIONS:
            raise ValueError(f"Opération '{operation}' non supportée: {SUPPORTED_CHUNK_OPERATIONS}")
        
        if chunk_size is None:
            chunk_size = self._get_chunk_size()
        
        if len(df) < chunk_size:
            self.logger.debug(f"DataFrame petit ({len(df)} < {chunk_size}), pas de chunking")
            return self._execute_operation(df, operation, **kwargs)
        
        self.logger.info(f"🔧 Processing {len(df):,} rows in chunks of {chunk_size:,} ({operation})")
        
        n_chunks = (len(df) // chunk_size) + (1 if len(df) % chunk_size else 0)
        chunks_processed = []
        
        for i in range(0, len(df), chunk_size):
            chunk_idx = i // chunk_size + 1
            chunk = df.iloc[i:i + chunk_size].copy()
            
            if show_progress:
                self.logger.debug(f"Chunk {chunk_idx}/{n_chunks} ({len(chunk):,} rows)")
            
            try:
                processed_chunk = self._execute_operation(chunk, operation, **kwargs)
                chunks_processed.append(processed_chunk)
            except Exception as e:
                self.logger.error(f"❌ Chunk {chunk_idx}/{n_chunks} error: {e}")
                raise
            
            if self.processor_config.get('memory_optimization', {}).get('gc_after_operations', True):
                gc.collect()
        
        result = pd.concat(chunks_processed, ignore_index=True)
        
        self._stats['chunks_processed'] += n_chunks
        self._stats['total_rows_processed'] += len(result)
        self._stats['last_operation'] = operation
        
        self.logger.info(f"✅ Chunked processing: {len(result):,} rows ({n_chunks} chunks)")
        return result

    def _execute_operation(self, df: pd.DataFrame, operation: str, **kwargs) -> pd.DataFrame:
        """Dispatcher opérations."""
        _OP_DISPATCH = {
            'resample': self.resample,
            'clean': self.clean,
            'add_features': self.add_features,
            'normalize': self.normalize,
        }
        
        handler = _OP_DISPATCH.get(operation)
        if handler is None:
            raise ValueError(f"Opération '{operation}' inconnue: {list(_OP_DISPATCH.keys())}")
        
        return handler(df, **kwargs)

    def export_to_csv(
        self,
        df: pd.DataFrame,
        filename: Optional[str] = None,
        filepath: Optional[Union[str, Path]] = None,
        validate: Optional[bool] = None,
        compression: Optional[str] = None,
        force: bool = False,
        **csv_kwargs
    ) -> bool:
        """Exporter données vers CSV."""
        export_config = self.processor_config.get('export_transformed_data_to_csv', {})
        
        if not force and not export_config.get('enabled', False):
            self.logger.debug("CSV export disabled (use force=True to override)")
            return False
        
        if validate is None:
            validate = export_config.get('validate_before_export', True)
        
        if validate:
            is_valid = self._quick_check_df(df)
            if not is_valid:
                self.logger.warning("⚠️ Validation failed before export")
                return False
        
        if filepath is None:
            output_dir = Path(export_config.get('output_dir', 'data/processed'))
            if filename is None:
                timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                add_ts = export_config.get('add_timestamp', True)
                filename = f"processed_data_{timestamp}.csv" if add_ts else "processed_data.csv"
            filepath = output_dir / filename
        else:
            filepath = Path(filepath)
        
        ensure_directory(filepath.parent)
        
        if compression is None:
            compression = export_config.get('compression', None)
        
        try:
            self.logger.info(f"📤 Exporting {len(df):,} rows to {filepath}")
            
            export_params = {'index': False, 'compression': compression, **csv_kwargs}
            df.to_csv(filepath, **export_params)
            
            file_size_mb = filepath.stat().st_size / (1024 * 1024)
            self.logger.info(f"✅ Export OK: {len(df):,} rows, {file_size_mb:.2f}MB")
            
            self._stats['exports_performed'] += 1
            return True
            
        except Exception as e:
            self.logger.error(f"❌ Export failed: {e}")
            return False

    def resample(
        self,
        df: pd.DataFrame,
        target_timeframe: str,
        validate: bool = True,
        fill_gaps: bool = True,
        use_chunking: Optional[bool] = None
    ) -> pd.DataFrame:
        """Resampler vers timeframe supérieur (up-sample uniquement)."""
        if use_chunking is None:
            use_chunking = self._should_use_chunking(df)
        
        if use_chunking:
            self.logger.debug("Auto-chunking enabled")
            return self.process_in_chunks(
                df, operation='resample',
                target_timeframe=target_timeframe,
                validate=validate,
                fill_gaps=fill_gaps
            )
        
        self.logger.info(f"🔄 Resampling to {target_timeframe}")
        
        if df.empty:
            raise ValueError("Cannot resample empty DataFrame")
        if 'timestamp' not in df.columns:
            raise ValueError("DataFrame must have 'timestamp' column")
        
        if not pd.api.types.is_datetime64_any_dtype(df['timestamp']):
            self.logger.debug("Converting timestamp to datetime")
            df = df.copy()
            df['timestamp'] = pd.to_datetime(df['timestamp'], utc=True)
        
        try:
            target_seconds = parse_timeframe(target_timeframe, strict=False)
        except ValueError as e:
            raise ValueError(f"Invalid target timeframe: {e}")
        
        if len(df) > 1:
            time_diffs = df['timestamp'].diff().dropna()
            source_seconds = int(time_diffs.median().total_seconds())
            
            if target_seconds <= source_seconds:
                raise ValueError(
                    f"Cannot down-sample: {target_timeframe} ({target_seconds}s) "
                    f"<= source ({source_seconds}s)"
                )
        
        df_indexed = df.set_index('timestamp')
        resampled = df_indexed.resample(f'{target_seconds}s').agg(RESAMPLE_METHODS)
        resampled = resampled.dropna(subset=['close'])
        
        if fill_gaps and resampled['volume'].isna().any():
            self.logger.debug("Filling gaps (ffill limit=3)")
            resampled = resampled.ffill(limit=3)
        
        resampled = resampled.reset_index()
        
        if validate:
            is_valid = self._quick_check_df(resampled)
            if not is_valid:
                self.logger.warning("⚠️ Resampled data validation failed")
            else:
                self.logger.debug("✅ Validation OK")
        
        self.logger.info(
            f"✅ Resampled: {len(df)} → {len(resampled)} candles "
            f"({len(df) / len(resampled):.1f}x)"
        )
        return resampled

    def clean(
        self,
        df: pd.DataFrame,
        aggressive: bool = False,
        auto_validate: bool = True
    ) -> pd.DataFrame:
        """Nettoyer données."""
        self.logger.info(f"🧹 Cleaning (aggressive={aggressive})")
        initial_len = len(df)
        
        if auto_validate:
            is_valid = self._quick_check_df(df)
            if is_valid:
                self.logger.info("Data already clean")
                return df.copy()
        
        df_clean = self._auto_clean_df(df, aggressive=aggressive)
        
        removed = initial_len - len(df_clean)
        pct = (removed / initial_len * 100) if initial_len > 0 else 0
        
        self.logger.info(f"✅ Cleaned: {removed:,} removed ({pct:.1f}%), {len(df_clean):,} remain")
        return df_clean

    def add_features(
        self,
        df: pd.DataFrame,
        features: List[str],
        window: int = 20,
        timeframe: Optional[str] = None,
        inplace: bool = False
    ) -> pd.DataFrame:
        """Ajouter features dérivées."""
        self.logger.info(f"➕ Adding {len(features)} features (window={window})")
        
        invalid = set(features) - set(AVAILABLE_FEATURES)
        if invalid:
            raise ValueError(f"Invalid features: {invalid}. Available: {AVAILABLE_FEATURES}")
        
        if timeframe is None:
            timeframe = (
                self.processor_config.get('general', {}).get('timeframe') or
                self.config.get('general', {}).get('timeframe', '5m')
            )
        
        df_result = df if inplace else df.copy()
        
        for feature in features:
            self.logger.debug(f"Computing: {feature}")
            
            if feature == 'returns':
                df_result['returns'] = df_result['close'].pct_change()
            
            elif feature == 'log_returns':
                df_result['log_returns'] = np.log(df_result['close'] / df_result['close'].shift(1))
            
            elif feature == 'volatility':
                returns = df_result.get('returns', df_result['close'].pct_change())
                
                periods_per_year = {
                    '1m': 525600, '5m': 105120, '15m': 35040, '30m': 17520,
                    '1h': 8760, '2h': 4380, '4h': 2190, '6h': 1460,
                    '8h': 1095, '12h': 730, '1d': 365, '1w': 52
                }
                
                annual_factor = np.sqrt(periods_per_year.get(timeframe, 8760))
                vol_raw = returns.rolling(window=window).std()
                df_result['volatility'] = vol_raw * annual_factor
                df_result['volatility_raw'] = vol_raw
            
            elif feature == 'high_low_range':
                df_result['high_low_range'] = (
                    (df_result['high'] - df_result['low']) / df_result['close'] * 100
                )
            
            elif feature == 'body_pct':
                range_hl = df_result['high'] - df_result['low']
                body = np.abs(df_result['close'] - df_result['open'])
                df_result['body_pct'] = np.where(range_hl > 0, (body / range_hl) * 100, 0.0)
            
            elif feature == 'upper_wick_pct':
                range_hl = df_result['high'] - df_result['low']
                max_oc = np.maximum(df_result['open'], df_result['close'])
                upper_wick = df_result['high'] - max_oc
                df_result['upper_wick_pct'] = np.where(range_hl > 0, (upper_wick / range_hl) * 100, 0.0)
            
            elif feature == 'lower_wick_pct':
                range_hl = df_result['high'] - df_result['low']
                min_oc = np.minimum(df_result['open'], df_result['close'])
                lower_wick = min_oc - df_result['low']
                df_result['lower_wick_pct'] = np.where(range_hl > 0, (lower_wick / range_hl) * 100, 0.0)
            
            elif feature == 'typical_price':
                df_result['typical_price'] = (
                    df_result['high'] + df_result['low'] + df_result['close']
                ) / 3
            
            elif feature == 'weighted_price':
                df_result['weighted_price'] = (
                    df_result['high'] + df_result['low'] + df_result['close'] * 2
                ) / 4
        
        self.logger.info(f"✅ Added {len(features)} features")
        return df_result

    def normalize(
        self,
        df: pd.DataFrame,
        columns: List[str],
        method: str = 'minmax',
        inplace: bool = False
    ) -> pd.DataFrame:
        """Normaliser colonnes."""
        self.logger.info(f"📊 Normalizing {len(columns)} columns ({method})")
        
        if method not in NORMALIZATION_METHODS:
            raise ValueError(f"Invalid method: {method}. Available: {NORMALIZATION_METHODS}")
        
        missing = set(columns) - set(df.columns)
        if missing:
            raise ValueError(f"Missing columns: {missing}")
        
        df_result = df if inplace else df.copy()
        
        for col in columns:
            self.logger.debug(f"Normalizing: {col}")
            
            if method == 'minmax':
                col_min, col_max = df_result[col].min(), df_result[col].max()
                if col_max > col_min:
                    df_result[col] = (df_result[col] - col_min) / (col_max - col_min)
                else:
                    self.logger.warning(f"Column {col} has no range")
            
            elif method == 'zscore':
                col_mean, col_std = df_result[col].mean(), df_result[col].std()
                if col_std > 0:
                    df_result[col] = (df_result[col] - col_mean) / col_std
                else:
                    self.logger.warning(f"Column {col} has zero std")
            
            elif method == 'robust':
                col_median = df_result[col].median()
                q75, q25 = df_result[col].quantile(0.75), df_result[col].quantile(0.25)
                iqr = q75 - q25
                if iqr > 0:
                    df_result[col] = (df_result[col] - col_median) / iqr
                else:
                    self.logger.warning(f"Column {col} has zero IQR")
        
        self.logger.info(f"✅ Normalized {len(columns)} columns")
        return df_result

    def interpolate_gaps(
        self,
        df: pd.DataFrame,
        method: str = 'linear',
        limit: Optional[int] = None
    ) -> pd.DataFrame:
        """Interpoler gaps (⚠️ linear peut créer look-ahead bias)."""
        self.logger.info(f"🔧 Interpolating gaps ({method})")
        df_result = df.copy()
        
        if method == 'linear':
            numeric_cols = df_result.select_dtypes(include=[np.number]).columns
            df_result[numeric_cols] = df_result[numeric_cols].interpolate(
                method='linear', limit=limit, limit_direction='forward'
            )
            self.logger.warning("Linear interpolation forward-only (gaps at end may remain)")
        
        elif method == 'ffill':
            df_result = df_result.ffill(limit=limit)
        
        elif method == 'bfill':
            df_result = df_result.bfill(limit=limit)
            self.logger.warning("⚠️ BFILL uses future data → look-ahead bias!")
        
        else:
            raise ValueError(f"Invalid method: {method}")
        
        remaining_nan = df_result.isna().sum().sum()
        self.logger.info(f"✅ Interpolation complete. Remaining NaN: {remaining_nan}")
        return df_result

    def flag_outliers(
        self,
        df: pd.DataFrame,
        column: str = 'close',
        method: str = 'zscore',
        threshold: float = 3.0
    ) -> pd.DataFrame:
        """Marquer outliers sans les supprimer."""
        outliers = self.detect_outliers(df, column, method, threshold)
        df_result = df.copy()
        
        df_result['is_outlier'] = outliers
        
        data = df[column]
        mean, std = data.mean(), data.std()
        
        if std > 0:
            df_result['outlier_z_score'] = np.abs((data - mean) / std)
        else:
            df_result['outlier_z_score'] = 0.0
        
        z_scores = df_result['outlier_z_score']
        conditions = [z_scores >= 10, z_scores >= 5, z_scores >= 3]
        choices = ['extreme', 'high', 'moderate']
        df_result['outlier_severity'] = np.select(conditions, choices, default='')
        df_result.loc[df_result['outlier_severity'] == '', 'outlier_severity'] = None
        
        n_outliers = outliers.sum()
        pct = (n_outliers / len(df)) * 100
        
        self.logger.info(f"🏴 Flagged {n_outliers} outliers in '{column}' ({pct:.2f}%)")
        
        if n_outliers > 0:
            severity_counts = df_result[df_result['outlier_severity'].notna()]['outlier_severity'].value_counts()
            self.logger.info(f"   Severity: {severity_counts.to_dict()}")
        
        return df_result

    def detect_outliers(
        self,
        df: pd.DataFrame,
        column: str = 'close',
        method: str = 'zscore',
        threshold: float = 3.0
    ) -> pd.Series:
        """Détecter outliers."""
        if column not in df.columns:
            raise ValueError(f"Column not found: {column}")
        
        data = df[column]
        
        if method == 'zscore':
            mean, std = data.mean(), data.std()
            if std > 0:
                z_scores = np.abs((data - mean) / std)
                return z_scores > threshold
            else:
                return pd.Series([False] * len(df), index=df.index)
        
        elif method == 'iqr':
            q1, q3 = data.quantile(0.25), data.quantile(0.75)
            iqr = q3 - q1
            lower_bound = q1 - threshold * iqr
            upper_bound = q3 + threshold * iqr
            return (data < lower_bound) | (data > upper_bound)
        
        else:
            raise ValueError(f"Invalid method: {method}")

    def get_statistics(self, df: pd.DataFrame) -> Dict[str, Any]:
        """Stats descriptives."""
        stats = {
            'shape': df.shape,
            'columns': list(df.columns),
            'dtypes': df.dtypes.to_dict(),
            'memory_mb': df.memory_usage(deep=True).sum() / (1024 * 1024),
            'null_counts': df.isnull().sum().to_dict(),
            'duplicates': df.duplicated().sum()
        }
        
        if 'close' in df.columns:
            stats['close_stats'] = {
                'mean': float(df['close'].mean()),
                'std': float(df['close'].std()),
                'min': float(df['close'].min()),
                'max': float(df['close'].max())
            }
        
        if 'volume' in df.columns:
            stats['volume_stats'] = {
                'mean': float(df['volume'].mean()),
                'median': float(df['volume'].median()),
                'zero_count': int((df['volume'] == 0).sum())
            }
        
        return stats

    def normalize_expanding(
        self,
        df: pd.DataFrame,
        columns: List[str],
        method: str = 'minmax',
        min_periods: int = 20
    ) -> pd.DataFrame:
        """Normalisation expanding window (évite look-ahead)."""
        self.logger.info(f"📊 Normalizing {len(columns)} cols (expanding, {method})")
        
        missing = set(columns) - set(df.columns)
        if missing:
            raise ValueError(f"Missing columns: {missing}")
        
        df_result = df.copy()
        
        for col in columns:
            self.logger.debug(f"Expanding normalization: {col}")
            
            if method == 'minmax':
                expanding_min = df_result[col].expanding(min_periods=min_periods).min()
                expanding_max = df_result[col].expanding(min_periods=min_periods).max()
                range_val = (expanding_max - expanding_min).replace(0, 1)
                df_result[col] = (df_result[col] - expanding_min) / range_val
            
            elif method == 'zscore':
                expanding_mean = df_result[col].expanding(min_periods=min_periods).mean()
                expanding_std = df_result[col].expanding(min_periods=min_periods).std()
                expanding_std = expanding_std.replace(0, 1)
                df_result[col] = (df_result[col] - expanding_mean) / expanding_std
            
            elif method == 'robust':
                expanding_median = df_result[col].expanding(min_periods=min_periods).median()
                expanding_q25 = df_result[col].expanding(min_periods=min_periods).quantile(0.25)
                expanding_q75 = df_result[col].expanding(min_periods=min_periods).quantile(0.75)
                iqr = (expanding_q75 - expanding_q25).replace(0, 1)
                df_result[col] = (df_result[col] - expanding_median) / iqr
            
            else:
                raise ValueError(f"Invalid method: {method}")
        
        self.logger.info(f"✅ Normalized {len(columns)} cols (expanding)")
        return df_result

    def get_processing_stats(self) -> Dict[str, Any]:
        """Stats processing."""
        return self._stats.copy()

    def reset_stats(self) -> None:
        """Reset stats."""
        self._stats = {
            'total_rows_processed': 0,
            'chunks_processed': 0,
            'exports_performed': 0,
            'last_operation': None
        }
        self.logger.debug("Stats reset")


# Helpers globaux
def quick_resample(df: pd.DataFrame, target_timeframe: str, validate: bool = False) -> pd.DataFrame:
    """Resample rapide sans config."""
    processor = DataProcessor(config={})
    return processor.resample(df, target_timeframe, validate=validate)


def quick_clean(df: pd.DataFrame, aggressive: bool = False) -> pd.DataFrame:
    """Nettoyage rapide."""
    processor = DataProcessor(config={})
    return processor.clean(df, aggressive=aggressive)

# FIN DU MODULE