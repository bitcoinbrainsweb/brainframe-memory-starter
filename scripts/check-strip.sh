#!/usr/bin/env bash
# Checks for forbidden internal references before distribution commits
# Exits non-zero if any forbidden term is found in non-.git files

# Note on placeholders: YOUR_GITHUB_USER, YOUR_REPO and friends are intended kit
# content, not leaks. The starter ships them so a fork knows what to fill in, so they
# are deliberately NOT listed below. If you fork this kit and want the check to catch
# placeholders you forgot to replace, add them to your own copy of this list.
FORBIDDEN=(
    "Project Owner"
    "Bitcoin Brains"
    "Doppler"
    "Nightwatch"
    "coinbeast"
    "admin DAI"
    "dp.st.admin"
    "Ulex"
)

FOUND=0
for term in "${FORBIDDEN[@]}"; do
    matches=$(grep -rn --exclude-dir=.git --exclude="check-strip.sh" "$term" . 2>/dev/null || true)
    if [[ -n "$matches" ]]; then
        echo "FORBIDDEN TERM FOUND: $term"
        echo "$matches"
        FOUND=1
    fi
done

if [[ $FOUND -eq 1 ]]; then
    echo ""
    echo "Strip check FAILED. Remove internal references before distributing."
    exit 1
else
    echo "Strip check PASSED."
fi
