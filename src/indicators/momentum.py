"""
BULLET-1 - Momentum Indicators Module
=============================================
Calcul RSI, MACD, ROC z-scored, Stochastic RSI, Williams %R,
CMF, MFI, OBV avec détection de divergences.

Gestion modes backtest/paper/live depuis config/momentum_config.json.
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
_VERSION = "2.1.2"  # [v2.1.2 — FIX-MOM-2]

# ---------------------------------------------------------------------------
# Constantes & Enums
# ---------------------------------------------------------------------------

DEFAULT_RSI_PERIOD          = 14
DEFAULT_MACD_FAST           = 12
DEFAULT_MACD_SLOW           = 26
DEFAULT_MACD_SIGNAL         = 9
DEFAULT_ROC_PERIOD          = 14
DEFAULT_ROC_ZSCORE_PERIOD   = 50
DEFAULT_STOCH_RSI_PERIOD    = 14
DEFAULT_STOCH_RSI_SMOOTH_K  = 3
DEFAULT_STOCH_RSI_SMOOTH_D  = 3
DEFAULT_WILLIAMS_PERIOD     = 14
DEFAULT_CMF_PERIOD          = 20
DEFAULT_MFI_PERIOD          = 14
DEFAULT_OBV_SLOPE_PERIOD    = 20
DEFAULT_DIVERGENCE_LOOKBACK = 30
DEFAULT_CACHE_SIZE          = 500


class MomentumZone(Enum):
    OVERBOUGHT   = 'overbought'
    UPPER        = 'upper'
    NEUTRAL      = 'neutral'
    LOWER        = 'lower'
    OVERSOLD     = 'oversold'


class OBVSlope(Enum):
    RISING   = 'rising'
    FALLING  = 'falling'
    FLAT     = 'flat'


class CMFSignal(Enum):
    STRONG_BUYING  = 'strong_buying'
    MODERATE_BUYING = 'moderate_buying'
    NEUTRAL        = 'neutral'
    MODERATE_SELLING = 'moderate_selling'
    STRONG_SELLING = 'strong_selling'


@dataclass
class MomentumSnapshot:
    """Snapshot complet de tous les indicateurs momentum à l'instant T."""
    rsi_value:             float
    rsi_zone:              str
    rsi_divergence_bull:   bool
    rsi_divergence_bear:   bool
    macd_line:             float
    macd_signal:           float
    macd_histogram:        float
    macd_histogram_expanding: bool
    macd_zero_cross_recent: bool
    macd_bars_since_cross:  Optional[int]
    macd_divergence_bull:  bool
    macd_divergence_bear:  bool
    roc_raw_pct:           float
    roc_zscore:            float
    stoch_k:               float
    stoch_d:               float
    stoch_zone:            str
    stoch_cross_up_recent: bool
    stoch_cross_down_recent: bool
    williams_r:            float
    williams_zone:         str
    cmf_value:             float
    cmf_signal:            str
    mfi_value:             float
    mfi_zone:              str
    mfi_divergence_bull:   bool
    mfi_divergence_bear:   bool
    obv_value:             float
    obv_slope:             str
    obv_divergence_bull:   bool
    obv_divergence_bear:   bool


# ---------------------------------------------------------------------------
# Fonctions mathématiques pures — utilisées en batch ET incrémental
# ---------------------------------------------------------------------------

def _ema_series(series: pd.Series, span: int) -> pd.Series:
    """EMA vectorisée (pandas ewm)."""
    return series.ewm(span=span, adjust=False, min_periods=span).mean()


def _rma_series(series: pd.Series, period: int) -> pd.Series:
    """RMA (Wilder's smoothing) vectorisée."""
    return series.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def _calc_rsi(close: pd.Series, period: int) -> pd.Series:
    """
    RSI via Wilder's smoothing (RMA).
    RSI = 100 − 100 / (1 + RS),  RS = RMA(gains) / RMA(losses)
    """
    delta = close.diff()
    gain  = delta.clip(lower=0.0)
    loss  = (-delta).clip(lower=0.0)
    avg_gain = _rma_series(gain, period)
    avg_loss = _rma_series(loss, period)
    rs  = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi.fillna(50.0)


def _calc_macd(close: pd.Series, fast: int, slow: int, signal: int
               ) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """
    MACD line, signal line, histogram.
    MACD   = EMA(fast) − EMA(slow)
    Signal = EMA(MACD, signal_period)
    Histo  = MACD − Signal
    """
    ema_fast   = close.ewm(span=fast,   adjust=False, min_periods=fast).mean()
    ema_slow   = close.ewm(span=slow,   adjust=False, min_periods=slow).mean()
    macd_line  = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    histogram  = macd_line - signal_line
    return macd_line, signal_line, histogram


