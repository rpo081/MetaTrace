import { useCallback } from 'react'
import type { BrowseImage, SearchResult } from '../types'

type ResultItem = SearchResult | BrowseImage

interface Props {
  results: ResultItem[]
  selectedId: number | null
  onSelect: (r: ResultItem) => void
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
        const hasScore = 'score' in r && r.score != null
        const score = hasScore ? (r as SearchResult).score : 0
        const exact = 'exact' in r ? (r as SearchResult).exact : false
        const source = 'source' in r ? (r as SearchResult).source : undefined
        return (
          <button
            key={r.id}
            type="button"
            className={`card ${isSelected ? 'card-selected' : ''}`}
            aria-pressed={isSelected}
            aria-label={`${basename(r.rel_path)}${
              hasScore ? ` — ${Math.round(score * 100)}% match` : ''
            }${exact ? ', exact copy' : ''}${
              sourceLabel(source) ? `, ${sourceLabel(source)?.toLowerCase()}` : ''
            }`}
            onClick={() => onSelect(r)}
          >
            <span className="card-img-wrap">
              <img src={r.thumb_url} loading="lazy" alt="" onError={(e) => { e.currentTarget.style.display = 'none' }} />
              {exact && <span className="badge badge-exact">EXACT</span>}
              {sourceLabel(source) && <span className="badge badge-source">{sourceLabel(source)}</span>}
              {hasScore && <span className="badge badge-score">{Math.round(score * 100)}%</span>}
            </span>
            <span className="card-footer">
              <span className="card-name" title={r.original_path}>
                {basename(r.rel_path)}
              </span>
              {hasScore && (
                <span className="score-bar" aria-hidden>
                  <span style={{ width: `${Math.max(0, Math.min(1, score)) * 100}%` }} />
                </span>
              )}
            </span>
          </button>
        )
      })}
    </div>
  )
}
