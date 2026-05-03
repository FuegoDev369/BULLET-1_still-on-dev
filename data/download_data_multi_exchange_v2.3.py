#!/usr/bin/env python3
"""
📥 Bullet-1 Trading Bot - Multi-Exchange Data Downloader v2.0
Interface CLI Interactive avec gestion avancée
"""

import ccxt
import pandas as pd
from datetime import datetime, timedelta
import time
import os
import json
from pathlib import Path
from typing import Optional, Tuple, Dict, List
import sys

# ============================================================================
# 🎨 CONFIGURATION & CONSTANTES
# ============================================================================

VERSION = "2.0.0"
CONFIG_DIR = Path.home() / ".bullet1_downloader"
CONFIG_FILE = CONFIG_DIR / "config.json"
CACHE_DIR = CONFIG_DIR / "cache"
HISTORY_FILE = CONFIG_DIR / "history.json"

EXCHANGES = ['binance', 'mexc', 'bybit', 'kraken']
TIMEFRAMES = ['1m', '5m', '15m', '30m', '1h', '4h', '1d']
EXPORT_FORMATS = ['csv', 'json', 'parquet']

# Couleurs ANSI
class Colors:
    HEADER = '\033[95m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    END = '\033[0m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'

# ============================================================================
# 🎭 BANNIÈRE ET INTERFACE
# ============================================================================

def print_banner():
    """Affiche la bannière du script"""
    banner = f"""
{Colors.CYAN}╔═══════════════════════════════════════════════════════════════╗
║  🚀 BULLET-1 TRADING BOT - DATA DOWNLOADER v{VERSION}        ║
║  📊 Multi-Exchange Historical Data Retrieval System          ║
║  ⚡ Interactive CLI Interface                                 ║
╚═══════════════════════════════════════════════════════════════╝{Colors.END}
"""
    print(banner)

def clear_screen():
    """Efface l'écran"""
    os.system('cls' if os.name == 'nt' else 'clear')

def print_section(title: str):
    """Affiche un titre de section"""
    print(f"\n{Colors.BOLD}{Colors.BLUE}{'='*65}")
    print(f"  {title}")
    print(f"{'='*65}{Colors.END}\n")

def print_success(message: str):
    """Message de succès"""
    print(f"{Colors.GREEN}✅ {message}{Colors.END}")

def print_error(message: str):
    """Message d'erreur"""
    print(f"{Colors.RED}❌ {message}{Colors.END}")

def print_warning(message: str):
    """Message d'avertissement"""
    print(f"{Colors.YELLOW}⚠️  {message}{Colors.END}")

def print_info(message: str):
    """Message d'information"""
    print(f"{Colors.CYAN}ℹ️  {message}{Colors.END}")

def progress_bar(current: int, total: int, prefix: str = '', length: int = 40):
    """Affiche une barre de progression"""
    percent = 100 * (current / float(total))
    filled = int(length * current // total)
    bar = '█' * filled + '░' * (length - filled)
    print(f'\r{prefix} |{bar}| {percent:.1f}% ({current}/{total})', end='', flush=True)
    if current == total:
        print()

# ============================================================================
# 💾 GESTION DE CONFIGURATION ET CACHE
# ============================================================================

def init_directories():
    """Initialise les dossiers de configuration"""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

def load_config() -> Dict:
    """Charge la configuration sauvegardée"""
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, 'r') as f:
            return json.load(f)
    return {"favorites": [], "last_used": {}}

def save_config(config: Dict):
    """Sauvegarde la configuration"""
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=2)

def add_to_history(download_info: Dict):
    """Ajoute un téléchargement à l'historique"""
    history = []
    if HISTORY_FILE.exists():
        with open(HISTORY_FILE, 'r') as f:
            history = json.load(f)
    
    history.append({
        **download_info,
        "timestamp": datetime.now().isoformat()
    })
    
    # Garder seulement les 50 derniers
    history = history[-50:]
    
    with open(HISTORY_FILE, 'w') as f:
        json.dump(history, f, indent=2)

def get_history() -> List[Dict]:
    """Récupère l'historique des téléchargements"""
    if HISTORY_FILE.exists():
        with open(HISTORY_FILE, 'r') as f:
            return json.load(f)
    return []

# ============================================================================
# 🔧 FONCTIONS UTILITAIRES
# ============================================================================

