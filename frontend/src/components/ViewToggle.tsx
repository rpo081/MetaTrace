import type { ViewMode } from '../types'
import { GridIcon, ListIcon } from './Icon'

interface Props {
  mode: ViewMode
  onChange: (mode: ViewMode) => void
}

export default function ViewToggle({ mode, onChange }: Props) {
  return (
    <div className="view-toggle" role="group" aria-label="View mode">
      <button
        type="button"
        className={`view-toggle-btn ${mode === 'grid' ? 'view-toggle-btn-active' : ''}`}
        onClick={() => onChange('grid')}
        aria-pressed={mode === 'grid'}
        title="Grid view"
        aria-label="Grid view"
      >
        <GridIcon width="16" height="16" />
      </button>
      <button
        type="button"
        className={`view-toggle-btn ${mode === 'list' ? 'view-toggle-btn-active' : ''}`}
        onClick={() => onChange('list')}
        aria-pressed={mode === 'list'}
        title="List view"
        aria-label="List view"
      >
        <ListIcon width="16" height="16" />
      </button>
    </div>
  )
}
