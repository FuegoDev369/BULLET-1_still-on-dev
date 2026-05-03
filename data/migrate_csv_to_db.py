#!/usr/bin/env python3
"""
BULLET-1 - Migration CSV → SQLite
====================================

Script one-time : importe les fichiers CSV historiques existants
(data/historical/**/*.csv) dans la base SQLite bullet1_market_data.db.

Usage :
    python data/migrate_csv_to_db.py

Les fichiers CSV ne sont PAS supprimés automatiquement. Une fois la migration
vérifiée, vous pouvez archiver ou supprimer le dossier data/historical/.

Convention de nommage CSV attendue :
    data/historical/{SYMBOL}/{TIMEFRAME}.csv
    Exemple : data/historical/BTC-USDT/15min.csv

Mapping timeframe (nom fichier → code BULLET-1) :
    1min.csv  → 1m   |  5min.csv  → 5m   |  15min.csv → 15m
    30min.csv → 30m  |  1h.csv    → 1h   |  4h.csv    → 4h
    1d.csv    → 1d

Version: 1.0.0
Date: 2026-04-21
Author: FuegoDev
"""

import sys
from pathlib import Path
from datetime import datetime

import pandas as pd

# ── Project root resolution ────────────────────────────────────────────────────
_SCRIPT_DIR   = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.data.db_manager import MarketDatabase

# =============================================================================
# CONSTANTES
# =============================================================================

_DB_PATH = _PROJECT_ROOT / "data" / "bullet1_market_data.db"
_CSV_ROOT = _PROJECT_ROOT / "data" / "historical"
_DEFAULT_EXCHANGE = "binance"

# Mapping nom_fichier → code timeframe BULLET-1
_FILENAME_TO_TF: dict = {
    "1min.csv":  "1m",
    "5min.csv":  "5m",
    "15min.csv": "15m",
    "30min.csv": "30m",
    "1h.csv":    "1h",
    "2h.csv":    "2h",
    "4h.csv":    "4h",
    "6h.csv":    "6h",
    "8h.csv":    "8h",
    "12h.csv":   "12h",
    "1d.csv":    "1d",
    "1w.csv":    "1w",
}

# Couleurs ANSI
_G  = "\033[92m"
_Y  = "\033[93m"
_R  = "\033[91m"
_C  = "\033[96m"
_B  = "\033[1m"
_E  = "\033[0m"


# =============================================================================
# UTILITAIRES
# =============================================================================

def _ok(msg: str):  print(f"{_G}✅ {msg}{_E}")
def _warn(msg: str): print(f"{_Y}⚠️  {msg}{_E}")
def _err(msg: str):  print(f"{_R}❌ {msg}{_E}")
def _info(msg: str): print(f"{_C}ℹ️  {msg}{_E}")


def _discover_csv_files() -> list[dict]:
    """
    Scanne data/historical/ et retourne la liste des CSV reconnus.

    Chaque entrée : { csv_path, exchange, symbol, timeframe }.
    """
    if not _CSV_ROOT.exists():
        return []

    found = []
    for symbol_dir in sorted(_CSV_ROOT.iterdir()):
        if not symbol_dir.is_dir():
            continue
        symbol = symbol_dir.name  # ex. 'BTC-USDT'
        for csv_file in sorted(symbol_dir.glob("*.csv")):
            tf = _FILENAME_TO_TF.get(csv_file.name)
            if tf is None:
                _warn(f"Fichier CSV ignoré (nom non reconnu) : {csv_file.name}")
                continue
            found.append({
                "csv_path":  csv_file,
                "exchange":  _DEFAULT_EXCHANGE,
                "symbol":    symbol,
                "timeframe": tf,
            })
    return found


