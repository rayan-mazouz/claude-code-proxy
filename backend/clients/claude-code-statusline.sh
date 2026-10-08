#!/usr/bin/env bash
# Claude Code status line for the Claude Code Proxy.
#
# Reproduces the look of https://github.com/leeguooooo/claude-code-usage-bar, but sourced for a relay:
# the proxy is not api.anthropic.com, so Anthropic's official 5h/7d rate-limit headers never reach Claude
# Code's stdin. Instead the two rate-limit bars come from the proxy's own pool telemetry via GET /v1/me/usage
# (pool.five_hour.used_pct / pool.weekly.used_pct + reset timestamps), while the model, context window, and the
# prompt-cache countdown come from the session JSON Claude Code pipes in on stdin. So `5h`/`7d` here are the
# AVAILABLE pool's average usage, not a personal Anthropic window.
#
#   5h[██░░░░░░] 38% ⏰3h12m │ 7d[███████░] 93% ⏰2d3h │ Opus 4.8 (64.6k/1.0M) │ cache 4m23s
#   ⤷ claude-code-proxy ⎇ main● · +182 -47 · ⏱ 12m
#
# Data sources:
#   - 5h / 7d % + reset timers ......... GET /v1/me/usage (disk-cached 30s; degrades quietly if unreachable)
#   - model name + context window ...... Claude Code stdin (model.display_name, context_window.*)
#   - cache 4m23s / COLD ............... tail-read of the active transcript JSONL (TTL auto-detected 1h vs 5m)
#   - project / branch / lines / ⏱ ..... stdin workspace+cost, plus a 5s-cached `git` for branch & dirty dot
#
# Requirements: bash, curl, jq, coreutils (GNU date). Honors NO_COLOR.
#
# Install: copy somewhere, `chmod +x`, and point Claude Code at it in ~/.claude/settings.json. For the cache
# countdown to tick every second, also set refreshInterval to 1:
#   { "statusLine": { "type": "command", "command": "/abs/path/claude-code-statusline.sh", "refreshInterval": 1 } }

set -uo pipefail  # deliberately NOT -e: every segment degrades to empty rather than aborting the line.

# Claude Code pipes a session JSON object on stdin. Capture it (may be empty when run by hand).
stdin_json="$(cat 2>/dev/null || true)"

# Cache files live in a shared temp dir and hold pool data fetched with this user's key; keep them private.
umask 077
cachedir="${TMPDIR:-/tmp}"
now="$(date +%s)"

# ---- colors ------------------------------------------------------------------
if [ -n "${NO_COLOR:-}" ]; then color=0; else color=1; fi
ESC=$'\033'
DIM=90; GREEN=32; YELLOW=33; RED=31; CYAN=36; GREY=90
paint() {  # paint <code> <text...>
  local code="$1"; shift
  if [ "$color" = 1 ]; then printf '%s' "${ESC}[${code}m$*${ESC}[0m"; else printf '%s' "$*"; fi
}
sev_color() {  # echo an ANSI code for a "% used" value (repo defaults: 30 / 70)
  local p="${1%.*}"; p="${p:-0}"
  if   [ "$p" -ge 70 ] 2>/dev/null; then echo "$RED"
  elif [ "$p" -ge 30 ] 2>/dev/null; then echo "$YELLOW"
  else echo "$GREEN"; fi
}

# ---- small helpers -----------------------------------------------------------
sj() { printf '%s' "$stdin_json" | jq -r "$1" 2>/dev/null || true; }  # query the stdin JSON

human() {  # 64586 -> 64.6k ; 1000000 -> 1.0M ; 0 -> 0
  awk -v n="${1:-0}" 'BEGIN{
    n=n+0;
    if (n>=1000000) printf "%.1fM", n/1000000;
    else if (n>=1000) printf "%.1fk", n/1000;
    else printf "%d", n;
  }'
}

