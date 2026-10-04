package api

// This file holds the lists, the properties and the members of a model.

import (
	"context"
	"errors"
	"fmt"
	"maps"
	"slices"
	"strconv"
	"strings"

	"connectrpc.com/connect"
	"github.com/jackc/pgx/v5"
	"google.golang.org/protobuf/encoding/protojson"

	nanashiv1 "github.com/kazu-2020/nanashi/api/gen/nanashi/v1"
)

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

// memberRenames gives the statements that rename a member in the api tables, or remove it when name is nil.
func memberRenames(app, list, old string, name *string) []stmt {
	return statements(memberRefs, app, list, old, name)
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
	targets := map[string]string{} // DIMENSION property to its target list.
	for _, p := range dim.Props {
		targets[p.Name] = p.Target
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
	// values keeps the DIMENSION and TEXT property values until the end, so that one operation or statement sets
	// the values of all members of an import. A nil value removes the value.
	values := map[string]map[string]*string{}
	// send sends the kept values that touch member (all values if member is nil). A rename or a removal of a member
	// first sends the values that name it, because the engine and memberRefs then follow the rename or the removal.
	send := func(member *string) {
		for _, prop := range slices.Sorted(maps.Keys(values)) {
			touched := map[string]*string{}
			for k, v := range values[prop] {
				if member == nil || k == *member || targets[prop] == list && v != nil && *v == *member {
					touched[k] = v
					delete(values[prop], k)
				}
			}
			if len(touched) == 0 {
				continue
			}
			if _, ok := targets[prop]; ok {
				ops = append(ops, newOp("set_property_values", list, prop, touched))
			} else {
				stmts = append(stmts, textValues(app, list, prop, touched))
			}
		}
	}
	setProps := func(member string, props map[string]string) error {
		for _, prop := range slices.Sorted(maps.Keys(props)) {
			v := strings.TrimSpace(props[prop])
			switch kind, isMetric := propKind[types[prop]]; {
			case targets[prop] != "" || types[prop] == nanashiv1.PropertyType_PROPERTY_TYPE_TEXT:
				if values[prop] == nil {
					values[prop] = map[string]*string{}
				}
				values[prop][member] = nil // A blank value removes the value of the member.
				if v != "" {
					values[prop][member] = &v
				}
			case isMetric:
				val, err := parseValue(kind, v)
				if err != nil {
					return fmt.Errorf("%s.%s: %w", list, prop, err)
				}
				ops = append(ops, newOp("set_cell", propMetric(list, prop), val).with(map[string]any{list: member}))
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
			send(&old)
			ops = append(ops, newOp("rename_member", list, old, name))
			stmts = append(stmts, memberRenames(app, list, old, &name)...)
			delete(members, old)
			members[name] = true
		case *nanashiv1.MemberEdit_Remove:
			if err := exists(x.Remove.Name); err != nil {
				return nil, nil, err
			}
			send(&x.Remove.Name)
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
	send(nil)
	return ops, stmts, nil
}

// textValues sets the TEXT property values of some members. A nil value removes the value of the member.
func textValues(app, list, prop string, values map[string]*string) stmt {
	removed, set := []string{}, map[string]string{}
	for member, v := range values {
		if v == nil {
			removed = append(removed, member)
		} else {
			set[member] = *v
		}
	}
	slices.Sort(removed)
	return stmt{"update app_property set text_values = (text_values - $4::text[]) || $5::jsonb where app_id = $1 and list = $2 and name = $3",
		[]any{app, list, prop, removed, textJSON(set)}}
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

const scenarioList = "Scenario"

// The actions follow. They do I/O.

func (s *PlanServer) meta(ctx context.Context, app string) (appMeta, error) {
	meta := appMeta{Kinds: map[string]nanashiv1.ListKind{}}
	rows, _ := s.Pool.Query(ctx, "select name, kind from app_list where app_id = $1", app)
	var name string
	var kind int32
	if _, err := pgx.ForEachRow(rows, []any{&name, &kind}, func() error {
		meta.Kinds[name] = nanashiv1.ListKind(kind)
		return nil
	}); err != nil {
		return meta, dbError(err)
	}
	rows, _ = s.Pool.Query(ctx, "select list, name, type, text_values from app_property where app_id = $1 order by ord", app)
	props, err := pgx.CollectRows(rows, func(row pgx.CollectableRow) (propRow, error) {
		var p propRow
		return p, row.Scan(&p.List, &p.Name, &p.Type, &p.Text)
	})
	if err != nil {
		return meta, dbError(err)
	}
	meta.Props = props
	return meta, nil
}

func (s *PlanServer) GetModel(ctx context.Context, req *connect.Request[nanashiv1.GetModelRequest]) (*connect.Response[nanashiv1.ModelDef], error) {
	app, c := req.Msg.AppId, callerOf(ctx)
	em, _, err := s.Engines.model(ctx, app)
	if err != nil {
		return nil, err
	}
	meta, err := s.meta(ctx, app)
	if err != nil {
		return nil, err
	}
	propCells := map[string]engineCube{}
	for _, p := range meta.Props {
		if p.Type != nanashiv1.PropertyType_PROPERTY_TYPE_TEXT {
			name := propMetric(p.List, p.Name)
			if propCells[name], err = s.Engines.read(ctx, app, engineRead{Metric: name, Path: "slice"}); err != nil {
				return nil, err
			}
		}
	}
	out := &nanashiv1.ModelDef{Role: c.role}
	out.Lists, out.Metrics = modelDef(em, meta, propCells, c.limitsIn(em))
	items, err := s.items(ctx, app)
	if err != nil {
		return nil, err
	}
	for _, it := range items {
		// The definition can come from a snapshot of another application, so set app_id and id again.
		switch it.Type {
		case nanashiv1.ItemType_ITEM_TYPE_TABLE:
			t := &nanashiv1.TableDef{}
			err = protojson.Unmarshal(it.Def, t)
			t.AppId, t.Id = app, it.ID
			out.Tables = append(out.Tables, t)
		case nanashiv1.ItemType_ITEM_TYPE_VIEW:
			v := &nanashiv1.ViewDef{}
			err = protojson.Unmarshal(it.Def, v)
			v.AppId, v.Id = app, it.ID
			out.Views = append(out.Views, v)
		default:
			b := &nanashiv1.BoardDef{}
			err = protojson.Unmarshal(it.Def, b)
			b.AppId, b.Id = app, it.ID
			out.Boards = append(out.Boards, b)
		}
		if err != nil {
			return nil, dbError(err)
		}
	}
	return connect.NewResponse(out), nil
}

func (s *PlanServer) CreateList(ctx context.Context, req *connect.Request[nanashiv1.CreateListRequest]) (*ack, error) {
	m := req.Msg
	if m.Kind != nanashiv1.ListKind_LIST_KIND_DIMENSION && m.Kind != nanashiv1.ListKind_LIST_KIND_TRANSACTION {
		return nil, invalid(errors.New("作れるリストは DIMENSION か TRANSACTION"))
	}
	if strings.TrimSpace(m.Name) == "" {
		return nil, invalid(errors.New("リストの名前が空"))
	}
	members := m.Members
	if members == nil {
		members = []string{}
	}
	return s.change(ctx, m.AppId, func(engineModel, appMeta) (plan, error) {
		return plan{[]op{newOp("add_dimension", m.Name, members)}, listKinds(m.AppId, m.Kind, m.Name)}, nil
	})
}

func (s *PlanServer) AddProperty(ctx context.Context, req *connect.Request[nanashiv1.AddPropertyRequest]) (*ack, error) {
	app, list, p := req.Msg.AppId, req.Msg.List, req.Msg.Property
	if p == nil || strings.TrimSpace(p.Name) == "" {
		return nil, invalid(errors.New("プロパティの名前が空"))
	}
	return s.change(ctx, app, func(em engineModel, meta appMeta) (plan, error) {
		dim, found := em.dim(list)
		if !found {
			return plan{}, fmt.Errorf("リスト %s がない", list)
		}
		// The engine knows only the DIMENSION properties. The api tables have the other types.
		exists := slices.ContainsFunc(dim.Props, func(x engineProp) bool { return x.Name == p.Name }) ||
			slices.ContainsFunc(meta.Props, func(x propRow) bool { return x.List == list && x.Name == p.Name })
		if exists {
			return plan{}, tag(errExists, "%s にプロパティ %s はすでにある", list, p.Name)
		}
		switch kind, isMetric := propKind[p.Type]; {
		case p.Type == nanashiv1.PropertyType_PROPERTY_TYPE_DIMENSION:
			return plan{ops: []op{newOp("add_property", list, p.Name, p.Target, map[string]string{})}}, nil
		case !isMetric && p.Type != nanashiv1.PropertyType_PROPERTY_TYPE_TEXT:
			return plan{}, errors.New("プロパティの型を指定する")
		default:
			// The property Metric must not replace a Metric of the user. add_input replaces the cells.
			if _, err := em.metric(propMetric(list, p.Name)); err == nil {
				return plan{}, tag(errExists, "Metric %s があるので、プロパティ %s を作れない", propMetric(list, p.Name), p.Name)
			}
			out := plan{stmts: []stmt{{"insert into app_property (app_id, list, name, type) values ($1, $2, $3, $4)", []any{app, list, p.Name, p.Type}}}}
			if isMetric {
				out.ops = []op{newOp("add_input", propMetric(list, p.Name), []string{list}, []any{}).with(map[string]any{"kind": kind})}
			}
			return out, nil
		}
	})
}

func (s *PlanServer) EditMembers(ctx context.Context, req *connect.Request[nanashiv1.EditMembersRequest]) (*ack, error) {
	app, list := req.Msg.AppId, req.Msg.List
	return s.change(ctx, app, func(em engineModel, meta appMeta) (plan, error) {
		ops, stmts, err := editOps(app, list, em, meta, req.Msg.Edits)
		return plan{ops, stmts}, err
	})
}

func (s *PlanServer) CreateCalendar(ctx context.Context, req *connect.Request[nanashiv1.CreateCalendarRequest]) (*ack, error) {
	app := req.Msg.AppId
	return s.change(ctx, app, func(engineModel, appMeta) (plan, error) {
		ops, err := calendarOps(int(req.Msg.StartYear), int(req.Msg.Years))
		return plan{ops, listKinds(app, nanashiv1.ListKind_LIST_KIND_CALENDAR, "Year", "Quarter", "Month")}, err
	})
}

func (s *PlanServer) CreateScenario(ctx context.Context, req *connect.Request[nanashiv1.CreateScenarioRequest]) (*ack, error) {
	app, name, from := req.Msg.AppId, strings.TrimSpace(req.Msg.Name), req.Msg.CopyFrom
	if name == "" {
		return nil, invalid(errors.New("シナリオの名前が空"))
	}
	return s.change(ctx, app, func(em engineModel, _ appMeta) (plan, error) {
		if _, found := em.dim(scenarioList); !found {
			if from != "" {
				return plan{}, fmt.Errorf("シナリオ %s がない", from)
			}
			return plan{[]op{newOp("add_dimension", scenarioList, []string{name})}, listKinds(app, nanashiv1.ListKind_LIST_KIND_SCENARIO, scenarioList)}, nil
		}
		ops := []op{newOp("add_member", scenarioList, name)}
		if from != "" {
			// The reads are under the lock, so they see the model that the write changes.
			for _, m := range em.Metrics {
				if m.Formula != "" || !slices.Contains(m.Dims, scenarioList) {
					continue
				}
				cube, err := s.Engines.read(ctx, app, engineRead{Metric: m.Name, Path: "slice", Query: map[string][]string{scenarioList: {from}}})
				if err != nil {
					return plan{}, err
				}
				ops = append(ops, copyCellOps(m.Name, scenarioList, name, cube)...)
			}
		}
		return plan{ops: ops}, nil
	})
}
