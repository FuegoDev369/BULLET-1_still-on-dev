"""
BULLET-1 - Analytics Engine
============================

Moteur analytique et de reporting du pipeline de backtesting BULLET-1.

Position dans l'architecture globale :
    ohlcv_data_engine  ──▶  trading_engine  ──▶  analytics_engine
         (données)          (orchestration)          (rapports ◀─ ici)

Responsabilités :
    1. Consommer un EngineRunResult produit par TradingEngine.
    2. Reconstruire la courbe d'equity depuis les trades.
    3. Alimenter ReportGenerator (trades + equity curve).
    4. Orchestrer la génération de rapports multi-format (HTML, MD, JSON, CSV, TXT).
    5. Retourner un dict de chemins fichiers générés.

Interface publique unique :
    generate_reports(results) -> Dict[str, Any]

Conception :
    - Sans état (stateless) : aucune donnée de session n'est conservée entre appels.
    - Déterministe : même input → même output (chemins basés sur session_id UUID).
    - Défensive : chaque génération de rapport est isolée dans un try/except.
    - Configurable : comportement piloté par analytics_engine_config.json.
    - Non-destructif : les répertoires existants ne sont jamais effacés.

Version: 2.1.5
Date: 2026-03-13
Author: FuegoDev
"""

from __future__ import annotations

import json
import sys
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# ============================================================================
# RÉSOLUTION RACINE DU PROJET
# ============================================================================

# [v2.1.5 — FIX-AE-1] Pattern direct unifié BULLET-1 — remplace _find_project_root().
# L'ancienne fonction cherchait des marqueurs (.git, pyproject.toml…) sur le
# filesystem, comportement fragile en CI sans .git ou en environnement packagé.
# Même correction que FIX-ENG-6 dans engine.py.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# ============================================================================
# IMPORTS BULLET-1
# ============================================================================

from src.utils.logger import BulletLogger
from src.utils.helpers import ensure_directory, sanitize_filename
from src.backtesting.report_generator import ReportGenerator


# ============================================================================
# CONSTANTES
# ============================================================================

#: Chemin vers le fichier de configuration centralisé.
_CONFIG_PATH: Path = _PROJECT_ROOT / 'config' / 'analytics_engine_config.json'

#: Configuration par défaut — utilisée si le fichier JSON est absent ou corrompu.
_DEFAULT_CONFIG: Dict[str, Any] = {
    "analytics_reports": {
        "generate_html":     True,
        "generate_markdown": True,
        "generate_json":     True,
        "generate_csv":      True,
        "charts_enabled":    True,
        "output_base_dir":   "results/backtests/sessions/",
    }
}


# ============================================================================
# CLASSE PRINCIPALE
# ============================================================================

