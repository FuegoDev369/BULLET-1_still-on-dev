"""
BULLET-1 - Data Loader
========================

Chargement des données historiques OHLCV depuis la base SQLite.

Remplace la v2.4.1 (backend CSV).
Interface publique identique — aucun changement requis dans les appelants.

Changements v3.0.0 :
    - Backend SQLite via MarketDatabase (src/data/db_manager.py)
    - Suppression de tout le code CSV / chunked / filesystem
    - load_lazy() redesigné : générateur par tranches de dates (Android RAM)
    - get_data_info() et get_available_timeframes() depuis table datasets
    - get_db_fingerprint() : empreinte traçabilité pour ohlcv_data_engine
    - Retrait de csv_path, load_chunked, reload, quick_load_csv

Interface conservée :
    DataLoader(config)
    .load(start_date, end_date, timeframe, use_cache) → DataFrame
    .load_lazy(start_date, end_date, timeframe, chunk_days) → Generator
    .get_available_timeframes() → List[str]
    .get_data_info(timeframe) → Dict
    .get_db_fingerprint(timeframe) → Optional[str]  [NEW]
    .get_db_path() → Path                            [NEW]
    .clear_cache()
    .get_cache_stats() → Dict
    .get_memory_usage() → Dict

Version: 3.0.0
Date: 2026-04-21
Author: FuegoDev
Dépendances: db_manager.py, helpers.py, logger.py
"""

import sys
import threading
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Tuple

import pandas as pd

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False
    warnings.warn(
        "psutil non disponible. pip install psutil. "
        "get_memory_usage() retournera uniquement les stats cache."
    )

# ── Project root resolution ────────────────────────────────────────────────────
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.utils.helpers import format_datetime, get_project_root, timestamp_to_datetime
from src.utils.logger import BulletLogger
from src.data.db_manager import MarketDatabase

_VERSION = "3.0.0"

_MAX_CACHE_SIZE = 3                             # entrées cache max (Android conservateur)
_DEFAULT_DB_PATH = "data/bullet1_market_data.db"  # relatif à la racine projet


