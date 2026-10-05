package api

import (
	"context"
	"encoding/csv"
	"encoding/json"
	"errors"
	"fmt"
	"strconv"
	"strings"

	"connectrpc.com/connect"

	nanashiv1 "github.com/kazu-2020/nanashi/api/gen/nanashi/v1"
)

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

func byName(d engineDim) map[string]string {
	out := map[string]string{}
	for _, m := range d.Members {
		out[m.Name] = m.ID
	}
	return out
}

// importListEdits changes CSV rows into member edits: add for a new member, set for an existing member. The api
// makes the id of each new member (newID), one time for each plan. A CSV column of a DIMENSION property has
// member names of the target list; the edit gets the ids.
// A transaction list without member_column gets new members "first", "first+1", ... (see reserveRows).
func importListEdits(text string, li *nanashiv1.ListImport, em engineModel, dim engineDim, meta appMeta, first int) ([]*nanashiv1.MemberEdit, int, error) {
	header, rows, err := parseCSV(text)
	if err != nil {
		return nil, 0, err
	}
	nameCol := -1
	if li.MemberColumn != "" {
		if nameCol, err = column(header, li.MemberColumn); err != nil {
			return nil, 0, err
		}
	} else if meta.Kinds[dim.ID] != nanashiv1.ListKind_LIST_KIND_TRANSACTION {
		return nil, 0, errors.New("メンバーの名前の列を指定する")
	}
	members := byName(dim)
	propCols := map[string]int{}
	targets := map[string]map[string]string{} // DIMENSION property id to the member ids of its target by name.
	for prop, col := range li.PropertyColumns {
		if propCols[prop], err = column(header, col); err != nil {
			return nil, 0, err
		}
		if p, ok := dim.prop(prop); ok && p.Target == dim.ID {
			targets[prop] = members // The members that this import adds are values too.
		} else if ok {
			target, _ := em.dim(p.Target)
			targets[prop] = byName(target)
		}
	}
	// First give each row its member, so a property value can name a member of any row.
	names, adds := make([]string, len(rows)), make([]bool, len(rows))
	next := first
	for i, row := range rows {
		if nameCol < 0 {
			names[i] = strconv.Itoa(next)
			next++
		} else if names[i] = strings.TrimSpace(row[nameCol]); names[i] == "" {
			return nil, 0, fmt.Errorf("%d 行目: メンバーの名前が空", i+2)
		}
		if _, ok := members[names[i]]; !ok {
			members[names[i]], adds[i] = newID(), true
		}
	}
	var edits []*nanashiv1.MemberEdit
	for i, row := range rows {
		props := map[string]string{}
		for prop, col := range propCols {
			v := strings.TrimSpace(row[col])
			if ids, ok := targets[prop]; ok && v != "" {
				id, ok := ids[v]
				if !ok {
					return nil, 0, fmt.Errorf("%d 行目: %q がない", i+2, v)
				}
				v = id
			}
			props[prop] = v
		}
		id := members[names[i]]
		if adds[i] {
			edits = append(edits, &nanashiv1.MemberEdit{Edit: &nanashiv1.MemberEdit_Add{Add: &nanashiv1.AddMember{Id: id, Name: names[i], Properties: props}}})
		} else {
			edits = append(edits, &nanashiv1.MemberEdit{Edit: &nanashiv1.MemberEdit_Set{Set: &nanashiv1.SetProperties{Id: id, Properties: props}}})
		}
	}
	return edits, len(rows), nil
}

func rowCount(text string) (int, error) {
	_, rows, err := parseCSV(text)
	return len(rows), err
}

func largestRow(dim engineDim) int {
	largest := 0
	for _, m := range dim.Members {
		if n, err := strconv.Atoi(m.Name); err == nil && n > largest {
			largest = n
		}
	}
	return largest
}

