#!/usr/bin/env python3
"""
BULLET-1 — Point d'entrée principal
======================================

Usage :
    python main.py backtest           # Backtest avec config par défaut
    python main.py backtest --config config/my_config.json
    python main.py paper              # Phase 3 — non disponible
    python main.py live               # Phase 4 — non disponible

Version: 1.1.0
Date: 2026-04-21
Author: FuegoDev
"""

import argparse
import sys
import time
from pathlib import Path

# ── Project root resolution ────────────────────────────────────────────────────
_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Couleurs ANSI
_B  = "\033[1m"
_C  = "\033[96m"
_G  = "\033[92m"
_Y  = "\033[93m"
_R  = "\033[91m"
_E  = "\033[0m"


def _banner():
    print(f"""
{_B}{_C}╔══════════════════════════════════════════════════════════╗
║             🎯 BULLET-1 Trading Bot v2.2                  ║
║        Algorithmic Futures Trading System                 ║
╚══════════════════════════════════════════════════════════╝{_E}
""")


def _run_backtest(config_path: str) -> int:
    """
    Lance le pipeline de backtesting complet via Engine.

    Returns:
        int: 0 si succès, 1 si erreur.
    """
    print(f"{_B}📊 Mode : BACKTEST{_E}")
    print(f"   Config : {config_path}")
    print()

    try:
        from src.backtesting.engine import Engine

        engine = Engine(config_path=config_path)
        results = engine.run()

        n_sessions = len(results)
        print()
        print(f"{_G}{'═' * 60}{_E}")
        print(f"{_G}✅ Backtest terminé — {n_sessions} session(s) traitée(s){_E}")
        print(f"{_G}{'═' * 60}{_E}")
        return 0

    except FileNotFoundError as exc:
        print(f"\n{_R}❌ Fichier introuvable : {exc}{_E}")
        print(f"{_Y}   Vérifiez que config_path existe et que la DB est alimentée.{_E}")
        print(f"{_Y}   Lancez d'abord : python data/migrate_csv_to_db.py{_E}")
        return 1

    except ValueError as exc:
        print(f"\n{_R}❌ Erreur de configuration ou de données : {exc}{_E}")
        return 1

    except KeyboardInterrupt:
        print(f"\n{_Y}⚠️  Backtest interrompu par l'utilisateur.{_E}")
        return 1

    except Exception as exc:
        print(f"\n{_R}❌ Erreur inattendue : {exc}{_E}")
        import traceback
        traceback.print_exc()
        return 1


def _run_optimize(config_path: str) -> int:
    """Lance l'optimisation — redirige vers optimize.py pour la gestion des phases."""
    print(f"{_B}🔧 Mode : OPTIMIZER (Phase 2){_E}")
    print(f"   Pour sélectionner une phase : python optimize.py --phase [2a|2b|2c|all]")
    print()
    try:
        # Phase 2A par défaut depuis main.py
        import copy, json as _json
        from pathlib import Path
        from src.backtesting.optimizer import Optimizer

        with open(config_path) as f:
            cfg = _json.load(f)

        phase_section = cfg.get("optimization", {}).get("phase_2a")
        if not phase_section:
            print(f"{_R}❌ Section 'optimization.phase_2a' absente dans config.json{_E}")
            return 1

        cfg_mod = copy.deepcopy(cfg)
        cfg_mod["optimization"]["parameters_to_optimize"] = (
            phase_section["parameters_to_optimize"]
        )
        tmp = Path(config_path).parent / "_optim_main_tmp.json"
        with open(tmp, "w") as f:
            _json.dump(cfg_mod, f, indent=2)

        try:
            opt     = Optimizer(config_path=str(tmp))
            results = opt.run()
            valid   = [r for r in results if r.is_valid]
            print()
            print(f"{_G}{'='*60}{_E}")
            print(f"{_G}✅ Phase 2A terminée — {len(results)} runs | {len(valid)} valides{_E}")
            print(f"{_G}{'='*60}{_E}")
            return 0
        finally:
            if tmp.exists(): tmp.unlink()

    except FileNotFoundError as exc:
        print(f"\n{_R}❌ Fichier introuvable : {exc}{_E}")
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


def _run_paper(_config_path: str) -> int:
    """Paper trading — Phase 3, non disponible."""
    print(f"{_B}📄 Mode : PAPER TRADING{_E}")
    print()
    print(f"{_Y}⏳ Phase 3 non encore disponible.{_E}")
    print()
    print("  Le paper trading (simulation en temps réel sur prix Binance)")
    print("  sera implémenté dans la Phase 3 du projet.")
    print()
    print("  Modules requis (non implémentés) :")
    print(f"    {_C}src/exchange/base_client.py{_E}         — Interface exchange abstraite")
    print(f"    {_C}src/exchange/binance_client.py{_E}      — Client Binance Futures")
    print(f"    {_C}src/exchange/paper_trading.py{_E}       — Moteur paper trading")
    print(f"    {_C}src/utils/performance_monitor.py{_E}    — Monitoring temps réel")
    print(f"    {_C}src/trading/trading_bot.py{_E}          — TradingBot principal")
    print()
    print(f"  {_DIM}Roadmap : Phase 2 (Optimizer) → Phase 3 (Paper) → Phase 4 (Live){_E}")
    return 0


def _run_live(_config_path: str) -> int:
    """Live trading — Phase 4, non disponible."""
    print(f"{_B}⚡ Mode : LIVE TRADING{_E}")
    print()
    print(f"{_Y}⏳ Phase 4 non encore disponible.{_E}")
    print()
    print("  Le live trading avec capital réel sera implémenté en Phase 4,")
    print("  après validation complète du paper trading en Phase 3.")
    print()
    print(f"  {_R}⚠️  Ne jamais utiliser en live sans validation préalable des Phases 1-3.{_E}")
    return 0


# Petite constante pour l'affichage "Phase non dispo"
_DIM = "\033[2m"


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="BULLET-1 — Bot de trading algorithmique Futures"
    )
    parser.add_argument(
        "mode",
        choices=["backtest", "optimize", "paper", "live"],
        help="Mode de fonctionnement"
    )
    parser.add_argument(
        "--config",
        default="config/config.json",
        metavar="PATH",
        help="Chemin vers config.json (défaut : config/config.json)"
    )

    args = parser.parse_args()
    _banner()

    config_path = str(_PROJECT_ROOT / args.config)

    if args.mode == "backtest":
        return _run_backtest(config_path)
    elif args.mode == "optimize":
        return _run_optimize(config_path)
    elif args.mode == "paper":
        return _run_paper(config_path)
    elif args.mode == "live":
        return _run_live(config_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