fmt_eta() {  # seconds-until -> "2d3h" / "3h12m" / "12m" / "now"
  local s="${1:-0}"; [ "$s" -le 0 ] 2>/dev/null && { printf 'now'; return; }
  local d=$((s/86400)) h=$(((s%86400)/3600)) m=$(((s%3600)/60))
  if   [ "$d" -gt 0 ]; then printf '%dd%dh' "$d" "$h"
  elif [ "$h" -gt 0 ]; then printf '%dh%dm' "$h" "$m"
  else printf '%dm' "$m"; fi
}

bar() {  # bar <pct 0-100> -> 8-cell battery, colored by severity
  local pct="${1:-0}"; pct="${pct%.*}"; [ -z "$pct" ] && pct=0
  [ "$pct" -lt 0 ] 2>/dev/null && pct=0; [ "$pct" -gt 100 ] 2>/dev/null && pct=100
  local cells=8 filled
  filled=$(( (pct * cells + 50) / 100 ))
  [ "$filled" -gt "$cells" ] && filled=$cells
  local i out=""
  for ((i=0; i<cells; i++)); do [ "$i" -lt "$filled" ] && out+="█" || out+="░"; done
  paint "$(sev_color "$pct")" "$out"
}

eta_until() {  # ISO8601 -> seconds from now (empty if unparseable / absent)
  local iso="$1" ep
  [ -n "$iso" ] && [ "$iso" != "null" ] || return 0
  ep="$(date -d "$iso" +%s 2>/dev/null)" || return 0
  echo $(( ep - now ))
}

# ---- rate-limit segments (5h / 7d) from the proxy ----------------------------
base="${ANTHROPIC_BASE_URL:-}"; token="${ANTHROPIC_AUTH_TOKEN:-}"
settings="${HOME}/.claude/settings.json"
if { [ -z "$base" ] || [ -z "$token" ]; } && [ -f "$settings" ]; then
  [ -n "$base" ]  || base="$(jq -r '.env.ANTHROPIC_BASE_URL // empty' "$settings" 2>/dev/null)"
  [ -n "$token" ] || token="$(jq -r '.env.ANTHROPIC_AUTH_TOKEN // empty' "$settings" 2>/dev/null)"
fi

ratelimit_seg=""
if [ -n "$base" ] && [ -n "$token" ]; then
  usage_cache="${cachedir}/ccproxy-usage-$(id -u).json"
  umtime="$(stat -c %Y "$usage_cache" 2>/dev/null || stat -f %m "$usage_cache" 2>/dev/null || echo 0)"
  if [ ! -s "$usage_cache" ] || [ "$(( now - umtime ))" -ge 30 ]; then
    # Never write directly to the cache: curl truncates its output file before
    # connecting, so a transient outage would erase the last good pool value.
    usage_tmp="${usage_cache}.tmp.$$"
    usage_url="${base%/}"
    case "$usage_url" in
      */api/v1|*/v1) usage_url+="/me/usage" ;;
      */api) usage_url+="/v1/me/usage" ;;
      *) usage_url+="/v1/me/usage" ;;
    esac
    if curl -fsS --max-time 5 -H "Authorization: Bearer ${token}" "$usage_url" -o "$usage_tmp" 2>/dev/null; then
      mv -f "$usage_tmp" "$usage_cache"
    else
      rm -f "$usage_tmp"
    fi
  fi
  if [ -s "$usage_cache" ]; then
    s_pct="$(jq -r '.pool.five_hour.used_pct // empty' "$usage_cache" 2>/dev/null)"
    w_pct="$(jq -r '.pool.weekly.used_pct // empty'  "$usage_cache" 2>/dev/null)"
    s_reset="$(jq -r '.pool.five_hour.next_reset_at // empty' "$usage_cache" 2>/dev/null)"
    w_reset="$(jq -r '.pool.weekly.next_reset_at // empty'  "$usage_cache" 2>/dev/null)"
    if [ -n "$s_pct" ]; then
      sp=$(awk -v x="$s_pct" 'BEGIN{printf "%d", x*100+0.5}')
      seg="$(paint "$DIM" 5h)[$(bar "$sp")] $(paint "$(sev_color "$sp")" "${sp}%")"
      eta="$(eta_until "$s_reset")"; [ -n "$eta" ] && seg+=" $(paint "$DIM" "⏰$(fmt_eta "$eta")")"
      ratelimit_seg="$seg"
    fi
    if [ -n "$w_pct" ]; then
      wp=$(awk -v x="$w_pct" 'BEGIN{printf "%d", x*100+0.5}')
      seg="$(paint "$DIM" 7d)[$(bar "$wp")] $(paint "$(sev_color "$wp")" "${wp}%")"
      eta="$(eta_until "$w_reset")"; [ -n "$eta" ] && seg+=" $(paint "$DIM" "⏰$(fmt_eta "$eta")")"
      [ -n "$ratelimit_seg" ] && ratelimit_seg+="$(paint "$DIM" ' │ ')"
      ratelimit_seg+="$seg"
    fi
  fi
