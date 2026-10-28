#!/usr/bin/env bash
# Persist the bot's memory by committing state/db.sql.
#
# The bot has no database server: what it has already seen, matched and
# alerted lives in a SQLite file, dumped to text and committed. Called once
# per cycle from the long-running loop, so it must be cheap and quiet when
# nothing changed — which is the normal case, since a cycle that finds no
# new email and no new Telegram activity writes nothing.
#
# The commit is built on top of the remote branch without touching the
# working tree, the index or HEAD: the remote's code, this run's dump. It
# used to `git pull --rebase` instead, and on 28 October a rebase of one
# db.sql over another conflicted, stopped half-way, and every later cycle
# failed against the leftover — six hours of state never saved, and a flat
# sent twice once the next run restored the old dump. Only one run holds the
# state at a time (the workflow's concurrency group), so its dump is the
# newest there is and simply replaces the remote's.
set -uo pipefail

cd "$(dirname "$0")/.."

python main.py db-dump >/dev/null || exit 0

branch="${STATE_BRANCH:-$(git rev-parse --abbrev-ref HEAD)}"
[ "$branch" = "HEAD" ] && branch="main"

# Left behind by the old rebase-based version; harmless to clear.
git rebase --abort >/dev/null 2>&1 || true

blob="$(git hash-object -w state/db.sql)" || exit 1
index="$(git rev-parse --git-dir)/state-index"

for attempt in 1 2 3 4; do
  git fetch -q origin "$branch" || { sleep $((attempt * attempt * 2)); continue; }
  base="$(git rev-parse FETCH_HEAD)"

  # The remote's tree with state/db.sql swapped for this run's dump
  rm -f "$index"
  GIT_INDEX_FILE="$index" git read-tree "$base"
  GIT_INDEX_FILE="$index" git update-index --add --cacheinfo "100644,$blob,state/db.sql"
  tree="$(GIT_INDEX_FILE="$index" git write-tree)"
  rm -f "$index"

  if [ "$tree" = "$(git rev-parse "$base^{tree}")" ]; then
    exit 0                                   # nothing new to save
  fi

  commit="$(git -c user.name="pisos-bot" -c user.email="bot@users.noreply.github.com" \
            commit-tree "$tree" -p "$base" \
            -m "state: bot run $(date -u +%Y-%m-%dT%H:%MZ) [skip ci]")"
  if git push -q origin "$commit:refs/heads/$branch"; then
    echo "state: guardado"
    exit 0
  fi
  sleep $((attempt * attempt * 2))
done

echo "state: no se pudo empujar tras 4 intentos" >&2
exit 1
