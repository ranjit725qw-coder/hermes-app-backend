-- Durable Android companion enrollment only. This migration is intentionally
-- local and unapplied until a separate production approval is granted.
-- It does not modify auth, chat, chat_memory, conversations, messages, or RLS.

create table if not exists public.android_companion_devices (
  device_id uuid primary key,
  owner_id text not null,
  label text not null check (char_length(label) between 1 and 64),
  public_key_der_b64 text not null,
  registered_at bigint not null,
  revoked_at bigint null,
  last_sequence bigint not null default 0 check (last_sequence >= 0)
);

create index if not exists android_companion_devices_owner_active_idx
  on public.android_companion_devices (owner_id, registered_at desc)
  where revoked_at is null;

alter table public.android_companion_devices enable row level security;
revoke all on table public.android_companion_devices from anon, authenticated;
grant select, insert, update on table public.android_companion_devices to service_role;

-- No client policy is created. Only the existing backend service-role path
-- may access this isolated public-key registry.
