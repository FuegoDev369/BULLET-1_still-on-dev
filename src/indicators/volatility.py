"""
BULLET-1 - Volatility Indicators Module
===============================================
Calcul Bollinger Bands, Keltner Channels, Squeeze detection,
Realized Volatility, Chandelier Exit.

Gestion modes backtest/paper/live depuis config/volatility_config.json.
Pattern identique à atr.py / trend.py / volume.py.

Version : 2.1.2
Author  : FuegoDev
Date    : 2026-03-15
Mode    : ✅ Backtest | ✅ Paper | ✅ Live
"""

import json
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Deque, Dict, Literal, Optional, Tuple
import sys

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Résolution racine projet
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.utils.logger import BulletLogger
from src.utils.helpers import (
    load_and_verify_module_config,
    get_project_root,
)

#: Version du module — utilisée dans get_configuration() pour cohérence automatique.
_VERSION = "2.1.2"  # [v2.1.2 — FIX-VOL-1]

# ---------------------------------------------------------------------------
# Constantes & Enums
# ---------------------------------------------------------------------------

DEFAULT_BB_PERIOD          = 20
DEFAULT_BB_STD             = 2.0
DEFAULT_KC_PERIOD          = 20
DEFAULT_KC_ATR_PERIOD      = 10
DEFAULT_KC_MULTIPLIER      = 1.5
DEFAULT_RV_SHORT_PERIOD    = 5
DEFAULT_RV_LONG_PERIOD     = 20
DEFAULT_CHANDELIER_PERIOD  = 22
DEFAULT_CHANDELIER_MULT    = 3.0
DEFAULT_CACHE_SIZE         = 500
# [v2.1.2 — FIX-VOL-4] ANNUALIZE_FACTOR_CRYPTO supprimée — était définie mais
# jamais utilisée dans le module (code mort). _resolve_annualize_factor() gère
# le facteur d'annualisation avec ses propres valeurs par timeframe.


class BBPriceZone(Enum):
    ABOVE_UPPER  = 'above_upper'
    UPPER_HALF   = 'upper_half'
    MIDDLE       = 'middle'
    LOWER_HALF   = 'lower_half'
    BELOW_LOWER  = 'below_lower'


class VolatilityRegime(Enum):
    EXPANDING   = 'expanding'
    NORMAL      = 'normal'
    COMPRESSING = 'compressing'


@dataclass
class VolatilitySnapshot:
    """Snapshot complet des indicateurs volatilité à l'instant T."""
    bb_upper:               float
    bb_middle:              float
    bb_lower:               float
    bb_bandwidth:           float
    bb_bandwidth_vs_mean:   float
    bb_pct_b:               float
    bb_price_zone:          str
    kc_upper:               float
    kc_middle:              float
    kc_lower:               float
    squeeze_active:         bool
    squeeze_bars_since:     Optional[int]
    squeeze_last_duration:  Optional[int]
    rv_short_annualized:    float
    rv_long_annualized:     float
    rv_ratio:               float
    rv_regime:              str
    chandelier_long:        float
    chandelier_short:       float


# ---------------------------------------------------------------------------
# Fonctions mathématiques pures
# ---------------------------------------------------------------------------

def _calc_atr_simple(high: pd.Series, low: pd.Series,
                     close: pd.Series, period: int) -> pd.Series:
    """ATR via EMA (utilisé en interne pour les Keltner Channels)."""
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs()
    ], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False, min_periods=period).mean()


def _calc_bollinger_bands(close: pd.Series, period: int,
                           std_dev: float) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """
    Bollinger Bands.
    Middle = SMA(period)
    Upper  = Middle + std_dev × StdDev(period)
    Lower  = Middle − std_dev × StdDev(period)
    """
    middle = close.rolling(period, min_periods=period).mean()
    std    = close.rolling(period, min_periods=period).std(ddof=1)
    upper  = middle + std_dev * std
    lower  = middle - std_dev * std
    return upper, middle, lower


def _calc_keltner_channels(close: pd.Series, high: pd.Series, low: pd.Series,
                            kc_period: int, atr_period: int,
                            multiplier: float) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """
    Keltner Channels.
    Middle = EMA(kc_period)
    Upper  = Middle + multiplier × ATR(atr_period)
    Lower  = Middle − multiplier × ATR(atr_period)
    """
    middle = close.ewm(span=kc_period, adjust=False, min_periods=kc_period).mean()
    atr    = _calc_atr_simple(high, low, close, atr_period)
    upper  = middle + multiplier * atr
    lower  = middle - multiplier * atr
    return upper, middle, lower


