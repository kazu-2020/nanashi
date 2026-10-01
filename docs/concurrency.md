# 同時の読み書き

複数の利用者が同時に読み書きするときは、`Workspace` にモデルを預ける。
`Workspace` は確定した状態を **版** として公開し、書き込みを 1 本のスレッド（ライター）で順に処理する。

```python
from sparse_engine.workspace import Conflict, Workspace

ws = Workspace.open("plan/", RustEngine(), checkpoint_every=1000)  # 記録から復元したモデルを預かる

seq = ws.write(lambda m: m.set_cell("Price", 12, Product="A"), user="alice", reason="値上げ")
v = ws.version                                   # 公開中の版（読み出し専用のビュー）
v.get("Revenue", Product="A", Month="Jan")       # get、slice、rows、summarize、value が使える
what_if = v.fork()                               # 手元で試すための複製（普通の Model）

try:  # 読んだ版より後に、同じセルを他人が変えていたら拒否する
    ws.write(lambda m: m.set_cell("Price", 13, Product="A"), user="bob", expect=v.seq)
except Conflict as e:
    print(e.user, e.seq)                         # 誰が、どの書き込みで変えたか
```

版は作ったら変えない。
`version` は読み出し専用のビュー（`Version`）で、書き込む操作を持たない（裏の `Model` は `version.model` で取れるが、操作を呼ぶと ValueError）。
読み出しはいつでも公開中の版をまるごと見るので、書き込みの途中の値（明細は新しいのに合計が古い、など）や、取り消された変更は見えない。
書き込みは公開していない複製だけを書き換える。
格納データの本体は版どうしで共有するので、版を作る費用は差分の分だけで済み、古い版は読んでいる人がいなくなれば捨てられる。

ライターは、列に溜まっている書き込みを 1 つのまとまりにして、公開中の版の複製に 1 件ずつトランザクションとして適用する。
記録は 1 回の書き出しでまとめて確定し（グループコミット）、そのあとで複製を新しい版として公開する。

- 1 件の書き込みが失敗したら、その 1 件だけを取り消し、同じまとまりのほかの書き込みは確定する。
- 記録の書き出しに失敗したら、まとまり全体を捨て、公開中の版は変えない。
- `expect=` に読んだ版の通し番号を渡すと、それより後に同じセルを変えた書き込みがあれば `Conflict` にする。比べるのは、その書き込みが実際に書き換えた入力セルなので、按分なら配った範囲全体が対象になる。
- `client_op_id=` が確定済みなら、適用せずに元の通し番号を返す。確定済みかどうかは、まとまりごとに 1 回で記録先に引く。
- `max_queue=` で列の長さを決めると、溢れたときは `submit` が `timeout=` の間だけ待ってから `Overloaded` を投げる。`write(timeout=)` は確定を待つ長さで、過ぎれば列から取り消して `TimeoutError`。
- `checkpoint_every=`（記録の件数）か `checkpoint_interval=`（秒）を決めると、その間隔で公開した版のスナップショットを別のスレッドで取る。取っている間も書き込みは止まらない（格納データの本体は版どうしで共有している）。
- 記録先が「手元の版が古い」と言えば（`journal.Stale`。別のプロセスが書き込んだとき）、そのまとまりを失敗にしてから、記録先の最新の版に追いつく。ほかのプロセスが確定した記録を公開中の版の複製に入力の変更として書き込み、影響範囲だけを計算し直す（追いつけなければ開き直す）。

読み手を複数のプロセスに増やすときは、書き込むプロセス（`Workspace`）とは別のプロセスで `Replica` を使う。
`Replica` は記録先に追従する読み出し専用の版で、読み出しは `Workspace` と同じく `version` で行う。

```python
from sparse_engine.workspace import Replica

replica = Replica(PgJournal(dsn, "plan-2027", "s3://nanashi/plans", heartbeat=False), RustEngine())
replica.version.get("Revenue", Product="A", Month="Jan")
```

別のスレッドで記録先を見張り（`PgJournal` は確定のたびに書き手が送る `NOTIFY nanashi_head`、`FileJournal` はファイルの長さ）、ほかのプロセスが確定した記録を、公開中の版の複製に入力の変更として書き込み、影響範囲だけを計算し直して新しい版として公開する。
軸、メンバー、Metric の定義を変えた記録は、全体を計算し直す。
HTTP サーバーでは `--follow` で、書き込みを受けない（405）読み出し専用のサーバーになる。

同じモデルを複数のプロセスで開き、書き手が止まっても別のプロセスが書き込みを引き継ぐには、`Workspace(standby=True)` にする（HTTP サーバーは常にこれで開く）。
各プロセスは書き手（`Role.LEADER`）か待機系（`Role.STANDBY`）のどちらかで、`ws.role` で分かる。

```python
from sparse_engine.workspace import NotLeader, Workspace

ws = Workspace.open(PgJournal(dsn, "plan-2027", "s3://nanashi/plans", endpoint="http://plan-b:8080"),
                    RustEngine(), standby=True)
ws.role                                          # 書き手がいれば Role.STANDBY
try:
    ws.write(lambda m: m.set_cell("Price", 12, Product="A"))
except NotLeader as e:
    print(e.leader)                              # 書き手の番地（http://plan-a:8080）。そちらへ送る
```

- 開くときに記録先の権利を `take` で試す。取れれば記録に追いついて書き手になり、取れなければ待機系になる。
- 待機系は見張りのスレッド（`nanashi-standby`）で記録先に追従し（`Replica` と同じ仕組み）、読み出しを受ける。書き込みは `NotLeader`（`leader` に書き手の番地）で待たずに拒む。
- 待機系は間隔（`interval=`、既定 1 秒）ごとに権利を取れるか試す。書き手が `close` で手放せば（SIGTERM）その通知ですぐ、落ちて期限が切れればその間隔で取り、取ってからもう一度記録に追いついて書き手になる。権利を持っている間はほかのプロセスは確定できないので、追いついた版は最新である。
- 書き手は、確定が締め出された（`Fenced`）か、見張りが権利を持っていないと気づいたら（延長できなかった）待機系に戻る。そのまとまりの書き込みと、その直前に列に入っていた書き込みは `NotLeader` になる。開き直さず、追いつくのは見張りに任せる。
- 昇格のたびに版を公開し直すので、それより前の版の通し番号を `expect` に付けた書き込みは `Conflict` になる（開き直したときと同じ）。
- 空いた権利を複数の待機系が同時に取りに行っても、取れるのは 1 つ（記録先の 1 回の条件付き更新）。
- `/ready` に当たる `ready()` は、待機系でも空（読み出しは受けられる）。見張りが失敗していれば、その理由を返す。

`standby=False`（既定）なら今までと同じで、最初の書き込みで権利を取り、別のプロセスが書いていれば開き直す。
`FileJournal` でも `standby=True` は使える（権利は `lock` の排他ロック）。

損益計画（490 万セル）で、8 人が 50 件ずつ給与を書き込むと、毎秒約 500 件を確定する（1 件ずつトランザクションで確定すると毎秒約 190 件）。
応答の時間は中央値 15 ms、95 パーセンタイル 21 ms で、1 回の書き出しで平均 4 件を確定した（手元の macOS、ディスクまで書き出す設定）。
