"""
BULLET-1 - Config Loader Module v2.3.6

Chargement et validation de la configuration BULLET-1.
Module fondamental (niveau 1) - Utilisé par TOUS les modules.

Version: 2.3.6
Date: 2026-03-07
Author: FuegoDev
Dépendances: helpers.py (module 1), logger.py (module 3)
"""

import os
import sys
from pathlib import Path
from typing import Optional, Dict, Any, Literal, List
from pydantic import (
    BaseModel, 
    Field, 
    field_validator,
    model_validator,
    ValidationError
)

# Trouver et ajouter la racine du projet
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Imports depuis modules BULLET-1
from src.utils.helpers import (
    read_json,
    get_project_root
)
from src.utils.logger import BulletLogger
# [FIX-UTILS-VERSION-1] Source de vérité unique pour la version runtime —
# résout l'incohérence interne (header v2.3.6 vs message d'erreur "v2.3.3").
from src.__version__ import __version__ as _RUNTIME_VERSION


# ============================================================================
# CONSTANTES CONFIGURATION NAMES
# ============================================================================

_VALID_CONFIG_NAMES: frozenset = frozenset({
    '1-normal', '2-normal', '3-normal', '4-normal',
    '5-reverse', '6-reverse', '7-reverse', '8-reverse'
})


# ============================================================================
# MODÈLES PYDANTIC - CONFIGURATION GÉNÉRALE
# ============================================================================

class GeneralConfig(BaseModel):
    """Configuration générale du bot."""
    bot_name: str = "BULLET-1"
    version: str = "2.1"
    mode: Literal["backtest", "paper", "live"] = "backtest"
    exchange: str = "binance"
    trading_pair: str = "BTC/USDT"
    timeframe: str = "5m"
    timezone: str = "UTC"


class EdgeCasesConfig(BaseModel):
    """Configuration gestion edge cases sessions."""
    grace_period_hours: int = Field(default=4, ge=1, le=24)
    max_loss_safety_margin_pct: float = Field(default=20.0, ge=0.0, le=100.0)
    allow_grace_period: bool = True
    force_close_on_critical: bool = True
    log_edge_cases: bool = True


class SessionDailyLimitsConfig(BaseModel):
    """
    Configuration limites quotidiennes (daily limits) dans session management.
    
    v2.3.0: Ajouté pour conformité config.json
    v2.3.1: Simplifié — suppression reset_time et edge_cases (non utilisés)
    """
    enabled: bool = Field(
        default=True,
        description="Activer limites quotidiennes"
    )
    max_loss_per_day_pct: float = Field(
        default=3.0,
        ge=0.1,
        le=50.0,
        description="Perte maximum par jour (% capital)"
    )
    max_gain_per_day_pct: float = Field(
        default=10.0,
        ge=1.0,
        le=100.0,
        description="Gain maximum par jour (% capital)"
    )
    max_trades_per_day: int = Field(
        default=5,
        ge=1,
        le=100,
        description="Nombre maximum de trades par jour"
    )
    auto_save_days_path: str = Field(
        default="results/backtests/sessions/days/",
        description="Chemin sauvegarde données journalières"
    )


class SessionConfig(BaseModel):
    """Configuration gestion sessions."""
    trades_period_days: int = Field(default=10, ge=1, le=30)
    reset_capital_between_sessions: bool = False
    max_loss_per_session_pct: float = Field(default=3.0, ge=0.1, le=20.0)
    max_gain_per_session_pct: float = Field(default=10.0, ge=1.0, le=50.0)
    auto_save_trades_path: str = "results/backtests/sessions/trades/"
    session_summary_save_path: str = "results/backtests/sessions/summaries/"
    edge_cases: EdgeCasesConfig = Field(default_factory=EdgeCasesConfig)
    
    # 🆕 v2.3.0 - Ajout daily_limits
    daily_limits: SessionDailyLimitsConfig = Field(
        default_factory=SessionDailyLimitsConfig,
        description="Configuration limites quotidiennes"
    )


class CapitalConfig(BaseModel):
    """Configuration capital."""
    initial_capital_backtest: float = Field(default=100.0, ge=10.0, le=100000.0)
    initial_capital_live: float = Field(default=50.0, ge=10.0, le=100000.0)
    target_capital_live: float = Field(default=1000.0, ge=50.0, le=1000000.0)


class PositionSizingConfig(BaseModel):
    """
    Configuration position sizing.
    v2.3.1: Suppression champ method (non utilisé dans config.json v2.3.1)
    """
    min_position_size_usdt: float = Field(default=5.0, ge=1.0)
    max_position_size_usdt: float = Field(default=1000.0, ge=10.0)


class PositionConfig(BaseModel):
    """Configuration positions."""
    leverage: int = Field(default=10, ge=1, le=125)
    margin_mode: Literal["isolated", "cross"] = "isolated"
    collateral_percentage: float = Field(default=20.0, ge=1.0, le=100.0)
    max_simultaneous_positions: int = Field(default=1, ge=1, le=10)
    position_sizing: PositionSizingConfig = Field(default_factory=PositionSizingConfig)


class MarketConditionsConfig(BaseModel):
    """Configuration validation market conditions."""
    max_spread_pct: float = Field(default=0.5, ge=0.01, le=10.0)
    min_volume_threshold: float = Field(default=1.0, ge=0.0)
    max_gap_pct: float = Field(default=0.5, ge=0.01, le=10.0)
    max_wick_ratio: float = Field(default=2.0, ge=0.5, le=10.0)
    max_wick_pct: float = Field(default=2.0, ge=0.1, le=10.0)
    max_atr_pct: float = Field(default=5.0, ge=0.1, le=20.0)
    max_consecutive_anomalies: int = Field(default=3, ge=1, le=10)
    enable_market_validation: bool = False


class RiskManagementConfig(BaseModel):
    """
    Configuration gestion risque.
    v2.3.1: Suppression daily_limits (consolidé dans session_management.daily_limits)
    """
    risk_reward_ratio: float = Field(default=3.0, ge=0.5, le=10.0)
    stop_loss_offset_pct: float = Field(default=2.0, ge=0.1, le=10.0)
    market_conditions: MarketConditionsConfig = Field(default_factory=MarketConditionsConfig)

class SignalSideReverserConfig(BaseModel):
    """
    Configuration inverseur de côté de signal.
    v2.3.6: Nouvelle sous-section de signal_generator
    """
    enabled: bool = Field(
        default=False,
        description="Activer l'inversion du côté du signal (LONG→SHORT, SHORT→LONG)"
    )


class SignalGeneratorConfig(BaseModel):
    """Configuration du générateur de signaux v2.3.6"""
    
    breakout_detection_mode: Literal["strict", "permissive"] = Field(
        default="permissive",
        description=(
            "Mode de détection de cassure. "
            "STRICT : close doit dépasser high/low (conservateur). "
            "PERMISSIVE : high/low actuel dépasse high/low précédent (agressif, défaut)."
        )
    )
    signal_side_reverser: SignalSideReverserConfig = Field(
        default_factory=SignalSideReverserConfig,
        description="Configuration inverseur de côté de signal"
    )
    
    @field_validator('breakout_detection_mode')
    @classmethod
    def validate_mode(cls, v: str) -> str:
        """Valide le mode de détection"""
        if v not in ['strict', 'permissive']:
            raise ValueError(
                f"breakout_detection_mode doit être 'strict' ou 'permissive', reçu: '{v}'"
            )
        return v


class EntryLogicConfig(BaseModel):
    """
    Configuration logique d'entrée.
    v2.3.1: Suppression des 7 champs volume (déplacés vers volume_confirmation)
    """
    logic_direction: Literal["normal", "reverse"] = "normal"
    for_short_case_comparison_operator: Literal[">", "<"] = ">"
    for_long_case_comparison_operator: Literal[">", "<"] = ">"


