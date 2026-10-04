package api

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestTrustedProxy(t *testing.T) {
	nets, err := ParseNetworks([]string{"10.0.1.0/24", "10.0.2.5", "::1"})
	if err != nil {
		t.Fatal(err)
	}
	for addr, want := range map[string]bool{
		"10.0.1.7:5000":          true,
		"10.0.2.5:5000":          true,
		"[::ffff:10.0.2.5]:5000": true, // An IPv4-mapped address is the IPv4 address.
		"10.0.2.6:5000":          false,
		"127.0.0.1:5000":         false, // Loopback is not trusted automatically.
		"[::1]:5000":             true,
		"bad":                    false,
	} {
		if Trusted(nets, addr) != want {
			t.Errorf("Trusted(%s) = %v, want %v", addr, !want, want)
		}
	}
	if _, err := ParseNetworks([]string{"10.0.1.0/24,10.0.2.0/24"}); err == nil {
		t.Error("a comma-separated list must be an error")
	}
	for listen, want := range map[string]bool{"127.0.0.1:8080": true, "[::1]:8080": true, "localhost:8080": true,
		"0.0.0.0:8080": false, ":8080": false, "10.0.1.5:8080": false} {
		if Loopback(listen) != want {
			t.Errorf("Loopback(%s) = %v, want %v", listen, !want, want)
		}
	}
}

func TestTrustedOnly(t *testing.T) {
	nets, _ := ParseNetworks([]string{"10.0.1.5"})
	h := TrustedOnly(nets, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {}))
	for addr, want := range map[string]int{"10.0.1.5:4000": http.StatusOK, "10.0.1.6:4000": http.StatusUnauthorized} {
		r := httptest.NewRequest(http.MethodPost, "/nanashi.v1.PlanService/GetAccess", nil)
		r.RemoteAddr = addr
		r.Header.Set("X-Nanashi-User", "admin")
		w := httptest.NewRecorder()
		h.ServeHTTP(w, r)
		if w.Code != want {
			t.Errorf("%s: got %d, want %d", addr, w.Code, want)
		}
	}
}
