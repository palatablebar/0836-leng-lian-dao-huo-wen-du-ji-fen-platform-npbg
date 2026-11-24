import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init()

    def connect(self):
        connection = sqlite3.connect(self.path, timeout=8)
        connection.row_factory = sqlite3.Row
        return connection

    def init(self):
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS runs(
              id TEXT PRIMARY KEY,
              request_key TEXT UNIQUE NOT NULL,
              created_at TEXT NOT NULL,
              profile TEXT NOT NULL,
              status TEXT NOT NULL,
              revision INTEGER NOT NULL,
              payload TEXT NOT NULL,
              comment TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS probe_handovers(
              id TEXT PRIMARY KEY,
              run_id TEXT NOT NULL,
              batch_id TEXT,
              client_key TEXT,
              content_digest TEXT NOT NULL,
              record TEXT NOT NULL,
              created_at TEXT NOT NULL,
              UNIQUE(run_id, content_digest)
            );
            ''')

    def find_by_request(self, request_key):
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE request_key=?", (request_key,)).fetchone()
        return dict(row) if row else None

    def create(self, request_key, profile, payload):
        existing = self.find_by_request(request_key)
        if existing:
            return existing
        run_id = uuid.uuid4().hex[:12]
        with self.connect() as db:
            db.execute("INSERT INTO runs(id,request_key,created_at,profile,status,revision,payload) VALUES(?,?,?,?,?,?,?)",
                       (run_id, request_key, datetime.now(timezone.utc).isoformat(), profile, "DRAFT", 0,
                        json.dumps(payload, ensure_ascii=False)))
        return self.get(run_id)

    def get(self, run_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def review(self, run_id, decision, comment, expected_revision):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                return None, "missing"
            if row["revision"] != expected_revision:
                return None, "stale"
            db.execute("UPDATE runs SET status=?, revision=revision+1, comment=? WHERE id=?",
                       (decision, comment, run_id))
        return self.get(run_id), None

    # ------------------------------------------------------------------
    # 探头交接记录：按内容摘要幂等去重，重跑同一批不重复累计
    # ------------------------------------------------------------------
    def handovers(self, run_id):
        with self.connect() as db:
            rows = db.execute(
                "SELECT id, run_id, batch_id, client_key, content_digest, record, created_at "
                "FROM probe_handovers WHERE run_id=? ORDER BY created_at, id", (run_id,)).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            try:
                item["data"] = json.loads(item.pop("record"))
            except (TypeError, ValueError):
                item["data"] = {}
            items.append(item)
        return items

    def handover_digests(self, run_id):
        return {row["content_digest"] for row in self.handovers(run_id)}

    def add_handover(self, run_id, record, content_digest, client_key=None):
        """追加一条交接记录。

        返回 (changed, row)：相同内容摘要重试时 changed=False 且不新增版本；
        新内容写入并令 revision+1。相同 client_key 但内容变化视为迟到更正，同样升版本。
        """
        handover_id = uuid.uuid4().hex[:12]
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            same = db.execute(
                "SELECT id FROM probe_handovers WHERE run_id=? AND content_digest=?",
                (run_id, content_digest)).fetchone()
            if same is not None:
                return False, self.get(run_id)
            if client_key:
                prior = db.execute(
                    "SELECT id, content_digest FROM probe_handovers WHERE run_id=? AND client_key=?",
                    (run_id, client_key)).fetchone()
                if prior is not None:
                    if prior["content_digest"] == content_digest:
                        return False, self.get(run_id)
                    db.execute(
                        "UPDATE probe_handovers SET content_digest=?, record=?, created_at=?, batch_id=? "
                        "WHERE id=?",
                        (content_digest, json.dumps(record, ensure_ascii=False), now,
                         record.get("batch_id"), prior["id"]))
                    db.execute("UPDATE runs SET revision=revision+1 WHERE id=?", (run_id,))
                    return True, self.get(run_id)
            db.execute(
                "INSERT INTO probe_handovers(id,run_id,batch_id,client_key,content_digest,record,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (handover_id, run_id, record.get("batch_id"), client_key, content_digest,
                 json.dumps(record, ensure_ascii=False), now))
            db.execute("UPDATE runs SET revision=revision+1 WHERE id=?", (run_id,))
        return True, self.get(run_id)

    def save_payload(self, run_id, payload):
        with self.connect() as db:
            db.execute("UPDATE runs SET payload=? WHERE id=?",
                       (json.dumps(payload, ensure_ascii=False), run_id))
