#!/usr/bin/env python3
"""
BULLET-1 — Lancement de l'optimisation (Phase 2)
==================================================

Usage :
    python optimize.py                    # Phase 2A (défaut)
    python optimize.py --phase 2a         # Phase 2A : stratégie de base
    python optimize.py --phase 2b         # Phase 2B : indicateurs externes
    python optimize.py --phase 2c         # Phase 2C : toggles avancés
    python optimize.py --phase all        # Toutes les phases séquentiellement
    python optimize.py --config config/my_config.json

Workflow recommandé :
    1. python optimize.py --phase 2a   → trouve la meilleure config stratégie
    2. Copier best_config_XXX.json → config/config.json
    3. python optimize.py --phase 2b   → optimise les indicateurs
    4. Copier best_config_XXX.json → config/config.json
    5. python optimize.py --phase 2c   → optimise les toggles avancés

Version: 2.0.0
Date: 2026-04-24
Author: FuegoDev
"""

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_B = "\033[1m"
_C = "\033[96m"
_G = "\033[92m"
_Y = "\033[93m"
_R = "\033[91m"
_E = "\033[0m"

PHASE_LABELS = {
    "2a": "Paramètres stratégie (config.json)",
    "2b": "Paramètres indicateurs (configs externes)",
    "2c": "Toggles avancés + trailing ATR",
    "all": "Toutes les phases (2A → 2B → 2C)",
}


def _run_phase(phase_key: str, cfg_path: str) -> int:
    """Lance l'optimisation pour une phase donnée."""
    from src.backtesting.optimizer import Optimizer
    import json

    # Vérifier que la phase existe dans config.json
    with open(cfg_path) as f:
        cfg = json.load(f)

    phase_map = {
        "2a": "phase_2a",
        "2b": "phase_2b",
        "2c": "phase_2c",
    }
    phase_cfg_key = phase_map.get(phase_key)
    if not phase_cfg_key:
        print(f"{_R}❌ Phase inconnue : {phase_key}{_E}")
        return 1

    phase_section = cfg.get("optimization", {}).get(phase_cfg_key)
    if not phase_section:
        print(f"{_R}❌ Section 'optimization.{phase_cfg_key}' absente dans config.json{_E}")
        return 1

    # Pour cette phase, on copie les paramètres dans la clé standard
    # que l'Optimizer lit via 'parameters_to_optimize'
    import copy, tempfile, os, json as _json

    # Construire une config modifiée pour cette phase
    cfg_modified = copy.deepcopy(cfg)
    cfg_modified["optimization"]["parameters_to_optimize"] = (
        phase_section["parameters_to_optimize"]
    )

    # Phase 2B et 2C : fixer la configuration stratégie à celle trouvée
    # en Phase 2A (config.json actuel). On n'itère pas sur les 8 configs —
    # seuls les paramètres indicateurs/toggles varient.
    if phase_key in ("2b", "2c"):
        best_config_name = cfg.get("strategy", {}).get("configuration_name")
        if best_config_name:
            cfg_modified["optimization"]["strategy_configurations"] = [best_config_name]

    # Écrire config temporaire pour cette phase
    tmp = _ROOT / "config" / f"_optim_phase_{phase_key}_tmp.json"
    with open(tmp, "w") as f:
        _json.dump(cfg_modified, f, indent=2)

    try:
        opt = Optimizer(config_path=str(tmp))
        results = opt.run()
        valid = [r for r in results if r.is_valid]
        print()
        print(f"{_G}{'═'*60}{_E}")
        print(
            f"{_G}✅ Phase {phase_key.upper()} terminée — "
            f"{len(results)} runs | {len(valid)} valides{_E}"
        )
        print(f"{_G}{'═'*60}{_E}")
        return 0

    finally:
        if tmp.exists():
            tmp.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="BULLET-1 — Optimiseur grid search (Phase 2)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Phases disponibles :\n"
            "  2a  — Paramètres stratégie base (8 configs × SL × levier × trailing × quality)\n"
            "  2b  — Indicateurs externes (ATR, Trend, UncertaintyCandle)\n"
            "  2c  — Toggles avancés (breakout, trend_filter, volume, trailing ATR)\n"
            "  all — Exécute 2A puis 2B puis 2C séquentiellement\n\n"
            "Workflow recommandé :\n"
            "  1. optimize.py --phase 2a   → copier best_config → config.json\n"
            "  2. optimize.py --phase 2b   → copier best_config → config.json\n"
            "  3. optimize.py --phase 2c   → configuration finale optimisée\n"
        )
    )
    parser.add_argument(
        "--phase",
        choices=["2a", "2b", "2c", "all"],
        default="2a",
        help="Phase d'optimisation (défaut : 2a)"
    )
    parser.add_argument(
        "--config",
        default="config/config.json",
        metavar="PATH",
        help="Chemin vers config.json (défaut : config/config.json)"
    )
    args = parser.parse_args()

    cfg_path = str(_ROOT / args.config)

    if not Path(cfg_path).exists():
        print(f"{_R}❌ Config introuvable : {cfg_path}{_E}")
        return 1

    print(f"\n{_B}{_C}🎯 BULLET-1 — Optimizer v2.0.0{_E}")
    print(f"   Phase  : {_Y}{args.phase.upper()}{_E} — {PHASE_LABELS.get(args.phase, '')}")
    print(f"   Config : {cfg_path}")

    phases_to_run = (
        ["2a", "2b", "2c"] if args.phase == "all"
        else [args.phase]
    )

    try:
        for phase in phases_to_run:
            if args.phase == "all":
                print(f"\n{_B}{'─'*60}")
                print(f"  Lancement Phase {phase.upper()} — {PHASE_LABELS[phase]}")
                print(f"{'─'*60}{_E}\n")

            rc = _run_phase(phase, cfg_path)
            if rc != 0:
                return rc

            if args.phase == "all" and phase != "2c":
                print(
                    f"\n{_Y}💡 Phase {phase.upper()} terminée. "
                    f"Appliquez best_config_*.json avant la phase suivante "
                    f"si vous souhaitez enchaîner avec la meilleure configuration.{_E}\n"
                )
                cont = input(
                    f"Continuer avec la Phase {phases_to_run[phases_to_run.index(phase)+1].upper()} "
                    f"(config.json actuelle) ? [O/n] : "
                ).strip().lower()
                if cont not in ("", "o", "oui", "y", "yes"):
                    print(f"{_Y}Optimisation arrêtée après Phase {phase.upper()}.{_E}")
                    return 0

        return 0

    except FileNotFoundError as exc:
        print(f"\n{_R}❌ Fichier introuvable : {exc}{_E}")
        print(f"{_Y}   Vérifiez la DB : python data/db_status.py{_E}")
        return 1

    except ValueError as exc:
        print(f"\n{_R}❌ Erreur configuration : {exc}{_E}")
        return 1

    except KeyboardInterrupt:
        print(f"\n{_Y}⚠️  Optimisation interrompue.{_E}")
        return 1

    except Exception as exc:
        print(f"\n{_R}❌ Erreur inattendue : {exc}{_E}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
