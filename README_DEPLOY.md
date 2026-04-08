# World Conquest — Deployment & Persistence Guide

## Why progress resets

The game uses **SQLite** stored as a file (`game.db`). On free hosting platforms
like Render, the filesystem is **ephemeral** — it wipes on every redeploy or
server restart. You need to persist the file explicitly.

---

## Fix: Persistent disk on Render (recommended)

1. In your Render service → go to **Disks** → Add Disk
   - Mount path: `/data`
   - Size: 1 GB (free tier allows 1 GB)
2. Set env var: `DB_PATH=/data/game.db`
3. The game will now load and save from that disk across restarts.

## Fix: Local / self-hosted

If running locally, `game.db` saves in the same folder as `app.py`. Progress
persists automatically across restarts — nothing to configure.

---

## Environment Variables

| Variable | Default | Notes |
|---|---|---|
| `DB_PATH` | `./game.db` | Path to SQLite file — point to a persistent disk |
| `SECRET_KEY` | built-in | Change to a long random string in production |
| `PORT` | `5000` | Set automatically by Render |

### For Render: add to your start command
```
web: python app.py
```

### Update app.py port for Render
At the bottom of `app.py`, change:
```python
app.run(debug=True, host='0.0.0.0', port=5000)
```
to:
```python
app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
```

---

## Requirements

```
flask>=3.0
requests  # optional, not currently used (geolocation uses stdlib urllib)
```

`requirements.txt`:
```
flask
```

---

## Admin: Download DB Backup

While logged in as admin, visit:
```
/api/admin/export_db
```
This downloads a `.sql` dump of the entire database you can keep as a backup.

---

## What Changed (summary)

### Persistence
- `DB_PATH` now reads from environment variable

### Guest / Spectator Mode
- Visitors can click "Watch Map Without Logging In" on the login screen
- They see the live map, leaderboard, and territory info
- They appear in the Online widget as `🇳🇴 Guest` (flag from their IP)
- Trying to take any action (claim, attack, etc.) prompts them to log in

### Economy Rebalance
| Thing | Before | After |
|---|---|---|
| Starting money | 100 | 200 |
| Starting food/wood/metal | 50 | 100 |
| Starting oil | 10 | 25 |
| Troop cost | 10💰 | 8💰 (6 with gunpowder) |
| Boat cost | 1000💰 | 800💰 |
| Plane cost | 1000💰 | 1200💰 |
| Plains yield | 6/min | 9/min |
| Forest yield | 8/min | 12/min |
| Mountains yield | 6/min | 9/min |
| Desert yield | 4/min | 7/min |
| City yield | 15/min | 18/min |
| Oil yield | 10/min | 14/min |
| Sell: food | 1💰 | 2💰 |
| Sell: wood | 2💰 | 4💰 |
| Sell: metal | 3💰 | 6💰 |
| Sell: oil | 5💰 | 10💰 |
| Claim cost | 30💰 flat | 25→40→80→150→400→1200 (tiered) |
| Offline cap | 60 min | 120 min |
