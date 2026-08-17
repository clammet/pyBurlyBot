from threading import Thread
from unittest import TestCase
from unittest.mock import patch

from twisted.python import threadable

from util.threads import call_in_reactor


class CallInReactorTest(TestCase):
    def setUp(self) -> None:
        self._io_thread = threadable.ioThread
        self.addCleanup(setattr, threadable, "ioThread", self._io_thread)

    def test_runs_directly_in_the_reactor_thread(self) -> None:
        threadable.registerAsIOThread()
        with patch("util.threads.blockingCallFromThread") as hop:
            self.assertEqual(call_in_reactor(lambda a, b=0: a + b, 1, b=2), 3)
        hop.assert_not_called()

    def test_hops_from_other_threads(self) -> None:
        threadable.registerAsIOThread()
        results = []
        with patch("util.threads.blockingCallFromThread", return_value="hopped") as hop:
            worker = Thread(target=lambda: results.append(call_in_reactor(len, "ab")))
            worker.start()
            worker.join()
        self.assertEqual(results, ["hopped"])
        hop.assert_called_once()
        self.assertEqual(hop.call_args.args[1:], (len, "ab"))
