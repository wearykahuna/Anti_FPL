# Anti FPL Mini League Dashboard — Project Brief

> Hand this document to Claude Code at the start of a new session.
> It captures everything built so far, the data sources, all features,
> and the target architecture for the v2 refactor.

---

## 1. What This Is

A personal mini league dashboard for **Anti FPL** — a variant of Fantasy Premier League
where the **lowest score wins**. The worst manager each week is the champion.
There is a forfeit involved for the overall loser at season end.

The 2025/26 season is currently tracked. GW34 is the most recently completed gameweek
at the time of writing, but the dashboard must handle new GWs dynamically as the
season continues (typically 38 GWs total).

---

## 2. The Ten Teams

```python
TEAM_IDS = [
    5388975,   # Geraint Hooper    — Sausage Roll FC
    6703903,   # Curtis Williams   — Stranger Mings
    6595399,   # Andrew Morris     — Expected Toulouse
    3640882,   # Jason Farrugia    — Losing Comes Easy
    5399604,   # Huw Jones         — The Pear Tree Pub
    6654853,   # Richard Owen      — Cunha Afford Him
    7667159,   # Ellison Griffiths — Funny loser!
    1610262,   # Joe Stout         — Utter Tripe
    3155889,   # James Elgar       — Saka on Wood
    911549,    # Ross Farrugia     — Man Utd's dream XI
]
```

Team names come from the official FPL site. The Anti FPL site uses the same IDs.

---

## 3. Data Sources

### 3.1 Anti FPL — per-manager history page
```
GET https://antifpl.pythonanywhere.com/antifpl/manager/{team_id}/
```
Returns an HTML page with a table. Each row = one gameweek. Scrape with BeautifulSoup.

**Column order (0-indexed):**

| Index | Field | Notes |
|-------|-------|-------|
| 0 | GW Rank | Rank on Anti FPL site that week |
| 1 | Last Rank | Previous GW rank |
| 2 | Gameweek | Integer — just the number |
| 3 | Team Value | £ value |
| 4 | Bank | £ in bank |
| 5 | Transfers | Number made |
| 6 | Transfer Cost | Points deducted (0 if none) |
| 7 | Chip | Chip played that week — see chip names below |
| 8 | C/VC Pens | Captain/Vice-captain penalty count |
| 9 | Inactive Players | Count of inactive player penalties |
| 10 | Last GW | Points last GW (ignore) |
| 11 | Site Points | Anti FPL site points (before penalties) |
| 12 | GW Points (With Pens) | **Primary score — use this** |
| 13 | Total | Cumulative total — **use this** |

**Chip name normalisation** (stored lowercase):
- `wildcard` — Wildcard
- `freehit` — Free Hit
- `bboost` — Bench Boost
- `3xc` — Triple Captain (also written as TC)
- Empty string `""` if no chip played

Each manager gets **2 uses** of each chip across the season (FPL updated rules).
So 8 chip slots per manager: WC×2, FH×2, BB×2, TC×2.

**Penalty scoring:**
- Each C/VC pen = **9 points added**
- Each inactive player = **9 points added**
- Transfer cost is already in points (shown directly in col 6)

### 3.2 FPL Official API — bootstrap (player names)
```
GET https://fantasy.premierleague.com/api/bootstrap-static/
```
Returns JSON. `elements` array contains all players.
Key fields per element: `id`, `web_name`, `first_name`, `second_name`, `team`.
Cache this — it's large (~2MB) and rarely changes mid-season.

### 3.3 FPL Official API — GW live scores
```
GET https://fantasy.premierleague.com/api/event/{gw}/live/
```
Returns JSON. `elements` array, each with `id` and `stats.total_points`.
Use `total_points` for that player's score in that GW.

### 3.4 FPL Official API — team picks per GW
```
GET https://fantasy.premierleague.com/api/entry/{team_id}/event/{gw}/picks/
```
Returns JSON with:
- `picks` — array of 15 players:
  - `element` — player ID
  - `position` — 1–11 = starting XI, 12–15 = bench
  - `is_captain` — bool
  - `is_vice_captain` — bool
  - `multiplier` — 2 if TC, 1 normally, 0 if benched
- `automatic_subs` — array of auto-substitutions:
  - `element_in` — player who came on
  - `element_out` — player who went off
