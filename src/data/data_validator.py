"""
BULLET-1 - Data Validator Module
==============================================

Validation de l'intégrité des données historiques OHLCV.
Module de gestion des données (Bloc 2, Module 9).

Fonctionnalités :
- Validation cohérence OHLCV stricte (vectorisée numpy)
- Détection valeurs manquantes (NaN, None, infinies)
- Vérification ordre chronologique des timestamps
- Détection gaps temporels selon timeframe
- Détection anomalies volume (zéro, pics aberrants)
- Détection anomalies prix (outliers, variations extrêmes)
- Génération rapports de validation détaillés (JSON + texte)
- Suggestions de correction automatique
- Normalisation automatique index datetime → colonne timestamp

Version: 2.3.1
Date: 2026-02-23
Author: FuegoDev
Dépendances: helpers.py , logger.py, config/data_validator_config.json
"""

import sys
import json
from pathlib import Path
from typing import Optional, Dict, List, Tuple, Any, Union
from datetime import datetime, timedelta
import warnings
import pandas as pd
import numpy as np

try:
    from typing import TypedDict
    
    class ValidationReport(TypedDict, total=False):
        is_valid: bool
        total_issues: int
        issues_by_type: Dict[str, int]
        statistics: Dict[str, Any]
        recommendations: List[str]
        checks: Dict[str, Dict[str, Any]]
    
    TYPED_DICT_AVAILABLE = True
except ImportError:
    ValidationReport = Dict[str, Any]
    TYPED_DICT_AVAILABLE = False

current_file = Path(__file__).resolve()
project_root = current_file.parent.parent.parent
sys.path.insert(0, str(project_root))

from src.utils.helpers import (
    timestamp_to_datetime,
    format_datetime,
    is_valid_price,
    is_valid_volume,
    format_percentage,
    parse_timeframe
)
from src.utils.logger import BulletLogger


# ============================================================================
# CONFIGURATION
# ============================================================================

EXPECTED_COLUMNS = ['timestamp', 'open', 'high', 'low', 'close', 'volume']

DEFAULT_THRESHOLDS = {
    'max_price_change_pct': 20.0,
    'outlier_sigma': 5.0,
    'min_volume': 0.0,
    'max_volume_spike_ratio': 10.0,
    'gap_tolerance_multiplier': 1.5,
    'min_data_points': 10,
}


def _log_fallback_details(
    _log,
    reason: str,
    config_path: Path,
    partial_config: Optional[Dict[str, Any]] = None
) -> None:
    """
    Émettre des logs WARNING détaillés clé par clé lors d'un fallback.

    Pour chaque clé de DEFAULT_THRESHOLDS, indique :
    - si la valeur vient du JSON (config partielle disponible)
    - ou si la valeur par défaut est activée (fallback complet ou clé manquante)
    """
    _log.warning(
        f"⚠️  [DataValidator] FALLBACK CONFIG ACTIVÉ\n"
        f"    Raison     : {reason}\n"
        f"    Fichier    : {config_path}\n"
        f"    ⚠️  ATTENTION : Les seuils de validation ci-dessous ne proviennent PAS\n"
        f"                   du fichier de configuration officiel.\n"
        f"                   Vérifiez la disponibilité et la validité du fichier JSON."
    )
    for key, default_value in DEFAULT_THRESHOLDS.items():
        if partial_config and key in partial_config:
            _log.warning(
                f"    ✅ {key:<30} = {partial_config[key]}  (lu depuis JSON partiel)"
            )
        else:
            _log.warning(
                f"    🔴 {key:<30} = {default_value}  ← VALEUR PAR DÉFAUT (hardcodée)"
            )


