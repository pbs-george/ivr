**Overview**
This repo now has two deployable parts:

1. [`function_app.py`] as the Azure Function that answers ACS calls.
2. [`bridge_server.py`] as the Azure Container Apps WebSocket bridge that relays audio between ACS and Azure OpenAI Realtime.

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
RG="pbs-ivr-rg"
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
PHONE_DIRECTORY_MCP_URL="https://pbs-common-mcp.azurewebsites.net/mcp"
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
    REALTIME_INSTRUCTIONS="You are a friendly phone agent. Answer naturally, keep responses concise, and ask clarifying questions when needed. Only offer extension numbers when the caller explicitly asks for them. Do not volunteer people's names until you have narrowed the probable matches to two or fewer. If the caller's information still leaves more than two probable matches, ask for more information to narrow the choice. Queue names are less sensitive and may be shared when appropriate." \
    PHONE_DIRECTORY_MCP_URL="$PHONE_DIRECTORY_MCP_URL" \
  --secrets "azure-openai-api-key=$AZURE_OPENAI_API_KEY" \
  --secret-env-vars AZURE_OPENAI_API_KEY=azure-openai-api-key
```

If the app already exists, update it:

```bash
az containerapp update \
  --name "$ACA_NAME" \
  --resource-group "$RG" \
  --image "$ACR_LOGIN_SERVER/$IMAGE_NAME:$IMAGE_TAG" \
  --min-replicas 1 \
  --max-replicas 3 \
  --set-env-vars \
    AZURE_OPENAI_ENDPOINT="$AZURE_OPENAI_ENDPOINT" \
    AZURE_OPENAI_DEPLOYMENT="$AZURE_OPENAI_DEPLOYMENT" \
    AZURE_OPENAI_API_VERSION="2025-04-01-preview" \
    BRIDGE_BIND_HOST="0.0.0.0" \
    BRIDGE_BIND_PORT="8765" \
    REALTIME_VOICE="$REALTIME_VOICE" \
    REALTIME_INSTRUCTIONS="You are a friendly phone agent. Answer naturally, keep responses concise, and ask clarifying questions when needed. Only offer extension numbers when the caller explicitly asks for them. Do not volunteer people's names until you have narrowed the probable matches to two or fewer. If the caller's information still leaves more than two probable matches, ask for more information to narrow the choice. Queue names are less sensitive and may be shared when appropriate." \
    PHONE_DIRECTORY_MCP_URL="$PHONE_DIRECTORY_MCP_URL"
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

For MCP-backed tool use, the bridge container also needs:

```text
PHONE_DIRECTORY_MCP_URL=https://pbs-common-mcp.azurewebsites.net/mcp
```

**Deploy The Function App**
For the current Flex Consumption Function App, deploy the Python app with zip deploy plus remote build.

Set the Function App name:

```bash
FUNCTIONAPP_NAME="pbs-ivr-app"
```

Make sure the app's deployment storage exists and the app settings point to it. If the storage account is missing, recreate it:

```bash
DEPLOY_STORAGE_ACCOUNT="pbsivrrgbcaa"

az storage account create \
  --name "$DEPLOY_STORAGE_ACCOUNT" \
  --resource-group "$RG" \
  --location "$LOCATION" \
  --sku Standard_LRS \
  --kind StorageV2 \
  --https-only true \
  --allow-blob-public-access false
```

Update both storage-related app settings to use that account's connection string:

```bash
DEPLOY_STORAGE_CONNECTION_STRING="$(az storage account show-connection-string \
  --name "$DEPLOY_STORAGE_ACCOUNT" \
  --resource-group "$RG" \
  -o tsv)"

az functionapp config appsettings set \
  --name "$FUNCTIONAPP_NAME" \
  --resource-group "$RG" \
  --settings \
    AzureWebJobsStorage="$DEPLOY_STORAGE_CONNECTION_STRING" \
    DEPLOYMENT_STORAGE_CONNECTION_STRING="$DEPLOY_STORAGE_CONNECTION_STRING"
```

Create the deployment container expected by Flex Consumption and restart the app once:

```bash
az storage container create \
  --name "app-package-$FUNCTIONAPP_NAME-43a8dc4" \
  --account-name "$DEPLOY_STORAGE_ACCOUNT" \
  --connection-string "$DEPLOY_STORAGE_CONNECTION_STRING"

az functionapp restart \
  --name "$FUNCTIONAPP_NAME" \
  --resource-group "$RG"
```

Package the Function App from the repo root:

```bash
zip -r /tmp/"$FUNCTIONAPP_NAME".zip . \
  -x '.git/*' '.venv/*' '.vscode/*' '__pycache__/*' 'local.settings.json' 'test*' '*.pyc' 'function_app.py.AnswersCalls'
```

Deploy the package with remote build enabled:

```bash
az functionapp deployment source config-zip \
  --resource-group "$RG" \
  --name "$FUNCTIONAPP_NAME" \
  --src /tmp/"$FUNCTIONAPP_NAME".zip \
  --build-remote true
```

Verify only the expected function is active:

```bash
az functionapp function list \
  --resource-group "$RG" \
  --name "$FUNCTIONAPP_NAME" \
  -o table
```

If you still see old disabled functions from a previous deployment, remove stale disable flags:

```bash
az functionapp config appsettings delete \
  --name "$FUNCTIONAPP_NAME" \
  --resource-group "$RG" \
  --setting-names \
    AzureWebJobs.match_ringcentral_directory.Disabled \
    AzureWebJobs.refresh_ringcentral_directory.Disabled \
    AzureWebJobs.refresh_ringcentral_directory_status.Disabled \
    AzureWebJobs.refresh_ringcentral_directory_timer.Disabled
```

**Deploy Using YAML Instead**
You can also start from [`aca-bridge.template.yaml`](/home/georgea/projects-wsl/ivr/code-ivr/aca-bridge.template.yaml), fill in the placeholders, and deploy with:

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
5. Confirm the Function App only exposes the expected route:

```bash
az functionapp function list \
  --resource-group "$RG" \
  --name "$FUNCTIONAPP_NAME" \
  -o table
```

**Notes**
- Keep `minReplicas` at `1` if you want to avoid cold starts on live calls.
- If you ever see `minReplicas` drift back to `0`, ACS can answer the call before the bridge finishes starting, which shows up as `MediaStreamingFailed` with silence to the caller.
- The bridge uses WebSockets and should live in Container Apps or another WebSocket-friendly host, not inside the Function HTTP app.
- The current Function App is on Flex Consumption, so Function deploys depend on the deployment storage account configured by `DEPLOYMENT_STORAGE_CONNECTION_STRING`.
- The deployed bridge uses the same raw Azure OpenAI Realtime WebSocket protocol as [`test.py`](/home/georgea/projects-wsl/ivr/code-incoming_call/test.py).
- Container Apps may still emit occasional probe-related WebSocket handshake noise in logs; that does not affect live call audio.