def _calc_squeeze(bb_upper: pd.Series, bb_lower: pd.Series,
                  kc_upper: pd.Series, kc_lower: pd.Series) -> pd.Series:
    """
    Squeeze : BB entièrement à l'intérieur des KC.
    True = compression de volatilité.
    """
    return (bb_upper < kc_upper) & (bb_lower > kc_lower)


def _calc_realized_volatility(close: pd.Series, period: int,
                                annualize_factor: int) -> pd.Series:
    """
    Volatilité réalisée (déviation standard des log-rendements) annualisée.
    RV = StdDev(log_returns, period) × √(annualize_factor)
    """
    log_ret = np.log(close / close.shift(1))
    rv = log_ret.rolling(period, min_periods=period).std(ddof=1) * np.sqrt(annualize_factor)
    return rv.fillna(0.0)


def _calc_chandelier_exit(high: pd.Series, low: pd.Series,
                           atr: pd.Series, period: int,
                           multiplier: float) -> Tuple[pd.Series, pd.Series]:
    """
    Chandelier Exit.
    Long  stop = Highest_High(period) − multiplier × ATR
    Short stop = Lowest_Low(period)   + multiplier × ATR
    """
    highest_high = high.rolling(period, min_periods=period).max()
    lowest_low   = low.rolling(period,  min_periods=period).min()
    long_stop    = highest_high - multiplier * atr
    short_stop   = lowest_low   + multiplier * atr
    return long_stop, short_stop


# ---------------------------------------------------------------------------
# Interprétations qualitatives
# ---------------------------------------------------------------------------

def _bb_price_zone(pct_b: float) -> str:
    """Localiser le prix dans les Bollinger Bands via %B."""
    if pct_b > 1.0:  return BBPriceZone.ABOVE_UPPER.value
    if pct_b > 0.5:  return BBPriceZone.UPPER_HALF.value
    if pct_b > 0.45: return BBPriceZone.MIDDLE.value
    if pct_b > 0.0:  return BBPriceZone.LOWER_HALF.value
    return BBPriceZone.BELOW_LOWER.value


def _rv_regime(rv_ratio: float) -> str:
    """Régime de volatilité depuis le ratio RV_court / RV_long."""
    if rv_ratio > 1.15: return VolatilityRegime.EXPANDING.value
    if rv_ratio < 0.85: return VolatilityRegime.COMPRESSING.value
    return VolatilityRegime.NORMAL.value


def _bb_bandwidth_vs_mean(bandwidth: pd.Series) -> pd.Series:
    """Ratio bandwidth / moyenne rolling(20) — mesure relative de l'expansion."""
    mean_bw = bandwidth.rolling(20, min_periods=5).mean().replace(0, np.nan)
    return (bandwidth / mean_bw).fillna(1.0)


def _calc_squeeze_bars_since_release(squeeze: pd.Series) -> pd.Series:
    """
    Pour chaque bougie, nombre de bougies depuis la fin du dernier squeeze.
    Returns NaN si actuellement en squeeze ou aucun squeeze précédent.

    [v2.1.2 — FIX-VOL-2] Vectorisé via cumsum pandas — remplace la boucle
    Python pure O(n). Logique :
      - Chaque front descendant (squeeze → non-squeeze) démarre un compteur.
      - Les bougies en squeeze restent NaN.
    """
    # Identifier les fins de squeeze : passage de True→False
    was_squeeze = squeeze.shift(1).fillna(False)
    end_of_squeeze = (~squeeze) & was_squeeze

    # Numéro de groupe : incrémente à chaque fin de squeeze
    group = end_of_squeeze.cumsum()

    # Position dans le groupe courant (0-based depuis le début du non-squeeze)
    cumcount = group.groupby(group).cumcount()

    # Masquer les bougies en squeeze et les bougies sans squeeze précédent
    has_prior_squeeze = group > 0
    result = pd.Series(np.nan, index=squeeze.index)
    mask = (~squeeze) & has_prior_squeeze
    result[mask] = cumcount[mask]
    return result


