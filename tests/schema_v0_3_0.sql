-- Schema of a Gearsmith database first created by the earliest release and upgraded to v0.3.0.
-- Used by tests/test_api.py to check that upgrading keeps every row.
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE users (
          id INTEGER PRIMARY KEY,
          username TEXT NOT NULL UNIQUE COLLATE NOCASE,
          password_hash TEXT NOT NULL,
          salt TEXT NOT NULL,
          is_admin INTEGER NOT NULL DEFAULT 0,
          active INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL
        );
CREATE TABLE sessions (
          token_hash TEXT PRIMARY KEY,
          user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          expires_at TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
CREATE TABLE api_tokens (
          id INTEGER PRIMARY KEY,
          name TEXT NOT NULL,
          token_hash TEXT NOT NULL UNIQUE,
          prefix TEXT NOT NULL,
          created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          created_at TEXT NOT NULL,
          last_used_at TEXT,
          revoked INTEGER NOT NULL DEFAULT 0
        );
CREATE TABLE gear (
          id INTEGER PRIMARY KEY,
          type TEXT NOT NULL CHECK (type IN ('guitar','amp','pedal','pick')),
          name TEXT NOT NULL,
          make TEXT NOT NULL DEFAULT '',
          model TEXT NOT NULL DEFAULT '',
          year INTEGER,
          serial TEXT NOT NULL DEFAULT '',
          specs TEXT NOT NULL DEFAULT '{}',
          status TEXT NOT NULL DEFAULT '',
          purchase_date TEXT,
          purchase_price REAL,
          notes TEXT NOT NULL DEFAULT '',
          restring_interval_days INTEGER,
          created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        , favorite INTEGER NOT NULL DEFAULT 0, lifecycle TEXT NOT NULL DEFAULT 'owned', want_price REAL, sold_date TEXT, sold_price REAL);
CREATE TABLE gear_photos (
          id INTEGER PRIMARY KEY,
          gear_id INTEGER NOT NULL REFERENCES gear(id) ON DELETE CASCADE,
          filename TEXT NOT NULL,
          sort INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL
        );
CREATE TABLE restrings (
          id INTEGER PRIMARY KEY,
          gear_id INTEGER NOT NULL REFERENCES gear(id) ON DELETE CASCADE,
          brand TEXT NOT NULL DEFAULT '',
          gauge TEXT NOT NULL DEFAULT '',
          date TEXT NOT NULL,
          note TEXT NOT NULL DEFAULT '',
          user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
          via TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL
        );
CREATE TABLE sets (
          id INTEGER PRIMARY KEY,
          name TEXT NOT NULL UNIQUE COLLATE NOCASE,
          notes TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
CREATE TABLE set_items (
          set_id INTEGER NOT NULL REFERENCES sets(id) ON DELETE CASCADE,
          gear_id INTEGER NOT NULL REFERENCES gear(id) ON DELETE CASCADE,
          sort INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY (set_id, gear_id)
        );
CREATE TABLE shares (
          id INTEGER PRIMARY KEY,
          token TEXT NOT NULL UNIQUE,
          gear_id INTEGER UNIQUE REFERENCES gear(id) ON DELETE CASCADE,
          set_id INTEGER UNIQUE REFERENCES sets(id) ON DELETE CASCADE,
          expires_at TEXT,
          created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
          created_at TEXT NOT NULL,
          CHECK ((gear_id IS NULL) != (set_id IS NULL))
        );
CREATE TABLE songs (
          id INTEGER PRIMARY KEY,
          title TEXT NOT NULL,
          artist TEXT NOT NULL DEFAULT '',
          tuning TEXT NOT NULL DEFAULT '',
          capo INTEGER,
          song_key TEXT NOT NULL DEFAULT '',
          bpm INTEGER,
          guitar_id INTEGER REFERENCES gear(id) ON DELETE SET NULL,
          guitar_name TEXT NOT NULL DEFAULT '',
          amp_id INTEGER REFERENCES gear(id) ON DELETE SET NULL,
          amp_name TEXT NOT NULL DEFAULT '',
          set_id INTEGER REFERENCES sets(id) ON DELETE SET NULL,
          set_name TEXT NOT NULL DEFAULT '',
          notes TEXT NOT NULL DEFAULT '',
          created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
CREATE TABLE song_gear_settings (
          id INTEGER PRIMARY KEY,
          song_id INTEGER NOT NULL REFERENCES songs(id) ON DELETE CASCADE,
          gear_id INTEGER REFERENCES gear(id) ON DELETE SET NULL,
          gear_name TEXT NOT NULL DEFAULT '',
          position INTEGER NOT NULL DEFAULT 0,
          engaged TEXT NOT NULL DEFAULT 'on',
          knobs TEXT NOT NULL DEFAULT '[]',
          note TEXT NOT NULL DEFAULT ''
        );
CREATE TABLE song_device_patches (
          id INTEGER PRIMARY KEY,
          song_id INTEGER NOT NULL REFERENCES songs(id) ON DELETE CASCADE,
          gear_id INTEGER REFERENCES gear(id) ON DELETE SET NULL,
          gear_name TEXT NOT NULL DEFAULT '',
          position INTEGER NOT NULL DEFAULT 0,
          patch_ref TEXT NOT NULL DEFAULT '',
          patch_name TEXT NOT NULL DEFAULT '',
          scenes TEXT NOT NULL DEFAULT '[]',
          midi TEXT,
          note TEXT NOT NULL DEFAULT ''
        );
CREATE TABLE song_effect_blocks (
          id INTEGER PRIMARY KEY,
          patch_id INTEGER NOT NULL REFERENCES song_device_patches(id) ON DELETE CASCADE,
          position INTEGER NOT NULL DEFAULT 0,
          slot TEXT NOT NULL DEFAULT '',
          block_type TEXT NOT NULL DEFAULT '',
          model TEXT NOT NULL DEFAULT '',
          enabled INTEGER NOT NULL DEFAULT 1,
          params TEXT NOT NULL DEFAULT '[]',
          scene_overrides TEXT
        );
CREATE TABLE song_photos (
          id INTEGER PRIMARY KEY,
          song_id INTEGER NOT NULL REFERENCES songs(id) ON DELETE CASCADE,
          filename TEXT NOT NULL,
          sort INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL
        );
CREATE TABLE presets (
          id INTEGER PRIMARY KEY,
          name TEXT NOT NULL,
          artist TEXT NOT NULL DEFAULT '',
          amp_id INTEGER REFERENCES gear(id) ON DELETE SET NULL,
          amp_name TEXT NOT NULL DEFAULT '',
          notes TEXT NOT NULL DEFAULT '',
          created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
CREATE TABLE preset_gear_settings (
          id INTEGER PRIMARY KEY,
          preset_id INTEGER NOT NULL REFERENCES presets(id) ON DELETE CASCADE,
          gear_id INTEGER REFERENCES gear(id) ON DELETE SET NULL,
          gear_name TEXT NOT NULL DEFAULT '',
          position INTEGER NOT NULL DEFAULT 0,
          engaged TEXT NOT NULL DEFAULT 'on',
          knobs TEXT NOT NULL DEFAULT '[]',
          note TEXT NOT NULL DEFAULT ''
        );
CREATE TABLE preset_device_patches (
          id INTEGER PRIMARY KEY,
          preset_id INTEGER NOT NULL REFERENCES presets(id) ON DELETE CASCADE,
          gear_id INTEGER REFERENCES gear(id) ON DELETE SET NULL,
          gear_name TEXT NOT NULL DEFAULT '',
          position INTEGER NOT NULL DEFAULT 0,
          patch_ref TEXT NOT NULL DEFAULT '',
          patch_name TEXT NOT NULL DEFAULT '',
          scenes TEXT NOT NULL DEFAULT '[]',
          midi TEXT,
          note TEXT NOT NULL DEFAULT ''
        );
CREATE TABLE preset_effect_blocks (
          id INTEGER PRIMARY KEY,
          patch_id INTEGER NOT NULL REFERENCES preset_device_patches(id) ON DELETE CASCADE,
          position INTEGER NOT NULL DEFAULT 0,
          slot TEXT NOT NULL DEFAULT '',
          block_type TEXT NOT NULL DEFAULT '',
          model TEXT NOT NULL DEFAULT '',
          enabled INTEGER NOT NULL DEFAULT 1,
          params TEXT NOT NULL DEFAULT '[]',
          scene_overrides TEXT
        );
CREATE TABLE song_presets (
          id INTEGER PRIMARY KEY,
          song_id INTEGER NOT NULL REFERENCES songs(id) ON DELETE CASCADE,
          preset_id INTEGER NOT NULL REFERENCES presets(id) ON DELETE CASCADE,
          position INTEGER NOT NULL DEFAULT 0,
          label TEXT NOT NULL DEFAULT '',
          note TEXT NOT NULL DEFAULT ''
        );
CREATE INDEX idx_gear_type ON gear(type);
CREATE INDEX idx_restrings_gear ON restrings(gear_id, date);
CREATE INDEX idx_photos_gear ON gear_photos(gear_id);
CREATE INDEX idx_preset_settings ON preset_gear_settings(preset_id, position);
CREATE INDEX idx_preset_patches ON preset_device_patches(preset_id, position);
CREATE INDEX idx_preset_blocks ON preset_effect_blocks(patch_id, position);
CREATE INDEX idx_song_presets ON song_presets(song_id, position);
CREATE INDEX idx_preset_songs ON song_presets(preset_id);
CREATE INDEX idx_song_settings ON song_gear_settings(song_id, position);
CREATE INDEX idx_song_patches ON song_device_patches(song_id, position);
CREATE INDEX idx_song_blocks ON song_effect_blocks(patch_id, position);
CREATE INDEX idx_song_photos ON song_photos(song_id);
