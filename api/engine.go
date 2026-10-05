package api

// All engine requests go through the router. The router starts the engine of a model when a request comes for it.
// docs/ids.md gives the engine HTTP contract: each reference is a UUID.

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"log"
	"net/http"
	"net/url"
	"slices"

	"connectrpc.com/connect"
)

// Engines sends requests to the engine servers through the router.
type Engines struct {
	Router string // Base URL of the router, for example http://127.0.0.1:8090.
	HTTP   *http.Client
}

// op is one Model operation in a write to the engine (POST /writes): {"op": <name>, <argument>: <value>, ...}.
// The arguments have names, so a stored op is readable and the engine checks the argument names.
type op map[string]any

func newOp(name string, args map[string]any) op {
	o := op{"op": name}
	for k, v := range args {
		o[k] = v
	}
	return o
}

// engineModel is the engine definition (GET /) with the order of dimensions, properties and Metrics kept.
// Seq is the version of the model that the engine sent.
type engineModel struct {
	Seq     int64
	Dims    []engineDim
	Metrics []engineMetric
}

type engineDim struct {
	ID      string
	Name    string
	Members []engineMember
	Ordered bool
	Props   []engineProp
}

type engineMember struct {
	ID   string `json:"id"`
	Name string `json:"name"`
}

// engineProp is a DIMENSION property: a member id of the list maps to a member id of Target.
type engineProp struct {
	ID     string
	Name   string
	Target string
	Values map[string]string
}

type engineMetric struct {
	ID          string
	Name        string
	Dims        []string // List ids.
	Kind        string   // "number", "boolean" or "member:<list id>".
	Formula     string   // Empty for an input Metric.
	Overridable bool
}

// engineCube is the body of slice and summary: each cell is the coordinates in dims order and then the value.
// Seq is the version of the model that the engine read. A snapshot stores it too. Replay ignores it.
type engineCube struct {
	Seq   int64    `json:"seq"`
	Dims  []string `json:"dims"`
	Cells [][]any  `json:"cells"`
}

func (m engineModel) dim(id string) (engineDim, bool) {
	i := slices.IndexFunc(m.Dims, func(d engineDim) bool { return d.ID == id })
	if i < 0 {
		return engineDim{}, false
	}
	return m.Dims[i], true
}

func (m engineModel) metric(id string) (engineMetric, bool) {
	i := slices.IndexFunc(m.Metrics, func(x engineMetric) bool { return x.ID == id })
	if i < 0 {
		return engineMetric{}, false
	}
	return m.Metrics[i], true
}

// names gives the names of the lists and the Metrics, by id, for error messages.
func (m engineModel) name(id string) string {
	if d, ok := m.dim(id); ok {
		return d.Name
	}
	if x, ok := m.metric(id); ok {
		return x.Name
	}
	return id
}

func (d engineDim) member(id string) (engineMember, bool) {
	i := slices.IndexFunc(d.Members, func(m engineMember) bool { return m.ID == id })
	if i < 0 {
		return engineMember{}, false
	}
	return d.Members[i], true
}

func (d engineDim) memberIDs() []string {
	out := make([]string, len(d.Members))
	for i, m := range d.Members {
		out[i] = m.ID
	}
	return out
}

func (d engineDim) prop(id string) (engineProp, bool) {
	i := slices.IndexFunc(d.Props, func(p engineProp) bool { return p.ID == id })
	if i < 0 {
		return engineProp{}, false
	}
	return d.Props[i], true
}

