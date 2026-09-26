"""Private, bounded GPS journal. Separate SQLite transactions, never the plan file.

Only device telemetry is persisted. A 202 response follows a committed write;
storage failure returns 503 so native clients can retry their offline queue.
"""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import math


class GpsHistory:
    def __init__(self, path, *, retention_seconds=604800, max_rows=100000):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.retention_seconds, self.max_rows = retention_seconds, max_rows
        with closing(self.connect()) as db, db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE IF NOT EXISTS streams (engineer TEXT PRIMARY KEY, updated REAL, data TEXT)')
            db.execute('''CREATE TABLE IF NOT EXISTS fixes (
                id INTEGER PRIMARY KEY, engineer TEXT NOT NULL, tracker TEXT NOT NULL,
                measured REAL NOT NULL, received REAL NOT NULL, data TEXT NOT NULL,
                UNIQUE(engineer, tracker, measured))''')
            db.execute('CREATE INDEX IF NOT EXISTS fixes_employee_time ON fixes(engineer, measured, id)')
        self._writes = 0

    def connect(self):
        db = sqlite3.connect(self.path, timeout=.2)
        db.execute('PRAGMA synchronous=FULL')
        return db

    def prune(self, db, now):
        db.execute('DELETE FROM fixes WHERE measured < ?', ((now-self.retention_seconds)*1000,))
        db.execute('DELETE FROM streams WHERE updated < ?', (now-self.retention_seconds,))
        db.execute('DELETE FROM fixes WHERE id IN (SELECT id FROM fixes ORDER BY measured DESC, id DESC LIMIT -1 OFFSET ?)', (self.max_rows,))
        for engineer, raw in db.execute('SELECT engineer,data FROM streams').fetchall():
            data = json.loads(raw)
            for key in ('last_good','last_raw'):
                if data.get(key) and data[key]['measured_at_ms'] < (now-self.retention_seconds)*1000:
                    data[key] = None
            db.execute('UPDATE streams SET data=? WHERE engineer=?', (json.dumps(data),engineer))

    def save(self, engineer, stream, sample, now):
        metadata = {k: stream[k] for k in ('tracker_id', 'received_at', 'state', 'last_raw',
                    'last_good', 'packet_sequence', 'quality', 'seq')}
        metadata.update({k:stream[k] for k in ('quality_detail','quality_notice','conflict_until','conflict_detail','recovery','continuity_version','points_since_ms') if k in stream})
        with closing(self.connect()) as db, db:
            if sample:
                self._insert(db, engineer, stream['tracker_id'], sample)
            db.execute('INSERT OR REPLACE INTO streams VALUES (?,?,?)',
                       (engineer, now, json.dumps(metadata, allow_nan=False)))
            self._writes += 1
            if self._writes % 100 == 1:
                self.prune(db, now)

    @staticmethod
    def _insert(db, engineer, tracker, sample):
        db.execute('INSERT OR IGNORE INTO fixes(engineer,tracker,measured,received,data) VALUES (?,?,?,?,?)',
                   (engineer, tracker, sample['point']['measured_at_ms'], sample['received_at'],
                    json.dumps(sample, allow_nan=False)))

    def archive(self, engineer, tracker, sample, now):
        # Delayed offline uploads belong to history, never to current location.
        with closing(self.connect()) as db, db:
            self._insert(db, engineer, tracker, sample)
            self.prune(db, now)

    def restore(self, now, limit):
        with closing(self.connect()) as db, db:
            self.prune(db, now)
            result = {}
            for engineer, data in db.execute('SELECT engineer,data FROM streams'):
                meta = json.loads(data)
                rows = db.execute('SELECT data FROM fixes WHERE engineer=? AND tracker=? ORDER BY measured DESC LIMIT ?',
                                  (engineer, meta['tracker_id'], limit)).fetchall()
                result[engineer] = (meta, [json.loads(row[0]) for row in reversed(rows)])
            return result

    def handoff_evidence(self, engineer, tracker, stale_seconds, limit=2000):
        """Bounded read-only evidence for the legacy token-reset bug."""
        with closing(self.connect()) as db:
            first=db.execute('SELECT id,measured,data FROM fixes WHERE engineer=? AND tracker=? ORDER BY id LIMIT 1',
                             (engineer,tracker)).fetchone()
            if not first:return None
            previous=db.execute('SELECT data FROM fixes WHERE engineer=? AND tracker<>? AND id<? '
                                'AND measured<? AND measured>=? ORDER BY measured DESC LIMIT 120',
                                (engineer,tracker,first[0],first[1],first[1]-stale_seconds*1000)).fetchall()
            anchor=next((json.loads(r[0])['point'] for r in previous if json.loads(r[0]).get('accepted')),None)
            if not anchor:return None
            rows=db.execute('SELECT data FROM fixes WHERE engineer=? AND tracker=? ORDER BY id LIMIT ?',
                            (engineer,tracker,limit+1)).fetchall()
            if len(rows)>limit:return None
        return anchor,[json.loads(r[0]) for r in rows]

    def latest(self, engineer, tracker, now):
        """Last sample from this exact device, including a rejected/offline fix."""
        with closing(self.connect()) as db:
            row = db.execute('SELECT data FROM fixes WHERE engineer=? AND tracker=? AND measured>=? '
                             'ORDER BY measured DESC, id DESC LIMIT 1',
                             (engineer, tracker, (now-self.retention_seconds)*1000)).fetchone()
        return json.loads(row[0]) if row else None

    def history(self, engineer, now, *, before=None, limit=500):
        # Cursor is (timestamp,id), so equal timestamps from different devices
        # do not disappear between pages. History is returned newest first.
        if type(limit) is not int or not 1 <= limit <= 2000:
            raise ValueError('GPS: limit должен быть от 1 до 2000')
        clause, args = '', [engineer, (now-self.retention_seconds)*1000]
        if before:
            stamp, key = before.split(':')
            stamp, key = float(stamp), int(key)
            if not math.isfinite(stamp) or stamp < 0 or key < 1:
                raise ValueError('GPS: неверный курсор')
            clause = ' AND (measured < ? OR (measured = ? AND id < ?))'
            args += [stamp, stamp, key]
        with closing(self.connect()) as db:
            rows = db.execute('SELECT id,measured,data FROM fixes WHERE engineer=? AND measured>=?'+clause+
                              ' ORDER BY measured DESC,id DESC LIMIT ?', (*args, limit+1)).fetchall()
        more, rows = len(rows)>limit, rows[:limit]
        return {'samples': [json.loads(row[2]) for row in rows],
                'next_cursor': f'{rows[-1][1]}:{rows[-1][0]}' if more else None,
                'retention_seconds': self.retention_seconds, 'persistent': True}
