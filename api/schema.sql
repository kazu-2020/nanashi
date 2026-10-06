-- ponytail: a database from before the uuid ids has text ids. The system is not in production, so drop the old
-- tables. Delete this block when no such database is left.
do $$ begin
  if exists (select 1 from information_schema.columns
             where table_name = 'app_application' and column_name = 'id' and data_type = 'text') then
    drop table if exists app_application, app_member, app_list, app_property, app_item, app_comment, app_audit,
      app_snapshot, app_access_rule cascade;
  end if;
end $$;
drop function if exists app_rename(jsonb, text, text);
-- ponytail: issue #50 removes the access rights tables. Delete this line when no database has them.
drop table if exists app_member, app_access_rule;

create table if not exists app_application (
  id uuid primary key,
  name text not null,
  -- A transaction that writes a row of a snapshot (app_list, app_property, app_item, app_metric) adds 1. CreateSnapshot compares it.
  version bigint not null default 0,
  created_at timestamptz not null default now()
);
create table if not exists app_list (
  app_id uuid not null references app_application (id) on delete cascade,
  id uuid not null,
  kind integer not null,
  -- The number of the next new row of a TRANSACTION list. Import reserves a range with one update (row lock), so
  -- concurrent imports get different names. The largest number in the model is a floor for a restored list.
  next_row bigint not null default 1,
  primary key (app_id, id)
);
-- app_property has a row for each property. The engine keeps the values of a DIMENSION property. The api keeps the
-- values of a TEXT property. The Metric metric_id keeps the values of a NUMBER or BOOLEAN property.
create table if not exists app_property (
  app_id uuid not null references app_application (id) on delete cascade,
  list_id uuid not null,
  id uuid not null,
  name text not null,
  type integer not null,
  metric_id uuid,
  text_values jsonb not null default '{}',
  ord bigserial,
  primary key (app_id, list_id, id)
);
-- The drop also drops the old partial index app_property_name, so the next statement makes it again.
alter table app_property drop column if exists deleted;
create unique index if not exists app_property_name on app_property (app_id, list_id, name);
create table if not exists app_item (
  app_id uuid not null references app_application (id) on delete cascade,
  id uuid not null,
  type integer not null,
  def jsonb not null,
  ord bigserial,
  primary key (app_id, id)
);
-- app_metric is the Metric catalog: the attributes of a Metric that only the api keeps. The engine keeps the name.
-- metric_id is the Metric UUID. A row for a Metric that the model does not have stays, and the readers skip it.
create table if not exists app_metric (
  app_id uuid not null references app_application (id) on delete cascade,
  metric_id uuid not null,
  description text not null default '',
  folder text not null default '',
  owner text not null,
  primary key (app_id, metric_id)
);
create table if not exists app_comment (
  app_id uuid not null references app_application (id) on delete cascade,
  id uuid not null,
  metric uuid not null,
  cell jsonb not null,
  user_name text not null,
  body text not null,
  created_at timestamptz not null default now(),
  primary key (app_id, id)
);
create table if not exists app_audit (
  id bigserial primary key,
  app_id uuid not null,
  user_name text not null,
  action text not null,
  detail text not null,
  created_at timestamptz not null default now()
);
create index if not exists app_audit_app on app_audit (app_id, id);
-- client_op_id is set only on the rows that the interceptor writes for WriteCells. A resend of the same
-- WriteCells adds no row.
alter table app_audit add column if not exists client_op_id uuid;
create unique index if not exists app_audit_op on app_audit (app_id, client_op_id);
create table if not exists app_snapshot (
  id uuid primary key,
  app_id uuid not null references app_application (id) on delete cascade,
  name text not null,
  user_name text not null,
  content text not null, -- text, not jsonb: jsonb does not keep the key order of the engine definitions
  created_at timestamptz not null default now()
);
-- app_operation is the outbox: one row for each client_op_id. A request that the engine must apply is pending
-- until the api knows the engine result. A done row keeps the result. A failed row keeps the error.
-- It has no foreign key: a failed CreateApplication deletes its application and keeps its row.
create table if not exists app_operation (
  app_id uuid not null,
  client_op_id uuid not null,
  user_name text not null,
  method text not null,
  request_hash text not null,
  ops jsonb,
  seq bigint,
  -- The rows that the first transaction made or changed (madeRow in server.go). A refusal undoes them.
  made jsonb,
  -- The request as JSON, for the audit row that the flip to done writes.
  detail text,
  status text not null,
  result jsonb,
  error jsonb,
  created_at timestamptz not null default now(),
  primary key (app_id, client_op_id)
);
alter table app_operation add column if not exists detail text;
alter table app_operation drop column if exists expect;
