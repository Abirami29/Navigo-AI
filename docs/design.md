# Family Adventure & Access Planner
### A weather-aware, kid-first, accessibility-first holiday planning agent — built entirely on Databricks Free Edition

---

## 1. The reframe

The original brief is a solid technical skeleton (Open-Meteo + Wikimedia + Lakebase + an agent). What's missing is the *reason a family would open the app at 11pm exhausted*: they don't just want a weather-aware itinerary — they want to stop doing five hours of tab-juggling across TripAdvisor, "kid-friendly restaurants near me," council accessibility PDFs, and a weather app, and still end up unsure if the pushchair will fit or the toddler will nap through it.

So the product becomes: **you tell it who's coming (ages, mobility needs, nap windows, allergies) and where — it builds the day, explains every call it makes, and re-plans the moment weather, air quality, or the kids' own limits change.**

This gives you a genuinely differentiated mockup: most "AI trip planner" demos are single-user, adult-oriented, and ignore accessibility entirely. Framing every table, retrieval step, and agent tool around a *family unit* rather than a generic "user" is what makes this defensible as production-grade thinking rather than a toy.

---

## 2. Who it's for (personas to design against)

| Persona | Core need |
|---|---|
| Parent of a 3 and 6 year old | Nap-aware scheduling, stroller access, kid menus, shaded playgrounds as buffer activities |
| Parent of a wheelchair-using teen | Step-free routes, accessible toilets/changing places, venue capacity info, realistic walking distances |
| Parent of a child with sensory needs | Quiet-hour info, indoor fallback for meltdown risk, low-stimulation venue flags |
| Parent with a food allergy in the family | Menu/allergy notes surfaced per restaurant, not buried in reviews |

Every schema and agent tool below should trace back to one of these.

---

## 3. Data sources (all free, no card required)

| Source | What it gives you | Notes |
|---|---|---|
| **Open-Meteo Geocoding API** | destination name → lat/lon | no key, noncommercial free tier |
| **Open-Meteo Weather API** | hourly forecast (rain, temp, wind) | drives outdoor/indoor rescheduling |
| **Open-Meteo Air Quality API** | AQI, PM2.5/PM10, UV index, pollen | critical for kids with asthma/allergies — bigger deal for families than the original brief treats it |
| **Wikimedia (Wikipedia/Wikivoyage) API** | destination + attraction descriptions | good for the "why we picked this" narrative text |
| **OpenStreetMap Overpass API** *(new — this is the piece that makes it a family/accessibility product, not a generic one)* | POI-level tags: `wheelchair=yes/limited/no`, `toilets:wheelchair=yes`, `changing_table=yes`, `highchair=yes`, `playground`, `stroller`-relevant `surface`/`smoothness` tags, `diet:*` for allergy-relevant food tags | free, no key, queryable by bounding box + tag filters — this is how you get real "step-free route to a playground with a changing table" data without paying for Google Places |

Why Overpass matters here: Wikimedia tells you *what* a place is; Overpass tells you *whether a family with a wheelchair or a pram can actually use it*. Without it you're back to a generic itinerary tool with a weather widget bolted on.

Optional stretch source: **Nominatim** as a geocoding fallback if Open-Meteo geocoding doesn't resolve a POI name.

---

## 4. Lakebase schema (Postgres via Lakebase — Free Edition, GA)

Expanded from the original 7 tables to carry family composition and accessibility as first-class data, not an afterthought bolted onto `activities`.

