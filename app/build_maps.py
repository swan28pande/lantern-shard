"""Docker build step: turn the committed JSON maps into a pickle the selector loads fast."""
import gzip, json, pickle, sys

src, dst = sys.argv[1], sys.argv[2]
with gzip.open(src, "rt", encoding="utf-8") as f:
    d = json.load(f)
d["lines"] = {f: {int(k): v for k, v in fl.items()} for f, fl in d["lines"].items()}
d["refs"] = {k: [tuple(x) for x in v] for k, v in d["refs"].items()}
with open(dst, "wb") as f:
    pickle.dump(d, f, protocol=pickle.HIGHEST_PROTOCOL)
print("maps ok", len(d["test_ids"]))
