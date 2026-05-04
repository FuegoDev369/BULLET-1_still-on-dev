#!/usr/bin/env python3
"""
BULLET-1 — Validateur de période de backtest (pré-lancement CI)
================================================================

Vérifie que (end_date - start_date) est un multiple exact de
trades_period_days avant de lancer le backtest.

Si la date de fin est invalide, propose automatiquement la date
corrigée la plus proche et échoue avec exit code 1.

Usage :
    python3 scripts/validate_backtest_period.py
    python3 scripts/validate_backtest_period.py --start 2024-01-01 --end 2024-04-15
    python3 scripts/validate_backtest_period.py --fix  # Corrige automatiquement
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


def set_output(key: str, value: str) -> None:
    """Écrit dans GITHUB_OUTPUT si disponible, sinon stdout."""
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a") as f:
            f.write(f"{key}={value}\n")
    print(f"  [output] {key} = {value}")


def load_config(config_path: str) -> dict:
    with open(config_path) as f:
        return json.load(f)


def parse_date(date_str: str) -> datetime:
    return datetime.strptime(date_str.strip(), "%Y-%m-%d").replace(tzinfo=timezone.utc)


def validate_and_fix(
    start_str: str,
    end_str: str,
    period_days: int,
    auto_fix: bool = False,
    config_path: str | None = None,
) -> int:
    """
    Valide la cohérence de la période.

    Returns:
        0 si valide (ou fixé avec succès)
        1 si invalide et non fixé
    """
    print(f"\n{'═' * 55}")
    print(f"  BULLET-1 — Validateur période backtest")
    print(f"{'═' * 55}")
    print(f"  Début          : {start_str}")
    print(f"  Fin            : {end_str}")
    print(f"  Session (jours): {period_days}")

    try:
        start_dt = parse_date(start_str)
        end_dt   = parse_date(end_str)
    except ValueError as e:
        print(f"\n❌ Format de date invalide (attendu YYYY-MM-DD) : {e}")
        return 1

    if end_dt <= start_dt:
        print(f"\n❌ La date de fin ({end_str}) doit être après le début ({start_str})")
        return 1

    total_days = (end_dt - start_dt).days
    remainder  = total_days % period_days

    print(f"  Durée totale   : {total_days} jour(s)")
    print(f"  Sessions       : {total_days // period_days} × {period_days} j" +
          (f" + {remainder} jour(s) restant(s) ← ❌" if remainder else " ✅"))

    if remainder == 0:
        print(f"\n✅ Période cohérente — {total_days // period_days} session(s) complète(s)")
        set_output("valid", "true")
        set_output("start_date", start_str)
        set_output("end_date", end_str)
        set_output("nb_sessions", str(total_days // period_days))
        set_output("total_days", str(total_days))
        return 0

    # ── Période incohérente → calculer les suggestions ──────────────────────
    nb_complete_floor = total_days // period_days
    nb_complete_ceil  = nb_complete_floor + 1

    suggested_end_floor = (start_dt + timedelta(days=nb_complete_floor * period_days)).strftime("%Y-%m-%d")
    suggested_end_ceil  = (start_dt + timedelta(days=nb_complete_ceil  * period_days)).strftime("%Y-%m-%d")

    print(f"\n❌ Période INCOHÉRENTE : {total_days} % {period_days} = {remainder} (≠ 0)")
    print(f"\n   Solutions possibles :")
    print(f"   ▸ Réduire  : end_date = '{suggested_end_floor}' → {nb_complete_floor} session(s)")
    print(f"   ▸ Augmenter: end_date = '{suggested_end_ceil}'  → {nb_complete_ceil} session(s)")

    if auto_fix and config_path:
        # Choisir la suggestion la plus proche de la date demandée
        delta_floor = abs((end_dt - parse_date(suggested_end_floor)).days)
        delta_ceil  = abs((end_dt - parse_date(suggested_end_ceil)).days)
        corrected   = suggested_end_floor if delta_floor <= delta_ceil else suggested_end_ceil
        nb_sessions = nb_complete_floor if corrected == suggested_end_floor else nb_complete_ceil
        corrected_days = nb_sessions * period_days

        print(f"\n🔧 Auto-correction → end_date = '{corrected}' ({nb_sessions} sessions)")

        # Modifier config.json
        with open(config_path) as f:
            cfg = json.load(f)
        cfg["backtesting"]["end_date"] = corrected
        with open(config_path, "w") as f:
            json.dump(cfg, f, indent=2)

        print(f"✅ config.json mis à jour : end_date = '{corrected}'")

        set_output("valid", "true")
        set_output("start_date", start_str)
        set_output("end_date", corrected)
        set_output("nb_sessions", str(nb_sessions))
        set_output("total_days", str(corrected_days))
        set_output("was_corrected", "true")
        set_output("original_end", end_str)
        return 0

    elif auto_fix:
        # Auto-fix sans config_path : juste exporter la valeur corrigée
        delta_floor = abs((end_dt - parse_date(suggested_end_floor)).days)
        delta_ceil  = abs((end_dt - parse_date(suggested_end_ceil)).days)
        corrected   = suggested_end_floor if delta_floor <= delta_ceil else suggested_end_ceil
        nb_sessions = nb_complete_floor if corrected == suggested_end_floor else nb_complete_ceil
        corrected_days = nb_sessions * period_days

        set_output("valid", "true")
        set_output("start_date", start_str)
        set_output("end_date", corrected)
        set_output("nb_sessions", str(nb_sessions))
        set_output("total_days", str(corrected_days))
        set_output("was_corrected", "true")
        set_output("original_end", end_str)
        print(f"\n🔧 Date corrigée exportée : '{corrected}' ({nb_sessions} sessions)")
        return 0

    else:
        set_output("valid", "false")
        set_output("suggested_end_floor", suggested_end_floor)
        set_output("suggested_end_ceil", suggested_end_ceil)
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="BULLET-1 — Validateur période backtest")
    parser.add_argument("--config",  default="config/config.json")
    parser.add_argument("--start",   default=None, help="Date début (YYYY-MM-DD) — override config")
    parser.add_argument("--end",     default=None, help="Date fin   (YYYY-MM-DD) — override config")
    parser.add_argument("--period",  default=None, type=int, help="trades_period_days — override config")
    parser.add_argument("--fix",     action="store_true", help="Auto-corriger la date si invalide")
    args = parser.parse_args()

    # Charger config
    try:
        cfg = load_config(args.config)
    except FileNotFoundError:
        print(f"❌ config.json introuvable : {args.config}")
        return 1

    # Résolution des valeurs (paramètres CLI > config.json)
    start_str   = args.start  or cfg["backtesting"]["start_date"]
    end_str     = args.end    or cfg["backtesting"]["end_date"]
    period_days = args.period or int(cfg["session_management"]["trades_period_days"])

    config_path_for_fix = args.config if args.fix else None

    return validate_and_fix(
        start_str=start_str,
        end_str=end_str,
        period_days=period_days,
        auto_fix=args.fix,
        config_path=config_path_for_fix,
    )


if __name__ == "__main__":
    sys.exit(main())