class TrendFilterConfig(BaseModel):
    """
    Configuration filtres de tendance.
    v2.3.1: Simplifié — suppression type, ema_fast, ema_slow, trend_strength_threshold
    """
    enabled: bool = True
    allow_counter_trend: bool = False


# ============================================================================
# VOLUME CONFIRMATION MODELS 
# ============================================================================

class VolumeConfirmationBasicConfig(BaseModel):
    """
    Configuration mode BASIC de volume confirmation.
    
    Valide uniquement le ratio de volume sans vérifier la direction.
    """
    min_ratio: float = Field(
        default=1.2, 
        ge=0.5, 
        le=5.0,
        description="Ratio minimum requis (volume_current / volume_avg)"
    )


class VolumeConfirmationDirectionalConfig(BaseModel):
    """
    Configuration mode DIRECTIONAL de volume confirmation.
    
    Valide le ratio de volume ET la cohérence de la direction de la bougie.
    Mode recommandé par défaut.
    """
    min_ratio: float = Field(
        default=1.2, 
        ge=0.5, 
        le=5.0,
        description="Ratio minimum requis (volume_current / volume_avg)"
    )
    require_matching_candle: bool = Field(
        default=True,
        description=(
            "Si true, LONG requiert bougie verte (close > open), "
            "SHORT requiert bougie rouge (close < open)"
        )
    )


class VolumeConfirmationAdvancedConfig(BaseModel):
    """
    Configuration mode ADVANCED de volume confirmation.
    
    Valide ratio, direction ET tendance du volume.
    Utilise is_volume_confirmation() de VolumeIndicator.
    """
    min_ratio: float = Field(
        default=1.2, 
        ge=0.5, 
        le=5.0,
        description="Ratio minimum requis (volume_current / volume_avg)"
    )
    require_matching_candle: bool = Field(
        default=True,
        description="Vérifier cohérence direction bougie"
    )
    check_volume_trend: bool = Field(
        default=True,
        description="Vérifier tendance volume (increasing/neutral attendu)"
    )
    allowed_trends: List[str] = Field(
        default=["increasing", "neutral"],
        description="Tendances volume acceptées pour validation"
    )
    
    @field_validator('allowed_trends')
    @classmethod
    def validate_trends(cls, v: List[str]) -> List[str]:
        """Valider que seules les tendances valides sont présentes"""
        valid_trends = ['increasing', 'decreasing', 'neutral']
        for trend in v:
            if trend not in valid_trends:
                raise ValueError(
                    f"Tendance invalide '{trend}'. "
                    f"Valeurs autorisées: {valid_trends}"
                )
        return v


class VolumeConfirmationConfig(BaseModel):
    """
    Configuration NIVEAU-2 de validation volume.
    
    Filtre supplémentaire après la validation volume NIVEAU-1 (entry_logic).
    Permet une validation directionnelle du volume pour améliorer la qualité
    des signaux.
    
    3 modes disponibles:
    - basic: Validation ratio uniquement
    - directional: Validation ratio + direction bougie (RECOMMANDÉ)
    - advanced: Validation complète avec tendance volume
    """
    enabled: bool = Field(
        default=False,
        description="Activer filtre NIVEAU-2 (false pour migration progressive)"
    )
    mode: Literal["basic", "directional", "advanced"] = Field(
        default="directional",
        description=(
            "Mode de validation. "
            "basic=ratio seul, directional=ratio+direction (recommandé), "
            "advanced=complet avec tendance"
        )
    )
    basic: VolumeConfirmationBasicConfig = Field(
        default_factory=VolumeConfirmationBasicConfig,
        description="Paramètres mode BASIC"
    )
    directional: VolumeConfirmationDirectionalConfig = Field(
        default_factory=VolumeConfirmationDirectionalConfig,
        description="Paramètres mode DIRECTIONAL"
    )
    advanced: VolumeConfirmationAdvancedConfig = Field(
        default_factory=VolumeConfirmationAdvancedConfig,
        description="Paramètres mode ADVANCED"
    )
    
    @field_validator('mode')
    @classmethod
    def validate_mode(cls, v: str) -> str:
        """Valider que le mode est valide"""
        if v not in ['basic', 'directional', 'advanced']:
            raise ValueError(
                f"Mode invalide '{v}'. "
                f"Valeurs autorisées: basic, directional, advanced"
            )
        return v


# ============================================================================
# TRAILING STOP MODELS - REFACTORISATION MAJEURE 
# ============================================================================

class CandleModeConfig(BaseModel):
    """
    Configuration trailing stop mode candle.
    
    Attributes:
        use_previous_candle: Si True, utilise bougie n-1 (précédente)
    """
    use_previous_candle: bool = Field(
        default=True,
        description="true = bougie PRÉCÉDENTE (n-1), false = bougie ACTUELLE (n)"
    )


class ProgressiveStageConfig(BaseModel):
    """
    Configuration d'un stage individual pour progressive tightening.
    
    Attributes:
        profit_threshold: Seuil de profit (en R) pour activer ce stage
        multiplier: Multiplicateur ATR à utiliser pour ce stage
    """
    profit_threshold: float = Field(
        ge=0.0,
        le=10.0,
        description="Seuil profit (en R) pour ce stage"
    )
    multiplier: float = Field(
        ge=0.5,
        le=5.0,
        description="Multiplicateur ATR pour ce stage"
    )


class ATRModeConfig(BaseModel):
    """
    Configuration trailing stop mode ATR.
    
    🆕 v2.2.3: base_multiplier est la SOURCE DE VÉRITÉ UNIQUE pour le multiplicateur ATR.
    """
    base_multiplier: float = Field(
        default=2.0,
        ge=0.5,
        le=5.0,
        description="Multiplicateur ATR de BASE (source de vérité unique). Valeurs: 0.5-5.0"
    )
    min_distance_pct: float = Field(
        default=0.5,
        ge=0.1,
        le=5.0,
        description="Distance MINIMUM SL en % du prix (ex: 0.5%)"
    )
    max_distance_pct: float = Field(
        default=3.0,
        ge=1.0,
        le=10.0,
        description="Distance MAXIMUM SL en % du prix (ex: 3%)"
    )
    progressive_tightening: bool = Field(
        default=False,
        description="Activer resserrement progressif distance SL avec profit"
    )
    stages: List[ProgressiveStageConfig] = Field(
        default_factory=list,
        description="Stages progressive tightening [{profit_threshold, multiplier}]"
    )
    
    @model_validator(mode='after')
    def validate_progressive_tightening(self):
        """
        Valider cohérence progressive tightening.
        
        Règles:
        - Si progressive_tightening=True, au moins 1 stage requis
        - profit_threshold doit être croissant
        - multiplier doit être décroissant (logique progressive)
        """
        if self.progressive_tightening:
            if not self.stages or len(self.stages) == 0:
                raise ValueError(
                    "progressive_tightening=True requiert au moins 1 stage. "
                    "Fournir atr_mode.stages: [{profit_threshold, multiplier}, ...]"
                )
            
            # Vérifier ordre croissant profit_threshold
            thresholds = [s.profit_threshold for s in self.stages]
            if thresholds != sorted(thresholds):
                raise ValueError(
                    f"profit_threshold doit être CROISSANT dans stages. "
                    f"Actuel: {thresholds}, Attendu: {sorted(thresholds)}"
                )
            
            # Vérifier ordre décroissant multiplier (logique progressive)
            multipliers = [s.multiplier for s in self.stages]
            if multipliers != sorted(multipliers, reverse=True):
                raise ValueError(
                    f"multiplier doit être DÉCROISSANT dans stages (logique progressive). "
                    f"Actuel: {multipliers}, Attendu: {sorted(multipliers, reverse=True)}"
                )
        
        return self


