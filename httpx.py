class AsyncClient:
    def __init__(self, *a, **k): pass
    async def get(self, *a, **k): raise RuntimeError("no network in tests")
    async def post(self, *a, **k): raise RuntimeError("no network in tests")
    async def aclose(self): pass
class Timeout:
    def __init__(self, *a, **k): pass
class Limits:
    def __init__(self, *a, **k): pass
class HTTPError(Exception): pass
class HTTPStatusError(HTTPError): pass
class TimeoutException(HTTPError): pass
class RequestError(HTTPError): pass
