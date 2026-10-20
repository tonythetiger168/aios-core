"""Memory search for aios-core: keyword ranking (BM25) plus optional vectors.

* Keyword search needs nothing: pure Python, works for English and CJK (Chinese is split into
  overlapping two-character pieces, so "小明的生日" can find a note about "生日").
* Vector search is optional: point `Embedder` at any OpenAI-compatible /embeddings endpoint
  (Ollama, LM Studio, vLLM, OpenAI). Vectors are cached in aios.db per model and recomputed when a
  note changes. NOTE: with a remote endpoint, your notes' text is sent to that service.
* The two rankings are merged with reciprocal-rank fusion.
"""
import asyncio
import hashlib
import json
import math
import re
import urllib.error
import urllib.request
from collections import Counter

K1, B = 1.5, 0.75
RRF_K = 60
MIN_COSINE = 0.25  # a heuristic: vector hits below this are treated as unrelated
EMBED_BATCH = 64

_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]+")
_WORD = re.compile(r"[^\W_]+")  # letters/digits; "_" and "-" separate words (favorite_color)


def tokenize(text: str) -> list[str]:
    text = str(text).lower()
    toks = []
    for run in _CJK.findall(text):
        toks.extend([run] if len(run) == 1 else (run[i:i + 2] for i in range(len(run) - 1)))
    toks.extend(_WORD.findall(_CJK.sub(" ", text)))
    return toks


def bm25(docs: list[list[str]], query: list[str]) -> list[tuple[int, float]]:
    """[(doc index, score)] for documents matching at least one query token, best first."""
    n = len(docs)
    if not n or not query:
        return []
    avg = sum(len(d) for d in docs) / n or 1.0
    df = Counter(t for d in docs for t in set(d))
    out = []
    for i, d in enumerate(docs):
        tf, score = Counter(d), 0.0
        for t in set(query):
            if t in tf:
                idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                score += idf * tf[t] * (K1 + 1) / (tf[t] + K1 * (1 - B + B * len(d) / avg))
        if score > 0:
            out.append((i, score))
    return sorted(out, key=lambda x: -x[1])


def _doc(row: dict) -> str:
    return f"{row['key']} {row['key']} {row['value']}"  # the key is usually the best hint: count it twice


def keyword_rank(rows: list[dict], query: str) -> list[tuple[int, float]]:
    return bm25([tokenize(_doc(r)) for r in rows], tokenize(query))


def select(rows: list[dict], query: str, limit: int) -> list[dict]:
    """The `limit` notes to show in a prompt: best keyword matches for `query`, then the most
    recently saved ones to fill up. Original order is kept so the prompt stays stable."""
    if len(rows) <= limit:
        return rows
    chosen = [i for i, _ in keyword_rank(rows, query)][:limit]
    if len(chosen) < limit:
        taken = set(chosen)
        recent = sorted((i for i in range(len(rows)) if i not in taken),
                        key=lambda i: rows[i]["ts"] or "", reverse=True)
        chosen += recent[:limit - len(chosen)]
    return [rows[i] for i in sorted(chosen)]


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class Embedder:
    """OpenAI-compatible /embeddings client (standard library only)."""

    def __init__(self, model: str, base_url: str = "http://localhost:11434/v1",
                 api_key: str | None = None, timeout: int = 120):
        self.model, self.base_url = model, base_url.rstrip("/")
        self.api_key, self.timeout = api_key, timeout

    def _post(self, payload: dict) -> dict:
        req = urllib.request.Request(
            f"{self.base_url}/embeddings", data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {})})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"embedding server returned HTTP {e.code}: "
                               f"{e.read().decode('utf-8', 'replace')[:300]}") from None
        except urllib.error.URLError as e:
            raise RuntimeError(f"cannot reach embedding server at {self.base_url}: {e.reason}") from None

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), EMBED_BATCH):
            chunk = texts[i:i + EMBED_BATCH]
            resp = await asyncio.to_thread(self._post, {"model": self.model, "input": chunk})
            data = sorted(resp.get("data") or [], key=lambda d: d.get("index", 0))
            vecs = [[float(x) for x in d["embedding"]] for d in data]
            if len(vecs) != len(chunk):
                raise RuntimeError(f"embedding server returned {len(vecs)} vectors for {len(chunk)} texts")
            out.extend(vecs)
        if len({len(v) for v in out}) > 1 or any(not v for v in out):
            raise RuntimeError("embedding server returned vectors of inconsistent size")
        return out


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


async def _vector_rank(store, agent: str, rows: list[dict], query: str, embedder: Embedder):
    texts = [f"{r['key']}: {r['value']}" for r in rows]
    cached = store.get_vectors(agent, embedder.model)  # key -> (sha, vector)
    missing = [i for i, r in enumerate(rows) if cached.get(r["key"], (None, None))[0] != _sha(texts[i])]
    if missing:
        for i, vec in zip(missing, await embedder.embed([texts[i] for i in missing])):
            store.put_vector(agent, rows[i]["key"], embedder.model, _sha(texts[i]), vec)
            cached[rows[i]["key"]] = (_sha(texts[i]), vec)
    (qv,) = await embedder.embed([query])
    scored = [(i, cosine(qv, cached[r["key"]][1])) for i, r in enumerate(rows)]
    return sorted([(i, s) for i, s in scored if s >= MIN_COSINE], key=lambda x: -x[1])


async def search(store, agent: str, query: str, limit: int = 5,
                 embedder: Embedder | None = None) -> tuple[list[dict], str]:
    """Best matching notes for `query`: ([{key, value, source, ts, score}], note)."""
    rows = store.fact_rows(agent)
    if not rows or not str(query).strip():
        return [], ""
    fused: dict[int, float] = {}
    for rank, (i, _) in enumerate(keyword_rank(rows, query)):
        fused[i] = fused.get(i, 0.0) + 1 / (RRF_K + rank + 1)
    note = ""
    if embedder is not None:
        try:
            for rank, (i, _) in enumerate(await _vector_rank(store, agent, rows, query, embedder)):
                fused[i] = fused.get(i, 0.0) + 1 / (RRF_K + rank + 1)
        except Exception as e:  # keyword results are still useful
            note = f"(vector search unavailable: {e}; showing keyword matches only)"
    best = sorted(fused, key=lambda i: -fused[i])[:limit]
    return [{**rows[i], "score": round(fused[i] * RRF_K, 3)} for i in best], note