class HybridModeConfig(BaseModel):
    """
    Configuration mode HYBRID (🆕 INNOVATION).
    
    Combine ATR (agressif, momentum) et Candle (conservateur, sécurité).
    Switch automatique basé sur profit.
    """
    start_with: Literal["atr", "candle"] = Field(
        default="atr",
        description="Mode initial à l'ouverture position"
    )
    switch_to_candle_after: float = Field(
        default=1.0,
        ge=0.1,
        le=5.0,
        description="Profit seuil (en R) pour switch ATR → Candle"
    )
    allow_switch_back: bool = Field(
        default=False,
        description="Permettre retour au mode initial si profit redescend"
    )


class Protection1RConfigV2(BaseModel):
    """
    Configuration Protection 1R avec asymétrie.
    
    🆕: Support seuils différents LONG vs SHORT.
    """
    auto_activate: bool = Field(
        default=True,
        description="Activer automatiquement protection 1R"
    )
    min_profit_for_breakeven: float = Field(
        default=1.0,
        ge=0.5,
        le=2.0,
        description="Seuil profit global (en R) si asymmetric_mode=False"
    )
    
    asymmetric_mode: bool = Field(
        default=False,
        description="Activer seuils différents LONG vs SHORT"
    )
    long_breakeven_threshold: float = Field(
        default=1.0,
        ge=0.5,
        le=2.0,
        description="Seuil pour positions LONG (en R)"
    )
    short_breakeven_threshold: float = Field(
        default=1.2,
        ge=0.5,
        le=2.0,
        description="Seuil pour positions SHORT (en R)"
    )


class VolatilityAdjustmentConfig(BaseModel):
    """
    Configuration Dynamic Risk Adjustment.
    
    🆕: Adaptation automatique distance SL selon volatilité marché.
    """
    enabled: bool = Field(
        default=False,
        description="Activer adaptation automatique distance SL selon volatilité"
    )
    atr_threshold_high: float = Field(
        default=3.0,
        ge=1.0,
        le=10.0,
        description="Seuil ATR (% prix) pour volatilité HAUTE"
    )
    atr_threshold_low: float = Field(
        default=0.5,
        ge=0.1,
        le=2.0,
        description="Seuil ATR (% prix) pour volatilité BASSE"
    )
    
    multiplier_increase: float = Field(
        default=1.5,
        ge=1.0,
        le=3.0,
        description="Facteur augmentation multiplier ATR en haute volatilité"
    )
    
    @model_validator(mode='after')
    def validate_thresholds(self):
        """Valider que threshold_low < threshold_high"""
        if self.atr_threshold_low >= self.atr_threshold_high:
            raise ValueError(
                f"atr_threshold_low ({self.atr_threshold_low}) doit être < "
                f"atr_threshold_high ({self.atr_threshold_high})"
            )
        return self


class TrailingStopConfig(BaseModel):
    """
    Configuration Trailing Stop v2.2.3+ avec validation stricte.
    
    🆕 v2.2.3 BREAKING CHANGES:
    - ❌ atr_multiplier SUPPRIMÉ (remplacé par atr_mode.base_multiplier)
    - ✅ base_multiplier est désormais la SOURCE DE VÉRITÉ UNIQUE
    - Structure complète OBLIGATOIRE (pas de backward compatibility)
    """
    type: Literal["candle", "atr", "hybrid"] = Field(
        default="candle",
        description="Type de trailing stop: 'candle', 'atr', ou 'hybrid'"
    )
    
    # Sous-configurations granulaires
    candle_mode: CandleModeConfig = Field(
        default_factory=CandleModeConfig,
        description="Configuration mode candle"
    )
    atr_mode: ATRModeConfig = Field(
        default_factory=ATRModeConfig,
        description="Configuration mode ATR (contient base_multiplier - SOURCE DE VÉRITÉ)"
    )
    hybrid_mode: HybridModeConfig = Field(
        default_factory=HybridModeConfig,
        description="Configuration mode hybrid"
    )
    protection_1r: Protection1RConfigV2 = Field(
        default_factory=Protection1RConfigV2,
        description="Configuration protection 1R (avec asymétrie)"
    )
    volatility_adjustment: VolatilityAdjustmentConfig = Field(
        default_factory=VolatilityAdjustmentConfig,
        description="Configuration adaptation volatilité"
    )
    
    @model_validator(mode='after')
    def validate_trailing_config_v2(self):
        """
        Valider cohérence configuration trailing stop v2.2.3+.
        
        Règles:
        - Si type='hybrid': hybrid_mode.start_with doit être cohérent
        - Si progressive_tightening: stages non vides
        Note: atr_period géré dans atr_config.json (module atr.py)
        """
        # Validation mode hybrid
        if self.type == 'hybrid':
            if self.hybrid_mode.start_with not in ['atr', 'candle']:
                raise ValueError(
                    f"hybrid_mode.start_with doit être 'atr' ou 'candle', "
                    f"reçu: '{self.hybrid_mode.start_with}'"
                )
        
        return self


class StrategyConfig(BaseModel):
    """
    Configuration stratégie.
    v2.3.2: Suppression uncertainty_candle et exit_conditions (absents du JSON)
    v2.3.3: configuration_name avec default="" pour permettre auto-détection
    """
    name: str = "uncertainty_candle_enhanced"
    # 🆕 v2.3.3: default="" → absent ou vide accepté, auto-détecté dans load_config()
    configuration_name: str = Field(
        default="",
        description=(
            "Nom de la configuration (1-normal … 8-reverse). "
            "Si vide, auto-détecté depuis (logic_direction, short_op, long_op)."
        )
    )
    min_quality_score: int = Field(
        default=40,
        ge=0,
        le=100,
        description="Score qualité minimum requis pour valider un signal (0-100)"
    )
    entry_logic: EntryLogicConfig = Field(default_factory=EntryLogicConfig)
    trend_filter: TrendFilterConfig = Field(default_factory=TrendFilterConfig)
    trailing_stop: TrailingStopConfig = Field(
        default_factory=TrailingStopConfig,
        description="Configuration trailing stop v2.2.3+ (candle/atr/hybrid)"
    )
    signal_generator: SignalGeneratorConfig = Field(
        default_factory=SignalGeneratorConfig,
        description="Configuration du générateur de signaux"
    )
    volume_confirmation: VolumeConfirmationConfig = Field(
        default_factory=VolumeConfirmationConfig,
        description="Configuration validation volume NIVEAU-2"
    )


# ============================================================================
# BACKTESTING, LIVE TRADING, NOTIFICATIONS, ETC.
# ============================================================================

class SimulationConfig(BaseModel):
    """Configuration simulation backtesting."""
    maker_fee: float = Field(default=0.02, ge=0.0, le=1.0)
    taker_fee: float = Field(default=0.04, ge=0.0, le=1.0)
    slippage_base: float = Field(default=0.0012, ge=0.0, le=0.01)
    slippage_max: float = Field(default=0.005, ge=0.0, le=0.1)
    slippage_dynamic: bool = True
    spread_pct: float = Field(default=0.02, ge=0.0, le=1.0)
    api_latency_ms: int = Field(default=200, ge=0, le=5000)
    funding_rate_8h: float = Field(default=0.01, ge=-1.0, le=1.0)

class BacktestingConfig(BaseModel):
    """Configuration backtesting."""
    data_path: str = "data/historical/BTC-USDT/5min.csv"
    start_date: str = "2025-05-01"
    end_date: str = "2025-12-01"
    simulation: SimulationConfig = Field(default_factory=SimulationConfig)


class SafetyConfig(BaseModel):
    """Configuration sécurité live trading."""
    max_daily_trades: int = Field(default=20, ge=1, le=1000)
    circuit_breaker_enabled: bool = True
    circuit_breaker_level: str = "multi"
    require_manual_approval: bool = False
    double_check_orders: bool = True
    confirm_before_order: bool = True
    emergency_stop_enabled: bool = True


