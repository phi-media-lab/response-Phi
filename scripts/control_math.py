"""Independent full-array Int32 reference for the P6 control-location audit.

Uses only Python's standard library and imports no experiment/oracle code.
The division is exact within the stated small bounds before truncation to zero.
"""
from functools import lru_cache
import hashlib
import struct


C, D, PENALTY = 64, 128, 257
SENTINEL = -(1 << 31)
ARRAY_NAMES = ("score_history", "winner_history", "context_history", "final_context", "final_visits")


def require(ok, message):
    if not ok:
        raise ValueError(message)


def array_hash(values):
    require(isinstance(values, list), "array must be a list")
    require(all(type(v) is int and -(1 << 31) <= v < (1 << 31) for v in values), "non-Int32 array entry")
    return hashlib.sha256(struct.pack("<" + "i" * len(values), *values)).hexdigest()


@lru_cache(maxsize=16, typed=True)
def reference(rounds, seed=7, completed=None):
    require(type(rounds) is int and rounds in (16, 64), "unsupported planned rounds")
    require(type(seed) is int and 0 <= seed <= 65535, "invalid seed")
    completed = rounds if completed is None else completed
    require(type(completed) is int and 0 <= completed <= rounds, "invalid completed prefix")
    # Flat input first, then explicit row slices, independently of the driver.
    features = [((17 * (index // D) + 13 * (index % D) + seed
                 + ((index // D + 1) * (index % D + 3)) % 23
                 + ((index // D) * (index % D) + 3 * (index // D) ** 2 + seed) % 29) % 17) - 8
                for index in range(C * D)]
    initial = [(7 * d + seed) % 17 - 8 for d in range(D)]
    rows = [features[j * D:(j + 1) * D] for j in range(C)]
    state, visits = initial[:], [0] * C
    scores, winners, contexts = [], [], []
    for _ in range(completed):
        next_scores = [sum(q * v for q, v in zip(state, row)) - PENALTY * visits[j]
                       for j, row in enumerate(rows)]
        require(all(-24640 <= s <= 8192 for s in next_scores), "score bound violated")
        best = max(next_scores)
        winner = next_scores.index(best)  # first maximum implements smallest-index tie break
        visits[winner] += 1
        numerators = [3 * q + v for q, v in zip(state, rows[winner])]
        require(all(-32 <= n <= 32 for n in numerators), "update numerator bound violated")
        state = [int(n / 4) for n in numerators]  # exact quarter, then truncation toward zero
        require(all(-8 <= q <= 8 for q in state), "context bound violated")
        scores.extend(next_scores); winners.append(winner); contexts.extend(state)
    arrays = {
        "score_history": scores + [SENTINEL] * ((rounds - completed) * C),
        "winner_history": winners + [-1] * (rounds - completed),
        "context_history": contexts + [SENTINEL] * ((rounds - completed) * D),
        "final_context": state,
        "final_visits": visits,
    }
    return {
        "inputs": {"features": features, "initial_context": initial,
                   "features_sha256": array_hash(features), "initial_context_sha256": array_hash(initial)},
        "actual": {"completed_rounds": completed, **arrays},
        "sha256": {name: array_hash(value) for name, value in arrays.items()},
        "nondegenerate": {"distinct_winners": len(set(winners)),
                          "adjacent_winner_changes": sum(a != b for a, b in zip(winners, winners[1:]))},
    }


def verify_reference_document(document):
    """Cross-check a generated CPU oracle file before it is used by Swift."""
    require(document['schema_version'] == 1 and document['experiment'] == 'metal-control-location-cpu-reference',
            'wrong CPU reference schema')
    config = document["config"]
    require(config == {"C": C, "D": D, "T": config["T"], "seed": config["seed"],
                       "penalty": PENALTY, "dtype": "int32"}, "CPU reference configuration mismatch")
    require(type(document['reference']['completed_rounds']) is int and document['reference']['completed_rounds'] == config['T'],
            'CPU reference must cover all planned rounds')
    result = reference(config["T"], config["seed"], document["reference"]["completed_rounds"])
    require(document["inputs"] == result["inputs"], "CPU reference input arrays or hashes differ")
    require(document["reference"] == dict(result["actual"], sha256=result["sha256"]),
            "CPU reference full output arrays or hashes differ")
    require(document["nondegenerate"] == result["nondegenerate"], "CPU reference history classification differs")
    return {"passed": True, "rounds": config["T"], "completed_rounds": result["actual"]["completed_rounds"],
            "sha256": result["sha256"], "nondegenerate": result["nondegenerate"],
            "scope": "Two independent standard-library references agree; no GPU or timing evidence"}
