"""
BULLET-1 - Market Database Manager
=====================================

Couche d'accès SQLite pour les données historiques OHLCV.
Remplace le système CSV (data/historical/**/*.csv) depuis la v3.0.

Schéma :
    ohlcv    — données de marché (exchange, symbol, timeframe, timestamp, OHLCV)
    datasets — registre des datasets disponibles (métadonnées rapides)

Choix techniques justifiés :
    - SQLite  : zéro serveur, fichier unique, compatible Termux/Android.
    - WAL     : lectures concurrentes pendant écriture.
    - UNIQUE  : déduplication native (exchange, symbol, timeframe, timestamp).
    - INDEX   : requêtes temporelles O(log N) sur grandes séries.
    - RLock   : thread-safety sur connexion partagée (single-process).
    - Pas d'ORM : dépendance nulle, stdlib sqlite3 suffit amplement.

Version: 1.0.0
Date: 2026-04-21
Author: FuegoDev
Dépendances: stdlib (sqlite3, threading, pathlib, hashlib) + pandas
"""

import hashlib
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

# ── Project root resolution ────────────────────────────────────────────────────
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.utils.logger import BulletLogger

_VERSION = "1.0.0"

# =============================================================================
# DDL
# =============================================================================

_DDL_OHLCV = """
CREATE TABLE IF NOT EXISTS ohlcv (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    exchange  TEXT    NOT NULL,
    symbol    TEXT    NOT NULL,
    timeframe TEXT    NOT NULL,
    timestamp INTEGER NOT NULL,   -- Unix millisecondes UTC
    open      REAL    NOT NULL,
    high      REAL    NOT NULL,
    low       REAL    NOT NULL,
    close     REAL    NOT NULL,
    volume    REAL    NOT NULL,
    UNIQUE(exchange, symbol, timeframe, timestamp)
);
"""

# Index composite couvrant toutes les requêtes de lecture BULLET-1 :
#   WHERE exchange=? AND symbol=? AND timeframe=? AND timestamp BETWEEN ? AND ?
_DDL_OHLCV_INDEX = """
CREATE INDEX IF NOT EXISTS idx_ohlcv_main
    ON ohlcv(exchange, symbol, timeframe, timestamp);
"""

# Registre léger des datasets — évite un COUNT(*) complet à chaque
# appel de get_data_info() ou get_available_timeframes().
_DDL_DATASETS = """
CREATE TABLE IF NOT EXISTS datasets (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    exchange       TEXT    NOT NULL,
    symbol         TEXT    NOT NULL,
    timeframe      TEXT    NOT NULL,
    first_ts       INTEGER NOT NULL,  -- Unix ms UTC (pour calculs internes)
    last_ts        INTEGER NOT NULL,  -- Unix ms UTC (pour calculs internes)
    first_date_utc TEXT    NOT NULL,  -- 'YYYY-MM-DD HH:MM' UTC (lecture humaine)
    last_date_utc  TEXT    NOT NULL,  -- 'YYYY-MM-DD HH:MM' UTC (lecture humaine)
    candle_count   INTEGER NOT NULL,
    source         TEXT,              -- 'binance_api', 'csv_import', etc.
    updated_at     INTEGER NOT NULL,  -- Unix ms UTC
    UNIQUE(exchange, symbol, timeframe)
);
"""

# Vue synthétique — colonnes calculées (durée, next_update).
# Accessible depuis n'importe quel navigateur SQLite (DB Browser, etc.)
# et depuis db_status.py.
# Note : first_date_utc / last_date_utc sont maintenant des colonnes TEXT
#        directement dans la table datasets (lisibles sans conversion).
_DDL_DATASETS_VIEW = """
CREATE VIEW IF NOT EXISTS datasets_readable AS
SELECT
    exchange,
    symbol,
    timeframe,
    first_date_utc,
    last_date_utc,
    CAST(
        (last_ts - first_ts) / 1000 / 86400 AS INTEGER
    ) AS duration_days,
    candle_count,
    source,
    datetime(updated_at / 1000, 'unixepoch') AS updated_at_utc
FROM datasets
ORDER BY exchange, symbol, timeframe;
"""


# =============================================================================
# CLASSE PRINCIPALE
# =============================================================================

