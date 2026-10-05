package api

import (
	"net/http"
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
	s, _ := em.dim(sales)
	if p := s.Props[0]; p.ID != salesProduct || p.Name != "Product" || p.Target != product || p.Values[sale2] != memberB {
		t.Errorf("property: %+v", p)
	}
	if m, _ := s.member(sale9); m.Name != "9" {
		t.Errorf("member: %+v", m)
	}
}

func TestEngineReplyOutcome(t *testing.T) {
	for _, c := range []struct {
		status int
		code   string
		want   outcome
	}{
		{http.StatusOK, "", done},
		{http.StatusBadRequest, "bad_request", failed},
		{http.StatusConflict, "duplicate_id", failed},
		{http.StatusConflict, "conflict", conflict},
		{http.StatusServiceUnavailable, "stale", unknown},
		{http.StatusGatewayTimeout, "timeout", unknown},
		{http.StatusNotFound, "no_model", unknown},
		{http.StatusMisdirectedRequest, "not_leader", unknown},
	} {
		if got := (engineReply{Status: c.status, Code: c.code}).outcome(); got != c.want {
			t.Errorf("%d %s: got %v, want %v", c.status, c.code, got, c.want)
		}
	}
}
