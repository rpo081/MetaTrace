---
name: architecture-guide
description: Project structure conventions, layering rules, and patterns
---
## Project Structure
```
src/
  features/     # Feature modules (co-located: component, hook, API, test)
  shared/       # Shared utilities, types, constants
  app/          # App shell, routing, global providers
  config/       # Configuration and environment handling
```

## Layering Rules
- **UI layer** → components, hooks, pages — NO direct data access
- **Data layer** → API clients, database queries, storage — NO UI logic
- **Business logic layer** → services, use cases — independent of UI and data
- Dependencies flow inward: UI → Business Logic → Data

## API Design
- RESTful endpoints: plural nouns, HTTP verbs for actions
- Pagination: cursor-based for lists, offset-based for admin
- Error responses: consistent format with `{ error, code, details }`
- Version via URL prefix: `/api/v1/`

## State Management
- Server state: React Query / SWR / TanStack Query
- Client state: Context for global, local state for component
- Avoid prop drilling beyond 3 levels
- Normalize complex nested data stores
