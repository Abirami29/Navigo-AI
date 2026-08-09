-- Navigo schema for Lakebase (Postgres)
-- Run via notebooks/00_setup_lakebase.py, or `psql $LAKEBASE_DSN -f schema.sql`

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ── Core identity ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS users (
    user_id      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    display_name TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS trips (
    trip_id                 UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id                 UUID NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    trip_name                TEXT NOT NULL,
    start_date               DATE NOT NULL,
    end_date                 DATE NOT NULL,
    home_base_destination_id UUID,
    status                    TEXT NOT NULL DEFAULT 'planning'
        CHECK (status IN ('planning', 'active', 'completed')),
    -- "Preferences" from the product brief: free-text interests the agent
    -- uses as the semantic search query when picking activities (see
    -- navigo.agent.retrieval.semantic_search_activities). Age/mobility/diet/
    -- sensory constraints live on `travelers` instead, since those are hard
    -- per-person filters, not soft interest signals.
    interests                 TEXT[] NOT NULL DEFAULT '{}',  -- e.g. {museums, hiking, castles, animals}
    notes                      TEXT,                          -- free-text trip notes, folded into the search query too
    created_at                TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Who is actually coming. Drives nap scheduling, walking budgets,
-- accessibility filtering, and dietary filtering downstream.
CREATE TABLE IF NOT EXISTS travelers (
    traveler_id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    trip_id               UUID NOT NULL REFERENCES trips(trip_id) ON DELETE CASCADE,
    label                  TEXT NOT NULL,               -- "Mum", "Leo (age 4)"
    age_years              NUMERIC,
    mobility_need          TEXT NOT NULL DEFAULT 'none'
        CHECK (mobility_need IN ('none', 'wheelchair', 'stroller', 'limited_walking')),
    max_walk_minutes       INT,
    nap_window_start       TIME,
    nap_window_end         TIME,
    sensory_notes           TEXT,
    dietary_restrictions   TEXT[] NOT NULL DEFAULT '{}'
);

-- ── Places ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS destinations (
    destination_id     UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name                 TEXT NOT NULL,
    country               TEXT,
    latitude              NUMERIC NOT NULL,
    longitude             NUMERIC NOT NULL,
    wikimedia_summary     TEXT,
    embedding_ref         TEXT,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (name, country)
);

CREATE TABLE IF NOT EXISTS activities (
    activity_id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    destination_id          UUID NOT NULL REFERENCES destinations(destination_id) ON DELETE CASCADE,
    name                      TEXT NOT NULL,
    category                  TEXT NOT NULL
        CHECK (category IN ('attraction', 'restaurant', 'playground', 'museum', 'outdoor', 'other')),
    description                TEXT,
    is_outdoor                 BOOLEAN NOT NULL DEFAULT FALSE,
    latitude                    NUMERIC,
    longitude                   NUMERIC,
    osm_wheelchair              TEXT NOT NULL DEFAULT 'unknown'
        CHECK (osm_wheelchair IN ('yes', 'limited', 'no', 'unknown')),
    has_accessible_toilet       BOOLEAN,
    has_changing_table          BOOLEAN,
    has_highchairs               BOOLEAN,
    stroller_friendly            BOOLEAN,
    min_recommended_age          INT,
    max_recommended_age          INT,
    typical_visit_minutes        INT,
    quiet_hours                   TEXT,
    dietary_tags                  TEXT[] NOT NULL DEFAULT '{}',
    accessibility_verified        BOOLEAN NOT NULL DEFAULT FALSE,  -- true once a human confirms OSM data
    embedding_ref                  TEXT,
    source                          TEXT NOT NULL DEFAULT 'overpass',
    created_at                       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_activities_destination ON activities(destination_id);
CREATE INDEX IF NOT EXISTS idx_activities_category ON activities(category);

-- ── Itinerary ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS itinerary_items (
    item_id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    trip_id                UUID NOT NULL REFERENCES trips(trip_id) ON DELETE CASCADE,
    activity_id             UUID NOT NULL REFERENCES activities(activity_id),
    day_date                 DATE NOT NULL,
    start_time                TIME NOT NULL,
    end_time                  TIME NOT NULL,
    status                      TEXT NOT NULL DEFAULT 'planned'
        CHECK (status IN ('planned', 'rescheduled', 'cancelled')),
    rescheduled_reason         TEXT,
    original_item_id            UUID REFERENCES itinerary_items(item_id) ON DELETE SET NULL,
    created_at                   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_itinerary_trip_day ON itinerary_items(trip_id, day_date);

CREATE TABLE IF NOT EXISTS weather_snapshots (
    snapshot_id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    destination_id         UUID NOT NULL REFERENCES destinations(destination_id) ON DELETE CASCADE,
    captured_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    forecast_date             DATE NOT NULL,
    hour                        INT NOT NULL CHECK (hour BETWEEN 0 AND 23),
    temp_c                      NUMERIC,
    precipitation_prob           NUMERIC,
    wind_kph                     NUMERIC,
    aqi                          INT,
    pm25                         NUMERIC,
    uv_index                     NUMERIC,
    pollen_level                  TEXT,
    -- One row per destination/date/hour: refreshing weather should update
    -- the existing forecast, not pile up a new row every time the job or a
    -- seed run fires. See navigo.ingestion.pipeline.refresh_weather, which
    -- does INSERT ... ON CONFLICT DO UPDATE against this constraint.
    UNIQUE (destination_id, forecast_date, hour)
);

CREATE INDEX IF NOT EXISTS idx_weather_dest_date ON weather_snapshots(destination_id, forecast_date, hour);

CREATE TABLE IF NOT EXISTS packing_items (
    packing_item_id      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    trip_id                UUID NOT NULL REFERENCES trips(trip_id) ON DELETE CASCADE,
    traveler_id              UUID REFERENCES travelers(traveler_id) ON DELETE CASCADE,
    item_name                  TEXT NOT NULL,
    category                    TEXT NOT NULL
        CHECK (category IN ('clothing', 'medical', 'comfort', 'documents', 'other')),
    reason                       TEXT,
    packed                        BOOLEAN NOT NULL DEFAULT FALSE
);

-- Audit trail: makes "explain why it made each change" a real, queryable
-- record instead of just a one-off chat message.
CREATE TABLE IF NOT EXISTS agent_decisions (
    decision_id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    trip_id                UUID NOT NULL REFERENCES trips(trip_id) ON DELETE CASCADE,
    item_id                  UUID REFERENCES itinerary_items(item_id) ON DELETE SET NULL,
    decision_type              TEXT NOT NULL
        CHECK (decision_type IN ('reschedule', 'swap', 'remove', 'packing_suggestion', 'accessibility_flag')),
    trigger                     TEXT NOT NULL
        CHECK (trigger IN ('rain_forecast', 'high_aqi', 'nap_conflict', 'walk_budget_exceeded',
                            'unverified_accessibility', 'user_request')),
    explanation                  TEXT NOT NULL,
    created_at                     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_decisions_trip ON agent_decisions(trip_id);