class MarketDatabase:
    """
    Gestionnaire de base de données SQLite pour les données OHLCV BULLET-1.

    Thread-safe : connexion unique protégée par RLock.
    WAL mode    : lectures parallèles pendant insertion.
    Android     : cache page SQLite limité à 4 MB (configurable).

    Usage typique :
        db = MarketDatabase(Path("data/bullet1_market_data.db"))
        n  = db.insert_candles("binance", "BTC-USDT", "15m", df)
        df = db.query_candles("binance", "BTC-USDT", "15m", start_ms, end_ms)
        db.close()

    Context manager :
        with MarketDatabase(path) as db:
            df = db.query_candles(...)
    """

    def __init__(self, db_path: Path) -> None:
        """
        Ouvre (ou crée) la base SQLite et initialise le schéma.

        Args:
            db_path: Chemin vers le fichier .db. Les répertoires parents
                     sont créés automatiquement.

        Raises:
            RuntimeError: Impossible d'ouvrir ou de créer la base.
        """
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        self.logger = BulletLogger()
        self._connect()
        self.logger.info(
            f"MarketDatabase v{_VERSION} — {self._db_path.name} "
            f"({'existing' if self._db_path.stat().st_size > 4096 else 'new'})"
        )

    # =========================================================================
    # CONNEXION & SCHÉMA
    # =========================================================================

    def _connect(self) -> None:
        """Ouvre la connexion SQLite et configure les PRAGMAs."""
        try:
            self._conn = sqlite3.connect(
                str(self._db_path),
                check_same_thread=False   # protégé par RLock
            )
            self._conn.row_factory = sqlite3.Row
        except sqlite3.Error as exc:
            raise RuntimeError(
                f"Impossible d'ouvrir la base SQLite : {self._db_path}\n"
                f"Erreur : {exc}"
            ) from exc

        cur = self._conn.cursor()
        # WAL mode — meilleure concurrence sur Android (pas de verrou exclusif)
        cur.execute("PRAGMA journal_mode=WAL")
        # NORMAL = safe avec WAL, plus rapide que FULL
        cur.execute("PRAGMA synchronous=NORMAL")
        # 4 MB page cache — conservateur pour Android (défaut SQLite = 2 MB)
        cur.execute("PRAGMA cache_size=-4000")
        # Tables temporaires en mémoire plutôt que sur disque
        cur.execute("PRAGMA temp_store=MEMORY")
        self._conn.commit()
        self._init_schema()

    def _init_schema(self) -> None:
        """Crée les tables et la vue lisible si inexistantes (idempotent)."""
        cur = self._conn.cursor()
        cur.execute(_DDL_OHLCV)
        cur.execute(_DDL_OHLCV_INDEX)
        cur.execute(_DDL_DATASETS)
        cur.execute(_DDL_DATASETS_VIEW)
        self._conn.commit()

    # =========================================================================
    # ÉCRITURE
    # =========================================================================

    def insert_candles(
        self,
        exchange: str,
        symbol: str,
        timeframe: str,
        df: pd.DataFrame,
        source: str = "unknown"
    ) -> int:
        """
        Insère des candles OHLCV dans la DB.

        Stratégie INSERT OR IGNORE : les doublons (même timestamp pour le même
        exchange/symbol/timeframe) sont silencieusement ignorés — ce comportement
        est intentionnel pour les téléchargements incrémentaux.

        Args:
            exchange:  Nom de l'exchange en minuscules ('binance').
            symbol:    Symbole normalisé avec tiret ('BTC-USDT').
            timeframe: Timeframe BULLET-1 ('5m', '15m', '1h', etc.)
            df:        DataFrame [timestamp, open, high, low, close, volume].
                       timestamp peut être datetime64[ns, UTC] ou Unix ms entier.
            source:    Origine des données ('binance_api', 'csv_import', etc.)

        Returns:
            int: Nombre de lignes réellement insérées (hors doublons ignorés).

        Raises:
            ValueError: DataFrame vide ou colonnes manquantes.
            RuntimeError: Erreur SQLite inattendue.
        """
        if df.empty:
            raise ValueError("DataFrame vide — rien à insérer.")

        required = {'timestamp', 'open', 'high', 'low', 'close', 'volume'}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"Colonnes manquantes dans le DataFrame : {missing}")

        # Conversion timestamps → Unix ms entiers
        # Gère toutes les résolutions pandas (ns, us, ms, s) et les types timezone-aware.
        ts_col = df['timestamp']
        if pd.api.types.is_datetime64_any_dtype(ts_col):
            dtype_str = str(ts_col.dtype)   # ex: 'datetime64[us, UTC]'
            raw_int   = ts_col.astype('int64')
            if '[us' in dtype_str:
                ts_ms = (raw_int // 1_000).tolist()       # microsecondes → ms
            elif '[ns' in dtype_str:
                ts_ms = (raw_int // 1_000_000).tolist()   # nanosecondes  → ms
            elif '[ms' in dtype_str:
                ts_ms = raw_int.tolist()                  # déjà en ms
            elif '[s' in dtype_str:
                ts_ms = (raw_int * 1_000).tolist()        # secondes → ms
            else:
                # Fallback universel — compatible toute résolution
                ts_ms = [int(t.timestamp() * 1000) for t in ts_col]
        elif pd.api.types.is_integer_dtype(ts_col):
            ts_ms = ts_col.tolist()
        else:
            ts_parsed = pd.to_datetime(ts_col, utc=True)
            dtype_str = str(ts_parsed.dtype)
            raw_int   = ts_parsed.astype('int64')
            if '[us' in dtype_str:
                ts_ms = (raw_int // 1_000).tolist()
            elif '[ns' in dtype_str:
                ts_ms = (raw_int // 1_000_000).tolist()
            else:
                ts_ms = [int(t.timestamp() * 1000) for t in ts_parsed]

        rows = list(zip(
            [exchange]   * len(df),
            [symbol]     * len(df),
            [timeframe]  * len(df),
            ts_ms,
            df['open'].tolist(),
            df['high'].tolist(),
            df['low'].tolist(),
            df['close'].tolist(),
            df['volume'].tolist(),
        ))

        sql_insert = (
            "INSERT OR IGNORE INTO ohlcv "
            "(exchange, symbol, timeframe, timestamp, open, high, low, close, volume) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )

        with self._lock:
            try:
                # total_changes avant/après = nombre de lignes réellement insérées
                # (INSERT OR IGNORE ne comptabilise pas les lignes ignorées)
                before = self._conn.total_changes
                cur = self._conn.cursor()
                cur.executemany(sql_insert, rows)
                self._conn.commit()
                n_inserted = self._conn.total_changes - before

                # Mise à jour du registre datasets
                self._update_dataset_registry(exchange, symbol, timeframe, source)

            except sqlite3.Error as exc:
                self._conn.rollback()
                raise RuntimeError(
                    f"Erreur SQLite lors de l'insertion ({exchange}/{symbol}/{timeframe}) : {exc}"
                ) from exc

        self.logger.info(
            f"✅ insert_candles — {n_inserted}/{len(df)} insérées "
            f"({exchange}/{symbol}/{timeframe})"
        )
        return n_inserted

    def _update_dataset_registry(
        self,
        exchange: str,
        symbol: str,
        timeframe: str,
        source: str
    ) -> None:
        """
        Recalcule et met à jour les métadonnées du dataset dans la table datasets.
        Appelé automatiquement après chaque insert_candles.

        Stocke à la fois :
        - first_ts / last_ts en Unix ms (pour calculs internes : fingerprint, arithmétique)
        - first_date_utc / last_date_utc en texte 'YYYY-MM-DD HH:MM' UTC (lecture humaine)
        """
        import datetime as _dt
        cur = self._conn.cursor()
        row = cur.execute(
            "SELECT MIN(timestamp), MAX(timestamp), COUNT(*) "
            "FROM ohlcv WHERE exchange=? AND symbol=? AND timeframe=?",
            (exchange, symbol, timeframe)
        ).fetchone()

        if row and row[0] is not None:
            first_ts, last_ts, count = int(row[0]), int(row[1]), int(row[2])
            now_ms = int(time.time() * 1000)

            # Colonnes lisibles : Unix ms → 'YYYY-MM-DD HH:MM' UTC
            first_date_utc = _dt.datetime.utcfromtimestamp(first_ts / 1000).strftime("%Y-%m-%d %H:%M")
            last_date_utc  = _dt.datetime.utcfromtimestamp(last_ts  / 1000).strftime("%Y-%m-%d %H:%M")

            cur.execute(
                """
                INSERT INTO datasets
                    (exchange, symbol, timeframe, first_ts, last_ts,
                     first_date_utc, last_date_utc,
                     candle_count, source, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(exchange, symbol, timeframe) DO UPDATE SET
                    first_ts       = excluded.first_ts,
                    last_ts        = excluded.last_ts,
                    first_date_utc = excluded.first_date_utc,
                    last_date_utc  = excluded.last_date_utc,
                    candle_count   = excluded.candle_count,
                    source         = excluded.source,
                    updated_at     = excluded.updated_at
                """,
                (exchange, symbol, timeframe,
                 first_ts, last_ts, first_date_utc, last_date_utc,
                 count, source, now_ms)
            )
            self._conn.commit()

    # =========================================================================
    # LECTURE
    # =========================================================================

    def query_candles(
        self,
        exchange: str,
        symbol: str,
        timeframe: str,
        start_ms: int,
        end_ms: int
    ) -> pd.DataFrame:
        """
        Charge les candles OHLCV pour une plage de temps donnée.

        L'index de la table couvre (exchange, symbol, timeframe, timestamp) :
        la requête est O(log N + résultat) — pas de full-scan.

        Args:
            exchange:  Nom de l'exchange.
            symbol:    Symbole ('BTC-USDT').
            timeframe: Timeframe.
            start_ms:  Timestamp début Unix ms UTC (inclusif).
            end_ms:    Timestamp fin   Unix ms UTC (inclusif).

        Returns:
            pd.DataFrame: Colonnes [timestamp(datetime64[ns,UTC]), open, high,
                          low, close, volume(float64)]. Trié ASC. Peut être vide.
        """
        sql = (
            "SELECT timestamp, open, high, low, close, volume "
            "FROM ohlcv "
            "WHERE exchange=? AND symbol=? AND timeframe=? "
            "  AND timestamp >= ? AND timestamp <= ? "
            "ORDER BY timestamp ASC"
        )
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(sql, (exchange, symbol, timeframe, start_ms, end_ms))
            rows = cur.fetchall()

        if not rows:
            return pd.DataFrame(
                columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']
            )

        df = pd.DataFrame(
            [(r[0], r[1], r[2], r[3], r[4], r[5]) for r in rows],
            columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']
        )
        # Retour en datetime64[ns, UTC] — format attendu par tout le pipeline BULLET-1
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True)
        for col in ('open', 'high', 'low', 'close', 'volume'):
            df[col] = df[col].astype('float64')

        return df

    # =========================================================================
    # MÉTADONNÉES
    # =========================================================================

    def get_available_datasets(self) -> List[Dict]:
        """
        Retourne tous les datasets disponibles avec leurs métadonnées.

        Returns:
            List[Dict]: exchange, symbol, timeframe, first_ts, last_ts,
                        candle_count, source, updated_at — pour chaque dataset.
        """
        with self._lock:
            cur = self._conn.cursor()
            rows = cur.execute(
                "SELECT exchange, symbol, timeframe, first_ts, last_ts, "
                "candle_count, source, updated_at "
                "FROM datasets ORDER BY exchange, symbol, timeframe"
            ).fetchall()

        return [dict(r) for r in rows]

    def get_dataset_info(
        self,
        exchange: str,
        symbol: str,
        timeframe: str
    ) -> Optional[Dict]:
        """
        Retourne les métadonnées d'un dataset, ou None s'il n'existe pas.

        Returns:
            dict avec first_ts, last_ts, candle_count, source, updated_at
            (timestamps en Unix ms UTC), ou None.
        """
        with self._lock:
            cur = self._conn.cursor()
            row = cur.execute(
                "SELECT exchange, symbol, timeframe, first_ts, last_ts, "
                "candle_count, source, updated_at "
                "FROM datasets WHERE exchange=? AND symbol=? AND timeframe=?",
                (exchange, symbol, timeframe)
            ).fetchone()

        return dict(row) if row else None

    def get_dataset_fingerprint(
        self,
        exchange: str,
        symbol: str,
        timeframe: str
    ) -> Optional[str]:
        """
        Empreinte déterministe du dataset pour la traçabilité des backtests.

        Basée sur first_ts + last_ts + candle_count — calcul O(1) sans
        lire les données. Deux backtests avec la même empreinte utilisent
        exactement le même jeu de données.

        Returns:
            str: Hash SHA-256 tronqué à 12 hex chars, ou None si dataset inconnu.
        """
        info = self.get_dataset_info(exchange, symbol, timeframe)
        if info is None:
            return None
        raw = (
            f"{exchange}|{symbol}|{timeframe}|"
            f"{info['first_ts']}|{info['last_ts']}|{info['candle_count']}"
        )
        return hashlib.sha256(raw.encode()).hexdigest()[:12]

    def dataset_exists(self, exchange: str, symbol: str, timeframe: str) -> bool:
        """Vérifie rapidement si un dataset est disponible."""
        return self.get_dataset_info(exchange, symbol, timeframe) is not None

    # =========================================================================
    # UTILITAIRES
    # =========================================================================

    def get_db_path(self) -> Path:
        """Retourne le chemin absolu du fichier SQLite."""
        return self._db_path

    def get_db_size_mb(self) -> float:
        """Taille du fichier DB en mégaoctets."""
        try:
            return round(self._db_path.stat().st_size / (1024 * 1024), 2)
        except OSError:
            return 0.0

    def close(self) -> None:
        """Ferme la connexion SQLite proprement (flush WAL inclus)."""
        with self._lock:
            if self._conn:
                try:
                    self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    self._conn.close()
                except sqlite3.Error:
                    pass
                finally:
                    self._conn = None
                    self.logger.debug("MarketDatabase : connexion fermée.")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def __repr__(self) -> str:
        return (
            f"MarketDatabase(path='{self._db_path.name}', "
            f"size={self.get_db_size_mb()}MB)"
        )


# FIN DU MODULE
