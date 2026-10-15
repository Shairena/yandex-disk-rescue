"""Read/download-only Yandex REST transport. Never logs URLs, headers or tokens."""
import json
import urllib.request
import urllib.error
import urllib.parse
import socket

BASE = 'https://cloud-api.yandex.net/v1/disk'


class Failure(Exception):
    def __init__(self, category, code=None, retry_after=0):
        self.category, self.code = category, code
        self.retry_after = min(3600, max(0, retry_after))
        super().__init__(category + (f' (HTTP {code})' if code else ''))


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not trusted_url(newurl):
            raise Failure('untrusted_download_host')
        # Credentials must never follow redirects, even to a trusted CDN.
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected:
            redirected.remove_header('Authorization')
        return redirected


def trusted_url(url):
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or '').lower()
    return (parsed.scheme == 'https' and not parsed.username and not parsed.password
            and parsed.port in (None, 443) and any(host == d or host.endswith('.' + d)
                for d in ('yandex.net', 'yandex.ru', 'yandex.com', 'yandexcloud.net')))


class DiskAPI:
    def __init__(self, token, timeout=30):
        if not token.strip() or any(c in token for c in '\r\n'):
            raise ValueError('Нужен OAuth-токен, введённый в приложении.')
        self.token = token.strip()
        self.timeout = timeout
        self.opener = urllib.request.build_opener(SafeRedirect())

    def _open(self, url, authenticated=False):
        if not trusted_url(url):
            raise Failure('untrusted_download_host')
        headers = {'User-Agent': 'YandexBackup/0.1', 'Accept-Encoding': 'identity'}
        if authenticated:
            if urllib.parse.urlsplit(url).hostname != 'cloud-api.yandex.net':
                raise Failure('invalid_api_host')
            headers['Authorization'] = 'OAuth ' + self.token
        try:
            return self.opener.open(urllib.request.Request(url, headers=headers, method='GET'), timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            code = exc.code
            try:
                delay = float(exc.headers.get('Retry-After', 0))
            except (ValueError, TypeError):
                delay = 0
            exc.close()
            category = ('auth' if code in (401, 403) else 'rate_limit' if code == 429
                        else 'service_unavailable' if code in (404, 410) else 'http_error')
            raise Failure(category, code, delay) from None
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError):
            raise Failure('transport') from None

    def get(self, endpoint='', **params):
        if endpoint not in ('', '/resources', '/resources/download'):
            raise ValueError('Read-only endpoint not allowed')
        with self._open(BASE + endpoint + '?' + urllib.parse.urlencode(params), True) as response:
            raw = response.read(16 * 1024 * 1024 + 1)
        if len(raw) > 16 * 1024 * 1024:
            raise Failure('invalid_api_response')
        try:
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except (ValueError, UnicodeError):
            raise Failure('invalid_api_response') from None

    def identity(self):
        result = self.get()
        uid = result.get('user', {}).get('uid')
        if not uid:
            raise Failure('missing_account_identity')
        return str(uid)

    def metadata(self, path, offset=0, limit=200):
        return self.get('/resources', path=path, offset=offset, limit=limit, sort='path')

    def download(self, path):
        link = self.get('/resources/download', path=path)
        if link.get('method', 'GET') != 'GET' or not isinstance(link.get('href'), str):
            raise Failure('invalid_download_link')
        return self._open(link['href'])

