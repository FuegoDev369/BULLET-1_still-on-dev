"""
BULLET-1 - Metrics Module
==========================

Calcul complet des métriques de performance trading crypto futures.
Support backtest, paper, et live trading.

Version: 2.2.8
Date: 2026-04-24
Author: FuegoDev
"""

import json
import csv
import math
import threading
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union, Literal
import numpy as np
import sys

# Trouver la racine du projet
# [v2.2.6 — FIX-PATH-6] Résolution racine projet : pattern direct unifié.
# Remplace find_project_root() locale dupliquée dans ~10 modules (DRY).
# Calcul en 1 ligne depuis __file__ — sans fonction nommée, sans import.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# BULLET-1 IMPORTS
from src.utils.logger import BulletLogger
from src.utils.helpers import format_percentage, ensure_directory, safe_divide


# ============================================================================
# CONSTANTES
# ============================================================================

_VALID_EXPORT_FORMATS: frozenset = frozenset({'json', 'csv', 'text'})
_RISK_FREE_RATE_DEFAULT: float   = 0.0
_TRADING_DAYS_PER_YEAR: int      = 365   # Crypto 24/7
_HOURS_PER_YEAR: int             = 8760  # 365 × 24
_JSON_INF_SENTINEL: float        = 999.0 # Remplace float('inf') en JSON
_CAGR_MIN_DAYS: int              = 30    # Durée minimale pour un CAGR significatif

# Seuil minimum trades pour que les ratios soient statistiquement valides
_MIN_TRADES_FOR_RATIOS: int = 5


# ============================================================================
# HELPERS INTERNES
# ============================================================================

def _rf_per_period(annual_rf: float, period: str) -> float:
    """Risk-free rate ramenée à la période cible."""
    if period == 'daily':
        return annual_rf / _TRADING_DAYS_PER_YEAR
    if period == 'hourly':
        return annual_rf / _HOURS_PER_YEAR
    return 0.0  # 'trade' : pas d'ajustement per-trade


def _safe_json(value: Any) -> Any:
    """Rend une valeur sérialisable en JSON (gère inf, nan, datetime)."""
    if isinstance(value, float):
        if math.isinf(value):
            return _JSON_INF_SENTINEL if value > 0 else -_JSON_INF_SENTINEL
        if math.isnan(value):
            return None
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _sanitize_for_json(data: Any) -> Any:
    """
    Applique _safe_json récursivement sur dicts, listes, et valeurs scalaires.

    [v2.2.7 — FIX-MET-3] L'ancienne implémentation n'aplainait que le premier
    niveau du dict. Les sous-dicts (long_stats, short_stats) contenant
    float('inf') n'étaient pas sanitisés → json.dump utilisait default=str
    → 'inf' (chaîne) au lieu du sentinel 999.0, incohérence de rapport.
    """
    if isinstance(data, dict):
        return {k: _sanitize_for_json(v) for k, v in data.items()}
    if isinstance(data, list):
        return [_sanitize_for_json(item) for item in data]
    return _safe_json(data)


# ============================================================================
# CLASSE PRINCIPALE
# ============================================================================

