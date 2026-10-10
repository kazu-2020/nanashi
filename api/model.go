package api

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
	"github.com/kazu-2020/nanashi/api/internal/block"
)

// propRow is a row of app_property. The api keeps the name of each property. The engine keeps the values of a
// DIMENSION property. MetricID is the input Metric that keeps the values of a NUMBER or BOOLEAN property.
type propRow struct {
	ListID   string                 `json:"list_id"`
	ID       string                 `json:"id"`
	Name     string                 `json:"name"`
	Type     nanashiv1.PropertyType `json:"type"`
	MetricID string                 `json:"metric_id,omitempty"`
	Text     map[string]string      `json:"text,omitempty"` // Member id to value, for TEXT only.
}

// metricRow is a row of the Metric catalog app_metric. Owner is empty in the values of a madeRow.
type metricRow struct {
	Description string `json:"description"`
	Folder      string `json:"folder"`
	Owner       string `json:"owner,omitempty"`
}

// appMeta is the api data of an application that the engine does not know.
type appMeta struct {
	Kinds   map[string]nanashiv1.ListKind // By list id.
	Props   []propRow
	Metrics map[string]metricRow // The catalog, by Metric id. It can have rows of deleted Metrics.
}

func (m appMeta) listOfKind(kind nanashiv1.ListKind) (string, bool) {
	for id, k := range m.Kinds {
		if k == kind {
			return id, true
		}
	}
	return "", false
}

// refuseProperty refuses a change to the Metric of a property. GetModel needs that Metric.
func (m appMeta) refuseProperty(metricIDs ...string) error {
	for _, id := range metricIDs {
		if slices.ContainsFunc(m.Props, func(p propRow) bool { return p.MetricID == id }) {
			return tag(errPrecondition, "プロパティの値を持つ Metric なので、変更できない")
		}
	}
	return nil
}

// propKind is the kind of the input Metric that holds a NUMBER or BOOLEAN property.
var propKind = map[nanashiv1.PropertyType]string{
	nanashiv1.PropertyType_PROPERTY_TYPE_NUMBER:  "number",
	nanashiv1.PropertyType_PROPERTY_TYPE_BOOLEAN: "boolean",
}

// propMetric is the name of the input Metric that holds a NUMBER or BOOLEAN property.
func propMetric(list, prop string) string { return list + "." + prop }

