"""Embedding providers — vendor-independent, offline by default.

The brief asks for provider-independent vector storage, which begins with
provider-independent *embeddings*. The same discipline as Module 6's LLM
router applies: the pipeline knows it needs a vector of a declared dimension,
and nothing more.

The default is a local, deterministic embedder rather than an API call. That
is not a compromise for the demo — it is the correct default for a platform
that must index a 300-page annual report without a network round-trip per
chunk, and it means the test suite pins exact numbers instead of mocking.

The local model is hashed character n-grams projected into a fixed space —
essentially a random-projection bag-of-n-grams. It captures lexical and
morphological similarity well and semantic paraphrase poorly. That limitation
is real and is why retrieval is *hybrid*: BM25 does the lexical work it is best
at, and the vector supplies fuzzy matching over spelling and inflection. Where
an API key is configured, a genuine semantic model replaces the local one
without any change above this module.
"""
from __future__ import annotations

import hashlib
import math
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar, Sequence

_TOKEN = re.compile(r"[a-z0-9₹%]+")


@dataclass(frozen=True, slots=True)
class EmbeddingSpec:
    """Identity of an embedding space.

    Stored beside every vector. Mixing two spaces in one index produces
    similarity scores that are arithmetically valid and completely meaningless,
    so the store refuses vectors whose spec does not match.
    """

    provider: str
    model: str
    dimension: int

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.model}:{self.dimension}"


