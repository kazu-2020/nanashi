"""テストで使う記録先。同じテストをファイルと PostgreSQL の両方の記録先で回すために使う。

    class FileBasics(JournalCase, Basics): store = FileStore
    class PgBasics(JournalCase, Basics): store = PgStore   # PostgreSQL がなければスキップする

JournalCase は setUp で self.journals（記録先の置き場所）を作る。self.journals.journal() は、同じ置き場所を
指す新しい記録先を返す（別のプロセスが開いたのと同じ）。後片付けはテストの終わりに行う。
"""
from __future__ import annotations

import importlib
import os
import tempfile
import unittest
import uuid

from sparse_engine.journal import FileJournal

DSN = os.environ.get("NANASHI_PG_DSN", "postgresql://postgres@127.0.0.1:55432/nanashi")


def _pg_available() -> bool:
    try:
        importlib.import_module("nanashi_core")  # 保存形式（Parquet）の読み書きに使う
        import psycopg
        psycopg.connect(DSN, connect_timeout=2).close()
        from sparse_engine.pg_journal import migrate
        migrate(DSN)  # テストのデータベースのスキーマを最新にする
        return True
    except Exception:
        return False


PG_AVAILABLE = _pg_available()


class FileStore:
    """一時ディレクトリの FileJournal。"""
    name = "file"
    available = True

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = self.tmp.name

    def journal(self, **kwargs) -> FileJournal:
        return FileJournal(self.path, **kwargs)

    def close(self) -> None:
        self.tmp.cleanup()


class PgStore:
    """テストごとに別のモデルの ID を使う PgJournal。スナップショットと大量の変更は一時ディレクトリに置く。"""
    name = "pg"
    available = PG_AVAILABLE

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = self.tmp.name
        self.model_id = f"test-{uuid.uuid4().hex[:12]}"
        self.opened = []

    def journal(self, **kwargs):
        from sparse_engine.pg_journal import PgJournal
        j = PgJournal(DSN, self.model_id, self.path, **kwargs)
        self.opened.append(j)
        return j

    def close(self) -> None:
        try:
            self.journal().drop()
        finally:
            for j in self.opened:
                j.close()
            self.tmp.cleanup()


class JournalCase:
    """store（FileStore か PgStore）の記録先でテストを回す混ぜ込み。unittest.TestCase の前に置く。"""
    store: type = FileStore

    def setUp(self):
        if not self.store.available:
            raise unittest.SkipTest("PostgreSQL（NANASHI_PG_DSN）と psycopg、nanashi_core が必要")
        self.journals = self.store()
        self.addCleanup(self.journals.close)
        super().setUp()
