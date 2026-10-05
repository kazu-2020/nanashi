package api

import (
	"context"
	"errors"
	"fmt"
	"maps"
	"slices"
	"strconv"
	"strings"

	"connectrpc.com/connect"
	"github.com/jackc/pgx/v5"
	"google.golang.org/protobuf/proto"

	nanashiv1 "github.com/kazu-2020/nanashi/api/gen/nanashi/v1"
)

// limit is the members (ids) of one list that a user can read and write.
type limit struct{ read, write map[string]bool }

// limits is list id to limit. A list without an entry has no limit.
type limits map[string]limit

// accessLimits gives the limits of the rules of the role. The rules are fail-closed: a member that the model
// does not have is dropped from the rule, so an empty rule shows nothing. A rule on a list that the model does
// not have is inactive.
func accessLimits(role nanashiv1.Role, rules []*nanashiv1.AccessRule, em engineModel) limits {
	out := limits{}
	if role >= nanashiv1.Role_ROLE_MODELER {
		return out
	}
	for _, r := range rules {
		d, ok := em.dim(r.List)
		if r.Role != role || !ok {
			continue
		}
		read := map[string]bool{}
		for _, m := range r.Members {
			if _, ok := d.member(m); ok {
				read[m] = true
			}
		}
		write := map[string]bool{}
		if r.Write {
			write = read
		}
		if old, ok := out[r.List]; ok {
			read, write = intersect(old.read, read), intersect(old.write, write)
		}
		out[r.List] = limit{read: read, write: write}
	}
	return out
}

// through also limits each list with a DIMENSION property on a limited list, for example the rows of a
// transaction list with a Product property when Product has a rule. A member with a blank value is hidden.
func (l limits) through(em engineModel) limits {
	if len(l) == 0 {
		return l
	}
	out := maps.Clone(l)
	for range em.Dims { // Each pass follows one more property step, so len(Dims) passes reach all chains.
		for _, d := range em.Dims {
			lim, limited := l[d.ID]
			for _, p := range d.Props {
				target, ok := out[p.Target]
				if !ok || p.Target == d.ID {
					continue
				}
				derived := limit{read: map[string]bool{}, write: map[string]bool{}}
				for _, m := range d.Members {
					if target.read[p.Values[m.ID]] {
						derived.read[m.ID] = true
					}
					if target.write[p.Values[m.ID]] {
						derived.write[m.ID] = true
					}
				}
				if limited {
					derived = limit{read: intersect(lim.read, derived.read), write: intersect(lim.write, derived.write)}
				}
				lim, limited = derived, true
			}
			if limited {
				out[d.ID] = lim
			}
		}
	}
	return out
}

func intersect(a, b map[string]bool) map[string]bool {
	out := map[string]bool{}
	for k := range a {
		if b[k] {
			out[k] = true
		}
	}
	return out
}

// hides tells if a formula Metric can show data of hidden members: it lacks a limited list, so it can aggregate the
// list away (for example Budget[REMOVE SUM: Product]). An input Metric without the list holds no data about it.
func (l limits) hides(m engineMetric) bool {
	for list := range l {
		if m.Formula != "" && !slices.Contains(m.Dims, list) {
			return true
		}
	}
	return false
}

func (l limits) visible(list, member string) bool {
	lim, ok := l[list]
	return !ok || lim.read[member]
}

// checkWrite refuses a write to a cell outside the write members, or a write that leaves a ruled list open.
func (l limits) checkWrite(metric string, dims []string, coords map[string]string, em engineModel) error {
	for _, d := range dims {
		lim, ok := l[d]
		if !ok {
			continue
		}
		c, set := coords[d]
		if !set {
			return tag(errDenied, "%s: 権限の制限がある %s のメンバーを指定せずに書き込めない", metric, em.name(d))
		}
		if !lim.write[c] {
			return tag(errDenied, "%s: %s の %q に書き込む権限がない", metric, em.name(d), c)
		}
	}
	return nil
}

