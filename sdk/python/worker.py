"""A synchronous component ABI. All host operations cross the JSON RPC boundary."""
import contextlib
import json
import sys
import traceback


def emit(packet):
    sys.__stdout__.write(json.dumps(packet, allow_nan=False) + '\n')
    sys.__stdout__.flush()


class API:
    def __init__(self):
        self.sequence = 0

    def request(self, operation, **arguments):
        self.sequence += 1
        emit({'type': 'request', 'id': self.sequence, 'operation': operation, 'arguments': arguments})
        reply = json.loads(sys.stdin.readline())
        if reply.get('id') != self.sequence:
            raise RuntimeError('RPC reply mismatch')
        if 'error' in reply:
            raise RuntimeError(reply['error'])
        return reply['result']


try:
    initial = json.loads(sys.stdin.readline())
    namespace = {'__name__': '__component__'}
    with contextlib.redirect_stdout(sys.stderr):
        exec(compile(initial['source'], '<component>', 'exec'), namespace)
        output = namespace['main'](initial['input'], API())
    emit({'type': 'return', 'value': output})
except BaseException as error:
    emit({'type': 'failure', 'error': f'{type(error).__name__}: {error}', 'traceback': traceback.format_exc()})
    sys.exit(1)
