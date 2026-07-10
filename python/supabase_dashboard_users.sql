-- Run once in Supabase Dashboard → SQL Editor → New query → Run
-- Creates dashboard login/register storage without Auth email rate limits.

create table if not exists public.dashboard_users (
  id uuid primary key default gen_random_uuid(),
  username text not null unique,
  password_hash text not null,
  salt text not null,
  created_at timestamptz not null default now()
);

alter table public.dashboard_users enable row level security;

-- No direct table access via REST; only SECURITY DEFINER RPCs below.
revoke all on table public.dashboard_users from anon, authenticated;
grant all on table public.dashboard_users to postgres, service_role;

create or replace function public.register_dashboard_user(
  p_username text,
  p_password_hash text,
  p_salt text
)
returns json
language plpgsql
security definer
set search_path = public
as $$
declare
  new_id uuid;
begin
  if p_username is null or length(trim(p_username)) < 3 then
    raise exception 'INVALID_USERNAME';
  end if;
  insert into public.dashboard_users (username, password_hash, salt)
  values (trim(p_username), p_password_hash, p_salt)
  returning id into new_id;
  return json_build_object('id', new_id, 'username', trim(p_username));
exception
  when unique_violation then
    raise exception 'USERNAME_TAKEN';
end;
$$;

create or replace function public.get_dashboard_user_auth(p_username text)
returns json
language plpgsql
security definer
set search_path = public
as $$
declare
  r public.dashboard_users%rowtype;
begin
  select * into r
  from public.dashboard_users
  where username = trim(p_username);
  if not found then
    return null;
  end if;
  return json_build_object(
    'id', r.id,
    'username', r.username,
    'password_hash', r.password_hash,
    'salt', r.salt
  );
end;
$$;

revoke all on function public.register_dashboard_user(text, text, text) from public;
revoke all on function public.get_dashboard_user_auth(text) from public;
grant execute on function public.register_dashboard_user(text, text, text) to anon, authenticated, service_role;
grant execute on function public.get_dashboard_user_auth(text) to anon, authenticated, service_role;
