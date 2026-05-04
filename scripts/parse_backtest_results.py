#!/usr/bin/env python3
"""
BULLET-1 — Parseur de résultats backtest pour notifications CI
==============================================================

Lit les répertoires de sessions générés par AnalyticsEngine et
extrait les métriques clés pour les notifications et le résumé GitHub.

Structure attendue :
    results/backtests/sessions/
        session_001_<uuid>/
            *.json   ← métriques
            *.html
            *.md
            *.csv
        session_002_<uuid>/
            ...

Usage :
    python3 scripts/parse_backtest_results.py
    python3 scripts/parse_backtest_results.py --sessions-dir results/backtests/sessions/
    python3 scripts/parse_backtest_results.py --notify-final --phase backtest
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


def set_output(key: str, value: str) -> None:
    """Écrit dans GITHUB_OUTPUT si dispo, sinon stdout."""
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a") as f:
            f.write(f"{key}={value}\n")
    print(f"  [output] {key} = {value}")


def fmt(value: Any, precision: int = 3, suffix: str = "") -> str:
    """Formate une valeur numérique ou retourne N/A."""
    if value is None or value == "N/A":
        return "N/A"
    try:
        return f"{round(float(value), precision)}{suffix}"
    except (ValueError, TypeError):
        return str(value)


def find_session_dirs(sessions_dir: Path) -> list[Path]:
    """Trouve et trie tous les dossiers de sessions."""
    if not sessions_dir.exists():
        return []
    dirs = sorted([
        d for d in sessions_dir.iterdir()
        if d.is_dir() and d.name.startswith("session_")
    ])
    return dirs


def load_session_json(session_dir: Path) -> dict | None:
    """Charge le fichier JSON de métriques d'une session."""
    # Chercher *.json dans le dossier de session
    json_files = list(session_dir.glob("*.json"))
    if not json_files:
        return None

    # Prioriser les fichiers avec "metrics" ou "report" dans le nom
    priority = [f for f in json_files if any(k in f.name.lower() for k in ("metric", "report", "result", "summary"))]
    target = priority[0] if priority else json_files[0]

    try:
        with open(target) as f:
            return json.load(f)
    except Exception as e:
        print(f"  ⚠️  Impossible de lire {target}: {e}")
        return None


def extract_session_metrics(data: dict) -> dict:
    """Extrait les métriques clés d'un JSON de session."""
    # Chercher dans plusieurs niveaux de nesting possible
    sources = [
        data,
        data.get("metrics", {}),
        data.get("results", {}),
        data.get("performance", {}),
        data.get("summary", {}),
        data.get("session_summary", {}),
    ]
    sources = [s for s in sources if isinstance(s, dict)]

    def get(*keys, default="N/A", prec=3):
        for src in sources:
            for key in keys:
                val = src.get(key)
                if val is not None and str(val) not in ("N/A", "", "null"):
                    try:
                        return round(float(val), prec)
                    except (ValueError, TypeError):
                        return str(val)
        return default

    def get_str(*keys, default="N/A"):
        for src in sources:
            for key in keys:
                val = src.get(key)
                if val is not None and str(val) not in ("", "null"):
                    return str(val)
        return default

    return {
        "session_id":      get_str("session_id", "id"),
        "start_date":      get_str("start_date", "period_start", "date_start"),
        "end_date":        get_str("end_date", "period_end", "date_end"),
        "total_trades":    get("total_trades", "trades_count", "nb_trades", prec=0),
        "win_rate":        get("win_rate", "winrate", "win_pct", prec=1),
        "net_pnl":         get("net_pnl", "pnl", "total_pnl", "net_profit", prec=4),
        "net_pnl_pct":     get("net_pnl_pct", "pnl_pct", "return_pct", "total_return_pct", prec=2),
        "sharpe_ratio":    get("sharpe_ratio", "sharpe", prec=3),
        "profit_factor":   get("profit_factor", "pf", prec=2),
        "max_drawdown":    get("max_drawdown_pct", "max_drawdown", "drawdown_pct", prec=2),
        "avg_trade_pnl":   get("avg_trade_pnl", "avg_pnl", "mean_pnl", prec=4),
        "capital_end":     get("capital_end", "final_capital", "ending_capital", prec=2),
    }


