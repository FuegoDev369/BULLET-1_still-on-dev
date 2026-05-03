"""
BULLET-1 - Uncertainty Candle Indicator
========================================
Module 13 - CRITIQUE (niveau 3) | Requis par signal_generator.py

Responsabilités :
    - Détection des bougies d'incertitude selon critères configurables
    - Calcul body_pct, mèches supérieure et inférieure
    - Validation des critères (body < body_max_pct, wicks >= wick_min_pct)
    - Métriques avancées (wick_ratio, body_position, signal_strength)
    - Traitement batch optimisé pandas (vectorisé)
    - Gestion robuste des cas limites (perfect doji, range=0)
    - Export détections vers JSON/CSV (configurable)

Fonctionnalités étendues (hors scope initial) :
    - Classification détaillée des types de doji
    - Fonctions utilitaires standalone (quick_detect, classify_candle_type)
    - Mise à jour dynamique de configuration (update_config)
    - Statistiques batch détaillées

Config : config/uncertainty_candle_config.json
Dépend de : logger.py (module 3)
Requis par : signal_generator.py, strategy.py, engine.py

Version: 2.2.2
Date: 2026-03-13
Author: FuegoDev
"""

import sys
import json
import csv
import gzip
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
from datetime import datetime, timezone
import warnings

try:
    import pandas as pd
    import numpy as np
    PANDAS_AVAILABLE = True
except ImportError:
    PANDAS_AVAILABLE = False
    warnings.warn("pandas/numpy non disponibles. Traitement batch non optimisé.", ImportWarning)

current_file = Path(__file__).resolve()
project_root = current_file.parent.parent.parent
# [v2.2.2 — FIX-UC-1] Guard ajouté — évite les doublons dans sys.path à chaque
# instanciation. Pattern uniforme avec tous les autres modules BULLET-1.
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from src.utils.logger import BulletLogger

#: Version du module — utilisée dans les métadonnées JSON exportées.
_VERSION = "2.2.2"


# Fallback si fichier JSON de configuration absent
DEFAULT_CONFIG = {
    'detection': {
        'body_max_pct':       {'value': 33.0},
        'wick_min_pct':       {'value': 20.0},
        'require_both_wicks': {'value': True},
    },
    'filters': {
        'min_candle_body_usdt':  {'value': 10.0},
        'max_candle_range_usdt': {'value': 10000.0},
    },
    'validation': {
        'strict_validation': {'value': True},
    },
    'features': {
        'enable_advanced_metrics':    {'value': True},
        'enable_doji_classification': {'value': True},
        'enable_batch_statistics':    {'value': True},
    },
    'export': {
        'enabled':        {'value': True},
        'default_format': {'value': 'json'},
        'json': {
            'indent':            {'value': 2},
            'include_metadata':  {'value': True},
        },
        'csv': {
            'delimiter':      {'value': ','},
            'encoding':       {'value': 'utf-8'},
            'flatten_nested': {'value': True},
        },
        'auto_export': {
            'enabled':   {'value': False},
            'directory': {'value': 'exports/uncertainty_candles'},
        },
        'compression': {
            'enabled': {'value': False},
            'level':   {'value': 6},
        },
    },
}

SIGNAL_STRENGTH_WEAK     = 0.33
SIGNAL_STRENGTH_MODERATE = 0.67
SIGNAL_STRENGTH_STRONG   = 1.0
FLOAT_TOLERANCE          = 1e-10
DEFAULT_CONFIG_PATH      = 'config/uncertainty_candle_config.json'

CSV_QUOTING_MAP = {
    'minimal':    csv.QUOTE_MINIMAL,
    'all':        csv.QUOTE_ALL,
    'nonnumeric': csv.QUOTE_NONNUMERIC,
    'none':       csv.QUOTE_NONE,
}


