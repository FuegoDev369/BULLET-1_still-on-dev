"""
BULLET-1 - Helpers Module
=========================

Fonctions utilitaires génériques pour le projet BULLET-1.
Module fondamental (niveau 0) - Aucune dépendance.

Ce module fournit des utilitaires réutilisables pour :
- Conversions dates/timestamps
- Formatage nombres (prix, volumes, pourcentages)
- Calculs mathématiques
- Gestion fichiers/dossiers
- Validation données
- Utilitaires strings (snake_case, camelCase, sanitization)
- Gestion erreurs communes
- Validation cohérence des modes de configuration (v2.4.0)

Version: 2.4.0
Date: 2026-03-01
Dépendances: AUCUNE (stdlib uniquement)

Auteur: FuegoDev
"""

import os
import re
import json
import uuid
import math
import shutil
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Union, Optional, Any, Dict, List
from decimal import Decimal, ROUND_HALF_UP

# ============================================================================
# CONSTANTES GLOBALES
# ============================================================================

# Modes d'exécution valides pour tout le système BULLET-1.
# Utilisé par verify_mode_consistency() et les modules indicateurs.
VALID_MODES: frozenset = frozenset({"backtest", "paper", "live"})


# ============================================================================
# EXCEPTIONS DÉDIÉES — VALIDATION DES MODES  [v2.4.0]
# ============================================================================

class ModeMissingError(ValueError):
    """
    Levée quand la clé 'mode' est absente d'un fichier de configuration.
    Hérite de ValueError pour compatibilité avec les blocs except existants.
    """


class ModeInvalidError(ValueError):
    """
    Levée quand la valeur de 'mode' n'est pas dans VALID_MODES.
    Hérite de ValueError pour compatibilité avec les blocs except existants.
    """


class ModeInconsistencyError(ValueError):
    """
    Levée quand le mode d'un fichier de config de module diffère du mode
    défini dans config/config.json.
    Hérite de ValueError pour compatibilité avec les blocs except existants.
    """



# ============================================================================
# CONVERSIONS DATES & TIMESTAMPS
# ============================================================================

def timestamp_to_datetime(ts: Union[int, float, str, datetime]) -> datetime:
    """
    Convertir timestamp (ms ou s) ou datetime en objet datetime UTC.
    
    Args:
        ts: Timestamp en millisecondes, secondes, string ISO, ou datetime
    
    Returns:
        datetime: Objet datetime en UTC
    
    Raises:
        ValueError: Si format timestamp invalide
    """
    try:
        # NOUVEAU: Si déjà datetime, normaliser UTC et retourner
        if isinstance(ts, datetime):
            # Si pas de timezone, ajouter UTC
            if ts.tzinfo is None:
                return ts.replace(tzinfo=timezone.utc)
            # Si autre timezone, convertir en UTC
            return ts.astimezone(timezone.utc)
        
        # Si string, parser format ISO
        if isinstance(ts, str):
            # Essayer plusieurs formats (ordre: plus spécifique → moins spécifique)
            formats = [
                "%Y-%m-%dT%H:%M:%S.%fZ",      # ISO avec microsecondes + Z
                "%Y-%m-%dT%H:%M:%SZ",         # ISO avec Z
                "%Y-%m-%dT%H:%M:%S.%f%z",     # ISO avec microsecondes + timezone
                "%Y-%m-%dT%H:%M:%S%z",        # ISO avec timezone (+00:00)
                "%Y-%m-%dT%H:%M:%S.%f",       # ISO avec microsecondes
                "%Y-%m-%dT%H:%M:%S",          # ISO basique
                "%Y-%m-%d %H:%M:%S",          # Format classique
                "%Y-%m-%d"                    # Date seule
            ]
            
            for fmt in formats:
                try:
                    dt = datetime.strptime(ts, fmt)
                    # Si pas de timezone, ajouter UTC
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    # Convertir en UTC si autre timezone
                    return dt.astimezone(timezone.utc)
                except ValueError:
                    continue
            
            raise ValueError(f"Format date non reconnu: {ts}")
        
        # Si numérique, déterminer ms vs s
        ts_num = float(ts)
        
        # Si > 1e12, c'est probablement en millisecondes
        if ts_num > 1e12:
            ts_num = ts_num / 1000
        
        return datetime.fromtimestamp(ts_num, tz=timezone.utc)
    
    except Exception as e:
        raise ValueError(f"Erreur conversion timestamp {ts}: {e}")


def datetime_to_timestamp(dt: datetime, milliseconds: bool = True) -> int:
    """
    Convertir datetime en timestamp.
    
    Note: Les datetime naïfs (sans timezone) sont supposés UTC.
          Utilisez des datetime timezone-aware pour éviter toute ambiguïté.
    
    Args:
        dt: Objet datetime (naïf ou timezone-aware)
        milliseconds: Si True, retourne ms, sinon secondes
    
    Returns:
        int: Timestamp en ms ou s
    
    Raises:
        TypeError: Si dt n'est pas un datetime
    """
    if not isinstance(dt, datetime):
        raise TypeError(f"Expected datetime, got {type(dt)}")
    
    # Datetime naïf → on suppose UTC (cohérent avec le reste du module)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    
    ts = int(dt.timestamp())
    return ts * 1000 if milliseconds else ts


