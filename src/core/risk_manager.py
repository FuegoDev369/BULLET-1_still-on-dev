"""
BULLET-1 Risk Manager - v2.4.1
====================================
Gestion complète du risque

Responsabilités:
- Position sizing (collateral, notional, size)
- Calcul SL/TP selon risk/reward ratio
- Validation market conditions (spread, volume, gaps, wicks, ATR)
- Protection anomalies marché
- compute_position() : point d'entrée unique Strategy (sizing + SL/TP)

Changelog:
    v2.4.1 — 2026-04-26
        [FIX-RM-9] Restauration logs observabilité validate_position_entry :
            logger.warning position_too_small / position_too_large supprimés
            en v2.4.0 (nettoyage) — réintroduits car seule trace RM d'un
            signal valide silencieusement filtré par sizing (position_too_small/large
            enregistré dans rejections_history mais invisible dans les logs RM).
        [FIX-RM-10] Restauration logs update_leverage / update_risk_reward_ratio :
            logger.info old → new supprimés en v2.4.0 — réintroduits pour tracer
            les mises à jour dynamiques de paramètres entre sessions.

    v2.4.0 — 2026-04-26
        [FEAT-RM-1] Logique directionnelle wick check : pénalise uniquement la
            mèche adverse à la direction (upper_wick pour LONG, lower_wick pour
            SHORT). Évite le rejet de configurations favorables (ex: longue mèche
            basse = rejet acheteur → favorable pour un SHORT).
        [FEAT-RM-2] Garde-fou absolu flash crash : max(upper_wick, lower_wick) /
            close > max_wick_pct × 1.5 → rejet systématique indépendant du side.
        [FIX-RM-8] Protection Doji (body == 0) : ancienne logique produisait une
            ZeroDivisionError silencieuse sur les bougies doji. Cas traité
            explicitement via total_wick_pct.
        validate_market_conditions() : nouveau paramètre side=None.
        validate_position_entry() : extrait side depuis signal et le propage
            à validate_market_conditions().
        Codes rejet wick mis à jour (breaking si parsés en aval) :
            extreme_wick → absolute_extreme_wick / extreme_rejection_wick_{side}
                         / extreme_doji_wick
        Nettoyage docstrings et réduction logging non-essentiel.

    v2.3.3 — 2026-03-13
        [FIX-RM-3] Guard division par zéro : close <= 0 (bougie corrompue).
        [FIX-RM-4] Formule wick standard analyse technique :
            upper_wick = high - max(open, close)
            lower_wick = min(open, close) - low
        [FIX-RM-5] reset_session() : réinitialise _anomaly_count et
            _atr_failures entre sessions (cross-session contamination).
        [FIX-RM-6] Pattern _PROJECT_ROOT unifié BULLET-1 (majuscules).
        [FIX-RM-7] Reset _atr_failures conditionnel sur _atr_available ET
            atr_succeeded (logique morte clarifiée).

Version: 2.4.1
Date: 2026-04-26
Mode: BACKTEST ONLY
Dépendances: logger.py, config_loader.py, atr.py
"""

import pandas as pd
from typing import Optional, Tuple, Dict, Literal, Union
from pathlib import Path
import sys

# Pattern unifié BULLET-1
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.utils.logger import BulletLogger
from src.utils.config_loader import BulletConfig
from src.indicators.atr import ATRIndicator


