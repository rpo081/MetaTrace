---
name: security-practices
description: Security standards covering OWASP Top 10, authentication, and data protection
---
## Authentication & Authorization
- Hash passwords with bcrypt (cost factor >= 12) or argon2id
- Use JWT with short expiration (15 min access, 7 day refresh)
- Validate all authorization checks server-side, not just client-side
- Implement rate limiting on auth endpoints (5 attempts per minute)

## Input Validation
- Validate and sanitize ALL user input on the server
- Use parameterized queries for all database operations
- Never concatenate user input into shell commands
- Validate file uploads: type, size, content verification

## Data Protection
- Never log sensitive data (passwords, tokens, PII)
- Encrypt sensitive data at rest (AES-256-GCM)
- Store secrets in environment variables, never in code
- Use HTTPS only; set Strict-Transport-Security header

## Dependencies
- Pin dependency versions in lock files
- Run `npm audit` / `pip audit` regularly
- No deprecated or unmaintained packages
- Review dependency licenses for compliance