def get_user_input(prompt: str, default: Optional[str] = None) -> str:
    """Récupère une entrée utilisateur avec validation"""
    if default:
        full_prompt = f"{prompt} [{default}]: "
    else:
        full_prompt = f"{prompt}: "
    
    value = input(full_prompt).strip()
    return value if value else (default or "")

def get_int_input(prompt: str, min_val: int = 1, max_val: int = None, default: Optional[int] = None) -> int:
    """Récupère un entier avec validation"""
    while True:
        try:
            value = get_user_input(prompt, str(default) if default else None)
            if not value:
                if default:
                    return default
                continue
            
            num = int(value)
            if num < min_val:
                print_error(f"Valeur minimale: {min_val}")
                continue
            if max_val and num > max_val:
                print_error(f"Valeur maximale: {max_val}")
                continue
            return num
        except ValueError:
            print_error("Veuillez entrer un nombre valide")

def get_datetime_input(prompt: str, default: Optional[datetime] = None, with_time: bool = True) -> datetime:
    """Récupère une date/heure avec validation (format YYYY-MM-DD HH:MM)"""
    while True:
        try:
            if with_time:
                format_str = 'YYYY-MM-DD HH:MM'
                parse_format = '%Y-%m-%d %H:%M'
                if default:
                    default_str = default.strftime('%Y-%m-%d %H:%M')
                else:
                    default_str = None
            else:
                format_str = 'YYYY-MM-DD'
                parse_format = '%Y-%m-%d'
                if default:
                    default_str = default.strftime('%Y-%m-%d')
                else:
                    default_str = None
            
            value = get_user_input(f"{prompt} ({format_str})", default_str)
            
            if not value:
                if default:
                    return default
                continue
            
            # Validation du format
            datetime_obj = datetime.strptime(value, parse_format)
            
            # Vérification que la date n'est pas dans le futur
            if datetime_obj > datetime.now():
                print_error("La date/heure ne peut pas être dans le futur")
                continue
            
            return datetime_obj
            
        except ValueError:
            print_error(f"Format invalide. Utilisez {format_str} (ex: {'2024-01-15 14:30' if with_time else '2024-01-15'})")

def get_date_range() -> Tuple[datetime, datetime, int]:
    """Récupère une plage de dates avec validation"""
    print_section("📅 SÉLECTION DE LA PÉRIODE")
    
    presets = [
        "Derniers 7 jours",
        "Derniers 30 jours", 
        "Derniers 90 jours",
        "📆 Période personnalisée (avec date/heure)"
    ]
    
    choice = display_menu("Choisir une période", presets, allow_back=False)
    
    now = datetime.now()
    
    if choice == 1:  # 7 jours
        start_date = now - timedelta(days=7)
        end_date = now
    elif choice == 2:  # 30 jours
        start_date = now - timedelta(days=30)
        end_date = now
    elif choice == 3:  # 90 jours
        start_date = now - timedelta(days=90)
        end_date = now
    else:  # Personnalisé
        print()
        print_info("📆 Saisie de la période personnalisée avec date et heure")
        print_info("💡 Format: YYYY-MM-DD HH:MM (ex: 2024-01-15 14:30)")
        print()
        
        while True:
            start_date = get_datetime_input("Date/heure de début", with_time=True)
            end_date = get_datetime_input("Date/heure de fin", datetime.now(), with_time=True)
            
            if start_date >= end_date:
                print_error("❌ La date/heure de début doit être antérieure à la date/heure de fin")
                print()
                continue
            
            break
    
    # Calcul du nombre de jours (et heures pour plus de précision)
    time_diff = end_date - start_date
    days = time_diff.days
    hours = time_diff.seconds // 3600
    
    # Affichage de la période sélectionnée
    print()
    print_info(f"📅 Période sélectionnée:")
    print(f"   Du:   {Colors.BOLD}{start_date.strftime('%Y-%m-%d %H:%M')}{Colors.END}")
    print(f"   Au:   {Colors.BOLD}{end_date.strftime('%Y-%m-%d %H:%M')}{Colors.END}")
    
    if hours > 0:
        print(f"   Durée: {Colors.BOLD}{days} jours et {hours} heures{Colors.END}")
    else:
        print(f"   Durée: {Colors.BOLD}{days} jours{Colors.END}")
    
    return start_date, end_date, days