def _migrate_one(db: MarketDatabase, entry: dict) -> dict:
    """
    Migre un seul fichier CSV vers la DB.

    Returns:
        dict : { success, csv_path, rows_read, rows_inserted, error }
    """
    csv_path  = entry["csv_path"]
    exchange  = entry["exchange"]
    symbol    = entry["symbol"]
    timeframe = entry["timeframe"]

    result = {
        "success":       False,
        "csv_path":      str(csv_path),
        "rows_read":     0,
        "rows_inserted": 0,
        "error":         None,
    }

    try:
        # Lecture CSV
        df = pd.read_csv(csv_path, parse_dates=False)

        required = {"timestamp", "open", "high", "low", "close", "volume"}
        missing  = required - set(df.columns)
        if missing:
            raise ValueError(f"Colonnes manquantes dans le CSV : {missing}")

        # Nettoyage minimal (cohérence types, NaN)
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = pd.to_numeric(df[col], errors="coerce").astype("float64")

        df = df.dropna(subset=["timestamp", "open", "high", "low", "close", "volume"])
        df = df.drop_duplicates(subset=["timestamp"])
        df = df.sort_values("timestamp").reset_index(drop=True)

        result["rows_read"] = len(df)

        if df.empty:
            raise ValueError("CSV vide ou toutes les lignes invalides après nettoyage.")

        # Insertion DB
        n = db.insert_candles(exchange, symbol, timeframe, df, source="csv_import")
        result["rows_inserted"] = n
        result["success"] = True

    except Exception as exc:
        result["error"] = str(exc)

    return result


# =============================================================================
# POINT D'ENTRÉE
# =============================================================================

def main() -> None:
    print(f"\n{_B}{_C}{'='*60}")
    print("  BULLET-1 — Migration CSV → SQLite")
    print(f"{'='*60}{_E}\n")

    # Découverte des CSV
    csv_files = _discover_csv_files()

    if not csv_files:
        _warn(f"Aucun fichier CSV trouvé dans : {_CSV_ROOT}")
        _info("Rien à migrer.")
        return

    _info(f"Répertoire source : {_CSV_ROOT}")
    _info(f"Base cible        : {_DB_PATH}")
    print()
    print(f"{_B}Fichiers détectés :{_E}")
    for e in csv_files:
        size_kb = round(e["csv_path"].stat().st_size / 1024, 1)
        print(
            f"  {e['exchange']}/{e['symbol']}/{e['timeframe']} — "
            f"{e['csv_path'].name} ({size_kb} KB)"
        )

    print()
    confirm = input("Lancer la migration ? [O/n] : ").strip().lower()
    if confirm not in ("", "o", "oui", "y", "yes"):
        _warn("Migration annulée.")
        return

    print()

    # Migration
    with MarketDatabase(_DB_PATH) as db:
        results = []
        for entry in csv_files:
            print(
                f"  ⏳ {entry['exchange']}/{entry['symbol']}/{entry['timeframe']} "
                f"({entry['csv_path'].name})...",
                end=" ",
                flush=True
            )
            r = _migrate_one(db, entry)
            results.append(r)
            if r["success"]:
                print(
                    f"{_G}OK{_E} — {r['rows_read']:,} lues, "
                    f"{r['rows_inserted']:,} insérées"
                )
            else:
                print(f"{_R}ERREUR{_E} — {r['error']}")

    # Résumé
    print(f"\n{_B}{'─'*60}")
    print("  Résumé de la migration")
    print(f"{'─'*60}{_E}")

    ok_count    = sum(1 for r in results if r["success"])
    err_count   = len(results) - ok_count
    total_read  = sum(r["rows_read"] for r in results if r["success"])
    total_ins   = sum(r["rows_inserted"] for r in results if r["success"])

    print(f"  Fichiers traités  : {len(results)}")
    _ok(f"Succès            : {ok_count}")
    if err_count:
        _err(f"Erreurs           : {err_count}")
    print(f"  Lignes lues       : {total_read:,}")
    print(f"  Lignes insérées   : {total_ins:,}")
    if total_read > 0:
        dup = total_read - total_ins
        if dup > 0:
            _warn(f"Doublons ignorés  : {dup:,} (déjà présents en base)")

    db_size = round(_DB_PATH.stat().st_size / (1024 * 1024), 2) if _DB_PATH.exists() else 0
    print(f"  Taille DB finale  : {db_size} MB")
    print()

    if ok_count > 0:
        _ok("Migration terminée.")
        _info(
            "Les fichiers CSV originaux sont conservés dans data/historical/. "
            "Vous pouvez les archiver une fois la migration vérifiée."
        )
        _info(
            "Pour voir l'état de la base en clair : python data/db_status.py"
        )
    else:
        _err("Aucun fichier migré avec succès. Vérifiez les erreurs ci-dessus.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n{_Y}⚠️  Interruption utilisateur.{_E}")
    except Exception as exc:
        _err(f"Erreur fatale : {exc}")
        sys.exit(1)
