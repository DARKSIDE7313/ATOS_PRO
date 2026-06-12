import asyncio
from collections import defaultdict


class AsyncEventBus:
    def __init__(self):
        self._handlers = defaultdict(list)
        self._queue = asyncio.Queue()
        self._running = False

    def subscribe(self, event_type, handler):
        self._handlers[event_type].append(handler)

    async def publish(self, event):
        await self._queue.put(event)

    async def run(self):
        self._running = True
        while self._running:
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=0.1)
                event_type = type(event)
                for handler in self._handlers.get(event_type, []):
                    try:
                        if asyncio.iscoroutinefunction(handler):
                            asyncio.create_task(handler(event))
                        else:
                            handler(event)
                    except Exception as e:
                        print(f"[EventBus] handler error: {e}")
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                print(f"[EventBus] error: {e}")

    def stop(self):
        self._running = False
