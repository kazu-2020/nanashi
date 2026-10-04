package api

import (
	"strings"
	"testing"
)

func TestSameSeq(t *testing.T) {
	em := model(t) // seq 7
	if em.Seq != 7 {
		t.Fatalf("seq: %d", em.Seq)
	}
	same := map[string]engineCube{"Budget": {Seq: 7}}
	if !sameSeq(em, same, map[string]engineCube{}) {
		t.Error("cubes of the model version must agree")
	}
	if sameSeq(em, same, map[string]engineCube{"Revenue": {Seq: 8}}) {
		t.Error("a cube of a later version must not agree")
	}
}

func TestReplayOps(t *testing.T) {
	em := model(t)
	inputs := map[string]engineCube{
		"Budget":       {Dims: []string{"Region", "Product"}, Cells: [][]any{{"East", "A", 5.0}}},
		"Sales.Amount": {Dims: []string{"Sales"}, Cells: [][]any{{"1", 10.0}}},
	}
	overrides := map[string]engineCube{"Revenue": {Dims: []string{"Product"}, Cells: [][]any{{"A", 99.0}}}}
	got := opsJSON(t, replayOps(em, inputs, overrides))
	for _, want := range []string{
		`{"op":"add_dimension","args":["Sales",["1","2","9"]],"kwargs":{"ordered":false}}`,
		`{"op":"add_property","args":["Sales","Product","Product",{"1":"A","2":"B"}]}`,
		`{"op":"add_input","args":["Budget",["Product","Region"],[[["A","East"],5]]],"kwargs":{"kind":"number"}}`,
		`{"op":"add_formula","args":["Revenue",["Product"],"'Sales.Amount'[BY SUM: Sales.Product]"],"kwargs":{"kind":"number","overridable":false}}`,
		`{"op":"add_input","args":["Owner",["Product"],[]],"kwargs":{"kind":"member:Region"}}`,
		`{"op":"set_cell","args":["Revenue",99],"kwargs":{"Product":"A"}}`,
	} {
		if !strings.Contains(got, want) {
			t.Errorf("missing %s in %s", want, got)
		}
	}
}
