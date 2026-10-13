package api

import (
	"fmt"
	"net"
	"net/http"
	"net/netip"
)

// ParseNetworks reads the --trusted-proxy values. A single address is a network of 1 address.
func ParseNetworks(values []string) ([]netip.Prefix, error) {
	var out []netip.Prefix
	for _, v := range values {
		if p, err := netip.ParsePrefix(v); err == nil {
			out = append(out, p.Masked())
			continue
		}
		a, err := netip.ParseAddr(v)
		if err != nil {
			return nil, fmt.Errorf("--trusted-proxy %q は CIDR かアドレスではない（複数なら繰り返して指定する）", v)
		}
		a = a.Unmap()
		out = append(out, netip.PrefixFrom(a, a.BitLen()))
	}
	return out, nil
}

// Trusted tells if the TCP peer address (host:port) is in one of the networks.
func Trusted(networks []netip.Prefix, remoteAddr string) bool {
	ap, err := netip.ParseAddrPort(remoteAddr)
	if err != nil {
		return false
	}
	for _, n := range networks {
		if n.Contains(ap.Addr().Unmap()) {
			return true
		}
	}
	return false
}

// Loopback tells if a listen address accepts connections only from this host.
func Loopback(listen string) bool {
	host, _, err := net.SplitHostPort(listen)
	if err != nil {
		return false
	}
	a, err := netip.ParseAddr(host)
	return (err == nil && a.IsLoopback()) || host == "localhost"
}

// TrustedOnly refuses each request that does not come from a trusted proxy. The proxy authenticates the
// user and sets X-Nanashi-User again, so the API can trust the header. It uses the TCP address, not
// X-Forwarded-For, because a client can set any header.
func TrustedOnly(networks []netip.Prefix, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !Trusted(networks, r.RemoteAddr) {
			http.Error(w, "信頼するプロキシ（--trusted-proxy）からの接続ではない", http.StatusUnauthorized)
			return
		}
		next.ServeHTTP(w, r)
	})
}
