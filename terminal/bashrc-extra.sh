# Sourced by `bash -l` in the web terminal.

export PATH="$PATH:/usr/local/bin"
export PAGER=less
export EDITOR=nano

# Shorthand for hitting the API without retyping the auth header.
ops() {
  local method="GET" path
  if [ "$1" = "-X" ]; then method="$2"; shift 2; fi
  path="$1"; shift
  curl -s -X "$method" \
    -H "X-API-Token: $OPSDECK_TOKEN" \
    -H 'Content-Type: application/json' \
    "$OPSDECK_URL/api${path}" "$@"
}

alias ops-context='ops /context | jq .'
alias ops-pending='ops /attempts?status=awaiting_questions | jq .'
alias ops-notes='ops "/notes/quick?status=pending" | jq .'

if [ -t 1 ]; then
  printf '\n\033[38;5;179m  ops deck :: claude code\033[0m\n'
  printf '  workspace persists · run \033[1mclaude\033[0m to start\n'
  printf '  helpers: \033[2mops /today\033[0m · \033[2mops-context\033[0m · \033[2mops-notes\033[0m · \033[2mops-pending\033[0m\n\n'
fi