def _calc_stoch_rsi(rsi: pd.Series, period: int, smooth_k: int, smooth_d: int
                    ) -> Tuple[pd.Series, pd.Series]:
    """
    Stochastic RSI.
    Raw_K = (RSI − min(RSI, N)) / (max(RSI, N) − min(RSI, N))
    %K    = SMA(Raw_K, smooth_k)
    %D    = SMA(%K, smooth_d)
    """
    low_rsi  = rsi.rolling(period, min_periods=period).min()
    high_rsi = rsi.rolling(period, min_periods=period).max()
    denom    = (high_rsi - low_rsi).replace(0, np.nan)
    raw_k    = (rsi - low_rsi) / denom
    pct_k    = raw_k.rolling(smooth_k, min_periods=smooth_k).mean()
    pct_d    = pct_k.rolling(smooth_d, min_periods=smooth_d).mean()
    return (pct_k * 100).fillna(50.0), (pct_d * 100).fillna(50.0)


def _calc_williams_r(high: pd.Series, low: pd.Series, close: pd.Series,
                     period: int) -> pd.Series:
    """
    Williams %R = (Highest_High − Close) / (Highest_High − Lowest_Low) × −100
    Plage : [−100, 0].  Survendu < −80, suracheté > −20.
    """
    highest = high.rolling(period, min_periods=period).max()
    lowest  = low.rolling(period, min_periods=period).min()
    denom   = (highest - lowest).replace(0, np.nan)
    wr      = ((highest - close) / denom) * -100.0
    return wr.fillna(-50.0)


def _calc_cmf(high: pd.Series, low: pd.Series, close: pd.Series,
              volume: pd.Series, period: int) -> pd.Series:
    """
    Chaikin Money Flow.
    MFM  = [(Close − Low) − (High − Close)] / (High − Low)
    MFV  = MFM × Volume
    CMF  = Σ(MFV, N) / Σ(Volume, N)
    Plage : [−1, +1].
    """
    hl_range = (high - low).replace(0, np.nan)
    mfm  = ((close - low) - (high - close)) / hl_range
    mfv  = mfm * volume
    sum_mfv = mfv.rolling(period, min_periods=period).sum()
    sum_vol = volume.rolling(period, min_periods=period).sum().replace(0, np.nan)
    return (sum_mfv / sum_vol).fillna(0.0)


def _calc_mfi(high: pd.Series, low: pd.Series, close: pd.Series,
              volume: pd.Series, period: int) -> pd.Series:
    """
    Money Flow Index (RSI pondéré par le volume).
    TP  = (H + L + C) / 3
    MF  = TP × Volume
    MFR = Σ(positive MF, N) / Σ(negative MF, N)
    MFI = 100 − 100 / (1 + MFR)
    """
    tp    = (high + low + close) / 3.0
    mf    = tp * volume
    tp_diff = tp.diff()
    pos_mf = mf.where(tp_diff > 0, 0.0)
    neg_mf = mf.where(tp_diff < 0, 0.0).abs()
    sum_pos = pos_mf.rolling(period, min_periods=period).sum()
    sum_neg = neg_mf.rolling(period, min_periods=period).sum().replace(0, np.nan)
    mfr    = sum_pos / sum_neg
    return (100.0 - 100.0 / (1.0 + mfr)).fillna(50.0)


def _calc_obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    """
    On-Balance Volume.
    OBV[t] = OBV[t−1] + Volume  si Close > Close_prev
    OBV[t] = OBV[t−1] − Volume  si Close < Close_prev
    OBV[t] = OBV[t−1]           si Close == Close_prev
    """
    sign = np.sign(close.diff()).fillna(0.0)
    return (sign * volume).cumsum()


def _calc_roc_zscore(close: pd.Series, roc_period: int,
                     zscore_period: int) -> Tuple[pd.Series, pd.Series]:
    """
    ROC brut (%) + z-score normalisé sur fenêtre glissante.
    ROC = (Close[t] − Close[t−N]) / Close[t−N] × 100
    Z   = (ROC − EMA(ROC, M)) / StdDev(ROC, M)
    """
    roc = close.pct_change(roc_period) * 100.0
    mu  = roc.rolling(zscore_period, min_periods=zscore_period).mean()
    std = roc.rolling(zscore_period, min_periods=zscore_period).std().replace(0, np.nan)
    zscore = ((roc - mu) / std).fillna(0.0)
    return roc.fillna(0.0), zscore


# ---------------------------------------------------------------------------
# Détection de divergences (batch)
# ---------------------------------------------------------------------------

