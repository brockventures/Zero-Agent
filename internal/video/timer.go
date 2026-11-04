package video

import "time"

// Timer abstracts an auto-dismiss countdown timer to enable deterministic testing without time.Sleep.
type Timer interface {
	Stop() bool
}

// realTimer implements Timer backed by Go's standard *time.Timer.
type realTimer struct {
	t *time.Timer
}

func (r *realTimer) Stop() bool {
	if r.t == nil {
		return false
	}
	return r.t.Stop()
}

// TimerFunc defines the constructor signature for starting countdown timers.
type TimerFunc func(d time.Duration, f func()) Timer

// defaultTimerFunc constructs a real time.Timer.
func defaultTimerFunc(d time.Duration, f func()) Timer {
	return &realTimer{t: time.AfterFunc(d, f)}
}
