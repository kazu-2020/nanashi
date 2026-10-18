package api

import (
	"context"
	"errors"
	"fmt"
	"net/http"

	"buf.build/go/protovalidate"
	"connectrpc.com/connect"
	"google.golang.org/protobuf/proto"

	"github.com/kazu-2020/nanashi/api/gen/nanashi/v1/nanashiv1connect"
)

// PlanServer implements PlanService. The RPCs that it does not implement give Unimplemented.
type PlanServer struct {
	nanashiv1connect.UnimplementedPlanServiceHandler
	Router string
	HTTP   *http.Client // Timeout is longer than the router --deadline (90 s).
}

// checkRequest refuses a request without a user, or with a field that breaks a rule in proto/.
func checkRequest(user string, m proto.Message) error {
	if user == "" {
		return connect.NewError(connect.CodeUnauthenticated, errors.New("X-Nanashi-User がない"))
	}
	verr := new(protovalidate.ValidationError)
	switch err := protovalidate.Validate(m); {
	case errors.As(err, &verr):
		v := verr.Violations[0].Proto
		return connect.NewError(connect.CodeInvalidArgument,
			fmt.Errorf("%s が%s", protovalidate.FieldPathString(v.GetField()), v.GetMessage()))
	case err != nil:
		return connect.NewError(connect.CodeInternal, err)
	}
	return nil
}

// The actions follow.

// Interceptor checks each request with checkRequest before the handler runs.
func Interceptor() connect.UnaryInterceptorFunc {
	return func(next connect.UnaryFunc) connect.UnaryFunc {
		return func(ctx context.Context, req connect.AnyRequest) (connect.AnyResponse, error) {
			if err := checkRequest(req.Header().Get("X-Nanashi-User"), req.Any().(proto.Message)); err != nil {
				return nil, err
			}
			return next(ctx, req)
		}
	}
}