class AnalyticsEngine:
    """
    Moteur analytique et de reporting BULLET-1.

    Consomme un EngineRunResult produit par TradingEngine et génère des rapports
    multi-format (HTML, Markdown, JSON, CSV, Texte) dans un répertoire de session
    dédié et horodaté.

    Conception sans état (stateless) :
        Aucune donnée de session n'est conservée entre les appels à generate_reports().
        L'instance peut être réutilisée pour plusieurs sessions successives sans
        réinstanciation.

    Thread-safety :
        generate_reports() est conçu pour un usage mono-thread.
        La thread-safety des sous-modules (Metrics, BulletLogger) est gérée
        par ces modules eux-mêmes.

    Gestion des erreurs :
        Chaque format de rapport est généré dans un bloc isolé.
        Un échec sur un format n'interrompt pas la génération des autres.
        Les erreurs sont collectées et retournées dans le dict de sortie.

    Examples:
        >>> engine = AnalyticsEngine()
        >>> paths = engine.generate_reports(run_result)
        >>> print(paths['html'])   # Path vers le rapport HTML
        >>> print(paths['json'])   # Path vers le JSON de métriques
        >>> if paths['errors']:
        ...     print("Erreurs:", paths['errors'])
    """

    def __init__(self) -> None:
        """
        Initialise l'AnalyticsEngine.

        Charge la configuration depuis analytics_engine_config.json.
        En cas d'absence ou de corruption du fichier, repli sur _DEFAULT_CONFIG.
        """
        self.logger = BulletLogger()
        self._config = self._load_config()

        self.logger.info(
            f"AnalyticsEngine initialized | "
            f"config_path={_CONFIG_PATH} | "
            f"html={self._config['analytics_reports']['generate_html']} | "
            f"markdown={self._config['analytics_reports']['generate_markdown']} | "
            f"json={self._config['analytics_reports']['generate_json']} | "
            f"csv={self._config['analytics_reports']['generate_csv']} | "
            f"charts={self._config['analytics_reports']['charts_enabled']}"
        )

    # =========================================================================
    # API PUBLIQUE — POINT D'ENTRÉE UNIQUE
    # =========================================================================

    def generate_reports(self, results: Any) -> Dict[str, Any]:
        """
        Génère tous les rapports configurés à partir d'un résultat de session.

        Flux d'exécution :
            1. Extraction et normalisation des données (dataclass ou dict).
            2. Résolution du répertoire de session (déterministe, basé sur session_id).
            3. Instanciation du ReportGenerator.
            4. Alimentation avec trades + courbe equity reconstruite.
            5. Génération de chaque format de rapport (erreurs isolées).
            6. Émission du log standardisé de fin de session.
            7. Retour du dict de chemins.

        Args:
            results: EngineRunResult (dataclass) ou dict équivalent.
                     Champs attendus : session_id, session_summary, trades,
                     closed_positions, strategy_stats, simulator_stats,
                     step_results, errors, candles_processed.

        Returns:
            Dict[str, Any] avec les clés suivantes :
                session_dir  : Path du répertoire de session (ou None si échec critique)
                html         : Path du rapport HTML (ou None si désactivé/échec)
                markdown     : Path du rapport Markdown (ou None si désactivé/échec)
                text         : Path du résumé texte (ou None si échec)
                json         : Path du JSON de métriques (ou None si désactivé/échec)
                csv          : Path du CSV de métriques (ou None si désactivé/échec)
                errors       : List[str] des erreurs non-fatales survenues

        Note:
            Cette méthode ne lève jamais d'exception. Toute erreur critique est
            capturée, loggée, et retournée dans 'errors'.
        """
        generation_errors: List[str] = []

        try:
            # ── 1. Extraction données ─────────────────────────────────────────
            run_data = self._extract_run_data(results)

            self.logger.info(
                f"AnalyticsEngine.generate_reports | "
                f"session_id={run_data['session_id']} | "
                f"trades={len(run_data['trades'])} | "
                f"candles={run_data['candles_processed']}"
            )

            # ── 2. Répertoire de session ──────────────────────────────────────
            # [v2.1.5 — FIX-AE-2] session_n passé directement en paramètre.
            # L'ancienne implémentation posait self._current_session_n comme
            # attribut temporaire puis le supprimait avec del. Si une exception
            # survenait entre les deux, l'attribut restait sur l'instance —
            # engine "souillé" entre sessions, non thread-safe.
            session_dir = self._resolve_session_dir(
                run_data['session_id'],
                run_data['session_number'],
            )
            ensure_directory(session_dir)

            self.logger.debug(f"Session directory: {session_dir.resolve()}")

            # ── 3. ReportGenerator ────────────────────────────────────────────
            report_gen = self._build_report_generator(run_data)

            # ── 4. Alimentation données ───────────────────────────────────────
            self._feed_data(report_gen, run_data)

            # ── 5. Génération rapports ────────────────────────────────────────
            paths = self._generate_configured_reports(
                report_gen=report_gen,
                session_dir=session_dir,
                errors=generation_errors,
            )

            # ── 6. Log fin session ────────────────────────────────────────────
            self._log_session_end(run_data)

            # ── 7. Assemblage résultat ────────────────────────────────────────
            paths['session_dir'] = session_dir.resolve()
            paths['errors']      = generation_errors

            n_files = sum(1 for v in paths.values() if isinstance(v, Path))
            self.logger.info(
                f"AnalyticsEngine complete | "
                f"session_dir={session_dir.name} | "
                f"files_generated={n_files} | "
                f"errors={len(generation_errors)}"
            )

            return paths

        except Exception as exc:
            # Erreur critique inattendue — ne jamais propager vers l'appelant
            self.logger.exception(
                f"AnalyticsEngine.generate_reports: critical failure: {exc}"
            )
            return {
                'session_dir': None,
                'html':        None,
                'markdown':    None,
                'text':        None,
                'json':        None,
                'csv':         None,
                'errors':      [f"Critical failure: {exc}"] + generation_errors,
            }

    # =========================================================================
    # EXTRACTION & NORMALISATION DES DONNÉES
    # =========================================================================

    def _extract_run_data(self, results: Any) -> Dict[str, Any]:
        """
        Extrait et normalise les données depuis un EngineRunResult ou un dict.

        Abstraction qui permet à generate_reports() de fonctionner aussi bien
        avec un dataclass (usage normal) qu'un dict (tests, mock, sérialisation).

        Toutes les valeurs manquantes sont comblées par des défauts sécurisés.

        Args:
            results: EngineRunResult dataclass ou dict équivalent.

        Returns:
            Dict normalisé avec toutes les clés garanties présentes.
        """
        def _get(obj: Any, attr: str, default: Any = None) -> Any:
            """Accès unifié dataclass (getattr) et dict (get)."""
            if isinstance(obj, dict):
                return obj.get(attr, default)
            return getattr(obj, attr, default)

        # ── session_summary ───────────────────────────────────────────────────
        session_summary = _get(results, 'session_summary', {})
        if not isinstance(session_summary, dict):
            self.logger.warning("session_summary invalide — utilisation dict vide")
            session_summary = {}

        # ── session_id ────────────────────────────────────────────────────────
        # Priorité: champ direct du dataclass > session_summary > fallback horodaté
        session_id = (
            _get(results, 'session_id', '')
            or session_summary.get('session_id', '')
        )
        if not session_id or not isinstance(session_id, str):
            session_id = f"session_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')}"
            self.logger.warning(
                f"session_id absent ou invalide — fallback horodaté: {session_id}"
            )

        # ── trades ────────────────────────────────────────────────────────────
        trades = _get(results, 'trades', [])
        if not isinstance(trades, list):
            self.logger.warning("trades invalide — utilisation liste vide")
            trades = []

        # ── capital initial ───────────────────────────────────────────────────
        # Chaîne de priorité :
        #   1. session_summary['initial_capital']  (source principale)
        #   2. session_summary['initial_funds']    (alias session_manager)
        #   3. Premier trade capital_before        (fallback trades)
        #   4. Config initial_capital_backtest     (fallback config)
        # [FIX-AE-CAP] Supprime le fallback hardcodé 1000.0 USDT qui masquait
        # les sessions à 0 trades (capital_start affiché 0.00 au lieu du vrai capital).
        raw_capital = (
            session_summary.get('initial_capital')
            or session_summary.get('initial_funds')
        )
        try:
            initial_capital = float(raw_capital) if raw_capital else 0.0
            if initial_capital <= 0:
                # Fallback 3 : premier trade capital_before
                if trades:
                    cap_from_trade = float(trades[0].get('capital_before', 0))
                    if cap_from_trade > 0:
                        initial_capital = cap_from_trade
                        self.logger.debug(
                            f"initial_capital depuis trades[0].capital_before : {initial_capital:.2f}"
                        )
                # Fallback 4 : valeur neutre 100.0 USDT
                if initial_capital <= 0:
                    initial_capital = 100.0
                    self.logger.warning(
                        "initial_capital non résolu — fallback 100.0 USDT "
                        "(session sans trades et sans capital_before dans les trades)"
                    )
        except (TypeError, ValueError) as exc:
            self.logger.warning(
                f"initial_capital invalide ({raw_capital}): {exc} — fallback 100.0 USDT"
            )
            initial_capital = 100.0

        # ── mode ──────────────────────────────────────────────────────────────
        raw_mode = session_summary.get('mode', _get(results, 'mode', 'backtest'))
        mode = str(raw_mode).lower() if raw_mode else 'backtest'

        # ── configuration_name ─────────────────────────────────────────────────────
        # Source prioritaire : premier trade record (champ 'configuration_name' posé
        # par trading_engine._build_trade_record()). Fallback : strategy_stats,
        # puis 'unknown' si absent des deux sources.
        # [v2.1.5 — FIX-AE-5] Réutilise trades (déjà lu) au lieu de relire
        # _get(results, 'trades', []) une seconde fois.
        configuration_name = 'unknown'
        if trades and isinstance(trades[0], dict):
            configuration_name = trades[0].get('configuration_name', 'unknown') or 'unknown'
        if configuration_name == 'unknown':
            configuration_name = (
                _get(results, 'strategy_stats', {}) or {}
            ).get('configuration_name', 'unknown')

        # ── session_number ────────────────────────────────────────────────────
        session_number = int(session_summary.get('session_n', 0) or 0)

        # ── final_capital ─────────────────────────────────────────────────────
        # Source : session_summary['final_funds'] — capital réel en fin de session.
        raw_final = session_summary.get('final_funds')
        try:
            final_capital = float(raw_final) if raw_final is not None else initial_capital
        except (TypeError, ValueError):
            final_capital = initial_capital

        # ── session period ────────────────────────────────────────────────────
        # Dates au format 'YYYY-MM-DD' (stringifiées par session_manager.end_session)
        session_start = session_summary.get('start_date', '')
        session_end   = session_summary.get('end_date',   '')

        return {
            'session_id':        session_id,
            'session_number':    session_number,
            'session_start':     session_start,
            'session_end':       session_end,
            'final_capital':     final_capital,
            'configuration_name':     configuration_name,
            'session_summary':   session_summary,
            'trades':            trades,
            'closed_positions':  _get(results, 'closed_positions', []) or [],
            'strategy_stats':    _get(results, 'strategy_stats', {})   or {},
            'simulator_stats':   _get(results, 'simulator_stats', {})  or {},
            'step_results':      _get(results, 'step_results', {})      or {},
            'errors':            _get(results, 'errors', [])            or [],
            'candles_processed': _get(results, 'candles_processed', 0)  or 0,
            'initial_capital':   initial_capital,
            'mode':              mode,
        }

    # =========================================================================
    # RÉSOLUTION DU RÉPERTOIRE DE SESSION
    # =========================================================================

    def _resolve_session_dir(self, session_id: str, session_n: int = 0) -> Path:
        """
        Construit le chemin du répertoire de session.

        Le nom du répertoire est préfixé par le numéro de session (session_NNN_)
        pour une identification immédiate, suivi du session_id sanitisé pour
        garantir l'unicité et la compatibilité tous systèmes de fichiers.

        Structure : {output_base_dir}/session_NNN_{session_id_sanitized}/

        Exemple : results/backtests/sessions/session_001_56438d1e-82d5-4b3c-.../

        Args:
            session_id: Identifiant unique de la session (UUID ou horodaté).
            session_n:  Numéro de session (1-based). Défaut 0 si non fourni.

        Returns:
            Path absolu du répertoire de session (non créé).
        """
        cfg      = self._config['analytics_reports']
        base_dir = Path(cfg.get('output_base_dir', 'results/backtests/sessions/'))
        safe_id  = sanitize_filename(session_id)

        # [v2.1.5 — FIX-AE-2] session_n reçu en paramètre direct (plus d'attribut
        # temporaire self._current_session_n — voir generate_reports()).
        prefix = f"session_{session_n:03d}_"

        return base_dir / f"{prefix}{safe_id}"

    # =========================================================================
    # CONSTRUCTION DU REPORTGENERATOR
    # =========================================================================

    def _build_report_generator(self, run_data: Dict[str, Any]) -> ReportGenerator:
        """
        Instancie et configure un ReportGenerator pour la session courante.

        Args:
            run_data: Données normalisées de la session.

        Returns:
            Instance ReportGenerator prête à recevoir les données.
        """
        return ReportGenerator(
            mode            = run_data['mode'],
            session_name    = run_data['session_id'],
            session_number  = run_data['session_number'],
            session_start   = run_data['session_start'],
            session_end     = run_data['session_end'],
            initial_capital = run_data['initial_capital'],
            final_capital   = run_data['final_capital'],
            configuration_name   = run_data['configuration_name'],
            risk_free_rate  = 0.0,
        )

    # =========================================================================
    # ALIMENTATION DES DONNÉES
    # =========================================================================

    def _feed_data(
        self,
        report_gen : ReportGenerator,
        run_data   : Dict[str, Any],
    ) -> None:
        """
        Alimente le ReportGenerator avec les trades et la courbe d'equity.

        Trades :
            Seuls les trades contenant 'pnl_net' sont transmis (requis par
            Metrics.add_trade). Les trades invalides sont filtrés et comptés.

        Courbe equity :
            Reconstruite depuis les trades valides triés chronologiquement.
            Si charts_enabled=False dans la configuration, la courbe n'est
            pas transmise (économie de mémoire + les charts retournent None
            gracieusement sur données insuffisantes).

        Args:
            report_gen: Instance ReportGenerator cible.
            run_data:   Données normalisées de la session.
        """
        trades = run_data['trades']

        # ── Filtrage trades valides ───────────────────────────────────────────
        valid_trades = [
            t for t in trades
            if isinstance(t, dict) and 'pnl_net' in t
        ]

        n_invalid = len(trades) - len(valid_trades)
        if n_invalid > 0:
            self.logger.warning(
                f"Trades filtrés: {n_invalid}/{len(trades)} sans 'pnl_net' ignorés"
            )

        report_gen.set_trades(valid_trades)

        self.logger.debug(
            f"Trades chargés: {len(valid_trades)} valides / {len(trades)} total"
        )

        # ── Courbe equity ─────────────────────────────────────────────────────
        charts_enabled = self._config['analytics_reports'].get('charts_enabled', True)

        if charts_enabled:
            equity_curve = self._build_equity_curve(
                trades         = valid_trades,
                initial_capital = run_data['initial_capital'],
            )

            if equity_curve:
                report_gen.set_equity_curve(equity_curve)
                self.logger.debug(
                    f"Equity curve chargée: {len(equity_curve)} points"
                )
            else:
                self.logger.warning(
                    "Equity curve vide — pas de trades valides avec timestamps. "
                    "Les graphiques ne seront pas générés."
                )
        else:
            self.logger.debug(
                "charts_enabled=False — equity curve non transmise (graphiques ignorés)"
            )

    def _build_equity_curve(
        self,
        trades          : List[Dict[str, Any]],
        initial_capital : float,
    ) -> List[Dict[str, Any]]:
        """
        Reconstruit la courbe d'equity depuis les trades triés chronologiquement.

        Algorithme :
            - Point initial : capital initial au timestamp d'entrée du premier trade.
            - Pour chaque trade (trié par exit_time) : equity += pnl_net.
            - Chaque point : {'timestamp': datetime, 'equity': float}.

        Les trades sans exit_time valide (datetime) sont intégrés dans le PnL
        mais ne génèrent pas de point de courbe.

        Args:
            trades:          Liste de trades valides (contenant 'pnl_net').
            initial_capital: Capital de départ USDT.

        Returns:
            Liste de points de courbe triés chronologiquement.
            Liste vide si aucun trade avec timestamps valides.
        """
        if not trades:
            return []

        def _safe_datetime(ts: Any) -> Optional[datetime]:
            """Retourne un datetime si valide, None sinon."""
            if isinstance(ts, datetime):
                return ts
            return None

        def _safe_datetime_utc(ts: Any) -> Optional[datetime]:
            """
            Retourne un datetime UTC-aware si valide, None sinon.

            [v2.1.5 — FIX-AE-3] Normalisation systématique en UTC-aware pour
            éviter TypeError lors du tri si exit_time est naïf (CSV) et la
            sentinelle est aware, ou vice-versa.
            """
            dt = _safe_datetime(ts)
            if dt is None:
                return None
            if dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)

        # Trier par exit_time — les trades sans exit_time passent en dernier.
        # [v2.1.5 — FIX-AE-3] Sentinelle datetime.max UTC-aware pour cohérence
        # avec _safe_datetime_utc (tous les timestamps sont normalisés aware).
        _SORT_SENTINEL = datetime.max.replace(tzinfo=timezone.utc)

        def _exit_key(t: Dict) -> datetime:
            ts = _safe_datetime_utc(t.get('exit_time'))
            return ts if ts is not None else _SORT_SENTINEL

        sorted_trades = sorted(trades, key=_exit_key)

        curve    : List[Dict[str, Any]] = []
        equity   : float                = initial_capital

        # ── Point initial (capital avant le 1er trade) ────────────────────────
        first_entry = _safe_datetime_utc(sorted_trades[0].get('entry_time'))
        if first_entry is not None:
            curve.append({'timestamp': first_entry, 'equity': equity})

        # ── Point par fermeture de trade ──────────────────────────────────────
        for trade in sorted_trades:
            pnl_net = trade.get('pnl_net', 0.0)
            try:
                equity += float(pnl_net) if pnl_net is not None else 0.0
            except (TypeError, ValueError):
                self.logger.warning(
                    f"pnl_net non numérique ({pnl_net!r}) — ignoré pour courbe equity"
                )

            exit_time = _safe_datetime_utc(trade.get('exit_time'))
            if exit_time is not None:
                curve.append({'timestamp': exit_time, 'equity': equity})

        return curve

    # =========================================================================
    # GÉNÉRATION DES RAPPORTS (ORCHESTRATION)
    # =========================================================================

    def _generate_configured_reports(
        self,
        report_gen  : ReportGenerator,
        session_dir : Path,
        errors      : List[str],
    ) -> Dict[str, Any]:
        """
        Orchestre la génération de chaque format de rapport selon la configuration.

        Chaque format est généré dans un bloc isolé via _safe_generate().
        Un échec sur un format n'interrompt pas les autres.

        Args:
            report_gen:  Instance ReportGenerator alimentée.
            session_dir: Répertoire de session de destination.
            errors:      Liste d'erreurs à compléter en cas d'échec.

        Returns:
            Dict partiellement rempli { format: Path | None }.
        """
        cfg   = self._config['analytics_reports']
        paths : Dict[str, Any] = {}

        # ── HTML ──────────────────────────────────────────────────────────────
        if cfg.get('generate_html', True):
            paths['html'] = self._safe_generate(
                name         = 'html',
                generator_fn = lambda: report_gen.generate_html_report(
                    session_dir / 'report.html'
                ),
                errors       = errors,
            )
        else:
            paths['html'] = None
            self.logger.debug("HTML report: désactivé par config")

        # ── Markdown ──────────────────────────────────────────────────────────
        if cfg.get('generate_markdown', True):
            paths['markdown'] = self._safe_generate(
                name         = 'markdown',
                generator_fn = lambda: report_gen.generate_markdown_report(
                    session_dir / 'report.md'
                ),
                errors       = errors,
            )
        else:
            paths['markdown'] = None
            self.logger.debug("Markdown report: désactivé par config")

        # ── Texte (toujours généré — résumé console/email essentiel) ──────────
        paths['text'] = self._safe_generate(
            name         = 'text',
            generator_fn = lambda: self._generate_text_report(
                report_gen, session_dir
            ),
            errors       = errors,
        )

        # ── JSON ──────────────────────────────────────────────────────────────
        # [v2.1.1 — CLEAN-AE-2] Délégation directe à report_gen.generate_json_report().
        # La sanitisation float('inf') est désormais intégrée nativement dans
        # report_generator v2.2.0 via _deep_sanitize_json() [FIX-RG-1].
        # _generate_json_safe() et _sanitize_for_json() sont supprimés.
        if cfg.get('generate_json', True):
            paths['json'] = self._safe_generate(
                name         = 'json',
                generator_fn = lambda: report_gen.generate_json_report(
                    session_dir / 'metrics.json'
                ),
                errors       = errors,
            )
        else:
            paths['json'] = None
            self.logger.debug("JSON report: désactivé par config")

        # ── CSV ───────────────────────────────────────────────────────────────
        if cfg.get('generate_csv', True):
            paths['csv'] = self._safe_generate(
                name         = 'csv',
                generator_fn = lambda: report_gen.generate_csv_report(
                    session_dir / 'metrics.csv'
                ),
                errors       = errors,
            )
        else:
            paths['csv'] = None
            self.logger.debug("CSV report: désactivé par config")

        return paths

    # =========================================================================
    # GÉNÉRATEURS SPÉCIALISÉS
    # =========================================================================

    def _generate_text_report(
        self,
        report_gen  : ReportGenerator,
        session_dir : Path,
    ) -> Path:
        """
        Génère le résumé texte et retourne le Path résolu.

        Wrapper nécessaire car generate_text_summary() retourne le contenu str,
        pas un Path — la sauvegarde sur disque est optionnelle dans son API.

        Args:
            report_gen:  ReportGenerator alimenté.
            session_dir: Répertoire de destination.

        Returns:
            Path résolu du fichier summary.txt.
        """
        path = session_dir / 'summary.txt'
        report_gen.generate_text_summary(output_path=path)
        return path.resolve()

    # =========================================================================
    # ISOLATION DES ERREURS
    # =========================================================================

    def _safe_generate(
        self,
        name         : str,
        generator_fn : Callable[[], Optional[Path]],
        errors       : List[str],
    ) -> Optional[Path]:
        """
        Exécute un générateur de rapport en isolant ses erreurs.

        En cas d'exception :
            - L'erreur est loggée via self.logger.
            - Un message descriptif est ajouté à la liste errors.
            - None est retourné (pas de propagation).

        [v2.1.5 — FIX-AE-4] Refactorisée de @staticmethod en méthode d'instance.
        L'ancienne implémentation instanciait BulletLogger() à chaque erreur
        (contrainte de staticmethod sans accès à self). Utilise self.logger directement.

        Args:
            name:         Nom du format (pour le log/message d'erreur).
            generator_fn: Callable sans argument retournant Path ou None.
            errors:       Liste mutable d'erreurs à enrichir.

        Returns:
            Path du fichier généré, ou None en cas d'échec.
        """
        try:
            result = generator_fn()
            return result
        except Exception as exc:
            msg = f"Échec génération {name}: {exc}"
            self.logger.error(msg)
            errors.append(msg)
            return None

    # =========================================================================
    # LOGGING STANDARDISÉ
    # =========================================================================

    def _log_session_end(self, run_data: Dict[str, Any]) -> None:
        """
        Émet le log standardisé de fin de session via BulletLogger.log_session_end().

        Non-bloquant : les erreurs de logging ne remontent pas à l'appelant.

        Args:
            run_data: Données normalisées contenant session_summary et trades.
        """
        try:
            summary = run_data['session_summary']

            self.logger.log_session_end(
                session_n = int(summary.get('session_n', 0) or 0),
                pnl       = float(summary.get('pnl', 0.0)       or 0.0),
                pnl_pct   = float(summary.get('pnl_pct', 0.0)   or 0.0),
                trades    = len(run_data['trades']),
                win_rate  = float(summary.get('win_rate', 0.0)   or 0.0),
                reason    = str(summary.get('end_reason', 'unknown')),
            )

        except Exception as exc:
            # Non-fatal : le logging ne doit jamais interrompre la génération
            self.logger.warning(
                f"_log_session_end non-fatal failure: {exc}"
            )

    # =========================================================================
    # CONFIGURATION
    # =========================================================================

    def _load_config(self) -> Dict[str, Any]:
        """
        Charge et valide la configuration depuis analytics_engine_config.json.

        Stratégie de repli progressif :
            1. Fichier présent et valide → configuration fichier.
            2. Fichier absent → warning + DEFAULT_CONFIG.
            3. JSON corrompu → error + DEFAULT_CONFIG.
            4. Erreur système → error + DEFAULT_CONFIG.

        Returns:
            Dict de configuration validé avec toutes les clés garanties présentes.
        """
        config_path = _CONFIG_PATH

        if not config_path.exists():
            self.logger.warning(
                f"Config introuvable: {config_path} — "
                "valeurs par défaut utilisées"
            )
            return deepcopy(_DEFAULT_CONFIG)  # [v2.1.5 — FIX-AE-6] deep copy — shallow copy ne protège pas les sous-dicts

        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                raw = json.load(f)

            validated = self._validate_config(raw)
            self.logger.info(f"Config chargée depuis: {config_path}")
            return validated

        except json.JSONDecodeError as exc:
            self.logger.error(
                f"Config JSON corrompue ({exc}) — "
                "repli sur valeurs par défaut"
            )
            return deepcopy(_DEFAULT_CONFIG)  # [v2.1.5 — FIX-AE-6] deep copy — shallow copy ne protège pas les sous-dicts

        except OSError as exc:
            self.logger.error(
                f"Config lecture impossible ({exc}) — "
                "repli sur valeurs par défaut"
            )
            return deepcopy(_DEFAULT_CONFIG)  # [v2.1.5 — FIX-AE-6] deep copy — shallow copy ne protège pas les sous-dicts

        except Exception as exc:
            self.logger.error(
                f"Config erreur inattendue ({exc}) — "
                "repli sur valeurs par défaut"
            )
            return deepcopy(_DEFAULT_CONFIG)  # [v2.1.5 — FIX-AE-6] deep copy — shallow copy ne protège pas les sous-dicts

    def _validate_config(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        """
        Valide et complète la configuration chargée depuis le fichier JSON.

        Stratégie de merge : les valeurs du fichier priment. Les clés absentes
        ou de type invalide sont remplacées par les valeurs DEFAULT_CONFIG.
        Un warning est émis pour chaque correction.

        Args:
            raw: Dict brut chargé depuis le fichier JSON.

        Returns:
            Dict validé avec toutes les clés garanties présentes et correctement typées.
        """
        default_reports = _DEFAULT_CONFIG['analytics_reports']

        # Vérification structure racine
        if not isinstance(raw, dict) or 'analytics_reports' not in raw:
            self.logger.warning(
                "Config: clé 'analytics_reports' absente — "
                "utilisation des valeurs par défaut complètes"
            )
            return deepcopy(_DEFAULT_CONFIG)  # [v2.1.5 — FIX-AE-6] deep copy — shallow copy ne protège pas les sous-dicts

        raw_reports = raw['analytics_reports']
        if not isinstance(raw_reports, dict):
            self.logger.warning(
                "Config: 'analytics_reports' n'est pas un objet — "
                "utilisation des valeurs par défaut complètes"
            )
            return deepcopy(_DEFAULT_CONFIG)  # [v2.1.5 — FIX-AE-6] deep copy — shallow copy ne protège pas les sous-dicts

        # Merge: valeurs fichier + defaults pour les clés manquantes
        reports_cfg: Dict[str, Any] = {**default_reports, **raw_reports}

        # Validation des flags booléens
        _bool_keys = [
            'generate_html', 'generate_markdown',
            'generate_json', 'generate_csv', 'charts_enabled',
        ]
        for key in _bool_keys:
            if not isinstance(reports_cfg.get(key), bool):
                self.logger.warning(
                    f"Config: '{key}' doit être booléen "
                    f"(reçu: {reports_cfg.get(key)!r}) — "
                    f"reset à {default_reports[key]}"
                )
                reports_cfg[key] = default_reports[key]

        # Validation output_base_dir
        if not isinstance(reports_cfg.get('output_base_dir'), str):
            self.logger.warning(
                f"Config: 'output_base_dir' invalide "
                f"({reports_cfg.get('output_base_dir')!r}) — "
                f"reset à '{default_reports['output_base_dir']}'"
            )
            reports_cfg['output_base_dir'] = default_reports['output_base_dir']

        # Vérification output_base_dir non vide
        if not reports_cfg['output_base_dir'].strip():
            self.logger.warning(
                "Config: 'output_base_dir' est vide — "
                f"reset à '{default_reports['output_base_dir']}'"
            )
            reports_cfg['output_base_dir'] = default_reports['output_base_dir']

        return {'analytics_reports': reports_cfg}

    # =========================================================================
    # REPRÉSENTATION
    # =========================================================================

    def __repr__(self) -> str:
        cfg = self._config['analytics_reports']
        return (
            f"AnalyticsEngine("
            f"html={cfg.get('generate_html')}, "
            f"markdown={cfg.get('generate_markdown')}, "
            f"json={cfg.get('generate_json')}, "
            f"csv={cfg.get('generate_csv')}, "
            f"charts={cfg.get('charts_enabled')}, "
            f"output='{cfg.get('output_base_dir')}')"
        )


# ============================================================================
# FIN DU MODULE
# ============================================================================