func parseEngineModel(body []byte) (engineModel, error) {
	var raw struct {
		Seq        int64           `json:"seq"`
		Dimensions json.RawMessage `json:"dimensions"`
		Metrics    json.RawMessage `json:"metrics"`
	}
	if err := json.Unmarshal(body, &raw); err != nil {
		return engineModel{}, err
	}
	// Go maps lose the key order. The order of dimensions and Metrics is significant (display, replay).
	out := engineModel{Seq: raw.Seq}
	dims, err := objectEntries(raw.Dimensions)
	if err != nil {
		return engineModel{}, err
	}
	for _, e := range dims {
		var d struct {
			Name           string                       `json:"name"`
			Members        []engineMember               `json:"members"`
			Ordered        bool                         `json:"ordered"`
			Properties     json.RawMessage              `json:"properties"`
			PropertyValues map[string]map[string]string `json:"property_values"`
		}
		if err := json.Unmarshal(e.value, &d); err != nil {
			return engineModel{}, err
		}
		props, err := objectEntries(d.Properties)
		if err != nil {
			return engineModel{}, err
		}
		dim := engineDim{ID: e.key, Name: d.Name, Members: d.Members, Ordered: d.Ordered}
		for _, p := range props {
			var prop struct {
				Name   string `json:"name"`
				Target string `json:"target"`
			}
			if err := json.Unmarshal(p.value, &prop); err != nil {
				return engineModel{}, err
			}
			values := d.PropertyValues[p.key]
			if values == nil {
				values = map[string]string{}
			}
			dim.Props = append(dim.Props, engineProp{ID: p.key, Name: prop.Name, Target: prop.Target, Values: values})
		}
		out.Dims = append(out.Dims, dim)
	}
	metrics, err := objectEntries(raw.Metrics)
	if err != nil {
		return engineModel{}, err
	}
	for _, e := range metrics {
		var m struct {
			Name        string   `json:"name"`
			Dims        []string `json:"dims"`
			Kind        string   `json:"kind"`
			Formula     string   `json:"formula"`
			Overridable bool     `json:"overridable"`
		}
		if err := json.Unmarshal(e.value, &m); err != nil {
			return engineModel{}, err
		}
		out.Metrics = append(out.Metrics, engineMetric{ID: e.key, Name: m.Name, Dims: m.Dims, Kind: m.Kind, Formula: m.Formula, Overridable: m.Overridable})
	}
	return out, nil
}

type jsonEntry struct {
	key   string
	value json.RawMessage
}

