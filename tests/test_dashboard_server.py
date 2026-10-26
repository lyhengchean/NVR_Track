import base64
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from unittest.mock import patch
from app import dashboard


class ServerSecuritySmokeTests(unittest.TestCase):
    def test_auth_protects_both_dashboard_and_api(self):
        with patch.object(dashboard, 'DASHBOARD_USERNAME', 'demo'), \
             patch.object(dashboard, 'DASHBOARD_PASSWORD', 'test-password-long'):
            server = ThreadingHTTPServer(('127.0.0.1', 0), dashboard.DashboardHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f'http://127.0.0.1:{server.server_port}'
                for url in ('/', '/api/dashboard'):
                    with self.assertRaises(HTTPError) as ctx:
                        urlopen(base+url,timeout=2)
                    self.assertEqual(ctx.exception.code,401)
                    token=base64.b64encode(b'demo:test-password-long').decode()
                    request=Request(base+url,headers={'Authorization':'Basic '+token})
                    with urlopen(request,timeout=2) as response:
                        self.assertEqual(response.status,200)
                        self.assertEqual(response.headers['Cache-Control'],'no-store')
                        if url=='/':
                            self.assertIn(b'NVR Track',response.read())
                        else:
                            self.assertIn('summary',json.loads(response.read()))
            finally:
                server.shutdown()
                server.server_close()
