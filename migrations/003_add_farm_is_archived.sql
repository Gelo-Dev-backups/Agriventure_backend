-- 003_add_farm_is_archived.sql
-- Soft-delete support for farms: archiving hides a farm from the app
-- without touching its sensors/readings/analyses, so nothing under it is
-- lost. list_farms filters is_archived = false by default.

ALTER TABLE "farms"
  ADD COLUMN IF NOT EXISTS is_archived BOOLEAN NOT NULL DEFAULT false;

CREATE INDEX IF NOT EXISTS idx_farms_is_archived ON "farms" (is_archived);
