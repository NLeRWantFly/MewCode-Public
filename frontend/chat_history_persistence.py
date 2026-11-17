"""Track durable chat additions across tool-result wrapper repairs."""
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import replace

from mewcode.conversation import Message, ToolResultBlock


class ChatHistoryPersistence:
    def __init__(self, history: Iterable[Message]) -> None:
        self.reset(history)

    def reset(self, history: Iterable[Message]) -> None:
        """Mark an existing or compacted history, releasing the previous refs."""
        messages: dict[int, Message] = {}
        results: dict[int, ToolResultBlock] = {}
        for message in history:
            if message.transient:
                continue
            if message.tool_results:
                results.update((id(result), result) for result in message.tool_results)
            else:
                messages[id(message)] = message
        self._messages, self._results = messages, results

    def persist(self, history: Iterable[Message], append: Callable[[Message], None]) -> None:
        for message in history:
            if message.transient:
                continue
            if message.tool_results:
                # Repair replaces Message wrappers but retains these blocks.
                # Strong refs keep their identities reserved for this boundary.
                new_results = {}
                for result in message.tool_results:
                    if id(result) not in self._results:
                        new_results[id(result)] = result
                if not new_results:
                    continue
                self._results.update(new_results)
                append(replace(message, tool_results=list(new_results.values())))
            elif id(message) not in self._messages:
                # Keep the object, not only its id: a released old wrapper's id
                # must never make a later assistant look already persisted.
                # Mark before append, as before: a post-write metadata failure
                # must not cause the durable message to be appended again.
                self._messages[id(message)] = message
                append(message)