// importMetricOps changes CSV rows into set_cell operations. Each row gives all dimensions of the Metric by
// member name. A new member gets an id from newID.
func importMetricOps(text string, mi *nanashiv1.MetricImport, em engineModel, l limits) ([]op, int, error) {
	header, rows, err := parseCSV(text)
	if err != nil {
		return nil, 0, err
	}
	m, ok := em.metric(mi.Metric)
	if !ok {
		return nil, 0, fmt.Errorf("Metric %s がない", mi.Metric)
	}
	valueCol, err := column(header, mi.ValueColumn)
	if err != nil {
		return nil, 0, err
	}
	cols := make([]int, len(m.Dims))
	known := make([]map[string]string, len(m.Dims))
	for i, d := range m.Dims {
		name, ok := mi.DimensionColumns[d]
		if !ok {
			return nil, 0, fmt.Errorf("%s の軸 %s の列を指定する", m.Name, em.name(d))
		}
		if cols[i], err = column(header, name); err != nil {
			return nil, 0, err
		}
		dim, _ := em.dim(d)
		known[i] = byName(dim)
	}
	var adds, sets []op
	for r, row := range rows {
		coords := map[string]string{}
		cell := map[string]any{}
		for i, d := range m.Dims {
			member := strings.TrimSpace(row[cols[i]])
			id, ok := known[i][member]
			if !ok {
				if !mi.AddMembers || member == "" {
					return nil, 0, fmt.Errorf("%d 行目: %s に %q がない", r+2, em.name(d), member)
				}
				id = newID()
				known[i][member] = id
				adds = append(adds, newOp("add_member", map[string]any{"dim": d, "id": id, "name": member}))
			}
			coords[d], cell[d] = id, id
		}
		if err := l.checkWrite(m.Name, m.Dims, coords, em); err != nil {
			return nil, 0, fmt.Errorf("%d 行目: %w", r+2, err)
		}
		v, err := parseValue(m.Kind, row[valueCol])
		if err != nil {
			return nil, 0, fmt.Errorf("%d 行目: %w", r+2, err)
		}
		sets = append(sets, newOp("set_cell", map[string]any{"metric": m.ID, "value": v, "coords": cell}))
	}
	return append(adds, sets...), len(rows), nil
}

// The actions follow.

// reserveRows reserves n row numbers of a TRANSACTION list and gives the first one. The update locks the row of
// app_list, so concurrent imports get different ranges. floor is the largest number in the model: the counter
// starts after it (a restored list has members, but a new counter). A plan that does not run leaves a gap.
func (s *PlanServer) reserveRows(ctx context.Context, app, list string, n, floor int) (int, error) {
	var first int
	err := s.Pool.QueryRow(ctx, "update app_list set next_row = greatest(next_row, $4) + $3 where app_id = $1 and id = $2 returning next_row - $3",
		app, list, n, floor+1).Scan(&first)
	if err != nil {
		return 0, dbError(err)
	}
	return first, nil
}

func (s *PlanServer) Import(ctx context.Context, req *connect.Request[nanashiv1.ImportRequest]) (*connect.Response[nanashiv1.ImportResponse], error) {
	app, c := req.Msg.AppId, callerOf(ctx)
	type rows struct {
		Rows int32 `json:"rows"`
	}
	var planOf func(engineModel, appMeta) (plan, error)
	switch t := req.Msg.Target.(type) {
	case *nanashiv1.ImportRequest_List:
		planOf = func(em engineModel, meta appMeta) (plan, error) {
			dim, ok := em.dim(t.List.List)
			if !ok {
				return plan{}, fmt.Errorf("リスト %s がない", t.List.List)
			}
			if _, ruled := c.limitsIn(em)[dim.ID]; ruled {
				return plan{}, tag(errDenied, "%s には権限の制限があるので読み込めない", dim.Name)
			}
			first := 0
			if t.List.MemberColumn == "" && meta.Kinds[dim.ID] == nanashiv1.ListKind_LIST_KIND_TRANSACTION {
				n, err := rowCount(req.Msg.Csv)
				if err != nil {
					return plan{}, err
				}
				if first, err = s.reserveRows(ctx, app, dim.ID, n, largestRow(dim)); err != nil {
					return plan{}, err
				}
			}
			edits, n, err := importListEdits(req.Msg.Csv, t.List, em, dim, meta, first)
			if err != nil {
				return plan{}, err
			}
			p, err := editOps(app, em, dim, meta, edits)
			p.result = rows{int32(n)}
			return p, err
		}
	case *nanashiv1.ImportRequest_Metric:
		planOf = func(em engineModel, _ appMeta) (plan, error) {
			ops, n, err := importMetricOps(req.Msg.Csv, t.Metric, em, c.limitsIn(em))
			return plan{ops: ops, result: rows{int32(n)}}, err
		}
	default:
		return nil, invalid(errors.New("読み込み先を指定する"))
	}
	result, err := s.change(ctx, app, req.Msg, planOf)
	if err != nil {
		return nil, err
	}
	var out rows
	json.Unmarshal(result, &out)
	return connect.NewResponse(&nanashiv1.ImportResponse{Rows: out.Rows}), nil
}
