package api

import (
	"context"
	"errors"
	"fmt"
	"slices"
	"strings"

	"connectrpc.com/connect"
	"google.golang.org/protobuf/proto"

	nanashiv1 "github.com/kazu-2020/nanashi/api/gen/nanashi/v1"
	"github.com/kazu-2020/nanashi/api/internal/block"
)

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

// metricOp gives the operations that make m (create) or change m with the engine kind.
// If create is true and the id exists, it gives an errExists error. If create is false and the id is missing, it
// gives an errNotFound error. An update keeps the current name: RenameMetric changes it, and a stale name from the
// client must not rename the Metric back. It gives no operation for an input Metric that does not change. Other
// changes to an input Metric delete its cells.
func metricOp(em engineModel, m *nanashiv1.MetricDef, kind string, create bool) ([]op, error) {
	dims := m.Dimensions
	if dims == nil {
		dims = []string{}
	}
	for _, d := range dims {
		if _, ok := em.dim(d); !ok {
			return nil, fmt.Errorf("リスト %s がない", d)
		}
	}
	// An update keeps the name, so only a create checks it.
	name := strings.TrimSpace(m.Name)
	if create {
		n, err := block.ParseName(m.Name)
		if err != nil {
			return nil, err
		}
		name = n.String()
	}
	old, found := em.metric(m.Id)
	if !create && found {
		name = old.Name
	}
	switch {
	case create && found:
		return nil, tag(errExists, "Metric %s はすでにある", old.Name)
	case !create && !found:
		return nil, tag(errNotFound, "Metric %s がない", name)
	case slices.ContainsFunc(em.Metrics, func(x engineMetric) bool { return x.Name == name && x.ID != m.Id }):
		return nil, tag(errExists, "Metric %s の名前はすでにある", name)
	case !create && old.Formula == "" && m.Formula == "" && old.Kind == kind && slices.Equal(old.Dims, dims):
		return nil, nil
	}
	if m.Formula == "" {
		return []op{newOp("add_input", map[string]any{"id": m.Id, "name": name, "dims": dims, "kind": kind, "cells": []any{}})}, nil
	}
	return []op{newOp("add_formula", map[string]any{"id": m.Id, "name": name, "dims": dims, "formula": m.Formula, "kind": kind, "overridable": m.Overridable})}, nil
}

// catalogStmt gives the statement that writes the catalog row of m, and the row for the compensation.
// A create inserts the row with the owner user. An update keeps the owner. An update without a catalog change
// gives no statement. old is the current row of m, if found.
func catalogStmt(app, user string, m *nanashiv1.MetricDef, old metricRow, found, create bool) ([]stmt, []madeRow) {
	next := metricRow{Description: strings.TrimSpace(m.Description), Folder: strings.TrimSpace(m.Folder)}
	made := madeRow{Table: "app_metric", ID: m.Id, New: &next}
	if create {
		return []stmt{{`insert into app_metric (app_id, metric_id, description, folder, owner) values ($1, $2, $3, $4, $5) on conflict do nothing`,
			[]any{app, m.Id, next.Description, next.Folder, user}, tag(errExists, "同じ id のメトリックがすでにある")}}, []madeRow{made}
	}
	if found && old.Description == next.Description && old.Folder == next.Folder {
		return nil, nil
	}
	if found {
		made.Old = &metricRow{Description: old.Description, Folder: old.Folder}
	}
	// A Metric from before the catalog has no row. The update makes it with the user as the owner.
	return []stmt{{`insert into app_metric (app_id, metric_id, description, folder, owner) values ($1, $2, $3, $4, $5)
		on conflict (app_id, metric_id) do update set description = excluded.description, folder = excluded.folder`,
		[]any{app, m.Id, next.Description, next.Folder, user}, nil}}, []madeRow{made}
}

// The actions follow.

func (s *PlanServer) CreateMetric(ctx context.Context, req *connect.Request[nanashiv1.CreateMetricRequest]) (*ack, error) {
	return s.saveMetric(ctx, req.Msg.AppId, req.Msg, req.Msg.Metric, true)
}

func (s *PlanServer) UpdateMetric(ctx context.Context, req *connect.Request[nanashiv1.UpdateMetricRequest]) (*ack, error) {
	return s.saveMetric(ctx, req.Msg.AppId, req.Msg, req.Msg.Metric, false)
}

func (s *PlanServer) saveMetric(ctx context.Context, app string, req proto.Message, m *nanashiv1.MetricDef, create bool) (*ack, error) {
	if m == nil || m.Id == "" {
		return nil, invalid(errors.New("Metric の id が要る"))
	}
	return ackOf(s.change(ctx, app, req, func(em engineModel, meta appMeta) (plan, error) {
		if err := meta.refuseProperty(m.Id); err != nil {
			return plan{}, err
		}
		kind, err := engineKind(em, m)
		if err != nil {
			return plan{}, err
		}
		ops, err := metricOp(em, m, kind, create)
		if err != nil {
			return plan{}, err
		}
		old, found := meta.Metrics[m.Id]
		stmts, made := catalogStmt(app, callerOf(ctx).user, m, old, found, create)
		return plan{ops: ops, stmts: stmts, made: made}, nil
	}))
}

func (s *PlanServer) RenameMetric(ctx context.Context, req *connect.Request[nanashiv1.RenameMetricRequest]) (*ack, error) {
	app, id := req.Msg.AppId, req.Msg.Id
	name, err := block.ParseName(req.Msg.Name)
	if err != nil {
		return nil, invalid(err)
	}
	// Tables, views and comments refer to the Metric by id, so no api row changes.
	return ackOf(s.change(ctx, app, req.Msg, func(em engineModel, meta appMeta) (plan, error) {
		if err := meta.refuseProperty(id); err != nil {
			return plan{}, err
		}
		if _, ok := em.metric(id); !ok {
			return plan{}, tag(errNotFound, "Metric %s がない", id)
		}
		return plan{ops: []op{newOp("rename_metric", map[string]any{"id": id, "name": name.String()})}}, nil
	}))
}

func (s *PlanServer) DeleteMetric(ctx context.Context, req *connect.Request[nanashiv1.DeleteMetricRequest]) (*ack, error) {
	app, id := req.Msg.AppId, req.Msg.Id
	// GetModel leaves the Metric out of the tables and views that refer to it.
	return ackOf(s.change(ctx, app, req.Msg, func(em engineModel, meta appMeta) (plan, error) {
		if err := meta.refuseProperty(id); err != nil {
			return plan{}, err
		}
		if _, ok := em.metric(id); !ok {
			return plan{}, tag(errNotFound, "Metric %s がない", id)
		}
		return plan{ops: []op{newOp("remove_metric", map[string]any{"id": id})}}, nil
	}))
}