// editOps changes member edits into the plan: engine operations, the statements for the TEXT values, and the
// old and new TEXT values for the compensation. The engine keeps the DIMENSION property values and the member
// names. Every reference is an id, so a rename changes no api row.
func editOps(app string, em engineModel, dim engineDim, meta appMeta, edits []*nanashiv1.MemberEdit) (plan, error) {
	members := map[string]string{}
	names := map[string]bool{}
	for _, m := range dim.Members {
		members[m.ID] = m.Name
		names[m.Name] = true
	}
	adds := map[string]bool{}
	for _, e := range edits {
		if a := e.GetAdd(); a != nil {
			adds[a.Id] = true
		}
	}
	props := map[string]propRow{}
	for _, p := range meta.Props {
		if p.ListID == dim.ID {
			props[p.ID] = p
		}
	}
	var p plan
	exists := func(id string) error {
		if _, ok := members[id]; !ok {
			return fmt.Errorf("%s にメンバー %s がない", dim.Name, id)
		}
		return nil
	}
	// values keeps the DIMENSION and TEXT property values until the end, so that one operation or statement sets
	// the values of all members of an import. A nil value removes the value.
	values := map[string]map[string]*string{}
	// send sends the kept values that touch member (all values if member is nil). A removal of a member first
	// sends the values that name it, because the engine then follows the removal.
	send := func(member *string) {
		for _, prop := range slices.Sorted(maps.Keys(values)) {
			touched := map[string]*string{}
			for k, v := range values[prop] {
				if member == nil || k == *member || v != nil && *v == *member {
					touched[k] = v
					delete(values[prop], k)
				}
			}
			if len(touched) == 0 {
				continue
			}
			if _, ok := dim.prop(prop); ok {
				p.ops = append(p.ops, newOp("set_property_values", map[string]any{"dim": dim.ID, "prop": prop, "values": touched}))
			} else {
				old := map[string]*string{}
				for k := range touched {
					old[k] = nil
					if v, ok := props[prop].Text[k]; ok {
						old[k] = &v
					}
				}
				p.stmts = append(p.stmts, textValues(app, dim.ID, prop, touched))
				p.made = append(p.made, madeRow{Table: "app_property_text", ID: prop, ListID: dim.ID, OldText: old, NewText: touched})
			}
		}
	}
	setProps := func(member string, in map[string]string) error {
		for _, prop := range slices.Sorted(maps.Keys(in)) {
			v := strings.TrimSpace(in[prop])
			pr, known := props[prop]
			ep, isDim := dim.prop(prop)
			switch kind, isMetric := propKind[pr.Type]; {
			case isDim, known && pr.Type == nanashiv1.PropertyType_PROPERTY_TYPE_TEXT:
				if isDim && v != "" {
					// The value is a member id of the target list. On a list that refers to itself, the member can be
					// one that an edit of this request adds, also a later edit (an import in any row order).
					if err := checkID(ep.Name, v); err != nil {
						return err
					}
					target, _ := em.dim(ep.Target)
					_, inTarget := target.member(v)
					_, inEdit := members[v]
					if !inTarget && !(ep.Target == dim.ID && (inEdit || adds[v])) {
						return fmt.Errorf("%s に %s がない", target.Name, v)
					}
				}
				if values[prop] == nil {
					values[prop] = map[string]*string{}
				}
				values[prop][member] = nil
				if v != "" {
					values[prop][member] = &v
				}
			case isMetric:
				val, err := parseValue(kind, v)
				if err != nil {
					return fmt.Errorf("%s.%s: %w", dim.Name, pr.Name, err)
				}
				p.ops = append(p.ops, newOp("set_cell", map[string]any{"metric": pr.MetricID, "value": val, "coords": map[string]any{dim.ID: member}}))
			default:
				return fmt.Errorf("%s にプロパティ %s がない", dim.Name, prop)
			}
		}
		return nil
	}
	for _, e := range edits {
		switch x := e.Edit.(type) {
		case *nanashiv1.MemberEdit_Add:
			name := strings.TrimSpace(x.Add.Name)
			if name == "" || x.Add.Id == "" {
				return plan{}, errors.New("メンバーの id と名前が要る")
			}
			if _, ok := members[x.Add.Id]; ok || names[name] {
				return plan{}, tag(errExists, "%s にメンバー %q はすでにある", dim.Name, name)
			}
			p.ops = append(p.ops, newOp("add_member", map[string]any{"dim": dim.ID, "id": x.Add.Id, "name": name}))
			members[x.Add.Id], names[name] = name, true
			if err := setProps(x.Add.Id, x.Add.Properties); err != nil {
				return plan{}, err
			}
		case *nanashiv1.MemberEdit_Set:
			if err := exists(x.Set.Id); err != nil {
				return plan{}, err
			}
			if err := setProps(x.Set.Id, x.Set.Properties); err != nil {
				return plan{}, err
			}
		case *nanashiv1.MemberEdit_Rename:
			name := strings.TrimSpace(x.Rename.Name)
			if err := exists(x.Rename.Id); err != nil {
				return plan{}, err
			}
			if name == "" || names[name] {
				return plan{}, fmt.Errorf("%s に %q という名前は付けられない", dim.Name, name)
			}
			p.ops = append(p.ops, newOp("rename_member", map[string]any{"dim": dim.ID, "id": x.Rename.Id, "name": name}))
			delete(names, members[x.Rename.Id])
			members[x.Rename.Id], names[name] = name, true
		case *nanashiv1.MemberEdit_Remove:
			if err := exists(x.Remove.Id); err != nil {
				return plan{}, err
			}
			send(&x.Remove.Id)
			p.ops = append(p.ops, newOp("remove_member", map[string]any{"dim": dim.ID, "id": x.Remove.Id}))
			delete(names, members[x.Remove.Id])
			delete(members, x.Remove.Id)
		case *nanashiv1.MemberEdit_Move:
			if err := exists(x.Move.Id); err != nil {
				return plan{}, err
			}
			p.ops = append(p.ops, newOp("move_member", map[string]any{"dim": dim.ID, "id": x.Move.Id, "at": x.Move.Position}))
		default:
			return plan{}, errors.New("メンバーの変更が空")
		}
	}
	send(nil)
	return p, nil
}

