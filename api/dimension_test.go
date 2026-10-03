package api

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"

	"connectrpc.com/connect"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"

	nanashiv1 "github.com/kazu-2020/nanashi/api/gen/nanashi/v1"
	"github.com/kazu-2020/nanashi/api/gen/nanashi/v1/nanashiv1connect"
)

func TestEngineOps(t *testing.T) {
	at := int32(2)
	tests := []struct {
		name string
		op   *nanashiv1.DimensionOp
		want string // JSON of the engine op, or "error: <text in the error>".
	}{
		{"add_dimension", &nanashiv1.DimensionOp{Op: &nanashiv1.DimensionOp_AddDimension{AddDimension: &nanashiv1.AddDimension{Name: "Month", Members: []string{"Jan", "Feb"}, Ordered: true}}},
			`{"op":"add_dimension","args":["Month",["Jan","Feb"]],"kwargs":{"ordered":true}}`},
		{"add_dimension without members", &nanashiv1.DimensionOp{Op: &nanashiv1.DimensionOp_AddDimension{AddDimension: &nanashiv1.AddDimension{Name: "Region"}}},
			`{"op":"add_dimension","args":["Region",[]],"kwargs":{"ordered":false}}`},
		{"add_member at the end", &nanashiv1.DimensionOp{Op: &nanashiv1.DimensionOp_AddMember{AddMember: &nanashiv1.AddMember{Dimension: "Region", Name: "EU"}}},
			`{"op":"add_member","args":["Region","EU"]}`},
		{"add_member at a position", &nanashiv1.DimensionOp{Op: &nanashiv1.DimensionOp_AddMember{AddMember: &nanashiv1.AddMember{Dimension: "Region", Name: "EU", At: &at, Properties: map[string]string{"Country": "FR"}}}},
			`{"op":"add_member","args":["Region","EU"],"kwargs":{"at":2,"Country":"FR"}}`},
		{"add_member with property at", &nanashiv1.DimensionOp{Op: &nanashiv1.DimensionOp_AddMember{AddMember: &nanashiv1.AddMember{Dimension: "Region", Name: "EU", Properties: map[string]string{"at": "x"}}}},
			"error: at"},
		{"move_member", &nanashiv1.DimensionOp{Op: &nanashiv1.DimensionOp_MoveMember{MoveMember: &nanashiv1.MoveMember{Dimension: "Region", Name: "EU", At: 0}}},
			`{"op":"move_member","args":["Region","EU",0]}`},
		{"rename_member", &nanashiv1.DimensionOp{Op: &nanashiv1.DimensionOp_RenameMember{RenameMember: &nanashiv1.RenameMember{Dimension: "Region", OldName: "EU", NewName: "Europe"}}},
			`{"op":"rename_member","args":["Region","EU","Europe"]}`},
		{"remove_member", &nanashiv1.DimensionOp{Op: &nanashiv1.DimensionOp_RemoveMember{RemoveMember: &nanashiv1.RemoveMember{Dimension: "Region", Name: "EU"}}},
			`{"op":"remove_member","args":["Region","EU"]}`},
		{"add_property", &nanashiv1.DimensionOp{Op: &nanashiv1.DimensionOp_AddProperty{AddProperty: &nanashiv1.AddProperty{Dimension: "Country", Name: "Region", TargetDimension: "Region", Values: map[string]string{"FR": "EU"}}}},
			`{"op":"add_property","args":["Country","Region","Region",{"FR":"EU"}]}`},
		{"add_property without values", &nanashiv1.DimensionOp{Op: &nanashiv1.DimensionOp_AddProperty{AddProperty: &nanashiv1.AddProperty{Dimension: "Country", Name: "Region", TargetDimension: "Region"}}},
			`{"op":"add_property","args":["Country","Region","Region",{}]}`},
		{"empty oneof", &nanashiv1.DimensionOp{}, "error: 空"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			ops, err := engineOps([]*nanashiv1.DimensionOp{tt.op})
			if want, ok := strings.CutPrefix(tt.want, "error: "); ok {
				if err == nil || !strings.Contains(err.Error(), want) {
					t.Fatalf("got %v, %v; want an error with %q", ops, err, want)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			got, _ := json.Marshal(ops[0])
			assertJSON(t, got, tt.want)
		})
	}
}

func TestDimensionsFromEngine(t *testing.T) {
	var m engineModel
	// The display order (b, a) differs from the ID order (a=1, b=2). Region (id 1) comes after Country (id 0) in the JSON order too.
	body := `{"seq": 3, "dimensions": {
		"Region": {"id": 1, "members": ["EU", "US"], "ids": [5, 4], "ordered": false, "properties": {}, "mappings": {}},
		"Country": {"id": 0, "members": ["FR", "DE", "JP"], "ids": [2, 0, 1], "ordered": false,
			"properties": {"Region": "Region", "Alt": "Region"}, "mappings": {"Region": {"FR": "EU", "DE": "EU"}, "Alt": {}}}
	}, "metrics": {"Sales": {"id": 0}}}`
	if err := json.Unmarshal([]byte(body), &m); err != nil {
		t.Fatal(err)
	}
	got := &nanashiv1.ListDimensionsResponse{Dimensions: dimensionsFromEngine(m)}
	want := &nanashiv1.ListDimensionsResponse{Dimensions: []*nanashiv1.Dimension{
		{Id: 0, Name: "Country",
			Members: []*nanashiv1.Member{{Id: 2, Name: "FR"}, {Id: 0, Name: "DE"}, {Id: 1, Name: "JP"}},
			Properties: []*nanashiv1.Property{
				{Name: "Alt", TargetDimension: "Region", Values: map[string]string{}},
				{Name: "Region", TargetDimension: "Region", Values: map[string]string{"FR": "EU", "DE": "EU"}},
			}},
		{Id: 1, Name: "Region", Members: []*nanashiv1.Member{{Id: 5, Name: "EU"}, {Id: 4, Name: "US"}}},
	}}
	if !proto.Equal(got, want) {
		t.Errorf("got %v\nwant %v", protojson.Format(got), protojson.Format(want))
	}
}

// fakeEngine records the requests and returns the responses in order.
type fakeEngine struct {
	paths, users, bodies []string
	responses            []fakeResponse
}

type fakeResponse struct {
	status int
	body   string
}

func (f *fakeEngine) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	b, _ := io.ReadAll(r.Body)
	f.paths = append(f.paths, r.Method+" "+r.URL.Path)
	f.users = append(f.users, r.Header.Get("X-Forwarded-User"))
	f.bodies = append(f.bodies, string(b))
	res := f.responses[0]
	f.responses = f.responses[1:]
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(res.status)
	io.WriteString(w, res.body)
}

