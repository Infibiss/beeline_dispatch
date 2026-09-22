"""One replaceable model in a disposable spawned process; no plan access."""
import importlib
import json
import logging


def worker_main(connection, entrypoint):
    try:
        module, name = entrypoint.split(':', 1)
        model = getattr(importlib.import_module(module), name)()
        connection.send_bytes(b'{"ready":true}')
        while True:
            job = json.loads(connection.recv_bytes(262144))
            try:
                result = model.analyze(job)
                encoded = json.dumps({'result': result}, ensure_ascii=False, allow_nan=False).encode('utf-8')
                if len(encoded) > 32768:
                    raise ValueError('Model output limit exceeded')
                connection.send_bytes(encoded)
            except Exception:
                logging.exception('GPS model inference failed')  # no coordinates in message
                connection.send_bytes(b'{"error":"model_error"}')
    except (EOFError, BrokenPipeError, OSError):
        pass
    except Exception:
        logging.exception('GPS model startup failed')
        try:
            connection.send_bytes(b'{"error":"startup_error"}')
        except OSError:
            pass
    finally:
        connection.close()
