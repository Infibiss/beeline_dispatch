"""Public brand assets must work before login without exposing private files."""
import hashlib
import http.client

from test_auth import Client, secured


def test_login_brand_assets_are_public_and_identical_on_regional_urls(secured):
    server, _, _ = secured
    for path, mime in (
        ('/assets/beeline-logo.png', 'image/png'),
        ('/assets/beeline-symbol.svg', 'image/svg+xml'),
        ('/brand.css', 'text/css'),
    ):
        payloads = []
        for prefix in ('', '/r/east', '/regions/east'):
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=10)
            connection.request('GET', prefix+path)
            response = connection.getresponse()
            assert response.status == 200
            assert response.getheader('Content-Type').startswith(mime)
            payloads.append(response.read())
            connection.close()
        assert payloads[0] == payloads[1] == payloads[2]
        if mime == 'image/png':
            assert hashlib.sha256(payloads[0]).hexdigest() == 'b97e2270326116a2481f0854d096eddea82c4319ddae5ab600f8fe7119d8d29e'
    anonymous = Client(server)
    assert anonymous.request('/api/state')[0] == 401
    assert anonymous.request('/assets/../accounts.sqlite3')[0] != 200
    assert anonymous.request('/assets/SOURCE.txt')[0] != 200
    assert anonymous.request('/brand.css', {}, method='POST')[0] != 200
    employee = Client(server)
    assert employee.login('worker1')[0] == 200
    assert employee.request('/api/state')[0] == 403
    assert employee.request('/assets/beeline-logo.png/../accounts.sqlite3')[0] == 404
