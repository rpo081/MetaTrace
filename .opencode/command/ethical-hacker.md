---
description: Delegates a security/ethical-hack audit to the ethical-hacker agent
agent: ethical-hacker
---

Run an ethical-hacking audit of $ARGUMENTS.

The ethical-hacker agent is specialized in security testing, backdoor detection, and zero-day-class vulnerability discovery. It runs industry-standard scanning tools (nmap, nikto, sqlmap, etc.) and analyses code for suspicious patterns.

It will write its findings to `result-ethical-hacker.md` and return a concise summary with severity-labelled findings and remediation hints.

If no argument is given, audit the current working directory and surface a clear scope statement before scanning.
