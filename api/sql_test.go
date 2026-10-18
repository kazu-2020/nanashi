package api

import (
	"bytes"
	"context"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"

	"connectrpc.com/connect"
	"github.com/google/go-cmp/cmp"

	nanashiv1 "github.com/kazu-2020/nanashi/api/gen/nanashi/v1"
	"github.com/kazu-2020/nanashi/api/gen/nanashi/v1/nanashiv1connect"
)

const testApp = "0192f3a4-5b6c-7d8e-9f01-23456789abcd"

type routerCall struct {
	Method, Path, ContentType, Accept, User, Body string
}

type fakeRouter struct {
	mu    sync.Mutex
	calls []routerCall
}

func (f *fakeRouter) got() []routerCall {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.calls
}

func newClient(t *testing.T, status int, body []byte) (nanashiv1connect.PlanServiceClient, *fakeRouter) {
	t.Helper()
	f := &fakeRouter{}
	router := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		f.mu.Lock()
		f.calls = append(f.calls, routerCall{r.Method, r.URL.Path, r.Header.Get("Content-Type"),
			r.Header.Get("Accept"), r.Header.Get("X-Nanashi-User"), string(b)})
		f.mu.Unlock()
		w.WriteHeader(status)
		_, _ = w.Write(body)
	}))
	t.Cleanup(router.Close)
	return clientFor(t, router.URL), f
}

func clientFor(t *testing.T, routerURL string) nanashiv1connect.PlanServiceClient {
	t.Helper()
	server := &PlanServer{Router: routerURL, HTTP: http.DefaultClient}
	mux := http.NewServeMux()
	mux.Handle(nanashiv1connect.NewPlanServiceHandler(server, connect.WithInterceptors(Interceptor())))
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	return nanashiv1connect.NewPlanServiceClient(http.DefaultClient, srv.URL)
}

func querySQL(c nanashiv1connect.PlanServiceClient, user, app, sql string) (*connect.Response[nanashiv1.QuerySqlResponse], error) {
	req := connect.NewRequest(&nanashiv1.QuerySqlRequest{AppId: app, Sql: sql})
	if user != "" {
		req.Header().Set("X-Nanashi-User", user)
	}
	return c.QuerySql(context.Background(), req)
}

func errorOf(err error) [2]string {
	cerr := new(connect.Error)
	if !errors.As(err, &cerr) {
		return [2]string{"", ""}
	}
	return [2]string{cerr.Code().String(), cerr.Message()}
}

func TestQuerySql(t *testing.T) {
	arrow := []byte("ARROW1\x00\x00\xff\x01")
	c, f := newClient(t, http.StatusOK, arrow)
	res, err := querySQL(c, "alice", testApp, "SELECT Month, SUM(Revenue) FROM model GROUP BY Month")
	if err != nil {
		t.Fatal(err)
	}
	if diff := cmp.Diff(arrow, res.Msg.GetArrow()); diff != "" {
		t.Errorf("arrow (-want +got):\n%s", diff)
	}
	want := []routerCall{{
		Method: "POST", Path: "/models/" + testApp + "/sql", ContentType: "application/json",
		Accept: "application/vnd.apache.arrow.stream", User: "alice",
		Body: `{"sql":"SELECT Month, SUM(Revenue) FROM model GROUP BY Month"}`,
	}}
	if diff := cmp.Diff(want, f.got()); diff != "" {
		t.Errorf("router calls (-want +got):\n%s", diff)
	}
}

func TestQuerySqlEngineErrors(t *testing.T) {
	tests := []struct {
		name   string
		status int
		body   string
		want   [2]string
	}{
		{"sql", 400, `{"error":"sql","code":"sql_unsupported","message":"JOIN は使えない"}`, [2]string{"invalid_argument", "JOIN は使えない"}},
		{"no model", 404, `{"error":"no_model","message":"モデルがない"}`, [2]string{"not_found", "モデルがない"}},
		{"too large", 413, `{"error":"sql_too_large","message":"SQL が長すぎる"}`, [2]string{"resource_exhausted", "SQL が長すぎる"}},
		{"reference engine", 501, `{"error":"not_implemented","message":"Arrow は使えない"}`, [2]string{"unimplemented", "Arrow は使えない"}},
		{"no leader", 503, `{"error":"no_leader","message":"リーダーがいない"}`, [2]string{"unavailable", "リーダーがいない"}},
		{"internal", 500, `{"error":"internal","message":"内部エラー"}`, [2]string{"unavailable", "計算エンジンが応答しない"}},
		{"not json", 502, `Bad Gateway`, [2]string{"unavailable", "計算エンジンが応答しない"}},
		{"no message", 400, `{"error":"bad_request"}`, [2]string{"unavailable", "計算エンジンが応答しない"}},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			c, _ := newClient(t, tt.status, []byte(tt.body))
			_, err := querySQL(c, "alice", testApp, "SELECT 1")
			if diff := cmp.Diff(tt.want, errorOf(err)); diff != "" {
				t.Errorf("error (-want +got):\n%s", diff)
			}
		})
	}
}

func TestQuerySqlRouterDown(t *testing.T) {
	router := httptest.NewServer(http.NotFoundHandler())
	router.Close()
	_, err := querySQL(clientFor(t, router.URL), "alice", testApp, "SELECT 1")
	if diff := cmp.Diff([2]string{"unavailable", "計算エンジンに接続できない"}, errorOf(err)); diff != "" {
		t.Errorf("error (-want +got):\n%s", diff)
	}
}

func TestQuerySqlReplyTooLarge(t *testing.T) {
	c, _ := newClient(t, http.StatusOK, bytes.Repeat([]byte{1}, maxEngineReply+1))
	_, err := querySQL(c, "alice", testApp, "SELECT 1")
	if got := connect.CodeOf(err); got != connect.CodeResourceExhausted {
		t.Errorf("code = %v, want resource_exhausted", got)
	}
}

func TestQuerySqlDeadline(t *testing.T) {
	router := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) { time.Sleep(time.Second) }))
	t.Cleanup(router.Close)
	ctx, cancel := context.WithTimeout(context.Background(), -time.Second)
	defer cancel()
	server := &PlanServer{Router: router.URL, HTTP: http.DefaultClient}
	_, err := server.post(ctx, "alice", testApp, "/sql", arrowStream, nil)
	if got := connect.CodeOf(err); got != connect.CodeDeadlineExceeded {
		t.Errorf("code = %v, want deadline_exceeded", got)
	}
}

func TestQuerySqlClientTimeoutInBody(t *testing.T) {
	router := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		w.(http.Flusher).Flush()
		time.Sleep(time.Second)
	}))
	t.Cleanup(router.Close)
	server := &PlanServer{Router: router.URL, HTTP: &http.Client{Timeout: 100 * time.Millisecond}}
	_, err := server.post(context.Background(), "alice", testApp, "/sql", arrowStream, nil)
	if got := connect.CodeOf(err); got != connect.CodeDeadlineExceeded {
		t.Errorf("code = %v, want deadline_exceeded", got)
	}
}
