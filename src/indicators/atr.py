"""
BULLET-1 - Module 19: ATR Indicator v2.3.4
==========================================
Calcul ATR, trailing stop dynamique, détection anomalies volatilité.
Gestion modes backtest/paper/live depuis config/atr_config.json.

Version: 2.3.4
Author: FuegoDev
"""

import json
import sys
from collections import deque
from contextlib import nullcontext
from pathlib import Path
from threading import Lock, RLock
from typing import Any, Dict, List, Literal, Optional, Tuple, Union

import numpy as np
import pandas as pd

current_file = Path(__file__).resolve()
project_root = current_file.parent.parent.parent
# [v2.3.4 — FIX-ATR-1] Guard ajouté — évite les doublons dans sys.path à chaque
# import. Pattern uniforme avec tous les autres modules BULLET-1.
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from src.utils.config_loader import BulletConfig
from src.utils.logger import BulletLogger


# ---------------------------------------------------------------------------
# Chargement et validation configuration
# ---------------------------------------------------------------------------

def load_atr_config(config_path: Optional[Path] = None) -> dict:
    """
    Charger config/atr_config.json.

    Args:
        config_path: Chemin explicite (auto-détecté si None).

    Raises:
        FileNotFoundError: Fichier introuvable.
        json.JSONDecodeError: JSON invalide.
    """
    if config_path is None:
        # [v2.3.4 — FIX-ATR-2] Recalcul local via __file__ au lieu du global
        # module-level `project_root`. Le global est calculé à l'import et peut
        # pointer au mauvais endroit si le module est importé depuis un répertoire
        # différent (tests, CI). Path(__file__) est toujours relatif au fichier
        # lui-même — stable quel que soit le répertoire de travail.
        _local_root = Path(__file__).resolve().parent.parent.parent
        path = _local_root / 'config' / 'atr_config.json'
    else:
        path = config_path

    if not path.exists():
        raise FileNotFoundError(
            f"Configuration ATR introuvable: {path}\n"
            "Créer config/atr_config.json avec la structure requise."
        )

    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def verify_mode_consistency(
    main_config: dict,
    atr_config: dict,
    logger: Optional[BulletLogger] = None
) -> None:
    """
    Vérifier que les modes de config.json et atr_config.json correspondent.

    Raises:
        ValueError: Modes incohérents (erreur fatale avec suggestion).
    """
    main_mode = main_config.get('general', {}).get('mode')
    atr_mode  = atr_config.get('general', {}).get('mode')

    if logger:
        logger.debug(f"Mode verification: config={main_mode}, atr_config={atr_mode}")

    if main_mode != atr_mode:
        msg = (
            f"\n{'='*70}\n"
            f"🔴 ERREUR FATALE: MODES INCOHÉRENTS\n"
            f"{'='*70}\n\n"
            f"  • config/config.json     → mode = '{main_mode}'\n"
            f"  • config/atr_config.json → mode = '{atr_mode}'\n\n"
            f"⚠️  Les deux fichiers DOIVENT avoir le même mode.\n\n"
            f"📝 SOLUTION:\n"
            f"  Option 1 → Ouvrir atr_config.json, passer mode='{main_mode}'\n"
            f"  Option 2 → Ouvrir config.json,     passer mode='{atr_mode}'\n\n"
            f"  Modes valides: 'backtest', 'paper', 'live'\n"
            f"{'='*70}\n"
        )
        if logger:
            logger.critical(msg)
        raise ValueError(msg)

    if logger:
        logger.info(f"✅ Mode consistency verified: {main_mode}")


# ---------------------------------------------------------------------------
# Constantes utilisées par la fonction standalone uniquement
# ---------------------------------------------------------------------------

DEFAULT_SMOOTHING_METHOD: Literal['ema', 'sma', 'rma'] = 'ema'


