#!/usr/bin/env bash
# Re-check every want-list title at all four systems, regenerate the markdown,
# and commit the result. Meant for the systemd user timer (see systemd/) or
# cron; safe to run by hand too.
#
#   ./refresh.sh              # refresh + commit the regenerated reports
#   SHELFWALK_PUSH=1 ./refresh.sh   # …and push
#
# Availability is the whole point of a refresh: popular board books turn over
# within hours, so a morning run is what makes the reports true when you
# actually walk into the branch.
set -uo pipefail
cd "$(dirname "$(readlink -f "$0")")" || exit 1

# systemd/cron give a minimal PATH; uv lives in ~/.local/bin
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"

mkdir -p logs
log="logs/refresh-$(date +%Y-%m-%d).log"
{
    echo "=== $(date -Is) refresh starting"
    uv run bayarea_lookup.py
    echo "=== $(date -Is) lookup exit $?"
} >>"$log" 2>&1

# Keep the working tree clean: the .md files are generated, so an un-committed
# refresh just looks like uncommitted work forever.
if ! git diff --quiet -- '*.md'; then
    git add -- '*.md'
    git commit -q -m "Refresh availability $(date +%Y-%m-%d)" >>"$log" 2>&1 \
        && echo "=== committed regenerated reports" >>"$log"
    if [ "${SHELFWALK_PUSH:-0}" = 1 ]; then
        git push -q >>"$log" 2>&1 && echo "=== pushed" >>"$log"
    fi
else
    echo "=== no report changes" >>"$log"
fi

find logs -name 'refresh-*.log' -mtime +30 -delete 2>/dev/null
echo "=== $(date -Is) done" >>"$log"