class ExecutionConfig(BaseModel):
    """Configuration exécution ordres."""
    order_type: Literal["market", "limit"] = "market"
    limit_order_offset_pct: float = Field(default=0.05, ge=0.0, le=1.0)
    max_order_retries: int = Field(default=3, ge=0, le=10)
    retry_delay_seconds: int = Field(default=2, ge=1, le=60)
    cancel_unfilled_after_seconds: int = Field(default=30, ge=5, le=300)


class SynchronizationConfig(BaseModel):
    """
    Configuration synchronisation.
    🆕 v2.3.0 - Validation stricte pour MODE LIVE
    """
    sync_positions_on_start: bool = Field(
        default=True,
        description="Synchroniser positions au démarrage (OBLIGATOIRE en MODE LIVE)"
    )
    sync_interval_seconds: int = Field(default=60, ge=10, le=3600)
    reconcile_on_mismatch: bool = True
    auto_fix_ghost_positions: bool = False
    
    # 🆕 v2.3.0 - Champs additionnels pour MODE LIVE
    validate_balance_on_startup: bool = Field(
        default=True,
        description="Valider balance au démarrage (RECOMMANDÉ en MODE LIVE)"
    )


class MonitoringConfig(BaseModel):
    """Configuration monitoring."""
    heartbeat_interval_seconds: int = Field(default=300, ge=60, le=3600)
    health_check_interval_seconds: int = Field(default=60, ge=10, le=600)
    log_api_calls: bool = False
    track_latency: bool = True


class LiveTradingConfig(BaseModel):
    """
    Configuration live trading.
    🆕 v2.3.0 - Validations strictes MODE LIVE + MODE PAPER
    """
    enabled: bool = False
    paper_trading: bool = True
    paper_initial_balance: float = Field(default=100.0, ge=10.0)
    
    # 🆕 v2.3.0 - Champs MODE PAPER
    enable_websocket: bool = Field(
        default=True,
        description="Activer WebSocket pour prix temps réel (REQUIS en MODE PAPER)"
    )
    use_real_prices: bool = Field(
        default=True,
        description="Utiliser prix réels (REQUIS en MODE PAPER)"
    )
    
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    synchronization: SynchronizationConfig = Field(default_factory=SynchronizationConfig)
    monitoring: MonitoringConfig = Field(default_factory=MonitoringConfig)


class EmailTriggersConfig(BaseModel):
    """Triggers notifications email."""
    on_trade_open: bool = True
    on_trade_close: bool = True
    on_session_start: bool = True
    on_session_end: bool = True
    on_trailing_update: bool = False
    on_1r_protection: bool = True
    on_error: bool = True
    on_max_loss_reached: bool = True
    on_max_gain_reached: bool = True
    on_circuit_breaker: bool = True
    on_bot_start: bool = True
    on_bot_stop: bool = True
    on_market_anomaly: bool = True
    on_edge_case: bool = True


class EmailRateLimitConfig(BaseModel):
    """Rate limiting email."""
    max_emails_per_hour: int = Field(default=50, ge=1, le=1000)
    batch_similar_events: bool = True


class EmailConfig(BaseModel):
    """Configuration notifications email."""
    enabled: bool = True
    smtp_server: str = "smtp.gmail.com"
    smtp_port: int = 587
    use_tls: bool = True
    triggers: EmailTriggersConfig = Field(default_factory=EmailTriggersConfig)
    rate_limiting: EmailRateLimitConfig = Field(default_factory=EmailRateLimitConfig)


class DiscordTriggersConfig(BaseModel):
    """Triggers notifications Discord."""
    on_trade_open: bool = True
    on_trade_close: bool = True
    on_session_start: bool = False
    on_session_end: bool = True
    on_trailing_update: bool = False
    on_1r_protection: bool = True
    on_error: bool = True
    on_max_loss_reached: bool = True
    on_max_gain_reached: bool = True
    on_circuit_breaker: bool = True
    on_bot_start: bool = True
    on_bot_stop: bool = True
    on_market_anomaly: bool = True
    on_edge_case: bool = True


class DiscordFormattingConfig(BaseModel):
    """Formatage Discord."""
    use_embeds: bool = True
    include_charts: bool = False
    color_code_pnl: bool = True


class DiscordConfig(BaseModel):
    """Configuration notifications Discord."""
    enabled: bool = False
    triggers: DiscordTriggersConfig = Field(default_factory=DiscordTriggersConfig)
    formatting: DiscordFormattingConfig = Field(default_factory=DiscordFormattingConfig)


class TelegramTriggersConfig(BaseModel):
    """Triggers notifications Telegram."""
    on_trade_open: bool = True
    on_trade_close: bool = True
    on_session_end: bool = True
    on_error: bool = True
    on_circuit_breaker: bool = True
    on_market_anomaly: bool = True


class TelegramConfig(BaseModel):
    """Configuration notifications Telegram."""
    enabled: bool = False
    triggers: TelegramTriggersConfig = Field(default_factory=TelegramTriggersConfig)


class NotificationsConfig(BaseModel):
    """Configuration notifications."""
    enabled: bool = True
    email: EmailConfig = Field(default_factory=EmailConfig)
    discord: DiscordConfig = Field(default_factory=DiscordConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)


class WalkForwardConfig(BaseModel):
    """Configuration walk-forward analysis."""
    enabled: bool = False
    train_period_months: int = Field(default=6, ge=1, le=24)
    test_period_months: int = Field(default=1, ge=1, le=12)
    step_months: int = Field(default=1, ge=1, le=12)
    min_test_scenarios: int = Field(default=3, ge=1, le=10)


class OptimizationOutputConfig(BaseModel):
    """Configuration output optimisation."""
    save_results: bool = True
    results_path: str = "results/optimization/"
    save_top_n_configs: int = Field(default=10, ge=1, le=100)
    generate_comparison_report: bool = True


class OptimizationParameter(BaseModel):
    """Paramètre à optimiser."""
    name: str
    type: Literal["float", "int", "categorical"]
    min: Optional[float] = Field(default=None)
    max: Optional[float] = Field(default=None)
    step: Optional[float] = Field(default=None)
    values: Optional[List[Any]] = Field(default=None)
    
    @model_validator(mode='after')
    def validate_parameter_config(self):
        """Valider cohérence selon type."""
        if self.type in ['float', 'int']:
            if self.min is None or self.max is None or self.step is None:
                raise ValueError(
                    f"Parameter '{self.name}' type='{self.type}' requires: "
                    f"min, max, step"
                )
            if self.min >= self.max:
                raise ValueError(
                    f"Parameter '{self.name}': min ({self.min}) must be < max ({self.max})"
                )
        elif self.type == 'categorical':
            if self.values is None or len(self.values) == 0:
                raise ValueError(
                    f"Parameter '{self.name}' type='categorical' requires: "
                    f"values (non-empty list)"
                )
        return self


class OptimizationConfig(BaseModel):
    """Configuration optimisation."""
    enabled: bool = False
    method: Literal["grid_search", "random_search"] = "grid_search"
    use_reset_capital: bool = True
    parameters_to_optimize: List[OptimizationParameter] = Field(default_factory=list)
    optimization_metric: str = "sharpe_ratio"
    min_trades_required: int = Field(default=30, ge=10)
    max_parallel_jobs: int = Field(default=4, ge=1, le=32)
    walk_forward: WalkForwardConfig = Field(default_factory=WalkForwardConfig)
    output: OptimizationOutputConfig = Field(default_factory=OptimizationOutputConfig)


class BacktestPerformanceTargets(BaseModel):
    """Objectifs performance backtest."""
    min_win_rate: float = Field(default=40.0, ge=0.0, le=100.0)
    min_profit_factor: float = Field(default=1.3, ge=0.0, le=10.0)
    max_drawdown: float = Field(default=15.0, ge=0.0, le=100.0)
    min_sharpe_ratio: float = Field(default=1.0, ge=0.0, le=10.0)
    min_trades_per_day: float = Field(default=1.0, ge=0.0)
    max_trades_per_day: float = Field(default=10.0, ge=1.0)


