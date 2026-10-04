package api

import (
	"context"
	"errors"
	"fmt"
	"slices"
	"strings"

	"connectrpc.com/connect"

	nanashiv1 "github.com/kazu-2020/nanashi/api/gen/nanashi/v1"
)

// metricRefs are the places in the api tables that name a Metric. The arguments are (app, old name, new name).
// DeleteMetric does not touch the comments of the Metric.
var metricRefs = []string{
	`update app_item set def = jsonb_set(def, '{metrics}', app_rename(def->'metrics', $2, $3)) where app_id = $1 and def->'metrics' ? $2`,
	`update app_comment set metric = $3 where app_id = $1 and metric = $2 and $3::text is not null`,
}

// metricRenames gives the statements that rename a Metric in the api tables, or remove it when name is nil.
func metricRenames(app, old string, name *string) []stmt {
	return statements(metricRefs, app, old, name)
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

// The actions follow.

func (s *PlanServer) SaveMetric(ctx context.Context, req *connect.Request[nanashiv1.SaveMetricRequest]) (*ack, error) {
	app, m := req.Msg.AppId, req.Msg.Metric
	if m == nil || strings.TrimSpace(m.Name) == "" {
		return nil, invalid(errors.New("Metric の名前が空"))
	}
	return s.change(ctx, app, func(em engineModel, meta appMeta) (plan, error) {
		if err := meta.refuseProperty(m.Name); err != nil {
			return plan{}, err
		}
		kind, err := engineKind(em, m)
		if err != nil {
			return plan{}, err
		}
		ops, err := metricOp(em, m, kind, req.Msg.Replace)
		return plan{ops: ops}, err
	})
}

func (s *PlanServer) RenameMetric(ctx context.Context, req *connect.Request[nanashiv1.RenameMetricRequest]) (*ack, error) {
	app, old, name := req.Msg.AppId, req.Msg.Name, req.Msg.NewName
	return s.change(ctx, app, func(_ engineModel, meta appMeta) (plan, error) {
		if err := meta.refuseProperty(old, name); err != nil {
			return plan{}, err
		}
		// Tables, views and comments refer to Metrics by name, so give them the new name too.
		return plan{[]op{newOp("rename_metric", old, name)}, metricRenames(app, old, &name)}, nil
	})
}

func (s *PlanServer) DeleteMetric(ctx context.Context, req *connect.Request[nanashiv1.DeleteMetricRequest]) (*ack, error) {
	app, name := req.Msg.AppId, req.Msg.Name
	return s.change(ctx, app, func(_ engineModel, meta appMeta) (plan, error) {
		if err := meta.refuseProperty(name); err != nil {
			return plan{}, err
		}
		// A table or a view with a deleted Metric cannot query, so remove the name from them.
		return plan{[]op{newOp("remove_metric", name)}, metricRenames(app, name, nil)}, nil
	})
}
