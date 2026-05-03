"""
BULLET-1 - OHLCV Data Engine
==============================

Pipeline de données OHLCV pour backtesting.

Rôle unique : orchestrer le chargement, la validation et le traitement LIGHT
des données de marché, puis retourner un DataFrame prêt pour la simulation.

Version: 2.3.0
Module:  src/backtesting/ohlcv_data_engine.py
Author:  FuegoDev

Changements v2.3.0 :
    - [DB-MIGRATION] Hash SHA-256 fichier CSV → empreinte DB (get_db_fingerprint)
    - [DB-MIGRATION] FileNotFoundError → ValueError pour données absentes
    - [DB-MIGRATION] Messages d'erreur : "CSV" → "base de données"


"""

import hashlib
import sys
from pathlib import Path
from typing import Optional

import pandas as pd

# ── Project root resolution ────────────────────────────────────────────────────
# [v2.2.2 — FIX-ODE-4] Pattern unifié BULLET-1 (majuscules = constante module-level).
# Aligné sur market_context.py, signal_generator.py, order_simulator.py, strategy.py.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.utils.logger  import BulletLogger
from src.utils.helpers import ensure_directory, get_project_root
from src.data.data_loader    import DataLoader
from src.data.data_validator import DataValidator
from src.data.data_processor import DataProcessor


# ── Required config keys (section, key) ───────────────────────────────────────
_REQUIRED_KEYS: tuple[tuple[str, str], ...] = (
    ("general",     "trading_pair"),
    ("general",     "timeframe"),
    ("backtesting", "start_date"),
    ("backtesting", "end_date"),
)


def _read_pipeline_flags(config: dict) -> tuple[bool, bool]:
    """
    Lire les flags d'activation du pipeline depuis config.

    Chemin JSON : config['engine_config']['ohlcv_data_engine']
    Valeur par défaut (clé absente) : True pour les deux flags.

    Args:
        config: Configuration complète du bot.

    Returns:
        tuple[bool, bool]: (validator_enabled, processor_enabled)
    """
    ode_cfg = (
        config
        .get("engine_config", {})
        .get("ohlcv_data_engine", {})
    )
    validator_enabled = ode_cfg.get("data_validator", {}).get("enabled", True)
    processor_enabled = ode_cfg.get("data_processor", {}).get("enabled", True)
    return bool(validator_enabled), bool(processor_enabled)


def _timeframe_to_minutes(timeframe: str) -> int:
    """
    Convertit un timeframe string en minutes entier.

    Utilisé par OHLCVDataEngine._compute_warmup_start_date() pour calculer
    le delta de chargement anticipé (warmup).

    Returns 0 si le format est inconnu (warmup désactivé silencieusement).
    """
    tf = timeframe.strip().lower()
    _KNOWN: dict = {
        '1m': 1,   '3m': 3,   '5m': 5,   '10m': 10,  '15m': 15,
        '30m': 30, '45m': 45,
        '1h': 60,  '2h': 120, '3h': 180, '4h': 240,  '6h': 360,
        '8h': 480, '12h': 720,
        '1d': 1440, '3d': 4320, '1w': 10080,
    }
    if tf in _KNOWN:
        return _KNOWN[tf]
    for suffix, factor in (('m', 1), ('h', 60), ('d', 1440), ('w', 10080)):
        if tf.endswith(suffix):
            try:
                return int(tf[:-len(suffix)]) * factor
            except ValueError:
                pass
    return 0


