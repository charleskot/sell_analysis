#!/usr/bin/env bash
# Persist the bot's memory by committing state/db.sql.
#
# The bot has no database server: what it has already seen, matched and
# alerted lives in a SQLite file, dumped to text and committed. Called once
# per cycle from the long-running loop, so it must be cheap and quiet when
# nothing changed — which is the normal case, since a cycle that finds no
# new email and no new Telegram activity writes nothing.
set -uo pipefail

cd "$(dirname "$0")/.."

python main.py db-dump >/dev/null || exit 0

git add state/db.sql
if git diff --staged --quiet; then
  exit 0
fi

git -c user.name="pisos-bot" -c user.email="bot@users.noreply.github.com" \
    commit -q -m "state: bot run $(date -u +%Y-%m-%dT%H:%MZ) [skip ci]"

branch="$(git rev-parse --abbrev-ref HEAD)"

# Another run — or a human pushing code — may have moved the branch while
# this cycle was working. Rebase on top and retry; a lost state push means
# re-alerting listings the user has already seen.
for attempt in 1 2 3 4; do
  git pull --rebase --autostash -q origin "$branch" || true
  if git push -q origin "HEAD:$branch"; then
    echo "state: guardado"
    exit 0
  fi
  sleep $((attempt * attempt * 2))
done

echo "state: no se pudo empujar tras 4 intentos" >&2
exit 1