// The actions follow.

// rights gives the role of the user in the application and, for a role under MODELER, the access rules.
// An application whose creation is not done has no members.
func (s *PlanServer) rights(ctx context.Context, app, user string) (role, []*nanashiv1.AccessRule, error) {
	var r int32
	err := s.Pool.QueryRow(ctx, `select m.role from app_member m join app_application a on a.id = m.app_id
		where m.app_id = $1 and m.user_name = $2 and `+doneApps, app, user).Scan(&r)
	if errors.Is(err, pgx.ErrNoRows) {
		return 0, nil, nil
	}
	if err != nil {
		return 0, nil, dbError(err)
	}
	if role(r) >= modeler {
		return role(r), nil, nil
	}
	rules, err := s.rules(ctx, app)
	return role(r), rules, err
}

func (s *PlanServer) ListAudit(ctx context.Context, req *connect.Request[nanashiv1.ListAuditRequest]) (*connect.Response[nanashiv1.ListAuditResponse], error) {
	limit := req.Msg.Limit
	if limit <= 0 || limit > 1000 {
		limit = 100
	}
	rows, _ := s.Pool.Query(ctx, `select id, user_name, (extract(epoch from created_at) * 1000)::bigint, action, detail
		from app_audit where app_id = $1 order by id desc limit $2`, req.Msg.AppId, limit)
	entries, err := pgx.CollectRows(rows, func(row pgx.CollectableRow) (*nanashiv1.AuditEntry, error) {
		e := &nanashiv1.AuditEntry{}
		var id int64
		err := row.Scan(&id, &e.User, &e.CreatedAt, &e.Action, &e.Detail)
		e.Id = strconv.FormatInt(id, 10)
		return e, err
	})
	if err != nil {
		return nil, dbError(err)
	}
	return connect.NewResponse(&nanashiv1.ListAuditResponse{Entries: entries}), nil
}

func (s *PlanServer) rules(ctx context.Context, app string) ([]*nanashiv1.AccessRule, error) {
	rows, _ := s.Pool.Query(ctx, "select id, role, list, members, write from app_access_rule where app_id = $1 order by id", app)
	rules, err := pgx.CollectRows(rows, func(row pgx.CollectableRow) (*nanashiv1.AccessRule, error) {
		r := &nanashiv1.AccessRule{AppId: app}
		return r, row.Scan(&r.Id, &r.Role, &r.List, &r.Members, &r.Write)
	})
	if err != nil {
		return nil, dbError(err)
	}
	return rules, nil
}

func (s *PlanServer) GetAccess(ctx context.Context, req *connect.Request[nanashiv1.GetAccessRequest]) (*connect.Response[nanashiv1.Access], error) {
	app := req.Msg.AppId
	rows, _ := s.Pool.Query(ctx, "select user_name, role from app_member where app_id = $1 order by user_name", app)
	members, err := pgx.CollectRows(rows, func(row pgx.CollectableRow) (*nanashiv1.AppMember, error) {
		m := &nanashiv1.AppMember{AppId: app}
		return m, row.Scan(&m.User, &m.Role)
	})
	if err != nil {
		return nil, dbError(err)
	}
	rules, err := s.rules(ctx, app)
	if err != nil {
		return nil, err
	}
	return connect.NewResponse(&nanashiv1.Access{Members: members, Rules: rules}), nil
}

