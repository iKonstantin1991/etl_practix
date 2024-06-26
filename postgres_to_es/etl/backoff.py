import time
import logging
from typing import Any, Tuple, Callable
from functools import wraps

from etl.logger import logger as main_logger


def backoff(start_sleep_time: float = 0.1,
            factor: int = 2,
            border_sleep_time: int = 10,
            attempts_threshold: int = 50,
            exceptions: Tuple[Exception, ...] = (),
            logger: logging.Logger = main_logger) -> Callable[..., Any]:
    def func_wrapper(func: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(func)
        def inner(*args: Any, **kwargs: Any) -> Any:
            n = 1
            while True:
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    logger.error(f"Exception has occurred: {e}")
                    if n > attempts_threshold:
                        raise
                    time_to_sleep = start_sleep_time * (factor ** n)
                    time.sleep(time_to_sleep if time_to_sleep < border_sleep_time else border_sleep_time)
                    n += 1
        return inner
    return func_wrapper
