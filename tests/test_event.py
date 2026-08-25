from copy import copy
from unittest import TestCase

from util.event import Event


class EventTest(TestCase):
    def test_shallow_copy_preserves_dynamic_attributes(self) -> None:
        event = Event("privmsged", msg="hello", payload={"id": 1})

        copied = copy(event)

        self.assertIsNot(copied, event)
        self.assertEqual(copied.payload, {"id": 1})
        self.assertIs(copied.kwargs, event.kwargs)
