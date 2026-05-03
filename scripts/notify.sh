#!/usr/bin/env bash
# ╔══════════════════════════════════════════════════════════════════╗
# ║         BULLET-1 — Système de notifications CI/CD               ║
# ║                                                                  ║
# ║  Usage : scripts/notify.sh <type> [args...]                     ║
# ║                                                                  ║
# ║  Configuration (GitHub Secrets) :                               ║
# ║    TELEGRAM_BOT_TOKEN  + TELEGRAM_CHAT_ID  → active Telegram    ║
# ║    DISCORD_WEBHOOK_URL                     → active Discord      ║
# ║  Si un secret est absent → canal silencieusement ignoré         ║
# ╚══════════════════════════════════════════════════════════════════╝

set -euo pipefail

# ── Couleurs Discord (décimales) ──────────────────────────────────
COLOR_INFO=3447003      # Bleu
COLOR_SUCCESS=3066993   # Vert
COLOR_WARNING=16776960  # Jaune
COLOR_ERROR=15158332    # Rouge
COLOR_PROGRESS=10181046 # Violet

# ── Envoi Telegram ────────────────────────────────────────────────
send_telegram() {
  local message="$1"
  local parse_mode="${2:-HTML}"

  [[ -z "${TELEGRAM_BOT_TOKEN:-}" || -z "${TELEGRAM_CHAT_ID:-}" ]] && return 0

  local response
  response=$(curl -s -X POST \
    "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    -H "Content-Type: application/json" \
    -d "{
      \"chat_id\": \"${TELEGRAM_CHAT_ID}\",
      \"text\": $(echo "$message" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))'),
      \"parse_mode\": \"${parse_mode}\",
      \"disable_web_page_preview\": true
    }" 2>/dev/null)

  if echo "$response" | python3 -c "import json,sys; d=json.load(sys.stdin); exit(0 if d.get('ok') else 1)" 2>/dev/null; then
    echo "  [Telegram] ✅ Message envoyé"
  else
    echo "  [Telegram] ⚠️  Échec envoi (response: $(echo "$response" | head -c 200))"
  fi
}

# ── Envoi Discord ─────────────────────────────────────────────────
send_discord() {
  local title="$1"
  local description="$2"
  local color="${3:-$COLOR_INFO}"
  local fields_json="${4:-[]}"

  [[ -z "${DISCORD_WEBHOOK_URL:-}" ]] && return 0

  local payload
  payload=$(python3 -c "
import json, sys
payload = {
  'embeds': [{
    'title': sys.argv[1],
    'description': sys.argv[2],
    'color': int(sys.argv[3]),
    'fields': json.loads(sys.argv[4]),
    'footer': {'text': 'BULLET-1 Trading Bot • GitHub Actions'},
    'timestamp': __import__('datetime').datetime.utcnow().isoformat()
  }]
}
print(json.dumps(payload))
" "$title" "$description" "$color" "$fields_json")

  local response
  response=$(curl -s -o /dev/null -w "%{http_code}" -X POST \
    "${DISCORD_WEBHOOK_URL}" \
    -H "Content-Type: application/json" \
    -d "$payload" 2>/dev/null)

  if [[ "$response" == "204" || "$response" == "200" ]]; then
    echo "  [Discord] ✅ Message envoyé (HTTP $response)"
  else
    echo "  [Discord] ⚠️  Échec envoi (HTTP $response)"
  fi
}

# ── Helpers ───────────────────────────────────────────────────────
current_time() { date '+%Y-%m-%d %H:%M UTC'; }
run_url() {
  echo "https://github.com/${GITHUB_REPOSITORY:-user/repo}/actions/runs/${GITHUB_RUN_ID:-0}"
}

# ══════════════════════════════════════════════════════════════════
#  TYPES DE NOTIFICATIONS
# ══════════════════════════════════════════════════════════════════

notify_start() {
  local phase="$1"
  local config="${2:-config/config.json}"
  local top_n="${3:-10}"
  local actor="${GITHUB_ACTOR:-manual}"
  local run_num="${GITHUB_RUN_NUMBER:-?}"

  # Estimation durée par phase (sur runner GitHub, ~3x plus rapide que Termux)
  local eta
  case "$phase" in
    "2a")  eta="~30 min" ;;
    "2b")  eta="~18 min" ;;
    "2c")  eta="~5 min"  ;;
    "all") eta="~55 min" ;;
    *)     eta="inconnu"  ;;
  esac

  local phase_label
  case "$phase" in
    "2a")  phase_label="Paramètres stratégie (432 runs)" ;;
    "2b")  phase_label="Indicateurs externes (243 runs)" ;;
    "2c")  phase_label="Toggles avancés (72 runs)"       ;;
    "all") phase_label="Toutes les phases (747 runs)"    ;;
    *)     phase_label="Phase $phase" ;;
  esac

  # Telegram
  send_telegram "🚀 <b>BULLET-1 — Optimisation démarrée</b>