// textValues gives the statement that sets the TEXT property values of some members. A nil value removes the
// value of the member.
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
	return stmt{sql: "update app_property set text_values = (text_values - $4::text[]) || $5::jsonb where app_id = $1 and list_id = $2 and id = $3",
		args: []any{app, list, prop, removed, textJSON(set)}}
}

// calendar is the plan of a calendar: the ids come from newID, one time for each plan.
type calendar struct {
	ops   []op
	lists []string // The ids of Year, Quarter and Month.
}

// calendarOps makes the ordered lists Year, Quarter and Month and the properties between them.
func calendarOps(start, years int) (calendar, error) {
	if years < 1 || years > 50 || start < 1900 || start > 2200 {
		return calendar{}, errors.New("暦は 1900 年から 2200 年まで、1 年から 50 年まで")
	}
	ids := [3]string{newID(), newID(), newID()}
	var out calendar
	out.lists = ids[:]
	for i, name := range []string{"Year", "Quarter", "Month"} {
		out.ops = append(out.ops, newOp("add_dimension", map[string]any{"id": ids[i], "name": name, "ordered": true}))
	}
	member := func(dim int, name string) string {
		id := newID()
		out.ops = append(out.ops, newOp("add_member", map[string]any{"dim": ids[dim], "id": id, "name": name}))
		return id
	}
	monthQuarter, monthYear, quarterYear := map[string]*string{}, map[string]*string{}, map[string]*string{}
	for y := start; y < start+years; y++ {
		year := member(0, strconv.Itoa(y))
		for q := 1; q <= 4; q++ {
			quarter := member(1, fmt.Sprintf("%d-Q%d", y, q))
			quarterYear[quarter] = &year
			for m := 3*q - 2; m <= 3*q; m++ {
				month := member(2, fmt.Sprintf("%d-%02d", y, m))
				monthQuarter[month], monthYear[month] = &quarter, &year
			}
		}
	}
	for _, p := range []struct {
		dim, target int
		name        string
		values      map[string]*string
	}{{2, 1, "Quarter", monthQuarter}, {2, 0, "Year", monthYear}, {1, 0, "Year", quarterYear}} {
		id := newID()
		out.ops = append(out.ops,
			newOp("add_property", map[string]any{"dim": ids[p.dim], "id": id, "name": p.name, "target": ids[p.target]}),
			newOp("set_property_values", map[string]any{"dim": ids[p.dim], "prop": id, "values": p.values}))
	}
	return out, nil
}

func calendarStmts(app string, cal calendar) ([]stmt, []madeRow) {
	stmts, made := listKinds(app, nanashiv1.ListKind_LIST_KIND_CALENDAR, cal.lists...)
	for _, o := range cal.ops {
		if o["op"] == "add_property" {
			st, m := propertyStmt(app, o["dim"].(string), o["id"].(string), o["name"].(string), nanashiv1.PropertyType_PROPERTY_TYPE_DIMENSION, "")
			stmts, made = append(stmts, st), append(made, m)
		}
	}
	return stmts, made
}

