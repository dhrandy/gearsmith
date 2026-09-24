# Gearsmith

**Beta** - a simple self-hosted gear tracker for guitarists. Keep your guitars, amps, pedals, picks, and strings in one place, track how old each guitar's strings are, group gear into sets (boards and rigs), and save the exact settings each song needs. One Docker container, SQLite storage, no subscriptions, no cloud account - your data stays on your box.

![MIT](https://img.shields.io/badge/license-MIT-green) ![status](https://img.shields.io/badge/status-beta-yellow)

## What it does

- **Gear inventory** - guitars, amps, pedals, picks, and strings, each with the spec fields that matter:
  - Guitars: finish, pickups, nut, scale length, tuning, string gauge, mods, serial, status (home / at the luthier / lent out)
  - Amps: wattage, speaker, tubes, serial
  - Pedals: voltage, current draw (mA), polarity, true bypass vs buffered
  - Picks: thickness, material, how many you've got
  - Strings: brand, gauge (10-46, 9-42...), type (electric, acoustic, classical, bass), material, strings per set, sets per pack
  - Photos on everything, purchase date, price paid and current value, notes
- **Strings on each guitar** - every guitar can pick the strings it uses from your Strings section, and each strings page lists the guitars using it. When you log a restring, pick the strings from a list and the brand and gauge fill in (or just type them). Deleting a strings item keeps the brand and gauge on old restring entries.
- **String stock countdown** - a strings item can track how many unopened sets you have. Logging a restring with those strings takes one set off the count automatically; the card and page show the count and flag "1 set left" and "Out of sets". Adjust the count by hand on the strings page (or leave it untracked).
- **Maintenance log** - every item keeps a service history: date, category (setup, tubes, fret work, repair, other) and a note. Add, edit and delete entries on the item's page.
- **Manual links** - each item can carry a link to its manual, shown as a **Manual** button on the item's page. Links only; Gearsmith never stores the files.
- **Built-in tuner** - an A440 tuner in its own tab. Pick a tuning - standard, half step down, full step down (D standard), drop D, drop C#, drop C, drop B, open G, open D, open E, DADGAD, or plain chromatic - and it shows the target note for each string, reads against the nearest one, and says tune up or down. Your last tuning is remembered on that device. It runs fully in your browser with the Web Audio API - the mic signal never leaves your device, and the tab can be hidden in Settings > Features.
- **Backup export** - Settings > Backup downloads the whole collection as one JSON file: gear, photos (stored names and links), sets, songs, presets, setlists, restrings, maintenance entries and string counts.
- **Restring tracking** - log a restring (brand, gauge, date) and each guitar gets a "strings: N days" chip that turns yellow as the interval nears and red when it's overdue. Every guitar has its own restring interval.
- **Previous and next** - every gear page has previous/next buttons (and the left/right arrow keys on a keyboard) to step through your gear without going back to the list. They follow the Gear page order - by section, favorites first, then by name - and respect whatever search or filter you had on. On a phone they shrink to two small arrows with an "N of M" count above the title.
- **Price paid and value** - every item can carry what you paid and what it's worth today (plain USD, both optional). The item page shows the gain or loss, and the Gear page shows a collection total: overall value, what you paid, and how much you're up or down. Items with no value set count at what you paid; items with neither aren't counted. The Want list gets its own total (what it would cost to buy everything on it) and never counts toward the collection, and the Sold view totals what you sold for. Values stay private: share pages never show them. Don't want the numbers on screen? Turn off **Collection value** in Settings > Features: the totals and the prices on item pages and badges go away, and the fields stay in the edit form so you can keep filling them in.
- **Favorites** - tap the star on any card or on the gear page to mark a favorite. Favorites sit at the top of their section, and the "Favorites only" button on the Gear page hides everything else (it remembers your choice on that device).
- **Songs** - a recall sheet for every song you play: tuning, capo, key, BPM, the guitar and amp, and the knob settings on each pedal and amp in the chain (on, off, or toggled mid-song). Knob positions are free text, so "2:00", "noon", "max", and "7.5" all work. Multi-effects units and modelers (Helix, Quad Cortex, Kemper, and so on) get patch pointers instead: patch number and name, scenes, MIDI notes, and an optional list of effect blocks. Add photos of your board or a handwritten sheet. Each gear page lists the songs that use it.
- **Presets** - save a tone once (the chain, knob settings, patches, and amp) under a name like "Classic crunch" or "Ambient clean", then use it in any song. Presets stay linked: change the preset and every song using it shows the new settings. A song can use several presets with labels ("Verse", "Solo"), plus its own settings on top. Turn a song's chain into a preset with **Save as preset**, or use **Copy into song** when one song needs its own tweaked version. Preset cards show the chain at a glance, and a rig in parentheses at the end of the name, like "Treaty Oak (Ampero Mini)", shows as a small tag under the title.
- **Setlists** - line up songs for a practice session or a gig in the Songs tab's Setlists view. Each song in the run shows its linked presets and knob settings right next to it, so one page covers the whole session, and the list flags when the next song needs a retune or a different guitar. Reorder by dragging or with the arrows; a song can appear more than once. Deleting a setlist keeps the songs.
- **Artists** - the Songs tab has an Artists view that groups your songs and presets by artist, so everything for one band sits together. Grouping ignores capitalization and extra spaces.
- **Knob names per pedal** - give a pedal or amp its own control names (Gain, Tone, Level...) and the song editor fills them in for you. Each control can also keep your everyday setting ("Gain: 6"), shown on the item's page as a reference. Mark a pedal as a modeler to track patches for it instead of knobs.
- **Want and Sold lists** - gear you're after (with a target price) and gear you've sold (with sale date and price) live in their own views next to what you own. Sold gear is dimmed and never shows up in the restring due list or notifications.
- **Share links** - make a read-only link to one piece of gear or a whole set to send to a buyer, a tech, or a friend. Links use a random token, can expire (7, 30, or 90 days, or never), can show as a QR code for sharing in person, and can be turned off or replaced at any time. Shared pages hide serial numbers and prices, show no links back into your app, and tell search engines not to index them.
- **Sets** - group gear into rigs: a pedalboard, a gig rig, a recording chain. Gear can live in several sets or none. Each set has its own page with its gear, notes and share link.
- **Pedalboard layout** - a set's page lays its pedals out as a signal chain: guitar in, pedals in order, amp out - left to right on a computer, top to bottom on a phone. Drag a pedal by its handle (mouse or finger) or use the arrow buttons to move it, and the order is saved on the set. **Suggest order** puts pedals in the usual chain order from their names (tuner, wah and filters, compressor, drive and fuzz, EQ, modulation, delay, reverb, looper), and a new set starts that way. The board also totals the pedals' current draw so you can size a power supply. Set names are links wherever they show up: on a gear page, on a song that uses the set, and in the Sets list.
- **Notifications** - Apprise alerts (ntfy, Pushover, Telegram, and 100+ others) when a guitar's strings pass their interval, with quiet hours and overdue repeats.
- **Search and filters** - the Gear page search matches names, makes, models, and spec values like a gauge, and a string-type filter narrows the Strings section. The API takes the same search as `?q=`.
- **Hideable sections** - don't have pedals? Don't care about songs or a wish list? Turn the section off in Settings > Features and it leaves the interface. Hidden sections keep their data.
- **Install it like an app** - add Gearsmith to your phone's home screen (Share > Add to Home Screen on iPhone, Install app on Android) and it opens in its own window without the browser bar, with a proper icon on both. Long-press the icon on Android for shortcuts to Gear, Setlists and the Tuner. There's no offline mode on purpose: your data lives on your server, and caching pages on the phone would risk showing stale gear after an update.
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

### Upgrading

Pull the new image and recreate the container; your data volume carries over. When an upgrade has to reshape the database (0.3.1 does, once, to add the strings type), Gearsmith first writes a full copy of it next to the original, named like `gearsmith-backup-before-0.3.1.db`. Once you're happy with the new version you can delete that file.

## Notifications

Settings > Notifications takes any [Apprise](https://github.com/caronc/apprise) URLs, one per line - `ntfy://ntfy.sh/your-topic`, `pover://user@token`, `tgram://...`, and so on. Gearsmith sends when a guitar passes its restring interval, once when it flips overdue and then every N days (your choice) until you log the restring. Send hour and quiet hours are configurable, and there's a test button.

## API tokens and AI assistants

Settings > API tokens creates a token that acts as you over the REST API. Interactive docs (with a "try it" button) are at `/api/docs`; the OpenAPI spec is at `/api/v1/openapi.json`.

To connect an AI assistant, paste it something like this, with your own URL and token filled in:

```
You can manage my guitar gear through the Gearsmith API at https://YOUR-URL-HERE.
Authenticate every request with the header: Authorization: Bearer YOUR-TOKEN-HERE
The interactive docs are at /api/docs and the OpenAPI spec at /api/v1/openapi.json.
Gear: list and add (GET/POST /api/v1/gear, filter the list with ?type=guitar, amp, pedal, pick
or strings, ?lifecycle=owned, want or sold, and ?q= to search names and specs), read one item (GET /api/v1/gear/{id}), update it (PATCH /api/v1/gear/{id}). Every item
has "favorite" (true/false) and "lifecycle" (owned, want or sold); want items can carry
want_price, sold items sold_date and sold_price. Any item can carry purchase_price (what
you paid) and current_value (what it's worth now), both in USD. GET /api/v1/collection returns
the totals: owned value (value, falling back to price paid), paid, change, plus want and sold. Pedals and amps can list their knob names in
specs.controls ([{name, kind, value?}], where value is your everyday setting), and
specs.modeler marks a multi-effects unit. Strings items (type "strings")
use make for the brand and specs gauge, string_type (electric, acoustic, classical or bass),
material, strings_per_set and sets_per_pack. A guitar's strings_id points at the strings it uses. Every item can carry manual_url (a link
to its manual), and strings items can carry sets_on_hand (unopened sets in stock); logging a
restring with strings_id takes one set off that count.
Restrings: log one (POST /api/v1/gear/{id}/restrings with brand, gauge and optional date, or
strings_id to fill brand and gauge from a strings item and switch the guitar to it) and
check what's due (GET /api/v1/due). Maintenance: list and log entries (GET/POST
/api/v1/gear/{id}/maintenance with date, category - setup, tubes, fret work, repair or other -
and note), edit or delete one (PATCH/DELETE /api/v1/maintenance/{entry_id}).
Sets: GET/POST /api/v1/sets. PATCH /api/v1/sets/{id} with item_ids sets the order, which is the
pedalboard's signal chain for pedals.
Photos: attach one (POST /api/v1/gear/{id}/photos, multipart field "photo"), delete one
(DELETE /api/v1/photos/{photo_id}), or make one the cover (POST /api/v1/photos/{photo_id}/cover).
Songs: list, search and add (GET/POST /api/v1/songs, ?q= searches title and artist, ?gear_id=
finds songs using a piece of gear), read, update or delete one (GET/PATCH/DELETE
/api/v1/songs/{id}). A song has title, artist, tuning, capo, key, bpm, guitar_id, amp_id,
notes, a "rig" list (gear_id, engaged on/off/toggle, knobs as [{name, value}] with text
values) and a "patches" list for modelers (gear_id, patch_ref, patch_name, scenes, note).
Edit single entries with /api/v1/songs/{id}/rig/{setting_id} and
/api/v1/songs/{id}/patches/{patch_id}. ?artist= lists one artist's songs.
Presets: list and add (GET/POST /api/v1/presets), read, update or delete one
(GET/PATCH/DELETE /api/v1/presets/{id}). A preset has name, artist, amp_id, notes, and the
same "rig" and "patches" lists as a song. Songs point at presets with a "presets" list
([{preset_id, label, note}]); editing a preset changes every song that uses it.
Preset reads also include chain_summary: the chain's gear and effect names in signal order.
POST /api/v1/songs/{id}/save-as-preset (name, use_in_song) turns a song's chain into a preset.
Artists: GET /api/v1/artists returns songs and presets grouped by artist.
Setlists: list and add (GET/POST /api/v1/setlists with name, notes and songs as
[{song_id, note}] in play order), read one with every song's presets and settings
(GET /api/v1/setlists/{id}), reorder or rename (PATCH, sending songs replaces the list),
or delete (DELETE).
Share links: create or replace one (POST /api/v1/gear/{id}/share or /api/v1/sets/{id}/share
with optional expires_in_days and regenerate), read it (GET) or turn it off (DELETE).
When I tell you I restrung a guitar, log it. When I ask what needs new strings, check the
due list. When I tell you how I set my rig for a song, save it on that song. When I
describe a tone I use in several songs, save it as a preset and link those songs to it.
When I plan a practice, build a setlist from my songs.
```

Treat tokens like passwords - anyone holding one can read and change your gear.

## Backups

Settings > Backup downloads everything as JSON (gear, photo references, sets, songs, presets, restrings, maintenance and stock counts). The photo files themselves live in your data volume, so back up `./data` for a complete copy.

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
