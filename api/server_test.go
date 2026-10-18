package api

import (
	"net/http"
	"strings"
	"testing"

	"github.com/google/go-cmp/cmp"
)

func TestInterceptor(t *testing.T) {
	tests := []struct {
		name, user, app string
		want            [2]string
	}{
		{"no user", "", testApp, [2]string{"unauthenticated", "X-Nanashi-User がない"}},
		{"empty app_id", "alice", "", [2]string{"invalid_argument", "app_id が正規形の UUID ではない"}},
		{"upper-case app_id", "alice", strings.ToUpper(testApp), [2]string{"invalid_argument", "app_id が正規形の UUID ではない"}},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			c, f := newClient(t, http.StatusOK, nil)
			_, err := querySQL(c, tt.user, tt.app, "SELECT 1")
			if diff := cmp.Diff(tt.want, errorOf(err)); diff != "" {
				t.Errorf("error (-want +got):\n%s", diff)
			}
			if n := len(f.got()); n != 0 {
				t.Errorf("router got %d requests, want 0", n)
			}
		})
	}
}
