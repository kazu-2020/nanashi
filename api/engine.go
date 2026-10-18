package api

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"log"
	"net/http"
	"net/url"

	"connectrpc.com/connect"
)

// maxEngineReply is the largest engine body that the api reads. The Arrow dictionary of a dimension has all its
// members, so the --max-cells of the engine does not limit the bytes.
const maxEngineReply = 64 << 20

// Each router and engine error body is {"error": <kind>, "message": <Japanese text>}.
var engineStatus = map[int]connect.Code{
	http.StatusBadRequest:            connect.CodeInvalidArgument,
	http.StatusNotFound:              connect.CodeNotFound,
	http.StatusRequestEntityTooLarge: connect.CodeResourceExhausted,
	http.StatusNotImplemented:        connect.CodeUnimplemented,
	http.StatusServiceUnavailable:    connect.CodeUnavailable,
}

// engineError hides the body for a status not in engineStatus, because the user cannot act on it.
func engineError(status int, body []byte) error {
	var reply struct {
		Message string `json:"message"`
	}
	_ = json.Unmarshal(body, &reply)
	code, ok := engineStatus[status]
	if !ok || reply.Message == "" {
		return connect.NewError(connect.CodeUnavailable, errors.New("計算エンジンが応答しない"))
	}
	return connect.NewError(code, errors.New(reply.Message))
}

// The actions follow.

// transportError keeps a cancel or a timeout (of the request context or of the client) as its own code.
// It logs other errors and gives Unavailable with message.
func transportError(err error, app, path, message string) error {
	var timeout interface{ Timeout() bool }
	switch {
	case errors.Is(err, context.Canceled):
		return connect.NewError(connect.CodeCanceled, err)
	case errors.As(err, &timeout) && timeout.Timeout():
		return connect.NewError(connect.CodeDeadlineExceeded, err)
	}
	log.Printf("engine %s %s: %v", app, path, err)
	return connect.NewError(connect.CodeUnavailable, errors.New(message))
}

func (s *PlanServer) post(ctx context.Context, user, app, path, accept string, body any) ([]byte, error) {
	b, err := json.Marshal(body)
	if err != nil {
		return nil, connect.NewError(connect.CodeInternal, err)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, s.Router+"/models/"+url.PathEscape(app)+path, bytes.NewReader(b))
	if err != nil {
		return nil, connect.NewError(connect.CodeInternal, err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", accept)
	// A router with --user-header X-Nanashi-User records the user.
	req.Header.Set("X-Nanashi-User", user)
	res, err := s.HTTP.Do(req)
	if err != nil {
		return nil, transportError(err, app, path, "計算エンジンに接続できない")
	}
	defer func() { _ = res.Body.Close() }()
	if res.StatusCode != http.StatusOK {
		reply, _ := io.ReadAll(io.LimitReader(res.Body, 64<<10))
		log.Printf("engine %s %s: %d %s", app, path, res.StatusCode, reply)
		return nil, engineError(res.StatusCode, reply)
	}
	reply, err := io.ReadAll(io.LimitReader(res.Body, maxEngineReply+1))
	if err != nil {
		return nil, transportError(err, app, path, "計算エンジンの応答を読めない")
	}
	if len(reply) > maxEngineReply {
		return nil, connect.NewError(connect.CodeResourceExhausted,
			errors.New("計算エンジンの応答が 64 MiB を超える（LIMIT を付けるか、列を減らす）"))
	}
	return reply, nil
}
