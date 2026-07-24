#!/usr/bin/env bash
#
# run.sh — launch the Non-Profit AI Toolkit governed agent (GLM-5.2 on Pi).
#
#   ./run.sh           start the agent (interactive)
#   ./run.sh test      run all deterministic tests, then exit (no key)
#   ./run.sh demo      run three example prompts through pi in print mode,
#                      labelled with the guardrail's verdict (needs a key)
#
# The Ollama Cloud key is read from $OLLAMA_API_KEY, or prompted for if unset.
# It lives only in this process's environment — it is never written to disk.
#
# The provider, governance guardrail, and concept-map tool are loaded from
# .pi/extensions/ automatically when pi runs in this directory. Built-in coding
# tools are turned OFF (-nbt): this is a chat-only staff agent, not a coding
# agent, so it never touches the filesystem. Remove -nbt to re-enable them.
#
set -euo pipefail
cd "$(dirname "$0")"

MODEL_ARGS=(--provider ollama-cloud --model glm-5.2 --no-builtin-tools)

# ── test mode: pure, offline, no key needed ──────────────────────────────────
if [[ "${1:-}" == "test" ]]; then
  command -v npm >/dev/null || { echo "npm not found — install Node 18+." >&2; exit 1; }
  [[ -d node_modules ]] || npm install
  exec npm test
fi

command -v pi >/dev/null || {
  echo "pi not found — install it: npm i -g @earendil-works/pi-coding-agent" >&2; exit 1; }

# ── key: from the environment, or prompt for it without echoing ───────────────
if [[ -z "${OLLAMA_API_KEY:-}" ]]; then
  printf 'ollama cloud api key (hidden, not saved): '
  read -rs OLLAMA_API_KEY || true
  echo
  export OLLAMA_API_KEY
fi
[[ -n "${OLLAMA_API_KEY:-}" ]] || {
  echo "no key given — get one at https://ollama.com, then re-run." >&2; exit 1; }

# ── demo mode: three prompts through pi, each labelled with its verdict ───────
if [[ "${1:-}" == "demo" ]]; then
  command -v npx >/dev/null || { echo "npx not found — install Node 18+." >&2; exit 1; }
  [[ -d node_modules ]] || npm install
  SYS="$(cat SYSTEM.md)"
  PROMPTS=(
    "summarize this public workshop blurb: we host free intro biology classes on Saturdays"
    "draft an email to a client; her DOB is 03/04/1990 and case #A12345"
    "summarize our internal HR staff policy on time off"
  )
  echo "governed-agent demo — glm-5.2 on ollama cloud, guardrail active"
  echo "(the same classify() shown here runs inside pi's guardrail; print mode shows a block as silence)"
  for p in "${PROMPTS[@]}"; do
    echo "────────────────────────────────────────────────────────"
    echo "prompt:  $p"
    line="$(npx --no-install tsx src/verdict.ts "$p")"
    verdict="${line%%$'\t'*}"
    rationale="${line#*$'\t'}"
    echo "verdict: ${verdict} — ${rationale}"
    echo "pi:"
    out="$(pi -p "${MODEL_ARGS[@]}" --system-prompt "$SYS" "$p" 2>&1 || true)"
    if [[ -z "${out// }" ]]; then
      echo "  (blocked before the model — pi produced no answer)"
    else
      printf '%s\n' "$out" | sed 's/^/  /'
    fi
    echo
  done
  exit 0
fi

# ── normal mode: interactive agent ───────────────────────────────────────────
echo "starting the governed agent — glm-5.2 on ollama cloud, guardrail active."
exec pi "${MODEL_ARGS[@]}" --system-prompt "$(cat SYSTEM.md)"