def load_validator_config(config_path: Optional[Path] = None) -> Dict[str, float]:
    """
    Charger configuration depuis data_validator_config.json.

    Le fichier JSON est la SEULE source de vérité.
    Aucun override par code n'est autorisé.

    En cas d'indisponibilité (fichier absent, JSON invalide, erreur I/O),
    fallback sur DEFAULT_THRESHOLDS avec logs WARNING détaillés clé par clé.

    Utilise logging standard (pas BulletLogger) pour éviter initialisation
    singleton à l'import du module.

    Args:
        config_path: Chemin alternatif vers le fichier JSON de configuration.
                     Si None, utilise <project_root>/config/data_validator_config.json

    Returns:
        Dict[str, float]: Thresholds de validation actifs (JSON ou fallback défaut).
    """
    import logging
    _log = logging.getLogger('BULLET-1')

    if config_path is None:
        config_path = project_root / 'config' / 'data_validator_config.json'

    config_path = Path(config_path)

    # ── Cas 1 : fichier introuvable ──────────────────────────────────────────
    if not config_path.exists():
        _log_fallback_details(
            _log,
            reason=f"Fichier introuvable : {config_path}",
            config_path=config_path
        )
        return DEFAULT_THRESHOLDS.copy()

    # ── Cas 2 : lecture + parsing JSON ──────────────────────────────────────
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            raw_config = json.load(f)

    except json.JSONDecodeError as e:
        _log.error(
            f"❌ [DataValidator] JSON invalide dans {config_path}\n"
            f"   Erreur     : {e}\n"
            f"   Ligne/Col  : ligne {e.lineno}, colonne {e.colno}"
        )
        _log_fallback_details(
            _log,
            reason=f"JSON malformé — JSONDecodeError: {e.msg}",
            config_path=config_path
        )
        return DEFAULT_THRESHOLDS.copy()

    except PermissionError as e:
        _log.error(
            f"❌ [DataValidator] Permission refusée pour lire {config_path}\n"
            f"   Erreur : {e}"
        )
        _log_fallback_details(
            _log,
            reason=f"Accès refusé au fichier — PermissionError: {e}",
            config_path=config_path
        )
        return DEFAULT_THRESHOLDS.copy()

    except OSError as e:
        _log.error(
            f"❌ [DataValidator] Erreur I/O lors de la lecture de {config_path}\n"
            f"   Erreur : {e}"
        )
        _log_fallback_details(
            _log,
            reason=f"Erreur I/O — OSError: {e}",
            config_path=config_path
        )
        return DEFAULT_THRESHOLDS.copy()

    # ── Cas 3 : résolution du namespace JSON ─────────────────────────────────
    # Structure attendue : {"data_validator": { ...clés numériques... }}
    # L'extraction cherche d'abord la clé racine "data_validator".
    # Si absente, tente un parsing à plat (flat) pour rétrocompatibilité.
    NAMESPACE_KEY = "data_validator"

    if NAMESPACE_KEY in raw_config:
        namespace = raw_config[NAMESPACE_KEY]
        if not isinstance(namespace, dict):
            _log.error(
                f"❌ [DataValidator] La clé '{NAMESPACE_KEY}' dans {config_path} "
                f"n'est pas un objet JSON (type reçu : {type(namespace).__name__})."
            )
            _log_fallback_details(
                _log,
                reason=f"'{NAMESPACE_KEY}' n'est pas un dict JSON valide",
                config_path=config_path
            )
            return DEFAULT_THRESHOLDS.copy()
        source = namespace
        _log.debug(f"   Namespace '{NAMESPACE_KEY}' détecté — extraction depuis raw_config['{NAMESPACE_KEY}']")
    else:
        # Rétrocompatibilité : JSON plat sans namespace
        source = raw_config
        _log.warning(
            f"⚠️  [DataValidator] Clé namespace '{NAMESPACE_KEY}' absente de {config_path}.\n"
            f"    Parsing en mode flat (rétrocompatibilité). "
            f"Structure recommandée : {{\"data_validator\": {{...}}}}"
        )

    # ── Cas 4 : extraction des clés numériques valides depuis le bon niveau ──
    thresholds_from_json = {
        key: value
        for key, value in source.items()
        if not key.endswith('_description') and isinstance(value, (int, float))
    }

    # ── Cas 5 : source vide ou sans aucune clé numérique ─────────────────────
    if not thresholds_from_json:
        _log.error(
            f"❌ [DataValidator] Aucune clé numérique valide trouvée dans {config_path}\n"
            f"   Clés présentes dans la source : {list(source.keys())}"
        )
        _log_fallback_details(
            _log,
            reason="Fichier JSON sans valeurs numériques exploitables",
            config_path=config_path
        )
        return DEFAULT_THRESHOLDS.copy()

    # ── Cas 6 : clés partiellement présentes → warning ciblé ─────────────────
    missing_keys = [k for k in DEFAULT_THRESHOLDS if k not in thresholds_from_json]
    if missing_keys:
        _log.warning(
            f"⚠️  [DataValidator] Clés manquantes dans {config_path} :\n"
            f"    Clés absentes : {missing_keys}\n"
            f"    Ces clés seront complétées par leur valeur DEFAULT_THRESHOLDS."
        )
        for key in missing_keys:
            _log.warning(
                f"    🔴 {key:<30} = {DEFAULT_THRESHOLDS[key]}  ← VALEUR PAR DÉFAUT (clé absente du JSON)"
            )

    # ── Merge final : JSON prime, défaut complète les manquants ──────────────
    final_config = {**DEFAULT_THRESHOLDS, **thresholds_from_json}

    _log.info(
        f"✅ [DataValidator] Configuration chargée depuis {config_path}\n"
        f"   max_price_change    = {final_config['max_price_change_pct']}%\n"
        f"   max_volume_spike    = {final_config['max_volume_spike_ratio']}x\n"
        f"   outlier_sigma       = {final_config['outlier_sigma']}\n"
        f"   gap_tolerance       = {final_config['gap_tolerance_multiplier']}x\n"
        f"   min_data_points     = {final_config['min_data_points']}\n"
        f"   min_volume          = {final_config['min_volume']}"
    )

    return final_config


# ============================================================================
# CLASSE DATAVALIDATOR
# ============================================================================

