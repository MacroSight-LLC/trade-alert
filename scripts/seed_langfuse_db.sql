-- Seed Langfuse with org + project + API key for trade-alert pipeline
INSERT INTO organizations (id, name)
VALUES ('org-trade-alert', 'trade-alert')
ON CONFLICT (id) DO NOTHING;

INSERT INTO projects (id, name, org_id)
VALUES ('proj-trade-alert', 'trade-alert', 'org-trade-alert')
ON CONFLICT (id) DO NOTHING;

INSERT INTO api_keys (id, public_key, hashed_secret_key, fast_hashed_secret_key, display_secret_key, project_id, note)
VALUES (
  'key-trade-alert',
  'pk-lf-7b3b3b6e-7aba-4008-9fde-8dfcb376f934',
  encode(sha256('sk-lf-f0a5452d-7e91-4d33-bf11-2b5a0efb8cb9'::bytea), 'hex'),
  encode(sha256(('sk-lf-f0a5452d-7e91-4d33-bf11-2b5a0efb8cb9'::text)::bytea), 'hex'),
  'sk-lf-...8cb9',
  'proj-trade-alert',
  'Pipeline API key (auto-seeded)'
)
ON CONFLICT (id) DO NOTHING;

SELECT 'Langfuse seeded OK' AS status;
