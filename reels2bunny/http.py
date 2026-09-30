import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def make_session() -> requests.Session:
    """Session with automatic retries for transient errors on idempotent reads.

    Uploads (PUT) are deliberately NOT retried here: a streamed file body can't be
    rewound by urllib3, so a retry could send a truncated file. Upload retries are
    done in sync.py, which reopens the file for every attempt.
    """
    retry = Retry(
        total=4,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "HEAD"}),
        respect_retry_after_header=True,
        raise_on_status=False,  # return the last response so callers can classify it
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=16, pool_maxsize=16)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers["User-Agent"] = "instagram-reels-to-bunny/0.1"
    return session
