package api

// This file holds the cell data: queries, writes and comments.

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

	nanashiv1 "github.com/kazu-2020/nanashi/api/gen/nanashi/v1"
)

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

// The actions follow. They do I/O.

func (s *PlanServer) Query(ctx context.Context, req *connect.Request[nanashiv1.QueryRequest]) (*connect.Response[nanashiv1.QueryResponse], error) {
	app := req.Msg.AppId
	em, _, err := s.Engines.model(ctx, app)
	if err != nil {
		return nil, err
	}
	l := callerOf(ctx).limitsIn(em)
	reads, err := queryReads(req.Msg, em, l)
	if err != nil {
		return nil, invalid(err)
	}
	dims := slices.Concat(req.Msg.Rows, req.Msg.Columns)
	out := &nanashiv1.QueryResponse{Dimensions: dims}
	for _, r := range reads {
		cube, err := s.Engines.read(ctx, app, r)
		if err != nil {
			return nil, err
		}
		m, _ := em.metric(r.Metric) // queryReads found it.
		out.Cells = append(out.Cells, queryCells(m, dims, cube, l)...)
	}
	return connect.NewResponse(out), nil
}

func (s *PlanServer) WriteCells(ctx context.Context, req *connect.Request[nanashiv1.WriteCellsRequest]) (*ack, error) {
	c := callerOf(ctx)
	return s.change(ctx, req.Msg.AppId, func(em engineModel, _ appMeta) (plan, error) {
		ops, err := writeOps(req.Msg.Writes, em, c.limitsIn(em))
		return plan{ops: ops}, err
	})
}

func (s *PlanServer) ListComments(ctx context.Context, req *connect.Request[nanashiv1.ListCommentsRequest]) (*connect.Response[nanashiv1.ListCommentsResponse], error) {
	rows, _ := s.Pool.Query(ctx, `select id, metric, cell, user_name, body, (extract(epoch from created_at) * 1000)::bigint from app_comment
		where app_id = $1 and ($2 = '' or metric = $2) order by id`, req.Msg.AppId, req.Msg.Metric)
	comments, err := pgx.CollectRows(rows, func(row pgx.CollectableRow) (*nanashiv1.Comment, error) {
		c := &nanashiv1.Comment{AppId: req.Msg.AppId}
		var id int64
		err := row.Scan(&id, &c.Metric, &c.Cell, &c.User, &c.Body, &c.CreatedAt)
		c.Id = strconv.FormatInt(id, 10)
		return c, err
	})
	if err != nil {
		return nil, dbError(err)
	}
	if c := callerOf(ctx); len(c.rules) > 0 {
		em, _, err := s.Engines.model(ctx, req.Msg.AppId)
		if err != nil {
			return nil, err
		}
		comments = visibleComments(comments, c.limitsIn(em))
	}
	return connect.NewResponse(&nanashiv1.ListCommentsResponse{Comments: comments}), nil
}

func (s *PlanServer) AddComment(ctx context.Context, req *connect.Request[nanashiv1.Comment]) (*connect.Response[nanashiv1.Comment], error) {
	c := req.Msg
	if strings.TrimSpace(c.Body) == "" || c.Metric == "" {
		return nil, invalid(errors.New("コメントのメトリックと本文が要る"))
	}
	c.User = callerOf(ctx).user
	var id int64
	if err := s.Pool.QueryRow(ctx, `insert into app_comment (app_id, metric, cell, user_name, body) values ($1, $2, $3, $4, $5)
		returning id, (extract(epoch from created_at) * 1000)::bigint`, c.AppId, c.Metric, textJSON(c.Cell), c.User, c.Body).Scan(&id, &c.CreatedAt); err != nil {
		return nil, dbError(err)
	}
	c.Id = strconv.FormatInt(id, 10)
	return connect.NewResponse(c), nil
}