class UncertaintyCandleIndicator:
    """
    Détecteur de bougies d'incertitude avec métriques avancées et export.

    Une bougie d'incertitude présente :
        - Un petit corps  (< body_max_pct% de la range)
        - De longues mèches (>= wick_min_pct% chacune)

    Configuration chargée depuis config/uncertainty_candle_config.json.
    Voir ce fichier pour la documentation complète des paramètres.

    Fonctionnalités étendues (hors scope initial) :
        - Classification détaillée des types de doji
        - Statistiques batch, update_config() dynamique
        - Export JSON/CSV configurable avec compression
    """

    def __init__(self, config_path: Optional[str] = None):
        """
        Args:
            config_path: Chemin vers le fichier JSON de configuration.
                         Si None, utilise DEFAULT_CONFIG_PATH.
                         Si introuvable, bascule sur DEFAULT_CONFIG.
        Raises:
            ValueError: Si paramètres invalides.
        """
        self.logger = BulletLogger()
        self.config_path = config_path or DEFAULT_CONFIG_PATH
        self.config = self._load_config()
        self._apply_config()
        self._validate_parameters()
        self.logger.info(
            f"UncertaintyCandleIndicator v2.2.1 | "
            f"body_max={self.body_max_pct}%, wick_min={self.wick_min_pct}%, "
            f"require_both_wicks={self.require_both_wicks}, "
            f"export={self.export_enabled}"
        )

    def _load_config(self) -> Dict:
        """Charge la config JSON. Bascule sur DEFAULT_CONFIG si absent ou invalide."""
        path = Path(self.config_path)
        if not path.exists():
            self.logger.warning(f"Config non trouvée: {self.config_path} → DEFAULT_CONFIG.")
            # [v2.2.2 — FIX-UC-2] deepcopy : évite que update_config() corrompe
            # la constante globale DEFAULT_CONFIG pour les instances suivantes.
            return deepcopy(DEFAULT_CONFIG)
        try:
            with open(path, encoding='utf-8') as f:
                config = json.load(f)
            self.logger.info(f"Config chargée: {self.config_path}")
            return config
        except (json.JSONDecodeError, OSError) as e:
            self.logger.error(f"Erreur chargement config: {e} → DEFAULT_CONFIG.")
            return deepcopy(DEFAULT_CONFIG)  # [v2.2.2 — FIX-UC-2]

    def _get(self, key_path: str, default: Any) -> Any:
        """
        Lit une valeur dans la config par chemin de clés pointées.
        Supporte le pattern {'value': X, 'desc': ...} du JSON.

        Args:
            key_path: Chemin point-séparé, ex: 'export.json.indent'
            default: Valeur retournée si clé absente.
        """
        node = self.config
        for key in key_path.split('.'):
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node['value'] if isinstance(node, dict) and 'value' in node else node

    def _apply_config(self):
        """Charge tous les attributs depuis self.config. Partagé par __init__ et update_config."""
        g = self._get
        self.body_max_pct         = float(g('detection.body_max_pct',       33.0))
        self.wick_min_pct         = float(g('detection.wick_min_pct',        20.0))
        self.require_both_wicks   = bool( g('detection.require_both_wicks',  True))
        self.min_candle_body_usdt = float(g('filters.min_candle_body_usdt',  10.0))
        self.max_candle_range_usdt= float(g('filters.max_candle_range_usdt', 10000.0))
        self.strict_validation    = bool( g('validation.strict_validation',  True))
        self.enable_advanced_metrics    = bool(g('features.enable_advanced_metrics',    True))
        self.enable_doji_classification = bool(g('features.enable_doji_classification', True))
        self.enable_batch_statistics    = bool(g('features.enable_batch_statistics',    True))
        self.export_enabled  = bool(g('export.enabled', True))
        self.export_config   = self._build_export_config()

    def _build_export_config(self) -> Dict:
        """Construit le dict de configuration d'export aplati."""
        g = self._get
        return {
            'default_format':       g('export.default_format',           'json'),
            'json_indent':          g('export.json.indent',               2),
            'json_include_metadata':g('export.json.include_metadata',     True),
            'csv_delimiter':        g('export.csv.delimiter',             ','),
            'csv_encoding':         g('export.csv.encoding',              'utf-8'),
            'csv_flatten_nested':   g('export.csv.flatten_nested',        True),
            'auto_export_enabled':  g('export.auto_export.enabled',       False),
            'auto_export_dir':      g('export.auto_export.directory',     'exports/uncertainty_candles'),
            'compression_enabled':  g('export.compression.enabled',       False),
            'compression_level':    g('export.compression.level',         6),
        }

    def _validate_parameters(self):
        """Valide la cohérence des paramètres de détection."""
        if not (0 < self.body_max_pct <= 100):
            raise ValueError(f"body_max_pct hors plage (0-100]: {self.body_max_pct}")
        if not (0 < self.wick_min_pct <= 100):
            raise ValueError(f"wick_min_pct hors plage (0-100]: {self.wick_min_pct}")
        if self.min_candle_body_usdt < 0:
            raise ValueError(f"min_candle_body_usdt doit être >= 0: {self.min_candle_body_usdt}")
        if self.max_candle_range_usdt <= 0:
            raise ValueError(f"max_candle_range_usdt doit être > 0: {self.max_candle_range_usdt}")

        if self.require_both_wicks:
            total = self.body_max_pct + 2 * self.wick_min_pct
            if total > 100:
                msg = (
                    f"Config impossible: body_max({self.body_max_pct}%) "
                    f"+ 2×wick_min({self.wick_min_pct}%) = {total}% > 100%."
                )
                if self.strict_validation:
                    raise ValueError(msg)
                self.logger.warning(msg)

    def _make_error_result(self) -> Dict:
        """Retourne un résultat vide (is_uncertainty=False) pour cas d'erreur."""
        result = {
            'is_uncertainty': False,
            'body_pct':        0.0,
            'upper_wick_pct':  0.0,
            'lower_wick_pct':  0.0,
            'range':           0.0,
            'body':            0.0,
            'criteria_met':    {},
            'reason':          'error',
            'signal_strength': 0.0,
            'doji_type':       'none',
        }
        if self.enable_advanced_metrics:
            result.update(wick_ratio=0.0, body_position=0.0, asymmetry=0.0)
        return result

    def _classify_doji_type(self, metrics: Dict) -> str:
        """
        Classifie le type de doji détecté.

        FONCTIONNALITÉ ÉTENDUE (hors scope initial) :
        Distingue les patterns d'incertitude pour affiner l'analyse de marché.

        Returns:
            'perfect_doji' | 'dragonfly_doji' | 'gravestone_doji' |
            'long_legged_doji' | 'standard_uncertainty' | 'none'
        """
        if not self.enable_doji_classification:
            return 'standard_uncertainty'

        bp = metrics['body_pct']
        up = metrics['upper_wick_pct']
        lp = metrics['lower_wick_pct']

        if bp < 1.0  and abs(up - lp) < 5.0:                     return 'perfect_doji'
        if lp >= 50.0 and up < 10.0  and bp < 10.0:              return 'dragonfly_doji'
        if up >= 50.0 and lp < 10.0  and bp < 10.0:              return 'gravestone_doji'
        if up >= 35.0 and lp >= 35.0 and bp < 15.0:              return 'long_legged_doji'
        if bp < self.body_max_pct:                                 return 'standard_uncertainty'
        return 'none'

    def _calculate_signal_strength(self, metrics: Dict, criteria_met: Dict) -> float:
        """
        Calcule la force du signal d'incertitude (0.0 → 1.0).
        Corps petit + mèches longues + mèches équilibrées = signal fort.
        Pondération : corps 40%, mèches 40%, équilibre 20%.
        """
        if not criteria_met.get('small_body', False):
            return 0.0

        body_score = max(0.0, 1.0 - metrics['body_pct'] / self.body_max_pct)
        wick_score = min(1.0, (metrics['upper_wick_pct'] + metrics['lower_wick_pct']) / 2 / 50.0)

        up, lp = metrics['upper_wick_pct'], metrics['lower_wick_pct']
        has_up, has_lo = up > FLOAT_TOLERANCE, lp > FLOAT_TOLERANCE
        if has_up and has_lo:
            balance_score = min(up / lp, lp / up)
        elif has_up or has_lo:
            balance_score = 0.0
        else:
            balance_score = 1.0  # perfect doji

        return round(0.4 * body_score + 0.4 * wick_score + 0.2 * balance_score, 4)

    def calculate_candle_metrics(self, candle: Dict) -> Dict:
        """
        Calcule les métriques complètes d'une bougie OHLC.

        Args:
            candle: Dict avec 'open', 'high', 'low', 'close'.

        Returns:
            Dict avec range, body, body_pct, upper/lower wick (abs et %),
            is_bullish, et métriques avancées si activées.

        Raises:
            ValueError: Si données OHLC invalides ou incohérentes.
        """
        try:
            o, h, l, c = (float(candle[k]) for k in ('open', 'high', 'low', 'close'))
        except (KeyError, TypeError, ValueError) as e:
            raise ValueError(f"Données OHLC invalides: {e}")

        if any(x < 0 for x in (o, h, l, c)):
            raise ValueError(f"OHLC contient des valeurs négatives: {o},{h},{l},{c}")
        if any(x > 1e12 for x in (o, h, l, c)):
            raise ValueError(f"OHLC contient des valeurs extrêmes (>1e12): {o},{h},{l},{c}")
        if not (l <= o <= h and l <= c <= h):
            raise ValueError(f"OHLC incohérent (L≤O,C≤H non satisfait): {o},{h},{l},{c}")

        is_perfect_doji = h == l or (
            abs(h - l) < FLOAT_TOLERANCE and
            abs(o - c) < FLOAT_TOLERANCE and
            abs(o - h) < FLOAT_TOLERANCE
        )

        if is_perfect_doji:
            self.logger.debug(f"Perfect doji: O={o} H={h} L={l} C={c}")
            base = {
                'range': 0.0, 'body': 0.0, 'body_pct': 0.0,
                'upper_wick': 0.0, 'upper_wick_pct': 0.0,
                'lower_wick': 0.0, 'lower_wick_pct': 0.0,
                'is_bullish': c >= o,
            }
            if self.enable_advanced_metrics:
                base.update(wick_ratio=1.0, body_position=0.5, is_perfect_doji=True, asymmetry=0.0)
            return base

        rng   = h - l
        body  = abs(c - o)
        max_oc, min_oc = max(o, c), min(o, c)
        upper = h - max_oc
        lower = min_oc - l

        base = {
            'range':           rng,
            'body':            body,
            'body_pct':        body  / rng * 100,
            'upper_wick':      upper,
            'upper_wick_pct':  upper / rng * 100,
            'lower_wick':      lower,
            'lower_wick_pct':  lower / rng * 100,
            'is_bullish':      c >= o,
        }

        if self.enable_advanced_metrics:
            wick_ratio = (upper / lower if lower > FLOAT_TOLERANCE
                          else (float('inf') if upper > FLOAT_TOLERANCE else 1.0))
            total_wicks = upper + lower
            base.update(
                wick_ratio    = wick_ratio,
                body_position = (min_oc - l) / rng,
                is_perfect_doji = False,
                asymmetry     = abs(upper - lower) / total_wicks if total_wicks > FLOAT_TOLERANCE else 0.0,
            )
        return base

    def detect(self, candle: Dict) -> Dict:
        """
        Détecte si une bougie est une bougie d'incertitude.

        Critères évalués dans l'ordre :
            1. body_pct < body_max_pct
            2. upper_wick_pct >= wick_min_pct
            3. lower_wick_pct >= wick_min_pct  (ou au moins 1 si require_both_wicks=False)
            4. body >= min_candle_body_usdt   (anti-bruit)
            5. range <= max_candle_range_usdt  (anti-anomalie)

        Args:
            candle: Dict avec 'open', 'high', 'low', 'close'.

        Returns:
            Dict avec is_uncertainty, métriques, criteria_met, reason,
            signal_strength, doji_type (et métriques avancées si activées).
        """
        try:
            metrics = self.calculate_candle_metrics(candle)
        except ValueError as e:
            self.logger.error(f"Erreur calcul métriques: {e}")
            result = self._make_error_result()
            result['reason'] = f'invalid_ohlc: {e}'
            return result

        # Cas spécial : perfect doji = incertitude maximale
        if metrics.get('is_perfect_doji', False):
            self.logger.debug("✅ Perfect doji: incertitude maximale")
            result = {
                'is_uncertainty': True,
                'body_pct': 0.0, 'upper_wick_pct': 0.0, 'lower_wick_pct': 0.0,
                'range': metrics['range'], 'body': metrics['body'],
                'criteria_met': {
                    'small_body': True, 'upper_wick_ok': True, 'lower_wick_ok': True,
                    'both_wicks_ok': True, 'body_above_min': True,
                    'range_below_max': True, 'is_perfect_doji': True,
                },
                'reason':          'perfect_doji_detected',
                'signal_strength': 1.0,
                'doji_type':       'perfect_doji',
            }
            if self.enable_advanced_metrics:
                result.update(
                    wick_ratio    = metrics.get('wick_ratio', 1.0),
                    body_position = metrics.get('body_position', 0.5),
                    asymmetry     = metrics.get('asymmetry', 0.0),
                )
            return result

        # Évaluation des critères
        small_body     = metrics['body_pct']      < self.body_max_pct
        upper_wick_ok  = metrics['upper_wick_pct'] >= self.wick_min_pct
        lower_wick_ok  = metrics['lower_wick_pct'] >= self.wick_min_pct
        both_wicks_ok  = (upper_wick_ok and lower_wick_ok) if self.require_both_wicks else (upper_wick_ok or lower_wick_ok)
        body_above_min = metrics['body']  >= self.min_candle_body_usdt
        range_below_max= metrics['range'] <= self.max_candle_range_usdt

        criteria_key = 'both_wicks_ok' if self.require_both_wicks else 'at_least_one_wick_ok'
        criteria_met = {
            'small_body':      small_body,
            'upper_wick_ok':   upper_wick_ok,
            'lower_wick_ok':   lower_wick_ok,
            criteria_key:      both_wicks_ok,
            'body_above_min':  body_above_min,
            'range_below_max': range_below_max,
        }

        is_uncertainty = small_body and both_wicks_ok and body_above_min and range_below_max

        reasons = []
        if not small_body:      reasons.append(f"body_too_large({metrics['body_pct']:.2f}%>={self.body_max_pct}%)")
        if not upper_wick_ok:   reasons.append(f"upper_wick_too_small({metrics['upper_wick_pct']:.2f}%<{self.wick_min_pct}%)")
        if not lower_wick_ok:   reasons.append(f"lower_wick_too_small({metrics['lower_wick_pct']:.2f}%<{self.wick_min_pct}%)")
        if not both_wicks_ok and self.require_both_wicks is False: reasons.append("no_significant_wick")
        if not body_above_min:  reasons.append(f"body_too_small_usdt({metrics['body']:.2f}<{self.min_candle_body_usdt})")
        if not range_below_max: reasons.append(f"range_too_large({metrics['range']:.2f}>{self.max_candle_range_usdt})")

        doji_type       = self._classify_doji_type(metrics) if is_uncertainty else 'none'
        signal_strength = self._calculate_signal_strength(metrics, criteria_met)

        result = {
            'is_uncertainty':  is_uncertainty,
            'body_pct':        metrics['body_pct'],
            'upper_wick_pct':  metrics['upper_wick_pct'],
            'lower_wick_pct':  metrics['lower_wick_pct'],
            'range':           metrics['range'],
            'body':            metrics['body'],
            'criteria_met':    criteria_met,
            'reason':          'uncertainty_detected' if is_uncertainty else '; '.join(reasons),
            'signal_strength': signal_strength,
            'doji_type':       doji_type,
        }

        if self.enable_advanced_metrics:
            result.update(
                wick_ratio    = metrics.get('wick_ratio', 0.0),
                body_position = metrics.get('body_position', 0.0),
                asymmetry     = metrics.get('asymmetry', 0.0),
            )

        if is_uncertainty:
            label = ("FORT" if signal_strength >= SIGNAL_STRENGTH_STRONG else
                     "MODÉRÉ" if signal_strength >= SIGNAL_STRENGTH_MODERATE else "FAIBLE")
            self.logger.debug(
                f"✅ Uncertainty [{label}] type={doji_type} strength={signal_strength:.2f} "
                f"body={metrics['body_pct']:.2f}% up={metrics['upper_wick_pct']:.2f}% lo={metrics['lower_wick_pct']:.2f}%"
            )
        else:
            self.logger.debug(f"❌ Not uncertainty: {result['reason']}")

        return result

    def detect_batch(self, candles: Union[List[Dict], 'pd.DataFrame']) -> Union[List[Dict], 'pd.DataFrame']:
        """
        Détecte les bougies d'incertitude sur une collection.

        Args:
            candles: Liste de dicts OHLC ou DataFrame pandas.

        Returns:
            Liste de résultats (si input=list) ou DataFrame enrichi (si input=DataFrame).
        """
        if PANDAS_AVAILABLE and isinstance(candles, pd.DataFrame):
            return self._detect_batch_vectorized(candles)
        if isinstance(candles, pd.DataFrame):
            self.logger.warning("pandas non dispo → conversion DataFrame→list.")
            candles = candles.to_dict('records')
        return self._detect_batch_standard(candles)

    def _detect_batch_standard(self, candles: List[Dict]) -> List[Dict]:
        """Traitement batch itératif (liste de dicts)."""
        results = []
        for i, candle in enumerate(candles):
            try:
                results.append(self.detect(candle))
            except Exception as e:
                self.logger.error(f"Erreur bougie #{i}: {e}", exc_info=True)
                err = self._make_error_result()
                err['reason'] = f'processing_error: {e}'
                results.append(err)

        if self.enable_batch_statistics:
            self._log_batch_stats(results)
        if self.export_config['auto_export_enabled']:
            self._auto_export(results)
        return results

    def _detect_batch_vectorized(self, df: 'pd.DataFrame') -> 'pd.DataFrame':
        """
        Traitement batch vectorisé pandas. Beaucoup plus rapide pour >1000 bougies.

        Args:
            df: DataFrame avec colonnes 'open', 'high', 'low', 'close'.

        Raises:
            ValueError: Si colonnes OHLC manquantes.
        """
        missing = [c for c in ('open', 'high', 'low', 'close') if c not in df.columns]
        if missing:
            raise ValueError(f"Colonnes manquantes: {missing}")

        self.logger.info(f"Batch vectorisé: {len(df)} bougies...")

        if len(df) == 0:
            empty = df.copy()
            for col in ('range', 'body', 'body_pct', 'upper_wick', 'upper_wick_pct',
                        'lower_wick', 'lower_wick_pct', 'signal_strength'):
                empty[col] = pd.Series(dtype='float64')
            empty['is_uncertainty'] = pd.Series(dtype='bool')
            empty['doji_type']      = pd.Series(dtype='object')
            if self.enable_advanced_metrics:
                for col in ('wick_ratio', 'body_position', 'asymmetry'):
                    empty[col] = pd.Series(dtype='float64')
            return empty

        r = df.copy()
        r['range'] = r['high'] - r['low']
        r['body']  = np.abs(r['close'] - r['open'])

        def pct(col): return np.where(r['range'] > 0, col / r['range'] * 100, 0.0)

        r['body_pct'] = pct(r['body'])
        max_oc = np.maximum(r['open'], r['close'])
        min_oc = np.minimum(r['open'], r['close'])
        r['upper_wick']     = r['high'] - max_oc
        r['lower_wick']     = min_oc - r['low']
        r['upper_wick_pct'] = pct(r['upper_wick'])
        r['lower_wick_pct'] = pct(r['lower_wick'])

        small_body    = r['body_pct']      < self.body_max_pct
        upper_ok      = r['upper_wick_pct'] >= self.wick_min_pct
        lower_ok      = r['lower_wick_pct'] >= self.wick_min_pct
        both_ok       = (upper_ok & lower_ok) if self.require_both_wicks else (upper_ok | lower_ok)
        body_above    = r['body']  >= self.min_candle_body_usdt
        range_below   = r['range'] <= self.max_candle_range_usdt

        r['is_uncertainty'] = small_body & both_ok & body_above & range_below

        if self.enable_advanced_metrics:
            r['wick_ratio'] = np.where(
                r['lower_wick'] > FLOAT_TOLERANCE, r['upper_wick'] / r['lower_wick'],
                np.where(r['upper_wick'] > FLOAT_TOLERANCE, np.inf, 1.0)
            )
            r['body_position'] = np.where(r['range'] > 0, (min_oc - r['low']) / r['range'], 0.5)
            tw = r['upper_wick'] + r['lower_wick']
            r['asymmetry'] = np.where(
                tw > FLOAT_TOLERANCE, np.abs(r['upper_wick'] - r['lower_wick']) / tw, 0.0
            )

        body_score = np.where(small_body, 1.0 - r['body_pct'] / self.body_max_pct, 0.0)
        wick_score = np.minimum(1.0, (r['upper_wick_pct'] + r['lower_wick_pct']) / 2 / 50.0)
        wmin = np.minimum(r['upper_wick_pct'], r['lower_wick_pct'])
        wmax = np.maximum(r['upper_wick_pct'], r['lower_wick_pct'])
        balance_score = np.where(wmax > FLOAT_TOLERANCE, wmin / wmax, 1.0)
        r['signal_strength'] = (0.4 * body_score + 0.4 * wick_score + 0.2 * balance_score).round(4)

        r['doji_type'] = np.where(r['is_uncertainty'], 'standard_uncertainty', 'none')

        if self.enable_batch_statistics:
            n = r['is_uncertainty'].sum()
            self.logger.info(f"Batch: {len(r)} bougies, {n} uncertainty ({n/len(r)*100:.2f}%)")
        if self.export_config['auto_export_enabled']:
            self._auto_export(r)
        return r

    def _log_batch_stats(self, results: List[Dict]):
        """
        Logue les statistiques d'un traitement batch.

        FONCTIONNALITÉ ÉTENDUE (hors scope initial) :
        Utile pour monitoring et optimisation des paramètres.
        """
        if not results:
            return
        total = len(results)
        uncertainty = [r for r in results if r['is_uncertainty']]
        n = len(uncertainty)

        self.logger.info(f"📊 Batch: {total} bougies | {n} uncertainty ({n/total*100:.2f}%)")
        if not n:
            return

        strengths = [r['signal_strength'] for r in uncertainty]
        avg = sum(strengths) / n
        strong   = sum(1 for s in strengths if s >= SIGNAL_STRENGTH_STRONG)
        moderate = sum(1 for s in strengths if SIGNAL_STRENGTH_MODERATE <= s < SIGNAL_STRENGTH_STRONG)
        weak     = sum(1 for s in strengths if s < SIGNAL_STRENGTH_MODERATE)
        self.logger.info(f"   Strength: avg={avg:.2f} | fort={strong} modéré={moderate} faible={weak}")

        doji_types: Dict[str, int] = {}
        for r in uncertainty:
            doji_types[r.get('doji_type', 'unknown')] = doji_types.get(r.get('doji_type', 'unknown'), 0) + 1
        self.logger.info(f"   Types: {', '.join(f'{k}={v}' for k, v in doji_types.items())}")

    def _auto_export(self, results: Union[List[Dict], 'pd.DataFrame']):
        """Export automatique après detect_batch() si activé dans la config."""
        try:
            out_dir = Path(self.export_config['auto_export_dir'])
            out_dir.mkdir(parents=True, exist_ok=True)
            fmt  = self.export_config['default_format']
            # [v2.2.2 — FIX-UC-4] datetime.now(timezone.utc) — était naïf (datetime.now())
            name = f"uncertainty_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.{fmt}"
            path = str(out_dir / name)
            if fmt == 'csv':
                self.export_to_csv(results, path)
            else:
                self.export_to_json(results, path)
            self.logger.info(f"✅ Auto-export: {path}")
        except Exception as e:
            self.logger.error(f"❌ Erreur auto-export: {e}", exc_info=True)

    def export_to_json(self, results: Union[List[Dict], 'pd.DataFrame'],
                       filepath: str, **kwargs):
        """
        Exporte les résultats vers un fichier JSON.

        Args:
            results:   Liste de résultats ou DataFrame.
            filepath:  Chemin de sortie (peut se terminer par .gz pour compression).
            **kwargs:
                indent (int):            Override json.indent depuis config.
                include_metadata (bool): Override json.include_metadata depuis config.
                compress (bool):         Override compression.enabled depuis config.

        Raises:
            ValueError: Si export désactivé.
            IOError:    Si erreur d'écriture fichier.
        """
        if not self.export_enabled:
            raise ValueError("Export désactivé (export.enabled=false dans la config).")

        ec = self.export_config
        indent   = kwargs.get('indent',            ec['json_indent'])
        metadata = kwargs.get('include_metadata',  ec['json_include_metadata'])
        compress = kwargs.get('compress',           ec['compression_enabled'])

        data = results.to_dict('records') if (PANDAS_AVAILABLE and isinstance(results, pd.DataFrame)) else results

        payload = data
        if metadata:
            payload = {
                'metadata': {
                    'timestamp':         datetime.now().isoformat(),
                    'module':            'uncertainty_candle',
                    'version':           _VERSION,  # [v2.2.2 — FIX-UC-3] était '2.2.1' hardcodé
                    'total_candles':     len(data),
                    'uncertainty_count': sum(1 for r in data if r.get('is_uncertainty', False)),
                    'config': {
                        'body_max_pct':      self.body_max_pct,
                        'wick_min_pct':      self.wick_min_pct,
                        'require_both_wicks':self.require_both_wicks,
                    },
                },
                'results': data,
            }

        try:
            Path(filepath).parent.mkdir(parents=True, exist_ok=True)
            content = json.dumps(payload, indent=indent, ensure_ascii=False)
            if compress:
                with gzip.open(filepath, 'wt', encoding='utf-8',
                               compresslevel=ec['compression_level']) as f:
                    f.write(content)
            else:
                with open(filepath, 'w', encoding='utf-8') as f:
                    f.write(content)
            self.logger.info(f"✅ JSON export: {filepath} ({len(data)} records)")
        except Exception as e:
            self.logger.error(f"❌ Erreur export JSON: {e}", exc_info=True)
            raise IOError(f"Export JSON échoué vers {filepath}: {e}")

    def export_to_csv(self, results: Union[List[Dict], 'pd.DataFrame'],
                      filepath: str, **kwargs):
        """
        Exporte les résultats vers un fichier CSV.

        Args:
            results:   Liste de résultats ou DataFrame.
            filepath:  Chemin de sortie.
            **kwargs:
                delimiter (str):      Override csv.delimiter depuis config.
                encoding (str):       Override csv.encoding depuis config.
                flatten_nested (bool):Override csv.flatten_nested depuis config.
                compress (bool):      Override compression.enabled depuis config.

        Raises:
            ValueError: Si export désactivé ou données vides.
            IOError:    Si erreur d'écriture fichier.
        """
        if not self.export_enabled:
            raise ValueError("Export désactivé (export.enabled=false dans la config).")

        ec = self.export_config
        delimiter = kwargs.get('delimiter',      ec['csv_delimiter'])
        encoding  = kwargs.get('encoding',       ec['csv_encoding'])
        flatten   = kwargs.get('flatten_nested', ec['csv_flatten_nested'])
        compress  = kwargs.get('compress',       ec['compression_enabled'])
        quoting   = CSV_QUOTING_MAP.get('minimal', csv.QUOTE_MINIMAL)

        try:
            Path(filepath).parent.mkdir(parents=True, exist_ok=True)

            if PANDAS_AVAILABLE and isinstance(results, pd.DataFrame):
                df = results
                open_fn = (lambda p: gzip.open(p, 'wt', encoding=encoding,
                                               compresslevel=ec['compression_level'])) if compress else None
                if open_fn:
                    with open_fn(filepath) as f:
                        df.to_csv(f, sep=delimiter, index=False, quoting=quoting)
                else:
                    df.to_csv(filepath, sep=delimiter, index=False,
                              encoding=encoding, quoting=quoting)
                count = len(df)
            else:
                if not results:
                    raise ValueError("Aucune donnée à exporter.")
                data = [self._flatten_dict(r) for r in results] if flatten else list(results)
                open_fn = (lambda p: gzip.open(p, 'wt', encoding=encoding, newline='',
                                               compresslevel=ec['compression_level'])) if compress else None
                ctx = open_fn(filepath) if open_fn else open(filepath, 'w', encoding=encoding, newline='')
                with ctx as f:
                    writer = csv.DictWriter(f, fieldnames=list(data[0].keys()),
                                           delimiter=delimiter, quoting=quoting)
                    writer.writeheader()
                    writer.writerows(data)
                count = len(data)

            self.logger.info(f"✅ CSV export: {filepath} ({count} records)")
        except Exception as e:
            self.logger.error(f"❌ Erreur export CSV: {e}", exc_info=True)
            raise IOError(f"Export CSV échoué vers {filepath}: {e}")

    def export_batch_results(self, results: Union[List[Dict], 'pd.DataFrame'],
                             filepath: str, format: Optional[str] = None):
        """
        Exporte en détectant le format depuis l'extension du fichier.

        Args:
            results:  Résultats de détection.
            filepath: Chemin de sortie.
            format:   'json' | 'csv'. Si None, déduit de l'extension.
        """
        if format is None:
            ext = Path(filepath).suffix.lower().lstrip('.')
            format = ext if ext in ('json', 'csv') else self.export_config['default_format']

        if format == 'json':
            self.export_to_json(results, filepath)
        elif format == 'csv':
            self.export_to_csv(results, filepath)
        else:
            raise ValueError(f"Format non supporté: {format}. Utiliser 'json' ou 'csv'.")

    def _flatten_dict(self, d: Dict, parent_key: str = '', sep: str = '_') -> Dict:
        """Aplatit un dict imbriqué. Ex: {'a': {'b': 1}} → {'a_b': 1}."""
        items = []
        for k, v in d.items():
            new_key = f"{parent_key}{sep}{k}" if parent_key else k
            if isinstance(v, dict):
                items.extend(self._flatten_dict(v, new_key, sep).items())
            else:
                items.append((new_key, v))
        return dict(items)

    def get_config(self) -> Dict:
        """Retourne une copie de la configuration active."""
        return self.config.copy()

    def update_config(self, new_config: Dict):
        """
        Met à jour la configuration à chaud avec rollback automatique.

        FONCTIONNALITÉ ÉTENDUE (hors scope initial) :
        Permet de modifier les paramètres sans redémarrer l'indicateur.
        À utiliser avec précaution en production.

        Args:
            new_config: Patch de configuration (partiel ou complet).

        Raises:
            ValueError: Si les nouveaux paramètres sont invalides.
        """
        self.logger.warning("⚠️ Mise à jour dynamique de config (production: utiliser avec précaution).")
        # [v2.2.2 — FIX-UC-5] Snapshot complet avant modification.
        # L'ancienne implémentation ne sauvegardait que self.config. Si
        # _apply_config() échouait pendant le rollback, self.export_config
        # restait dans un état partiellement mis à jour.
        # Correction : snapshot de config + export_config pour rollback atomique.
        old_config        = deepcopy(self.config)
        old_export_config = self.export_config.copy()
        try:
            self.config = self._deep_merge(self.config, new_config)
            self._apply_config()
            self._validate_parameters()
            self.logger.info(f"✅ Config updated: body_max={self.body_max_pct}%, wick_min={self.wick_min_pct}%")
        except Exception as e:
            self.logger.error(f"❌ update_config échoué: {e}. Rollback.")
            self.config        = old_config
            self.export_config = old_export_config
            self._apply_config()
            raise

    def _deep_merge(self, base: Dict, override: Dict) -> Dict:
        """Fusion récursive : override écrase base, les dicts sont fusionnés récursivement."""
        result = base.copy()
        for k, v in override.items():
            if k in result and isinstance(result[k], dict) and isinstance(v, dict):
                result[k] = self._deep_merge(result[k], v)
            else:
                result[k] = v
        return result


