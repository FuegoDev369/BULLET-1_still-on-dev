"""
BULLET-1 - Optimizer (Phase 2)
================================

Optimisation des paramètres de la stratégie par grid search multi-phases.

Phases :
    2A — Paramètres stratégie dans config.json
         (8 configs × SL × levier × trailing_type × quality → 432 runs)
    2B — Paramètres indicateurs dans fichiers de config externes
         (ATR period/multiplier × Trend × UncertaintyCandle → ~243 runs)
    2C — Toggles stratégie avancés avec skipping conditionnel
         (breakout × trend_filter × volume × trailing avancé → ~72 runs)

Chaque phase peut être lancée indépendamment. La meilleure config de la
phase précédente est utilisée comme base pour la suivante (via best_config_XXX.json).

Architecture :
    - Grid search séquentiel (Android/Termux : pas de multiprocessing)
    - Un fichier config temporaire par run (config.json + configs externes)
    - Les fichiers de config indicateurs sont patchés/restaurés autour
      de chaque run (même mécanisme que analytics_engine_config.json)
    - Métriques agrégées cross-sessions pour Sharpe/Sortino ≥ 30j
    - Skipping conditionnel : évite les combos sans sens
      (ex: volume.mode irrelevant quand volume.enabled=false)

Nouveau champ "config_file" dans parameters_to_optimize :
    Absent → le paramètre est dans config.json (comportement existant)
    Présent → le paramètre est dans ce fichier de config externe

Nouveau champ "condition" dans parameters_to_optimize :
    {"param": "strategy.volume_confirmation.enabled", "value": true}
    → la variation est skippée si la condition n'est pas remplie

Version: 2.0.0
Date: 2026-04-24
Author: FuegoDev
"""

from __future__ import annotations

import copy
import itertools
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.utils.logger  import BulletLogger
from src.utils.helpers import ensure_directory, get_project_root

# ── Constantes ───────────────────────────────────────────────────────────────
_PRIMARY_METRIC_LONG  = "sharpe_ratio"
_PRIMARY_METRIC_SHORT = "profit_factor"
_MIN_DAYS_SHARPE      = 30
_MIN_TRADES_DEFAULT   = 10

# Mapping 8 configurations → (logic_direction, short_op, long_op)
_CONFIG_MAP: Dict[str, Tuple[str, str, str]] = {
    "1-normal":  ("normal",  ">", ">"),
    "2-normal":  ("normal",  ">", "<"),
    "3-normal":  ("normal",  "<", ">"),
    "4-normal":  ("normal",  "<", "<"),
    "5-reverse": ("reverse", "<", ">"),
    "6-reverse": ("reverse", ">", "<"),
    "7-reverse": ("reverse", "<", "<"),
    "8-reverse": ("reverse", ">", ">"),
}

# Stages de progressive_tightening proportionnels au base_multiplier
# Formule : stage_mult = base_mult * ratio_fixe
# Ratios : [0.85, 0.65, 0.45, 0.25] (profil de serrage progressif)
_PROGRESSIVE_STAGE_RATIOS = [0.85, 0.65, 0.45, 0.25]
_PROGRESSIVE_THRESHOLDS   = [0.5, 0.8, 1.2, 1.6]   # profit_threshold (en R)

# Analytics silencieux pendant l'optimisation
_ANALYTICS_SILENT: Dict[str, Any] = {
    "analytics_reports": {
        "generate_html":     False,
        "generate_markdown": False,
        "generate_json":     True,
        "generate_csv":      False,
        "charts_enabled":    False,
        "output_base_dir":   "results/backtests/sessions/"
    }
}

_B   = "\033[1m"
_C   = "\033[96m"
_G   = "\033[92m"
_Y   = "\033[93m"
_R   = "\033[91m"
_DIM = "\033[2m"
_E   = "\033[0m"


# =============================================================================
# UTILITAIRES JSON PATH
# =============================================================================

def _get_nested(d: dict, path: str, default: Any = None) -> Any:
    """Lit une valeur dans un dict imbriqué via un chemin 'a.b.c'."""
    keys = path.split(".")
    cur  = d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def _set_nested(d: dict, path: str, value: Any) -> None:
    """Écrit une valeur dans un dict imbriqué via un chemin 'a.b.c'."""
    keys = path.split(".")
    cur  = d
    for k in keys[:-1]:
        if k not in cur or not isinstance(cur[k], dict):
            cur[k] = {}
        cur = cur[k]
    cur[keys[-1]] = value


# =============================================================================
# DATACLASSES
# =============================================================================

@dataclass
class OptimizationParams:
    """
    Paramètres d'une combinaison testée.

    Attributes:
        configuration_name:    Nom de la config stratégie (ex: "8-reverse")
        stop_loss_offset_pct:  Offset SL en % du prix
        leverage:              Levier (entier)
        trailing_stop_type:    Type de trailing stop ('atr', 'candle', 'hybrid')
        min_quality_score:     Score qualité minimum du signal
        collateral_percentage: % du capital utilisé comme collateral
        config_json_overrides: Paramètres additionnels à injecter dans config.json
                               {json_path: value}
        external_overrides:    Paramètres à injecter dans des configs externes
                               {relative_file_path: {json_path: value}}
    """
    configuration_name:    str
    stop_loss_offset_pct:  float
    leverage:              int
    trailing_stop_type:    str
    min_quality_score:     int
    collateral_percentage: float
    config_json_overrides: Dict[str, Any]       = field(default_factory=dict)
    external_overrides:    Dict[str, Dict[str, Any]] = field(default_factory=dict)


