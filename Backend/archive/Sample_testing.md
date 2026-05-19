# Sample Testing Documentation

This document contains validation information for 8 sample teams selected from the results to cover different scoring scenarios and edge cases.

---

## 1. Most Chip Penalty — Mass Anti

| Field | Value |
|-------|-------|
| **Team ID** | 9962597 |
| **Manager** | Mass DL |
| **Team Name** | Mass Anti |
| **Standing** | #65 |
| **Anti Total Score** | 1446 |
| **FPL History** | [https://fantasy.premierleague.com/entry/9962597/history](https://fantasy.premierleague.com/entry/9962597/history) |

**Purpose**: Validate chip penalty calculations, especially unused chip penalties at GW19 and GW38.

### Testing Notes
- [ ] Verify chip penalties are calculated correctly for all chips
- [ ] Check GW19 threshold for unused chips
- [ ] Check GW38 threshold for unused chips
- [ ] Confirm Wildcard is exempt from unused chip penalty

### Comments
```
[Add test results and observations here]
```

---

## 2. Most Transfer Hits — Cup Runner Up

| Field | Value |
|-------|-------|
| **Team ID** | 9650501 |
| **Manager** | Alittlebit Spursy |
| **Team Name** | Cup runner up |
| **Standing** | #164 |
| **Anti Total Score** | 2507 |
| **FPL History** | [https://fantasy.premierleague.com/entry/9650501/history](https://fantasy.premierleague.com/entry/9650501/history) |

**Purpose**: Validate transfer hit penalty calculations (+4 pts per extra transfer).

### Testing Notes
- [ ] Count total hits taken across all GWs
- [ ] Verify hit points calculation (+4 per hit)
- [ ] Check hits are properly added to score

### Comments
```
[Add test results and observations here]
```

---

## 3. Most Inactive Penalties — Hougang

| Field | Value |
|-------|-------|
| **Team ID** | 6914415 |
| **Manager** | Sebastien Tan |
| **Team Name** | Hougang |
| **Standing** | #175 |
| **Anti Total Score** | 4759 |
| **FPL History** | [https://fantasy.premierleague.com/entry/6914415/history](https://fantasy.premierleague.com/entry/6914415/history) |

**Purpose**: Validate inactive player penalty calculations (+9 pts per 0-min player in final XI).

### Testing Notes
- [ ] Count 0-minute players in final XI for each GW
- [ ] Verify +9 pts per inactive player
- [ ] Check if penalties apply during Bench Boost (should apply to all 15)
- [ ] Verify auto-sub logic is applied before counting

### Comments
```
[Add test results and observations here]
```

---

## 4. Used Bench Boost — Walker's Lovechilds

| Field | Value |
|-------|-------|
| **Team ID** | 96242 |
| **Manager** | Meister Pumuckl |
| **Team Name** | Walker's Lovechilds |
| **Standing** | #1 |
| **Anti Total Score** | 1096 |
| **FPL History** | [https://fantasy.premierleague.com/entry/96242/history](https://fantasy.premierleague.com/entry/96242/history) |

**Purpose**: Validate Bench Boost chip handling and inactive penalty application during chip usage.

### Testing Notes
- [ ] Identify which GW had Bench Boost active
- [ ] Verify inactive penalty applies to ALL 15 players during BB (not just XI)
- [ ] Check that captain boost is included in score calculation
- [ ] Confirm chip was properly recorded and applied

### Comments
```
[Add test results and observations here]
```

---

## 5. Hit C/VC Penalty — Auntie Fantasy

| Field | Value |
|-------|-------|
| **Team ID** | 1583478 |
| **Manager** | Iain Reid |
| **Team Name** | Auntie Fantasy |
| **Standing** | #14 |
| **Anti Total Score** | 1226 |
| **FPL History** | [https://fantasy.premierleague.com/entry/1583478/history](https://fantasy.premierleague.com/entry/1583478/history) |

**Purpose**: Validate Captain/Vice-Captain penalty (+15 pts if BOTH play 0 mins).

### Testing Notes
- [ ] Find GW(s) where C/VC penalty was applied
- [ ] Verify both captain AND vice-captain played 0 minutes
- [ ] Check vice-captain auto-sub logic was applied first
- [ ] Confirm penalty is +15 pts (not cumulative)
- [ ] Check penalty is only applied once per GW maximum

### Comments
```
[Add test results and observations here]
```

---

## 6. Hit Bank Penalty — San Marino u7s

| Field | Value |
|-------|-------|
| **Team ID** | 7758715 |
| **Manager** | Bob Boba |
| **Team Name** | San Marino u7s |
| **Standing** | #29 |
| **Anti Total Score** | 1304 |
| **FPL History** | [https://fantasy.premierleague.com/entry/7758715/history](https://fantasy.premierleague.com/entry/7758715/history) |

**Purpose**: Validate bank penalty (+25 pts if bank > £3.0m).

### Testing Notes
- [ ] Identify GW(s) with bank > £3.0m (stored as > 30 in 0.1m units)
- [ ] Verify +25 pts penalty applied for those GWs
- [ ] Check bank amount progression throughout season
- [ ] Confirm penalty applied at correct threshold

### Comments
```
[Add test results and observations here]
```

---

## 7. Current League Leader — Walker's Lovechilds

| Field | Value |
|-------|-------|
| **Team ID** | 96242 |
| **Manager** | Meister Pumuckl |
| **Team Name** | Walker's Lovechilds |
| **Standing** | #1 |
| **Anti Total Score** | 1096 |
| **FPL History** | [https://fantasy.premierleague.com/entry/96242/history](https://fantasy.premierleague.com/entry/96242/history) |

**Purpose**: Comprehensive validation of the league winner with lowest anti-FPL score.

### Testing Notes
- [ ] Verify all GW scores sum to 1096
- [ ] Cross-check scoring calculations for each GW
- [ ] Validate all penalties are correctly applied
- [ ] Check team structure and transfers make sense
- [ ] Confirm no scoring anomalies for the season

### Comments
```
[Add test results and observations here]
```

---

## 8. Current League Bottom — Hougang

| Field | Value |
|-------|-------|
| **Team ID** | 6914415 |
| **Manager** | Sebastien Tan |
| **Team Name** | Hougang |
| **Standing** | #175 |
| **Anti Total Score** | 4759 |
| **FPL History** | [https://fantasy.premierleague.com/entry/6914415/history](https://fantasy.premierleague.com/entry/6914415/history) |

**Purpose**: Comprehensive validation of the league bottom with highest anti-FPL score.

### Testing Notes
- [ ] Verify all GW scores sum to 4759
- [ ] Identify major penalty contributors (chips, hits, inactives, etc.)
- [ ] Check for patterns in poor decision-making
- [ ] Validate all calculations for this team
- [ ] Compare with league leader to understand performance gap

### Comments
```
[Add test results and observations here]
```

---

## Summary

### Validation Checklist
- [ ] All 8 teams tested and verified
- [ ] No duplicate or conflicting results between teams
- [ ] FPL API data matches manual verification
- [ ] All penalty types validated
- [ ] Chip usage patterns verified
- [ ] Standing/ranking calculations confirmed

### Overall Comments
```
[Add final summary and any issues found across all samples]
```
