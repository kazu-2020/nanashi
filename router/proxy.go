package router

import (
	"errors"
	"fmt"
	"net/http"
	"net/netip"
	"strings"
)

// ParsePrefix parses a CIDR such as "10.0.1.0/24". A single address is a prefix of that address only.
// It sets the host bits to zero, so "10.0.1.5/24" is "10.0.1.0/24".
// An IPv4-mapped prefix (::ffff:10.0.1.0/120) becomes the IPv4 prefix, as Trusted unmaps the client.
func ParsePrefix(s string) (netip.Prefix, error) {
	s = strings.TrimSpace(s)
	p, err := netip.ParsePrefix(s)
	if err != nil {
		a, err := netip.ParseAddr(s)
		if err != nil || a.Zone() != "" {
			return netip.Prefix{}, fmt.Errorf("%q は番地（CIDR。例: 10.0.1.0/24）ではない", s)
		}
		p = netip.PrefixFrom(a, a.BitLen())
	}
	if p.Addr().Is4In6() && p.Bits() >= 96 {
		p = netip.PrefixFrom(p.Addr().Unmap(), p.Bits()-96)
	}
	return p.Masked(), nil
}

// Trusted reports whether remoteAddr ("host:port", as in http.Request.RemoteAddr) is in one of trusted.
func Trusted(remoteAddr string, trusted []netip.Prefix) bool {
	ap, err := netip.ParseAddrPort(remoteAddr)
	if err != nil {
		return false
	}
	addr := ap.Addr().Unmap() // a dual-stack listener can show an IPv4 client as ::ffff:a.b.c.d
	if addr.Zone() != "" {
		return false
	}
	for _, p := range trusted {
		if p.Contains(addr) {
			return true
		}
	}
	return false
}

// ProxyUser returns an Auth function for a router behind an authenticating proxy (for example, oauth2-proxy).
// The user is the value of header, but only on a connection from an address in trusted.
// It does not fall back to the header for other senders, because they can set any user.
func ProxyUser(header string, trusted []netip.Prefix) func(*http.Request) (string, error) {
	return func(r *http.Request) (string, error) {
		if !Trusted(r.RemoteAddr, trusted) {
			return "", errors.New("信頼するプロキシ（--trusted-proxy）からの接続ではない")
		}
		user := r.Header.Get(header)
		if user == "" {
			return "", errors.New(header + " がない")
		}
		return user, nil
	}
}
