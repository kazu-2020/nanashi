package api

// This file holds the calculations: pure functions that change requests and engine data into
// engine operations and responses. They do no I/O.

import (
	"bytes"
	"encoding/csv"
	"encoding/json"
	"errors"
	"fmt"
	"maps"
	"slices"
	"strconv"
	"strings"

	nanashiv1 "github.com/kazu-2020/nanashi/api/gen/nanashi/v1"
)

// op is one Model operation in a write to the engine (POST /writes).
type op struct {
	Op     string         `json:"op"`
	Args   []any          `json:"args"`
	Kwargs map[string]any `json:"kwargs,omitempty"`
}

func newOp(name string, args ...any) op { return op{Op: name, Args: args} }

func (o op) with(kwargs map[string]any) op {
	o.Kwargs = kwargs
	return o
}

// engineModel is the engine definition (GET /) with the order of dimensions, properties and Metrics kept.
// Seq is the version of the model that the engine sent.
type engineModel struct {
	Seq     int64
	Dims    []engineDim
	Metrics []engineMetric
}

type engineDim struct {
	Name    string
	Members []string
	Ordered bool
	Props   []engineProp
}

// engineProp is a DIMENSION property: a member of the list maps to a member of Target.
type engineProp struct {
	Name   string
	Target string
	Values map[string]string
}

type engineMetric struct {
	Name        string
	Dims        []string
	Kind        string
	Formula     string // Empty for an input Metric.
	Overridable bool
}

// engineCube is the body of slice and summary: each cell is the coordinates in dims order and then the value.
// Seq is the version of the model that the engine read. A snapshot stores it too; replay ignores it.
type engineCube struct {
	Seq   int64    `json:"seq"`
	Dims  []string `json:"dims"`
	Cells [][]any  `json:"cells"`
}

// sameSeq tells if every cube comes from the version of em.
func sameSeq(em engineModel, cubes ...map[string]engineCube) bool {
	for _, m := range cubes {
		for _, c := range m {
			if c.Seq != em.Seq {
				return false
			}
		}
	}
	return true
}

func (m engineModel) dim(name string) (engineDim, bool) {
	i := slices.IndexFunc(m.Dims, func(d engineDim) bool { return d.Name == name })
	if i < 0 {
		return engineDim{}, false
	}
	return m.Dims[i], true
}

func (m engineModel) metric(name string) (engineMetric, error) {
	i := slices.IndexFunc(m.Metrics, func(x engineMetric) bool { return x.Name == name })
	if i < 0 {
		return engineMetric{}, fmt.Errorf("Metric %s がない", name)
	}
	return m.Metrics[i], nil
}

func parseEngineModel(body []byte) (engineModel, error) {
	var raw struct {
		Seq        int64           `json:"seq"`
		Dimensions json.RawMessage `json:"dimensions"`
		Metrics    json.RawMessage `json:"metrics"`
	}
	if err := json.Unmarshal(body, &raw); err != nil {
		return engineModel{}, err
	}
	// Go maps lose the key order. The order of dimensions and Metrics is significant (display, replay).
	out := engineModel{Seq: raw.Seq}
	dims, err := objectEntries(raw.Dimensions)
	if err != nil {
		return engineModel{}, err
	}
	for _, e := range dims {
		var d struct {
			Members        []string                     `json:"members"`
			Ordered        bool                         `json:"ordered"`
			Properties     json.RawMessage              `json:"properties"`
			PropertyValues map[string]map[string]string `json:"property_values"`
		}
		if err := json.Unmarshal(e.value, &d); err != nil {
			return engineModel{}, err
		}
		props, err := objectEntries(d.Properties)
		if err != nil {
			return engineModel{}, err
		}
		dim := engineDim{Name: e.key, Members: d.Members, Ordered: d.Ordered}
		for _, p := range props {
			var target string
			if err := json.Unmarshal(p.value, &target); err != nil {
				return engineModel{}, err
			}
			values := d.PropertyValues[p.key]
			if values == nil {
				values = map[string]string{}
			}
			dim.Props = append(dim.Props, engineProp{Name: p.key, Target: target, Values: values})
		}
		out.Dims = append(out.Dims, dim)
	}
	metrics, err := objectEntries(raw.Metrics)
	if err != nil {
		return engineModel{}, err
	}
	for _, e := range metrics {
		var m struct {
			Dims        []string `json:"dims"`
			Kind        string   `json:"kind"`
			Formula     string   `json:"formula"`
			Overridable bool     `json:"overridable"`
		}
		if err := json.Unmarshal(e.value, &m); err != nil {
			return engineModel{}, err
		}
		out.Metrics = append(out.Metrics, engineMetric{Name: e.key, Dims: m.Dims, Kind: m.Kind, Formula: m.Formula, Overridable: m.Overridable})
	}
	return out, nil
}

type jsonEntry struct {
	key   string
	value json.RawMessage
}

