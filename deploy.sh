#!/bin/bash

set -e

# === CONFIG ===
APP_NAME=pehr-ai-billing-agent

# QA Environment
QA_ACR_NAME=ACRPehrAppsCUS
QA_REGISTRY=acrpehrappscus-dadmdwhuamagdsgt.azurecr.io
QA_TAG=qa

# PROD Environment
PROD_ACR_NAME=ACRPehrAppsCUS
PROD_REGISTRY=acrpehrappscus-dadmdwhuamagdsgt.azurecr.io
PROD_TAG=prod

# === INPUT CHECK ===
if [[ "$1" != "qa" && "$1" != "prod" ]]; then
  echo "❌ Usage: ./deploy.sh [qa|prod]"
  exit 1
fi

ENV_FILE=".env"
# === SELECT ENV CONFIG ===
if [[ "$1" == "qa" ]]; then
  ACR_NAME=$QA_ACR_NAME
  REGISTRY=$QA_REGISTRY
  TAG=$QA_TAG
  ENV_FILE=".env.qa"
  az account set --subscription "Sub-PEHR-Prod"
elif [[ "$1" == "prod" ]]; then
  ACR_NAME=$PROD_ACR_NAME
  REGISTRY=$PROD_REGISTRY
  TAG=$PROD_TAG
  ENV_FILE=".env.prod"
  az account set --subscription "Sub-PEHR-Prod"
fi

IMAGE_NAME=$REGISTRY/$APP_NAME:$TAG

# === BUMP PATCH VERSION (portable) ===
if [[ -f $ENV_FILE ]]; then
  echo "📋 Updating APP_VERSION in $ENV_FILE"

  # Read current version (tolerate spaces and CRLF)
  CURRENT_VERSION=$(grep -E '^APP_VERSION[[:space:]]*=' "$ENV_FILE" | head -n1 | cut -d'=' -f2- | tr -d ' \r')
  [[ -z "$CURRENT_VERSION" ]] && CURRENT_VERSION="0.0.0"

  IFS='.' read -r MAJOR MINOR PATCH <<< "$CURRENT_VERSION"
  MAJOR=${MAJOR:-0}; MINOR=${MINOR:-0}; PATCH=${PATCH:-0}
  NEW_VERSION="$MAJOR.$MINOR.$((PATCH + 1))"

  # Rewrite file without using in-place edits (portable across BSD/GNU/Windows Git Bash)
  TMP_FILE="${ENV_FILE}.tmp.$$"
  awk -v newv="$NEW_VERSION" '
    BEGIN{updated=0}
    /^APP_VERSION[[:space:]]*=/ {
      print "APP_VERSION=" newv
      updated=1
      next
    }
    { print }
    END{
      if(!updated){
        # If APP_VERSION was missing, append it
        print "APP_VERSION=" newv
      }
    }
  ' "$ENV_FILE" > "$TMP_FILE" && mv "$TMP_FILE" "$ENV_FILE"

  echo "   → $CURRENT_VERSION → $NEW_VERSION"
else
  echo "❌ Missing expected file: $ENV_FILE"
  exit 1
fi

# === COPY ENV FILE ===
echo "📋 Using $ENV_FILE → .env for build"
cp $ENV_FILE .env

# === BUILD + PUSH ===
echo "🔐 Logging in to ACR: $ACR_NAME"
az acr login --name $ACR_NAME

echo "🐳 Building image: $IMAGE_NAME"
docker build -t $IMAGE_NAME .

echo "📤 Pushing image: $IMAGE_NAME"
docker push $IMAGE_NAME

echo "🧹 Clean up local .env file"
rm -f .env

echo "✅ Deployment complete: $IMAGE_NAME (version $NEW_VERSION)"