def compute_global_metrics(sessions: list[dict]) -> dict:
    """Calcule les métriques globales agrégées sur toutes les sessions."""
    valid = [s for s in sessions if s.get("total_trades", "N/A") != "N/A"]

    def safe_list(key):
        return [float(s[key]) for s in valid if s.get(key, "N/A") != "N/A"]

    def safe_avg(key, prec=3):
        vals = safe_list(key)
        return round(sum(vals) / len(vals), prec) if vals else "N/A"

    def safe_sum(key, prec=4):
        vals = safe_list(key)
        return round(sum(vals), prec) if vals else "N/A"

    trades_list = safe_list("total_trades")
    pnl_list    = safe_list("net_pnl_pct")
    win_list    = safe_list("win_rate")

    return {
        "nb_sessions":      len(sessions),
        "nb_valid":         len(valid),
        "total_trades":     int(sum(trades_list)) if trades_list else "N/A",
        "avg_win_rate":     round(sum(win_list) / len(win_list), 1) if win_list else "N/A",
        "total_pnl_pct":    round(sum(pnl_list), 2) if pnl_list else "N/A",
        "avg_pnl_pct":      round(sum(pnl_list) / len(pnl_list), 2) if pnl_list else "N/A",
        "best_session_pnl": round(max(pnl_list), 2) if pnl_list else "N/A",
        "worst_session_pnl":round(min(pnl_list), 2) if pnl_list else "N/A",
        "avg_sharpe":       safe_avg("sharpe_ratio", 3),
        "avg_pf":           safe_avg("profit_factor", 2),
        "avg_drawdown":     safe_avg("max_drawdown", 2),
    }


def build_github_summary(sessions: list[dict], global_metrics: dict, config_info: dict) -> str:
    """Construit le markdown pour le GitHub Step Summary."""
    lines = []
    lines.append("## 📊 BULLET-1 — Résultats Backtest\n")

    # Infos config
    lines.append("### ⚙️ Configuration\n")
    lines.append("| Paramètre | Valeur |")
    lines.append("|-----------|--------|")
    for k, v in config_info.items():
        lines.append(f"| {k} | {v} |")
    lines.append("")

    # Métriques globales
    g = global_metrics
    lines.append("### 🌐 Vue d'ensemble — Toutes sessions\n")
    lines.append("| Métrique | Valeur |")
    lines.append("|----------|--------|")
    lines.append(f"| **Sessions traitées** | {g['nb_valid']} / {g['nb_sessions']} |")
    lines.append(f"| **Total trades** | {g['total_trades']} |")
    lines.append(f"| **PnL total cumulé** | {fmt(g['total_pnl_pct'], 2)}% |")
    lines.append(f"| **PnL moyen / session** | {fmt(g['avg_pnl_pct'], 2)}% |")
    lines.append(f"| **Meilleure session** | {fmt(g['best_session_pnl'], 2)}% |")
    lines.append(f"| **Pire session** | {fmt(g['worst_session_pnl'], 2)}% |")
    lines.append(f"| **Win rate moyen** | {fmt(g['avg_win_rate'], 1)}% |")
    lines.append(f"| **Sharpe moyen** | {fmt(g['avg_sharpe'], 3)} |")
    lines.append(f"| **Profit Factor moyen** | {fmt(g['avg_pf'], 2)} |")
    lines.append(f"| **Drawdown moyen** | {fmt(g['avg_drawdown'], 2)}% |")
    lines.append("")

    # Tableau par session
    if sessions:
        lines.append("### 📅 Détail par session\n")
        lines.append("| Session | Période | Trades | Win Rate | PnL % | Sharpe | PF | Drawdown |")
        lines.append("|---------|---------|--------|----------|-------|--------|----|----------|")
        for i, s in enumerate(sessions, 1):
            period = f"{s.get('start_date','?')} → {s.get('end_date','?')}"
            lines.append(
                f"| **#{i}** "
                f"| {period} "
                f"| {s.get('total_trades', 'N/A')} "
                f"| {fmt(s.get('win_rate'), 1)}% "
                f"| {fmt(s.get('net_pnl_pct'), 2)}% "
                f"| {fmt(s.get('sharpe_ratio'), 3)} "
                f"| {fmt(s.get('profit_factor'), 2)} "
                f"| {fmt(s.get('max_drawdown'), 2)}% |"
            )
        lines.append("")

    lines.append("### 💾 Récupérer les résultats")
    lines.append("```bash")
    lines.append("git pull   # Rapports dans results/backtests/sessions/")
    lines.append("```")
    lines.append("> Rapports disponibles : HTML, Markdown, JSON, CSV par session")

    return "\n".join(lines)


