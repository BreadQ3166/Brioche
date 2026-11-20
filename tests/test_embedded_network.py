import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
import unittest
from unittest.mock import patch
from brioche.embedded_network import EmbeddedNetwork, login_url
from brioche.security import access_uri, parse_uri


class LoginURLTests(unittest.TestCase):
    def test_only_official_https_login_urls(self):
        self.assertTrue(login_url('https://login.tailscale.com/a/test'))
        for value in ('http://login.tailscale.com/a/test', 'https://login.tailscale.com.evil.test/',
                      'https://user:secret@login.tailscale.com/', 'file:///etc/passwd',
                      'https://login.tailscale.com:8080/a/test', None):
            self.assertFalse(login_url(value))


@unittest.skipIf(os.name == 'nt', 'POSIX subprocess fixture')
class EmbeddedIPCTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.helper = self.folder / 'helper'
        self.helper.write_text('#!' + sys.executable + '\n' + '''import sys,json,os
for line in sys.stdin:
 r=json.loads(line);op=r['op'];result={'ok':True}
 if op in ('status','login'):
  result={'state':'Running','ips':['100.70.1.2'],'peers':[{'ip':'100.70.1.3','name':'other','online':True,'path':'直连'}],'auth_url':''}
 elif op=='tunnel':result={'port':32123,'tunnel':'1'}
 elif op=='environment':result={'auth_present':'TS_AUTHKEY' in os.environ}
 print(json.dumps({'id':r['id'],'result':result}),flush=True)
 if op=='shutdown':break
''')
        self.helper.chmod(0o700)
        self.network = EmbeddedNetwork(folder=self.folder / 'node', executable=self.helper)

    def tearDown(self):
        self.network.close()
        self.temp.cleanup()

    def test_idle_does_not_start_network_until_login(self):
        self.assertFalse(self.network.status()['ready'])
        self.assertIsNone(self.network.process)
        self.assertTrue(self.network.login()['ready'])
        self.assertEqual(self.network.status('100.70.1.3')['path'], '直连')
        if os.name != 'nt':
            self.assertEqual(self.network.folder.stat().st_mode & 0o777, 0o700)
        self.network.logout()
        self.assertFalse(self.network.enabled)
        self.assertFalse(self.network.status()['ready'])

    def test_proxy_preserves_certificate_and_session_token(self):
        token=secrets.token_urlsafe(32)
        uri=access_uri('100.70.1.3',47995,'a'*64,token)
        self.assertEqual(parse_uri(self.network.proxy_uri(uri)),('127.0.0.1',32123,'a'*64,token))

    def test_no_environment_auth_key_is_implicitly_used(self):
        with patch.dict(os.environ, {'TS_AUTHKEY':'test-key-not-for-brioche'}):
            self.assertFalse(self.network.request('environment')['auth_present'])

    def test_close_exits_child_and_cannot_restart(self):
        self.network.request('status')
        process=self.network.process
        self.network.close()
        self.assertEqual(process.poll(),0)
        with self.assertRaises(RuntimeError):
            self.network.request('status')

    def test_stopped_share_cannot_be_published_after_delayed_work(self):
        from types import SimpleNamespace
        import threading
        stopped=threading.Event()
        stopped.set()
        host=SimpleNamespace(bind='127.0.0.1', stop_requested=stopped, port=32123, pin='a'*64, token='b'*43)
        self.network.publish(host)
        self.assertIsNone(self.network.process)
