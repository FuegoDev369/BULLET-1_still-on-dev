"""
BULLET-1 - MarketContextCapture  (src/ml/market_context.py)
============================================================

Version : 2.1.2
Date    : 2026-03-15
Author  : FuegoDev
Mode    : ✅ Backtest | ✅ Paper | ❌ Live (extension future)
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Résolution racine projet (pattern unifié BULLET-1)
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.utils.logger import BulletLogger

# Indicateurs techniques
from src.indicators.atr        import ATRIndicator
from src.indicators.trend       import TrendIndicator
from src.indicators.volume      import VolumeIndicator
from src.indicators.momentum    import MomentumIndicator
from src.indicators.volatility  import VolatilityIndicator
from src.indicators.structure   import StructureIndicator
from src.indicators.regime      import RegimeIndicator


# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

#: Clé dans config['engine_config'] pour la fenêtre minimale
_CFG_KEY_MIN_CANDLES: str = 'market_context_min_candles'

#: Fenêtre minimale par défaut (standard industrie)
_DEFAULT_MIN_CANDLES: int = 300

#: Format ISO-8601 UTC pour les timestamps JSON
_TS_FORMAT: str = '%Y-%m-%dT%H:%M:%SZ'

#: Colonnes OHLCV requises dans candles_window
_REQUIRED_COLS: Tuple[str, ...] = ('open', 'high', 'low', 'close', 'volume', 'timestamp')

#: Nombre d'indicateurs gérés (hors ATR — injecté séparément)
_INDICATOR_COUNT: int = 6


# ---------------------------------------------------------------------------
# Exception dédiée
# ---------------------------------------------------------------------------

class MarketContextError(RuntimeError):
    """
    Levée uniquement pour les erreurs fatales d'initialisation.

    Les erreurs de capture (méthode capture()) ne lèvent jamais d'exception —
    elles sont absorbées et retournent None ou un snapshot partiel.
    """
    pass


# ---------------------------------------------------------------------------
# Classe principale
# ---------------------------------------------------------------------------

class MarketContextCapture:
    """
    Capture l'état complet du marché à l'instant t de l'ouverture d'une position.

    Responsabilités :
        - Initialiser une fois tous les indicateurs techniques nécessaires
        - Sur déclenchement d'ouverture de position : calculer et retourner
          un snapshot complet (dict JSON-serializable) de l'état du marché
        - Garantir la robustesse absolue : l'échec du calcul d'un indicateur
          ne bloque jamais la capture des autres

    Design :
        - Instance unique créée dans engine.py, injectée par DI dans TradingEngine
        - L'instance ATRIndicator est RÉUTILISÉE (injectée depuis engine.py) :
          cohérence de configuration garantie, zéro surcoût de calcul
        - Enrichissement cumulatif du DataFrame :
              _enrich_atr()        → ajoute vol_atr_value
              _enrich_trend()      → ajoute ma_fast, ma_slow, trend, ...
              _enrich_volume()     → ajoute volume_sma, volume_ratio, ...
              _enrich_momentum()   → ajoute mom_rsi, mom_macd_line, ...
              _enrich_volatility() → ajoute vol_bb_upper, vol_kc_upper, ...
              _enrich_structure()  → consomme vol_atr_value, ajoute str_vwap, ...
              _enrich_regime()     → ajoute reg_adx, reg_vr, ...

    Robustesse :
        - Chaque bloc d'enrichissement est isolé dans son propre try/except
        - Chaque bloc de snapshot est isolé dans son propre try/except
        - Un indicateur None (init échoué) → sa section retourne None dans le dict
        - Un enrichissement échoué → les colonnes attendues seront absentes,
          le snapshot de ce bloc retournera None, les autres continuent
        - capture() ne lève JAMAIS d'exception — always safe to call

    Thread-safety :
        Non thread-safe — usage single-threaded (même convention que TradingEngine).

    Examples:
        >>> ctx = MarketContextCapture(config=cfg, atr_indicator=atr_inst)
        >>> snapshot = ctx.capture(candles_window=df, entry_time=ts)
        >>> if snapshot:
        ...     trade_record['origine_signal'] = snapshot
    """

    def __init__(
        self,
        config: dict,
        atr_indicator: Optional[ATRIndicator] = None,
    ) -> None:
        """
        Initialise MarketContextCapture et tous les indicateurs techniques.

        Chaque indicateur est initialisé de façon isolée dans son propre
        try/except : un échec individuel ne bloque pas les autres. Les erreurs
        d'initialisation sont loggées en WARNING et enregistrées dans
        self._init_errors pour inspection.

        Args:
            config:        Configuration principale BULLET-1 (dict complet).
                           Utilisé pour MomentumIndicator, VolatilityIndicator,
                           StructureIndicator et RegimeIndicator.
            atr_indicator: Instance ATRIndicator réutilisée depuis engine.py.
                           Si None (trailing='candle'), la section ATR du snapshot
                           sera absente (None) — comportement normal et attendu.

        Raises:
            TypeError:          Si config n'est pas un dict.
            MarketContextError: En cas d'erreur fatale d'initialisation
                                (ne devrait pas se produire — les erreurs par
                                indicateur sont absorbées individuellement).
        """
        if not isinstance(config, dict):
            raise TypeError(
                f"config doit être un dict, reçu : {type(config).__name__}"
            )

        self.logger = BulletLogger()
        self._config = config

        # ── Paramètre MIN_CANDLES (configurable via engine_config) ────────────
        ec = config.get('engine_config', {})
        self._min_candles: int = int(
            ec.get(_CFG_KEY_MIN_CANDLES, _DEFAULT_MIN_CANDLES)
        )

        # [v2.1.1 — FIX-MCC-1] Avertissement si _min_candles semble trop bas.
        # La plupart des indicateurs techniques (MACD, ADX, Bollinger) requièrent
        # au minimum 50-100 bougies pour converger. En dessous de ce seuil, les
        # snapshots produits peuvent être biaisés sans que le système le signale.
        _SAFE_MIN_CANDLES = 100
        if self._min_candles < _SAFE_MIN_CANDLES:
            self.logger.warning(
                f"[MCCapture] market_context_min_candles={self._min_candles} "
                f"est inférieur au seuil recommandé ({_SAFE_MIN_CANDLES}). "
                f"Les indicateurs techniques peuvent être insuffisamment convergés "
                f"en début de session, produisant des snapshots biaisés."
            )

        # ── Informations contextuelles ────────────────────────────────────────
        general = config.get('general', {})
        self._trading_pair: str = general.get('trading_pair', '')
        self._timeframe:    str = general.get('timeframe', '')

        # ── ATR : instance réutilisée depuis engine.py ────────────────────────
        # Peut être None si trailing_stop.type = 'candle' — comportement attendu
        self._atr: Optional[ATRIndicator] = atr_indicator

        # ── Indicateurs — attributs pré-initialisés à None ───────────────────
        self._trend:      Optional[TrendIndicator]      = None
        self._volume:     Optional[VolumeIndicator]     = None
        self._momentum:   Optional[MomentumIndicator]   = None
        self._volatility: Optional[VolatilityIndicator] = None
        self._structure:  Optional[StructureIndicator]  = None
        self._regime:     Optional[RegimeIndicator]     = None

        # ── Initialisation isolée par indicateur ──────────────────────────────
        self._init_errors: List[str] = []
        self._initialize_indicators()

        n_ready = self._count_ready_indicators()

        self.logger.info(
            f"MarketContextCapture initialized | "
            f"pair={self._trading_pair} | tf={self._timeframe} | "
            f"min_candles={self._min_candles} | "
            f"atr={'injected' if self._atr else 'none'} | "
            f"indicators={n_ready}/{_INDICATOR_COUNT} ready | "
            f"init_errors={len(self._init_errors)}"
        )

        if self._init_errors:
            self.logger.warning(
                f"[MCCapture] {len(self._init_errors)} erreur(s) d'initialisation :"
            )
            for err in self._init_errors:
                self.logger.warning(f"  ⚠️  {err}")

    # =========================================================================
    # API PUBLIQUE — POINT D'ENTRÉE UNIQUE
    # =========================================================================

    def capture(
        self,
        candles_window: pd.DataFrame,
        entry_time: datetime,
    ) -> Optional[Dict[str, Any]]:
        """
        Capture l'état complet du marché à l'instant t du signal de trading.

        Appelée UNIQUEMENT à l'ouverture d'une position dans TradingEngine.
        La rareté de cet événement (quelques dizaines de fois par session)
        justifie le calcul complet des indicateurs sur candles_window.

        Flux d'exécution :
            1. Guard fenêtre minimale (< min_candles → None, résultats non fiables)
            2. Validation colonnes OHLCV requises
            3. Enrichissement cumulatif du DataFrame par bloc :
               ATR → Trend → Volume → Momentum → Volatility → Structure → Regime
            4. Extraction row = iloc[-1] (bougie signal, convention strategy.py)
            5. Construction du dict origine_signal par section isolée

        Garanties de robustesse :
            - Ne lève JAMAIS d'exception (catch-all global)
            - Fenêtre insuffisante → None (pas de snapshot)
            - Échec d'enrichissement d'un indicateur → section = None dans le dict,
              les autres sections continuent normalement
            - Échec d'un snapshot individuel → section = None, les autres continuent
            - Snapshot partiel retourné plutôt qu'aucun snapshot

        Args:
            candles_window: Fenêtre glissante OHLCV passée par TradingEngine.
                            Colonnes requises : open, high, low, close, volume, timestamp.
                            Convention : iloc[-1] = bougie ayant déclenché le signal.
                            Taille minimale : engine_config.market_context_min_candles.
            entry_time:     Timestamp UTC de l'ouverture de la position.

        Returns:
            Dict `origine_signal` JSON-serializable si capture réussie (totale ou partielle).
            None si fenêtre insuffisante ou erreur fatale inattendue.
        """
        # ── Guard : entrée None ou vide ───────────────────────────────────────
        if candles_window is None or candles_window.empty:
            self.logger.debug("[MCCapture] candles_window vide — capture ignorée")
            return None

        # ── Guard : fenêtre minimale ──────────────────────────────────────────
        n_candles = len(candles_window)
        if n_candles < self._min_candles:
            self.logger.debug(
                f"[MCCapture] Fenêtre insuffisante : {n_candles} < {self._min_candles} "
                f"— capture ignorée (indicateurs potentiellement biaisés)"
            )
            return None

        # ── Guard : colonnes requises ─────────────────────────────────────────
        missing_cols = [c for c in _REQUIRED_COLS if c not in candles_window.columns]
        if missing_cols:
            self.logger.warning(
                f"[MCCapture] Colonnes OHLCV manquantes : {missing_cols} — capture ignorée"
            )
            return None

        # ── Capture avec catch-all global ────────────────────────────────────
        # Ne jamais propager d'exception depuis capture() — niveau orchestration
        try:
            return self._build_snapshot(candles_window, entry_time)
        except Exception as exc:
            self.logger.error(
                f"[MCCapture] Erreur inattendue dans capture() : "
                f"{type(exc).__name__}: {exc}",
                exc_info=True,
            )
            return None

    # =========================================================================
    # INITIALISATION DES INDICATEURS (privé)
    # =========================================================================

    def _initialize_indicators(self) -> None:
        """
        Initialise chaque indicateur technique de façon isolée.

        Un échec d'initialisation enregistre l'erreur dans self._init_errors
        et laisse l'attribut à None. Aucune exception n'est propagée.

        Ordre d'initialisation : sans contrainte d'ordre (chaque indicateur
        charge sa propre config JSON indépendamment).
        """
        # ── TrendIndicator ────────────────────────────────────────────────────
        # Lit trend_config.json et config.json directement — pas de paramètre config
        try:
            self._trend = TrendIndicator()
            self.logger.debug("  ✅ TrendIndicator initialized")
        except Exception as exc:
            self._record_init_error('TrendIndicator', exc)

        # ── VolumeIndicator ───────────────────────────────────────────────────
        # Lit volume_config.json directement — pas de paramètre config
        try:
            self._volume = VolumeIndicator()
            self.logger.debug("  ✅ VolumeIndicator initialized")
        except Exception as exc:
            self._record_init_error('VolumeIndicator', exc)

        # ── MomentumIndicator ─────────────────────────────────────────────────
        try:
            self._momentum = MomentumIndicator(config=self._config)
            self.logger.debug("  ✅ MomentumIndicator initialized")
        except Exception as exc:
            self._record_init_error('MomentumIndicator', exc)

        # ── VolatilityIndicator ───────────────────────────────────────────────
        try:
            self._volatility = VolatilityIndicator(config=self._config)
            self.logger.debug("  ✅ VolatilityIndicator initialized")
        except Exception as exc:
            self._record_init_error('VolatilityIndicator', exc)

        # ── StructureIndicator ────────────────────────────────────────────────
        try:
            self._structure = StructureIndicator(config=self._config)
            self.logger.debug("  ✅ StructureIndicator initialized")
        except Exception as exc:
            self._record_init_error('StructureIndicator', exc)

        # ── RegimeIndicator ───────────────────────────────────────────────────
        try:
            self._regime = RegimeIndicator(config=self._config)
            self.logger.debug("  ✅ RegimeIndicator initialized")
        except Exception as exc:
            self._record_init_error('RegimeIndicator', exc)

    def _record_init_error(self, indicator_name: str, exc: Exception) -> None:
        """Enregistre et logge une erreur d'initialisation d'indicateur."""
        msg = f"{indicator_name} init failed : {type(exc).__name__}: {exc}"
        self._init_errors.append(msg)
        self.logger.warning(f"  ⚠️  [MCCapture] {msg}")

    # =========================================================================
    # CONSTRUCTION DU SNAPSHOT (privé)
    # =========================================================================

    def _build_snapshot(
        self,
        candles_window: pd.DataFrame,
        entry_time: datetime,
    ) -> Dict[str, Any]:
        """
        Construit le dict `origine_signal` complet.

        Enrichissement cumulatif :
            Chaque indicateur ajoute ses colonnes au df passé en chaîne.
            En cas d'échec d'un enrichissement, le df conserve son dernier
            état valide et la section correspondante retournera None dans le dict.

        Point clé : _enrich_atr() est appelé en PREMIER pour que la colonne
        'vol_atr_value' soit disponible pour StructureIndicator.get_snapshot()
        (calcul de la distance VWAP en ATR). Structure est enrichi en DERNIER
        pour bénéficier de toutes les colonnes précédentes.

        Returns:
            Dict `origine_signal` JSON-serializable.
        """
        # ── Index de la bougie signal ─────────────────────────────────────────
        # Convention validée : iloc[-1] = current_candle dans strategy.py
        row_idx     = len(candles_window) - 1
        # [v2.1.2 — FIX-MCC-5] n_candles défini ici (portée _build_snapshot).
        # En v2.1.1, n_candles était défini dans capture() mais consommé dans
        # _build_snapshot() → NameError silencieusement avalé par le catch-all
        # → 100% des captures retournaient None → snapshots absents des JSON.
        n_candles   = len(candles_window)
        close_price = float(candles_window['close'].iloc[row_idx])
        candle_ts   = candles_window['timestamp'].iloc[row_idx]

        # ── Métadonnées ───────────────────────────────────────────────────────
        last_closed_candle = self._ts_to_iso(candle_ts)
        calculated_at      = datetime.now(timezone.utc).strftime(_TS_FORMAT)

        # ── Enrichissement cumulatif du DataFrame ─────────────────────────────
        # ATR en premier : fournit 'vol_atr_value' pour Structure
        df = candles_window.copy()
        df, atr_series   = self._enrich_atr(df)
        df               = self._enrich_trend(df)
        df               = self._enrich_volume(df)
        df               = self._enrich_momentum(df)
        df               = self._enrich_volatility(df)
        df               = self._enrich_structure(df)   # après volatility (vol_atr_value disponible)
        df               = self._enrich_regime(df)

        # ── Bougie signal enrichie ────────────────────────────────────────────
        row = df.iloc[row_idx]

        # ── [v2.1.1 — FIX-MCC-2] Métrique qualité données ───────────────────
        # Ratio bougies sans NaN sur les colonnes OHLCV / total fenêtre.
        # Un ratio < 1.0 indique des bougies forward-fillées dans la fenêtre,
        # ce qui peut fausser les indicateurs calculés sur cette fenêtre.
        try:
            ohlcv_cols = ['open', 'high', 'low', 'close', 'volume']
            valid_cols = [c for c in ohlcv_cols if c in df.columns]
            n_valid = int(df[valid_cols].notna().all(axis=1).sum()) if valid_cols else n_candles
            data_quality_ratio = round(n_valid / n_candles, 4) if n_candles > 0 else None
        except Exception:
            data_quality_ratio = None

        # ── Construction des sections — chacune indépendante ─────────────────
        indicators: Dict[str, Any] = {
            '_meta': {
                'calculated_at':      calculated_at,
                'last_closed_candle': last_closed_candle,
                # [v2.1.1 — FIX-MCC-2] Qualité des données sous-jacentes.
                # < 1.0 signale des bougies interpolées (ffill) dans la fenêtre.
                'data_quality_ratio': data_quality_ratio,
                'candles_in_window':  n_candles,
            },
            'trend':         self._snapshot_trend(row),
            'atr':           self._snapshot_atr(df, row_idx, close_price, atr_series),
            'volume':        self._snapshot_volume(row),
            'momentum':      self._snapshot_momentum(row),
            'volatility':    self._snapshot_volatility(row),
            'structure':     self._snapshot_structure(row, df),
            'market_regime': self._snapshot_regime(row),
        }

        # [v2.1.1 — FIX-MCC-3] Log INFO avec compteur sections None.
        # Permet diagnostic post-backtest sans mode DEBUG complet.
        sections = ['trend', 'atr', 'volume', 'momentum', 'volatility', 'structure', 'market_regime']
        none_sections = [s for s in sections if indicators.get(s) is None]
        if none_sections:
            self.logger.info(
                f"[MCCapture] Snapshot partiel : {len(none_sections)}/{len(sections)} "
                f"sections absentes : {none_sections} | "
                f"data_quality={data_quality_ratio}"
            )
        else:
            self.logger.info(
                f"[MCCapture] Snapshot complet ({len(sections)} sections) | "
                f"data_quality={data_quality_ratio} | candles={n_candles}"
            )

        raw_snapshot = {
            'timestamp': self._ts_to_iso(entry_time),
            'timeframe': self._timeframe,
            'indicators': indicators,
        }

        # Sanitization défensive : garantit que chaque valeur est un type Python
        # natif JSON-serializable, même si un get_snapshot() d'indicateur retourne
        # des types numpy (float64, bool_, int64) ou pandas (Timestamp, NA).
        # Cette passe est légère — le snapshot est un dict de taille fixe, pas le df.
        return _sanitize_for_json(raw_snapshot)

    # =========================================================================
    # ENRICHISSEMENT DU DATAFRAME — chaque méthode isolée par try/except
    # =========================================================================

    def _enrich_atr(
        self, df: pd.DataFrame
    ) -> Tuple[pd.DataFrame, Optional[pd.Series]]:
        """
        Calcule la série ATR et injecte 'vol_atr_value' dans df.

        Retourne également la série ATR brute pour les calculs dérivés
        dans _snapshot_atr (trailing_distance, regime vs_mean_20).

        'vol_atr_value' est la colonne que StructureIndicator.get_snapshot()
        utilise pour calculer la distance VWAP en ATR.
        """
        if self._atr is None:
            return df, None
        try:
            atr_series = self._atr.calculate_atr(df)
            df = df.copy()
            df['vol_atr_value'] = atr_series
            return df, atr_series
        except Exception as exc:
            self.logger.warning(
                f"[MCCapture] ⚠️  ATR enrichment failed : {type(exc).__name__}: {exc}"
            )
            return df, None

    def _enrich_trend(self, df: pd.DataFrame) -> pd.DataFrame:
        """Enrichit df avec TrendIndicator (ma_fast, ma_slow, trend, slopes...)."""
        if self._trend is None:
            return df
        try:
            return self._trend.add_trend_indicators(df)
        except Exception as exc:
            self.logger.warning(
                f"[MCCapture] ⚠️  add_trend_indicators failed : {type(exc).__name__}: {exc}"
            )
            return df

    def _enrich_volume(self, df: pd.DataFrame) -> pd.DataFrame:
        """Enrichit df avec VolumeIndicator (volume_sma, volume_ratio, volume_zscore...)."""
        if self._volume is None:
            return df
        try:
            return self._volume.add_volume_indicators(df)
        except Exception as exc:
            self.logger.warning(
                f"[MCCapture] ⚠️  add_volume_indicators failed : {type(exc).__name__}: {exc}"
            )
            return df

    def _enrich_momentum(self, df: pd.DataFrame) -> pd.DataFrame:
        """Enrichit df avec MomentumIndicator (mom_rsi, mom_macd_*, mom_stoch_*, ...)."""
        if self._momentum is None:
            return df
        try:
            return self._momentum.add_momentum_indicators(df)
        except Exception as exc:
            self.logger.warning(
                f"[MCCapture] ⚠️  add_momentum_indicators failed : {type(exc).__name__}: {exc}"
            )
            return df

    def _enrich_volatility(self, df: pd.DataFrame) -> pd.DataFrame:
        """Enrichit df avec VolatilityIndicator (vol_bb_*, vol_kc_*, vol_squeeze, ...)."""
        if self._volatility is None:
            return df
        try:
            return self._volatility.add_volatility_indicators(df)
        except Exception as exc:
            self.logger.warning(
                f"[MCCapture] ⚠️  add_volatility_indicators failed : {type(exc).__name__}: {exc}"
            )
            return df

    def _enrich_structure(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Enrichit df avec StructureIndicator (str_vwap, str_zscore, str_pivot_*, ...).

        Appelé après _enrich_atr() et _enrich_volatility() pour bénéficier
        de 'vol_atr_value' (déjà dans df), utilisé par get_snapshot() pour
        calculer la distance VWAP en ATR.
        """
        if self._structure is None:
            return df
        try:
            return self._structure.add_structure_indicators(df)
        except Exception as exc:
            self.logger.warning(
                f"[MCCapture] ⚠️  add_structure_indicators failed : {type(exc).__name__}: {exc}"
            )
            return df

    def _enrich_regime(self, df: pd.DataFrame) -> pd.DataFrame:
        """Enrichit df avec RegimeIndicator (reg_adx, reg_vr, reg_regime_composite, ...)."""
        if self._regime is None:
            return df
        try:
            return self._regime.add_regime_indicators(df)
        except Exception as exc:
            self.logger.warning(
                f"[MCCapture] ⚠️  add_regime_indicators failed : {type(exc).__name__}: {exc}"
            )
            return df

    # =========================================================================
    # SNAPSHOTS PAR SECTION — chacun isolé dans son propre try/except
    # =========================================================================

    def _snapshot_trend(self, row: pd.Series) -> Optional[Dict[str, Any]]:
        """
        Snapshot section 'trend'.

        Construit manuellement depuis les colonnes add_trend_indicators()
        (TrendIndicator n'a pas de get_snapshot()).
        """
        if self._trend is None:
            return None
        try:
            ma_fast  = _safe_float(row.get('ma_fast'), 2)
            ma_slow  = _safe_float(row.get('ma_slow'), 2)
            close    = _safe_float(row.get('close'),   2)

            # Distance MA fast vs slow (%)
            ma_distance_pct = None
            if ma_fast is not None and ma_slow is not None and ma_slow != 0.0:
                ma_distance_pct = round(((ma_fast - ma_slow) / ma_slow) * 100.0, 4)

            # Prix vs MA (%)
            price_vs_fast_pct = None
            if close is not None and ma_fast is not None and ma_fast != 0.0:
                price_vs_fast_pct = round(((close - ma_fast) / ma_fast) * 100.0, 4)

            price_vs_slow_pct = None
            if close is not None and ma_slow is not None and ma_slow != 0.0:
                price_vs_slow_pct = round(((close - ma_slow) / ma_slow) * 100.0, 4)

            return {
                'ma_type':            self._trend.ma_type,
                'fast_period':        self._trend.fast_period,
                'slow_period':        self._trend.slow_period,
                'ma_fast':            ma_fast,
                'ma_slow':            ma_slow,
                'ma_fast_slope':      _safe_float(row.get('ma_fast_slope'), 6),
                'ma_slow_slope':      _safe_float(row.get('ma_slow_slope'), 6),
                'ma_distance_pct':    ma_distance_pct,
                'price_vs_fast_pct':  price_vs_fast_pct,
                'price_vs_slow_pct':  price_vs_slow_pct,
            }
        except Exception as exc:
            self.logger.warning(
                f"[MCCapture] ⚠️  trend snapshot failed : {type(exc).__name__}: {exc}"
            )
            return None

    def _snapshot_atr(
        self,
        df:         pd.DataFrame,
        row_idx:    int,
        close_price: float,
        atr_series: Optional[pd.Series],
    ) -> Optional[Dict[str, Any]]:
        """
        Snapshot section 'atr'.

        Construit manuellement depuis la série ATR retournée par _enrich_atr()
        (ATRIndicator n'a pas de get_snapshot()).

        Calculs dérivés :
            value_pct        = ATR / close × 100
            normalized       = ATR / mean(ATR sur 20 dernières bougies)
            trailing_distance = ATR × base_multiplier (via ATRIndicator.get_trailing_distance)
        """
        if self._atr is None or atr_series is None:
            return None
        try:
            atr_val_raw = atr_series.iloc[row_idx]
            if pd.isna(atr_val_raw) or float(atr_val_raw) <= 0:
                self.logger.debug("[MCCapture] ATR snapshot : valeur invalide — section absente")
                return None

            atr_val = float(atr_val_raw)

            # [v2.1.1 — FIX-MCC-4] Validation explicite close_price > 0.
            # Protège contre une bougie corrompue (close = 0) qui produirait
            # une division par zéro ou un atr_pct = inf dans le snapshot.
            if close_price <= 0:
                self.logger.warning(
                    f"[MCCapture] close_price invalide ({close_price}) "
                    f"pour le calcul ATR% — atr_pct forcé à None."
                )
                atr_pct = None
            else:
                # ATR en % du prix de clôture
                atr_pct = round((atr_val / close_price) * 100.0, 4)

            # ATR vs moyenne 20 bougies (régime de volatilité relatif)
            atr_vs_mean20 = None
            lookback       = min(20, row_idx + 1)   # Ne pas dépasser le début du df
            if lookback >= 2:
                window = atr_series.iloc[max(0, row_idx - lookback + 1): row_idx + 1].dropna()
                if len(window) >= 2:
                    mean_20 = float(window.mean())
                    if mean_20 > 0:
                        atr_vs_mean20 = round(atr_val / mean_20, 4)

            # Distance du trailing stop (ATR × base_multiplier)
            trailing_distance = None
            try:
                candle_dict = df.iloc[row_idx].to_dict()
                trailing_distance = _safe_float(
                    self._atr.get_trailing_distance(
                        candle=candle_dict,
                        historical_data=df,
                    ),
                    2,
                )
            except Exception as td_exc:
                self.logger.debug(
                    f"[MCCapture] trailing_distance calc failed (non-fatal) : {td_exc}"
                )

            return {
                'period':             self._atr.period,
                'smoothing_method':   self._atr.smoothing_method,
                'value':              round(atr_val, 4),
                'value_pct':          atr_pct,
                'normalized':         atr_vs_mean20,
                'trailing_distance':  trailing_distance,
                'regime': {
                    'vs_mean_20': atr_vs_mean20,
                },
            }
        except Exception as exc:
            self.logger.warning(
                f"[MCCapture] ⚠️  atr snapshot failed : {type(exc).__name__}: {exc}"
            )
            return None

    def _snapshot_volume(self, row: pd.Series) -> Optional[Dict[str, Any]]:
        """
        Snapshot section 'volume'.

        Construit manuellement depuis les colonnes add_volume_indicators()
        (VolumeIndicator n'a pas de get_snapshot()).
        'volume_zscore' est utilisé comme proxy de percentile (score de position
        du volume courant par rapport à la distribution historique).
        """
        if self._volume is None:
            return None
        try:
            return {
                'lookback_period': self._volume.lookback_period,
                'current':         _safe_float(row.get('volume'),       2),
                'sma':             _safe_float(row.get('volume_sma'),   2),
                'ratio':           _safe_float(row.get('volume_ratio'), 4),
                'percentile':      _safe_float(row.get('volume_zscore'), 4),
            }
        except Exception as exc:
            self.logger.warning(
                f"[MCCapture] ⚠️  volume snapshot failed : {type(exc).__name__}: {exc}"
            )
            return None

    def _snapshot_momentum(self, row: pd.Series) -> Optional[Dict[str, Any]]:
        """
        Snapshot section 'momentum'.

        Délégué à MomentumIndicator.get_snapshot() — dict complet et fiable.
        Le champ '_source' (méta interne) est supprimé du résultat final.
        """
        if self._momentum is None:
            return None
        try:
            snap = self._momentum.get_snapshot(row)
            snap.pop('_source', None)
            return snap
        except Exception as exc:
            self.logger.warning(
                f"[MCCapture] ⚠️  momentum snapshot failed : {type(exc).__name__}: {exc}"
            )
            return None

    def _snapshot_volatility(self, row: pd.Series) -> Optional[Dict[str, Any]]:
        """
        Snapshot section 'volatility'.

        Délégué à VolatilityIndicator.get_snapshot() — dict complet et fiable.
        Le champ '_source' (méta interne) est supprimé du résultat final.
        """
        if self._volatility is None:
            return None
        try:
            snap = self._volatility.get_snapshot(row)
            snap.pop('_source', None)
            return snap
        except Exception as exc:
            self.logger.warning(
                f"[MCCapture] ⚠️  volatility snapshot failed : {type(exc).__name__}: {exc}"
            )
            return None

    def _snapshot_structure(
        self,
        row: pd.Series,
        df:  pd.DataFrame,
    ) -> Optional[Dict[str, Any]]:
        """
        Snapshot section 'structure'.

        Délégué à StructureIndicator.get_snapshot(row, df=df).
        Passe df complet pour permettre le calcul de la distance VWAP en ATR
        via la colonne 'vol_atr_value' (injectée en amont par _enrich_atr).
        Le champ '_source' (méta interne) est supprimé du résultat final.
        """
        if self._structure is None:
            return None
        try:
            snap = self._structure.get_snapshot(row, df=df)
            snap.pop('_source', None)
            return snap
        except Exception as exc:
            self.logger.warning(
                f"[MCCapture] ⚠️  structure snapshot failed : {type(exc).__name__}: {exc}"
            )
            return None

    def _snapshot_regime(self, row: pd.Series) -> Optional[Dict[str, Any]]:
        """
        Snapshot section 'market_regime'.

        Délégué à RegimeIndicator.get_snapshot() — dict complet et fiable.
        Le champ '_source' (méta interne) est supprimé du résultat final.
        """
        if self._regime is None:
            return None
        try:
            snap = self._regime.get_snapshot(row)
            snap.pop('_source', None)
            return snap
        except Exception as exc:
            self.logger.warning(
                f"[MCCapture] ⚠️  regime snapshot failed : {type(exc).__name__}: {exc}"
            )
            return None

    # =========================================================================
    # UTILITAIRES INTERNES
    # =========================================================================

    def _ts_to_iso(self, ts: Any) -> Optional[str]:
        """
        Convertit tout type de timestamp en string ISO-8601 UTC.

        Supporte : datetime (aware/naïf), int/float ms, pd.Timestamp, string.
        Retourne None si la conversion est impossible.
        """
        if ts is None:
            return None

        if isinstance(ts, datetime):
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            return ts.strftime(_TS_FORMAT)

        if isinstance(ts, (int, float)):
            try:
                return datetime.fromtimestamp(
                    ts / 1000, tz=timezone.utc
                ).strftime(_TS_FORMAT)
            except (OSError, ValueError, OverflowError):
                pass

        try:
            return pd.Timestamp(ts, tz='UTC').strftime(_TS_FORMAT)
        except Exception:
            pass

        try:
            return str(ts)
        except Exception:
            return None

    def _count_ready_indicators(self) -> int:
        """Retourne le nombre d'indicateurs correctement initialisés (hors ATR)."""
        return sum(
            1 for ind in (
                self._trend, self._volume, self._momentum,
                self._volatility, self._structure, self._regime,
            )
            if ind is not None
        )

    # =========================================================================
    # API PUBLIQUE — ACCESSEURS D'ÉTAT
    # =========================================================================

    @property
    def is_fully_ready(self) -> bool:
        """True si tous les indicateurs sont correctement initialisés."""
        return self._count_ready_indicators() == _INDICATOR_COUNT

    @property
    def init_errors(self) -> List[str]:
        """Liste des erreurs d'initialisation des indicateurs (lecture seule)."""
        return list(self._init_errors)

    @property
    def min_candles(self) -> int:
        """Fenêtre minimale requise pour capture() (lecture seule)."""
        return self._min_candles

    def get_status(self) -> Dict[str, Any]:
        """
        Retourne un snapshot de l'état interne pour monitoring / debugging.

        Returns:
            Dict avec indicateurs prêts, config et erreurs d'init.
        """
        return {
            'trading_pair':   self._trading_pair,
            'timeframe':      self._timeframe,
            'min_candles':    self._min_candles,
            'atr_injected':   self._atr is not None,
            'indicators': {
                'trend':      self._trend      is not None,
                'volume':     self._volume     is not None,
                'momentum':   self._momentum   is not None,
                'volatility': self._volatility is not None,
                'structure':  self._structure  is not None,
                'regime':     self._regime     is not None,
            },
            'ready_count':   self._count_ready_indicators(),
            'total_count':   _INDICATOR_COUNT,
            'is_fully_ready': self.is_fully_ready,
            'init_errors':   list(self._init_errors),
        }

    def __repr__(self) -> str:
        return (
            f"MarketContextCapture("
            f"pair={self._trading_pair}, "
            f"tf={self._timeframe}, "
            f"indicators={self._count_ready_indicators()}/{_INDICATOR_COUNT}, "
            f"atr={'ok' if self._atr else 'none'}, "
            f"min_candles={self._min_candles})"
        )