class OHLCVDataEngine:
    """
    Pipeline de données OHLCV pour backtesting.

    Responsabilités:
        - Charger données SQLite via DataLoader (toujours actif)
        - Valider qualité via DataValidator  (activable/désactivable via config)
        - Traiter légèrement via DataProcessor.clean, mode LIGHT (activable/désactivable)
        - Retourner un DataFrame prêt pour trading_engine

    Configuration du pipeline (config/config.json) :
        engine_config.ohlcv_data_engine.data_validator.enabled  (bool, défaut: true)
        engine_config.ohlcv_data_engine.data_processor.enabled  (bool, défaut: true)

    Comportement par défaut (clé absente) : pipeline complet — rétrocompatibilité totale.

    Architecture:
        - Stateless  : load_and_validate() est une fonction pure (input → output)
        - Thread-safe: pas d'état mutable inter-appels
        - Délégation : aucune logique dupliquée depuis les modules spécialisés

    Attributes:
        config             (dict):                    Configuration complète.
        logger             (BulletLogger):            Logger centralisé singleton.
        data_loader        (DataLoader):              Module de chargement CSV (toujours présent).
        data_validator     (Optional[DataValidator]): Module de validation — None si désactivé.
        data_processor     (Optional[DataProcessor]): Module de traitement  — None si désactivé.
        _validator_enabled (bool):                    Flag activation validation.
        _processor_enabled (bool):                    Flag activation traitement.
    """

    def __init__(self, config: dict) -> None:
        """
        Initialise le pipeline de données.

        Lit les flags d'activation depuis config['engine_config']['ohlcv_data_engine'].
        N'instancie que les sous-modules réellement actifs (économie mémoire + init).

        Args:
            config: Configuration complète du bot (dict Python standard).
                    Clés requises :
                      config['general']['trading_pair']   — ex: 'BTC/USDT'
                      config['general']['timeframe']      — ex: '5m', '1h'
                      config['backtesting']['start_date'] — ex: '2025-01-01'
                      config['backtesting']['end_date']   — ex: '2025-12-31'
                    Clés optionnelles (défaut: true) :
                      config['engine_config']['ohlcv_data_engine']['data_validator']['enabled']
                      config['engine_config']['ohlcv_data_engine']['data_processor']['enabled']

        Raises:
            TypeError:  Si config n'est pas un dict.
            ValueError: Si une clé requise est absente ou vide.
        """
        self.logger = BulletLogger()

        self._validate_config(config)
        self.config = config

        # ── Lecture des flags avant instanciation ─────────────────────────────
        self._validator_enabled, self._processor_enabled = _read_pipeline_flags(config)

        # ── DataLoader : toujours instancié ───────────────────────────────────
        self.data_loader: DataLoader = DataLoader(config)

        # ── DataValidator : conditionnel ──────────────────────────────────────
        self.data_validator: Optional[DataValidator]
        if self._validator_enabled:
            self.data_validator = DataValidator()   # config_path=None → JSON dédié
        else:
            self.data_validator = None

        # ── DataProcessor : conditionnel ──────────────────────────────────────
        self.data_processor: Optional[DataProcessor]
        if self._processor_enabled:
            self.data_processor = DataProcessor(config)
        else:
            self.data_processor = None

        # ── Log de synthèse ───────────────────────────────────────────────────
        pipeline_summary = (
            f"loader=ON | "
            f"validator={'ON' if self._validator_enabled else 'OFF'} | "
            f"processor={'ON' if self._processor_enabled else 'OFF'}"
        )
        self.logger.info(
            f"✅ OHLCVDataEngine v2.3.0 initialized — "
            f"pair={config['general']['trading_pair']}, "
            f"timeframe={config['general']['timeframe']}, "
            f"period={config['backtesting']['start_date']} → "
            f"{config['backtesting']['end_date']}"
        )
        self.logger.info(f"   Pipeline : {pipeline_summary}")

    # =========================================================================
    # MÉTHODE PRINCIPALE
    # =========================================================================

    def load_and_validate(self) -> pd.DataFrame:
        """
        Charge, valide (si activé) et traite (si activé) les données OHLCV.

        Pipeline séquentiel dont les étapes 2 et 3 sont configurables :
            1. Chargement  → DataLoader.load()                     [toujours actif]
            2. Validation  → DataValidator.validate()              [si validator enabled]
            3. Nettoyage   → DataProcessor.clean() [LIGHT]         [si processor enabled]

        L'interface de retour est identique quel que soit le mode pipeline.
        engine.py n'a pas connaissance de ces flags — contrat public inchangé.

        Returns:
            pd.DataFrame: Colonnes garanties —
                          timestamp (datetime64[ns, UTC]), open (float64),
                          high (float64), low (float64), close (float64),
                          volume (float64). Index entier.

        Raises:
            FileNotFoundError: Fichier CSV introuvable.
            ValueError:        Données invalides (si validation activée).
            RuntimeError:      DataFrame vide en sortie de pipeline.
        """
        timeframe   = self.config["general"]["timeframe"]
        total_steps = 1 + int(self._validator_enabled) + int(self._processor_enabled)
        step        = 0

        self.logger.info("=" * 70)
        self.logger.info("DATA PIPELINE START")
        self.logger.info("=" * 70)

        # ── [v2.3.0 — DB-MIGRATION] Empreinte dataset SQLite ─────────────────
        # Remplace le hash SHA-256 du fichier CSV (supprimé lors de la migration
        # vers SQLite). L'empreinte est calculée depuis les métadonnées de la
        # table datasets (first_ts, last_ts, candle_count) — O(1), non bloquant.
        # Deux backtests avec la même empreinte utilisent exactement le même dataset.
        try:
            fingerprint = self.data_loader.get_db_fingerprint()
            db_path     = self.data_loader.get_db_path()
            if fingerprint:
                self.logger.info(
                    f"[DB-MIGRATION] Dataset : {db_path.name} | "
                    f"fingerprint={fingerprint}"
                )
            else:
                self.logger.warning(
                    "[DB-MIGRATION] Empreinte non calculée — dataset absent ou DB inaccessible."
                )
        except AttributeError:
            self.logger.debug(
                "[DB-MIGRATION] DataLoader.get_db_fingerprint() indisponible (non bloquant)."
            )
        except Exception as _fp_exc:
            self.logger.warning(
                f"[DB-MIGRATION] Empreinte échouée (non bloquant) : {_fp_exc}"
            )

        # ── [WARMUP-FIX] Calcul date de début étendue pour warmup ───────────
        # Si market_context est activé et market_context_min_candles > 0,
        # on recule start_date de N×timeframe pour que les bougies de warmup
        # soient physiquement chargées depuis le CSV. Sans cela, DataLoader
        # filtre strictement sur [start_date, end_date] et _extract_warmup_slice()
        # dans engine.py ne trouve rien à transmettre à TradingEngine.
        warmup_start_date = self._compute_warmup_start_date()

        # ── Étape 1 : Chargement (toujours actif) ────────────────────────────
        step += 1
        self.logger.info(f"Step {step}/{total_steps}: Loading raw data...")
        raw_data = self._step_load(warmup_start_date=warmup_start_date)
        self.logger.info(f"✓ Loaded {len(raw_data):,} candles")
        self._log_data_info(raw_data, label="Raw")

        # ── Étape 2 : Validation (conditionnelle) ────────────────────────────
        if self._validator_enabled:
            step += 1
            self.logger.info(f"Step {step}/{total_steps}: Validating quality...")
            self._step_validate(raw_data, timeframe=timeframe)
            self.logger.info("✓ Validation passed")
        else:
            self.logger.info("Step [skipped]: Validation disabled via config.")

        # ── Étape 3 : Traitement LIGHT (conditionnel) ─────────────────────────
        if self._processor_enabled:
            step += 1
            self.logger.info(f"Step {step}/{total_steps}: Processing data (LIGHT)...")
            # [v2.2.2 — FIX-ODE-5] Snapshot avant nettoyage pour comparaison.
            n_before = len(raw_data)
            processed_data = self._step_process(raw_data)
            n_after  = len(processed_data)
            n_removed = n_before - n_after
            if n_before > 0 and n_removed > n_before * 0.01:
                self.logger.warning(
                    f"⚠️  [FIX-ODE-5] DataProcessor.clean() a retiré {n_removed:,} bougies "
                    f"({n_removed / n_before * 100:.1f}% du dataset). "
                    f"Données sources de mauvaise qualité — vérifier le CSV."
                )
            self.logger.info(f"✓ Processing complete — {n_after:,} candles ready")
            self._log_data_info(processed_data, label="Ready")
        else:
            self.logger.info("Step [skipped]: Processor disabled via config — data used as-is.")
            processed_data = raw_data

        # ── Guard : DataFrame non vide ────────────────────────────────────────
        if processed_data.empty:
            raise RuntimeError(
                "Data pipeline produced an empty DataFrame. "
                "Check database content and backtesting date range in config."
            )

        self.logger.info("=" * 70)
        self.logger.info(
            f"DATA PIPELINE COMPLETE — {len(processed_data):,} candles ready for simulation"
        )
        self.logger.info("=" * 70)

        return processed_data

    # =========================================================================
    # ÉTAPES PIPELINE (privées)
    # =========================================================================

    def _compute_warmup_start_date(self) -> Optional[str]:
        """
        Calcule la date de début étendue pour inclure les bougies de warmup.

        Lit market_context_min_candles et timeframe depuis config pour calculer
        combien de temps avant backtesting.start_date il faut remonter.

        Rétrocompatibilité garantie :
            - market_context.enabled=false → retourne None (comportement original)
            - market_context_min_candles absent → défaut 300
            - timeframe inconnu → retourne None (warmup silencieusement désactivé)
            - start_date mal formée → retourne None (dégradation gracieuse)

        Returns:
            str 'YYYY-MM-DD' si warmup applicable, None sinon.
        """
        ec = self.config.get('engine_config', {})

        # Feature flag : si market_context désactivé, pas besoin de warmup
        mc_cfg     = ec.get('market_context', {})
        mc_enabled = str(mc_cfg.get('enabled', 'true')).strip().lower() == 'true'
        if not mc_enabled:
            self.logger.debug(
                "[WARMUP-FIX] market_context désactivé — "
                "chargement warmup ignoré."
            )
            return None

        warmup_n = int(ec.get('market_context_min_candles', 300))
        if warmup_n <= 0:
            self.logger.debug(
                "[WARMUP-FIX] market_context_min_candles=0 — "
                "chargement warmup ignoré."
            )
            return None

        tf_str     = self.config.get('general', {}).get('timeframe', '')
        tf_minutes = _timeframe_to_minutes(tf_str)
        if tf_minutes <= 0:
            self.logger.warning(
                f"[WARMUP-FIX] Timeframe '{tf_str}' inconnu — "
                f"impossible de calculer warmup_start_date. "
                f"Les premiers trades peuvent manquer d'origine_signal."
            )
            return None

        start_date_str = self.config.get('backtesting', {}).get('start_date', '')
        try:
            from datetime import datetime, timezone, timedelta as _timedelta
            start_dt     = datetime.strptime(start_date_str, '%Y-%m-%d').replace(
                               tzinfo=timezone.utc)
            warmup_delta = _timedelta(minutes=warmup_n * tf_minutes)
            warmup_start = start_dt - warmup_delta
            warmup_str   = warmup_start.strftime('%Y-%m-%d')

            self.logger.info(
                f"[WARMUP-FIX] start_date chargement étendu : "
                f"{start_date_str} → {warmup_str} "
                f"({warmup_n} bougies × {tf_minutes}min = "
                f"{warmup_n * tf_minutes // 1440}j "
                f"{(warmup_n * tf_minutes) % 1440 // 60}h)"
            )
            return warmup_str

        except (ValueError, TypeError) as exc:
            self.logger.warning(
                f"[WARMUP-FIX] Impossible de calculer warmup_start_date "
                f"(start_date='{start_date_str}') : {exc}. "
                f"Chargement sans warmup."
            )
            return None

    def _step_load(self, warmup_start_date: Optional[str] = None) -> pd.DataFrame:
        """
        Délègue le chargement CSV à DataLoader.

        DataLoader résout automatiquement le chemin, applique le filtre de dates
        et retourne des données brutes propres (types corrects, timestamps UTC).

        Args:
            warmup_start_date: Si fourni, remplace start_date pour inclure les
                               bougies de warmup antérieures à backtesting.start_date.
                               None → comportement original (filtre sur start_date config).

        Returns:
            pd.DataFrame: Données CSV brutes (inclut le warmup si warmup_start_date fourni).

        Raises:
            FileNotFoundError: CSV introuvable.
            ValueError:        Format invalide ou plage de dates incorrecte.
        """
        try:
            return self.data_loader.load(start_date=warmup_start_date)

        except ValueError as exc:
            # ValueError couvre : données absentes en DB, plage invalide, paramètres manquants
            err_msg = str(exc)
            if "Aucune donnée disponible" in err_msg or "absent" in err_msg.lower():
                self.logger.error(
                    "❌ Données introuvables en base SQLite. "
                    "Vérifiez trading_pair, timeframe et la plage de dates en config. "
                    "Lancez : python data/download_data_v3.0.py  "
                    "ou : python data/migrate_csv_to_db.py"
                )
            else:
                self.logger.error(f"❌ Erreur chargement données : {exc}")
            raise

        except RuntimeError as exc:
            self.logger.error(f"❌ Erreur base de données : {exc}")
            raise ValueError(str(exc)) from exc

    def _step_validate(self, df: pd.DataFrame, timeframe: Optional[str] = None) -> None:
        """
        Délègue la validation complète à DataValidator.

        Appelé uniquement si self._validator_enabled is True.
        En mode strict=True (défaut), tout problème structurel, NaN critique
        ou incohérence OHLCV lève une ValueError.
        Les anomalies volume/prix sont reportées en warning non bloquant.

        Args:
            df:        DataFrame brut à valider.
            timeframe: Timeframe actif pour la détection des gaps temporels.

        Raises:
            ValueError: Si is_valid=False dans le rapport de validation.
        """
        assert self.data_validator is not None  # Garanti par l'appelant (flag check)

        try:
            report = self.data_validator.validate(
                df,
                timeframe=timeframe,
                strict=True,
                warn_missing_timeframe=False,
            )
        except Exception as exc:
            self.logger.error(f"❌ Validator raised unexpected error: {exc}", exc_info=True)
            raise ValueError(f"Validation error: {exc}") from exc

        if not report.get("is_valid", False):
            total   = report.get("total_issues", "?")
            by_type = report.get("issues_by_type", {})
            recs    = report.get("recommendations", [])

            details = "\n".join(f"   • {k}: {v}" for k, v in by_type.items())
            self.logger.error(
                f"❌ Validation FAILED — {total} critical issue(s):\n{details}"
            )
            if recs:
                hints = "\n".join(f"   → {r}" for r in recs)
                self.logger.error(f"   Recommendations:\n{hints}")

            raise ValueError(
                f"OHLCV data validation failed: {total} issue(s) in "
                f"{list(by_type.keys())}. See logs for actionable details."
            )

        non_critical = report.get("total_issues", 0)
        if non_critical:
            by_type = report.get("issues_by_type", {})
            self.logger.warning(
                f"⚠️  Validation passed with {non_critical} non-critical warning(s): "
                f"{list(by_type.keys())}"
            )

    def _step_process(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Nettoyage LIGHT via DataProcessor.clean.

        Appelé uniquement si self._processor_enabled is True.

        Mode LIGHT uniquement :
            - Suppression lignes NaN sur colonnes OHLCV
            - Suppression valeurs infinies
            - Suppression incohérences OHLCV (high < low, prix négatifs, volume < 0)
            - Tri chronologique + déduplication timestamps
            - PAS de resampling (CSV déjà au bon timeframe)
            - PAS de feature engineering (responsabilité de la stratégie)

        Args:
            df: DataFrame (validé si étape validation active, brut sinon).

        Returns:
            pd.DataFrame: DataFrame nettoyé, trié, prêt pour simulation.

        Raises:
            Exception: Toute erreur propagée avec log d'erreur.
        """
        assert self.data_processor is not None  # Garanti par l'appelant (flag check)

        try:
            return self.data_processor.clean(df, aggressive=False, auto_validate=True)

        except Exception as exc:
            self.logger.error(f"❌ Data processing failed: {exc}", exc_info=True)
            raise

    # =========================================================================
    # MÉTHODES PRIVÉES UTILITAIRES
    # =========================================================================

    def _validate_config(self, config: dict) -> None:
        """
        Valide la structure de la configuration reçue.

        Args:
            config: Configuration à valider.

        Raises:
            TypeError:  Si config n'est pas un dict.
            ValueError: Si une ou plusieurs clés requises sont absentes ou vides.
        """
        if not isinstance(config, dict):
            raise TypeError(
                f"config must be a plain Python dict, got {type(config).__name__}. "
                "Make sure the caller passes config as a standard dict."
            )

        missing = [
            f"config['{section}']['{key}']"
            for section, key in _REQUIRED_KEYS
            if not config.get(section, {}).get(key)
        ]

        if missing:
            raise ValueError(
                "OHLCVDataEngine: missing or empty required config key(s):\n"
                + "\n".join(f"  • {m}" for m in missing)
            )

    def _log_data_info(self, df: pd.DataFrame, label: str = "") -> None:
        """
        Log DEBUG avec diagnostics du DataFrame (période, lignes, NaN, qualité).

        [v2.2.2 — FIX-ODE-2] Ajout du data_quality_ratio : ratio bougies OHLCV
        sans NaN / total. Un ratio < 0.99 émet un WARNING explicite — signal
        que les données contiennent des bougies forward-fillées ou corrompues
        susceptibles de biaiser les indicateurs techniques.

        Args:
            df:    DataFrame à inspecter.
            label: Label contextuel (ex: 'Raw', 'Ready').
        """
        if df.empty:
            self.logger.debug(f"[{label}] DataFrame is empty.")
            return

        prefix     = f"[{label}] " if label else ""
        ohlcv_cols = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
        nan_count  = int(df[ohlcv_cols].isna().sum().sum()) if ohlcv_cols else 0
        ts_col     = df["timestamp"] if "timestamp" in df.columns else None
        date_start = ts_col.iloc[0]  if ts_col is not None else "N/A"
        date_end   = ts_col.iloc[-1] if ts_col is not None else "N/A"

        # [v2.2.2 — FIX-ODE-2] Ratio qualité : bougies entièrement valides / total
        n_total = len(df)
        if ohlcv_cols and n_total > 0:
            n_valid = int(df[ohlcv_cols].notna().all(axis=1).sum())
            quality_ratio = round(n_valid / n_total, 4)
        else:
            n_valid       = n_total
            quality_ratio = 1.0

        self.logger.debug(
            f"{prefix}"
            f"rows={n_total:,} | "
            f"period={date_start} → {date_end} | "
            f"NaN={nan_count} | "
            f"quality={quality_ratio:.2%} ({n_valid:,}/{n_total:,} valid candles)"
        )

        # WARNING explicite si plus d'1% de bougies avec NaN OHLCV
        if quality_ratio < 0.99:
            self.logger.warning(
                f"⚠️  [FIX-ODE-2] {prefix}data_quality_ratio={quality_ratio:.2%} — "
                f"{n_total - n_valid:,} bougies avec valeurs NaN détectées. "
                f"Risque de biais indicateurs (forward-fill ou données corrompues)."
            )

# FIN DU MODULE