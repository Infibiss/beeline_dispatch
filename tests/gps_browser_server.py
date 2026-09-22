"""Test-only launcher: close owned server/workers on a temporary stop-file signal."""
from http.server import ThreadingHTTPServer
import os
from pathlib import Path
import threading
import time


if __name__ == '__main__':
    from field_router.scenario_server import main
    original = ThreadingHTTPServer.serve_forever

    def serve_until_parent_stops(server, *args, **kwargs):
        def monitor():
            stop = Path(os.environ['GPS_TEST_STOP_FILE'])
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline and not stop.exists():
                time.sleep(.2)
            server.shutdown()
        threading.Thread(target=monitor, daemon=True).start()
        original(server, *args, **kwargs)

    ThreadingHTTPServer.serve_forever = serve_until_parent_stops
    main()
