#!/usr/bin/env python3
"""
BULLET-1 — Parseur de résultats pour notifications CI
======================================================

Lit les fichiers JSON générés par l'optimizer et extrait
les métriques clés pour les notifications Telegram/Discord.

Usage (depuis le workflow GitHub Actions) :
  python3 scripts/parse_results.py --results-dir results/optimization/ --phase 2a
  python3 scripts/parse_results.py --best-only --results-dir results/optimization/

Sorties (variables d'environnement pour le workflow) :
  Écrit dans $GITHUB_OUTPUT si disponible, sinon affiche sur stdout.
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def set_output(key: str, value: str) -> None:
    """Écrit une variable dans GITHUB_OUTPUT ou sur stdout."""
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a") as f:
            f.write(f"{key}={value}\n")
    print(f"  {key} = {value}")


def find_best_config(results_dir: Path) -> dict | None:
    """Trouve la meilleure config JSON dans le dossier de résultats."""
    candidates = list(results_dir.glob("best_config*.json"))
    candidates += list(results_dir.glob("**/best_config*.json"))

    if not candidates:
        return None

    # Trier par date de modification (le plus récent en premier)
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    best_file = candidates[0]

    try:
        with open(best_file) as f:
            data = json.load(f)
        data["_source_file"] = str(best_file.name)
        return data
    except Exception as e:
        print(f"  ⚠️  Impossible de lire {best_file}: {e}")
        return None


def extract_metrics(data: dict) -> dict:
    """Extrait les métriques clés d'un fichier de résultats."""
    # Chercher les métriques à différents niveaux du JSON
    metrics_sources = [
        data,
        data.get("metrics", {}),
        data.get("results", {}),
        data.get("performance", {}),
    ]

    def get_metric(*keys, default="N/A", precision=3):
        for source in metrics_sources:
            if not isinstance(source, dict):
                continue
            for key in keys:
                val = source.get(key)
                if val is not None and val != "N/A":
                    try:
                        return str(round(float(val), precision))
                    except (ValueError, TypeError):
                        return str(val)
        return default

    return {
        "sharpe_ratio":    get_metric("sharpe_ratio", "sharpe", precision=3),
        "profit_factor":   get_metric("profit_factor", "pf", precision=2),
        "win_rate":        get_metric("win_rate", "winrate", precision=1),
        "max_drawdown":    get_metric("max_drawdown_pct", "max_drawdown", "drawdown_pct", precision=2),
        "total_trades":    get_metric("total_trades", "trades", precision=0),
        "net_pnl_pct":     get_metric("net_pnl_pct", "total_return_pct", "pnl_pct", precision=2),
        "config_name":     data.get("config_name", data.get("configuration_name",
                           data.get("_source_file", "unknown"))),
        "source_file":     data.get("_source_file", "unknown"),
    }


def count_results(results_dir: Path) -> dict:
    """Compte les fichiers de résultats."""
    all_jsons = list(results_dir.glob("**/*.json"))
    best_configs = [f for f in all_jsons if "best_config" in f.name]
    run_results = [f for f in all_jsons if "best_config" not in f.name and "summary" not in f.name.lower()]

    return {
        "total_files": len(all_jsons),
        "best_configs": len(best_configs),
        "run_results": len(run_results),
    }


def send_shell_notification(notify_type: str, args: list[str]) -> None:
    """Appelle scripts/notify.sh avec les arguments donnés."""
    notify_script = Path(__file__).parent / "notify.sh"
    if not notify_script.exists():
        print(f"  ⚠️  scripts/notify.sh introuvable — notification ignorée")
        return

    cmd = ["bash", str(notify_script), notify_type] + [str(a) for a in args]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if result.stdout:
            print(result.stdout.strip())
        if result.returncode != 0 and result.stderr:
            print(f"  ⚠️  notify.sh stderr: {result.stderr.strip()[:200]}")
    except subprocess.TimeoutExpired:
        print("  ⚠️  Timeout lors de l'envoi de la notification")
    except Exception as e:
        print(f"  ⚠️  Erreur notification: {e}")


def main() -> int:
    parser = argparse.ArgumentParser(description="BULLET-1 — Parseur résultats CI")
    parser.add_argument("--results-dir", default="results/optimization/",
                        help="Dossier des résultats d'optimisation")
    parser.add_argument("--phase", default="?",
                        help="Phase en cours (2a/2b/2c/all)")
    parser.add_argument("--mode", choices=["best", "summary", "count"],
                        default="best",
                        help="Mode de sortie")
    parser.add_argument("--notify-final", action="store_true",
                        help="Envoyer la notification finale via notify.sh")
    args = parser.parse_args()

    results_dir = Path(args.results_dir)
    if not results_dir.exists():
        print(f"  ⚠️  Dossier {results_dir} inexistant — pas de résultats à parser")
        set_output("best_config_name", "N/A")
        set_output("best_sharpe", "N/A")
        set_output("best_pf", "N/A")
        set_output("best_wr", "N/A")
        set_output("best_dd", "N/A")
        set_output("best_trades", "N/A")
        return 0

    print(f"\n📊 Parsing des résultats dans : {results_dir}")
    counts = count_results(results_dir)
    print(f"   Fichiers JSON trouvés : {counts['total_files']}")
    print(f"   Best configs          : {counts['best_configs']}")

    best = find_best_config(results_dir)
    if not best:
        print("  ⚠️  Aucune best_config*.json trouvée")
        set_output("best_config_name", "N/A")
        set_output("has_results", "false")
        return 0

    metrics = extract_metrics(best)
    print(f"\n🏆 Meilleure configuration : {metrics['config_name']}")
    print(f"   Sharpe Ratio   : {metrics['sharpe_ratio']}")
    print(f"   Profit Factor  : {metrics['profit_factor']}")
    print(f"   Win Rate       : {metrics['win_rate']}%")
    print(f"   Max Drawdown   : {metrics['max_drawdown']}%")
    print(f"   Total Trades   : {metrics['total_trades']}")
    print(f"   Net PnL        : {metrics['net_pnl_pct']}%")

    # Exporter vers GITHUB_OUTPUT
    set_output("best_config_name",  metrics["config_name"])
    set_output("best_sharpe",       metrics["sharpe_ratio"])
    set_output("best_pf",           metrics["profit_factor"])
    set_output("best_wr",           metrics["win_rate"])
    set_output("best_dd",           metrics["max_drawdown"])
    set_output("best_trades",       metrics["total_trades"])
    set_output("has_results",       "true")

    # Notification finale si demandée
    if args.notify_final:
        print("\n📲 Envoi de la notification finale...")
        send_shell_notification("final_summary", [
            args.phase,
            metrics["config_name"],
            metrics["sharpe_ratio"],
            metrics["profit_factor"],
            metrics["win_rate"],
            metrics["max_drawdown"],
            metrics["total_trades"],
        ])

    return 0


if __name__ == "__main__":
    sys.exit(main())
