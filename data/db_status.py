#!/usr/bin/env python3
"""
BULLET-1 - État de la base de données
========================================

Affiche de façon lisible le contenu et l'état de bullet1_market_data.db :
  - Datasets disponibles avec dates explicites (début, fin, durée)
  - Nombre de bougies par dataset
  - Taille de la base
  - Dernière mise à jour

Usage :
    python data/db_status.py

Aucun argument requis — lit la DB du projet automatiquement.

Version: 1.0.0
Date: 2026-04-21
Author: FuegoDev
"""

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# ── Chemin racine ──────────────────────────────────────────────────────────────
_SCRIPT_DIR   = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
_DB_PATH      = _PROJECT_ROOT / "data" / "bullet1_market_data.db"

# Couleurs ANSI
_B  = "\033[1m"
_C  = "\033[96m"
_G  = "\033[92m"
_Y  = "\033[93m"
_R  = "\033[91m"
_E  = "\033[0m"
_DIM = "\033[2m"


def _ts_to_str(ts_ms: int) -> str:
    """Convertit un timestamp Unix ms en chaîne lisible UTC."""
    return datetime.utcfromtimestamp(ts_ms / 1000).strftime("%Y-%m-%d %H:%M")


def main() -> None:
    if not _DB_PATH.exists():
        print(f"{_R}❌ Base de données introuvable : {_DB_PATH}{_E}")
        print(f"{_Y}   Lancez d'abord : python data/migrate_csv_to_db.py{_E}")
        print(f"{_Y}   Ou : python data/download_data_v3.0.py{_E}")
        sys.exit(1)

    db_size_mb = round(_DB_PATH.stat().st_size / (1024 * 1024), 2)

    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row

    # ── En-tête ────────────────────────────────────────────────────────────────
    print()
    print(f"{_B}{_C}{'═' * 66}{_E}")
    print(f"{_B}{_C}  📊 BULLET-1 — État de la base de données{_E}")
    print(f"{_B}{_C}{'═' * 66}{_E}")
    print(f"  Fichier : {_DB_PATH}")
    print(f"  Taille  : {db_size_mb} MB")
    print()

    # ── Datasets disponibles ───────────────────────────────────────────────────
    rows = conn.execute(
        "SELECT exchange, symbol, timeframe, first_ts, last_ts, "
        "candle_count, source, updated_at "
        "FROM datasets ORDER BY exchange, symbol, timeframe"
    ).fetchall()

    if not rows:
        print(f"  {_Y}⚠️  Aucun dataset en base.{_E}")
        print(f"  {_DIM}Lancez : python data/migrate_csv_to_db.py{_E}")
        print(f"  {_DIM}Ou    : python data/download_data_v3.0.py{_E}")
        conn.close()
        return

    print(f"{_B}  Datasets disponibles ({len(rows)}) :{_E}")
    print()

    # En-tête du tableau
    hdr = (
        f"  {'Exchange':<10} {'Symbol':<10} {'TF':<5}  "
        f"{'Début':<17} {'Fin':<17} {'Durée':>7}  "
        f"{'Bougies':>9}  {'Source'}"
    )
    print(f"{_B}{_DIM}{hdr}{_E}")
    print(f"  {'─' * 92}")

    total_candles = 0
    for r in rows:
        first_str = _ts_to_str(r['first_ts'])
        last_str  = _ts_to_str(r['last_ts'])
        days      = int((r['last_ts'] - r['first_ts']) / 1000 / 86400)
        source    = r['source'] or '?'
        updated   = _ts_to_str(r['updated_at'])
        total_candles += r['candle_count']

        print(
            f"  {_G}{r['exchange']:<10}{_E} "
            f"{r['symbol']:<10} "
            f"{_C}{r['timeframe']:<5}{_E}  "
            f"{first_str:<17} {last_str:<17} "
            f"{_Y}{days:>6}j{_E}  "
            f"{r['candle_count']:>9,}  "
            f"{_DIM}{source}{_E}"
        )

    print(f"  {'─' * 92}")
    print(f"  {_B}{'Total':>49} {total_candles:>9,} bougies{_E}")
    print()

    # ── Mise à jour incrémentale ───────────────────────────────────────────────
    print(f"{_B}  Pour mettre à jour votre base de données :{_E}")
    print()
    for r in rows:
        last_str = _ts_to_str(r['last_ts'])
        next_str = datetime.utcfromtimestamp(
            r['last_ts'] / 1000 + 60  # +1 minute
        ).strftime("%Y-%m-%d %H:%M")
        print(
            f"  {r['exchange']}/{r['symbol']}/{r['timeframe']} — "
            f"dernier : {_G}{last_str} UTC{_E} "
            f"→ reprendre depuis {_Y}{next_str} UTC{_E}"
        )

    print()

    # ── Conseil mise à jour ────────────────────────────────────────────────────
    print(f"  {_DIM}→ python data/download_data_v3.0.py  (Menu : Mise à jour){_E}")
    print()

    # ── Vérification intégrité rapide ─────────────────────────────────────────
    total_in_ohlcv = conn.execute("SELECT COUNT(*) FROM ohlcv").fetchone()[0]
    if total_in_ohlcv != total_candles:
        print(
            f"  {_Y}⚠️  Écart détecté : {total_in_ohlcv:,} bougies en table ohlcv "
            f"vs {total_candles:,} en registre datasets.{_E}"
        )
        print(f"  {_Y}   Re-lancez migrate_csv_to_db.py pour recalculer les métadonnées.{_E}")
        print()

    conn.close()

    # ── Astuce DB Browser ──────────────────────────────────────────────────────
    print(f"{_B}  Astuce :{_E} pour lire la base directement dans DB Browser for SQLite,")
    print(f"  exécutez la requête suivante dans l'onglet SQL :")
    print(f"  {_C}SELECT * FROM datasets_readable;{_E}")
    print()


if __name__ == "__main__":
    main()