class PaperPerformanceTargets(BaseModel):
    """Objectifs performance paper trading."""
    min_duration_days: int = Field(default=60, ge=7)
    min_trades: int = Field(default=100, ge=10)
    min_win_rate: float = Field(default=40.0, ge=0.0, le=100.0)
    min_profit_factor: float = Field(default=1.2, ge=0.0)
    max_drawdown: float = Field(default=20.0, ge=0.0, le=100.0)
    performance_vs_backtest_min: float = Field(default=0.8, ge=0.0, le=1.0)
    min_market_conditions: int = Field(default=2, ge=1, le=10)


class LivePerformanceTargets(BaseModel):
    """Objectifs performance live trading."""
    min_win_rate: float = Field(default=40.0, ge=0.0, le=100.0)
    min_profit_factor: float = Field(default=1.3, ge=0.0)
    max_drawdown: float = Field(default=15.0, ge=0.0, le=100.0)
    min_sharpe_ratio: float = Field(default=0.8, ge=0.0)


class PerformanceTargetsConfig(BaseModel):
    """Configuration objectifs performance."""
    backtest: BacktestPerformanceTargets = Field(default_factory=BacktestPerformanceTargets)
    paper_trading: PaperPerformanceTargets = Field(default_factory=PaperPerformanceTargets)
    live_trading: LivePerformanceTargets = Field(default_factory=LivePerformanceTargets)


class MemoryMonitoringConfig(BaseModel):
    """Configuration monitoring mémoire."""
    enabled: bool = True
    check_interval_seconds: int = Field(default=60, ge=10, le=3600)
    warning_threshold_pct: float = Field(default=80.0, ge=50.0, le=100.0)
    critical_threshold_pct: float = Field(default=95.0, ge=80.0, le=100.0)
    auto_cleanup_on_warning: bool = True
    aggressive_cleanup_on_critical: bool = True


class PerformanceConfig(BaseModel):
    """Configuration performance Android."""
    use_numpy_vectorization: bool = True
    use_pandas_optimizations: bool = True
    limit_historical_data: bool = True
    max_historical_candles: int = Field(default=500, ge=100, le=10000)


class AndroidOptimizationConfig(BaseModel):
    """Configuration optimisation Android."""
    max_memory_mb: int = Field(default=1024, ge=256, le=8192)
    chunk_size: int = Field(default=10000, ge=1000, le=100000)
    enable_gc: bool = True
    gc_threshold: int = Field(default=800, ge=100, le=2048)
    cache_indicators: bool = True
    cache_atr: bool = True
    atr_cache_mode: Literal["adaptive", "fixed", "minimal"] = "adaptive"
    max_cache_size_mb: int = Field(default=100, ge=10, le=1024)
    memory_monitoring: MemoryMonitoringConfig = Field(default_factory=MemoryMonitoringConfig)
    performance: PerformanceConfig = Field(default_factory=PerformanceConfig)


class TestingFixturesConfig(BaseModel):
    """Configuration fixtures tests."""
    use_global_fixtures: bool = True
    fixtures_path: str = "tests/conftest.py"


class TestingConfig(BaseModel):
    """Configuration testing."""
    run_tests_on_start: bool = False
    coverage_threshold: float = Field(default=80.0, ge=0.0, le=100.0)
    test_output_path: str = "tests/results/"
    fixtures: TestingFixturesConfig = Field(default_factory=TestingFixturesConfig)


class DevelopmentConfig(BaseModel):
    """Configuration développement."""
    debug_mode: bool = False
    verbose_logging: bool = False
    profile_performance: bool = False
    save_debug_data: bool = False
    hot_reload: bool = False
    auto_restart_on_error: bool = False


# ============================================================================
# ENGINE CONFIG
# ============================================================================

class MarketContextConfig(BaseModel):
    """Configuration contexte marché dans engine."""
    enabled: bool = False


class OhlcvDataValidatorConfig(BaseModel):
    """Configuration validateur de données OHLCV."""
    enabled: bool = False


class OhlcvDataProcessorConfig(BaseModel):
    """Configuration processeur de données OHLCV."""
    enabled: bool = False


class OhlcvDataEngineConfig(BaseModel):
    """
    Configuration moteur de données OHLCV.
    v2.3.6: Nouvelle sous-section de engine_config
    """
    data_validator: OhlcvDataValidatorConfig = Field(
        default_factory=OhlcvDataValidatorConfig,
        description="Configuration validateur de données OHLCV"
    )
    data_processor: OhlcvDataProcessorConfig = Field(
        default_factory=OhlcvDataProcessorConfig,
        description="Configuration processeur de données OHLCV"
    )


class EngineConfig(BaseModel):
    """
    Configuration moteur de backtesting/trading.
    v2.3.6: Ajout ohlcv_data_engine
    """
    min_candles_window: int = Field(
        default=300,
        ge=1,
        description="Nombre minimum de bougies dans la fenêtre glissante"
    )
    max_candles_window: int = Field(
        default=500,
        ge=1,
        description="Nombre maximum de bougies dans la fenêtre glissante"
    )
    accumulate_funding: bool = Field(
        default=True,
        description="Accumuler les funding rates dans le P&L"
    )
    close_position_on_session_end: bool = Field(
        default=True,
        description="Fermer les positions ouvertes à la fin de chaque session"
    )
    log_every_n_candles: int = Field(
        default=1000,
        ge=1,
        description="Fréquence de logging (toutes les N bougies)"
    )
    market_context_min_candles: int = Field(
        default=300,
        ge=1,
        description="Nombre minimum de bougies pour le contexte marché"
    )
    market_context: MarketContextConfig = Field(
        default_factory=MarketContextConfig,
        description="Configuration contexte marché"
    )
    ohlcv_data_engine: OhlcvDataEngineConfig = Field(
        default_factory=OhlcvDataEngineConfig,
        description="Configuration moteur de données OHLCV"
    )

    @model_validator(mode='after')
    def validate_candles_window(self):
        """Valider que min <= max pour la fenêtre de bougies."""
        if self.min_candles_window > self.max_candles_window:
            raise ValueError(
                f"min_candles_window ({self.min_candles_window}) doit être <= "
                f"max_candles_window ({self.max_candles_window})"
            )
        return self


