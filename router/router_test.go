package router

import (
	"bytes"
	"cmp"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"net/netip"
	"strings"
	"sync"
	"testing"
	"time"
)

func TestDecide(t *testing.T) {
	tests := []struct {
		name   string
		method string // POST unless set
		status int
		head   string
		want   decision
	}{
		{"ok", "", 200, `{"seq": 3}`, decision{action: deliver}},
		{"bad request", "", 400, `{"error": "bad_request"}`, decision{action: deliver}},
		{"unauthorized", "", 401, `{"error": "unauthorized"}`, decision{action: deliver}},
		{"not found", "", 404, `{"error": "not_found"}`, decision{action: deliver}},
		{"method", "", 405, `{"error": "read_only"}`, decision{action: deliver}},
		{"conflict", "", 409, `{"error": "conflict", "seq": 4}`, decision{action: deliver}},
		{"length required", "", 411, `{"error": "length_required"}`, decision{action: deliver}},
		{"too large", "", 413, `{"error": "too_large"}`, decision{action: deliver}},
		{"not leader", "", 421, `{"error": "not_leader", "leader": "http://a:8080"}`, decision{action: redirect, leader: "http://a:8080"}},
		{"not leader, unknown leader", "", 421, `{"error": "not_leader", "leader": null}`, decision{action: reresolve}},
		{"not leader, not a URL", "", 421, `{"error": "not_leader", "leader": "a:8080"}`, decision{action: reresolve}},
		{"not leader, no body", "", 421, ``, decision{action: reresolve}},
		{"overloaded", "", 429, `{"error": "overloaded"}`, decision{action: retrySame}},
		{"internal", "", 500, `{"error": "internal"}`, decision{action: reresolve}},
		{"internal on a read", "GET", 500, `{"error": "internal"}`, decision{action: deliver}},
		{"busy", "", 503, `{"error": "busy"}`, decision{action: reresolve}},
		{"stale", "", 503, `{"error": "stale"}`, decision{action: reresolve}},
		{"closed", "", 503, `{"error": "closed"}`, decision{action: reresolve}},
		{"not ready", "", 503, `{"ready": false, "reasons": ["x"]}`, decision{action: deliver}},
		{"timeout", "", 504, `{"error": "timeout"}`, decision{action: reresolve}},
		{"no response", "", 0, ``, decision{action: reresolve}},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			method := cmp.Or(tt.method, http.MethodPost)
			if got := decide(method, tt.status, []byte(tt.head)); got != tt.want {
				t.Errorf("decide(%s, %d, %s) = %+v, want %+v", method, tt.status, tt.head, got, tt.want)
			}
		})
	}
}

// resolver answers Leader from a list of answers, repeating the last one.
type resolver struct {
	mu      sync.Mutex
	answers []string
	err     error
	calls   int
}

func (r *resolver) count() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.calls
}

func (r *resolver) Leader(context.Context, string) (string, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.calls++
	if r.err != nil {
		return "", r.err
	}
	i := min(r.calls, len(r.answers)) - 1
	return r.answers[i], nil
}

type received struct {
	uri    string
	body   []byte
	header http.Header
}

// engine is a fake engine that answers each request with the next reply and records what it got.
type engine struct {
	*httptest.Server
	mu      sync.Mutex
	got     []received
	replies []reply
}

type reply struct {
	status int
	body   string
	header map[string]string
}

func newEngine(t *testing.T, replies ...reply) *engine {
	e := &engine{replies: replies}
	e.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		e.mu.Lock()
		e.got = append(e.got, received{r.RequestURI, body, r.Header.Clone()})
		rep := e.replies[min(len(e.got), len(e.replies))-1]
		e.mu.Unlock()
		for k, v := range rep.header {
			w.Header().Set(k, v)
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(rep.status)
		io.WriteString(w, rep.body)
	}))
	t.Cleanup(e.Close)
	return e
}

func (e *engine) requests() []received {
	e.mu.Lock()
	defer e.mu.Unlock()
	return append([]received(nil), e.got...)
}

// deadURL is an address nothing listens on.
func deadURL(t *testing.T) string {
	s := httptest.NewServer(http.NotFoundHandler())
	s.Close()
	return s.URL
}

type response struct {
	status int
	body   string
	header http.Header
}

