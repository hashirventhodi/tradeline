"""Source adapters: each turns one external source's responses into typed rows.

Every adapter makes its HTTP calls through an injected ``Transport`` and
nothing else; tests feed them recorded fixtures through ``PlaybackTransport``.
"""
