"""APK response metadata and single byte ranges; caller owns authentication."""
import hashlib
import re


def apk_response(path, range_header=None, if_range=None):
    data = path.read_bytes()
    size = len(data)
    etag = '"' + hashlib.sha256(data).hexdigest() + '"'
    headers = {'Content-Disposition': 'attachment; filename="field-engineer-debug.apk"',
               'Accept-Ranges': 'bytes', 'ETag': etag}
    if not range_header or (if_range and if_range != etag):
        return 200, data, headers
    match = re.fullmatch(r'bytes=(\d*)-(\d*)', range_header.strip()) if len(range_header) <= 200 else None
    try:
        if not match or not any(match.groups()) or not size:
            raise ValueError()
        first, last = match.groups()
        if first:
            start = int(first)
            end = min(int(last), size-1) if last else size-1
        else:
            count = int(last)
            if count < 1:
                raise ValueError()
            start, end = max(0, size-count), size-1
        if not 0 <= start <= end < size:
            raise ValueError()
    except ValueError:
        return 416, b'', {**headers, 'Content-Range': f'bytes */{size}'}
    headers['Content-Range'] = f'bytes {start}-{end}/{size}'
    return 206, data[start:end+1], headers
