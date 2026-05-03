"""
BULLET-1 - Logger Module
=========================

Système de logging centralisé pour le projet BULLET-1.
Module fondamental (niveau 1) - Utilisé par TOUS les modules.

Version: 2.3.2
Date: 2026-02-26
Author: FuegoDev
"""

import logging
import sys
import json
import shutil
import warnings
from pathlib import Path
from typing import Optional, Dict, Any
from logging.handlers import RotatingFileHandler
from datetime import datetime, timedelta
from collections import deque
import threading

# trouvé la racine du projet
def find_project_root(marker_files=None):
    """
    Trouve la racine du projet en cherchant des fichiers marqueurs
    """
    if marker_files is None:
        marker_files = ['.git', 'pyproject.toml', 'setup.py', 'requirements.txt', '.gitignore']
    
    current_path = Path(__file__).resolve().parent
    
    # Remonte jusqu'à trouver un marqueur ou atteindre la racine du système
    for parent in [current_path] + list(current_path.parents):
        if any((parent / marker).exists() for marker in marker_files):
            return parent
    
    # Si aucun marqueur trouvé, retourne le répertoire du fichier
    return current_path

# Trouver et ajouter la racine du projet
project_root = find_project_root()
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))
    

# BULLET-1 IMPORTS
from src.utils.helpers import ensure_directory, format_datetime, get_project_root

try:
    import colorlog
    COLORLOG_AVAILABLE = True
except ImportError:
    COLORLOG_AVAILABLE = False
    warnings.warn(
        "Package 'colorlog' non installé - Logs console sans couleurs.\n"
        "Installation: pip install colorlog",
        category=ImportWarning,
        stacklevel=2
    )


# ============================================================================
# EXCEPTIONS
# ============================================================================

class ConfigurationError(Exception):
    """Exception levée en cas d'erreur de configuration."""
    pass


class ModeIncoherenceError(ConfigurationError):
    """Exception levée en cas d'incohérence entre modes configurés."""
    pass


# ============================================================================
# CONFIGURATION GLOBALE
# ============================================================================

LOG_COLORS = {
    'DEBUG': 'cyan',
    'INFO': 'green',
    'WARNING': 'yellow',
    'ERROR': 'red',
    'CRITICAL': 'bold_red',
}

LOG_FORMAT_CONSOLE = (
    '%(log_color)s%(levelname)-8s%(reset)s | '
    '%(cyan)s%(asctime)s%(reset)s | '
    '%(blue)s%(name)s%(reset)s | '
    '%(message)s'
)

LOG_FORMAT_FILE = (
    '%(levelname)-8s | %(asctime)s | %(name)s | '
    '%(filename)s:%(lineno)d | %(message)s'
)

DATE_FORMAT = '%Y-%m-%d %H:%M:%S'

DEFAULT_CONFIG = {
    'general': {'mode': 'BACKTEST'},
    'logging': {
        'level': 'auto',
        'console': True,
        'colorize_console': True,
        'include_timestamp': True,
        'include_module_name': True,
        'files': {
            'main_log': 'logs/bullet1.log',
            'trading_log': 'logs/trading.log',
            'error_log': 'logs/errors.log',
            'api_log': 'logs/api.log',
            'session_log': 'logs/sessions.log',
            'circuit_breaker_log': 'logs/circuit_breaker.log',
            'backtest_log': 'logs/backtest.log'
        },
        'rotation': {
            'max_bytes': 10485760,
            'backup_count': 10,
            'compress_old_logs': False
        },
        'retention': {
            'enabled': True,
            'days': 30,
            'auto_cleanup': True
        },
        'live_mode': {
            'flush_critical': True,
            'alert_threshold': 5,
            'alert_window_minutes': 5,
            'auto_backup': True,
            'backup_interval_hours': 6
        }
    }
}