@dataclass
class OptimizationResult:
    """Résultat d'un run (1 combinaison de paramètres)."""
    params:     OptimizationParams
    metrics:    Dict[str, Any]  = field(default_factory=dict)
    n_sessions: int             = 0
    elapsed_s:  float           = 0.0
    run_index:  int             = 0
    error:      Optional[str]  = None

    @property
    def is_valid(self) -> bool:
        return self.error is None and self.metrics.get("total_trades", 0) > 0

    @property
    def primary_score(self) -> float:
        if not self.is_valid:
            return -999.0
        elapsed = self.metrics.get("elapsed_days", 0.0)
        sharpe  = self.metrics.get("sharpe_ratio", 0.0)
        if elapsed >= _MIN_DAYS_SHARPE and sharpe != 0.0:
            return float(sharpe)
        return float(self.metrics.get("profit_factor", 0.0))

    @property
    def primary_metric_name(self) -> str:
        elapsed = self.metrics.get("elapsed_days", 0.0)
        sharpe  = self.metrics.get("sharpe_ratio", 0.0)
        if elapsed >= _MIN_DAYS_SHARPE and sharpe != 0.0:
            return _PRIMARY_METRIC_LONG
        return _PRIMARY_METRIC_SHORT

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_index":      self.run_index,
            "params": {
                "configuration_name":    self.params.configuration_name,
                "stop_loss_offset_pct":  self.params.stop_loss_offset_pct,
                "leverage":              self.params.leverage,
                "trailing_stop_type":    self.params.trailing_stop_type,
                "min_quality_score":     self.params.min_quality_score,
                "collateral_percentage": self.params.collateral_percentage,
                "config_json_overrides": self.params.config_json_overrides,
                "external_overrides":    self.params.external_overrides,
            },
            "metrics":        self.metrics,
            "n_sessions":     self.n_sessions,
            "elapsed_s":      round(self.elapsed_s, 2),
            "primary_score":  round(self.primary_score, 4),
            "primary_metric": self.primary_metric_name,
            "is_valid":       self.is_valid,
            "error":          self.error,
        }


# =============================================================================
# OPTIMIZER
# =============================================================================

