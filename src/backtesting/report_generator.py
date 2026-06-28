"""
BULLET-1 - Report Generator Module
====================================

Génération rapports de performance trading (frontend + backend).
Support BACKTEST, PAPER, et LIVE trading.

Frontend: HTML (CSS), Markdown, Texte
Backend:  JSON, CSV
Graphiques: Equity curve, Drawdown, Trailing Stop evolution

Version: 2.2.3
Date: 2026-03-15
Author: FuegoDev
"""

import csv
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union, Literal

import matplotlib
matplotlib.use('Agg')  # Backend non-interactif (serveur)
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import sys

# [v2.2.3 — FIX-RG-4] Pattern direct unifié BULLET-1 — remplace find_project_root().
# Même correction que FIX-ENG-6 (engine.py) et FIX-AE-1 (analytics_engine.py).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# ============================================================================
# IMPORTS BULLET-1
# ============================================================================

from src.utils.logger import BulletLogger
from src.utils.helpers import (
    format_percentage,
    ensure_directory,
    format_datetime
)
from src.backtesting.metrics import Metrics, _JSON_INF_SENTINEL  # [FIX-RG-INF-DOC] import sentinelle JSON


# ============================================================================
# CONSTANTES
# ============================================================================

_VALID_FORMATS = frozenset({'html', 'markdown', 'json', 'csv', 'text'})
_RESULTS_BASE_DIR = 'results'  # Structure: results/backtests/sessions/
#: Version du module — utilisée dans les rapports générés (footer HTML, note MD).
_VERSION = "2.2.3"  # [v2.2.3 — FIX-RG-8]


# ============================================================================
# CLASSE PRINCIPALE - ReportGenerator
# ============================================================================

