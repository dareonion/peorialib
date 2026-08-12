#!/usr/bin/env bash
# Re-check every want-list title at all four systems, regenerate the markdown,
# and commit (and optionally push) the result. Meant for the systemd user timer
# (see systemd/) or cron; safe to run by hand too.
#
#   ./refresh.sh                                  # refresh + commit the reports
#   SHELFWALK_PUSH=1 ./refresh.sh                 # …and push
#   SHELFWALK_ARGS="--system sccl --limit 2" ./refresh.sh    # quick smoke test
#
# Availability is the whole point of a refresh: popular board books turn over
# within hours, so a morning run is what makes the reports true when you
# actually walk into the branch.
#
# Everything lives in main(), called at the very end: bash reads a script
# lazily, so a plain top-to-bottom script that gets edited mid-run resumes at a
# now-meaningless byte offset (that silently ate one morning's commit step).
# Wrapping the body means the whole thing is parsed before any of it runs.
set -uo pipefail

main() {
    cd "$(dirname "$(readlink -f "$0")")" || exit 1

    # systemd/cron give a minimal PATH; uv lives in ~/.local/bin
    export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"

    mkdir -p logs
    local log="logs/refresh-$(date +%Y-%m-%d).log"

    # One refresh at a time: a full pass takes ~30 min, so a daily timer plus a
    # hand-run (or a slow night) could otherwise stack two scrapes on one DB.
    exec 9>>logs/.refresh.lock
    if ! flock -n 9; then
        echo "=== $(date -Is) skipped — another refresh holds the lock" >>"$log"
        return 0
    fi

    {
        echo "=== $(date -Is) refresh starting ${SHELFWALK_ARGS:+(args: $SHELFWALK_ARGS)}"
        # shellcheck disable=SC2086  # deliberate word-splitting of the args knob
        uv run bayarea_lookup.py ${SHELFWALK_ARGS:-}
        echo "=== $(date -Is) lookup exit $?"
    } >>"$log" 2>&1

    # Keep the working tree clean: the .md files are generated, so an
    # un-committed refresh just looks like uncommitted work forever.
    if ! git diff --quiet -- '*.md'; then
        git add -- '*.md'
        git commit -q -m "Refresh availability $(date +%Y-%m-%d)" >>"$log" 2>&1 \
            && echo "=== committed regenerated reports" >>"$log"
    else
        # normal overnight: the libraries were shut, so nothing moved
        echo "=== no report changes" >>"$log"
    fi

    # Push whatever is unpushed, not just this run's commit — a refresh that
    # committed while the network was down should get carried up the next day.
    if [ "${SHELFWALK_PUSH:-0}" = 1 ]; then
        local ahead
        ahead=$(git rev-list --count '@{u}..HEAD' 2>/dev/null || echo 0)
        if [ "$ahead" -gt 0 ]; then
            if git push -q >>"$log" 2>&1; then
                echo "=== pushed $ahead commit(s)" >>"$log"
            else
                echo "=== PUSH FAILED — $ahead commit(s) still local" >>"$log"
            fi
        fi
    fi

    find logs -name 'refresh-*.log' -mtime +30 -delete 2>/dev/null
    echo "=== $(date -Is) done" >>"$log"
}

main "$@"