class Optimizer:
    """
    Optimiseur grid search multi-phases pour BULLET-1.

    Nouveau par rapport à v1.0.0 :
        - Support paramètres dans fichiers de config externes
          (atr_config.json, trend_config.json, uncertainty_candle_config.json)
        - Skipping conditionnel (évite combos sans sens)
        - Stages progressive_tightening corrélés au base_multiplier
        - Patching/restauration générique pour N fichiers de config externes
    """

    def __init__(self, config_path: Optional[str] = None) -> None:
        self.logger = BulletLogger()
        self._root  = get_project_root()

        if config_path is None:
            self._config_path = self._root / "config" / "config.json"
        else:
            self._config_path = Path(config_path)

        if not self._config_path.exists():
            raise FileNotFoundError(f"Config introuvable : {self._config_path}")

        with open(self._config_path, "r", encoding="utf-8") as f:
            self._base_config: Dict[str, Any] = json.load(f)

        self._opt_cfg            = self._base_config.get("optimization", {})
        self._analytics_cfg_path = self._root / "config" / "analytics_engine_config.json"

        # Sauvegardes originales des configs externes
        self._analytics_cfg_bak:  Optional[Dict[str, Any]] = None
        self._external_cfgs_bak:  Dict[str, Any] = {}   # {rel_path: original_content}

        self.logger.info(
            f"Optimizer v2.0.0 — config={self._config_path.name}"
        )

    # =========================================================================
    # API PUBLIQUE
    # =========================================================================

    def run(self) -> List[OptimizationResult]:
        """Lance le grid search complet."""
        grid = self._build_grid()

        if not grid:
            self.logger.warning(
                "Grille vide — vérifiez 'optimization.parameters_to_optimize' dans config.json"
            )
            return []

        self._display_plan(grid)

        try:
            confirm = input(
                f"\n{_Y}Lancer l'optimisation ({len(grid)} runs) ? [O/n] : {_E}"
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            confirm = "o"

        if confirm not in ("", "o", "oui", "y", "yes"):
            print(f"{_Y}Optimisation annulée.{_E}")
            return []

        print()
        results: List[OptimizationResult] = []
        total   = len(grid)
        t_start = time.monotonic()

        self._patch_analytics_silent()

        try:
            for idx, params in enumerate(grid, start=1):
                self._display_run_header(idx, total, params)
                result = self._run_single(params, run_index=idx)
                results.append(result)
                self._display_run_result(result, idx, total, t_start)

        except KeyboardInterrupt:
            print(
                f"\n{_Y}⚠️  Optimisation interrompue "
                f"({len(results)}/{total} runs complétés).{_E}"
            )
        finally:
            self._restore_analytics()
            self._restore_external_configs()

        results = self._rank_results(results)

        if results:
            self._save_results(results)
            self._display_final_report(results)

        return results

    # =========================================================================
    # CONSTRUCTION DE LA GRILLE
    # =========================================================================

    def _build_grid(self) -> List[OptimizationParams]:
        """
        Construit la liste des combinaisons à tester.

        Gère trois types de paramètres :
            1. Paramètres de base (config.json, champs fixes de OptimizationParams)
            2. Paramètres additionnels config.json (config_json_overrides)
               → identifiés par l'absence de "config_file"
            3. Paramètres configs externes (external_overrides)
               → identifiés par la présence de "config_file"

        Skipping conditionnel :
            Si un paramètre a un champ "condition": {"param": "X.Y", "value": V},
            sa variation est ignorée si le paramètre X.Y n'a pas la valeur V
            dans la combinaison courante.
        """
        # ── Configurations stratégie ──────────────────────────────────────────
        requested = self._opt_cfg.get("strategy_configurations", None)
        configs   = (
            [c for c in requested if c in _CONFIG_MAP]
            if requested
            else list(_CONFIG_MAP.keys())
        )

        # ── Lecture et classification des paramètres ──────────────────────────
        base_params, config_extra_params, external_params = [], [], []

        BASE_PARAM_NAMES = {
            "stop_loss_offset_pct", "leverage",
            "trailing_stop_type", "min_quality_score"
        }

        for p in self._opt_cfg.get("parameters_to_optimize", []):
            name = p["name"]
            if name in BASE_PARAM_NAMES:
                base_params.append(p)
            elif p.get("config_file"):
                external_params.append(p)
            else:
                config_extra_params.append(p)

        # ── Listes de valeurs pour chaque paramètre ───────────────────────────
        def _param_values(p: dict) -> List[Any]:
            if p["type"] == "categorical":
                return list(p.get("values") or [])
            lo, hi, step = float(p["min"]), float(p["max"]), float(p["step"])
            vals, v = [], lo
            while v <= hi + 1e-9:
                vals.append(round(v, 6))
                v += step
            return [int(x) for x in vals] if p["type"] == "int" else vals

        def _default(name: str, path: list, fallback: Any) -> List[Any]:
            for p in base_params:
                if p["name"] == name:
                    return _param_values(p)
            val = self._base_config
            for k in path:
                val = val.get(k, {}) if isinstance(val, dict) else fallback
            return [val if val else fallback]

        sl_vals    = _default("stop_loss_offset_pct",
                              ["risk_management", "stop_loss_offset_pct"], 0.8)
        lev_vals   = _default("leverage", ["position", "leverage"], 10)
        trail_vals = _default("trailing_stop_type",
                              ["strategy", "trailing_stop", "type"], "atr")
        qual_vals  = _default("min_quality_score",
                              ["strategy", "min_quality_score"], 10)
        collateral_pct = self._base_config.get("position", {}).get(
            "collateral_percentage", 10.0
        )

        # Listes pour paramètres additionnels config.json et externes
        extra_names  = [p["name"] for p in config_extra_params]
        extra_values = [_param_values(p) for p in config_extra_params]
        ext_names    = [p["name"] for p in external_params]
        ext_files    = [p["config_file"] for p in external_params]
        ext_values   = [_param_values(p) for p in external_params]
        ext_conds    = [p.get("condition") for p in external_params]
        extra_conds  = [p.get("condition") for p in config_extra_params]

        # ── Produit cartésien avec skipping conditionnel ───────────────────
        #
        # Stratégie : produit COMPLET de tous les axes → pour chaque combo,
        # les params conditionnels dont la condition n'est pas remplie sont
        # forcés à None (exclus des overrides). Les combos dupliqués résultants
        # sont dédupliqués via un set de signatures.
        #
        # Exemple : enabled ∈ [False,True], mode ∈ ["basic","adv"] (cond: enabled=True)
        #   Produit complet : (F,basic),(F,adv),(T,basic),(T,adv) = 4 combos bruts
        #   Après forcing   : (F,None),(F,None),(T,basic),(T,adv) = 4
        #   Après dédup     : (F,None),(T,basic),(T,adv)          = 3 combos uniques ✅

        grid: List[OptimizationParams] = []
        seen_signatures: set = set()

        def _cond_met(cond: Optional[Dict], ctx: Dict) -> bool:
            """Condition remplie si la valeur du param de référence == valeur attendue."""
            if not cond:
                return True
            ref = cond["param"]
            val = ctx.get(ref, ctx.get(ref.split(".")[-1]))
            return val == cond["value"]

        base_axes = [configs, sl_vals, lev_vals, trail_vals, qual_vals]
        all_extra_axes = extra_values if extra_values else []
        all_ext_axes   = ext_values   if ext_values   else []

        extra_product = list(itertools.product(*all_extra_axes)) if all_extra_axes else [()]
        ext_product   = list(itertools.product(*all_ext_axes))   if all_ext_axes   else [()]

        for base_combo in itertools.product(*base_axes):
            cfg_name, sl, lev, trail, qual = base_combo

            base_ctx: Dict[str, Any] = {
                "configuration_name":   cfg_name,
                "stop_loss_offset_pct": sl,
                "leverage":             lev,
                "trailing_stop_type":   trail,
                "min_quality_score":    qual,
            }

            for raw_extra in extra_product:
                for raw_ext in ext_product:

                    # Contexte complet = base + valeurs extra brutes
                    ctx = dict(base_ctx)
                    for name, val in zip(extra_names, raw_extra):
                        if val is not None:
                            ctx[name] = val
                    for name, val in zip(ext_names, raw_ext):
                        if val is not None:
                            ctx[name] = val

                    # ── Forcer None sur les params conditionnels non actifs ──
                    forced_extra = list(raw_extra)
                    for i, cond in enumerate(extra_conds):
                        if cond and not _cond_met(cond, ctx):
                            forced_extra[i] = None

                    forced_ext = list(raw_ext)
                    for i, cond in enumerate(ext_conds):
                        if cond and not _cond_met(cond, ctx):
                            forced_ext[i] = None

                    # ── Signature pour déduplication ────────────────────────
                    sig = (
                        cfg_name, sl, lev, trail, qual,
                        tuple(forced_extra),
                        tuple(
                            (k, v)
                            for k, v in sorted(
                                {name: val for name, val in zip(ext_names, forced_ext)
                                 if val is not None}.items()
                            )
                        )
                    )
                    if sig in seen_signatures:
                        continue
                    seen_signatures.add(sig)

                    # ── Construire config_json_overrides ─────────────────────
                    cj_overrides: Dict[str, Any] = {}
                    for name, val in zip(extra_names, forced_extra):
                        if val is not None:
                            cj_overrides[name] = val
                            # Cas spécial : progressive_tightening=True → stages corrélés
                            if (name == "strategy.trailing_stop.atr_mode.progressive_tightening"
                                    and val is True):
                                ts_mult = float(ctx.get(
                                    "strategy.trailing_stop.atr_mode.base_multiplier",
                                    _get_nested(self._base_config,
                                                "strategy.trailing_stop.atr_mode.base_multiplier",
                                                2.0)
                                ))
                                cj_overrides["strategy.trailing_stop.atr_mode.stages"] = (
                                    self._compute_stages(ts_mult)
                                )

                    # ── Construire external_overrides ────────────────────────
                    ext_overrides: Dict[str, Dict[str, Any]] = {}
                    for name, cfg_file, val in zip(ext_names, ext_files, forced_ext):
                        if val is not None:
                            ext_overrides.setdefault(cfg_file, {})[name] = val

                    grid.append(OptimizationParams(
                        configuration_name    = cfg_name,
                        stop_loss_offset_pct  = sl,
                        leverage              = lev,
                        trailing_stop_type    = trail,
                        min_quality_score     = qual,
                        collateral_percentage = collateral_pct,
                        config_json_overrides = cj_overrides,
                        external_overrides    = ext_overrides,
                    ))

        return grid

    # =========================================================================
    # STAGES CORRÉÉS AU BASE_MULTIPLIER
    # =========================================================================

    @staticmethod
    def _compute_stages(base_multiplier: float) -> List[Dict[str, Any]]:
        """
        Calcule les stages de progressive_tightening proportionnels au base_multiplier.

        Formule : stage.multiplier = base_multiplier × ratio_fixe
        Ratios   : [0.85, 0.65, 0.45, 0.25] (profil de serrage progressif)

        Exemples :
            base_multiplier=2.0 → stages=[1.70, 1.30, 0.90, 0.50]
            base_multiplier=2.5 → stages=[2.13, 1.63, 1.13, 0.63]
            base_multiplier=3.0 → stages=[2.55, 1.95, 1.35, 0.75]
        """
        return [
            {
                "profit_threshold": threshold,
                "multiplier":       round(base_multiplier * ratio, 2)
            }
            for threshold, ratio in zip(
                _PROGRESSIVE_THRESHOLDS, _PROGRESSIVE_STAGE_RATIOS
            )
        ]

    # =========================================================================
    # EXÉCUTION D'UN SEUL RUN
    # =========================================================================

    def _run_single(
        self,
        params: OptimizationParams,
        run_index: int,
    ) -> OptimizationResult:
        """Exécute un backtest complet pour une combinaison de paramètres."""
        t0 = time.monotonic()
        tmp_config_path: Optional[Path] = None

        try:
            # 1. Patcher les configs externes (ATR, Trend, UncertaintyCandle…)
            self._patch_external_configs(params.external_overrides)

            # 2. Écrire config temporaire (config.json)
            tmp_config_path = self._build_temp_config(params, run_index)

            # 2b. [FIX-OPT-ORPHAN] Purger les fichiers trades orphelins
            # des sessions précédentes AVANT de lancer le nouveau run.
            self._purge_trades_dir()

            # 3. Lancer Engine
            from src.backtesting.engine import Engine
            engine  = Engine(config_path=str(tmp_config_path))
            paths   = engine.run()
            n_sess  = len(paths)

            # 4. Collecter et agréger les métriques
            all_trades  = self._collect_trades()
            agg_metrics = self._aggregate_metrics(all_trades, n_sess)

            return OptimizationResult(
                params     = params,
                metrics    = agg_metrics,
                n_sessions = n_sess,
                elapsed_s  = time.monotonic() - t0,
                run_index  = run_index,
            )

        except Exception as exc:
            self.logger.warning(
                f"[Optimizer] Run {run_index} FAILED "
                f"({params.configuration_name} "
                f"SL={params.stop_loss_offset_pct}% "
                f"lev={params.leverage}×) : {exc}"
            )
            return OptimizationResult(
                params    = params,
                elapsed_s = time.monotonic() - t0,
                run_index = run_index,
                error     = str(exc),
            )

        finally:
            # 5. Restaurer les configs externes (toujours)
            self._restore_external_configs()
            if tmp_config_path and tmp_config_path.exists():
                try:
                    tmp_config_path.unlink()
                except OSError:
                    pass

    # =========================================================================
    # CONFIG TEMPORAIRE (config.json)
    # =========================================================================

    def _build_temp_config(
        self,
        params: OptimizationParams,
        run_index: int,
    ) -> Path:
        """
        Construit le fichier config.json temporaire pour un run.

        Applique dans l'ordre :
            1. Paramètres de base (configuration_name, SL, levier, trailing, quality)
            2. config_json_overrides (paramètres additionnels via chemin JSON)
            3. Options d'isolation (reset_capital, market_context)
        """
        cfg = copy.deepcopy(self._base_config)

        # Paramètres stratégie de base
        direction, short_op, long_op = _CONFIG_MAP[params.configuration_name]
        cfg["strategy"]["configuration_name"]                              = params.configuration_name
        cfg["strategy"]["entry_logic"]["logic_direction"]                  = direction
        cfg["strategy"]["entry_logic"]["for_short_case_comparison_operator"] = short_op
        cfg["strategy"]["entry_logic"]["for_long_case_comparison_operator"]  = long_op
        cfg["strategy"]["trailing_stop"]["type"]                           = params.trailing_stop_type
        cfg["strategy"]["min_quality_score"]                               = params.min_quality_score
        cfg["risk_management"]["stop_loss_offset_pct"]                     = params.stop_loss_offset_pct
        cfg["position"]["leverage"]                                        = params.leverage
        cfg["position"]["collateral_percentage"]                           = params.collateral_percentage
        cfg["session_management"]["reset_capital_between_sessions"]        = self._opt_cfg.get(
            "use_reset_capital", True
        )

        # Overrides additionnels config.json (via chemin JSON)
        for path, value in params.config_json_overrides.items():
            _set_nested(cfg, path, value)

        # Désactiver market_context (non utile en optimization, économise RAM)
        if "engine_config" in cfg and "market_context" in cfg.get("engine_config", {}):
            cfg["engine_config"]["market_context"]["enabled"] = False

        tmp_path = (
            self._root / "config" /
            f"_optim_{run_index:04d}_{params.configuration_name}_{int(time.time()*1000)}.json"
        )
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)

        return tmp_path

    # =========================================================================
    # PATCH / RESTORE CONFIGS EXTERNES
    # =========================================================================

    def _patch_external_configs(
        self,
        external_overrides: Dict[str, Dict[str, Any]]
    ) -> None:
        """
        Patche les fichiers de config externes (ATR, Trend, UncertaintyCandle…)
        avec les overrides d'un run.

        Sauvegarde l'original de chaque fichier en mémoire.
        Appelé avant Engine.run(). Restauré dans _restore_external_configs()
        via le bloc finally de _run_single().

        Args:
            external_overrides: {relative_config_path: {json_dot_path: value}}
            Exemple:
                {
                    "config/atr_config.json": {
                        "atr_parameters.period": 14,
                        "atr_parameters.base_multiplier": 2.5
                    }
                }
        """
        if not external_overrides:
            return

        for rel_path, overrides in external_overrides.items():
            abs_path = self._root / rel_path

            # Lire et sauvegarder l'original (une seule fois par fichier)
            if rel_path not in self._external_cfgs_bak:
                if abs_path.exists():
                    with open(abs_path, "r", encoding="utf-8") as f:
                        self._external_cfgs_bak[rel_path] = json.load(f)
                else:
                    self._external_cfgs_bak[rel_path] = None

            # Appliquer les overrides
            original = self._external_cfgs_bak.get(rel_path)
            patched  = copy.deepcopy(original) if original else {}

            for json_path, value in overrides.items():
                _set_nested(patched, json_path, value)

            with open(abs_path, "w", encoding="utf-8") as f:
                json.dump(patched, f, indent=2, ensure_ascii=False)

        self.logger.debug(
            f"Configs externes patchées : {list(external_overrides.keys())}"
        )

    def _restore_external_configs(self) -> None:
        """
        Restaure tous les fichiers de config externes à leur état original.
        Appelé dans le finally de _run_single() — garanti même sur exception.
        """
        for rel_path, original in self._external_cfgs_bak.items():
            if original is None:
                continue
            abs_path = self._root / rel_path
            try:
                with open(abs_path, "w", encoding="utf-8") as f:
                    json.dump(original, f, indent=2, ensure_ascii=False)
            except Exception as exc:
                self.logger.error(
                    f"[Optimizer] Impossible de restaurer {rel_path} : {exc}"
                )

        # Réinitialiser pour le prochain run (chaque run re-sauvegarde)
        self._external_cfgs_bak.clear()

    # =========================================================================
    # PATCH / RESTORE ANALYTICS CONFIG
    # =========================================================================

    def _patch_analytics_silent(self) -> None:
        """Remplace analytics_engine_config.json par la version silencieuse."""
        if self._analytics_cfg_path.exists():
            with open(self._analytics_cfg_path, "r", encoding="utf-8") as f:
                self._analytics_cfg_bak = json.load(f)
        with open(self._analytics_cfg_path, "w", encoding="utf-8") as f:
            json.dump(_ANALYTICS_SILENT, f, indent=2)
        self.logger.debug("Analytics config → mode silencieux (optimization)")

    def _restore_analytics(self) -> None:
        """Restaure analytics_engine_config.json. Appelé dans finally de run()."""
        try:
            if self._analytics_cfg_bak is not None:
                with open(self._analytics_cfg_path, "w", encoding="utf-8") as f:
                    json.dump(self._analytics_cfg_bak, f, indent=2)
            self.logger.debug("Analytics config restaurée.")
        except Exception as exc:
            self.logger.error(
                f"[Optimizer] Impossible de restaurer analytics config : {exc}"
            )

    # =========================================================================
    # COLLECTE ET AGRÉGATION DES MÉTRIQUES
    # =========================================================================

    def _collect_trades(self) -> List[Dict[str, Any]]:
        """
        Collecte les trades du run Engine actuel.

        [FIX-OPT-ORPHAN] Avant de lire, purge tous les fichiers session_*_trades.json
        qui N'ONT PAS été créés/modifiés pendant ce run.
        
        Problème sans ce fix :
            Engine n'écrit pas de fichier pour les sessions sans trades (S4, S7…).
            Si un backtest précédent avait laissé session_004_trades.json et
            session_007_trades.json, _collect_trades() les lisait, ajoutant des
            trades fantômes au total. Résultat : optimizer reportait 30 trades
            alors que le backtest direct avec la même config n'en produisait que 10.

        Solution : purger le dossier trades/ AVANT chaque Engine.run(), puis
        lire ce qui reste. On purge dans _run_single() avant l'appel à Engine.
        Cette méthode lit simplement ce qu'il y a après le run.
        """
        trades_dir = self._root / "results" / "backtests" / "sessions" / "trades"
        all_trades: List[Dict[str, Any]] = []

        if not trades_dir.exists():
            return all_trades

        for f in sorted(trades_dir.glob("session_*_trades.json")):
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                trades = data if isinstance(data, list) else data.get("trades", [])
                all_trades.extend(trades)
            except Exception:
                pass

        return all_trades

    def _purge_trades_dir(self) -> None:
        """
        [FIX-OPT-ORPHAN] Supprime tous les fichiers session_*_trades.json
        avant chaque run Engine pour éviter la contamination par les fichiers
        orphelins des sessions précédentes (sessions sans trades qui ne
        génèrent pas de nouveau fichier).

        Appelé dans _run_single() AVANT Engine.run().
        """
        trades_dir = self._root / "results" / "backtests" / "sessions" / "trades"
        if not trades_dir.exists():
            return
        purged = 0
        for f in trades_dir.glob("session_*_trades.json"):
            try:
                f.unlink()
                purged += 1
            except OSError:
                pass
        if purged:
            self.logger.debug(f"[FIX-OPT-ORPHAN] {purged} fichier(s) trades purgés avant run Engine")

    def _aggregate_metrics(
        self,
        all_trades: List[Dict[str, Any]],
        n_sessions: int,
    ) -> Dict[str, Any]:
        """
        Calcule les métriques agrégées sur tous les trades d'un run.
        Sharpe/Sortino calculables si période totale ≥ 30 jours.
        """
        if not all_trades:
            return {
                "total_trades": 0, "win_rate": 0.0, "profit_factor": 0.0,
                "sharpe_ratio": 0.0, "sortino_ratio": 0.0, "total_pnl": 0.0,
                "max_drawdown_pct": 0.0, "elapsed_days": 0.0, "n_sessions": n_sessions,
            }

        from src.backtesting.metrics import Metrics

        initial_capital = float(
            self._base_config.get("capital", {}).get("initial_capital_backtest", 100.0)
        )
        metrics = Metrics(mode="backtest", initial_capital=initial_capital)

        for t in all_trades:
            try:
                metrics.add_trade(t)
            except Exception:
                pass

        result = metrics.calculate_all()
        result["n_sessions"]    = n_sessions
        result["final_capital"] = round(
            initial_capital + sum(t.get("pnl_net", 0) for t in all_trades), 4
        )
        return result

    # =========================================================================
    # CLASSEMENT
    # =========================================================================

    def _rank_results(
        self,
        results: List[OptimizationResult],
    ) -> List[OptimizationResult]:
        """Tri par score primaire décroissant. Invalides en fin de liste."""
        min_trades = self._opt_cfg.get("min_trades_required", _MIN_TRADES_DEFAULT)

        def sort_key(r: OptimizationResult) -> Tuple[int, float, float, float]:
            if not r.is_valid or r.metrics.get("total_trades", 0) < min_trades:
                return (1, -999.0, -999.0, 999.0)
            return (
                0,
                -r.primary_score,
                -r.metrics.get("profit_factor", 0.0),
                r.metrics.get("max_drawdown_pct", 999.0),
            )

        return sorted(results, key=sort_key)

    # =========================================================================
    # SAUVEGARDE
    # =========================================================================

    def _save_results(self, results: List[OptimizationResult]) -> None:
        """Sauvegarde JSON + rapport texte + meilleure config."""
        output_cfg  = self._opt_cfg.get("output", {})
        output_path = self._root / output_cfg.get("results_path", "results/optimization/")
        ensure_directory(output_path)

        ts    = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        valid = [r for r in results if r.is_valid]

        # JSON complet
        with open(output_path / f"optimization_results_{ts}.json", "w", encoding="utf-8") as f:
            json.dump(
                {
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "config_path":  str(self._config_path),
                    "total_runs":   len(results),
                    "valid_runs":   len(valid),
                    "results":      [r.to_dict() for r in results],
                },
                f, indent=2, ensure_ascii=False, default=str
            )

        # Rapport texte
        with open(output_path / f"optimization_report_{ts}.txt", "w", encoding="utf-8") as f:
            self._write_text_report(f, results, valid, ts)

        # Meilleure config (config.json + external configs patchés)
        if valid:
            best = valid[0]
            with open(output_path / f"best_config_{ts}.json", "w", encoding="utf-8") as f:
                json.dump(
                    self._build_best_config_dict(best), f, indent=2, ensure_ascii=False
                )
            print(f"\n  {_G}✅ Résultats sauvegardés :{_E} {output_path}")
            print(f"  {_G}→ best_config_{ts}.json{_E}")

    def _write_text_report(
        self,
        f: Any,
        all_results: List[OptimizationResult],
        valid: List[OptimizationResult],
        ts: str,
    ) -> None:
        SEP  = "=" * 72
        DASH = "─" * 72
        top_n = self._opt_cfg.get("output", {}).get("save_top_n_configs", 10)

        f.write(f"{SEP}\nBULLET-1 — RAPPORT D'OPTIMISATION\n{SEP}\n")
        f.write(
            f"Généré le  : {ts[:4]}-{ts[4:6]}-{ts[6:8]} {ts[9:11]}:{ts[11:13]} UTC\n"
            f"Config     : {self._config_path}\n"
            f"Période    : {self._base_config['backtesting']['start_date']} → "
            f"{self._base_config['backtesting']['end_date']}\n"
            f"Total runs : {len(all_results)} | Valides : {len(valid)}\n"
            f"{SEP}\n\n"
        )

        if not valid:
            f.write("Aucun run valide.\n")
            return

        f.write(f"TOP {min(top_n, len(valid))} CONFIGURATIONS\n{DASH}\n\n")
        for rank, r in enumerate(valid[:top_n], start=1):
            m    = r.metrics
            note = m.get("sharpe_note")
            sharpe_str = f"N/A ({note})" if note else f"{m.get('sharpe_ratio', 0):.3f}"

            f.write(
                f"#{rank:02d}  {r.params.configuration_name:<12} | "
                f"SL={r.params.stop_loss_offset_pct}%  "
                f"lev={r.params.leverage}×  "
                f"trail={r.params.trailing_stop_type}  "
                f"qual={r.params.min_quality_score}\n"
            )
            # Overrides additionnels
            if r.params.config_json_overrides:
                ov = {k: v for k, v in r.params.config_json_overrides.items()
                      if "stages" not in k}   # stages trop verbeux
                if ov:
                    f.write(f"    Config overrides : {ov}\n")
            if r.params.external_overrides:
                f.write(f"    External overrides : {r.params.external_overrides}\n")

            f.write(
                f"    Score ({r.primary_metric_name:<14}) : {r.primary_score:+.4f}\n"
                f"    Trades={m.get('total_trades', 0):>4} | "
                f"WR={m.get('win_rate', 0):.1f}% | "
                f"PF={m.get('profit_factor', 0):.3f} | "
                f"Sharpe={sharpe_str}\n"
                f"    PnL={m.get('total_pnl', 0):>+8.2f} USDT | "
                f"DD={m.get('max_drawdown_pct', 0):.2f}% | "
                f"Sessions={r.n_sessions}\n\n"
            )

        # Résumé par configuration (meilleur score de chaque)
        f.write(f"\n{SEP}\nRÉSUMÉ PAR CONFIGURATION (meilleur score de chaque)\n{DASH}\n")
        best_by_cfg: Dict[str, OptimizationResult] = {}
        for r in valid:
            cfg = r.params.configuration_name
            if cfg not in best_by_cfg or r.primary_score > best_by_cfg[cfg].primary_score:
                best_by_cfg[cfg] = r

        for cfg_name in sorted(_CONFIG_MAP.keys()):
            r = best_by_cfg.get(cfg_name)
            if r:
                f.write(
                    f"  {cfg_name:<12} | score={r.primary_score:+.4f} | "
                    f"trades={r.metrics.get('total_trades', 0):>4} | "
                    f"PnL={r.metrics.get('total_pnl', 0):>+7.2f} | "
                    f"WR={r.metrics.get('win_rate', 0):.1f}%\n"
                )
            else:
                f.write(f"  {cfg_name:<12} | aucun résultat valide\n")

    def _build_best_config_dict(self, best: OptimizationResult) -> Dict[str, Any]:
        """Config.json complet avec les meilleurs paramètres trouvés."""
        cfg = copy.deepcopy(self._base_config)
        direction, short_op, long_op = _CONFIG_MAP[best.params.configuration_name]

        cfg["strategy"]["configuration_name"]                              = best.params.configuration_name
        cfg["strategy"]["entry_logic"]["logic_direction"]                  = direction
        cfg["strategy"]["entry_logic"]["for_short_case_comparison_operator"] = short_op
        cfg["strategy"]["entry_logic"]["for_long_case_comparison_operator"]  = long_op
        cfg["strategy"]["trailing_stop"]["type"]                           = best.params.trailing_stop_type
        cfg["strategy"]["min_quality_score"]                               = best.params.min_quality_score
        cfg["risk_management"]["stop_loss_offset_pct"]                     = best.params.stop_loss_offset_pct
        cfg["position"]["leverage"]                                        = best.params.leverage
        cfg["position"]["collateral_percentage"]                           = best.params.collateral_percentage
        cfg["session_management"]["reset_capital_between_sessions"]        = False

        # Appliquer les overrides config.json
        for path, value in best.params.config_json_overrides.items():
            _set_nested(cfg, path, value)

        cfg["_optimization_metadata"] = {
            "optimized_at":    datetime.now(timezone.utc).isoformat(),
            "primary_score":   round(best.primary_score, 6),
            "primary_metric":  best.primary_metric_name,
            "n_sessions":      best.n_sessions,
            "total_trades":    best.metrics.get("total_trades", 0),
            "external_overrides": best.params.external_overrides,
        }
        return cfg

    # =========================================================================
    # AFFICHAGE CONSOLE
    # =========================================================================

    def _display_plan(self, grid: List[OptimizationParams]) -> None:
        """Affiche le résumé de la grille avant lancement."""
        cfg_names  = sorted(set(p.configuration_name for p in grid))
        sl_vals    = sorted(set(p.stop_loss_offset_pct for p in grid))
        lev_vals   = sorted(set(p.leverage for p in grid))
        trail_vals = sorted(set(p.trailing_stop_type for p in grid))
        qual_vals  = sorted(set(p.min_quality_score for p in grid))
        period_s   = self._base_config["backtesting"]["start_date"]
        period_e   = self._base_config["backtesting"]["end_date"]

        print(f"\n{_B}{_C}{'═'*60}{_E}")
        print(f"{_B}{_C}  BULLET-1 — Optimizer v2.0.0{_E}")
        print(f"{_B}{_C}{'═'*60}{_E}")
        print(f"\n  Période    : {period_s} → {period_e}")
        print(f"  Timeframe  : {self._base_config['general']['timeframe']}")
        print(f"  Capital    : {self._base_config['capital']['initial_capital_backtest']} USDT")
        print(f"\n{_B}  Paramètres de la grille :{_E}")
        print(f"    Configurations  : {cfg_names}")
        print(f"    SL offset %     : {sl_vals}")
        print(f"    Levier          : {lev_vals}×")
        print(f"    Trailing stop   : {trail_vals}")
        print(f"    Quality score   : {qual_vals}")

        # Afficher aussi les overrides s'il y en a
        all_cj_keys = set()
        all_ext_keys: Dict[str, set] = {}
        for p in grid:
            all_cj_keys.update(p.config_json_overrides.keys())
            for fp, ov in p.external_overrides.items():
                all_ext_keys.setdefault(fp, set()).update(ov.keys())

        if all_cj_keys:
            print(f"\n  Params config.json additionnels :")
            for k in sorted(all_cj_keys):
                vals = sorted(set(
                    str(p.config_json_overrides.get(k, "—")) for p in grid
                ))
                if len(vals) <= 6:
                    print(f"    {k} : {vals}")

        if all_ext_keys:
            print(f"\n  Params configs externes :")
            for fp, keys in sorted(all_ext_keys.items()):
                print(f"    [{fp}]")
                for k in sorted(keys):
                    vals = sorted(set(
                        str(p.external_overrides.get(fp, {}).get(k, "—")) for p in grid
                    ))
                    if len(vals) <= 6:
                        print(f"      {k} : {vals}")

        print(f"\n  {_B}Total combinaisons : {_Y}{len(grid)}{_E}")

    def _display_run_header(self, idx: int, total: int, params: OptimizationParams) -> None:
        bar_len = 25
        filled  = int(bar_len * (idx - 1) / max(total, 1))
        bar     = "█" * filled + "░" * (bar_len - filled)
        pct     = (idx - 1) / max(total, 1) * 100
        print(
            f"\r  [{bar}] {pct:>3.0f}% "
            f"Run {idx:>4}/{total} : "
            f"{_C}{params.configuration_name}{_E} "
            f"SL={params.stop_loss_offset_pct}% "
            f"lev={params.leverage}×"
            f"{'  ext' if params.external_overrides else ''}    ",
            end="", flush=True
        )

    def _display_run_result(
        self,
        result: OptimizationResult,
        idx: int,
        total: int,
        t_global_start: float,
    ) -> None:
        elapsed_total = time.monotonic() - t_global_start
        eta_s = (elapsed_total / idx) * (total - idx) if idx > 0 else 0

        if result.error:
            print(
                f"\r  Run {idx:>4}/{total} | "
                f"{_R}ERREUR{_E} {result.params.configuration_name:<12} : "
                f"{result.error[:55]}"
            )
            return

        m      = result.metrics
        score  = result.primary_score
        color  = _G if score > 1.0 else (_Y if score > 0 else _R)
        print(
            f"\r  Run {idx:>4}/{total} | "
            f"{result.params.configuration_name:<12} "
            f"trades={m.get('total_trades', 0):>4} "
            f"WR={m.get('win_rate', 0):>5.1f}% "
            f"PF={m.get('profit_factor', 0):>5.2f} "
            f"PnL={m.get('total_pnl', 0):>+7.2f} "
            f"score={color}{score:>+6.3f}{_E} "
            f"({result.elapsed_s:.0f}s|ETA:{eta_s/60:.0f}min)"
        )

    def _display_final_report(self, results: List[OptimizationResult]) -> None:
        valid = [r for r in results if r.is_valid]
        if not valid:
            print(f"\n{_R}Aucun run valide.{_E}")
            return

        print(f"\n{_B}{_C}{'═'*60}{_E}")
        print(f"{_B}{_C}  TOP {min(5, len(valid))} RÉSULTATS{_E}")
        print(f"{_B}{_C}{'═'*60}{_E}\n")

        for rank, r in enumerate(valid[:5], start=1):
            m     = r.metrics
            color = _G if rank == 1 else (_C if rank <= 3 else _E)
            print(
                f"  {color}#{rank}{_E} {r.params.configuration_name:<12} | "
                f"score={color}{r.primary_score:+.4f}{_E} | "
                f"trades={m.get('total_trades', 0)} | "
                f"WR={m.get('win_rate', 0):.1f}% | "
                f"PF={m.get('profit_factor', 0):.3f} | "
                f"PnL={m.get('total_pnl', 0):>+.2f}"
            )

        best = valid[0]
        print(
            f"\n  {_DIM}Best: SL={best.params.stop_loss_offset_pct}% | "
            f"lev={best.params.leverage}× | "
            f"trail={best.params.trailing_stop_type} | "
            f"qual={best.params.min_quality_score}"
        )
        if best.params.external_overrides:
            print(f"       Ext: {best.params.external_overrides}{_E}")
        else:
            print(_E, end="")


# FIN DU MODULE
