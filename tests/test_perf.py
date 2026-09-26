"""The speed instrumentation: stalls are caught and blamed on the right job."""

import asyncio
import time
import unittest

from scheduler import perf


class TestPerf(unittest.TestCase):
    def test_a_blocking_job_is_blamed_for_the_stall(self):
        async def blocking_job():
            time.sleep(0.6)            # CPU work on the event loop

        async def run():
            mon = asyncio.create_task(perf.loop_monitor(interval=0.05, threshold=0.2))
            await asyncio.sleep(0.1)
            await perf.wrap_job("heavy_job", blocking_job)()
            await asyncio.sleep(0.1)
            mon.cancel()

        asyncio.run(run())
        r = perf.report()
        self.assertIn("heavy_job", r["stall_blame_ms"])
        self.assertGreater(r["stall_blame_ms"]["heavy_job"], 300)
        self.assertEqual(r["jobs"][0]["job"], "heavy_job")
        self.assertGreater(r["jobs"][0]["max_ms"], 500)

    def test_scheduler_jobs_are_wrapped(self):
        added = {}

        class Sched:
            def add_job(self, func, *a, **kw):
                added[kw.get("id")] = func

        s = Sched()
        perf.instrument_scheduler(s)

        async def job():
            return 7
        s.add_job(job, "interval", id="x")
        self.assertEqual(asyncio.run(added["x"]()), 7)
        self.assertIn("x", perf.report()["stall_blame_ms"] | {j["job"]: 1
                                                              for j in perf.report()["jobs"]})


if __name__ == "__main__":
    unittest.main()
