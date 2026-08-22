"""Query-side search: embed the uploaded image and rank it against FAISS."""
from __future__ import annotations

import hashlib

from . import db, embeddings


class SearchService:
    def __init__(self, indexer, settings):
        self.indexer = indexer
        self.settings = settings

    def search(self, image_bytes: bytes, k: int, min_score: float) -> dict:
        s = self.settings

        # Early-out only; the authoritative snapshot happens after embedding.
        with self.indexer.lock:
            index = self.indexer.index
            if index is None or int(index.ntotal) == 0:
                return {"total_indexed": 0, "exact_match": False, "results": []}

        img = embeddings.decode_image(image_bytes)
        vector = embeddings.embed_images([img], s)[0]
        sha = hashlib.sha256(image_bytes).hexdigest()

        # Snapshot the index reference under a brief lock, then run the FAISS
        # query WITHOUT holding Indexer._lock. Scans build on a private working
        # copy and swap the new index in atomically, so the snapshotted object
        # is never mutated after publication — flat-index reads are safe.
        with self.indexer.lock:
            index = self.indexer.index
        total = int(index.ntotal) if index is not None else 0
        if total == 0:
            return {"total_indexed": 0, "exact_match": False, "results": []}
        fetch = min(total, max(k * 3, k + 10))
        scores, ids = index.search(vector.reshape(1, -1).astype("float32"), fetch)

        rows = db.fetch_by_ids(s.db_path, [int(i) for i in ids[0] if i >= 0])
        by_id = {int(r["id"]): r for r in rows}

        ranked: list[dict] = []
        exact_hit: dict | None = None
        seen_ids: set[int] = set()
        for sid, score in zip(ids[0], scores[0]):
            row = by_id.get(int(sid))
            if row is None or int(sid) in seen_ids:
                continue
            seen_ids.add(int(sid))
            item = {
                **db.row_to_result(row),
                "score": round(float(score), 4),
                "exact": False,
                "thumb_url": f"/api/thumb/{row['id']}",
                "file_url": f"/api/file/{row['id']}",
            }
            if row["sha256"] and row["sha256"] == sha and exact_hit is None:
                item["exact"] = True
                item["score"] = 1.0
                exact_hit = item
            else:
                ranked.append(item)

        results = ([exact_hit] if exact_hit else []) + [
            r for r in ranked if r["score"] >= min_score
        ]
        return {
            "total_indexed": total,
            "exact_match": exact_hit is not None,
            "results": results[:k],
        }