def _detect_divergences(price: pd.Series, indicator: pd.Series,
                         lookback: int) -> Tuple[pd.Series, pd.Series]:
    """
    Détecter divergences haussières/baissières entre prix et indicateur.

    Divergence haussière : prix fait Lower Low, indicateur fait Higher Low.
    Divergence baissière : prix fait Higher High, indicateur fait Lower High.

    Returns:
        bull_div, bear_div : deux pd.Series bool
    """
    bull = pd.Series(False, index=price.index)
    bear = pd.Series(False, index=price.index)

    for i in range(lookback, len(price)):
        window_price = price.iloc[i - lookback: i + 1]
        window_ind   = indicator.iloc[i - lookback: i + 1]

        if window_price.isna().any() or window_ind.isna().any():
            continue

        price_cur, price_prev = window_price.iloc[-1], window_price.min()
        ind_cur,   ind_prev   = window_ind.iloc[-1],   window_ind.iloc[window_price.values.argmin()]

        # Haussière : nouveau bas de prix mais indicateur ne confirme pas
        if price_cur <= price_prev and ind_cur > ind_prev:
            bull.iloc[i] = True

        # Baissière : nouveau haut de prix mais indicateur ne confirme pas
        # [v2.1.2 — FIX-MOM-4] .values.argmin() / .values.argmax() :
        # pandas 2.0+ peut retourner la valeur d'index au lieu d'une position
        # entière avec argmin()/argmax() sur Series à index non 0-based.
        # .values garantit un ndarray numpy → position entière toujours.
        price_max_pos = window_price.values.argmax()
        if price_cur >= window_price.max() and ind_cur < window_ind.iloc[price_max_pos]:
            bear.iloc[i] = True

    return bull, bear


# ---------------------------------------------------------------------------
# Interprétations qualitatives
# ---------------------------------------------------------------------------

def _rsi_zone(value: float) -> str:
    if value >= 70:  return MomentumZone.OVERBOUGHT.value
    if value >= 55:  return MomentumZone.UPPER.value
    if value >= 45:  return MomentumZone.NEUTRAL.value
    if value >= 30:  return MomentumZone.LOWER.value
    return MomentumZone.OVERSOLD.value


def _stoch_zone(k: float) -> str:
    if k >= 80:  return MomentumZone.OVERBOUGHT.value
    if k >= 60:  return MomentumZone.UPPER.value
    if k >= 40:  return MomentumZone.NEUTRAL.value
    if k >= 20:  return MomentumZone.LOWER.value
    return MomentumZone.OVERSOLD.value


def _williams_zone(value: float) -> str:
    if value >= -20: return MomentumZone.OVERBOUGHT.value
    if value >= -40: return MomentumZone.UPPER.value
    if value >= -60: return MomentumZone.NEUTRAL.value
    if value >= -80: return MomentumZone.LOWER.value
    return MomentumZone.OVERSOLD.value


def _cmf_signal(value: float) -> str:
    if value >=  0.25: return CMFSignal.STRONG_BUYING.value
    if value >=  0.05: return CMFSignal.MODERATE_BUYING.value
    if value >= -0.05: return CMFSignal.NEUTRAL.value
    if value >= -0.25: return CMFSignal.MODERATE_SELLING.value
    return CMFSignal.STRONG_SELLING.value


def _roc_zscore_interpretation(z: float) -> str:
    if z >  2.0: return 'strong_bullish'
    if z >  1.0: return 'moderate_bullish'
    if z > -1.0: return 'neutral'
    if z > -2.0: return 'moderate_bearish'
    return 'strong_bearish'


def _obv_slope_label(slope: float) -> str:
    if slope >  0.01: return OBVSlope.RISING.value
    if slope < -0.01: return OBVSlope.FALLING.value
    return OBVSlope.FLAT.value


# ---------------------------------------------------------------------------
# Classe principale
# ---------------------------------------------------------------------------

