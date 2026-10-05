package api

import (
	"cmp"
	"fmt"
	"testing"

	"connectrpc.com/connect"

	nanashiv1 "github.com/kazu-2020/nanashi/api/gen/nanashi/v1"
)

func TestMetricOpCreateAndUpdate(t *testing.T) {
	em := model(t)
	budgetDef := func(dims ...string) *nanashiv1.MetricDef {
		return &nanashiv1.MetricDef{Id: budget, Name: "Budget", Dimensions: dims}
	}
	if _, err := metricOp(em, budgetDef(product), "number", true); connect.CodeOf(connectError(err)) != connect.CodeAlreadyExists {
		t.Errorf("create of an existing Metric: got %v, want AlreadyExists", err)
	}
	if _, err := metricOp(em, &nanashiv1.MetricDef{Id: newMember, Name: "New"}, "number", false); connect.CodeOf(connectError(err)) != connect.CodeNotFound {
		t.Errorf("update of a missing Metric: got %v, want NotFound", err)
	}
	if _, err := metricOp(em, &nanashiv1.MetricDef{Id: newMember, Name: "Budget"}, "number", true); connect.CodeOf(connectError(err)) != connect.CodeAlreadyExists {
		t.Errorf("create with the name of another Metric: got %v, want AlreadyExists", err)
	}
	if ops, err := metricOp(em, &nanashiv1.MetricDef{Id: newMember, Name: "New"}, "number", true); err != nil ||
		opsJSON(t, ops) != fmt.Sprintf(`[{"cells":[],"dims":[],"id":%q,"kind":"number","name":"New","op":"add_input"}]`, newMember) {
		t.Errorf("create: got %s, %v", opsJSON(t, ops), err)
	}
	if ops, err := metricOp(em, budgetDef(product, region), "number", false); err != nil || len(ops) != 0 {
		t.Errorf("same input Metric: got %v, %v, want no operation", ops, err)
	}
	for _, c := range []struct {
		m    *nanashiv1.MetricDef
		kind string
	}{
		{budgetDef(product), "number"},
		{budgetDef(product, region), "boolean"},
		{&nanashiv1.MetricDef{Id: budget, Name: "Budget", Dimensions: []string{product, region}, Formula: "1"}, "number"},
	} {
		if ops, err := metricOp(em, c.m, c.kind, false); err != nil || len(ops) != 1 {
			t.Errorf("update %v: got %v, %v", c.m, ops, err)
		}
	}
	if ops, err := metricOp(em, &nanashiv1.MetricDef{Id: revenue, Name: "Revenue", Dimensions: []string{product}, Formula: "2"}, "number", false); err != nil ||
		opsJSON(t, ops) != fmt.Sprintf(`[{"dims":[%q],"formula":"2","id":%q,"kind":"number","name":"Revenue","op":"add_formula","overridable":false}]`, product, revenue) {
		t.Errorf("formula change: got %s, %v", opsJSON(t, ops), err)
	}
	if _, err := metricOp(em, &nanashiv1.MetricDef{Id: newMember, Name: "New", Dimensions: []string{newMember}}, "number", true); err == nil {
		t.Error("an unknown list must be an error")
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
		{&nanashiv1.MetricDef{Kind: member, MemberList: region}, "member:" + region},
		{&nanashiv1.MetricDef{Kind: member, MemberList: newMember}, ""},
		{&nanashiv1.MetricDef{Kind: member}, ""},
		{&nanashiv1.MetricDef{Kind: boolean, MemberList: region}, ""},
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
