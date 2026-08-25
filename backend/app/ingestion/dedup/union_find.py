"""Disjoint-set forest with path compression and union by size."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Hashable, Iterator
from typing import Generic, TypeVar

K = TypeVar("K", bound=Hashable)


class UnionFind(Generic[K]):
    """Groups items that share any key, transitively.

    Transitivity is the point: a record matching on email and another matching on phone end up
    in the same cluster even though no single key links them directly. It is also the risk, and
    the reason only exact, identity-bearing keys are fed to it — one shared link-aggregator URL
    would otherwise chain hundreds of unrelated businesses into a single blob.
    """

    __slots__ = ("_parent", "_size")

    def __init__(self) -> None:
        self._parent: dict[K, K] = {}
        self._size: dict[K, int] = {}

    def add(self, item: K) -> None:
        if item not in self._parent:
            self._parent[item] = item
            self._size[item] = 1

    def find(self, item: K) -> K:
        self.add(item)
        root = item
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[item] != root:
            self._parent[item], item = root, self._parent[item]
        return root

    def union(self, left: K, right: K) -> K:
        left_root, right_root = self.find(left), self.find(right)
        if left_root == right_root:
            return left_root
        if self._size[left_root] < self._size[right_root]:
            left_root, right_root = right_root, left_root
        self._parent[right_root] = left_root
        self._size[left_root] += self._size[right_root]
        return left_root

    def groups(self) -> dict[K, list[K]]:
        clustered: dict[K, list[K]] = defaultdict(list)
        for item in self._parent:
            clustered[self.find(item)].append(item)
        return dict(clustered)

    def __len__(self) -> int:
        return len(self._parent)

    def __iter__(self) -> Iterator[K]:
        return iter(self._parent)