📌 <b>Phase</b> : ${phase^^} — ${phase_label}
⏱ <b>ETA estimée</b> : ${eta}
⚙️ <b>Config</b> : ${config}
🏆 <b>Top N</b> : ${top_n} configs sauvegardées
👤 <b>Déclenché par</b> : ${actor}
🔢 <b>Run</b> : #${run_num}
🕐 <b>Démarrage</b> : $(current_time)

🔗 <a href=\"$(run_url)\">Voir le run Actions</a>"

  # Discord
  send_discord \
    "🚀 Optimisation démarrée — Phase ${phase^^}" \
    "Le grid search vient de commencer." \
    "$COLOR_INFO" \
    "[
      {\"name\": \"Phase\", \"value\": \"**${phase^^}** — ${phase_label}\", \"inline\": false},
      {\"name\": \"⏱ ETA estimée\", \"value\": \"${eta}\", \"inline\": true},
      {\"name\": \"🏆 Top N\", \"value\": \"${top_n} configs\", \"inline\": true},
      {\"name\": \"👤 Lancé par\", \"value\": \"${actor}\", \"inline\": true},
      {\"name\": \"🔗 Lien\", \"value\": \"[Voir le run]($(run_url))\", \"inline\": false}
    ]"
}

notify_phase_done() {
  local phase="$1"
  local runs_total="${2:-?}"
  local runs_valid="${3:-?}"
  local next_phase="${4:-}"

  local msg_next=""
  [[ -n "$next_phase" ]] && msg_next="
➡️ <b>Prochaine phase</b> : ${next_phase^^} en cours..."

  send_telegram "✅ <b>Phase ${phase^^} terminée !</b>

📊 <b>Runs</b> : ${runs_valid} valides / ${runs_total} total
🕐 <b>Terminé</b> : $(current_time)${msg_next}

🔗 <a href=\"$(run_url)\">Voir les détails</a>"

  local fields_next=""
  [[ -n "$next_phase" ]] && fields_next=",{\"name\": \"➡️ Suivant\", \"value\": \"Phase ${next_phase^^} lancée\", \"inline\": false}"

  send_discord \
    "✅ Phase ${phase^^} terminée" \
    "Le grid search de la phase **${phase^^}** est complet." \
    "$COLOR_SUCCESS" \
    "[
      {\"name\": \"📊 Runs valides\", \"value\": \"**${runs_valid}** / ${runs_total}\", \"inline\": true},
      {\"name\": \"🕐 Heure\", \"value\": \"$(current_time)\", \"inline\": true}
      ${fields_next}
    ]"
}

notify_best_config() {
  local phase="$1"
  local config_name="$2"
  local sharpe="${3:-N/A}"
  local profit_factor="${4:-N/A}"
  local win_rate="${5:-N/A}"
  local drawdown="${6:-N/A}"
  local run_current="${7:-?}"
  local run_total="${8:-?}"

  send_telegram "🏆 <b>Nouveau meilleur résultat — Phase ${phase^^}</b>

📋 <b>Config</b> : <code>${config_name}</code>
📈 <b>Sharpe Ratio</b> : ${sharpe}
⚖️ <b>Profit Factor</b> : ${profit_factor}
🎯 <b>Win Rate</b> : ${win_rate}%
📉 <b>Max Drawdown</b> : ${drawdown}%

🔢 Progression : run ${run_current}/${run_total}"

  send_discord \
    "🏆 Nouveau meilleur résultat — Phase ${phase^^}" \
    "Une meilleure configuration vient d'être trouvée !" \
    "$COLOR_WARNING" \
    "[
      {\"name\": \"📋 Config\", \"value\": \"\`${config_name}\`\", \"inline\": false},
      {\"name\": \"📈 Sharpe\", \"value\": \"**${sharpe}**\", \"inline\": true},
      {\"name\": \"⚖️ Profit Factor\", \"value\": \"**${profit_factor}**\", \"inline\": true},
      {\"name\": \"🎯 Win Rate\", \"value\": \"**${win_rate}%**\", \"inline\": true},
      {\"name\": \"📉 Drawdown\", \"value\": \"${drawdown}%\", \"inline\": true},
      {\"name\": \"🔢 Progression\", \"value\": \"Run ${run_current}/${run_total}\", \"inline\": true}
    ]"
}

notify_progress() {
  local phase="$1"
  local percent="$2"      # ex: 25, 50, 75
  local run_current="$3"
  local run_total="$4"
  local elapsed="${5:-?}"
  local best_sharpe="${6:-N/A}"

  # Barre de progression ASCII
  local filled=$(( percent / 5 ))
  local empty=$(( 20 - filled ))
  local bar=""
  for ((i=0; i<filled; i++)); do bar+="█"; done
  for ((i=0; i<empty;  i++)); do bar+="░"; done

  send_telegram "⏳ <b>Progression — Phase ${phase^^} : ${percent}%</b>

[${bar}] ${percent}%
🔢 Run : ${run_current} / ${run_total}
⏱ Écoulé : ${elapsed}
📈 Meilleur Sharpe actuel : ${best_sharpe}

🔗 <a href=\"$(run_url)\">Voir le run</a>"

  send_discord \
    "⏳ Progression Phase ${phase^^} — ${percent}%" \
    "\`\`\`\n[${bar}] ${percent}%\n\`\`\`" \
    "$COLOR_PROGRESS" \
    "[
      {\"name\": \"🔢 Runs\", \"value\": \"${run_current} / ${run_total}\", \"inline\": true},
      {\"name\": \"⏱ Écoulé\", \"value\": \"${elapsed}\", \"inline\": true},
      {\"name\": \"📈 Best Sharpe\", \"value\": \"${best_sharpe}\", \"inline\": true}
    ]"
}

notify_final_summary() {
  local phase="$1"
  local best_config="$2"
  local sharpe="$3"
  local profit_factor="$4"
  local win_rate="$5"
  local drawdown="$6"
  local total_trades="$7"
  local branch="${GITHUB_REF_NAME:-main}"

  send_telegram "🎯 <b>BULLET-1 — Optimisation terminée !</b>

📌 <b>Phase</b> : ${phase^^}
🏆 <b>Meilleure config</b> : <code>${best_config}</code>

<b>── Métriques clés ──</b>
📈 Sharpe Ratio     : <b>${sharpe}</b>
⚖️ Profit Factor    : <b>${profit_factor}</b>
🎯 Win Rate         : <b>${win_rate}%</b>
📉 Max Drawdown     : <b>${drawdown}%</b>
🔢 Total Trades     : <b>${total_trades}</b>

💾 Résultats commités sur branche <code>${branch}</code>

📲 <b>Récupère tes résultats :</b>
<code>git pull</code>

🔗 <a href=\"$(run_url)\">Voir le run complet</a>"

  send_discord \
    "🎯 Optimisation complète — Phase ${phase^^}" \
    "Tous les résultats sont disponibles. **Lance \`git pull\` pour les récupérer !**" \
    "$COLOR_SUCCESS" \
    "[
      {\"name\": \"🏆 Meilleure config\", \"value\": \"\`${best_config}\`\", \"inline\": false},
      {\"name\": \"📈 Sharpe Ratio\", \"value\": \"**${sharpe}**\", \"inline\": true},
      {\"name\": \"⚖️ Profit Factor\", \"value\": \"**${profit_factor}**\", \"inline\": true},
      {\"name\": \"🎯 Win Rate\", \"value\": \"**${win_rate}%**\", \"inline\": true},
      {\"name\": \"📉 Drawdown\", \"value\": \"**${drawdown}%**\", \"inline\": true},
      {\"name\": \"🔢 Trades\", \"value\": \"**${total_trades}**\", \"inline\": true},
      {\"name\": \"💾 Commande\", \"value\": \"\`git pull\`\", \"inline\": false}
    ]"
}

notify_error() {
  local phase="$1"
  local error_msg="$2"
  local step="${3:-inconnu}"

  send_telegram "❌ <b>BULLET-1 — ERREUR !</b>

📌 Phase : ${phase^^}
🔍 Étape : ${step}

<b>Message :</b>
<code>${error_msg}</code>

🕐 $(current_time)
🔗 <a href=\"$(run_url)\">Voir les logs complets</a>"

  send_discord \
    "❌ Erreur — Phase ${phase^^}" \
    "Une erreur a interrompu l'optimisation." \
    "$COLOR_ERROR" \
    "[
      {\"name\": \"🔍 Étape\", \"value\": \"${step}\", \"inline\": true},
      {\"name\": \"🕐 Heure\", \"value\": \"$(current_time)\", \"inline\": true},
      {\"name\": \"💬 Erreur\", \"value\": \"\`\`\`${error_msg}\`\`\`\", \"inline\": false},
      {\"name\": \"🔗 Logs\", \"value\": \"[Voir les logs complets]($(run_url))\", \"inline\": false}
    ]"
}

# ── Dispatcher principal ──────────────────────────────────────────
COMMAND="${1:-}"
shift || true

case "$COMMAND" in
  start)         notify_start "$@" ;;
  phase_done)    notify_phase_done "$@" ;;
  best_config)   notify_best_config "$@" ;;
  progress)      notify_progress "$@" ;;
  final_summary) notify_final_summary "$@" ;;
  error)         notify_error "$@" ;;
  *)
    echo "Usage: notify.sh <start|phase_done|best_config|progress|final_summary|error> [args...]"
    exit 1
    ;;
esac
