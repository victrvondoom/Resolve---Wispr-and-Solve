"""Concurrent first requests must not race the fixture world's seeding (previously 500 UNIQUE constraint)."""
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_web_api_g1 import HAVE_PW, Server, free_port


@unittest.skipUnless(HAVE_PW, 'playwright not installed')
class WorldRace(unittest.TestCase):
    def test_parallel_first_requests(self):
        for _ in range(3):
            with tempfile.TemporaryDirectory() as tmp:
                srv = Server(tmp, free_port()).start()
                try:
                    paths = ['/api/web/agents', '/api/web/skills', '/api/web/agents', '/api/web/skills', '/api/web/missions']
                    with ThreadPoolExecutor(5) as ex:
                        codes = list(ex.map(lambda p: srv.req('GET', p)[0], paths))
                    self.assertEqual(codes, [200] * 5)
                finally:
                    srv.stop()


if __name__ == '__main__':
    unittest.main()
