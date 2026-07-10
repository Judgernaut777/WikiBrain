"""Detection engines. Each answers "what is in this text", never "what should we do".

Every module here imports its dependency lazily, inside the engine's `__init__` or
`scan`, so that `import wiki` never pulls in spaCy, torch, or detect-secrets.
"""
