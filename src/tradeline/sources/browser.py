"""What the exchange sites and news publishers need before they answer.

NSE, BSE, and some RSS publishers refuse a client that does not look like a
browser, and the exchanges throttle. The adapters share one browser
User-Agent and one backoff on throttling (429) and server errors.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from tradeline.transport import Request, Response, Transport

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
    " (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# The delay before each retry.
RETRY_DELAYS_SECONDS: tuple[float, ...] = (1.0, 3.0, 9.0)
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


def send_with_backoff(
    transport: Transport, request: Request, *, sleep: Callable[[float], None], site: str
) -> Response:
    """Send ``request``, retrying after each delay while the answer is throttled or failing.

    The last response is returned whatever its status; ``site`` names the
    source in the warning logged before each retry.
    """
    response = transport.send(request)
    for attempt, delay in enumerate(RETRY_DELAYS_SECONDS, start=1):
        if response.status not in RETRY_STATUSES:
            break
        log.warning(
            f"{site} throttled or failing; backing off",
            extra={
                "url": request.url,
                "status": response.status,
                "attempt": attempt,
                "delay": delay,
            },
        )
        sleep(delay)
        response = transport.send(request)
    return response
