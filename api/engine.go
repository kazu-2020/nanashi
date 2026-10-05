package api

// All engine requests go through the router. The router starts the engine of a model when a request comes for it.

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
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

// op is one Model operation in a write to the engine (POST /writes).
type op struct {
	Op     string         `json:"op"`
	Args   []any          `json:"args"`
	Kwargs map[string]any `json:"kwargs,omitempty"`
}

func newOp(name string, args ...any) op { return op{Op: name, Args: args} }

func (o op) with(kwargs map[string]any) op {
	o.Kwargs = kwargs
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
	Name    string
	Members []string
	Ordered bool
	Props   []engineProp
}

// engineProp is a DIMENSION property: a member of the list maps to a member of Target.
type engineProp struct {
	Name   string
	Target string
	Values map[string]string
}

type engineMetric struct {
	Name        string
	Dims        []string
	Kind        string
	Formula     string // Empty for an input Metric.
	Overridable bool
}

// engineCube is the body of slice and summary: each cell is the coordinates in dims order and then the value.
// Seq is the version of the model that the engine read. A snapshot stores it too. Replay ignores it.
type engineCube struct {
	Seq   int64    `json:"seq"`
	Dims  []string `json:"dims"`
	Cells [][]any  `json:"cells"`
}

func (m engineModel) dim(name string) (engineDim, bool) {
	i := slices.IndexFunc(m.Dims, func(d engineDim) bool { return d.Name == name })
	if i < 0 {
		return engineDim{}, false
	}
	return m.Dims[i], true
}

func (m engineModel) metric(name string) (engineMetric, error) {
	i := slices.IndexFunc(m.Metrics, func(x engineMetric) bool { return x.Name == name })
	if i < 0 {
		return engineMetric{}, fmt.Errorf("Metric %s がない", name)
	}
	return m.Metrics[i], nil
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
			Members        []string                     `json:"members"`
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
		dim := engineDim{Name: e.key, Members: d.Members, Ordered: d.Ordered}
		for _, p := range props {
			var target string
			if err := json.Unmarshal(p.value, &target); err != nil {
				return engineModel{}, err
			}
			values := d.PropertyValues[p.key]
			if values == nil {
				values = map[string]string{}
			}
			dim.Props = append(dim.Props, engineProp{Name: p.key, Target: target, Values: values})
		}
		out.Dims = append(out.Dims, dim)
	}
	metrics, err := objectEntries(raw.Metrics)
	if err != nil {
		return engineModel{}, err
	}
	for _, e := range metrics {
		var m struct {
			Dims        []string `json:"dims"`
			Kind        string   `json:"kind"`
			Formula     string   `json:"formula"`
			Overridable bool     `json:"overridable"`
		}
		if err := json.Unmarshal(e.value, &m); err != nil {
			return engineModel{}, err
		}
		out.Metrics = append(out.Metrics, engineMetric{Name: e.key, Dims: m.Dims, Kind: m.Kind, Formula: m.Formula, Overridable: m.Overridable})
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

// engineRead is one read of a Metric from the engine: GET /metrics/<metric>/<path>?<query>.
type engineRead struct {
	Metric string
	Path   string // "summary" for a number Metric, "slice" for other kinds, "overrides" for the overrides of a formula.
	Query  map[string][]string
}

// The actions follow.

// Create makes the model of an application in the router. It is idempotent and starts no engine.
func (e *Engines) Create(ctx context.Context, app string) error {
	req, err := http.NewRequestWithContext(ctx, "PUT", e.Router+"/models/"+app, nil)
	if err != nil {
		return err
	}
	_, err = e.do(req)
	return err
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
	return e.do(req)
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

// write sends the operations as one transaction. The client_op_id of the request (opID) lets the router and the
// frontend resend it safely: the engine does not commit the same opID two times.
func (e *Engines) write(ctx context.Context, app, reason, opID string, ops []op) error {
	if len(ops) == 0 {
		return nil
	}
	body, err := json.Marshal(map[string]any{"client_op_id": opID, "reason": reason, "ops": ops})
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, "POST", e.Router+"/models/"+app+"/writes", bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	_, err = e.do(req)
	return err
}

// do sends a request. An engine error with a message for the user becomes a Connect error with that message.
func (e *Engines) do(req *http.Request) ([]byte, error) {
	res, err := e.HTTP.Do(req)
	if err != nil {
		log.Printf("engine %s: %v", req.URL.Path, err)
		return nil, connect.NewError(connect.CodeUnavailable, errors.New("計算エンジンに接続できない"))
	}
	defer res.Body.Close()
	body, err := io.ReadAll(res.Body)
	if err != nil {
		return nil, connect.NewError(connect.CodeUnavailable, errors.New("計算エンジンの応答を読めない"))
	}
	if res.StatusCode == http.StatusOK {
		return body, nil
	}
	var fail struct {
		Message string `json:"message"`
	}
	json.Unmarshal(body, &fail)
	code := map[int]connect.Code{
		http.StatusBadRequest:            connect.CodeInvalidArgument,
		http.StatusNotFound:              connect.CodeNotFound,
		http.StatusConflict:              connect.CodeAborted,
		http.StatusRequestEntityTooLarge: connect.CodeResourceExhausted,
	}[res.StatusCode]
	if code == 0 || fail.Message == "" {
		log.Printf("engine %s: %d %s", req.URL.Path, res.StatusCode, body)
		return nil, connect.NewError(connect.CodeUnavailable, errors.New("計算エンジンが応答しない"))
	}
	return nil, connect.NewError(code, errors.New(fail.Message))
}
