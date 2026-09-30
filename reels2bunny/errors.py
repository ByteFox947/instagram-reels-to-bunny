"""Error types used to decide what to do when something goes wrong.

- RetryableError: temporary (network, rate limit, 5xx) -> retry with backoff
- SkipError:      this one reel can never be fetched (deleted, unavailable) -> skip it
- FatalError:     nothing will work until the user fixes something (bad password,
                  expired cookies, profile not found) -> stop the whole run
"""


class Reels2BunnyError(Exception):
    """Base class for all expected errors (printed without a traceback)."""


class RetryableError(Reels2BunnyError):
    pass


class SkipError(Reels2BunnyError):
    pass


class FatalError(Reels2BunnyError):
    pass
