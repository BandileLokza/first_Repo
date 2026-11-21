"""8-ball pool domain for EsportsLive.

The pool app deliberately owns its own competition, participant and result
models.  It shares site-level infrastructure with football, but never uses
football competition or team records as pool participants.
"""
