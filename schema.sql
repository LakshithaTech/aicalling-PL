-- Run with a DBA/migration account before enabling outbound calls.
CREATE TABLE IF NOT EXISTS public.ai_agent_do_not_call (
    phone_digits TEXT PRIMARY KEY,
    opted_out_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
-- Your existing table public.ai_agent_calls is reused, not re-created.
