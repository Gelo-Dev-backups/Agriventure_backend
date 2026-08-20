-- 004_role_battery_advice_engine.sql
-- Brings this Python backend up to parity with the PHP/XAMPP backend's
-- later additions: account roles + IoT device tokens, per-reading battery
-- tracking, and the rule-based recommendation engine (advice_rules).
--
-- Deliberately NOT included: irrigation_settings / Controls. There is no
-- irrigation device/actuator yet - AgriVenture is monitoring + advice
-- focused for now (same reasoning as agriventure_backedn_xampp's
-- IRRIGATION_CONTROL_ENABLED=False). Add that table/feature later if real
-- hardware shows up.

-- ---------------------------------------------------------------------------
-- users: role + IoT device token
-- ---------------------------------------------------------------------------
ALTER TABLE "users"
    ADD COLUMN IF NOT EXISTS role VARCHAR(20) NOT NULL DEFAULT 'user',
    ADD COLUMN IF NOT EXISTS iot_token TEXT NULL,
    ADD COLUMN IF NOT EXISTS iot_token_expires_at TIMESTAMPTZ NULL;

ALTER TABLE "users"
    ADD CONSTRAINT chk_users_role CHECK (role IN ('user', 'iot', 'admin', 'superadmin'));

CREATE INDEX IF NOT EXISTS idx_users_role ON "users" (role);

-- Seed a default superadmin so there's a way into an admin panel on a
-- fresh database. CHANGE THIS PASSWORD after first login.
--   email:    superadmin@agriventure.local
--   password: SuperAdmin@123
-- (Same bcrypt hash used by agriventure_backedn_xampp's seed row - one
-- password to remember across both backends.)
INSERT INTO "users" (full_name, email, password_hash, role, is_verified)
VALUES (
    'Super Admin',
    'superadmin@agriventure.local',
    '$2y$12$WFZnbN9Dgp2OujS.Bk8GAeEIstniV9AejnqDZvW/ZV04sm1f0BvT6',
    'superadmin',
    true
)
ON CONFLICT (email) DO NOTHING;

-- ---------------------------------------------------------------------------
-- sensors: battery/heartbeat (current-value snapshot)
-- ---------------------------------------------------------------------------
ALTER TABLE "sensors"
    ADD COLUMN IF NOT EXISTS battery_percent SMALLINT NULL,
    ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ NULL;

ALTER TABLE "sensors"
    ADD CONSTRAINT chk_sensors_battery_percent CHECK (battery_percent IS NULL OR (battery_percent BETWEEN 0 AND 100));

-- ---------------------------------------------------------------------------
-- sensorreadings: battery (real historical per-reading value, distinct
-- from sensors.battery_percent's single overwritten "current" value - see
-- routes/sensors.py)
-- ---------------------------------------------------------------------------
ALTER TABLE "sensorreadings"
    ADD COLUMN IF NOT EXISTS battery_percent SMALLINT NULL;

ALTER TABLE "sensorreadings"
    ADD CONSTRAINT chk_sensorreadings_battery_percent CHECK (battery_percent IS NULL OR (battery_percent BETWEEN 0 AND 100));

-- ---------------------------------------------------------------------------
-- recommendations: priority/source/action + acknowledgement + direct
-- farm_id (denormalized so RecommendationEngine-created rows can be
-- filtered/joined by farm directly, without the reading->sensor->farm
-- chain the older crop-analysis rows still rely on)
-- ---------------------------------------------------------------------------
ALTER TABLE "recommendations"
    ADD COLUMN IF NOT EXISTS farm_id INTEGER NULL REFERENCES "farms"(farm_id) ON DELETE CASCADE,
    ADD COLUMN IF NOT EXISTS priority VARCHAR(20) NOT NULL DEFAULT 'info',
    ADD COLUMN IF NOT EXISTS source_reference VARCHAR(255) NULL,
    ADD COLUMN IF NOT EXISTS recommended_action TEXT NULL,
    ADD COLUMN IF NOT EXISTS is_acknowledged BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS acknowledged_at TIMESTAMPTZ NULL;

