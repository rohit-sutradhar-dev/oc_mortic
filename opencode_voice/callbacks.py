from typing import Callable, Any, Awaitable

EventCallback = Callable[[dict[str, Any]], Awaitable[None]]
