// Package router sends each request for a model to that model's current leader engine,
// resending across a leader handover.
package router

import (
	"bytes"
	"cmp"
	"context"
	"encoding/json"
	"errors"
	"io"
	"math/rand/v2"
	"net"
	"net/http"
	"regexp"
	"strings"
	"sync"
	"time"
)

// ErrUnknownModel means the model has no row in the journal.
var ErrUnknownModel = errors.New("unknown model")

// Resolver finds the engine that currently holds a model's lease.
type Resolver interface {
	// Leader returns the live leader URL for model, "" if the model exists but has no live leader,
	// or ErrUnknownModel.
	Leader(ctx context.Context, model string) (string, error)
}

const (
	maxBody        = 16 << 20
	maxHead        = 64 << 10
	attemptTimeout = 70 * time.Second // longer than the engine's own wait for a write (queue 30 s + commit 30 s)
	firstBackoff   = 50 * time.Millisecond
	maxBackoff     = time.Second
)

// modelID matches what the engine accepts; it uses the id unescaped as an object storage key prefix.
var modelID = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$`)

var upstream = &http.Client{
	Timeout: attemptTimeout,
	Transport: &http.Transport{
		DialContext:         (&net.Dialer{Timeout: 5 * time.Second, KeepAlive: 30 * time.Second}).DialContext,
		MaxIdleConnsPerHost: 64,
		IdleConnTimeout:     90 * time.Second,
		DisableCompression:  true,
	},
	CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
}

// Router is an http.Handler for /models/{id}/... that forwards to the model's leader.
type Router struct {
	Resolve    Resolver
	Auth       func(*http.Request) (user string, err error) // nil = no auth (loopback dev)
	UserHeader string                                       // header set toward engines, default "X-Forwarded-User"
	Deadline   time.Duration                                // overall per request, default 90s

	leaders leaderCache
}

func (rt *Router) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path == "/healthz" {
		writeJSON(w, http.StatusOK, map[string]any{"ok": true})
		return
	}
	model, rest, fail := route(r.RequestURI)
	if fail != nil {
		fail.write(w)
		return
	}
	var user string
	if rt.Auth != nil {
		u, err := rt.Auth(r)
		if err != nil {
			apiError{http.StatusUnauthorized, "unauthorized", err.Error()}.write(w)
			return
		}
		user = u
	}
	body, err := io.ReadAll(io.LimitReader(r.Body, maxBody+1))
	if err != nil {
		apiError{http.StatusBadRequest, "bad_request", "本文を読めなかった"}.write(w)
		return
	}
	if len(body) > maxBody {
		apiError{http.StatusRequestEntityTooLarge, "too_large", "本文は 16 MiB まで"}.write(w)
		return
	}
	deadline := rt.Deadline
	if deadline == 0 {
		deadline = 90 * time.Second
	}
	ctx, cancel := context.WithTimeout(r.Context(), deadline)
	defer cancel()
	out := outgoing{method: r.Method, rest: rest, body: body, header: rt.upstreamHeader(r.Header, user)}
	rt.forward(ctx, model, out).write(w)
}

// outgoing is the request as sent on every attempt; body is replayed byte for byte.
type outgoing struct {
	method string
	rest   string // path and query below /models/{id}, escaping untouched
	body   []byte
	header http.Header
}

// forward tries the model's leader until an attempt is worth delivering or ctx ends.
// It returns the response to write: an upstream attempt, or the router's own error.
func (rt *Router) forward(ctx context.Context, model string, out outgoing) writer {
	var (
		target     string
		last       attempt
		resolveErr error
		delay      backoff
		redirected bool
	)
	for {
		if target == "" {
			leader, err := rt.leader(ctx, model)
			if errors.Is(err, ErrUnknownModel) {
				return apiError{http.StatusNotFound, "no_model", "モデル " + model + " はない"}
			}
			target, last, resolveErr = leader, attempt{}, err
		}
		pause := true
		if target != "" {
			last = send(ctx, target, out)
			d := decide(last.status(), last.head)
			switch d.action {
			case deliver:
				return last
			case redirect:
				rt.leaders.set(model, d.leader)
				target = d.leader
				pause = redirected // two engines pointing at each other must not spin
			case retrySame:
			case reresolve:
				rt.leaders.forget(model, target)
				target = ""
			}
			redirected = d.action == redirect
		}
		if ctx.Err() != nil || pause && !sleep(ctx, delay.next()) {
			break
		}
		last.discard()
	}
	if last.resp != nil {
		return last
	}
	if isTimeout(last.err) {
		return apiError{http.StatusGatewayTimeout, "timeout", "書き手の応答を待つ時間を過ぎた（同じ本文で送り直せば二重には確定しない）"}
	}
	msg := "期限までに書き手が見つからなかった"
	if err := cmp.Or(last.err, resolveErr); err != nil {
		msg += "（" + err.Error() + "）"
	}
	return apiError{http.StatusServiceUnavailable, "no_leader", msg}
}

func (rt *Router) leader(ctx context.Context, model string) (string, error) {
	if url := rt.leaders.get(model); url != "" {
		return url, nil
	}
	url, err := rt.Resolve.Leader(ctx, model)
	if url != "" && err == nil {
		rt.leaders.set(model, url)
	}
	return url, err
}

func (rt *Router) upstreamHeader(in http.Header, user string) http.Header {
	h := in.Clone()
	removeHopByHop(h)
	name := rt.UserHeader
	if name == "" {
		name = "X-Forwarded-User"
	}
	h.Del(name)
	h.Del("Content-Length")
	if rt.Auth != nil {
		h.Del("Authorization") // the router consumed the credential
	}
	if user != "" {
		h.Set(name, user)
	}
	return h
}

// route splits "/models/{id}/rest?query" into the model id and the engine-relative remainder,
// keeping the remainder exactly as the client escaped it (metric names may contain %2F).
func route(requestURI string) (model, rest string, fail *apiError) {
	after, ok := strings.CutPrefix(requestURI, "/models/")
	if !ok {
		return "", "", &apiError{http.StatusNotFound, "not_found", requestURI + " はない"}
	}
	end := strings.IndexAny(after, "/?")
	if end < 0 {
		end = len(after)
	}
	model, rest = after[:end], after[end:]
	if !modelID.MatchString(model) {
		return "", "", &apiError{http.StatusBadRequest, "bad_request", "モデルの ID は英数字で始まり、英数字と _ と - の 128 文字まで"}
	}
	if !strings.HasPrefix(rest, "/") {
		rest = "/" + rest
	}
	return model, rest, nil
}

// attempt is one try against an engine: a response with the head of its body, or why there was none.
type attempt struct {
	resp *http.Response
	head []byte
	err  error
}

func (a attempt) status() int {
	if a.resp == nil {
		return 0
	}
	return a.resp.StatusCode
}

func send(ctx context.Context, target string, out outgoing) attempt {
	req, err := http.NewRequestWithContext(ctx, out.method, strings.TrimSuffix(target, "/")+out.rest, bytes.NewReader(out.body))
	if err != nil {
		return attempt{err: err}
	}
	req.Header = out.header.Clone()
	resp, err := upstream.Do(req)
	if err != nil {
		return attempt{err: err}
	}
	head, err := io.ReadAll(io.LimitReader(resp.Body, maxHead))
	if err != nil {
		resp.Body.Close()
		return attempt{err: err}
	}
	return attempt{resp: resp, head: head}
}

func (a attempt) discard() {
	if a.resp != nil {
		a.resp.Body.Close()
	}
}

func (a attempt) write(w http.ResponseWriter) {
	defer a.resp.Body.Close()
	h := w.Header()
	for k, v := range a.resp.Header {
		h[k] = v
	}
	removeHopByHop(h)
	w.WriteHeader(a.resp.StatusCode)
	w.Write(a.head)
	io.Copy(w, a.resp.Body)
}

type action int

const (
	deliver   action = iota
	redirect         // send to decision.leader at once
	retrySame        // back off, same engine
	reresolve        // forget the cached leader, back off, resolve again
)

type decision struct {
	action action
	leader string
}

// decide maps one attempt's outcome to what the router does next. status 0 means no complete response.
// Every resend is safe because the body is unchanged and the engine deduplicates client_op_id.
func decide(status int, head []byte) decision {
	var body struct {
		Error  string  `json:"error"`
		Leader *string `json:"leader"`
	}
	json.Unmarshal(head, &body)
	switch status {
	case 0, http.StatusInternalServerError, http.StatusGatewayTimeout:
		return decision{action: reresolve}
	case http.StatusMisdirectedRequest:
		if body.Leader != nil && isHTTPURL(*body.Leader) {
			return decision{action: redirect, leader: *body.Leader}
		}
		return decision{action: reresolve}
	case http.StatusTooManyRequests:
		return decision{action: retrySame}
	case http.StatusServiceUnavailable:
		switch body.Error {
		case "busy", "stale", "closed":
			return decision{action: reresolve}
		}
	}
	return decision{action: deliver}
}

func isHTTPURL(s string) bool {
	return strings.HasPrefix(s, "http://") || strings.HasPrefix(s, "https://")
}

func isTimeout(err error) bool {
	var ne net.Error
	return errors.Is(err, context.DeadlineExceeded) || errors.As(err, &ne) && ne.Timeout()
}

type backoff struct{ d time.Duration }

func (b *backoff) next() time.Duration {
	b.d = min(max(2*b.d, firstBackoff), maxBackoff)
	return b.d/2 + rand.N(b.d/2+1)
}

func sleep(ctx context.Context, d time.Duration) bool {
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-t.C:
		return true
	case <-ctx.Done():
		return false
	}
}

// leaderCache remembers each model's leader URL between requests.
type leaderCache struct {
	mu sync.Mutex
	m  map[string]string
}

func (c *leaderCache) get(model string) string {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.m[model]
}

func (c *leaderCache) set(model, url string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.m == nil {
		c.m = map[string]string{}
	}
	c.m[model] = url
}

// forget drops model's entry only if it still names url, so a newer leader learned concurrently survives.
func (c *leaderCache) forget(model, url string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.m[model] == url {
		delete(c.m, model)
	}
}

var hopByHop = []string{"Connection", "Proxy-Connection", "Keep-Alive", "Proxy-Authenticate",
	"Proxy-Authorization", "Te", "Trailer", "Transfer-Encoding", "Upgrade"}

func removeHopByHop(h http.Header) {
	for _, v := range h.Values("Connection") {
		for _, name := range strings.Split(v, ",") {
			h.Del(strings.TrimSpace(name))
		}
	}
	for _, name := range hopByHop {
		h.Del(name)
	}
}

// writer is a response the router can deliver: an upstream attempt or its own error.
type writer interface{ write(http.ResponseWriter) }

type apiError struct {
	status  int
	kind    string
	message string
}

func (e apiError) write(w http.ResponseWriter) {
	writeJSON(w, e.status, map[string]any{"error": e.kind, "message": e.message})
}

func writeJSON(w http.ResponseWriter, status int, body any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetEscapeHTML(false)
	enc.Encode(body)
}