# ---------------------------------------------------------------------------
# Constantes publiques — source de vérité unique pour les consommateurs
#
# Chargées une seule fois depuis atr_config.json à l'import du module.
# Tout module ayant besoin de la période/méthode ATR par défaut doit
# importer ATR_DEFAULT_PERIOD et ATR_DEFAULT_METHOD depuis ce module,
# jamais les dupliquer en dur.
#
# Fallback sur les valeurs standards si atr_config.json est introuvable
# (ex: environnement de test sans fichier de config).
# ---------------------------------------------------------------------------

try:
    _module_atr_cfg    = load_atr_config()
    ATR_DEFAULT_PERIOD: int = int(
        _module_atr_cfg.get('atr_parameters', {}).get('period', 14)
    )
    ATR_DEFAULT_METHOD: Literal['ema', 'sma', 'rma'] = (
        _module_atr_cfg.get('atr_parameters', {}).get('smoothing_method', 'ema')
    )
    del _module_atr_cfg   # Libère la référence, les constantes sont extraites
except FileNotFoundError:
    ATR_DEFAULT_PERIOD = 14
    ATR_DEFAULT_METHOD = 'ema'


# ---------------------------------------------------------------------------
# Classe principale
# ---------------------------------------------------------------------------

class ATRIndicator:
    """
    ATR (Average True Range) thread-safe avec gestion modes backtest/paper/live.

    - BACKTEST : calcul batch, pas de cache, pas de détection temps réel.
    - PAPER/LIVE : cache incrémental O(1), Wilder's smoothing, détection
      spikes/crashs, recalibrage automatique, thread-safe.

    Tous les paramètres sont lus depuis config/atr_config.json.
    Le mode doit correspondre à config/config.json (vérifié au démarrage).
    """

    def __init__(self, config: Union[dict, BulletConfig]) -> None:
        """
        Initialiser ATRIndicator.

        Charge atr_config.json, vérifie la cohérence des modes,
        puis configure caches et paramètres selon le mode.

        Args:
            config: Configuration principale BULLET-1 (dict ou BulletConfig).

        Raises:
            TypeError: config n'est pas dict ou BulletConfig.
            FileNotFoundError: atr_config.json introuvable.
            ValueError: Modes incohérents, type trailing_stop invalide,
                        ou paramètre ATR hors limites.
        """
        self.logger = BulletLogger()

        if isinstance(config, BulletConfig):
            self.config = config.dict()
        elif isinstance(config, dict):
            self.config = config
        else:
            raise TypeError(
                f"config doit être BulletConfig ou dict, reçu: {type(config).__name__}"
            )

        # Chargement atr_config.json
        self.atr_config = load_atr_config()

        # Vérification cohérence modes
        verify_mode_consistency(self.config, self.atr_config, self.logger)

        # Thread-safety
        lock_cfg = self.atr_config.get('thread_safety', {})
        if lock_cfg.get('enable_thread_safety', True):
            self._lock = RLock() if lock_cfg.get('lock_type', 'RLock') == 'RLock' else Lock()
        else:
            self._lock = None

        # Mode
        self.mode = self.atr_config['general']['mode'].lower()
        if self.mode not in ('backtest', 'paper', 'live'):
            raise ValueError(
                f"Mode invalide dans atr_config.json: '{self.mode}'. "
                "Valeurs valides: 'backtest', 'paper', 'live'"
            )

        self.logger.info(f"ATRIndicator initializing in {self.mode.upper()} mode")

        # Validation trailing_stop.type
        trailing_config  = self.config['strategy']['trailing_stop']
        supported_types  = self.atr_config.get('trailing_stop', {}).get(
            'types_supported', ['atr', 'hybrid']
        )
        if trailing_config['type'] not in supported_types:
            raise ValueError(
                f"trailing_stop.type='{trailing_config['type']}' non supporté. "
                f"Valeurs valides: {supported_types}"
            )

        # Paramètres ATR
        atr_params = self.atr_config.get('atr_parameters', {})
        limits     = self.atr_config.get('validation_limits', {})

        self.period = atr_params.get('period', 14)
        min_p, max_p = limits.get('min_atr_period', 7), limits.get('max_atr_period', 50)
        if not (min_p <= self.period <= max_p):
            raise ValueError(
                f"period invalide dans atr_config.json: {self.period} "
                f"(attendu: {min_p}-{max_p})"
            )

        self.base_multiplier = atr_params.get('base_multiplier', 2.0)
        min_m, max_m = limits.get('min_base_multiplier', 0.5), limits.get('max_base_multiplier', 5.0)
        if not (min_m <= self.base_multiplier <= max_m):
            raise ValueError(
                f"base_multiplier invalide dans atr_config.json: {self.base_multiplier} "
                f"(attendu: {min_m}-{max_m})"
            )

        self.smoothing_method = atr_params.get('smoothing_method', 'ema')
        if self.smoothing_method not in ('ema', 'sma', 'rma'):
            raise ValueError(
                f"smoothing_method invalide: '{self.smoothing_method}'. "
                "Valeurs valides: 'ema', 'sma', 'rma'"
            )

        # Validation market conditions
        mc = self.atr_config.get('market_conditions', {})
        self.enable_validation = mc.get('enable_validation', True)
        self.min_volatility    = mc.get('min_volatility', 0.5)
        self.max_volatility    = mc.get('max_volatility', 5.0)

        # Détection anomalies
        ad = self.atr_config.get('anomaly_detection', {})
        self.enable_spike_detection  = ad.get('enable_spike_detection', True)
        self.spike_threshold         = ad.get('spike_threshold', 2.0)
        self.spike_min_samples       = ad.get('spike_min_samples', 10)
        self.enable_crash_detection  = ad.get('enable_crash_detection', True)
        self.crash_threshold         = ad.get('crash_threshold', 3.0)
        self.enable_auto_recalibrate = ad.get('enable_auto_recalibrate', True)
        self.recalibrate_threshold   = ad.get('recalibrate_threshold', 3.0)

        # Configuration caches selon mode
        self._configure_for_mode()

        # État interne
        self._current_atr: Optional[float] = None
        self._prev_close:  Optional[float] = None
        self._last_price:  Optional[float] = None

        # Statistiques (optionnel)
        if self.atr_config.get('statistics', {}).get('enable_statistics', True):
            self._update_count:     int = 0
            self._spike_count:      int = 0
            self._recalibrate_count: int = 0
        else:
            self._update_count = self._spike_count = self._recalibrate_count = None

        self.logger.info(
            f"ATRIndicator ready: mode={self.mode}, period={self.period}, "
            f"multiplier={self.base_multiplier}, smoothing={self.smoothing_method}, "
            f"cache={self._cache_enabled}"
        )

    def _configure_for_mode(self) -> None:
        """Initialiser les caches selon le mode actuel (depuis atr_config.json)."""
        cache_cfg = self.atr_config.get('cache_configuration', {})

        if self.mode == 'backtest':
            bc = cache_cfg.get('backtest', {})
            atr_size, tr_size = bc.get('atr_cache_size', 0), bc.get('tr_cache_size', 0)
            self._cache_enabled = False
            self._atr_history   = None if atr_size == 0 else deque(maxlen=atr_size)
            self._tr_cache      = None if tr_size  == 0 else deque(maxlen=tr_size)
            self.logger.debug(f"Cache disabled (BACKTEST): atr={atr_size}, tr={tr_size}")
        else:
            plc = cache_cfg.get('paper_live', {})
            atr_size, tr_size = plc.get('atr_cache_size', 100), plc.get('tr_cache_size', 50)
            self._cache_enabled = True
            self._atr_history   = deque(maxlen=atr_size)
            self._tr_cache      = deque(maxlen=tr_size)
            self.logger.debug(f"Cache enabled ({self.mode.upper()}): atr={atr_size}, tr={tr_size}")

    # -----------------------------------------------------------------------
    # Calcul ATR batch — MODE BACKTEST
    # -----------------------------------------------------------------------

    def calculate_atr(
        self,
        df: pd.DataFrame,
        period: Optional[int] = None
    ) -> pd.Series:
        """
        Calculer ATR sur un DataFrame complet.

        TR = max(High-Low, |High-Close_prev|, |Low-Close_prev|)
        ATR = EMA/SMA/RMA(TR, period)

        [v2.3.4 — FIX-ATR-3] Calcul TR vectorisé (numpy) — remplace apply()
        Python ligne par ligne qui était 10-50x plus lent sur les grands datasets.
        Implémentation identique à calculate_atr_simple() au niveau module.

        Args:
            df: DataFrame avec colonnes ['high', 'low', 'close'].
            period: Période ATR (self.period si None).

        Returns:
            pd.Series: Valeurs ATR.

        Raises:
            ValueError: Colonnes manquantes ou DataFrame vide.
        """
        required = ['high', 'low', 'close']
        missing  = [c for c in required if c not in df.columns]
        if missing:
            raise ValueError(f"Colonnes manquantes: {missing}")
        if df.empty:
            raise ValueError("DataFrame vide")

        p   = period if period is not None else self.period
        dfc = df.copy()

        # [v2.3.4 — FIX-ATR-3] Vectorisé : .abs() + .max(axis=1) au lieu de
        # .apply(lambda r: max(...), axis=1) — même résultat, ~10-50x plus rapide.
        dfc['prev_close'] = dfc['close'].shift(1)
        dfc['hl']  = dfc['high'] - dfc['low']
        dfc['hc']  = (dfc['high'] - dfc['prev_close']).abs()
        dfc['lc']  = (dfc['low']  - dfc['prev_close']).abs()
        dfc['tr']  = dfc[['hl', 'hc', 'lc']].max(axis=1)

        if self.smoothing_method == 'sma':
            atr = dfc['tr'].rolling(window=p).mean()
        elif self.smoothing_method == 'rma':
            atr = dfc['tr'].ewm(alpha=1.0 / p, adjust=False).mean()
        else:  # ema (défaut)
            atr = dfc['tr'].ewm(span=p, adjust=False).mean()

        self.logger.debug(f"ATR calculated: method={self.smoothing_method}, period={p}, rows={len(df)}")
        return atr

    # -----------------------------------------------------------------------
    # Update incrémental O(1) — MODE PAPER/LIVE
    # -----------------------------------------------------------------------

    def update_atr_incremental(self, candle: dict) -> Optional[float]:
        """
        Mettre à jour l'ATR de façon incrémentale (Wilder's smoothing).

        ATR_new = (ATR_old × (Period-1) + TR_new) / Period

        Args:
            candle: Dict avec clés 'high', 'low', 'close'.

        Returns:
            float: ATR actuel.

        Raises:
            RuntimeError: Appelé en MODE BACKTEST.
            ValueError: Clés manquantes dans candle.
        """
        if self.mode == 'backtest':
            raise RuntimeError(
                "update_atr_incremental() interdit en MODE BACKTEST. "
                "Utiliser calculate_atr()."
            )

        missing = [k for k in ('high', 'low', 'close') if k not in candle]
        if missing:
            raise ValueError(f"Clés manquantes dans candle: {missing}")

        with self._lock if self._lock else nullcontext():
            tr = self._calculate_true_range(candle)

            if self._tr_cache is not None:
                self._tr_cache.append(tr)

            if self._current_atr is None:
                if self._tr_cache and len(self._tr_cache) >= self.period:
                    self._current_atr = np.mean(list(self._tr_cache)[-self.period:])
                else:
                    self._current_atr = tr
            else:
                self._current_atr = (self._current_atr * (self.period - 1) + tr) / self.period

            if self._atr_history is not None:
                self._atr_history.append(self._current_atr)

            self._last_price = candle['close']
            self._prev_close = candle['close']

            if self._update_count is not None:
                self._update_count += 1

            return self._current_atr

    def _calculate_true_range(self, candle: dict) -> float:
        """Calculer le True Range d'une bougie."""
        high, low = candle['high'], candle['low']
        hl = high - low
        if self._prev_close is None:
            return hl
        return max(hl, abs(high - self._prev_close), abs(low - self._prev_close))

    # -----------------------------------------------------------------------
    # Validation market conditions — pour RiskManager
    # -----------------------------------------------------------------------

    def validate_market_conditions(
        self,
        current_atr:    Optional[float] = None,
        min_volatility: Optional[float] = None,
        max_volatility: Optional[float] = None,
        current_price:  Optional[float] = None
    ) -> Tuple[bool, str]:
        """
        Valider la volatilité pour le RiskManager.

        Utilise min/max_volatility de atr_config.json si non fournis.

        Args:
            current_atr:    ATR à valider (self._current_atr si None).
            min_volatility: Seuil min en % du prix.
            max_volatility: Seuil max en % du prix.
            current_price:  Prix de référence (self._last_price si None).

        Returns:
            (bool, str): (is_valid, raison).
        """
        if not self.enable_validation:
            return True, "OK (validation disabled)"

        min_vol = min_volatility if min_volatility is not None else self.min_volatility
        max_vol = max_volatility if max_volatility is not None else self.max_volatility

        with self._lock if self._lock else nullcontext():
            atr   = current_atr   if current_atr   is not None else self._current_atr
            price = current_price  if current_price  is not None else self._last_price

            if atr is None:
                return False, "ATR not calculated yet"
            if not price or price <= 0:
                return False, "Invalid current price"

            atr_pct = self.normalize_atr(atr, price)

            if atr_pct < min_vol:
                return False, f"Volatilité trop faible: {atr_pct:.2f}% < {min_vol}%"
            if atr_pct > max_vol:
                return False, f"Volatilité trop élevée: {atr_pct:.2f}% > {max_vol}%"

            return True, "OK"

    # -----------------------------------------------------------------------
    # Détection anomalies
    # -----------------------------------------------------------------------

    def detect_volatility_spike(
        self,
        threshold:   Optional[float] = None,
        min_samples: Optional[int]   = None
    ) -> bool:
        """
        Détecter un spike de volatilité (ATR > threshold × ATR_moyen).

        Args:
            threshold:   Multiplicateur (spike_threshold config si None).
            min_samples: Échantillons min (spike_min_samples config si None).

        Returns:
            bool: True si spike détecté.
        """
        if not self.enable_spike_detection:
            return False

        thresh   = threshold   if threshold   is not None else self.spike_threshold
        min_samp = min_samples if min_samples is not None else self.spike_min_samples

        with self._lock if self._lock else nullcontext():
            if not self._cache_enabled or self._atr_history is None:
                return False
            if len(self._atr_history) < min_samp or self._current_atr is None:
                return False

            is_spike = self._current_atr > thresh * np.mean(list(self._atr_history))

            if is_spike:
                if self._spike_count is not None:
                    self._spike_count += 1
                self.logger.warning(
                    f"🚨 Volatility spike #{self._spike_count}: "
                    f"ATR={self._current_atr:.4f} > {thresh}×mean"
                )

            return is_spike

    def detect_crash(
        self,
        candle:    dict,
        threshold: Optional[float] = None
    ) -> bool:
        """
        Détecter un crash (TR > threshold × ATR).

        Args:
            candle:    Bougie actuelle.
            threshold: Multiplicateur (crash_threshold config si None).

        Returns:
            bool: True si crash détecté.
        """
        if not self.enable_crash_detection:
            return False

        thresh = threshold if threshold is not None else self.crash_threshold

        with self._lock if self._lock else nullcontext():
            if self._current_atr is None or self._current_atr == 0:
                return False

            tr       = self._calculate_true_range(candle)
            is_crash = tr > thresh * self._current_atr

            if is_crash:
                self.logger.error(
                    f"🔴 Market crash: TR={tr:.4f} > {thresh}×ATR({self._current_atr:.4f})"
                )
            return is_crash

    # -----------------------------------------------------------------------
    # Recalibrage automatique
    # -----------------------------------------------------------------------

    def auto_recalibrate(
        self,
        threshold: Optional[float] = None,
        force:     bool = False
    ) -> bool:
        """
        Recalibrer l'ATR si la volatilité a changé drastiquement.

        Args:
            threshold: Seuil déclenchement (recalibrate_threshold config si None).
            force:     Forcer même sans spike.

        Returns:
            bool: True si recalibrage effectué.
        """
        if not self.enable_auto_recalibrate and not force:
            return False

        thresh = threshold if threshold is not None else self.recalibrate_threshold

        with self._lock if self._lock else nullcontext():
            if not force and not self.detect_volatility_spike(thresh):
                return False

            if self._recalibrate_count is not None:
                self._recalibrate_count += 1

            self.logger.warning(
                f"🔄 ATR recalibrage #{self._recalibrate_count}: "
                f"ATR={self._current_atr:.4f}, threshold={thresh}×mean"
            )

            if self._atr_history is not None:
                self._atr_history.clear()
            if self._tr_cache is not None:
                self._tr_cache.clear()
            self._current_atr = None

            self.logger.info("ATR recalibrated")
            return True

    # -----------------------------------------------------------------------
    # Normalisation ATR
    # -----------------------------------------------------------------------

    def normalize_atr(self, atr: float, current_price: float) -> float:
        """
        Normaliser l'ATR en % du prix (comparaison multi-symboles).

        Args:
            atr:           Valeur ATR absolue.
            current_price: Prix actuel.

        Returns:
            float: ATR en % du prix.
        """
        if current_price <= 0:
            self.logger.warning(f"Prix invalide pour normalisation: {current_price}")
            return 0.0
        return (atr / current_price) * 100

    def get_normalized_atr(self, current_price: Optional[float] = None) -> float:
        """Retourner l'ATR actuel normalisé en % du prix."""
        with self._lock if self._lock else nullcontext():
            if self._current_atr is None:
                return 0.0
            price = current_price if current_price is not None else self._last_price
            return self.normalize_atr(self._current_atr, price) if price else 0.0

    # -----------------------------------------------------------------------
    # Multi-périodes ATR
    # -----------------------------------------------------------------------

    def calculate_atr_multi_period(
        self,
        df:      pd.DataFrame,
        periods: List[int] = None
    ) -> Dict[str, pd.Series]:
        """
        Calculer l'ATR pour plusieurs périodes simultanément.

        Args:
            df:      DataFrame OHLC.
            periods: Liste de périodes (défaut: [14, 20, 50]).

        Returns:
            dict: {'atr_14': Series, 'atr_20': Series, ...}
        """
        periods = periods or [14, 20, 50]
        result  = {}

        for p in periods:
            limits   = self.atr_config.get('validation_limits', {})
            min_p    = limits.get('min_atr_period', 7)
            max_p    = limits.get('max_atr_period', 50)
            if not (min_p <= p <= max_p):
                self.logger.warning(f"Période ignorée (hors limites): {p}")
                continue
            result[f'atr_{p}'] = self.calculate_atr(df, period=p)

        return result

    # -----------------------------------------------------------------------
    # Trailing stop
    # -----------------------------------------------------------------------

    def get_trailing_distance(
        self,
        candle:          dict,
        historical_data: Optional[pd.DataFrame] = None,
        multiplier:      Optional[float] = None
    ) -> float:
        """
        Calculer la distance du trailing stop (ATR × multiplier).

        Args:
            candle:          Bougie actuelle.
            historical_data: Données historiques (MODE BACKTEST).
            multiplier:      Multiplicateur (base_multiplier si None).

        Returns:
            float: Distance trailing stop.
        """
        with self._lock if self._lock else nullcontext():
            if self.mode == 'backtest' and historical_data is not None:
                current_atr = self.calculate_atr(historical_data).iloc[-1]
            else:
                current_atr = self._current_atr

            if current_atr is None:
                self.logger.warning("ATR non disponible, fallback sur high-low")
                current_atr = candle.get('high', 0) - candle.get('low', 0)

            return current_atr * (multiplier if multiplier is not None else self.base_multiplier)

    def calculate_trailing_sl(
        self,
        direction:       Literal['LONG', 'SHORT'],
        entry_price:     float,
        candle:          dict,
        historical_data: Optional[pd.DataFrame] = None,
        multiplier:      Optional[float] = None
    ) -> float:
        """
        Calculer le niveau de stop loss trailing ATR.

        Args:
            direction:       'LONG' ou 'SHORT'.
            entry_price:     Prix d'entrée.
            candle:          Bougie actuelle.
            historical_data: Données historiques (MODE BACKTEST).
            multiplier:      Multiplicateur ATR.

        Returns:
            float: Niveau stop loss.
        """
        distance = self.get_trailing_distance(candle, historical_data, multiplier)
        return entry_price + distance if direction == 'SHORT' else entry_price - distance

    # -----------------------------------------------------------------------
    # Statistiques & état
    # -----------------------------------------------------------------------

    def get_volatility_stats(self) -> Dict[str, Any]:
        """Retourner les statistiques de volatilité pour monitoring."""
        with self._lock if self._lock else nullcontext():
            base = {
                'current_atr':            self._current_atr,
                'current_atr_normalized': self.get_normalized_atr(),
                'update_count':           self._update_count,
                'spike_count':            self._spike_count,
                'recalibrate_count':      self._recalibrate_count,
            }

            if not self._cache_enabled or not self._atr_history:
                return {**base, 'mean_atr': None, 'std_atr': None,
                        'min_atr': None, 'max_atr': None, 'spike_ratio': None}

            arr      = np.array(list(self._atr_history))
            mean_atr = arr.mean()
            return {
                **base,
                'mean_atr':   mean_atr,
                'std_atr':    arr.std(),
                'min_atr':    arr.min(),
                'max_atr':    arr.max(),
                'spike_ratio': self._current_atr / mean_atr if mean_atr > 0 else None,
                'cache_size':  len(self._atr_history),
                'cache_maxlen': self._atr_history.maxlen,
            }

    def get_current_state(self) -> Dict[str, Any]:
        """Retourner l'état complet pour sauvegarde/recovery."""
        with self._lock if self._lock else nullcontext():
            state = {
                'mode':             self.mode,
                'period':           self.period,
                'base_multiplier':  self.base_multiplier,
                'smoothing_method': self.smoothing_method,
                'current_atr':      self._current_atr,
                'prev_close':       self._prev_close,
                'last_price':       self._last_price,
                'update_count':     self._update_count,
                'spike_count':      self._spike_count,
                'recalibrate_count': self._recalibrate_count,
            }
            if self._cache_enabled:
                state['atr_history'] = list(self._atr_history) if self._atr_history else []
                state['tr_cache']    = list(self._tr_cache)    if self._tr_cache    else []
            return state

    def restore_state(self, state: dict) -> None:
        """
        Restaurer l'état depuis une sauvegarde.

        Args:
            state: Dict issu de get_current_state().

        Raises:
            ValueError: Période incompatible.
        """
        if state.get('period') != self.period:
            raise ValueError(
                f"Period mismatch: state={state.get('period')}, current={self.period}"
            )

        # [v2.3.4 — FIX-ATR-4] Validation du mode restauré.
        # Restaurer un state 'backtest' sur une instance 'live' (ou vice-versa)
        # produirait un _cache_enabled incohérent — le mode détermine si les
        # caches sont actifs (_configure_for_mode). On log un warning.
        state_mode = state.get('mode')
        if state_mode and state_mode != self.mode:
            self.logger.warning(
                f"⚠️  restore_state: mode mismatch — "
                f"state.mode='{state_mode}' vs instance.mode='{self.mode}'. "
                f"Les caches seront ceux de l'instance courante (mode={self.mode})."
            )

        with self._lock if self._lock else nullcontext():
            self._current_atr       = state.get('current_atr')
            self._prev_close        = state.get('prev_close')
            self._last_price        = state.get('last_price')
            self._update_count      = state.get('update_count', 0)
            self._spike_count       = state.get('spike_count', 0)
            self._recalibrate_count = state.get('recalibrate_count', 0)

            if self._cache_enabled:
                if self._atr_history is not None:
                    self._atr_history.clear()
                    for v in state.get('atr_history', []):
                        self._atr_history.append(v)
                if self._tr_cache is not None:
                    self._tr_cache.clear()
                    for v in state.get('tr_cache', []):
                        self._tr_cache.append(v)

        self.logger.info("ATR state restored")

    def reset(self) -> None:
        """Vider les caches et réinitialiser l'état."""
        with self._lock if self._lock else nullcontext():
            if self._atr_history is not None:
                self._atr_history.clear()
            if self._tr_cache is not None:
                self._tr_cache.clear()
            self._current_atr = self._prev_close = self._last_price = None
            self._update_count = self._spike_count = self._recalibrate_count = 0
        self.logger.debug("ATR state reset")

    def __repr__(self) -> str:
        return (
            f"ATRIndicator(mode={self.mode}, period={self.period}, "
            f"multiplier={self.base_multiplier}, smoothing={self.smoothing_method}, "
            f"cache={self._cache_enabled})"
        )


