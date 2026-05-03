"""
BULLET-1 - Market Regime Module
=======================================
Calcul ADX (+DI / -DI), Variance Ratio (Lo-MacKinlay),
et synthèse du régime composite de marché.

Gestion modes backtest/paper/live depuis config/regime_config.json.
Pattern identique à atr.py / trend.py / volume.py.

Version : 2.1.2
Author  : FuegoDev
Date    : 2026-03-13
Mode    : ✅ Backtest | ✅ Paper | ✅ Live
"""

import json
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Deque, Dict, Optional, Tuple
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
_VERSION = "2.1.2"  # [v2.1.2 — FIX-REG-1]

# ---------------------------------------------------------------------------
# Constantes & Enums
# ---------------------------------------------------------------------------

DEFAULT_ADX_PERIOD          = 14
DEFAULT_VR_SHORT_WINDOW     = 5
DEFAULT_VR_LONG_WINDOW      = 20
DEFAULT_CACHE_SIZE          = 500

# Seuils ADX (Wilder)
ADX_THRESHOLD_TREND         = 20.0   # ADX >= 20 → tendance présente
ADX_THRESHOLD_STRONG        = 40.0   # ADX >= 40 → tendance forte
ADX_THRESHOLD_VERY_STRONG   = 60.0   # ADX >= 60 → tendance très forte (rare)

# Seuils Variance Ratio
VR_MOMENTUM_THRESHOLD       = 1.05   # VR > 1.05 → momentum
VR_MEANREV_THRESHOLD        = 0.95   # VR < 0.95 → mean-reversion


class RegimeComposite(Enum):
    TRENDING_BULLISH    = 'trending_bullish'
    TRENDING_BEARISH    = 'trending_bearish'
    RANGING_MOMENTUM    = 'ranging_momentum'
    RANGING_MEANREV     = 'ranging_meanrev'
    RANGING_NEUTRAL     = 'ranging_neutral'
    TRANSITIONING       = 'transitioning'


class ADXStrength(Enum):
    VERY_STRONG = 'very_strong'   # ADX >= 60
    STRONG      = 'strong'        # ADX >= 40
    MODERATE    = 'moderate'      # ADX >= 20
    WEAK        = 'weak'          # ADX < 20


class VRType(Enum):
    MOMENTUM    = 'momentum'      # VR > 1.05
    RANDOM_WALK = 'random_walk'   # 0.95 <= VR <= 1.05
    MEAN_REVERT = 'mean_revert'   # VR < 0.95


@dataclass
class RegimeSnapshot:
    """Snapshot complet du régime de marché à l'instant T."""
    adx_value:        float
    plus_di:          float
    minus_di:         float
    adx_trend_present: bool
    adx_strength:     str
    vr_value:         float
    vr_type:          str
    vr_short_window:  int
    vr_long_window:   int
    regime_composite: str
    regime_confidence: float


# ---------------------------------------------------------------------------
# Fonctions mathématiques pures
# ---------------------------------------------------------------------------

def _calc_adx(high: pd.Series, low: pd.Series, close: pd.Series,
              period: int) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """
    ADX, +DI, -DI via Wilder's smoothing (RMA).

    +DM = High[t] − High[t-1]  si positif ET > |Low[t] − Low[t-1]|, sinon 0
    -DM = Low[t-1] − Low[t]    si positif ET > |High[t] − High[t-1]|, sinon 0
    TR  = max(H−L, |H−Cp|, |L−Cp|)

    ATR_w    = RMA(TR, period)
    +DI      = 100 × RMA(+DM, period) / ATR_w
    -DI      = 100 × RMA(-DM, period) / ATR_w
    DX       = 100 × |+DI − -DI| / (+DI + -DI)
    ADX      = RMA(DX, period)
    """
    prev_high  = high.shift(1)
    prev_low   = low.shift(1)
    prev_close = close.shift(1)

    # True Range
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs()
    ], axis=1).max(axis=1)

    # Directional Movement
    up_move   = high - prev_high
    down_move = prev_low - low

    plus_dm  = up_move.where(  (up_move > down_move) & (up_move > 0),   0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)

    # Wilder's smoothing (RMA = EMA with alpha = 1/period)
    def _rma(series: pd.Series) -> pd.Series:
        return series.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()

    atr_w     = _rma(tr)
    plus_di   = (100.0 * _rma(plus_dm)  / atr_w.replace(0, np.nan)).fillna(0.0)
    minus_di  = (100.0 * _rma(minus_dm) / atr_w.replace(0, np.nan)).fillna(0.0)

    # DX et ADX
    di_sum    = (plus_di + minus_di).replace(0, np.nan)
    dx        = (100.0 * (plus_di - minus_di).abs() / di_sum).fillna(0.0)
    adx       = _rma(dx)

    return adx.round(4), plus_di.round(4), minus_di.round(4)


