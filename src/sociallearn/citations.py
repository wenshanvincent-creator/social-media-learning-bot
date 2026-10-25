"""Lossless display ranges; stored citations always retain exact segment IDs."""
from __future__ import annotations

import re


def compact_ids(ids):
    result, i = [], 0
    while i < len(ids):
        first = re.fullmatch(r"(.*?)(\d+)", ids[i])
        j = i
        if first:
            while j + 1 < len(ids):
                other = re.fullmatch(r"(.*?)(\d+)", ids[j + 1])
                if not other or other[1] != first[1] or len(other[2]) != len(first[2]) or int(other[2]) != int(first[2]) + j + 1 - i:
                    break
                j += 1
        if j - i >= 2:
            result.append(ids[i] + ".." + ids[j])
        else:
            result.extend(ids[i:j + 1])
        i = j + 1
    return ",".join(result)


def expand_ids(selectors, known):
    """Expand display ranges against real source IDs, rejecting holes/unknowns."""
    result = []
    known = set(known)
    for selector in selectors:
        for item in selector.split(","):
            if item in known:
                result.append(item)
                continue
            match = re.fullmatch(r"(.*?)(\d+)\.\.(.*?)(\d+)", item)
            if not match or match[1] != match[3] or len(match[2]) != len(match[4]):
                raise ValueError("Unknown segment ID or invalid citation range")
            low, high = int(match[2]), int(match[4])
            members = sorted((sid for sid in known if re.fullmatch(re.escape(match[1]) + r"\d{" + str(len(match[2])) + r"}", sid)
                              and low <= int(sid[len(match[1]):]) <= high),
                             key=lambda sid: int(sid[len(match[1]):]))
            if high < low or len(members) != high - low + 1:
                raise ValueError("Citation range contains missing segment IDs")
            result.extend(members)
    return result
