package api

// This file holds the actions on the engine servers: the HTTP client. All requests go through the router,
// and the router starts the engine of a model when a request comes for it.

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"log"
	"net/http"
	"net/url"

	"connectrpc.com/connect"
)

// Engines sends requests to the engine servers through the router.
type Engines struct {
	Router string // Base URL of the router, for example http://127.0.0.1:8090.
	HTTP   *http.Client
}

func newID() string {
	b := make([]byte, 8)
	rand.Read(b)
	return hex.EncodeToString(b)
}

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

// write sends the operations as one transaction. A new client_op_id lets the router resend it safely.
func (e *Engines) write(ctx context.Context, app, reason string, ops []op) error {
	if len(ops) == 0 {
		return nil
	}
	body, err := json.Marshal(map[string]any{"client_op_id": newID(), "reason": reason, "ops": ops})
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