class DataLoader:
    """
    Chargeur de données historiques OHLCV depuis la base SQLite BULLET-1.

    Responsabilité unique : charger les données et retourner un DataFrame
    prêt pour le pipeline (types corrects, timestamps UTC).
    La validation OHLCV appartient à data_validator.py.
    La gestion du schéma DB appartient à db_manager.py.

    Structure de config attendue :
        {
            'general': {
                'exchange':     'binance',
                'trading_pair': 'BTC/USDT',
                'timeframe':    '15m'
            },
            'backtesting': {
                'start_date': '2024-01-01',
                'end_date':   '2024-03-11'
            },
            'data': {
                'db_path': 'data/bullet1_market_data.db'  # optionnel
            }
        }
    """

    def __init__(self, config: dict) -> None:
        """
        Args:
            config: Configuration complète du bot (dict Python standard).

        Raises:
            TypeError:    Si config n'est pas un dict.
            RuntimeError: Si la base SQLite est inaccessible.
        """
        if not isinstance(config, dict):
            raise TypeError(
                f"config doit être un dict Python standard, reçu : {type(config).__name__}."
            )

        self.logger = BulletLogger()
        self.config = config

        # ── Résolution du chemin DB ────────────────────────────────────────────
        db_path_raw = config.get('data', {}).get('db_path', _DEFAULT_DB_PATH)
        project_root = get_project_root()
        db_path = project_root / db_path_raw
        self._db = MarketDatabase(db_path)

        # ── Cache mémoire (thread-safe) ────────────────────────────────────────
        self._cache: Dict[str, pd.DataFrame] = {}
        self._cache_keys: List[str] = []
        self._cache_lock = threading.Lock()

        self.logger.info(
            f"DataLoader v{_VERSION} — DB : {self._db.get_db_path().name} "
            f"({self._db.get_db_size_mb()} MB) | cache_max={_MAX_CACHE_SIZE}"
        )

    # =========================================================================
    # MÉTHODE PRINCIPALE
    # =========================================================================

    def load(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        timeframe: Optional[str] = None,
        use_cache: bool = True,
        show_progress: bool = False   # no-op, conservé pour rétrocompatibilité
    ) -> pd.DataFrame:
        """
        Charge les données OHLCV depuis la base SQLite.

        Args:
            start_date:    'YYYY-MM-DD' ou ISO. Défaut : config['backtesting']['start_date']
            end_date:      'YYYY-MM-DD' ou ISO. Défaut : config['backtesting']['end_date']
            timeframe:     '5m', '15m', '1h', etc. Défaut : config['general']['timeframe']
            use_cache:     Utiliser le cache mémoire si disponible.
            show_progress: Ignoré (conservé pour compatibilité API).

        Returns:
            pd.DataFrame: [timestamp(datetime64[ns,UTC]), open, high, low,
                           close, volume(float64)]. Trié ASC.

        Raises:
            ValueError: Dates invalides, données absentes, paramètres manquants.
        """
        exchange, symbol, timeframe = self._resolve_db_keys(timeframe)

        if start_date is None:
            start_date = self.config.get('backtesting', {}).get('start_date')
        if end_date is None:
            end_date = self.config.get('backtesting', {}).get('end_date')

        self._validate_date_range(start_date, end_date)

        cache_key = f"{exchange}_{symbol}_{timeframe}_{start_date}_{end_date}"

        if use_cache:
            cached = self._get_from_cache(cache_key)
            if cached is not None:
                self.logger.info(f"📦 Cache hit : {cache_key}")
                return cached.copy()

        start_ms = self._date_to_ms(start_date)
        end_ms   = self._date_to_ms(end_date)

        self.logger.info(
            f"📂 Loading from DB : {exchange}/{symbol}/{timeframe} "
            f"[{start_date} → {end_date}]"
        )

        df = self._db.query_candles(exchange, symbol, timeframe, start_ms, end_ms)

        if df.empty:
            raise ValueError(
                f"Aucune donnée disponible pour {exchange}/{symbol}/{timeframe} "
                f"sur [{start_date} → {end_date}].\n"
                f"Lancez le téléchargement : python data/download_data_v3.0.py\n"
                f"Ou migrez les CSV existants : python data/migrate_csv_to_db.py"
            )

        if use_cache:
            self._add_to_cache(cache_key, df)

        self.logger.info(
            f"✅ {len(df):,} candles chargées "
            f"({df['timestamp'].iloc[0]} → {df['timestamp'].iloc[-1]})"
        )
        return df

    # =========================================================================
    # GÉNÉRATEUR (économie RAM Android)
    # =========================================================================

    def load_lazy(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        timeframe: Optional[str] = None,
        chunk_days: int = 30
    ) -> Generator[pd.DataFrame, None, None]:
        """
        Générateur de DataFrames par tranche de dates.

        Recommandé sur Android pour les très larges plages (>6 mois à 1m).
        Chaque chunk est une requête SQL indépendante — RAM libérée entre chaque yield.

        Args:
            chunk_days: Nombre de jours par tranche (défaut : 30).

        Yields:
            pd.DataFrame: Chunk OHLCV non vide.
        """
        exchange, symbol, timeframe = self._resolve_db_keys(timeframe)

        if start_date is None:
            start_date = self.config.get('backtesting', {}).get('start_date')
        if end_date is None:
            end_date = self.config.get('backtesting', {}).get('end_date')

        self._validate_date_range(start_date, end_date)

        current_dt = self._parse_datetime_utc(start_date)
        end_dt     = self._parse_datetime_utc(end_date)
        delta      = timedelta(days=chunk_days)
        chunk_n    = 0

        self.logger.info(
            f"📂 load_lazy — {exchange}/{symbol}/{timeframe} "
            f"[{start_date} → {end_date}] chunk_days={chunk_days}"
        )

        while current_dt < end_dt:
            chunk_end_dt = min(current_dt + delta, end_dt)
            start_ms = int(current_dt.timestamp() * 1000)
            end_ms   = int(chunk_end_dt.timestamp() * 1000)

            # Borne droite exclusive sauf sur le dernier chunk :
            # évite de compter deux fois la candle sur la frontière.
            if chunk_end_dt < end_dt:
                end_ms -= 1

            df = self._db.query_candles(exchange, symbol, timeframe, start_ms, end_ms)

            if not df.empty:
                chunk_n += 1
                self.logger.debug(
                    f"  Chunk {chunk_n} : {len(df):,} candles "
                    f"[{current_dt.date()} → {chunk_end_dt.date()}]"
                )
                yield df

            current_dt = chunk_end_dt

    # =========================================================================
    # INFORMATIONS & UTILITAIRES
    # =========================================================================

    def get_available_timeframes(
        self,
        exchange: Optional[str] = None,
        symbol: Optional[str] = None
    ) -> List[str]:
        """
        Liste les timeframes disponibles pour le trading pair courant.

        Returns:
            list: Timeframes triés (ex. ['5m', '15m', '1h']).
        """
        if exchange is None:
            exchange = self.config.get('general', {}).get('exchange', 'binance').lower()
        if symbol is None:
            raw = self.config.get('general', {}).get('trading_pair', '')
            symbol = raw.replace('/', '-')

        datasets = self._db.get_available_datasets()
        return sorted(
            d['timeframe']
            for d in datasets
            if d['exchange'] == exchange and d['symbol'] == symbol
        )

    def get_data_info(
        self,
        timeframe: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Métadonnées du dataset (depuis la table datasets — lecture O(1)).

        Returns:
            dict: exchange, symbol, timeframe, first_date, last_date,
                  duration_days, candle_count, db_path, db_size_mb.
                  Contient 'error' si le dataset est introuvable.
        """
        try:
            exchange, symbol, tf = self._resolve_db_keys(timeframe)
        except ValueError as exc:
            return {'error': str(exc)}

        info = self._db.get_dataset_info(exchange, symbol, tf)
        if info is None:
            return {
                'exchange':  exchange,
                'symbol':    symbol,
                'timeframe': tf,
                'error':     (
                    'Dataset non trouvé en base. '
                    'Lancez download_data_v3.0.py ou migrate_csv_to_db.py.'
                )
            }

        first_dt = datetime.fromtimestamp(info['first_ts'] / 1000, tz=timezone.utc)
        last_dt  = datetime.fromtimestamp(info['last_ts']  / 1000, tz=timezone.utc)

        return {
            'exchange':     exchange,
            'symbol':       symbol,
            'timeframe':    tf,
            'first_date':   format_datetime(first_dt, '%Y-%m-%d'),
            'last_date':    format_datetime(last_dt,  '%Y-%m-%d'),
            'duration_days': (last_dt - first_dt).days,
            'candle_count': info['candle_count'],
            'source':       info.get('source', 'unknown'),
            'db_path':      str(self._db.get_db_path()),
            'db_size_mb':   self._db.get_db_size_mb(),
        }

    def get_db_fingerprint(self, timeframe: Optional[str] = None) -> Optional[str]:
        """
        Empreinte déterministe du dataset actif.
        Utilisée par ohlcv_data_engine pour la traçabilité (remplace hash SHA-256 CSV).

        Returns:
            str: Hash 12 hex chars, ou None si dataset inconnu.
        """
        try:
            exchange, symbol, tf = self._resolve_db_keys(timeframe)
            return self._db.get_dataset_fingerprint(exchange, symbol, tf)
        except Exception:
            return None

    def get_db_path(self) -> Path:
        """Retourne le chemin du fichier SQLite (remplace l'ancien resolve_path())."""
        return self._db.get_db_path()

    # =========================================================================
    # CACHE (thread-safe)
    # =========================================================================

    def clear_cache(self) -> None:
        """Vide le cache mémoire. Utile pour libérer RAM sur Android."""
        with self._cache_lock:
            n = len(self._cache)
            self._cache.clear()
            self._cache_keys.clear()
        self.logger.info(f"Cache vidé ({n} DataFrame(s) supprimés).")

    def get_cache_stats(self) -> Dict[str, Any]:
        """Stats du cache (thread-safe)."""
        with self._cache_lock:
            total_rows = sum(len(df) for df in self._cache.values())
            memory_mb  = (total_rows * 6 * 8) / (1024 * 1024)
            return {
                'size':       len(self._cache),
                'max_size':   _MAX_CACHE_SIZE,
                'keys':       self._cache_keys.copy(),
                'total_rows': total_rows,
                'memory_mb':  round(memory_mb, 2),
            }

    def get_memory_usage(self) -> Dict[str, Any]:
        """Usage mémoire du DataLoader (cache + système si psutil disponible)."""
        cache_mb = self.get_cache_stats()['memory_mb']

        if not PSUTIL_AVAILABLE:
            return {'cache_mb': cache_mb, 'psutil_available': False}

        process = psutil.Process()
        system_mb = process.memory_info().rss / (1024 * 1024)
        vmem = psutil.virtual_memory()

        return {
            'cache_mb':         cache_mb,
            'system_mb':        round(system_mb, 2),
            'available_mb':     round(vmem.available / (1024 * 1024), 2),
            'total_ram_mb':     round(vmem.total / (1024 * 1024), 2),
            'ram_percent_used': vmem.percent,
            'psutil_available': True,
        }

    # =========================================================================
    # MÉTHODES PRIVÉES
    # =========================================================================

    def _resolve_db_keys(
        self,
        timeframe: Optional[str] = None
    ) -> Tuple[str, str, str]:
        """
        Résout (exchange, symbol, timeframe) depuis la config.

        Returns:
            tuple: ('binance', 'BTC-USDT', '15m')

        Raises:
            ValueError: trading_pair ou timeframe absent.
        """
        exchange = self.config.get('general', {}).get('exchange', 'binance').lower()

        raw_pair = self.config.get('general', {}).get('trading_pair', '')
        if not raw_pair:
            raise ValueError("config['general']['trading_pair'] absent ou vide.")
        symbol = raw_pair.replace('/', '-')  # 'BTC/USDT' → 'BTC-USDT'

        if timeframe is None:
            timeframe = self.config.get('general', {}).get('timeframe')
        if not timeframe:
            raise ValueError(
                "timeframe non spécifié et config['general']['timeframe'] absent."
            )

        return exchange, symbol, timeframe

    def _date_to_ms(self, date_str: str) -> int:
        """Convertit 'YYYY-MM-DD' ou ISO en Unix millisecondes UTC."""
        dt = self._parse_datetime_utc(date_str)
        return int(dt.timestamp() * 1000)

    def _parse_datetime_utc(self, timestamp: Any) -> datetime:
        """Parser un timestamp vers datetime UTC."""
        dt = timestamp_to_datetime(timestamp)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        elif dt.tzinfo != timezone.utc:
            dt = dt.astimezone(timezone.utc)
        return dt

    def _validate_date_range(
        self,
        start_date: Optional[str],
        end_date: Optional[str]
    ) -> None:
        """
        Valide présence et ordre des dates.

        Raises:
            ValueError: Dates absentes, format invalide ou start >= end.
        """
        if not start_date:
            raise ValueError(
                "start_date non spécifié et config['backtesting']['start_date'] absent."
            )
        if not end_date:
            raise ValueError(
                "end_date non spécifié et config['backtesting']['end_date'] absent."
            )
        try:
            start_dt = self._parse_datetime_utc(start_date)
            end_dt   = self._parse_datetime_utc(end_date)
        except Exception as exc:
            raise ValueError(f"Format de date invalide : {exc}") from exc

        if start_dt >= end_dt:
            raise ValueError(
                f"Plage invalide : start_date ({start_date}) "
                f"doit être antérieure à end_date ({end_date})."
            )

    def _get_from_cache(self, key: str) -> Optional[pd.DataFrame]:
        with self._cache_lock:
            return self._cache.get(key)

    def _add_to_cache(self, key: str, df: pd.DataFrame) -> None:
        with self._cache_lock:
            if len(self._cache) >= _MAX_CACHE_SIZE:
                oldest = self._cache_keys.pop(0)
                del self._cache[oldest]
                self.logger.debug(f"Cache plein, éviction : {oldest}")
            self._cache[key] = df.copy()
            self._cache_keys.append(key)
            mem_mb = (len(df) * 6 * 8) / (1024 * 1024)
            self.logger.debug(
                f"Mis en cache : {key} ({len(df):,} lignes, ~{mem_mb:.2f} MB)"
            )

    def resolve_path(self) -> Optional[str]:
        """
        [Compat ohlcv_data_engine ≤ v2.2.x] Retourne le chemin DB (string).
        Remplacé par get_db_fingerprint() en ohlcv_data_engine v2.3.0.
        """
        return str(self._db.get_db_path())


# FIN DU MODULE