class DataValidator:
    """
    Validateur de données historiques OHLCV.
    
    Effectue validation complète : cohérence OHLCV, valeurs manquantes,
    ordre chronologique, gaps temporels, anomalies volume/prix.
    
    MÉTHODOLOGIE STATISTIQUE (amélioration vs specs) :
    Utilise MÉDIANE + MAD au lieu de MOYENNE + STD pour robustesse aux outliers.
    Exemple : [100, 110, 105, 95, 10000]
    - MOYENNE = 2082 → Seuil 10x = 20820 (outlier 10000 non détecté)
    - MÉDIANE = 105 → Seuil 10x = 1050 (outlier 10000 détecté correctement)
    """
    
    def __init__(
        self,
        config_path: Optional[Path] = None
    ):
        """
        Initialiser le validateur depuis la configuration JSON centralisée.

        Le fichier JSON est la SEULE source de vérité pour les seuils de validation.
        Aucun override par code n'est possible — toute modification de configuration
        doit passer par le fichier data_validator_config.json.

        Args:
            config_path: Chemin alternatif vers le fichier JSON de configuration.
                         Si None, utilise <project_root>/config/data_validator_config.json
        """
        self.logger = BulletLogger()
        self.thresholds = load_validator_config(config_path)

        self.logger.info(
            f"DataValidator initialized (config: JSON-only, no code override):\n"
            f"  max_price_change={self.thresholds['max_price_change_pct']}%, "
            f"max_volume_spike={self.thresholds['max_volume_spike_ratio']}x, "
            f"outlier_sigma={self.thresholds['outlier_sigma']}"
        )
    
    # ========================================================================
    # VALIDATION PRINCIPALE
    # ========================================================================
    
    def validate(
        self,
        df: pd.DataFrame,
        timeframe: Optional[str] = None,
        strict: bool = True,
        warn_missing_timeframe: bool = False
    ) -> ValidationReport:
        """
        Validation complète OHLCV.
        
        Args:
            df: DataFrame avec données OHLCV
            timeframe: Timeframe ('5m', '1h', etc.) pour détection gaps
            strict: Si True, rejette au moindre problème
            warn_missing_timeframe: Si True, émet WARNING si timeframe=None (sinon DEBUG)
        """
        self.logger.info("🔍 Starting comprehensive data validation...")
        
        report = {
            'is_valid': True,
            'total_issues': 0,
            'issues_by_type': {},
            'statistics': {},
            'recommendations': [],
            'checks': {}
        }
        
        # 1. Structure (normalise DatetimeIndex → colonne timestamp)
        self.logger.debug("Step 1/7: Checking structure...")
        struct_result = self._check_structure(df)
        report['checks']['structure'] = struct_result
        
        if not struct_result['valid']:
            report['is_valid'] = False
            report['total_issues'] += struct_result['issues_count']
            report['issues_by_type']['structure'] = struct_result['issues_count']
            self.logger.error(f"❌ Structure validation failed: {struct_result['message']}")
            return report
        
        # FORTIFICATION v2.2.2 : Utiliser df normalisé de _check_structure
        df = struct_result.get('normalized_df', df)
        
        # 2. Valeurs manquantes
        self.logger.debug("Step 2/7: Checking missing values...")
        missing_result = self.check_missing_values(df)
        report['checks']['missing_values'] = missing_result
        
        if missing_result['has_missing']:
            if strict:
                report['is_valid'] = False
            report['total_issues'] += missing_result['total_missing']
            report['issues_by_type']['missing_values'] = missing_result['total_missing']
            report['recommendations'].append(
                f"Remove or interpolate {missing_result['total_missing']} missing values"
            )
        
        # 3. Cohérence OHLCV
        self.logger.debug("Step 3/7: Checking OHLCV consistency...")
        ohlcv_result = self.check_ohlcv_consistency(df)
        report['checks']['ohlcv_consistency'] = ohlcv_result
        
        if ohlcv_result['has_inconsistencies']:
            if strict:
                report['is_valid'] = False
            report['total_issues'] += ohlcv_result['total_inconsistent']
            report['issues_by_type']['ohlcv_inconsistency'] = ohlcv_result['total_inconsistent']
            report['recommendations'].append(
                f"Fix {ohlcv_result['total_inconsistent']} OHLCV inconsistencies"
            )
        
        # 4. Ordre chronologique
        self.logger.debug("Step 4/7: Checking chronological order...")
        chrono_result = self.check_chronological_order(df)
        report['checks']['chronological_order'] = chrono_result
        
        if not chrono_result['is_sorted']:
            if strict:
                report['is_valid'] = False
            report['total_issues'] += chrono_result['out_of_order_count']
            report['issues_by_type']['chronological'] = chrono_result['out_of_order_count']
            report['recommendations'].append("Sort data by timestamp")
        
        # 5. Gaps temporels
        if timeframe:
            self.logger.debug("Step 5/7: Detecting temporal gaps...")
            gaps_result = self.detect_gaps(df, timeframe)
            report['checks']['gaps'] = gaps_result
            
            if gaps_result['has_gaps']:
                if strict and gaps_result['total_gaps'] > 0:
                    report['is_valid'] = False
                report['total_issues'] += gaps_result['total_gaps']
                report['issues_by_type']['gaps'] = gaps_result['total_gaps']
                report['recommendations'].append(
                    f"Address {gaps_result['total_gaps']} temporal gaps"
                )
        else:
            # FORTIFICATION v2.2.2 : Niveau warning contrôlable
            msg = (
                "No timeframe provided — temporal gap detection SKIPPED. "
                "Pass timeframe='5m' (or '1h', '1d', etc.) for gap validation."
            )
            if warn_missing_timeframe:
                self.logger.warning(f"⚠️ {msg}")
            else:
                self.logger.debug(msg)
            
            report['checks']['gaps'] = {'skipped': True, 'reason': 'No timeframe provided'}
        
        # 6. Anomalies volume
        self.logger.debug("Step 6/7: Detecting volume anomalies...")
        volume_result = self.detect_volume_anomalies(df)
        report['checks']['volume_anomalies'] = volume_result
        
        if volume_result['has_anomalies']:
            report['total_issues'] += volume_result['total_anomalies']
            report['issues_by_type']['volume_anomalies'] = volume_result['total_anomalies']
            report['recommendations'].append(
                f"Review {volume_result['total_anomalies']} volume anomalies"
            )
        
        # 7. Anomalies prix
        self.logger.debug("Step 7/7: Detecting price anomalies...")
        price_result = self.detect_price_anomalies(df)
        report['checks']['price_anomalies'] = price_result
        
        if price_result['has_anomalies']:
            report['total_issues'] += price_result['total_anomalies']
            report['issues_by_type']['price_anomalies'] = price_result['total_anomalies']
            report['recommendations'].append(
                f"Review {price_result['total_anomalies']} price anomalies"
            )
        
        report['statistics'] = self._compute_statistics(df)
        
        if report['is_valid']:
            self.logger.info(f"✅ Validation passed: {len(df)} clean rows")
        else:
            self.logger.warning(
                f"⚠️ Validation failed: {report['total_issues']} issues found"
            )
        
        return report
    
    # ========================================================================
    # VALIDATIONS INDIVIDUELLES
    # ========================================================================
    
    def _check_structure(self, df: pd.DataFrame) -> Dict[str, Any]:
        """
        Vérifier structure DataFrame + normaliser DatetimeIndex.
        
        FORTIFICATION v2.2.2 : Retourne df normalisé dans result['normalized_df']
        pour éviter duplication code dans quick_check().
        """
        result = {
            'valid': True,
            'issues_count': 0,
            'message': 'OK',
            'details': {},
            'normalized_df': df  # NOUVEAU v2.2.2
        }
        
        if df.empty:
            result['valid'] = False
            result['issues_count'] = 1
            result['message'] = "DataFrame is empty"
            return result
        
        # Normaliser DatetimeIndex → colonne timestamp
        if isinstance(df.index, pd.DatetimeIndex) and 'timestamp' not in df.columns:
            self.logger.debug(
                "DatetimeIndex detected without 'timestamp' column. "
                "Normalizing: reset_index() + rename('index' → 'timestamp')."
            )
            df = df.reset_index()
            if 'index' in df.columns and 'timestamp' not in df.columns:
                df = df.rename(columns={'index': 'timestamp'})
            result['normalized_df'] = df  # Retourner df normalisé
        
        # Vérifier colonnes
        missing_cols = set(EXPECTED_COLUMNS) - set(df.columns)
        if missing_cols:
            result['valid'] = False
            result['issues_count'] = len(missing_cols)
            result['message'] = f"Missing columns: {missing_cols}"
            result['details']['missing_columns'] = list(missing_cols)
            return result
        
        # Vérifier types
        type_issues = []
        
        if not pd.api.types.is_datetime64_any_dtype(df['timestamp']):
            type_issues.append("timestamp must be datetime64")
        
        for col in ['open', 'high', 'low', 'close', 'volume']:
            if not pd.api.types.is_numeric_dtype(df[col]):
                type_issues.append(f"{col} must be numeric")
        
        if type_issues:
            result['valid'] = False
            result['issues_count'] = len(type_issues)
            result['message'] = "; ".join(type_issues)
            result['details']['type_issues'] = type_issues
        
        return result
    
    def check_missing_values(self, df: pd.DataFrame) -> Dict[str, Any]:
        """Détecter valeurs manquantes (NaN, None, inf)."""
        result = {
            'has_missing': False,
            'total_missing': 0,
            'by_column': {},
            'missing_rows_indices': []
        }
        
        for col in EXPECTED_COLUMNS:
            nan_count = df[col].isna().sum()
            result['by_column'][col] = int(nan_count)
            result['total_missing'] += int(nan_count)
        
        for col in ['open', 'high', 'low', 'close', 'volume']:
            inf_count = np.isinf(df[col]).sum()
            if inf_count > 0:
                result['by_column'][f'{col}_inf'] = int(inf_count)
                result['total_missing'] += int(inf_count)
        
        missing_mask = df[EXPECTED_COLUMNS].isna().any(axis=1)
        inf_mask = np.isinf(df[['open', 'high', 'low', 'close', 'volume']]).any(axis=1)
        combined_mask = missing_mask | inf_mask
        
        if combined_mask.any():
            result['has_missing'] = True
            result['missing_rows_indices'] = df.index[combined_mask].tolist()[:100]
        
        return result
    
    def check_ohlcv_consistency(self, df: pd.DataFrame) -> Dict[str, Any]:
        """Vérifier cohérence OHLCV stricte (vectorisé numpy)."""
        result = {
            'has_inconsistencies': False,
            'total_inconsistent': 0,
            'by_type': {},
            'inconsistent_rows_indices': []
        }
        
        h = df['high'].values
        l = df['low'].values
        o = df['open'].values
        c = df['close'].values
        v = df['volume'].values
        
        masks_dict = {
            'high_low': h < l,
            'high_open': h < o,
            'high_close': h < c,
            'low_open': l > o,
            'low_close': l > c,
            'open_range': (o < l) | (o > h),
            'close_range': (c < l) | (c > h),
            'negative_volume': v < 0
        }
        
        for issue_type, mask in masks_dict.items():
            count = int(mask.sum())
            if count > 0:
                result['by_type'][issue_type] = count
                result['total_inconsistent'] += count
                result['has_inconsistencies'] = True
        
        combined_mask = (
            masks_dict['high_low'] | masks_dict['high_open'] | masks_dict['high_close'] |
            masks_dict['low_open'] | masks_dict['low_close'] | masks_dict['open_range'] |
            masks_dict['close_range'] | masks_dict['negative_volume']
        )
        
        if combined_mask.any():
            result['inconsistent_rows_indices'] = df.index[combined_mask].tolist()[:100]
        
        return result
    
    def check_chronological_order(self, df: pd.DataFrame) -> Dict[str, Any]:
        """Vérifier ordre chronologique timestamps."""
        result = {
            'is_sorted': True,
            'out_of_order_count': 0,
            'first_out_of_order_index': None,
            'duplicates': 0
        }
        
        is_sorted = df['timestamp'].is_monotonic_increasing
        result['is_sorted'] = bool(is_sorted)
        
        if not is_sorted:
            time_diffs = df['timestamp'].diff()
            out_of_order_mask = time_diffs < pd.Timedelta(0)
            result['out_of_order_count'] = int(out_of_order_mask.sum())
            
            if out_of_order_mask.any():
                result['first_out_of_order_index'] = int(
                    df.index[out_of_order_mask][0]
                )
        
        duplicates = df['timestamp'].duplicated().sum()
        result['duplicates'] = int(duplicates)
        
        if duplicates > 0:
            result['is_sorted'] = False
            result['out_of_order_count'] += int(duplicates)
        
        return result
    
    def detect_gaps(self, df: pd.DataFrame, timeframe: str) -> Dict[str, Any]:
        """Détecter gaps temporels selon timeframe."""
        result = {
            'has_gaps': False,
            'total_gaps': 0,
            'expected_interval': timeframe,
            'gaps': []
        }
        
        try:
            interval_seconds = parse_timeframe(timeframe, strict=False)
            expected_interval = pd.Timedelta(seconds=interval_seconds)
            
            time_diffs = df['timestamp'].diff()
            
            tolerance_multiplier = self.thresholds['gap_tolerance_multiplier']
            threshold = expected_interval * tolerance_multiplier
            
            gap_mask = time_diffs > threshold
            
            if gap_mask.any():
                result['has_gaps'] = True
                gap_indices = df.index[gap_mask].tolist()
                result['total_gaps'] = len(gap_indices)
                
                for idx in gap_indices[:50]:
                    if idx > 0:
                        gap_info = {
                            'index': int(idx),
                            'from': str(df.loc[idx-1, 'timestamp']),
                            'to': str(df.loc[idx, 'timestamp']),
                            'duration': str(time_diffs.loc[idx]),
                            'expected': str(expected_interval),
                            'missing_candles': int(
                                time_diffs.loc[idx] / expected_interval - 1
                            )
                        }
                        result['gaps'].append(gap_info)
                
                if len(gap_indices) > 50:
                    result['gaps'].append({
                        'note': f'... and {len(gap_indices) - 50} more gaps'
                    })
        
        except ValueError as e:
            result['error'] = f"Invalid timeframe: {e}"
        
        return result
    
    def detect_volume_anomalies(self, df: pd.DataFrame) -> Dict[str, Any]:
        """
        Détecter anomalies volume (MÉDIANE robuste aux outliers).
        
        Utilise MÉDIANE + MAD au lieu de MOYENNE + STD (justification : voir docstring classe).
        """
        result = {
            'has_anomalies': False,
            'total_anomalies': 0,
            'zero_volume_count': 0,
            'low_volume_count': 0,
            'spike_count': 0,
            'spike_indices': []
        }
        
        vol = df['volume'].values
        
        zero_mask = vol == 0
        result['zero_volume_count'] = int(zero_mask.sum())
        
        min_vol = self.thresholds['min_volume']
        low_mask = (vol > 0) & (vol < min_vol)
        result['low_volume_count'] = int(low_mask.sum())
        
        if len(df) >= self.thresholds['min_data_points']:
            volume_median = np.median(vol[vol > 0])
            volume_std = vol.std()
            
            if volume_median > 0 and volume_std > 0:
                max_spike_ratio = self.thresholds['max_volume_spike_ratio']
                spike_mask_ratio = vol > (volume_median * max_spike_ratio)
                
                mad = np.median(np.abs(vol - volume_median))
                if mad > 0:
                    modified_z = np.abs(vol - volume_median) / (1.4826 * mad)
                    spike_mask_zscore = modified_z > self.thresholds['outlier_sigma']
                else:
                    spike_mask_zscore = np.zeros(len(vol), dtype=bool)
                
                spike_mask = spike_mask_ratio | spike_mask_zscore
                result['spike_count'] = int(spike_mask.sum())
                
                if spike_mask.any():
                    result['spike_indices'] = df.index[spike_mask].tolist()[:50]
        
        result['total_anomalies'] = (
            result['zero_volume_count'] +
            result['low_volume_count'] +
            result['spike_count']
        )
        
        if result['total_anomalies'] > 0:
            result['has_anomalies'] = True
        
        return result
    
    def detect_price_anomalies(self, df: pd.DataFrame) -> Dict[str, Any]:
        """Détecter anomalies prix (MÉDIANE + MAD robuste)."""
        result = {
            'has_anomalies': False,
            'total_anomalies': 0,
            'extreme_changes': 0,
            'outliers': 0,
            'anomaly_indices': []
        }
        
        if len(df) < 2:
            return result
        
        close = df['close'].values
        
        price_changes_pct = np.abs(np.diff(close) / close[:-1] * 100)
        max_change = self.thresholds['max_price_change_pct']
        extreme_mask_np = price_changes_pct > max_change
        
        extreme_mask = np.concatenate([[False], extreme_mask_np])
        result['extreme_changes'] = int(extreme_mask.sum())
        
        if len(df) >= self.thresholds['min_data_points']:
            close_median = np.median(close)
            mad = np.median(np.abs(close - close_median))
            
            if mad > 0:
                modified_z = np.abs(close - close_median) / (1.4826 * mad)
                outlier_mask = modified_z > self.thresholds['outlier_sigma']
                result['outliers'] = int(outlier_mask.sum())
                
                combined_mask = extreme_mask | outlier_mask
            else:
                combined_mask = extreme_mask
        else:
            combined_mask = extreme_mask
        
        if combined_mask.any():
            result['has_anomalies'] = True
            result['total_anomalies'] = int(combined_mask.sum())
            result['anomaly_indices'] = df.index[combined_mask].tolist()[:50]
        
        return result
    
    # ========================================================================
    # STATISTIQUES & RAPPORTS
    # ========================================================================
    
    def _compute_statistics(self, df: pd.DataFrame) -> Dict[str, Any]:
        """Calculer statistiques descriptives."""
        stats = {
            'total_rows': len(df),
            'date_range': {
                'start': str(df['timestamp'].min()),
                'end': str(df['timestamp'].max()),
                'duration_days': (df['timestamp'].max() - df['timestamp'].min()).days
            },
            'price_stats': {
                'close_mean': float(df['close'].mean()),
                'close_std': float(df['close'].std()),
                'close_min': float(df['close'].min()),
                'close_max': float(df['close'].max()),
                'close_range_pct': float(
                    (df['close'].max() - df['close'].min()) / df['close'].mean() * 100
                )
            },
            'volume_stats': {
                'mean': float(df['volume'].mean()),
                'std': float(df['volume'].std()),
                'min': float(df['volume'].min()),
                'max': float(df['volume'].max()),
                'zero_count': int((df['volume'] == 0).sum())
            }
        }
        
        return stats
    
    def print_report(self, report: ValidationReport, detailed: bool = True):
        """Afficher rapport formaté."""
        self.logger.log_separator('INFO', '=', 80)
        self.logger.info("📊 DATA VALIDATION REPORT")
        self.logger.log_separator('INFO', '=', 80)
        
        status = "✅ VALID" if report['is_valid'] else "❌ INVALID"
        self.logger.info(f"Status: {status}")
        self.logger.info(f"Total Issues: {report['total_issues']}")
        
        if report['issues_by_type']:
            self.logger.info("\nIssues by Type:")
            for issue_type, count in report['issues_by_type'].items():
                self.logger.info(f"  - {issue_type}: {count}")
        
        if 'statistics' in report and report['statistics']:
            stats = report['statistics']
            self.logger.info(f"\nData Statistics:")
            self.logger.info(f"  Total Rows: {stats['total_rows']:,}")
            self.logger.info(
                f"  Period: {stats['date_range']['start']} → "
                f"{stats['date_range']['end']} "
                f"({stats['date_range']['duration_days']} days)"
            )
            self.logger.info(
                f"  Price Range: ${stats['price_stats']['close_min']:.2f} - "
                f"${stats['price_stats']['close_max']:.2f} "
                f"({stats['price_stats']['close_range_pct']:.1f}% range)"
            )
            self.logger.info(
                f"  Volume: mean={stats['volume_stats']['mean']:.2f}, "
                f"zero_count={stats['volume_stats']['zero_count']}"
            )
        
        if detailed and 'checks' in report:
            self.logger.info("\nDetailed Checks:")
            
            for check_name, check_result in report['checks'].items():
                if isinstance(check_result, dict):
                    self.logger.info(f"\n  {check_name.upper().replace('_', ' ')}:")
                    
                    for key, value in list(check_result.items())[:5]:
                        if not isinstance(value, (list, dict)):
                            self.logger.info(f"    {key}: {value}")
        
        if report['recommendations']:
            self.logger.info("\nRecommendations:")
            for i, rec in enumerate(report['recommendations'], 1):
                self.logger.info(f"  {i}. {rec}")
        
        self.logger.log_separator('INFO', '=', 80)
    
    def generate_summary(self, report: ValidationReport) -> str:
        """Générer résumé textuel."""
        lines = []
        lines.append("=" * 60)
        lines.append("DATA VALIDATION SUMMARY")
        lines.append("=" * 60)
        
        status = "✅ VALID" if report['is_valid'] else "❌ INVALID"
        lines.append(f"Status: {status}")
        lines.append(f"Total Issues: {report['total_issues']}")
        
        if report['issues_by_type']:
            lines.append("\nIssues Breakdown:")
            for issue_type, count in report['issues_by_type'].items():
                lines.append(f"  • {issue_type}: {count}")
        
        if report['recommendations']:
            lines.append("\nRecommendations:")
            for i, rec in enumerate(report['recommendations'], 1):
                lines.append(f"  {i}. {rec}")
        
        lines.append("=" * 60)
        
        return "\n".join(lines)
    
    def export_report_json(
        self,
        report: ValidationReport,
        filepath: Union[str, Path],
        pretty: bool = True
    ) -> None:
        """Exporter rapport au format JSON."""
        filepath = Path(filepath)
        
        def json_serializer(obj):
            if isinstance(obj, (datetime, pd.Timestamp)):
                return obj.isoformat()
            if isinstance(obj, np.integer):
                return int(obj)
            if isinstance(obj, np.floating):
                return float(obj)
            if isinstance(obj, np.ndarray):
                return obj.tolist()
            if isinstance(obj, pd.Series):
                return obj.tolist()
            return str(obj)
        
        try:
            filepath.parent.mkdir(parents=True, exist_ok=True)
            
            with open(filepath, 'w', encoding='utf-8') as f:
                if pretty:
                    json.dump(report, f, indent=2, default=json_serializer, ensure_ascii=False)
                else:
                    json.dump(report, f, default=json_serializer, ensure_ascii=False)
            
            self.logger.info(f"✅ Validation report exported to JSON: {filepath}")
            
        except TypeError as e:
            self.logger.error(f"❌ Failed to serialize report to JSON: {e}")
            raise TypeError(f"Report contains non-serializable objects: {e}") from e
        
        except IOError as e:
            self.logger.error(f"❌ Failed to write JSON file {filepath}: {e}")
            raise IOError(f"Cannot write to {filepath}: {e}") from e
        
        except Exception as e:
            self.logger.error(f"❌ Unexpected error exporting JSON: {e}")
            raise
    
    # ========================================================================
    # CORRECTION AUTOMATIQUE
    # ========================================================================
    
    def auto_clean(
        self,
        df: pd.DataFrame,
        report: Optional[ValidationReport] = None,
        aggressive: bool = False
    ) -> pd.DataFrame:
        """Nettoyer données selon rapport validation."""
        self.logger.info(f"🧹 Auto-cleaning data (aggressive={aggressive})...")
        
        df_clean = df.copy()
        initial_len = len(df_clean)
        
        if report is None:
            report = self.validate(df_clean, strict=False)
        
        # 1. Supprimer valeurs manquantes
        missing = report['checks'].get('missing_values', {})
        if missing.get('has_missing', False) or missing.get('total_missing', 0) > 0:
            self.logger.info("  Removing rows with missing/infinite values...")
            
            df_clean = df_clean.dropna(subset=EXPECTED_COLUMNS, how='any')
            
            numeric_cols = ['open', 'high', 'low', 'close', 'volume']
            for col in numeric_cols:
                if col in df_clean.columns:
                    finite_mask = np.isfinite(df_clean[col].values)
                    if not finite_mask.all():
                        df_clean = df_clean[finite_mask]
                        self.logger.debug(f"    Removed infinite values from {col}")
        
        # 2. Supprimer OHLCV incohérent
        ohlcv = report['checks'].get('ohlcv_consistency', {})
        if ohlcv.get('has_inconsistencies', False):
            inconsistent_indices = ohlcv.get('inconsistent_rows_indices', [])
            
            if inconsistent_indices:
                self.logger.info(
                    f"  Removing {len(inconsistent_indices)} rows with OHLCV inconsistencies"
                )
                valid_indices = [idx for idx in inconsistent_indices if idx in df_clean.index]
                if valid_indices:
                    df_clean = df_clean.drop(index=valid_indices, errors='ignore')
        
        # 3. Trier par timestamp
        chrono = report['checks'].get('chronological_order', {})
        if not chrono.get('is_sorted', True):
            self.logger.info("  Sorting by timestamp...")
            df_clean = df_clean.sort_values('timestamp').reset_index(drop=True)
        
        # 4. Supprimer doublons
        if chrono.get('duplicates', 0) > 0:
            self.logger.info("  Removing duplicate timestamps...")
            df_clean = df_clean.drop_duplicates(subset=['timestamp'], keep='first')
        
        # 5. Mode agressif
        if aggressive:
            volume_anom = report['checks'].get('volume_anomalies', {})
            if volume_anom.get('zero_volume_count', 0) > 0:
                self.logger.info("  Removing zero volume rows...")
                if 'volume' in df_clean.columns:
                    zero_mask = df_clean['volume'].values == 0
                    df_clean = df_clean[~zero_mask]
            
            price_anom = report['checks'].get('price_anomalies', {})
            anomaly_indices = price_anom.get('anomaly_indices', [])
            
            if anomaly_indices:
                self.logger.info(f"  Removing {len(anomaly_indices)} price anomalies")
                valid_indices = [idx for idx in anomaly_indices if idx in df_clean.index]
                if valid_indices:
                    df_clean = df_clean.drop(index=valid_indices, errors='ignore')
        
        df_clean = df_clean.reset_index(drop=True)
        
        removed = initial_len - len(df_clean)
        pct_removed = (removed / initial_len * 100) if initial_len > 0 else 0
        
        self.logger.info(
            f"✅ Cleaning complete: {removed:,} rows removed "
            f"({pct_removed:.1f}%), {len(df_clean):,} remaining"
        )
        
        return df_clean
    
    def suggest_interpolation(
        self,
        df: pd.DataFrame,
        report: ValidationReport
    ) -> List[str]:
        """Suggérer méthodes interpolation pour gaps et valeurs manquantes."""
        suggestions = []
        
        gaps = report['checks'].get('gaps', {})
        if gaps.get('has_gaps', False):
            total_gaps = gaps['total_gaps']
            suggestions.append(
                f"Found {total_gaps} temporal gaps. Consider:\n"
                f"  - Interpolate missing candles (forward fill for OHLC, 0 for volume)\n"
                f"  - Download missing data from exchange\n"
                f"  - Split dataset at large gaps"
            )
        
        missing = report['checks'].get('missing_values', {})
        if missing.get('has_missing', False):
            by_col = missing['by_column']
            suggestions.append(
                f"Found {missing['total_missing']} missing values:\n"
                f"  {by_col}\n"
                f"  Consider: df.interpolate(method='linear') or df.fillna(method='ffill')"
            )
        
        vol_anom = report['checks'].get('volume_anomalies', {})
        if vol_anom.get('zero_volume_count', 0) > 0:
            suggestions.append(
                f"Found {vol_anom['zero_volume_count']} zero-volume candles.\n"
                f"  Options:\n"
                f"  - Remove these rows\n"
                f"  - Replace with small non-zero value (e.g., volume.mean() * 0.01)\n"
                f"  - Verify data source quality"
            )
        
        return suggestions
    
    # ========================================================================
    # UTILITAIRES
    # ========================================================================
    
    def quick_check(self, df: pd.DataFrame) -> bool:
        """
        Vérification rapide (structure + cohérence OHLCV).
        
        FORTIFICATION v2.2.2 : Utilise df normalisé de _check_structure()
        pour éviter duplication code normalisation DatetimeIndex.
        """
        if df is None or not isinstance(df, pd.DataFrame):
            return False

        struct = self._check_structure(df)
        if not struct['valid']:
            return False

        # FORTIFICATION v2.2.2 : Utiliser df normalisé (pas de duplication)
        df_norm = struct.get('normalized_df', df)

        ohlcv = self.check_ohlcv_consistency(df_norm)
        if ohlcv['has_inconsistencies']:
            return False

        return True
    
    def validate_single_candle(self, candle: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """Valider une seule bougie OHLCV."""
        for key in EXPECTED_COLUMNS:
            if key not in candle:
                return False, f"Missing key: {key}"
        
        try:
            ts = candle['timestamp']
            if not isinstance(ts, (datetime, pd.Timestamp)):
                timestamp_to_datetime(ts)
            
            o = float(candle['open'])
            h = float(candle['high'])
            l = float(candle['low'])
            c = float(candle['close'])
            v = float(candle['volume'])
        except (ValueError, TypeError) as e:
            return False, f"Invalid type: {e}"
        
        if h < l:
            return False, f"high ({h}) < low ({l})"
        if h < o:
            return False, f"high ({h}) < open ({o})"
        if h < c:
            return False, f"high ({h}) < close ({c})"
        if l > o:
            return False, f"low ({l}) > open ({o})"
        if l > c:
            return False, f"low ({l}) > close ({c})"
        if o < l or o > h:
            return False, f"open ({o}) not in [low, high]"
        if c < l or c > h:
            return False, f"close ({c}) not in [low, high]"
        if v < 0:
            return False, f"volume ({v}) < 0"
        
        if not is_valid_price(o):
            return False, f"Invalid open price: {o}"
        if not is_valid_price(h):
            return False, f"Invalid high price: {h}"
        if not is_valid_price(l):
            return False, f"Invalid low price: {l}"
        if not is_valid_price(c):
            return False, f"Invalid close price: {c}"
        if not is_valid_volume(v):
            return False, f"Invalid volume: {v}"
        
        return True, None
    
    def get_validation_statistics(self) -> Dict[str, Any]:
        """Obtenir seuils validation configurés."""
        return self.thresholds.copy()


# ============================================================================
# FONCTIONS UTILITAIRES GLOBALES
# ============================================================================

def quick_validate(
    df: pd.DataFrame,
    timeframe: Optional[str] = None,
    print_report: bool = False
) -> bool:
    """Validation rapide sans instancier DataValidator."""
    validator = DataValidator()
    report = validator.validate(df, timeframe=timeframe, strict=True)
    
    if print_report:
        validator.print_report(report)
    
    return report['is_valid']


def validate_and_clean(
    df: pd.DataFrame,
    timeframe: Optional[str] = None,
    aggressive: bool = False
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """Valider et nettoyer données en une fonction."""
    validator = DataValidator()
    
    report = validator.validate(df, timeframe=timeframe, strict=False)
    
    if not report['is_valid'] or aggressive:
        df_clean = validator.auto_clean(df, report=report, aggressive=aggressive)
    else:
        df_clean = df.copy()
    
    return df_clean, report

# FIN DU MODULE