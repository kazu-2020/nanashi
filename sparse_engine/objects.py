"""ファイルの置き場所（BlobStore）。記録先が、スナップショットと大量の変更のファイルを置く。

置き場所は次のどちらか（open_objects が文字列から選ぶ）。

    s3://<バケット>/<接頭辞>   S3 互換のオブジェクトストレージ（boto3 を使う）。接続先と認証情報は boto3 の
                              決まりどおり環境変数（AWS_ENDPOINT_URL、AWS_ACCESS_KEY_ID、
                              AWS_SECRET_ACCESS_KEY など）から読む
    それ以外                   ローカルのディレクトリ。一時ファイルに書いてディスクまで書き出してから名前を変える

どちらも、置き場所の中の相対的なキー（"model/snapshots/…/model.json" など）で読み書きする。

    put(キー, 中身)    置く。置いたオブジェクトは書き換えない（呼ぶ側がキーに乱数を含める）ので、読み手が
                      書きかけのものを見ることはない
    get(キー)         読む。無ければ FileNotFoundError
    list(接頭辞)       その接頭辞で始まるキーの一覧（順不同）
    delete(キー)       消す（無くてもよい）

記録先はキーだけを保存する（置き場所の絶対パスや URI を保存しないので、置き場所を移しても読める）。
以前の版が保存した絶対パスや s3:// の URI も get で読める。
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Protocol


class BlobStore(Protocol):
    def put(self, key: str, data: bytes) -> None: ...
    def get(self, key: str) -> bytes: ...
    def list(self, prefix: str) -> list[str]: ...
    def delete(self, key: str) -> None: ...
    def uri(self, key: str) -> str:
        """キーの置き場所（表示とログ用）。"""


class LocalObjects:
    def __init__(self, root, *, fsync: bool = True):
        self.root = Path(root)
        self.fsync = fsync

    def uri(self, key: str) -> str:
        return str(self.root / key)

    def put(self, key: str, data: bytes) -> None:
        from .journal import _fsync_dir, _sync
        final = self.root / key
        final.parent.mkdir(parents=True, exist_ok=True)
        tmp = final.with_name(f".tmp-{uuid.uuid4().hex}")
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            if self.fsync:
                _sync(f.fileno())
        os.rename(tmp, final)
        if self.fsync:
            _fsync_dir(final.parent)

    def get(self, key: str) -> bytes:
        # 以前の版は置き場所の絶対パスを保存していた
        return (Path(key) if os.path.isabs(key) else self.root / key).read_bytes()

    def list(self, prefix: str) -> list[str]:
        base = self.root / prefix
        start = base if base.is_dir() else base.parent
        if not start.is_dir():
            return []
        out = []
        for dirpath, _, names in os.walk(start):
            for n in names:
                if n.startswith(".tmp-"):
                    continue
                key = Path(dirpath, n).relative_to(self.root).as_posix()
                if key.startswith(prefix):
                    out.append(key)
        return out

    def delete(self, key: str) -> None:
        (self.root / key).unlink(missing_ok=True)


class S3Objects:
    def __init__(self, url: str, client=None):
        """url は s3://<バケット>/<接頭辞>。client を省略すると boto3.client("s3")（設定は環境変数から）。"""
        bucket, _, prefix = url.removeprefix("s3://").partition("/")
        self.bucket, self.prefix = bucket, prefix.strip("/")
        if client is None:
            import boto3
            client = boto3.client("s3")
        self.client = client

    def uri(self, key: str) -> str:
        return f"s3://{self.bucket}/{self._key(key)}"

    def _key(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def put(self, key: str, data: bytes) -> None:
        self.client.put_object(Bucket=self.bucket, Key=self._key(key), Body=data)

    def get(self, key: str) -> bytes:
        if os.path.isabs(key):  # オブジェクトストレージに移る前に、ローカルのディレクトリに置いたもの
            return Path(key).read_bytes()
        bucket, name = self.bucket, self._key(key)
        if key.startswith("s3://"):  # 以前の版は URI を保存していた
            bucket, _, name = key.removeprefix("s3://").partition("/")
        try:
            return self.client.get_object(Bucket=bucket, Key=name)["Body"].read()
        except self.client.exceptions.NoSuchKey:
            raise FileNotFoundError(self.uri(key)) from None

    def list(self, prefix: str) -> list[str]:
        strip = len(self._key(""))
        pages = self.client.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=self._key(prefix))
        return [o["Key"][strip:] for page in pages for o in page.get("Contents", [])]

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=self._key(key))


def open_objects(location, *, fsync: bool = True) -> BlobStore:
    """s3://… なら S3Objects、それ以外はローカルのディレクトリ（LocalObjects）。作ったものならそのまま返す。"""
    if isinstance(location, (LocalObjects, S3Objects)):
        return location
    if str(location).startswith("s3://"):
        return S3Objects(str(location))
    return LocalObjects(location, fsync=fsync)
