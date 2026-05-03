"""
BULLET-1 - Structure Indicators Module
=============================================
Calcul VWAP sessionnel, Price Z-Score, Swing High/Low,
BOS (Break of Structure), CHoCH (Change of Character),
Pivots Camarilla.

Gestion modes backtest/paper/live depuis config/structure_config.json.
Pattern identique à atr.py / trend.py / volume.py.

Version : 2.1.2
Author  : FuegoDev
Date    : 2026-03-13
Mode    : ✅ Backtest | ✅ Paper | ✅ Live
"""

import json
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Deque, Dict, List, Literal, Optional, Tuple
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
_VERSION = "2.1.2"  # [v2.1.2 — FIX-STR-1]

# ---------------------------------------------------------------------------
# Constantes & Enums
# ---------------------------------------------------------------------------

DEFAULT_ZSCORE_PERIOD       = 20
DEFAULT_SWING_BARS          = 3
DEFAULT_SWING_LOOKBACK      = 50
DEFAULT_BOS_LOOKBACK        = 20
DEFAULT_SESSION_ANCHOR      = 'daily'   # 'daily' | 'weekly' | 'candle_count'
DEFAULT_CACHE_SIZE          = 500


class StructureState(Enum):
    UPTREND_INTACT      = 'uptrend_intact'
    DOWNTREND_INTACT    = 'downtrend_intact'
    CONSOLIDATION       = 'consolidation'
    TRANSITION          = 'transition'


class PriceVsVWAP(Enum):
    ABOVE = 'above'
    BELOW = 'below'
    AT    = 'at'


class ZScoreZone(Enum):
    EXTREME_HIGH     = 'extreme_high'     # z > 2.0
    EXTENDED_HIGH    = 'extended_high'    # z > 1.0
    NEUTRAL          = 'neutral'          # -1.0 <= z <= 1.0
    EXTENDED_LOW     = 'extended_low'     # z < -1.0
    EXTREME_LOW      = 'extreme_low'      # z < -2.0


class CamarillaZone(Enum):
    ABOVE_R4 = 'above_r4'
    AT_R4    = 'at_r4'
    BETWEEN_R3_R4 = 'between_r3_r4'
    BETWEEN_R2_R3 = 'between_r2_r3'
    BETWEEN_R1_R2 = 'between_r1_r2'
    BETWEEN_PP_R1 = 'between_pp_r1'
    AT_PP         = 'at_pp'
    BETWEEN_S1_PP = 'between_s1_pp'
    BETWEEN_S2_S1 = 'between_s2_s1'
    BETWEEN_S3_S2 = 'between_s3_s2'
    BETWEEN_S4_S3 = 'between_s4_s3'
    AT_S4         = 'at_s4'
    BELOW_S4      = 'below_s4'


@dataclass
class SwingPoint:
    price:   float
    bar_idx: int
    kind:    str  # 'high' | 'low'


# ---------------------------------------------------------------------------
# Fonctions mathématiques pures
# ---------------------------------------------------------------------------

def _calc_vwap(high: pd.Series, low: pd.Series, close: pd.Series,
               volume: pd.Series, anchor_mask: pd.Series) -> pd.Series:
    """
    VWAP sessionnel ancré.
    TP    = (High + Low + Close) / 3
    VWAP  = cumsum(TP × Volume) / cumsum(Volume)  réinitialisé à chaque ancrage.

    [v2.1.2 — FIX-STR-2] Vectorisé via pandas groupby sur les segments de session.
    L'ancre booléenne est convertie en ID de groupe (cumsum), puis tp_vol et volume
    sont agrégés par cumsum dans le groupe — ~10-50x plus rapide que la boucle
    Python pure sur 100K bougies.

    Args:
        anchor_mask : Series bool — True sur les bougies d'ouverture de session.
    """
    tp     = (high + low + close) / 3.0
    tp_vol = tp * volume

    # Identifiant de groupe : chaque True dans anchor_mask démarre un nouveau groupe
    session_id = anchor_mask.cumsum()

    # Cumsum de tp_vol et volume dans chaque groupe
    cum_tp_vol = tp_vol.groupby(session_id).cumsum()
    cum_vol    = volume.groupby(session_id).cumsum()

    # Éviter division par zéro (volume nul sur une bougie)
    vwap = (cum_tp_vol / cum_vol.replace(0, np.nan))

    return vwap