class MomentumIndicator:
    """
    Indicateurs de momentum pour BULLET-1.

    Indicateurs calculés :
      • RSI avec divergences
      • MACD (ligne, signal, histogram, expanding, zero-cross)
      • ROC z-scoré
      • Stochastic RSI (%K, %D, zone, cross)
      • Williams %R
      • CMF (Chaikin Money Flow)
      • MFI (Money Flow Index)
      • OBV avec pente et divergences

    BACKTEST  : add_momentum_indicators(df) → DataFrame enrichi.
    PAPER/LIVE: update_incremental(candle)  → dict résultat O(1).
    SNAPSHOT  : get_snapshot(row, df)       → dict JSON-ready.
    """

    def __init__(self, config: dict, config_path: Optional[Path] = None) -> None:
        """
        Initialiser MomentumIndicator.

        Args:
            config      : Configuration principale BULLET-1 (dict).
            config_path : Chemin momentum_config.json (auto-détecté si None).

        Raises:
            TypeError         : config n'est pas un dict.
            FileNotFoundError : momentum_config.json introuvable.
            ValueError        : modes incohérents ou paramètres invalides.
        """
        if not isinstance(config, dict):
            raise TypeError(
                f"config doit être dict, reçu : {type(config).__name__}"
            )

        self.logger = BulletLogger()
        self.logger.info("Initializing MomentumIndicator...")

        self._main_config = config

        # Chargement momentum_config.json
        # Chargement + vérification cohérence du mode (helpers.py v2.4.0)
        self._mom_config, self.mode = load_and_verify_module_config(
            module_config_path = get_project_root() / 'config' / 'momentum_config.json',
            module_name        = 'momentum',
            main_config        = config,
            logger             = self.logger
        )

        # Extraction des paramètres depuis config (avec fallback sur les defaults)
        p = self._mom_config.get('parameters', {})
        self.rsi_period          = int(p.get('rsi_period',          DEFAULT_RSI_PERIOD))
        self.macd_fast           = int(p.get('macd_fast',           DEFAULT_MACD_FAST))
        self.macd_slow           = int(p.get('macd_slow',           DEFAULT_MACD_SLOW))
        self.macd_signal         = int(p.get('macd_signal',         DEFAULT_MACD_SIGNAL))
        self.roc_period          = int(p.get('roc_period',          DEFAULT_ROC_PERIOD))
        self.roc_zscore_period   = int(p.get('roc_zscore_period',   DEFAULT_ROC_ZSCORE_PERIOD))
        self.stoch_period        = int(p.get('stoch_rsi_period',    DEFAULT_STOCH_RSI_PERIOD))
        self.stoch_smooth_k      = int(p.get('stoch_smooth_k',      DEFAULT_STOCH_RSI_SMOOTH_K))
        self.stoch_smooth_d      = int(p.get('stoch_smooth_d',      DEFAULT_STOCH_RSI_SMOOTH_D))
        self.williams_period     = int(p.get('williams_period',     DEFAULT_WILLIAMS_PERIOD))
        self.cmf_period          = int(p.get('cmf_period',          DEFAULT_CMF_PERIOD))
        self.mfi_period          = int(p.get('mfi_period',          DEFAULT_MFI_PERIOD))
        self.obv_slope_period    = int(p.get('obv_slope_period',    DEFAULT_OBV_SLOPE_PERIOD))
        self.divergence_lookback = int(p.get('divergence_lookback', DEFAULT_DIVERGENCE_LOOKBACK))

        self._validate_params()

        # Nombre minimum de bougies requises pour calculs fiables
        self.min_candles = max(
            self.macd_slow + self.macd_signal,
            self.roc_zscore_period,
            self.stoch_period * 2,
            self.divergence_lookback + 5
        )

        # Cache incrémental pour PAPER/LIVE
        if self.mode in ('paper', 'live'):
            self._init_incremental_cache()
        else:
            self._cache: Optional[Dict] = None

        self.logger.info(
            f"✅ MomentumIndicator ready: mode={self.mode}, "
            f"rsi={self.rsi_period}, macd={self.macd_fast}/{self.macd_slow}/{self.macd_signal}, "
            f"min_candles={self.min_candles}"
        )

    # -----------------------------------------------------------------------
    # Validation
    # -----------------------------------------------------------------------

    def _validate_params(self) -> None:
        checks = [
            (self.rsi_period        >= 2,                    f"rsi_period doit être >= 2"),
            (self.macd_fast         >= 2,                    f"macd_fast doit être >= 2"),
            (self.macd_slow         > self.macd_fast,        f"macd_slow doit être > macd_fast"),
            (self.macd_signal       >= 2,                    f"macd_signal doit être >= 2"),
            (self.roc_period        >= 2,                    f"roc_period doit être >= 2"),
            (self.roc_zscore_period > self.roc_period,       f"roc_zscore_period doit être > roc_period"),
            (self.stoch_period      >= 2,                    f"stoch_rsi_period doit être >= 2"),
            (self.stoch_smooth_k    >= 1,                    f"stoch_smooth_k doit être >= 1"),
            (self.stoch_smooth_d    >= 1,                    f"stoch_smooth_d doit être >= 1"),
            (self.williams_period   >= 2,                    f"williams_period doit être >= 2"),
            (self.cmf_period        >= 2,                    f"cmf_period doit être >= 2"),
            (self.mfi_period        >= 2,                    f"mfi_period doit être >= 2"),
            (self.obv_slope_period  >= 2,                    f"obv_slope_period doit être >= 2"),
            (self.divergence_lookback >= 5,                  f"divergence_lookback doit être >= 5"),
        ]
        for ok, msg in checks:
            if not ok:
                raise ValueError(msg)

    # -----------------------------------------------------------------------
    # Cache incrémental — PAPER / LIVE
    # -----------------------------------------------------------------------

    def _init_incremental_cache(self) -> None:
        """Initialiser le cache rolling pour modes PAPER/LIVE."""
        size = self._mom_config.get('cache', {}).get('buffer_size', DEFAULT_CACHE_SIZE)
        self._cache = {
            'close':  deque(maxlen=size),
            'high':   deque(maxlen=size),
            'low':    deque(maxlen=size),
            'volume': deque(maxlen=size),
            'size':   size,
            'warmed_up': False,
        }
        self.logger.debug(f"Incremental cache initialized: buffer_size={size}")

    def initialize_warmup(self, historical_df: pd.DataFrame) -> None:
        """
        Initialiser le cache avec données historiques (PAPER/LIVE).

        Args:
            historical_df : DataFrame OHLCV avec colonnes ['open','high','low','close','volume'].

        Raises:
            RuntimeError : appelé en mode BACKTEST.
            ValueError   : colonnes manquantes ou données insuffisantes.
        """
        if self.mode == 'backtest':
            raise RuntimeError("initialize_warmup() interdit en BACKTEST. Utiliser add_momentum_indicators().")

        self._validate_ohlcv(historical_df)
        if len(historical_df) < self.min_candles:
            raise ValueError(
                f"Données insuffisantes pour warmup : {len(historical_df)} bougies, "
                f"minimum requis : {self.min_candles}"
            )

        cache = self._cache
        tail  = historical_df.tail(cache['size'])
        for col in ('close', 'high', 'low', 'volume'):
            cache[col].clear()
            for v in tail[col]:
                cache[col].append(float(v))

        cache['warmed_up'] = True
        self.logger.info(
            f"Warmup OK : {len(tail)} bougies chargées dans le cache."
        )

    def update_incremental(self, candle: dict) -> dict:
        """
        Mettre à jour le cache avec une nouvelle bougie et calculer tous les
        indicateurs momentum (PAPER/LIVE).

        Args:
            candle : Dict avec clés 'open', 'high', 'low', 'close', 'volume'.

        Returns:
            dict : tous les indicateurs momentum pour cette bougie.

        Raises:
            RuntimeError : appelé en BACKTEST.
            ValueError   : clés manquantes ou warmup non effectué.
        """
        if self.mode == 'backtest':
            raise RuntimeError(
                "update_incremental() interdit en BACKTEST. Utiliser add_momentum_indicators()."
            )
        if not self._cache.get('warmed_up'):
            raise ValueError("Warmup non effectué. Appeler initialize_warmup() d'abord.")

        start = time.perf_counter()

        required = ('open', 'high', 'low', 'close', 'volume')
        missing  = [k for k in required if k not in candle]
        if missing:
            raise ValueError(f"Clés manquantes dans candle : {missing}")

        # Mise à jour cache
        for col in ('close', 'high', 'low', 'volume'):
            self._cache[col].append(float(candle[col]))

        # Recalcul depuis le cache (DataFrame temporaire)
        df_tmp = pd.DataFrame({
            'close':  list(self._cache['close']),
            'high':   list(self._cache['high']),
            'low':    list(self._cache['low']),
            'volume': list(self._cache['volume']),
        })
        df_result = self.add_momentum_indicators(df_tmp)
        row = df_result.iloc[-1]

        elapsed = (time.perf_counter() - start) * 1000
        if elapsed > 50:
            self.logger.warning(f"update_incremental lent : {elapsed:.1f}ms")

        return self._row_to_dict(row)

    # -----------------------------------------------------------------------
    # Calcul batch — BACKTEST
    # -----------------------------------------------------------------------

    def add_momentum_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Ajouter tous les indicateurs momentum au DataFrame (vectorisé, batch).

        Colonnes ajoutées :
          mom_rsi, mom_rsi_zone, mom_rsi_div_bull, mom_rsi_div_bear
          mom_macd_line, mom_macd_signal, mom_macd_histogram,
          mom_macd_histo_expanding, mom_macd_zero_cross, mom_macd_bars_since_cross,
          mom_macd_div_bull, mom_macd_div_bear
          mom_roc_pct, mom_roc_zscore
          mom_stoch_k, mom_stoch_d, mom_stoch_zone,
          mom_stoch_cross_up, mom_stoch_cross_down
          mom_williams_r, mom_williams_zone
          mom_cmf
          mom_mfi, mom_mfi_div_bull, mom_mfi_div_bear
          mom_obv, mom_obv_slope, mom_obv_div_bull, mom_obv_div_bear

        Args:
            df : DataFrame avec colonnes ['high', 'low', 'close', 'volume'].

        Returns:
            DataFrame enrichi (copie).

        Raises:
            ValueError : colonnes manquantes ou données insuffisantes.
        """
        self._validate_ohlcv(df)
        if len(df) < self.min_candles:
            self.logger.warning(
                f"Données potentiellement insuffisantes : {len(df)} bougies, "
                f"recommandé >= {self.min_candles}."
            )

        df = df.copy()
        close  = df['close']
        high   = df['high']
        low    = df['low']
        volume = df['volume']

        # ---- RSI ----
        rsi = _calc_rsi(close, self.rsi_period)
        df['mom_rsi']       = rsi.round(4)
        df['mom_rsi_zone']  = rsi.apply(_rsi_zone)
        bull_div, bear_div  = _detect_divergences(close, rsi, self.divergence_lookback)
        df['mom_rsi_div_bull'] = bull_div
        df['mom_rsi_div_bear'] = bear_div

        # ---- MACD ----
        macd_line, signal_line, histogram = _calc_macd(
            close, self.macd_fast, self.macd_slow, self.macd_signal
        )
        df['mom_macd_line']    = macd_line.round(4)
        df['mom_macd_signal']  = signal_line.round(4)
        df['mom_macd_histogram'] = histogram.round(4)

        # Histogram expanding : |histo[t]| > |histo[t-1]|
        df['mom_macd_histo_expanding'] = histogram.abs() > histogram.abs().shift(1)

        # Zero-cross : changement de signe du MACD line
        macd_sign         = np.sign(macd_line)
        macd_sign_prev    = macd_sign.shift(1)
        zero_cross        = macd_sign != macd_sign_prev
        df['mom_macd_zero_cross'] = zero_cross.fillna(False)

        # Bars since last zero-cross (cumulatif depuis dernier cross)
        cross_idx = zero_cross[zero_cross].index
        bars_since = pd.Series(np.nan, index=df.index)
        for idx in cross_idx:
            loc = df.index.get_loc(idx)
            for j in range(loc, len(df)):
                bars_since.iloc[j] = j - loc
        df['mom_macd_bars_since_cross'] = bars_since

        macd_bull_div, macd_bear_div = _detect_divergences(close, histogram, self.divergence_lookback)
        df['mom_macd_div_bull'] = macd_bull_div
        df['mom_macd_div_bear'] = macd_bear_div

        # ---- ROC z-scoré ----
        roc_raw, roc_z = _calc_roc_zscore(close, self.roc_period, self.roc_zscore_period)
        df['mom_roc_pct']    = roc_raw.round(4)
        df['mom_roc_zscore'] = roc_z.round(4)

        # ---- Stochastic RSI ----
        stoch_k, stoch_d = _calc_stoch_rsi(
            rsi, self.stoch_period, self.stoch_smooth_k, self.stoch_smooth_d
        )
        df['mom_stoch_k']    = stoch_k.round(4)
        df['mom_stoch_d']    = stoch_d.round(4)
        df['mom_stoch_zone'] = stoch_k.apply(_stoch_zone)

        # Cross récent %K/%D
        #
        # [FIX-MOM-1] k_above.shift(1) introduit un NaN sur la première ligne.
        # pandas ne peut pas représenter NaN dans une Series bool → dtype promu à
        # object (ou float64 selon la version). L'opérateur ~ exige strictement bool.
        # Sans ce fix : TypeError: bad operand type for unary ~: 'float'
        # Reproduit systématiquement quand le df contient des bougies de début de
        # session (cas nominal dans MCCapture avec candles_window brut).
        k_above      = stoch_k > stoch_d
        k_above_prev = k_above.shift(1).fillna(False).astype(bool)
        df['mom_stoch_cross_up']   = (~k_above_prev & k_above).fillna(False)
        df['mom_stoch_cross_down'] = (k_above_prev & ~k_above).fillna(False)

        # ---- Williams %R ----
        wr = _calc_williams_r(high, low, close, self.williams_period)
        df['mom_williams_r']    = wr.round(4)
        df['mom_williams_zone'] = wr.apply(_williams_zone)

        # ---- CMF ----
        df['mom_cmf'] = _calc_cmf(high, low, close, volume, self.cmf_period).round(4)

        # ---- MFI ----
        mfi = _calc_mfi(high, low, close, volume, self.mfi_period)
        df['mom_mfi']      = mfi.round(4)
        df['mom_mfi_zone'] = mfi.apply(_rsi_zone)  # mêmes seuils que RSI
        mfi_bull, mfi_bear = _detect_divergences(close, mfi, self.divergence_lookback)
        df['mom_mfi_div_bull'] = mfi_bull
        df['mom_mfi_div_bear'] = mfi_bear

        # ---- OBV ----
        obv = _calc_obv(close, volume)
        df['mom_obv'] = obv.round(0)

        # Slope OBV normalisée sur N périodes (% de variation)
        obv_prev = obv.shift(self.obv_slope_period)
        obv_slope = ((obv - obv_prev) / obv_prev.abs().replace(0, np.nan)).fillna(0.0)
        df['mom_obv_slope_raw'] = obv_slope.round(6)
        df['mom_obv_slope']     = obv_slope.apply(_obv_slope_label)

        obv_bull, obv_bear = _detect_divergences(close, obv, self.divergence_lookback)
        df['mom_obv_div_bull'] = obv_bull
        df['mom_obv_div_bear'] = obv_bear

        self.logger.debug(f"add_momentum_indicators: {len(df)} bougies traitées.")
        return df

    # -----------------------------------------------------------------------
    # Snapshot JSON — appelé à l'entrée d'un trade
    # -----------------------------------------------------------------------

    def get_snapshot(self, row: pd.Series) -> Dict[str, Any]:
        """
        Retourner le dict indicators['momentum'] pour le rapport de trade.

        Ce dict est directement sérialisable en JSON et peut être
        aplati via pd.json_normalize() pour le ML.

        Args:
            row : ligne du DataFrame à l'instant T (bougie clôturée précédant l'entrée).
                  Doit être issue d'un DataFrame traité par add_momentum_indicators().

        Returns:
            dict : section 'momentum' de indicators.

        Raises:
            ValueError : colonnes momentum absentes (add_momentum_indicators non appelé).
        """
        required_cols = ['mom_rsi', 'mom_macd_line', 'mom_stoch_k', 'mom_obv']
        missing = [c for c in required_cols if c not in row.index]
        if missing:
            raise ValueError(
                f"Colonnes momentum absentes : {missing}. "
                "Appeler add_momentum_indicators() d'abord."
            )

        def _safe(val, decimals: int = 4) -> Optional[float]:
            """Convertir en float arrondi ou None si NaN."""
            if val is None or (isinstance(val, float) and np.isnan(val)):
                return None
            return round(float(val), decimals)

        def _safe_int(val) -> Optional[int]:
            if val is None or (isinstance(val, float) and np.isnan(val)):
                return None
            return int(val)

        def _safe_bool(val) -> bool:
            return bool(val) if val is not None else False

        bars_cross = _safe_int(row.get('mom_macd_bars_since_cross'))
        roc_z      = _safe(row.get('mom_roc_zscore'))

        return {
            "_source": "momentum.py / MomentumIndicator",

            "rsi": {
                "period":           self.rsi_period,
                "value":            _safe(row['mom_rsi'], 2),
                "zone":             str(row.get('mom_rsi_zone', 'neutral')),
                "divergence_bull":  _safe_bool(row.get('mom_rsi_div_bull')),
                "divergence_bear":  _safe_bool(row.get('mom_rsi_div_bear')),
            },

            "macd": {
                "fast_period":          self.macd_fast,
                "slow_period":          self.macd_slow,
                "signal_period":        self.macd_signal,
                "macd_line":            _safe(row['mom_macd_line'], 4),
                "signal_line":          _safe(row.get('mom_macd_signal'), 4),
                "histogram":            _safe(row.get('mom_macd_histogram'), 4),
                "histogram_expanding":  _safe_bool(row.get('mom_macd_histo_expanding')),
                "zero_cross_recent":    _safe_bool(row.get('mom_macd_zero_cross')),
                "bars_since_cross":     bars_cross,
                "divergence_bull":      _safe_bool(row.get('mom_macd_div_bull')),
                "divergence_bear":      _safe_bool(row.get('mom_macd_div_bear')),
            },

            "roc_zscore": {
                "period_roc":       self.roc_period,
                "period_zscore":    self.roc_zscore_period,
                "roc_raw_pct":      _safe(row.get('mom_roc_pct'), 4),
                "zscore":           roc_z,
                "interpretation":   _roc_zscore_interpretation(roc_z or 0.0),
            },

            "stoch_rsi": {
                "period":               self.stoch_period,
                "smooth_k":             self.stoch_smooth_k,
                "smooth_d":             self.stoch_smooth_d,
                "k":                    _safe(row.get('mom_stoch_k'), 2),
                "d":                    _safe(row.get('mom_stoch_d'), 2),
                "zone":                 str(row.get('mom_stoch_zone', 'neutral')),
                "cross_up_recent":      _safe_bool(row.get('mom_stoch_cross_up')),
                "cross_down_recent":    _safe_bool(row.get('mom_stoch_cross_down')),
            },

            "williams_r": {
                "period": self.williams_period,
                "value":  _safe(row.get('mom_williams_r'), 2),
                "zone":   str(row.get('mom_williams_zone', 'neutral')),
            },

            "cmf": {
                "period":           self.cmf_period,
                "value":            _safe(row.get('mom_cmf'), 4),
                "signal":           _cmf_signal(float(row.get('mom_cmf', 0.0))),
            },

            "mfi": {
                "period":           self.mfi_period,
                "value":            _safe(row.get('mom_mfi'), 2),
                "zone":             str(row.get('mom_mfi_zone', 'neutral')),
                "divergence_bull":  _safe_bool(row.get('mom_mfi_div_bull')),
                "divergence_bear":  _safe_bool(row.get('mom_mfi_div_bear')),
            },

            "obv": {
                "period_slope":     self.obv_slope_period,
                "value":            _safe(row.get('mom_obv'), 0),
                "slope":            str(row.get('mom_obv_slope', 'flat')),
                "slope_raw":        _safe(row.get('mom_obv_slope_raw'), 6),
                "divergence_bull":  _safe_bool(row.get('mom_obv_div_bull')),
                "divergence_bear":  _safe_bool(row.get('mom_obv_div_bear')),
            },
        }

    # -----------------------------------------------------------------------
    # Helpers privés
    # -----------------------------------------------------------------------

    def _validate_ohlcv(self, df: pd.DataFrame) -> None:
        required = ['high', 'low', 'close', 'volume']
        missing  = [c for c in required if c not in df.columns]
        if missing:
            raise ValueError(f"Colonnes OHLCV manquantes : {missing}")
        if df.empty:
            raise ValueError("DataFrame vide.")

    def _row_to_dict(self, row: pd.Series) -> dict:
        """Convertir une ligne en dict simple (PAPER/LIVE)."""
        return self.get_snapshot(row)

    def get_configuration(self) -> Dict[str, Any]:
        """Retourner la configuration active du module."""
        return {
            'version':           _VERSION,  # [v2.1.2 — FIX-MOM-2] était '2.1.1' hardcodé
            'mode':              self.mode,
            'rsi_period':        self.rsi_period,
            'macd_fast':         self.macd_fast,
            'macd_slow':         self.macd_slow,
            'macd_signal':       self.macd_signal,
            'roc_period':        self.roc_period,
            'roc_zscore_period': self.roc_zscore_period,
            'stoch_period':      self.stoch_period,
            'stoch_smooth_k':    self.stoch_smooth_k,
            'stoch_smooth_d':    self.stoch_smooth_d,
            'williams_period':   self.williams_period,
            'cmf_period':        self.cmf_period,
            'mfi_period':        self.mfi_period,
            'obv_slope_period':  self.obv_slope_period,
            'divergence_lookback': self.divergence_lookback,
            'min_candles_required': self.min_candles,
        }

    def __repr__(self) -> str:
        return (
            f"MomentumIndicator(mode={self.mode}, rsi={self.rsi_period}, "
            f"macd={self.macd_fast}/{self.macd_slow}/{self.macd_signal})"
        )


# ---------------------------------------------------------------------------
# Fonctions utilitaires standalone
# ---------------------------------------------------------------------------

def calculate_rsi_simple(close: pd.Series, period: int = 14) -> pd.Series:
    """Calculer RSI sans instancier MomentumIndicator."""
    # [v2.1.2 — FIX-MOM-3] Guard simplifié — l'ancienne condition
    # `'close' not in getattr(close, 'name', 'close') and not isinstance(...)`
    # était toujours False (premier membre) → TypeError jamais levé.
    if not isinstance(close, pd.Series):
        raise TypeError("close doit être une pd.Series")
    return _calc_rsi(close, period)


def calculate_macd_simple(close: pd.Series, fast: int = 12,
                           slow: int = 26, signal: int = 9
                           ) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """Calculer MACD sans instancier MomentumIndicator."""
    return _calc_macd(close, fast, slow, signal)


def calculate_obv_simple(close: pd.Series, volume: pd.Series) -> pd.Series:
    """Calculer OBV sans instancier MomentumIndicator."""
    return _calc_obv(close, volume)


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

__all__ = [
    'MomentumIndicator',
    'MomentumSnapshot',
    'MomentumZone',
    'OBVSlope',
    'CMFSignal',
    'calculate_rsi_simple',
    'calculate_macd_simple',
    'calculate_obv_simple',
]

# FIN DU MODULE