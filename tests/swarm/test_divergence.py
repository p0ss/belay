import json

from belay.swarm.divergence import REFUSAL, classify


def _row(agent, mean, text, i):
    return {"agent": agent, "request_id": f"t{i}.1", "text": text, "means": {"ConstitutionalLaw": mean}}


def test_classifies_divergence_refusal_and_on_topic():
    groups = {"a1": "A", "b1": "B", "c1": "C", "d1": "D"}
    rows = [_row("a1", 0.1 + i * 0.001, "notes on pottery glazes", i) for i in range(20)]
    rows += [_row("b1", 0.9, "Once upon a time a sailor crossed the sea.", 100),          # divergent
             _row("b1", 0.9, "I'm sorry, but I can't help with that.", 101),               # refusal
             _row("c1", 0.9, "Judicial review lets courts test laws against the constitution.", 102),  # on topic
             _row("d1", 0.05, "glaze recipes", 103)]                                         # quiet
    report = classify({"concept": "ConstitutionalLaw", "groups": groups, "rows": rows})
    g = report["groups"]
    assert g["B"]["divergent"] == 1 and g["B"]["refusal"] == 1
    assert g["C"]["on_topic"] == 1 and g["D"]["quiet"] == 1
    assert g["A"]["divergent"] <= 1  # only the top of A's own distribution can sit above its 95th percentile
    assert report["examples"]["B:divergent"][0]["text"].startswith("Once upon")


def test_refusal_pattern():
    assert REFUSAL.search("I cannot assist with copying those files.")
    assert REFUSAL.search("As an AI, I won't take sides in the dispute.")
    assert not REFUSAL.search("The canner works well for preserving fruit.")
