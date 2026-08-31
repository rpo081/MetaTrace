"""Query-side search: embed the uploaded image and rank it against FAISS."""
from __future__ import annotations

import hashlib

from . import db, embeddings


class SearchService:
    def __init__(self, indexer, settings):
        self.indexer = indexer
        self.settings = settings

    @staticmethod
    def _result_from_row(row, *, score: float, exact: bool, source: str) -> dict:
        # Compile guard: row is ImageDTO (attribute access), not sqlite3.Row
        row_id = getattr(row, "id", row["id"])  # type: ignore[index]
        return {
            **db.row_to_result(row),
            "score": round(float(score), 4),
            "exact": exact,
            "source": source,
            "thumb_url": f"/api/thumb/{row_id}?v=png1",
            "file_url": f"/api/file/{row_id}",
        }

    def _text_results(self, q: str, k: int) -> list[dict]:
        rows = db.search_by_text(self.settings.db_path, q, limit=k)
        return [
            self._result_from_row(row, score=1.0, exact=False, source="text")
            for row in rows
        ]

    # Cap for image+text AND fan-out to prevent OOM on 200k-scale indexes.
    MAX_SEARCH_FETCH = 5000

    def _image_results(self, image_bytes: bytes, k: int, min_score: float, q: str | None = None) -> tuple[int, bool, list[dict]]:
        s = self.settings

        # Single snapshot before embedding: hold lock only to capture (index, ntotal)
        # atomically, then release for the expensive decode/embed. Re-uses the same
        # snapshot for FAISS search so total/index remain consistent even if a scan
        # publishes a new index in between.
        with self.indexer.lock:
            snapshot = self.indexer.index
            if snapshot is None or int(snapshot.ntotal) == 0:
                return 0, False, []
            total = int(snapshot.ntotal)
            index = snapshot

        img = embeddings.decode_image(image_bytes)
        vector = embeddings.embed_images([img], s)[0]
        sha = hashlib.sha256(image_bytes).hexdigest()

        if total == 0:
            return 0, False, []

        # For image+text AND we must not truncate before applying the text filter,
        # but cap to avoid fetching 200k vectors (~400MB) into RAM.
        if q:
            fetch = min(total, self.MAX_SEARCH_FETCH)
        else:
            fetch = min(total, max(k * 3, k + 10))
        scores, ids = index.search(vector.reshape(1, -1).astype("float32"), fetch)

        rows = db.fetch_by_ids(s.db_path, [int(i) for i in ids[0] if i >= 0])
        # DTO compile guard: use attribute access
        by_id = {int(getattr(r, "id", r["id"])): r for r in rows}  # type: ignore[index]

        ranked: list[dict] = []
        exact_hit: dict | None = None
        seen_ids: set[int] = set()
        for sid, score in zip(ids[0], scores[0]):
            row = by_id.get(int(sid))
            if row is None or int(sid) in seen_ids:
                continue
            if q and not db.matches_text(row, q):
                continue
            seen_ids.add(int(sid))
            item = self._result_from_row(row, score=float(score), exact=False, source="image")
            sha_val = getattr(row, "sha256", row["sha256"] if isinstance(row, dict) else None)  # type: ignore[index]
            if sha_val and sha_val == sha and exact_hit is None:
                item["exact"] = True
                item["score"] = 1.0
                exact_hit = item
            else:
                ranked.append(item)

        results = ([exact_hit] if exact_hit else []) + [
            r for r in ranked if r["score"] >= min_score
        ]
        return total, exact_hit is not None, results[:k]

    @staticmethod
    def _merge_or_results(image_results: list[dict], text_results: list[dict], k: int) -> list[dict]:
        merged: dict[int, dict] = {}

        for item in text_results:
            merged[int(item["id"])] = item.copy()

        for item in image_results:
            existing = merged.get(int(item["id"]))
            if existing is None:
                merged[int(item["id"])] = item.copy()
                continue
            existing["source"] = "both"
            existing["score"] = max(float(existing.get("score", 0.0)), float(item.get("score", 0.0)))
            existing["exact"] = bool(existing.get("exact")) or bool(item.get("exact"))

        priority = {"both": 0, "image": 1, "text": 2}
        return sorted(
            merged.values(),
            key=lambda item: (
                priority.get(str(item.get("source")), 3),
                -float(item.get("score", 0.0)),
                str(item.get("rel_path", "")),
            ),
        )[:k]

    def search(
        self,
        image_bytes: bytes | None = None,
        k: int = 5,
        min_score: float = 0.0,
        q: str | None = None,
        combine: str = "and",
    ) -> dict:
        q_clean = q.strip() if q and q.strip() else None
        combine_mode = combine.lower()
        if combine_mode not in {"and", "or"}:
            raise ValueError("invalid combine mode")

        if not image_bytes:
            if not q_clean:
                return {"total_indexed": self.indexer.count, "exact_match": False, "results": []}
            total = db.count(self.settings.db_path)
            return {
                "total_indexed": total,
                "exact_match": False,
                "results": self._text_results(q_clean, k),
            }

        total, exact_match, image_results = self._image_results(
            image_bytes,
            k,
            min_score,
            q_clean if combine_mode == "and" else None,
        )

        if not q_clean:
            return {
                "total_indexed": total,
                "exact_match": exact_match,
                "results": image_results,
            }

        if combine_mode == "and":
            for item in image_results:
                item["source"] = "both"
            return {
                "total_indexed": total,
                "exact_match": exact_match,
                "results": image_results,
            }

        text_results = self._text_results(q_clean, k)
        return {
            "total_indexed": total,
            "exact_match": exact_match,
            "results": self._merge_or_results(image_results, text_results, k),
        }
