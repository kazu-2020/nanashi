create table if not exists app_application (
  id text primary key,
  name text not null,
  created_at timestamptz not null default now()
);
create table if not exists app_member (
  app_id text not null references app_application (id) on delete cascade,
  user_name text not null,
  role integer not null,
  primary key (app_id, user_name)
);
create table if not exists app_list (
  app_id text not null,
  name text not null,
  kind integer not null,
  primary key (app_id, name)
);
create table if not exists app_property (
  app_id text not null,
  list text not null,
  name text not null,
  type integer not null,
  text_values jsonb not null default '{}',
  ord bigserial,
  primary key (app_id, list, name)
);
create table if not exists app_item (
  app_id text not null,
  id text not null,
  type integer not null,
  def jsonb not null,
  ord bigserial,
  primary key (app_id, id)
);
create table if not exists app_comment (
  id bigserial primary key,
  app_id text not null,
  metric text not null,
  cell jsonb not null,
  user_name text not null,
  body text not null,
  created_at timestamptz not null default now()
);
-- ponytail: a database from before the column rename has "target". Delete this block when no such database is left.
do $$ begin
  alter table app_comment rename column target to metric;
exception when undefined_column then null;
end $$;
create table if not exists app_audit (
  id bigserial primary key,
  app_id text not null,
  user_name text not null,
  action text not null,
  detail text not null,
  created_at timestamptz not null default now()
);
create index if not exists app_audit_app on app_audit (app_id, id);
create table if not exists app_snapshot (
  id text primary key,
  app_id text not null,
  name text not null,
  user_name text not null,
  content text not null, -- text, not jsonb: jsonb does not keep the key order of the engine definitions
  created_at timestamptz not null default now()
);
create table if not exists app_access_rule (
  app_id text not null,
  id text not null,
  role integer not null,
  list text not null,
  members jsonb not null,
  write boolean not null,
  primary key (app_id, id)
);
-- app_rename gives names (a JSON array of strings) with old replaced by new, in the same order.
-- If new is null, it removes old. An empty result is '[]'.
create or replace function app_rename(names jsonb, old text, new text) returns jsonb language sql immutable as $$
  select coalesce(jsonb_agg(case when n = to_jsonb(old) then to_jsonb(new) else n end order by i), '[]')
  from jsonb_array_elements(names) with ordinality as e(n, i)
  where n <> to_jsonb(old) or new is not null
$$;
