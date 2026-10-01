# HTTP サーバー

`Workspace` を JSON の API で公開する薄いサーバーがある（標準ライブラリだけで動く）。

```bash
.venv/bin/python -m sparse_engine.server plan/ --port 8080 --checkpoint-every 1000   # FileJournal
.venv/bin/python -m sparse_engine.server s3://nanashi/plans --pg postgresql://... --model-id plan-2027 \
    --host 0.0.0.0 --advertise http://plan-a:8080 --tokens tokens.json                 # {"<トークン>": "<利用者>"}
```

| 要求 | 内容 |
|---|---|
| `GET /` | 軸と Metric の定義、公開中の版の通し番号 |
| `GET /metrics/<name>/cell?<軸>=<メンバー>` | 1 セル |
| `GET /metrics/<name>/slice?<軸>=a,b` | 範囲のセル |
| `GET /metrics/<name>/rows?<軸>=a&offset=0&limit=50` | 行の列と全行数 |
| `GET /metrics/<name>/summary?keep=Month&agg=sum&<軸>=a,b` | 集計 |
| `POST /writes` | `{"client_op_id", "reason", "expect", "ops": [{"op": "set_cell", "args": [...], "kwargs": {...}}, ...]}` |
| `GET /health` | 生きているか（公開中の版の通し番号と役割 `role`。認証なしで読める） |
| `GET /ready` | 要求を受けられるか。ライターが止まった、記録先からの開き直しに失敗した、リースを延長できない、待機系の見張りが失敗した、`Replica` が追いつけない、のどれかなら 503 と理由。役割 `role`（`leader`、`standby`、`--follow` なら `follower`）も返す（認証なしで読める） |
| `GET /stats` | 観察用の数（Prometheus のテキスト形式。確定の件数と時間、取り消した書き込み、列の長さ、書き手か（`nanashi_leader`）、リースの状態、スナップショットからの経過、`Replica` の遅れなど） |

監査に残す利用者は、本文ではなく認証で決める。
`--tokens` なら `Authorization: Bearer <トークン>` を求め、`--user-header X-Forwarded-User` なら、認証を済ませたプロキシが付けた見出しの値を使う。
どちらもなければ認証せず（利用者は空）、127.0.0.1 以外で待ち受けるのを拒む（`--insecure` で外せる）。

本文は 16 MiB（`--max-body`）、slice、rows、summary で返すセルは 10 万件（`--max-cells`）、同時に処理する要求は 64 本（`--max-threads`、超えれば 503）までで、要求の読み書きが 30 秒止まった接続は切る。

書き込みは 1 要求 1 トランザクションで、`client_op_id` が必須（再送しても二重に確定しない。再起動をまたいでも同じ）。
`expect` に読んだ版の通し番号を付けると、その後に同じセルを変えた書き込みがあれば 409 で拒否する。
列が溢れれば 429、確定を待ちきれなければ 504、式や引数の誤りは 400、認証の誤りは 401、大きすぎれば 413 で、いずれも `{"error", "message"}` を返す。
待機系への書き込みは 421 で、`leader` に書き手の番地を返す（次の段落）。
式の誤りは `code` も返す（`messages.MESSAGES` のキー。文言でなくこれで見分ける）。
内部の誤りは 500 で、文言は固定にし、原因はサーバーのログに `error_id` と一緒に残す。

同じモデルを複数のサーバーで開くと、書き込みの権利（リース）を持つ 1 つが書き手になり、ほかは待機系として追従する（`Workspace(standby=True)`。[同時の読み書き](concurrency.md)）。
待機系は読み出しを受け、書き込みは 421 と `{"error": "not_leader", "leader": <書き手の番地>}` で拒むので、送り手は `leader` へ送り直す。
ルーター（[router.md](router.md)）は `nanashi_model.lease_endpoint` で書き手を見つける。
書き手としてほかのプロセスに知らせる自分の番地は `--advertise`（既定は `http://<host>:<port>`。`0.0.0.0` で待ち受けるときは必須）、リースの期限は `--lease-ttl`（既定 30 秒）で決める。

SIGTERM と SIGINT で、受け付けた要求を処理し終え、列の書き込みを確定させ、リースを手放してから止まる（ECS などのコンテナは SIGTERM で止める）。
待機系はその通知ですぐ権利を取って書き手になる。
落ちたとき（SIGKILL）は、リースの期限が切れてから、待機系が権利を試す間隔（1 秒）までの間に引き継ぐ。

損益計画（大）で、HTTP 経由の 1 セルの読み出しは 0.24 ms、給与を 1 人変える書き込みは 0.9 ms、8 人が休みなく読み続ける中での書き込みは 2.2 ms（`bench_http.py`、手元の loopback、読み出しは毎秒約 2,800 件）。
サーバーは起動時に Python のスレッド切り替えの間隔を 0.5 ms にする（`--switch-interval`）。