def send_notification(notify_type: str, args_list: list[str]) -> None:
    notify_script = Path(__file__).parent / "notify.sh"
    if not notify_script.exists():
        print("  ⚠️  scripts/notify.sh introuvable")
        return
    try:
        result = subprocess.run(
            ["bash", str(notify_script), notify_type] + [str(a) for a in args_list],
            capture_output=True, text=True, timeout=15
        )
        if result.stdout:
            print(result.stdout.strip())
    except Exception as e:
        print(f"  ⚠️  Erreur notification : {e}")


def main() -> int:
    parser = argparse.ArgumentParser(description="BULLET-1 — Parseur résultats backtest CI")
    parser.add_argument("--sessions-dir", default="results/backtests/sessions/")
    parser.add_argument("--config",       default="config/config.json")
    parser.add_argument("--notify-final", action="store_true")
    parser.add_argument("--write-summary", default=None, help="Chemin vers GITHUB_STEP_SUMMARY")
    args = parser.parse_args()

    sessions_dir = Path(args.sessions_dir)
    print(f"\n📊 Parsing backtest sessions dans : {sessions_dir}")

    # Infos de config pour le summary
    config_info = {}
    try:
        with open(args.config) as f:
            cfg = json.load(f)
        config_info = {
            "Paire":          cfg.get("general", {}).get("trading_pair", "N/A"),
            "Timeframe":      cfg.get("general", {}).get("timeframe", "N/A"),
            "Période":        f"{cfg.get('backtesting',{}).get('start_date','?')} → {cfg.get('backtesting',{}).get('end_date','?')}",
            "Durée session":  f"{cfg.get('session_management',{}).get('trades_period_days','?')} jours",
        }
    except Exception:
        pass

    # Trouver les dossiers de sessions
    session_dirs = find_session_dirs(sessions_dir)
    if not session_dirs:
        print(f"  ⚠️  Aucun dossier session_* trouvé dans {sessions_dir}")
        set_output("has_results", "false")
        set_output("nb_sessions", "0")
        return 0

    print(f"  {len(session_dirs)} session(s) trouvée(s)")

    # Parser chaque session
    all_metrics = []
    for i, sdir in enumerate(session_dirs, 1):
        data = load_session_json(sdir)
        if data:
            metrics = extract_session_metrics(data)
            all_metrics.append(metrics)
            print(f"  Session {i}: trades={metrics.get('total_trades','N/A')} "
                  f"pnl={fmt(metrics.get('net_pnl_pct'), 2)}% "
                  f"wr={fmt(metrics.get('win_rate'), 1)}%")
        else:
            print(f"  Session {i}: aucun JSON trouvé dans {sdir.name}")
            all_metrics.append({})

    # Métriques globales
    global_metrics = compute_global_metrics(all_metrics)
    g = global_metrics

    print(f"\n🌐 Global : {g['nb_valid']} sessions | "
          f"{g['total_trades']} trades | "
          f"PnL total={fmt(g['total_pnl_pct'], 2)}% | "
          f"WR moy={fmt(g['avg_win_rate'], 1)}%")

    # Exports GITHUB_OUTPUT
    set_output("has_results",     "true")
    set_output("nb_sessions",     str(g['nb_sessions']))
    set_output("total_trades",    str(g['total_trades']))
    set_output("total_pnl_pct",   str(g['total_pnl_pct']))
    set_output("avg_win_rate",    str(g['avg_win_rate']))
    set_output("avg_sharpe",      str(g['avg_sharpe']))
    set_output("avg_pf",          str(g['avg_pf']))
    set_output("avg_drawdown",    str(g['avg_drawdown']))
    set_output("best_pnl",        str(g['best_session_pnl']))
    set_output("worst_pnl",       str(g['worst_session_pnl']))

    # GitHub Step Summary
    summary_path = args.write_summary or os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        summary_md = build_github_summary(all_metrics, global_metrics, config_info)
        with open(summary_path, "a") as f:
            f.write(summary_md)
        print(f"\n✅ GitHub Summary écrit dans : {summary_path}")

    # Notification finale
    if args.notify_final:
        send_notification("final_summary", [
            "backtest",
            f"{g['nb_valid']} session(s)",
            fmt(g['avg_sharpe'], 3),
            fmt(g['avg_pf'], 2),
            fmt(g['avg_win_rate'], 1),
            fmt(g['avg_drawdown'], 2),
            str(g['total_trades']),
        ])

    return 0


if __name__ == "__main__":
    sys.exit(main())
