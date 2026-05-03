#!/usr/bin/env python3
"""
BULLET-1 - Data Downloader v3.0
==================================

Téléchargement des données historiques OHLCV depuis les exchanges crypto.
Sauvegarde directe dans la base SQLite BULLET-1 (bullet1_market_data.db).

Remplace download_data_multi_exchange_v2.3.py (sauvegarde CSV).

Changements v3.0 :
    - Sauvegarde SQLite (INSERT OR IGNORE) au lieu de CSV
    - Mode incrémental : détecte le dernier timestamp en base, ne télécharge
      que les nouvelles bougies (évite les re-téléchargements complets)
    - Suppression des formats JSON/parquet (non utilisés par BULLET-1)
    - Suppression du répertoire de config ~/.bullet1_downloader (non nécessaire)

Usage :
    python data/download_data_v3.0.py

Exchanges supportés : binance, mexc, bybit, kraken
Timeframes          : 1m, 5m, 15m, 30m, 1h, 4h, 1d

Version: 3.0.0
Date: 2026-04-21
Author: FuegoDev
Dépendances: ccxt, pandas, src/data/db_manager.py
"""

import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Tuple

import pandas as pd

# ── Project root resolution ────────────────────────────────────────────────────
_SCRIPT_DIR   = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

try:
    import ccxt
except ImportError:
    print("❌ ccxt non installé. Lancez : pip install ccxt")
    sys.exit(1)

from src.data.db_manager import MarketDatabase

# =============================================================================
# CONFIGURATION
# =============================================================================

VERSION    = "3.0.0"
_DB_PATH   = _PROJECT_ROOT / "data" / "bullet1_market_data.db"
_FAV_FILE  = _PROJECT_ROOT / "data" / ".downloader_favorites.json"

EXCHANGES  = ["binance", "mexc", "bybit", "kraken"]
TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]

# Durée max de la fenêtre glissante par requête ccxt (bougies)
_FETCH_LIMIT = 1000

# Délai entre requêtes (secondes) — respecte les rate limits exchanges
_REQUEST_DELAY = 0.25


# =============================================================================
# COULEURS ANSI
# =============================================================================

class C:
    HEADER = "\033[95m"
    BLUE   = "\033[94m"
    CYAN   = "\033[96m"
    GREEN  = "\033[92m"
    YELLOW = "\033[93m"
    RED    = "\033[91m"
    END    = "\033[0m"
    BOLD   = "\033[1m"


def _ok(m):   print(f"{C.GREEN}✅ {m}{C.END}")
def _err(m):  print(f"{C.RED}❌ {m}{C.END}")
def _warn(m): print(f"{C.YELLOW}⚠️  {m}{C.END}")
def _info(m): print(f"{C.CYAN}ℹ️  {m}{C.END}")
def _section(t): print(f"\n{C.BOLD}{C.BLUE}{'='*60}\n  {t}\n{'='*60}{C.END}\n")


# =============================================================================
# INTERFACE UTILISATEUR
# =============================================================================

def _banner():
    print(f"""
{C.CYAN}{C.BOLD}╔══════════════════════════════════════════════════════════╗
║  🚀 BULLET-1 DATA DOWNLOADER v{VERSION}                     ║
║  📊 Multi-Exchange Historical Data → SQLite              ║
╚══════════════════════════════════════════════════════════╝{C.END}
""")


def _menu(title: str, options: list, allow_back: bool = True) -> int:
    """Affiche un menu numéroté et retourne le choix (1-based). 0 = retour."""
    print(f"\n{C.BOLD}{title}{C.END}")
    print("─" * 40)
    for i, opt in enumerate(options, 1):
        print(f"  {C.CYAN}{i}.{C.END} {opt}")
    if allow_back:
        print(f"  {C.CYAN}0.{C.END} ← Retour")
    print()

    while True:
        try:
            raw = input("Choix : ").strip()
            choice = int(raw)
            max_val = len(options)
            if allow_back and choice == 0:
                return 0
            if 1 <= choice <= max_val:
                return choice
            _err(f"Valeur entre 1 et {max_val}.")
        except ValueError:
            _err("Entrez un nombre.")


