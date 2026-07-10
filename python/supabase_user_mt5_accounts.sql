-- Run once in Supabase Dashboard → SQL Editor (after supabase_dashboard_users.sql)
-- Links web dashboard users to MT5 account_id values.

create table if not exists public.dashboard_user_accounts (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.dashboard_users(id) on delete cascade,
  account_id text not null,
  linked_via text not null check (linked_via in ('manual', 'discovered', 'admin')),
  created_at timestamptz not null default now(),
  unique (account_id)
);

create index if not exists dashboard_user_accounts_user_id_idx
  on public.dashboard_user_accounts (user_id);

alter table public.dashboard_user_accounts enable row level security;

revoke all on table public.dashboard_user_accounts from anon, authenticated;
grant all on table public.dashboard_user_accounts to postgres, service_role;

-- List accounts linked to a dashboard user
create or replace function public.list_user_accounts(p_user_id uuid)
returns json
language plpgsql
security definer
set search_path = public
as $$
begin
  return coalesce(
    (
      select json_agg(
        json_build_object(
          'account_id', account_id,
          'linked_via', linked_via,
          'created_at', created_at
        )
        order by created_at
      )
      from public.dashboard_user_accounts
      where user_id = p_user_id
    ),
    '[]'::json
  );
end;
$$;

-- All claimed MT5 account_ids (for pending discovery)
create or replace function public.list_claimed_account_ids()
returns json
language sql
security definer
set search_path = public
as $$
  select coalesce(json_agg(account_id order by account_id), '[]'::json)
  from public.dashboard_user_accounts;
$$;

-- Who owns this MT5 account_id (null if unclaimed)
create or replace function public.get_account_owner(p_account_id text)
returns json
language plpgsql
security definer
set search_path = public
as $$
declare
  r public.dashboard_user_accounts%rowtype;
begin
  select * into r
  from public.dashboard_user_accounts
  where account_id = trim(p_account_id);
  if not found then
    return null;
  end if;
  return json_build_object(
    'user_id', r.user_id,
    'account_id', r.account_id,
    'linked_via', r.linked_via
  );
end;
$$;

-- Link MT5 account to dashboard user (manual / discovered / admin)
create or replace function public.link_user_account(
  p_user_id uuid,
  p_account_id text,
  p_via text
)
returns json
language plpgsql
security definer
set search_path = public
as $$
declare
  new_id uuid;
  aid text := trim(p_account_id);
begin
  if aid is null or aid !~ '^[0-9]{5,12}$' then
    raise exception 'INVALID_ACCOUNT_ID';
  end if;
  if p_via not in ('manual', 'discovered', 'admin') then
    raise exception 'INVALID_VIA';
  end if;

  insert into public.dashboard_user_accounts (user_id, account_id, linked_via)
  values (p_user_id, aid, p_via)
  returning id into new_id;

  return json_build_object(
    'id', new_id,
    'user_id', p_user_id,
    'account_id', aid,
    'linked_via', p_via
  );
exception
  when unique_violation then
    raise exception 'ACCOUNT_ALREADY_LINKED';
end;
$$;

-- Unlink MT5 account from dashboard user
create or replace function public.unlink_user_account(
  p_user_id uuid,
  p_account_id text
)
returns boolean
language plpgsql
security definer
set search_path = public
as $$
declare
  deleted int;
begin
  delete from public.dashboard_user_accounts
  where user_id = p_user_id and account_id = trim(p_account_id);
  get diagnostics deleted = row_count;
  return deleted > 0;
end;
$$;

revoke all on function public.list_user_accounts(uuid) from public;
revoke all on function public.list_claimed_account_ids() from public;
revoke all on function public.get_account_owner(text) from public;
revoke all on function public.link_user_account(uuid, text, text) from public;
revoke all on function public.unlink_user_account(uuid, text) from public;

grant execute on function public.list_user_accounts(uuid) to anon, authenticated, service_role;
grant execute on function public.list_claimed_account_ids() to anon, authenticated, service_role;
grant execute on function public.get_account_owner(text) to anon, authenticated, service_role;
grant execute on function public.link_user_account(uuid, text, text) to anon, authenticated, service_role;
grant execute on function public.unlink_user_account(uuid, text) to anon, authenticated, service_role;

-- Admin example (C): assign account to user by username
-- select public.link_user_account(
--   (select id from public.dashboard_users where username = 'ten_user'),
--   '12345678',
--   'admin'
-- );
