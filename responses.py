class _R:
    def __init__(self, content=None, status_code=200, media_type=None, headers=None, **k): self.body, self.status_code, self.headers = content, status_code, headers or {}
HTMLResponse = JSONResponse = Response = RedirectResponse = FileResponse = StreamingResponse = PlainTextResponse = _R