func listKinds(app string, kind nanashiv1.ListKind, lists ...string) ([]stmt, []madeRow) {
	var stmts []stmt
	var made []madeRow
	for _, list := range lists {
		stmts = append(stmts, stmt{`insert into app_list (app_id, id, kind) values ($1, $2, $3) on conflict do nothing`,
			[]any{app, list, kind}, tag(errExists, "同じ id のリストがすでにある")})
		made = append(made, madeRow{Table: "app_list", ID: list})
	}
	return stmts, made
}

func propertyStmt(app, list, id, name string, typ nanashiv1.PropertyType, metricID string) (stmt, madeRow) {
	var metric *string
	if metricID != "" {
		metric = &metricID
	}
	return stmt{`insert into app_property (app_id, list_id, id, name, type, metric_id) values ($1, $2, $3, $4, $5, $6) on conflict do nothing`,
		[]any{app, list, id, name, typ, metric}, tag(errExists, "同じ id のプロパティがすでにある")}, madeRow{Table: "app_property", ID: id, ListID: list}
}

// copyCellOps copies the cells of a slice to the member to of the dimension dim. If dim is empty, it sets the
// cells as they are. With override, the cells go to the hidden override input of a formula Metric.
func copyCellOps(metric, dim, to string, cube engineCube, override bool) []op {
	var ops []op
	for _, c := range cube.Cells {
		coords := map[string]any{}
		for i, d := range cube.Dims {
			coords[d] = c[i]
		}
		if dim != "" {
			coords[dim] = to
		}
		o := newOp("set_cell", map[string]any{"metric": metric, "value": c[len(c)-1], "coords": coords})
		if override {
			o["override"] = true
		}
		ops = append(ops, o)
	}
	return ops
}

// modelDef builds the lists and Metrics of ModelDef. propCells has the cells of each NUMBER or BOOLEAN property
// Metric, by Metric id. A property whose engine object is missing (a pending change) is left out.
func modelDef(em engineModel, meta appMeta, propCells map[string]engineCube) ([]*nanashiv1.ListDef, []*nanashiv1.MetricDef) {
	hidden := map[string]bool{}
	var lists []*nanashiv1.ListDef
	for _, d := range em.Dims {
		kind, ok := meta.Kinds[d.ID]
		if !ok {
			kind = nanashiv1.ListKind_LIST_KIND_DIMENSION
		}
		ld := &nanashiv1.ListDef{Id: d.ID, Name: d.Name, Kind: kind}
		values := map[string]map[string]string{} // member -> property -> value
		set := func(member, prop, v string) {
			if values[member] == nil {
				values[member] = map[string]string{}
			}
			values[member][prop] = v
		}
		for _, p := range meta.Props {
			if p.ListID != d.ID {
				continue
			}
			def := &nanashiv1.PropertyDef{Id: p.ID, Name: p.Name, Type: p.Type}
			switch p.Type {
			case nanashiv1.PropertyType_PROPERTY_TYPE_DIMENSION:
				ep, ok := d.prop(p.ID)
				if !ok {
					continue
				}
				def.Target = ep.Target
				for member, v := range ep.Values {
					set(member, p.ID, v)
				}
			case nanashiv1.PropertyType_PROPERTY_TYPE_TEXT:
				for member, v := range p.Text {
					set(member, p.ID, v)
				}
			default:
				if _, ok := em.metric(p.MetricID); !ok {
					continue
				}
				hidden[p.MetricID] = true
				for _, c := range propCells[p.MetricID].Cells {
					member, _ := c[0].(string)
					set(member, p.ID, formatValue(c[len(c)-1]))
				}
			}
			ld.Properties = append(ld.Properties, def)
		}
		for _, m := range d.Members {
			ld.Members = append(ld.Members, &nanashiv1.Member{Id: m.ID, Name: m.Name, Properties: values[m.ID]})
		}
		lists = append(lists, ld)
	}
	var metrics []*nanashiv1.MetricDef
	for _, m := range em.Metrics {
		if !hidden[m.ID] {
			kind, list := valueKind(m.Kind)
			c := meta.Metrics[m.ID] // A Metric without a catalog row has empty values.
			metrics = append(metrics, &nanashiv1.MetricDef{Id: m.ID, Name: m.Name, Dimensions: m.Dims, Kind: kind, MemberList: list, Formula: m.Formula, Overridable: m.Overridable,
				Description: c.Description, Folder: c.Folder, Owner: c.Owner})
		}
	}
	return lists, metrics
}