ALTER TABLE "recommendations"
    ADD CONSTRAINT chk_recommendations_priority CHECK (priority IN ('urgent', 'maintenance', 'info'));

CREATE INDEX IF NOT EXISTS idx_recommendations_farm_id ON "recommendations" (farm_id);
CREATE INDEX IF NOT EXISTS idx_recommendations_is_acknowledged ON "recommendations" (is_acknowledged);

-- ---------------------------------------------------------------------------
-- advice_rules: the rule/threshold "book" RecommendationEngine grounds
-- recommendations against (see utils/recommendation_engine.py). Same
-- internally-authored starter table as agriventure_backedn_xampp's -
-- source_reference is labeled honestly, not as a real external standard.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS "advice_rules" (
    id                   SERIAL PRIMARY KEY,
    crop_type            VARCHAR(100) NULL,  -- NULL = applies to every crop type
    metric               VARCHAR(20) NOT NULL,
    comparator           VARCHAR(2) NOT NULL,
    threshold            DOUBLE PRECISION NOT NULL,
    priority             VARCHAR(20) NOT NULL,
    -- {value} is substituted with the actual reading at evaluation time.
    problem_template     VARCHAR(500) NOT NULL,
    recommended_action   VARCHAR(500) NOT NULL,
    source_reference     VARCHAR(255) NOT NULL,
    is_active            BOOLEAN NOT NULL DEFAULT true,
    CONSTRAINT chk_advice_rules_metric CHECK (metric IN ('soil_moisture', 'temperature', 'humidity')),
    CONSTRAINT chk_advice_rules_comparator CHECK (comparator IN ('lt', 'gt')),
    CONSTRAINT chk_advice_rules_priority CHECK (priority IN ('urgent', 'maintenance', 'info'))
);

CREATE INDEX IF NOT EXISTS idx_advice_rules_crop_type ON "advice_rules" (crop_type);
CREATE INDEX IF NOT EXISTS idx_advice_rules_metric ON "advice_rules" (metric);

INSERT INTO "advice_rules"
    (id, crop_type, metric, comparator, threshold, priority, problem_template, recommended_action, source_reference)
VALUES
    (1, NULL, 'soil_moisture', 'lt', 20, 'urgent',
        'Soil moisture critically low at {value}%.',
        'Activate irrigation as soon as possible and recheck moisture within 2 hours.',
        'AgriVenture Agronomy Reference v1 (internal) - Soil Moisture Thresholds'),
    (2, NULL, 'soil_moisture', 'lt', 35, 'maintenance',
        'Soil moisture trending low at {value}%.',
        'Plan irrigation within the next 24 hours; monitor for further decline.',
        'AgriVenture Agronomy Reference v1 (internal) - Soil Moisture Thresholds'),
    (3, NULL, 'temperature', 'gt', 38, 'urgent',
        'Ambient temperature critically high at {value}°C.',
        'Provide shading/ventilation if available and increase irrigation frequency to offset heat stress.',
        'AgriVenture Agronomy Reference v1 (internal) - Temperature Thresholds'),
    (4, NULL, 'temperature', 'gt', 33, 'maintenance',
        'Ambient temperature elevated at {value}°C.',
        'Monitor crop for heat stress; consider additional watering in the next irrigation cycle.',
        'AgriVenture Agronomy Reference v1 (internal) - Temperature Thresholds'),
    (5, NULL, 'humidity', 'lt', 30, 'maintenance',
        'Relative humidity low at {value}%.',
        'Increase irrigation frequency or use mulching to help retain soil/ambient moisture.',
        'AgriVenture Agronomy Reference v1 (internal) - Humidity Thresholds')
ON CONFLICT (id) DO NOTHING;

-- Keep the SERIAL sequence ahead of the hand-assigned ids above so the
-- next auto-generated id doesn't collide with them.
SELECT setval(pg_get_serial_sequence('"advice_rules"', 'id'), (SELECT MAX(id) FROM "advice_rules"));