```sql
-- Core identity
users (
  user_id UUID PRIMARY KEY,
  display_name TEXT,
  created_at TIMESTAMPTZ
);

-- A trip belongs to a family unit, not a single user
trips (
  trip_id UUID PRIMARY KEY,
  user_id UUID REFERENCES users,
  trip_name TEXT,
  start_date DATE,
  end_date DATE,
  home_base_destination_id UUID,
  status TEXT -- planning | active | completed
);

-- NEW: who's actually coming — this drives nearly everything downstream
travelers (
  traveler_id UUID PRIMARY KEY,
  trip_id UUID REFERENCES trips,
  label TEXT,                 -- "Mum", "Leo (age 4)"
  age_years NUMERIC,           -- null for adults if not needed
  mobility_need TEXT,          -- none | wheelchair | stroller | limited_walking
  max_walk_minutes INT,        -- realistic per-traveler walking budget
  nap_window_start TIME,       -- null if n/a
  nap_window_end TIME,
  sensory_notes TEXT,          -- e.g. "avoid loud/crowded venues after 3pm"
  dietary_restrictions TEXT[]  -- e.g. {peanut_allergy, vegetarian}
);

destinations (
  destination_id UUID PRIMARY KEY,
  name TEXT,
  country TEXT,
  latitude NUMERIC,
  longitude NUMERIC,
  wikimedia_summary TEXT,
  embedding_ref TEXT           -- pointer to vector index entry
);

-- Attractions / restaurants / venues, enriched with Overpass accessibility tags
activities (
  activity_id UUID PRIMARY KEY,
  destination_id UUID REFERENCES destinations,
  name TEXT,
  category TEXT,               -- attraction | restaurant | playground | museum | outdoor
  description TEXT,
  is_outdoor BOOLEAN,
  osm_wheelchair TEXT,         -- yes | limited | no | unknown
  has_accessible_toilet BOOLEAN,
  has_changing_table BOOLEAN,
  has_highchairs BOOLEAN,
  stroller_friendly BOOLEAN,
  min_recommended_age INT,
  max_recommended_age INT,
  typical_visit_minutes INT,
  quiet_hours TEXT,            -- for sensory-sensitive kids
  dietary_tags TEXT[],         -- restaurants: vegetarian, nut_free, etc.
  embedding_ref TEXT
);

itinerary_items (
  item_id UUID PRIMARY KEY,
  trip_id UUID REFERENCES trips,
  activity_id UUID REFERENCES activities,
  day_date DATE,
  start_time TIME,
  end_time TIME,
  status TEXT,                  -- planned | rescheduled | cancelled
  rescheduled_reason TEXT,       -- human-readable, agent-authored explanation
  original_item_id UUID          -- traceability when the agent moves something
);

weather_snapshots (
  snapshot_id UUID PRIMARY KEY,
  destination_id UUID REFERENCES destinations,
  captured_at TIMESTAMPTZ,
  forecast_date DATE,
  hour INT,
  temp_c NUMERIC,
  precipitation_prob NUMERIC,
  wind_kph NUMERIC,
  aqi INT,
  pm25 NUMERIC,
  uv_index NUMERIC,
  pollen_level TEXT
);

packing_items (
  packing_item_id UUID PRIMARY KEY,
  trip_id UUID REFERENCES trips,
  traveler_id UUID REFERENCES travelers,  -- packing is per-child, not per-trip
  item_name TEXT,
  category TEXT,                -- clothing | medical | comfort | documents
  reason TEXT,                  -- "sunny + high UV forecast" / "nap comfort item"
  packed BOOLEAN DEFAULT FALSE
);

-- NEW: audit trail the original brief implies but doesn't schema for.
-- "Explain why it made each weather-based change" needs somewhere to live.
agent_decisions (
  decision_id UUID PRIMARY KEY,
  trip_id UUID REFERENCES trips,
  item_id UUID REFERENCES itinerary_items,
  decision_type TEXT,           -- reschedule | swap | packing_suggestion
  trigger TEXT,                 -- rain_forecast | high_aqi | nap_conflict | walk_budget_exceeded
  explanation TEXT,
  created_at TIMESTAMPTZ
);
```

`agent_decisions` is the table that turns "explain why it made each change" from a prompt instruction into something you can actually show the user as a timeline/log in the UI — and it's what makes the demo *feel* trustworthy to a parent, which is the whole point.

---

## 5. Context engineering

**Embed** (via Databricks Vector Search on Free Edition, backed by a Foundation Model embedding endpoint):
- `destinations.wikimedia_summary`
- `activities.description` + concatenated accessibility/kid metadata (so "step-free playground with changing table, quiet before 2pm" is retrievable by meaning, not just filterable by column)
- Free-text `sensory_notes` / user notes per traveler

**Retrieve, filtered before ranked** — this ordering matters for a family product:
1. Hard filters first (non-negotiable): `osm_wheelchair != 'no'` if any traveler needs it, `max_recommended_age` covers the youngest child, `dietary_tags` excludes allergens, `is_outdoor` flag checked against current weather/AQI.
2. Semantic retrieval second, over the filtered set, using the family's stated interests/notes.

This ordering is the key design decision: a beautifully-matched activity that isn't step-free or nut-free isn't a near-miss, it's disqualifying. Doing semantic search first and filtering after (the more common naive pattern) risks surfacing things that look great and simply don't work for the family.

---

## 6. Agent capabilities (expanded from the brief)

| Capability | Family/accessibility-specific behavior |
|---|---|
| Generate day-by-day itinerary | Respects each traveler's `max_walk_minutes`, avoids scheduling anything during any child's nap window, alternates high-stimulation and low-stimulation blocks |
| Reschedule for rain/AQI | Same as brief, plus: swaps to indoor *and* still checks accessibility/age fit of the replacement, not just "any indoor activity" |
| Reschedule for kid-energy limits | New: if the day's cumulative walk time exceeds a traveler's budget, proposes a swap or a rest/playground buffer — not just weather-triggered |
| Build packing list | Per-traveler, tied to forecast (UV, rain, temp) *and* to `mobility_need`/`sensory_notes` (e.g., ear defenders, spare change of clothes, mobility aid spares) |
| Add/remove/move items | Standard CRUD, logs to `agent_decisions` |
| Explain each change | Writes a plain-language `explanation` row a parent can actually read: *"Moved the castle visit to 10am — afternoon AQI is forecast high and Leo's asthma notes flagged that."* |
| **New: pre-trip accessibility check** | On save, agent flags any itinerary item where accessibility/age data is `unknown` so the parent can double-check before relying on it — important for trust; don't silently assume |

