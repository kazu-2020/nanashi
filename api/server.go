// Package api is the application server. It gives the Connect API (nanashi.v1.PlanService) to the frontend.
package api

// This file holds the parts that every feature uses: the server, the RPC rules, the change of a model and the errors.

import (
	"context"
	_ "embed"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"path"
	"sync"

	"connectrpc.com/connect"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"

	nanashiv1 "github.com/kazu-2020/nanashi/api/gen/nanashi/v1"
	"github.com/kazu-2020/nanashi/api/gen/nanashi/v1/nanashiv1connect"
)

//go:embed schema.sql
var schema string

// PlanServer implements PlanService. It keeps its own data in PostgreSQL and the model in the engines.
type PlanServer struct {
	Pool    *pgxpool.Pool
	Engines *Engines
	locks   sync.Map // Application ID to *sync.Mutex.
}

var _ nanashiv1connect.PlanServiceHandler = (*PlanServer)(nil)

type (
	ack     = connect.Response[nanashiv1.Ack]
	role    = nanashiv1.Role
	rpcRule struct {
		min   role // The minimum role in the application. ROLE_UNSPECIFIED: the RPC needs no application.
		audit bool // Record the call in the audit trail.
	}
)

const (
	viewer      = nanashiv1.Role_ROLE_VIEWER
	contributor = nanashiv1.Role_ROLE_CONTRIBUTOR
	modeler     = nanashiv1.Role_ROLE_MODELER
	admin       = nanashiv1.Role_ROLE_ADMIN
)

// rpcRules gives the minimum role and the audit of each RPC. An RPC that is not in the table is refused.
var rpcRules = map[string]rpcRule{
	"ListApplications": {}, "CreateApplication": {audit: true},
	"GetModel": {min: viewer}, "Query": {min: viewer}, "ListComments": {min: viewer}, "AddComment": {viewer, true},
	"ListSnapshots": {min: viewer},
	// The audit detail has the full requests (cells and CSV), and the access rules do not apply to it.
	"ListAudit":  {min: modeler},
	"WriteCells": {contributor, true}, "Import": {contributor, true}, "CreateSnapshot": {contributor, true},
	"CreateList": {modeler, true}, "AddProperty": {modeler, true}, "EditMembers": {modeler, true},
	"CreateCalendar": {modeler, true}, "CreateScenario": {modeler, true}, "SaveMetric": {modeler, true},
	"RenameMetric": {modeler, true}, "DeleteMetric": {modeler, true}, "SaveTable": {modeler, true},
	"SaveView": {modeler, true}, "SaveBoard": {modeler, true}, "DeleteItem": {modeler, true},
	"GetAccess": {min: admin}, "SetMemberRole": {admin, true}, "SaveAccessRule": {admin, true},
	"DeleteAccessRule": {admin, true},
}

// caller is the user of a request and the rights of the user in the application of the request.
type caller struct {
	user  string
	role  role
	rules []*nanashiv1.AccessRule // The access rules of the application. Empty when role >= MODELER.
}

// limitsIn gives the limits of the caller in the model em.
func (c caller) limitsIn(em engineModel) limits { return accessLimits(c.role, c.rules).through(em) }

type callerKey struct{}

func callerOf(ctx context.Context) caller { return ctx.Value(callerKey{}).(caller) }

// dbError hides the database error from the client. It can contain table names and addresses.
// A *connect.Error passes through.
func dbError(err error) error {
	if cerr := new(connect.Error); errors.As(err, &cerr) {
		return err
	}
	log.Printf("database: %v", err)
	return connect.NewError(connect.CodeUnavailable, errors.New("データベースを読み書きできない"))
}

func invalid(err error) error { return connect.NewError(connect.CodeInvalidArgument, err) }

// connectError gives a plan error its Connect code. A *connect.Error passes through, a tagged error gets the
// code of its tag, and any other error is an input error.
func connectError(err error) error {
	if cerr := new(connect.Error); errors.As(err, &cerr) {
		return err
	}
	for sentinel, code := range errorCodes {
		if errors.Is(err, sentinel) {
			return connect.NewError(code, err)
		}
	}
	return connect.NewError(connect.CodeInvalidArgument, err)
}

var errorCodes = map[error]connect.Code{
	errDenied:       connect.CodePermissionDenied,
	errExists:       connect.CodeAlreadyExists,
	errPrecondition: connect.CodeFailedPrecondition,
}

func ok() (*ack, error) { return connect.NewResponse(&nanashiv1.Ack{}), nil }

func textJSON(m map[string]string) string {
	if m == nil {
		m = map[string]string{}
	}
	b, _ := json.Marshal(m)
	return string(b)
}

// plan is one change to an application: the statements for the api tables and the engine operations.
type plan struct {
	ops   []op
	stmts []stmt
}

// listKinds records the kind of the lists in app_list.
func listKinds(app string, kind nanashiv1.ListKind, lists ...string) []stmt {
	var out []stmt
	for _, list := range lists {
		out = append(out, stmt{`insert into app_list (app_id, name, kind) values ($1, $2, $3)
			on conflict (app_id, name) do update set kind = excluded.kind`, []any{app, list, kind}})
	}
	return out
}

// stmt is one SQL statement with its arguments.
type stmt struct {
	sql  string
	args []any
}

