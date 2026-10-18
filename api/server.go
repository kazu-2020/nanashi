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
	Router string       // Base URL of nanashi-router, for example http://127.0.0.1:8090.
	HTTP   *http.Client // Its Timeout is longer than the --deadline of the router (90 s).
}

// checkRequest refuses a request without a user, or with a field that breaks a rule in proto/.
// The message names the field, for example "app_id が正規形の UUID ではない".
func checkRequest(user string, m proto.Message) error {
	if user == "" {
		return connect.NewError(connect.CodeUnauthenticated, errors.New("X-Nanashi-User がない"))
	}
	verr := new(protovalidate.ValidationError)
	if err := protovalidate.Validate(m); errors.As(err, &verr) {
		v := verr.Violations[0].Proto
		return connect.NewError(connect.CodeInvalidArgument,
			fmt.Errorf("%s が%s", protovalidate.FieldPathString(v.GetField()), v.GetMessage()))
	} else if err != nil {
		return connect.NewError(connect.CodeInternal, err)
	}
	return nil
}

// The actions follow.

// Interceptor checks each request with checkRequest before the handler runs.
func Interceptor() connect.UnaryInterceptorFunc {
	return func(next connect.UnaryFunc) connect.UnaryFunc {
		return func(ctx context.Context, req connect.AnyRequest) (connect.AnyResponse, error) {
			// The API trusts this header only from this host or from a --trusted-proxy (cmd/nanashi-api).
			if err := checkRequest(req.Header().Get("X-Nanashi-User"), req.Any().(proto.Message)); err != nil {
				return nil, err
			}
			return next(ctx, req)
		}
	}
}
