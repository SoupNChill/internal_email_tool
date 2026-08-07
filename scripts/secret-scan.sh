#!/usr/bin/env bash
#
# Secret scan.
#
# A real script rather than logic embedded in a workflow, for two reasons: it can
# be tested (§54 -- do not ship commands that have not been run), and it can be
# used locally before committing, which is where a leak is still cheap to fix.
#
#   scripts/secret-scan.sh [--staged] [PATH...]
#
# Patterns are specific to this project. A generic scanner catches generic
# things; the credentials that would actually hurt here are an MXRoute
# account-root key, a Fernet encryption key, and an emaild API key -- so those
# are matched exactly rather than hoped for.
#
set -euo pipefail

err()  { printf '\033[31m  LEAK\033[0m  %s\n' "$*" >&2; }
ok()   { printf '\033[32m  ok\033[0m    %s\n' "$*"; }
info() { printf '%s\n' "$*"; }

STAGED=0
declare -a TARGETS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --staged) STAGED=1; shift ;;
    *) TARGETS+=("$1"); shift ;;
  esac
done

# Files that legitimately contain secret-shaped material and are never committed.
readonly EXCLUDE_RE='^(\.venv/|\.git/|\.mypy_cache/|\.ruff_cache/|\.pytest_cache/|backups/|\.env$|mxroute_information\.md$)'

# Patterns, as parallel arrays.
#
# NOT a delimited string: the regexes contain "|" themselves, and splitting on
# it silently truncated them into patterns that matched nothing while still
# reporting "ok". A scanner that always passes is worse than no scanner.
readonly PATTERN_NAMES=(
  'mxroute-api-key'
  'emaild-api-key'
  'fernet-key'
  'private-key-block'
  'aws-access-key'
  'generic-assignment'
)
readonly PATTERN_REGEXES=(
  'Mx[0-9a-fA-F]{28,}K[0-9]'
  'em_live_[A-Za-z0-9_-]{20,}'
  '[A-Za-z0-9_-]{43}=(\s|$|")'
  '-----BEGIN [A-Z ]*PRIVATE KEY-----'
  'AKIA[0-9A-Z]{16}'
  '(?i)(password|passwd|secret|api[_-]?key|token)\s*[=:]\s*["'"'"'][^"'"'"'{}$][^"'"'"']{7,}["'"'"']'
)
readonly PATTERN_REASONS=(
  'MXRoute account-root credential: can delete mailboxes and manage reseller users'
  'emaild API key: grants sending as a scoped identity'
  'Fernet key: decrypts every stored SMTP password'
  'private key material'
  'AWS access key'
  'hard-coded credential'
)

# Literal values that are known-public and therefore not leaks. An explicit
# allowlist rather than excluding whole files: excluding mxroute_api.yaml
# wholesale would hide a REAL key if one were ever pasted into it, which is
# exactly the mistake this script exists to catch.
readonly ALLOWED=(
  'Mx8d989005f0cded8371b7d7271c50K1'   # MXRoute's own published API docs example
)

collect_files() {
  if [ "$STAGED" -eq 1 ]; then
    git diff --cached --name-only --diff-filter=ACM
  elif [ "${#TARGETS[@]}" -gt 0 ]; then
    for t in "${TARGETS[@]}"; do
      [ -d "$t" ] && find "$t" -type f || printf '%s\n' "$t"
    done
  else
    # --others --exclude-standard includes untracked files that are not
    # gitignored: a new file holding a leaked key is the case most worth
    # catching, and `git ls-files` alone would miss it entirely.
    git ls-files --cached --others --exclude-standard
  fi
}

mapfile -t FILES < <(collect_files | grep -vE "$EXCLUDE_RE" || true)

if [ "${#FILES[@]}" -eq 0 ]; then
  ok "no files to scan"
  exit 0
fi

found=0
for i in "${!PATTERN_NAMES[@]}"; do
  name="${PATTERN_NAMES[$i]}"
  regex="${PATTERN_REGEXES[$i]}"
  why="${PATTERN_REASONS[$i]}"

  hits=""
  for f in "${FILES[@]}"; do
    [ -f "$f" ] || continue
    case "$f" in
      # Documentation and tests may legitimately show the SHAPE of a
      # credential. Real ones are caught by the live-value check below.
      tests/*|*.example|docs/*|*.md) continue ;;
    esac
    if match="$(grep -nPI -m3 -- "$regex" "$f" 2>/dev/null)"; then
      filtered="$match"
      for allowed in "${ALLOWED[@]}"; do
        filtered="$(printf '%s\n' "$filtered" | grep -vF -- "$allowed" || true)"
      done
      [ -n "$filtered" ] && hits+="$(printf '%s\n' "$filtered" | sed "s|^|$f:|")"$'\n'
    fi
  done

  if [ -n "$hits" ]; then
    err "$name -- $why"
    printf '%s' "$hits" | sed 's/^/         /' | head -6
    found=$((found + 1))
  else
    ok "$name"
  fi
done

# The check that actually protects this repository: whatever is in .env right
# now must not appear anywhere tracked. Catches the exact mistake made twice
# during development -- a live key copied into a test file.
if [ -f .env ]; then
  while IFS='=' read -r key value; do
    case "$key" in ''|\#*) continue ;; esac
    [ "${#value}" -ge 16 ] || continue
    case "$key" in *PASSWORD*|*KEY*|*TOKEN*|*SECRET*) ;; *) continue ;; esac

    if hits="$(printf '%s\n' "${FILES[@]}" | xargs -r grep -lF -- "$value" 2>/dev/null)"; then
      err "a LIVE value from .env ($key) appears in tracked files:"
      printf '%s\n' "$hits" | sed 's/^/         /'
      found=$((found + 1))
    fi
  done < .env
  [ "$found" -eq 0 ] && ok "no live .env values in tracked files"
fi

info ""
if [ "$found" -eq 0 ]; then
  info "No secrets found in ${#FILES[@]} file(s)."
  exit 0
fi
err "$found pattern(s) matched. Nothing has been committed."
exit 1
