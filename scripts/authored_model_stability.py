"""Sustained authored long-context workload; never reads evaluation populations.

Run only in a coordinator-admitted exclusive model window under product_lock.
The pinned live server is paused on any failure and after a successful run.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import psutil

ROOT = Path(__file__).resolve().parents[1]
PB = ROOT.parent
PURPOSE = 'authored-model-stability'
sys.path.insert(0, str(PB / 'tools'))
import product_lock  # noqa: E402


def resources():
    value = json.loads(subprocess.check_output(
        ['powershell', '-NoProfile', '-Command',
         'Get-CimInstance Win32_PerfFormattedData_PerfOS_Memory | '
         'Select-Object CommittedBytes,CommitLimit | ConvertTo-Json -Compress'],
        text=True, timeout=12))
    limit = int(value['CommitLimit'])
    if limit <= 0:
        raise ValueError('COMMIT_COUNTER_UNAVAILABLE')
    result = {'at': time.time(), 'c_free_bytes': shutil.disk_usage('C:/').free,
              'available_ram_bytes': psutil.virtual_memory().available,
              'committed_bytes': int(value['CommittedBytes']), 'commit_limit_bytes': limit}
    result['admitted'] = (result['c_free_bytes'] >= 50 * 1024**3
                          and result['available_ram_bytes'] >= 6 * 1024**3
                          and result['committed_bytes'] / limit < .85)
    return result


def assert_lock():
    owner = product_lock.read_owner(PB / '.cache/product-build.lock') or {}
    ancestors = {(p.pid, p.create_time()) for p in psutil.Process().parents()}
    if ((owner.get('product'), owner.get('purpose')) != ('folio', PURPOSE)
            or (owner.get('pid'), owner.get('create_time')) not in ancestors):
        raise ValueError('ANCESTOR_MODEL_LOCK_REQUIRED')


def live_profile():
    pid = int(subprocess.check_output(
        ['powershell', '-NoProfile', '-Command',
         '(Get-NetTCPConnection -State Listen -LocalPort 18301 -ErrorAction Stop | '
         'Select-Object -First 1).OwningProcess'], text=True, timeout=12).strip())
    proc = psutil.Process(pid)
    argv = proc.cmdline()
    spec = {'--host': '127.0.0.1', '--port': '18301', '--ctx-size': '8192',
            '--parallel': '1', '--n-gpu-layers': '99', '--cache-type-k': 'q8_0',
            '--cache-type-v': 'q8_0', '--cache-ram': '0', '--load-mode': 'mmap',
            '--batch-size': '512', '--ubatch-size': '128'}
    for flag, value in spec.items():
        if argv.count(flag) != 1 or argv[argv.index(flag) + 1] != value:
            raise ValueError('LIVE_MODEL_PROFILE_MISMATCH')
    from model_serve import ENGINE, SHA, WEIGHTS
    if Path(proc.exe()).resolve() != ENGINE.resolve() or Path(argv[argv.index('--model') + 1]).resolve() != WEIGHTS.resolve():
        raise ValueError('LIVE_MODEL_IDENTITY_MISMATCH')
    state = json.loads((ROOT / '.local/live-round4/state.json').read_text(encoding='utf-8'))
    component = state['components']['model']
    lineage = {(p.pid, p.create_time()) for p in [proc, *proc.parents()]}
    if (component.get('pid'), component.get('created')) not in lineage:
        raise ValueError('SUPERVISOR_MODEL_TARGET_MISMATCH')
    with WEIGHTS.open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != SHA:
            raise ValueError('MODEL_WEIGHT_MISMATCH')
    return {'pid': pid, 'created': proc.create_time(), 'supervisor_model_pid': component['pid'],
            'supervisor_model_created': component['created'], 'profile': spec, 'weights_sha256': SHA}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--seconds', type=int, default=900)
    args = ap.parse_args()
    if not 900 <= args.seconds <= 3600:
        ap.error('stability duration must be between 900 and3600 seconds')
    if args.out.drive.upper() != 'C:':
        ap.error('output must be on C:')
    args.out.resolve().relative_to((ROOT / '.local').resolve())
    assert_lock()
    output_created = False
    stop = threading.Event()
    responses = []
    errors = []
    started = None
    profile = None
    worker = None
    client = None
    status = 'FAIL'
    final_error = None
    # Literal authored content only. Tokenize it to keep the request bounded
    # while exercising substantially more context than a short health prompt.
    line = 'Authored invoice INV-STABILITY: 2 each at USD10.00; total USD20.00; supplier Example Paper Co.\n'
    try:
        args.out.mkdir(parents=True, exist_ok=False)
        output_created = True
        if not resources()['admitted']:
            raise ValueError('HOST_RESOURCE_ADMISSION_DENIED')
        profile = live_profile()
        client = httpx.Client(base_url='http://127.0.0.1:18301', timeout=180)
        count = 220
        for _ in range(8):
            prompt = line * count + '\nReply with the word organized.'
            tokenized = client.post('/tokenize', json={'content': prompt, 'add_special': False})
            tokenized.raise_for_status()
            tokens = len(tokenized.json()['tokens'])
            if 4500 <= tokens <= 6500:
                break
            if tokens < 1:
                raise ValueError('AUTHORED_TOKEN_BOUNDS')
            count = min(500, max(20, int(count * 5500 / tokens)))
        else:
            raise ValueError('AUTHORED_TOKEN_BOUNDS')
        started = time.monotonic()
        deadline = started + args.seconds

        def generate():
            try:
                with (args.out / 'responses.jsonl').open('x', encoding='utf-8') as log:
                    while not stop.is_set() and time.monotonic() < deadline:
                        tick = time.monotonic()
                        response = client.post('/v1/chat/completions', json={
                            'model': 'folio-local', 'messages': [{'role': 'user', 'content': prompt}],
                            'temperature': 0, 'max_tokens': 128, 'cache_prompt': False,
                            'chat_template_kwargs': {'enable_thinking': False}})
                        response.raise_for_status()
                        value = response.json()
                        content = value['choices'][0]['message'].get('content')
                        if not isinstance(content, str) or not content.strip():
                            raise ValueError('AUTHORED_RESPONSE_EMPTY')
                        record = {'elapsed_s': time.monotonic() - tick,
                                  'chars': len(content), 'input_tokens': tokens}
                        responses.append(record)
                        log.write(json.dumps(record) + '\n')
                        log.flush()
                        stop.wait(1)
            except Exception as exc:
                errors.append(type(exc).__name__)
                stop.set()
        worker = threading.Thread(target=generate, daemon=True)
        worker.start()
        with (args.out / 'resources.jsonl').open('x', encoding='utf-8') as log:
            while True:
                snapshot = resources()
                log.write(json.dumps(snapshot) + '\n')
                log.flush()
                if not snapshot['admitted']:
                    raise ValueError('HOST_RESOURCE_GATE')
                if errors:
                    raise ValueError('AUTHORED_REQUEST_FAILED')
                if time.monotonic() >= deadline:
                    # Stop new requests, then let the already-issued request
                    # finish while continuing host monitoring.
                    stop.set()
                    if not worker.is_alive():
                        break
                    if time.monotonic() > deadline + 190:
                        raise ValueError('AUTHORED_REQUEST_NOT_EXITED')
                    worker.join(timeout=1)
                else:
                    stop.wait(min(5, max(0, deadline - time.monotonic())))
        if errors or not responses:
            raise ValueError('AUTHORED_STABILITY_INCOMPLETE')
        status = 'PASS'
    except BaseException as exc:
        final_error = type(exc).__name__
    finally:
        stop.set()
        # Even profile/tokenizer/setup failures must pause the owned server.
        try:
            paused = subprocess.run([sys.executable, '-B', str(ROOT / 'scripts/live_supervisor.py'),
                                     'pause-model', '--lock-owner', PURPOSE], cwd=ROOT,
                                    capture_output=True, timeout=35)
            pause_exit = paused.returncode
        except (OSError, subprocess.TimeoutExpired):
            pause_exit = -1
        if pause_exit:
            status = 'FAIL'
            final_error = 'MODEL_PAUSE_FAILED'
        if worker is not None:
            worker.join(timeout=15)
            if worker.is_alive():
                status = 'FAIL'
                final_error = 'AUTHORED_REQUEST_NOT_EXITED'
        if client is not None:
            client.close()
    result = {'status': status, 'error_class': final_error, 'request_errors': errors,
              'elapsed_s': time.monotonic() - started if started is not None else 0,
              'required_s': args.seconds,
              'model': profile, 'responses': responses,
              'source_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'heldout_accessed': False, 'model_pause_exit_code': pause_exit}
    if output_created:
        (args.out / 'result.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'status': status, 'responses': len(responses),
                      'error_class': final_error,
                      'result': str(args.out / 'result.json') if output_created else None}))
    return 0 if status == 'PASS' else 3


if __name__ == '__main__':
    sys.exit(main())
