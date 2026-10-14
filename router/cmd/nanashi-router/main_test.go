package main

import (
	"slices"
	"strings"
	"testing"
	"time"
)

func TestEngineArgs(t *testing.T) {
	base := func(path, host string) []string {
		return []string{"-m", "sparse_engine.server", path, "--pg", "postgres://x", "--model-id", "m1",
			"--port", "0", "--idle-exit", "900", "--host", host}
	}
	today := base("/data/m1", "127.0.0.1")
	tests := []struct {
		name     string
		dir      string
		host     string
		maxBytes int64
		trusted  []string
		hasAuth  bool
		header   string
		want     []string
	}{
		{"loopback default", "/data", "127.0.0.1", 0, nil, false, "", today},
		{"tokens: default header and loopback proxy", "/data", "127.0.0.1", 0, nil, true, "",
			append(slices.Clone(today), "--user-header", "X-Forwarded-User", "--trusted-proxy", "127.0.0.1/32")},
		{"user header on another host", "/data", "10.0.1.5", 0, []string{"10.0.1.0/24", "10.0.2.7/32"}, true, "X-Auth-User",
			append(base("/data/m1", "10.0.1.5"), "--user-header", "X-Auth-User",
				"--trusted-proxy", "10.0.1.0/24", "--trusted-proxy", "10.0.2.7/32")},
		{"s3 prefix", "s3://bucket/engines//", "127.0.0.1", 0, nil, false, "", base("s3://bucket/engines/m1", "127.0.0.1")},
		{"s3 bucket only", "s3://bucket", "127.0.0.1", 0, nil, false, "", base("s3://bucket/m1", "127.0.0.1")},
		{"max bytes", "/data", "127.0.0.1", 1 << 30, nil, false, "",
			append(slices.Clone(today), "--max-bytes", "1073741824")},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			cfg, err := newEngineConfig(tt.dir, "postgres://x", 15*time.Minute, tt.host, tt.maxBytes, tt.trusted,
				tt.hasAuth, tt.header)
			if err != nil {
				t.Fatal(err)
			}
			if got := engineArgs(cfg, "m1"); !slices.Equal(got, tt.want) {
				t.Errorf("engineArgs\n got %q\nwant %q", got, tt.want)
			}
		})
	}
}

func TestNewEngineConfigRefuses(t *testing.T) {
	tests := []struct {
		name    string
		host    string
		trusted []string
		hasAuth bool
		want    string
	}{
		{"all IPv4 addresses", "0.0.0.0", nil, true, "--engine-host"},
		{"all IPv6 addresses", "::", nil, true, "--engine-host"},
		{"an IPv4-mapped IPv6 address", "::ffff:10.0.1.5", []string{"10.0.1.0/24"}, true, "--engine-host"},
		{"an IPv6 address", "fd00::1", []string{"fd00::/8"}, true, "IPv4"},
		{"no auth on another host", "10.0.1.5", nil, false, "--tokens か --user-header で認証する"},
		{"auth on another host without proxies", "10.0.1.5", nil, true, "--engine-trusted-proxy で指定する"},
		{"proxies on a loopback host", "127.0.0.1", []string{"10.0.1.0/24"}, true, "--engine-trusted-proxy は"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			_, err := newEngineConfig("/data", "postgres://x", time.Minute, tt.host, 0, tt.trusted, tt.hasAuth, "")
			if err == nil || !strings.Contains(err.Error(), tt.want) {
				t.Errorf("err = %v, want it to contain %q", err, tt.want)
			}
		})
	}
}