MODE_CONFIGS = {
    'BACKTEST': {
        'level': 'DEBUG',
        'rotation': {'max_bytes': 10485760, 'backup_count': 5},
        'primary_log': 'backtest_log'
    },
    'PAPER': {
        'level': 'INFO',
        'rotation': {'max_bytes': 5242880, 'backup_count': 10},
        'primary_log': 'trading_log'
    },
    'LIVE': {
        # [v2.3.0 — FIX L-2] 'WARNING' → 'INFO' : en mode LIVE, les événements
        # INFO (ouverture/fermeture position, settle_trade, balance update) sont
        # critiques pour l'auditabilité opérationnelle et réglementaire.
        # Supprimer INFO en production revenait à trader en aveugle sur les logs.
        'level': 'INFO',
        'rotation': {'max_bytes': 3145728, 'backup_count': 15},
        'primary_log': 'trading_log'
    }
}


# ============================================================================
# CLASSE BULLETLOGGER (SINGLETON, THREAD-SAFE)
# ============================================================================

class BulletLogger:
    """
    Système de logging centralisé pour BULLET-1.
    
    Pattern Singleton thread-safe avec RLock.
    
    Lock Hierarchy (ordre acquisition strict pour éviter deadlocks):
    1. _lock (RLock) - Niveau module (singleton)
    2. error_lock (Lock) - Niveau instance (tracking erreurs)
    RÈGLE: Toujours acquérir _lock AVANT error_lock si les deux requis.
    
    Raises:
        ModeIncoherenceError: Si modes logger_config.json et config.json incohérents
        ConfigurationError: Si erreur lecture/validation configuration
    """
    
    _instance: Optional['BulletLogger'] = None
    _initialized: bool = False
    _lock = threading.RLock()
    
    def __new__(cls, config: Optional[Dict] = None):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(BulletLogger, cls).__new__(cls)
        return cls._instance
    
    def __init__(self, config: Optional[Dict] = None):
        if self._initialized:
            return
        
        with self._lock:
            if self._initialized:
                return
            
            self.config = self._load_config(config)
            self.mode = self._detect_mode()
            self._verify_mode_coherence()
            self._apply_mode_config()
            self._validate_config()
            
            self.logger = logging.getLogger('BULLET-1')
            self.logger.setLevel(self._get_log_level(self.config['logging']['level']))
            self.logger.propagate = False
            self.logger.handlers.clear()
            
            self.handlers: Dict[str, logging.Handler] = {}
            self.error_tracker: deque = deque(maxlen=100)
            self.error_lock = threading.Lock()
            self.last_backup: Optional[datetime] = None
            self._closed: bool = False
            
            if self.config['logging']['console']:
                self._setup_console_handler()
            
            self._setup_file_handlers()
            
            if self.config['logging']['retention']['enabled'] and \
               self.config['logging']['retention']['auto_cleanup']:
                self._cleanup_old_logs()
            
            type(self)._initialized = True  # [v2.3.1 — FIX L-4] cls attr, évite le shadow instance
            self.info(f"🚀 BulletLogger initialized - Mode: {self.mode}")
            self.debug(f"Config source: config/logger_config.json")
    
    def _load_config(self, custom_config: Optional[Dict] = None) -> Dict:
        """
        Charger configuration depuis logger_config.json ou defaults.
        
        Politique par mode:
        - LIVE: logger_config.json REQUIS (erreur fatale si absent)
        - PAPER/BACKTEST: Fallback gracieux avec warning
        
        Raises:
            ConfigurationError: Si config requise mais absente/invalide (LIVE)
        """
        config = DEFAULT_CONFIG.copy()
        
        temp_mode = config.get('general', {}).get('mode', 'BACKTEST').upper()
        if custom_config and 'general' in custom_config:
            temp_mode = custom_config.get('general', {}).get('mode', temp_mode).upper()
        
        logger_config_path = get_project_root() / 'config' / 'logger_config.json'
        
        if logger_config_path.exists():
            try:
                with open(logger_config_path, 'r', encoding='utf-8') as f:
                    full_config = json.load(f)
                    config = self._deep_merge(config, full_config)
            except json.JSONDecodeError as e:
                error_msg = f"Erreur parsing logger_config.json: {e}"
                if temp_mode == 'LIVE':
                    raise ConfigurationError(
                        f"🚨 ERREUR CRITIQUE: {error_msg}\n"
                        "Configuration valide requise en mode LIVE."
                    )
                else:
                    warnings.warn(f"{error_msg}\nUtilisation config par défaut.", category=UserWarning)
            except Exception as e:
                error_msg = f"Erreur lecture logger_config.json: {e}"
                if temp_mode == 'LIVE':
                    raise ConfigurationError(f"🚨 ERREUR CRITIQUE: {error_msg}")
                else:
                    warnings.warn(f"{error_msg}\nUtilisation config par défaut.", category=UserWarning)
        else:
            if temp_mode == 'LIVE':
                raise ConfigurationError(
                    f"🚨 ERREUR CRITIQUE: logger_config.json REQUIS en mode LIVE!\n"
                    f"Fichier attendu: {logger_config_path}"
                )
            else:
                warnings.warn(
                    f"logger_config.json absent: {logger_config_path}\n"
                    "Utilisation config par défaut.",
                    category=UserWarning
                )
        
        if custom_config:
            config = self._deep_merge(config, custom_config)
        
        return config
    
    def _deep_merge(self, base: Dict, update: Dict) -> Dict:
        """Fusionner deux dictionnaires récursivement (ignore clés préfixées '_')."""
        result = base.copy()
        for key, value in update.items():
            if key.startswith('_'):
                continue
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = self._deep_merge(result[key], value)
            else:
                result[key] = value
        return result
    
    def _detect_mode(self) -> str:
        """Détecter mode opérationnel depuis config."""
        mode = self.config.get('general', {}).get('mode', 'BACKTEST').upper()
        if mode not in ['BACKTEST', 'PAPER', 'LIVE']:
            warnings.warn(
                f"Mode invalide '{mode}' - Utilisation 'BACKTEST' par défaut.",
                category=UserWarning
            )
            mode = 'BACKTEST'
        return mode
    
    def _verify_mode_coherence(self):
        """
        Vérifier cohérence modes logger_config.json vs config.json.
        
        Politique par mode:
        - LIVE: config.json REQUIS (erreur fatale)
        - PAPER: FORTEMENT RECOMMANDÉ (warning)
        - BACKTEST: OPTIONNEL (tolérance)
        
        Raises:
            ModeIncoherenceError: Si modes différents
            ConfigurationError: Si config.json requis mais absent (LIVE)
        """
        try:
            mode_logger = self.mode
            main_config_path = get_project_root() / 'config' / 'config.json'
            
            if not main_config_path.exists():
                if self.mode == 'LIVE':
                    raise ConfigurationError(
                        "🚨 ERREUR CRITIQUE: config.json REQUIS en mode LIVE!\n"
                        f"Fichier attendu: {main_config_path}"
                    )
                elif self.mode == 'PAPER':
                    warnings.warn(
                        f"config.json absent: {main_config_path}\n"
                        "FORTEMENT RECOMMANDÉ en mode PAPER.",
                        category=UserWarning
                    )
                    return
                else:
                    return
            
            with open(main_config_path, 'r', encoding='utf-8') as f:
                main_config = json.load(f)
            
            mode_main = main_config.get('general', {}).get('mode', '').upper()
            
            if not mode_main:
                if self.mode == 'LIVE':
                    raise ConfigurationError(
                        "🚨 ERREUR CRITIQUE: Mode non trouvé dans config.json!"
                    )
                else:
                    warnings.warn("Mode non trouvé dans config.json.", category=UserWarning)
                    return
            
            if mode_logger != mode_main:
                error_msg = (
                    f"\n{'='*80}\n"
                    f"🚨 ERREUR CONFIGURATION - INCOHÉRENCE MODES DÉTECTÉE\n"
                    f"{'='*80}\n\n"
                    f"  📄 config/logger_config.json → mode = '{mode_logger}'\n"
                    f"  📄 config/config.json        → mode = '{mode_main}'\n\n"
                    f"{'='*80}\n"
                    f"💡 SOLUTION: Synchronisez les deux fichiers\n"
                    f"{'='*80}\n"
                )
                raise ModeIncoherenceError(error_msg)
        
        except json.JSONDecodeError as e:
            if self.mode == 'LIVE':
                raise ConfigurationError(f"🚨 ERREUR CRITIQUE: Erreur parsing config.json: {e}")
            else:
                warnings.warn(f"Erreur parsing config.json: {e}", category=UserWarning)
        except (ModeIncoherenceError, ConfigurationError):
            raise
        except Exception as e:
            if self.mode == 'LIVE':
                raise ConfigurationError(f"🚨 ERREUR CRITIQUE: {e}")
            else:
                warnings.warn(f"Erreur vérification cohérence: {e}", category=UserWarning)
    
    def _validate_config(self):
        """
        Valider cohérence configuration interne.
        
        Raises:
            ConfigurationError: Si config invalide
        """
        max_bytes = self.config['logging']['rotation']['max_bytes']
        backup_count = self.config['logging']['rotation']['backup_count']
        
        if max_bytes < 1048576:
            warnings.warn(
                f"Rotation max_bytes faible: {max_bytes} bytes. Recommandé: >= 1 MB",
                category=UserWarning
            )
        
        if backup_count < 1:
            raise ConfigurationError(f"backup_count invalide: {backup_count} (doit être >= 1)")
        
        if self.mode == 'LIVE':
            threshold = self.config['logging']['live_mode']['alert_threshold']
            window = self.config['logging']['live_mode']['alert_window_minutes']
            
            if threshold <= 0 or window <= 0:
                raise ConfigurationError(
                    f"Config LIVE invalide: alert_threshold={threshold}, window={window}"
                )
            
            if threshold > 50:
                warnings.warn(
                    f"alert_threshold élevé: {threshold}. Recommandé: <= 20",
                    category=UserWarning
                )
        
        if self.config['logging']['retention']['enabled']:
            days = self.config['logging']['retention']['days']
            if days < 1:
                raise ConfigurationError(f"retention.days invalide: {days} (doit être >= 1)")
            if days > 365:
                warnings.warn(f"Rétention longue: {days} jours", category=UserWarning)
    
    def _apply_mode_config(self):
        """Appliquer configuration spécifique au mode."""
        if self.mode not in MODE_CONFIGS:
            return
        
        mode_config = MODE_CONFIGS[self.mode]
        
        if self.config['logging']['level'] == 'auto':
            self.config['logging']['level'] = mode_config['level']
        
        self.config['logging']['rotation'].update(mode_config['rotation'])
        self.primary_log = mode_config['primary_log']
    
    def _get_log_level(self, level_str: str) -> int:
        """Convertir string niveau en constante logging."""
        levels = {
            'DEBUG': logging.DEBUG,
            'INFO': logging.INFO,
            'WARNING': logging.WARNING,
            'ERROR': logging.ERROR,
            'CRITICAL': logging.CRITICAL
        }
        return levels.get(level_str.upper(), logging.INFO)
    
    def _setup_console_handler(self):
        """Configurer handler console avec couleurs (si disponible)."""
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(self._get_log_level(self.config['logging']['level']))
        
        if COLORLOG_AVAILABLE and self.config['logging']['colorize_console']:
            formatter = colorlog.ColoredFormatter(
                LOG_FORMAT_CONSOLE,
                datefmt=DATE_FORMAT,
                log_colors=LOG_COLORS,
                reset=True,
                style='%'
            )
        else:
            formatter = logging.Formatter(
                '%(levelname)-8s | %(asctime)s | %(name)s | %(message)s',
                datefmt=DATE_FORMAT
            )
        
        console_handler.setFormatter(formatter)
        self.logger.addHandler(console_handler)
        self.handlers['console'] = console_handler
    
    def _setup_file_handlers(self):
        """
        Configurer handlers fichiers avec rotation.

        [v2.3.2 — FIX L-5] Isolation par handler : un échec sur un fichier
        (répertoire non créable, permissions, disque plein) n'interrompt plus
        la boucle. Les handlers restants sont créés, l'erreur est reportée sur
        la console (seul canal garanti disponible à ce stade).
        """
        project_root = get_project_root()
        
        for log_name, log_path_str in self.config['logging']['files'].items():
            if self.mode == 'BACKTEST' and log_name not in ['main_log', 'backtest_log', 'error_log', 'session_log']:
                continue
            if self.mode in ['PAPER', 'LIVE'] and log_name == 'backtest_log':
                continue
            
            try:
                log_path = project_root / log_path_str
                ensure_directory(log_path.parent)  # [v2.3.2 — FIX L-5] isolé dans try
                
                handler = RotatingFileHandler(
                    filename=log_path,
                    maxBytes=self.config['logging']['rotation']['max_bytes'],
                    backupCount=self.config['logging']['rotation']['backup_count'],
                    encoding='utf-8'
                )
                
                handler.setLevel(logging.WARNING if 'error' in log_name else logging.DEBUG)
                
                formatter = logging.Formatter(LOG_FORMAT_FILE, datefmt=DATE_FORMAT)
                handler.setFormatter(formatter)
                
                self.logger.addHandler(handler)
                self.handlers[log_name] = handler

            except Exception as e:
                # Console handler déjà actif à ce stade — warning garanti visible.
                # On ne lève pas : les autres handlers doivent être créés.
                warnings.warn(
                    f"[BulletLogger] Handler '{log_name}' non créé ({log_path_str}): {e}",
                    RuntimeWarning,
                    stacklevel=2
                )
    
    def _cleanup_old_logs(self):
        """Supprimer logs plus anciens que retention configurée."""
        try:
            retention_days = self.config['logging']['retention']['days']
            cutoff_timestamp = (datetime.now() - timedelta(days=retention_days)).timestamp()
            
            log_dir = get_project_root() / 'logs'
            if not log_dir.exists():
                return
            
            deleted_count = 0
            for log_file in log_dir.glob('*.log*'):
                if log_file.is_file() and log_file.stat().st_mtime < cutoff_timestamp:
                    try:
                        log_file.unlink()
                        deleted_count += 1
                        self.debug(f"🗑️  Supprimé: {log_file.name}")
                    except Exception as e:
                        self.warning(f"Impossible supprimer {log_file.name}: {e}")
            
            if deleted_count > 0:
                self.info(f"🗑️  Cleanup: {deleted_count} fichier(s) supprimé(s)")
        except Exception as e:
            self.warning(f"Erreur cleanup logs: {e}")
    
    def _check_critical_alert(self):
        """
        Vérifier seuil erreurs critiques (LIVE uniquement).
        Thread-safe via error_lock.
        """
        if self.mode != 'LIVE':
            return
        
        try:
            with self.error_lock:
                now = datetime.now()
                window_minutes = self.config['logging']['live_mode']['alert_window_minutes']
                window_start = now - timedelta(minutes=window_minutes)
                recent_errors = [ts for ts in self.error_tracker if ts >= window_start]
                threshold = self.config['logging']['live_mode']['alert_threshold']
                
                if len(recent_errors) >= threshold:
                    self.logger.critical(
                        f"🚨 ALERTE: {len(recent_errors)} erreurs en {window_minutes}min! "
                        f"Seuil: {threshold}"
                    )
                    self._flush_all_handlers()
                    if self.config['logging']['live_mode']['auto_backup']:
                        self._backup_critical_logs()
        except Exception as e:
            self.logger.warning(f"Erreur vérification alertes: {e}")
    
    def _flush_all_handlers(self):
        """Forcer flush tous handlers (LIVE). Thread-safe natif."""
        for handler in self.logger.handlers:
            try:
                handler.flush()
            except Exception:
                pass
    
    def _backup_critical_logs(self):
        """
        Backup automatique errors.log (LIVE uniquement).
        Thread-safe, idempotent.
        """
        if self.mode != 'LIVE':
            return
        
        try:
            now = datetime.now()
            interval_hours = self.config['logging']['live_mode']['backup_interval_hours']
            
            if self.last_backup:
                delta = now - self.last_backup
                if delta < timedelta(hours=interval_hours):
                    return
            
            error_log_path = get_project_root() / self.config['logging']['files']['error_log']
            if not error_log_path.exists():
                return
            
            backup_dir = get_project_root() / 'logs' / 'backups'
            ensure_directory(backup_dir)
            
            timestamp = now.strftime('%Y%m%d_%H%M%S')
            backup_path = backup_dir / f'errors_{timestamp}.log'
            
            shutil.copy2(error_log_path, backup_path)
            
            if not backup_path.exists() or backup_path.stat().st_size == 0:
                raise OSError(f"Backup invalide: {backup_path}")
            
            self.last_backup = now
            self.info(f"📦 Backup → {backup_path.name}")
        except Exception as e:
            self.logger.warning(f"Erreur backup: {e}", exc_info=True)
    
    # ========================================================================
    # API PUBLIQUE - MÉTHODES DE LOGGING
    # ========================================================================
    
    def debug(self, message: str, *args, **kwargs):
        """Log niveau DEBUG."""
        self.logger.debug(message, *args, **kwargs)
    
    def info(self, message: str, *args, **kwargs):
        """Log niveau INFO."""
        self.logger.info(message, *args, **kwargs)
    
    def warning(self, message: str, *args, **kwargs):
        """Log niveau WARNING."""
        self.logger.warning(message, *args, **kwargs)
    
    def error(self, message: str, *args, **kwargs):
        """
        Log niveau ERROR.
        LIVE: Tracking auto, flush synchrone, vérif alertes.
        """
        self.logger.error(message, *args, **kwargs)
        
        if self.mode == 'LIVE':
            with self.error_lock:
                self.error_tracker.append(datetime.now())
            
            if self.config['logging']['live_mode']['flush_critical']:
                self._flush_all_handlers()
            
            self._check_critical_alert()
    
    def critical(self, message: str, *args, **kwargs):
        """
        Log niveau CRITICAL.
        LIVE: Flush + backup immédiat.
        """
        self.logger.critical(message, *args, **kwargs)
        
        if self.mode == 'LIVE':
            self._flush_all_handlers()
            if self.config['logging']['live_mode']['auto_backup']:
                self._backup_critical_logs()
    
    def exception(self, message: str, *args, **kwargs):
        """Log exception avec traceback complet. Doit être appelé dans except."""
        self.logger.exception(message, *args, **kwargs)
        
        if self.mode == 'LIVE':
            with self.error_lock:
                self.error_tracker.append(datetime.now())
            
            if self.config['logging']['live_mode']['flush_critical']:
                self._flush_all_handlers()
            
            self._check_critical_alert()
    
    # ========================================================================
    # API PUBLIQUE - MÉTHODES UTILITAIRES
    # ========================================================================
    
    def set_level(self, level: str):
        """Changer niveau de log dynamiquement."""
        new_level = self._get_log_level(level)
        self.logger.setLevel(new_level)
        
        for handler_name, handler in self.handlers.items():
            if handler_name != 'error_log':
                handler.setLevel(new_level)
        
        self.config['logging']['level'] = level.upper()
        self.info(f"Log level changé: {level}")
    
    def get_handlers(self) -> Dict[str, logging.Handler]:
        """Obtenir dictionnaire handlers actifs."""
        return self.handlers.copy()
    
    def get_mode(self) -> str:
        """Obtenir mode opérationnel actuel."""
        return self.mode
    
    def get_status(self) -> Dict[str, Any]:
        """
        Obtenir état complet logger (monitoring/debug).
        
        Returns:
            dict: État détaillé (version, mode, handlers, closed, error_count_5min, last_backup)
        """
        status = {
            'version': '2.3.2',
            'mode': self.mode,
            'level': self.config['logging']['level'],
            'handlers': list(self.handlers.keys()),
            'closed': self._closed,
            'initialized': self._initialized
        }
        
        if self.mode == 'LIVE':
            status['error_count_5min'] = self.get_error_count(5)
            status['last_backup'] = self.last_backup.isoformat() if self.last_backup else None
        
        return status
    
    def get_error_count(self, minutes: int = 5) -> int:
        """Obtenir nombre erreurs dans fenêtre temporelle (LIVE)."""
        if self.mode != 'LIVE':
            return 0
        
        with self.error_lock:
            now = datetime.now()
            window_start = now - timedelta(minutes=minutes)
            recent_errors = [ts for ts in self.error_tracker if ts >= window_start]
            return len(recent_errors)
    
    def force_backup(self):
        """Forcer backup immédiat logs critiques (LIVE), ignore interval."""
        if self.mode == 'LIVE':
            old_last_backup = self.last_backup
            self.last_backup = None
            self._backup_critical_logs()
            if self.last_backup is None:
                self.last_backup = old_last_backup
    
    def close_handlers(self):
        """
        Fermer handlers SANS reset singleton (safe multi-thread).
        Pour reset complet, utiliser reset_singleton().
        """
        if self._closed:
            self.warning("Handlers déjà fermés")
            return

        # [v2.3.0 — FIX L-3] Log émis EN PREMIER, avant toute fermeture de handler.
        # L'ancienne implémentation loggait APRÈS removeHandler() + _closed=True,
        # rendant le message silencieux (aucun handler actif pour le recevoir).
        self.info("Logger handlers closing — flush en cours")
        self._flush_all_handlers()

        for handler in self.logger.handlers[:]:
            handler.close()
            self.logger.removeHandler(handler)

        self.handlers.clear()
        self._closed = True
    
    @classmethod
    def reset_singleton(cls):
        """
        Reset complet singleton (UNIQUEMENT tests/redémarrage).
        ATTENTION: Invalide toutes références existantes.
        
        Raises:
            RuntimeError: Si handlers non fermés
        """
        with cls._lock:
            if cls._instance and not getattr(cls._instance, '_closed', False):
                raise RuntimeError(
                    "Impossible reset singleton: handlers actifs.\n"
                    "Appelez close_handlers() d'abord."
                )
            cls._instance = None
            cls._initialized = False
    
    def log_separator(self, level: str = 'INFO', char: str = '=', length: int = 80):
        """Logger ligne de séparation visuelle."""
        separator = char * length
        log_method = getattr(self, level.lower(), self.info)
        log_method(separator)
    
    def log_dict(self, data: Dict, title: str = "Data", level: str = 'DEBUG'):
        """Logger dictionnaire formaté."""
        log_method = getattr(self, level.lower(), self.debug)
        log_method(f"{title}:")
        for key, value in data.items():
            log_method(f"  {key}: {value}")
    
    def log_session_start(self, session_n: int, capital: float, 
                         start_date: str, end_date: str):
        """Logger démarrage session (format standardisé)."""
        self.log_separator('INFO', '=', 80)
        self.info(f"📊 SESSION {session_n} STARTED")
        self.info(f"   Capital: {capital:.2f} USDT")
        self.info(f"   Period: {start_date} → {end_date}")
        self.log_separator('INFO', '=', 80)
    
    def log_session_end(self, session_n: int, pnl: float, pnl_pct: float,
                       trades: int, win_rate: float, reason: str):
        """Logger fin session (format standardisé)."""
        self.log_separator('INFO', '=', 80)
        self.info(f"📊 SESSION {session_n} ENDED ({reason})")
        self.info(f"   PnL: {pnl:+.2f} USDT ({pnl_pct:+.2f}%)")
        self.info(f"   Trades: {trades} | Win Rate: {win_rate:.2f}%")
        self.log_separator('INFO', '=', 80)
    
    def log_trade_open(self, trade_id: str, side: str, entry_price: float,
                      size: float, sl_price: float, tp_price: float):
        """Logger ouverture trade (format standardisé)."""
        self.info(f"🔵 TRADE OPEN: {trade_id}")
        self.info(f"   Side: {side} | Entry: {entry_price:.2f}")
        self.info(f"   Size: {size:.6f} BTC")
        self.info(f"   SL: {sl_price:.2f} | TP: {tp_price:.2f}")
    
    def log_trade_close(self, trade_id: str, exit_price: float, pnl: float,
                       pnl_pct: float, reason: str):
        """Logger fermeture trade (format standardisé)."""
        emoji = "🟢" if pnl > 0 else "🔴"
        self.info(f"{emoji} TRADE CLOSE: {trade_id} ({reason})")
        self.info(f"   Exit: {exit_price:.2f}")
        self.info(f"   PnL: {pnl:+.2f} USDT ({pnl_pct:+.2f}%)")


# ============================================================================
# FONCTION UTILITAIRE GLOBALE
# ============================================================================

def get_logger(name: str = 'BULLET-1') -> logging.Logger:
    """Obtenir logger standard Python (pas le singleton BulletLogger)."""
    return logging.getLogger(name)

# FIN DU MODULE