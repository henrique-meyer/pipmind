"""One owned event loop for SDK work, isolated from the private HTTP server."""

import asyncio
from threading import Event, Thread

from pipmind.shadow.common import ShadowError


class ReasoningWorker:
    def __init__(self):
        self.loop = None
        self.ready = Event()
        self.thread = Thread(target=self._run, name="pipmind-reasoning", daemon=True)

    def _run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.ready.set()
        try:
            self.loop.run_forever()
        finally:
            self.loop.close()

    def start(self):
        self.thread.start()
        if not self.ready.wait(5) or not self.alive():
            raise ShadowError("shadow_worker_start_failed")

    def alive(self):
        return self.thread.is_alive() and self.loop is not None and not self.loop.is_closed()

    async def run(self, coroutine):
        if not self.alive():
            coroutine.close()
            raise ShadowError("shadow_worker_unavailable")
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        return await asyncio.wrap_future(future)

    def close(self):
        if not self.alive():
            return

        async def stop():
            tasks = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.wait(tasks, timeout=2)
            self.loop.call_soon(self.loop.stop)

        future = asyncio.run_coroutine_threadsafe(stop(), self.loop)
        try:
            future.result(timeout=3)
        except Exception:
            self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=3)
