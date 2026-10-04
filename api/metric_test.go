package api

import (
	"cmp"
	"testing"

	nanashiv1 "github.com/kazu-2020/nanashi/api/gen/nanashi/v1"
)

func TestMetricOpKeepsInputCells(t *testing.T) {
	em := model(t)
	budget := func(dims ...string) *nanashiv1.MetricDef {
		return &nanashiv1.MetricDef{Name: "Budget", Dimensions: dims}
	}
	if ops, err := metricOp(em, budget("Product", "Region"), "number", false); err != nil || len(ops) != 0 {
		t.Errorf("same input Metric: got %v, %v, want no operation", ops, err)
	}
	for _, c := range []struct {
		m    *nanashiv1.MetricDef
		kind string
	}{
		{budget("Product"), "number"},
		{budget("Product", "Region"), "boolean"},
		{&nanashiv1.MetricDef{Name: "Budget", Dimensions: []string{"Product", "Region"}, Formula: "1"}, "number"},
	} {
		m := c.m
		if _, err := metricOp(em, m, c.kind, false); err == nil {
			t.Errorf("%v: replaced the input cells without replace", m)
		}
		if ops, err := metricOp(em, m, c.kind, true); err != nil || len(ops) != 1 {
			t.Errorf("%v with replace: got %v, %v", m, ops, err)
		}
	}
	if ops, err := metricOp(em, &nanashiv1.MetricDef{Name: "Revenue", Dimensions: []string{"Product"}, Formula: "2"}, "number", false); err != nil ||
		opsJSON(t, ops) != `[{"op":"add_formula","args":["Revenue",["Product"],"2"],"kwargs":{"kind":"number","overridable":false}}]` {
		t.Errorf("formula change: got %s, %v", opsJSON(t, ops), err)
	}
}

func TestEngineKind(t *testing.T) {
	em := model(t)
	member, boolean := nanashiv1.ValueKind_VALUE_KIND_MEMBER, nanashiv1.ValueKind_VALUE_KIND_BOOLEAN
	for _, c := range []struct {
		m    *nanashiv1.MetricDef
		want string // Empty if the api refuses m.
	}{
		{&nanashiv1.MetricDef{}, "number"},
		{&nanashiv1.MetricDef{Kind: boolean}, "boolean"},
		{&nanashiv1.MetricDef{Kind: member, MemberList: "Region"}, "member:Region"},
		{&nanashiv1.MetricDef{Kind: member, MemberList: "Nothing"}, ""},
		{&nanashiv1.MetricDef{Kind: member}, ""},
		{&nanashiv1.MetricDef{Kind: boolean, MemberList: "Region"}, ""},
	} {
		got, err := engineKind(em, c.m)
		if got != c.want || (err == nil) != (c.want != "") {
			t.Errorf("%v: got %q, %v, want %q", c.m, got, err, c.want)
		}
		if k, list := valueKind(got); err == nil && (k != cmp.Or(c.m.Kind, nanashiv1.ValueKind_VALUE_KIND_NUMBER) || list != c.m.MemberList) {
			t.Errorf("%v: valueKind(%q) gives %v, %q", c.m, got, k, list)
		}
	}
}
