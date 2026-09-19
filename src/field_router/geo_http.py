"""Sequential HTTP with persistent response cache; no retries or hidden fallbacks."""
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError


class CachedHttp:
    def __init__(self, directory, *, offline=False, fetch=None, interval=1.1):
        self.directory=Path(directory)
        self.directory.mkdir(parents=True,exist_ok=True)
        self.offline=offline
        self.fetch=fetch or self._fetch
        self.interval=max(1.1,interval) if fetch is None else interval
        self.last_request=0.

    def _fetch(self,url):
        request=Request(url,headers={'User-Agent':'FieldRouter-Hackathon-Team40/0.2 (local research demo)', 'Accept':'application/json'})
        try:
            with urlopen(request,timeout=40) as response:
                return json.load(response)
        except (URLError,TimeoutError) as exc:
            raise ValueError(f'Внешний сервис недоступен; повторите позже или используйте кэш: {exc}') from exc

    def get(self,url):
        key=hashlib.sha256(url.encode()).hexdigest()
        path=self.directory/f'{key}.json'
        if path.exists():
            cached=json.loads(path.read_text())
            if cached['url']!=url: raise ValueError('Несовпадение ключа кэша')
            return cached['response']
        if self.offline: raise ValueError('Ответ отсутствует в кэше; сетевой режим выключен')
        delay=self.interval-(time.monotonic()-self.last_request)
        if delay>0: time.sleep(delay)
        self.last_request=time.monotonic()
        value=self.fetch(url)
        record={'url':url,'fetched_at':datetime.now(timezone.utc).isoformat(),'response':value}
        temp=path.with_suffix('.tmp')
        temp.write_text(json.dumps(record,ensure_ascii=False),encoding='utf-8')
        temp.replace(path)
        return value
