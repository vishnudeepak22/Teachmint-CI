#!/usr/bin/env python3
"""Control the existing Teachmint CI services on the GCP VM."""
import argparse
import base64
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

ROOT = Path('/home/uat/Teachmint-CI')
STATE = ROOT / 'state/github-ci-stop.json'
UNIT = 'teachmint-ci-scheduled-stop.service'
TOKEN = Path('/home/uat/.jenkins-agent/jenkins-api-token')
JENKINS = 'http://127.0.0.1:18082'
IST = ZoneInfo('Asia/Kolkata')
CONTAINERS = ['teachmint-artifactory-db', 'teachmint-artifactory',
              'mt83x1-gerrit-migrated', 'teachmint-jenkins-server']
HEALTH = ['http://10.201.0.119:8085/config/server/version',
          'http://10.201.0.119:8082/login', 'http://10.201.0.119:8083/ui/login/']


def command(*args, check=True):
    return subprocess.run(args, check=check, capture_output=True, text=True)


def container_running(name):
    result = command('docker', 'inspect', '--format', '{{.State.Running}}', name)
    return result.stdout.strip() == 'true'


def jenkins(path, post=False):
    token = TOKEN.read_text().strip()
    authorization = base64.b64encode(('vishnu:' + token).encode()).decode()
    request = urllib.request.Request(JENKINS + path,
        headers={'Authorization': 'Basic ' + authorization},
        data=b'' if post else None, method='POST' if post else 'GET')
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read()
        return json.loads(body) if body and not post else None


def running_jobs(path=''):
    query = '/api/json?tree=jobs[name,url,_class,builds[number,building]]'
    document = jenkins(path + query)
    running = []
    for job in document.get('jobs', []):
        for build in job.get('builds', []):
            if build.get('building'):
                running.append(job['name'] + ' #' + str(build['number']))
        if job.get('_class', '').endswith('Folder'):
            child_path = urllib.parse.urlsplit(job['url']).path.rstrip('/')
            running.extend(running_jobs(child_path))
    return running


def manual_builds():
    tools = {'make', 'ninja', 'bazel', 'bazelisk', 'soong_ui', 'ckati'}
    scripts = {'split_build.sh', 'run-green-build.py', 'server-build.py',
               'validate-freedom-build.py', 'publish-firmware.py',
               'publish-validated-firmware.py', 'ota_from_target_files'}
    found = []
    for process in Path('/proc').iterdir():
        if not process.name.isdigit() or int(process.name) == os.getpid():
            continue
        try:
            args = (process / 'cmdline').read_bytes().split(b'\0')
            args = [os.fsdecode(arg) for arg in args if arg]
            executable = Path(os.readlink(process / 'exe')).name
        except (OSError, ValueError):
            continue
        if executable in tools or any(Path(arg).name in scripts for arg in args):
            found.append(process.name + ': ' + executable)
    return found


def next_opening(now):
    candidate = now.astimezone(IST).replace(hour=9, minute=0, second=0, microsecond=0)
    if candidate <= now:
        candidate += dt.timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += dt.timedelta(days=1)
    return candidate


def wait_http(url, deadline):
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(10)
    raise RuntimeError('Service did not become ready: ' + url)


def cancel_drain():
    if STATE.exists():
        STATE.unlink()
    if container_running('teachmint-jenkins-server'):
        jenkins('/cancelQuietDown', post=True)


def start():
    command('systemctl', 'stop', UNIT, check=False)
    command('systemctl', 'start', 'docker.service')
    for name in CONTAINERS:
        command('docker', 'start', name)
    if command('systemctl', 'cat', 'nginx.service', check=False).returncode == 0:
        command('systemctl', 'start', 'nginx.service')
    deadline = time.monotonic() + 600
    wait_http(HEALTH[1], deadline)
    cancel_drain()
    command('systemctl', 'start', 'teachmint-server-jenkins-agent.service')
    command('systemctl', 'is-active', '--quiet', 'teachmint-server-jenkins-agent.service')
    for url in HEALTH:
        wait_http(url, deadline)
    print(json.dumps({'status': 'ready', 'services': ['Gerrit', 'Jenkins', 'Artifactory']}))


def request_stop():
    # Token authentication is already used by the server's Gerrit dispatcher.
    if container_running('teachmint-jenkins-server'):
        jenkins('/quietDown', post=True)
    if command('systemctl', 'is-active', '--quiet', UNIT, check=False).returncode == 0:
        print(json.dumps({'status': 'shutdown already pending'}))
        return
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({'requested_at': dt.datetime.now(IST).isoformat(),
                                'cancel_at': next_opening(dt.datetime.now(IST)).isoformat()}))
    os.chmod(STATE, 0o600)
    try:
        command('systemd-run', '--unit=' + UNIT, '--collect',
                '/usr/bin/python3', str(Path(__file__).resolve()), 'wait-stop')
    except Exception:
        cancel_drain()
        raise
    print(json.dumps({'status': 'shutdown requested; waiting for active builds',
                      'log': 'journalctl -u ' + UNIT}))


def wait_stop():
    while STATE.exists():
        state = json.loads(STATE.read_text())
        if dt.datetime.now(IST) >= dt.datetime.fromisoformat(state['cancel_at']):
            cancel_drain()
            print('Cancelled pending shutdown at the next working-day opening.', flush=True)
            return
        try:
            jobs = running_jobs() if container_running('teachmint-jenkins-server') else []
            processes = manual_builds()
        except Exception as error:
            print('Unable to confirm idle state: ' + str(error), flush=True)
            time.sleep(30)
            continue
        if jobs or processes:
            print(json.dumps({'waiting_for_jobs': jobs, 'waiting_for_processes': processes}), flush=True)
            time.sleep(30)
            continue
        # Quiet-down prevents Jenkins from starting queued work during shutdown.
        try:
            command('systemctl', 'stop', 'teachmint-server-jenkins-agent.service')
            for name in reversed(CONTAINERS):
                if container_running(name):
                    command('docker', 'stop', '--time', '120', name)
            print('Services stopped; powering off the VM.', flush=True)
            command('systemctl', 'poweroff')
            STATE.unlink(missing_ok=True)
            return
        except subprocess.CalledProcessError as error:
            print('Shutdown step failed; retrying: ' + str(error), flush=True)
            time.sleep(30)


def status():
    report = {name: container_running(name) for name in CONTAINERS}
    report['shutdown_pending'] = STATE.exists()
    report['agent_active'] = command('systemctl', 'is-active', '--quiet',
        'teachmint-server-jenkins-agent.service', check=False).returncode == 0
    if report['teachmint-jenkins-server']:
        report['running_builds'] = running_jobs()
    print(json.dumps(report, indent=2))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['start', 'stop', 'status', 'wait-stop'])
    action = parser.parse_args().action
    if os.geteuid() != 0:
        raise SystemExit('Run this helper through sudo.')
    {'start': start, 'stop': request_stop, 'status': status, 'wait-stop': wait_stop}[action]()


if __name__ == '__main__':
    main()
