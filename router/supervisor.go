package router

import (
	"context"
	"errors"
	"log"
	"os"
	"os/exec"
	"sync"
	"syscall"
	"time"
)

// ErrEngineFailing means the engine of the model crashed quietFails or more times in a row,
// and the next start is not yet permitted. Leader returns an engineFailing, which errors.Is matches to it.
var ErrEngineFailing = errors.New("engine failing")

// engineFailing carries only the text of the last crash, so the 503 message of the router shows it alone.
type engineFailing struct{ last string }

func (e engineFailing) Error() string        { return e.last }
func (e engineFailing) Is(target error) bool { return target == ErrEngineFailing }

const (
	quietFails = 3
	firstRetry = time.Second
	maxRetry   = 60 * time.Second
	healthyRun = time.Minute
)

// state is the data about the engine of one model. It has no process handle, so plan and afterExit stay pure.
type state struct {
	running      bool
	started      time.Time // start time of the running process
	noLeaseSince time.Time // first time plan saw the running process without a lease; zero while it has one
	killed       bool      // plan asked for a kill; the exit then counts as a failure
	fails        int       // crashes in a row; a clean exit (code 0) sets it to 0
	retryAt      time.Time // do not start before this time
	lastErr      string    // text of the last crash, for the 503 message
}

type verb int

const (
	none verb = iota
	spawn
	kill
	failing
)

// plan is the decision for a model that has no live leader. boot is Supervisor.Boot.
func plan(st state, now time.Time, boot time.Duration) (verb, state) {
	switch {
	case st.running && st.noLeaseSince.IsZero():
		st.noLeaseSince = now
		return none, st
	case st.running && now.Sub(st.noLeaseSince) < boot:
		return none, st
	case st.running:
		st.killed = true
		return kill, st
	case now.Before(st.retryAt) && st.fails >= quietFails:
		return failing, st
	case now.Before(st.retryAt):
		return none, st
	}
	st.running, st.started, st.noLeaseSince = true, now, now
	return spawn, st
}

// afterExit is the state after the process exits. A nil err is exit code 0 (idle stop or SIGTERM).
// A kill by plan is a failure whatever the run time: the process ran, but never took the lease.
func afterExit(st state, err error, now time.Time) state {
	ranFor, killed := now.Sub(st.started), st.killed
	st.running, st.killed = false, false
	if err == nil {
		st.fails, st.retryAt = 0, time.Time{}
		return st
	}
	if ranFor >= healthyRun && !killed {
		st.fails = 1
	} else {
		st.fails++
	}
	st.lastErr = err.Error()
	retry := maxRetry // the shift overflows a Duration after about 35 crashes, so stop at the cap before it
	if st.fails <= 6 {
		retry = min(firstRetry<<(st.fails-1), maxRetry)
	}
	st.retryAt = now.Add(retry)
	return st
}

// Supervisor is a Resolver that also starts the engine processes on this host.
// At most one process of this Supervisor runs for a model: the models map under mu is the record.
type Supervisor struct {
	Lease   Resolver                     // the lease is the only source of the leader address
	Command func(model string) *exec.Cmd // the same model gives the same argv
	// Boot is the time a process may run without a lease before the kill. The engine loads the model and
	// does a full recalc before it takes the lease, so a large model needs more than 60 s.
	Boot time.Duration

	mu     sync.Mutex
	closed bool // Close ran; start no process after it
	models map[string]state
	procs  map[string]*exec.Cmd // running processes, for kill and Close
	wg     sync.WaitGroup
}

// Leader returns the leader from Lease. If the model exists but has no live leader, it starts,
// kills, or refuses per plan, and returns "". Router.forward backs off and asks again.
func (s *Supervisor) Leader(ctx context.Context, model string) (string, error) {
	url, err := s.Lease.Leader(ctx, model)
	if url != "" {
		s.mu.Lock()
		if st := s.models[model]; st.running {
			st.noLeaseSince = time.Time{}
			s.models[model] = st
		}
		s.mu.Unlock()
	}
	if url != "" || err != nil {
		return url, err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.closed {
		return "", nil
	}
	v, st := plan(s.models[model], time.Now(), s.Boot)
	if s.models == nil {
		s.models, s.procs = map[string]state{}, map[string]*exec.Cmd{}
	}
	s.models[model] = st
	switch v {
	case spawn:
		s.start(model)
	case kill:
		log.Printf("engine %s held no lease for %v; kill it", model, s.Boot)
		s.procs[model].Process.Kill()
	case failing:
		return "", engineFailing{st.lastErr}
	}
	return "", nil
}

// start starts the process of model. The caller holds mu.
func (s *Supervisor) start(model string) {
	cmd := s.Command(model)
	cmd.Stdout, cmd.Stderr = os.Stderr, os.Stderr
	// Ctrl-C signals the terminal's process group. The engine must get only the one SIGTERM from Close:
	// a second signal stops it before it releases the lease.
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	if err := cmd.Start(); err != nil {
		s.models[model] = afterExit(s.models[model], err, time.Now())
		return
	}
	log.Printf("engine %s started (pid %d)", model, cmd.Process.Pid)
	s.procs[model] = cmd
	s.wg.Add(1)
	go func() {
		defer s.wg.Done()
		err := cmd.Wait()
		log.Printf("engine %s stopped: %v", model, err)
		s.mu.Lock()
		defer s.mu.Unlock()
		delete(s.procs, model)
		s.models[model] = afterExit(s.models[model], err, time.Now())
	}()
}

// Close sends SIGTERM to the running engines, waits up to 15 seconds, then kills them.
// Call it after http.Server.Shutdown.
func (s *Supervisor) Close() {
	s.mu.Lock()
	s.closed = true
	for _, cmd := range s.procs {
		cmd.Process.Signal(syscall.SIGTERM)
	}
	s.mu.Unlock()
	done := make(chan struct{})
	go func() { s.wg.Wait(); close(done) }()
	select {
	case <-done:
		return
	case <-time.After(15 * time.Second):
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, cmd := range s.procs {
		cmd.Process.Kill()
	}
}