def _calc_squeeze_last_duration(squeeze: pd.Series) -> pd.Series:
    """
    Pour chaque bougie (après un squeeze), durée en bougies du dernier squeeze terminé.

    [v2.1.2 — FIX-VOL-2] Vectorisé via cumsum/groupby pandas — remplace la boucle
    Python pure O(n). Logique :
      - Chaque front montant (non-squeeze → squeeze) démarre un groupe de squeeze.
      - On calcule la longueur de chaque groupe de squeeze.
      - Après la fin du squeeze, on propage cette durée jusqu'au prochain squeeze.
    """
    # Identifier les groupes de squeeze (chaque passage à True démarre un nouveau groupe)
    was_not_squeeze = (~squeeze).shift(1).fillna(True)
    start_of_squeeze = squeeze & was_not_squeeze
    squeeze_group = start_of_squeeze.cumsum()

    # Durée de chaque groupe de squeeze
    squeeze_sizes = squeeze.groupby(squeeze_group).transform('sum')

    # La durée ne s'applique qu'en dehors des squeezes et si un squeeze s'est terminé
    result = pd.Series(np.nan, index=squeeze.index)
    # Propager la durée du dernier squeeze terminé hors squeeze
    last_duration = squeeze_sizes.where(~squeeze).ffill()
    # Masquer si on n'a jamais connu de squeeze (squeeze_group == 0 et squeeze == False)
    has_prior = squeeze_group > 0
    valid_mask = (~squeeze) & has_prior
    result[valid_mask] = last_duration[valid_mask]
    return result


# ---------------------------------------------------------------------------
# Classe principale
# ---------------------------------------------------------------------------