class RiskManager:
    """
    Gestionnaire de risque BULLET-1 (MODE BACKTEST).

    Gestion risque via LEVERAGE + COLLATERAL_PCT (méthodologie personnalisée).
    Position sizing → SL/TP calcul → Market conditions validation → Trade.
    """

    def __init__(
        self,
        config: Union[dict, BulletConfig],
        atr_indicator: Optional[ATRIndicator] = None
    ):
        self.logger = BulletLogger()

        if hasattr(config, 'dict'):
            self.config = config.dict()
        else:
            self.config = config

        self._atr_indicator = atr_indicator
        self._atr_available = atr_indicator is not None
        self._atr_failures = 0

        position_config = self.config['position']
        self.leverage = position_config['leverage']
        self.collateral_pct = position_config['collateral_percentage']

        risk_config = self.config['risk_management']
        self.rr_ratio = risk_config['risk_reward_ratio']
        self.sl_offset_pct = risk_config['stop_loss_offset_pct']

        mc_config = risk_config.get('market_conditions', {})
        self.max_spread_pct = mc_config.get('max_spread_pct', 0.5)
        self.min_volume_threshold = mc_config.get('min_volume_threshold', 1.0)
        self.max_gap_pct = mc_config.get('max_gap_pct', 0.5)
        self.max_wick_ratio = mc_config.get('max_wick_ratio', 2.0)
        self.max_wick_pct = mc_config.get('max_wick_pct', 2.0)
        self.max_atr_pct = mc_config.get('max_atr_pct', 5.0)
        self.max_consecutive_anomalies = mc_config.get('max_consecutive_anomalies', 3)
        self.enable_market_validation = mc_config.get('enable_market_validation', True)

        self._anomaly_count = 0

        self.logger.info(
            f"RiskManager [BACKTEST] v2.4.1: leverage={self.leverage}x, "
            f"collateral={self.collateral_pct}%, RR={self.rr_ratio}, "
            f"SL={self.sl_offset_pct}%, ATR={'ON' if self._atr_available else 'OFF'}"
        )

    def set_atr_indicator(self, atr_indicator: ATRIndicator):
        self._atr_indicator = atr_indicator
        self._atr_available = True
        self._atr_failures = 0
        self.logger.info("ATRIndicator injected")

    # =========================================================================
    # Position Sizing
    # =========================================================================

    def calculate_position_size(self, capital: float, entry_price: float) -> Dict[str, float]:
        if capital <= 0 or entry_price <= 0:
            raise ValueError(f"Invalid inputs: capital={capital}, entry_price={entry_price}")

        collateral = capital * (self.collateral_pct / 100)
        notional = collateral * self.leverage
        size = notional / entry_price

        self.logger.debug(
            f"Position: capital={capital:.2f}, collateral={collateral:.2f}, "
            f"notional={notional:.2f}, size={size:.6f}"
        )

        return {
            'collateral': round(collateral, 2),
            'notional': round(notional, 2),
            'size': round(size, 8)
        }

    # =========================================================================
    # SL/TP Calculation
    # =========================================================================

    def calculate_sl_tp(self, side: Literal['LONG', 'SHORT'], entry_price: float) -> Dict[str, float]:
        if entry_price <= 0:
            raise ValueError(f"Entry price must be > 0, got {entry_price}")

        if side not in ['LONG', 'SHORT']:
            raise ValueError(f"Side must be 'LONG' or 'SHORT', got '{side}'")

        if side == 'SHORT':
            sl_price = entry_price * (1 + self.sl_offset_pct / 100)
            risk = sl_price - entry_price
            reward = risk * self.rr_ratio
            tp_price = entry_price - reward
        else:
            sl_price = entry_price * (1 - self.sl_offset_pct / 100)
            risk = entry_price - sl_price
            reward = risk * self.rr_ratio
            tp_price = entry_price + reward

        risk_pct = (risk / entry_price) * 100
        reward_pct = (reward / entry_price) * 100

        return {
            'sl_price': round(sl_price, 2),
            'tp_price': round(tp_price, 2),
            'risk': round(risk, 2),
            'reward': round(reward, 2),
            'risk_pct': round(risk_pct, 2),
            'reward_pct': round(reward_pct, 2)
        }

    def compute_position(
        self,
        capital: float,
        side: Literal['LONG', 'SHORT'],
        entry_price: float
    ) -> Dict:
        position_size = self.calculate_position_size(capital, entry_price)
        sl_tp = self.calculate_sl_tp(side, entry_price)

        return {
            'collateral':  position_size['collateral'],
            'notional':    position_size['notional'],
            'size':        position_size['size'],
            'stop_loss':   sl_tp['sl_price'],
            'take_profit': sl_tp['tp_price'],
            'risk':        sl_tp['risk'],
            'reward':      sl_tp['reward'],
            'rr_ratio':    self.rr_ratio,
            'leverage':    self.leverage
        }

    # =========================================================================
    # Market Conditions Validation
    # =========================================================================

    def validate_market_conditions(
        self,
        candle: dict,
        prev_candle: Optional[dict] = None,
        historical_data: Optional[pd.DataFrame] = None,
        side: Optional[str] = None
    ) -> Tuple[bool, str]:
        """
        Valide conditions marché AVANT trade.
        [v2.4.0] Intègre la logique directionnelle (side) et la symétrie.
        """
        if not self.enable_market_validation:
            return True, "validation_disabled"

        atr_succeeded = False

        # [FIX-RM-3] Guard division par zéro : close = 0 (bougie corrompue).
        if candle.get('close', 0) <= 0:
            self._anomaly_count += 1
            self.logger.warning(
                f"[FIX-RM-3] Bougie corrompue : close={candle.get('close')} <= 0."
            )
            return False, "invalid_candle:close_zero_or_negative"

        # 1. Spread check
        spread_pct = ((candle['high'] - candle['low']) / candle['close']) * 100
        if spread_pct > self.max_spread_pct:
            self._anomaly_count += 1
            self.logger.warning(f"Spread too wide: {spread_pct:.2f}% > {self.max_spread_pct}%")
            return False, f"spread_too_wide:{spread_pct:.2f}%"

        # 2. Volume check
        if candle['volume'] < self.min_volume_threshold:
            self._anomaly_count += 1
            return False, f"volume_too_low:{candle['volume']:.2f}"

        # 3. Gap detection
        if prev_candle:
            gap_pct = abs(candle['open'] - prev_candle['close']) / prev_candle['close']
            if gap_pct > (self.max_gap_pct / 100):
                self._anomaly_count += 1
                return False, f"price_gap:{gap_pct*100:.2f}%"

        # 4. Wick / Flash crash detection & Reversals Logic [v2.4.0]
        candle_open  = candle['open']
        candle_close = candle['close']
        body_top     = max(candle_open, candle_close)
        body_bottom  = min(candle_open, candle_close)
        upper_wick   = candle['high'] - body_top
        lower_wick   = body_bottom - candle['low']
        body         = body_top - body_bottom

        # [FEAT-RM-2] Garde-fou absolu (Flash Crash Protection)
        # Même si la mèche est directionnelle, une taille colossale est bloquée.
        absolute_max_wick_pct = max(upper_wick, lower_wick) / candle_close
        hard_cap_pct = (self.max_wick_pct * 1.5) / 100
        if absolute_max_wick_pct > hard_cap_pct:
            self._anomaly_count += 1
            self.logger.warning(f"Absolute extreme wick (Crash protection): {absolute_max_wick_pct*100:.2f}%")
            return False, f"absolute_extreme_wick:{absolute_max_wick_pct*100:.2f}%"

        # [FIX-RM-8] Protection Doji à corps nul
        if body == 0:
            total_wick_pct = (candle['high'] - candle['low']) / candle_close
            if total_wick_pct > (self.max_wick_pct / 100):
                self._anomaly_count += 1
                return False, f"extreme_doji_wick:{total_wick_pct*100:.2f}%"
        else:
            lower_wick_ratio = lower_wick / body
            upper_wick_ratio = upper_wick / body

            # [FEAT-RM-1] Symétrie & Logique Directionnelle
            wick_min = min(lower_wick, upper_wick)
            wick_max = max(lower_wick, upper_wick)
            symmetry_factor = wick_min / wick_max if wick_max > 0 else 1.0

            current_max_ratio = self.max_wick_ratio
            if symmetry_factor > 0.7:
                current_max_ratio = self.max_wick_ratio * 1.5  # Tolérance pour vraie incertitude

            if side == 'LONG':
                penalized_ratio = upper_wick_ratio   # Danger: rejet vendeur
            elif side == 'SHORT':
                penalized_ratio = lower_wick_ratio   # Danger: rejet acheteur
            else:
                penalized_ratio = max(lower_wick_ratio, upper_wick_ratio)

            if penalized_ratio > current_max_ratio:
                wick_to_check = (
                    upper_wick if side == 'LONG'
                    else lower_wick if side == 'SHORT'
                    else max(upper_wick, lower_wick)
                )
                extreme_wick_pct = wick_to_check / candle_close

                if extreme_wick_pct > (self.max_wick_pct / 100):
                    self._anomaly_count += 1
                    self.logger.warning(
                        f"Extreme rejection wick: side={side}, "
                        f"ratio={penalized_ratio:.2f}, pct={extreme_wick_pct*100:.2f}%"
                    )
                    return False, f"extreme_rejection_wick_{side or 'neutral'}:{extreme_wick_pct*100:.2f}%"

        # 5. ATR spike detection
        if self._atr_available and historical_data is not None:
            try:
                atr_pct = self._atr_indicator.get_atr_percentage(candle, historical_data)
                if atr_pct > (self.max_atr_pct / 100):
                    self._anomaly_count += 1
                    return False, f"atr_spike:{atr_pct*100:.2f}%"
                atr_succeeded = True
            except Exception as e:
                self._atr_failures += 1
                if self._atr_failures >= 10:
                    self.logger.error(f"ATR repeatedly failing ({self._atr_failures} times)")

        # 6. Consecutive anomalies check
        if self._anomaly_count >= self.max_consecutive_anomalies:
            return False, f"multiple_anomalies:{self._anomaly_count}"

        self._anomaly_count = 0
        if self._atr_available and atr_succeeded:
            self._atr_failures = 0

        return True, "conditions_ok"

    # =========================================================================
    # Position Entry Validation
    # =========================================================================

    def validate_position_entry(
        self,
        signal: dict,
        candle: dict,
        capital: float,
        prev_candle: Optional[dict] = None,
        historical_data: Optional[pd.DataFrame] = None
    ) -> Tuple[bool, str]:
        # [v2.4.0] Injection du 'side' depuis le signal pour valider la mèche
        side = signal.get('side', signal.get('direction'))

        # 1. Market conditions
        is_valid, reason = self.validate_market_conditions(
            candle=candle,
            prev_candle=prev_candle,
            historical_data=historical_data,
            side=side
        )
        if not is_valid:
            self.logger.warning(f"Market conditions invalid: {reason}")
            return False, reason

        # 2. Position size limits
        position_size = self.calculate_position_size(capital, signal['entry_price'])
        sizing_config = self.config['position']['position_sizing']
        min_position = sizing_config.get('min_position_size_usdt', 5.0)
        max_position = sizing_config.get('max_position_size_usdt', 1000.0)

        if position_size['notional'] < min_position:
            # [FIX-RM-9] Restauré depuis v2.3.3 : seule trace RM d'un rejet sizing.
            self.logger.warning(
                f"Position too small: {position_size['notional']:.2f} < {min_position}"
            )
            return False, f"position_too_small:{position_size['notional']:.2f}"

        if position_size['notional'] > max_position:
            # [FIX-RM-9] Restauré depuis v2.3.3 : seule trace RM d'un rejet sizing.
            self.logger.warning(
                f"Position too large: {position_size['notional']:.2f} > {max_position}"
            )
            return False, f"position_too_large:{position_size['notional']:.2f}"

        return True, "validated"

    # =========================================================================
    # Session Management
    # =========================================================================

    def reset_anomaly_counter(self):
        self._anomaly_count = 0
        self._atr_failures = 0

    def reset_session(self) -> None:
        self._anomaly_count = 0
        self._atr_failures  = 0

    # =========================================================================
    # Getters
    # =========================================================================

    def get_config(self) -> dict:
        return self.config.copy()

    def get_anomaly_count(self) -> int:
        return self._anomaly_count

    def get_atr_failures(self) -> int:
        return self._atr_failures

    def is_atr_available(self) -> bool:
        return self._atr_available

    # =========================================================================
    # Setters
    # =========================================================================

    def update_leverage(self, new_leverage: int):
        if not (1 <= new_leverage <= 125):
            raise ValueError(f"Leverage must be 1-125, got {new_leverage}")
        old_leverage = self.leverage
        self.leverage = new_leverage
        # [FIX-RM-10] Restauré depuis v2.3.3 : traçabilité mise à jour dynamique.
        self.logger.info(f"Leverage: {old_leverage}x → {new_leverage}x")

    def update_risk_reward_ratio(self, new_rr: float):
        if not (1.0 <= new_rr <= 10.0):
            raise ValueError(f"RR ratio must be 1.0-10.0, got {new_rr}")
        old_rr = self.rr_ratio
        self.rr_ratio = new_rr
        # [FIX-RM-10] Restauré depuis v2.3.3 : traçabilité mise à jour dynamique.
        self.logger.info(f"RR ratio: {old_rr} → {new_rr}")


# =============================================================================
# Utility Functions
# =============================================================================

def calculate_position_size_simple(
    capital: float, entry_price: float, collateral_pct: float = 20.0, leverage: int = 10
) -> Dict[str, float]:
    collateral = capital * (collateral_pct / 100)
    notional = collateral * leverage
    size = notional / entry_price
    return {'collateral': round(collateral, 2), 'notional': round(notional, 2), 'size': round(size, 8)}


def calculate_sl_tp_simple(
    side: Literal['LONG', 'SHORT'], entry_price: float, sl_offset_pct: float = 2.0, rr_ratio: float = 3.0
) -> Dict[str, float]:
    if side == 'SHORT':
        sl_price = entry_price * (1 + sl_offset_pct / 100)
        risk = sl_price - entry_price
        reward = risk * rr_ratio
        tp_price = entry_price - reward
    else:
        sl_price = entry_price * (1 - sl_offset_pct / 100)
        risk = entry_price - sl_price
        reward = risk * rr_ratio
        tp_price = entry_price + reward
    return {'sl_price': round(sl_price, 2), 'tp_price': round(tp_price, 2), 'risk': round(risk, 2), 'reward': round(reward, 2)}


# FIN DU MODULE
