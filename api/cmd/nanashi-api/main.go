// nanashi-api starts the application server. It also starts one engine server for each application.
package main

import (
	"context"
	"errors"
	"flag"
	"log"
	"net/http"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	"connectrpc.com/connect"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/kazu-2020/nanashi/api"
	"github.com/kazu-2020/nanashi/api/gen/nanashi/v1/nanashiv1connect"
)

func main() {
	dsn := flag.String("pg", "postgresql://postgres@127.0.0.1:55432/nanashi", "DSN of PostgreSQL (the api tables and the engine journal)")
	listen := flag.String("listen", "127.0.0.1:8080", "address to listen on")
	router := flag.String("router", "http://127.0.0.1:8090", "URL of nanashi-router")
	tessera := flag.String("tessera", "../tessera", "path to tessera/")
	engineDir := flag.String("engine-dir", "../.nanashi-data", "directory for the engine files")
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
	if err := api.Migrate(ctx, pool); err != nil {
		log.Fatal(err)
	}
	tesseraAbs, err := filepath.Abs(*tessera)
	if err != nil {
		log.Fatal(err)
	}
	dirAbs, err := filepath.Abs(*engineDir)
	if err != nil {
		log.Fatal(err)
	}
	engines := &api.Engines{Router: *router, Tessera: tesseraAbs, Dir: dirAbs, DSN: *dsn,
		HTTP: &http.Client{Timeout: 100 * time.Second}}
	defer engines.StopAll()
	server := &api.PlanServer{Pool: pool, Engines: engines}
	if err := server.StartAll(ctx); err != nil {
		log.Fatal(err)
	}

	mux := http.NewServeMux()
	mux.Handle(nanashiv1connect.NewPlanServiceHandler(server, connect.WithInterceptors(server.Interceptor())))
	// The Vite dev server sends /nanashi.v1.* to this server, so the API does not need CORS.
	var handler http.Handler = mux
	if len(networks) > 0 {
		handler = api.TrustedOnly(networks, mux)
	}
	srv := &http.Server{Addr: *listen, Handler: handler, ReadHeaderTimeout: 30 * time.Second}
	go func() {
		<-ctx.Done()
		shutdown, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		srv.Shutdown(shutdown)
	}()
	log.Printf("nanashi-api: listening on %s", *listen)
	if err := srv.ListenAndServe(); !errors.Is(err, http.ErrServerClosed) {
		log.Print(err)
	}
}