// pruneItems removes the references that the model does not have from the tables, views and boards: a deleted
// Metric or list, or a deleted view in a widget. The rows do not change (the reader rules of issue #6).
func pruneItems(out *nanashiv1.ModelDef, em engineModel) {
	hasMetric := func(id string) bool { _, ok := em.metric(id); return ok }
	hasDim := func(id string) bool { _, ok := em.dim(id); return ok }
	keep := func(ids []string, has func(string) bool) []string {
		return slices.DeleteFunc(ids, func(id string) bool { return !has(id) })
	}
	for _, t := range out.Tables {
		t.Metrics = keep(t.Metrics, hasMetric)
	}
	for _, v := range out.Views {
		v.Metrics, v.Rows, v.Columns = keep(v.Metrics, hasMetric), keep(v.Rows, hasDim), keep(v.Columns, hasDim)
	}
	views := map[string]bool{}
	for _, v := range out.Views {
		views[v.Id] = true
	}
	for _, b := range out.Boards {
		b.PageSelectors = keep(b.PageSelectors, hasDim)
		b.Widgets = slices.DeleteFunc(b.Widgets, func(w *nanashiv1.Widget) bool {
			return w.GetViewId() != "" && !views[w.GetViewId()]
		})
	}
}

// renameListPlan renames the list id. The Metric "<list>.<property>" of each NUMBER or BOOLEAN property gets the
// new list name. The api keeps no list name, so no api row changes.
func renameListPlan(em engineModel, meta appMeta, id string, name block.Name) (plan, error) {
	if _, ok := em.dim(id); !ok {
		return plan{}, tag(errNotFound, "リスト %s がない", id)
	}
	ops := []op{newOp("rename_dimension", map[string]any{"id": id, "name": name.String()})}
	for _, p := range meta.Props {
		if _, ok := em.metric(p.MetricID); p.ListID == id && p.MetricID != "" && ok {
			ops = append(ops, newOp("rename_metric", map[string]any{"id": p.MetricID, "name": propMetric(name.String(), p.Name)}))
		}
	}
	return plan{ops: ops}, nil
}

// renamePropertyPlan renames the property id of the list in app_property. The engine also gets the new name: as
// a property for a DIMENSION property, in the Metric name for a NUMBER or BOOLEAN property. A TEXT property has no
// engine object.
func renamePropertyPlan(app string, em engineModel, meta appMeta, list, id, name string) (plan, error) {
	dim, ok := em.dim(list)
	if !ok {
		return plan{}, tag(errNotFound, "リスト %s がない", list)
	}
	i := slices.IndexFunc(meta.Props, func(p propRow) bool { return p.ListID == list && p.ID == id })
	if i < 0 {
		return plan{}, tag(errNotFound, "%s にプロパティ %s がない", dim.Name, id)
	}
	prop := meta.Props[i]
	if slices.ContainsFunc(meta.Props, func(p propRow) bool { return p.ListID == list && p.ID != id && p.Name == name }) {
		return plan{}, tag(errExists, "%s にプロパティ %s はすでにある", dim.Name, name)
	}
	out := plan{
		stmts: []stmt{{sql: "update app_property set name = $4 where app_id = $1 and list_id = $2 and id = $3",
			args: []any{app, list, id, name}, zero: tag(errNotFound, "%s にプロパティ %s がない", dim.Name, id)}},
		made: []madeRow{{Table: "app_property_name", ID: id, ListID: list, OldName: prop.Name, NewName: name}},
	}
	if _, ok := em.metric(prop.MetricID); prop.MetricID != "" && ok {
		out.ops = []op{newOp("rename_metric", map[string]any{"id": prop.MetricID, "name": propMetric(dim.Name, name)})}
	} else if prop.Type == nanashiv1.PropertyType_PROPERTY_TYPE_DIMENSION {
		out.ops = []op{newOp("rename_property", map[string]any{"dim": list, "id": id, "name": name})}
	}
	return out, nil
}

