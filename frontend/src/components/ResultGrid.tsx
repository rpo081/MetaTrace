import type { SearchResult } from '../types'

interface Props {
  results: SearchResult[]
  selectedId: number | null
  onSelect: (r: SearchResult) => void
}

function basename(p: string): string {
  return p.split(/[\\/]/).pop() ?? p
}

function sourceLabel(source: SearchResult['source']): string | null {
  if (source === 'both') return 'IMAGE + TEXT'
  if (source === 'image') return 'IMAGE'
  if (source === 'text') return 'TEXT'
  return null
}

export default function ResultGrid({ results, selectedId, onSelect }: Props) {
  return (
    <div className="result-grid">
      {results.map((r) => {
        const isSelected = selectedId === r.id
        return (
          <button
            key={r.id}
            type="button"
            className={`card ${isSelected ? 'card-selected' : ''}`}
            aria-pressed={isSelected}
            aria-label={`${basename(r.rel_path)} — ${Math.round(r.score * 100)}% match${
              r.exact ? ', exact copy' : ''
            }${sourceLabel(r.source) ? `, ${sourceLabel(r.source)?.toLowerCase()}` : ''}`}
            onClick={() => onSelect(r)}
          >
            <span className="card-img-wrap">
              <img src={r.thumb_url} loading="lazy" alt="" />
              {r.exact && <span className="badge badge-exact">EXACT</span>}
              {sourceLabel(r.source) && <span className="badge badge-source">{sourceLabel(r.source)}</span>}
              <span className="badge badge-score">{Math.round(r.score * 100)}%</span>
            </span>
            <span className="card-footer">
              <span className="card-name" title={r.original_path}>
                {basename(r.rel_path)}
              </span>
              <span className="score-bar" aria-hidden>
                <span style={{ width: `${Math.max(0, Math.min(1, r.score)) * 100}%` }} />
              </span>
            </span>
          </button>
        )
      })}
    </div>
  )
}