class Metrics:
    """
    Calculateur de métriques de performance trading crypto futures.

    Calcule l'ensemble des métriques nécessaires à l'évaluation rigoureuse
    d'une stratégie en contexte futures (perpetuals inclus) :

    Win/Loss      : Win Rate, Profit Factor, Avg Win/Loss, R-Ratio, Expectancy
    Risk-adjusted : Sharpe, Sortino (correct), Calmar (CAGR-based), Kelly
    Drawdown      : Max Drawdown %, Max Drawdown $, Recovery Factor
    Rendement     : CAGR, Annualized Return %, Total PnL, Gross P&L, Fees
    Séquences     : Max Consecutive Wins/Losses, Streak Loss Drawdown
    Temps         : Avg Holding (all / winners / losers), Trades/day
    Directionnels : Stats Long vs Short
    Qualité       : MAE, MFE, Best/Worst trade

    Notes importantes:
        - Sharpe/Sortino calculés sur les % returns par trade, pas sur
          les montants USDT bruts (non comparables entre stratégies sinon).
        - Si 'return_pct' absent, calculé automatiquement depuis
          'capital_before' ou 'initial_margin'.
        - CAGR et Calmar requièrent entry_time / exit_time dans les trades.
        - initial_capital est requis pour tous les calculs en % corrects.

    Thread-safety:
        Toutes les opérations mutatrices sont protégées par RLock.
        calculate_all() travaille sur un snapshot — lock non tenu pendant
        les calculs.

    Args:
        mode:            'backtest', 'paper', ou 'live'
        initial_capital: Capital de départ (USDT). Requis pour CAGR, Calmar,
                         Recovery Factor corrects.
        risk_free_rate:  Taux sans risque annuel en décimal (ex: 0.02 = 2%)

    Examples:
        >>> metrics = Metrics(mode='backtest', initial_capital=10_000.0)
        >>>
        >>> metrics.add_trade({
        ...     'pnl_net':      43.50,
        ...     'return_pct':   0.00435,
        ...     'entry_time':   datetime(2026, 2, 17, 10, 0),
        ...     'exit_time':    datetime(2026, 2, 17, 14, 30),
        ...     'is_winner':    True,
        ...     'side':         'long',
        ...     'fees':         1.20,
        ... })
        >>>
        >>> results = metrics.calculate_all()
        >>> metrics.export('results/metrics.json', fmt='json')
    """

    def __init__(
        self,
        mode: str = 'backtest',
        initial_capital: float = 1_000.0,
        risk_free_rate: float = _RISK_FREE_RATE_DEFAULT,
    ) -> None:
        if initial_capital <= 0:
            raise ValueError(f"initial_capital must be > 0, got {initial_capital}")

        self.logger          = BulletLogger()
        self.mode            = mode
        self.initial_capital = initial_capital
        self.risk_free_rate  = risk_free_rate

        self._lock: threading.RLock = threading.RLock()

        self._trades: List[Dict[str, Any]]       = []
        self._equity_curve: List[Dict[str, Any]] = []

        self.logger.info(
            f"Metrics v3 initialized | mode={mode} | "
            f"capital={initial_capital:,.2f} USDT | "
            f"rf={risk_free_rate * 100:.2f}%"
        )

    # ========================================================================
    # AJOUT / REMPLACEMENT DONNÉES
    # ========================================================================

    def add_trade(self, trade: Dict[str, Any]) -> None:
        """
        Ajoute un trade fermé.

        Deepcopy pour isolation totale. Si 'return_pct' absent, il est
        calculé automatiquement depuis 'capital_before' ou 'initial_margin'.

        Args:
            trade: Dict trade fermé (cf. docstring module).

        Raises:
            ValueError: Si 'pnl_net' est absent.
        """
        if 'pnl_net' not in trade:
            raise ValueError("Trade must contain 'pnl_net'")

        trade_copy = deepcopy(trade)

        # Injection is_winner — flag requis par calculate_win_rate(),
        # calculate_directional_stats(), winning_trades/losing_trades, etc.
        # Posé ici car trading_engine ne le set pas dans le trade record.
        trade_copy['is_winner'] = trade_copy.get('pnl_net', 0.0) > 0

        # Calcul auto return_pct si absent
        if 'return_pct' not in trade_copy:
            capital_ref = trade_copy.get('capital_before') or trade_copy.get('initial_margin')
            if capital_ref and capital_ref > 0:
                trade_copy['return_pct'] = trade_copy['pnl_net'] / capital_ref

        with self._lock:
            self._trades.append(trade_copy)
            count = len(self._trades)  # Lu sous lock — pas de race condition

        self.logger.debug(
            f"Trade added | PnL={trade['pnl_net']:+.2f} USDT | total={count}"
        )

    def add_equity_point(self, timestamp: datetime, equity: float) -> None:
        """
        Ajoute un point à la courbe d'equity.

        Args:
            timestamp: Datetime du point (timezone-aware recommandé)
            equity:    Capital total à cet instant (USDT)
        """
        with self._lock:
            self._equity_curve.append({'timestamp': timestamp, 'equity': equity})

    def set_trades(self, trades: List[Dict[str, Any]]) -> None:
        """Remplace la liste de trades par une nouvelle (deepcopy).

        Injecte is_winner sur chaque trade si absent — flag requis par toutes
        les métriques win/loss. Source : pnl_net > 0.
        """
        copied = deepcopy(trades)
        for t in copied:
            if 'is_winner' not in t:
                t['is_winner'] = t.get('pnl_net', 0.0) > 0
        with self._lock:
            self._trades = copied
        self.logger.info(f"Trades replaced | count={len(trades)}")

    def set_equity_curve(self, equity_curve: List[Dict[str, Any]]) -> None:
        """Remplace la courbe equity complète (deepcopy)."""
        copied = deepcopy(equity_curve)
        with self._lock:
            self._equity_curve = copied
        self.logger.info(f"Equity curve replaced | points={len(equity_curve)}")

    # ========================================================================
    # HELPERS INTERNES
    # ========================================================================

    def _get_trades_snapshot(self) -> List[Dict[str, Any]]:
        """Snapshot thread-safe de la liste des trades."""
        with self._lock:
            return list(self._trades)

    def _get_full_snapshot(self) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Snapshot atomique de (trades, equity_curve) — 1 seul lock."""
        with self._lock:
            return list(self._trades), list(self._equity_curve)

    @staticmethod
    def _ensure_utc_dt(dt: datetime) -> datetime:
        """
        Normalise un datetime en UTC-aware.

        [v2.2.7 — FIX-MET-1/2] Nécessaire pour éviter TypeError lors des
        comparaisons ou soustractions entre datetimes naïfs (CSV) et aware
        (live). Naïf → assume UTC (convention backtest). Non-UTC → converti.

        [v2.2.8 — FIX-MET-5] Gère les strings ISO 8601 produites par la
        désérialisation JSON (optimizer/_collect_trades). Quand les trades
        sont lus depuis session_XXX_trades.json, entry_time/exit_time sont
        des str et non des datetime — ce qui levait :
            AttributeError: 'str' object has no attribute 'tzinfo'
        """
        if isinstance(dt, str):
            dt = datetime.fromisoformat(dt)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    def _extract_returns_pct(self, trades: List[Dict[str, Any]]) -> List[float]:
        """
        Extrait les % returns depuis les trades.

        Ordre de priorité :
            1. return_pct présent dans le trade
            2. pnl_net / capital_before
            3. pnl_net / initial_margin
            4. Fallback : pnl_net / initial_capital (approximation)
        """
        returns: List[float] = []
        for t in trades:
            if 'return_pct' in t:
                returns.append(float(t['return_pct']))
            else:
                ref = t.get('capital_before') or t.get('initial_margin')
                if ref and ref > 0:
                    returns.append(t['pnl_net'] / ref)
                else:
                    returns.append(t.get('pnl_net', 0.0) / self.initial_capital)
        return returns

    def _elapsed_days(self, trades: List[Dict[str, Any]]) -> float:
        """Durée réelle de la session en jours (first entry → last exit)."""
        entries = [t['entry_time'] for t in trades if 'entry_time' in t]
        exits   = [t['exit_time']  for t in trades if 'exit_time'  in t]
        if not entries or not exits:
            return 0.0
        # [v2.2.7 — FIX-MET-1] Normalisation UTC avant comparaison min/max.
        # Sans ce guard, un mélange de datetime naïfs (CSV) et aware (live)
        # lève TypeError. _ensure_utc_dt assume UTC si naïf (standard backtest).
        try:
            entries_utc = [self._ensure_utc_dt(e) for e in entries]
            exits_utc   = [self._ensure_utc_dt(e) for e in exits]
            return max((max(exits_utc) - min(entries_utc)).total_seconds() / 86_400.0, 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _final_equity(self, trades: List[Dict[str, Any]]) -> float:
        """Capital final : depuis equity_curve ou reconstruit depuis trades."""
        with self._lock:
            curve = list(self._equity_curve)
        if curve:
            return curve[-1]['equity']
        return self.initial_capital + sum(t.get('pnl_net', 0.0) for t in trades)

    # ========================================================================
    # MÉTRIQUES — WIN/LOSS
    # ========================================================================

    def calculate_win_rate(
        self, trades: Optional[List[Dict[str, Any]]] = None
    ) -> float:
        """Win Rate en % (0–100). Source : flag 'is_winner'."""
        t = trades if trades is not None else self._get_trades_snapshot()
        if not t:
            return 0.0
        return (sum(1 for x in t if x.get('is_winner', False)) / len(t)) * 100.0

    def calculate_profit_factor(
        self, trades: Optional[List[Dict[str, Any]]] = None
    ) -> float:
        """
        Profit Factor = Gross Profit / |Gross Loss|.

        Returns float('inf') si aucune perte, 0.0 si aucun profit ni perte.
        Note: float('inf') est géré proprement lors de l'export JSON
        via _safe_json() → _JSON_INF_SENTINEL.
        """
        t = trades if trades is not None else self._get_trades_snapshot()
        if not t:
            return 0.0
        gross_p = sum(x['pnl_net'] for x in t if x.get('pnl_net', 0.0) > 0)
        gross_l = abs(sum(x['pnl_net'] for x in t if x.get('pnl_net', 0.0) < 0))
        if gross_l == 0:
            return float('inf') if gross_p > 0 else 0.0
        return gross_p / gross_l

    def calculate_avg_win(
        self, trades: Optional[List[Dict[str, Any]]] = None
    ) -> float:
        """Gain moyen des trades gagnants (USDT)."""
        t    = trades if trades is not None else self._get_trades_snapshot()
        wins = [x['pnl_net'] for x in t if x.get('pnl_net', 0.0) > 0]
        return sum(wins) / len(wins) if wins else 0.0

    def calculate_avg_loss(
        self, trades: Optional[List[Dict[str, Any]]] = None
    ) -> float:
        """Perte moyenne des trades perdants (USDT, valeur absolue positive)."""
        t      = trades if trades is not None else self._get_trades_snapshot()
        losses = [abs(x['pnl_net']) for x in t if x.get('pnl_net', 0.0) < 0]
        return sum(losses) / len(losses) if losses else 0.0

    def calculate_r_ratio(
        self, trades: Optional[List[Dict[str, Any]]] = None
    ) -> float:
        """
        R-Ratio (Payoff Ratio) = avg_win / avg_loss.

        R > 1 : les gains moyens dépassent les pertes moyennes.
        Indépendant du win rate — à combiner avec pour juger l'edge.
        """
        avg_win  = self.calculate_avg_win(trades)
        avg_loss = self.calculate_avg_loss(trades)
        if avg_loss == 0:
            return float('inf') if avg_win > 0 else 0.0
        return avg_win / avg_loss

    def calculate_expectancy(
        self, trades: Optional[List[Dict[str, Any]]] = None
    ) -> float:
        """
        Expectancy = (WR × AvgWin) − (LR × AvgLoss)

        Gain moyen attendu par trade (USDT).
        Positif = stratégie profitable en espérance mathématique.
        """
        t         = trades if trades is not None else self._get_trades_snapshot()
        win_rate  = self.calculate_win_rate(t) / 100.0
        avg_win   = self.calculate_avg_win(t)
        avg_loss  = self.calculate_avg_loss(t)
        loss_rate = 1.0 - win_rate
        return (win_rate * avg_win) - (loss_rate * avg_loss)

    def calculate_kelly_criterion(
        self, trades: Optional[List[Dict[str, Any]]] = None
    ) -> float:
        """
        Kelly Criterion = W − (L / R)

        Fraction optimale du capital à risquer par trade.
        En pratique : appliquer 1/4 ou 1/2 Kelly pour limiter la volatilité.

        Returns:
            Décimal (ex: 0.15 = 15%). Négatif = pas d'edge.
        """
        t        = trades if trades is not None else self._get_trades_snapshot()
        win_rate = self.calculate_win_rate(t) / 100.0
        r_ratio  = self.calculate_r_ratio(t)
        if r_ratio == 0 or math.isinf(r_ratio):
            return 0.0
        return win_rate - ((1.0 - win_rate) / r_ratio)

    # ========================================================================
    # MÉTRIQUES — RATIOS RISQUE/RENDEMENT
    # ========================================================================

    def calculate_sharpe_ratio(
        self,
        period: str = 'trade',
        trades: Optional[List[Dict[str, Any]]] = None,
    ) -> float:
        """
        Sharpe Ratio annualisé.

        IMPORTANT: calculé sur les % returns par trade, pas sur les PnL USDT.
        Un PnL USDT sans context de capital n'est pas normalisé.

        Formule :
            sharpe_raw  = (mean_r − rf_per_period) / std_r(ddof=1)
            sharpe_ann  = sharpe_raw × sqrt(trades_per_year)

        Args:
            period: 'trade' (annualisé via densité réelle de trades),
                    'daily', 'hourly'

        Returns:
            Sharpe annualisé. >1 acceptable, >2 excellent.
            0.0 si moins de _MIN_TRADES_FOR_RATIOS trades.
        """
        t = trades if trades is not None else self._get_trades_snapshot()
        if len(t) < _MIN_TRADES_FOR_RATIOS:
            return 0.0

        returns = np.array(self._extract_returns_pct(t))
        if len(returns) < 2:
            return 0.0

        mean_r = float(np.mean(returns))
        std_r  = float(np.std(returns, ddof=1))
        if std_r == 0:
            return 0.0

        rf         = _rf_per_period(self.risk_free_rate, period)
        sharpe_raw = (mean_r - rf) / std_r

        if period == 'daily':
            return sharpe_raw * math.sqrt(_TRADING_DAYS_PER_YEAR)
        if period == 'hourly':
            return sharpe_raw * math.sqrt(_HOURS_PER_YEAR)

        # period='trade' : annualiser via densité réelle
        elapsed_days = self._elapsed_days(t)
        if elapsed_days < 1:
            return sharpe_raw

        # [FIX-MET-SHARPE] Guard période minimale pour l'annualisation.
        # Sur < 30 jours, sqrt(trades_per_year) peut atteindre ×40 ou plus,
        # rendant le Sharpe annualisé sans signification statistique.
        # Cohérent avec _CAGR_MIN_DAYS = 30 (même raisonnement que le CAGR).
        if elapsed_days < _CAGR_MIN_DAYS:
            return 0.0   # Période trop courte — voir 'sharpe_note' dans les métriques

        trades_per_year = len(t) / elapsed_days * _TRADING_DAYS_PER_YEAR
        return sharpe_raw * math.sqrt(trades_per_year)

    def calculate_sortino_ratio(
        self,
        period: str = 'trade',
        target_return: float = 0.0,
        trades: Optional[List[Dict[str, Any]]] = None,
    ) -> float:
        """
        Sortino Ratio annualisé (formule Romani & Price 1994).

        Downside Deviation correcte :
            DD = sqrt( mean( min(r_i − target, 0)² ) )
            ↑ Le mean est sur TOUS les trades (pas seulement les négatifs).

        Erreur classique (corrigée ici) : filtrer d'abord les returns négatifs
        puis faire std() dessus — cela sous-estime la DD et surestime le ratio.

        Args:
            period:        'trade', 'daily', 'hourly'
            target_return: Minimum Acceptable Return (décimal, défaut 0.0)

        Returns:
            Sortino annualisé. >2 excellent.
        """
        t = trades if trades is not None else self._get_trades_snapshot()
        if len(t) < _MIN_TRADES_FOR_RATIOS:
            return 0.0

        returns = np.array(self._extract_returns_pct(t))
        if len(returns) < 2:
            return 0.0

        mean_r = float(np.mean(returns))

        # Downside deviation — dénominateur = N total
        downside_sq  = np.minimum(returns - target_return, 0.0) ** 2
        downside_dev = math.sqrt(float(np.mean(downside_sq)))

        if downside_dev == 0:
            return float('inf') if mean_r > target_return else 0.0

        rf          = _rf_per_period(self.risk_free_rate, period)
        sortino_raw = (mean_r - rf) / downside_dev

        if period == 'daily':
            return sortino_raw * math.sqrt(_TRADING_DAYS_PER_YEAR)
        if period == 'hourly':
            return sortino_raw * math.sqrt(_HOURS_PER_YEAR)

        elapsed_days = self._elapsed_days(t)
        if elapsed_days < 1:
            return sortino_raw

        # [FIX-MET-SHARPE] Même guard que Sharpe — cohérence des ratios.
        if elapsed_days < _CAGR_MIN_DAYS:
            return 0.0   # Période trop courte — voir 'sharpe_note' dans les métriques

        trades_per_year = len(t) / elapsed_days * _TRADING_DAYS_PER_YEAR
        return sortino_raw * math.sqrt(trades_per_year)

    def calculate_calmar_ratio(
        self, trades: Optional[List[Dict[str, Any]]] = None
    ) -> float:
        """
        Calmar Ratio = CAGR / Max Drawdown (décimal).

        Utilise le CAGR réel basé sur le temps réellement écoulé entre
        le premier et le dernier trade — pas une approximation par
        nombre de trades.

        Returns:
            Calmar. >3 excellent. 0.0 si données insuffisantes.
        """
        t      = trades if trades is not None else self._get_trades_snapshot()
        max_dd = self.calculate_max_drawdown(t)
        if max_dd == 0 or not t:
            return 0.0

        cagr = self.calculate_cagr(t)
        if cagr is None:
            return 0.0

        # max_dd est en % positif (ex: 15.3) → convertir en décimal
        return cagr / (max_dd / 100.0)

    # ========================================================================
    # MÉTRIQUES — RENDEMENT
    # ========================================================================

    def calculate_total_pnl(
        self, trades: Optional[List[Dict[str, Any]]] = None
    ) -> float:
        """PnL net total (USDT)."""
        t = trades if trades is not None else self._get_trades_snapshot()
        return sum(x.get('pnl_net', 0.0) for x in t)

    def calculate_total_return_pct(
        self, trades: Optional[List[Dict[str, Any]]] = None
    ) -> float:
        """Rendement total sur capital initial (%). Ex: 23.5 = +23.5%."""
        t = trades if trades is not None else self._get_trades_snapshot()
        return (self.calculate_total_pnl(t) / self.initial_capital) * 100.0

    def calculate_cagr(
        self, trades: Optional[List[Dict[str, Any]]] = None
    ) -> Optional[float]:
        """
        CAGR (Compound Annual Growth Rate) en décimal.

        CAGR = (final_equity / initial_equity)^(1/years) − 1

        Returns:
            Décimal (ex: 0.45 = +45%/an).
            None si durée réelle < 1 jour ou < _CAGR_MIN_DAYS (résultat
            mathématiquement valide mais sans signification pratique sur
            des sessions courtes — extrapolation annuelle aberrante).
        """
        t            = trades if trades is not None else self._get_trades_snapshot()
        elapsed_days = self._elapsed_days(t)
        if elapsed_days < 1:
            return None

        # [v2.2.2 — Action 2] Guard durée minimale : CAGR non significatif
        # sur des sessions courtes. Extrapoler 2.3 jours à 365 jours produit
        # des valeurs comme 12_899_398% — mathématiquement correctes mais
        # pratiquement inutiles et trompeuses dans les rapports.
        if elapsed_days < _CAGR_MIN_DAYS:
            return None

        final_equity = self._final_equity(t)
        years        = elapsed_days / _TRADING_DAYS_PER_YEAR
        ratio        = final_equity / self.initial_capital

        if ratio <= 0:
            return None  # Perte totale — CAGR indéfini

        return ratio ** (1.0 / years) - 1.0

    def calculate_total_fees(
        self, trades: Optional[List[Dict[str, Any]]] = None
    ) -> float:
        """Total fees payées (USDT). Somme entry_fees + exit_fees + funding_fees par trade.

        [FIX] Ancienne implémentation cherchait 'fees' (clé inexistante dans les
        trade records BULLET-1) → total_fees=0.0 systématique.
        Les trade records exposent 'entry_fees', 'exit_fees', et 'funding_fees'.
        """
        t = trades if trades is not None else self._get_trades_snapshot()
        return sum(
            x.get('entry_fees', 0.0)
            + x.get('exit_fees', 0.0)
            + x.get('funding_fees', 0.0)
            for x in t
        )

    def calculate_gross_profit_loss(
        self, trades: Optional[List[Dict[str, Any]]] = None
    ) -> Tuple[float, float]:
        """Retourne (gross_profit, gross_loss). gross_loss = valeur absolue."""
        t = trades if trades is not None else self._get_trades_snapshot()
        gp = sum(x['pnl_net'] for x in t if x.get('pnl_net', 0.0) > 0)
        gl = abs(sum(x['pnl_net'] for x in t if x.get('pnl_net', 0.0) < 0))
        return gp, gl

    # ========================================================================
    # MÉTRIQUES — DRAWDOWN
    # ========================================================================

    def calculate_max_drawdown(
        self, trades: Optional[List[Dict[str, Any]]] = None
    ) -> float:
        """
        Max Drawdown pic-creux en %.

        Utilise l'equity_curve si disponible.
        Sinon reconstruit depuis les trades avec initial_capital réel
        (pas un capital arbitraire de 1000).

        Returns:
            Max Drawdown en % positif. Ex: 15.3 = −15.3% depuis le pic.
        """
        equities = self._build_equity_series(trades)
        if not equities:
            return 0.0

        peak   = equities[0]
        max_dd = 0.0
        for eq in equities:
            if eq > peak:
                peak = eq
            if peak > 0:
                dd = ((peak - eq) / peak) * 100.0
                max_dd = max(max_dd, dd)
        return max_dd

    def calculate_max_drawdown_absolute(
        self, trades: Optional[List[Dict[str, Any]]] = None
    ) -> float:
        """
        Max Drawdown absolu (USDT) — montant exact de la perte pic-creux.

        Essentiel pour Recovery Factor correct (pas une approximation
        basée sur un capital arbitraire).
        """
        equities = self._build_equity_series(trades)
        if not equities:
            return 0.0

        peak   = equities[0]
        max_dd = 0.0
        for eq in equities:
            if eq > peak:
                peak = eq
            dd = peak - eq
            max_dd = max(max_dd, dd)
        return max_dd

    def _build_equity_series(
        self, trades: Optional[List[Dict[str, Any]]] = None
    ) -> List[float]:
        """
        Construit la série d'equity.
        Priorité : equity_curve fournie. Sinon : reconstruit depuis trades.
        """
        with self._lock:
            curve = list(self._equity_curve)

        if len(curve) > 1:
            return [p['equity'] for p in curve]

        t = trades if trades is not None else self._get_trades_snapshot()
        if not t:
            return []

        equity   = self.initial_capital  # Capital réel, pas arbitraire
        equities = [equity]
        for trade in t:
            equity += trade.get('pnl_net', 0.0)
            equities.append(equity)
        return equities

    def calculate_recovery_factor(
        self, trades: Optional[List[Dict[str, Any]]] = None
    ) -> float:
        """
        Recovery Factor = Net Profit / Max Drawdown Absolu (USDT).

        Indique combien de fois le système a gagné l'équivalent de son
        drawdown maximum. >3 = bon, >10 = excellent.
        """
        t      = trades if trades is not None else self._get_trades_snapshot()
        max_dd = self.calculate_max_drawdown_absolute(t)
        if max_dd == 0:
            return 0.0
        return self.calculate_total_pnl(t) / max_dd

    # ========================================================================
    # MÉTRIQUES — SÉQUENCES
    # ========================================================================

    def calculate_max_consecutive_wins(
        self, trades: Optional[List[Dict[str, Any]]] = None
    ) -> int:
        """Nombre maximum de wins consécutifs."""
        t = trades if trades is not None else self._get_trades_snapshot()
        return self._max_streak(t, win=True)

    def calculate_max_consecutive_losses(
        self, trades: Optional[List[Dict[str, Any]]] = None
    ) -> int:
        """Nombre maximum de losses consécutifs."""
        t = trades if trades is not None else self._get_trades_snapshot()
        return self._max_streak(t, win=False)

    def calculate_consecutive_loss_drawdown(
        self, trades: Optional[List[Dict[str, Any]]] = None
    ) -> float:
        """
        Perte totale (USDT) accumulée sur la pire série de losses consécutifs.
        Métrique de survie critique en futures.
        """
        t = trades if trades is not None else self._get_trades_snapshot()
        if not t:
            return 0.0
        max_loss = current = 0.0
        for trade in t:
            if not trade.get('is_winner', False):
                current  += abs(trade.get('pnl_net', 0.0))
                max_loss  = max(max_loss, current)
            else:
                current = 0.0
        return max_loss

    @staticmethod
    def _max_streak(trades: List[Dict[str, Any]], win: bool) -> int:
        max_streak = current = 0
        for trade in trades:
            if trade.get('is_winner', False) == win:
                current   += 1
                max_streak = max(max_streak, current)
            else:
                current = 0
        return max_streak

    # ========================================================================
    # MÉTRIQUES — TEMPS
    # ========================================================================

    def calculate_avg_holding_time(
        self,
        trades: Optional[List[Dict[str, Any]]] = None,
        filter_winner: Optional[bool] = None,
    ) -> float:
        """
        Durée moyenne de détention en heures.

        Args:
            filter_winner: None = tous, True = gagnants seulement,
                           False = perdants seulement

        Returns:
            Durée en heures. 0.0 si aucune donnée temporelle.
        """
        t = trades if trades is not None else self._get_trades_snapshot()
        times: List[float] = []
        for trade in t:
            if filter_winner is not None:
                if trade.get('is_winner', False) != filter_winner:
                    continue
            entry = trade.get('entry_time')
            exit_ = trade.get('exit_time')
            if entry and exit_:
                # [v2.2.7 — FIX-MET-2] Normalisation UTC avant soustraction.
                # Même risque que FIX-MET-1 : TypeError si naïf vs aware.
                try:
                    entry_utc = self._ensure_utc_dt(entry)
                    exit_utc  = self._ensure_utc_dt(exit_)
                    h = (exit_utc - entry_utc).total_seconds() / 3600.0
                except (TypeError, ValueError):
                    continue
                if h >= 0:
                    times.append(h)
        return sum(times) / len(times) if times else 0.0

    def calculate_trades_per_day(
        self, trades: Optional[List[Dict[str, Any]]] = None
    ) -> float:
        """Nombre moyen de trades par jour sur la durée réelle de session."""
        t            = trades if trades is not None else self._get_trades_snapshot()
        elapsed_days = self._elapsed_days(t)
        if elapsed_days < 1:
            return float(len(t))
        return len(t) / elapsed_days

    # ========================================================================
    # MÉTRIQUES — LONG vs SHORT
    # ========================================================================

    def calculate_directional_stats(
        self, trades: Optional[List[Dict[str, Any]]] = None
    ) -> Dict[str, Any]:
        """
        Stats séparées Long vs Short (nécessite 'side' dans les trades).

        Returns:
            {'long': {...}, 'short': {...}}
            Chaque sous-dict : count, win_rate, avg_pnl,
                               total_pnl, profit_factor
        """
        t = trades if trades is not None else self._get_trades_snapshot()

        def _stats(subset: List[Dict[str, Any]]) -> Dict[str, Any]:
            if not subset:
                return {
                    'count': 0, 'win_rate': 0.0, 'avg_pnl': 0.0,
                    'total_pnl': 0.0, 'profit_factor': 0.0,
                }
            pnls    = [x.get('pnl_net', 0.0) for x in subset]
            wins    = sum(1 for x in subset if x.get('is_winner', False))
            gross_p = sum(p for p in pnls if p > 0)
            gross_l = abs(sum(p for p in pnls if p < 0))
            pf      = gross_p / gross_l if gross_l > 0 else float('inf')
            return {
                'count':         len(subset),
                'win_rate':      (wins / len(subset)) * 100.0,
                'avg_pnl':       sum(pnls) / len(pnls),
                'total_pnl':     sum(pnls),
                'profit_factor': pf,
            }

        # Trade records BULLET-1 utilisent 'direction' (LONG/SHORT majuscule),
        # pas 'side'. Support des deux pour robustesse.
        longs  = [x for x in t if x.get('direction', x.get('side', '')).upper() == 'LONG']
        shorts = [x for x in t if x.get('direction', x.get('side', '')).upper() == 'SHORT']
        return {'long': _stats(longs), 'short': _stats(shorts)}

    # ========================================================================
    # MÉTRIQUES — MAE / MFE
    # ========================================================================

    def calculate_mae_mfe_stats(
        self, trades: Optional[List[Dict[str, Any]]] = None
    ) -> Dict[str, Optional[float]]:
        """
        Stats MAE (Max Adverse Excursion) et MFE (Max Favorable Excursion).

        Nécessite 'mae' et 'mfe' dans les trades (décimal, ex: -0.015).

        Returns:
            avg_mae, avg_mfe, avg_mae_winners, avg_mae_losers,
            avg_mfe_winners, avg_mfe_losers — en % (×100).
            None si données absentes.
        """
        t = trades if trades is not None else self._get_trades_snapshot()

        def avg(lst: List[float]) -> Optional[float]:
            return sum(lst) / len(lst) if lst else None

        maes  = [x['mae'] * 100 for x in t if 'mae' in x]
        mfes  = [x['mfe'] * 100 for x in t if 'mfe' in x]
        w_mae = [x['mae'] * 100 for x in t if 'mae' in x and x.get('is_winner')]
        l_mae = [x['mae'] * 100 for x in t if 'mae' in x and not x.get('is_winner')]
        w_mfe = [x['mfe'] * 100 for x in t if 'mfe' in x and x.get('is_winner')]
        l_mfe = [x['mfe'] * 100 for x in t if 'mfe' in x and not x.get('is_winner')]

        return {
            'avg_mae':         avg(maes),
            'avg_mfe':         avg(mfes),
            'avg_mae_winners': avg(w_mae),
            'avg_mae_losers':  avg(l_mae),
            'avg_mfe_winners': avg(w_mfe),
            'avg_mfe_losers':  avg(l_mfe),
        }

    # ========================================================================
    # CALCUL COMPLET — ATOMIQUE
    # ========================================================================

    def calculate_all(self) -> Dict[str, Any]:
        """
        Calcule TOUTES les métriques en une passe atomique.

        Prend un snapshot thread-safe unique, puis effectue tous les calculs
        hors lock. Aucun thread ne peut modifier les données pendant le calcul.

        Returns:
            Dict complet. Toutes les valeurs sont sérialisables JSON.
        """
        # ── Snapshot unique et atomique ──────────────────────────────────────
        trades, _ = self._get_full_snapshot()

        if not trades:
            self.logger.warning("No trades — returning empty metrics")
            return self._make_empty_metrics(self.initial_capital)

        # ── Calculs (hors lock) ──────────────────────────────────────────────
        gross_p, gross_l = self.calculate_gross_profit_loss(trades)
        cagr_raw         = self.calculate_cagr(trades)
        dir_stats        = self.calculate_directional_stats(trades)
        mae_mfe          = self.calculate_mae_mfe_stats(trades)
        pf               = self.calculate_profit_factor(trades)

        results: Dict[str, Any] = {
            # Comptage
            'total_trades':              len(trades),
            'winning_trades':            sum(1 for x in trades if x.get('is_winner', False)),
            'losing_trades':             sum(1 for x in trades if not x.get('is_winner', False)),
            'trades_per_day':            self.calculate_trades_per_day(trades),

            # Win/Loss
            'win_rate':                  self.calculate_win_rate(trades),
            'profit_factor':             pf,
            'r_ratio':                   self.calculate_r_ratio(trades),
            'avg_win':                   self.calculate_avg_win(trades),
            'avg_loss':                  self.calculate_avg_loss(trades),
            'expectancy':                self.calculate_expectancy(trades),
            'kelly_criterion':           self.calculate_kelly_criterion(trades),

            # PnL
            'total_pnl':                 self.calculate_total_pnl(trades),
            'total_return_pct':          self.calculate_total_return_pct(trades),
            'gross_profit':              gross_p,
            'gross_loss':                gross_l,
            'total_fees':                self.calculate_total_fees(trades),
            'best_trade':                max((x.get('pnl_net', 0.0) for x in trades), default=0.0),
            'worst_trade':               min((x.get('pnl_net', 0.0) for x in trades), default=0.0),

            # Rendement annualisé
            'cagr_pct':                  cagr_raw * 100.0 if cagr_raw is not None else None,
            'cagr_note':                 (
                None if cagr_raw is not None
                else f'insufficient_data (< {_CAGR_MIN_DAYS}d)'
                if self._elapsed_days(trades) >= 1
                else 'insufficient_data (< 1d)'
            ),
            'elapsed_days':              self._elapsed_days(trades),

            # Ratios risque/rendement
            # [FIX-MET-SHARPE] 0.0 si période < 30j (annualisation non fiable).
            # Consulter 'sharpe_note' pour distinguer 0.0 réel vs période insuffisante.
            'sharpe_ratio':              self.calculate_sharpe_ratio(period='trade', trades=trades),
            'sortino_ratio':             self.calculate_sortino_ratio(period='trade', trades=trades),
            'calmar_ratio':              self.calculate_calmar_ratio(trades),
            # Note explicative — même logique que cagr_note
            'sharpe_note':               (
                None
                if self._elapsed_days(trades) >= _CAGR_MIN_DAYS
                else f'insufficient_period (< {_CAGR_MIN_DAYS}d)'
            ),

            # Drawdown
            'max_drawdown_pct':          self.calculate_max_drawdown(trades),
            'max_drawdown_usdt':         self.calculate_max_drawdown_absolute(trades),
            'recovery_factor':           self.calculate_recovery_factor(trades),

            # Séquences
            'max_consecutive_wins':      self.calculate_max_consecutive_wins(trades),
            'max_consecutive_losses':    self.calculate_max_consecutive_losses(trades),
            'consecutive_loss_drawdown': self.calculate_consecutive_loss_drawdown(trades),

            # Temps
            'avg_holding_time_hours':         self.calculate_avg_holding_time(trades),
            'avg_holding_time_winners_hours':  self.calculate_avg_holding_time(trades, filter_winner=True),
            'avg_holding_time_losers_hours':   self.calculate_avg_holding_time(trades, filter_winner=False),

            # Directionnel
            'long_stats':                dir_stats['long'],
            'short_stats':               dir_stats['short'],

            # MAE / MFE
            **{f'mae_mfe_{k}': v for k, v in mae_mfe.items()},

            # Metadata
            'initial_capital':           self.initial_capital,
            'mode':                      self.mode,
            'calculated_at':             datetime.now(tz=timezone.utc).isoformat(),
        }

        self.logger.info(
            f"Metrics calculated | trades={len(trades)} | "
            f"WR={results['win_rate']:.1f}% | "
            f"PF={_safe_json(pf):.2f} | "
            f"Sharpe={results['sharpe_ratio']:.2f} | "
            f"MaxDD={results['max_drawdown_pct']:.2f}%"
        )

        return results

    @staticmethod
    @staticmethod
    def _make_empty_metrics(initial_capital: float = 0.0) -> Dict[str, Any]:
        """
        Dict métriques vide — structure cohérente quand aucun trade.

        [FIX-MET-EMPTY] initial_capital transmis explicitement pour que
        analytics_engine et summary.txt affichent le bon capital de départ
        même sur les sessions sans trades (évite l'affichage 0.00 USDT).
        """
        return {
            'total_trades': 0, 'winning_trades': 0, 'losing_trades': 0,
            'trades_per_day': 0.0, 'win_rate': 0.0, 'profit_factor': 0.0,
            'r_ratio': 0.0, 'avg_win': 0.0, 'avg_loss': 0.0,
            'expectancy': 0.0, 'kelly_criterion': 0.0,
            'total_pnl': 0.0, 'total_return_pct': 0.0,
            'gross_profit': 0.0, 'gross_loss': 0.0, 'total_fees': 0.0,
            'best_trade': 0.0, 'worst_trade': 0.0,
            'cagr_pct': None, 'cagr_note': 'no_trades', 'elapsed_days': 0.0,
            'sharpe_ratio': 0.0, 'sortino_ratio': 0.0, 'calmar_ratio': 0.0,
            'sharpe_note': 'no_trades',  # [FIX-MET-SHARPE]
            'max_drawdown_pct': 0.0, 'max_drawdown_usdt': 0.0,
            'recovery_factor': 0.0,
            'max_consecutive_wins': 0, 'max_consecutive_losses': 0,
            'consecutive_loss_drawdown': 0.0,
            'avg_holding_time_hours': 0.0,
            'avg_holding_time_winners_hours': 0.0,
            'avg_holding_time_losers_hours': 0.0,
            'long_stats': {}, 'short_stats': {},
            # [FIX-MET-EMPTY] Capital initial transmis explicitement
            # → analytics_engine + summary.txt affichent la bonne valeur
            # même quand la session n'a produit aucun trade.
            'initial_capital': initial_capital,
            'final_capital':   initial_capital,  # inchangé sans trades
        }

    # ========================================================================
    # EXPORT
    # ========================================================================

    def export(
        self,
        output_path: Union[str, Path],
        fmt: Literal['json', 'csv', 'text'] = 'json',
    ) -> Path:
        """
        Exporte les métriques calculées vers un fichier.

        Args:
            output_path: Chemin fichier de sortie
            fmt:         'json', 'csv', ou 'text'

        Returns:
            Path absolu du fichier créé.

        Raises:
            ValueError: Si fmt invalide.
        """
        if fmt not in _VALID_EXPORT_FORMATS:
            raise ValueError(
                f"Invalid format '{fmt}'. "
                f"Expected one of {_VALID_EXPORT_FORMATS}"
            )

        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        results = self.calculate_all()
        clean   = _sanitize_for_json(results)

        if fmt == 'json':
            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump(clean, f, indent=2, ensure_ascii=False, default=str)

        elif fmt == 'csv':
            with open(output_path, 'w', encoding='utf-8', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(['Metric', 'Value'])
                for key, val in clean.items():
                    if isinstance(val, dict):
                        for sub_k, sub_v in val.items():
                            writer.writerow([f"{key}.{sub_k}", sub_v])
                    else:
                        writer.writerow([key, val])

        elif fmt == 'text':
            self._write_text_report(output_path, results)

        resolved = output_path.resolve()
        self.logger.info(f"Metrics exported | {resolved} | fmt={fmt}")
        return resolved

    def _write_text_report(self, path: Path, r: Dict[str, Any]) -> None:
        """Écrit le rapport texte lisible par un humain."""
        SEP  = "=" * 65
        DASH = "-" * 65

        def fmt_val(val: Any, spec: str = '.2f') -> str:
            if val is None:
                return 'N/A'
            if isinstance(val, float) and math.isinf(val):
                return '∞'
            if isinstance(val, float) and math.isnan(val):
                return 'NaN'
            # [v2.2.2 — Action 3] Sentinel JSON 999.0 → '∞' dans les formats textuels.
            # Le JSON garde 999.0 (contrainte de sérialisabilité float('inf')),
            # mais HTML/Markdown/Text affichent '∞' pour la lisibilité.
            if isinstance(val, float) and val == _JSON_INF_SENTINEL:
                return '∞'
            return format(val, spec)

        with open(path, 'w', encoding='utf-8') as f:
            f.write(f"{SEP}\n  BULLET-1 — TRADING PERFORMANCE REPORT\n{SEP}\n\n")
            f.write(f"  Mode            : {r['mode']}\n")
            f.write(f"  Initial Capital : {r['initial_capital']:,.2f} USDT\n")
            f.write(f"  Calculated at   : {r.get('calculated_at', 'N/A')}\n\n")

            f.write(f"OVERVIEW\n{DASH}\n")
            f.write(f"  Total Trades        : {r['total_trades']}\n")
            f.write(f"  Winning / Losing    : {r['winning_trades']} / {r['losing_trades']}\n")
            f.write(f"  Win Rate            : {r['win_rate']:.2f}%\n")
            f.write(f"  Trades per Day      : {r['trades_per_day']:.2f}\n")
            f.write(f"  Session Duration    : {r['elapsed_days']:.1f} days\n\n")

            f.write(f"PnL\n{DASH}\n")
            f.write(f"  Total PnL (net)     : {r['total_pnl']:+,.2f} USDT\n")
            f.write(f"  Total Return        : {r['total_return_pct']:+.2f}%\n")
            cagr_str = (
                f"{r['cagr_pct']:+.2f}%"
                if r.get('cagr_pct') is not None
                else f"N/A ({r.get('cagr_note', 'insufficient_data')})"
            )
            f.write(f"  CAGR                : {cagr_str}\n")
            f.write(f"  Gross Profit        : {r['gross_profit']:+,.2f} USDT\n")
            f.write(f"  Gross Loss          : {r['gross_loss']:,.2f} USDT\n")
            f.write(f"  Total Fees          : {r['total_fees']:,.2f} USDT\n")
            f.write(f"  Best Trade          : {r['best_trade']:+,.2f} USDT\n")
            f.write(f"  Worst Trade         : {r['worst_trade']:+,.2f} USDT\n\n")

            f.write(f"WIN/LOSS METRICS\n{DASH}\n")
            f.write(f"  Profit Factor       : {fmt_val(r['profit_factor'])}\n")
            f.write(f"  R-Ratio (Payoff)    : {fmt_val(r['r_ratio'])}\n")
            f.write(f"  Average Win         : {r['avg_win']:,.2f} USDT\n")
            f.write(f"  Average Loss        : {r['avg_loss']:,.2f} USDT\n")
            f.write(f"  Expectancy          : {r['expectancy']:+,.2f} USDT/trade\n")
            f.write(f"  Kelly Criterion     : {r['kelly_criterion']:.2%}\n\n")

            f.write(f"RISK-ADJUSTED RETURNS\n{DASH}\n")
            # [FIX-MET-SHARPE] Afficher la note si période insuffisante
            _sharpe_note = r.get('sharpe_note')
            _sharpe_str  = (
                f"N/A ({_sharpe_note})"
                if _sharpe_note
                else f"{r['sharpe_ratio']:.2f}"
            )
            _sortino_note = r.get('sharpe_note')   # même condition période
            _sortino_str  = (
                f"N/A ({_sortino_note})"
                if _sortino_note
                else fmt_val(r['sortino_ratio'])
            )
            f.write(f"  Sharpe Ratio        : {_sharpe_str}\n")
            f.write(f"  Sortino Ratio       : {_sortino_str}\n")
            f.write(f"  Calmar Ratio        : {r['calmar_ratio']:.2f}\n\n")

            f.write(f"DRAWDOWN\n{DASH}\n")
            f.write(f"  Max Drawdown        : {r['max_drawdown_pct']:.2f}%\n")
            f.write(f"  Max Drawdown (USDT) : {r['max_drawdown_usdt']:,.2f} USDT\n")
            f.write(f"  Recovery Factor     : {r['recovery_factor']:.2f}\n\n")

            f.write(f"STREAKS\n{DASH}\n")
            f.write(f"  Max Consec. Wins    : {r['max_consecutive_wins']}\n")
            f.write(f"  Max Consec. Losses  : {r['max_consecutive_losses']}\n")
            f.write(f"  Consec. Loss DD     : {r['consecutive_loss_drawdown']:,.2f} USDT\n\n")

            f.write(f"HOLDING TIME\n{DASH}\n")
            f.write(f"  All trades          : {r['avg_holding_time_hours']:.1f} h\n")
            f.write(f"  Winners             : {r['avg_holding_time_winners_hours']:.1f} h\n")
            f.write(f"  Losers              : {r['avg_holding_time_losers_hours']:.1f} h\n\n")

            ls, ss = r.get('long_stats', {}), r.get('short_stats', {})
            if ls.get('count', 0) or ss.get('count', 0):
                f.write(f"DIRECTIONAL\n{DASH}\n")
                if ls.get('count', 0):
                    f.write(
                        f"  Long   | n={ls['count']:>4} | "
                        f"WR={ls['win_rate']:.1f}% | "
                        f"PnL={ls['total_pnl']:+,.2f} | "
                        f"PF={fmt_val(ls['profit_factor'])}\n"
                    )
                if ss.get('count', 0):
                    f.write(
                        f"  Short  | n={ss['count']:>4} | "
                        f"WR={ss['win_rate']:.1f}% | "
                        f"PnL={ss['total_pnl']:+,.2f} | "
                        f"PF={fmt_val(ss['profit_factor'])}\n"
                    )
                f.write('\n')

            mae_keys = [k for k in r if k.startswith('mae_mfe_') and r[k] is not None]
            if mae_keys:
                f.write(f"MAE / MFE\n{DASH}\n")
                for k in sorted(mae_keys):
                    label = k.replace('mae_mfe_', '').replace('_', ' ').title()
                    f.write(f"  {label:<25}: {r[k]:.3f}%\n")
                f.write('\n')

            f.write(f"{SEP}\n")

    # ========================================================================
    # UTILS
    # ========================================================================

    def reset(self) -> None:
        """Réinitialise toutes les données (trades + equity curve)."""
        with self._lock:
            self._trades.clear()
            self._equity_curve.clear()
        self.logger.info("Metrics reset")

    def get_trades_count(self) -> int:
        """Nombre de trades enregistrés (thread-safe)."""
        with self._lock:
            return len(self._trades)

    def __repr__(self) -> str:
        with self._lock:
            return (
                f"Metrics("
                f"mode={self.mode!r}, "
                f"capital={self.initial_capital:,.2f}, "
                f"trades={len(self._trades)}, "
                f"equity_points={len(self._equity_curve)})"
            )

# FIN DU MODULE