def display_menu(title: str, options: List[str], allow_back: bool = True) -> int:
    """Affiche un menu et retourne le choix"""
    print_section(title)
    
    for i, option in enumerate(options, 1):
        print(f"  {Colors.CYAN}{i}.{Colors.END} {option}")
    
    if allow_back:
        print(f"  {Colors.YELLOW}0.{Colors.END} ← Retour")
    
    print()
    
    max_choice = len(options)
    min_choice = 0 if allow_back else 1
    
    choice = get_int_input("Votre choix", min_val=min_choice, max_val=max_choice)
    return choice

def confirm_action(message: str, default: bool = False) -> bool:
    """Demande confirmation à l'utilisateur"""
    default_str = "O/n" if default else "o/N"
    response = get_user_input(f"{message} ({default_str})", "o" if default else "n").lower()
    
    if not response:
        return default
    
    return response in ['o', 'oui', 'y', 'yes']

# ============================================================================
# 🏦 FONCTIONS EXCHANGE (depuis v1)
# ============================================================================

def get_exchange(exchange_name: str):
    """Initialise la connexion à un exchange"""
    try:
        exchange_class = getattr(ccxt, exchange_name)
        exchange = exchange_class({
            'enableRateLimit': True,
            'options': {'defaultType': 'spot'} if exchange_name in ['binance', 'bybit'] else {}
        })
        return exchange
    except Exception as e:
        print_error(f"Erreur lors de l'initialisation de {exchange_name}: {str(e)}")
        return None

def test_exchange_history(exchange, pair: str, timeframe: str) -> Tuple[int, float, str]:
    """Teste l'historique disponible sur un exchange"""
    try:
        exchange.load_markets()
        
        if pair not in exchange.markets:
            return 0, 0, exchange.id
        
        since = int((datetime.now() - timedelta(days=90)).timestamp() * 1000)
        ohlcv = exchange.fetch_ohlcv(pair, timeframe, since=since, limit=1000)
        
        if not ohlcv:
            return 0, 0, exchange.id
        
        time_diff = (ohlcv[-1][0] - ohlcv[0][0]) / (1000 * 86400)
        return len(ohlcv), time_diff, exchange.id
    
    except Exception as e:
        return 0, 0, exchange.id

def find_best_exchange(pair: str, timeframe: str, days_needed: int, retries: int = 3) -> Tuple:
    """Trouve le meilleur exchange avec retry automatique"""
    print_info(f"🔍 Recherche du meilleur exchange pour {days_needed} jours...")
    print()
    
    results = []
    
    for exchange_name in EXCHANGES:
        attempt = 0
        success = False
        
        while attempt < retries and not success:
            try:
                print(f"   📡 Test de {Colors.BOLD}{exchange_name}{Colors.END}", end='')
                if attempt > 0:
                    print(f" (tentative {attempt + 1}/{retries})", end='')
                print("...", end='', flush=True)
                
                exchange = get_exchange(exchange_name)
                if not exchange:
                    print(f" {Colors.RED}✗ Échec d'initialisation{Colors.END}")
                    break
                
                candles, days, _ = test_exchange_history(exchange, pair, timeframe)
                
                if candles > 0:
                    print(f" {Colors.GREEN}✅ {days:.1f} jours ({candles} bougies){Colors.END}")
                    results.append((exchange, exchange_name, days, candles))
                    success = True
                else:
                    print(f" {Colors.YELLOW}⚠ Paire non disponible{Colors.END}")
                    break
                
            except Exception as e:
                attempt += 1
                if attempt < retries:
                    print(f" {Colors.YELLOW}⚠ Erreur, retry...{Colors.END}")
                    time.sleep(2)
                else:
                    print(f" {Colors.RED}✗ Échec après {retries} tentatives{Colors.END}")
        
        time.sleep(0.5)
    
    if not results:
        print_error("\n❌ Aucun exchange disponible pour cette paire!")
        return None, None, 0
    
    results.sort(key=lambda x: x[2], reverse=True)
    best = results[0]
    
    print()
    print(f"🏆 {Colors.GREEN}{Colors.BOLD}Meilleur exchange: {best[1]}{Colors.END}")
    print(f"   ✅ Historique disponible: {best[2]:.1f} jours ({best[3]} bougies)")
    
    if len(results) > 1:
        print(f"\n💡 Alternatives disponibles:")
        for alt in results[1:]:
            print(f"   • {alt[1]}: {alt[2]:.1f} jours")
    
    return best[0], best[1], best[2]

