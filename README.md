# SG Transport Pulse — Easy Deploy

This package is ready for a Python/Docker hosting service.

Required secret:
- LTA_ACCOUNT_KEY

OneMap:
- ONEMAP_TOKEN can be used for immediate testing.
- For continuous operation, set ONEMAP_EMAIL and ONEMAP_PASSWORD as private hosting environment variables.
  The backend caches the token, refreshes it before expiry, and retries after authentication expiry.

Start command:
uvicorn app:app --host 0.0.0.0 --port $PORT

Security:
Do not put keys/tokens/passwords in index.html or commit them to a public repository.
Rotate any credential previously pasted into a chat before production deployment.
