#!/usr/bin/env python3
"""Offline runner-level concurrency, cap, pacing and resume regressions.
Only local loopback sockets and dummy credentials; no external service calls.
"""
import collections
import fcntl
import concurrent.futures
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import runner_v2 as R
from parallel_runner import Pacer

REPO = Path(__file__).resolve().parent
TOKEN = 'dummy-parallel-test-credential-0123456789abcdef'
PAIR = json.dumps({'instruction': 'Write a short haiku about rain.',
                   'response': 'Soft rain on the window, drops tracing their quiet paths.'})
MODEL = 'meta.llama-4-maverick-17b-128e-instruct-fp8'


class Endpoint:
    def __init__(self, responses=None, delay=.10):
        self.lock = threading.Lock()
        self.active = self.peak = 0
        self.starts = []
        self.reservations = []
        self.ledger_path = None
        self.responses = collections.deque(responses or [])
        self.delay = delay
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if owner.ledger_path:
                    ledger = R.Ledger(owner.ledger_path)
                    with open(ledger._lockfile(), 'a') as lock:
                        fcntl.flock(lock, fcntl.LOCK_EX)
                        owner.reservations.append((ledger.spent_chars(), ledger.attempts()))
                        fcntl.flock(lock, fcntl.LOCK_UN)
                with owner.lock:
                    owner.starts.append((time.monotonic(), body))
                    owner.active += 1
                    owner.peak = max(owner.peak, owner.active)
                    status = owner.responses.popleft() if owner.responses else 200
                try:
                    time.sleep(owner.delay)
                    if status == 'bad':
                        self.wfile.write(b'BAD ' + TOKEN[:28].encode() + b'\r\n\r\n')
                        self.close_connection = True
                        return
                    reply = {'choices': [{'message': {'content': PAIR}, 'finish_reason': 'stop'}],
                             'usage': {'prompt_tokens': 8, 'completion_tokens': 20, 'total_tokens': 28}}
                    if status == 'invalid':
                        reply['choices'][0]['message']['content'] = 'plain prose'
                        status = 200
                    if status == 'echo':
                        reply['choices'][0]['message']['content'] = PAIR + TOKEN
                        status = 200
                    data = json.dumps(reply if status == 200 else {'error': TOKEN}).encode()
                    self.send_response(status)
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    with owner.lock:
                        owner.active -= 1
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_address[1]}/openai/v1'
    def close(self):
        self.server.shutdown()
        self.server.server_close()


class ParallelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.endpoint = Endpoint()
        self.addCleanup(self.endpoint.close)
        self.cfg = dict(auth='bearer', token_env='OCI_PARALLEL_TEST_KEY',
                        region='us-ashburn-1', api_base_url=self.endpoint.base,
                        models=[dict(model_id=MODEL, weight=1)],
                        budget_usd=5, price_per_million_chars=.18,
                        attempt_char_cap=20000, max_tokens=128, temperature=.7,
                        max_attempts=100, max_retries=0, workers=3,
                        min_request_interval_seconds=.025,
                        require_rendered_prompt=True, pairs_per_seed=1,
                        seeds_path=str(self.root/'seeds.jsonl'),
                        out_path=str(self.root/'pairs.jsonl'),
                        ledger_path=str(self.root/'ledger.jsonl'))
        self.seeds = [dict(id=f's{i}', prompt=f'Please create a good rain haiku {i}.',
                           execution_status='eligible') for i in range(8)]
    def write(self):
        Path(self.cfg['seeds_path']).write_text(''.join(json.dumps(s)+'\n' for s in self.seeds))
        p = self.root/'config.json'
        p.write_text(json.dumps(self.cfg))
        return p
    def cmd(self, *extra):
        return [sys.executable, str(REPO/'runner_v2.py'), '--config', str(self.write()), *extra]
    def env(self):
        return dict(os.environ, OCI_PARALLEL_TEST_KEY=TOKEN)
    def run_cli(self, *extra):
        r = subprocess.run(self.cmd(*extra), capture_output=True, text=True,
                           env=self.env(), timeout=30)
        return r
    def rows(self, key='ledger_path'):
        p = Path(self.cfg[key])
        return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []
    def effective(self):
        return list({r['attempt_id']: r for r in self.rows()}.values())
    def assert_clean(self, r):
        blob = r.stdout+r.stderr
        for k in ('ledger_path', 'out_path'):
            p = Path(self.cfg[k])
            if p.exists():
                blob += p.read_text()
        self.assertNotIn(TOKEN, blob)
        self.assertNotIn(TOKEN[:28], blob)

    def test_parallel_overlap_and_shared_pacing(self):
        r = self.run_cli()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertGreaterEqual(self.endpoint.peak, 2)
        self.assertLessEqual(self.endpoint.peak, 3)
        times = [t for t, _ in self.endpoint.starts]
        self.assertTrue(all(b-a >= .018 for a,b in zip(times,times[1:])), times)
        self.assertEqual(len(self.rows('out_path')), 8)
        self.assertEqual(len({r['id'] for r in self.rows('out_path')}), 8)
        self.assert_clean(r)

    def test_global_count_cap_with_inflight(self):
        self.cfg['max_attempts'] = 2
        r = self.run_cli()
        self.assertEqual(len(self.endpoint.starts), 2)
        self.assertEqual(len(self.effective()), 2)
        self.assert_clean(r)

    def test_global_estimated_char_cap_with_inflight(self):
        # Enough for two in-flight reservations, not three. Completed calls
        # settle lower, so the run should resume admitting jobs as holds clear.
        one = len(self.seeds[0]['prompt'].encode())+200+113*128
        allowance = 2*one+1
        self.cfg['budget_usd'] = allowance/1e6*.18
        self.endpoint.ledger_path = self.cfg['ledger_path']
        r = self.run_cli()
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual(len(self.endpoint.starts),8,r.stdout+r.stderr)
        self.assertTrue(self.endpoint.reservations)
        self.assertTrue(all(chars<=allowance for chars,_ in self.endpoint.reservations),
                        self.endpoint.reservations)
        self.assertLessEqual(self.endpoint.peak,2)
        self.assert_clean(r)

    def test_pair_limit_includes_inflight(self):
        r = self.run_cli('--limit', '2')
        self.assertEqual(r.returncode, 0)
        self.assertEqual(len(self.endpoint.starts), 2)
        self.assertEqual(len(self.rows('out_path')), 2)

    def test_429_retry_shared_bound(self):
        self.endpoint.responses = collections.deque([429]*20)
        self.endpoint.delay = .01
        self.cfg.update(max_attempts=4, max_retries=5)
        r = self.run_cli()
        self.assertEqual(r.returncode, 4)
        self.assertEqual(len(self.endpoint.starts), 4)
        self.assertEqual(len(self.effective()), 4)
        self.assertTrue(all(a['status']=='throttled' for a in self.effective()))
        self.assert_clean(r)

    def test_429_then_success(self):
        self.endpoint.responses = collections.deque([429,200])
        self.seeds = self.seeds[:1]
        self.cfg.update(max_retries=1, max_attempts=2)
        r = self.run_cli()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([a['status'] for a in self.effective()], ['throttled','completed'])
        self.assertEqual(len(self.rows('out_path')), 1)

    def test_resume_never_rebills_completed(self):
        r = self.run_cli()
        self.assertEqual(r.returncode, 0)
        hits = len(self.endpoint.starts)
        r = self.run_cli()
        self.assertEqual(len(self.endpoint.starts), hits)
        self.assertEqual(len(self.rows('out_path')), hits)

    def test_pending_unknown_timeout_remain_held(self):
        self.seeds = self.seeds[:4]
        self.write()
        rows = []
        for i, status in enumerate(('pending','unknown','timeout','completed')):
            a = R.Attempt('old', f's{i}', MODEL)
            a.pair_key = f's{i}#1'; a.status = status
            a.input_chars=200; a.output_chars=14464
            rows.append(a.row())
        Path(self.cfg['ledger_path']).write_text(''.join(json.dumps(r)+'\n' for r in rows))
        r = self.run_cli()
        self.assertEqual(len(self.endpoint.starts), 0)
        self.assertEqual(len(self.effective()), 4)
        self.assert_clean(r)

    def test_language_and_execution_holds(self):
        self.seeds[0]['execution_status']='pending_user_approval'
        self.seeds[1]['support']={'llama4_documented':False}
        self.seeds[2]['off_list_source_language']=True
        self.seeds[3]['llama4_pair_fully_documented']=False
        r = self.run_cli()
        self.assertEqual(r.returncode, 0)
        self.assertEqual(len(self.endpoint.starts), 4)

    def test_unparseable_and_secret_echo_closed(self):
        self.endpoint.responses = collections.deque(['invalid','echo','bad'])
        self.seeds=self.seeds[:3]
        r = self.run_cli()
        self.assertEqual(r.returncode, 4)
        self.assertEqual(len(self.rows('out_path')), 0)
        self.assertEqual(sorted(a['status'] for a in self.effective()),
                         ['unknown','unknown','unparseable'])
        self.assert_clean(r)

    def test_workers_cli_cannot_widen(self):
        r = self.run_cli('--workers','4')
        self.assertNotEqual(r.returncode,0)
        self.assertEqual(len(self.endpoint.starts),0)

    def test_workers_tightened_to_one_keeps_pacing(self):
        r = self.run_cli('--workers','1')
        self.assertEqual(r.returncode,0)
        self.assertEqual(self.endpoint.peak,1)
        self.assertEqual(len(self.endpoint.starts),8)

    def test_duplicate_seed_cannot_duplicate_pair(self):
        self.seeds = self.seeds[:1]*8
        r = self.run_cli()
        self.assertEqual(r.returncode,0)
        self.assertEqual(len(self.endpoint.starts),1)
        self.assertEqual(len(self.rows('out_path')),1)

    def test_invalid_config_no_calls(self):
        for key, value in [('workers',True), ('workers',0), ('workers',17),
                           ('min_request_interval_seconds',0),
                           ('min_request_interval_seconds',float('nan'))]:
            old=self.cfg.get(key)
            self.cfg[key]=value
            r=self.run_cli()
            self.assertNotEqual(r.returncode,0,(key,value))
            self.cfg[key]=old
        self.assertEqual(len(self.endpoint.starts),0)

    def test_partial_tail_refused(self):
        Path(self.cfg['ledger_path']).write_text('{"partial":')
        r=self.run_cli()
        self.assertNotEqual(r.returncode,0)
        self.assertEqual(len(self.endpoint.starts),0)

    def test_multiple_pairs_per_seed_are_distinct(self):
        self.seeds=self.seeds[:1]
        self.seeds[0]['n_pairs']=4
        r=self.run_cli()
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual(len(self.endpoint.starts),4)
        self.assertEqual(len({a['pair_key'] for a in self.effective()}),4)

    def test_second_process_runlock_prevents_extra_calls(self):
        self.endpoint.delay=.3
        proc=subprocess.Popen(self.cmd(),stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                              text=True,env=self.env())
        deadline=time.monotonic()+5
        while not self.endpoint.starts and time.monotonic()<deadline:
            time.sleep(.01)
        self.assertTrue(self.endpoint.starts)
        r=self.run_cli()
        self.assertEqual(r.returncode,2,r.stderr)
        self.assertIn('run lock',r.stderr)
        stdout,stderr=proc.communicate(timeout=10)
        self.assertEqual(proc.returncode,0,stderr)
        self.assertEqual(len(self.endpoint.starts),8)

    def test_killed_process_pending_rows_stay_blocked(self):
        self.endpoint.delay=.8
        proc=subprocess.Popen(self.cmd(),stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                              text=True,env=self.env())
        deadline=time.monotonic()+5
        while not self.endpoint.starts and time.monotonic()<deadline:
            time.sleep(.01)
        self.assertTrue(self.endpoint.starts)
        proc.kill()
        proc.communicate(timeout=5)
        previous={a['pair_key'] for a in self.effective()}
        self.assertTrue(previous)
        self.assertTrue(all(a['status']=='pending' for a in self.effective()))
        before=len(self.endpoint.starts)
        r=self.run_cli()
        self.assertEqual(r.returncode,0,r.stderr)
        # Unsent but already reserved work is deliberately held too: no guess.
        self.assertEqual(len(self.endpoint.starts)-before,8-len(previous))
        new=[a for a in self.effective() if a['pair_key'] in previous]
        self.assertTrue(all(a['status']=='pending' for a in new))
        self.assert_clean(r)

    def test_invalid_complete_output_record_refused(self):
        Path(self.cfg['out_path']).write_text('{"broken":true}\n')
        r=self.run_cli()
        self.assertNotEqual(r.returncode,0)
        self.assertEqual(len(self.endpoint.starts),0)

    def test_pacer_waiters_honor_new_global_cooldown(self):
        p = Pacer(.04)
        self.assertTrue(p.acquire())
        starts=[]
        t=threading.Thread(target=lambda:(p.acquire(),starts.append(time.monotonic())))
        t.start()
        time.sleep(.01)
        stamp=time.monotonic()
        p.throttled()
        t.join(1)
        self.assertEqual(len(starts),1)
        self.assertGreaterEqual(starts[0]-stamp,.07)
        self.assertEqual(p.interval,.08)
        p.stop()
        self.assertFalse(p.acquire())

    def test_sigint_stops_admission_and_resume_no_rebill(self):
        self.endpoint.delay=.3
        self.cfg['min_request_interval_seconds']=.15
        proc=subprocess.Popen(self.cmd(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, env=self.env())
        deadline=time.monotonic()+5
        while not self.endpoint.starts and time.monotonic()<deadline:
            time.sleep(.01)
        self.assertTrue(self.endpoint.starts)
        proc.send_signal(signal.SIGINT)
        stdout,stderr=proc.communicate(timeout=10)
        self.assertEqual(proc.returncode,130,stderr)
        before=len(self.endpoint.starts)
        self.assertLessEqual(before,2)
        self.assertTrue(all(a['status'] not in ('pending','claimed') for a in self.effective()))
        self.cfg['max_attempts']=before
        r=self.run_cli()
        self.assertEqual(len(self.endpoint.starts),before)

class PersistenceTests(unittest.TestCase):
    setUp = ParallelTests.setUp
    write = ParallelTests.write
    cmd = ParallelTests.cmd
    env = ParallelTests.env
    run_cli = ParallelTests.run_cli
    rows = ParallelTests.rows
    effective = ParallelTests.effective
    assert_clean = ParallelTests.assert_clean

    def fault_cli(self, fault):
        script = r"""
import os, runpy, sys
import parallel_runner as P
mode = sys.argv[1]
config = sys.argv[2]
original_persist = P.persist_output
original_run = P.run_parallel
if mode in ('before_output', 'partial_output', 'after_output_sync', 'readonly_output'):
    writes = 0
    def fail_output(path, rec):
        global writes
        if mode == 'readonly_output':
            writes += 1
            if writes == 1:
                return original_persist(path, rec)
            os.chmod(path, 0o444)
            return original_persist(path, rec)
        if mode == 'partial_output':
            with open(path, 'a') as f:
                f.write('{"partial":')
                f.flush()
                os.fsync(f.fileno())
        if mode == 'after_output_sync':
            original_persist(path, rec)
        raise OSError('simulated private error detail must not escape')
    P.persist_output = fail_output
elif mode == 'before_completed_close':
    def wrapped(R, cfg, ledger, **kw):
        original_append = ledger.append
        def append(a):
            if getattr(a, 'status', None) == 'completed':
                raise OSError('simulated private error detail must not escape')
            return original_append(a)
        ledger.append = append
        return original_run(R, cfg, ledger, **kw)
    P.run_parallel = wrapped
sys.argv = ['runner_v2.py', '--config', config]
runpy.run_path('runner_v2.py', run_name='__main__')
"""
        return subprocess.run([sys.executable,'-c',script,fault,str(self.write())],
                              cwd=REPO,env=self.env(),capture_output=True,text=True,timeout=30)

    def test_failure_before_output_never_marks_completed(self):
        self.cfg['workers']=2
        r=self.fault_cli('before_output')
        self.assertEqual(r.returncode,5,r.stderr)
        self.assertIn('PERSISTENCE FAILED',r.stderr)
        self.assertNotIn('private error detail',r.stderr)
        self.assertTrue(self.effective())
        self.assertTrue(all(a['status']=='pending' for a in self.effective()))
        self.assertEqual(len(self.rows('out_path')),0)
        before=len(self.endpoint.starts)
        self.cfg['max_attempts']=len(self.effective())
        r=self.run_cli()
        self.assertEqual(len(self.endpoint.starts),before)
        self.assertIn('RESUME RECONCILIATION',r.stderr)
        self.assert_clean(r)

    def test_synced_output_before_failed_close_is_skipped_without_rebill(self):
        self.seeds=self.seeds[:2]
        for fault in ('after_output_sync','before_completed_close'):
            # Independent paths inside this subtest use their own files.
            for key in ('ledger_path','out_path'):
                path=Path(self.cfg[key])
                if path.exists(): path.unlink()
            before=len(self.endpoint.starts)
            r=self.fault_cli(fault)
            self.assertEqual(r.returncode,5,r.stderr)
            self.assertEqual(len(self.endpoint.starts)-before,2)
            self.assertEqual(len(self.rows('out_path')),2)
            self.assertTrue(all(a['status']=='pending' for a in self.effective()))
            before=len(self.endpoint.starts)
            r=self.run_cli()
            self.assertEqual(r.returncode,0,r.stderr)
            self.assertIn('2 uncertain attempt(s) have output',r.stderr)
            self.assertEqual(len(self.endpoint.starts),before)
            self.assertEqual(len(self.rows('out_path')),2)
            self.assert_clean(r)

    def test_real_permission_failure_is_visible_and_has_no_orphan_completed(self):
        self.cfg['workers']=1
        self.seeds=self.seeds[:4]
        r=self.fault_cli('readonly_output')
        self.assertEqual(r.returncode,5,r.stderr)
        self.assertIn('PERSISTENCE FAILED',r.stderr)
        self.assertNotIn('Traceback',r.stderr)
        self.assertEqual(len(self.rows('out_path')),1)
        self.assertEqual([a['status'] for a in self.effective()],['completed','pending'])
        Path(self.cfg['out_path']).chmod(0o644)
        r=self.run_cli()
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertIn('1 have no matching output',r.stderr)
        self.assertEqual(len(self.endpoint.starts),4)
        self.assertEqual(len(self.rows('out_path')),3)
        self.assertEqual(sum(a['status']=='completed' for a in self.effective()),3)
        self.assertEqual(sum(a['status']=='pending' for a in self.effective()),1)
        self.assert_clean(r)

    def test_partial_output_failure_refuses_resume_with_no_calls(self):
        self.cfg['workers']=1
        r=self.fault_cli('partial_output')
        self.assertEqual(r.returncode,5,r.stderr)
        self.assertTrue(all(a['status']=='pending' for a in self.effective()))
        before=len(self.endpoint.starts)
        r=self.run_cli()
        self.assertEqual(r.returncode,5,r.stderr)
        self.assertIn('incomplete ledger/output tail',r.stderr)
        self.assertEqual(len(self.endpoint.starts),before)

    def test_legacy_orphan_completed_is_visible_and_nonzero_at_cap(self):
        self.seeds=self.seeds[:1]
        a=R.Attempt('old','s0',MODEL)
        a.pair_key='s0#1';a.status='completed'
        a.input_chars=220;a.output_chars=100
        Path(self.cfg['ledger_path']).write_text(json.dumps(a.row())+'\n')
        self.cfg['max_attempts']=1
        r=self.run_cli()
        self.assertEqual(r.returncode,5,r.stderr)
        self.assertIn('1 completed attempt(s) lack matching output',r.stderr)
        self.assertEqual(len(self.endpoint.starts),0)
        self.assertEqual(len(self.rows('out_path')),0)

    def test_record_sync_precedes_completed_ledger_close(self):
        script=r"""
import json,runpy,sys
import parallel_runner as P
original=P.run_parallel
checks=[]
def wrapped(R,cfg,ledger,**kw):
    append=ledger.append
    def verify(a):
        if getattr(a,'status',None)=='completed':
            with open(cfg['out_path']) as f:
                ids={json.loads(line)['attempt_id'] for line in f if line.strip()}
            assert a.attempt_id in ids, 'completed before durable output'
            checks.append(a.attempt_id)
        return append(a)
    ledger.append=verify
    return original(R,cfg,ledger,**kw)
P.run_parallel=wrapped
sys.argv=['runner_v2.py','--config',sys.argv[1]]
runpy.run_path('runner_v2.py',run_name='__main__')
assert len(checks)==8, checks
"""
        r=subprocess.run([sys.executable,'-c',script,str(self.write())],cwd=REPO,
                         env=self.env(),capture_output=True,text=True,timeout=30)
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual(len(self.rows('out_path')),8)

class ExceptionDrainTests(unittest.TestCase):
    setUp = ParallelTests.setUp
    write = ParallelTests.write
    cmd = ParallelTests.cmd
    env = ParallelTests.env
    run_cli = ParallelTests.run_cli
    rows = ParallelTests.rows
    effective = ParallelTests.effective
    assert_clean = ParallelTests.assert_clean

    def test_missing_s5_prompt_refused_before_any_call(self):
        self.seeds[5].pop('prompt')
        self.endpoint.delay=.3
        r=self.run_cli()
        self.assertEqual(r.returncode,5,r.stderr)
        self.assertIn('SEED PREFLIGHT FAILED',r.stderr)
        self.assertNotIn('Traceback',r.stderr)
        self.assertEqual(len(self.endpoint.starts),0)
        self.assertEqual(self.effective(),[])
        self.assertEqual(self.rows('out_path'),[])

    def test_missing_prompt_on_held_row_does_not_activate_or_block_pack(self):
        self.seeds[5].pop('prompt')
        self.seeds[5]['execution_status']='pending_user_approval'
        r=self.run_cli()
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual(len(self.endpoint.starts),7)

    def test_general_exception_drains_admitted_and_releases_waiters(self):
        script=r"""
import runpy,sys
import parallel_runner as P
original=P.run_parallel
mode=sys.argv[1]
def wrapped(R,cfg,ledger,**kw):
    build=R.build_client
    count=0
    def fail_fourth(c):
        nonlocal count
        count+=1
        if count==4:
            if mode=='systemexit':
                raise SystemExit('private detail not for diagnostics')
            raise ValueError('private detail not for diagnostics')
        return build(c)
    R.build_client=fail_fourth
    return original(R,cfg,ledger,**kw)
P.run_parallel=wrapped
sys.argv=['runner_v2.py','--config',sys.argv[2]]
runpy.run_path('runner_v2.py',run_name='__main__')
"""
        for mode,spacing in [('valueerror',.025),('systemexit',.3)]:
            for key in ('ledger_path','out_path'):
                path=Path(self.cfg[key])
                if path.exists():path.unlink()
            self.cfg['min_request_interval_seconds']=spacing
            before=len(self.endpoint.starts)
            r=subprocess.run([sys.executable,'-c',script,mode,str(self.write())],
                             cwd=REPO,env=self.env(),capture_output=True,text=True,timeout=30)
            self.assertEqual(r.returncode,6,r.stderr)
            self.assertIn('COORDINATOR FAILED',r.stderr)
            self.assertNotIn('Traceback',r.stderr)
            self.assertNotIn('private detail',r.stderr)
            self.assertTrue(self.effective())
            self.assertTrue(all(a['status'] in ('completed','released') for a in self.effective()),
                            self.effective())
            served=len(self.endpoint.starts)-before
            self.assertEqual(len(self.rows('out_path')),served)
            self.assertEqual(sum(a['status']=='completed' for a in self.effective()),served)
            if mode=='systemexit':
                self.assertTrue(any(a['status']=='released' for a in self.effective()))
            self.assert_clean(r)

class SigintBoundaryTests(unittest.TestCase):
    setUp = ParallelTests.setUp
    write = ParallelTests.write
    cmd = ParallelTests.cmd
    env = ParallelTests.env
    run_cli = ParallelTests.run_cli
    rows = ParallelTests.rows
    effective = ParallelTests.effective
    assert_clean = ParallelTests.assert_clean

    def test_sigint_at_reserve_submit_and_commit_boundaries(self):
        script=r"""
import os,signal,runpy,sys
import parallel_runner as P
mode=sys.argv[1]
sent=False
def interrupt_once():
    global sent
    if not sent:
        sent=True
        os.kill(os.getpid(),signal.SIGINT)
if mode=='after_reserve':
    original=P.reserve
    def reserve(*args,**kw):
        result=original(*args,**kw)
        if result[0] is not None: interrupt_once()
        return result
    P.reserve=reserve
elif mode in ('after_submit','submit_rejected'):
    original=P.ThreadPoolExecutor.submit
    def submit(self,*args,**kw):
        if mode=='submit_rejected':
            raise KeyboardInterrupt()
        future=original(self,*args,**kw)
        interrupt_once()
        return future
    P.ThreadPoolExecutor.submit=submit
elif mode=='after_output_sync':
    original=P.persist_output
    def persist(*args,**kw):
        original(*args,**kw)
        interrupt_once()
    P.persist_output=persist
sys.argv=['runner_v2.py','--config',sys.argv[2]]
runpy.run_path('runner_v2.py',run_name='__main__')
"""
        for mode in ('after_reserve','after_submit','submit_rejected','after_output_sync'):
            with self.subTest(mode=mode):
                for key in ('ledger_path','out_path'):
                    path=Path(self.cfg[key])
                    if path.exists():path.unlink()
                before=len(self.endpoint.starts)
                r=subprocess.run([sys.executable,'-c',script,mode,str(self.write())],
                                 cwd=REPO,env=self.env(),capture_output=True,text=True,timeout=30)
                self.assertEqual(r.returncode,130,r.stderr)
                effective=self.effective()
                self.assertTrue(effective)
                allowed = ('pending',) if mode=='submit_rejected' else ('completed','released')
                self.assertTrue(all(a['status'] in allowed for a in effective),
                                [(a['pair_key'],a['status']) for a in effective])
                if mode=='submit_rejected':
                    self.assertIn('Uncertain attempts remain spent and blocked',r.stderr)
                self.assertEqual(len(self.rows('out_path')),
                                 sum(a['status']=='completed' for a in effective))
                self.assertEqual(len(self.endpoint.starts)-before,len(self.rows('out_path')))
                self.assertNotIn('Traceback',r.stderr)
                self.assert_clean(r)

    def test_loaded_sigint_repeated_standalone(self):
        # Same shipped SIGINT regression, repeatedly, with modest CPU pressure.
        burners=[subprocess.Popen([sys.executable,'-c','while True: pass'],
                                  stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                 for _ in range(2)]
        try:
            for _ in range(20):
                r=subprocess.run([sys.executable,'-m','unittest',
                    'test_parallel.ParallelTests.test_sigint_stops_admission_and_resume_no_rebill'],
                    cwd=REPO,capture_output=True,text=True,timeout=20)
                self.assertEqual(r.returncode,0,r.stdout+r.stderr)
        finally:
            for proc in burners:
                proc.terminate()
            for proc in burners:
                proc.wait(timeout=10)

class PartialSubmitTests(unittest.TestCase):
    setUp = ParallelTests.setUp
    write = ParallelTests.write
    cmd = ParallelTests.cmd
    env = ParallelTests.env
    run_cli = ParallelTests.run_cli
    rows = ParallelTests.rows
    effective = ParallelTests.effective
    assert_clean = ParallelTests.assert_clean

    def test_actual_enqueue_then_thread_start_failure_retains_count_on_resume(self):
        script=r"""
import runpy,sys,threading
import parallel_runner as P
original_start=threading.Thread.start
original_call=P._call
second_finished=threading.Event()
count=0
lock=threading.Lock()
def call(*args,**kw):
    global count
    with lock:
        count+=1
        number=count
    result=original_call(*args,**kw)
    if number==2:second_finished.set()
    return result
P._call=call
starts=0
def start(self,*args,**kw):
    global starts
    if self.name.startswith('oracle-call'):
        starts+=1
        if starts==2:
            # Real submit has already enqueued item 2. Worker 1 completes its
            # first request and executes item 2 before Thread.start raises.
            assert second_finished.wait(10), 'existing worker did not execute queued work'
            raise RuntimeError('simulated second Thread.start failure')
    return original_start(self,*args,**kw)
threading.Thread.start=start
sys.argv=['runner_v2.py','--config',sys.argv[1]]
runpy.run_path('runner_v2.py',run_name='__main__')
"""
        self.cfg.update(workers=2,max_attempts=2)
        self.seeds=self.seeds[:2]
        r=subprocess.run([sys.executable,'-c',script,str(self.write())],cwd=REPO,
                         env=self.env(),capture_output=True,text=True,timeout=20)
        self.assertEqual(r.returncode,6,r.stderr)
        self.assertEqual(len(self.endpoint.starts),2)
        rows=self.effective()
        self.assertEqual(len(rows),2)
        self.assertEqual(sum(a['status']!='released' for a in rows),2)
        self.assertEqual(sorted(a['status'] for a in rows),['completed','pending'])
        self.assertEqual(len(self.rows('out_path')),1)
        self.assert_clean(r)
        r=self.run_cli()
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertIn('RESUME RECONCILIATION',r.stderr)
        self.assertIn('cap already reached',r.stdout)
        self.assertEqual(len(self.endpoint.starts),2)
        self.assertEqual(len(self.effective()),2)

if __name__=='__main__':
    unittest.main(verbosity=2)