That last one matters a lot for an accessibility-focused product: OSM data has gaps, and a family product that confidently states "step-free" when the tag is actually missing is worse than useless. Surfacing "unverified — check ahead" is the honest, production-grade move.

---

## 7. Architecture on Databricks Free Edition

```
┌─────────────────────────────────────────────────────────────┐
│ Databricks App (Free Edition Apps)                            │
│  — family/trip UI, itinerary timeline, "why" log               │
└───────────────┬─────────────────────────────────────────────┘
                │
┌───────────────▼─────────────────────────────────────────────┐
│ Agent (Agent Bricks / Mosaic AI Agent Framework)               │
│  tools: geocode, get_weather, get_air_quality, search_venues,  │
│         reschedule_item, build_packing_list, log_decision       │
│  model: Databricks-hosted foundation model via Model Serving   │
└───────────────┬─────────────────────────────────────────────┘
                │
   ┌────────────┼─────────────────┬───────────────────┐
┌──▼───┐   ┌─────▼──────┐   ┌──────▼──────┐    ┌───────▼──────┐
│Lakebase│  │Vector Search│  │Lakeflow Jobs │    │External APIs │
│(Postgres│  │(embeddings  │  │(scheduled    │    │Open-Meteo x3 │
│ OLTP,   │  │ for destin- │  │ weather/AQI  │    │Wikimedia     │
│ GA)     │  │ ations &    │  │ refresh,     │    │Overpass API  │
│         │  │ activities) │  │ Overpass POI │    │              │
│         │  │             │  │ sync)        │    │              │
└────────┘   └─────────────┘  └─────────────┘    └──────────────┘
        all governed through Unity Catalog (lineage + access control)
        MLflow 3 traces every agent run for debugging/eval
```

- **Lakebase** — your seven-ish tables above, transactional, queried directly by the agent tools and the app.
- **Lakeflow Jobs** — a scheduled job to refresh `weather_snapshots` per active destination, and a separate ingestion job to pull/refresh Overpass POI data into `activities` (Overpass has usage etiquette — batch and cache, don't call it live per-request).
- **Vector Search** — semantic layer over `destinations`/`activities`, filtered first by the hard accessibility/age/diet constraints.
- **Agent Bricks / Mosaic AI Agent Framework** — orchestrates the tool calls, calls a Databricks-hosted foundation model (e.g. via Model Serving) for reasoning and the "explain why" text generation.
- **MLflow 3** — traces every agent decision, useful both for debugging and as a demoable "here's how the agent reasoned" artifact.
- **Unity Catalog** — governs everything, gives you lineage from raw Overpass/Open-Meteo ingestion through to what the agent showed the user — good to show off in a portfolio piece since it's the thing that makes this "production-grade" rather than a notebook hack.

### Free Edition constraints to design around
- **Serverless-only, quota-based** — no long-running always-on clusters; design Lakeflow jobs to run on a schedule (e.g. every 3–6 hours for weather, daily for POI sync) rather than continuously, to stay well inside fair-use limits.
- **No SLA/reliability guarantee** — fine for a mockup/portfolio project, but worth a line in your README so it reads as an informed constraint, not an oversight.
- **Per-account quotas on jobs, model serving, Lakebase projects, apps** — keep it to one Lakebase project and one Databricks App; batch your external API calls rather than firing them per-user-request.

---

## 8. Suggested build order

1. **Schema first** — stand up the Lakebase tables above, seed `destinations`/`activities` for 1–2 test destinations manually so you can build against real data before automating ingestion.
2. **Ingestion jobs** — Lakeflow job for Open-Meteo (geocode → weather → AQI) and one for Overpass (POI + accessibility tags) into `activities`.
3. **Vector Search index** over `destinations` + `activities`.
4. **Agent tools**, one at a time: geocode → weather/AQI → filtered+semantic search → itinerary generation → reschedule logic → packing list → decision logging.
5. **Databricks App UI** — trip setup (traveler profiles with accessibility/nap fields), itinerary timeline, "why" log view.
6. **Eval pass** — a handful of scripted scenarios (rainy day, high pollen day, wheelchair user, two kids with conflicting nap windows) to demo the reschedule logic convincingly.

---

Want me to start on step 1 — write the actual Lakebase SQL as a runnable script — or would it help more to first sketch the traveler-profile UI so the accessibility/kid fields are concrete before you build the schema around them?