class ReportGenerator:
    """
    Générateur de rapports de performance trading.
    
    Génère rapports frontend (HTML, Markdown, Texte) et backend (JSON, CSV)
    avec graphiques intégrés (equity curve, drawdown, trailing stop).
    
    Architecture v2.1.0:
    - Utilise Metrics en interne (délégation calculs)
    - Génère HTML avec CSS styling professionnel
    - Génère Markdown complet
    - Génère JSON/CSV pour analyses externes
    - Graphiques matplotlib intégrés
    - Structure organisée results/backtests/sessions/
    
    Responsabilités:
    1. Génération HTML avec CSS (frontend)
    2. Génération Markdown pro (frontend)
    3. Export texte console/email (frontend)
    4. Génération JSON complet (backend)
    5. Génération CSV analysable (backend)
    6. Graphiques equity/drawdown/trailing stop
    7. Sauvegarde organisée
    
    Attributes:
        logger (BulletLogger): Logger centralisé
        metrics (Metrics): Calculateur métriques (délégation)
        mode (str): 'BACKTEST', 'PAPER', 'LIVE'
        session_name (str): Nom session pour fichiers
        
    Examples:
        >>> report_gen = ReportGenerator(
        ...     mode='BACKTEST',
        ...     session_name='3-volume2_2026-02-22',
        ...     initial_capital=10_000.0
        ... )
        >>> 
        >>> # Ajouter trades
        >>> for trade in closed_positions:
        ...     report_gen.add_trade(trade)
        >>> 
        >>> # Générer rapports
        >>> report_gen.generate_all_reports('results/backtests/sessions/session1/')
        >>> 
        >>> # Ou individuellement
        >>> report_gen.generate_html_report('report.html')
        >>> report_gen.generate_json_report('metrics.json')
    """
    
    def __init__(
        self,
        mode: str = 'BACKTEST',
        session_name: Optional[str] = None,
        session_number: int = 0,
        session_start: str = '',
        session_end: str = '',
        initial_capital: float = 1_000.0,
        final_capital: Optional[float] = None,
        configuration_name: str = 'unknown',
        risk_free_rate: float = 0.0
    ) -> None:
        """
        Initialise le générateur de rapports.

        Args:
            mode: Mode opération ('BACKTEST', 'PAPER', 'LIVE')
            session_name: Nom session pour fichiers (auto si None)
            session_number: Numéro de session (ex: 1, 2, 3...)
            session_start: Date début session 'YYYY-MM-DD'
            session_end: Date fin session 'YYYY-MM-DD'
            initial_capital: Capital initial (USDT)
            final_capital: Capital final (USDT) — None = identique à initial
            configuration_name: Nom de la configuration stratégie (ex: '1-normal)
            risk_free_rate: Taux sans risque annuel (ex: 0.02 = 2%)
        """
        self.logger = BulletLogger()
        self.mode            = mode
        self.session_name    = session_name or self._generate_session_name()
        self.session_number  = session_number
        self.session_start   = session_start
        self.session_end     = session_end
        self.final_capital   = final_capital if final_capital is not None else initial_capital
        self.configuration_name   = configuration_name
        
        # Metrics en interne (délégation)
        self.metrics = Metrics(
            mode=mode,
            initial_capital=initial_capital,
            risk_free_rate=risk_free_rate
        )
        
        # Storage graphiques générés
        self._chart_paths: Dict[str, Path] = {}
        
        self.logger.info(
            f"ReportGenerator initialized | mode={mode} | "
            f"session={self.session_name}"
        )
    
    # ========================================================================
    # AJOUT DONNÉES (Délégation à Metrics)
    # ========================================================================
    
    def add_trade(self, trade: Dict[str, Any]) -> None:
        """Ajoute un trade (délégué à metrics)."""
        self.metrics.add_trade(trade)
    
    def add_equity_point(self, timestamp: datetime, equity: float) -> None:
        """Ajoute point equity curve (délégué à metrics)."""
        self.metrics.add_equity_point(timestamp, equity)
    
    def set_trades(self, trades: List[Dict[str, Any]]) -> None:
        """Définit liste trades (délégué à metrics)."""
        self.metrics.set_trades(trades)
    
    def set_equity_curve(
        self,
        equity_curve: List[Dict[str, Any]]
    ) -> None:
        """Définit courbe equity (délégué à metrics)."""
        self.metrics.set_equity_curve(equity_curve)
    
    # ========================================================================
    # GÉNÉRATION RAPPORTS - FRONTEND
    # ========================================================================
    
    def generate_html_report(
        self,
        output_path: Union[str, Path]
    ) -> Path:
        """
        Génère rapport HTML avec CSS styling professionnel.
        
        Inclut:
        - Header (titre, date, mode)
        - Executive Summary (métriques clés)
        - Performance Metrics (tableau)
        - Risk Analysis
        - Graphiques (equity, drawdown, trailing stop)
        - Directional Stats (Long vs Short)
        - Trade List
        - Footer
        
        Args:
            output_path: Chemin fichier HTML sortie
        
        Returns:
            Path fichier créé
        
        Examples:
            >>> path = report_gen.generate_html_report(
            ...     'results/backtests/sessions/session1/report.html'
            ... )
        """
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Calculer métriques
        results = self.metrics.calculate_all()
        
        # Générer graphiques
        charts_dir = output_path.parent / 'charts'
        charts_dir.mkdir(exist_ok=True)
        
        equity_chart = self._generate_equity_chart(charts_dir)
        dd_chart = self._generate_drawdown_chart(charts_dir)
        
        # Build HTML
        html = self._build_html(results, equity_chart, dd_chart)
        
        # Save
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(html)
        
        self.logger.info(f"HTML report generated: {output_path}")
        return output_path.resolve()
    
    def generate_markdown_report(
        self,
        output_path: Union[str, Path]
    ) -> Path:
        """
        Génère rapport Markdown professionnel complet.
        
        Inclut:
        - Header
        - Overview
        - Performance Metrics
        - Risk Analysis
        - Directional Stats
        - Best/Worst Trades
        - Notes
        
        Args:
            output_path: Chemin fichier Markdown sortie
        
        Returns:
            Path fichier créé
        
        Examples:
            >>> path = report_gen.generate_markdown_report('report.md')
        """
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Calculer métriques
        results = self.metrics.calculate_all()
        
        # Build Markdown
        markdown = self._build_markdown(results)
        
        # Save
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(markdown)
        
        self.logger.info(f"Markdown report generated: {output_path}")
        return output_path.resolve()
    
    def generate_text_summary(
        self,
        output_path: Optional[Union[str, Path]] = None
    ) -> str:
        """
        Génère résumé texte lisible (console/email).
        
        Format compact pour affichage terminal ou email.
        
        Args:
            output_path: Chemin fichier txt (optionnel).
                        Si None, retourne string uniquement.
        
        Returns:
            String résumé texte
        
        Examples:
            >>> # Console
            >>> summary = report_gen.generate_text_summary()
            >>> print(summary)
            >>> 
            >>> # Fichier
            >>> report_gen.generate_text_summary('summary.txt')
        """
        results = self.metrics.calculate_all()
        
        text = self._build_text_summary(results)
        
        if output_path:
            output_path = Path(output_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(text)
            
            self.logger.info(f"Text summary generated: {output_path}")
        
        return text
    
    # ========================================================================
    # GÉNÉRATION RAPPORTS - BACKEND
    # ========================================================================
    
    def generate_json_report(
        self,
        output_path: Union[str, Path]
    ) -> Path:
        """
        Génère rapport JSON complet (backend).
        
        Contient toutes métriques + metadata pour analyses externes.
        
        Args:
            output_path: Chemin fichier JSON sortie
        
        Returns:
            Path fichier créé
        
        Examples:
            >>> path = report_gen.generate_json_report('metrics.json')
        """
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Calculer métriques
        results = self.metrics.calculate_all()
        
        # Add session metadata
        report_data = {
            'session_name':   self.session_name,
            'session_number': self.session_number,
            'configuration_name':  self.configuration_name,
            'session_period': {
                'start': self.session_start,
                'end':   self.session_end,
            },
            'mode':          self.mode,
            'generated_at':  datetime.now(tz=timezone.utc).isoformat(),
            # [FIX-RG-INF-DOC] Annotation explicite : JSON ne supporte pas
            # Infinity nativement, donc tout float('inf') (ex: profit_factor
            # sans perte) est remplacé par cette valeur sentinelle. Les
            # rapports HTML/Markdown/Text affichent déjà '∞' pour la même
            # donnée — seul le JSON brut a besoin de cette note pour un
            # consommateur externe (audit Phase 8, mineur m5).
            'json_sentinel_note': (
                f"Toute valeur égale à {_JSON_INF_SENTINEL} dans 'metrics' "
                f"représente float('inf') (ex: aucune perte sur la période)."
            ),
            'metrics':       results
        }

        # Injecter final_capital dans metrics pour cohérence
        if isinstance(report_data.get('metrics'), dict):
            report_data['metrics']['final_capital'] = round(self.final_capital, 8)
        
        # [FIX-RG-1] Sanitisation récursive avant sérialisation.
        # Corrige le crash ValueError sur float('inf') dans long_stats /
        # short_stats.profit_factor retourné par calculate_directional_stats().
        report_data = self._deep_sanitize_json(report_data)
        
        # Save
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(report_data, f, indent=2, ensure_ascii=False)
        
        self.logger.info(f"JSON report generated: {output_path}")
        return output_path.resolve()
    
    def generate_csv_report(
        self,
        output_path: Union[str, Path]
    ) -> Path:
        """
        Génère rapport CSV pour analyses externes (Excel, pandas).
        
        Format: metric_name, value
        
        Args:
            output_path: Chemin fichier CSV sortie
        
        Returns:
            Path fichier créé
        
        Examples:
            >>> path = report_gen.generate_csv_report('metrics.csv')
        """
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Calculer métriques
        results = self.metrics.calculate_all()

        # [v2.2.3 — FIX-RG-6] Sanitisation avant aplatissement.
        # _flatten_metrics() sans sanitisation préalable écrivait float('inf')
        # brut dans le CSV → incompatible Excel/pandas (lu comme erreur).
        results_sanitized = self._deep_sanitize_json(results)

        # Flatten nested dicts
        flat_results = self._flatten_metrics(results_sanitized)
        
        # Save CSV
        with open(output_path, 'w', encoding='utf-8', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['Metric', 'Value'])
            for key, value in flat_results.items():
                writer.writerow([key, value])
        
        self.logger.info(f"CSV report generated: {output_path}")
        return output_path.resolve()
    
    # ========================================================================
    # GÉNÉRATION COMPLÈTE
    # ========================================================================
    
    def generate_all_reports(
        self,
        output_dir: Union[str, Path]
    ) -> Dict[str, Path]:
        """
        Génère TOUS les rapports dans répertoire organisé.
        
        Structure créée:
            output_dir/
                ├── report.html
                ├── report.md
                ├── summary.txt
                ├── metrics.json
                ├── metrics.csv
                └── charts/
                    ├── equity_curve.png
                    ├── drawdown.png
                    └── trailing_stop.png
        
        Args:
            output_dir: Répertoire sortie
        
        Returns:
            Dict avec tous paths créés
        
        Examples:
            >>> paths = report_gen.generate_all_reports(
            ...     'results/backtests/sessions/session_2026-02-22/'
            ... )
            >>> print(paths['html'])
            >>> print(paths['json'])
        """
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        
        self.logger.info(f"Generating all reports in: {output_dir}")
        
        paths = {
            'html': self.generate_html_report(output_dir / 'report.html'),
            'markdown': self.generate_markdown_report(output_dir / 'report.md'),
            'text': output_dir / 'summary.txt',
            'json': self.generate_json_report(output_dir / 'metrics.json'),
            'csv': self.generate_csv_report(output_dir / 'metrics.csv')
        }
        
        # Text summary
        self.generate_text_summary(paths['text'])
        
        self.logger.info(f"All reports generated: {len(paths)} files")
        return paths
    
    # ========================================================================
    # GRAPHIQUES
    # ========================================================================
    
    def _generate_equity_chart(
        self,
        output_dir: Path
    ) -> Optional[Path]:
        """Génère graphique equity curve (PNG)."""
        try:
            # [FIX-RG-2] Accès thread-safe via _get_full_snapshot() au lieu de
            # self.metrics._equity_curve (accès direct hors lock — violation
            # d'encapsulation et risque race condition en contexte multi-thread).
            _, equity_curve = self.metrics._get_full_snapshot()
            
            if not equity_curve or len(equity_curve) < 2:
                self.logger.warning("Insufficient equity data for chart")
                return None
            
            # Extract data
            timestamps = [p['timestamp'] for p in equity_curve]
            equities = [p['equity'] for p in equity_curve]
            
            # Plot
            fig, ax = plt.subplots(figsize=(12, 6))
            ax.plot(timestamps, equities, linewidth=2, color='#2E86AB')
            ax.fill_between(
                timestamps, equities,
                alpha=0.2, color='#2E86AB'
            )
            
            # Styling
            ax.set_title('Equity Curve', fontsize=16, fontweight='bold')
            ax.set_xlabel('Time', fontsize=12)
            ax.set_ylabel('Equity (USDT)', fontsize=12)
            ax.grid(True, alpha=0.3)
            ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m-%d'))
            fig.autofmt_xdate()
            
            # Save
            output_path = output_dir / 'equity_curve.png'
            fig.savefig(output_path, dpi=100, bbox_inches='tight')
            plt.close(fig)
            
            self.logger.debug(f"Equity chart generated: {output_path}")
            return output_path
        
        except Exception as e:
            self.logger.error(f"Failed to generate equity chart: {e}")
            return None
    
    def _generate_drawdown_chart(
        self,
        output_dir: Path
    ) -> Optional[Path]:
        """Génère graphique drawdown (PNG)."""
        try:
            # [FIX-RG-2] Accès thread-safe via _get_full_snapshot() — même
            # correctif que _generate_equity_chart().
            _, equity_curve = self.metrics._get_full_snapshot()
            
            if not equity_curve or len(equity_curve) < 2:
                return None
            
            # Calculate drawdown %
            timestamps = [p['timestamp'] for p in equity_curve]
            equities = [p['equity'] for p in equity_curve]
            
            drawdowns = []
            peak = equities[0]
            
            for equity in equities:
                if equity > peak:
                    peak = equity
                dd = ((peak - equity) / peak) * 100 if peak > 0 else 0
                drawdowns.append(-dd)  # Négatif pour affichage
            
            # Plot
            fig, ax = plt.subplots(figsize=(12, 6))
            ax.fill_between(
                timestamps, drawdowns, 0,
                where=[d < 0 for d in drawdowns],
                color='#A23B72', alpha=0.6
            )
            ax.plot(timestamps, drawdowns, linewidth=2, color='#A23B72')
            
            # Styling
            ax.set_title('Drawdown', fontsize=16, fontweight='bold')
            ax.set_xlabel('Time', fontsize=12)
            ax.set_ylabel('Drawdown (%)', fontsize=12)
            ax.grid(True, alpha=0.3)
            ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m-%d'))
            fig.autofmt_xdate()
            
            # Save
            output_path = output_dir / 'drawdown.png'
            fig.savefig(output_path, dpi=100, bbox_inches='tight')
            plt.close(fig)
            
            self.logger.debug(f"Drawdown chart generated: {output_path}")
            return output_path
        
        except Exception as e:
            self.logger.error(f"Failed to generate drawdown chart: {e}")
            return None
    
    # ========================================================================
    # BUILDERS - HTML
    # ========================================================================
    
    def _build_html(
        self,
        results: Dict[str, Any],
        equity_chart: Optional[Path],
        dd_chart: Optional[Path]
    ) -> str:
        """Construit HTML complet avec CSS."""
        css = self._get_css()
        
        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Trading Report - {self.session_name}</title>
    <style>{css}</style>
</head>
<body>
    <div class="container">
        {self._build_html_header(results)}
        {self._build_html_executive_summary(results)}
        {self._build_html_performance_metrics(results)}
        {self._build_html_risk_section(results)}
        {self._build_html_charts_section(equity_chart, dd_chart)}
        {self._build_html_directional_stats(results)}
        {self._build_html_footer()}
    </div>
</body>
</html>"""
        
        return html
    
    def _build_html_header(self, results: Dict[str, Any]) -> str:
        """Build HTML header."""
        period_str = (
            f"{self.session_start} → {self.session_end}"
            if self.session_start and self.session_end
            else "N/A"
        )
        return f"""
        <div class="header">
            <h1>🚀 BULLET-1 Trading Report</h1>
            <div class="session-info">
                <p><strong>Session:</strong> {self.session_name}</p>
                <p><strong>Session #:</strong> {self.session_number}</p>
                <p><strong>configuration_name:</strong> {self.configuration_name}</p>
                <p><strong>Period:</strong> {period_str}</p>
                <p><strong>Mode:</strong> {self.mode.upper()}</p>
                <p><strong>Initial Capital:</strong> {results.get('initial_capital', 0):,.2f} USDT</p>
                <p><strong>Final Capital:</strong> {self.final_capital:,.2f} USDT</p>
                <p><strong>Generated:</strong> {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}</p>
            </div>
        </div>
        """
    
    def _build_html_executive_summary(
        self,
        results: Dict[str, Any]
    ) -> str:
        """Build executive summary section."""
        # [FIX-RG-3] La classe CSS de la carte Total PnL est désormais conditionnelle.
        # Précédemment hardcodée à 'positive', elle restait verte même en cas de perte.
        total_pnl = results.get('total_pnl', 0.0) or 0.0
        pnl_card_class = 'metric-card positive' if total_pnl >= 0 else 'metric-card negative'

        return f"""
        <div class="section">
            <h2>📊 Executive Summary</h2>
            <div class="metrics-grid">
                <div class="{pnl_card_class}" title="Total Profit/Loss">
                    <div class="metric-label">Total PnL</div>
                    <div class="metric-value">{results['total_pnl']:+,.2f} USDT</div>
                </div>
                <div class="metric-card" title="Win Rate Percentage">
                    <div class="metric-label">Win Rate</div>
                    <div class="metric-value">{results['win_rate']:.1f}%</div>
                </div>
                <div class="metric-card" title="Total Number of Trades">
                    <div class="metric-label">Total Trades</div>
                    <div class="metric-value">{results['total_trades']}</div>
                </div>
                <div class="metric-card" title="Sharpe Ratio (Risk-Adjusted Return)">
                    <div class="metric-label">Sharpe Ratio</div>
                    <div class="metric-value">{results['sharpe_ratio']:.2f}</div>
                </div>
                <div class="metric-card negative" title="Maximum Drawdown">
                    <div class="metric-label">Max Drawdown</div>
                    <div class="metric-value">{results['max_drawdown_pct']:.2f}%</div>
                </div>
                <div class="metric-card" title="Profit Factor">
                    <div class="metric-label">Profit Factor</div>
                    <div class="metric-value">{self._fmt_inf(results.get('profit_factor', 0))}</div>
                </div>
            </div>
        </div>
        """
    
    def _build_html_performance_metrics(
        self,
        results: Dict[str, Any]
    ) -> str:
        """Build performance metrics table."""
        cagr_str = self._fmt_cagr(results)
        
        return f"""
        <div class="section">
            <h2>📈 Performance Metrics</h2>
            <table class="metrics-table">
                <tr>
                    <td><strong>Total Trades</strong></td>
                    <td>{results['total_trades']}</td>
                    <td><strong>Winning Trades</strong></td>
                    <td>{results['winning_trades']}</td>
                </tr>
                <tr>
                    <td><strong>Losing Trades</strong></td>
                    <td>{results['losing_trades']}</td>
                    <td><strong>Win Rate</strong></td>
                    <td>{results['win_rate']:.2f}%</td>
                </tr>
                <tr>
                    <td><strong>Profit Factor</strong></td>
                    <td>{self._fmt_inf(results.get('profit_factor', 0))}</td>
                    <td><strong>R-Ratio</strong></td>
                    <td>{self._fmt_inf(results.get('r_ratio', 0))}</td>
                </tr>
                <tr>
                    <td><strong>Expectancy</strong></td>
                    <td>{results['expectancy']:+.2f} USDT</td>
                    <td><strong>Kelly Criterion</strong></td>
                    <td>{results.get('kelly_criterion', 0):.2%}</td>
                </tr>
                <tr>
                    <td><strong>Total PnL</strong></td>
                    <td class="{'positive' if results['total_pnl'] > 0 else 'negative'}">{results['total_pnl']:+,.2f} USDT</td>
                    <td><strong>Total Return</strong></td>
                    <td>{results.get('total_return_pct', 0):.2f}%</td>
                </tr>
                <tr>
                    <td><strong>CAGR</strong></td>
                    <td>{cagr_str}</td>
                    <td><strong>Total Fees</strong></td>
                    <td>{results.get('total_fees', 0):.2f} USDT</td>
                </tr>
                <tr>
                    <td><strong>Avg Win</strong></td>
                    <td class="positive">{results['avg_win']:.2f} USDT</td>
                    <td><strong>Avg Loss</strong></td>
                    <td class="negative">{results['avg_loss']:.2f} USDT</td>
                </tr>
                <tr>
                    <td><strong>Best Trade</strong></td>
                    <td class="positive">{results.get('best_trade', 0):+.2f} USDT</td>
                    <td><strong>Worst Trade</strong></td>
                    <td class="negative">{results.get('worst_trade', 0):+.2f} USDT</td>
                </tr>
            </table>
        </div>
        """
    
    def _build_html_risk_section(
        self,
        results: Dict[str, Any]
    ) -> str:
        """Build risk analysis section."""
        return f"""
        <div class="section">
            <h2>⚠️ Risk Analysis</h2>
            <table class="metrics-table">
                <tr>
                    <td><strong>Sharpe Ratio</strong></td>
                    <td>{results['sharpe_ratio']:.2f}</td>
                    <td><strong>Sortino Ratio</strong></td>
                    <td>{results['sortino_ratio']:.2f}</td>
                </tr>
                <tr>
                    <td><strong>Calmar Ratio</strong></td>
                    <td>{results['calmar_ratio']:.2f}</td>
                    <td><strong>Recovery Factor</strong></td>
                    <td>{results['recovery_factor']:.2f}</td>
                </tr>
                <tr>
                    <td><strong>Max Drawdown %</strong></td>
                    <td class="negative">{results['max_drawdown_pct']:.2f}%</td>
                    <td><strong>Max Drawdown USDT</strong></td>
                    <td class="negative">{results.get('max_drawdown_usdt', 0):.2f} USDT</td>
                </tr>
                <tr>
                    <td><strong>Max Consecutive Wins</strong></td>
                    <td>{results['max_consecutive_wins']}</td>
                    <td><strong>Max Consecutive Losses</strong></td>
                    <td>{results['max_consecutive_losses']}</td>
                </tr>
                <tr>
                    <td><strong>Consecutive Loss Drawdown</strong></td>
                    <td class="negative">{results.get('consecutive_loss_drawdown', 0):.2f} USDT</td>
                    <td><strong>Avg Holding Time</strong></td>
                    <td>{results['avg_holding_time_hours']:.1f}h</td>
                </tr>
            </table>
        </div>
        """
    
    def _build_html_charts_section(
        self,
        equity_chart: Optional[Path],
        dd_chart: Optional[Path]
    ) -> str:
        """Build charts section."""
        html = '<div class="section"><h2>📊 Charts</h2>'
        
        if equity_chart:
            html += f'<div class="chart"><img src="charts/{equity_chart.name}" alt="Equity Curve"></div>'
        
        if dd_chart:
            html += f'<div class="chart"><img src="charts/{dd_chart.name}" alt="Drawdown"></div>'
        
        html += '</div>'
        return html
    
    def _build_html_directional_stats(
        self,
        results: Dict[str, Any]
    ) -> str:
        """Build directional stats section."""
        long_stats = results.get('long_stats', {})
        short_stats = results.get('short_stats', {})
        
        return f"""
        <div class="section">
            <h2>📍 Directional Stats</h2>
            <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 20px;">
                <div>
                    <h3>🟢 Long Positions</h3>
                    <table class="metrics-table">
                        <tr><td><strong>Count</strong></td><td>{long_stats.get('count', 0)}</td></tr>
                        <tr><td><strong>Win Rate</strong></td><td>{long_stats.get('win_rate', 0):.1f}%</td></tr>
                        <tr><td><strong>Avg PnL</strong></td><td>{long_stats.get('avg_pnl', 0):+.2f} USDT</td></tr>
                        <tr><td><strong>Total PnL</strong></td><td>{long_stats.get('total_pnl', 0):+.2f} USDT</td></tr>
                        <tr><td><strong>Profit Factor</strong></td><td>{self._fmt_inf(long_stats.get('profit_factor', 0))}</td></tr>
                    </table>
                </div>
                <div>
                    <h3>🔴 Short Positions</h3>
                    <table class="metrics-table">
                        <tr><td><strong>Count</strong></td><td>{short_stats.get('count', 0)}</td></tr>
                        <tr><td><strong>Win Rate</strong></td><td>{short_stats.get('win_rate', 0):.1f}%</td></tr>
                        <tr><td><strong>Avg PnL</strong></td><td>{short_stats.get('avg_pnl', 0):+.2f} USDT</td></tr>
                        <tr><td><strong>Total PnL</strong></td><td>{short_stats.get('total_pnl', 0):+.2f} USDT</td></tr>
                        <tr><td><strong>Profit Factor</strong></td><td>{self._fmt_inf(short_stats.get('profit_factor', 0))}</td></tr>
                    </table>
                </div>
            </div>
        </div>
        """
    
    def _build_html_footer(self) -> str:
        """Build HTML footer."""
        return f"""
        <div class="footer">
            <p>Generated by BULLET-1 ReportGenerator v{_VERSION} | {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}</p>
        </div>
        """
    
    def _get_css(self) -> str:
        """Retourne CSS styling professionnel."""
        return """
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            padding: 20px;
            line-height: 1.6;
        }
        .container {
            max-width: 1200px;
            margin: 0 auto;
            background: white;
            border-radius: 10px;
            box-shadow: 0 10px 40px rgba(0,0,0,0.1);
            overflow: hidden;
        }
        .header {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 40px;
            text-align: center;
        }
        .header h1 { font-size: 2.5em; margin-bottom: 10px; }
        .session-info { margin-top: 20px; opacity: 0.9; }
        .session-info p { margin: 5px 0; }
        .section {
            padding: 30px 40px;
            border-bottom: 1px solid #e0e0e0;
        }
        .section:last-child { border-bottom: none; }
        .section h2 {
            font-size: 1.8em;
            margin-bottom: 20px;
            color: #333;
        }
        .metrics-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 20px;
            margin: 20px 0;
        }
        .metric-card {
            background: #f5f7fa;
            padding: 20px;
            border-radius: 8px;
            text-align: center;
            transition: transform 0.2s;
        }
        .metric-card:hover { transform: translateY(-5px); }
        .metric-card.positive { background: #d4edda; border-left: 4px solid #28a745; }
        .metric-card.negative { background: #f8d7da; border-left: 4px solid #dc3545; }
        .metric-label {
            font-size: 0.9em;
            color: #666;
            text-transform: uppercase;
            letter-spacing: 1px;
            margin-bottom: 10px;
        }
        .metric-value {
            font-size: 1.8em;
            font-weight: bold;
            color: #333;
        }
        .metrics-table {
            width: 100%;
            border-collapse: collapse;
            margin: 20px 0;
        }
        .metrics-table td {
            padding: 12px;
            border-bottom: 1px solid #e0e0e0;
        }
        .metrics-table td:first-child { width: 40%; }
        .metrics-table tr:hover { background: #f5f7fa; }
        .positive { color: #28a745; font-weight: bold; }
        .negative { color: #dc3545; font-weight: bold; }
        .chart {
            margin: 20px 0;
            text-align: center;
        }
        .chart img {
            max-width: 100%;
            height: auto;
            border-radius: 8px;
            box-shadow: 0 4px 8px rgba(0,0,0,0.1);
        }
        .footer {
            background: #f5f7fa;
            padding: 20px;
            text-align: center;
            color: #666;
            font-size: 0.9em;
        }
        h3 { margin: 20px 0 10px 0; color: #555; }
        """
    
    # ========================================================================
    # BUILDERS - MARKDOWN
    # ========================================================================
    
    def _build_markdown(self, results: Dict[str, Any]) -> str:
        """Construit Markdown complet."""
        cagr_str = self._fmt_cagr(results)
        
        period_str = (
            f"{self.session_start} → {self.session_end}"
            if self.session_start and self.session_end else "N/A"
        )

        md = f"""# 🚀 BULLET-1 Trading Report

**Session:** {self.session_name}  
**Session #:** {self.session_number}  
**Strategy:** {self.configuration_name}  
**Period:** {period_str}  
**Mode:** {self.mode.upper()}  
**Generated:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}  
**Initial Capital:** {results['initial_capital']:,.2f} USDT  
**Final Capital:** {self.final_capital:,.2f} USDT

---

## 📊 Executive Summary

| Metric | Value |
|--------|-------|
| **Total PnL** | **{results['total_pnl']:+,.2f} USDT** |
| **Win Rate** | {results['win_rate']:.1f}% |
| **Total Trades** | {results['total_trades']} |
| **Sharpe Ratio** | {results['sharpe_ratio']:.2f} |
| **Max Drawdown** | {results['max_drawdown_pct']:.2f}% |
| **Profit Factor** | {self._fmt_inf(results.get('profit_factor', 0))} |

---

## 📈 Performance Metrics

### Win/Loss
- **Total Trades:** {results['total_trades']}
- **Winning Trades:** {results['winning_trades']}
- **Losing Trades:** {results['losing_trades']}
- **Win Rate:** {results['win_rate']:.2f}%
- **Profit Factor:** {self._fmt_inf(results.get('profit_factor', 0))}
- **R-Ratio:** {self._fmt_inf(results.get('r_ratio', 0))}
- **Expectancy:** {results['expectancy']:+.2f} USDT/trade
- **Kelly Criterion:** {results.get('kelly_criterion', 0):.2%}

### PnL
- **Total PnL:** {results['total_pnl']:+,.2f} USDT
- **Total Return:** {results.get('total_return_pct', 0):.2f}%
- **CAGR:** {cagr_str}
- **Gross Profit:** {results['gross_profit']:+,.2f} USDT
- **Gross Loss:** {results['gross_loss']:+,.2f} USDT
- **Total Fees:** {results.get('total_fees', 0):.2f} USDT
- **Best Trade:** {results.get('best_trade', 0):+.2f} USDT
- **Worst Trade:** {results.get('worst_trade', 0):+.2f} USDT

### Averages
- **Avg Win:** {results['avg_win']:.2f} USDT
- **Avg Loss:** {results['avg_loss']:.2f} USDT
- **Avg Holding Time:** {results['avg_holding_time_hours']:.1f}h
- **Avg Holding (Winners):** {results.get('avg_holding_time_winners_hours', 0):.1f}h
- **Avg Holding (Losers):** {results.get('avg_holding_time_losers_hours', 0):.1f}h

---

## ⚠️ Risk Analysis

### Risk-Adjusted Returns
- **Sharpe Ratio:** {results['sharpe_ratio']:.2f}
- **Sortino Ratio:** {results['sortino_ratio']:.2f}
- **Calmar Ratio:** {results['calmar_ratio']:.2f}

### Drawdown
- **Max Drawdown %:** {results['max_drawdown_pct']:.2f}%
- **Max Drawdown USDT:** {results.get('max_drawdown_usdt', 0):.2f} USDT
- **Recovery Factor:** {results['recovery_factor']:.2f}
- **Consecutive Loss Drawdown:** {results.get('consecutive_loss_drawdown', 0):.2f} USDT

### Streaks
- **Max Consecutive Wins:** {results['max_consecutive_wins']}
- **Max Consecutive Losses:** {results['max_consecutive_losses']}

---

## 📍 Directional Stats

### 🟢 Long Positions
{self._build_markdown_directional_table(results.get('long_stats', {}))}

### 🔴 Short Positions
{self._build_markdown_directional_table(results.get('short_stats', {}))}

---

## 📝 Notes

- CAGR calculated based on actual elapsed time (entry_time/exit_time)
- Sharpe/Sortino ratios calculated on per-trade % returns
- Kelly Criterion indicates optimal position sizing (use 1/4 or 1/2 Kelly in practice)
- MAE/MFE stats available if 'mae'/'mfe' present in trades

---

**Report generated by BULLET-1 ReportGenerator v{_VERSION}**
"""
        return md
    
    def _build_markdown_directional_table(
        self,
        stats: Dict[str, Any]
    ) -> str:
        """Build Markdown table pour stats directionnelles."""
        if not stats:
            return "_No data available_"
        
        return f"""
| Metric | Value |
|--------|-------|
| **Count** | {stats.get('count', 0)} |
| **Win Rate** | {stats.get('win_rate', 0):.1f}% |
| **Avg PnL** | {stats.get('avg_pnl', 0):+.2f} USDT |
| **Total PnL** | {stats.get('total_pnl', 0):+.2f} USDT |
| **Profit Factor** | {self._fmt_inf(stats.get('profit_factor', 0))} |
"""
    
    # ========================================================================
    # BUILDERS - TEXT
    # ========================================================================
    
    def _build_text_summary(self, results: Dict[str, Any]) -> str:
        """Construit résumé texte compact."""
        cagr_str = self._fmt_cagr(results)
        
        period_str = (
            f"{self.session_start} → {self.session_end}"
            if self.session_start and self.session_end else "N/A"
        )

        text = f"""
{'='*70}
BULLET-1 TRADING REPORT SUMMARY
{'='*70}

Session:        {self.session_name}
Session #:      {self.session_number}
Strategy:       {self.configuration_name}
Period:         {period_str}
Mode:           {self.mode.upper()}
Generated:      {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}
Initial Capital:{results.get('initial_capital', 0):>12,.2f} USDT
Final Capital:  {self.final_capital:>12,.2f} USDT

{'─'*70}
OVERVIEW
{'─'*70}
Total Trades:        {results['total_trades']:>8}
Winning Trades:      {results['winning_trades']:>8}
Losing Trades:       {results['losing_trades']:>8}
Win Rate:            {results['win_rate']:>7.2f}%

{'─'*70}
PERFORMANCE
{'─'*70}
Total PnL:           {results['total_pnl']:>+12,.2f} USDT
Total Return:        {results.get('total_return_pct', 0):>7.2f}%
CAGR:                {cagr_str}
Profit Factor:       {self._fmt_inf(results.get('profit_factor', 0)):>10}
Expectancy:          {results['expectancy']:>+12.2f} USDT/trade

Avg Win:             {results['avg_win']:>12.2f} USDT
Avg Loss:            {results['avg_loss']:>12.2f} USDT
Best Trade:          {results.get('best_trade', 0):>+12.2f} USDT
Worst Trade:         {results.get('worst_trade', 0):>+12.2f} USDT

{'─'*70}
RISK METRICS
{'─'*70}
Sharpe Ratio:        {results['sharpe_ratio']:>7.2f}
Sortino Ratio:       {results['sortino_ratio']:>7.2f}
Calmar Ratio:        {results['calmar_ratio']:>7.2f}
Max Drawdown:        {results['max_drawdown_pct']:>7.2f}%
Recovery Factor:     {results['recovery_factor']:>7.2f}

Max Consecutive Wins:    {results['max_consecutive_wins']:>4}
Max Consecutive Losses:  {results['max_consecutive_losses']:>4}

{'─'*70}
DIRECTIONAL STATS
{'─'*70}
LONG  - Count: {results.get('long_stats', {}).get('count', 0):>4} | WR: {results.get('long_stats', {}).get('win_rate', 0):>5.1f}% | PnL: {results.get('long_stats', {}).get('total_pnl', 0):>+10.2f}
SHORT - Count: {results.get('short_stats', {}).get('count', 0):>4} | WR: {results.get('short_stats', {}).get('win_rate', 0):>5.1f}% | PnL: {results.get('short_stats', {}).get('total_pnl', 0):>+10.2f}

{'='*70}
"""
        return text
    
    # ========================================================================
    # HELPERS
    # ========================================================================

    @staticmethod
    def _fmt_inf(val: Any, spec: str = '.2f') -> str:
        """Formate une valeur numérique pour affichage textuel.

        Convertit le sentinel JSON 999.0 (et float('inf')) en '∞'.
        Utilisé dans HTML, Markdown et Text pour tous les champs qui peuvent
        valoir float('inf') → 999.0 après _deep_sanitize_json().

        Args:
            val:  Valeur à formater.
            spec: Format spec Python (défaut '.2f').

        Returns:
            Chaîne formatée, '∞', 'N/A' ou 'NaN' selon le cas.
        """
        # [v2.2.3 — FIX-RG-7] math importé au niveau module — import local supprimé.
        if val is None:
            return 'N/A'
        if isinstance(val, float) and (math.isinf(val) or val == 999.0):
            return '∞'
        if isinstance(val, float) and math.isnan(val):
            return 'NaN'
        return format(val, spec)

    @staticmethod
    def _fmt_cagr(results: Dict[str, Any]) -> str:
        """Formate le CAGR pour affichage textuel.

        Retourne 'N/A (<raison>)' si cagr_pct est None, sinon la valeur en %.
        """
        cagr = results.get('cagr_pct')
        if cagr is not None:
            return f"{cagr:+.2f}%"
        note = results.get('cagr_note', 'insufficient_data')
        return f"N/A ({note})"
    
    @staticmethod
    def _deep_sanitize_json(data: Any) -> Any:
        """
        [FIX-RG-1] Sanitise récursivement une structure pour la sérialisation JSON.

        Gère les types non-sérialisables nativement par json.dump() :
            - float('inf') / float('-inf') → 999.0 / -999.0 (sentinel conventionnel)
            - float('nan')                 → None
            - datetime                     → str ISO 8601
            - dict                         → récursion sur les valeurs
            - list                         → récursion sur les éléments

        La version précédente de generate_json_report() appelait json.dump()
        sans sanitisation, ce qui provoquait un crash (ValueError: Out of range
        float values are not JSON compliant) dès que calculate_directional_stats()
        retournait float('inf') dans long_stats.profit_factor ou
        short_stats.profit_factor.

        Args:
            data: Valeur ou structure imbriquée à sanitiser.

        Returns:
            Structure JSON-sérialisable de même forme.
        """
        # [v2.2.3 — FIX-RG-7] math importé au niveau module — import local supprimé.
        if isinstance(data, dict):
            return {k: ReportGenerator._deep_sanitize_json(v) for k, v in data.items()}
        if isinstance(data, list):
            return [ReportGenerator._deep_sanitize_json(v) for v in data]
        if isinstance(data, float):
            if math.isinf(data):
                return 999.0 if data > 0 else -999.0
            if math.isnan(data):
                return None
        if isinstance(data, datetime):
            return data.isoformat()
        return data

    def _flatten_metrics(
        self,
        metrics: Dict[str, Any],
        prefix: str = ''
    ) -> Dict[str, Any]:
        """Aplatit dict nested pour CSV."""
        flat = {}
        
        for key, value in metrics.items():
            new_key = f"{prefix}{key}" if prefix else key
            
            if isinstance(value, dict):
                flat.update(self._flatten_metrics(value, f"{new_key}."))
            else:
                flat[new_key] = value
        
        return flat
    
    @staticmethod
    def _generate_session_name() -> str:
        """Génère nom session auto."""
        return f"session_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    
    def __repr__(self) -> str:
        """Représentation string."""
        return (
            f"ReportGenerator(mode={self.mode}, "
            f"session={self.session_name}, "
            f"trades={self.metrics.get_trades_count()})"
        )


# ============================================================================
# FIN DU MODULE
# ============================================================================
