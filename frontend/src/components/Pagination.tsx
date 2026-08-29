interface Props {
  offset: number
  limit: number
  total: number
  hasMore: boolean
  onPrev: () => void
  onNext: () => void
}

export default function Pagination({ offset, limit, total, hasMore, onPrev, onNext }: Props) {
  if (total === 0) return null
  const from = offset + 1
  const to = Math.min(offset + limit, total)

  return (
    <div className="pagination" aria-label="Pagination">
      <button
        type="button"
        className="btn btn-sm"
        disabled={offset === 0}
        onClick={onPrev}
      >
        Prev
      </button>
      <span className="muted pagination-summary">
        Showing {from}–{to} of {total}
      </span>
      <button
        type="button"
        className="btn btn-sm"
        disabled={!hasMore}
        onClick={onNext}
      >
        Next
      </button>
    </div>
  )
}
