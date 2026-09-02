---
name: design-system
description: UI/UX design standards, component patterns, and accessibility guidelines
---
## Design Standards
- Follow existing component library conventions (Material UI, Shadcn, Tailwind classes)
- Use design tokens for colors, spacing, typography, shadows
- Maintain consistent spacing using 4px/8px grid system
- Typography: use system font stack unless otherwise specified

## Accessibility Requirements
- All interactive elements must be keyboard accessible
- Color contrast ratio minimum 4.5:1 for normal text, 3:1 for large text
- Form inputs must have associated labels
- Error messages must be programmatically associated with inputs
- Touch targets minimum 44x44px on mobile

## Component Patterns
- Atomic design: atoms -> molecules -> organisms -> templates -> pages
- Compose small components rather than creating large monolithic ones
- Export components with TypeScript interfaces for all props
- Use forwardRef for reusable interactive components