def format_datetime(dt: datetime, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """
    Formater datetime en string.
    
    Note: Cette fonction remplace l'ancienne timestamp_to_str (consolidation)
    
    Args:
        dt: Objet datetime
        fmt: Format string (défaut: YYYY-MM-DD HH:MM:SS)
    
    Returns:
        str: Date formatée
    
    Raises:
        TypeError: Si dt n'est pas un datetime
    """
    if not isinstance(dt, datetime):
        raise TypeError(f"Expected datetime, got {type(dt)}")
    
    return dt.strftime(fmt)


def str_to_datetime(date_str: str, fmt: str = "%Y-%m-%d %H:%M:%S", 
                    assume_utc: bool = True) -> datetime:
    """
    Convertir string en datetime timezone-aware.
    
    Note: Cette fonction remplace l'ancienne str_to_timestamp (consolidation)
    
    Args:
        date_str: String date
        fmt: Format attendu
        assume_utc: Si True (défaut), les dates sans timezone sont supposées UTC.
                    Si False, retourne un datetime naïf.
    
    Returns:
        datetime: Objet datetime (UTC si assume_utc=True et format sans %z)
    
    Raises:
        TypeError: Si date_str est None
        ValueError: Si format invalide
    """
    if date_str is None:
        raise TypeError("date_str ne peut pas être None")
    
    dt = datetime.strptime(date_str, fmt)
    
    # Si le format ne contient pas de timezone et assume_utc est True → ajouter UTC
    if dt.tzinfo is None and assume_utc:
        dt = dt.replace(tzinfo=timezone.utc)
    
    return dt


def parse_timeframe_to_minutes(timeframe: str) -> int:
    """
    Convertit un timeframe string en minutes entier.

    [v2.4.1 — FIX-DEDUP-TF] Source de vérité unique — remplace les
    implémentations dupliquées historiquement dans
    src/backtesting/engine.py (_parse_timeframe_minutes) et
    src/backtesting/ohlcv_data_engine.py (_timeframe_to_minutes),
    strictement identiques. La justification initiale de la duplication
    (risque d'import circulaire engine.py <-> ohlcv_data_engine.py) a été
    vérifiée inexistante : ohlcv_data_engine.py n'importe rien de
    engine.py. helpers.py n'a de dépendance ni vers l'un ni vers l'autre
    (module fondamental niveau 0), c'est donc l'emplacement neutre
    approprié.

    Ne pas confondre avec parse_timeframe() ci-dessous : cette dernière
    retourne des SECONDES, est case-sensitive ('m' vs 'M' pour minute vs
    mois) et lève ValueError sur un format invalide. parse_timeframe_to_minutes()
    retourne des MINUTES, est case-insensitive, et dégrade silencieusement
    sur un format inconnu (retourne 0) — comportement requis par les
    appelants historiques (warmup désactivé silencieusement plutôt qu'une
    exception en pleine boucle de backtest).

    Args:
        timeframe: Format '1m', '5m', '15m', '1h', '4h', '1d', '1w', etc.

    Returns:
        int: Nombre de minutes. 0 si le format est inconnu (dégradation
            silencieuse — warmup désactivé plutôt qu'une exception).
    """
    tf = timeframe.strip().lower()
    _KNOWN: Dict[str, int] = {
        '1m': 1, '3m': 3, '5m': 5, '10m': 10, '15m': 15,
        '30m': 30, '45m': 45,
        '1h': 60, '2h': 120, '3h': 180, '4h': 240, '6h': 360,
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
    return 0   # Format inconnu → dégradation silencieuse (warmup désactivé)


def parse_timeframe(timeframe: str, strict: bool = False) -> int:
    """
    Convertir timeframe string en secondes.
    
    IMPORTANT pour 'M' (mois):
    - Mode standard (strict=False): Utilise 30 jours (2,592,000s) - approximation
    - Mode strict (strict=True): Refuse les timeframes mensuels (raise ValueError)
    
    Pour backtesting précis sur périodes mensuelles, préférer des timeframes
    en jours (ex: '30d' au lieu de '1M') ou gérer manuellement.
    
    Args:
        timeframe: Format '1m', '5m', '15m', '1h', '4h', '1d', '1w', '1M'
        strict: Si True, refuse les timeframes avec approximation
    
    Returns:
        int: Nombre de secondes
    
    Raises:
        ValueError: Si format invalide ou timeframe approximatif en mode strict
    """
    units = {
        's': 1,          # Seconde
        'm': 60,         # Minute
        'h': 3600,       # Heure
        'd': 86400,      # Jour
        'w': 604800,     # Semaine (7 jours - exact)
        'M': 2592000     # Mois (30 jours - APPROXIMATION)
    }
    
    if not timeframe or len(timeframe) < 2:
        raise ValueError(f"Timeframe invalide: {timeframe}")
    
    try:
        value = int(timeframe[:-1])
        unit = timeframe[-1]  # Garder case-sensitive pour 'm' vs 'M'
        
        if unit not in units:
            raise ValueError(f"Unité invalide: {unit}. Unités supportées: {list(units.keys())}")
        
        # Vérification mode strict pour approximations
        if unit == 'M':
            if strict:
                raise ValueError(
                    f"Timeframe '{timeframe}' utilise approximation (30j). "
                    f"En mode strict, utilisez '{value * 30}d'."
                )
            else:
                warnings.warn(
                    f"Timeframe '{timeframe}' utilise une approximation de 30 jours par mois. "
                    f"Pour un backtesting précis, préférez '{value * 30}d' ou gérez les mois manuellement.",
                    UserWarning,
                    stacklevel=2
                )
        
        return value * units[unit]
    
    except (ValueError, IndexError) as e:
        raise ValueError(f"Format timeframe invalide '{timeframe}': {e}")


# ============================================================================
# FORMATAGE NOMBRES
# ============================================================================

def format_price(price: Union[float, Decimal], decimals: int = 2) -> str:
    """
    Formater prix avec séparateurs de milliers.
    
    Args:
        price: Prix à formater
        decimals: Nombre de décimales (défaut: 2)
    
    Returns:
        str: Prix formaté avec séparateurs
    """
    if price is None:
        return "N/A"
    
    try:
        price_decimal = Decimal(str(price))
        formatted = f"{price_decimal:,.{decimals}f}"
        return formatted
    except Exception:
        return str(price)


def format_volume(volume: Union[float, int]) -> str:
    """
    Formater volume avec suffixes (K, M, B).
    
    Note v2.2: Limite à 'B' (milliards) car adapté au contexte crypto.
    Les volumes en trillions sont inexistants sur les marchés crypto actuels.
    
    Args:
        volume: Volume à formater
    
    Returns:
        str: Volume formaté
    """
    if volume is None:
        return "N/A"
    
    try:
        vol = float(volume)
        
        if vol >= 1e9:
            return f"{vol / 1e9:.2f}B"
        elif vol >= 1e6:
            return f"{vol / 1e6:.2f}M"
        elif vol >= 1e3:
            return f"{vol / 1e3:.2f}K"
        else:
            return f"{vol:.2f}"
    except Exception:
        return str(volume)


def format_percentage(value: Union[float, Decimal], decimals: int = 2, 
                      include_sign: bool = True) -> str:
    """
    Formater pourcentage avec signe optionnel.
    
    Args:
        value: Valeur en pourcentage (ex: 3.5 pour 3.5%)
        decimals: Nombre de décimales
        include_sign: Inclure signe + pour valeurs positives
    
    Returns:
        str: Pourcentage formaté
    """
    if value is None:
        return "N/A"
    
    try:
        val = float(value)
        sign = "+" if val > 0 and include_sign else ""
        return f"{sign}{val:.{decimals}f}%"
    except Exception:
        return str(value)


def round_price(price: Union[float, Decimal], decimals: int = 2) -> Decimal:
    """
    Arrondir prix avec précision Decimal.
    
    Args:
        price: Prix à arrondir
        decimals: Nombre de décimales
    
    Returns:
        Decimal: Prix arrondi
    """
    try:
        price_decimal = Decimal(str(price))
        quantize_value = Decimal(10) ** -decimals
        return price_decimal.quantize(quantize_value, rounding=ROUND_HALF_UP)
    except Exception as e:
        raise ValueError(f"Erreur arrondi prix {price}: {e}")


# ============================================================================
# CALCULS MATHÉMATIQUES
# ============================================================================

def calculate_percentage_change(old_value: float, new_value: float) -> float:
    """
    Calculer variation en pourcentage.
    
    Args:
        old_value: Valeur ancienne
        new_value: Valeur nouvelle
    
    Returns:
        float: Variation en % (positif = hausse, négatif = baisse)
    
    Raises:
        TypeError: Si old_value ou new_value est None
        ValueError: Si old_value est 0 ou NaN, ou si new_value est NaN
    """
    if old_value is None or new_value is None:
        raise TypeError("old_value et new_value ne peuvent pas être None")
    
    old_f = float(old_value)
    new_f = float(new_value)
    
    if math.isnan(old_f) or math.isnan(new_f):
        raise ValueError("old_value et new_value ne peuvent pas être NaN")
    
    if old_f == 0:
        raise ValueError("old_value ne peut pas être 0")
    
    return ((new_f - old_f) / old_f) * 100


def safe_divide(numerator: float, denominator: float, 
                default: Optional[float] = 0.0) -> Optional[float]:
    """
    Division sécurisée (évite division par zéro).
    
    Args:
        numerator: Numérateur
        denominator: Dénominateur
        default: Valeur par défaut si division impossible (peut être None)
    
    Returns:
        float | None: Résultat division ou default
    """
    try:
        if denominator == 0:
            return default
        return numerator / denominator
    except Exception:
        return default


def clamp(value: float, min_value: float, max_value: float) -> float:
    """
    Contraindre valeur entre min et max.
    
    Note: Python 3.13+ inclut cette fonction nativement.
    
    Args:
        value: Valeur à contraindre
        min_value: Minimum
        max_value: Maximum
    
    Returns:
        float: Valeur contrainte
    """
    return max(min_value, min(value, max_value))


def interpolate_linear(x: float, x0: float, x1: float, y0: float, y1: float) -> float:
    """
    Interpolation linéaire entre deux points.
    
    Utile pour:
    - Combler gaps dans données OHLCV
    - Estimer valeurs intermédiaires
    - Lissage de courbes
    
    Args:
        x: Point où calculer la valeur interpolée
        x0: Coordonnée x du premier point
        x1: Coordonnée x du second point
        y0: Coordonnée y du premier point
        y1: Coordonnée y du second point
    
    Returns:
        float: Valeur interpolée au point x
    
    Raises:
        ValueError: Si x0 == x1 (division par zéro)
        ValueError: Si x en dehors de [x0, x1]
    """
    if x0 == x1:
        raise ValueError(f"x0 et x1 doivent être différents (x0={x0}, x1={x1})")
    
    # Vérification que x est dans l'intervalle [x0, x1]
    if not (min(x0, x1) <= x <= max(x0, x1)):
        raise ValueError(
            f"x={x} doit être dans l'intervalle [{min(x0, x1)}, {max(x0, x1)}]"
        )
    
    # Formule d'interpolation linéaire: y = y0 + (x - x0) * (y1 - y0) / (x1 - x0)
    return y0 + (x - x0) * (y1 - y0) / (x1 - x0)


# ============================================================================
# GESTION FICHIERS & DOSSIERS
# ============================================================================

def ensure_directory(path: Union[str, Path]) -> Path:
    """
    Créer dossier s'il n'existe pas.
    
    Args:
        path: Chemin du dossier
    
    Returns:
        Path: Objet Path du dossier
    """
    path_obj = Path(path)
    path_obj.mkdir(parents=True, exist_ok=True)
    return path_obj


def delete_file(file_path: Union[str, Path], raise_on_missing: bool = False) -> bool:
    """
    Supprimer un fichier de manière sécurisée.
    
    NOUVEAU v2.2.0 - Conforme aux specs "Gestion fichiers/dossiers (suppression)"
    
    Args:
        file_path: Chemin du fichier à supprimer
        raise_on_missing: Si True, raise FileNotFoundError si fichier inexistant
    
    Returns:
        bool: True si fichier supprimé, False si fichier n'existait pas
    
    Raises:
        FileNotFoundError: Si fichier inexistant et raise_on_missing=True
        PermissionError: Si permissions insuffisantes
        OSError: Si erreur système
    """
    path = Path(file_path)
    
    try:
        if not path.exists():
            if raise_on_missing:
                raise FileNotFoundError(f"Fichier non trouvé: {file_path}")
            return False
        
        if not path.is_file():
            raise ValueError(f"Chemin '{file_path}' n'est pas un fichier")
        
        path.unlink()
        return True
    
    except (PermissionError, OSError) as e:
        raise OSError(f"Erreur suppression fichier '{file_path}': {e}")


def delete_directory(dir_path: Union[str, Path], 
                     raise_on_missing: bool = False,
                     recursive: bool = False) -> bool:
    """
    Supprimer un dossier de manière sécurisée.
    
    NOUVEAU v2.2.0 - Conforme aux specs "Gestion fichiers/dossiers (suppression)"
    
    Args:
        dir_path: Chemin du dossier à supprimer
        raise_on_missing: Si True, raise FileNotFoundError si dossier inexistant
        recursive: Si True, supprime contenu récursivement (shutil.rmtree)
    
    Returns:
        bool: True si dossier supprimé, False si n'existait pas
    
    Raises:
        FileNotFoundError: Si dossier inexistant et raise_on_missing=True
        OSError: Si dossier non vide et recursive=False
        PermissionError: Si permissions insuffisantes
    """
    path = Path(dir_path)
    
    try:
        if not path.exists():
            if raise_on_missing:
                raise FileNotFoundError(f"Dossier non trouvé: {dir_path}")
            return False
        
        if not path.is_dir():
            raise ValueError(f"Chemin '{dir_path}' n'est pas un dossier")
        
        if recursive:
            shutil.rmtree(path)
        else:
            path.rmdir()  # Raise OSError si non vide
        
        return True
    
    except OSError as e:
        if "not empty" in str(e).lower():
            raise OSError(
                f"Dossier '{dir_path}' non vide. "
                f"Utilisez recursive=True pour forcer suppression."
            )
        raise OSError(f"Erreur suppression dossier '{dir_path}': {e}")


def file_exists(file_path: Union[str, Path]) -> bool:
    """
    Vérifier si un fichier existe.
    
    Args:
        file_path: Chemin du fichier
    
    Returns:
        bool: True si fichier existe, False sinon
    """
    path = Path(file_path)
    return path.exists() and path.is_file()


def dir_exists(dir_path: Union[str, Path]) -> bool:
    """
    Vérifier si un dossier existe.
    
    Args:
        dir_path: Chemin du dossier
    
    Returns:
        bool: True si dossier existe, False sinon
    """
    path = Path(dir_path)
    return path.exists() and path.is_dir()


def read_json(file_path: Union[str, Path]) -> Dict[str, Any]:
    """
    Lire fichier JSON.
    
    Args:
        file_path: Chemin fichier JSON
    
    Returns:
        dict: Contenu JSON
    
    Raises:
        FileNotFoundError: Si fichier inexistant
        json.JSONDecodeError: Si JSON invalide
    """
    path = Path(file_path)
    
    if not path.exists():
        raise FileNotFoundError(f"Fichier non trouvé: {file_path}")
    
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def write_json(data: Dict[str, Any], file_path: Union[str, Path], 
               indent: int = 2) -> None:
    """
    Écrire données dans fichier JSON.
    
    Args:
        data: Données à écrire
        file_path: Chemin fichier destination
        indent: Indentation JSON (défaut: 2)
    """
    path = Path(file_path)
    
    # Créer dossier parent si nécessaire
    path.parent.mkdir(parents=True, exist_ok=True)
    
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=indent, ensure_ascii=False)


def get_project_root(marker_file: str = "pyproject.toml") -> Path:
    """
    Obtenir chemin racine du projet BULLET-1.

    Recherche par marqueurs de présence (ordre de priorité) :
    1. pyproject.toml / .git / requirements.txt / setup.py
    2. Fallback garanti : helpers.py est dans src/utils/ → 3 parents = racine

    Args:
        marker_file: Fichier marqueur personnalisé (ex: '.bullet-root')

    Returns:
        Path: Chemin racine du projet
    """
    current = Path(__file__).resolve()

    root_markers = [marker_file]
    for m in ['pyproject.toml', '.git', 'requirements.txt', 'setup.py']:
        if m != marker_file:
            root_markers.append(m)

    for parent in [current] + list(current.parents):
        for marker in root_markers:
            if (parent / marker).exists():
                return parent

    # [FIX] helpers.py est dans src/utils/ → 3 niveaux pour atteindre la racine.
    # Avant (bug) : current.parent.parent retournait src/ au lieu de BULLET-1/.
    return current.parent.parent.parent


# ============================================================================
# VALIDATION DONNÉES
# ============================================================================

def is_valid_price(price: Union[float, int, str]) -> bool:
    """
    Valider qu'un prix est valide (> 0).
    
    Args:
        price: Prix à valider
    
    Returns:
        bool: True si valide
    """
    try:
        price_float = float(price)
        return price_float > 0
    except (ValueError, TypeError):
        return False


def is_valid_volume(volume: Union[float, int, str]) -> bool:
    """
    Valider qu'un volume est valide (>= 0).
    
    Args:
        volume: Volume à valider
    
    Returns:
        bool: True si valide
    """
    try:
        volume_float = float(volume)
        return volume_float >= 0
    except (ValueError, TypeError):
        return False


def validate_percentage(value: Union[float, int], 
                       min_pct: float = 0.0, 
                       max_pct: float = 100.0) -> bool:
    """
    Valider qu'un pourcentage est dans les limites.
    
    Args:
        value: Valeur à valider
        min_pct: Pourcentage minimum
        max_pct: Pourcentage maximum
    
    Returns:
        bool: True si valide
    """
    try:
        val = float(value)
        return min_pct <= val <= max_pct
    except (ValueError, TypeError):
        return False


# ============================================================================
# UTILITAIRES TEXTE
# ============================================================================

def truncate_string(text: str, max_length: int = 50, 
                   suffix: str = "...") -> str:
    """
    Tronquer texte à longueur maximale.
    
    Args:
        text: Texte à tronquer
        max_length: Longueur maximale
        suffix: Suffixe si tronqué
    
    Returns:
        str: Texte tronqué
    """
    if len(text) <= max_length:
        return text
    
    return text[:max_length - len(suffix)] + suffix


def generate_id(prefix: str = "", use_uuid: bool = False) -> str:
    """
    Générer ID unique.
    
    Args:
        prefix: Préfixe optionnel
        use_uuid: Si True, utilise UUID court. Sinon, timestamp avec microsecondes
    
    Returns:
        str: ID unique
    """
    if use_uuid:
        uid = str(uuid.uuid4())[:8]
        return f"{prefix}_{uid}" if prefix else uid
    else:
        # [v2.3.1 — FIX H-1] UTC explicite pour cohérence avec tous les timestamps du système.
        # datetime.now() retournait l'heure locale de la machine, incohérent avec
        # les timestamps UTC issus du DataFrame OHLCV et du reste du système.
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        return f"{prefix}_{ts}" if prefix else ts


def to_snake_case(text: str) -> str:
    """
    Convertir CamelCase ou PascalCase en snake_case.
    
    NOUVEAU v2.2.0 - Conforme aux specs "Utilitaires strings (snake_case)"
    
    Args:
        text: Texte à convertir
    
    Returns:
        str: Texte en snake_case
    """
    # Insérer underscore avant majuscules précédées de minuscules
    s1 = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', text)
    # Insérer underscore avant majuscules précédées de lettres/chiffres
    s2 = re.sub('([a-z0-9])([A-Z])', r'\1_\2', s1)
    return s2.lower()


def to_camel_case(text: str, pascal_case: bool = False) -> str:
    """
    Convertir snake_case en camelCase ou PascalCase.
    
    NOUVEAU v2.2.0 - Conforme aux specs "Utilitaires strings (camelCase)"
    
    Note: Cette fonction opère sur du snake_case pur. Les entrées contenant
          déjà de la casse mixte doivent d'abord passer par to_snake_case().
    
    Args:
        text: Texte en snake_case à convertir
        pascal_case: Si True, première lettre en majuscule (PascalCase)
    
    Returns:
        str: Texte en camelCase ou PascalCase
    """
    if not text:
        return text
    
    components = text.split('_')
    # Capitaliser uniquement la première lettre de chaque composant (title vs capitalize)
    # title() préserve la casse des lettres suivantes; capitalize() les met en minuscule
    # On préserve la casse interne en ne touchant qu'à la première lettre
    def cap_first(s: str) -> str:
        return s[0].upper() + s[1:] if s else s
    
    if pascal_case:
        return ''.join(cap_first(word) for word in components if word)
    else:
        first = components[0].lower() if components[0] else ''
        rest = ''.join(cap_first(word) for word in components[1:] if word)
        return first + rest


def sanitize_filename(filename: str, replacement: str = "_") -> str:
    """
    Nettoyer nom de fichier pour compatibilité système de fichiers.
    
    Supprime/remplace les caractères invalides pour Windows/Linux/macOS:
    - Caractères interdits: < > : " / \\ | ? *
    - Caractères de contrôle (0x00-0x1F)
    - Espaces multiples → espace unique
    - Points de début/fin de nom
    
    Args:
        filename: Nom de fichier à nettoyer
        replacement: Caractère de remplacement (défaut: "_")
    
    Returns:
        str: Nom de fichier sécurisé
    """
    # Caractères invalides pour système de fichiers
    invalid_chars = '<>:"/\\|?*'
    
    # Remplacer caractères invalides
    for char in invalid_chars:
        filename = filename.replace(char, replacement)
    
    # Supprimer caractères de contrôle (0x00-0x1F)
    filename = re.sub(r'[\x00-\x1F]', '', filename)
    
    # Remplacer espaces multiples par un seul
    filename = re.sub(r'\s+', ' ', filename)
    
    # Supprimer espaces et points au début/fin
    filename = filename.strip(' .')
    
    # Si vide après nettoyage, utiliser nom par défaut
    if not filename:
        filename = "unnamed"
    
    return filename


def sanitize_string(text: str, allow_quotes: bool = False) -> str:
    """
    Nettoyer string pour éviter injections (SQL, XSS basique).
    
    NOUVEAU v2.2.0 - Conforme aux specs "Utilitaires strings (sanitization)"
    
    Protection basique contre:
    - Quotes SQL (', ")
    - Balises HTML (<, >)
    - Caractères de contrôle
    - Scripts basiques
    
    Note: Pour protection SQL robuste, utilisez parameterized queries.
          Pour protection XSS robuste, utilisez bibliothèque dédiée (bleach).
    
    Args:
        text: Texte à nettoyer
        allow_quotes: Si True, garde les quotes (pour texte légitime)
    
    Returns:
        str: Texte nettoyé
    """
    # Supprimer balises HTML
    text = re.sub(r'<[^>]*>', '', text)
    
    # Supprimer quotes si non autorisées
    if not allow_quotes:
        text = text.replace("'", "").replace('"', '')
    
    # Supprimer caractères de contrôle
    text = re.sub(r'[\x00-\x1F\x7F]', '', text)
    
    # Supprimer séquences suspectes (basique)
    dangerous_patterns = [
        r'javascript:',
        r'onerror=',
        r'onclick=',
        r'onload=',
        r'<script',
        r'</script',
    ]
    
    for pattern in dangerous_patterns:
        text = re.sub(pattern, '', text, flags=re.IGNORECASE)
    
    return text.strip()


# ============================================================================
# UTILITAIRES CONVERSION
# ============================================================================

def to_float(value: Any, default: float = 0.0) -> float:
    """
    Convertir valeur en float avec fallback.
    
    Args:
        value: Valeur à convertir
        default: Valeur par défaut si conversion échoue
    
    Returns:
        float: Valeur convertie ou default
    """
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


def to_int(value: Any, default: int = 0) -> int:
    """
    Convertir valeur en int avec fallback.
    
    Args:
        value: Valeur à convertir
        default: Valeur par défaut si conversion échoue
    
    Returns:
        int: Valeur convertie ou default
    """
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return default



# ============================================================================
# VALIDATION COHÉRENCE DES MODES DE CONFIGURATION  [v2.4.0]
# ============================================================================

def _extract_mode_from_config(config: dict, filepath: "Path") -> str:
    """
    Extraire et valider la valeur de 'mode' depuis un dict de configuration.

    Supporte deux formats de façon transparente :
      Format A — config.json (string directe) :
        { "general": { "mode": "backtest" } }

      Format B — module_config.json (objet avec clé 'value') :
        { "general": { "mode": { "value": "backtest", "description": "..." } } }

    Args:
        config   : dict du fichier de config déjà chargé en mémoire.
        filepath : Path du fichier (utilisé exclusivement pour les messages
                   d'erreur — aucune lecture disque effectuée ici).

    Returns:
        str : mode normalisé en minuscules ('backtest', 'paper' ou 'live').

    Raises:
        ModeMissingError : section 'general' ou clé 'mode' absente.
        ModeInvalidError : valeur de mode non présente dans VALID_MODES.
    """
    SEP = "=" * 68

    # ── Vérification section 'general' ──────────────────────────────────────
    general = config.get("general")
    if general is None:
        raise ModeMissingError(
            f"\n{SEP}\n"
            f"\u274c CLÉ 'general' MANQUANTE\n"
            f"{SEP}\n\n"
            f"  Fichier : {filepath}\n\n"
            f"  La section 'general' est obligatoire dans tout fichier de config.\n"
            f"  Structure minimale attendue :\n"
            f"    {{\n"
            f"      \"general\": {{\n"
            f"        \"mode\": \"backtest\"\n"
            f"      }}\n"
            f"    }}\n"
            f"{SEP}\n"
        )

    # ── Vérification clé 'mode' ──────────────────────────────────────────────
    raw = general.get("mode")
    if raw is None:
        raise ModeMissingError(
            f"\n{SEP}\n"
            f"\u274c CLÉ 'mode' MANQUANTE DANS 'general'\n"
            f"{SEP}\n\n"
            f"  Fichier : {filepath}\n\n"
            f"  La clé 'mode' est obligatoire dans la section 'general'.\n"
            f"  Format A (config.json)        : \"mode\": \"backtest\"\n"
            f"  Format B (module_config.json) : \"mode\": {{ \"value\": \"backtest\" }}\n"
            f"{SEP}\n"
        )

    # ── Support Format B : { "value": "backtest", "description": "..." } ────
    if isinstance(raw, dict):
        raw = raw.get("value")
        if raw is None:
            raise ModeMissingError(
                f"\n{SEP}\n"
                f"\u274c CLÉ 'value' MANQUANTE DANS 'general.mode'\n"
                f"{SEP}\n\n"
                f"  Fichier : {filepath}\n\n"
                f"  Format objet détecté pour 'mode' mais clé 'value' absente.\n"
                f"  Attendu : {{ \"mode\": {{ \"value\": \"backtest\" }} }}\n"
                f"{SEP}\n"
            )

    # ── Validation de la valeur ───────────────────────────────────────────────
    mode = str(raw).strip().lower()
    if mode not in VALID_MODES:
        raise ModeInvalidError(
            f"\n{SEP}\n"
            f"\u274c VALEUR DE MODE INVALIDE\n"
            f"{SEP}\n\n"
            f"  Fichier        : {filepath}\n"
            f"  Valeur reçue   : '{raw}'\n"
            f"  Valeurs valides: {sorted(VALID_MODES)}\n\n"
            f"  Corriger la valeur de 'mode' dans ce fichier.\n"
            f"{SEP}\n"
        )

    return mode


def verify_mode_consistency(
    module_config,
    module_config_path,
    module_name: str,
    main_config=None,
    main_config_path=None,
    logger=None
) -> str:
    """
    Vérifier que le mode du module correspond au mode système (config/config.json).

    C'est la fonction centrale de validation des modes dans BULLET-1.
    Tous les modules indicateurs doivent l'appeler dans leur __init__ via
    load_and_verify_module_config() (voir ci-dessous).

    Comportement :
      1. Charge config/config.json si main_config non fourni en paramètre.
      2. Extrait le mode des deux configs (supporte Format A et Format B).
      3. Valide que chaque mode est dans VALID_MODES.
      4. Compare — lève ModeInconsistencyError avec instructions si différents.
      5. Logue le résultat si un logger est fourni.

    Aucun fallback silencieux. Toute anomalie lève une exception explicite.

    Args:
        module_config      : dict du fichier de config du module (déjà chargé).
        module_config_path : Path du fichier de config du module.
        module_name        : Nom lisible du module (ex: 'momentum', 'volatility').
        main_config        : dict de config.json. Si None, rechargé depuis disque.
        main_config_path   : Path de config.json. Si None, auto-résolu via
                             get_project_root() / 'config' / 'config.json'.
        logger             : Instance BulletLogger (optionnelle).

    Returns:
        str : mode validé et cohérent ('backtest', 'paper' ou 'live').

    Raises:
        FileNotFoundError      : config.json introuvable sur disque.
        json.JSONDecodeError   : JSON invalide dans l'un des fichiers.
        ModeMissingError       : clé 'mode' absente dans l'un des fichiers.
        ModeInvalidError       : valeur de mode non reconnue.
        ModeInconsistencyError : modes différents entre les deux fichiers.
    """
    SEP = "=" * 68

    # ── Résolution du chemin config.json ────────────────────────────────────
    if main_config_path is None:
        main_config_path = get_project_root() / "config" / "config.json"

    # ── Chargement config.json si non fourni ────────────────────────────────
    if main_config is None:
        main_config = read_json(main_config_path)   # Réutilise read_json() existant

    # ── Extraction des modes ─────────────────────────────────────────────────
    system_mode = _extract_mode_from_config(main_config,   main_config_path)
    module_mode = _extract_mode_from_config(module_config, module_config_path)

    if logger:
        logger.debug(
            f"[helpers] verify_mode_consistency — {module_name}: "
            f"system='{system_mode}', module='{module_mode}'"
        )

    # ── Comparaison ─────────────────────────────────────────────────────────
    if system_mode != module_mode:
        msg = (
            f"\n{SEP}\n"
            f"\U0001f534 ERREUR FATALE \u2014 INCOHÉRENCE DE MODE DÉTECTÉE\n"
            f"{SEP}\n\n"
            f"  Module concerné : {module_name}\n\n"
            f"  config/config.json           \u2192 mode = '{system_mode}'\n"
            f"  {module_config_path.name:<33} \u2192 mode = '{module_mode}'\n\n"
            f"  \u26a0\ufe0f  Les deux fichiers DOIVENT avoir le même mode.\n"
            f"     Une incohérence provoque un comportement imprévisible\n"
            f"     (calculs batch vs incrémental, caches mal initialisés).\n\n"
            f"  \U0001f4dd SOLUTION \u2014 choisir UNE des deux options :\n\n"
            f"  Option 1 \u2192 Harmoniser sur '{system_mode}' (mode système actuel) :\n"
            f"    Ouvrir  : {module_config_path}\n"
            f"    Modifier: general.mode \u2192 \"{system_mode}\"\n\n"
            f"  Option 2 \u2192 Harmoniser sur '{module_mode}' (mode module actuel) :\n"
            f"    Ouvrir  : {main_config_path}\n"
            f"    Modifier: general.mode \u2192 \"{module_mode}\"\n\n"
            f"  Modes valides : {sorted(VALID_MODES)}\n"
            f"{SEP}\n"
        )
        if logger:
            logger.critical(msg)
        raise ModeInconsistencyError(msg)

    # ── Succès ───────────────────────────────────────────────────────────────
    if logger:
        logger.info(
            f"\u2705 [helpers] {module_name}: mode '{system_mode}' cohérent "
            f"(config.json \u2194 {module_config_path.name})"
        )

    return system_mode


def load_and_verify_module_config(
    module_config_path,
    module_name: str,
    main_config=None,
    main_config_path=None,
    logger=None
) -> tuple:
    """
    Charger un fichier de config de module ET vérifier la cohérence du mode
    en une seule opération atomique. Aucun fallback silencieux.

    C'est le point d'entrée recommandé pour tous les modules indicateurs.
    Remplace le pattern try/except FileNotFoundError + if config fragile
    qui permettait de démarrer silencieusement avec un mode incohérent.

    Usage type dans un module indicateur :
        from src.utils.helpers import load_and_verify_module_config, get_project_root

        self._cfg, self.mode = load_and_verify_module_config(
            module_config_path = get_project_root() / 'config' / 'momentum_config.json',
            module_name        = 'momentum',
            main_config        = config,      # dict déjà chargé par le système
            logger             = self.logger
        )

    Args:
        module_config_path : Path du fichier de config du module.
        module_name        : Nom lisible du module (pour les messages d'erreur).
        main_config        : dict de config.json (optionnel, rechargé si None).
        main_config_path   : Path de config.json (optionnel, auto-résolu si None).
        logger             : Instance BulletLogger (optionnelle).

    Returns:
        tuple(dict, str) : (module_config dict, mode validé et cohérent)

    Raises:
        FileNotFoundError      : module_config_path introuvable sur disque.
        json.JSONDecodeError   : JSON invalide dans l'un des fichiers.
        ModeMissingError       : clé 'mode' absente dans l'un des fichiers.
        ModeInvalidError       : valeur de mode non reconnue.
        ModeInconsistencyError : modes différents entre les deux fichiers.
    """
    # ── Chargement (lève FileNotFoundError si absent — PAS de fallback) ─────
    module_cfg = read_json(module_config_path)   # Réutilise read_json() existant

    # ── Vérification cohérence ───────────────────────────────────────────────
    verified_mode = verify_mode_consistency(
        module_config      = module_cfg,
        module_config_path = Path(module_config_path),
        module_name        = module_name,
        main_config        = main_config,
        main_config_path   = main_config_path,
        logger             = logger
    )

    return module_cfg, verified_mode


# FIN DU MODULE