// The actions follow.

type querier interface {
	Query(ctx context.Context, sql string, args ...any) (pgx.Rows, error)
}

func metaIn(ctx context.Context, q querier, app string) (appMeta, error) {
	meta := appMeta{Kinds: map[string]nanashiv1.ListKind{}, Metrics: map[string]metricRow{}}
	rows, _ := q.Query(ctx, "select id, kind from app_list where app_id = $1", app)
	var id string
	var kind int32
	if _, err := pgx.ForEachRow(rows, []any{&id, &kind}, func() error {
		meta.Kinds[id] = nanashiv1.ListKind(kind)
		return nil
	}); err != nil {
		return meta, dbError(err)
	}
	rows, _ = q.Query(ctx, "select list_id, id, name, type, coalesce(metric_id::text, ''), text_values from app_property where app_id = $1 order by ord", app)
	props, err := pgx.CollectRows(rows, func(row pgx.CollectableRow) (propRow, error) {
		var p propRow
		return p, row.Scan(&p.ListID, &p.ID, &p.Name, &p.Type, &p.MetricID, &p.Text)
	})
	if err != nil {
		return meta, dbError(err)
	}
	meta.Props = props
	rows, _ = q.Query(ctx, "select metric_id, description, folder, owner from app_metric where app_id = $1", app)
	var c metricRow
	if _, err := pgx.ForEachRow(rows, []any{&id, &c.Description, &c.Folder, &c.Owner}, func() error {
		meta.Metrics[id] = c
		return nil
	}); err != nil {
		return meta, dbError(err)
	}
	return meta, nil
}

func (s *PlanServer) GetModel(ctx context.Context, req *connect.Request[nanashiv1.GetModelRequest]) (*connect.Response[nanashiv1.ModelDef], error) {
	app := req.Msg.AppId
	em, _, err := s.Engines.model(ctx, app)
	if err != nil {
		return nil, err
	}
	meta, err := metaIn(ctx, s.Pool, app)
	if err != nil {
		return nil, err
	}
	propCells := map[string]engineCube{}
	for _, p := range meta.Props {
		if _, ok := em.metric(p.MetricID); p.MetricID != "" && ok {
			if propCells[p.MetricID], err = s.Engines.read(ctx, app, engineRead{Metric: p.MetricID, Path: "slice"}); err != nil {
				return nil, err
			}
		}
	}
	out := &nanashiv1.ModelDef{}
	out.Lists, out.Metrics = modelDef(em, meta, propCells)
	items, err := itemsIn(ctx, s.Pool, app)
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
	pruneItems(out, em)
	return connect.NewResponse(out), nil
}

func (s *PlanServer) CreateList(ctx context.Context, req *connect.Request[nanashiv1.CreateListRequest]) (*ack, error) {
	m := req.Msg
	if m.Kind != nanashiv1.ListKind_LIST_KIND_DIMENSION && m.Kind != nanashiv1.ListKind_LIST_KIND_TRANSACTION {
		return nil, invalid(errors.New("作れるリストは DIMENSION か TRANSACTION"))
	}
	if m.Id == "" {
		return nil, invalid(errors.New("リストの id が要る"))
	}
	n, err := block.ParseName(m.Name)
	if err != nil {
		return nil, invalid(err)
	}
	name := n.String()
	return ackOf(s.change(ctx, m.AppId, m, func(em engineModel, _ appMeta) (plan, error) {
		if _, ok := em.dim(m.Id); ok {
			return plan{}, tag(errExists, "リスト %s はすでにある", name)
		}
		ops := []op{newOp("add_dimension", map[string]any{"id": m.Id, "name": name})}
		for _, x := range m.Members {
			if x.Id == "" || strings.TrimSpace(x.Name) == "" {
				return plan{}, errors.New("メンバーの id と名前が要る")
			}
			ops = append(ops, newOp("add_member", map[string]any{"dim": m.Id, "id": x.Id, "name": strings.TrimSpace(x.Name)}))
		}
		stmts, made := listKinds(m.AppId, m.Kind, m.Id)
		return plan{ops: ops, stmts: stmts, made: made}, nil
	}))
}