func (s *PlanServer) SetMemberRole(ctx context.Context, req *connect.Request[nanashiv1.SetMemberRoleRequest]) (*ack, error) {
	app, m := req.Msg.AppId, req.Msg.Member
	if strings.TrimSpace(m.GetUser()) == "" {
		return nil, invalid(errors.New("利用者が空"))
	}
	return ackOf(s.apiOnly(ctx, app, req.Msg, pgx.TxOptions{}, func(tx pgx.Tx) (any, error) {
		// Lock the members of the application, so that two ADMINs cannot remove each other at the same time.
		if _, err := tx.Exec(ctx, "select 1 from app_member where app_id = $1 for update", app); err != nil {
			return nil, err
		}
		var err error
		if m.Role == nanashiv1.Role_ROLE_UNSPECIFIED {
			_, err = tx.Exec(ctx, "delete from app_member where app_id = $1 and user_name = $2", app, m.User)
		} else {
			_, err = tx.Exec(ctx, `insert into app_member (app_id, user_name, role) values ($1, $2, $3)
				on conflict (app_id, user_name) do update set role = excluded.role`, app, m.User, m.Role)
		}
		if err != nil {
			return nil, err
		}
		var admins int
		if err := tx.QueryRow(ctx, "select count(*) from app_member where app_id = $1 and role = $2", app, admin).Scan(&admins); err != nil {
			return nil, err
		}
		if admins == 0 {
			return nil, connect.NewError(connect.CodeFailedPrecondition, errors.New("最後の ADMIN は外せない"))
		}
		return nil, nil
	}))
}

func (s *PlanServer) CreateAccessRule(ctx context.Context, req *connect.Request[nanashiv1.CreateAccessRuleRequest]) (*ack, error) {
	return s.saveAccessRule(ctx, req.Msg.AppId, req.Msg, req.Msg.Rule, true)
}

func (s *PlanServer) UpdateAccessRule(ctx context.Context, req *connect.Request[nanashiv1.UpdateAccessRuleRequest]) (*ack, error) {
	return s.saveAccessRule(ctx, req.Msg.AppId, req.Msg, req.Msg.Rule, false)
}

// saveAccessRule makes the rule r (create) or changes it. If create is true, an existing id gives AlreadyExists.
// If create is false, a missing id gives NotFound.
func (s *PlanServer) saveAccessRule(ctx context.Context, app string, req proto.Message, r *nanashiv1.AccessRule, create bool) (*ack, error) {
	if r.GetId() == "" || r.GetList() == "" {
		return nil, invalid(errors.New("ルールの id とリストが要る"))
	}
	if r.Role != viewer && r.Role != contributor {
		return nil, invalid(errors.New("ルールは VIEWER か CONTRIBUTOR に付ける（MODELER と ADMIN はルールを無視する）"))
	}
	em, _, err := s.Engines.model(ctx, app)
	if err != nil {
		return nil, err
	}
	d, found := em.dim(r.List)
	if !found {
		return nil, invalid(fmt.Errorf("リスト %s がない", r.List))
	}
	for _, x := range r.Members {
		if _, ok := d.member(x); !ok {
			return nil, invalid(fmt.Errorf("%s に %s がない", d.Name, x))
		}
	}
	members := r.Members
	if members == nil {
		members = []string{}
	}
	st := stmt{"update app_access_rule set role = $3, list = $4, members = $5, write = $6 where app_id = $1 and id = $2",
		[]any{app, r.Id, r.Role, r.List, members, r.Write}, tag(errNotFound, "変更するルールがない")}
	if create {
		st.sql = `insert into app_access_rule (app_id, id, role, list, members, write) values ($1, $2, $3, $4, $5, $6)
			on conflict (app_id, id) do nothing`
		st.zero = tag(errExists, "同じ id のルールがすでにある")
	}
	return ackOf(s.apiOnly(ctx, app, req, pgx.TxOptions{}, func(tx pgx.Tx) (any, error) {
		return nil, execStmt(ctx, tx, st)
	}))
}

func (s *PlanServer) DeleteAccessRule(ctx context.Context, req *connect.Request[nanashiv1.DeleteAccessRuleRequest]) (*ack, error) {
	return ackOf(s.apiOnly(ctx, req.Msg.AppId, req.Msg, pgx.TxOptions{}, func(tx pgx.Tx) (any, error) {
		_, err := tx.Exec(ctx, "delete from app_access_rule where app_id = $1 and id = $2", req.Msg.AppId, req.Msg.Id)
		return nil, err
	}))
}
