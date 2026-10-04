package api

// This file holds the actions on the engine servers: the HTTP client (through the router) and the supervisor
// that starts one engine process for each application.

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"sync"
	"syscall"
	"time"

	"connectrpc.com/connect"
)

// Engines sends requests to the engine servers through the router, and starts and stops the engine processes.
type Engines struct {
	Router  string // Base URL of the router, for example http://127.0.0.1:8090.
	Tessera string // Path to tessera/.
	Dir     string // Directory for the engine files. Each application uses <Dir>/<app id>.
	DSN     string
	HTTP    *http.Client

	mu    sync.Mutex
	procs map[string]*engineProc
}

type engineProc struct {
	cmd  *exec.Cmd
	done chan struct{}
}

func newID() string {
	b := make([]byte, 8)
	rand.Read(b)
	return hex.EncodeToString(b)
}

// Start starts the engine of an application if it does not run, and waits until the router finds it.
func (e *Engines) Start(ctx context.Context, app string) error {
	e.mu.Lock()
	p, ok := e.procs[app]
	if !ok {
		cmd := exec.Command(filepath.Join(e.Tessera, ".venv/bin/python"), "-m", "sparse_engine.server",
			filepath.Join(e.Dir, app), "--pg", e.DSN, "--model-id", app, "--migrate", "--port", "0")
		cmd.Dir = e.Tessera
		cmd.Stdout, cmd.Stderr = os.Stderr, os.Stderr
		if err := cmd.Start(); err != nil {
			e.mu.Unlock()
			return err
		}
		p = &engineProc{cmd: cmd, done: make(chan struct{})}
		go func() {
			err := cmd.Wait()
			log.Printf("engine %s stopped: %v", app, err)
			e.mu.Lock()
			delete(e.procs, app)
			e.mu.Unlock()
			close(p.done)
		}()
		if e.procs == nil {
			e.procs = map[string]*engineProc{}
		}
		e.procs[app] = p
	}
	done := p.done
	e.mu.Unlock()
	ctx, cancel := context.WithTimeout(ctx, 90*time.Second)
	defer cancel()
	for {
		req, _ := http.NewRequestWithContext(ctx, "GET", e.Router+"/models/"+app+"/ready", nil)
		if res, err := e.HTTP.Do(req); err == nil {
			res.Body.Close()
			if res.StatusCode == http.StatusOK {
				return nil
			}
		}
		select {
		case <-done:
			return fmt.Errorf("engine %s stopped before it was ready", app)
		case <-ctx.Done():
			return fmt.Errorf("engine %s is not ready: %w", app, ctx.Err())
		case <-time.After(200 * time.Millisecond):
		}
	}
}

// StopAll sends SIGTERM to the engines that this process started, and waits until they stop.
func (e *Engines) StopAll() {
	e.mu.Lock()
	procs := make([]*engineProc, 0, len(e.procs))
	for _, p := range e.procs {
		procs = append(procs, p)
	}
	e.mu.Unlock()
	for _, p := range procs {
		p.cmd.Process.Signal(syscall.SIGTERM)
	}
	for _, p := range procs {
		select {
		case <-p.done:
		case <-time.After(15 * time.Second):
			p.cmd.Process.Kill()
		}
	}
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
