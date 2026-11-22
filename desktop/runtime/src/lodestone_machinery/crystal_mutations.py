"""Exact integer mutation features; no text equivalence or compression claim.

A[i][j] counts arrows i -> j. Mutating vertex j replaces its coordinate by
the incoming weighted sum minus that coordinate. A fixed zero-diagonal graph
makes each mutation an involution. Finite graph does NOT imply finite orbit.
"""
from collections import deque

A3 = ((0, 1, 0), (1, 0, 1), (0, 1, 0))


def _check(vector, adjacency):
    if (type(vector) not in (list, tuple) or not 1 <= len(vector) <= 16
            or any(type(x) is not int or abs(x).bit_length() > 256 for x in vector)):
        raise ValueError("Supply 1..16 bounded integer coordinates")
    n = len(vector)
    if type(adjacency) not in (tuple, list) or len(adjacency) != n:
        raise ValueError("Adjacency shape mismatch")
    for i, row in enumerate(adjacency):
        if (type(row) not in (tuple, list) or len(row) != n
                or any(type(x) is not int or not 0 <= x <= 16 for x in row)
                or row[i] != 0):
            raise ValueError("Require square nonnegative integer adjacency with zero diagonal")


def mutate(vector, vertex, adjacency=A3):
    _check(vector, adjacency)
    if type(vertex) is not int or not 0 <= vertex < len(vector):
        raise ValueError("Invalid mutation vertex")
    result = list(vector)
    result[vertex] = sum(adjacency[i][vertex] * vector[i] for i in range(len(vector))) - vector[vertex]
    if abs(result[vertex]).bit_length() > 256:
        raise ValueError("Mutation integer bound exceeded")
    return tuple(result)


def orbit(vector, adjacency=A3, *, max_states=4096):
    """Return full orbit only if exhausted; reaching cap is an explicit failure."""
    _check(vector, adjacency)
    if type(max_states) is not int or not 1 <= max_states <= 4096:
        raise ValueError("Invalid orbit bound")
    seen = {tuple(vector)}
    queue = deque(seen)
    while queue:
        current = queue.popleft()
        for vertex in range(len(vector)):
            following = mutate(current, vertex, adjacency)
            if following not in seen:
                if len(seen) >= max_states:
                    raise ValueError("Orbit bound reached; finiteness not established")
                seen.add(following)
                queue.append(following)
    return tuple(sorted(seen))


def a3_features(vector):
    """Identity then each one-step mutation: 12 numbers, still only rank 3."""
    if len(vector) != 3:
        raise ValueError("A3 requires exactly three coordinates")
    _check(vector, A3)
    return tuple(vector) + tuple(x for vertex in range(3) for x in mutate(vector, vertex))


def reverse_trace(vector, vertices, adjacency=A3):
    if type(vertices) not in (tuple, list) or len(vertices) > 4096:
        raise ValueError("Mutation trace exceeds bound")
    result = tuple(vector)
    for vertex in reversed(vertices):
        result = mutate(result, vertex, adjacency)
    return result