class BulletConfig(BaseModel):
    """
    Configuration complète BULLET-1 v2.3.6.
    
    🆕 v2.3.6:
    - Ajout OhlcvDataValidatorConfig, OhlcvDataProcessorConfig, OhlcvDataEngineConfig
    - Ajout champ ohlcv_data_engine dans EngineConfig
    
    🆕 v2.3.5:
    - Ajout SignalSideReverserConfig + champ signal_side_reverser dans SignalGeneratorConfig
    """
    config_version: str = "2.3.6"
    general: GeneralConfig = Field(default_factory=GeneralConfig)
    session_management: SessionConfig = Field(default_factory=SessionConfig)
    capital: CapitalConfig = Field(default_factory=CapitalConfig)
    position: PositionConfig = Field(default_factory=PositionConfig)
    risk_management: RiskManagementConfig = Field(default_factory=RiskManagementConfig)
    strategy: StrategyConfig
    engine_config: EngineConfig = Field(
        default_factory=EngineConfig,
        description="Configuration moteur backtesting/trading"
    )
    backtesting: BacktestingConfig = Field(default_factory=BacktestingConfig)
    live_trading: LiveTradingConfig = Field(default_factory=LiveTradingConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    optimization: OptimizationConfig = Field(default_factory=OptimizationConfig)
    performance_targets: PerformanceTargetsConfig = Field(default_factory=PerformanceTargetsConfig)
    android_optimization: AndroidOptimizationConfig = Field(default_factory=AndroidOptimizationConfig)
    testing: TestingConfig = Field(default_factory=TestingConfig)
    development: DevelopmentConfig = Field(default_factory=DevelopmentConfig)
    
    @model_validator(mode='after')
    def validate_mode_specific_requirements(self):
        """
        Validations strictes selon mode (backtest/paper/live).
        
        MODE LIVE:
        - reset_capital DOIT être false
        - credentials DOIVENT être chargées (validé dans load_config)
        - sync_positions_on_startup DOIT être true
        
        MODE PAPER:
        - enable_websocket DOIT être true
        - use_real_prices DOIT être true
        """
        mode = self.general.mode
        
        # ========================================
        # MODE LIVE - VALIDATIONS STRICTES
        # ========================================
        if mode == 'live':
            # 1. Reset capital INTERDIT
            if self.session_management.reset_capital_between_sessions:
                raise ValueError(
                    "❌ MODE LIVE: reset_capital_between_sessions MUST be false. "
                    "Capital réel ne peut JAMAIS être reset automatiquement. "
                    "Corrigez dans config.json: session_management.reset_capital_between_sessions = false"
                )
            
            # 2. Sync positions OBLIGATOIRE
            if not self.live_trading.synchronization.sync_positions_on_start:
                raise ValueError(
                    "❌ MODE LIVE: sync_positions_on_start MUST be true. "
                    "Synchronisation positions requise pour éviter états incohérents. "
                    "Corrigez dans config.json: live_trading.synchronization.sync_positions_on_start = true"
                )
        
        # ========================================
        # MODE PAPER - VALIDATIONS
        # ========================================
        elif mode == 'paper':
            # 1. WebSocket REQUIS pour prix temps réel
            if not self.live_trading.enable_websocket:
                raise ValueError(
                    "❌ MODE PAPER: enable_websocket MUST be true. "
                    "WebSocket requis pour recevoir prix temps réel. "
                    "Corrigez dans config.json: live_trading.enable_websocket = true"
                )
            
            # 2. Real prices REQUIS
            if not self.live_trading.use_real_prices:
                raise ValueError(
                    "❌ MODE PAPER: use_real_prices MUST be true. "
                    "Prix réels requis pour simulation réaliste. "
                    "Corrigez dans config.json: live_trading.use_real_prices = true"
                )
        
        return self


# ============================================================================
# CREDENTIALS MODELS
# ============================================================================

class BinanceTestnetCredentials(BaseModel):
    """Credentials Binance Testnet."""
    api_key: str
    api_secret: str
    enabled: bool = True
    base_url: str = "https://testnet.binancefuture.com"


class BinanceSecurityConfig(BaseModel):
    """
    Configuration sécurité Binance Live.
    🆕 v2.3.0 - Validation permissions stricte
    """
    ip_whitelist: List[str] = Field(default_factory=list)
    permissions: List[str] = Field(
        default_factory=lambda: ["SPOT_TRADING", "FUTURES_TRADING"],
        description="Permissions API (JAMAIS 'WITHDRAW' en production)"
    )
    restrict_to_trusted_ips: bool = True
    
    @field_validator('permissions')
    @classmethod
    def validate_no_withdraw_permission(cls, v: List[str]) -> List[str]:
        """
        🆕 v2.3.0 - Interdire WITHDRAW en production.
        
        Règle CRITIQUE sécurité: API keys ne doivent JAMAIS avoir
        permission WITHDRAW pour éviter vols de fonds.
        """
        forbidden = ['WITHDRAW', 'WITHDRAWAL']
        for perm in v:
            if perm.upper() in forbidden:
                raise ValueError(
                    f"❌ SÉCURITÉ CRITIQUE: Permission '{perm}' INTERDITE. "
                    f"API keys ne doivent JAMAIS avoir permission WITHDRAW. "
                    f"Permissions autorisées: SPOT_TRADING, FUTURES_TRADING, MARGIN_TRADING. "
                    f"Retirez '{perm}' de credentials.json > binance.live.security.permissions"
                )
        return v


class BinanceLiveCredentials(BaseModel):
    """
    Credentials Binance Live.
    🆕 v2.3.0 - Validation stricte api_key/secret non vides
    """
    api_key: str = Field(
        description="API Key Binance (REQUIS en MODE LIVE)"
    )
    api_secret: str = Field(
        description="API Secret Binance (REQUIS en MODE LIVE)"
    )
    enabled: bool = False
    base_url: str = "https://fapi.binance.com"
    security: BinanceSecurityConfig = Field(default_factory=BinanceSecurityConfig)
    
    @field_validator('api_key', 'api_secret')
    @classmethod
    def validate_not_empty(cls, v: str, info) -> str:
        """
        🆕 v2.3.0 - Valider que API key/secret ne sont pas vides.
        
        Note: Validation appelée si mode='live' dans load_config().
        """
        field_name = info.field_name
        if not v or v.strip() == '':
            raise ValueError(
                f"❌ MODE LIVE: {field_name} ne peut pas être vide. "
                f"Fournissez une {field_name} valide dans credentials.json > binance.live.{field_name}"
            )
        return v.strip()


class BinanceCredentials(BaseModel):
    """Credentials Binance (testnet + live)."""
    testnet: BinanceTestnetCredentials
    live: BinanceLiveCredentials


class EmailCredentials(BaseModel):
    """Credentials Email."""
    smtp_username: str
    smtp_password: str
    from_email: str
    to_email: str


class DiscordCredentials(BaseModel):
    """Credentials Discord."""
    webhook_url: str
    bot_token: str = ""
    channel_id: str = ""


class TelegramCredentials(BaseModel):
    """Credentials Telegram."""
    bot_token: str
    chat_id: str
    enabled: bool = False


class NotificationCredentials(BaseModel):
    """Credentials notifications."""
    email: EmailCredentials
    discord: DiscordCredentials
    telegram: TelegramCredentials


class PostgresCredentials(BaseModel):
    """Credentials PostgreSQL."""
    host: str = "localhost"
    port: int = 5432
    database: str = "bullet1_db"
    username: str = "bullet1_user"
    password: str
    enabled: bool = False


class MongoDBCredentials(BaseModel):
    """Credentials MongoDB."""
    connection_string: str = "mongodb://localhost:27017/"
    database: str = "bullet1"
    username: str = "bullet1_user"
    password: str
    enabled: bool = False


class DatabaseCredentials(BaseModel):
    """Credentials bases de données."""
    postgres: PostgresCredentials
    mongodb: MongoDBCredentials


class SentryCredentials(BaseModel):
    """Credentials Sentry."""
    dsn: str
    enabled: bool = False


class DatadogCredentials(BaseModel):
    """Credentials Datadog."""
    api_key: str
    app_key: str
    enabled: bool = False


class MonitoringCredentials(BaseModel):
    """Credentials monitoring."""
    sentry: SentryCredentials
    datadog: DatadogCredentials


class EncryptionConfig(BaseModel):
    """Configuration encryption."""
    master_key: str
    salt: str


class Credentials(BaseModel):
    """Credentials complètes BULLET-1."""
    binance: BinanceCredentials
    notifications: NotificationCredentials
    database: DatabaseCredentials
    monitoring: MonitoringCredentials
    encryption: EncryptionConfig


# ============================================================================
# FONCTIONS UTILITAIRES
# ============================================================================

def detect_configuration_name(config_dict: Dict[str, Any]) -> str:
    """Détecter automatiquement le nom de configuration (1-8)."""
    try:
        entry = config_dict['strategy']['entry_logic']
        direction = entry['logic_direction']
        short_op = entry['for_short_case_comparison_operator']
        long_op = entry['for_long_case_comparison_operator']
        
        configs = {
            ('normal', '>', '>'): '1-normal',
            ('normal', '>', '<'): '2-normal',
            ('normal', '<', '>'): '3-normal',
            ('normal', '<', '<'): '4-normal',
            ('reverse', '<', '>'): '5-reverse',
            ('reverse', '>', '<'): '6-reverse',
            ('reverse', '<', '<'): '7-reverse',
            ('reverse', '>', '>'): '8-reverse',
        }
        
        key = (direction, short_op, long_op)
        
        if key not in configs:
            raise ValueError(
                f"Combinaison configuration invalide: "
                f"direction={direction}, short={short_op}, long={long_op}"
            )
        
        return configs[key]
    
    except KeyError as e:
        raise ValueError(f"Champ manquant dans configuration: {e}")


def merge_env_vars(config_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Fusionner variables d'environnement dans config."""
    if 'BULLET1_MODE' in os.environ:
        mode = os.environ['BULLET1_MODE']
        if mode in ['backtest', 'paper', 'live']:
            config_dict['general']['mode'] = mode
    
    if 'BULLET1_LEVERAGE' in os.environ:
        try:
            leverage = int(os.environ['BULLET1_LEVERAGE'])
            config_dict['position']['leverage'] = leverage
        except ValueError:
            pass
    
    if 'BULLET1_CAPITAL' in os.environ:
        try:
            capital = float(os.environ['BULLET1_CAPITAL'])
            config_dict['capital']['initial_capital_backtest'] = capital
        except ValueError:
            pass
    
    if 'BULLET1_LOG_LEVEL' in os.environ:
        level = os.environ['BULLET1_LOG_LEVEL'].upper()
        if level in ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL', 'AUTO']:
            config_dict['logging']['level'] = level
    
    return config_dict


def merge_credentials(config_dict: Dict[str, Any], creds_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Fusionner credentials dans config."""
    config_dict['credentials'] = creds_dict
    return config_dict


# ============================================================================
# FONCTION PRINCIPALE - LOAD CONFIG v2.3.3
# ============================================================================

def load_config(
    config_path: Optional[str] = None,
    credentials_path: Optional[str] = None,
    validate: bool = True
) -> BulletConfig:
    """
    Charger et valider configuration BULLET-1.
    
    🆕 v2.3.3 CHANGEMENTS:
    - configuration_name vide/absent → auto-déduction depuis les opérateurs
    - configuration_name invalide (hors des 8 noms valides) → erreur fatale
      avec exposition de la valeur correcte attendue
    
    🆕 v2.3.2 CHANGEMENTS (historique):
    - Suppression champs orphelins (non alignés avec config.json)
    
    Étapes:
    1. Charger config.json
    2. Charger credentials.json (si mode != backtest)
    3. Valider / déduire configuration_name  ← 🆕 v2.3.3
    4. Fusionner env vars
    5. Fusionner credentials
    6. Valider avec Pydantic (strict v2.3.3)
    7. Validations MODE-spécifiques (live/paper)
    8. Logger warnings et infos
    
    Args:
        config_path: Chemin config.json (défaut: config/config.json)
        credentials_path: Chemin credentials.json (défaut: config/credentials.json)
        validate: Si True, valider avec Pydantic
    
    Returns:
        BulletConfig: Configuration validée v2.3.3
    
    Raises:
        FileNotFoundError: Si fichiers manquants
        ValidationError: Si validation Pydantic échoue
        ValueError: Si configuration invalide (mode-specific ou configuration_name)
    """
    try:
        logger = BulletLogger()
    except Exception:
        logger = None
    
    project_root = get_project_root()
    
    if config_path is None:
        config_path = project_root / 'config' / 'config.json'
    else:
        config_path = Path(config_path)
    
    if credentials_path is None:
        credentials_path = project_root / 'config' / 'credentials.json'
    else:
        credentials_path = Path(credentials_path)
    
    if logger:
        logger.info(f"📂 Loading config v2.3.3: {config_path}")
    
    try:
        config_dict = read_json(config_path)
    except FileNotFoundError:
        raise FileNotFoundError(
            f"Config file not found: {config_path}\n"
            f"Please create config/config.json"
        )
    
    # Support multi-versions
    config_version = config_dict.get('config_version', 'unknown')
    supported_versions = ['2.2.3', '2.2.4', '2.2.5', '2.3.0', '2.3.1', '2.3.2', '2.3.3', '2.3.4', '2.3.5', '2.3.6']
    
    if config_version not in supported_versions:
        raise ValueError(
            f"❌ VERSION NON SUPPORTÉE: Config version '{config_version}' détectée.\n"
            f"Versions supportées: {', '.join(supported_versions)}\n"
            f"Config Loader v{_RUNTIME_VERSION} nécessite structure config >= 2.2.3.\n"
            f"Veuillez mettre à jour config.json selon MIGRATION.md"
        )
    
    # Charger credentials (sauf si mode backtest pur)
    mode = config_dict.get('general', {}).get('mode', 'backtest')
    
    if mode in ['paper', 'live']:
        if logger:
            logger.info(f"🔐 Loading credentials (MODE {mode.upper()}): {credentials_path}")
        
        try:
            creds_dict = read_json(credentials_path)
        except FileNotFoundError:
            raise FileNotFoundError(
                f"❌ MODE {mode.upper()}: Credentials file not found: {credentials_path}\n"
                f"MODE {mode.upper()} requiert credentials.json pour API access.\n"
                f"Please create config/credentials.json (NEVER commit!)"
            )
    else:
        # Mode backtest - credentials optionnels
        if credentials_path.exists():
            if logger:
                logger.debug(f"🔐 Loading credentials (MODE BACKTEST): {credentials_path}")
            try:
                creds_dict = read_json(credentials_path)
            except Exception as e:
                if logger:
                    logger.warning(f"⚠️ Failed to load credentials in BACKTEST mode: {e}")
                creds_dict = None
        else:
            if logger:
                logger.debug("MODE BACKTEST: credentials.json not required")
            creds_dict = None
    
    # ============================================================================
    # 🆕 v2.3.3 — VALIDATION / AUTO-DÉTECTION configuration_name
    # ============================================================================
    declared_name: str = config_dict.get('strategy', {}).get('configuration_name', '')

    if not declared_name:
        # ── CAS 1 : vide ou absent → auto-déduction depuis les opérateurs ──────
        if logger:
            logger.info("🔍 configuration_name vide/absent — auto-détection depuis les opérateurs...")

        config_name = detect_configuration_name(config_dict)
        config_dict['strategy']['configuration_name'] = config_name

        if logger:
            logger.info(f"✅ configuration_name auto-détecté: '{config_name}'")

    elif declared_name not in _VALID_CONFIG_NAMES:
        # ── CAS 2 : valeur non-vide mais invalide → erreur fatale ───────────────
        expected_name = detect_configuration_name(config_dict)
        entry = config_dict.get('strategy', {}).get('entry_logic', {})

        raise ValueError(
            f"\n"
            f"{'=' * 70}\n"
            f"❌ FATAL ERROR: configuration_name INVALIDE\n"
            f"{'=' * 70}\n"
            f"\n"
            f"  Valeur déclarée dans config.json : '{declared_name}'\n"
            f"  Valeur correcte attendue         : '{expected_name}'\n"
            f"\n"
            f"  D'après vos opérateurs configurés :\n"
            f"    logic_direction                    = '{entry.get('logic_direction', '?')}'\n"
            f"    for_short_case_comparison_operator = '{entry.get('for_short_case_comparison_operator', '?')}'\n"
            f"    for_long_case_comparison_operator  = '{entry.get('for_long_case_comparison_operator', '?')}'\n"
            f"\n"
            f"  Noms valides : {sorted(_VALID_CONFIG_NAMES)}\n"
            f"\n"
            f"  SOLUTION :\n"
            f"    Option 1 — Corriger le nom dans config.json :\n"
            f"      strategy.configuration_name = '{expected_name}'\n"
            f"\n"
            f"    Option 2 — Laisser vide pour auto-détection :\n"
            f"      strategy.configuration_name = ''\n"
            f"\n"
            f"{'=' * 70}\n"
        )

    # ── CAS 3 : valeur syntaxiquement valide → vérifier cohérence avec les opérateurs ─
    else:
        expected_name = detect_configuration_name(config_dict)
        if declared_name != expected_name:
            entry = config_dict.get('strategy', {}).get('entry_logic', {})
            raise ValueError(
                f"\n"
                f"{'=' * 70}\n"
                f"❌ FATAL ERROR: configuration_name INCOHÉRENT avec les opérateurs\n"
                f"{'=' * 70}\n"
                f"\n"
                f"  Valeur déclarée dans config.json : '{declared_name}'\n"
                f"  Valeur correcte attendue         : '{expected_name}'\n"
                f"\n"
                f"  D'après vos opérateurs configurés :\n"
                f"    logic_direction                    = '{entry.get('logic_direction', '?')}'\n"
                f"    for_short_case_comparison_operator = '{entry.get('for_short_case_comparison_operator', '?')}'\n"
                f"    for_long_case_comparison_operator  = '{entry.get('for_long_case_comparison_operator', '?')}'\n"
                f"\n"
                f"  Le nom '{declared_name}' est syntaxiquement valide mais ne correspond\n"
                f"  PAS à la combinaison d'opérateurs active — incohérence silencieuse\n"
                f"  qui fausserait le comportement du système.\n"
                f"\n"
                f"  SOLUTION :\n"
                f"    Option 1 — Corriger le nom pour qu'il corresponde aux opérateurs :\n"
                f"      strategy.configuration_name = '{expected_name}'\n"
                f"\n"
                f"    Option 2 — Corriger les opérateurs pour qu'ils correspondent au nom :\n"
                f"      Consultez la table des 8 configurations dans config.json\n"
                f"\n"
                f"    Option 3 — Laisser vide pour auto-détection automatique :\n"
                f"      strategy.configuration_name = ''\n"
                f"\n"
                f"{'=' * 70}\n"
            )
        if logger:
            logger.info(f"✅ configuration_name valide et cohérent avec les opérateurs: '{declared_name}'")

    # ============================================================================
    # Fusion env vars
    # ============================================================================
    if logger:
        logger.debug("🔄 Merging environment variables...")
    
    config_dict = merge_env_vars(config_dict)
    
    # Fusion credentials
    if creds_dict:
        if logger:
            logger.debug("🔄 Merging credentials...")
        
        config_dict = merge_credentials(config_dict, creds_dict)
    
    # Validation Pydantic
    if validate:
        if logger:
            logger.info("✅ Validating configuration v2.3.6 with Pydantic...")
        
        try:
            config = BulletConfig(**config_dict)
        except ValidationError as e:
            if logger:
                logger.error(f"❌ Configuration validation failed:\n{e}")
            raise
        
        # ========================================
        # 🆕 v2.3.0 - VALIDATIONS MODE-SPÉCIFIQUES
        # ========================================
        
        if config.general.mode == 'live':
            # MODE LIVE - Validations additionnelles
            if logger:
                logger.info("🔒 MODE LIVE: Performing strict validations...")
            
            # Valider credentials présentes
            if not hasattr(config, 'credentials') or not config.credentials:
                raise ValueError(
                    "❌ MODE LIVE: Credentials manquantes. "
                    "Chargez credentials.json avec API keys Binance."
                )
            
            testnet_enabled = config.credentials.binance.testnet.enabled
            live_enabled = config.credentials.binance.live.enabled
            
            if not testnet_enabled and not live_enabled:
                raise ValueError(
                    "❌ MODE LIVE: Aucune API Binance activée. "
                    "Activez credentials.binance.testnet.enabled OU credentials.binance.live.enabled"
                )
            
            # Warning si validate_balance_on_startup = false
            if not config.live_trading.synchronization.validate_balance_on_startup:
                if logger:
                    logger.warning(
                        "⚠️ MODE LIVE: validate_balance_on_startup=false. "
                        "RECOMMANDÉ: Activer pour vérifier balance au démarrage. "
                        "Corrigez dans config.json: live_trading.synchronization.validate_balance_on_startup = true"
                    )
            
            if logger:
                logger.info("✅ MODE LIVE: All strict validations passed")
        
        elif config.general.mode == 'paper':
            # MODE PAPER - Validations additionnelles
            if logger:
                logger.info("📝 MODE PAPER: Performing validations...")
            
            # Warning si reset_capital = true
            if config.session_management.reset_capital_between_sessions:
                if logger:
                    logger.warning(
                        "⚠️ MODE PAPER: reset_capital_between_sessions=true. "
                        "RECOMMANDÉ: false pour continuité capital entre sessions. "
                        "Corrigez dans config.json: session_management.reset_capital_between_sessions = false"
                    )
            
            if logger:
                logger.info("✅ MODE PAPER: All validations passed")
        
        # ========================================
        # LOGGING INFORMATIONS CONFIGURATION
        # ========================================
        
        trailing = config.strategy.trailing_stop
        
        if trailing.type == 'hybrid':
            if logger:
                logger.info(
                    f"✨ Mode HYBRID activé: start_with={trailing.hybrid_mode.start_with}, "
                    f"switch_after={trailing.hybrid_mode.switch_to_candle_after}R"
                )
        
        if trailing.atr_mode.progressive_tightening:
            if logger:
                logger.info(
                    f"✨ Progressive tightening activé: {len(trailing.atr_mode.stages)} stages"
                )
        
        if trailing.protection_1r.asymmetric_mode:
            if logger:
                logger.info(
                    f"✨ Protection asymétrique: LONG={trailing.protection_1r.long_breakeven_threshold}R, "
                    f"SHORT={trailing.protection_1r.short_breakeven_threshold}R"
                )
        
        if trailing.volatility_adjustment.enabled:
            if logger:
                logger.info(
                    f"✨ Volatility adjustment activé: high={trailing.volatility_adjustment.atr_threshold_high}%, "
                    f"multiplier_increase={trailing.volatility_adjustment.multiplier_increase}x"
                )
        
        # Volume confirmation
        vol_conf = config.strategy.volume_confirmation
        if vol_conf.enabled:
            if logger:
                logger.info(
                    f"✨ Volume NIVEAU-2 activé: mode={vol_conf.mode}, "
                    f"min_ratio={getattr(vol_conf, vol_conf.mode).min_ratio}"
                )
        
        # Daily limits
        if config.session_management.daily_limits.enabled:
            if logger:
                logger.info(
                    f"✨ Daily limits activé: max_loss={config.session_management.daily_limits.max_loss_per_day_pct}%, "
                    f"max_trades={config.session_management.daily_limits.max_trades_per_day}"
                )
        
        if logger:
            logger.info("=" * 80)
            logger.info("📊 CONFIGURATION LOADED SUCCESSFULLY")
            logger.info("=" * 80)
            logger.info(f"   Mode: {config.general.mode.upper()}")
            logger.info(f"   Config: {config.strategy.configuration_name}")
            logger.info(f"   Min Quality Score: {config.strategy.min_quality_score}")
            logger.info(f"   Trailing: {config.strategy.trailing_stop.type}")
            logger.info(f"   Session: {config.session_management.trades_period_days} days")
            logger.info(f"   Reset Capital: {config.session_management.reset_capital_between_sessions}")
            logger.info(f"   Leverage: {config.position.leverage}x")
            logger.info(f"   Risk/Reward: {config.risk_management.risk_reward_ratio}")
            logger.info(f"   Breakout mode: {config.strategy.signal_generator.breakout_detection_mode}")
            logger.info(f"   Volume NIVEAU-2: enabled={vol_conf.enabled}, mode={vol_conf.mode}")
            logger.info("=" * 80)
        
        return config
    
    else:
        # Retourner dict brut sans validation
        return config_dict


# ============================================================================
# FIN DU MODULE
# ============================================================================