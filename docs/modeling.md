# モデルの操作

## 値の読み出し

読み出しは、必要な分だけ読む 4 つの口と、全部を読む `value` がある。
大きな Metric では `value` が Metric 全体を Python の dict に変換するので、表示や API には必要な分だけ読む口を使う。

```python
m.get("Revenue", Product="p0001", Version="予算", Month="m01")   # 1 セル。空なら None
m.slice("Revenue", Product="p0001")                              # 範囲を Cube で（軸はメンバー名か、その集まり）
m.rows("Payroll", Department="営業", offset=0, limit=50)          # 行の列と全行数（宣言した軸の順に並ぶ）
m.summarize("Revenue", keep=["Month"], Product=["p0001", "p0002"])  # keep の軸だけ残して集計（SUM、AVG、MIN、MAX、COUNT）
```

Rust のエンジンでは、これらは格納データを丸ごと読まない。1 セルの `get` 以外は GIL も外して読む。
490 万セルの損益計画で、137 万セルの Metric の 1 セルを `get` で読むのは 0.01 ms 未満、`value` で丸ごと読むと約 500 ms かかる（[性能](performance.md)の表）。

## 軸とメンバー

軸のメンバーは、実行中に `add_member` で末尾に追加できる。
新しいメンバーはどの Metric でも空で始まり、全メンバーへ値を広げる演算（定数との足し算、`IFBLANK`、引き下ろし、前月参照など）だけが新しいメンバーに値を作る。
追加も入力の変更と同じく、影響する範囲だけを計算し直す。

```python
m.add_member("Employee", "dave")                         # どの Metric でも空で始まる
m.set_cell("DeptOf", "開発", Employee="dave", Month="Mar")
m.add_member("Month", "Apr")                             # 順序付きの軸は最後の時点の次に入る
```

軸にプロパティがあれば、`m.add_member("Product", "p9", Category="ハード")` のように追加と同時に値を設定できる。

メンバーの名前は `rename_member` で変えられ、`remove_member` で消せる。

```python
m.rename_member("Employee", "dave", "David")  # 値、プロパティ、式の Employee."dave" がすべて新しい名前になる
m.remove_member("Month", "Feb")               # Mar の前月は Jan になる
```

軸、メンバー、Metric は、モデルの中で一意の **変わらない ID** を持つ。
名前を変えても ID は変わらず、消した ID は再利用しない。
エンジンの中ではメンバーを並び順の番号で扱い、この番号はメンバーを消すと詰まるので、変更の記録や外部とのやり取りには ID を使う。

```python
pid = m.dimensions["Product"].id_of("p9")      # メンバーの ID
m.dimensions["Product"].member_of(pid)         # ID から今の名前
m.metrics["Revenue"].id, m.metric_name(mid)    # Metric の ID と、ID から今の名前
```

複製（`fork`）は同じ番号から ID を振り続けるので、複製と元で別々に足したものが同じ ID になりうる。

エンジンの中ではメンバーを番号で持つので、名前を変えても値は変わらず、何も計算し直さない。

メンバーを消すと、そのメンバーのセルはすべての Metric から消える。
プロパティの対応表からも外れ、そのメンバーを参照先にしていたメンバーは参照先なしになる。
メンバー型の Metric でそのメンバーを指していた値（締め月が Feb など）は空になる。
式が `Month."Feb"` のようにそのメンバーを書いている場合は、先に式を直さないと消せない。

削除は 2 段階で計算し直す。
まず、入力のうちそのメンバーのセルと、そのメンバーを指す値を空にし、普通の入力の変更として計算し直す。
こうすると、差分集計と値の変化による絞り込みがそのまま効く。
次にメンバーそのものを消す。
空になったメンバーを消してもなお変わるのは、次の 2 つだけである。

- 全メンバーへ値を広げる演算（`X + 1` など）がそのメンバーに作っていたセルと、それを集計した値
- そのメンバーをまたぐ前月参照

この 2 つが届く範囲だけを計算し直す。

## 計画の入力

計算 Metric を `add_formula(..., overridable=True)` で登録すると、`set_cell` で式の結果を手入力で上書きできる。
上書きした値は式より優先され、下流の集計にもそのまま伝わる。
`set_cell` で `None` を入れたセルは、式の結果に戻る。

```python
m.add_formula("Bonus", ["Employee"], "Salary * 0.1", overridable=True)
m.set_cell("Bonus", 8, Employee="alice")   # alice だけ手入力
```

`spread` は、上位の合計値を入力 Metric の範囲へ配る。
範囲は軸のメンバーと、「軸.プロパティ」の絞り込みで指定する。
既定では今の値の比率で配り、値がなければ均等に配る（`how="even"` で常に均等）。
配ったセルは 1 セルずつではなくまとめて書き込むので、22.5 万セルの按分でも数十 ms で終わる。

```python
m.spread("Budget", 12_000, Version="予算", Month="m01", where={"Product.Category": "ハード"})
```

## ホワットイフ分析

`fork` はモデルを複製する。
複製での入力、上書き、メンバーの追加は元のモデルに影響せず、元の変更も複製に影響しない。
元の計画を壊さずに「値上げしたら利益はどうなるか」を試し、比べてから捨てられる。

```python
what_if = m.fork()
what_if.set_cell("Salary", 70, Employee="alice")
print(what_if.value("Cash").cells, m.value("Cash").cells)
```

Rust のエンジンでは、格納データの本体（キー順の配列）を複製どうしで共有し、書き換えは古い版を壊さない永続的な木（差分）に入れる。
複製は Metric の数に比例する時間で済み（490 万セルのモデルで 1 ms 未満）、複製した側で書き換えても本体は写さない。
