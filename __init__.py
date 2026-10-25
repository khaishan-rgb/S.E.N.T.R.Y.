class _Deco:
    def __getattr__(self, name):
        def deco(*a, **k):
            def wrap(f): return f
            return wrap
        return deco
class FastAPI(_Deco):
    def __init__(self, *a, **k): self.state = type("S", (), {})()
    def add_middleware(self, *a, **k): pass
    def mount(self, *a, **k): pass
    def include_router(self, *a, **k): pass
def Query(default=None, *a, **k): return default
class Request: pass
class HTTPException(Exception):
    def __init__(self, status_code=500, detail=""): self.status_code, self.detail = status_code, detail