# Fonctions utilitaires standalone

def quick_detect(candle: Dict, body_max_pct: float = 33.0, wick_min_pct: float = 20.0) -> bool:
    """
    Détection rapide sans instancier UncertaintyCandleIndicator.

    FONCTION UTILITAIRE (hors scope initial) :
    Convenance pour tests rapides. En production, préférer UncertaintyCandleIndicator.

    Args:
        candle:       Dict OHLC.
        body_max_pct: % max du corps.
        wick_min_pct: % min des mèches.

    Returns:
        True si bougie d'incertitude.
    """
    try:
        o, h, l, c = (float(candle[k]) for k in ('open', 'high', 'low', 'close'))
    except (KeyError, TypeError, ValueError):
        return False

    rng = h - l
    if rng <= 0:
        return True  # perfect doji

    body      = abs(c - o) / rng * 100
    max_oc    = max(o, c)
    min_oc    = min(o, c)
    upper_pct = (h - max_oc) / rng * 100
    lower_pct = (min_oc - l)  / rng * 100

    return body < body_max_pct and upper_pct >= wick_min_pct and lower_pct >= wick_min_pct


def classify_candle_type(candle: Dict) -> str:
    """
    Classifie le type général d'une bougie.

    FONCTION UTILITAIRE (hors scope initial) :
    Helper pour classification rapide sans configuration avancée.

    Args:
        candle: Dict OHLC.

    Returns:
        'uncertainty' | 'strong_bullish' | 'strong_bearish' |
        'normal_bullish' | 'normal_bearish'
    """
    indicator = UncertaintyCandleIndicator()
    result    = indicator.detect(candle)

    if result['is_uncertainty']:
        return 'uncertainty'

    is_bullish = candle['close'] >= candle['open']
    if result['body_pct'] > 70:
        return 'strong_bullish' if is_bullish else 'strong_bearish'
    return 'normal_bullish' if is_bullish else 'normal_bearish'

# FIN DU MODULE