// Command nanashi-router sends each request for /models/{id}/... to that model's leader engine.
package main

import (
	"cmp"
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
	"os/exec"
	"os/signal"
	"path/filepath"
	"strconv"
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
	var trusted []netip.Prefix
	flag.Func("trusted-proxy",
		"--user-header の見出しを信頼する送信元（例: 10.0.1.0/24、10.0.1.5）。複数なら繰り返す。"+
			"ほかの送信元には 401 を返す（127.0.0.1 も自動では信頼しない）",
		func(v string) error {
			x, err := router.ParsePrefix(v)
			if err != nil {
				return err
			}
			trusted = append(trusted, x)
			return nil
		})
	deadline := flag.Duration("deadline", 90*time.Second, "1 つの要求を送り直し続ける長さ")
	tessera := flag.String("tessera", "", "tessera/ の場所。指定すると、要求が来たモデルのエンジンをこのルーターが起動する")
	engineDir := flag.String("engine-dir", "../.nanashi-data", "エンジンのデータを置く場所（--tessera と一緒に使う）")
	engineIdle := flag.Duration("engine-idle", 15*time.Minute, "要求がこの時間ないエンジンは自分で止まる（--tessera と一緒に使う）")
	engineHost := flag.String("engine-host", "127.0.0.1",
		"エンジンが待ち受ける番地（--tessera と一緒に使う）。ルーターはこの番地でエンジンにつなぐ。0.0.0.0 と IPv6 の番地は使えない")
	var engineTrusted []string
	flag.Func("engine-trusted-proxy",
		"ルーターがエンジンにつなぐときの送信元（例: 10.0.1.0/24）。認証があるとき、エンジンはこの番地からの利用者の見出しを信頼する。"+
			"複数なら繰り返す（--tessera と一緒に使う）。--engine-host が 127.0.0.1 なら 127.0.0.1/32 で、指定できない",
		func(v string) error {
			x, err := router.ParsePrefix(v)
			if err != nil {
				return err
			}
			if x.Bits() == 0 {
				return errors.New("すべての番地は信頼できない。ルーターのホストの番地（/32）を指定する")
			}
			engineTrusted = append(engineTrusted, x.String())
			return nil
		})
	engineMaxBytes := flag.Int64("engine-max-bytes", 0,
		"エンジンの --max-bytes（1 つの式の評価が持つ途中結果の上限、バイト）。0 なら渡さない（--tessera と一緒に使う）")
	engineBoot := flag.Duration("engine-boot", 60*time.Second,
		"エンジンがリースを取るまで待つ長さ。過ぎたら止める。大きいモデルは読み込みと全体の再計算に時間がかかるので長くする（--tessera と一緒に使う）")
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
	var engine engineConfig
	if *tessera != "" {
		engine, err = newEngineConfig(*engineDir, *dsn, *engineIdle, *engineHost, *engineMaxBytes, engineTrusted,
			*tokensFile != "" || *userHeader != "", *userHeader)
		if err != nil {
			fail(err.Error())
		}
		if *engineBoot <= 0 {
			fail("--engine-boot は正の長さ")
		}
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
	if err := resolver.Ready(ctx); err != nil {
		fail(fmt.Sprintf("nanashi_model を読めない（%v）。tessera/ で python -m sparse_engine.pg_journal migrate <DSN> を実行してスキーマを作る", err))
	}
	var resolve router.Resolver = resolver
	var supervisor *router.Supervisor
	if *tessera != "" {
		supervisor, err = newSupervisor(resolver, *tessera, engine, *engineBoot)
		if err != nil {
			fail(err.Error())
		}
		resolve = supervisor
	}
	srv := &http.Server{
		Addr: *listen,
		Handler: &router.Router{Resolve: resolve, Create: resolver.Create, Auth: auth, UserHeader: *userHeader,
			Deadline: *deadline},
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
	if supervisor != nil {
		supervisor.Close()
	}
}

type engineConfig struct {
	dir            string
	dsn            string
	idle           time.Duration
	host           string
	userHeader     string
	trustedProxies []string
	maxBytes       int64
}

func newEngineConfig(dir, dsn string, idle time.Duration, host string, maxBytes int64, trusted []string,
	hasAuth bool, userHeader string) (engineConfig, error) {
	// The engine advertises http://host:port without brackets, so only IPv4 addresses and host names work.
	if ip, err := netip.ParseAddr(host); host == "" || err == nil && (!ip.Is4() || ip.IsUnspecified()) {
		return engineConfig{}, fmt.Errorf("--engine-host %q は使えない。エンジンは自分の番地として知らせるので、"+
			"ルーターがつなぐ IPv4 の番地（例: 10.0.1.5）かホスト名を指定する", host)
	}
	if maxBytes < 0 {
		return engineConfig{}, errors.New("--engine-max-bytes は 0 以上")
	}
	if strings.HasPrefix(dir, "s3://") {
		dir = strings.TrimRight(dir, "/")
	} else {
		abs, err := filepath.Abs(dir)
		if err != nil {
			return engineConfig{}, err
		}
		dir = abs
	}
	cfg := engineConfig{dir: dir, dsn: dsn, idle: idle, host: host, maxBytes: maxBytes}
	if loopback(host) {
		if len(trusted) > 0 {
			return engineConfig{}, errors.New("--engine-trusted-proxy は --engine-host が 127.0.0.1 以外のときに使う" +
				"（127.0.0.1 のエンジンには同じホストのルーターだけがつなぐ）")
		}
		trusted = []string{"127.0.0.1/32"}
	} else if !hasAuth {
		return engineConfig{}, errors.New("--engine-host を 127.0.0.1 以外にするには、ルーターを --tokens か --user-header で認証する")
	} else if len(trusted) == 0 {
		return engineConfig{}, errors.New("--engine-host を 127.0.0.1 以外にするには、ルーターがエンジンにつなぐ送信元を --engine-trusted-proxy で指定する")
	}
	if hasAuth {
		cfg.userHeader = cmp.Or(userHeader, router.DefaultUserHeader)
		cfg.trustedProxies = trusted
	}
	return cfg, nil
}

func engineArgs(cfg engineConfig, model string) []string {
	path := filepath.Join(cfg.dir, model)
	if strings.HasPrefix(cfg.dir, "s3://") { // filepath.Join would make s3://b into s3:/b
		path = cfg.dir + "/" + model
	}
	args := []string{"-m", "sparse_engine.server", path, "--pg", cfg.dsn, "--model-id", model, "--port", "0",
		"--idle-exit", strconv.FormatFloat(cfg.idle.Seconds(), 'g', -1, 64), "--host", cfg.host}
	if cfg.userHeader != "" {
		args = append(args, "--user-header", cfg.userHeader)
		for _, c := range cfg.trustedProxies {
			args = append(args, "--trusted-proxy", c)
		}
	}
	if cfg.maxBytes > 0 {
		args = append(args, "--max-bytes", strconv.FormatInt(cfg.maxBytes, 10))
	}
	return args
}

func newSupervisor(lease router.Resolver, tessera string, cfg engineConfig, boot time.Duration) (*router.Supervisor, error) {
	tessera, err := filepath.Abs(tessera)
	if err != nil {
		return nil, err
	}
	return &router.Supervisor{Lease: lease, Boot: boot, Command: func(model string) *exec.Cmd {
		cmd := exec.Command(filepath.Join(tessera, ".venv/bin/python"), engineArgs(cfg, model)...)
		cmd.Dir = tessera
		return cmd
	}}, nil
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
