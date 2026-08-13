from types import SimpleNamespace
from typing import cast
from unittest import TestCase
from unittest.mock import Mock, patch

from util.timer import Timer, Timers


class TimersTest(TestCase):
    def test_timer_callbacks_are_scheduled_outside_the_reactor_thread(self) -> None:
        blocking_http_callback = Mock()
        timer = cast(
            Timer,
            SimpleNamespace(
                f=blocking_http_callback,
                args=("argument",),
                kwargs={"keyword": "value"},
                reps=-1,
            ),
        )

        with patch("util.timer.reactor.callInThread") as call_in_thread:
            Timers.runTimer(timer)

        blocking_http_callback.assert_not_called()
        call_in_thread.assert_called_once_with(
            blocking_http_callback, "argument", keyword="value"
        )