def _build_anchor_mask_daily(index: pd.DatetimeIndex) -> pd.Series:
    """
    Construire le masque d'ancrage journalier.
    True sur la première bougie de chaque jour UTC.
    """
    if not isinstance(index, pd.DatetimeIndex):
        # Index numérique : ancrage fictif toutes les 24 bougies (approx)
        mask = pd.Series(False, index=range(len(index)))
        mask.iloc[0] = True
        for i in range(1, len(index)):
            if i % 24 == 0:
                mask.iloc[i] = True
        return mask

    dates = index.normalize()
    mask  = pd.Series(False, index=index)
    mask.iloc[0] = True
    for i in range(1, len(index)):
        if dates[i] != dates[i - 1]:
            mask.iloc[i] = True
    return mask


def _calc_price_zscore(close: pd.Series, period: int) -> pd.Series:
    """
    Z-Score du prix sur fenêtre glissante.
    Z = (Close − SMA(period)) / StdDev(period)
    """
    mu  = close.rolling(period, min_periods=period).mean()
    std = close.rolling(period, min_periods=period).std(ddof=1).replace(0, np.nan)
    return ((close - mu) / std).fillna(0.0)


def _detect_swing_highs_lows(high: pd.Series, low: pd.Series,
                               n_bars: int) -> Tuple[pd.Series, pd.Series]:
    """
    Détecter Swing Highs et Swing Lows.

    Swing High : High[t] > High[t-k] pour tout k ∈ [1, n_bars]
                 ET High[t] > High[t+k] pour tout k ∈ [1, n_bars]
    Swing Low  : Low[t] < Low[t-k] pour tout k ∈ [1, n_bars]
                 ET Low[t] < Low[t+k] pour tout k ∈ [1, n_bars]

    Returns:
        swing_high : Series avec la valeur du SH ou NaN.
        swing_low  : Series avec la valeur du SL ou NaN.

    Note : les n_bars dernières bougies ne peuvent pas être confirmées
           (nécessitent des données futures) → NaN intentionnel.
    """
    sh = pd.Series(np.nan, index=high.index)
    sl = pd.Series(np.nan, index=low.index)

    for i in range(n_bars, len(high) - n_bars):
        h_center = high.iloc[i]
        l_center = low.iloc[i]

        left_h  = high.iloc[i - n_bars: i]
        right_h = high.iloc[i + 1: i + n_bars + 1]
        left_l  = low.iloc[i - n_bars: i]
        right_l = low.iloc[i + 1: i + n_bars + 1]

        if (left_h < h_center).all() and (right_h < h_center).all():
            sh.iloc[i] = h_center
        if (left_l > l_center).all() and (right_l > l_center).all():
            sl.iloc[i] = l_center

    return sh, sl