def suggest_alternatives(exchange_name: str, pair: str, timeframe: str):
    """Suggère des alternatives si une paire n'existe pas"""
    print_warning(f"La paire {pair} n'est pas disponible sur {exchange_name}")
    print_info("🔍 Recherche de paires similaires...")
    
    try:
        exchange = get_exchange(exchange_name)
        if not exchange:
            return
        
        exchange.load_markets()
        base = pair.split('/')[0]
        
        similar = [m for m in exchange.markets.keys() if base in m][:5]
        
        if similar:
            print(f"\n💡 Paires similaires disponibles sur {exchange_name}:")
            for i, p in enumerate(similar, 1):
                print(f"   {i}. {p}")
        else:
            print_warning("Aucune paire similaire trouvée")
    
    except Exception as e:
        print_error(f"Erreur lors de la recherche: {str(e)}")

def download_from_exchange(exchange, pair: str, timeframe: str, start_date: datetime, 
                          end_date: datetime, output_file: str, export_format: str = 'csv') -> bool:
    """Télécharge les données depuis un exchange"""
    try:
        exchange.load_markets()
        
        if pair not in exchange.markets:
            print_error(f"❌ La paire {pair} n'existe pas sur {exchange.id}")
            suggest_alternatives(exchange.id, pair, timeframe)
            return False
        
        # Calcul du nombre de jours
        days = (end_date - start_date).days
        since = int(start_date.timestamp() * 1000)
        
        print_section(f"📅 Configuration du téléchargement")
        print(f"   Exchange: {Colors.BOLD}{exchange.id}{Colors.END}")
        print(f"   Paire: {Colors.BOLD}{pair}{Colors.END}")
        print(f"   Timeframe: {Colors.BOLD}{timeframe}{Colors.END}")
        print(f"   Période: {Colors.BOLD}{start_date.strftime('%Y-%m-%d %H:%M')}{Colors.END}")
        print(f"          → {Colors.BOLD}{end_date.strftime('%Y-%m-%d %H:%M')}{Colors.END}")
        
        # Calcul des bougies attendues
        timeframe_minutes = {
            '1m': 1, '5m': 5, '15m': 15, '30m': 30,
            '1h': 60, '4h': 240, '1d': 1440
        }
        
        total_minutes = (end_date - start_date).total_seconds() / 60
        expected_candles = int(total_minutes / timeframe_minutes.get(timeframe, 5))
        print(f"   Bougies attendues: ~{expected_candles}")
        print()
        
        if not confirm_action("Lancer le téléchargement ?", True):
            print_warning("Téléchargement annulé")
            return False
        
        print()
        print_info(f"⏳ Téléchargement en cours depuis {exchange.id}...")
        
        all_data = []
        current_since = since
        request_count = 0
        max_requests = 100
        
        while current_since < int(end_date.timestamp() * 1000) and request_count < max_requests:
            try:
                ohlcv = exchange.fetch_ohlcv(pair, timeframe, since=current_since, limit=1000)
                
                if not ohlcv:
                    break
                
                all_data.extend(ohlcv)
                request_count += 1
                
                # Affichage simple sans barre de progression
                last_candle_date = datetime.fromtimestamp(ohlcv[-1][0] / 1000).strftime('%Y-%m-%d %H:%M')
                print(f"   📦 Requête {request_count}: {len(ohlcv)} bougies récupérées (jusqu'à {last_candle_date})")
                
                current_since = ohlcv[-1][0] + 1
                time.sleep(0.2)
                
            except Exception as e:
                print_error(f"\n❌ Erreur: {str(e)}")
                break
        
        if not all_data:
            print_error("\n❌ Aucune donnée récupérée")
            return False
        
        print()
        print_info(f"🔄 Traitement de {len(all_data)} bougies...")
        
        # Conversion en DataFrame
        df = pd.DataFrame(all_data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        
        # Nettoyage
        df = df.drop_duplicates(subset=['timestamp'])
        df = df.sort_values('timestamp')
        df = df[(df['timestamp'] >= start_date) & (df['timestamp'] <= end_date)]
        
        print_success(f"✅ {len(df)} bougies uniques après nettoyage")
        
        # Statistiques
        print_section("💰 Statistiques des données")
        print(f"   Prix min: {Colors.BOLD}{df['low'].min():.2f} USDT{Colors.END}")
        print(f"   Prix max: {Colors.BOLD}{df['high'].max():.2f} USDT{Colors.END}")
        print(f"   Prix moyen: {Colors.BOLD}{df['close'].mean():.2f} USDT{Colors.END}")
        
        variation = ((df['close'].iloc[-1] - df['close'].iloc[0]) / df['close'].iloc[0]) * 100
        color = Colors.GREEN if variation > 0 else Colors.RED
        print(f"   Variation: {color}{Colors.BOLD}{variation:+.2f}%{Colors.END}")
        print(f"   Volume moyen: {Colors.BOLD}{df['volume'].mean():.2f}{Colors.END}")
        
        # Sauvegarde
        output_path = Path(output_file)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        print()
        print_info(f"💾 Sauvegarde au format {export_format.upper()}...")
        
        if export_format == 'csv':
            df.to_csv(output_path, index=False)
        elif export_format == 'json':
            df.to_json(output_path, orient='records', date_format='iso')
        elif export_format == 'parquet':
            df.to_parquet(output_path, index=False)
        
        file_size = output_path.stat().st_size / 1024
        print_success(f"✅ Fichier sauvegardé: {output_path} ({file_size:.1f} KB)")
        
        # Ajout à l'historique
        add_to_history({
            "exchange": exchange.id,
            "pair": pair,
            "timeframe": timeframe,
            "start_date": start_date.strftime('%Y-%m-%d %H:%M'),
            "end_date": end_date.strftime('%Y-%m-%d %H:%M'),
            "days": days,
            "candles": len(df),
            "output_file": str(output_path),
            "format": export_format
        })
        
        return True
    
    except Exception as e:
        print_error(f"❌ Erreur: {str(e)}")
        return False

# ============================================================================
# 🎯 MODES INTERACTIFS
# ============================================================================

def quick_start_mode():
    """Mode Quick Start - Configuration rapide"""
    clear_screen()
    print_banner()
    print_section("⚡ MODE QUICK START")
    
    print("Configuration rapide avec valeurs par défaut\n")
    
    # Paramètres par défaut
    pair = get_user_input("Paire de trading", "ETH/USDT")
    
    # Sélection de la période
    start_date, end_date, days = get_date_range()
    
    print()
    print_info("🔍 Sélection automatique du meilleur exchange...")
    
    exchange, exchange_name, available_days = find_best_exchange(pair, '5m', days)
    
    if not exchange:
        print_error("Impossible de continuer")
        input("\nAppuyez sur Entrée pour continuer...")
        return
    
    if available_days < days:
        print_warning(f"\n⚠️  Seulement {available_days:.0f} jours disponibles sur {exchange_name}")
        print_info(f"   Période demandée: {days} jours")
        print_info(f"   Période disponible: ~{available_days:.0f} jours")
        if not confirm_action("Continuer quand même ?", True):
            return
    
    output_file = f"Downloads/{pair.replace('/', '_')}_{start_date.strftime('%Y%m%d_%H%M')}_{end_date.strftime('%Y%m%d_%H%M')}.csv"
    
    print()
    if download_from_exchange(exchange, pair, '5m', start_date, end_date, output_file):
        print_success("\n🎉 Téléchargement terminé avec succès!")
    
    input("\nAppuyez sur Entrée pour continuer...")

def advanced_mode():
    """Mode Advanced - Configuration complète"""
    clear_screen()
    print_banner()
    print_section("🔧 MODE ADVANCED")
    
    # Étape 1: Choix de la paire
    pair = get_user_input("Paire de trading", "ETH/USDT")
    
    # Étape 2: Choix du timeframe
    print()
    tf_choice = display_menu(
        "Sélection du timeframe",
        TIMEFRAMES,
        allow_back=False
    )
    timeframe = TIMEFRAMES[tf_choice - 1]
    
    # Étape 3: Sélection de la période
    print()
    start_date, end_date, days = get_date_range()
    
    # Étape 4: Choix de l'exchange
    print()
    exchange_options = EXCHANGES + ["Auto (meilleur exchange)"]
    ex_choice = display_menu(
        "Sélection de l'exchange",
        exchange_options,
        allow_back=False
    )
    
    if ex_choice == len(exchange_options):
        # Mode auto
        exchange, exchange_name, available_days = find_best_exchange(pair, timeframe, days)
    else:
        # Exchange spécifique
        exchange_name = EXCHANGES[ex_choice - 1]
        print_info(f"Initialisation de {exchange_name}...")
        exchange = get_exchange(exchange_name)
        available_days = days
    
    if not exchange:
        print_error("Impossible de continuer")
        input("\nAppuyez sur Entrée pour continuer...")
        return
    
    # Étape 5: Format d'export
    print()
    fmt_choice = display_menu(
        "Format d'export",
        [f.upper() for f in EXPORT_FORMATS],
        allow_back=False
    )
    export_format = EXPORT_FORMATS[fmt_choice - 1]
    
    # Étape 6: Fichier de sortie
    print()
    default_output = f"Downloads/{pair.replace('/', '_')}_{start_date.strftime('%Y%m%d_%H%M')}_{end_date.strftime('%Y%m%d_%H%M')}.{export_format}"
    output_file = get_user_input("Fichier de sortie", default_output)
    
    # Validation finale
    print()
    print_section("📋 VALIDATION DE LA CONFIGURATION")
    print(f"   Paire: {Colors.BOLD}{pair}{Colors.END}")
    print(f"   Timeframe: {Colors.BOLD}{timeframe}{Colors.END}")
    print(f"   Période: {Colors.BOLD}{start_date.strftime('%Y-%m-%d %H:%M')} → {end_date.strftime('%Y-%m-%d %H:%M')}{Colors.END}")
    print(f"   Durée: {Colors.BOLD}{days} jours{Colors.END}")
    print(f"   Exchange: {Colors.BOLD}{exchange_name}{Colors.END}")
    print(f"   Format: {Colors.BOLD}{export_format.upper()}{Colors.END}")
    print(f"   Fichier: {Colors.BOLD}{output_file}{Colors.END}")
    print()
    
    if not confirm_action("Tout est correct ?", True):
        print_warning("Configuration annulée")
        input("\nAppuyez sur Entrée pour continuer...")
        return
    
    # Sauvegarde optionnelle en favori
    if confirm_action("\n💾 Sauvegarder cette configuration en favori ?", False):
        config = load_config()
        fav_name = get_user_input("Nom du favori", f"{pair}_{timeframe}")
        
        config["favorites"].append({
            "name": fav_name,
            "pair": pair,
            "timeframe": timeframe,
            "start_date": start_date.strftime('%Y-%m-%d %H:%M'),
            "end_date": end_date.strftime('%Y-%m-%d %H:%M'),
            "days": days,
            "exchange": exchange_name,
            "format": export_format
        })
        save_config(config)
        print_success("✅ Configuration sauvegardée!")
    
    # Téléchargement
    print()
    if download_from_exchange(exchange, pair, timeframe, start_date, end_date, output_file, export_format):
        print_success("\n🎉 Téléchargement terminé avec succès!")
    
    input("\nAppuyez sur Entrée pour continuer...")

def favorites_mode():
    """Mode Favoris - Utiliser une configuration sauvegardée"""
    clear_screen()
    print_banner()
    
    config = load_config()
    favorites = config.get("favorites", [])
    
    if not favorites:
        print_warning("Aucun favori sauvegardé")
        input("\nAppuyez sur Entrée pour continuer...")
        return
    
    fav_names = [f"{f['name']} ({f['pair']} - {f['timeframe']})" for f in favorites]
    
    choice = display_menu("💾 FAVORIS SAUVEGARDÉS", fav_names)
    
    if choice == 0:
        return
    
    fav = favorites[choice - 1]
    
    print_section(f"Configuration: {fav['name']}")
    print(f"   Paire: {Colors.BOLD}{fav['pair']}{Colors.END}")
    print(f"   Timeframe: {Colors.BOLD}{fav['timeframe']}{Colors.END}")
    
    # Gestion des anciennes configs (avec 'days') et nouvelles (avec dates + heures)
    if 'start_date' in fav and 'end_date' in fav:
        # Essayer de parser avec heures, sinon sans heures
        try:
            start_date = datetime.strptime(fav['start_date'], '%Y-%m-%d %H:%M')
            end_date = datetime.strptime(fav['end_date'], '%Y-%m-%d %H:%M')
            print(f"   Période: {Colors.BOLD}{fav['start_date']} → {fav['end_date']}{Colors.END}")
        except ValueError:
            start_date = datetime.strptime(fav['start_date'], '%Y-%m-%d')
            end_date = datetime.strptime(fav['end_date'], '%Y-%m-%d')
            print(f"   Période: {Colors.BOLD}{fav['start_date']} → {fav['end_date']}{Colors.END}")
        
        print(f"   Durée: {Colors.BOLD}{fav['days']} jours{Colors.END}")
    else:
        # Ancien format avec seulement 'days'
        print(f"   Jours: {Colors.BOLD}{fav['days']}{Colors.END}")
        end_date = datetime.now()
        start_date = end_date - timedelta(days=fav['days'])
    
    print(f"   Exchange: {Colors.BOLD}{fav['exchange']}{Colors.END}")
    print()
    
    if not confirm_action("Utiliser cette configuration ?", True):
        return
    
    exchange = get_exchange(fav['exchange'])
    if not exchange:
        print_error("Impossible d'initialiser l'exchange")
        input("\nAppuyez sur Entrée pour continuer...")
        return
    
    output_file = f"Downloads/{fav['pair'].replace('/', '_')}_{start_date.strftime('%Y%m%d_%H%M')}_{end_date.strftime('%Y%m%d_%H%M')}.{fav.get('format', 'csv')}"
    
    if download_from_exchange(exchange, fav['pair'], fav['timeframe'], 
                             start_date, end_date, output_file, fav.get('format', 'csv')):
        print_success("\n🎉 Téléchargement terminé avec succès!")
    
    input("\nAppuyez sur Entrée pour continuer...")

def history_mode():
    """Affiche l'historique des téléchargements"""
    clear_screen()
    print_banner()
    print_section("📜 HISTORIQUE DES TÉLÉCHARGEMENTS")
    
    history = get_history()
    
    if not history:
        print_warning("Aucun historique disponible")
        input("\nAppuyez sur Entrée pour continuer...")
        return
    
    for i, item in enumerate(reversed(history[-20:]), 1):
        timestamp = datetime.fromisoformat(item['timestamp']).strftime('%Y-%m-%d %H:%M')
        print(f"{Colors.CYAN}{i}.{Colors.END} {timestamp} - {item['pair']} ({item['timeframe']})")
        print(f"   Exchange: {item['exchange']}", end='')
        
        # Gestion de l'ancien format (days) et nouveau format (dates)
        if 'start_date' in item and 'end_date' in item:
            print(f" | Période: {item['start_date']} → {item['end_date']}", end='')
        else:
            print(f" | Jours: {item.get('days', 'N/A')}", end='')
        
        print(f" | Bougies: {item['candles']}")
        print(f"   Fichier: {item['output_file']}")
        print()
    
    input("Appuyez sur Entrée pour continuer...")

# ============================================================================
# 🎯 MENU PRINCIPAL
# ============================================================================

def main_menu():
    """Menu principal de l'application"""
    init_directories()
    
    while True:
        clear_screen()
        print_banner()
        
        choice = display_menu(
            "🎯 MENU PRINCIPAL",
            [
                "⚡ Quick Start (Configuration rapide)",
                "🔧 Advanced Mode (Configuration complète)",
                "💾 Favoris (Configurations sauvegardées)",
                "📜 Historique des téléchargements",
                "❌ Quitter"
            ],
            allow_back=False
        )
        
        if choice == 1:
            quick_start_mode()
        elif choice == 2:
            advanced_mode()
        elif choice == 3:
            favorites_mode()
        elif choice == 4:
            history_mode()
        elif choice == 5:
            print()
            print_success("👋 Au revoir!")
            break

# ============================================================================
# 🚀 POINT D'ENTRÉE
# ============================================================================

if __name__ == "__main__":
    try:
        main_menu()
    except KeyboardInterrupt:
        print()
        print_warning("\n⚠️  Programme interrompu par l'utilisateur")
        print_success("👋 Au revoir!")
    except Exception as e:
        print_error(f"\n❌ Erreur fatale: {str(e)}")
        sys.exit(1)