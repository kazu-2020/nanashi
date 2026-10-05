package api

import (
	"fmt"
	"strings"
	"testing"
)

func TestSameSeq(t *testing.T) {
	em := model(t)
	if em.Seq != 7 {
		t.Fatalf("seq: %d", em.Seq)
	}
	same := map[string]engineCube{budget: {Seq: 7}}
	if !sameSeq(em, same, map[string]engineCube{}) {
		t.Error("cubes of the model version must agree")
	}
	if sameSeq(em, same, map[string]engineCube{revenue: {Seq: 8}}) {
		t.Error("a cube of a later version must not agree")
	}
}

func TestReplayOps(t *testing.T) {
	em := model(t)
	inputs := map[string]engineCube{
		budget: {Dims: []string{region, product}, Cells: [][]any{{east, memberA, 5.0}}},
		amount: {Dims: []string{sales}, Cells: [][]any{{sale1, 10.0}}},
	}
	overrides := map[string]engineCube{revenue: {Dims: []string{product}, Cells: [][]any{{memberA, 99.0}}}}
	got := opsJSON(t, replayOps(em, inputs, overrides))
	for _, want := range []string{
		fmt.Sprintf(`{"id":%q,"name":"Sales","op":"add_dimension","ordered":false}`, sales),
		fmt.Sprintf(`{"dim":%q,"id":%q,"name":"9","op":"add_member"}`, sales, sale9),
		fmt.Sprintf(`{"dim":%q,"id":%q,"name":"Product","op":"add_property","target":%q}`, sales, salesProduct, product),
		fmt.Sprintf(`{"dim":%q,"op":"set_property_values","prop":%q,"values":{%q:%q,%q:%q}}`, sales, salesProduct, sale1, memberA, sale2, memberB),
		fmt.Sprintf(`{"cells":[[[%q,%q],5]],"dims":[%q,%q],"id":%q,"kind":"number","name":"Budget","op":"add_input"}`, memberA, east, product, region, budget),
		fmt.Sprintf(`{"dims":[%q],"formula":"'Sales.Amount'[BY SUM: Sales.Product]","id":%q,"kind":"number","name":"Revenue","op":"add_formula","overridable":false}`, product, revenue),
		fmt.Sprintf(`{"cells":[],"dims":[%q],"id":%q,"kind":"member:%s","name":"Owner","op":"add_input"}`, product, own, region),
		fmt.Sprintf(`{"coords":{%q:%q},"metric":%q,"op":"set_cell","override":true,"value":99}`, product, memberA, revenue),
	} {
		if !strings.Contains(got, want) {
			t.Errorf("missing %s in %s", want, got)
		}
	}
}
