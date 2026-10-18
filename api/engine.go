package api

// All engine requests go through the router. The router starts the engine of a model when a request comes for it.

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"log"
	"net/http"

	"connectrpc.com/connect"
)

// maxEngineReply is the largest engine body that the api reads. The Arrow dictionary of a dimension has all its
// members, so the --max-cells of the engine does not limit the bytes.
const maxEngineReply = 64 << 20

// engineStatus gives the Connect code for each router or engine status that has a message for the user.
// Each router and engine error body is {"error": <kind>, "message": <Japanese text>}.
var engineStatus = map[int]connect.Code{
	http.StatusBadRequest:            connect.CodeInvalidArgument,
	http.StatusNotFound:              connect.CodeNotFound,
	http.StatusRequestEntityTooLarge: connect.CodeResourceExhausted,
	http.StatusNotImplemented:        connect.CodeUnimplemented,
	http.StatusServiceUnavailable:    connect.CodeUnavailable,
}

// engineError changes a reply that is not 200 to a Connect error. It keeps the message for a status in
// engineStatus. For other statuses it hides the body, because the user cannot act on it.
func engineError(status int, body []byte) error {
	var reply struct {
		Message string `json:"message"`
	}
	_ = json.Unmarshal(body, &reply) // A body that is not JSON has no message.
	code, ok := engineStatus[status]
	if !ok || reply.Message == "" {
		return connect.NewError(connect.CodeUnavailable, errors.New("計算エンジンが応答しない"))
	}
	return connect.NewError(code, errors.New(reply.Message))
}

// The actions follow.

// post sends a JSON body to /models/<app>/<path> on the router and gives the 200 body.
// checkRequest makes sure that app is a canonical UUID, so the path needs no escape.
func (s *PlanServer) post(ctx context.Context, user, app, path, accept string, body any) ([]byte, error) {
	b, err := json.Marshal(body)
	if err != nil {
		return nil, connect.NewError(connect.CodeInternal, err)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, s.Router+"/models/"+app+path, bytes.NewReader(b))
	if err != nil {
		return nil, connect.NewError(connect.CodeInternal, err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", accept)
	// A router with --user-header X-Nanashi-User records the user.
	req.Header.Set("X-Nanashi-User", user)
	res, err := s.HTTP.Do(req)
	if err != nil {
		log.Printf("engine %s %s: %v", app, path, err)
		return nil, connect.NewError(connect.CodeUnavailable, errors.New("計算エンジンに接続できない"))
	}
	defer func() { _ = res.Body.Close() }()
	reply, err := io.ReadAll(io.LimitReader(res.Body, maxEngineReply+1))
	if err != nil {
		log.Printf("engine %s %s: %v", app, path, err)
		return nil, connect.NewError(connect.CodeUnavailable, errors.New("計算エンジンの応答を読めない"))
	}
	if len(reply) > maxEngineReply {
		return nil, connect.NewError(connect.CodeResourceExhausted,
			errors.New("計算エンジンの応答が 64 MiB を超える（LIMIT を付けるか、列を減らす）"))
	}
	if res.StatusCode != http.StatusOK {
		log.Printf("engine %s %s: %d %s", app, path, res.StatusCode, reply)
		return nil, engineError(res.StatusCode, reply)
	}
	return reply, nil
}
