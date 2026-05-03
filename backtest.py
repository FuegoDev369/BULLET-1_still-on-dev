#!/usr/bin/env python3
"""
BULLET-1 — Script de lancement backtest dédié
================================================

Alternative directe à `python main.py backtest`.
Utile pour les automatisations et les scripts shell.

Usage :
    python backtest.py
    python backtest.py --config config/config.json
    python backtest.py --config config/my_custom_config.json

Version: 1.1.0
Date: 2026-04-21
Author: FuegoDev
"""

import argparse
import sys
from pathlib import Path

# ── Project root resolution ────────────────────────────────────────────────────
_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

_B = "\033[1m"
_C = "\033[96m"
_G = "\033[92m"
_Y = "\033[93m"
_R = "\033[91m"
_E = "\033[0m"


def run_backtest(config_path: str = "config/config.json") -> int:
    """
    Lance le backtest BULLET-1.

    Args:
        config_path: Chemin vers config.json (relatif ou absolu).

    Returns:
        int: 0 si succès, 1 si erreur.
    """
    # Résoudre le chemin config
    cfg = Path(config_path)
    if not cfg.is_absolute():
        cfg = _PROJECT_ROOT / cfg

    print(f"\n{_B}{_C}🎯 BULLET-1 — Backtest{_E}")
    print(f"   Config : {cfg}")
    print()

    if not cfg.exists():
        print(f"{_R}❌ Fichier de configuration introuvable : {cfg}{_E}")
        return 1

    try:
        from src.backtesting.engine import Engine

        engine = Engine(config_path=str(cfg))
        results = engine.run()

        n_sessions = len(results)
        print()
        print(f"{_G}{'═' * 60}{_E}")
        print(f"{_G}✅ Backtest terminé — {n_sessions} session(s){_E}")
        print(f"{_G}{'═' * 60}{_E}")
        return 0

    except FileNotFoundError as exc:
        print(f"\n{_R}❌ Fichier introuvable : {exc}{_E}")
        print(f"{_Y}   Vérifiez la DB : python data/db_status.py{_E}")
        return 1

    except ValueError as exc:
        print(f"\n{_R}❌ Erreur config/données : {exc}{_E}")
        return 1

    except KeyboardInterrupt:
        print(f"\n{_Y}⚠️  Interrompu.{_E}")
        return 1

    except Exception as exc:
        print(f"\n{_R}❌ Erreur : {exc}{_E}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="BULLET-1 — Lancement backtest direct"
    )
    parser.add_argument(
        "--config",
        default="config/config.json",
        metavar="PATH",
        help="Chemin vers config.json"
    )
    args = parser.parse_args()
    sys.exit(run_backtest(args.config))
