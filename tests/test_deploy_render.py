"""Deploy guards: a failed build/push must never replace the live demo."""
import importlib.util
from pathlib import Path
import subprocess

import pytest


def module():
    spec = importlib.util.spec_from_file_location('deploy_render', Path(__file__).parents[1] / 'scripts/deploy_render.py')
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def environment():
    return {'DOCKERHUB_PUSH_TOKEN': 'private-test-token',
            'RENDER_DEPLOY_HOOK': 'https://api.render.com/deploy/srv-test?key=private-hook-key'}


@pytest.mark.parametrize('failure', ['build', 'push'])
def test_failed_image_never_triggers_deploy(failure):
    deploy = module()
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        if command[0] == 'git': return subprocess.CompletedProcess(command, 0, 'a' * 40 + '\n')
        if command[1] == failure: raise subprocess.CalledProcessError(1, command)
        return subprocess.CompletedProcess(command, 0)
    with pytest.raises(subprocess.CalledProcessError):
        deploy.publish(environment(), run=run, trigger=lambda url: pytest.fail('deployment after failure'))
    assert all('private-test-token' not in str(call) for call in calls)
    if failure == 'build':assert not any(command[1] == 'push' for command in calls)


def test_deploy_uses_exact_commit_and_keeps_credentials_out_of_arguments():
    deploy = module()
    calls, urls = [], []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        if command[0] == 'git': return subprocess.CompletedProcess(command, 0, 'b' * 40 + '\n')
        return subprocess.CompletedProcess(command, 0)
    deploy.publish(environment(), run=run, trigger=urls.append)
    assert len(urls) == 1 and 'b' * 40 in urls[0] and 'imgURL=' in urls[0]
    commands = [c for c, _ in calls]
    assert any(c[:2] == ['docker', 'push'] and c[-1].endswith('b' * 40) for c in commands)
    login = next((c, kw) for c, kw in calls if c[:2] == ['docker', 'login'])
    assert '--password-stdin' in login[0] and login[1]['input'] == 'private-test-token'
    assert all('private-test-token' not in str(c) for c in commands)
    config = Path(login[1]['env']['DOCKER_CONFIG'])
    assert not config.exists()


@pytest.mark.parametrize('hook', ['http://api.render.com/deploy/srv-test?key=x',
                                 'https://evil.example/deploy/srv-test?key=x',
                                 'https://api.render.com/deploy/srv-test?key=x#fragment'])
def test_bad_hook_fails_before_upload(hook):
    deploy = module()
    with pytest.raises(ValueError, match='RENDER_DEPLOY_HOOK'):
        deploy.publish({**environment(), 'RENDER_DEPLOY_HOOK': hook},
                       run=lambda *a, **k: pytest.fail('command before validation'))


def test_missing_secrets_fail_without_building():
    deploy = module()
    with pytest.raises(ValueError, match='DOCKERHUB_PUSH_TOKEN'):
        deploy.publish({}, run=lambda *a, **k: pytest.fail('command before validation'))


def test_hook_error_reports_status_but_never_secret(monkeypatch):
    from urllib.error import HTTPError
    deploy = module()
    secret_url = environment()['RENDER_DEPLOY_HOOK']
    class Opener:
        def open(self, *args, **kwargs):
            raise HTTPError(secret_url, 401, 'contains private-hook-key', {}, None)
    monkeypatch.setattr(deploy, 'build_opener', lambda *args: Opener())
    with pytest.raises(RuntimeError, match='HTTP 401') as error:
        deploy.trigger_deploy(secret_url)
    assert 'private-hook-key' not in str(error.value)


def test_dockerhub_mode_pushes_demo_once_without_render_access():
    deploy = module()
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, 'c' * 40 + '\n')
    deploy.publish({'DOCKERHUB_PUSH_TOKEN': 'test-token', 'RENDER_TRIGGER': 'dockerhub'},
                   run=run, trigger=lambda url: pytest.fail('CI must not call Render'))
    pushes = [c for c in calls if c[:2] == ['docker', 'push']]
    assert pushes == [['docker', 'push', deploy.IMAGE + ':demo']]
    build = next(c for c in calls if c[:2] == ['docker', 'build'])
    assert 'org.opencontainers.image.revision=' + 'c' * 40 in build


def test_unknown_trigger_fails_before_build():
    deploy = module()
    with pytest.raises(ValueError, match='RENDER_TRIGGER'):
        deploy.publish({**environment(), 'RENDER_TRIGGER': 'typo'},
                       run=lambda *a, **k: pytest.fail('command before validation'))