- `active_chip` — chip played (`null` if none)
- `entry_history.points_on_bench` — total bench points that GW (includes players who didn't sub on)

---

## 4. Target Architecture (v2)

```
antifpl-dashboard/
├── fetch_data.py          # Run locally after each GW — writes data.json
├── data.json              # Committed to repo — static host reads this
├── index.html             # Pure display — reads data.json via fetch()
├── requirements.txt       # requests, beautifulsoup4, lxml
├── .github/
│   └── workflows/
│       └── update.yml     # Optional: auto-run fetch_data.py on schedule
└── BRIEF.md               # This file
```

### fetch_data.py responsibilities

1. Scrape all 10 Anti FPL manager pages → per-GW history
2. Fetch FPL bootstrap-static → player name lookup dict
3. For each completed GW, fetch all 10 teams' picks + GW live scores
4. Compute derived stats (best pick, worst pick, bench bummings, etc.)
5. Write everything to `data.json`

### index.html responsibilities

- `fetch('./data.json')` on load — no API calls, no auth
- Render all tabs from the JSON
- Works on any static host (GitHub Pages, Netlify, etc.)

---

## 5. data.json Schema

```json
{
  "meta": {
    "lastUpdated": "2026-01-14T20:30:00Z",
    "currentGW": 34,
    "season": "2025/26"
  },
  "players": {
    "123": "Salah",
    "456": "Haaland"
  },
  "teams": [
    {
      "id": 911549,
      "manager": "Ross Farrugia",
      "team": "Man Utd's dream XI",
      "fplUrl": "https://fantasy.premierleague.com/entry/911549/event/34/",
      "color": "#10b981",
      "gws": [
        {
          "gw": 1,
          "rank": 8,
          "pts": 24,
          "total": 24,
          "chip": "",
          "sitePts": 24,
          "cvcPens": 0,
          "transferCost": 0,
          "inactive": 0,
          "bestPick": { "name": "Isak", "pts": 2 },
          "worstPick": { "name": "Haaland", "pts": 18 },
          "captain": { "name": "Haaland", "pts": 18 },
          "benchPts": 4,
          "benchBummingPts": 0,
          "benchPlayers": [
            { "name": "Flekken", "pts": 2, "autoSubbed": false },
            { "name": "Mykolenko", "pts": 1, "autoSubbed": false }
          ]
        }
      ]
    }
  ]
}
```

**Notes on derived fields:**
- `bestPick` — lowest scoring player in starting XI (good in Anti FPL)
- `worstPick` — highest scoring player in starting XI (bad in Anti FPL)
- `captain` — the designated captain and their FPL score
- `benchPts` — `entry_history.points_on_bench` from FPL API (all bench pts)
- `benchBummingPts` — points scored by players in `automatic_subs.element_in` only
- `benchPlayers` — all 4 bench slots with name, pts, and whether they auto-subbed on

---

## 6. Dashboard Features (all tabs)

### Tab 1 — 📊 Standings

League table sorted **ascending by total** (lowest = 1st place).

Columns:
- **Pos** — mini-league position (1–10) with ▲/▼ movement badge vs previous GW
- **Team** — team name as hyperlink to FPL team page for latest GW, manager name below
- **Total** — cumulative Anti FPL points
- **GW Pts** — points scored last GW
- **Anti FPL Rank** — rank on the full Anti FPL site this GW, with ▲/▼ vs previous GW
  - Arrow logic: rank number going DOWN is GOOD (▲ green). Going UP is BAD (▼ red).
- **Prev Rank** — Anti FPL site rank previous GW
- **3-GW Avg** — rolling 3-week average
- **5-GW Avg** — rolling 5-week average
- **Chips Left** — visual grid of 4 chip types × 2 uses each (coloured pill = available, greyed ✓ = used)
- **Form** — mini bar chart of last 5 GW scores (shorter bar = better)

### Tab 2 — 🎯 GW Scores

GW selector strip (buttons 1–N) + Prev/Next arrows.

Cards sorted ascending by GW score for that week. Each card shows:
- GW Rank on Anti FPL
- Team name + chip badge if played (colour coded: WC purple, BB cyan, FH amber, TC red)
- Manager name
- GW points (large, colour coded: green < 25, amber < 45, red ≥ 45)
- Running total
- **Best Pick** — lowest scoring starter (with name + pts)
- **Worst Pick** — highest scoring starter (with name + pts)

Best/worst picks require data from `fetch_data.py` — they come from `data.json`, no live API calls.

### Tab 3 — 📈 Season Chart

Cumulative line chart (Chart.js). Lower = better. Each team a distinct colour.
Clickable legend to toggle teams on/off.
X-axis: GW1–GWN. Y-axis: total points. Tooltip shows all teams at hovered GW.

### Tab 4 — 🏆 Stats of Season

**4a. GW Score Distribution table** (computed from data, instant)

Per team:
- Count of scores < 20
- Min score (with GW number)
- Max score (with GW number)
- Range (max − min)
- Count of scores > 40

Highlight best (lowest/most favourable) value in each column in green.

**4b. Chip Points Table** (8 columns × 10 rows)

Columns: WC1, WC2, FH1, FH2, BB1, BB2, TC1, TC2
Each cell shows: GW number (small) + points scored that GW.
Empty `—` if chip not yet used.
👑 highlight on the lowest score for each chip type across all teams and both uses.

**4c. Penalty Breakdown table**

Per team:
- C/VC Pen count (total across season)
- C/VC Pen points (count × 9)
- Transfer cost points (sum of col 6 across season)
- Inactive player count
- Inactive player points (count × 9)
- **Total penalty points** (highlighted red if > 0)

**4d. Top 3 Best / Worst GW Scores** (side by side)
- Best = 3 lowest single-GW scores across all teams all GWs
- Worst = 3 highest single-GW scores across all teams all GWs

**4e. Captain Penalty League**
Ranked by total C/VC penalty points across the season (from Anti FPL data).
Shows: total pen count, total pts, worst single GW.

**4f. Bench Bummings** (3 sub-sections)

- **Total Bench Bummings** — sum of `benchBummingPts` across season, ranked descending
- **Worst Single Bench Score** — highest `benchBummingPts` in one GW (player name + GW)
- **9 Lives Moments** — highest pts by a bench player who did NOT auto-sub on (sat on bench wasted)

---

## 7. Visual Design

Dark theme. CSS variables:
```css
--bg: #080c14
--surface: #0f1623
--card: #151e2e
--border: #1e2d44
--green: #10b981
--green-dim: #064e35
--gold: #f59e0b
--red: #ef4444
--text: #e2e8f0
--muted: #64748b
--chip-wc: #8b5cf6    (Wildcard — purple)
--chip-bb: #06b6d4    (Bench Boost — cyan)
--chip-fh: #f59e0b    (Free Hit — amber)
--chip-3xc: #ef4444   (Triple Captain — red)
```

Fonts: Oswald (headers/numbers), DM Mono (stats/values), DM Sans (body).
All from Google Fonts.

Team colour palette (assign in order of league table position at season end):
```
#10b981, #3b82f6, #f59e0b, #ef4444,
#8b5cf6, #06b6d4, #f97316, #ec4899,
#84cc16, #e2e8f0
```

Chart.js 4.4.1 via cdnjs for the cumulative line chart.

---

## 8. Arrow / Movement Logic

**Mini-league position** (1st–10th among the 10 teams):
- Position number goes DOWN (e.g. 3→1) = ▲ green (improved)
- Position number goes UP (e.g. 1→3) = ▼ red (worsened)
- `delta = prevRank - currentRank` → positive = improved → green ▲

**Anti FPL overall rank** (rank among all Anti FPL managers, e.g. #33 out of 154):
- Rank number goes DOWN (e.g. #50→#33) = ▲ green (improved, closer to winning)
- Rank number goes UP (e.g. #33→#50) = ▼ red (worsened)
- Same delta logic as mini-league position

---

## 9. Future Ideas (not yet built)

- **Weekly commentary tab** — auto-generated match report style, with jokes/forfeits, based on a style guide Skill. The manager has a history of writing these manually each GW.
- **Full Anti FPL player base scoring** — extending to rank all FPL managers by Anti FPL score, not just the mini-league. This is the "pipe dream" — would likely need a proper backend (Claude Code + Python API).
- **GitHub Actions automation** — `.github/workflows/update.yml` that runs `fetch_data.py` on a cron (e.g. every Tuesday night) and auto-commits `data.json`.

---

## 10. Hosting

Target: **GitHub Pages** (free, static, public repo).
URL pattern: `https://{username}.github.io/antifpl-dashboard/`

Alternatively: **Netlify** (supports private repo, optional password protection).

The v2 architecture (static HTML + pre-fetched JSON) works on any static host
with zero server-side requirements.

---

## 11. Repo Structure

```
main branch          ← last known-good HTML version (v1)
v2-json-approach     ← new clean architecture (fetch_data.py + data.json + index.html)
```

Start all Claude Code work on `v2-json-approach`.