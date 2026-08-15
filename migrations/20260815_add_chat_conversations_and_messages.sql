-- Approved additive conversation-history migration.
-- This migration does not alter, migrate, or delete legacy chat_memory.

create table if not exists public.chat_conversations (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  title text not null default 'New conversation' check (char_length(title) between 1 and 160),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists chat_conversations_user_updated_idx
  on public.chat_conversations (user_id, updated_at desc);

create table if not exists public.chat_messages (
  id uuid primary key default gen_random_uuid(),
  conversation_id uuid not null references public.chat_conversations(id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade,
  role text not null check (role in ('user', 'assistant')),
  content text not null check (char_length(content) between 1 and 50000),
  created_at timestamptz not null default now()
);

create index if not exists chat_messages_conversation_created_idx
  on public.chat_messages (conversation_id, created_at asc);

create index if not exists chat_messages_user_created_idx
  on public.chat_messages (user_id, created_at desc);

-- Backend-only access model: these new tables have RLS enabled and no direct
-- client policies.  The existing Flask backend continues to use the server-side
-- service-role path; anon and authenticated API roles cannot access either table.
alter table public.chat_conversations enable row level security;
alter table public.chat_messages enable row level security;

revoke all on table public.chat_conversations from anon, authenticated;
revoke all on table public.chat_messages from anon, authenticated;

-- Do not alter legacy chat_memory or its RLS configuration in this migration.