class EmbeddingProvider(ABC):
    """Turns text into vectors of a declared dimension."""

    name: ClassVar[str] = "abstract"

    @property
    @abstractmethod
    def spec(self) -> EmbeddingSpec: ...

    @abstractmethod
    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed a batch. Order of the output matches the input."""

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]

    @property
    def available(self) -> bool:
        return True


# ---------------------------------------------------------------------------
def tokenise(text: str) -> list[str]:
    """Lowercase word tokens. Shared by the embedder and the lexical index."""
    return _TOKEN.findall(text.lower())


#: Suffixes stripped by :func:`stem`, longest first so "itional" beats "al".
_SUFFIXES: tuple[str, ...] = (
    "isations", "izations", "isation", "ization", "iveness", "fulness",
    "ability", "ibility", "ational", "tional", "ements", "ements", "ingly",
    "edness", "ement", "ments", "ances", "ences", "ition", "ities", "ively",
    "ature", "ances", "ings", "ment", "ness", "ance", "ence", "ical", "ions",
    "ity", "ies", "ive", "ing", "ers", "est", "ion", "ise", "ize", "ed",
    "es", "ly", "al", "er", "or", "s",
)
#: Words whose stem would be misleading or too short to discriminate.
_NO_STEM = frozenset(
    """gas its as is was has does goes less business analysis basis crisis
    series species address process access loss cross press""".split()
)
_MIN_STEM = 4


#: Irregular pairs a suffix stripper cannot reach. Kept deliberately short —
#: this is a lookup for words where the morphology is genuinely irregular, not
#: a thesaurus.
_IRREGULAR: dict[str, str] = {
    "competitors": "compet", "competitor": "compet", "competition": "compet",
    "competitive": "compet", "competing": "compet", "competes": "compet",
    "compete": "compet",
    "subsidiaries": "subsidiar", "subsidiary": "subsidiar",
    "guidance": "guid", "guide": "guid", "guided": "guid", "guiding": "guid",
    "acquisitions": "acquir", "acquisition": "acquir", "acquired": "acquir",
    "acquire": "acquir", "acquiring": "acquir",
    "liabilities": "liabil", "liability": "liabil",
    "facilities": "facil", "facility": "facil",
    "capacities": "capac", "capacity": "capac",
    "maturities": "matur", "maturity": "matur", "mature": "matur",
    "revenues": "revenue", "revenue": "revenue",
    "borrowings": "borrow", "borrowing": "borrow", "borrowed": "borrow",
    "governance": "govern", "governing": "govern",
    "shareholders": "sharehold", "shareholder": "sharehold",
    "shareholding": "sharehold",
    "directors": "director", "director": "director", "directorship": "director",
    # The four-character floor blocks "rating" → "rat" and "rated" → "rat", so
    # the two never unified and a query for "credit rating" could not reach a
    # document saying "has been rated CRISIL AA+". Central enough vocabulary
    # to be worth naming explicitly rather than lowering the floor for every
    # word, which would over-collapse far more than it fixed.
    "rating": "rating", "ratings": "rating", "rated": "rating",
    "pledge": "pledg", "pledged": "pledg",
    "utilisation": "util", "utilization": "util",
    "utilised": "util", "utilized": "util",
}


def stem(token: str) -> str:
    """Very light suffix stripping, iterated to a fixed point.

    Not a linguistic stemmer and not trying to be. Its whole job is to let a
    question phrased one way retrieve a document phrased another —
    "competitors" against "compete", "guidance" against "guide". Without it the
    index answered "who are the competitors?" with *nothing*, because no filing
    uses the noun the question does.

    Iteration matters. Single-pass stripping is not idempotent: "competitors"
    lost only "s" and became "competitor", while "competition" lost "ion" and
    became "compet" — two different keys for the same concept, which defeats
    the entire purpose. Repeating until stable makes the function a genuine
    normalisation, so ``stem(stem(x)) == stem(x)`` holds and is tested.

    Conservative by construction: never shortens below :data:`_MIN_STEM`
    characters, never touches the exception list. Over-stemming collapses
    distinct financial terms into one another, which is a subtler and worse
    failure than under-stemming.
    """
    if not token.isalpha():
        return token
    if token in _IRREGULAR:
        return _IRREGULAR[token]
    if len(token) <= _MIN_STEM or token in _NO_STEM:
        return token

    current = token
    for _ in range(3):  # bounded; three rounds reaches a fixed point in practice
        stripped = current
        for suffix in _SUFFIXES:
            if current.endswith(suffix) and len(current) - len(suffix) >= _MIN_STEM:
                stripped = current[: -len(suffix)]
                break
        if stripped == current:
            break
        current = stripped
        if current in _NO_STEM:
            break
    return current


def stem_tokens(text: str) -> list[str]:
    """Tokenise then stem — the matching vocabulary used by lexical retrieval."""
    return [stem(token) for token in tokenise(text)]


class HashingEmbeddingProvider(EmbeddingProvider):
    """Deterministic local embedder: hashed word and character n-grams.

    Signed hashing (the sign taken from a second hash bit) keeps the projection
    approximately unbiased; without it every feature would push each dimension
    in the same direction and cosine similarity would collapse toward 1.
    """

    name: ClassVar[str] = "local-hashing"

    def __init__(self, dimension: int = 384, char_ngram: int = 4) -> None:
        self.dimension = dimension
        self.char_ngram = char_ngram
        self._spec = EmbeddingSpec(self.name, f"hash-{char_ngram}g", dimension)

    @property
    def spec(self) -> EmbeddingSpec:
        return self._spec

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        tokens = tokenise(text)
        if not tokens:
            return vector

        for token in tokens:
            self._add(vector, f"w:{token}", 1.0)
        # Word bigrams carry the phrase-level signal a bag of words loses:
        # "operating margin" should not match "operating" plus "margin".
        for first, second in zip(tokens, tokens[1:]):
            self._add(vector, f"b:{first}_{second}", 0.7)
        # Character n-grams give robustness to inflection and OCR damage.
        joined = " ".join(tokens)
        n = self.char_ngram
        for index in range(len(joined) - n + 1):
            self._add(vector, f"c:{joined[index : index + n]}", 0.35)

        return self._normalise(vector)

    def _add(self, vector: list[float], feature: str, weight: float) -> None:
        digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
        index = int.from_bytes(digest[:4], "big") % self.dimension
        sign = 1.0 if digest[4] & 1 else -1.0
        vector[index] += sign * weight

    @staticmethod
    def _normalise(vector: list[float]) -> list[float]:
        norm = math.sqrt(sum(v * v for v in vector))
        if norm == 0.0:
            return vector
        return [v / norm for v in vector]


# ---------------------------------------------------------------------------
class OpenAIEmbeddingProvider(EmbeddingProvider):
    """OpenAI embeddings, used when a key is configured.

    Present so the abstraction is real rather than notional. It has never been
    exercised against the live API here — there is no key in this environment —
    and that is stated plainly rather than implied by its existence.
    """

    name: ClassVar[str] = "openai"
    ENDPOINT = "https://api.openai.com/v1/embeddings"

    def __init__(
        self,
        api_key: str | None,
        model: str = "text-embedding-3-small",
        dimension: int = 1536,
        timeout: float = 30.0,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self._spec = EmbeddingSpec(self.name, model, dimension)

    @property
    def spec(self) -> EmbeddingSpec:
        return self._spec

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not self.available:
            raise RuntimeError("OpenAI embeddings require OPENAI_API_KEY")
        import httpx

        response = httpx.post(
            self.ENDPOINT,
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.model, "input": list(texts)},
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        # Sorted by index because the API does not promise input order.
        rows = sorted(payload["data"], key=lambda d: d["index"])
        return [row["embedding"] for row in rows]


# ---------------------------------------------------------------------------
def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity. Defined once; every ranker uses this one."""
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


_PROVIDERS: dict[str, type[EmbeddingProvider]] = {
    HashingEmbeddingProvider.name: HashingEmbeddingProvider,
    OpenAIEmbeddingProvider.name: OpenAIEmbeddingProvider,
}


def build_embedder(
    provider: str | None = None, *, api_key: str | None = None
) -> EmbeddingProvider:
    """Resolve an embedding provider by name, falling back to the local model.

    Falling back rather than failing is deliberate: an unconfigured key must
    degrade indexing quality, not prevent a user from uploading a document.
    """
    if provider == OpenAIEmbeddingProvider.name and api_key:
        return OpenAIEmbeddingProvider(api_key)
    return HashingEmbeddingProvider()


def available_providers() -> tuple[str, ...]:
    return tuple(sorted(_PROVIDERS))