class VolatilityIndicator:
    """
    Indicateurs de volatilité pour BULLET-1.

    Indicateurs calculés :
      • Bollinger Bands (upper, middle, lower, bandwidth, %B, zone)
      • Keltner Channels (upper, middle, lower)
      • Squeeze (BB inside KC + durée + bars depuis release)
      • Realized Volatility court / long terme + ratio + régime
      • Chandelier Exit (long stop, short stop)

    BACKTEST  : add_volatility_indicators(df) → DataFrame enrichi.
    PAPER/LIVE: update_incremental(candle)    → dict résultat O(1).
    SNAPSHOT  : get_snapshot(row)             → dict JSON-ready.
    """

    def __init__(self, config: dict, config_path: Optional[Path] = None) -> None:
        """
        Initialiser VolatilityIndicator.

        Args:
            config      : Configuration principale BULLET-1 (dict).
            config_path : Chemin volatility_config.json (auto-détecté si None).

        Raises:
            TypeError         : config n'est pas un dict.
            FileNotFoundError : volatility_config.json introuvable.
            ValueError        : modes incohérents ou paramètres invalides.
        """
        if not isinstance(config, dict):
            raise TypeError(
                f"config doit être dict, reçu : {type(config).__name__}"
            )

        self.logger = BulletLogger()
        self.logger.info("Initializing VolatilityIndicator...")

        self._main_config = config

        # Chargement volatility_config.json
        # Chargement + vérification cohérence du mode (helpers.py v2.4.0)
        self._vol_config, self.mode = load_and_verify_module_config(
            module_config_path = get_project_root() / 'config' / 'volatility_config.json',
            module_name        = 'volatility',
            main_config        = config,
            logger             = self.logger
        )

        # Paramètres
        p = self._vol_config.get('parameters', {})
        self.bb_period          = int(p.get('bb_period',          DEFAULT_BB_PERIOD))
        self.bb_std             = float(p.get('bb_std_dev',        DEFAULT_BB_STD))
        self.kc_period          = int(p.get('kc_period',          DEFAULT_KC_PERIOD))
        self.kc_atr_period      = int(p.get('kc_atr_period',      DEFAULT_KC_ATR_PERIOD))
        self.kc_multiplier      = float(p.get('kc_multiplier',    DEFAULT_KC_MULTIPLIER))
        self.rv_short_period    = int(p.get('rv_short_period',    DEFAULT_RV_SHORT_PERIOD))
        self.rv_long_period     = int(p.get('rv_long_period',     DEFAULT_RV_LONG_PERIOD))
        self.chandelier_period  = int(p.get('chandelier_period',  DEFAULT_CHANDELIER_PERIOD))
        self.chandelier_mult    = float(p.get('chandelier_multiplier', DEFAULT_CHANDELIER_MULT))

        # Facteur d'annualisation adapté à la timeframe (configurable)
        raw_tf = config.get('general', {}).get('timeframe', '1h')
        self.annualize_factor   = self._resolve_annualize_factor(
            p.get('annualize_factor'), raw_tf, self.logger
        )

        self._validate_params()

        self.min_candles = max(
            self.bb_period,
            self.kc_period + self.kc_atr_period,
            self.rv_long_period,
            self.chandelier_period
        )

        # Cache PAPER/LIVE
        if self.mode in ('paper', 'live'):
            self._init_incremental_cache()
        else:
            self._cache: Optional[Dict] = None

        self.logger.info(
            f"✅ VolatilityIndicator ready: mode={self.mode}, "
            f"bb={self.bb_period}/{self.bb_std}σ, "
            f"kc={self.kc_period}/mult={self.kc_multiplier}, "
            f"rv={self.rv_short_period}/{self.rv_long_period}, "
            f"chandelier={self.chandelier_period}/{self.chandelier_mult}"
        )

    # -----------------------------------------------------------------------
    # Résolution du facteur d'annualisation selon la timeframe
    # -----------------------------------------------------------------------

    @staticmethod
    def _resolve_annualize_factor(config_value: Optional[int], timeframe: str,
                                   logger: Optional[Any] = None) -> int:
        """
        Retourner le facteur d'annualisation adapté à la timeframe.
        Crypto : marché 24h/7j/365j.

        Timeframes supportées : '1m','3m','5m','15m','30m','1h','2h','4h','6h','12h','1d'

        [v2.1.2 — FIX-VOL-3] WARNING ajouté si timeframe non reconnu.
        Un timeframe mal formaté (ex: '240m', '4H') ne lèvait aucune alerte
        et retournait silencieusement 8760 (facteur horaire) — RV incorrecte.
        """
        if config_value is not None:
            return int(config_value)

        tf_map = {
            '1m':  525600,
            '3m':  175200,
            '5m':  105120,
            '15m':  35040,
            '30m':  17520,
            '1h':    8760,
            '2h':    4380,
            '4h':    2190,
            '6h':    1460,
            '12h':    730,
            '1d':     365,
        }
        factor = tf_map.get(timeframe.lower())
        if factor is None:
            # [v2.1.2 — FIX-VOL-3] Fallback avec warning — timeframe non reconnu.
            if logger is not None:
                logger.warning(
                    f"⚠️  [FIX-VOL-3] annualize_factor : timeframe '{timeframe}' non reconnu "
                    f"dans le mapping standard. Fallback sur 8760 (1h). "
                    f"Timeframes supportés : {list(tf_map.keys())}. "
                    f"Configurer 'annualize_factor' explicitement dans volatility_config.json."
                )
            return 8760
        return factor

    # -----------------------------------------------------------------------
    # Validation
    # -----------------------------------------------------------------------

    def _validate_params(self) -> None:
        checks = [
            (self.bb_period        >= 2,                        "bb_period doit être >= 2"),
            (self.bb_std           > 0,                         "bb_std_dev doit être > 0"),
            (self.kc_period        >= 2,                        "kc_period doit être >= 2"),
            (self.kc_atr_period    >= 2,                        "kc_atr_period doit être >= 2"),
            (self.kc_multiplier    > 0,                         "kc_multiplier doit être > 0"),
            (self.rv_short_period  >= 2,                        "rv_short_period doit être >= 2"),
            (self.rv_long_period   > self.rv_short_period,      "rv_long_period doit être > rv_short_period"),
            (self.chandelier_period >= 2,                       "chandelier_period doit être >= 2"),
            (self.chandelier_mult  > 0,                         "chandelier_multiplier doit être > 0"),
            (self.annualize_factor > 0,                         "annualize_factor doit être > 0"),
        ]
        for ok, msg in checks:
            if not ok:
                raise ValueError(msg)

    # -----------------------------------------------------------------------
    # Cache incrémental — PAPER / LIVE
    # -----------------------------------------------------------------------

    def _init_incremental_cache(self) -> None:
        size = self._vol_config.get('cache', {}).get('buffer_size', DEFAULT_CACHE_SIZE)
        self._cache = {
            'close':     deque(maxlen=size),
            'high':      deque(maxlen=size),
            'low':       deque(maxlen=size),
            'size':      size,
            'warmed_up': False,
        }
        self.logger.debug(f"Incremental cache initialized: buffer_size={size}")

    def initialize_warmup(self, historical_df: pd.DataFrame) -> None:
        """
        Initialiser le cache avec données historiques (PAPER/LIVE).

        Args:
            historical_df : DataFrame avec colonnes ['high','low','close'].

        Raises:
            RuntimeError : appelé en BACKTEST.
            ValueError   : colonnes manquantes ou données insuffisantes.
        """
        if self.mode == 'backtest':
            raise RuntimeError(
                "initialize_warmup() interdit en BACKTEST. Utiliser add_volatility_indicators()."
            )
        self._validate_hlc(historical_df)
        if len(historical_df) < self.min_candles:
            raise ValueError(
                f"Données insuffisantes : {len(historical_df)} bougies, "
                f"minimum : {self.min_candles}"
            )

        tail = historical_df.tail(self._cache['size'])
        for col in ('close', 'high', 'low'):
            self._cache[col].clear()
            for v in tail[col]:
                self._cache[col].append(float(v))
        self._cache['warmed_up'] = True
        self.logger.info(f"Warmup OK : {len(tail)} bougies chargées.")

    def update_incremental(self, candle: dict) -> dict:
        """
        Mettre à jour le cache et calculer tous les indicateurs volatilité (PAPER/LIVE).

        Args:
            candle : Dict avec clés 'high', 'low', 'close'.

        Returns:
            dict : indicateurs volatilité pour cette bougie.

        Raises:
            RuntimeError : appelé en BACKTEST.
            ValueError   : clés manquantes ou warmup non effectué.
        """
        if self.mode == 'backtest':
            raise RuntimeError(
                "update_incremental() interdit en BACKTEST. Utiliser add_volatility_indicators()."
            )
        if not self._cache.get('warmed_up'):
            raise ValueError("Warmup non effectué. Appeler initialize_warmup() d'abord.")

        start = time.perf_counter()

        missing = [k for k in ('high', 'low', 'close') if k not in candle]
        if missing:
            raise ValueError(f"Clés manquantes dans candle : {missing}")

        for col in ('close', 'high', 'low'):
            self._cache[col].append(float(candle[col]))

        df_tmp = pd.DataFrame({
            'close': list(self._cache['close']),
            'high':  list(self._cache['high']),
            'low':   list(self._cache['low']),
        })
        df_result = self.add_volatility_indicators(df_tmp)
        row = df_result.iloc[-1]

        elapsed = (time.perf_counter() - start) * 1000
        if elapsed > 50:
            self.logger.warning(f"update_incremental lent : {elapsed:.1f}ms")

        return self.get_snapshot(row)

    # -----------------------------------------------------------------------
    # Calcul batch — BACKTEST
    # -----------------------------------------------------------------------

    def add_volatility_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Ajouter tous les indicateurs volatilité au DataFrame (vectorisé, batch).

        Colonnes ajoutées :
          vol_bb_upper, vol_bb_middle, vol_bb_lower,
          vol_bb_bandwidth, vol_bb_bandwidth_vs_mean, vol_bb_pct_b, vol_bb_zone
          vol_kc_upper, vol_kc_middle, vol_kc_lower
          vol_squeeze, vol_squeeze_bars_since, vol_squeeze_last_duration
          vol_rv_short, vol_rv_long, vol_rv_ratio, vol_rv_regime
          vol_chandelier_long, vol_chandelier_short

        Args:
            df : DataFrame avec colonnes ['high', 'low', 'close'].

        Returns:
            DataFrame enrichi (copie).

        Raises:
            ValueError : colonnes manquantes ou DataFrame vide.
        """
        self._validate_hlc(df)
        if len(df) < self.min_candles:
            self.logger.warning(
                f"Données potentiellement insuffisantes : {len(df)} bougies, "
                f"recommandé >= {self.min_candles}."
            )

        df     = df.copy()
        close  = df['close']
        high   = df['high']
        low    = df['low']

        # ---- Bollinger Bands ----
        bb_upper, bb_middle, bb_lower = _calc_bollinger_bands(close, self.bb_period, self.bb_std)
        df['vol_bb_upper']  = bb_upper.round(4)
        df['vol_bb_middle'] = bb_middle.round(4)
        df['vol_bb_lower']  = bb_lower.round(4)

        # Bandwidth = (Upper − Lower) / Middle
        denom_mid = bb_middle.replace(0, np.nan)
        bandwidth = (bb_upper - bb_lower) / denom_mid
        df['vol_bb_bandwidth']         = bandwidth.round(6)
        df['vol_bb_bandwidth_vs_mean'] = _bb_bandwidth_vs_mean(bandwidth).round(4)

        # %B = (Close − Lower) / (Upper − Lower)
        bb_range = (bb_upper - bb_lower).replace(0, np.nan)
        pct_b    = (close - bb_lower) / bb_range
        df['vol_bb_pct_b'] = pct_b.round(4)
        df['vol_bb_zone']  = pct_b.apply(_bb_price_zone)

        # ---- Keltner Channels ----
        kc_upper, kc_middle, kc_lower = _calc_keltner_channels(
            close, high, low, self.kc_period, self.kc_atr_period, self.kc_multiplier
        )
        df['vol_kc_upper']  = kc_upper.round(4)
        df['vol_kc_middle'] = kc_middle.round(4)
        df['vol_kc_lower']  = kc_lower.round(4)

        # ---- Squeeze ----
        squeeze = _calc_squeeze(bb_upper, bb_lower, kc_upper, kc_lower)
        df['vol_squeeze']              = squeeze
        df['vol_squeeze_bars_since']   = _calc_squeeze_bars_since_release(squeeze)
        df['vol_squeeze_last_duration'] = _calc_squeeze_last_duration(squeeze)

        # ---- Realized Volatility ----
        rv_short = _calc_realized_volatility(close, self.rv_short_period, self.annualize_factor)
        rv_long  = _calc_realized_volatility(close, self.rv_long_period,  self.annualize_factor)
        df['vol_rv_short'] = rv_short.round(6)
        df['vol_rv_long']  = rv_long.round(6)

        rv_ratio = (rv_short / rv_long.replace(0, np.nan)).fillna(1.0)
        df['vol_rv_ratio']  = rv_ratio.round(4)
        df['vol_rv_regime'] = rv_ratio.apply(_rv_regime)

        # ---- Chandelier Exit ----
        # Utilise l'ATR interne (même période que KC_ATR pour cohérence)
        atr_internal = _calc_atr_simple(high, low, close, self.chandelier_period)
        chan_long, chan_short = _calc_chandelier_exit(
            high, low, atr_internal, self.chandelier_period, self.chandelier_mult
        )
        df['vol_chandelier_long']  = chan_long.round(4)
        df['vol_chandelier_short'] = chan_short.round(4)

        self.logger.debug(f"add_volatility_indicators: {len(df)} bougies traitées.")
        return df

    # -----------------------------------------------------------------------
    # Snapshot JSON
    # -----------------------------------------------------------------------

    def get_snapshot(self, row: pd.Series) -> Dict[str, Any]:
        """
        Retourner le dict indicators['volatility'] pour le rapport de trade.

        Args:
            row : ligne du DataFrame à l'instant T, issue de add_volatility_indicators().

        Returns:
            dict : section 'volatility' de indicators.

        Raises:
            ValueError : colonnes volatilité absentes.
        """
        required_cols = ['vol_bb_upper', 'vol_kc_upper', 'vol_squeeze', 'vol_rv_short']
        missing = [c for c in required_cols if c not in row.index]
        if missing:
            raise ValueError(
                f"Colonnes volatilité absentes : {missing}. "
                "Appeler add_volatility_indicators() d'abord."
            )

        def _safe(val, decimals: int = 4) -> Optional[float]:
            if val is None or (isinstance(val, float) and np.isnan(val)):
                return None
            return round(float(val), decimals)

        def _safe_int(val) -> Optional[int]:
            if val is None or (isinstance(val, float) and np.isnan(val)):
                return None
            return int(val)

        rv_ratio = _safe(row.get('vol_rv_ratio'), 4) or 1.0

        return {
            "_source": "volatility.py / VolatilityIndicator",

            "bollinger_bands": {
                "period":                self.bb_period,
                "std_dev":               self.bb_std,
                "upper":                 _safe(row['vol_bb_upper'],  2),
                "middle":                _safe(row['vol_bb_middle'], 2),
                "lower":                 _safe(row['vol_bb_lower'],  2),
                "bandwidth":             _safe(row.get('vol_bb_bandwidth'), 6),
                "bandwidth_vs_mean":     _safe(row.get('vol_bb_bandwidth_vs_mean'), 4),
                "pct_b":                 _safe(row.get('vol_bb_pct_b'), 4),
                "price_zone":            str(row.get('vol_bb_zone', BBPriceZone.MIDDLE.value)),
            },

            "keltner_channels": {
                "period":       self.kc_period,
                "atr_period":   self.kc_atr_period,
                "multiplier":   self.kc_multiplier,
                "upper":        _safe(row['vol_kc_upper'],  2),
                "middle":       _safe(row['vol_kc_middle'], 2),
                "lower":        _safe(row['vol_kc_lower'],  2),
            },

            "squeeze": {
                "active":               bool(row.get('vol_squeeze', False)),
                "bars_since_release":   _safe_int(row.get('vol_squeeze_bars_since')),
                "last_squeeze_duration_bars": _safe_int(row.get('vol_squeeze_last_duration')),
            },

            "realized_volatility": {
                "short_period":          self.rv_short_period,
                "long_period":           self.rv_long_period,
                "annualize_factor":      self.annualize_factor,
                "rv_short_annualized_pct": round(_safe(row['vol_rv_short'], 6) * 100, 2)
                                           if _safe(row['vol_rv_short']) else None,
                "rv_long_annualized_pct":  round(_safe(row['vol_rv_long'], 6) * 100, 2)
                                           if _safe(row['vol_rv_long']) else None,
                "rv_ratio":              rv_ratio,
                "regime":                str(row.get('vol_rv_regime', VolatilityRegime.NORMAL.value)),
            },

            "chandelier_exit": {
                "period":      self.chandelier_period,
                "multiplier":  self.chandelier_mult,
                "long_stop":   _safe(row.get('vol_chandelier_long'),  2),
                "short_stop":  _safe(row.get('vol_chandelier_short'), 2),
            },
        }

    # -----------------------------------------------------------------------
    # Helpers privés
    # -----------------------------------------------------------------------

    def _validate_hlc(self, df: pd.DataFrame) -> None:
        required = ['high', 'low', 'close']
        missing  = [c for c in required if c not in df.columns]
        if missing:
            raise ValueError(f"Colonnes HLC manquantes : {missing}")
        if df.empty:
            raise ValueError("DataFrame vide.")

    def get_configuration(self) -> Dict[str, Any]:
        """Retourner la configuration active du module."""
        return {
            'version':            _VERSION,  # [v2.1.2 — FIX-VOL-1] était '2.1.1' hardcodé
            'mode':               self.mode,
            'bb_period':          self.bb_period,
            'bb_std_dev':         self.bb_std,
            'kc_period':          self.kc_period,
            'kc_atr_period':      self.kc_atr_period,
            'kc_multiplier':      self.kc_multiplier,
            'rv_short_period':    self.rv_short_period,
            'rv_long_period':     self.rv_long_period,
            'annualize_factor':   self.annualize_factor,
            'chandelier_period':  self.chandelier_period,
            'chandelier_mult':    self.chandelier_mult,
            'min_candles_required': self.min_candles,
        }

    def __repr__(self) -> str:
        return (
            f"VolatilityIndicator(mode={self.mode}, "
            f"bb={self.bb_period}/{self.bb_std}σ, "
            f"kc={self.kc_period}/{self.kc_multiplier}, "
            f"rv={self.rv_short_period}/{self.rv_long_period})"
        )


# ---------------------------------------------------------------------------
# Fonctions utilitaires standalone
# ---------------------------------------------------------------------------

def calculate_bollinger_simple(close: pd.Series, period: int = 20,
                                std_dev: float = 2.0
                                ) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """Calculer Bollinger Bands sans instancier VolatilityIndicator."""
    return _calc_bollinger_bands(close, period, std_dev)


def calculate_realized_volatility_simple(close: pd.Series, period: int = 20,
                                          annualize_factor: int = 8760) -> pd.Series:
    """Calculer la volatilité réalisée sans instancier VolatilityIndicator."""
    return _calc_realized_volatility(close, period, annualize_factor)


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

__all__ = [
    'VolatilityIndicator',
    'VolatilitySnapshot',
    'BBPriceZone',
    'VolatilityRegime',
    'calculate_bollinger_simple',
    'calculate_realized_volatility_simple',
]

# FIN DU MODULE