package api

import (
	"bytes"
	"cmp"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"maps"
	"net/http"
	"regexp"
	"slices"

	"connectrpc.com/connect"

	nanashiv1 "github.com/kazu-2020/nanashi/api/gen/nanashi/v1"
)

// DimensionServer reads and writes the dimensions of a model through the router.
type DimensionServer struct {
	Engine string // Base URL of the router.
	Client *http.Client
	User   string // The API sends it in X-Forwarded-User. If it is empty, the API sends no user.
}

// modelID is the rule of the router for a model ID.
var modelID = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$`)

// engineOp is one element of "ops" in POST /writes of the engine.
type engineOp struct {
	Op     string         `json:"op"`
	Args   []any          `json:"args"`
	Kwargs map[string]any `json:"kwargs,omitempty"`
}

// engineWrite is the body of POST /writes of the engine.
type engineWrite struct {
	ClientOpID string     `json:"client_op_id"`
	Reason     string     `json:"reason,omitempty"`
	Expect     *int64     `json:"expect,omitempty"`
	Ops        []engineOp `json:"ops"`
}

type engineWritten struct {
	Seq    int64 `json:"seq"`
	Resent bool  `json:"resent"`
}

// engineModel is the dimension part of the body of GET / of the engine.
type engineModel struct {
	Seq        int64                      `json:"seq"`
	Dimensions map[string]engineDimension `json:"dimensions"`
}

type engineDimension struct {
	ID      int64    `json:"id"`
	Members []string `json:"members"` // In the display order.
	IDs     []int64  `json:"ids"`     // The member IDs, in the same order as Members.
	Ordered bool     `json:"ordered"`
	// Property name -> target dimension.
	Properties map[string]string `json:"properties"`
	// Property name -> member -> member of the target dimension.
	Mappings map[string]map[string]string `json:"mappings"`
}

// engineError is the body of an error response of the engine or the router.
type engineError struct {
	Error   string `json:"error"`
	Message string `json:"message"`
}

func (s *DimensionServer) ListDimensions(ctx context.Context, req *connect.Request[nanashiv1.ListDimensionsRequest]) (*connect.Response[nanashiv1.ListDimensionsResponse], error) {
	if !modelID.MatchString(req.Msg.ModelId) {
		return nil, badModelID()
	}
	var m engineModel
	if err := s.call(ctx, http.MethodGet, req.Msg.ModelId, "/", nil, &m); err != nil {
		return nil, err
	}
	for name, d := range m.Dimensions {
		if len(d.IDs) != len(d.Members) {
			log.Printf("ListDimensions %s: dimension %s has %d members and %d ids", req.Msg.ModelId, name, len(d.Members), len(d.IDs))
			return nil, connect.NewError(connect.CodeInternal, errors.New("エンジンの応答を読めません"))
		}
	}
	return connect.NewResponse(&nanashiv1.ListDimensionsResponse{Seq: m.Seq, Dimensions: dimensionsFromEngine(m)}), nil
}

func (s *DimensionServer) WriteDimensions(ctx context.Context, req *connect.Request[nanashiv1.WriteDimensionsRequest]) (*connect.Response[nanashiv1.WriteDimensionsResponse], error) {
	msg := req.Msg
	switch {
	case !modelID.MatchString(msg.ModelId):
		return nil, badModelID()
	case msg.ClientOpId == "":
		return nil, connect.NewError(connect.CodeInvalidArgument, errors.New("client_op_id（再送しても二重に確定しないための ID）が要る"))
	case len(msg.Ops) == 0:
		return nil, connect.NewError(connect.CodeInvalidArgument, errors.New("ops（操作の列）が要る"))
	}
	ops, err := engineOps(msg.Ops)
	if err != nil {
		return nil, connect.NewError(connect.CodeInvalidArgument, err)
	}
	var w engineWritten
	body := engineWrite{ClientOpID: msg.ClientOpId, Reason: msg.Reason, Expect: msg.Expect, Ops: ops}
	if err := s.call(ctx, http.MethodPost, msg.ModelId, "/writes", body, &w); err != nil {
		return nil, err
	}
	return connect.NewResponse(&nanashiv1.WriteDimensionsResponse{Seq: w.Seq, Resent: w.Resent}), nil
}

func badModelID() error {
	return connect.NewError(connect.CodeInvalidArgument, errors.New("モデルの ID は英数字で始まり、英数字と _ と - の 128 文字まで"))
}

// call sends one request to the engine and decodes a 200 response into out.
// It returns a Connect error without the engine URL, because the URL is internal.
func (s *DimensionServer) call(ctx context.Context, method, model, path string, in, out any) error {
	url := s.Engine + "/models/" + model + path
	var body io.Reader
	if in != nil {
		b, err := json.Marshal(in)
		if err != nil {
			return err
		}
		body = bytes.NewReader(b)
	}
	req, err := http.NewRequestWithContext(ctx, method, url, body)
	if err != nil {
		return err
	}
	if in != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if s.User != "" {
		req.Header.Set("X-Forwarded-User", s.User)
	}
	res, err := s.Client.Do(req)
	if err != nil {
		log.Printf("%s %s: %v", method, url, err)
		return statusError(http.StatusServiceUnavailable, "")
	}
	defer res.Body.Close()
	if res.StatusCode != http.StatusOK {
		var e engineError
		json.NewDecoder(res.Body).Decode(&e) // The router can send a body that is not JSON. Then Message stays empty.
		log.Printf("%s %s: %d %s %s", method, url, res.StatusCode, e.Error, e.Message)
		return statusError(res.StatusCode, e.Message)
	}
	if err := json.NewDecoder(res.Body).Decode(out); err != nil {
		log.Printf("%s %s: %v", method, url, err)
		return connect.NewError(connect.CodeInternal, errors.New("エンジンの応答を読めません"))
	}
	return nil
}

// statusError maps an error status of the engine to a Connect error.
// Only the messages of 400 and 409 go to the user. They are already in Japanese.
func statusError(status int, message string) *connect.Error {
	switch {
	case status == http.StatusBadRequest:
		return connect.NewError(connect.CodeInvalidArgument, errors.New(cmp.Or(message, "要求が正しくありません")))
	case status == http.StatusConflict:
		return connect.NewError(connect.CodeAborted, errors.New(cmp.Or(message, "ほかの書き込みと競合しました。読み直してください")))
	case status == http.StatusNotFound:
		return connect.NewError(connect.CodeNotFound, errors.New("モデルがありません"))
	case status == http.StatusUnauthorized:
		return connect.NewError(connect.CodeUnauthenticated, errors.New("エンジンが認証を拒否しました"))
	case status == http.StatusTooManyRequests, status == http.StatusMisdirectedRequest, status >= 500:
		return connect.NewError(connect.CodeUnavailable, errors.New("エンジンを使えません。しばらく待ってから再送してください"))
	}
	return connect.NewError(connect.CodeInternal, errors.New("エンジンが要求を処理できません"))
}

// engineOps maps the operations of the request to the operations of the engine.
func engineOps(ops []*nanashiv1.DimensionOp) ([]engineOp, error) {
	out := make([]engineOp, 0, len(ops))
	for i, op := range ops {
		var e engineOp
		switch o := op.GetOp().(type) {
		case *nanashiv1.DimensionOp_AddDimension:
			a := o.AddDimension
			// The engine needs a list, not null, for the members.
			e = engineOp{"add_dimension", []any{a.Name, append([]string{}, a.Members...)}, map[string]any{"ordered": a.Ordered}}
		case *nanashiv1.DimensionOp_AddMember:
			a := o.AddMember
			kwargs := map[string]any{}
			for p, v := range a.Properties {
				if p == "at" {
					// The engine takes the properties as keyword arguments, and "at" is the position.
					return nil, fmt.Errorf("操作 %d: プロパティ名 at は使えません", i)
				}
				kwargs[p] = v
			}
			if a.At != nil {
				kwargs["at"] = *a.At
			}
			e = engineOp{"add_member", []any{a.Dimension, a.Name}, kwargs}
		case *nanashiv1.DimensionOp_MoveMember:
			a := o.MoveMember
			e = engineOp{"move_member", []any{a.Dimension, a.Name, a.At}, nil}
		case *nanashiv1.DimensionOp_RenameMember:
			a := o.RenameMember
			e = engineOp{"rename_member", []any{a.Dimension, a.OldName, a.NewName}, nil}
		case *nanashiv1.DimensionOp_RemoveMember:
			a := o.RemoveMember
			e = engineOp{"remove_member", []any{a.Dimension, a.Name}, nil}
		case *nanashiv1.DimensionOp_AddProperty:
			a := o.AddProperty
			values := maps.Clone(a.Values)
			if values == nil {
				values = map[string]string{}
			}
			e = engineOp{"add_property", []any{a.Dimension, a.Name, a.TargetDimension, values}, nil}
		default:
			return nil, fmt.Errorf("操作 %d が空です", i)
		}
		out = append(out, e)
	}
	return out, nil
}

// dimensionsFromEngine builds the dimensions in the order of their IDs, because a JSON object has no order.
// The caller makes sure that each dimension has one ID for each member.
func dimensionsFromEngine(m engineModel) []*nanashiv1.Dimension {
	out := make([]*nanashiv1.Dimension, 0, len(m.Dimensions))
	for name, d := range m.Dimensions {
		dim := &nanashiv1.Dimension{Id: d.ID, Name: name, Ordered: d.Ordered}
		for i, member := range d.Members {
			dim.Members = append(dim.Members, &nanashiv1.Member{Id: d.IDs[i], Name: member})
		}
		for _, p := range slices.Sorted(maps.Keys(d.Properties)) {
			dim.Properties = append(dim.Properties, &nanashiv1.Property{Name: p, TargetDimension: d.Properties[p], Values: d.Mappings[p]})
		}
		out = append(out, dim)
	}
	slices.SortFunc(out, func(a, b *nanashiv1.Dimension) int { return cmp.Compare(a.Id, b.Id) })
	return out
}
