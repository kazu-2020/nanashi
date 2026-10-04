#!/usr/bin/env bash
# End-to-end check of nanashi-api with real engines.
# It needs PostgreSQL (NANASHI_PG_DSN) and nanashi-router (ROUTER). It starts nanashi-api, which starts the engines.
# At the end it stops nanashi-api and the engines that nanashi-api started.
set -euo pipefail
cd "$(dirname "$0")"

DSN=${NANASHI_PG_DSN:-postgresql://postgres@127.0.0.1:55432/nanashi}
ROUTER=${ROUTER:-http://127.0.0.1:8090}
LISTEN=${LISTEN:-127.0.0.1:8080}
API=http://$LISTEN/nanashi.v1.PlanService
WORK=$(mktemp -d)

curl -sf "$ROUTER/healthz" >/dev/null || { echo "nanashi-router does not run at $ROUTER"; exit 1; }
go build -o "$WORK/nanashi-api" ./cmd/nanashi-api
"$WORK/nanashi-api" --pg "$DSN" --listen "$LISTEN" --router "$ROUTER" --tessera ../tessera --engine-dir "${ENGINE_DIR:-../.nanashi-data}" \
  >"$WORK/api.log" 2>&1 &
API_PID=$!
trap 'kill $API_PID 2>/dev/null; wait $API_PID 2>/dev/null; echo "log: $WORK/api.log"' EXIT

call() { # user method body
  curl -sS -X POST "$API/$2" -H 'Content-Type: application/json' -H "X-Nanashi-User: $1" -d "$3"
}
ok() { # user method body: the call must succeed
  local out
  out=$(call "$@")
  if jq -e 'has("code")' <<<"$out" >/dev/null; then echo "FAIL $2: $out"; exit 1; fi
  echo "$out"
}
check() { # description jq-expression json
  if jq -e "$2" <<<"$3" >/dev/null; then echo "ok   $1"; else echo "FAIL $1: $3"; exit 1; fi
}
cell() { # metric coords-json: the jq filter for the value of one cell
  echo "(.cells[] | select(.metric == \"$1\" and .coords == $2) | .value)"
}

for _ in $(seq 100); do call alice ListApplications '{}' >/dev/null 2>&1 && break; sleep 0.1; done

APP=$(ok alice CreateApplication '{"name": "E2E"}' | jq -r .id)
check "create application" '.role == "ROLE_ADMIN"' "$(ok alice ListApplications '{}' | jq ".applications[] | select(.id == \"$APP\")")"

ok alice CreateCalendar "{\"appId\": \"$APP\", \"startYear\": 2026, \"years\": 1}" >/dev/null
ok alice CreateList "{\"appId\": \"$APP\", \"name\": \"Category\", \"kind\": \"LIST_KIND_DIMENSION\", \"members\": [\"Hard\", \"Soft\"]}" >/dev/null
ok alice CreateList "{\"appId\": \"$APP\", \"name\": \"Product\", \"kind\": \"LIST_KIND_DIMENSION\", \"members\": [\"A\", \"B\", \"C\"]}" >/dev/null
ok alice AddProperty "{\"appId\": \"$APP\", \"list\": \"Product\", \"property\": {\"name\": \"Category\", \"type\": \"PROPERTY_TYPE_DIMENSION\", \"target\": \"Category\"}}" >/dev/null
ok alice AddProperty "{\"appId\": \"$APP\", \"list\": \"Product\", \"property\": {\"name\": \"Note\", \"type\": \"PROPERTY_TYPE_TEXT\"}}" >/dev/null
ok alice EditMembers "{\"appId\": \"$APP\", \"list\": \"Product\", \"edits\": [
  {\"set\": {\"name\": \"A\", \"properties\": {\"Category\": \"Hard\", \"Note\": \"first\"}}},
  {\"set\": {\"name\": \"B\", \"properties\": {\"Category\": \"Hard\"}}},
  {\"set\": {\"name\": \"C\", \"properties\": {\"Category\": \"Soft\"}}}]}" >/dev/null
check "a property name is used once in a list" '.code == "already_exists"' \
  "$(call alice AddProperty "{\"appId\": \"$APP\", \"list\": \"Product\", \"property\": {\"name\": \"Note\", \"type\": \"PROPERTY_TYPE_DIMENSION\", \"target\": \"Category\"}}")"

ok alice CreateList "{\"appId\": \"$APP\", \"name\": \"Sales\", \"kind\": \"LIST_KIND_TRANSACTION\"}" >/dev/null
ok alice AddProperty "{\"appId\": \"$APP\", \"list\": \"Sales\", \"property\": {\"name\": \"Product\", \"type\": \"PROPERTY_TYPE_DIMENSION\", \"target\": \"Product\"}}" >/dev/null
ok alice AddProperty "{\"appId\": \"$APP\", \"list\": \"Sales\", \"property\": {\"name\": \"Amount\", \"type\": \"PROPERTY_TYPE_NUMBER\"}}" >/dev/null
CSV=$'product,amount\nA,100\nB,50\nA,25\nC,10\n'
IMPORT=$(ok alice Import "$(jq -n --arg app "$APP" --arg csv "$CSV" \
  '{appId: $app, csv: $csv, list: {list: "Sales", propertyColumns: {Product: "product", Amount: "amount"}}}')")
check "import 4 transactions" '.rows == 4' "$IMPORT"

ok alice CreateScenario "{\"appId\": \"$APP\", \"name\": \"Base\"}" >/dev/null
ok alice SaveMetric "{\"appId\": \"$APP\", \"metric\": {\"name\": \"Budget\", \"dimensions\": [\"Product\", \"Scenario\", \"Month\"]}}" >/dev/null
ok alice SaveMetric "{\"appId\": \"$APP\", \"metric\": {\"name\": \"Revenue\", \"dimensions\": [\"Product\"], \"formula\": \"'Sales.Amount'[BY SUM: Sales.Product]\"}}" >/dev/null
ok alice WriteCells "{\"appId\": \"$APP\", \"writes\": [
  {\"metric\": \"Budget\", \"coords\": {\"Product\": \"A\", \"Scenario\": \"Base\"}, \"value\": {\"number\": 1200}},
  {\"metric\": \"Budget\", \"coords\": {\"Product\": \"B\", \"Scenario\": \"Base\", \"Month\": \"2026-01\"}, \"value\": {\"number\": 7}}]}" >/dev/null

MODEL=$(ok alice GetModel "{\"appId\": \"$APP\"}")
check "model lists" '[.lists[].name] == ["Year", "Quarter", "Month", "Category", "Product", "Sales", "Scenario"]' "$MODEL"
check "list kinds" '[.lists[] | .kind] == ["LIST_KIND_CALENDAR", "LIST_KIND_CALENDAR", "LIST_KIND_CALENDAR", "LIST_KIND_DIMENSION", "LIST_KIND_DIMENSION", "LIST_KIND_TRANSACTION", "LIST_KIND_SCENARIO"]' "$MODEL"
check "member properties" '.lists[] | select(.name == "Product") | .members[0].properties == {"Category": "Hard", "Note": "first"}' "$MODEL"
check "transaction rows" '.lists[] | select(.name == "Sales") | [.members[] | .properties.Amount] == ["100", "50", "25", "10"]' "$MODEL"
check "property Metric is not a Metric" '[.metrics[].name] == ["Budget", "Revenue"]' "$MODEL"

Q=$(ok alice Query "{\"appId\": \"$APP\", \"metrics\": [\"Revenue\"], \"rows\": [\"Product\"]}")
check "BY SUM of transactions" "[$(cell Revenue '["A"]').number, $(cell Revenue '["C"]').number] == [125, 10]" "$Q"
Q=$(ok alice Query "{\"appId\": \"$APP\", \"metrics\": [\"Budget\"], \"rows\": [\"Product\"], \"columns\": [\"Scenario\"],
  \"filters\": {\"Month\": {\"names\": [\"2026-01\", \"2026-02\", \"2026-03\"]}}}")
check "spread over 12 months, Q1 total" "[$(cell Budget '["A", "Base"]').number, $(cell Budget '["B", "Base"]').number] == [300, 7]" "$Q"

ok alice CreateScenario "{\"appId\": \"$APP\", \"name\": \"Plan\", \"copyFrom\": \"Base\"}" >/dev/null
Q=$(ok alice Query "{\"appId\": \"$APP\", \"metrics\": [\"Budget\"], \"columns\": [\"Scenario\"], \"aggregation\": \"SUM\"}")
check "scenario copy" "[$(cell Budget '["Base"]').number, $(cell Budget '["Plan"]').number] == [1207, 1207]" "$Q"

TABLE=$(ok alice SaveTable "{\"appId\": \"$APP\", \"name\": \"Sales table\", \"metrics\": [\"Budget\", \"Revenue\"]}" | jq -r .id)
VIEW=$(ok alice SaveView "{\"appId\": \"$APP\", \"name\": \"Budget by product\", \"metrics\": [\"Budget\"], \"rows\": [\"Product\"],
  \"columns\": [\"Scenario\"], \"display\": \"DISPLAY_BAR\"}" | jq -r .id)
ok alice SaveBoard "{\"appId\": \"$APP\", \"name\": \"Overview\", \"widgets\": [{\"viewId\": \"$VIEW\"}, {\"text\": \"Notes\"}],
  \"pageSelectors\": [\"Scenario\"]}" >/dev/null
MODEL=$(ok alice GetModel "{\"appId\": \"$APP\"}")
check "table, view and board" "(.tables[0].id == \"$TABLE\") and (.views[0].display == \"DISPLAY_BAR\") and (.boards[0].widgets[0].viewId == \"$VIEW\")" "$MODEL"

ok alice AddComment "{\"appId\": \"$APP\", \"target\": \"metric:Budget\", \"cell\": {\"Product\": \"A\"}, \"body\": \"check this\"}" >/dev/null
check "comment" '.comments[0].user == "alice" and .comments[0].cell.Product == "A"' \
  "$(ok alice ListComments "{\"appId\": \"$APP\", \"target\": \"metric:Budget\"}")"

SNAP=$(ok alice CreateSnapshot "{\"appId\": \"$APP\", \"name\": \"before review\"}" | jq -r .id)
check "snapshot list" ".snapshots[0].id == \"$SNAP\"" "$(ok alice ListSnapshots "{\"appId\": \"$APP\"}")"
APP2=$(ok alice CreateApplication "{\"name\": \"E2E restored\", \"snapshotId\": \"$SNAP\"}" | jq -r .id)
Q=$(ok alice Query "{\"appId\": \"$APP2\", \"metrics\": [\"Revenue\", \"Budget\"], \"rows\": [\"Product\"], \"columns\": [\"Scenario\"]}")
check "restored data" "[$(cell Revenue '["A", ""]').number, $(cell Budget '["A", "Plan"]').number] == [125, 1200]" "$Q"
ok alice RenameMetric "{\"appId\": \"$APP2\", \"name\": \"Budget\", \"newName\": \"Budget 2027\"}" >/dev/null
check "rename reaches tables and views" '(.tables[0].metrics == ["Budget 2027", "Revenue"]) and (.views[0].metrics == ["Budget 2027"])' \
  "$(ok alice GetModel "{\"appId\": \"$APP2\"}")"
MODEL2=$(ok alice GetModel "{\"appId\": \"$APP2\"}")
check "restored model" '(.lists[] | select(.name == "Product") | .members[0].properties.Note == "first") and (.views | length == 1) and (.lists[-1].kind == "LIST_KIND_SCENARIO")' "$MODEL2"
B27='"appId": "'$APP2'", "metric": {"name": "Budget 2027"'
check "a dimension change of an input Metric is refused" '.code == "failed_precondition"' \
  "$(call alice SaveMetric "{$B27, \"dimensions\": [\"Product\"]}}")"
check "a formula over an input Metric is refused" '.code == "failed_precondition"' \
  "$(call alice SaveMetric "{$B27, \"formula\": \"1\"}}")"
Q=$(ok alice Query "{\"appId\": \"$APP2\", \"metrics\": [\"Budget 2027\"], \"rows\": [\"Product\"]}")
check "the refused change keeps the input values" "$(cell 'Budget 2027' '["A"]').number == 2400" "$Q"
ok alice SaveMetric "{$B27, \"dimensions\": [\"Product\"]}, \"replace\": true}" >/dev/null
check "replace changes the input Metric" '.metrics[] | select(.name == "Budget 2027") | .dimensions == ["Product"]' \
  "$(ok alice GetModel "{\"appId\": \"$APP2\"}")"
ok alice DeleteMetric "{\"appId\": \"$APP2\", \"name\": \"Budget 2027\"}" >/dev/null
check "delete removes the Metric from tables and views" '(.tables[0].metrics == ["Revenue"]) and ((.views[0].metrics // []) == [])' \
  "$(ok alice GetModel "{\"appId\": \"$APP2\"}")"
ok alice SaveView "{\"appId\": \"$APP2\", \"name\": \"C only\", \"metrics\": [\"Revenue\"], \"filters\": {\"Product\": {\"names\": [\"C\"]}}}" >/dev/null
ok alice EditMembers "{\"appId\": \"$APP2\", \"list\": \"Product\", \"edits\": [{\"rename\": {\"name\": \"C\", \"newName\": \" D \"}}]}" >/dev/null
check "rename gives the view filter the trimmed name" '.views[] | select(.name == "C only") | .filters.Product.names == ["D"]' \
  "$(ok alice GetModel "{\"appId\": \"$APP2\"}")"
ok alice CreateList "{\"appId\": \"$APP2\", \"name\": \"Load\", \"kind\": \"LIST_KIND_DIMENSION\"}" >/dev/null
ok alice AddProperty "{\"appId\": \"$APP2\", \"list\": \"Load\", \"property\": {\"name\": \"Note\", \"type\": \"PROPERTY_TYPE_TEXT\"}}" >/dev/null
PIDS=()
for i in $(seq 8); do
  ok alice EditMembers "{\"appId\": \"$APP2\", \"list\": \"Load\", \"edits\": [{\"add\": {\"name\": \"m$i\", \"properties\": {\"Note\": \"n$i\"}}}]}" >/dev/null &
  PIDS+=($!)
done
wait "${PIDS[@]}"
check "concurrent edits keep all TEXT values" '[.lists[] | select(.name == "Load") | .members[].properties.Note] | sort == ["n1", "n2", "n3", "n4", "n5", "n6", "n7", "n8"]' \
  "$(ok alice GetModel "{\"appId\": \"$APP2\"}")"

ok alice SetMemberRole "{\"appId\": \"$APP\", \"user\": \"bob\", \"role\": \"ROLE_VIEWER\"}" >/dev/null
ok alice SaveAccessRule "{\"appId\": \"$APP\", \"role\": \"ROLE_VIEWER\", \"list\": \"Product\", \"members\": [\"A\"]}" >/dev/null
check "access" '(.members | length == 2) and (.rules | length == 1)' "$(ok alice GetAccess "{\"appId\": \"$APP\"}")"
check "bob sees the app as VIEWER" ".applications | map(select(.id == \"$APP\")) | .[0].role == \"ROLE_VIEWER\"" "$(ok bob ListApplications '{}')"
check "bob sees only Product A" '.lists[] | select(.name == "Product") | [.members[].name] == ["A"]' "$(ok bob GetModel "{\"appId\": \"$APP\"}")"
Q=$(ok bob Query "{\"appId\": \"$APP\", \"metrics\": [\"Revenue\", \"Budget\"], \"rows\": [\"Product\"]}")
check "bob reads only Product A" '[.cells[].coords[0]] | unique == ["A"]' "$Q"
check "bob total is A only" "$(cell Budget '["A"]').number == 2400" "$Q"
check "bob cannot write" '.code == "permission_denied"' \
  "$(call bob WriteCells "{\"appId\": \"$APP\", \"writes\": [{\"metric\": \"Budget\", \"coords\": {\"Product\": \"A\", \"Scenario\": \"Base\", \"Month\": \"2026-01\"}, \"value\": {\"number\": 1}}]}")"
check "bob cannot model" '.code == "permission_denied"' "$(call bob SaveMetric "{\"appId\": \"$APP\", \"metric\": {\"name\": \"X\"}}")"
ok alice AddComment "{\"appId\": \"$APP\", \"target\": \"metric:Budget\", \"cell\": {\"Product\": \"B\"}, \"body\": \"hidden\"}" >/dev/null
check "bob does not see a comment on a hidden cell" '[.comments[].body] == ["check this"]' \
  "$(ok bob ListComments "{\"appId\": \"$APP\", \"target\": \"metric:Budget\"}")"
check "alice sees all comments" '[.comments[].body] == ["check this", "hidden"]' \
  "$(ok alice ListComments "{\"appId\": \"$APP\", \"target\": \"metric:Budget\"}")"
check "bob cannot read the audit trail" '.code == "permission_denied"' "$(call bob ListAudit "{\"appId\": \"$APP\"}")"
ok alice SetMemberRole "{\"appId\": \"$APP\", \"user\": \"carol\", \"role\": \"ROLE_CONTRIBUTOR\"}" >/dev/null
ok alice SaveAccessRule "{\"appId\": \"$APP\", \"role\": \"ROLE_CONTRIBUTOR\", \"list\": \"Product\", \"members\": [\"A\"], \"write\": true}" >/dev/null
W='"metric": "Budget", "coords": {"Scenario": "Base", "Month": "2026-02", "Product": '
ok carol WriteCells "{\"appId\": \"$APP\", \"writes\": [{$W \"A\"}, \"value\": {\"number\": 1}}]}" >/dev/null
check "carol cannot write outside her rule" '.code == "permission_denied"' \
  "$(call carol WriteCells "{\"appId\": \"$APP\", \"writes\": [{$W \"B\"}, \"value\": {\"number\": 1}}]}")"
check "carol cannot spread over Product" '.code == "permission_denied"' \
  "$(call carol WriteCells "{\"appId\": \"$APP\", \"writes\": [{\"metric\": \"Budget\", \"coords\": {\"Scenario\": \"Base\"}, \"value\": {\"number\": 1}}]}")"
check "no user" '.code == "unauthenticated"' "$(curl -sS -X POST "$API/ListApplications" -H 'Content-Type: application/json' -d '{}')"

AUDIT=$(ok alice ListAudit "{\"appId\": \"$APP\"}")
check "audit trail" '([.entries[].action] | index("WriteCells") != null) and ([.entries[].action] | index("Query") == null) and (.entries[0] | .action == "WriteCells" and .user == "carol")' "$AUDIT"
echo "e2e passed: $APP (restored as $APP2), $(jq '.entries | length' <<<"$AUDIT") audit entries"