func dimensionClient(t *testing.T, engine *fakeEngine, user string) nanashiv1connect.DimensionServiceClient {
	eng := httptest.NewServer(engine)
	t.Cleanup(eng.Close)
	mux := http.NewServeMux()
	mux.Handle(nanashiv1connect.NewDimensionServiceHandler(&DimensionServer{Engine: eng.URL, Client: eng.Client(), User: user}))
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	return nanashiv1connect.NewDimensionServiceClient(srv.Client(), srv.URL)
}

func TestWriteDimensions(t *testing.T) {
	engine := &fakeEngine{responses: []fakeResponse{
		{200, `{"seq": 7}`},
		{200, `{"seq": 7, "resent": true}`},
		{400, `{"error": "bad_request", "message": "軸 Region がない"}`},
		{409, `{"error": "conflict", "message": "ほかの人が先に変えた", "seq": 8, "user": "bob"}`},
		{503, `{"error": "no_leader", "message": "書き手がいない"}`},
	}}
	client := dimensionClient(t, engine, "alice")
	expect := int64(6)
	req := &nanashiv1.WriteDimensionsRequest{ModelId: "plan-2027", ClientOpId: "op-1", Expect: &expect, Reason: "new region", Ops: []*nanashiv1.DimensionOp{
		{Op: &nanashiv1.DimensionOp_AddDimension{AddDimension: &nanashiv1.AddDimension{Name: "Region", Members: []string{"EU"}}}},
		{Op: &nanashiv1.DimensionOp_RenameMember{RenameMember: &nanashiv1.RenameMember{Dimension: "Region", OldName: "EU", NewName: "Europe"}}},
	}}
	write := func() (*nanashiv1.WriteDimensionsResponse, error) {
		res, err := client.WriteDimensions(context.Background(), connect.NewRequest(req))
		if err != nil {
			return nil, err
		}
		return res.Msg, nil
	}

	res, err := write()
	if err != nil || res.Seq != 7 || res.Resent {
		t.Fatalf("got %v, %v; want seq 7", res, err)
	}
	if engine.paths[0] != "POST /models/plan-2027/writes" || engine.users[0] != "alice" {
		t.Errorf("got %q user %q", engine.paths[0], engine.users[0])
	}
	assertJSON(t, []byte(engine.bodies[0]), `{"client_op_id": "op-1", "reason": "new region", "expect": 6, "ops": [
		{"op": "add_dimension", "args": ["Region", ["EU"]], "kwargs": {"ordered": false}},
		{"op": "rename_member", "args": ["Region", "EU", "Europe"]}]}`)

	res, err = write()
	if err != nil || res.Seq != 7 || !res.Resent {
		t.Fatalf("got %v, %v; want seq 7 resent", res, err)
	}

	for _, want := range []struct {
		code    connect.Code
		message string
	}{
		{connect.CodeInvalidArgument, "軸 Region がない"},
		{connect.CodeAborted, "ほかの人が先に変えた"},
		{connect.CodeUnavailable, "エンジンを使えません"},
	} {
		_, err := write()
		var ce *connect.Error
		if !errors.As(err, &ce) || ce.Code() != want.code || !strings.Contains(ce.Message(), want.message) {
			t.Errorf("got %v, want %v with %q", err, want.code, want.message)
		}
		if err != nil && strings.Contains(err.Error(), "127.0.0.1") {
			t.Errorf("the error shows the engine address: %v", err)
		}
	}
	if len(engine.paths) != 5 {
		t.Errorf("the engine got %d requests, want 5", len(engine.paths))
	}
}

