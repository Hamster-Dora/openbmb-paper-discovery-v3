"""Peewee models: `Papers` 表（与 TODO / PRD 蓄水池一致）。"""

from __future__ import annotations

from pathlib import Path

from peewee import CharField, IntegerField, Model, SqliteDatabase, TextField

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DATA_DIR = _PROJECT_ROOT / "data"
_DATA_DIR.mkdir(parents=True, exist_ok=True)

# 与 TODO 一致：SQLite 文件位于 `data/openbmb_papers.db`（相对项目根目录）。
db = SqliteDatabase(str(_DATA_DIR / "openbmb_papers.db"))


class Paper(Model):
    """单条论文记录；``arxiv_id`` 为 ArXiv id 或 ``s2:{{paperId}}`` 占位，唯一去重。

    ``status`` 常见值：PENDING、SUCCESS、REJECTED_BY_REGEX、NO_PDF（无 URL / 无法推断 arXiv PDF）、
    PDF_UNREACHABLE（有 URL 但下载或解析失败，如 403/截断/非 PDF）等。
    """

    arxiv_id = CharField(max_length=256, unique=True, index=True)
    title = TextField()
    authors = TextField(null=True)
    abs_url = TextField(null=True)
    pdf_url = TextField(null=True)
    publication_date = CharField(max_length=32, null=True, index=True)
    status = CharField(max_length=32, default="PENDING", index=True)
    ai_score = IntegerField(null=True)
    author_email = TextField(null=True)
    core_product = TextField(null=True)
    # 已写入过「建联用」Excel 的线索行（SQLite 用 0/1，避免 Boolean 绑定在部分环境下比较失效）。
    clue_exported = IntegerField(default=0, index=True)

    class Meta:
        database = db
        table_name = "papers"


def _migrate_clue_exported_column() -> None:
    """SQLite 在线迁移：为已有 `papers` 表追加 ``clue_exported`` 列（旧库无此列时）。"""
    cursor = db.execute_sql("PRAGMA table_info(papers)")
    cols = {row[1] for row in cursor.fetchall()}
    if "clue_exported" not in cols:
        db.execute_sql("ALTER TABLE papers ADD COLUMN clue_exported INTEGER NOT NULL DEFAULT 0")
    db.execute_sql(
        'CREATE INDEX IF NOT EXISTS "paper_clue_exported" ON "papers" ("clue_exported")'
    )
    db.execute_sql(
        "UPDATE papers SET clue_exported = 0 "
        "WHERE typeof(clue_exported) = 'text' OR clue_exported IS NULL"
    )
    # 大表上 ALTER/UPDATE 后，SQLite 上 ``clue_exported`` 索引偶发与行不一致，会报 malformed；重建索引可修复
    _reindex_paper_clue_exported()


def _migrate_publication_date_column() -> None:
    """SQLite 在线迁移：为已有 `papers` 表追加 ``publication_date`` 列（旧库无此列时）。"""
    cursor = db.execute_sql("PRAGMA table_info(papers)")
    cols = {row[1] for row in cursor.fetchall()}
    if "publication_date" not in cols:
        db.execute_sql("ALTER TABLE papers ADD COLUMN publication_date VARCHAR(32)")
    db.execute_sql(
        'CREATE INDEX IF NOT EXISTS "paper_publication_date" ON "papers" ("publication_date")'
    )


def _reindex_paper_clue_exported() -> None:
    cur = db.execute_sql(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='paper_clue_exported' LIMIT 1"
    )
    if not cur.fetchone():
        return
    try:
        db.execute_sql("REINDEX paper_clue_exported")
    except Exception:
        pass


def init_db() -> None:
    """建表（可重复调用；Peewee 会跳过已存在表）。"""
    db.connect(reuse_if_open=True)
    db.create_tables([Paper])
    _migrate_publication_date_column()
    _migrate_clue_exported_column()


init_db()
