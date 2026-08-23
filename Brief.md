# Anti FPL Dashboard — Project Brief

> Hand this document to Claude Code at the start of a new session.
> It captures everything built so far, the data sources, all features,
> and the current architecture.

---

## 1. What This Is

A live dashboard for **Anti FPL** — a variant of Fantasy Premier League
where the **lowest score wins**. Every manager plays a completely normal FPL
team; the twist is entirely in how it's scored afterwards — see the
[rules.html](rules.html) page (or `Backend/scoring.py`'s docstring) for the
full penalty list. The worst manager each week is the champion.

The 2026/27 season is currently tracked. The dashboard handles new GWs
dynamically as the season continues (38 GWs total, split into two halves at
GW19).

Teams are tracked at two scopes:
- **Overall** — every eligible team the backend has ever scored (currently ~80).
- **Mini-leagues** — a named subset of those teams, defined in Supabase by an
  `invite_code` (e.g. `KINGANTI2627` → "The next Badly drawn boy", 10 teams).
  New mini-leagues can be created on demand from any FPL classic league ID —
  see `Backend/tasks/create_mini_league.py`.

A team becomes "eligible" (shows up anywhere) once it has a valid GW1 row —
see `Backend/tasks/score_new_team.py`.

---

## 2. Data Sources

### 2.1 FPL Official API (the only external data source)
```
GET https://fantasy.premierleague.com/api/bootstrap-static/            # players, teams, GW deadlines
GET https://fantasy.premierleague.com/api/fixtures/?event={gw}         # fixtures + live status for a GW
GET https://fantasy.premierleague.com/api/entry/{team_id}/history/     # a team's full GW-by-GW history
GET https://fantasy.premierleague.com/api/entry/{team_id}/event/{gw}/picks/  # a team's picks for one GW
GET https://fantasy.premierleague.com/api/event/{gw}/live/             # every player's live score for one GW
GET https://fantasy.premierleague.com/api/leagues-classic/{id}/standings/    # team IDs in an FPL classic league
```
All wrapped in `Backend/fpl_api.py`. There is no scraping of any third-party
Anti FPL site — all scoring (including penalties) is computed locally in
`Backend/scoring.py` from this raw FPL data.

### 2.2 Supabase (Postgres, the persisted store)
Everything the backend computes is upserted into Supabase via
`Backend/db.py` (using `SUPABASE_URL` / `SUPABASE_KEY` env vars — the key is
the **anon** key, safe to embed client-side since it's read-only for the
frontend's purposes). Tables referenced across the codebase:

| Table | Purpose |
|---|---|
| `teams` | One row per team per season — `team_id`, `manager`, `team_name`, `eligible` |
| `gw_scores` | One row per team per GW — FPL raw points, every penalty breakdown, `anti_total`, `cumulative_standing`, `active_chip`, live/provisional flags |
| `team_gw_selections` | One row per team per GW — `squad` (15 player IDs, starters first), `captain_id`, `vice_captain_id` |
| `player_gw_scores` | One row per player per GW — `minutes`, `base_pts` (FPL score), `anti_pts` |
| `players` | Reference data — `player_id`, `web_name`, `position`, `team_short`, `team_id` (FPL club) |
| `fixtures` | One row per fixture per GW — `team_h`, `team_a`, `started`, `finished`, `finished_provisional` |
| `gameweeks` | One row per GW — `is_current`, `is_finished` |
| `mini_leagues` / `mini_league_members` | Named subsets of teams, looked up by `invite_code` |

The frontend pages query these tables **directly** via the Supabase REST API
(PostgREST) — there is no build step and no generated JSON file.

---

## 3. Architecture

```
Anti_FPL/
├── index.html                    # Overall Anti FPL — live leaderboard (site homepage)
├── mini_league.html               # Mini-league ("The next Badly drawn boy") — live leaderboard
├── rules.html                    # Scoring rules & penalties, shared by every page
├── mini_league_dashboard.html    # Mini-league rich stats (standings/charts/chip tables/etc)
├── global_dashboard.html         # Overall rich stats (same tabs, all teams)
├── Overall_anti_fpl_league.html  # Redirect stub → index.html (kept for old links/bookmarks)
├── logo_antifpl.png              # Site logo, used in every page header
├── Backend/
│   ├── fpl_api.py                # All FPL Official API calls
│   ├── scoring.py                # Pure scoring engine — no I/O, see its docstring for full rules
│   ├── db.py                     # Supabase read/write helpers
│   ├── snapshot.py               # Weekly JSON snapshot of season state, committed to snapshots/
│   ├── worker.py                 # Adaptive live-scoring loop (fast while matches are live)
│   ├── run_task.py               # CLI entrypoint — `python run_task.py <task_name>`
│   └── tasks/                    # One module per on-demand or scheduled job (see below)
├── snapshots/2025-26/            # Committed weekly snapshots (history/audit trail)
├── .github/workflows/
│   ├── anti_fpl.yml              # Cron-driven scoring pipeline (see below)
│   └── deploy_pages.yml          # Deploys the whole repo root to GitHub Pages on every push
└── Brief.md                      # This file
```

**No data.json, no static site generator, no server.** Every HTML file is a
self-contained page that calls the Supabase REST API directly from
client-side JS on load (and on a 2-minute timer for the live boards).
GitHub Pages just serves the repo root as-is — `deploy_pages.yml` re-deploys
on every push to `*.html`.

### Backend pipeline (`.github/workflows/anti_fpl.yml`)
A single scheduled workflow dispatches to different `Backend/tasks/*.py`
jobs depending on the cron slot that fired:

| Cadence | Task | What it does |
|---|---|---|
| Daily 03:00 + 14:00 UTC | `refresh_reference` | Re-pulls bootstrap data (players, GW deadlines) — catches new GWs opening |
| Hourly, Fri evening → Sun afternoon | `refresh_picks` | Pulls picks for the upcoming/current GW before/during matches |
| Top of every hour, 11:00–22:00 UTC | `scheduler` (via `worker.py`) | Adaptive loop — ticks every 90s while matches are live, 300s while idle, exits early if nothing is coming up. Runs live scoring (`recalc_scores`) throughout. |
| Weekly, Sunday 23:00 UTC | `snapshot` | Commits a JSON snapshot of season state to `snapshots/` |

On-demand tasks (run manually via `workflow_dispatch` or locally):
`score_new_team` (backfill a newly-registered team), `create_mini_league`
(register a new mini-league from an FPL league ID + invite code),
`recalc_gw` / `finalize_gw` / `backfill_player_scores` (targeted fixes),
`full_refresh` (refresh_reference → refresh_picks → recalc_scores in sequence).

---

## 4. Scoring Rules

Full canonical description lives in the `Backend/scoring.py` docstring and
is rendered for humans on **rules.html**. Summary:

- **Standings**: lowest score wins.
- **Inactive player**: +9 pts per 0-minute player in the final starting XI
  (after auto-subs). During Bench Boost, applies to all 15 squad players.
- **Bank**: +25 pts if bank > £3.0m at the end of the GW.
- **Transfer hits**: +4 pts per extra transfer (adds to score — the
  opposite of normal FPL).
- **Captain/Vice-Captain**: +15 pts if BOTH captain and vice-captain play 0
  minutes.
- **Unused chip**: +25 pts per required chip (Bench Boost, Triple Captain,
  Free Hit — Wildcard exempt) not used by the half-season deadline. Each
  required chip is issued twice a season (once per half): deadlines are
  **GW19** (first half) and **GW38** (second half).

**Live GW nuances** (`gw_finished=False` branch of `score_gw()`):
- Chip penalties are never applied mid-GW.
- Inactive / C-VC penalties only count once a player's fixture has finished
  (a bench player with an unplayed fixture is neither "in" nor "out" yet).
- Bank and transfer-hit penalties apply immediately (known at GW deadline).

**Known gap (not yet fixed):** the live "pending" penalty total under-counts
players who are *likely* to end up inactive but whose own or their
replacement's fixture hasn't kicked off yet — it only counts confirmed (post
fixture) 0-minute players. See the "pending score backend refactor" note in
whatever session picks this up next.

---

## 5. Frontend Pages

### `index.html` / `mini_league.html` — live leaderboards
The "main format" for the site: a light-themed, single sortable table,
click-to-expand (or "See players" button) per team row. Refreshes silently
every 2 minutes. Per team row:
- Position (with ▲/▼ movement vs previous GW), team name (links to their
  live FPL squad page), manager name.
- Chip pips (2 uses each of WC/FH/BB/TC — used/available/currently-live),
  plus a permanent small "TC · GW1" style note under any chip already played.
- GW points (live FPL score while the GW is in progress, Anti FPL score with
  penalties once finished).
- **To Play** — count of *starting XI* players whose fixture hasn't finished
  yet (upcoming, or live with 0 minutes so far).
- Pending/confirmed penalty total, previous total, running total.

Expanded squad view: starters then a clearly separated **BENCH** section
(amber divider + tinted rows). Each player shows minutes/pts/anti-pts and a
fixture-status glyph: ✓ finished, ● live, **?** yet to play.

`index.html` covers every eligible team (no mini-league filter); its nav
never links to a mini-league page. `mini_league.html` is scoped to one
mini-league via its Supabase `invite_code` lookup, and its nav can freely
link out to the Overall page.

### `rules.html`
Static (no Supabase calls) — renders the penalty list from §4 above. Single
shared page linked from every other page's nav, so the rules only need
maintaining in one place.

### `mini_league_dashboard.html` / `global_dashboard.html` — rich stats
Six internal tabs (JS-driven, no page navigation):
1. **📊 Standings** — sortable league table: total, GW pts, overall FPL
   rank (▲/▼), 3-GW / 5-GW rolling averages, chips-left grid, form sparkline.
2. **🎯 GW Scores** — card grid for one GW at a time (selector + prev/next),
   sorted ascending by score, with best/worst pick per team.
3. **📈 Season Chart** — cumulative Chart.js line chart, one line per team,
   toggleable legend.
4. **🏆 Stats of Season** — score distribution, chip points table (👑 best
   score per chip type), top-3 best/worst single GWs.
5. **💥 BB, Pens & Chips** — bench-bumming leaderboards, captain penalty
   league, chip usage breakdown.
6. **🧬 Score Breakdown** — stacked bar charts decomposing each team's
   season total into regular picks / captain / bench bummings / penalties.

Both dashboards share identical structure; `mini_league_dashboard.html`
filters to one mini-league, `global_dashboard.html` shows every team.

---

## 6. Visual Design

Light theme (flipped from an earlier dark-mode version). CSS variables
(live-board pages):
```css
--bg:#f5f5f5; --surface:#fff; --border:#e0e0e0;
--text:#111; --muted:#888;
--accent:#d62828; --good:#1a7f4b; --warn:#b86e00; --live:#d62828;
--font:'Inter',system-ui,sans-serif;
--mono:'JetBrains Mono','Fira Code',monospace;
```

Rich-dashboard pages use a slightly different but harmonized light palette
(Oswald/DM Mono/DM Sans fonts, green/gold/red accents) — see the `:root`
block at the top of `mini_league_dashboard.html`. Chart.js tooltips
intentionally keep a dark bubble for pop/contrast even on the light pages.

Chip colours: WC purple, BB cyan, FH amber, TC red — consistent across
every page.

Logo: `logo_antifpl.png` (flat RGB, no transparency, ~#f5f5f5 background
that blends into the page), used at ~40px header height on every page.

---

## 7. Arrow / Movement Logic

**Mini-league / standings position**:
- Position number goes DOWN (e.g. 3→1) = ▲ green (improved).
- Position number goes UP (e.g. 1→3) = ▼ red (worsened).
- `delta = prevRank - currentRank` → positive = improved → green ▲.

**Overall FPL rank** (rank among all Anti FPL managers):
- Same delta logic as above — rank number going down is good.

---

## 8. Future Ideas (not yet built)

- **Fix the live "pending" penalty undercount** — see §4's known gap. Next
  up after this doc was refreshed.
- **Weekly commentary tab** — auto-generated match-report style GW recap.
- **Season-ending knockout Cup** — `Backend/anti_fpl_cup.py` exists
  (`CUP_ROUNDS=10`, starts GW29, up to 1024 entrants) but its current
  activation status/UI isn't wired into any of the HTML pages yet — confirm
  scope before building a Cup tab.
- **GitHub Actions automation** — already built (`anti_fpl.yml`); future
  work here would be tightening cron windows or adding alerting on failures.

---

## 9. Hosting

**GitHub Pages**, serving the repo root as static files
(`.github/workflows/deploy_pages.yml`, triggers on any `*.html` push).
No build step. `index.html` is the site homepage.
URL pattern: `https://{username}.github.io/{repo}/`.