func do(t *testing.T, rt *Router, method, target string, body []byte, header map[string]string) response {
	t.Helper()
	srv := httptest.NewServer(rt)
	defer srv.Close()
	req, err := http.NewRequest(method, srv.URL+target, bytes.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	for k, v := range header {
		req.Header.Set(k, v)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(resp.Body)
	return response{resp.StatusCode, string(data), resp.Header}
}

func errorKind(t *testing.T, body string) string {
	t.Helper()
	var v struct{ Error string }
	if err := json.Unmarshal([]byte(body), &v); err != nil {
		t.Fatalf("body %q: %v", body, err)
	}
	return v.Error
}

const write = `{"client_op_id": "c-1",  "ops": [{"op": "set_cell", "args": ["Value", 1.50], "kwargs": {"Item": "日本"}}]}`

func TestRedirectsToLeaderNamedBy421(t *testing.T) {
	leader := newEngine(t, reply{200, `{"seq": 7}`, nil})
	standby := newEngine(t, reply{421, `{"error": "not_leader", "message": "x", "leader": "` + leader.URL + `"}`, nil})
	res := &resolver{answers: []string{standby.URL}}
	rt := &Router{Resolve: res}

	got := do(t, rt, "POST", "/models/plan/writes", []byte(write), nil)
	if got.status != 200 || got.body != `{"seq": 7}` {
		t.Fatalf("got %d %s", got.status, got.body)
	}
	if n := len(leader.requests()); n != 1 {
		t.Errorf("leader got %d requests, want 1", n)
	}
	do(t, rt, "POST", "/models/plan/writes", []byte(write), nil)
	if n := len(standby.requests()); n != 1 {
		t.Errorf("standby got %d requests, want 1 (the 421 leader should be cached)", n)
	}
	if res.count() != 1 {
		t.Errorf("resolver called %d times, want 1", res.count())
	}
}

func TestConnectionRefusedReresolves(t *testing.T) {
	leader := newEngine(t, reply{200, `{"seq": 1}`, nil})
	res := &resolver{answers: []string{deadURL(t), leader.URL}}
	got := do(t, &Router{Resolve: res}, "POST", "/models/plan/writes", []byte(write), nil)
	if got.status != 200 {
		t.Fatalf("got %d %s", got.status, got.body)
	}
	if res.count() != 2 {
		t.Errorf("resolver called %d times, want 2", res.count())
	}
}

func TestStaleThenSuccess(t *testing.T) {
	e := newEngine(t, reply{503, `{"error": "stale", "message": "x"}`, nil}, reply{200, `{"seq": 2}`, nil})
	got := do(t, &Router{Resolve: &resolver{answers: []string{e.URL}}}, "POST", "/models/plan/writes", []byte(write), nil)
	if got.status != 200 || len(e.requests()) != 2 {
		t.Fatalf("got %d %s after %d requests", got.status, got.body, len(e.requests()))
	}
}

func TestTimeoutResendsSameBody(t *testing.T) {
	e := newEngine(t, reply{504, `{"error": "timeout", "message": "x"}`, nil}, reply{200, `{"seq": 3}`, nil})
	got := do(t, &Router{Resolve: &resolver{answers: []string{e.URL}}}, "POST", "/models/plan/writes", []byte(write), nil)
	reqs := e.requests()
	if got.status != 200 || len(reqs) != 2 {
		t.Fatalf("got %d %s after %d requests", got.status, got.body, len(reqs))
	}
	if !bytes.Equal(reqs[1].body, []byte(write)) {
		t.Errorf("resent body %q, want %q", reqs[1].body, write)
	}
}

func TestFinalStatusDeliveredUnchanged(t *testing.T) {
	for _, status := range []int{400, 401, 404, 405, 409, 411, 413} {
		body := `{"error": "conflict", "message": "x", "seq": 4, "user": "bob"}`
		e := newEngine(t, reply{status, body, map[string]string{"X-Engine": "e1"}})
		rt := &Router{Resolve: &resolver{answers: []string{e.URL}}, Deadline: 2 * time.Second}
		got := do(t, rt, "POST", "/models/plan/writes", []byte(write), nil)
		if got.status != status || got.body != body || got.header.Get("X-Engine") != "e1" {
			t.Errorf("%d: got %d %s %v", status, got.status, got.body, got.header)
		}
		if n := len(e.requests()); n != 1 {
			t.Errorf("%d: engine got %d requests, want 1", status, n)
		}
	}
}

func TestBodyReplayedByteForByte(t *testing.T) {
	e := newEngine(t,
		reply{500, `{"error": "internal", "message": "x"}`, nil},
		reply{503, `{"error": "busy", "message": "x"}`, nil},
		reply{429, `{"error": "overloaded", "message": "x"}`, nil},
		reply{200, `{"seq": 9}`, nil})
	got := do(t, &Router{Resolve: &resolver{answers: []string{e.URL}}}, "POST", "/models/plan/writes", []byte(write),
		map[string]string{"Content-Type": "application/json"})
	reqs := e.requests()
	if got.status != 200 || len(reqs) != 4 {
		t.Fatalf("got %d %s after %d requests", got.status, got.body, len(reqs))
	}
	for i, r := range reqs {
		if !bytes.Equal(r.body, []byte(write)) {
			t.Errorf("attempt %d body %q, want %q", i+1, r.body, write)
		}
		if r.header.Get("Content-Type") != "application/json" {
			t.Errorf("attempt %d Content-Type %q", i+1, r.header.Get("Content-Type"))
		}
	}
}

func TestUserHeaderComesFromAuth(t *testing.T) {
	e := newEngine(t, reply{200, `{"seq": 1}`, nil})
	rt := &Router{
		Resolve: &resolver{answers: []string{e.URL}},
		Auth: func(r *http.Request) (string, error) {
			if r.Header.Get("Authorization") != "Bearer t1" {
				return "", errors.New("Authorization: Bearer <トークン> が要る")
			}
			return "alice", nil
		},
	}
	hdr := map[string]string{"Authorization": "Bearer t1", "X-Forwarded-User": "mallory", "Content-Type": "application/json"}
	if got := do(t, rt, "POST", "/models/plan/writes", []byte(write), hdr); got.status != 200 {
		t.Fatalf("got %d %s", got.status, got.body)
	}
	h := e.requests()[0].header
	if v := h.Values("X-Forwarded-User"); len(v) != 1 || v[0] != "alice" {
		t.Errorf("engine saw X-Forwarded-User %q, want [alice]", v)
	}
	if h.Get("Authorization") != "" {
		t.Errorf("engine saw the client's Authorization %q", h.Get("Authorization"))
	}

	hdr["Authorization"] = "Bearer wrong"
	if got := do(t, rt, "POST", "/models/plan/writes", []byte(write), hdr); got.status != 401 || errorKind(t, got.body) != "unauthorized" {
		t.Errorf("wrong token: got %d %s", got.status, got.body)
	}
	if n := len(e.requests()); n != 1 {
		t.Errorf("engine got %d requests, want 1 (unauthorized must not be forwarded)", n)
	}
}

func TestNoAuthStripsClientUserHeader(t *testing.T) {
	e := newEngine(t, reply{200, `{}`, nil})
	rt := &Router{Resolve: &resolver{answers: []string{e.URL}}, UserHeader: "X-User"}
	do(t, rt, "GET", "/models/plan/", nil, map[string]string{"X-User": "mallory"})
	if v := e.requests()[0].header.Values("X-User"); len(v) != 0 {
		t.Errorf("engine saw X-User %q, want none", v)
	}
}

// fromAddr sends one request to rt as if it came from remoteAddr ("host:port").
func fromAddr(rt *Router, remoteAddr, method, target string, body []byte, header map[string]string) response {
	req := httptest.NewRequest(method, target, bytes.NewReader(body))
	req.RemoteAddr = remoteAddr
	for k, v := range header {
		req.Header.Set(k, v)
	}
	rec := httptest.NewRecorder()
	rt.ServeHTTP(rec, req)
	return response{rec.Code, rec.Body.String(), rec.Header()}
}

// TestProxyUserThroughRouter covers OIDC proxy -> router -> engine: the router trusts the user header
// only from the proxy's CIDR, and sends the same header to the engine.
func TestProxyUserThroughRouter(t *testing.T) {
	e := newEngine(t, reply{200, `{"seq": 1}`, nil})
	trusted := []netip.Prefix{netip.MustParsePrefix("10.0.0.0/24"), netip.MustParsePrefix("fd00::/8")}
	rt := &Router{
		Resolve:    &resolver{answers: []string{e.URL}},
		Auth:       ProxyUser("X-Forwarded-Email", trusted),
		UserHeader: "X-Forwarded-Email",
	}
	hdr := map[string]string{"X-Forwarded-Email": "alice@example.com", "Authorization": "Bearer id-token",
		"Content-Type": "application/json"}
	for _, proxy := range []string{"10.0.0.7:41000", "[::ffff:10.0.0.7]:41000", "[fd00::5]:41000"} {
		before := len(e.requests())
		if got := fromAddr(rt, proxy, "POST", "/models/plan/writes", []byte(write), hdr); got.status != 200 {
			t.Fatalf("from %s: got %d %s", proxy, got.status, got.body)
		}
		h := e.requests()[before].header
		if v := h.Values("X-Forwarded-Email"); len(v) != 1 || v[0] != "alice@example.com" {
			t.Errorf("from %s: engine saw X-Forwarded-Email %q, want [alice@example.com]", proxy, v)
		}
		if h.Get("Authorization") != "" {
			t.Errorf("from %s: engine saw Authorization %q", proxy, h.Get("Authorization"))
		}
	}

	// A sender outside the trusted CIDRs can set any user, so the router does not use or send its header.
	sent := len(e.requests())
	for _, sender := range []string{"10.0.1.7:41000", "127.0.0.1:41000", "[::1]:41000", "[fe80::1%eth0]:41000", "bad"} {
		got := fromAddr(rt, sender, "POST", "/models/plan/writes", []byte(write), hdr)
		if got.status != 401 || errorKind(t, got.body) != "unauthorized" {
			t.Errorf("from %s: got %d %s, want 401", sender, got.status, got.body)
		}
	}
	// The trusted proxy must send the header.
	if got := fromAddr(rt, "10.0.0.7:41000", "GET", "/models/plan/", nil, nil); got.status != 401 {
		t.Errorf("no header: got %d %s, want 401", got.status, got.body)
	}
	if n := len(e.requests()); n != sent {
		t.Errorf("engine got %d more requests, want 0 (unauthorized must not be forwarded)", n-sent)
	}
}

func TestParsePrefix(t *testing.T) {
	for in, want := range map[string]string{
		"10.0.1.0/24":         "10.0.1.0/24",
		"10.0.1.5/24":         "10.0.1.0/24",
		" 10.0.1.5 ":          "10.0.1.5/32",
		"fd00::/8":            "fd00::/8",
		"::1":                 "::1/128",
		"::ffff:10.0.1.5":     "10.0.1.5/32",
		"::ffff:10.0.1.0/120": "10.0.1.0/24",
	} {
		got, err := ParsePrefix(in)
		if err != nil || got.String() != want {
			t.Errorf("ParsePrefix(%q) = %v, %v, want %s", in, got, err, want)
		}
	}
	for _, in := range []string{"", "10.0.0.0/33", "host.example", "fe80::1%eth0", "10.0.0.0/8,10.1.0.0/16"} {
		if _, err := ParsePrefix(in); err == nil {
			t.Errorf("ParsePrefix(%q) succeeded, want an error", in)
		}
	}
}

func TestPrefixStrippedKeepingEscapes(t *testing.T) {
	e := newEngine(t, reply{200, `{}`, nil})
	rt := &Router{Resolve: &resolver{answers: []string{e.URL}}}
	for target, want := range map[string]string{
		"/models/plan/metrics/a%2Fb/cell?Item=x%2Fy&Month=%E6%97%A5": "/metrics/a%2Fb/cell?Item=x%2Fy&Month=%E6%97%A5",
		"/models/plan":        "/",
		"/models/plan?x=1":    "/?x=1",
		"/models/plan/health": "/health",
	} {
		before := len(e.requests())
		if got := do(t, rt, "GET", target, nil, nil); got.status != 200 {
			t.Fatalf("%s: got %d %s", target, got.status, got.body)
		}
		if uri := e.requests()[before].uri; uri != want {
			t.Errorf("%s: engine got %s, want %s", target, uri, want)
		}
	}
}

func TestNoLeaderUntilDeadline(t *testing.T) {
	rt := &Router{Resolve: &resolver{answers: []string{""}}, Deadline: 300 * time.Millisecond}
	start := time.Now()
	got := do(t, rt, "POST", "/models/plan/writes", []byte(write), nil)
	if got.status != 503 || errorKind(t, got.body) != "no_leader" {
		t.Fatalf("got %d %s", got.status, got.body)
	}
	if took := time.Since(start); took < 300*time.Millisecond || took > 2*time.Second {
		t.Errorf("took %v, want about the 300ms deadline", took)
	}
}

func TestDeadlineDeliversLastResponse(t *testing.T) {
	e := newEngine(t, reply{504, `{"error": "timeout", "message": "x"}`, nil})
	rt := &Router{Resolve: &resolver{answers: []string{e.URL}}, Deadline: 300 * time.Millisecond}
	got := do(t, rt, "POST", "/models/plan/writes", []byte(write), nil)
	if got.status != 504 || got.body != `{"error": "timeout", "message": "x"}` {
		t.Fatalf("got %d %s", got.status, got.body)
	}
}

func TestUnknownModel(t *testing.T) {
	res := &resolver{err: ErrUnknownModel}
	got := do(t, &Router{Resolve: res}, "GET", "/models/nope/", nil, nil)
	if got.status != 404 || errorKind(t, got.body) != "no_model" || res.count() != 1 {
		t.Fatalf("got %d %s after %d resolves", got.status, got.body, res.count())
	}
}

func TestRouterOwnPaths(t *testing.T) {
	res := &resolver{answers: []string{deadURL(t)}}
	rt := &Router{Resolve: res, Deadline: 2 * time.Second}
	for target, want := range map[string]int{
		"/healthz":                            200,
		"/writes":                             404,
		"/models":                             404,
		"/models/":                            400,
		"/models/-x/writes":                   400,
		"/models/a.b/writes":                  400,
		"/models/a%2Fb/writes":                400,
		"/models/" + strings.Repeat("a", 129): 400,
	} {
		if got := do(t, rt, "GET", target, nil, nil); got.status != want {
			t.Errorf("%s: got %d %s, want %d", target, got.status, got.body, want)
		}
	}
	if res.count() != 0 {
		t.Errorf("resolver called %d times, want 0", res.count())
	}
}

func TestBodyTooLarge(t *testing.T) {
	e := newEngine(t, reply{200, `{}`, nil})
	got := do(t, &Router{Resolve: &resolver{answers: []string{e.URL}}}, "POST", "/models/plan/writes", make([]byte, maxBody+1), nil)
	if got.status != 413 || len(e.requests()) != 0 {
		t.Fatalf("got %d %s, engine got %d", got.status, got.body, len(e.requests()))
	}
}

func TestPutCreatesModel(t *testing.T) {
	var created []string
	rt := &Router{Resolve: &resolver{err: errors.New("must not resolve")}, Create: func(_ context.Context, model string) error {
		created = append(created, model)
		return nil
	}}
	for _, target := range []string{"/models/plan", "/models/plan/"} {
		if got := do(t, rt, "PUT", target, nil, nil); got.status != 200 || got.body != "{\"ok\":true}\n" {
			t.Errorf("%s: got %d %s", target, got.status, got.body)
		}
	}
	if len(created) != 2 || created[0] != "plan" {
		t.Errorf("Create got %v, want [plan plan]", created)
	}
	rt.Create = nil
	if got := do(t, rt, "PUT", "/models/plan", nil, nil); got.status != 405 {
		t.Errorf("nil Create: got %d %s, want 405", got.status, got.body)
	}
}

func TestEngineFailingAtOnce(t *testing.T) {
	res := &resolver{err: fmt.Errorf("%w: exit status 1", ErrEngineFailing)}
	rt := &Router{Resolve: res, Deadline: 5 * time.Second}
	start := time.Now()
	got := do(t, rt, "GET", "/models/plan/", nil, nil)
	if got.status != 503 || errorKind(t, got.body) != "engine_failing" || !strings.Contains(got.body, "exit status 1") {
		t.Fatalf("got %d %s", got.status, got.body)
	}
	if took := time.Since(start); took > time.Second {
		t.Errorf("took %v, want an answer at once", took)
	}
}
