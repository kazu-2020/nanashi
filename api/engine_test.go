package api

import (
	"strings"
	"testing"
)

func TestParseEngineModelKeepsOrder(t *testing.T) {
	em := model(t)
	var dims, metrics []string
	for _, d := range em.Dims {
		dims = append(dims, d.Name)
	}
	for _, m := range em.Metrics {
		metrics = append(metrics, m.Name)
	}
	if got := strings.Join(dims, ",") + "|" + strings.Join(metrics, ","); got != "Product,Sales,Region|Sales.Amount,Budget,Revenue,Owner" {
		t.Errorf("order: %s", got)
	}
	sales, _ := em.dim("Sales")
	if p := sales.Props[0]; p.Target != "Product" || p.Values["2"] != "B" {
		t.Errorf("property: %+v", p)
	}
}