// The tags give an error its Connect code (connectError). An error without a tag is an input error.
var (
	errDenied       = errors.New("permission denied")
	errExists       = errors.New("already exists")
	errPrecondition = errors.New("failed precondition")
)

// tagged is an error with a tag. Error() gives only the message of err, so the user sees the Japanese text.
type tagged struct{ err, tag error }

func (t tagged) Error() string   { return t.err.Error() }
func (t tagged) Unwrap() []error { return []error{t.err, t.tag} }

func tag(t error, format string, a ...any) error { return tagged{fmt.Errorf(format, a...), t} }

// The actions follow. They do I/O.

// Migrate makes the api tables (prefix app_) if they are missing.
func Migrate(ctx context.Context, pool *pgxpool.Pool) error {
	_, err := pool.Exec(ctx, schema)
	return err
}

// lock makes the read-modify-write steps of one application run one at a time. Call the result to unlock.
// ponytail: the lock is in one api process. If a second api process runs, change only this body to
// pg_advisory_xact_lock(<2-key>, hashtext(app)) in a transaction on a dedicated connection outside the pool.
// The 2-key form does not collide with the 1-key locks of tessera (docs/engine-lifecycle.md).
func (s *PlanServer) lock(app string) func() {
	v, _ := s.locks.LoadOrStore(app, &sync.Mutex{})
	mu := v.(*sync.Mutex)
	mu.Lock()
	return mu.Unlock
}

// Interceptor identifies the user, checks the role for the RPC, and records the audit trail.
func (s *PlanServer) Interceptor() connect.UnaryInterceptorFunc {
	return func(next connect.UnaryFunc) connect.UnaryFunc {
		return func(ctx context.Context, req connect.AnyRequest) (connect.AnyResponse, error) {
			// The API trusts this header only from this host or from a --trusted-proxy (cmd/nanashi-api).
			user := req.Header().Get("X-Nanashi-User")
			if user == "" {
				return nil, connect.NewError(connect.CodeUnauthenticated, errors.New("X-Nanashi-User がない"))
			}
			method := path.Base(req.Spec().Procedure)
			rule, ok := rpcRules[method]
			if !ok {
				return nil, connect.NewError(connect.CodeUnimplemented, fmt.Errorf("%s は使えない", method))
			}
			c := caller{user: user}
			appID := ""
			if a, ok := req.Any().(interface{ GetAppId() string }); ok {
				appID = a.GetAppId()
			}
			if rule.min != nanashiv1.Role_ROLE_UNSPECIFIED {
				var err error
				if c.role, c.rules, err = s.rights(ctx, appID, user); err != nil {
					return nil, err
				}
				if c.role < rule.min {
					return nil, connect.NewError(connect.CodePermissionDenied, errors.New("この操作をする権限がない"))
				}
			}
			res, err := next(context.WithValue(ctx, callerKey{}, c), req)
			if err == nil && rule.audit {
				if a, ok := res.Any().(*nanashiv1.Application); ok {
					appID = a.Id
				}
				detail, _ := protojson.Marshal(req.Any().(proto.Message))
				if _, err := s.Pool.Exec(ctx, "insert into app_audit (app_id, user_name, action, detail) values ($1, $2, $3, $4)",
					appID, user, method, string(detail)); err != nil {
					log.Printf("audit %s %s: %v", appID, method, err)
				}
			}
			return res, err
		}
	}
}

func (s *PlanServer) ListApplications(ctx context.Context, _ *connect.Request[nanashiv1.ListApplicationsRequest]) (*connect.Response[nanashiv1.ListApplicationsResponse], error) {
	rows, _ := s.Pool.Query(ctx, `select a.id, a.name, m.role from app_application a
		join app_member m on m.app_id = a.id where m.user_name = $1 order by a.created_at, a.id`, callerOf(ctx).user)
	apps, err := pgx.CollectRows(rows, func(row pgx.CollectableRow) (*nanashiv1.Application, error) {
		a := &nanashiv1.Application{}
		return a, row.Scan(&a.Id, &a.Name, &a.Role)
	})
	if err != nil {
		return nil, dbError(err)
	}
	return connect.NewResponse(&nanashiv1.ListApplicationsResponse{Applications: apps}), nil
}

// change applies the plan that planOf makes from the current model and api data of the application. The lock
// makes the plan and the write see the same model. The statements and the engine write are one transaction: an
// engine error rolls the rows back.
func (s *PlanServer) change(ctx context.Context, app string, planOf func(engineModel, appMeta) (plan, error)) (*ack, error) {
	defer s.lock(app)()
	em, _, err := s.Engines.model(ctx, app)
	if err != nil {
		return nil, err
	}
	meta, err := s.meta(ctx, app)
	if err != nil {
		return nil, err
	}
	p, err := planOf(em, meta)
	if err != nil {
		return nil, connectError(err)
	}
	err = pgx.BeginFunc(ctx, s.Pool, func(tx pgx.Tx) error {
		for _, st := range p.stmts {
			if _, err := tx.Exec(ctx, st.sql, st.args...); err != nil {
				return err
			}
		}
		return s.Engines.write(ctx, app, callerOf(ctx).user, p.ops)
	})
	if err != nil {
		return nil, dbError(err)
	}
	return ok()
}