func (s *PlanServer) AddProperty(ctx context.Context, req *connect.Request[nanashiv1.AddPropertyRequest]) (*ack, error) {
	app, list, p := req.Msg.AppId, req.Msg.List, req.Msg.Property
	if p == nil || strings.TrimSpace(p.Name) == "" || p.Id == "" {
		return nil, invalid(errors.New("プロパティの id と名前が要る"))
	}
	name := strings.TrimSpace(p.Name)
	return ackOf(s.change(ctx, app, req.Msg, func(em engineModel, meta appMeta) (plan, error) {
		dim, found := em.dim(list)
		if !found {
			return plan{}, fmt.Errorf("リスト %s がない", list)
		}
		if slices.ContainsFunc(meta.Props, func(x propRow) bool { return x.ListID == list && (x.ID == p.Id || x.Name == name) }) {
			return plan{}, tag(errExists, "%s にプロパティ %s はすでにある", dim.Name, name)
		}
		kind, isMetric := propKind[p.Type]
		switch {
		case p.Type == nanashiv1.PropertyType_PROPERTY_TYPE_TEXT:
			// A TEXT property has no engine object, so the plan has no operation.
			st, made := propertyStmt(app, list, p.Id, name, p.Type, "")
			return plan{stmts: []stmt{st}, made: []madeRow{made}}, nil
		case p.Type == nanashiv1.PropertyType_PROPERTY_TYPE_DIMENSION:
			if _, ok := em.dim(p.Target); !ok {
				return plan{}, fmt.Errorf("対象のリスト %s がない", p.Target)
			}
			st, made := propertyStmt(app, list, p.Id, name, p.Type, "")
			return plan{ops: []op{newOp("add_property", map[string]any{"dim": list, "id": p.Id, "name": name, "target": p.Target})},
				stmts: []stmt{st}, made: []madeRow{made}}, nil
		case !isMetric:
			return plan{}, errors.New("プロパティの型を指定する")
		}
		// The Metric gets an id here, one time for each plan. The engine refuses its name if a Metric of the user has it.
		metric := newID()
		st, made := propertyStmt(app, list, p.Id, name, p.Type, metric)
		return plan{ops: []op{newOp("add_input", map[string]any{"id": metric, "name": propMetric(dim.Name, name), "dims": []string{list}, "kind": kind, "cells": []any{}})},
			stmts: []stmt{st}, made: []madeRow{made}}, nil
	}))
}

func (s *PlanServer) RenameList(ctx context.Context, req *connect.Request[nanashiv1.RenameListRequest]) (*ack, error) {
	name, err := block.ParseName(req.Msg.Name)
	if err != nil {
		return nil, invalid(err)
	}
	return ackOf(s.change(ctx, req.Msg.AppId, req.Msg, func(em engineModel, meta appMeta) (plan, error) {
		return renameListPlan(em, meta, req.Msg.Id, name)
	}))
}

func (s *PlanServer) RenameProperty(ctx context.Context, req *connect.Request[nanashiv1.RenamePropertyRequest]) (*ack, error) {
	m, name := req.Msg, strings.TrimSpace(req.Msg.Name)
	if name == "" {
		return nil, invalid(errors.New("プロパティの名前が空"))
	}
	return ackOf(s.change(ctx, m.AppId, m, func(em engineModel, meta appMeta) (plan, error) {
		return renamePropertyPlan(m.AppId, em, meta, m.List, m.Id, name)
	}))
}