// objectEntries gives the keys and values of a JSON object in their order.
func objectEntries(raw json.RawMessage) ([]jsonEntry, error) {
	if len(raw) == 0 {
		return nil, nil
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	if _, err := dec.Token(); err != nil {
		return nil, err
	}
	var out []jsonEntry
	for dec.More() {
		t, err := dec.Token()
		if err != nil {
			return nil, err
		}
		var value json.RawMessage
		if err := dec.Decode(&value); err != nil {
			return nil, err
		}
		out = append(out, jsonEntry{t.(string), value})
	}
	return out, nil
}

// stmt is one SQL statement with its arguments.
type stmt struct {
	sql  string
	args []any
}

// memberRefs are the places in the api tables that name a member of a list. The arguments of each statement
// are (app, list, old name, new name). A null new name removes the old name (app_rename in schema.sql): if a
// later edit adds the name again, an old rule must not give access to it.
// A cell comment of a removed member stays as it is. The comments list still shows it.
var memberRefs = []string{
	`update app_property set text_values = case when $4::text is null then text_values - $3
		else jsonb_set(text_values - $3, array[$4::text], text_values->$3) end where app_id = $1 and list = $2 and text_values ? $3`,
	`update app_access_rule set members = app_rename(members, $3, $4) where app_id = $1 and list = $2 and members ? $3`,
	`update app_item set def = jsonb_set(def, array['filters', $2, 'names'], app_rename(def->'filters'->$2->'names', $3, $4))
		where app_id = $1 and def->'filters'->$2->'names' ? $3`,
	`update app_comment set cell = jsonb_set(cell, array[$2], to_jsonb($4::text)) where app_id = $1 and cell->>$2 = $3 and $4::text is not null`,
}

// metricRefs are the places in the api tables that name a Metric. The arguments are (app, old name, new name).
// DeleteMetric does not touch the comments of the Metric.
var metricRefs = []string{
	`update app_item set def = jsonb_set(def, '{metrics}', app_rename(def->'metrics', $2, $3)) where app_id = $1 and def->'metrics' ? $2`,
	`update app_comment set metric = $3 where app_id = $1 and metric = $2 and $3::text is not null`,
}

// memberRenames gives the statements that rename a member in the api tables, or remove it when name is nil.
func memberRenames(app, list, old string, name *string) []stmt {
	return statements(memberRefs, app, list, old, name)
}

// metricRenames gives the statements that rename a Metric in the api tables, or remove it when name is nil.
func metricRenames(app, old string, name *string) []stmt {
	return statements(metricRefs, app, old, name)
}

func statements(sqls []string, args ...any) []stmt {
	out := make([]stmt, len(sqls))
	for i, sql := range sqls {
		out[i] = stmt{sql, args}
	}
	return out
}

// propRow is a NUMBER, BOOLEAN or TEXT property, which the api keeps. The engine keeps DIMENSION properties.
type propRow struct {
	List string                 `json:"list"`
	Name string                 `json:"name"`
	Type nanashiv1.PropertyType `json:"type"`
	Text map[string]string      `json:"text,omitempty"` // Member to value, for TEXT only.
}

// appMeta is the api data of an application that the engine does not know.
type appMeta struct {
	Kinds map[string]nanashiv1.ListKind
	Props []propRow
}

// refuseProperty refuses a change to a Metric with the name of a property Metric. GetModel needs that Metric.
func (m appMeta) refuseProperty(names ...string) error {
	for _, n := range names {
		if slices.ContainsFunc(m.Props, func(p propRow) bool { return propMetric(p.List, p.Name) == n }) {
			return tag(errPrecondition, "%s はプロパティの値を持つ Metric なので、変更できない", n)
		}
	}
	return nil
}

// propKind is the kind of the input Metric that holds a NUMBER or BOOLEAN property.
var propKind = map[nanashiv1.PropertyType]string{
	nanashiv1.PropertyType_PROPERTY_TYPE_NUMBER:  "number",
	nanashiv1.PropertyType_PROPERTY_TYPE_BOOLEAN: "boolean",
}

// propMetric is the input Metric that holds a NUMBER or BOOLEAN property.
func propMetric(list, prop string) string { return list + "." + prop }

// The tags give an error its Connect code (connectError). An error without a tag is an input error.
var (
	errDenied       = errors.New("permission denied")
	errExists       = errors.New("already exists")
	errPrecondition = errors.New("failed precondition")
)

// tagged is an error with a tag. Error() gives only the message of err, so the user sees the Japanese text.
type tagged struct{ err, tag error }

func (t tagged) Error() string   { return t.err.Error() }
func (t tagged) Unwrap() []error { return []error{t.err, t.tag} }

func tag(t error, format string, a ...any) error { return tagged{fmt.Errorf(format, a...), t} }

// limit is the members of one list that a user can read and write.
type limit struct{ read, write map[string]bool }

// limits is list name to limit. A list without an entry has no limit.
type limits map[string]limit

func accessLimits(role nanashiv1.Role, rules []*nanashiv1.AccessRule) limits {
	out := limits{}
	if role >= nanashiv1.Role_ROLE_MODELER {
		return out
	}
	for _, r := range rules {
		if r.Role != role {
			continue
		}
		read := map[string]bool{}
		for _, m := range r.Members {
			read[m] = true
		}
		write := map[string]bool{}
		if r.Write {
			write = read
		}
		if old, ok := out[r.List]; ok {
			read, write = intersect(old.read, read), intersect(old.write, write)
		}
		out[r.List] = limit{read: read, write: write}
	}
	return out
}

// through also limits each list with a DIMENSION property on a limited list, for example the rows of a
// transaction list with a Product property when Product has a rule. A member with a blank value is hidden.
func (l limits) through(em engineModel) limits {
	if len(l) == 0 {
		return l
	}
	out := maps.Clone(l)
	for range em.Dims { // Each pass follows one more property step, so len(Dims) passes reach all chains.
		for _, d := range em.Dims {
			lim, limited := l[d.Name]
			for _, p := range d.Props {
				target, ok := out[p.Target]
				if !ok || p.Target == d.Name {
					continue
				}
				derived := limit{read: map[string]bool{}, write: map[string]bool{}}
				for _, m := range d.Members {
					if target.read[p.Values[m]] {
						derived.read[m] = true
					}
					if target.write[p.Values[m]] {
						derived.write[m] = true
					}
				}
				if limited {
					derived = limit{read: intersect(lim.read, derived.read), write: intersect(lim.write, derived.write)}
				}
				lim, limited = derived, true
			}
			if limited {
				out[d.Name] = lim
			}
		}
	}
	return out
}

func intersect(a, b map[string]bool) map[string]bool {
	out := map[string]bool{}
	for k := range a {
		if b[k] {
			out[k] = true
		}
	}
	return out
}

// hides tells if a formula Metric can show data of hidden members: it lacks a limited list, so it can aggregate the
// list away (for example Budget[REMOVE SUM: Product]). An input Metric without the list holds no data about it.
func (l limits) hides(m engineMetric) bool {
	for list := range l {
		if m.Formula != "" && !slices.Contains(m.Dims, list) {
			return true
		}
	}
	return false
}

func (l limits) visible(list, member string) bool {
	lim, ok := l[list]
	return !ok || lim.read[member]
}

// checkWrite refuses a write to a cell outside the write members, or a write that leaves a ruled list open.
func (l limits) checkWrite(metric string, dims []string, coords map[string]string) error {
	for _, d := range dims {
		lim, ok := l[d]
		if !ok {
			continue
		}
		c, set := coords[d]
		if !set {
			return tag(errDenied, "%s: 権限の制限がある %s のメンバーを指定せずに書き込めない", metric, d)
		}
		if !lim.write[c] {
			return tag(errDenied, "%s: %s の %q に書き込む権限がない", metric, d, c)
		}
	}
	return nil
}

func valueOf(v *nanashiv1.Value) any {
	switch x := v.GetValue().(type) {
	case *nanashiv1.Value_Number:
		return x.Number
	case *nanashiv1.Value_Boolean:
		return x.Boolean
	case *nanashiv1.Value_Member:
		return x.Member
	}
	return nil
}

func toValue(v any) *nanashiv1.Value {
	switch x := v.(type) {
	case float64:
		return &nanashiv1.Value{Value: &nanashiv1.Value_Number{Number: x}}
	case bool:
		return &nanashiv1.Value{Value: &nanashiv1.Value_Boolean{Boolean: x}}
	case string:
		return &nanashiv1.Value{Value: &nanashiv1.Value_Member{Member: x}}
	}
	return nil
}

func formatValue(v any) string {
	switch x := v.(type) {
	case float64:
		return strconv.FormatFloat(x, 'f', -1, 64)
	case bool:
		return strings.ToUpper(strconv.FormatBool(x))
	case string:
		return x
	}
	return ""
}

// parseValue reads a text value for a Metric kind. A blank text gives nil (a blank cell).
func parseValue(kind, text string) (any, error) {
	text = strings.TrimSpace(text)
	if text == "" {
		return nil, nil
	}
	switch kind {
	case "number":
		f, err := strconv.ParseFloat(strings.ReplaceAll(text, ",", ""), 64)
		if err != nil {
			return nil, fmt.Errorf("%q は数値ではない", text)
		}
		return f, nil
	case "boolean":
		b, err := strconv.ParseBool(strings.ToLower(text))
		if err != nil {
			return nil, fmt.Errorf("%q は TRUE か FALSE ではない", text)
		}
		return b, nil
	}
	return text, nil
}

// engineRead is one read of a Metric from the engine: GET /metrics/<metric>/<path>?<query>.
type engineRead struct {
	Metric string
	Path   string // "summary" for a number Metric, "slice" for other kinds, "overrides" for the overrides of a formula.
	Query  map[string][]string
}

// engineAggs gives the engine aggregation of each Aggregation.
var engineAggs = map[nanashiv1.Aggregation]string{
	nanashiv1.Aggregation_AGGREGATION_UNSPECIFIED: "sum",
	nanashiv1.Aggregation_AGGREGATION_SUM:         "sum",
	nanashiv1.Aggregation_AGGREGATION_AVG:         "avg",
	nanashiv1.Aggregation_AGGREGATION_MIN:         "min",
	nanashiv1.Aggregation_AGGREGATION_MAX:         "max",
	nanashiv1.Aggregation_AGGREGATION_COUNT:       "count",
}

// queryReads makes the engine reads for a QueryRequest. It leaves out a Metric that the filters make empty.
func queryReads(req *nanashiv1.QueryRequest, em engineModel, l limits) ([]engineRead, error) {
	agg, ok := engineAggs[req.Aggregation]
	if !ok {
		return nil, fmt.Errorf("集計 %d がない", req.Aggregation)
	}
	shown := slices.Concat(req.Rows, req.Columns)
	var out []engineRead
next:
	for _, name := range req.Metrics {
		m, err := em.metric(name)
		if err != nil {
			return nil, err
		}
		if l.hides(m) {
			continue
		}
		q := map[string][]string{}
		for _, d := range m.Dims {
			members := req.Filters[d].GetNames()
			lim, limited := l[d]
			if limited && len(members) == 0 {
				members = slices.Sorted(maps.Keys(lim.read))
			}
			if limited || len(members) > 0 {
				// A saved filter or a rule can name a member that was removed later. The engine refuses such a name.
				dim, _ := em.dim(d)
				exists := map[string]bool{}
				for _, x := range dim.Members {
					exists[x] = true
				}
				members = slices.DeleteFunc(slices.Clone(members), func(x string) bool { return !exists[x] || limited && !lim.read[x] })
				if len(members) == 0 {
					continue next
				}
				// All members is the same as no filter, and a big transaction list does not fit in the URL.
				// ponytail: a limit that hides some members still sends all readable names in the URL. Past about
				// 64 KiB the engine refuses the request (414). Upgrade: let the engine take the filter in a POST body.
				if len(members) == len(dim.Members) {
					members = nil
				}
			}
			if len(members) > 0 {
				// ponytail: the engine splits members at commas, so a member name with a comma cannot be a filter.
				q[d] = []string{strings.Join(members, ",")}
			}
		}
		read := engineRead{Metric: name, Path: "slice", Query: q}
		if m.Kind == "number" {
			keep := slices.DeleteFunc(slices.Clone(shown), func(d string) bool { return !slices.Contains(m.Dims, d) })
			q["keep"] = []string{strings.Join(keep, ",")}
			q["agg"] = []string{agg}
			read.Path = "summary"
		}
		out = append(out, read)
	}
	return out, nil
}

// queryCells changes an engine cube into QueryCells with coordinates in the order of dims.
// A slice of a non-number Metric can have dimensions that are not shown. Then the first cell wins.
// It leaves out a cell of a member Metric whose value is a member that l hides.
func queryCells(m engineMetric, dims []string, cube engineCube, l limits) []*nanashiv1.QueryCell {
	target, _ := strings.CutPrefix(m.Kind, "member:")
	index := make([]int, len(dims))
	for i, d := range dims {
		index[i] = slices.Index(cube.Dims, d)
	}
	seen := map[string]bool{}
	var out []*nanashiv1.QueryCell
	for _, c := range cube.Cells {
		v := toValue(c[len(c)-1])
		if v == nil || v.GetMember() != "" && !l.visible(target, v.GetMember()) {
			continue
		}
		coords := make([]string, len(dims))
		for i, j := range index {
			if j >= 0 {
				coords[i], _ = c[j].(string)
			}
		}
		key := strings.Join(coords, "\x00")
		if seen[key] {
			continue
		}
		seen[key] = true
		out = append(out, &nanashiv1.QueryCell{Metric: m.Name, Coords: coords, Value: v})
	}
	return out
}

// writeOps sets a cell if the coordinates give all dimensions of the Metric. Otherwise it spreads the value.
func writeOps(writes []*nanashiv1.CellWrite, em engineModel, l limits) ([]op, error) {
	var ops []op
	for _, w := range writes {
		m, err := em.metric(w.Metric)
		if err != nil {
			return nil, err
		}
		coords := map[string]any{}
		for d, member := range w.Coords {
			if !slices.Contains(m.Dims, d) {
				return nil, fmt.Errorf("%s: 軸 %s がない", m.Name, d)
			}
			coords[d] = member
		}
		if err := l.checkWrite(m.Name, m.Dims, w.Coords); err != nil {
			return nil, err
		}
		if len(w.Coords) == len(m.Dims) {
			ops = append(ops, newOp("set_cell", m.Name, valueOf(w.Value)).with(coords))
			continue
		}
		total, ok := valueOf(w.Value).(float64)
		if !ok {
			return nil, fmt.Errorf("%s: 集計したセルに入れられるのは数値だけ", m.Name)
		}
		ops = append(ops, newOp("spread", m.Name, total).with(coords))
	}
	return ops, nil
}

// editOps changes member edits into engine operations and the statements for the api tables (TEXT values and
// the name references). The engine keeps the DIMENSION property values and follows a rename or a removal itself.
func editOps(app, list string, em engineModel, meta appMeta, edits []*nanashiv1.MemberEdit) ([]op, []stmt, error) {
	dim, ok := em.dim(list)
	if !ok {
		return nil, nil, fmt.Errorf("リスト %s がない", list)
	}
	members := map[string]bool{}
	for _, m := range dim.Members {
		members[m] = true
	}
	dimProps := map[string]bool{}
	for _, p := range dim.Props {
		dimProps[p.Name] = true
	}
	types := map[string]nanashiv1.PropertyType{}
	for _, p := range meta.Props {
		if p.List == list {
			types[p.Name] = p.Type
		}
	}
	var ops []op
	var stmts []stmt
	exists := func(name string) error {
		if !members[name] {
			return fmt.Errorf("%s にメンバー %q がない", list, name)
		}
		return nil
	}
	setProps := func(member string, props map[string]string) error {
		for _, prop := range slices.Sorted(maps.Keys(props)) {
			v := strings.TrimSpace(props[prop])
			switch kind, isMetric := propKind[types[prop]]; {
			case dimProps[prop]:
				var value any // A blank value removes the value of the member.
				if v != "" {
					value = v
				}
				ops = append(ops, newOp("set_property_values", list, prop, map[string]any{member: value}))
			case isMetric:
				val, err := parseValue(kind, v)
				if err != nil {
					return fmt.Errorf("%s.%s: %w", list, prop, err)
				}
				ops = append(ops, newOp("set_cell", propMetric(list, prop), val).with(map[string]any{list: member}))
			case types[prop] == nanashiv1.PropertyType_PROPERTY_TYPE_TEXT:
				stmts = append(stmts, textValue(app, list, prop, member, v))
			default:
				return fmt.Errorf("%s にプロパティ %s がない", list, prop)
			}
		}
		return nil
	}
	for _, e := range edits {
		switch x := e.Edit.(type) {
		case *nanashiv1.MemberEdit_Add:
			name := strings.TrimSpace(x.Add.Name)
			if name == "" {
				return nil, nil, errors.New("メンバーの名前が空")
			}
			if members[name] {
				return nil, nil, fmt.Errorf("%s にメンバー %q はすでにある", list, name)
			}
			ops = append(ops, newOp("add_member", list, name))
			members[name] = true
			if err := setProps(name, x.Add.Properties); err != nil {
				return nil, nil, err
			}
		case *nanashiv1.MemberEdit_Set:
			if err := exists(x.Set.Name); err != nil {
				return nil, nil, err
			}
			if err := setProps(x.Set.Name, x.Set.Properties); err != nil {
				return nil, nil, err
			}
		case *nanashiv1.MemberEdit_Rename:
			old, name := x.Rename.Name, strings.TrimSpace(x.Rename.NewName)
			if err := exists(old); err != nil {
				return nil, nil, err
			}
			if name == "" || members[name] {
				return nil, nil, fmt.Errorf("%s に %q という名前は付けられない", list, name)
			}
			ops = append(ops, newOp("rename_member", list, old, name))
			stmts = append(stmts, memberRenames(app, list, old, &name)...)
			delete(members, old)
			members[name] = true
		case *nanashiv1.MemberEdit_Remove:
			if err := exists(x.Remove.Name); err != nil {
				return nil, nil, err
			}
			ops = append(ops, newOp("remove_member", list, x.Remove.Name))
			stmts = append(stmts, memberRenames(app, list, x.Remove.Name, nil)...)
			delete(members, x.Remove.Name)
		case *nanashiv1.MemberEdit_Move:
			if err := exists(x.Move.Name); err != nil {
				return nil, nil, err
			}
			ops = append(ops, newOp("move_member", list, x.Move.Name, x.Move.Position))
		default:
			return nil, nil, errors.New("メンバーの変更が空")
		}
	}
	return ops, stmts, nil
}

// textValue sets the TEXT property value of one member. A blank value removes the value of the member.
func textValue(app, list, prop, member, value string) stmt {
	if value == "" {
		return stmt{"update app_property set text_values = text_values - $4 where app_id = $1 and list = $2 and name = $3",
			[]any{app, list, prop, member}}
	}
	return stmt{"update app_property set text_values = jsonb_set(text_values, array[$4], to_jsonb($5::text)) where app_id = $1 and list = $2 and name = $3",
		[]any{app, list, prop, member, value}}
}

func parseCSV(text string) (map[string]int, [][]string, error) {
	r := csv.NewReader(strings.NewReader(strings.TrimPrefix(text, "\ufeff")))
	r.TrimLeadingSpace = true
	records, err := r.ReadAll()
	if err != nil {
		return nil, nil, fmt.Errorf("CSV を読めない: %w", err)
	}
	if len(records) == 0 {
		return nil, nil, errors.New("CSV に見出しの行がない")
	}
	header := map[string]int{}
	for i, h := range records[0] {
		header[strings.TrimSpace(h)] = i
	}
	return header, records[1:], nil
}

func column(header map[string]int, name string) (int, error) {
	i, ok := header[name]
	if !ok {
		return 0, fmt.Errorf("CSV に列 %q がない", name)
	}
	return i, nil
}

// importListEdits changes CSV rows into member edits: add for a new member, set for an existing member.
// A transaction list without member_column gets new members "1", "2", ... after the largest number.
func importListEdits(text string, li *nanashiv1.ListImport, em engineModel, kind nanashiv1.ListKind) ([]*nanashiv1.MemberEdit, int, error) {
	header, rows, err := parseCSV(text)
	if err != nil {
		return nil, 0, err
	}
	dim, ok := em.dim(li.List)
	if !ok {
		return nil, 0, fmt.Errorf("リスト %s がない", li.List)
	}
	nameCol := -1
	if li.MemberColumn != "" {
		if nameCol, err = column(header, li.MemberColumn); err != nil {
			return nil, 0, err
		}
	} else if kind != nanashiv1.ListKind_LIST_KIND_TRANSACTION {
		return nil, 0, errors.New("メンバーの名前の列を指定する")
	}
	propCols := map[string]int{}
	for prop, col := range li.PropertyColumns {
		if propCols[prop], err = column(header, col); err != nil {
			return nil, 0, err
		}
	}
	members := map[string]bool{}
	next := 1
	for _, m := range dim.Members {
		members[m] = true
		if n, err := strconv.Atoi(m); err == nil && n >= next {
			next = n + 1
		}
	}
	var edits []*nanashiv1.MemberEdit
	for i, row := range rows {
		props := map[string]string{}
		for prop, col := range propCols {
			props[prop] = row[col]
		}
		var name string
		if nameCol < 0 {
			name = strconv.Itoa(next)
			next++
		} else if name = strings.TrimSpace(row[nameCol]); name == "" {
			return nil, 0, fmt.Errorf("%d 行目: メンバーの名前が空", i+2)
		}
		if members[name] {
			edits = append(edits, &nanashiv1.MemberEdit{Edit: &nanashiv1.MemberEdit_Set{Set: &nanashiv1.SetProperties{Name: name, Properties: props}}})
			continue
		}
		members[name] = true
		edits = append(edits, &nanashiv1.MemberEdit{Edit: &nanashiv1.MemberEdit_Add{Add: &nanashiv1.AddMember{Name: name, Properties: props}}})
	}
	return edits, len(rows), nil
}

// importMetricOps changes CSV rows into set_cell operations. Each row gives all dimensions of the Metric.
func importMetricOps(text string, mi *nanashiv1.MetricImport, em engineModel, l limits) ([]op, int, error) {
	header, rows, err := parseCSV(text)
	if err != nil {
		return nil, 0, err
	}
	m, err := em.metric(mi.Metric)
	if err != nil {
		return nil, 0, err
	}
	valueCol, err := column(header, mi.ValueColumn)
	if err != nil {
		return nil, 0, err
	}
	cols := make([]int, len(m.Dims))
	known := make([]map[string]bool, len(m.Dims))
	for i, d := range m.Dims {
		name, ok := mi.DimensionColumns[d]
		if !ok {
			return nil, 0, fmt.Errorf("%s の軸 %s の列を指定する", m.Name, d)
		}
		if cols[i], err = column(header, name); err != nil {
			return nil, 0, err
		}
		dim, _ := em.dim(d)
		known[i] = map[string]bool{}
		for _, x := range dim.Members {
			known[i][x] = true
		}
	}
	var adds, sets []op
	for r, row := range rows {
		coords := map[string]string{}
		kwargs := map[string]any{}
		for i, d := range m.Dims {
			member := strings.TrimSpace(row[cols[i]])
			if !known[i][member] {
				if !mi.AddMembers || member == "" {
					return nil, 0, fmt.Errorf("%d 行目: %s に %q がない", r+2, d, member)
				}
				known[i][member] = true
				adds = append(adds, newOp("add_member", d, member))
			}
			coords[d], kwargs[d] = member, member
		}
		if err := l.checkWrite(m.Name, m.Dims, coords); err != nil {
			return nil, 0, fmt.Errorf("%d 行目: %w", r+2, err)
		}
		v, err := parseValue(m.Kind, row[valueCol])
		if err != nil {
			return nil, 0, fmt.Errorf("%d 行目: %w", r+2, err)
		}
		sets = append(sets, newOp("set_cell", m.Name, v).with(kwargs))
	}
	return append(adds, sets...), len(rows), nil
}

// calendarOps makes the ordered lists Year, Quarter and Month and the properties between them.
func calendarOps(start, years int) ([]op, error) {
	if years < 1 || years > 50 || start < 1900 || start > 2200 {
		return nil, errors.New("暦は 1900 年から 2200 年まで、1 年から 50 年まで")
	}
	var ys, qs, ms []string
	monthQuarter, monthYear, quarterYear := map[string]string{}, map[string]string{}, map[string]string{}
	for y := start; y < start+years; y++ {
		year := strconv.Itoa(y)
		ys = append(ys, year)
		for q := 1; q <= 4; q++ {
			quarter := fmt.Sprintf("%d-Q%d", y, q)
			qs = append(qs, quarter)
			quarterYear[quarter] = year
			for m := 3*q - 2; m <= 3*q; m++ {
				month := fmt.Sprintf("%d-%02d", y, m)
				ms = append(ms, month)
				monthQuarter[month], monthYear[month] = quarter, year
			}
		}
	}
	ordered := map[string]any{"ordered": true}
	return []op{
		newOp("add_dimension", "Year", ys).with(ordered),
		newOp("add_dimension", "Quarter", qs).with(ordered),
		newOp("add_dimension", "Month", ms).with(ordered),
		newOp("add_property", "Month", "Quarter", "Quarter", monthQuarter),
		newOp("add_property", "Month", "Year", "Year", monthYear),
		newOp("add_property", "Quarter", "Year", "Year", quarterYear),
	}, nil
}

// copyCellOps copies the cells of a slice to the member to of the dimension dim. If dim is empty, it sets the cells as they are.
func copyCellOps(metric, dim, to string, cube engineCube) []op {
	var ops []op
	for _, c := range cube.Cells {
		coords := map[string]any{}
		for i, d := range cube.Dims {
			coords[d] = c[i]
		}
		if dim != "" {
			coords[dim] = to
		}
		ops = append(ops, newOp("set_cell", metric, c[len(c)-1]).with(coords))
	}
	return ops
}

// snapshotData is the content of a snapshot. Engine is the body of GET / as the engine sent it.
// Overrides has the cells that override the formula of each overridable Metric.
type snapshotData struct {
	Engine    json.RawMessage               `json:"engine"`
	Inputs    map[string]engineCube         `json:"inputs"`
	Overrides map[string]engineCube         `json:"overrides,omitempty"`
	Kinds     map[string]nanashiv1.ListKind `json:"kinds"`
	Props     []propRow                     `json:"props"`
	Items     []itemRow                     `json:"items"`
}

type itemRow struct {
	Type nanashiv1.ItemType `json:"type"`
	ID   string             `json:"id"`
	Def  json.RawMessage    `json:"def"`
}

// replayOps makes the operations that build the engine model of a snapshot in an empty model.
// ponytail: formulas replay in the engine order.
func replayOps(em engineModel, inputs, overrides map[string]engineCube) []op {
	var ops []op
	for _, d := range em.Dims {
		ops = append(ops, newOp("add_dimension", d.Name, d.Members).with(map[string]any{"ordered": d.Ordered}))
	}
	for _, d := range em.Dims {
		for _, p := range d.Props {
			ops = append(ops, newOp("add_property", d.Name, p.Name, p.Target, p.Values))
		}
	}
	for _, m := range em.Metrics {
		if m.Formula != "" {
			ops = append(ops, newOp("add_formula", m.Name, m.Dims, m.Formula).with(map[string]any{"kind": m.Kind, "overridable": m.Overridable}))
			continue
		}
		cube := inputs[m.Name]
		index := make([]int, len(m.Dims))
		for i, d := range m.Dims {
			index[i] = slices.Index(cube.Dims, d)
		}
		cells := [][]any{}
		for _, c := range cube.Cells {
			coords := make([]any, len(index))
			for i, j := range index {
				coords[i] = c[j]
			}
			cells = append(cells, []any{coords, c[len(c)-1]})
		}
		ops = append(ops, newOp("add_input", m.Name, m.Dims, cells).with(map[string]any{"kind": m.Kind}))
	}
	for _, m := range em.Metrics {
		if cube, ok := overrides[m.Name]; ok {
			ops = append(ops, copyCellOps(m.Name, "", "", cube)...)
		}
	}
	return ops
}

// modelDef builds the lists and Metrics of ModelDef. propCells has the cells of each NUMBER or BOOLEAN property Metric.
func modelDef(em engineModel, meta appMeta, propCells map[string]engineCube, l limits) ([]*nanashiv1.ListDef, []*nanashiv1.MetricDef) {
	hidden := map[string]bool{}
	var lists []*nanashiv1.ListDef
	for _, d := range em.Dims {
		kind, ok := meta.Kinds[d.Name]
		if !ok {
			kind = nanashiv1.ListKind_LIST_KIND_DIMENSION
		}
		ld := &nanashiv1.ListDef{Name: d.Name, Kind: kind}
		values := map[string]map[string]string{} // member -> property -> text
		set := func(member, prop, v string) {
			if values[member] == nil {
				values[member] = map[string]string{}
			}
			values[member][prop] = v
		}
		for _, p := range d.Props {
			ld.Properties = append(ld.Properties, &nanashiv1.PropertyDef{Name: p.Name, Type: nanashiv1.PropertyType_PROPERTY_TYPE_DIMENSION, Target: p.Target})
			for member, v := range p.Values {
				if l.visible(p.Target, v) { // A property value can name a hidden member, for example on a list that refers to itself.
					set(member, p.Name, v)
				}
			}
		}
		for _, p := range meta.Props {
			if p.List != d.Name {
				continue
			}
			ld.Properties = append(ld.Properties, &nanashiv1.PropertyDef{Name: p.Name, Type: p.Type})
			for member, v := range p.Text {
				set(member, p.Name, v)
			}
			name := propMetric(p.List, p.Name)
			hidden[name] = true
			for _, c := range propCells[name].Cells {
				member, _ := c[0].(string)
				set(member, p.Name, formatValue(c[len(c)-1]))
			}
		}
		for _, m := range d.Members {
			if l.visible(d.Name, m) {
				ld.Members = append(ld.Members, &nanashiv1.Member{Name: m, Properties: values[m]})
			}
		}
		lists = append(lists, ld)
	}
	var metrics []*nanashiv1.MetricDef
	for _, m := range em.Metrics {
		if !hidden[m.Name] && !l.hides(m) {
			kind, list := valueKind(m.Kind)
			metrics = append(metrics, &nanashiv1.MetricDef{Name: m.Name, Dimensions: m.Dims, Kind: kind, MemberList: list, Formula: m.Formula, Overridable: m.Overridable})
		}
	}
	return lists, metrics
}

// engineKind gives the engine kind of m: "number", "boolean" or "member:<list>".
func engineKind(em engineModel, m *nanashiv1.MetricDef) (string, error) {
	if m.Kind == nanashiv1.ValueKind_VALUE_KIND_MEMBER {
		if _, ok := em.dim(m.MemberList); !ok {
			return "", fmt.Errorf("リスト %q がない", m.MemberList)
		}
		return "member:" + m.MemberList, nil
	}
	if m.MemberList != "" {
		return "", errors.New("リストはメンバーのメトリックにだけ付ける")
	}
	if m.Kind == nanashiv1.ValueKind_VALUE_KIND_BOOLEAN {
		return "boolean", nil
	}
	return "number", nil
}

// valueKind is the reverse of engineKind.
func valueKind(kind string) (nanashiv1.ValueKind, string) {
	if list, ok := strings.CutPrefix(kind, "member:"); ok {
		return nanashiv1.ValueKind_VALUE_KIND_MEMBER, list
	}
	if kind == "boolean" {
		return nanashiv1.ValueKind_VALUE_KIND_BOOLEAN, ""
	}
	return nanashiv1.ValueKind_VALUE_KIND_NUMBER, ""
}

// metricOp gives the operations that save m with the engine kind. It gives no operation for an input Metric that does not change.
// add_input and add_formula delete the cells of an input Metric, so a change to one needs replace.
func metricOp(em engineModel, m *nanashiv1.MetricDef, kind string, replace bool) ([]op, error) {
	dims := m.Dimensions
	if dims == nil {
		dims = []string{}
	}
	if old, err := em.metric(m.Name); err == nil && old.Formula == "" {
		if m.Formula == "" && old.Kind == kind && slices.Equal(old.Dims, dims) {
			return nil, nil
		}
		if !replace {
			return nil, tag(errPrecondition, "入力メトリック %s を変更すると、入力した値がすべて消える", m.Name)
		}
	}
	if m.Formula == "" {
		return []op{newOp("add_input", m.Name, dims, []any{}).with(map[string]any{"kind": kind})}, nil
	}
	return []op{newOp("add_formula", m.Name, dims, m.Formula).with(map[string]any{"kind": kind, "overridable": m.Overridable})}, nil
}

// visibleComments removes the comments on a cell with a member that l hides.
func visibleComments(comments []*nanashiv1.Comment, l limits) []*nanashiv1.Comment {
	return slices.DeleteFunc(comments, func(c *nanashiv1.Comment) bool {
		for list, member := range c.Cell {
			if !l.visible(list, member) {
				return true
			}
		}
		return false
	})
}
