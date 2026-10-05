package api

import (
	"context"
	"encoding/json"
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

// queryReads makes the engine reads for a QueryRequest. It leaves out a Metric that the model does not have
// (a deleted Metric of a saved view) and a Metric that the filters make empty.
func queryReads(req *nanashiv1.QueryRequest, em engineModel, l limits) ([]engineRead, error) {
	agg, ok := engineAggs[req.Aggregation]
	if !ok {
		return nil, fmt.Errorf("集計 %d がない", req.Aggregation)
	}
	shown := slices.Concat(req.Rows, req.Columns)
	var out []engineRead
next:
	for _, id := range req.Metrics {
		m, ok := em.metric(id)
		if !ok || l.hides(m) {
			continue
		}
		q := map[string][]string{}
		for _, d := range m.Dims {
			members := req.Filters[d].GetIds()
			lim, limited := l[d]
			if limited && len(members) == 0 {
				members = slices.Sorted(maps.Keys(lim.read))
			}
			if limited || len(members) > 0 {
				// A saved filter can name a member that was removed later. The engine refuses such an id.
				dim, _ := em.dim(d)
				exists := map[string]bool{}
				for _, x := range dim.Members {
					exists[x.ID] = true
				}
				members = slices.DeleteFunc(slices.Clone(members), func(x string) bool { return !exists[x] || limited && !lim.read[x] })
				if len(members) == 0 {
					continue next
				}
				// All members is the same as no filter, and a big transaction list does not fit in the URL.
				// ponytail: a limit that hides some members still sends all readable ids in the URL. Past about
				// 64 KiB the engine refuses the request (414). Upgrade: let the engine take the filter in a POST body.
				if len(members) == len(dim.Members) {
					members = nil
				}
			}
			if len(members) > 0 {
				q[d] = []string{strings.Join(members, ",")}
			}
		}
		read := engineRead{Metric: id, Path: "slice", Query: q}
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
		out = append(out, &nanashiv1.QueryCell{Metric: m.ID, Coords: coords, Value: v})
	}
	return out
}

// writeOps sets a cell if the coordinates give all dimensions of the Metric. Otherwise it spreads the value.
func writeOps(writes []*nanashiv1.CellWrite, em engineModel, l limits) ([]op, error) {
	var ops []op
	for _, w := range writes {
		m, ok := em.metric(w.Metric)
		if !ok {
			return nil, fmt.Errorf("Metric %s がない", w.Metric)
		}
		coords := map[string]any{}
		for d, member := range w.Coords {
			if !slices.Contains(m.Dims, d) {
				return nil, fmt.Errorf("%s: 軸 %s がない", m.Name, em.name(d))
			}
			coords[d] = member
		}
		if err := l.checkWrite(m.Name, m.Dims, w.Coords, em); err != nil {
			return nil, err
		}
		if len(w.Coords) == len(m.Dims) {
			ops = append(ops, newOp("set_cell", map[string]any{"metric": m.ID, "value": valueOf(w.Value), "coords": coords}))
			continue
		}
		total, ok := valueOf(w.Value).(float64)
		if !ok {
			return nil, fmt.Errorf("%s: 集計したセルに入れられるのは数値だけ", m.Name)
		}
		ops = append(ops, newOp("spread", map[string]any{"metric": m.ID, "total": total, "coords": coords}))
	}
	return ops, nil
}

// checkCommentCell refuses a cell that does not give one member for each dimension of the Metric.
func checkCommentCell(metric string, cell map[string]string, em engineModel) error {
	m, ok := em.metric(metric)
	if !ok {
		return fmt.Errorf("Metric %s がない", metric)
	}
	bad := fmt.Errorf("%s: コメントのセルには各軸のメンバーを 1 つずつ指定する", m.Name)
	if len(cell) != len(m.Dims) {
		return bad
	}
	for _, list := range m.Dims {
		d, _ := em.dim(list)
		if _, ok := d.member(cell[list]); !ok {
			return bad
		}
	}
	return nil
}

// visibleComments removes the comments that a query does not show to a reader with the limits l. It removes a
// comment on a Metric that l hides, on a cell without a member of a limited list, or on a member that l hides.
// For a reader with rules, a Metric or a cell that the model does not have is hidden too (fail-closed).
func visibleComments(comments []*nanashiv1.Comment, l limits, em engineModel) []*nanashiv1.Comment {
	return slices.DeleteFunc(comments, func(c *nanashiv1.Comment) bool {
		m, ok := em.metric(c.Metric)
		if !ok || l.hides(m) {
			return true
		}
		for list := range l {
			if _, set := c.Cell[list]; !set && slices.Contains(m.Dims, list) {
				return true
			}
		}
		for list, member := range c.Cell {
			d, ok := em.dim(list)
			if !ok {
				return true
			}
			if _, ok := d.member(member); !ok || !l.visible(list, member) {
				return true
			}
		}
		return false
	})
}

// The actions follow.

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

// WriteCells changes only the engine. The engine does not commit the same client_op_id two times, so the api
// keeps no outbox row for it.
func (s *PlanServer) WriteCells(ctx context.Context, req *connect.Request[nanashiv1.WriteCellsRequest]) (*ack, error) {
	app, c := req.Msg.AppId, callerOf(ctx)
	em, _, err := s.Engines.model(ctx, app)
	if err != nil {
		return nil, err
	}
	ops, err := writeOps(req.Msg.Writes, em, c.limitsIn(em))
	if err != nil {
		return nil, connectError(err)
	}
	if len(ops) == 0 {
		return ok()
	}
	reply, err := s.Engines.write(ctx, app, c.user, c.opID, ops, nil)
	if err != nil {
		return nil, err
	}
	if reply.outcome() != done {
		return nil, reply.connectError()
	}
	return ok()
}

func (s *PlanServer) ListComments(ctx context.Context, req *connect.Request[nanashiv1.ListCommentsRequest]) (*connect.Response[nanashiv1.ListCommentsResponse], error) {
	rows, _ := s.Pool.Query(ctx, `select id, metric, cell, user_name, body, (extract(epoch from created_at) * 1000)::bigint from app_comment
		where app_id = $1 and ($2 = '' or metric::text = $2) order by created_at, id`, req.Msg.AppId, req.Msg.Metric)
	comments, err := pgx.CollectRows(rows, func(row pgx.CollectableRow) (*nanashiv1.Comment, error) {
		c := &nanashiv1.Comment{AppId: req.Msg.AppId}
		return c, row.Scan(&c.Id, &c.Metric, &c.Cell, &c.User, &c.Body, &c.CreatedAt)
	})
	if err != nil {
		return nil, dbError(err)
	}
	if c := callerOf(ctx); len(c.rules) > 0 {
		em, _, err := s.Engines.model(ctx, req.Msg.AppId)
		if err != nil {
			return nil, err
		}
		comments = visibleComments(comments, c.limitsIn(em), em)
	}
	return connect.NewResponse(&nanashiv1.ListCommentsResponse{Comments: comments}), nil
}

func (s *PlanServer) AddComment(ctx context.Context, req *connect.Request[nanashiv1.AddCommentRequest]) (*connect.Response[nanashiv1.Comment], error) {
	c := req.Msg.Comment
	if strings.TrimSpace(c.GetBody()) == "" || c.GetMetric() == "" || c.GetId() == "" {
		return nil, invalid(errors.New("コメントの id、メトリックと本文が要る"))
	}
	em, _, err := s.Engines.model(ctx, req.Msg.AppId)
	if err != nil {
		return nil, err
	}
	if err := checkCommentCell(c.Metric, c.Cell, em); err != nil {
		return nil, invalid(err)
	}
	c.AppId, c.User = req.Msg.AppId, callerOf(ctx).user
	result, err := s.apiOnly(ctx, req.Msg.AppId, req.Msg, func(tx pgx.Tx) (any, error) {
		res, err := tx.Exec(ctx, `insert into app_comment (app_id, id, metric, cell, user_name, body) values ($1, $2, $3, $4, $5, $6) on conflict do nothing`,
			c.AppId, c.Id, c.Metric, textJSON(c.Cell), c.User, c.Body)
		if err != nil {
			return nil, err
		}
		if res.RowsAffected() == 0 {
			return nil, tag(errExists, "同じ id のコメントがすでにある")
		}
		var ms int64
		err = tx.QueryRow(ctx, "select (extract(epoch from created_at) * 1000)::bigint from app_comment where app_id = $1 and id = $2", c.AppId, c.Id).Scan(&ms)
		return map[string]int64{"created_at": ms}, err
	})
	if err != nil {
		return nil, err
	}
	var stored struct {
		CreatedAt int64 `json:"created_at"`
	}
	json.Unmarshal(result, &stored)
	c.CreatedAt = stored.CreatedAt
	return connect.NewResponse(c), nil
}
