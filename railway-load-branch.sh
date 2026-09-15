#!/usr/bin/env sh
# Load a branch from the Azure repo into THIS Railway repo as a deployable branch.
#
#   sh railway-load-branch.sh <azure-branch> [railway-branch-name]
#
# Examples:
#   sh railway-load-branch.sh feature/molina-claim-status
#   sh railway-load-branch.sh feature/aetna-claim-status aetna
#
# It exports the branch's tracked files, strips the committed Google key,
# re-applies the $PORT Dockerfile patch, ignores secrets, and commits on a
# branch named after it. Then just: git push -u origin <railway-branch-name>
set -e

SRC="../OUTBOUND_AZURE_TELNYX"        # path to the Azure repo (sibling folder)
BRANCH="$1"
NAME="${2:-$(printf '%s' "$BRANCH" | sed 's#.*/##')}"

[ -z "$BRANCH" ] && { echo "usage: sh railway-load-branch.sh <azure-branch> [railway-branch]"; exit 1; }

# keep this helper out of the deploy + safe from the wipe
grep -q '^railway-load-branch.sh$' .gitignore 2>/dev/null || echo 'railway-load-branch.sh' >> .gitignore

echo ">> switching to Railway branch '$NAME'"
git checkout -B "$NAME" >/dev/null 2>&1

echo ">> clearing old tracked files"
git rm -rqf . >/dev/null 2>&1 || true

echo ">> exporting '$BRANCH' from $SRC"
( cd "$SRC" && git archive "$BRANCH" ) | tar -x

echo ">> stripping committed secret (src/secrets)"
rm -rf src/secrets

echo ">> patching Dockerfile to bind Railway \$PORT"
sed -i 's#^CMD \["uvicorn".*#CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-5000}"]#' Dockerfile

echo ">> ensuring secrets/env are gitignored"
grep -q '^src/secrets/$' .gitignore 2>/dev/null || printf 'src/secrets/\n.env\n.env.qa\n.env.prod\n*.env\n' >> .gitignore

git add -A
git commit -qm "Railway deploy: $NAME" || { echo "nothing to commit"; exit 0; }

echo ""
echo "Done. '$BRANCH' is loaded onto Railway branch '$NAME'."
echo "Push it:   git push -u origin $NAME"
echo "Then in Railway: Settings -> Source -> Branch = $NAME  (redeploys)"