def _calc_variance_ratio(close: pd.Series, short_window: int,
                          long_window: int) -> pd.Series:
    """
    Variance Ratio (Lo-MacKinlay simplifié).

    VR(q) = Var(r_q) / (q × Var(r_1))

    où r_q = log(Close[t] / Close[t−q])
       r_1 = log(Close[t] / Close[t−1])

    VR > 1 → momentum (les rendements sont corrélés positivement)
    VR = 1 → marche aléatoire
    VR < 1 → mean-reversion (corrélation négative)

    Fenêtre glissante de taille long_window × short_window pour la variance.
    """
    log_ret_1 = np.log(close / close.shift(1))
    log_ret_q = np.log(close / close.shift(short_window))

    # Variance glissante sur long_window observations
    var_1 = log_ret_1.rolling(long_window, min_periods=long_window).var(ddof=1)
    var_q = log_ret_q.rolling(long_window, min_periods=long_window).var(ddof=1)

    # VR = Var(r_q) / (q × Var(r_1))
    denom = (short_window * var_1).replace(0, np.nan)
    vr    = (var_q / denom).fillna(1.0)

    return vr.round(4)


# ---------------------------------------------------------------------------
# Interprétations qualitatives
# ---------------------------------------------------------------------------

def _adx_strength_label(adx_val: float) -> str:
    if adx_val >= ADX_THRESHOLD_VERY_STRONG: return ADXStrength.VERY_STRONG.value
    if adx_val >= ADX_THRESHOLD_STRONG:      return ADXStrength.STRONG.value
    if adx_val >= ADX_THRESHOLD_TREND:       return ADXStrength.MODERATE.value
    return ADXStrength.WEAK.value


def _vr_type_label(vr: float) -> str:
    if vr > VR_MOMENTUM_THRESHOLD:  return VRType.MOMENTUM.value
    if vr < VR_MEANREV_THRESHOLD:   return VRType.MEAN_REVERT.value
    return VRType.RANDOM_WALK.value


def _composite_regime(adx: float, plus_di: float, minus_di: float,
                       vr: float) -> Tuple[str, float]:
    """
    Synthèse du régime composite à partir d'ADX et Variance Ratio.

    Matrice de décision :
    ┌───────────────────┬───────────────────────────────────────────────┐
    │ ADX               │ VR                                            │
    ├───────────────────┼───────────────────────────────────────────────┤
    │ >= threshold      │ > momentum_thresh  → trending (direction ±DI)│
    │ >= threshold      │ random walk        → trending (direction ±DI)│
    │ >= threshold      │ < meanrev_thresh   → trending mais incohérent│
    │ < threshold       │ > momentum_thresh  → ranging momentum        │
    │ < threshold       │ random walk        → ranging neutral         │
    │ < threshold       │ < meanrev_thresh   → ranging mean-reversion  │
    └───────────────────┴───────────────────────────────────────────────┘

    Confidence : combinaison normalisée ADX + cohérence VR.

    Returns:
        (regime_label, confidence 0.0−1.0)
    """
    trend_present = adx >= ADX_THRESHOLD_TREND
    vr_type       = _vr_type_label(vr)
    bullish       = plus_di > minus_di

    # Score de confiance partiel ADX (0 → 1)
    adx_conf = min(adx / ADX_THRESHOLD_STRONG, 1.0)

    # Score cohérence VR (0 → 1)
    if trend_present:
        # En tendance, VR momentum = cohérent (+), VR mean-rev = incohérent (-)
        vr_conf = 1.0 if vr_type == VRType.MOMENTUM.value else \
                  0.5 if vr_type == VRType.RANDOM_WALK.value else 0.2
    else:
        # Hors tendance, VR mean-rev = cohérent (+), VR momentum = incohérent (-)
        vr_conf = 1.0 if vr_type == VRType.MEAN_REVERT.value else \
                  0.5 if vr_type == VRType.RANDOM_WALK.value else 0.3

    # [v2.1.2 — FIX-REG-3] Clamp défensif : la somme pondérée est actuellement
    # ≤ 1.0 grâce aux valeurs des composants, mais si les seuils sont modifiés
    # ultérieurement, confidence pourrait dépasser 1.0 sans guard explicite.
    confidence = round(min(0.6 * adx_conf + 0.4 * vr_conf, 1.0), 4)

    if trend_present:
        if vr_type == VRType.MEAN_REVERT.value:
            # Tendance ADX mais VR contradictoire → transitioning
            regime = RegimeComposite.TRANSITIONING.value
        elif bullish:
            regime = RegimeComposite.TRENDING_BULLISH.value
        else:
            regime = RegimeComposite.TRENDING_BEARISH.value
    else:
        if vr_type == VRType.MOMENTUM.value:
            regime = RegimeComposite.RANGING_MOMENTUM.value
        elif vr_type == VRType.MEAN_REVERT.value:
            regime = RegimeComposite.RANGING_MEANREV.value
        else:
            regime = RegimeComposite.RANGING_NEUTRAL.value

    return regime, confidence