def _ask(prompt: str, default: Optional[str] = None) -> str:
    full = f"{prompt} [{default}]: " if default else f"{prompt}: "
    val  = input(full).strip()
    return val if val else (default or "")


def _ask_date(prompt: str, default: Optional[datetime] = None) -> datetime:
    fmt = "%Y-%m-%d"
    def_str = default.strftime(fmt) if default else None
    while True:
        raw = _ask(f"{prompt} (YYYY-MM-DD)", def_str)
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            _err("Format invalide. Exemple : 2024-01-01")


def _progress(current: int, total: int, prefix: str = "", width: int = 35):
    pct    = 100 * current / max(total, 1)
    filled = int(width * current // max(total, 1))
    bar    = "█" * filled + "░" * (width - filled)
    print(f"\r{prefix} |{bar}| {pct:.0f}% ({current}/{total})", end="", flush=True)
    if current >= total:
        print()


# =============================================================================
# EXCHANGE
# =============================================================================

def _get_exchange(name: str):
    """Initialise un exchange ccxt, retourne None en cas d'échec."""
    try:
        ex = getattr(ccxt, name)({"enableRateLimit": True})
        ex.load_markets()
        return ex
    except Exception as exc:
        _err(f"Impossible d'initialiser {name} : {exc}")
        return None


def _find_best_exchange(pair: str, timeframe: str) -> Tuple[Optional[object], str]:
    """
    Teste chaque exchange dans l'ordre et retourne le premier qui supporte
    la paire + timeframe. Retourne (None, '') si aucun ne convient.
    """
    _info("Recherche du meilleur exchange...")
    for name in EXCHANGES:
        try:
            ex = getattr(ccxt, name)({"enableRateLimit": True})
            ex.load_markets()
            if pair in ex.markets and timeframe in ex.timeframes:
                _ok(f"{name} supporte {pair} @ {timeframe}")
                return ex, name
        except Exception:
            pass
    _err(f"Aucun exchange supporté pour {pair} @ {timeframe}")
    return None, ""


# =============================================================================
# TÉLÉCHARGEMENT
# =============================================================================

def _get_last_ts_in_db(
    db: MarketDatabase,
    exchange: str,
    symbol: str,
    timeframe: str
) -> Optional[int]:
    """
    Retourne le dernier timestamp (Unix ms) disponible en base pour ce dataset,
    ou None s'il n'existe pas encore.
    """
    info = db.get_dataset_info(exchange, symbol.replace("/", "-"), timeframe)
    return info["last_ts"] if info else None


def _download(
    db: MarketDatabase,
    exchange_obj,
    exchange_name: str,
    pair: str,
    timeframe: str,
    start_dt: datetime,
    end_dt: datetime,
    incremental: bool = False
) -> bool:
    """
    Télécharge les bougies OHLCV et les insère dans la DB.

    Args:
        incremental: Si True, démarre depuis le dernier timestamp en base.

    Returns:
        bool: True si au moins une bougie insérée.
    """
    symbol_norm = pair.replace("/", "-")

    # Mode incrémental : trouver le dernier ts en base
    if incremental:
        last_ts = _get_last_ts_in_db(db, exchange_name, pair, timeframe)
        if last_ts:
            last_dt = datetime.utcfromtimestamp(last_ts / 1000)
            if last_dt >= end_dt:
                _ok(f"Données déjà à jour (dernier : {last_dt.strftime('%Y-%m-%d %H:%M')})")
                return True
            start_dt = last_dt + timedelta(seconds=1)
            _info(f"Mode incrémental — reprise depuis {start_dt.strftime('%Y-%m-%d %H:%M')}")
        else:
            _info("Aucune donnée existante — téléchargement complet.")

    since = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)

    # Estimation du nombre de bougies
    tf_minutes = {
        "1m": 1, "5m": 5, "15m": 15, "30m": 30,
        "1h": 60, "4h": 240, "1d": 1440
    }
    total_min = (end_dt - start_dt).total_seconds() / 60
    expected  = int(total_min / tf_minutes.get(timeframe, 15))
    _info(f"Bougies attendues : ~{expected:,}")
    print()

    all_data   = []
    req_count  = 0
    max_req    = 500  # garde-fou contre les boucles infinies

    while since < end_ms and req_count < max_req:
        try:
            ohlcv = exchange_obj.fetch_ohlcv(
                pair, timeframe, since=since, limit=_FETCH_LIMIT
            )
        except Exception as exc:
            _err(f"Erreur requête {req_count + 1} : {exc}")
            break

        if not ohlcv:
            break

        all_data.extend(ohlcv)
        req_count += 1
        since = ohlcv[-1][0] + 1

        last_date = datetime.utcfromtimestamp(ohlcv[-1][0] / 1000).strftime("%Y-%m-%d %H:%M")
        _progress(len(all_data), expected, f"  Requête {req_count}")
        _ = last_date  # utilisé dans le message de la barre ci-dessus

        time.sleep(_REQUEST_DELAY)

    if not all_data:
        _err("Aucune donnée récupérée.")
        return False

    print()  # nouvelle ligne après barre de progression

    # Conversion & nettoyage
    df = pd.DataFrame(
        all_data,
        columns=["timestamp", "open", "high", "low", "close", "volume"]
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df[df["timestamp"] <= pd.Timestamp(end_dt, tz="UTC")]
    df = df.drop_duplicates(subset=["timestamp"]).sort_values("timestamp")
    df = df.reset_index(drop=True)

    _info(f"{len(df):,} bougies après nettoyage")

    # Insertion DB
    n = db.insert_candles(
        exchange_name,
        symbol_norm,
        timeframe,
        df,
        source="download"
    )
    _ok(f"{n:,} bougies insérées en base ({exchange_name}/{symbol_norm}/{timeframe})")

    if n == 0 and len(df) > 0:
        _warn("Toutes les bougies étaient déjà en base (doublons ignorés).")

    return n > 0 or len(df) > 0


# =============================================================================
# FAVORIS
# =============================================================================

def _load_favorites() -> list:
    if _FAV_FILE.exists():
        with open(_FAV_FILE) as f:
            return json.load(f)
    return []


def _save_favorites(favs: list):
    _FAV_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(_FAV_FILE, "w") as f:
        json.dump(favs, f, indent=2)


# =============================================================================
# MODES INTERACTIFS
# =============================================================================

def _mode_quick(db: MarketDatabase):
    """Mode rapide : saisie minimale, exchange auto-sélectionné."""
    _section("⚡ MODE QUICK START")

    pair = _ask("Paire de trading", "BTC/USDT").upper()

    choice = _menu("Timeframe", TIMEFRAMES, allow_back=False)
    timeframe = TIMEFRAMES[choice - 1]

    days    = int(_ask("Nombre de jours à télécharger", "90"))
    end_dt  = datetime.utcnow()
    start_dt = end_dt - timedelta(days=days)

    exchange_obj, exchange_name = _find_best_exchange(pair, timeframe)
    if not exchange_obj:
        input("\nEntrée pour continuer...")
        return

    print()
    _download(db, exchange_obj, exchange_name, pair, timeframe, start_dt, end_dt)
    input("\nEntrée pour continuer...")


def _mode_advanced(db: MarketDatabase):
    """Mode avancé : configuration complète."""
    _section("🔧 MODE ADVANCED")

    pair      = _ask("Paire de trading", "BTC/USDT").upper()
    tf_idx    = _menu("Timeframe", TIMEFRAMES, allow_back=False)
    timeframe = TIMEFRAMES[tf_idx - 1]

    start_dt = _ask_date("Date de début", datetime(2024, 1, 1))
    end_dt   = _ask_date("Date de fin",   datetime.utcnow())

    if start_dt >= end_dt:
        _err("La date de début doit être antérieure à la date de fin.")
        input("\nEntrée pour continuer...")
        return

    # Choix exchange
    ex_options = EXCHANGES + ["Auto (meilleur exchange)"]
    ex_choice  = _menu("Exchange", ex_options, allow_back=False)
    if ex_choice == len(ex_options):
        exchange_obj, exchange_name = _find_best_exchange(pair, timeframe)
    else:
        exchange_name = EXCHANGES[ex_choice - 1]
        _info(f"Initialisation de {exchange_name}...")
        exchange_obj = _get_exchange(exchange_name)

    if not exchange_obj:
        input("\nEntrée pour continuer...")
        return

    # Résumé
    _section("📋 CONFIRMATION")
    print(f"  Paire     : {C.BOLD}{pair}{C.END}")
    print(f"  Timeframe : {C.BOLD}{timeframe}{C.END}")
    print(f"  Période   : {C.BOLD}{start_dt.strftime('%Y-%m-%d')} → {end_dt.strftime('%Y-%m-%d')}{C.END}")
    print(f"  Exchange  : {C.BOLD}{exchange_name}{C.END}")
    print()

    confirm = input("Confirmer ? [O/n] : ").strip().lower()
    if confirm not in ("", "o", "oui", "y", "yes"):
        _warn("Annulé.")
        input("\nEntrée pour continuer...")
        return

    # Sauvegarder en favori ?
    if input("\n💾 Sauvegarder en favori ? [o/N] : ").strip().lower() in ("o", "oui", "y"):
        fav_name = _ask("Nom du favori", f"{pair}_{timeframe}")
        favs = _load_favorites()
        favs.append({
            "name":       fav_name,
            "pair":       pair,
            "timeframe":  timeframe,
            "exchange":   exchange_name,
            "days":       (end_dt - start_dt).days,
        })
        _save_favorites(favs)
        _ok("Favori sauvegardé.")

    print()
    _download(db, exchange_obj, exchange_name, pair, timeframe, start_dt, end_dt)
    input("\nEntrée pour continuer...")


def _mode_update(db: MarketDatabase):
    """
    Mode mise à jour incrémentale : détecte le dernier timestamp en base
    et ne télécharge que les nouvelles bougies.
    """
    _section("🔄 MISE À JOUR INCRÉMENTALE")

    datasets = db.get_available_datasets()
    if not datasets:
        _warn("Aucun dataset en base. Utilisez le mode Quick Start ou Advanced.")
        input("\nEntrée pour continuer...")
        return

    # Affichage des datasets disponibles
    options = [
        f"{d['exchange']}/{d['symbol']}/{d['timeframe']} — "
        f"{d['candle_count']:,} bougies"
        for d in datasets
    ]
    choice = _menu("Dataset à mettre à jour", options)
    if choice == 0:
        return

    ds = datasets[choice - 1]
    pair      = ds["symbol"].replace("-", "/")
    timeframe = ds["timeframe"]
    exchange_name = ds["exchange"]

    _info(f"Initialisation de {exchange_name}...")
    exchange_obj = _get_exchange(exchange_name)
    if not exchange_obj:
        input("\nEntrée pour continuer...")
        return

    end_dt = datetime.utcnow()
    last_ts = ds["last_ts"]
    start_dt = datetime.utcfromtimestamp(last_ts / 1000) + timedelta(minutes=1)

    if start_dt >= end_dt:
        _ok("Données déjà à jour.")
        input("\nEntrée pour continuer...")
        return

    days_missing = (end_dt - start_dt).days
    _info(f"Mise à jour de {days_missing} jour(s) manquant(s)...")
    print()

    _download(
        db, exchange_obj, exchange_name, pair, timeframe,
        start_dt, end_dt, incremental=False
    )
    input("\nEntrée pour continuer...")


def _mode_favorites(db: MarketDatabase):
    """Mode favoris : utilise une configuration sauvegardée."""
    _section("💾 FAVORIS")

    favs = _load_favorites()
    if not favs:
        _warn("Aucun favori sauvegardé.")
        input("\nEntrée pour continuer...")
        return

    fav_names = [
        f"{f['name']} ({f['pair']} — {f['timeframe']})"
        for f in favs
    ]
    choice = _menu("Favoris", fav_names)
    if choice == 0:
        return

    fav = favs[choice - 1]
    pair      = fav["pair"]
    timeframe = fav["timeframe"]
    days      = fav.get("days", 90)
    end_dt    = datetime.utcnow()
    start_dt  = end_dt - timedelta(days=days)

    _info(f"Initialisation de {fav['exchange']}...")
    exchange_obj = _get_exchange(fav["exchange"])
    if not exchange_obj:
        input("\nEntrée pour continuer...")
        return

    print()
    _download(
        db, exchange_obj, fav["exchange"], pair, timeframe,
        start_dt, end_dt
    )
    input("\nEntrée pour continuer...")


def _mode_status(db: MarketDatabase):
    """Affiche l'état des datasets disponibles en base."""
    _section("📊 ÉTAT DE LA BASE DE DONNÉES")

    _info(f"Fichier : {_DB_PATH}")
    size_mb = round(_DB_PATH.stat().st_size / (1024 * 1024), 2) if _DB_PATH.exists() else 0
    _info(f"Taille  : {size_mb} MB")
    print()

    datasets = db.get_available_datasets()
    if not datasets:
        _warn("Base vide — aucun dataset disponible.")
    else:
        print(f"{C.BOLD}{'Exchange':<10} {'Symbol':<12} {'TF':<6} "
              f"{'Candles':>10}  {'Début':<12} {'Fin':<12} {'Source'}{C.END}")
        print("─" * 72)
        for d in datasets:
            first = datetime.utcfromtimestamp(d["first_ts"] / 1000).strftime("%Y-%m-%d")
            last  = datetime.utcfromtimestamp(d["last_ts"]  / 1000).strftime("%Y-%m-%d")
            print(
                f"  {d['exchange']:<10} {d['symbol']:<12} {d['timeframe']:<6} "
                f"{d['candle_count']:>10,}  {first:<12} {last:<12} "
                f"{d.get('source','?')}"
            )

    input("\nEntrée pour continuer...")


# =============================================================================
# MENU PRINCIPAL
# =============================================================================

def main():
    _banner()

    with MarketDatabase(_DB_PATH) as db:
        while True:
            choice = _menu(
                "🎯 MENU PRINCIPAL",
                [
                    "⚡ Quick Start   — Téléchargement rapide",
                    "🔧 Advanced      — Configuration complète",
                    "🔄 Mise à jour   — Incrémental (nouvelles bougies uniquement)",
                    "💾 Favoris       — Configurations sauvegardées",
                    "📊 État DB       — Datasets disponibles",
                    "❌ Quitter",
                ],
                allow_back=False
            )

            if choice == 1:
                _mode_quick(db)
            elif choice == 2:
                _mode_advanced(db)
            elif choice == 3:
                _mode_update(db)
            elif choice == 4:
                _mode_favorites(db)
            elif choice == 5:
                _mode_status(db)
            elif choice == 6:
                print()
                _ok("Au revoir !")
                break


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n{C.YELLOW}⚠️  Interruption utilisateur.{C.END}")
        _ok("Au revoir !")
    except Exception as exc:
        _err(f"Erreur fatale : {exc}")
        sys.exit(1)
