"""How good are the recommendations? Compares embedding models on the companies in Postgres.

Run `just evaluate` after the consumer has loaded the feed. There are no human relevance labels, so this
uses two proxies:

- **Industry precision@4**: the share of the 4 recommended cards in the same industry as the company. The
  industry is never shown to the model (it only sees the description), so it is a fair, if rough, label.
- **Different-text precision@4**: the same, but only counting neighbours whose description is not identical
  to the company's. Many companies share an exact description, and any model ranks those first; this
  isolates whether the model understands meaning.
- **Cross-language rate**: of those different-text neighbours, the share written in the other language
  (English vs Malay). About 30% of descriptions are Malay translations of the English ones, so a model that
  understands both should often cross; one that only matches surface words will not.

Ranking is exact cosine similarity in NumPy with ties broken by registration number, i.e. the same order
the service returns (the HNSW index only approximates it).
"""

import hashlib
import re
from dataclasses import dataclass

import numpy as np
import psycopg
from fastembed import TextEmbedding

from app.config import load_settings

K = 4
MODELS = ("BAAI/bge-small-en-v1.5", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
MALAY_WORDS = re.compile(
    r"\b(dan|di|untuk|ke|daripada|perkhidmatan|pengeluaran|kedai|klinik|tadika|ladang|kebun|pusat|agensi|"
    r"pembuatan|pengilangan|penjualan|penghantaran|pengusaha|perunding|farmasi|restoran|mengusahakan|kerja-kerja|"
    r"kontraktor|pengubahsuaian|penternakan|pembangunan|perniagaan|hotel butik)\b",
    re.IGNORECASE,
)
EXAMPLE = "SYN-000861"  # "Restoran masakan Melayu dan Cina"


@dataclass(frozen=True)
class Company:
    registration_number: str
    description: str
    industry: str | None

    @property
    def is_malay(self) -> bool:
        return bool(MALAY_WORDS.search(self.description))


def load_companies() -> list[Company]:
    with psycopg.connect(load_settings().database_url) as connection:
        rows = connection.execute(
            "SELECT registration_number, description, industry FROM companies"
            " WHERE status = 'active' AND description IS NOT NULL ORDER BY registration_number"
        ).fetchall()
    return [Company(*row) for row in rows]


def hashed_bag_of_words(texts: list[str]) -> np.ndarray:
    """Lexical baseline: only shared words count, no meaning."""
    vectors = np.zeros((len(texts), 384))
    for row, text in enumerate(texts):
        for word in text.lower().split():
            vectors[row, int.from_bytes(hashlib.sha256(word.encode()).digest()[:4], "big") % 384] += 1
    return vectors / np.linalg.norm(vectors, axis=1, keepdims=True)


def embed(model: str, texts: list[str]) -> np.ndarray:
    if model == "hashed bag-of-words":
        return hashed_bag_of_words(texts)
    vectors = np.array(list(TextEmbedding(model, threads=4).embed(texts)))
    return vectors / np.linalg.norm(vectors, axis=1, keepdims=True)


def evaluate(model: str, companies: list[Company]) -> dict[str, float]:
    texts = sorted({company.description for company in companies})
    text_vectors = embed(model, texts)
    index = {text: row for row, text in enumerate(texts)}
    vectors = text_vectors[[index[company.description] for company in companies]]
    similarity = vectors @ vectors.T  # rows are unit vectors, so this is cosine similarity

    hits, different_hits, crossings, scored = 0, 0, 0, 0
    for row, company in enumerate(companies):
        if company.industry is None:
            continue
        scores = similarity[row].copy()
        scores[row] = -np.inf  # never recommend the company itself
        # Stable sort on -score keeps registration-number order among ties (companies are sorted by it).
        ranked = np.argsort(-scores, kind="stable")
        top = [companies[i] for i in ranked[:K]]
        different = [companies[i] for i in ranked if companies[i].description != company.description][:K]
        hits += sum(other.industry == company.industry for other in top)
        different_hits += sum(other.industry == company.industry for other in different)
        crossings += sum(other.is_malay != company.is_malay for other in different)
        scored += 1

    if EXAMPLE in {company.registration_number for company in companies}:
        row = next(i for i, company in enumerate(companies) if company.registration_number == EXAMPLE)
        scores = similarity[row]
        seen, examples = {companies[row].description}, []
        for i in np.argsort(-scores, kind="stable"):
            if companies[i].description not in seen and len(examples) < 3:
                seen.add(companies[i].description)
                examples.append(f"{companies[i].description} ({scores[i]:.2f})")
        print(f"  {model}: nearest other descriptions to {companies[row].description!r}: {examples}")

    return {
        "industry_precision": hits / (scored * K),
        "different_text_precision": different_hits / (scored * K),
        "cross_language_rate": crossings / (scored * K),
    }


def main() -> None:
    companies = load_companies()
    malay = sum(company.is_malay for company in companies)
    print(f"{len(companies)} active companies with a description ({malay} in Malay); k = {K}\n")
    results = {model: evaluate(model, companies) for model in ("hashed bag-of-words", *MODELS)}
    print("\n| Model | Industry P@4 | Different-text P@4 | Cross-language rate |")
    print("|---|---|---|---|")
    for model, metrics in results.items():
        print(
            f"| {model} | {metrics['industry_precision']:.3f} | {metrics['different_text_precision']:.3f}"
            f" | {metrics['cross_language_rate']:.3f} |"
        )


if __name__ == "__main__":
    main()