func (s *PlanServer) EditMembers(ctx context.Context, req *connect.Request[nanashiv1.EditMembersRequest]) (*ack, error) {
	app, list := req.Msg.AppId, req.Msg.List
	return ackOf(s.change(ctx, app, req.Msg, func(em engineModel, meta appMeta) (plan, error) {
		dim, ok := em.dim(list)
		if !ok {
			return plan{}, fmt.Errorf("リスト %s がない", list)
		}
		return editOps(app, em, dim, meta, req.Msg.Edits)
	}))
}

func (s *PlanServer) CreateCalendar(ctx context.Context, req *connect.Request[nanashiv1.CreateCalendarRequest]) (*ack, error) {
	app := req.Msg.AppId
	return ackOf(s.change(ctx, app, req.Msg, func(_ engineModel, meta appMeta) (plan, error) {
		if _, ok := meta.listOfKind(nanashiv1.ListKind_LIST_KIND_CALENDAR); ok {
			return plan{}, tag(errExists, "カレンダーはすでにある")
		}
		cal, err := calendarOps(int(req.Msg.StartYear), int(req.Msg.Years))
		if err != nil {
			return plan{}, err
		}
		stmts, made := calendarStmts(app, cal)
		return plan{ops: cal.ops, stmts: stmts, made: made}, nil
	}))
}

func (s *PlanServer) CreateScenario(ctx context.Context, req *connect.Request[nanashiv1.CreateScenarioRequest]) (*ack, error) {
	app, name, from := req.Msg.AppId, strings.TrimSpace(req.Msg.Name), req.Msg.CopyFrom
	if name == "" || req.Msg.Id == "" {
		return nil, invalid(errors.New("シナリオの id と名前が要る"))
	}
	return ackOf(s.change(ctx, app, req.Msg, func(em engineModel, meta appMeta) (plan, error) {
		list, found := meta.listOfKind(nanashiv1.ListKind_LIST_KIND_SCENARIO)
		if !found {
			if from != "" {
				return plan{}, fmt.Errorf("シナリオ %s がない", from)
			}
			// The list gets an id here, one time for each plan.
			list = newID()
			stmts, made := listKinds(app, nanashiv1.ListKind_LIST_KIND_SCENARIO, list)
			return plan{ops: []op{newOp("add_dimension", map[string]any{"id": list, "name": "Scenario"}),
				newOp("add_member", map[string]any{"dim": list, "id": req.Msg.Id, "name": name})}, stmts: stmts, made: made}, nil
		}
		dim, _ := em.dim(list)
		if _, ok := dim.member(req.Msg.Id); ok {
			return plan{}, tag(errExists, "シナリオ %s はすでにある", name)
		}
		ops := []op{newOp("add_member", map[string]any{"dim": list, "id": req.Msg.Id, "name": name})}
		if from == "" {
			return plan{ops: ops}, nil
		}
		if _, ok := dim.member(from); !ok {
			return plan{}, fmt.Errorf("シナリオ %s がない", from)
		}
		// The copy reads the cells after the model. If a write came in between, a cube has another seq than the
		// model: plan again with the new model (errConflict in outbox). Nobody else writes the new scenario, so the
		// engine cannot see a conflict for this write.
		cubes := map[string]engineCube{}
		for _, m := range em.Metrics {
			if m.Formula != "" || !slices.Contains(m.Dims, list) {
				continue
			}
			cube, err := s.Engines.read(ctx, app, engineRead{Metric: m.ID, Path: "slice", Query: map[string][]string{list: {from}}})
			if err != nil {
				return plan{}, err
			}
			cubes[m.ID] = cube
			ops = append(ops, copyCellOps(m.ID, list, req.Msg.Id, cube, false)...)
		}
		if !sameSeq(em, cubes) {
			return plan{}, errConflict
		}
		return plan{ops: ops}, nil
	}))
}
