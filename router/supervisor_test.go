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
	tests := []struct {
		name string
		st   state
		now  time.Time
		want verb
	}{
		{"new model", state{}, t0, spawn},
		{"booting", state{running: true, started: t0}, t0.Add(bootBudget - time.Second), none},
		{"no lease after the budget", state{running: true, started: t0}, t0.Add(bootBudget), kill},
		{"crashed, waiting", state{fails: 1, retryAt: sec(1)}, t0, none},
		{"crashed, retry time", state{fails: 1, retryAt: sec(1)}, sec(1), spawn},
		{"crash loop, waiting", state{fails: quietFails, retryAt: sec(4)}, sec(3), failing},
		{"crash loop, retry time", state{fails: quietFails, retryAt: sec(4)}, sec(4), spawn},
		{"clean exit", state{retryAt: time.Time{}}, t0, spawn},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got, st := plan(tt.st, tt.now)
			if got != tt.want {
				t.Fatalf("plan = %v, want %v", got, tt.want)
			}
			if got == spawn && (!st.running || !st.started.Equal(tt.now)) {
				t.Errorf("spawn state %+v, want running from %v", st, tt.now)
			}
			if got != spawn && st != tt.st {
				t.Errorf("%v changed the state to %+v", got, st)
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
		err       error
		wantFails int
		wantRetry time.Duration // after now; 0 = no wait
	}{
		{"clean exit resets", 5, time.Second, nil, 0, 0},
		{"first crash", 0, time.Second, crash, 1, time.Second},
		{"second crash", 1, time.Second, crash, 2, 2 * time.Second},
		{"third crash", 2, time.Second, crash, 3, 4 * time.Second},
		{"backoff stops at maxRetry", 9, time.Second, crash, 10, maxRetry},
		{"crash after a healthy run", 7, healthyRun, crash, 1, time.Second},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			now := t0.Add(tt.ranFor)
			got := afterExit(state{running: true, started: t0, fails: tt.fails}, tt.err, now)
			if got.running || got.fails != tt.wantFails {
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
	if !errors.Is(err, ErrEngineFailing) || err.Error() != "engine failing: exit status 1" {
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
	s, _ := fakeSupervisor("exec sleep 30")
	s.Leader(context.Background(), "m")
	start := time.Now()
	s.Close()
	if took := time.Since(start); took > 5*time.Second {
		t.Errorf("Close took %v", took)
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if st := s.models["m"]; st.running || len(s.procs) != 0 {
		t.Errorf("state after Close %+v with %d processes, want stopped", st, len(s.procs))
	}
}
