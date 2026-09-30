#!/bin/bash
# session-env-check.sh

echo "── Environment check ────────────────────────────"

if [ -n "$MAX_THINKING_TOKENS" ]; then
  echo "⚠️  MAX_THINKING_TOKENS is set to $MAX_THINKING_TOKENS (should be unset)"
else
  echo "✓  MAX_THINKING_TOKENS: unset (adaptive thinking active)"
fi

if [ -n "$CLAUDE_CODE_MAX_OUTPUT_TOKENS" ]; then
  echo "✓  CLAUDE_CODE_MAX_OUTPUT_TOKENS: $CLAUDE_CODE_MAX_OUTPUT_TOKENS"
else
  echo "⚠️  CLAUDE_CODE_MAX_OUTPUT_TOKENS: not set (will use default)"
fi

echo "────────────────────────────────────────────────"