# ---------------------------------------------------------------------------
# Helpers module-level (privés au module)
# ---------------------------------------------------------------------------

def _safe_float(val: Any, decimals: int = 4) -> Optional[float]:
    """
    Convertit une valeur en float arrondi, ou None si invalide / NaN / inf.

    Utilisé dans les snapshots manuels (trend, volume, atr) pour garantir
    la sérialisabilité JSON de toutes les valeurs numériques.

    Args:
        val:      Valeur à convertir (float, int, np.float64, None, NaN...).
        decimals: Nombre de décimales après arrondi.

    Returns:
        float arrondi ou None.
    """
    if val is None:
        return None
    try:
        v = float(val)
        if np.isnan(v) or np.isinf(v):
            return None
        return round(v, decimals)
    except (TypeError, ValueError):
        return None


def _sanitize_for_json(obj: Any) -> Any:
    """
    Convertit récursivement un objet en types Python natifs JSON-compatibles.

    Couche de défense finale appliquée sur le snapshot complet avant retour.
    Garantit que json.dump ne lèvera jamais de TypeError quelle que soit la
    valeur retournée par un get_snapshot() d'indicateur.

    Conversions :
        numpy.bool_      → bool
        numpy.integer    → int
        numpy.floating   → float (None si NaN/Inf)
        numpy.ndarray    → list (récursif)
        pandas.Timestamp → str ISO-8601 UTC
        pandas.NA/NaT    → None
        datetime         → str ISO-8601 UTC
        Enum             → .value (récursif)
        dict             → dict (récursif sur valeurs)
        list/tuple       → list (récursif)
        float NaN/Inf    → None
        autres           → str() en fallback ultime

    Args:
        obj: Valeur à convertir.

    Returns:
        Valeur JSON-serializable native Python.
    """
    import math
    from enum import Enum as _Enum

    # ── Types natifs triviaux ────────────────────────────────────────────────
    if obj is None or isinstance(obj, (bool, str)):
        return obj

    if isinstance(obj, int):
        return obj

    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj

    # ── datetime standard ────────────────────────────────────────────────────
    if isinstance(obj, datetime):
        if obj.tzinfo is None:
            obj = obj.replace(tzinfo=timezone.utc)
        return obj.strftime(_TS_FORMAT)

    # ── numpy types ──────────────────────────────────────────────────────────
    try:
        if isinstance(obj, np.bool_):
            return bool(obj)

        if isinstance(obj, np.integer):
            return int(obj)

        if isinstance(obj, np.floating):
            v = float(obj)
            return None if (math.isnan(v) or math.isinf(v)) else v

        if isinstance(obj, np.ndarray):
            return [_sanitize_for_json(x) for x in obj.tolist()]
    except TypeError:
        pass

    # ── pandas types ─────────────────────────────────────────────────────────
    try:
        import pandas as pd  # type: ignore[import]

        if isinstance(obj, pd.Timestamp):
            try:
                if obj.tzinfo is None:
                    obj = obj.tz_localize('UTC')
                return obj.strftime(_TS_FORMAT)
            except Exception:
                return str(obj)

        if obj is pd.NA or obj is pd.NaT:
            return None
    except ImportError:
        pass

    # ── Enum ─────────────────────────────────────────────────────────────────
    if isinstance(obj, _Enum):
        return _sanitize_for_json(obj.value)

    # ── Structures récursives ────────────────────────────────────────────────
    if isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}

    if isinstance(obj, (list, tuple)):
        return [_sanitize_for_json(item) for item in obj]

    # ── Fallback : str() pour tout type inconnu ──────────────────────────────
    try:
        return str(obj)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# FIN DU MODULE
# ---------------------------------------------------------------------------
