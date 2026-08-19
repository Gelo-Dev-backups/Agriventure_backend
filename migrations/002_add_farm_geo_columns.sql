-- 002_add_farm_geo_columns.sql
-- Adds a queryable center point (latitude/longitude) to "farms" so the
-- Flutter map can center on a saved farm and list results without parsing
-- the boundary_coordinates JSON blob. Sensor counts and health status are
-- deliberately NOT stored columns - they're computed at query time in
-- routes/farms.py from live sensor/reading data, to avoid duplicated,
-- staleness-prone state.

ALTER TABLE "farms"
  ADD COLUMN IF NOT EXISTS latitude DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS longitude DOUBLE PRECISION;

CREATE INDEX IF NOT EXISTS idx_farms_lat_lng ON "farms" (latitude, longitude);
