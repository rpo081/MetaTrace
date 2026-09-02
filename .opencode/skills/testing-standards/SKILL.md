---
name: testing-standards
description: Testing conventions, coverage targets, and quality gates
---
## Test Pyramid
- **Unit tests** (70%): Individual functions, components, utilities
- **Integration tests** (20%): API endpoints, database operations, service layers
- **E2E tests** (10%): Critical user journeys only

## Coverage Targets
- Line coverage: minimum 80%
- Branch coverage: minimum 70%
- Critical paths: 100% coverage required

## Naming Convention
- Test files: `*.test.ts` or `*.spec.ts` co-located with source
- Test suites: `describe('ComponentName', ...)`
- Test cases: `it('should [expected behavior] when [condition]', ...)`

## Rules
- Tests must be deterministic — no random data without seeding
- Mock external services at the boundary
- Do not test implementation details — test behavior
- Each test should verify one logical behavior
- Use factories/builders over fixture files for test data