func TestWriteDimensionsRejectsBadRequests(t *testing.T) {
	engine := &fakeEngine{}
	client := dimensionClient(t, engine, "")
	op := []*nanashiv1.DimensionOp{{Op: &nanashiv1.DimensionOp_RemoveMember{RemoveMember: &nanashiv1.RemoveMember{Dimension: "Region", Name: "EU"}}}}
	for _, req := range []*nanashiv1.WriteDimensionsRequest{
		{ModelId: "../secret", ClientOpId: "op-1", Ops: op},
		{ModelId: "", ClientOpId: "op-1", Ops: op},
		{ModelId: strings.Repeat("a", 129), ClientOpId: "op-1", Ops: op},
		{ModelId: "plan", ClientOpId: "", Ops: op},
		{ModelId: "plan", ClientOpId: "op-1"},
		{ModelId: "plan", ClientOpId: "op-1", Ops: []*nanashiv1.DimensionOp{{}}},
	} {
		_, err := client.WriteDimensions(context.Background(), connect.NewRequest(req))
		if connect.CodeOf(err) != connect.CodeInvalidArgument {
			t.Errorf("%v: got %v, want invalid_argument", req, err)
		}
	}
	if len(engine.paths) != 0 {
		t.Errorf("the engine got %v, want no requests", engine.paths)
	}
}

func TestListDimensions(t *testing.T) {
	engine := &fakeEngine{responses: []fakeResponse{
		{200, `{"seq": 4, "dimensions": {"Region": {"id": 0, "members": ["EU"], "ids": [0], "ordered": false, "properties": {}, "mappings": {}}}, "metrics": {}}`},
		{404, `{"error": "no_model"}`},
	}}
	client := dimensionClient(t, engine, "")
	res, err := client.ListDimensions(context.Background(), connect.NewRequest(&nanashiv1.ListDimensionsRequest{ModelId: "plan"}))
	if err != nil || res.Msg.Seq != 4 || len(res.Msg.Dimensions) != 1 || res.Msg.Dimensions[0].Members[0].Name != "EU" {
		t.Fatalf("got %v, %v", res, err)
	}
	if engine.paths[0] != "GET /models/plan/" || engine.users[0] != "" {
		t.Errorf("got %q user %q, want no user", engine.paths[0], engine.users[0])
	}
	_, err = client.ListDimensions(context.Background(), connect.NewRequest(&nanashiv1.ListDimensionsRequest{ModelId: "plan"}))
	if connect.CodeOf(err) != connect.CodeNotFound {
		t.Errorf("got %v, want not_found", err)
	}
}

func assertJSON(t *testing.T, got []byte, want string) {
	t.Helper()
	var g, w any
	if err := json.Unmarshal(got, &g); err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal([]byte(want), &w); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(g, w) {
		t.Errorf("got %s\nwant %s", got, want)
	}
}
