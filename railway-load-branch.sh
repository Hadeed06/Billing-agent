#!/usr/bin/env sh
# Load a branch from the Azure repo into THIS Railway repo as a deployable branch.
#
#   sh railway-load-branch.sh <azure-branch> [railway-branch-name]
#
# Examples:
#   sh railway-load-branch.sh feature/molina-claim-status
#   sh railway-load-branch.sh feature/aetna-claim-status aetna
#
# It exports the branch's tracked files, strips the committed Google key, and
# re-applies ALL the Railway build fixes (bookworm+libssl3 Dockerfile, $PORT,
# Speech SDK 1.51.2, UTF-8 requirements). Then: git push -u origin <name>, and
# in Railway set Settings -> Source -> Branch to <name>.
set -e

SRC="../OUTBOUND_AZURE_TELNYX"        # path to the Azure repo (sibling folder)
BRANCH="$1"
NAME="${2:-$(printf '%s' "$BRANCH" | sed 's#.*/##')}"

[ -z "$BRANCH" ] && { echo "usage: sh railway-load-branch.sh <azure-branch> [railway-branch]"; exit 1; }

# keep this helper (and the good Dockerfile we stash) out of the wipe/commit
grep -q '^railway-load-branch.sh$' .gitignore 2>/dev/null || echo 'railway-load-branch.sh' >> .gitignore

# stash the current KNOWN-GOOD bookworm Dockerfile — it is branch-agnostic, so
# we restore it after extracting the branch (whose Dockerfile is old bullseye).
cp Dockerfile /tmp/railway-good-dockerfile 2>/dev/null || true

echo ">> switching to Railway branch '$NAME'"
git checkout -B "$NAME" >/dev/null 2>&1

echo ">> clearing old tracked files"
git rm -rqf . >/dev/null 2>&1 || true

echo ">> exporting '$BRANCH' from $SRC"
( cd "$SRC" && git archive "$BRANCH" ) | tar -x

echo ">> stripping committed secret (src/secrets)"
rm -rf src/secrets

echo ">> restoring bookworm + \$PORT Dockerfile"
if [ -f /tmp/railway-good-dockerfile ]; then
  cp /tmp/railway-good-dockerfile Dockerfile
else
  # fallback: patch the branch's Dockerfile in place
  sed -i 's#^FROM .*python:3.11-slim-bullseye#FROM python:3.11-slim-bookworm#' Dockerfile
  sed -i 's#libssl1.1#libssl3#' Dockerfile
  sed -i 's#^CMD \["uvicorn".*#CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-5000}"]#' Dockerfile
fi

echo ">> requirements.txt -> UTF-8 + Speech SDK 1.51.2"
python - <<'PY'
data = open('requirements.txt','rb').read()
enc = 'utf-16' if data[:2] in (b'\xff\xfe', b'\xfe\xff') else 'utf-8'
text = data.decode(enc)
text = text.replace('azure-cognitiveservices-speech==1.34.0',
                    'azure-cognitiveservices-speech==1.51.2')
lines = [l.rstrip('\r') for l in text.splitlines()]
open('requirements.txt','w',encoding='utf-8',newline='\n').write('\n'.join(lines)+'\n')
PY

echo ">> ensuring secrets/env are gitignored"
grep -q '^src/secrets/$' .gitignore 2>/dev/null || printf 'src/secrets/\n.env\n.env.qa\n.env.prod\n*.env\n' >> .gitignore

git add -A
git commit -qm "Railway deploy: $NAME" || { echo "nothing to commit"; exit 0; }

echo ""
echo "Done. '$BRANCH' is loaded onto Railway branch '$NAME' (bookworm + SDK 1.51.2)."
echo "Push it:   git push -u origin $NAME"
echo "Then in Railway: Settings -> Source -> Branch = $NAME  (redeploys)"