// objectEntries gives the keys and values of a JSON object in their order.
func objectEntries(raw json.RawMessage) ([]jsonEntry, error) {
	if len(raw) == 0 {
		return nil, nil
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	if _, err := dec.Token(); err != nil {
		return nil, err
	}
	var out []jsonEntry
	for dec.More() {
		t, err := dec.Token()
		if err != nil {
			return nil, err
		}
		var value json.RawMessage
		if err := dec.Decode(&value); err != nil {
			return nil, err
		}
		out = append(out, jsonEntry{t.(string), value})
	}
	return out, nil
}

// engineRead is one read of a Metric from the engine: GET /metrics/<metric id>/<path>?<query>.
type engineRead struct {
	Metric string
	Path   string // "summary" for a number Metric, "slice" for other kinds, "overrides" for the overrides of a formula.
	Query  map[string][]string
}

// engineReply is the status and the body of an engine response. Code and Message come from an error body.
type engineReply struct {
	Status  int
	Body    []byte
	Code    string
	Message string
	Seq     int64
}

// outcome is what a write reply means for the outbox.
type outcome int

const (
	done     outcome = iota // 200: the engine committed the write, now or at an earlier send.
	failed                  // 400 (bad_request or formula) or 409 duplicate_id: the engine refused the write and records the refusal.
	conflict                // 409 conflict: a write in between changed the cells that expect protects. Plan again.
	unknown                 // Anything else: the result is not known. The write stays pending.
)

func (r engineReply) outcome() outcome {
	switch {
	case r.Status == http.StatusOK:
		return done
	case r.Status == http.StatusBadRequest, r.Status == http.StatusConflict && r.Code == "duplicate_id":
		return failed
	case r.Status == http.StatusConflict && r.Code == "conflict":
		return conflict
	}
	return unknown
}

// connectError changes an engine error reply into a Connect error with the message of the engine.
func (r engineReply) connectError() error {
	code := map[int]connect.Code{
		http.StatusBadRequest:            connect.CodeInvalidArgument,
		http.StatusNotFound:              connect.CodeNotFound,
		http.StatusConflict:              connect.CodeAborted,
		http.StatusRequestEntityTooLarge: connect.CodeResourceExhausted,
	}[r.Status]
	if code == 0 || r.Message == "" {
		return connect.NewError(connect.CodeUnavailable, errors.New("計算エンジンが応答しない"))
	}
	return connect.NewError(code, errors.New(r.Message))
}

// The actions follow.

// Create makes the model of an application in the router. It is idempotent and starts no engine.
func (e *Engines) Create(ctx context.Context, app string) error {
	req, err := http.NewRequestWithContext(ctx, "PUT", e.Router+"/models/"+app, nil)
	if err != nil {
		return err
	}
	r, err := e.do(req)
	if err != nil {
		return err
	}
	if r.Status != http.StatusOK {
		return r.connectError()
	}
	return nil
}

func (e *Engines) get(ctx context.Context, app, path string, query url.Values) ([]byte, error) {
	u := e.Router + "/models/" + app + path
	if len(query) > 0 {
		u += "?" + query.Encode()
	}
	req, err := http.NewRequestWithContext(ctx, "GET", u, nil)
	if err != nil {
		return nil, err
	}
	r, err := e.do(req)
	if err != nil {
		return nil, err
	}
	if r.Status != http.StatusOK {
		return nil, r.connectError()
	}
	return r.Body, nil
}

func (e *Engines) model(ctx context.Context, app string) (engineModel, []byte, error) {
	body, err := e.get(ctx, app, "/", nil)
	if err != nil {
		return engineModel{}, nil, err
	}
	em, err := parseEngineModel(body)
	return em, body, err
}

func (e *Engines) read(ctx context.Context, app string, r engineRead) (engineCube, error) {
	var cube engineCube
	body, err := e.get(ctx, app, "/metrics/"+url.PathEscape(r.Metric)+"/"+r.Path, r.Query)
	if err != nil {
		return cube, err
	}
	err = json.Unmarshal(body, &cube)
	return cube, err
}

// write sends the operations as one transaction. The client_op_id of the request (opID) lets the api resend it
// safely: the engine does not commit the same opID two times, and it returns the same refusal again.
// With expect, the engine refuses the write (409 conflict) if a write after that version changed the same cells.
// The error is only for a transport failure: the result is then not known.
func (e *Engines) write(ctx context.Context, app, reason, opID string, ops []op, expect *int64) (engineReply, error) {
	body := map[string]any{"client_op_id": opID, "reason": reason, "ops": ops}
	if expect != nil {
		body["expect"] = *expect
	}
	b, err := json.Marshal(body)
	if err != nil {
		return engineReply{}, err
	}
	req, err := http.NewRequestWithContext(ctx, "POST", e.Router+"/models/"+app+"/writes", bytes.NewReader(b))
	if err != nil {
		return engineReply{}, err
	}
	req.Header.Set("Content-Type", "application/json")
	return e.do(req)
}

// do sends a request and gives the reply. A transport failure is an Unavailable error.
func (e *Engines) do(req *http.Request) (engineReply, error) {
	res, err := e.HTTP.Do(req)
	if err != nil {
		log.Printf("engine %s: %v", req.URL.Path, err)
		return engineReply{}, connect.NewError(connect.CodeUnavailable, errors.New("計算エンジンに接続できない"))
	}
	defer res.Body.Close()
	body, err := io.ReadAll(res.Body)
	if err != nil {
		return engineReply{}, connect.NewError(connect.CodeUnavailable, errors.New("計算エンジンの応答を読めない"))
	}
	r := engineReply{Status: res.StatusCode, Body: body}
	var fail struct {
		Error   string `json:"error"`
		Message string `json:"message"`
		Seq     int64  `json:"seq"`
	}
	json.Unmarshal(body, &fail)
	r.Code, r.Message, r.Seq = fail.Error, fail.Message, fail.Seq
	if r.Status != http.StatusOK {
		log.Printf("engine %s: %d %s", req.URL.Path, r.Status, body)
	}
	return r, nil
}