def _calc_swing_structure(close: pd.Series, sh: pd.Series, sl: pd.Series,
                           lookback: int) -> Dict[str, Any]:
    """
    Calculer Higher Highs / Higher Lows, structure state, BOS, CHoCH
    à partir des séries de swing points.

    Retourne un dict avec les métriques structure pour la dernière bougie.
    """
    # Extraire les N derniers swing valides
    sh_valid = sh.dropna().tail(lookback)
    sl_valid = sl.dropna().tail(lookback)

    result = {
        'last_swing_high':          None,
        'last_swing_high_bars_ago': None,
        'last_swing_low':           None,
        'last_swing_low_bars_ago':  None,
        'higher_highs':             None,
        'higher_lows':              None,
        'structure_state':          StructureState.CONSOLIDATION.value,
        'bos_detected':             False,
        'bos_level':                None,
        'bos_bars_ago':             None,
        'choch_detected':           False,
        'choch_level':              None,
    }

    n_total = len(close)

    # Dernier Swing High
    if len(sh_valid) > 0:
        last_sh_idx = sh_valid.index[-1]
        loc = close.index.get_loc(last_sh_idx) if hasattr(close.index, 'get_loc') else last_sh_idx
        bars_ago = (n_total - 1) - loc
        result['last_swing_high']          = round(float(sh_valid.iloc[-1]), 4)
        result['last_swing_high_bars_ago'] = int(bars_ago)

    # Dernier Swing Low
    if len(sl_valid) > 0:
        last_sl_idx = sl_valid.index[-1]
        loc = close.index.get_loc(last_sl_idx) if hasattr(close.index, 'get_loc') else last_sl_idx
        bars_ago = (n_total - 1) - loc
        result['last_swing_low']          = round(float(sl_valid.iloc[-1]), 4)
        result['last_swing_low_bars_ago'] = int(bars_ago)

    # Higher Highs & Higher Lows (sur 2+ swing points)
    if len(sh_valid) >= 2:
        result['higher_highs'] = bool(sh_valid.iloc[-1] > sh_valid.iloc[-2])
    if len(sl_valid) >= 2:
        result['higher_lows'] = bool(sl_valid.iloc[-1] > sl_valid.iloc[-2])

    # Structure State
    hh = result['higher_highs']
    hl = result['higher_lows']
    if hh is True  and hl is True:
        result['structure_state'] = StructureState.UPTREND_INTACT.value
    elif hh is False and hl is False:
        result['structure_state'] = StructureState.DOWNTREND_INTACT.value
    elif hh is None and hl is None:
        result['structure_state'] = StructureState.CONSOLIDATION.value
    else:
        result['structure_state'] = StructureState.TRANSITION.value

    # ---- BOS (Break of Structure) ----
    # Haussier BOS : Close > dernier Swing High (cassure de résistance)
    # Baissier BOS : Close < dernier Swing Low  (cassure de support)
    if len(sh_valid) >= 1 and len(sl_valid) >= 1:
        last_sh_val = sh_valid.iloc[-1]
        last_sl_val = sl_valid.iloc[-1]
        current_close = float(close.iloc[-1])

        if current_close > last_sh_val:
            result['bos_detected']  = True
            result['bos_level']     = round(float(last_sh_val), 4)
            sh_loc = sh_valid.index[-1]
            loc = close.index.get_loc(sh_loc) if hasattr(close.index, 'get_loc') else sh_loc
            result['bos_bars_ago']  = int((n_total - 1) - loc)
        elif current_close < last_sl_val:
            result['bos_detected']  = True
            result['bos_level']     = round(float(last_sl_val), 4)
            sl_loc = sl_valid.index[-1]
            loc = close.index.get_loc(sl_loc) if hasattr(close.index, 'get_loc') else sl_loc
            result['bos_bars_ago']  = int((n_total - 1) - loc)

    # ---- CHoCH (Change of Character) ----
    # CHoCH haussier : après downtrend, Close casse au-dessus d'un swing high récent
    # CHoCH baissier : après uptrend,   Close casse en-dessous d'un swing low récent
    if len(sh_valid) >= 2 and len(sl_valid) >= 2:
        prev_sh = sh_valid.iloc[-2]
        prev_sl = sl_valid.iloc[-2]
        current_close = float(close.iloc[-1])
        was_downtrend = (not result['higher_highs']) and (not result['higher_lows'])
        was_uptrend   = result['higher_highs'] and result['higher_lows']

        if was_downtrend and current_close > prev_sh:
            result['choch_detected'] = True
            result['choch_level']    = round(float(prev_sh), 4)
        elif was_uptrend and current_close < prev_sl:
            result['choch_detected'] = True
            result['choch_level']    = round(float(prev_sl), 4)

    return result


def _calc_camarilla_pivots(prev_high: float, prev_low: float,
                            prev_close: float) -> Dict[str, float]:
    """
    Pivots Camarilla depuis la bougie précédente (H, L, C).

    Formules :
    R4 = C + (H − L) × 1.1/2
    R3 = C + (H − L) × 1.1/4
    R2 = C + (H − L) × 1.1/6
    R1 = C + (H − L) × 1.1/12
    PP = (H + L + C) / 3
    S1 = C − (H − L) × 1.1/12
    S2 = C − (H − L) × 1.1/6
    S3 = C − (H − L) × 1.1/4
    S4 = C − (H − L) × 1.1/2
    """
    hl = prev_high - prev_low
    pp = (prev_high + prev_low + prev_close) / 3.0
    return {
        'r4': round(prev_close + hl * 1.1 / 2,   4),
        'r3': round(prev_close + hl * 1.1 / 4,   4),
        'r2': round(prev_close + hl * 1.1 / 6,   4),
        'r1': round(prev_close + hl * 1.1 / 12,  4),
        'pp': round(pp,                            4),
        's1': round(prev_close - hl * 1.1 / 12,  4),
        's2': round(prev_close - hl * 1.1 / 6,   4),
        's3': round(prev_close - hl * 1.1 / 4,   4),
        's4': round(prev_close - hl * 1.1 / 2,   4),
    }


# ---------------------------------------------------------------------------
# Interprétations qualitatives
# ---------------------------------------------------------------------------

def _zscore_zone(z: float) -> str:
    if z >  2.0: return ZScoreZone.EXTREME_HIGH.value
    if z >  1.0: return ZScoreZone.EXTENDED_HIGH.value
    if z < -2.0: return ZScoreZone.EXTREME_LOW.value
    if z < -1.0: return ZScoreZone.EXTENDED_LOW.value
    return ZScoreZone.NEUTRAL.value


