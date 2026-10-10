# Glossary

This glossary gives the product terms for `web/`, `api/`, `proto/` and `tessera/`. Use these terms in documents, comments and specifications. The column "UI" gives the Japanese word that the screens show.

The engine terms (for example partition dimension, delta, journal) are in the glossary in `tessera/CLAUDE.md`.

| Term | UI | Meaning |
|---|---|---|
| block | ブロック | A list or a Metric. A formula refers to a block by its name. The name of a block is unique among all blocks of an application. Tables, views and boards are not blocks. |
| dimension list | ディメンション | A list of the kind dimension. A user adds and removes its members. In the code, the kind is `LIST_KIND_DIMENSION` (`proto/`). |
