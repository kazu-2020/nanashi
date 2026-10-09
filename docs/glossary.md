# Glossary

This glossary gives the product terms for `web/`, `api/`, `proto/` and `tessera/`. Use these terms in documents, comments and specifications. The column "UI" gives the Japanese word that the screens show.

The engine terms (for example partition dimension, delta, journal) are in the glossary in `tessera/CLAUDE.md`.

## Terms

| Term | UI | Meaning |
|---|---|---|
| block | ブロック | A list or a Metric. A formula refers to a block by its name. Tables, views and boards are items, not blocks. |
| model namespace | — | The names of all blocks of an application. Each name is unique in it. |
| list | リスト | A named set of members. |
| list kind | 種類 | The type of a list: dimension, transaction, calendar or scenario. |
| dimension list | ディメンションリスト | A list of the kind dimension. A user adds and removes its members. |
| axis | 軸 | A list in the dimensions of a Metric. "Axis" is the role of a list in a Metric. |
| member | メンバー | An item of a list. |
| property | プロパティ | An attribute of a list. It has one value for each member. |
| property Metric | — | The Metric that keeps the values of a NUMBER or BOOLEAN property. Its name is `<list>.<property>`. |
| Metric | Metric | A multidimensional set of cells. Its axes are lists. |
| list namespace | — | The names in one list. The member names are unique in it, and the property names are unique in it. |
| name | 名前 | An attribute of a block, a member or a property. A user can change it. |
| id | ID | The identifier of an object. It is a UUIDv7 and it does not change (`ids.md`). |
| name normalization | — | Remove the white space and the control characters at the start and the end of a name. |

## Names in the code

Some code uses older words. Use the terms above in new text. Do not rename the code only for the glossary.

| Term | Word in the code |
|---|---|
| list | `dimension`, `dim` (`tessera/`). `List` (`proto/`, `api/`). |
| dimension list | `LIST_KIND_DIMENSION` (`proto/`) |
| axis | `dimensions`, `dims` of a Metric |
| model namespace | `_dim_ids` and `_metric_ids` (`tessera/sparse_engine/model.py`) |