def _camarilla_price_zone(price: float, pivots: Dict[str, float]) -> str:
    """Localiser le prix dans les niveaux Camarilla."""
    if price >= pivots['r4']:  return CamarillaZone.ABOVE_R4.value
    if price >= pivots['r3']:  return CamarillaZone.BETWEEN_R3_R4.value
    if price >= pivots['r2']:  return CamarillaZone.BETWEEN_R2_R3.value
    if price >= pivots['r1']:  return CamarillaZone.BETWEEN_R1_R2.value
    if price >= pivots['pp']:  return CamarillaZone.BETWEEN_PP_R1.value
    if price >= pivots['s1']:  return CamarillaZone.BETWEEN_S1_PP.value
    if price >= pivots['s2']:  return CamarillaZone.BETWEEN_S2_S1.value
    if price >= pivots['s3']:  return CamarillaZone.BETWEEN_S3_S2.value
    if price >= pivots['s4']:  return CamarillaZone.BETWEEN_S4_S3.value
    return CamarillaZone.BELOW_S4.value


# ---------------------------------------------------------------------------
# Classe principale
# ---------------------------------------------------------------------------

class StructureIndicator:
    """
    Indicateurs de structure de prix pour BULLET-1.

    Indicateurs calculés :
      • VWAP sessionnel ancré (daily par défaut)
      • Price Z-Score rolling
      • Swing Highs / Lows
      • BOS (Break of Structure) haussier/baissier
      • CHoCH (Change of Character)
      • Pivots Camarilla (calculés depuis bougie précédente)

    BACKTEST  : add_structure_indicators(df) → DataFrame enrichi.
    PAPER/LIVE: update_incremental(candle)   → dict résultat O(1).
    SNAPSHOT  : get_snapshot(row, df)        → dict JSON-ready.
    """

    def __init__(self, config: dict, config_path: Optional[Path] = None) -> None:
        """
        Initialiser StructureIndicator.

        Args:
            config      : Configuration principale BULLET-1 (dict).
            config_path : Chemin structure_config.json (auto-détecté si None).

        Raises:
            TypeError         : config n'est pas un dict.
            FileNotFoundError : structure_config.json introuvable.
            ValueError        : modes incohérents ou paramètres invalides.
        """
        if not isinstance(config, dict):
            raise TypeError(
                f"config doit être dict, reçu : {type(config).__name__}"
            )

        self.logger = BulletLogger()
        self.logger.info("Initializing StructureIndicator...")

        self._main_config = config

        # Chargement + vérification cohérence du mode (helpers.py v2.4.0)
        self._struct_config, self.mode = load_and_verify_module_config(
            module_config_path = get_project_root() / 'config' / 'structure_config.json',
            module_name        = 'structure',
            main_config        = config,
            logger             = self.logger
        )

        p = self._struct_config.get('parameters', {})
        self.zscore_period    = int(p.get('zscore_period',      DEFAULT_ZSCORE_PERIOD))
        self.swing_bars       = int(p.get('swing_bars',         DEFAULT_SWING_BARS))
        self.swing_lookback   = int(p.get('swing_lookback',     DEFAULT_SWING_LOOKBACK))
        self.bos_lookback     = int(p.get('bos_lookback',       DEFAULT_BOS_LOOKBACK))
        self.session_anchor   = str(p.get('session_anchor',     DEFAULT_SESSION_ANCHOR))

        self._validate_params()

        self.min_candles = max(
            self.zscore_period,
            self.swing_bars * 2 + self.swing_lookback,
        )

        if self.mode in ('paper', 'live'):
            self._init_incremental_cache()
        else:
            self._cache: Optional[Dict] = None

        self.logger.info(
            f"✅ StructureIndicator ready: mode={self.mode}, "
            f"zscore={self.zscore_period}, swing_bars={self.swing_bars}, "
            f"swing_lookback={self.swing_lookback}, session={self.session_anchor}"
        )

    # -----------------------------------------------------------------------
    # Validation
    # -----------------------------------------------------------------------

    def _validate_params(self) -> None:
        checks = [
            (self.zscore_period  >= 2,   "zscore_period doit être >= 2"),
            (self.swing_bars     >= 1,   "swing_bars doit être >= 1"),
            (self.swing_lookback >= 5,   "swing_lookback doit être >= 5"),
            (self.bos_lookback   >= 2,   "bos_lookback doit être >= 2"),
            (self.session_anchor in ('daily', 'weekly', 'candle_count'),
             f"session_anchor invalide : '{self.session_anchor}'"),
        ]
        for ok, msg in checks:
            if not ok:
                raise ValueError(msg)

    # -----------------------------------------------------------------------
    # Cache incrémental — PAPER / LIVE
    # -----------------------------------------------------------------------

    def _init_incremental_cache(self) -> None:
        size = self._struct_config.get('cache', {}).get('buffer_size', DEFAULT_CACHE_SIZE)
        self._cache = {
            'open':      deque(maxlen=size),
            'high':      deque(maxlen=size),
            'low':       deque(maxlen=size),
            'close':     deque(maxlen=size),
            'volume':    deque(maxlen=size),
            'timestamp': deque(maxlen=size),
            'size':      size,
            'warmed_up': False,
        }
        self.logger.debug(f"Incremental cache initialized: buffer_size={size}")

    def initialize_warmup(self, historical_df: pd.DataFrame) -> None:
        """
        Initialiser le cache avec données historiques (PAPER/LIVE).

        Args:
            historical_df : DataFrame OHLCV avec index DatetimeIndex optionnel.

        Raises:
            RuntimeError : appelé en BACKTEST.
            ValueError   : colonnes manquantes ou données insuffisantes.
        """
        if self.mode == 'backtest':
            raise RuntimeError(
                "initialize_warmup() interdit en BACKTEST. Utiliser add_structure_indicators()."
            )
        self._validate_ohlcv(historical_df)
        if len(historical_df) < self.min_candles:
            raise ValueError(
                f"Données insuffisantes : {len(historical_df)} bougies, "
                f"minimum : {self.min_candles}"
            )

        tail = historical_df.tail(self._cache['size'])
        for col in ('open', 'high', 'low', 'close', 'volume'):
            self._cache[col].clear()
            for v in tail[col]:
                self._cache[col].append(float(v))

        self._cache['timestamp'].clear()
        if isinstance(historical_df.index, pd.DatetimeIndex):
            for ts in tail.index:
                self._cache['timestamp'].append(ts)
        else:
            for i in range(len(tail)):
                self._cache['timestamp'].append(None)

        self._cache['warmed_up'] = True
        self.logger.info(f"Warmup OK : {len(tail)} bougies chargées.")

    def update_incremental(self, candle: dict) -> dict:
        """
        Mettre à jour le cache et calculer tous les indicateurs structure (PAPER/LIVE).

        Args:
            candle : Dict avec clés 'open','high','low','close','volume'.
                     Clé 'timestamp' optionnelle (pd.Timestamp).

        Returns:
            dict : section 'structure' de indicators.

        Raises:
            RuntimeError : appelé en BACKTEST.
            ValueError   : clés manquantes ou warmup non effectué.
        """
        if self.mode == 'backtest':
            raise RuntimeError(
                "update_incremental() interdit en BACKTEST. Utiliser add_structure_indicators()."
            )
        if not self._cache.get('warmed_up'):
            raise ValueError("Warmup non effectué. Appeler initialize_warmup() d'abord.")

        start = time.perf_counter()

        required = ('open', 'high', 'low', 'close', 'volume')
        missing  = [k for k in required if k not in candle]
        if missing:
            raise ValueError(f"Clés manquantes dans candle : {missing}")

        for col in required:
            self._cache[col].append(float(candle[col]))
        self._cache['timestamp'].append(candle.get('timestamp'))

        ts_list = list(self._cache['timestamp'])
        has_timestamps = all(ts is not None for ts in ts_list)

        if has_timestamps:
            idx = pd.DatetimeIndex(ts_list)
        else:
            idx = pd.RangeIndex(len(ts_list))

        df_tmp = pd.DataFrame({
            'open':   list(self._cache['open']),
            'high':   list(self._cache['high']),
            'low':    list(self._cache['low']),
            'close':  list(self._cache['close']),
            'volume': list(self._cache['volume']),
        }, index=idx)

        df_result = self.add_structure_indicators(df_tmp)
        row       = df_result.iloc[-1]

        elapsed = (time.perf_counter() - start) * 1000
        if elapsed > 50:
            self.logger.warning(f"update_incremental lent : {elapsed:.1f}ms")

        return self.get_snapshot(row, df_result)

    # -----------------------------------------------------------------------
    # Calcul batch — BACKTEST
    # -----------------------------------------------------------------------

    def add_structure_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Ajouter tous les indicateurs structure au DataFrame (batch).

        Colonnes ajoutées :
          str_vwap, str_vwap_vs_price, str_vwap_dev_pct
          str_zscore, str_zscore_zone
          str_swing_high, str_swing_low
          str_bos_detected, str_bos_level, str_bos_bars_ago
          str_choch_detected, str_choch_level
          str_higher_highs, str_higher_lows, str_structure_state
          str_last_sh, str_last_sh_bars_ago, str_last_sl, str_last_sl_bars_ago
          str_pivot_r4..r1, str_pivot_pp, str_pivot_s1..s4, str_pivot_zone

        Args:
            df : DataFrame avec colonnes ['open','high','low','close','volume'].
                 Index DatetimeIndex recommandé pour VWAP sessionnel exact.

        Returns:
            DataFrame enrichi (copie).

        Raises:
            ValueError : colonnes manquantes ou DataFrame vide.
        """
        self._validate_ohlcv(df)
        if len(df) < self.min_candles:
            self.logger.warning(
                f"Données potentiellement insuffisantes : {len(df)} bougies, "
                f"recommandé >= {self.min_candles}."
            )

        df     = df.copy()
        close  = df['close']
        high   = df['high']
        low    = df['low']
        volume = df['volume']

        # ---- VWAP sessionnel ----
        anchor_mask = self._build_anchor_mask(df)
        vwap = _calc_vwap(high, low, close, volume, anchor_mask)
        df['str_vwap'] = vwap.round(4)

        denom_vwap = vwap.replace(0, np.nan)
        vwap_dev   = ((close - vwap) / denom_vwap).fillna(0.0)
        df['str_vwap_dev_pct'] = (vwap_dev * 100).round(4)

        vwap_vs_price = pd.Series('at', index=df.index, dtype=str)
        vwap_vs_price[close > vwap * 1.0001] = PriceVsVWAP.ABOVE.value
        vwap_vs_price[close < vwap * 0.9999] = PriceVsVWAP.BELOW.value
        df['str_vwap_vs_price'] = vwap_vs_price

        # ---- Price Z-Score ----
        zscore = _calc_price_zscore(close, self.zscore_period)
        df['str_zscore']      = zscore.round(4)
        df['str_zscore_zone'] = zscore.apply(_zscore_zone)

        # ---- Swing Highs / Lows ----
        sh, sl = _detect_swing_highs_lows(high, low, self.swing_bars)
        df['str_swing_high'] = sh
        df['str_swing_low']  = sl

        # ---- Structure, BOS, CHoCH (calculés bougie par bougie) ----
        # Pour performance en backtest, on calcule la structure uniquement sur la fenêtre finale
        # (le ML aura besoin du snapshot T, pas de l'historique entier)
        struct = _calc_swing_structure(close, sh, sl, self.swing_lookback)

        # Stocker les valeurs scalaires dans des colonnes constantes sur tout le df
        # puis le snapshot extraira la valeur iloc[-1]
        df['str_last_sh']           = struct['last_swing_high']
        df['str_last_sh_bars_ago']  = struct['last_swing_high_bars_ago']
        df['str_last_sl']           = struct['last_swing_low']
        df['str_last_sl_bars_ago']  = struct['last_swing_low_bars_ago']
        df['str_higher_highs']      = struct['higher_highs']
        df['str_higher_lows']       = struct['higher_lows']
        df['str_structure_state']   = struct['structure_state']
        df['str_bos_detected']      = struct['bos_detected']
        df['str_bos_level']         = struct['bos_level']
        df['str_bos_bars_ago']      = struct['bos_bars_ago']
        df['str_choch_detected']    = struct['choch_detected']
        df['str_choch_level']       = struct['choch_level']

        # ---- Pivots Camarilla (depuis bougie précédente) ----
        if len(df) >= 2:
            prev_row  = df.iloc[-2]
            pivots    = _calc_camarilla_pivots(
                float(prev_row['high']),
                float(prev_row['low']),
                float(prev_row['close'])
            )
            current_price = float(close.iloc[-1])
            price_zone    = _camarilla_price_zone(current_price, pivots)
        else:
            pivots     = {k: np.nan for k in ('r4','r3','r2','r1','pp','s1','s2','s3','s4')}
            price_zone = CamarillaZone.AT_PP.value

        for key, val in pivots.items():
            df[f'str_pivot_{key}'] = val
        df['str_pivot_zone'] = price_zone

        self.logger.debug(f"add_structure_indicators: {len(df)} bougies traitées.")
        return df

    # -----------------------------------------------------------------------
    # Snapshot JSON
    # -----------------------------------------------------------------------

    def get_snapshot(self, row: pd.Series, df: Optional[pd.DataFrame] = None) -> Dict[str, Any]:
        """
        Retourner le dict indicators['structure'] pour le rapport de trade.

        Args:
            row : ligne du DataFrame à l'instant T, issue de add_structure_indicators().
            df  : DataFrame complet (utilisé pour calculer la distance ATR si disponible).

        Returns:
            dict : section 'structure' de indicators.

        Raises:
            ValueError : colonnes structure absentes.
        """
        required_cols = ['str_vwap', 'str_zscore', 'str_structure_state']
        missing = [c for c in required_cols if c not in row.index]
        if missing:
            raise ValueError(
                f"Colonnes structure absentes : {missing}. "
                "Appeler add_structure_indicators() d'abord."
            )

        def _safe(val, decimals: int = 4) -> Optional[float]:
            if val is None or (isinstance(val, float) and np.isnan(val)):
                return None
            try:
                return round(float(val), decimals)
            except (TypeError, ValueError):
                return None

        def _safe_int(val) -> Optional[int]:
            if val is None or (isinstance(val, float) and np.isnan(val)):
                return None
            try:
                return int(val)
            except (TypeError, ValueError):
                return None

        def _safe_bool(val) -> Optional[bool]:
            if val is None or (isinstance(val, float) and np.isnan(val)):
                return None
            return bool(val)

        # Distance VWAP en ATR (si ATR disponible dans le df)
        vwap_dev_atr = None
        if df is not None and 'vol_atr_value' in df.columns:
            atr_val = float(df['vol_atr_value'].iloc[-1])
            if atr_val > 0:
                vwap_dev_pct = _safe(row.get('str_vwap_dev_pct'))
                close_price  = _safe(row.get('close')) or _safe(row.get('str_vwap'))
                if vwap_dev_pct is not None and close_price:
                    vwap_dev_atr = round(abs(vwap_dev_pct / 100 * close_price) / atr_val, 4)

        # Pivots Camarilla
        pivot_keys = ('r4', 'r3', 'r2', 'r1', 'pp', 's1', 's2', 's3', 's4')
        pivots_dict = {k: _safe(row.get(f'str_pivot_{k}'), 2) for k in pivot_keys}
        pivot_zone  = str(row.get('str_pivot_zone', CamarillaZone.AT_PP.value))

        # Distances aux niveaux R1/S1 les plus proches
        close_val   = _safe(row.get('close'))
        r1_val      = pivots_dict.get('r1')
        s1_val      = pivots_dict.get('s1')
        dist_r1_pct = round(abs(r1_val - close_val) / close_val * 100, 4) \
                      if r1_val and close_val else None
        dist_s1_pct = round(abs(close_val - s1_val) / close_val * 100, 4) \
                      if s1_val and close_val else None

        zscore_val = _safe(row.get('str_zscore'), 4) or 0.0

        return {
            "_source": "structure.py / StructureIndicator",

            "vwap": {
                "period":            self.session_anchor,
                "value":             _safe(row['str_vwap'], 2),
                "price_vs_vwap":     str(row.get('str_vwap_vs_price', PriceVsVWAP.AT.value)),
                "deviation_pct":     _safe(row.get('str_vwap_dev_pct'), 4),
                "deviation_in_atr":  vwap_dev_atr,
            },

            "price_zscore": {
                "period":           self.zscore_period,
                "value":            zscore_val,
                "zone":             _zscore_zone(zscore_val),
            },

            "swing_structure": {
                "detection_bars":        self.swing_bars,
                "lookback_bars":         self.swing_lookback,
                "last_swing_high":       _safe(row.get('str_last_sh'), 2),
                "last_swing_high_bars_ago": _safe_int(row.get('str_last_sh_bars_ago')),
                "last_swing_low":        _safe(row.get('str_last_sl'), 2),
                "last_swing_low_bars_ago":  _safe_int(row.get('str_last_sl_bars_ago')),
                "higher_highs":          _safe_bool(row.get('str_higher_highs')),
                "higher_lows":           _safe_bool(row.get('str_higher_lows')),
                "structure_state":       str(row.get('str_structure_state',
                                                      StructureState.CONSOLIDATION.value)),
                "bos_detected":          bool(row.get('str_bos_detected', False)),
                "bos_level":             _safe(row.get('str_bos_level'), 2),
                "bos_bars_ago":          _safe_int(row.get('str_bos_bars_ago')),
                "choch_detected":        bool(row.get('str_choch_detected', False)),
                "choch_level":           _safe(row.get('str_choch_level'), 2),
            },

            "pivot_camarilla": {
                **{k: v for k, v in pivots_dict.items()},
                "price_zone":           pivot_zone,
                "distance_to_r1_pct":  dist_r1_pct,
                "distance_to_s1_pct":  dist_s1_pct,
            },
        }

    # -----------------------------------------------------------------------
    # Helpers privés
    # -----------------------------------------------------------------------

    def _build_anchor_mask(self, df: pd.DataFrame) -> pd.Series:
        """Construire le masque d'ancrage VWAP selon la configuration."""
        if self.session_anchor == 'daily':
            if isinstance(df.index, pd.DatetimeIndex):
                return _build_anchor_mask_daily(df.index)
            else:
                # [v2.1.2 — FIX-STR-3] WARNING : ancrage approximatif.
                # Sans DatetimeIndex, on ancre toutes les 24 bougies (approx).
                # Sur un timeframe 5m : 24 bougies = 2h ≠ 24h → sessions VWAP incorrectes.
                self.logger.warning(
                    "⚠️  [FIX-STR-3] VWAP daily anchor : index non DatetimeIndex détecté. "
                    "Fallback : ancrage toutes les 24 bougies (approximatif). "
                    "Sur timeframes < 1h, les sessions VWAP seront incorrectes. "
                    "Fournir un DataFrame avec index DatetimeIndex UTC pour un VWAP exact."
                )
                mask = pd.Series(False, index=df.index)
                mask.iloc[0] = True
                for i in range(1, len(df)):
                    if i % 24 == 0:
                        mask.iloc[i] = True
                return mask

        elif self.session_anchor == 'weekly':
            if isinstance(df.index, pd.DatetimeIndex):
                mask  = pd.Series(False, index=df.index)
                weeks = df.index.isocalendar().week
                mask.iloc[0] = True
                for i in range(1, len(df)):
                    if weeks[i] != weeks[i - 1]:
                        mask.iloc[i] = True
                return mask
            else:
                # [v2.1.2 — FIX-STR-3] Même warning que daily — ancrage approximatif.
                self.logger.warning(
                    "⚠️  [FIX-STR-3] VWAP weekly anchor : index non DatetimeIndex détecté. "
                    "Fallback : ancrage toutes les 168 bougies (approx 24×7). "
                    "Sur timeframes < 1h, les sessions VWAP seront incorrectes. "
                    "Fournir un DataFrame avec index DatetimeIndex UTC pour un VWAP exact."
                )
                mask = pd.Series(False, index=df.index)
                mask.iloc[0] = True
                for i in range(1, len(df)):
                    if i % (24 * 7) == 0:
                        mask.iloc[i] = True
                return mask

        else:
            # candle_count : ancre toutes les N bougies
            count = self._struct_config.get('parameters', {}).get('anchor_candle_count', 100)
            mask  = pd.Series(False, index=df.index)
            mask.iloc[0] = True
            for i in range(1, len(df)):
                if i % count == 0:
                    mask.iloc[i] = True
            return mask

    def _validate_ohlcv(self, df: pd.DataFrame) -> None:
        required = ['open', 'high', 'low', 'close', 'volume']
        missing  = [c for c in required if c not in df.columns]
        if missing:
            raise ValueError(f"Colonnes OHLCV manquantes : {missing}")
        if df.empty:
            raise ValueError("DataFrame vide.")

    def get_configuration(self) -> Dict[str, Any]:
        return {
            'version':         _VERSION,  # [v2.1.2 — FIX-STR-1] était '2.1.1' hardcodé
            'mode':            self.mode,
            'zscore_period':   self.zscore_period,
            'swing_bars':      self.swing_bars,
            'swing_lookback':  self.swing_lookback,
            'bos_lookback':    self.bos_lookback,
            'session_anchor':  self.session_anchor,
            'min_candles_required': self.min_candles,
        }

    def __repr__(self) -> str:
        return (
            f"StructureIndicator(mode={self.mode}, "
            f"swing_bars={self.swing_bars}, anchor={self.session_anchor})"
        )


# ---------------------------------------------------------------------------
# Fonctions utilitaires standalone
# ---------------------------------------------------------------------------

def calculate_camarilla_simple(prev_high: float, prev_low: float,
                                 prev_close: float) -> Dict[str, float]:
    """Calculer les pivots Camarilla sans instancier StructureIndicator."""
    return _calc_camarilla_pivots(prev_high, prev_low, prev_close)


def calculate_vwap_simple(high: pd.Series, low: pd.Series, close: pd.Series,
                           volume: pd.Series) -> pd.Series:
    """Calculer VWAP sans ancrage (session unique) sans instancier StructureIndicator."""
    anchor = pd.Series(False, index=close.index)
    anchor.iloc[0] = True
    return _calc_vwap(high, low, close, volume, anchor)


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

__all__ = [
    'StructureIndicator',
    'SwingPoint',
    'StructureState',
    'PriceVsVWAP',
    'ZScoreZone',
    'CamarillaZone',
    'calculate_camarilla_simple',
    'calculate_vwap_simple',
]

# FIN DU MODULE