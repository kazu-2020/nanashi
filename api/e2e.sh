#!/usr/bin/env bash
# End-to-end check of nanashi-api with real engines.
# It needs PostgreSQL (NANASHI_PG_DSN) with the engine tables, and nanashi-router (ROUTER) with --tessera.
# The router starts the engines. The script starts nanashi-api, and stops it at the end.
# Each reference is an id (docs/ids.md). The script makes the ids of the objects it creates, and reads the ids of
# the objects that the api makes (the calendar, the scenario list) from GetModel.
set -euo pipefail
cd "$(dirname "$0")"

DSN=${NANASHI_PG_DSN:-postgresql://postgres@127.0.0.1:55432/nanashi}
ROUTER=${ROUTER:-http://127.0.0.1:8090}
LISTEN=${LISTEN:-127.0.0.1:8080}
API=http://$LISTEN/nanashi.v1.PlanService
WORK=$(mktemp -d)

curl -sf "$ROUTER/healthz" >/dev/null || { echo "nanashi-router does not run at $ROUTER"; exit 1; }
go build -o "$WORK/nanashi-api" ./cmd/nanashi-api
"$WORK/nanashi-api" --pg "$DSN" --listen "$LISTEN" --router "$ROUTER" >"$WORK/api.log" 2>&1 &
API_PID=$!
trap 'kill $API_PID 2>/dev/null; wait $API_PID 2>/dev/null; echo "log: $WORK/api.log"' EXIT

uuid() { uuidgen 2>/dev/null | tr 'A-F' 'a-f' || cat /proc/sys/kernel/random/uuid; }
call() { # user method body: a request that changes data gets a new clientOpId
  local body=$3
  case $2 in
    Create* | Update* | Add* | Edit* | Rename* | Delete* | Write* | Import) body=$(jq -c --arg op "$(uuid)" '. + {clientOpId: $op}' <<<"$3") ;;
  esac
  curl -sS -X POST "$API/$2" -H 'Content-Type: application/json' -H "X-Nanashi-User: $1" -d "$body"
}
ok() { # user method body: the call must succeed
  local out
  out=$(call "$@")
  if jq -e 'has("code")' <<<"$out" >/dev/null; then echo "FAIL $2: $out" >&2; exit 1; fi
  echo "$out"
}
waitall() { # waits each background call; one failed call fails the script
  local pid
  for pid in "${PIDS[@]}"; do wait "$pid" || { echo "FAIL a background call"; exit 1; }; done
  PIDS=()
}
check() { # description jq-expression json
  if jq -e "$2" <<<"$3" >/dev/null; then echo "ok   $1"; else echo "FAIL $1: $3"; exit 1; fi
}
cell() { # metric-id coords-json: the jq filter for the value of one cell
  echo "(.cells[] | select(.metric == \"$1\" and .coords == $2) | .value)"
}
# The lookups read the ids of the objects by name from the model of an application.
model() { ok alice GetModel "{\"appId\": \"$1\"}"; }
list() { jq -r --arg n "$2" '.lists[] | select(.name == $n) | .id' <<<"$1"; }           # model name
member() { jq -r --arg l "$2" --arg n "$3" '.lists[] | select(.name == $l) | .members[] | select(.name == $n) | .id' <<<"$1"; } # model list name
prop() { jq -r --arg l "$2" --arg n "$3" '.lists[] | select(.name == $l) | .properties[] | select(.name == $n) | .id' <<<"$1"; }
metric() { jq -r --arg n "$2" '.metrics[] | select(.name == $n) | .id' <<<"$1"; }
members() { jq -c --arg l "$2" '.lists[] | select(.name == $l) | .members | map({(.name): .id}) | add' <<<"$1"; } # name -> id

for _ in $(seq 100); do call alice ListApplications '{}' >/dev/null 2>&1 && break; sleep 0.1; done

APP=$(ok alice CreateApplication "{\"id\": \"$(uuid)\", \"name\": \"E2E\"}" | jq -r .id)
check "create application" ".id == \"$APP\"" "$(ok alice ListApplications '{}' | jq ".applications[] | select(.id == \"$APP\")")"
check "a second create of the id is refused" '.code == "already_exists"' "$(call alice CreateApplication "{\"id\": \"$APP\", \"name\": \"Again\"}")"
check "an id that is not a UUID is refused" '.code == "invalid_argument"' "$(call alice GetModel '{"appId": "plan"}')"

ok alice CreateCalendar "{\"appId\": \"$APP\", \"startYear\": 2026, \"years\": 1}" >/dev/null
CATEGORY=$(uuid) HARD=$(uuid) SOFT=$(uuid) PRODUCT=$(uuid) A=$(uuid) B=$(uuid) C=$(uuid) SALES=$(uuid)
ok alice CreateList "{\"appId\": \"$APP\", \"id\": \"$CATEGORY\", \"name\": \"Category\", \"kind\": \"LIST_KIND_DIMENSION\", \"members\": [{\"id\": \"$HARD\", \"name\": \"Hard\"}, {\"id\": \"$SOFT\", \"name\": \"Soft\"}]}" >/dev/null
ok alice CreateList "{\"appId\": \"$APP\", \"id\": \"$PRODUCT\", \"name\": \"Product\", \"kind\": \"LIST_KIND_DIMENSION\", \"members\": [{\"id\": \"$A\", \"name\": \"A\"}, {\"id\": \"$B\", \"name\": \"B\"}, {\"id\": \"$C\", \"name\": \"C\"}]}" >/dev/null
check "create of an existing list id is refused" '.code == "already_exists"' \
  "$(call alice CreateList "{\"appId\": \"$APP\", \"id\": \"$PRODUCT\", \"name\": \"Product2\", \"kind\": \"LIST_KIND_DIMENSION\"}")"
PCAT=$(uuid) NOTE=$(uuid)
ok alice AddProperty "{\"appId\": \"$APP\", \"list\": \"$PRODUCT\", \"property\": {\"id\": \"$PCAT\", \"name\": \"Category\", \"type\": \"PROPERTY_TYPE_DIMENSION\", \"target\": \"$CATEGORY\"}}" >/dev/null
ok alice AddProperty "{\"appId\": \"$APP\", \"list\": \"$PRODUCT\", \"property\": {\"id\": \"$NOTE\", \"name\": \"Note\", \"type\": \"PROPERTY_TYPE_TEXT\"}}" >/dev/null
ok alice EditMembers "{\"appId\": \"$APP\", \"list\": \"$PRODUCT\", \"edits\": [
  {\"set\": {\"id\": \"$A\", \"properties\": {\"$PCAT\": \"$HARD\", \"$NOTE\": \"first\"}}},
  {\"set\": {\"id\": \"$B\", \"properties\": {\"$PCAT\": \"$HARD\"}}},
  {\"set\": {\"id\": \"$C\", \"properties\": {\"$PCAT\": \"$SOFT\"}}}]}" >/dev/null
check "a property name is used once in a list" '.code == "already_exists"' \
  "$(call alice AddProperty "{\"appId\": \"$APP\", \"list\": \"$PRODUCT\", \"property\": {\"id\": \"$(uuid)\", \"name\": \"Note\", \"type\": \"PROPERTY_TYPE_DIMENSION\", \"target\": \"$CATEGORY\"}}")"

ok alice CreateList "{\"appId\": \"$APP\", \"id\": \"$SALES\", \"name\": \"Sales\", \"kind\": \"LIST_KIND_TRANSACTION\"}" >/dev/null
SPROD=$(uuid) AMOUNT=$(uuid)
ok alice AddProperty "{\"appId\": \"$APP\", \"list\": \"$SALES\", \"property\": {\"id\": \"$SPROD\", \"name\": \"Product\", \"type\": \"PROPERTY_TYPE_DIMENSION\", \"target\": \"$PRODUCT\"}}" >/dev/null
ok alice AddProperty "{\"appId\": \"$APP\", \"list\": \"$SALES\", \"property\": {\"id\": \"$AMOUNT\", \"name\": \"Amount\", \"type\": \"PROPERTY_TYPE_NUMBER\"}}" >/dev/null
CSV=$'product,amount\nA,100\nB,50\nA,25\nC,10\n'
IMPORT=$(ok alice Import "$(jq -n --arg app "$APP" --arg csv "$CSV" --arg sales "$SALES" --arg sprod "$SPROD" --arg amount "$AMOUNT" \
  '{appId: $app, csv: $csv, list: {list: $sales, propertyColumns: {($sprod): "product", ($amount): "amount"}}}')")
check "import 4 transactions" '.rows == 4' "$IMPORT"

BASE=$(uuid)
ok alice CreateScenario "{\"appId\": \"$APP\", \"id\": \"$BASE\", \"name\": \"Base\"}" >/dev/null
MODEL=$(model "$APP")
SCENARIO=$(list "$MODEL" Scenario) MONTH=$(list "$MODEL" Month) JAN=$(member "$MODEL" Month 2026-01)
BUDGET=$(uuid) REVENUE=$(uuid)
ok alice CreateMetric "{\"appId\": \"$APP\", \"metric\": {\"id\": \"$BUDGET\", \"name\": \"Budget\", \"dimensions\": [\"$PRODUCT\", \"$SCENARIO\", \"$MONTH\"],
  \"description\": \"Sales budget\", \"folder\": \"Plan\", \"owner\": \"mallory\"}}" >/dev/null
ok alice CreateMetric "{\"appId\": \"$APP\", \"metric\": {\"id\": \"$REVENUE\", \"name\": \"Revenue\", \"dimensions\": [\"$PRODUCT\"], \"formula\": \"'Sales.Amount'[BY SUM: Sales.Product]\"}}" >/dev/null
ok alice WriteCells "{\"appId\": \"$APP\", \"writes\": [
  {\"metric\": \"$BUDGET\", \"coords\": {\"$PRODUCT\": \"$A\", \"$SCENARIO\": \"$BASE\"}, \"value\": {\"number\": 1200}},
  {\"metric\": \"$BUDGET\", \"coords\": {\"$PRODUCT\": \"$B\", \"$SCENARIO\": \"$BASE\", \"$MONTH\": \"$JAN\"}, \"value\": {\"number\": 7}}]}" >/dev/null

MODEL=$(model "$APP")
check "model lists" '[.lists[].name] == ["Year", "Quarter", "Month", "Category", "Product", "Sales", "Scenario"]' "$MODEL"
check "list kinds" '[.lists[] | .kind] == ["LIST_KIND_CALENDAR", "LIST_KIND_CALENDAR", "LIST_KIND_CALENDAR", "LIST_KIND_DIMENSION", "LIST_KIND_DIMENSION", "LIST_KIND_TRANSACTION", "LIST_KIND_SCENARIO"]' "$MODEL"
check "the ids of the lists come back" ".lists[] | select(.name == \"Product\") | .id == \"$PRODUCT\" and .members[0].id == \"$A\"" "$MODEL"
check "member properties" ".lists[] | select(.name == \"Product\") | .members[0].properties == {\"$PCAT\": \"$HARD\", \"$NOTE\": \"first\"}" "$MODEL"
check "transaction rows" ".lists[] | select(.name == \"Sales\") | [.members[] | .properties[\"$AMOUNT\"]] == [\"100\", \"50\", \"25\", \"10\"]" "$MODEL"
check "the catalog gives the description, the folder and the owner" \
  ".metrics[] | select(.id == \"$BUDGET\") | .description == \"Sales budget\" and .folder == \"Plan\" and .owner == \"alice\"" "$MODEL"
ok alice UpdateMetric "{\"appId\": \"$APP\", \"metric\": {\"id\": \"$BUDGET\", \"name\": \"Budget\", \"dimensions\": [\"$PRODUCT\", \"$SCENARIO\", \"$MONTH\"],
  \"description\": \"Sales budget\", \"folder\": \"Plan 2026\"}}" >/dev/null
check "an update changes the folder and keeps the input values" \
  ".metrics[] | select(.id == \"$BUDGET\") | .folder == \"Plan 2026\" and .owner == \"alice\"" "$(model "$APP")"
ok alice UpdateMetric "{\"appId\": \"$APP\", \"metric\": {\"id\": \"$BUDGET\", \"name\": \"Budget\", \"dimensions\": [\"$PRODUCT\", \"$SCENARIO\", \"$MONTH\"],
  \"description\": \"Sales budget\", \"folder\": \"Plan\"}}" >/dev/null
# The engine refuses the formula. The refused create must leave no catalog row, so a second create with the id works.
BROKEN=$(uuid)
check "the engine refuses a bad formula" '.code == "invalid_argument"' \
  "$(call alice CreateMetric "{\"appId\": \"$APP\", \"metric\": {\"id\": \"$BROKEN\", \"name\": \"Broken\", \"formula\": \"Budget +\", \"description\": \"x\"}}")"
ok alice CreateMetric "{\"appId\": \"$APP\", \"metric\": {\"id\": \"$BROKEN\", \"name\": \"Broken\", \"description\": \"y\"}}" >/dev/null
check "a refused create leaves no catalog row" ".metrics[] | select(.id == \"$BROKEN\") | .description == \"y\"" "$(model "$APP")"
ok alice DeleteMetric "{\"appId\": \"$APP\", \"id\": \"$BROKEN\"}" >/dev/null
check "property Metric is not a Metric" '[.metrics[].name] == ["Budget", "Revenue"]' "$MODEL"
# The Metric of a property has an id that the api made. Find it through the engine: the api refuses its change.
AMOUNT_METRIC=$(curl -sS "$ROUTER/models/$APP/" | jq -r '.metrics | to_entries[] | select(.value.name == "Sales.Amount") | .key')
for req in "UpdateMetric {\"appId\": \"$APP\", \"metric\": {\"id\": \"$AMOUNT_METRIC\", \"name\": \"Sales.Amount\", \"dimensions\": [\"$SALES\"], \"kind\": \"VALUE_KIND_BOOLEAN\"}}" \
  "RenameMetric {\"appId\": \"$APP\", \"id\": \"$AMOUNT_METRIC\", \"name\": \"X\"}" \
  "DeleteMetric {\"appId\": \"$APP\", \"id\": \"$AMOUNT_METRIC\"}"; do
  check "${req%% *} refuses a property Metric" '.code == "failed_precondition"' "$(call alice ${req%% *} "${req#* }")"
done
check "a Metric does not take the name of a property Metric" '.code == "already_exists" or .code == "invalid_argument"' \
  "$(call alice CreateMetric "{\"appId\": \"$APP\", \"metric\": {\"id\": \"$(uuid)\", \"name\": \"Sales.Amount\", \"dimensions\": [\"$SALES\"]}}")"
COST=$(uuid)
ok alice CreateMetric "{\"appId\": \"$APP\", \"metric\": {\"id\": \"$COST\", \"name\": \"Product.Cost\", \"dimensions\": [\"$PRODUCT\"]}}" >/dev/null
check "a property does not replace a Metric with its name" '.code == "already_exists" or .code == "invalid_argument"' \
  "$(call alice AddProperty "{\"appId\": \"$APP\", \"list\": \"$PRODUCT\", \"property\": {\"id\": \"$(uuid)\", \"name\": \"Cost\", \"type\": \"PROPERTY_TYPE_NUMBER\"}}")"
check "the refused property left no row" ".lists[] | select(.name == \"Product\") | [.properties[].name] == [\"Category\", \"Note\"]" "$(model "$APP")"
ok alice DeleteMetric "{\"appId\": \"$APP\", \"id\": \"$COST\"}" >/dev/null

Q=$(ok alice Query "{\"appId\": \"$APP\", \"metrics\": [\"$REVENUE\"], \"rows\": [\"$PRODUCT\"]}")
check "BY SUM of transactions" "[$(cell "$REVENUE" "[\"$A\"]").number, $(cell "$REVENUE" "[\"$C\"]").number] == [125, 10]" "$Q"
# A rename changes only the name. The formula shows the new names, and the values do not change.
ok alice RenameList "{\"appId\": \"$APP\", \"id\": \"$SALES\", \"name\": \"Orders\"}" >/dev/null
ok alice RenameProperty "{\"appId\": \"$APP\", \"list\": \"$SALES\", \"id\": \"$SPROD\", \"name\": \"Item\"}" >/dev/null
ok alice RenameProperty "{\"appId\": \"$APP\", \"list\": \"$SALES\", \"id\": \"$AMOUNT\", \"name\": \"Total\"}" >/dev/null
check "a formula shows the new list and property names" \
  ".metrics[] | select(.id == \"$REVENUE\") | .formula == \"'Orders.Total'[BY SUM: Orders.Item]\"" "$(model "$APP")"
check "the renamed list and properties keep their values" \
  "[$(cell "$REVENUE" "[\"$A\"]").number, $(cell "$REVENUE" "[\"$C\"]").number] == [125, 10]" \
  "$(ok alice Query "{\"appId\": \"$APP\", \"metrics\": [\"$REVENUE\"], \"rows\": [\"$PRODUCT\"]}")"
check "a list name is used once" '.code == "already_exists" or .code == "invalid_argument"' \
  "$(call alice RenameList "{\"appId\": \"$APP\", \"id\": \"$SALES\", \"name\": \"Product\"}")"
ok alice RenameList "{\"appId\": \"$APP\", \"id\": \"$SALES\", \"name\": \"Sales\"}" >/dev/null
ok alice RenameProperty "{\"appId\": \"$APP\", \"list\": \"$SALES\", \"id\": \"$SPROD\", \"name\": \"Product\"}" >/dev/null
ok alice RenameProperty "{\"appId\": \"$APP\", \"list\": \"$SALES\", \"id\": \"$AMOUNT\", \"name\": \"Amount\"}" >/dev/null
FEB=$(member "$MODEL" Month 2026-02) MAR=$(member "$MODEL" Month 2026-03)
Q=$(ok alice Query "{\"appId\": \"$APP\", \"metrics\": [\"$BUDGET\"], \"rows\": [\"$PRODUCT\"], \"columns\": [\"$SCENARIO\"],
  \"filters\": {\"$MONTH\": {\"ids\": [\"$JAN\", \"$FEB\", \"$MAR\"]}}}")
check "spread over 12 months, Q1 total" "[$(cell "$BUDGET" "[\"$A\", \"$BASE\"]").number, $(cell "$BUDGET" "[\"$B\", \"$BASE\"]").number] == [300, 7]" "$Q"

PLAN=$(uuid)
ok alice CreateScenario "{\"appId\": \"$APP\", \"id\": \"$PLAN\", \"name\": \"Plan\", \"copyFrom\": \"$BASE\"}" >/dev/null
Q=$(ok alice Query "{\"appId\": \"$APP\", \"metrics\": [\"$BUDGET\"], \"columns\": [\"$SCENARIO\"], \"aggregation\": \"AGGREGATION_SUM\"}")
check "scenario copy" "[$(cell "$BUDGET" "[\"$BASE\"]").number, $(cell "$BUDGET" "[\"$PLAN\"]").number] == [1207, 1207]" "$Q"

TABLE=$(uuid) VIEW=$(uuid)
ok alice CreateTable "{\"appId\": \"$APP\", \"table\": {\"id\": \"$TABLE\", \"name\": \"Sales table\", \"metrics\": [\"$BUDGET\", \"$REVENUE\"]}}" >/dev/null
check "create of an existing id is refused" '.code == "already_exists"' \
  "$(call alice CreateTable "{\"appId\": \"$APP\", \"table\": {\"id\": \"$TABLE\", \"name\": \"Again\"}}")"
check "update of a missing id is refused" '.code == "not_found"' \
  "$(call alice UpdateTable "{\"appId\": \"$APP\", \"table\": {\"id\": \"$(uuid)\", \"name\": \"Missing\"}}")"
ok alice CreateView "{\"appId\": \"$APP\", \"view\": {\"id\": \"$VIEW\", \"name\": \"Budget by product\", \"metrics\": [\"$BUDGET\"], \"rows\": [\"$PRODUCT\"],
  \"columns\": [\"$SCENARIO\"], \"display\": \"DISPLAY_BAR\"}}" >/dev/null
ok alice CreateBoard "{\"appId\": \"$APP\", \"board\": {\"id\": \"$(uuid)\", \"name\": \"Overview\", \"widgets\": [{\"viewId\": \"$VIEW\"}, {\"text\": \"Notes\"}],
  \"pageSelectors\": [\"$SCENARIO\"]}}" >/dev/null
MODEL=$(model "$APP")
check "table, view and board" "(.tables[0].id == \"$TABLE\") and (.views[0].display == \"DISPLAY_BAR\") and (.boards[0].widgets[0].viewId == \"$VIEW\")" "$MODEL"

ok alice AddComment "{\"appId\": \"$APP\", \"comment\": {\"id\": \"$(uuid)\", \"metric\": \"$BUDGET\", \"cell\": {\"$PRODUCT\": \"$A\", \"$SCENARIO\": \"$BASE\", \"$MONTH\": \"$JAN\"}, \"body\": \"check this\"}}" >/dev/null
check "a comment needs a member for each dimension" '.code == "invalid_argument"' \
  "$(call alice AddComment "{\"appId\": \"$APP\", \"comment\": {\"id\": \"$(uuid)\", \"metric\": \"$BUDGET\", \"cell\": {\"$PRODUCT\": \"$A\"}, \"body\": \"total\"}}")"
check "comment" ".comments[0].user == \"alice\" and .comments[0].cell[\"$PRODUCT\"] == \"$A\"" \
  "$(ok alice ListComments "{\"appId\": \"$APP\", \"metric\": \"$BUDGET\"}")"

TARGET=$(uuid)
ok alice CreateMetric "{\"appId\": \"$APP\", \"metric\": {\"id\": \"$TARGET\", \"name\": \"Target\", \"dimensions\": [\"$PRODUCT\"], \"formula\": \"Revenue * 2\", \"overridable\": true}}" >/dev/null
ok alice WriteCells "{\"appId\": \"$APP\", \"writes\": [{\"metric\": \"$TARGET\", \"coords\": {\"$PRODUCT\": \"$A\"}, \"value\": {\"number\": 999}}]}" >/dev/null
SNAP=$(ok alice CreateSnapshot "{\"appId\": \"$APP\", \"id\": \"$(uuid)\", \"name\": \"before review\"}" | jq -r .id)
check "snapshot list" ".snapshots[0].id == \"$SNAP\"" "$(ok alice ListSnapshots "{\"appId\": \"$APP\"}")"
APP2=$(ok alice CreateApplication "{\"id\": \"$(uuid)\", \"name\": \"E2E restored\", \"snapshotId\": \"$SNAP\"}" | jq -r .id)
# The restore keeps the ids, so the same ids work in the new application.
Q=$(ok alice Query "{\"appId\": \"$APP2\", \"metrics\": [\"$REVENUE\", \"$BUDGET\"], \"rows\": [\"$PRODUCT\"], \"columns\": [\"$SCENARIO\"]}")
check "restored data" "[$(cell "$REVENUE" "[\"$A\", \"\"]").number, $(cell "$BUDGET" "[\"$A\", \"$PLAN\"]").number] == [125, 1200]" "$Q"
Q=$(ok alice Query "{\"appId\": \"$APP2\", \"metrics\": [\"$TARGET\"], \"rows\": [\"$PRODUCT\"]}")
check "restore keeps an override value" "[$(cell "$TARGET" "[\"$A\"]").number, $(cell "$TARGET" "[\"$C\"]").number] == [999, 20]" "$Q"
ok alice AddComment "{\"appId\": \"$APP2\", \"comment\": {\"id\": \"$(uuid)\", \"metric\": \"$BUDGET\", \"cell\": {\"$PRODUCT\": \"$C\", \"$SCENARIO\": \"$BASE\", \"$MONTH\": \"$JAN\"}, \"body\": \"on C\"}}" >/dev/null
ok alice RenameMetric "{\"appId\": \"$APP2\", \"id\": \"$BUDGET\", \"name\": \"Budget 2027\"}" >/dev/null
check "a rename keeps the comments" '[.comments[].body] == ["on C"]' "$(ok alice ListComments "{\"appId\": \"$APP2\", \"metric\": \"$BUDGET\"}")"
MODEL2=$(model "$APP2")
check "a rename keeps the tables and views" "(.tables[0].metrics == [\"$BUDGET\", \"$REVENUE\"]) and (.views[0].metrics == [\"$BUDGET\"]) and (.metrics[] | select(.id == \"$BUDGET\") | .name == \"Budget 2027\")" "$MODEL2"
check "a rename and a restore keep the catalog" \
  ".metrics[] | select(.id == \"$BUDGET\") | .description == \"Sales budget\" and .folder == \"Plan\" and .owner == \"alice\"" "$MODEL2"
check "restored model" "(.lists[] | select(.name == \"Product\") | .members[0].properties[\"$NOTE\"] == \"first\") and (.views | length == 1) and (.lists[-1].kind == \"LIST_KIND_SCENARIO\")" "$MODEL2"
B27='"appId": "'$APP2'", "metric": {"id": "'$BUDGET'", "name": "Budget 2027"'
check "create of an existing Metric is refused" '.code == "already_exists"' \
  "$(call alice CreateMetric "{$B27, \"dimensions\": [\"$PRODUCT\"]}}")"
check "update of a missing Metric is refused" '.code == "not_found"' \
  "$(call alice UpdateMetric "{\"appId\": \"$APP2\", \"metric\": {\"id\": \"$(uuid)\", \"name\": \"Nothing\"}}")"
Q=$(ok alice Query "{\"appId\": \"$APP2\", \"metrics\": [\"$BUDGET\"], \"rows\": [\"$PRODUCT\"]}")
check "the refused change keeps the input values" "$(cell "$BUDGET" "[\"$A\"]").number == 2400" "$Q"
ok alice UpdateMetric "{$B27, \"dimensions\": [\"$PRODUCT\"]}}" >/dev/null
check "update changes the input Metric" ".metrics[] | select(.id == \"$BUDGET\") | .dimensions == [\"$PRODUCT\"]" "$(model "$APP2")"
ok alice DeleteMetric "{\"appId\": \"$APP2\", \"id\": \"$BUDGET\"}" >/dev/null
check "a deleted Metric leaves the tables and views in GetModel" "(.tables[0].metrics == [\"$REVENUE\"]) and ((.views[0].metrics // []) == [])" "$(model "$APP2")"
check "a deleted Metric is skipped in a query" '(.cells // []) == []' "$(ok alice Query "{\"appId\": \"$APP2\", \"metrics\": [\"$BUDGET\"], \"rows\": [\"$PRODUCT\"]}")"
VIEW2=$(uuid)
ok alice CreateView "{\"appId\": \"$APP2\", \"view\": {\"id\": \"$VIEW2\", \"name\": \"C only\", \"metrics\": [\"$REVENUE\"], \"filters\": {\"$PRODUCT\": {\"ids\": [\"$C\"]}}}}" >/dev/null
ok alice EditMembers "{\"appId\": \"$APP2\", \"list\": \"$PRODUCT\", \"edits\": [{\"rename\": {\"id\": \"$C\", \"name\": \" D \"}}]}" >/dev/null
check "a member rename keeps the comment cell" ".comments[0].cell[\"$PRODUCT\"] == \"$C\"" "$(ok alice ListComments "{\"appId\": \"$APP2\", \"metric\": \"$BUDGET\"}")"
check "a member rename gives the trimmed name" ".lists[] | select(.name == \"Product\") | .members[] | select(.id == \"$C\") | .name == \"D\"" "$(model "$APP2")"
ok alice EditMembers "{\"appId\": \"$APP2\", \"list\": \"$PRODUCT\", \"edits\": [{\"rename\": {\"id\": \"$A\", \"name\": \"A1\"}}]}" >/dev/null
check "a member rename keeps the TEXT value and the DIMENSION value" \
  ".lists[] | select(.name == \"Product\") | .members[] | select(.id == \"$A\") | .name == \"A1\" and .properties == {\"$PCAT\": \"$HARD\", \"$NOTE\": \"first\"}" "$(model "$APP2")"
ok alice EditMembers "{\"appId\": \"$APP2\", \"list\": \"$PRODUCT\", \"edits\": [{\"rename\": {\"id\": \"$A\", \"name\": \"A\"}}]}" >/dev/null
ok alice EditMembers "{\"appId\": \"$APP2\", \"list\": \"$PRODUCT\", \"edits\": [{\"remove\": {\"id\": \"$C\"}}]}" >/dev/null
check "a removed member keeps its comments" ".comments[0].cell[\"$PRODUCT\"] == \"$C\"" "$(ok alice ListComments "{\"appId\": \"$APP2\", \"metric\": \"$BUDGET\"}")"
check "a removed member stays in the view filter in GetModel" ".views[] | select(.name == \"C only\") | .filters[\"$PRODUCT\"].ids == [\"$C\"]" "$(model "$APP2")"
check "a removed id cannot come back" '.code == "aborted" or .code == "already_exists"' \
  "$(call alice EditMembers "{\"appId\": \"$APP2\", \"list\": \"$PRODUCT\", \"edits\": [{\"add\": {\"id\": \"$C\", \"name\": \"C again\"}}]}")"
LOAD=$(uuid) LNOTE=$(uuid)
ok alice CreateList "{\"appId\": \"$APP2\", \"id\": \"$LOAD\", \"name\": \"Load\", \"kind\": \"LIST_KIND_DIMENSION\"}" >/dev/null
ok alice AddProperty "{\"appId\": \"$APP2\", \"list\": \"$LOAD\", \"property\": {\"id\": \"$LNOTE\", \"name\": \"Note\", \"type\": \"PROPERTY_TYPE_TEXT\"}}" >/dev/null
PIDS=()
for i in $(seq 8); do
  ok alice EditMembers "{\"appId\": \"$APP2\", \"list\": \"$LOAD\", \"edits\": [{\"add\": {\"id\": \"$(uuid)\", \"name\": \"m$i\", \"properties\": {\"$LNOTE\": \"n$i\"}}}]}" >/dev/null &
  PIDS+=($!)
done
waitall
check "concurrent edits keep all TEXT values" "[.lists[] | select(.name == \"Load\") | .members[].properties[\"$LNOTE\"]] | sort == [\"n1\", \"n2\", \"n3\", \"n4\", \"n5\", \"n6\", \"n7\", \"n8\"]" "$(model "$APP2")"
for i in $(seq 8); do
  ok alice Import "$(jq -n --arg app "$APP2" --arg csv "$(printf 'amount\n%s\n' "$i")" --arg sales "$SALES" --arg amount "$AMOUNT" \
    '{appId: $app, csv: $csv, list: {list: $sales, propertyColumns: {($amount): "amount"}}}')" >/dev/null &
  PIDS+=($!)
done
waitall
check "concurrent imports add all transaction rows" '[.lists[] | select(.name == "Sales") | .members[].name] | length == 12' "$(model "$APP2")"
ok alice DeleteItem "{\"appId\": \"$APP2\", \"id\": \"$VIEW\", \"type\": \"ITEM_TYPE_VIEW\"}" >/dev/null
check "a deleted view leaves the board widget in GetModel" '(.boards[0].widgets | length == 1) and (.boards[0].widgets[0].text == "Notes")' "$(model "$APP2")"

TOTAL=$(uuid)
ok alice CreateMetric "{\"appId\": \"$APP\", \"metric\": {\"id\": \"$TOTAL\", \"name\": \"Total\", \"dimensions\": [\"$SCENARIO\", \"$MONTH\"], \"formula\": \"Budget[REMOVE SUM: Product]\"}}" >/dev/null
check "alice reads the total" '.cells[0].value.number == 2414' "$(ok alice Query "{\"appId\": \"$APP\", \"metrics\": [\"$TOTAL\"], \"aggregation\": \"AGGREGATION_SUM\"}")"
PICK=$(uuid)
ok alice CreateMetric "{\"appId\": \"$APP\", \"metric\": {\"id\": \"$PICK\", \"name\": \"Pick\", \"dimensions\": [\"$CATEGORY\"], \"kind\": \"VALUE_KIND_MEMBER\", \"memberList\": \"$PRODUCT\"}}" >/dev/null
check "a member Metric gives its list" ".metrics[] | select(.id == \"$PICK\") | .kind == \"VALUE_KIND_MEMBER\" and .memberList == \"$PRODUCT\"" "$(model "$APP")"
check "a member Metric needs an existing list" '.code == "invalid_argument"' \
  "$(call alice CreateMetric "{\"appId\": \"$APP\", \"metric\": {\"id\": \"$(uuid)\", \"name\": \"Pick2\", \"kind\": \"VALUE_KIND_MEMBER\", \"memberList\": \"$(uuid)\"}}")"
ok alice WriteCells "{\"appId\": \"$APP\", \"writes\": [{\"metric\": \"$PICK\", \"coords\": {\"$CATEGORY\": \"$HARD\"}, \"value\": {\"member\": \"$B\"}},
  {\"metric\": \"$PICK\", \"coords\": {\"$CATEGORY\": \"$SOFT\"}, \"value\": {\"member\": \"$A\"}}]}" >/dev/null
ok alice AddComment "{\"appId\": \"$APP\", \"comment\": {\"id\": \"$(uuid)\", \"metric\": \"$BUDGET\", \"cell\": {\"$PRODUCT\": \"$B\", \"$SCENARIO\": \"$BASE\", \"$MONTH\": \"$JAN\"}, \"body\": \"second\"}}" >/dev/null
check "alice sees all comments" '[.comments[].body] == ["check this", "second"]' \
  "$(ok alice ListComments "{\"appId\": \"$APP\", \"metric\": \"$BUDGET\"}")"
check "no user" '.code == "unauthenticated"' "$(curl -sS -X POST "$API/ListApplications" -H 'Content-Type: application/json' -d '{}')"

# A resend with the same client_op_id gives the stored result, and another content with that id is refused.
OP=$(uuid) RESENT=$(uuid)
BODY="{\"appId\": \"$APP\", \"clientOpId\": \"$OP\", \"id\": \"$RESENT\", \"name\": \"Resent\", \"kind\": \"LIST_KIND_DIMENSION\"}"
curl -sS -X POST "$API/CreateList" -H 'Content-Type: application/json' -H "X-Nanashi-User: alice" -d "$BODY" >/dev/null
check "a resend gives the stored result" 'has("code") | not' "$(curl -sS -X POST "$API/CreateList" -H 'Content-Type: application/json' -H "X-Nanashi-User: alice" -d "$BODY")"
check "another content with the same client_op_id is refused" '.code == "invalid_argument"' \
  "$(curl -sS -X POST "$API/CreateList" -H 'Content-Type: application/json' -H "X-Nanashi-User: alice" -d "${BODY/Resent/Other}")"

AUDIT=$(ok alice ListAudit "{\"appId\": \"$APP\"}")
check "audit trail" '([.entries[].action] | index("WriteCells") != null) and ([.entries[].action] | index("Query") == null) and ([.entries[].action] | index("CreateList") != null)' "$AUDIT"
echo "e2e passed: $APP (restored as $APP2), $(jq '.entries | length' <<<"$AUDIT") audit entries"
