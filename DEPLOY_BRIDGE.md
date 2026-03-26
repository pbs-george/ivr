**Overview**
This repo now has two deployable parts:

1. [`function_app.py`](/home/georgea/projects-wsl/ivr/code-incoming_call/function_app.py) as the Azure Function that answers ACS calls.
2. [`bridge_server.py`](/home/georgea/projects-wsl/ivr/code-incoming_call/bridge_server.py) as the Azure Container Apps WebSocket bridge that relays audio between ACS and Azure OpenAI Realtime.

`ACS_MEDIA_STREAMING_URL` must point to the public `wss://...` URL of the container app bridge, not the Function App URL.

**Prereqs**
- Azure CLI installed
- `az login`
- `az extension add --name containerapp --upgrade`
- An Azure Container Registry (`ACR`)
- An Azure Container Apps environment
- Your Azure OpenAI realtime deployment already created

**Build And Push**
Set these variables first:

```bash
RG="<resource-group>"
LOCATION="<location>"
ACR_NAME="<acr-name>"
IMAGE_NAME="acs-realtime-bridge"
IMAGE_TAG="latest"
```

Build and push the bridge image:

```bash
az acr build \
  --registry "$ACR_NAME" \
  --image "$IMAGE_NAME:$IMAGE_TAG" \
  --file Dockerfile.bridge \
  .
```

Get the registry login server:

```bash
ACR_LOGIN_SERVER="$(az acr show --name "$ACR_NAME" --resource-group "$RG" --query loginServer -o tsv)"
```

**Create Or Update The Container App**
Set the remaining variables:

```bash
ACA_ENV="<container-app-environment>"
ACA_NAME="acs-realtime-bridge"
AZURE_OPENAI_ENDPOINT="https://<your-openai-resource>.cognitiveservices.azure.com/"
AZURE_OPENAI_DEPLOYMENT="gpt-realtime-1.5"
AZURE_OPENAI_API_KEY="<your-api-key>"
REALTIME_VOICE="cedar"
```

Create the container app:

```bash
az containerapp create \
  --name "$ACA_NAME" \
  --resource-group "$RG" \
  --environment "$ACA_ENV" \
  --image "$ACR_LOGIN_SERVER/$IMAGE_NAME:$IMAGE_TAG" \
  --target-port 8765 \
  --ingress external \
  --registry-server "$ACR_LOGIN_SERVER" \
  --cpu 0.5 \
  --memory 1.0Gi \
  --min-replicas 1 \
  --max-replicas 3 \
  --env-vars \
    AZURE_OPENAI_ENDPOINT="$AZURE_OPENAI_ENDPOINT" \
    AZURE_OPENAI_DEPLOYMENT="$AZURE_OPENAI_DEPLOYMENT" \
    AZURE_OPENAI_API_VERSION="2025-04-01-preview" \
    BRIDGE_BIND_HOST="0.0.0.0" \
    BRIDGE_BIND_PORT="8765" \
    REALTIME_VOICE="$REALTIME_VOICE" \
    REALTIME_INSTRUCTIONS="You are a friendly phone agent. Answer naturally, keep responses concise, and ask clarifying questions when needed." \
  --secrets "azure-openai-api-key=$AZURE_OPENAI_API_KEY" \
  --secret-env-vars AZURE_OPENAI_API_KEY=azure-openai-api-key
```

If the app already exists, update it:

```bash
az containerapp update \
  --name "$ACA_NAME" \
  --resource-group "$RG" \
  --image "$ACR_LOGIN_SERVER/$IMAGE_NAME:$IMAGE_TAG" \
  --set-env-vars \
    AZURE_OPENAI_ENDPOINT="$AZURE_OPENAI_ENDPOINT" \
    AZURE_OPENAI_DEPLOYMENT="$AZURE_OPENAI_DEPLOYMENT" \
    AZURE_OPENAI_API_VERSION="2025-04-01-preview" \
    BRIDGE_BIND_HOST="0.0.0.0" \
    BRIDGE_BIND_PORT="8765" \
    REALTIME_VOICE="$REALTIME_VOICE" \
    REALTIME_INSTRUCTIONS="You are a friendly phone agent. Answer naturally, keep responses concise, and ask clarifying questions when needed."
```

If you need to refresh the secret too:

```bash
az containerapp secret set \
  --name "$ACA_NAME" \
  --resource-group "$RG" \
  --secrets "azure-openai-api-key=$AZURE_OPENAI_API_KEY"
```

**Get The Public WebSocket URL**
Fetch the Container App FQDN:

```bash
ACA_FQDN="$(az containerapp show --name "$ACA_NAME" --resource-group "$RG" --query properties.configuration.ingress.fqdn -o tsv)"
echo "$ACA_FQDN"
```

Your ACS media streaming URL should then be:

```text
wss://<ACA_FQDN>/media
```

Use the `/media` path so the Function App and Container App stay aligned with the deployed bridge route.

**Update The Function App Setting**
Set `ACS_MEDIA_STREAMING_URL` on the Function App to the Container App URL:

```bash
FUNCTIONAPP_NAME="<function-app-name>"

az functionapp config appsettings set \
  --name "$FUNCTIONAPP_NAME" \
  --resource-group "$RG" \
  --settings ACS_MEDIA_STREAMING_URL="wss://$ACA_FQDN/media"
```

You should also make sure the Function App has:

```text
AZURE_OPENAI_API_VERSION=2025-04-01-preview
```

in case you keep shared config values aligned across services.

**Deploy Using YAML Instead**
You can also start from [`aca-bridge.template.yaml`](/home/georgea/projects-wsl/ivr/code-incoming_call/aca-bridge.template.yaml), fill in the placeholders, and deploy with:

```bash
az containerapp create \
  --resource-group "$RG" \
  --yaml aca-bridge.yaml
```

**Smoke Test**
After deployment:

1. Confirm the Container App is running.
2. Confirm the Function App setting `ACS_MEDIA_STREAMING_URL` points to the Container App `wss://` URL.
3. Place a test call.
4. Check Container App logs:

```bash
az containerapp logs show \
  --name "$ACA_NAME" \
  --resource-group "$RG" \
  --follow
```

You want to see log lines indicating:
- ACS media websocket connected
- Realtime session created
- Realtime session updated
- Realtime response done

**Notes**
- Keep `minReplicas` at `1` if you want to avoid cold starts on live calls.
- The bridge uses WebSockets and should live in Container Apps or another WebSocket-friendly host, not inside the Function HTTP app.
- The deployed bridge uses the same raw Azure OpenAI Realtime WebSocket protocol as [`test.py`](/home/georgea/projects-wsl/ivr/code-incoming_call/test.py).
- Container Apps may still emit occasional probe-related WebSocket handshake noise in logs; that does not affect live call audio.
