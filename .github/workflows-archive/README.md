# Archived workflows

Superseded on 2026-09-05. Kept for reference, not run — GitHub only reads
workflow definitions from `.github/workflows/`, never from a sibling directory.

Rollback tag for the state before the reset: `workflows-pre-reset` (fff2319).

## anti_fpl.yml

Did everything: nine cron lines mapped to three tasks through a `case`
statement on `github.event.schedule`, plus a fifteen-option dispatch menu.

Replaced by `live.yml`, `reference.yml`, `snapshot.yml` and `manual.yml`.

Why it failed: GitHub silently DROPS scheduled occurrences under load rather
than queueing them. Measured from the API run history in Sept 2026, of 20
occurrences requested on a Saturday GitHub created 6, deferred by 30 min to
4.5 hours. Because the design asked for only one occurrence per hour, every
dropped one cost a full hour of live scoring. The 55-minute worker holding a
concurrency group made it worse: any deferral over 5 minutes left the next
occurrence queued behind it, and a third cancelled the pending one.

The `case` mapping itself was correct. The problem was the lack of redundancy.

## deploy_pages.yml

A second, redundant GitHub Pages publisher. Pages on this repo is configured as
"Deploy from a branch", so GitHub's own `pages-build-deployment` builder is what
actually publishes the site, on every push to main with no path filter.

This workflow raced it. On 31 Aug 2026 the deployments API shows pairs of
`github-pages` deployments seconds apart on the same commit, one from each
publisher. It had also not run at all since 31 Aug, because its `paths` filter
only matched root-level `*.html`, yet the site kept deploying — proof the branch
builder was doing the work.

Deleted rather than replaced. Nothing publishes Pages from `.github/workflows/`
any more.

## tests.yml

Superseded by the rewritten `.github/workflows/tests.yml`, which adds
`.github/workflows/**` to its triggers, a YAML parse check over every workflow
file, and pip caching.
