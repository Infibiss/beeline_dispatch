"""Build a tested main commit, push a private image, then request a Render deploy.

Run only in the release workflow after checks. Secrets come from SourceCraft,
not command arguments or the Docker build context. HTTP success means queued,
not that Render has finished deploying.
"""
import os
import re
import subprocess
from tempfile import TemporaryDirectory
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

IMAGE = 'docker.io/infibiss/field-router-demo'


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def trigger_deploy(url):
    try:
        with build_opener(NoRedirect()).open(Request(url, data=b'', method='POST'), timeout=30) as response:
            if not 200 <= response.status < 300:
                raise RuntimeError('Render rejected the deployment request')
    except HTTPError as exc:
        raise RuntimeError(f'Render deploy hook failed: HTTP {exc.code}; check the service dashboard') from None
    except URLError as exc:
        raise RuntimeError(f'Render deploy hook connection failed: {type(exc.reason).__name__}') from None
    except Exception as exc:
        # urllib errors can contain the secret hook URL; never print it.
        raise RuntimeError(f'Render deploy hook failed: {type(exc).__name__}; check the service dashboard') from None


def publish(env=None, *, run=subprocess.run, trigger=trigger_deploy):
    env = dict(os.environ if env is None else env)
    token = env.get('DOCKERHUB_PUSH_TOKEN', '')
    if not token:
        raise ValueError('Set SourceCraft secret DOCKERHUB_PUSH_TOKEN')
    hook = urlsplit(env.get('RENDER_DEPLOY_HOOK', ''))
    query = parse_qsl(hook.query, keep_blank_values=True)
    if (hook.scheme != 'https' or hook.netloc != 'api.render.com' or hook.fragment or
            not re.fullmatch(r'/deploy/srv-[a-zA-Z0-9]+', hook.path) or
            len(query) != 1 or query[0][0] != 'key' or not query[0][1]):
        raise ValueError('Set a valid RENDER_DEPLOY_HOOK from the Render service settings')
    revision = run(['git', 'rev-parse', 'HEAD'], check=True, capture_output=True, text=True).stdout.strip()
    if not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise ValueError('Expected full Git commit SHA')
    image = IMAGE + ':' + revision
    with TemporaryDirectory(prefix='field-router-docker-auth-') as config:
        docker_env = {**env, 'DOCKER_CONFIG': config}
        run(['docker', 'build', '--platform', 'linux/amd64', '-t', image, '.'],
            check=True, env=docker_env)
        run(['docker', 'login', 'docker.io', '-u', 'infibiss', '--password-stdin'],
            input=token, text=True, check=True, capture_output=True, env=docker_env)
        run(['docker', 'push', image], check=True, env=docker_env)
    url = urlunsplit((hook.scheme, hook.netloc, hook.path,
                     urlencode(query + [('imgURL', image)]), ''))
    trigger(url)
    print('Render deployment queued for ' + revision + '; verify completion in Render.')


if __name__ == '__main__':
    publish()
