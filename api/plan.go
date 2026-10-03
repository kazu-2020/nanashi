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
type engineCube struct {
	Dims  []string `json:"dims"`
	Cells [][]any  `json:"cells"`
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
		Seq        int64                      `json:"seq"`
		Dimensions map[string]json.RawMessage `json:"dimensions"`
		Metrics    map[string]struct {
			Dims        []string `json:"dims"`
			Kind        string   `json:"kind"`
			Formula     *string  `json:"formula"`
			Overridable bool     `json:"overridable"`
		} `json:"metrics"`
	}
	if err := json.Unmarshal(body, &raw); err != nil {
		return engineModel{}, err
	}
	// Go maps lose the key order. The order of dimensions and Metrics is significant (display, replay).
	var top struct {
		Dimensions json.RawMessage `json:"dimensions"`
		Metrics    json.RawMessage `json:"metrics"`
	}
	if err := json.Unmarshal(body, &top); err != nil {
		return engineModel{}, err
	}
	out := engineModel{Seq: raw.Seq}
	dimNames, err := objectKeys(top.Dimensions)
	if err != nil {
		return engineModel{}, err
	}
	for _, name := range dimNames {
		var d struct {
			Members        []string                     `json:"members"`
			Ordered        bool                         `json:"ordered"`
			Properties     json.RawMessage              `json:"properties"`
			PropertyValues map[string]map[string]string `json:"property_values"`
		}
		if err := json.Unmarshal(raw.Dimensions[name], &d); err != nil {
			return engineModel{}, err
		}
		var targets map[string]string
		if err := json.Unmarshal(d.Properties, &targets); err != nil {
			return engineModel{}, err
		}
		propNames, err := objectKeys(d.Properties)
		if err != nil {
			return engineModel{}, err
		}
		dim := engineDim{Name: name, Members: d.Members, Ordered: d.Ordered}
		for _, p := range propNames {
			dim.Props = append(dim.Props, engineProp{Name: p, Target: targets[p], Values: d.PropertyValues[p]})
		}
		out.Dims = append(out.Dims, dim)
	}
	metricNames, err := objectKeys(top.Metrics)
	if err != nil {
		return engineModel{}, err
	}
	for _, name := range metricNames {
		m := raw.Metrics[name]
		out.Metrics = append(out.Metrics, engineMetric{Name: name, Dims: m.Dims, Kind: m.Kind,
			Formula: deref(m.Formula), Overridable: m.Overridable})
	}
	return out, nil
}

func deref(s *string) string {
	if s == nil {
		return ""
	}
	return *s
}

// objectKeys gives the keys of a JSON object in their order.
func objectKeys(raw json.RawMessage) ([]string, error) {
	if len(raw) == 0 {
		return nil, nil
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	if _, err := dec.Token(); err != nil {
		return nil, err
	}
	var keys []string
	for dec.More() {
		t, err := dec.Token()
		if err != nil {
			return nil, err
		}
		keys = append(keys, t.(string))
		var skip json.RawMessage
		if err := dec.Decode(&skip); err != nil {
			return nil, err
		}
	}
	return keys, nil
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

// propMetric is the input Metric that holds a NUMBER or BOOLEAN property.
func propMetric(list, prop string) string { return list + "." + prop }

// ---------------------------------------------------------------- access

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

func intersect(a, b map[string]bool) map[string]bool {
	out := map[string]bool{}
	for k := range a {
		if b[k] {
			out[k] = true
		}
	}
	return out
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
			return fmt.Errorf("%s: 権限の制限がある %s のメンバーを指定せずに書き込めない", metric, d)
		}
		if !lim.write[c] {
			return fmt.Errorf("%s: %s の %q に書き込む権限がない", metric, d, c)
		}
	}
	return nil
}

// ---------------------------------------------------------------- values

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
	switch {
	case kind == "number":
		f, err := strconv.ParseFloat(strings.ReplaceAll(text, ",", ""), 64)
		if err != nil {
			return nil, fmt.Errorf("%q は数値ではない", text)
		}
		return f, nil
	case kind == "boolean":
		b, err := strconv.ParseBool(strings.ToLower(text))
		if err != nil {
			return nil, fmt.Errorf("%q は TRUE か FALSE ではない", text)
		}
		return b, nil
	}
	return text, nil
}

// ---------------------------------------------------------------- query

// engineRead is one read of a Metric from the engine: GET /metrics/<metric>/<path>?<query>.
type engineRead struct {
	Metric string
	Path   string // "summary" for a number Metric, "slice" for other kinds.
	Query  map[string][]string
}

