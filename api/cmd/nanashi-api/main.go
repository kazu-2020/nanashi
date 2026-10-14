// nanashi-api starts the application server. It has no RPC yet.
package main

import (
	"context"
	"errors"
	"flag"
	"log"
	"net/http"
	"os/signal"
	"syscall"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/kazu-2020/nanashi/api"
)

func main() {
	dsn := flag.String("pg", "postgresql://postgres@127.0.0.1:55432/nanashi", "DSN of PostgreSQL")
	listen := flag.String("listen", "127.0.0.1:8080", "address to listen on")
	var proxies []string
	flag.Func("trusted-proxy", "CIDR or address of the authenticating proxy that sets X-Nanashi-User (repeat for more)",
		func(v string) error { proxies = append(proxies, v); return nil })
	flag.Parse()
	networks, err := api.ParseNetworks(proxies)
	if err != nil {
		log.Fatal(err)
	}
	// Without a trusted proxy, any client can set X-Nanashi-User. Then only local clients can connect.
	if len(networks) == 0 && !api.Loopback(*listen) {
		log.Fatalf("%s で待ち受けるには、利用者を認証するプロキシを --trusted-proxy で指定する", *listen)
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	pool, err := pgxpool.New(ctx, *dsn)
	if err != nil {
		log.Fatal(err)
	}
	defer pool.Close()
	if err := pool.Ping(ctx); err != nil {
		log.Fatal(err)
	}

	// Register the Connect handlers here.
	mux := http.NewServeMux()
	// The Vite dev server sends /nanashi.v1.* to this server, so the API does not need CORS.
	var handler http.Handler = mux
	if len(networks) > 0 {
		handler = api.TrustedOnly(networks, mux)
	}
	srv := &http.Server{Addr: *listen, Handler: handler, ReadHeaderTimeout: 30 * time.Second}
	stopped := make(chan struct{})
	go func() {
		defer close(stopped)
		<-ctx.Done()
		shutdown, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		if err := srv.Shutdown(shutdown); err != nil {
			log.Printf("shutdown: %v", err)
		}
	}()
	log.Printf("nanashi-api: listening on %s", *listen)
	if err := srv.ListenAndServe(); !errors.Is(err, http.ErrServerClosed) {
		log.Print(err)
		return
	}
	// ListenAndServe returns when Shutdown starts. Wait for the requests in flight before the deferred
	// call closes the pool.
	<-stopped
}
