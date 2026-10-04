package router

import (
	"context"
	"errors"
	"os/exec"
	"sync/atomic"
	"testing"
	"time"
)

var t0 = time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)

func TestPlan(t *testing.T) {
	sec := func(n int) time.Time { return t0.Add(time.Duration(n) * time.Second) }
	spawned := func(fails int, retryAt time.Time) state {
		return state{running: true, started: t0, noLeaseSince: t0, fails: fails, retryAt: retryAt}
	}
	leased := state{running: true, started: t0} // Leader saw a lease and cleared noLeaseSince
	lapsed := state{running: true, started: t0, noLeaseSince: sec(300)}
	tests := []struct {
		name   string
		st     state
		now    time.Time
		want   verb
		wantSt state
	}{
		{"new model", state{}, t0, spawn, spawned(0, time.Time{})},
		{"booting", spawned(0, time.Time{}), t0.Add(bootBudget - time.Second), none, spawned(0, time.Time{})},
		{"no lease after the budget", spawned(0, time.Time{}), t0.Add(bootBudget), kill, state{running: true, started: t0, noLeaseSince: t0, killed: true}},
		{"lease was held, first leaderless observation", leased, sec(300), none, lapsed},
		{"no lease for 59 s", lapsed, sec(359), none, lapsed},
		{"no lease for 60 s", lapsed, sec(360), kill, state{running: true, started: t0, noLeaseSince: sec(300), killed: true}},
		{"crashed, waiting", state{fails: 1, retryAt: sec(1)}, t0, none, state{fails: 1, retryAt: sec(1)}},
		{"crashed, retry time", state{fails: 1, retryAt: sec(1)}, sec(1), spawn, state{running: true, started: sec(1), noLeaseSince: sec(1), fails: 1, retryAt: sec(1)}},
		{"crash loop, waiting", state{fails: quietFails, retryAt: sec(4)}, sec(3), failing, state{fails: quietFails, retryAt: sec(4)}},
		{"crash loop, retry time", state{fails: quietFails, retryAt: sec(4)}, sec(4), spawn, state{running: true, started: sec(4), noLeaseSince: sec(4), fails: quietFails, retryAt: sec(4)}},
		{"clean exit", state{retryAt: time.Time{}}, t0, spawn, spawned(0, time.Time{})},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got, st := plan(tt.st, tt.now)
			if got != tt.want {
				t.Fatalf("plan = %v, want %v", got, tt.want)
			}
			if st != tt.wantSt {
				t.Errorf("state %+v, want %+v", st, tt.wantSt)
			}
		})
	}
}

func TestAfterExit(t *testing.T) {
	crash := errors.New("exit status 1")
	tests := []struct {
		name      string
		fails     int
		ranFor    time.Duration
		killed    bool
		err       error
		wantFails int
		wantRetry time.Duration // after now; 0 = no wait
	}{
		{"clean exit resets", 5, time.Second, false, nil, 0, 0},
		{"first crash", 0, time.Second, false, crash, 1, time.Second},
		{"second crash", 1, time.Second, false, crash, 2, 2 * time.Second},
		{"third crash", 2, time.Second, false, crash, 3, 4 * time.Second},
		{"backoff stops at maxRetry", 9, time.Second, false, crash, 10, maxRetry},
		{"backoff stays at maxRetry after many crashes", 39, time.Second, false, crash, 40, maxRetry},
		{"crash after a healthy run", 7, healthyRun, false, crash, 1, time.Second},
		{"killed after a long run", 2, 2 * healthyRun, true, errors.New("signal: killed"), 3, 4 * time.Second},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			now := t0.Add(tt.ranFor)
			got := afterExit(state{running: true, started: t0, fails: tt.fails, killed: tt.killed}, tt.err, now)
			if got.running || got.killed || got.fails != tt.wantFails {
				t.Errorf("got %+v, want stopped with fails %d", got, tt.wantFails)
			}
			wantRetry := time.Time{}
			if tt.wantRetry > 0 {
				wantRetry = now.Add(tt.wantRetry)
			}
			if !got.retryAt.Equal(wantRetry) {
				t.Errorf("retryAt %v, want %v", got.retryAt, wantRetry)
			}
			if tt.err != nil && got.lastErr != tt.err.Error() {
				t.Errorf("lastErr %q, want %q", got.lastErr, tt.err)
			}
		})
	}
}

// fakeSupervisor runs `sh -c script` for each start; its lease never names a leader.
func fakeSupervisor(script string) (*Supervisor, *atomic.Int32) {
	var starts atomic.Int32
	return &Supervisor{
		Lease: &resolver{answers: []string{""}},
		Command: func(string) *exec.Cmd {
			starts.Add(1)
			return exec.Command("sh", "-c", script)
		},
	}, &starts
}

func (s *Supervisor) waitStopped(t *testing.T, model string) {
	t.Helper()
	for range 200 {
		s.mu.Lock()
		running := s.models[model].running
		s.mu.Unlock()
		if !running {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatal("the process did not stop")
}

func TestSupervisorCrashLoop(t *testing.T) {
	s, starts := fakeSupervisor("exit 1")
	ctx := context.Background()
	deadline := time.Now().Add(10 * time.Second)
	var err error
	for time.Now().Before(deadline) {
		if _, err = s.Leader(ctx, "m"); err != nil {
			break
		}
		time.Sleep(20 * time.Millisecond)
	}
	if !errors.Is(err, ErrEngineFailing) || err.Error() != "exit status 1" {
		t.Fatalf("got %v, want ErrEngineFailing with the crash text", err)
	}
	if n := starts.Load(); n != quietFails {
		t.Errorf("started %d times, want %d", n, quietFails)
	}
}

func TestSupervisorCleanExitStartsAgainAtOnce(t *testing.T) {
	s, starts := fakeSupervisor("exit 0")
	ctx := context.Background()
	for i := range 3 {
		if url, err := s.Leader(ctx, "m"); url != "" || err != nil {
			t.Fatalf("got %q, %v", url, err)
		}
		s.waitStopped(t, "m")
		if n := starts.Load(); n != int32(i+1) {
			t.Fatalf("started %d times, want %d", n, i+1)
		}
	}
}

func TestSupervisorPassesLeaseThrough(t *testing.T) {
	s, starts := fakeSupervisor("exit 1")
	s.Lease = &resolver{answers: []string{"http://a:8080"}}
	if url, err := s.Leader(context.Background(), "m"); url != "http://a:8080" || err != nil {
		t.Errorf("got %q, %v", url, err)
	}
	s.Lease = &resolver{err: ErrUnknownModel}
	if _, err := s.Leader(context.Background(), "m"); !errors.Is(err, ErrUnknownModel) {
		t.Errorf("got %v, want ErrUnknownModel", err)
	}
	if n := starts.Load(); n != 0 {
		t.Errorf("started %d times, want 0", n)
	}
}

func TestSupervisorCloseStopsEngines(t *testing.T) {
	s, starts := fakeSupervisor("exec sleep 30")
	s.Leader(context.Background(), "m")
	start := time.Now()
	s.Close()
	if took := time.Since(start); took > 5*time.Second {
		t.Errorf("Close took %v", took)
	}
	if _, err := s.Leader(context.Background(), "m"); err != nil || starts.Load() != 1 {
		t.Errorf("after Close: %v, %d starts, want no new start", err, starts.Load())
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if st := s.models["m"]; st.running || len(s.procs) != 0 {
		t.Errorf("state after Close %+v with %d processes, want stopped", st, len(s.procs))
	}
}