// queryReads makes the engine reads for a QueryRequest. It leaves out a Metric that the filters make empty.
func queryReads(req *nanashiv1.QueryRequest, em engineModel, l limits) ([]engineRead, error) {
	agg := strings.ToLower(req.Aggregation)
	if agg == "" {
		agg = "sum"
	}
	if !slices.Contains([]string{"sum", "avg", "min", "max", "count"}, agg) {
		return nil, fmt.Errorf("集計は SUM、AVG、MIN、MAX、COUNT のいずれか（%q）", req.Aggregation)
	}
	shown := slices.Concat(req.Rows, req.Columns)
	var out []engineRead
next:
	for _, name := range req.Metrics {
		m, err := em.metric(name)
		if err != nil {
			return nil, err
		}
		q := map[string][]string{}
		for _, d := range m.Dims {
			members := req.Filters[d].GetNames()
			if lim, ok := l[d]; ok {
				if len(members) == 0 {
					members = slices.Sorted(maps.Keys(lim.read))
				} else {
					members = slices.DeleteFunc(slices.Clone(members), func(x string) bool { return !lim.read[x] })
				}
				if len(members) == 0 {
					continue next
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
func queryCells(metric string, dims []string, cube engineCube) []*nanashiv1.QueryCell {
	index := make([]int, len(dims))
	for i, d := range dims {
		index[i] = slices.Index(cube.Dims, d)
	}
	seen := map[string]bool{}
	var out []*nanashiv1.QueryCell
	for _, c := range cube.Cells {
		v := toValue(c[len(c)-1])
		if v == nil {
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
		out = append(out, &nanashiv1.QueryCell{Metric: metric, Coords: coords, Value: v})
	}
	return out
}

// ---------------------------------------------------------------- writes

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

// ---------------------------------------------------------------- members

// editOps changes member edits into engine operations. It also gives the new TEXT property values of the list.
func editOps(list string, em engineModel, meta appMeta, edits []*nanashiv1.MemberEdit) ([]op, map[string]map[string]string, error) {
	dim, ok := em.dim(list)
	if !ok {
		return nil, nil, fmt.Errorf("リスト %s がない", list)
	}
	members := map[string]bool{}
	for _, m := range dim.Members {
		members[m] = true
	}
	dimProps := map[string]engineProp{}
	for _, p := range dim.Props {
		p.Values = maps.Clone(p.Values)
		if p.Values == nil {
			p.Values = map[string]string{}
		}
		dimProps[p.Name] = p
	}
	types := map[string]nanashiv1.PropertyType{}
	text := map[string]map[string]string{}
	for _, p := range meta.Props {
		if p.List != list {
			continue
		}
		types[p.Name] = p.Type
		if p.Type == nanashiv1.PropertyType_PROPERTY_TYPE_TEXT {
			text[p.Name] = maps.Clone(p.Text)
			if text[p.Name] == nil {
				text[p.Name] = map[string]string{}
			}
		}
	}
	dirty := map[string]bool{}
	var ops []op
	exists := func(name string) error {
		if !members[name] {
			return fmt.Errorf("%s にメンバー %q がない", list, name)
		}
		return nil
	}
	setProps := func(member string, props map[string]string) error {
		for _, prop := range slices.Sorted(maps.Keys(props)) {
			v := strings.TrimSpace(props[prop])
			if p, ok := dimProps[prop]; ok {
				if v == "" {
					delete(p.Values, member)
				} else {
					p.Values[member] = v
				}
				dirty[prop] = true
				continue
			}
			switch types[prop] {
			case nanashiv1.PropertyType_PROPERTY_TYPE_NUMBER, nanashiv1.PropertyType_PROPERTY_TYPE_BOOLEAN:
				kind := "number"
				if types[prop] == nanashiv1.PropertyType_PROPERTY_TYPE_BOOLEAN {
					kind = "boolean"
				}
				val, err := parseValue(kind, v)
				if err != nil {
					return fmt.Errorf("%s.%s: %w", list, prop, err)
				}
				ops = append(ops, newOp("set_cell", propMetric(list, prop), val).with(map[string]any{list: member}))
			case nanashiv1.PropertyType_PROPERTY_TYPE_TEXT:
				if v == "" {
					delete(text[prop], member)
				} else {
					text[prop][member] = v
				}
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
			delete(members, old)
			members[name] = true
			for _, p := range dimProps {
				renameKey(p.Values, old, name)
				if p.Target == list {
					for k, v := range p.Values {
						if v == old {
							p.Values[k] = name
						}
					}
				}
			}
			for _, t := range text {
				renameKey(t, old, name)
			}
		case *nanashiv1.MemberEdit_Remove:
			if err := exists(x.Remove.Name); err != nil {
				return nil, nil, err
			}
			ops = append(ops, newOp("remove_member", list, x.Remove.Name))
			delete(members, x.Remove.Name)
			for _, p := range dimProps {
				delete(p.Values, x.Remove.Name)
				if p.Target == list {
					maps.DeleteFunc(p.Values, func(_, v string) bool { return v == x.Remove.Name })
				}
			}
			for _, t := range text {
				delete(t, x.Remove.Name)
			}
		case *nanashiv1.MemberEdit_Move:
			if err := exists(x.Move.Name); err != nil {
				return nil, nil, err
			}
			ops = append(ops, newOp("move_member", list, x.Move.Name, x.Move.Position))
		default:
			return nil, nil, errors.New("メンバーの変更が空")
		}
	}
	// add_property replaces the whole mapping, so send the final mapping once for each changed property.
	for _, p := range dim.Props {
		if dirty[p.Name] {
			ops = append(ops, newOp("add_property", list, p.Name, p.Target, dimProps[p.Name].Values))
		}
	}
	return ops, text, nil
}

func renameKey(m map[string]string, old, name string) {
	if v, ok := m[old]; ok {
		delete(m, old)
		m[name] = v
	}
}

// ---------------------------------------------------------------- import

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

// ---------------------------------------------------------------- calendar and scenarios

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

// copyCellOps copies the cells of a slice to the member to of the dimension dim.
func copyCellOps(metric, dim, to string, cube engineCube) []op {
	var ops []op
	for _, c := range cube.Cells {
		coords := map[string]any{}
		for i, d := range cube.Dims {
			coords[d] = c[i]
		}
		coords[dim] = to
		ops = append(ops, newOp("set_cell", metric, c[len(c)-1]).with(coords))
	}
	return ops
}

// ---------------------------------------------------------------- snapshots

// snapshotData is the content of a snapshot. Engine is the body of GET / as the engine sent it.
type snapshotData struct {
	Engine json.RawMessage               `json:"engine"`
	Inputs map[string]engineCube         `json:"inputs"`
	Kinds  map[string]nanashiv1.ListKind `json:"kinds"`
	Props  []propRow                     `json:"props"`
	Items  []itemRow                     `json:"items"`
}

type itemRow struct {
	Type nanashiv1.ItemType `json:"type"`
	ID   string             `json:"id"`
	Def  json.RawMessage    `json:"def"`
}

// replayOps makes the operations that build the engine model of a snapshot in an empty model.
// ponytail: formulas replay in the engine order, and the values that override a formula are not kept.
func replayOps(em engineModel, inputs map[string]engineCube) []op {
	var ops []op
	for _, d := range em.Dims {
		ops = append(ops, newOp("add_dimension", d.Name, d.Members).with(map[string]any{"ordered": d.Ordered}))
	}
	for _, d := range em.Dims {
		for _, p := range d.Props {
			values := p.Values
			if values == nil {
				values = map[string]string{}
			}
			ops = append(ops, newOp("add_property", d.Name, p.Name, p.Target, values))
		}
	}
	for _, m := range em.Metrics {
		kind := map[string]any{"kind": m.Kind}
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
		ops = append(ops, newOp("add_input", m.Name, m.Dims, cells).with(kind))
	}
	return ops
}

// ---------------------------------------------------------------- model

// modelDef builds the lists and Metrics of ModelDef. propCells has the cells of each NUMBER or BOOLEAN property Metric.
func modelDef(em engineModel, meta appMeta, propCells map[string]engineCube, l limits) ([]*nanashiv1.ListDef, []*nanashiv1.MetricDef) {
	hidden := map[string]bool{}
	var lists []*nanashiv1.ListDef
	for _, d := range em.Dims {
		kind, ok := meta.Kinds[d.Name]
		if !ok {
			kind = nanashiv1.ListKind_LIST_KIND_DIMENSION
		}
		ld := &nanashiv1.ListDef{Name: d.Name, Kind: kind, Ordered: d.Ordered}
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
				set(member, p.Name, v)
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
		if !hidden[m.Name] {
			metrics = append(metrics, &nanashiv1.MetricDef{Name: m.Name, Dimensions: m.Dims, Kind: m.Kind, Formula: m.Formula, Overridable: m.Overridable})
		}
	}
	return lists, metrics
}
