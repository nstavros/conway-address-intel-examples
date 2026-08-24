#!/bin/zsh
# ENGAGE LLM backend: reads prompt on stdin, writes completion on stdout.
# Used both for draft generation and the fail-closed LLM gate judge.
exec claude -p --model claude-sonnet-5
