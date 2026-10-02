// Command nanashi-router sends each request for /models/{id}/... to that model's leader engine.
package main

import (
	"context"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"log"
	"net"
	"net/http"
	"net/netip"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/kazu-2020/nanashi/router"
)

func main() {
	listen := flag.String("listen", "127.0.0.1:8090", "待ち受ける番地")
	dsn := flag.String("pg", "", "記録先の PostgreSQL（必須）。書き手を nanashi_model のリースから引く")
	tokensFile := flag.String("tokens", "", "{トークン: 利用者} の JSON。Bearer トークンで認証する")
	userHeader := flag.String("user-header", "",
		"認証を済ませたプロキシが付ける、利用者の見出し（例: X-Forwarded-User）。同じ見出しでエンジンに渡す。--trusted-proxy が要る")
	var trusted prefixes
	flag.Var(&trusted, "trusted-proxy",
		"--user-header の見出しを信頼する送信元（例: 10.0.1.0/24、10.0.1.5）。複数なら繰り返す。"+
			"ほかの送信元には 401 を返す（127.0.0.1 も自動では信頼しない）")
	deadline := flag.Duration("deadline", 90*time.Second, "1 つの要求を送り直し続ける長さ")
	insecure := flag.Bool("insecure", false, "認証なしで 127.0.0.1 以外でも待ち受ける")
	flag.Parse()
	if *dsn == "" {
		fail("--pg が要る")
	}
	host, _, err := net.SplitHostPort(*listen)
	if err != nil {
		fail(fmt.Sprintf("--listen %s: %v", *listen, err))
	}
	if *tokensFile != "" && *userHeader != "" {
		fail("--tokens と --user-header はどちらか一方")
	}
	if *userHeader != "" && len(trusted) == 0 {
		fail("--user-header には、見出しを付けるプロキシの番地を --trusted-proxy で指定する")
	}
	if len(trusted) > 0 && *userHeader == "" {
		fail("--trusted-proxy は --user-header と一緒に使う")
	}
	// --user-header always comes with --trusted-proxy (above), so it also allows a non-loopback address.
	if *tokensFile == "" && *userHeader == "" && !*insecure && !loopback(host) {
		fail(*listen + " で待ち受けるには --tokens か --user-header で認証する（試すだけなら --insecure）")
	}
	var auth func(*http.Request) (string, error)
	if *tokensFile != "" {
		tokens, err := readTokens(*tokensFile)
		if err != nil {
			fail(err.Error())
		}
		auth = bearer(tokens)
	}
	if *userHeader != "" {
		auth = router.ProxyUser(*userHeader, trusted)
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()
	resolver, err := router.NewPgResolver(ctx, *dsn)
	if err != nil {
		log.Fatalf("PostgreSQL につながらない: %v", err)
	}
	defer resolver.Close()
	srv := &http.Server{
		Addr:              *listen,
		Handler:           &router.Router{Resolve: resolver, Auth: auth, UserHeader: *userHeader, Deadline: *deadline},
		ReadHeaderTimeout: 30 * time.Second,
	}
	ln, err := net.Listen("tcp", *listen)
	if err != nil {
		log.Fatal(err)
	}
	log.Printf("%s で待ち受ける", ln.Addr())
	done := make(chan error, 1)
	go func() { done <- srv.Serve(ln) }()
	select {
	case err := <-done:
		log.Fatal(err)
	case <-ctx.Done():
	}
	// In-flight requests may be resending for up to the deadline.
	shutdown, cancel := context.WithTimeout(context.Background(), *deadline+5*time.Second)
	defer cancel()
	if err := srv.Shutdown(shutdown); err != nil {
		log.Printf("止めるのを待ちきれなかった: %v", err)
	}
	if err := <-done; !errors.Is(err, http.ErrServerClosed) {
		log.Print(err)
	}
}

// prefixes is the value of a repeated --trusted-proxy flag.
type prefixes []netip.Prefix

func (p *prefixes) String() string {
	s := make([]string, len(*p))
	for i, x := range *p {
		s[i] = x.String()
	}
	return strings.Join(s, ",")
}

func (p *prefixes) Set(v string) error {
	x, err := router.ParsePrefix(v)
	if err != nil {
		return err
	}
	*p = append(*p, x)
	return nil
}

func readTokens(path string) (map[string]string, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var tokens map[string]string
	if err := json.Unmarshal(data, &tokens); err != nil {
		return nil, fmt.Errorf("%s は {トークン: 利用者} の JSON ではない: %v", path, err)
	}
	return tokens, nil
}

func bearer(tokens map[string]string) func(*http.Request) (string, error) {
	return func(r *http.Request) (string, error) {
		scheme, token, _ := strings.Cut(r.Header.Get("Authorization"), " ")
		if strings.EqualFold(scheme, "bearer") {
			token = strings.TrimSpace(token)
			for known, user := range tokens {
				if subtle.ConstantTimeCompare([]byte(known), []byte(token)) == 1 {
					return user, nil
				}
			}
		}
		return "", errors.New("Authorization: Bearer <トークン> が要る")
	}
}

func loopback(host string) bool {
	if host == "localhost" {
		return true
	}
	ip := net.ParseIP(host)
	return ip != nil && ip.IsLoopback()
}

func fail(msg string) {
	fmt.Fprintln(os.Stderr, "nanashi-router:", msg)
	os.Exit(2)
}
