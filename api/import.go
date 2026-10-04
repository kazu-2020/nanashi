package api

import (
	"context"
	"encoding/csv"
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

// The actions follow.

func (s *PlanServer) Import(ctx context.Context, req *connect.Request[nanashiv1.ImportRequest]) (*connect.Response[nanashiv1.ImportResponse], error) {
	app, c := req.Msg.AppId, callerOf(ctx)
	var rows int
	var planOf func(engineModel, appMeta) (plan, error)
	switch t := req.Msg.Target.(type) {
	case *nanashiv1.ImportRequest_List:
		planOf = func(em engineModel, meta appMeta) (plan, error) {
			if _, ruled := c.limitsIn(em)[t.List.List]; ruled {
				return plan{}, tag(errDenied, "%s には権限の制限があるので読み込めない", t.List.List)
			}
			edits, n, err := importListEdits(req.Msg.Csv, t.List, em, meta.Kinds[t.List.List])
			if err != nil {
				return plan{}, err
			}
			rows = n
			ops, stmts, err := editOps(app, t.List.List, em, meta, edits)
			return plan{ops, stmts}, err
		}
	case *nanashiv1.ImportRequest_Metric:
		planOf = func(em engineModel, _ appMeta) (plan, error) {
			ops, n, err := importMetricOps(req.Msg.Csv, t.Metric, em, c.limitsIn(em))
			rows = n
			return plan{ops: ops}, err
		}
	default:
		return nil, invalid(errors.New("読み込み先を指定する"))
	}
	if _, err := s.change(ctx, app, planOf); err != nil {
		return nil, err
	}
	return connect.NewResponse(&nanashiv1.ImportResponse{Rows: int32(rows)}), nil
}
