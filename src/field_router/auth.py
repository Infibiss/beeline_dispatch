"""Local account store and revocable sessions, independent of the planner."""
from collections import OrderedDict
from contextlib import closing
import hashlib
import hmac
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path


class AuthError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def password_hash(password, salt):
    return hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 600_000)


def validate_password(value):
    if not isinstance(value, str) or not 12 <= len(value) <= 128:
        raise AuthError('Пароль: от 12 до 128 символов')


class AccountStore:
    def __init__(self, path, *, clock=time.time):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.lock = threading.RLock()
        self.sessions = OrderedDict()
        self.attempts = OrderedDict()
        with closing(self.connect()) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS accounts (
                username TEXT PRIMARY KEY, salt BLOB NOT NULL, digest BLOB NOT NULL,
                role TEXT NOT NULL, region TEXT, engineer_id TEXT,
                enabled INTEGER NOT NULL DEFAULT 1, revision INTEGER NOT NULL DEFAULT 1,
                UNIQUE(region, engineer_id))""")
        self.dummy_salt = secrets.token_bytes(16)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def public(row):
        return {key: row[key] for key in ('username', 'role', 'region', 'engineer_id', 'enabled')}

    def bootstrap(self, password=None):
        """Return a generated password only once, on creation; never persist it."""
        with closing(self.connect()) as db, db:
            if db.execute("SELECT 1 FROM accounts WHERE role='admin'").fetchone():
                return None
            password = password or secrets.token_urlsafe(18)
            validate_password(password)
            salt = secrets.token_bytes(16)
            db.execute("INSERT INTO accounts(username,salt,digest,role) VALUES(?,?,?,'admin')",
                       ('admin', salt, password_hash(password, salt)))
            return password

    def list(self):
        with closing(self.connect()) as db:
            return [self.public(row) for row in db.execute('SELECT * FROM accounts ORDER BY username')]

    def create_engineer(self, username, password, region, engineer_id):
        if not isinstance(username, str) or not re.fullmatch(r'[a-zA-Z0-9._-]{3,64}', username):
            raise AuthError('Логин: 3–64 латинских буквы, цифры, точка, дефис или подчёркивание')
        validate_password(password)
        salt = secrets.token_bytes(16)
        try:
            with closing(self.connect()) as db, db:
                db.execute("""INSERT INTO accounts(username,salt,digest,role,region,engineer_id)
                              VALUES(?,?,?,'engineer',?,?)""",
                           (username.lower(), salt, password_hash(password, salt), region, engineer_id))
        except sqlite3.IntegrityError:
            raise AuthError('Логин или учётная запись этого инженера уже существуют', 409) from None

    def update(self, username, *, password=None, enabled=None, allow_admin=False):
        if not isinstance(username,str): raise AuthError('Некорректный логин')
        if password is not None:
            validate_password(password)
        with self.lock, closing(self.connect()) as db, db:
            row = db.execute('SELECT * FROM accounts WHERE username=?', (username,)).fetchone()
            if row is None or (row['role'] == 'admin' and not allow_admin):
                raise AuthError('Выберите существующую учётную запись инженера')
            if password is not None:
                salt = secrets.token_bytes(16)
                db.execute('UPDATE accounts SET salt=?,digest=?,revision=revision+1 WHERE username=?',
                           (salt, password_hash(password, salt), username))
            if enabled is not None:
                if type(enabled) is not bool:
                    raise AuthError('enabled должен быть true или false')
                db.execute('UPDATE accounts SET enabled=?,revision=revision+1 WHERE username=?', (int(enabled), username))
            self.sessions = OrderedDict((k, v) for k, v in self.sessions.items() if v['username'] != username)

    def login(self, username, password, address):
        if not isinstance(username, str) or not isinstance(password, str) or len(username)>64 or len(password)>128:
            raise AuthError('Неверный логин или пароль', 401)
        now = self.clock()
        with self.lock:
            attempts = [t for t in self.attempts.get(address, []) if now-t < 60]
            if len(attempts) >= 10:
                raise AuthError('Слишком много попыток. Подождите минуту.', 429)
            self.attempts[address] = attempts + [now]
            self.attempts.move_to_end(address)
            while len(self.attempts)>1024: self.attempts.popitem(last=False)
        with closing(self.connect()) as db:
            row = db.execute('SELECT * FROM accounts WHERE username=?', (username.lower(),)).fetchone()
        digest = password_hash(password, row['salt'] if row else self.dummy_salt)
        if not row or not hmac.compare_digest(digest, row['digest']) or not row['enabled']:
            raise AuthError('Неверный логин или пароль', 401)
        token = secrets.token_urlsafe(32)
        session = {'username':row['username'], 'revision':row['revision'],
                   'expires':now+12*3600, 'csrf':secrets.token_urlsafe(32)}
        with self.lock:
            self.sessions[token] = session
            while len(self.sessions)>1024: self.sessions.popitem(last=False)
        return token, {**self.public(row), 'csrf':session['csrf']}

    def identify(self, token):
        with self.lock:
            session = self.sessions.get(token)
            if not session or session['expires'] <= self.clock():
                self.sessions.pop(token, None)
                return None
            with closing(self.connect()) as db:
                row = db.execute('SELECT * FROM accounts WHERE username=?', (session['username'],)).fetchone()
            if not row or not row['enabled'] or row['revision'] != session['revision']:
                self.sessions.pop(token, None)
                return None
            return {**self.public(row), 'csrf':session['csrf']}

    def logout(self, token):
        with self.lock: self.sessions.pop(token, None)


def main():
    import argparse
    import getpass
    parser = argparse.ArgumentParser(description='Сброс локального пароля диспетчера')
    parser.add_argument('file', type=Path, help='accounts.sqlite3 рядом с файлом состояния')
    args = parser.parse_args()
    if not args.file.is_file(): parser.error('База учётных записей не найдена')
    password = getpass.getpass('Новый пароль admin (12+ символов): ')
    if password != getpass.getpass('Повторите пароль: '): parser.error('Пароли не совпали')
    try: AccountStore(args.file).update('admin', password=password, allow_admin=True)
    except AuthError as exc: parser.error(str(exc))
    print('Пароль admin изменён. Старые сеансы недействительны.')


if __name__ == '__main__':
    main()
