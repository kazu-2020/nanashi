"""オブジェクトストレージ。PostgreSQL の記録先が、スナップショットと大量の変更のファイルを置く。

置き場所は次のどちらか（open_objects が文字列から選ぶ）。

    s3://<バケット>/<接頭辞>   S3 互換のオブジェクトストレージ（boto3 を使う）。接続先と認証情報は boto3 の
                              決まりどおり環境変数（AWS_ENDPOINT_URL、AWS_ACCESS_KEY_ID、
                              AWS_SECRET_ACCESS_KEY など）から読む
    それ以外                   ローカルのディレクトリ。一時ファイルに書いてディスクまで書き出してから名前を変える

どちらも put(キー, 中身) で置いて置き場所（URI）を返し、get(URI) で読む。無ければ FileNotFoundError。
置いたオブジェクトは書き換えない（呼ぶ側がキーに乱数を含める）ので、読み手が書きかけのものを見ることはない。
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path


class LocalObjects:
    def __init__(self, root):
        self.root = Path(root)

    def uri(self, key: str) -> str:
        return str(self.root / key)

    def put(self, key: str, data: bytes) -> str:
        from .journal import _fsync_dir, _sync
        final = self.root / key
        final.parent.mkdir(parents=True, exist_ok=True)
        tmp = final.with_name(f".tmp-{uuid.uuid4().hex}")
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            _sync(f.fileno())
        os.rename(tmp, final)
        _fsync_dir(final.parent)
        return str(final)

    def get(self, uri: str) -> bytes:
        return Path(uri).read_bytes()


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

    def put(self, key: str, data: bytes) -> str:
        self.client.put_object(Bucket=self.bucket, Key=self._key(key), Body=data)
        return self.uri(key)

    def get(self, uri: str) -> bytes:
        if not uri.startswith("s3://"):  # オブジェクトストレージに移る前に、ローカルのディレクトリに置いたもの
            return Path(uri).read_bytes()
        bucket, _, key = uri.removeprefix("s3://").partition("/")
        try:
            return self.client.get_object(Bucket=bucket, Key=key)["Body"].read()
        except self.client.exceptions.NoSuchKey:
            raise FileNotFoundError(uri) from None


Objects = LocalObjects | S3Objects


def open_objects(location) -> Objects:
    """s3://… なら S3Objects、それ以外はローカルのディレクトリ（LocalObjects）。作ったものならそのまま返す。"""
    if isinstance(location, (LocalObjects, S3Objects)):
        return location
    if str(location).startswith("s3://"):
        return S3Objects(str(location))
    return LocalObjects(location)