# ---------------------------------------------------------------------------
# Fonction utilitaire standalone
# ---------------------------------------------------------------------------

def calculate_atr_simple(
    df:     pd.DataFrame,
    period: int = 14,
    method: Literal['ema', 'sma', 'rma'] = DEFAULT_SMOOTHING_METHOD
) -> pd.Series:
    """
    Calculer l'ATR sans instancier ATRIndicator.

    Args:
        df:     DataFrame avec colonnes ['high', 'low', 'close'].
        period: Période ATR.
        method: Méthode de lissage ('ema', 'sma', 'rma').

    Returns:
        pd.Series: Valeurs ATR.

    Raises:
        ValueError: Colonnes manquantes ou méthode invalide.
    """
    missing = [c for c in ('high', 'low', 'close') if c not in df.columns]
    if missing:
        raise ValueError(f"Colonnes manquantes: {missing}")

    dfc = df.copy()
    dfc['prev_close'] = dfc['close'].shift(1)
    dfc['hl'] = dfc['high'] - dfc['low']
    dfc['hc'] = (dfc['high'] - dfc['prev_close']).abs()
    dfc['lc'] = (dfc['low']  - dfc['prev_close']).abs()
    dfc['tr'] = dfc[['hl', 'hc', 'lc']].max(axis=1)

    if method == 'sma':
        return dfc['tr'].rolling(window=period).mean()
    if method == 'rma':
        return dfc['tr'].ewm(alpha=1.0 / period, adjust=False).mean()
    if method == 'ema':
        return dfc['tr'].ewm(span=period, adjust=False).mean()

    raise ValueError(f"Method invalide: '{method}'. Valeurs valides: 'ema', 'sma', 'rma'")


def calculate_true_range(
    high:       float,
    low:        float,
    prev_close: Optional[float] = None
) -> float:
    """
    Calculer le True Range pour des valeurs individuelles.

    Args:
        high:       High de la bougie.
        low:        Low de la bougie.
        prev_close: Close précédent (optionnel).

    Returns:
        float: True Range.
    """
    hl = high - low
    if prev_close is None:
        return hl
    return max(hl, abs(high - prev_close), abs(low - prev_close))

# FIN DU MODULE