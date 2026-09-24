# Gearsmith

**Beta** - a simple self-hosted gear tracker for guitarists. Keep your guitars, amps, pedals, and picks in one place, track how old each guitar's strings are, group gear into sets (boards and rigs), and save the exact settings each song needs. One Docker container, SQLite storage, no subscriptions, no cloud account - your data stays on your box.

![MIT](https://img.shields.io/badge/license-MIT-green) ![status](https://img.shields.io/badge/status-beta-yellow)

## What it does

- **Gear inventory** - guitars, amps, pedals, and picks, each with the spec fields that matter:
  - Guitars: finish, pickups, nut, scale length, tuning, string gauge, mods, serial, status (home / at the luthier / lent out)
  - Amps: wattage, speaker, tubes, serial
  - Pedals: voltage, current draw (mA), polarity, true bypass vs buffered
  - Picks: thickness, material, how many you've got
  - Photos on everything, purchase date and price, notes
- **Restring tracking** - log a restring (brand, gauge, date) and each guitar gets a "strings: N days" chip that turns yellow as the interval nears and red when it's overdue. Every guitar has its own restring interval.
- **Favorites** - tap the star on any card or on the gear page to mark a favorite. Favorites sit at the top of their section, and the "Favorites only" button on the Gear page hides everything else (it remembers your choice on that device).
- **Songs** - a recall sheet for every song you play: tuning, capo, key, BPM, the guitar and amp, and the knob settings on each pedal and amp in the chain (on, off, or toggled mid-song). Knob positions are free text, so "2:00", "noon", "max", and "7.5" all work. Multi-effects units and modelers (Helix, Quad Cortex, Kemper, and so on) get patch pointers instead: patch number and name, scenes, MIDI notes, and an optional list of effect blocks. Add photos of your board or a handwritten sheet. Each gear page lists the songs that use it.
- **Knob names per pedal** - give a pedal or amp its own control names (Gain, Tone, Level...) and the song editor fills them in for you. Mark a pedal as a modeler to track patches for it instead of knobs.
- **Want and Sold lists** - gear you're after (with a target price) and gear you've sold (with sale date and price) live in their own views next to what you own. Sold gear is dimmed and never shows up in the restring due list or notifications.
- **Share links** - make a read-only link to one piece of gear or a whole set to send to a buyer, a tech, or a friend. Links use a random token, can expire (7, 30, or 90 days, or never), and can be turned off or replaced at any time. Shared pages hide serial numbers and prices, show no links back into your app, and tell search engines not to index them.
- **Sets** - group gear into rigs: a pedalboard, a gig rig, a recording chain. Gear can live in several sets or none.
- **Notifications** - Apprise alerts (ntfy, Pushover, Telegram, and 100+ others) when a guitar's strings pass their interval, with quiet hours and overdue repeats.
- **Hideable sections** - don't have pedals? Don't care about songs or a wish list? Turn the section off in Settings > Features and it leaves the interface. Hidden sections keep their data.
- **Multi-user** - admin plus member accounts, everyone with their own login.
- **Token API** - per-user API tokens and interactive docs at `/api/docs`, so you can log a restring or read your collection from anywhere: a script, a shortcut, or an AI assistant.

## Run it

You need Docker. Everything below works with plain `docker compose`; if you use a GUI manager (Dockhand, CasaOS, Portainer, Synology Container Manager), create a stack/project from the same compose file instead - no `.env` file is required, fill the `${...}` values in the manager's environment editor.

```yaml
services:
  gearsmith:
    image: ghcr.io/dhrandy/gearsmith:latest
    container_name: gearsmith
    restart: unless-stopped
    volumes:
      - ./data:/app/data
    environment:
      # Your timezone, so "due today" and quiet hours match your clock.
      - TZ=${TZ:-UTC}
      # Set to true once Gearsmith is served over HTTPS (reverse proxy).
      - GEARSMITH_COOKIE_SECURE=${GEARSMITH_COOKIE_SECURE:-false}
      # IP of your reverse proxy, so login rate limits see real client addresses.
      - FORWARDED_ALLOW_IPS=${FORWARDED_ALLOW_IPS:-127.0.0.1}
    ports:
      - 8743:8000
```

Then:

```bash
docker compose up -d
```

Open http://localhost:8743, create the admin account, and delete the example gear once you've clicked around. All data (the SQLite database and photos) lives in the `./data` directory you mounted - back that up and you've backed up everything.

### Environment variables

| Variable | Default | What it does |
| --- | --- | --- |
| `TZ` | `UTC` | Timezone for "due today" and quiet hours, e.g. `America/New_York` |
| `GEARSMITH_COOKIE_SECURE` | `false` | Set `true` when served over HTTPS (see reverse proxy below) |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | IPs trusted to set `X-Forwarded-For` (your reverse proxy), so rate limits see real client addresses |
| `GEARSMITH_DATA_DIR` | `/app/data` | Where the database and photos live inside the container |
| `GEARSMITH_NOTIFY_WORKER` | `true` | Set `false` to disable the background notification checker |

### Reverse proxy

Any reverse proxy works (nginx, Caddy, Traefik, Synology's built-in one). Point it at port 8743, then:

1. Set `GEARSMITH_COOKIE_SECURE=true` so session cookies are HTTPS-only.
2. Set `FORWARDED_ALLOW_IPS` to your proxy's IP so login rate limiting sees real client IPs.
3. In Settings > Notifications, set the public app address so notification links point at your URL.

## Notifications

Settings > Notifications takes any [Apprise](https://github.com/caronc/apprise) URLs, one per line - `ntfy://ntfy.sh/your-topic`, `pover://user@token`, `tgram://...`, and so on. Gearsmith sends when a guitar passes its restring interval, once when it flips overdue and then every N days (your choice) until you log the restring. Send hour and quiet hours are configurable, and there's a test button.

## API tokens and AI assistants

Settings > API tokens creates a token that acts as you over the REST API. Interactive docs (with a "try it" button) are at `/api/docs`; the OpenAPI spec is at `/api/v1/openapi.json`.

To connect an AI assistant, paste it something like this, with your own URL and token filled in:

```
You can manage my guitar gear through the Gearsmith API at https://YOUR-URL-HERE.
Authenticate every request with the header: Authorization: Bearer YOUR-TOKEN-HERE
The interactive docs are at /api/docs and the OpenAPI spec at /api/v1/openapi.json.
Gear: list and add (GET/POST /api/v1/gear, filter the list with ?lifecycle=owned, want or
sold), read one item (GET /api/v1/gear/{id}), update it (PATCH /api/v1/gear/{id}). Every item
has "favorite" (true/false) and "lifecycle" (owned, want or sold); want items can carry
want_price, sold items sold_date and sold_price. Pedals and amps can list their knob names in
specs.controls, and specs.modeler marks a multi-effects unit.
Restrings: log one (POST /api/v1/gear/{id}/restrings with brand, gauge and optional date) and
check what's due (GET /api/v1/due). Sets: GET/POST /api/v1/sets.
Photos: attach one (POST /api/v1/gear/{id}/photos, multipart field "photo"), delete one
(DELETE /api/v1/photos/{photo_id}), or make one the cover (POST /api/v1/photos/{photo_id}/cover).
Songs: list, search and add (GET/POST /api/v1/songs, ?q= searches title and artist, ?gear_id=
finds songs using a piece of gear), read, update or delete one (GET/PATCH/DELETE
/api/v1/songs/{id}). A song has title, artist, tuning, capo, key, bpm, guitar_id, amp_id,
notes, a "rig" list (gear_id, engaged on/off/toggle, knobs as [{name, value}] with text
values) and a "patches" list for modelers (gear_id, patch_ref, patch_name, scenes, note).
Edit single entries with /api/v1/songs/{id}/rig/{setting_id} and
/api/v1/songs/{id}/patches/{patch_id}.
Share links: create or replace one (POST /api/v1/gear/{id}/share or /api/v1/sets/{id}/share
with optional expires_in_days and regenerate), read it (GET) or turn it off (DELETE).
When I tell you I restrung a guitar, log it. When I ask what needs new strings, check the
due list. When I tell you how I set my rig for a song, save it on that song.
```

Treat tokens like passwords - anyone holding one can read and change your gear.

## Development

```bash
pip install -r requirements.txt -r requirements-dev.txt
playwright install --with-deps chromium
GEARSMITH_DATA_DIR=/tmp/gearsmith PYTHONPATH=. pytest -q
GEARSMITH_DATA_DIR=/tmp/gearsmith uvicorn app.main:app --port 8000
```

Tests cover the API and the UI (Playwright) and run in CI before every image publish. The image builds for `linux/amd64` and publishes to `ghcr.io/dhrandy/gearsmith` on every push to `main`.

## License

MIT - see [LICENSE](LICENSE).
