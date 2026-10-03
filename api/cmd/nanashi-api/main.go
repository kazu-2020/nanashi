// nanashi-api starts the application server.
package main

import (
	"context"
	"flag"
	"log"
	"net/http"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/kazu-2020/nanashi/api"
	"github.com/kazu-2020/nanashi/api/gen/nanashi/v1/nanashiv1connect"
)

func main() {
	dsn := flag.String("pg", "postgresql://postgres@127.0.0.1:55432/nanashi", "DSN of the engine journal")
	listen := flag.String("listen", "127.0.0.1:8080", "address to listen on")
	engine := flag.String("engine", "http://127.0.0.1:8090", "base URL of the router")
	user := flag.String("user", "", "user that the API sends to the engine until the login exists")
	flag.Parse()

	pool, err := pgxpool.New(context.Background(), *dsn)
	if err != nil {
		log.Fatal(err)
	}
	defer pool.Close()

	mux := http.NewServeMux()
	mux.Handle(nanashiv1connect.NewModelServiceHandler(&api.ModelServer{Pool: pool}))
	mux.Handle(nanashiv1connect.NewDimensionServiceHandler(&api.DimensionServer{Engine: *engine, Client: http.DefaultClient, User: *user}))
	// The Vite dev server sends /nanashi.v1.* to this server, so the API does not need CORS.
	log.Printf("nanashi-api: listening on %s", *listen)
	srv := &http.Server{Addr: *listen, Handler: mux, ReadHeaderTimeout: 30 * time.Second}
	log.Fatal(srv.ListenAndServe())
}
