#!/usr/bin/env sh
# Load a branch from the Azure repo into THIS Railway repo, applying all the
# Railway build fixes (bookworm+libssl3 Dockerfile, $PORT, Speech SDK 1.51.2,
# UTF-8 requirements) and stripping the committed Google key.
#   sh railway-load-branch.sh <azure-branch> [railway-branch-name]
set -e
SRC="../OUTBOUND_AZURE_TELNYX"
BRANCH="$1"
NAME="${2:-$(printf '%s' "$BRANCH" | sed 's#.*/##')}"
[ -z "$BRANCH" ] && { echo "usage: sh railway-load-branch.sh <azure-branch> [railway-branch]"; exit 1; }

grep -q '^railway-load-branch.sh$' .gitignore 2>/dev/null || echo 'railway-load-branch.sh' >> .gitignore
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
  sed -i 's#^FROM .*python:3.11-slim-bullseye#FROM python:3.11-slim-bookworm#' Dockerfile
  sed -i 's#libssl1.1#libssl3#' Dockerfile
  sed -i 's#^CMD \["uvicorn".*#CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-5000}"]#' Dockerfile
fi
echo ">> requirements.txt -> UTF-8 + Speech SDK 1.51.2"
python - <<'PY'
data = open('requirements.txt','rb').read()
enc = 'utf-16' if data[:2] in (b'\xff\xfe', b'\xfe\xff') else 'utf-8'
text = data.decode(enc).replace('azure-cognitiveservices-speech==1.34.0','azure-cognitiveservices-speech==1.51.2')
lines = [l.rstrip('\r') for l in text.splitlines()]
open('requirements.txt','w',encoding='utf-8',newline='\n').write('\n'.join(lines)+'\n')
PY
grep -q '^src/secrets/$' .gitignore 2>/dev/null || printf 'src/secrets/\n.env\n.env.qa\n.env.prod\n*.env\n' >> .gitignore
git add -A
git commit -qm "Railway deploy: $NAME" || { echo "nothing to commit"; exit 0; }
echo "Done. Push:  git push -u origin $NAME"