# ---------------------------------------------------------------------------
# Classe principale
# ---------------------------------------------------------------------------

class RegimeIndicator:
    """
    Indicateur de régime de marché pour BULLET-1.

    Indicateurs calculés :
      • ADX (14) + +DI / -DI avec interprétation force/présence tendance
      • Variance Ratio (court/long window)
      • Régime composite + score de confiance

    BACKTEST  : add_regime_indicators(df) → DataFrame enrichi.
    PAPER/LIVE: update_incremental(candle) → dict résultat O(1).
    SNAPSHOT  : get_snapshot(row)          → dict JSON-ready.
    """

    def __init__(self, config: dict, config_path: Optional[Path] = None) -> None:
        """
        Initialiser RegimeIndicator.

        Args:
            config      : Configuration principale BULLET-1 (dict).
            config_path : Chemin regime_config.json (auto-détecté si None).

        Raises:
            TypeError         : config n'est pas un dict.
            FileNotFoundError : regime_config.json introuvable.
            ValueError        : modes incohérents ou paramètres invalides.
        """
        if not isinstance(config, dict):
            raise TypeError(
                f"config doit être dict, reçu : {type(config).__name__}"
            )

        self.logger = BulletLogger()
        self.logger.info("Initializing RegimeIndicator...")

        self._main_config = config

        # Chargement + vérification cohérence du mode (helpers.py v2.4.0)
        self._regime_config, self.mode = load_and_verify_module_config(
            module_config_path = get_project_root() / 'config' / 'regime_config.json',
            module_name        = 'regime',
            main_config        = config,
            logger             = self.logger
        )

        p = self._regime_config.get('parameters', {})
        self.adx_period      = int(p.get('adx_period',       DEFAULT_ADX_PERIOD))
        self.vr_short_window = int(p.get('vr_short_window',  DEFAULT_VR_SHORT_WINDOW))
        self.vr_long_window  = int(p.get('vr_long_window',   DEFAULT_VR_LONG_WINDOW))

        # Seuils configurables (avec fallback sur les constantes)
        self.adx_threshold_trend      = float(p.get('adx_threshold_trend',      ADX_THRESHOLD_TREND))
        self.adx_threshold_strong     = float(p.get('adx_threshold_strong',     ADX_THRESHOLD_STRONG))
        self.adx_threshold_very_strong = float(p.get('adx_threshold_very_strong', ADX_THRESHOLD_VERY_STRONG))
        self.vr_momentum_threshold    = float(p.get('vr_momentum_threshold',    VR_MOMENTUM_THRESHOLD))
        self.vr_meanrev_threshold     = float(p.get('vr_meanrev_threshold',     VR_MEANREV_THRESHOLD))

        self._validate_params()

        self.min_candles = max(
            self.adx_period * 2,
            self.vr_long_window + self.vr_short_window
        )

        if self.mode in ('paper', 'live'):
            self._init_incremental_cache()
        else:
            self._cache: Optional[Dict] = None

        self.logger.info(
            f"✅ RegimeIndicator ready: mode={self.mode}, "
            f"adx={self.adx_period}, vr={self.vr_short_window}/{self.vr_long_window}, "
            f"min_candles={self.min_candles}"
        )

    # -----------------------------------------------------------------------
    # Validation
    # -----------------------------------------------------------------------

    def _validate_params(self) -> None:
        checks = [
            (self.adx_period        >= 2,                         "adx_period doit être >= 2"),
            (self.vr_short_window   >= 2,                         "vr_short_window doit être >= 2"),
            (self.vr_long_window    > self.vr_short_window,       "vr_long_window doit être > vr_short_window"),
            (self.adx_threshold_trend >= 0,                       "adx_threshold_trend doit être >= 0"),
            (self.adx_threshold_strong > self.adx_threshold_trend, "adx_threshold_strong doit être > trend"),
            (self.adx_threshold_very_strong > self.adx_threshold_strong, "adx_threshold_very_strong doit être > strong"),
            (0 < self.vr_meanrev_threshold < self.vr_momentum_threshold,
             "vr_meanrev_threshold doit être < vr_momentum_threshold"),
        ]
        for ok, msg in checks:
            if not ok:
                raise ValueError(msg)

    # -----------------------------------------------------------------------
    # Cache incrémental — PAPER / LIVE
    # -----------------------------------------------------------------------

    def _init_incremental_cache(self) -> None:
        size = self._regime_config.get('cache', {}).get('buffer_size', DEFAULT_CACHE_SIZE)
        self._cache = {
            'close': deque(maxlen=size),
            'high':  deque(maxlen=size),
            'low':   deque(maxlen=size),
            'size':  size,
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
                "initialize_warmup() interdit en BACKTEST. Utiliser add_regime_indicators()."
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
        Mettre à jour le cache et calculer les indicateurs de régime (PAPER/LIVE).

        Args:
            candle : Dict avec clés 'high', 'low', 'close'.

        Returns:
            dict : section 'market_regime' de indicators.

        Raises:
            RuntimeError : appelé en BACKTEST.
            ValueError   : clés manquantes ou warmup non effectué.
        """
        if self.mode == 'backtest':
            raise RuntimeError(
                "update_incremental() interdit en BACKTEST. Utiliser add_regime_indicators()."
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
        df_result = self.add_regime_indicators(df_tmp)
        row = df_result.iloc[-1]

        elapsed = (time.perf_counter() - start) * 1000
        if elapsed > 50:
            self.logger.warning(f"update_incremental lent : {elapsed:.1f}ms")

        return self.get_snapshot(row)

    # -----------------------------------------------------------------------
    # Calcul batch — BACKTEST
    # -----------------------------------------------------------------------

    def add_regime_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Ajouter tous les indicateurs de régime au DataFrame (vectorisé, batch).

        Colonnes ajoutées :
          reg_adx, reg_plus_di, reg_minus_di,
          reg_adx_trend_present, reg_adx_strength,
          reg_vr, reg_vr_type,
          reg_regime_composite, reg_regime_confidence

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

        df    = df.copy()
        close = df['close']
        high  = df['high']
        low   = df['low']

        # ---- ADX ----
        adx, plus_di, minus_di = _calc_adx(high, low, close, self.adx_period)
        df['reg_adx']       = adx
        df['reg_plus_di']   = plus_di
        df['reg_minus_di']  = minus_di
        df['reg_adx_trend_present'] = adx >= self.adx_threshold_trend
        df['reg_adx_strength']      = adx.apply(self._adx_strength_label)

        # ---- Variance Ratio ----
        vr = _calc_variance_ratio(close, self.vr_short_window, self.vr_long_window)
        df['reg_vr']      = vr
        df['reg_vr_type'] = vr.apply(self._vr_type_label)

        # ---- Régime composite ----
        # Vectorisation via apply ligne par ligne (logique conditionnelle complexe)
        composite_data = pd.DataFrame({
            'adx':      adx,
            'plus_di':  plus_di,
            'minus_di': minus_di,
            'vr':       vr,
        })

        def _row_regime(row) -> Tuple[str, float]:
            return _composite_regime(
                float(row['adx']),
                float(row['plus_di']),
                float(row['minus_di']),
                float(row['vr']),
            )

        regimes = composite_data.apply(_row_regime, axis=1)
        # [v2.1.2 — FIX-REG-2] Construction en une passe via pd.DataFrame(list(...)).
        # L'ancienne implémentation faisait deux passes supplémentaires :
        #   regimes.apply(lambda x: x[0]) + regimes.apply(lambda x: x[1])
        # pd.DataFrame(list(regimes)) décompacte les tuples en colonnes directement.
        regime_df = pd.DataFrame(list(regimes), columns=['_regime', '_confidence'], index=df.index)
        df['reg_regime_composite']  = regime_df['_regime']
        df['reg_regime_confidence'] = regime_df['_confidence']

        self.logger.debug(f"add_regime_indicators: {len(df)} bougies traitées.")
        return df

    # -----------------------------------------------------------------------
    # Snapshot JSON
    # -----------------------------------------------------------------------

    def get_snapshot(self, row: pd.Series) -> Dict[str, Any]:
        """
        Retourner le dict indicators['market_regime'] pour le rapport de trade.

        Args:
            row : ligne du DataFrame à l'instant T, issue de add_regime_indicators().

        Returns:
            dict : section 'market_regime' de indicators.

        Raises:
            ValueError : colonnes régime absentes.
        """
        required_cols = ['reg_adx', 'reg_vr', 'reg_regime_composite']
        missing = [c for c in required_cols if c not in row.index]
        if missing:
            raise ValueError(
                f"Colonnes régime absentes : {missing}. "
                "Appeler add_regime_indicators() d'abord."
            )

        def _safe(val, decimals: int = 4) -> Optional[float]:
            if val is None or (isinstance(val, float) and np.isnan(val)):
                return None
            return round(float(val), decimals)

        adx_val  = _safe(row['reg_adx'],   2) or 0.0
        pdi_val  = _safe(row['reg_plus_di'],  2) or 0.0
        mdi_val  = _safe(row['reg_minus_di'], 2) or 0.0
        vr_val   = _safe(row['reg_vr'], 4)    or 1.0

        return {
            "_source": "regime.py / RegimeIndicator",

            "adx": {
                "period":           self.adx_period,
                "value":            adx_val,
                "plus_di":          pdi_val,
                "minus_di":         mdi_val,
                "trend_present":    bool(row.get('reg_adx_trend_present', False)),
                "strength_label":   str(row.get('reg_adx_strength', ADXStrength.WEAK.value)),
                "threshold_trend":  self.adx_threshold_trend,
                "threshold_strong": self.adx_threshold_strong,
            },

            "variance_ratio": {
                "short_window": self.vr_short_window,
                "long_window":  self.vr_long_window,
                "value":        vr_val,
                "type":         str(row.get('reg_vr_type', VRType.RANDOM_WALK.value)),
                "threshold_momentum": self.vr_momentum_threshold,
                "threshold_meanrev":  self.vr_meanrev_threshold,
            },

            "regime_composite":  str(row.get('reg_regime_composite',
                                              RegimeComposite.RANGING_NEUTRAL.value)),
            "regime_confidence": _safe(row.get('reg_regime_confidence'), 4) or 0.0,
        }

    # -----------------------------------------------------------------------
    # Helpers — wrappers des fonctions pures utilisant les seuils de l'instance
    # -----------------------------------------------------------------------

    def _adx_strength_label(self, adx_val: float) -> str:
        if adx_val >= self.adx_threshold_very_strong: return ADXStrength.VERY_STRONG.value
        if adx_val >= self.adx_threshold_strong:      return ADXStrength.STRONG.value
        if adx_val >= self.adx_threshold_trend:       return ADXStrength.MODERATE.value
        return ADXStrength.WEAK.value

    def _vr_type_label(self, vr: float) -> str:
        if vr > self.vr_momentum_threshold: return VRType.MOMENTUM.value
        if vr < self.vr_meanrev_threshold:  return VRType.MEAN_REVERT.value
        return VRType.RANDOM_WALK.value

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
            'version':                _VERSION,  # [v2.1.2 — FIX-REG-1] était '2.1.1' hardcodé
            'mode':                   self.mode,
            'adx_period':             self.adx_period,
            'vr_short_window':        self.vr_short_window,
            'vr_long_window':         self.vr_long_window,
            'adx_threshold_trend':    self.adx_threshold_trend,
            'adx_threshold_strong':   self.adx_threshold_strong,
            'adx_threshold_very_strong': self.adx_threshold_very_strong,
            'vr_momentum_threshold':  self.vr_momentum_threshold,
            'vr_meanrev_threshold':   self.vr_meanrev_threshold,
            'min_candles_required':   self.min_candles,
        }

    def __repr__(self) -> str:
        return (
            f"RegimeIndicator(mode={self.mode}, adx={self.adx_period}, "
            f"vr={self.vr_short_window}/{self.vr_long_window})"
        )


# ---------------------------------------------------------------------------
# Fonctions utilitaires standalone
# ---------------------------------------------------------------------------

def calculate_adx_simple(high: pd.Series, low: pd.Series,
                          close: pd.Series, period: int = 14
                          ) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """Calculer ADX/+DI/-DI sans instancier RegimeIndicator."""
    return _calc_adx(high, low, close, period)


def calculate_variance_ratio_simple(close: pd.Series, short_window: int = 5,
                                     long_window: int = 20) -> pd.Series:
    """Calculer le Variance Ratio sans instancier RegimeIndicator."""
    return _calc_variance_ratio(close, short_window, long_window)


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

__all__ = [
    'RegimeIndicator',
    'RegimeSnapshot',
    'RegimeComposite',
    'ADXStrength',
    'VRType',
    'calculate_adx_simple',
    'calculate_variance_ratio_simple',
]

# FIN DU MODULE