fi

# ---- model + context window from stdin ---------------------------------------
model_seg=""
model="$(sj '.model.display_name // empty')"
# Drop a "(... context)" suffix — the context readout below already conveys size.
model="$(printf '%s' "$model" | sed -E 's/[[:space:]]*\([^)]*[Cc]ontext[^)]*\)//')"
if [ -n "$model" ]; then
  ctx_size="$(sj '.context_window.context_window_size // empty')"
  ctx_in="$(sj '.context_window.total_input_tokens // 0')"
  ctx_out="$(sj '.context_window.total_output_tokens // 0')"
  ctx_pct="$(sj '.context_window.used_percentage // empty')"
  model_seg="$(paint "$CYAN" "$model")"
  if [ -n "$ctx_size" ] && [ "$ctx_size" != "0" ]; then
    used=$(( ${ctx_in:-0} + ${ctx_out:-0} ))
    cc="$DIM"; [ -n "$ctx_pct" ] && cc="$(sev_color "${ctx_pct%.*}")"
    model_seg+=" $(paint "$cc" "($(human "$used")/$(human "$ctx_size"))")"
  fi
fi

# ---- prompt-cache countdown from the transcript ------------------------------
cache_seg=""
tp="$(sj '.transcript_path // empty')"
if [ -n "$tp" ] && [ -f "$tp" ]; then
  read -r c_ts c_ttl <<<"$(tail -n 400 "$tp" 2>/dev/null | jq -rs '
      (map(select(.type=="assistant"))) as $a
      | (($a | map(select(.timestamp)) | last | .timestamp) // "") as $ts
      | (($a | map(.message.usage.cache_creation) | map(select(. != null))
             | map(if .ephemeral_1h_input_tokens > 0 then 3600
                   elif .ephemeral_5m_input_tokens > 0 then 300 else empty end)
             | last) // 0) as $ttl
      | "\($ts) \($ttl)"' 2>/dev/null)"
  if [ -n "${c_ts:-}" ] && [ "$c_ts" != "null" ]; then
    c_ttl="${c_ttl:-0}"; [ "$c_ttl" = 0 ] && c_ttl=300
    c_ep="$(date -d "$c_ts" +%s 2>/dev/null || echo 0)"
    if [ "$c_ep" != 0 ]; then
      age=$(( now - c_ep )); [ "$age" -lt 0 ] && age=0
      rem=$(( c_ttl - age ))
      if [ "$rem" -le 0 ]; then
        cache_seg="$(paint "$RED" 'cache COLD')"
      else
        if   [ "$rem" -ge 3600 ]; then ctxt=$(printf 'cache %dh%02dm%02ds' $((rem/3600)) $(((rem%3600)/60)) $((rem%60)))
        elif [ "$rem" -ge 60 ];   then ctxt=$(printf 'cache %dm%02ds' $((rem/60)) $((rem%60)))
        else ctxt=$(printf 'cache %ds' "$rem"); fi
        [ "$rem" -lt 60 ] && cache_seg="$(paint "$YELLOW" "$ctxt")" || cache_seg="$(paint "$GREEN" "$ctxt")"
      fi
    fi
  fi
fi

# ---- assemble line 1 ---------------------------------------------------------
SEP="$(paint "$DIM" ' │ ')"
line1=""
for s in "$ratelimit_seg" "$model_seg" "$cache_seg"; do
  [ -n "$s" ] || continue
  [ -n "$line1" ] && line1+="$SEP"
  line1+="$s"
done

# ---- identity line (project ⎇ branch · +/- · ⏱) -----------------------------
proj="$(sj '.workspace.repo.name // empty')"
cwd="$(sj '.workspace.current_dir // .cwd // empty')"
[ -n "$proj" ] || { [ -n "$cwd" ] && proj="$(basename "$cwd")"; }

branch=""; dirty=0
if [ -n "$cwd" ] && [ -d "$cwd" ]; then
  gkey="$(printf '%s' "$cwd" | cksum | cut -d' ' -f1)"
  gcache="${cachedir}/ccproxy-git-${gkey}"
  gmtime="$(stat -c %Y "$gcache" 2>/dev/null || stat -f %m "$gcache" 2>/dev/null || echo 0)"
  if [ -s "$gcache" ] && [ "$(( now - gmtime ))" -lt 5 ]; then
    IFS=$'\t' read -r branch dirty <"$gcache"
  else
    branch="$(git -C "$cwd" symbolic-ref --quiet --short HEAD 2>/dev/null || git -C "$cwd" rev-parse --short HEAD 2>/dev/null || true)"
    if [ -n "$branch" ] && [ -n "$(git -C "$cwd" status --porcelain 2>/dev/null)" ]; then dirty=1; fi
    printf '%s\t%s' "$branch" "$dirty" >"$gcache" 2>/dev/null || true
  fi
fi

line2=""
if [ -n "$proj" ]; then
  line2="$(paint "$DIM" '⤷ ')$(paint "$CYAN" "$proj")"
  if [ -n "$branch" ]; then
    line2+=" $(paint "$DIM" '⎇') $(paint "$GREY" "$branch")"
    [ "${dirty:-0}" = 1 ] && line2+="$(paint "$YELLOW" '●')"
  fi
  add="$(sj '.cost.total_lines_added // 0')"; rml="$(sj '.cost.total_lines_removed // 0')"
  if [ "${add:-0}" != 0 ] || [ "${rml:-0}" != 0 ]; then
    line2+=" $(paint "$DIM" '·') $(paint "$GREEN" "+${add}") $(paint "$RED" "-${rml}")"
  fi
  dur_ms="$(sj '.cost.total_duration_ms // 0')"
  if [ "${dur_ms:-0}" != 0 ]; then
    dsec=$(( dur_ms / 1000 ))
    if [ "$dsec" -ge 3600 ]; then dtxt=$(printf '%dh%dm' $((dsec/3600)) $(((dsec%3600)/60)))
    elif [ "$dsec" -ge 60 ]; then dtxt=$(printf '%dm' $((dsec/60)))
    else dtxt=$(printf '%ds' "$dsec"); fi
    line2+=" $(paint "$DIM" '·') $(paint "$DIM" "⏱ $dtxt")"
  fi
fi

# ---- emit --------------------------------------------------------------------
if [ -z "$line1" ] && [ -z "$line2" ]; then
  echo "ccproxy: status unavailable (need ANTHROPIC_BASE_URL/ANTHROPIC_AUTH_TOKEN + jq, or Claude Code stdin)"
  exit 0
fi
[ -n "$line1" ] && printf '%s\n' "$line1"
[ -n "$line2" ] && printf '%s\n' "$line2"
exit 0
