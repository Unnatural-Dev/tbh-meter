#!/bin/sh
# Blocks build output and oversized files from entering the repo.
#
# Usage:
#   scripts/guard-paths.sh --staged          # local pre-commit: check the index
#   scripts/guard-paths.sh <base> [<head>]   # CI: check files changed between two commits
#
# Blocked (added / copied / modified / renamed files only; deletions are always fine):
#   - anything under a build/ or dist/ directory (except app/build/, the electron-builder
#     resources dir: icons, installer scripts)
#   - *.exe and *.spec (PyInstaller output and specs)
#   - any file over 1 MiB
# Why: a `git add -A` once committed an 8.6 MB reader exe; it now lives in history for good.
set -eu

MAX_BYTES=1048576

if [ "${1:-}" = "--staged" ]; then
  mode=staged
  files=$(git -c core.quotepath=off diff --cached --name-only --diff-filter=ACMR)
elif [ -n "${1:-}" ]; then
  mode=range
  head_ref="${2:-HEAD}"
  files=$(git -c core.quotepath=off diff --name-only --diff-filter=ACMR "$1" "$head_ref")
else
  echo "usage: $0 --staged | <base> [<head>]" >&2
  exit 2
fi

bad=0
flag() {
  printf 'BLOCKED: %s (%s)\n' "$1" "$2" >&2
  bad=1
}

# Here-doc, not a pipe: the loop must run in this shell so `bad` survives. Paths with spaces are kept.
while IFS= read -r f; do
  [ -n "$f" ] || continue

  case "$f" in
    app/build/*) ;;
    build/* | */build/* | dist/* | */dist/*) flag "$f" "build output directory" ;;
  esac
  case "$f" in
    *.exe) flag "$f" "executable" ;;
    *.spec) flag "$f" "PyInstaller spec" ;;
  esac

  if [ "$mode" = staged ]; then
    size=$(git cat-file -s ":$f" 2>/dev/null || echo 0)
  else
    size=$(git cat-file -s "$head_ref:$f" 2>/dev/null || echo 0)
  fi
  if [ "$size" -gt "$MAX_BYTES" ]; then
    flag "$f" "$size bytes, over the 1 MiB limit"
  fi
done <<EOF
$files
EOF

if [ "$bad" -ne 0 ]; then
  echo "Path guard failed. Remove these (git rm --cached <path>) and stage files by explicit path, never 'git add -A'." >&2
  exit 1
fi
echo "Path guard: OK"
