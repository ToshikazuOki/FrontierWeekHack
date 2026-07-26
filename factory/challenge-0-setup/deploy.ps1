# Requires -Version 5.1
$ErrorActionPreference = 'Stop'

# =============================================================================
# Foundry Hackathon — Infrastructure Deployment Script の　PowerShell バージョンです。
# Provisions: AI Foundry (hub + project + model), Log Analytics, App Insights
# Region: swedencentral
# resource group: Set the resource group name to foundry-hackathon-rg 
# without any suffix; if one already exists, use that.
# =============================================================================

# --- Azure CLI extensions ----------------------------------------------------
# Auto-install required CLI extensions non-interactively (no Y/n prompts).
az config set extension.use_dynamic_install=yes_without_prompt --only-show-errors *>$null
az extension add --name application-insights --only-show-errors *>$null

# --- Configuration -----------------------------------------------------------
$LOCATION = if ($env:LOCATION) { $env:LOCATION } else { "swedencentral" }
$PROJECT_NAME = if ($env:PROJECT_NAME) { $env:PROJECT_NAME } else { "factory-project" }
$MODEL_DEPLOYMENT_NAME = if ($env:MODEL_DEPLOYMENT_NAME) { $env:MODEL_DEPLOYMENT_NAME } else { "gpt-5.4" }
$MODEL_NAME = if ($env:MODEL_NAME) { $env:MODEL_NAME } else { "gpt-5.4" }
$MODEL_VERSION = if ($env:MODEL_VERSION) { $env:MODEL_VERSION } else { "2026-03-05" }
$RESOURCE_GROUP = if ($env:RESOURCE_GROUP) { $env:RESOURCE_GROUP } else { "foundry-hackathon-rg" }

# Suffix 生成（4バイトのランダム16進数文字列）
$RandomBytes = New-Object Byte[] 4
(New-Object Security.Cryptography.RNGCryptoServiceProvider).GetBytes($RandomBytes)
$RandomSuffix = [System.BitConverter]::ToString($RandomBytes).Replace("-", "").ToLower()

$SUFFIX = if ($env:SUFFIX) { $env:SUFFIX } else { $RandomSuffix }
$FOUNDRY_RESOURCE_NAME = if ($env:FOUNDRY_RESOURCE_NAME) { $env:FOUNDRY_RESOURCE_NAME } else { "foundry-hack-$SUFFIX" }
$LOG_ANALYTICS_NAME = if ($env:LOG_ANALYTICS_NAME) { $env:LOG_ANALYTICS_NAME } else { "foundry-hack-logs-$SUFFIX" }
$APP_INSIGHTS_NAME = if ($env:APP_INSIGHTS_NAME) { $env:APP_INSIGHTS_NAME } else { "foundry-hack-insights-$SUFFIX" }

# --- Argument parsing --------------------------------------------------------
$TAGS = [System.Collections.Generic.List[string]]::new()
$TAGS.Add("environment=hack")

$i = 0
while ($i -lt $args.Count) {
    if ($args[$i] -eq "--tags") {
        $i++
        while ($i -lt $args.Count -and -not $args[$i].StartsWith("--")) {
            $TAGS.Add($args[$i])
            $i++
        }
    } else {
        Write-Error "Unknown argument: $($args[$i])"
        Write-Host "Usage: .\deploy.ps1 [--tags 'Key=Value' ...]" -ForegroundColor Red
        exit 1
    }
}

Write-Host "=============================================="
Write-Host "  Foundry Hackathon — Infrastructure Deploy"
Write-Host "=============================================="
Write-Host ""
Write-Host "Suffix:            $SUFFIX"
Write-Host "Resource Group:    $RESOURCE_GROUP"
Write-Host "Location:          $LOCATION"
Write-Host "Foundry Resource:  $FOUNDRY_RESOURCE_NAME"
Write-Host "Project:           $PROJECT_NAME"
Write-Host "Model Deployment:  $MODEL_DEPLOYMENT_NAME"
Write-Host "Model Name:        $MODEL_NAME"
Write-Host "Model Version:     $MODEL_VERSION"
Write-Host "Tags:              $($TAGS -join ' ')"
Write-Host ""

# --- Resource Group ----------------------------------------------------------
# リソースグループの存在確認
Write-Host ">>> Checking if resource group '$RESOURCE_GROUP' exists..."
$rgExists = (az group exists --name $RESOURCE_GROUP) -eq "true"

if ($rgExists) {
    Write-Host "✓ Resource group '$RESOURCE_GROUP' already exists. Using existing resource group." -ForegroundColor Green
} else {
    Write-Host ">>> Resource group not found. Creating resource group '$RESOURCE_GROUP'..."
    az group create `
        --name $RESOURCE_GROUP `
        --location $LOCATION `
        --output none `
        --tags $TAGS
}

# --- AI Foundry Hub ----------------------------------------------------------
Write-Host ">>> Creating Microsoft Foundry Account resource (AIServices)..."
$SUBSCRIPTION_ID = az account show --query id -o tsv

$hubJson = @{
    kind = "AIServices"
    sku = @{ name = "S0" }
    location = $LOCATION
    identity = @{ type = "SystemAssigned" }
    properties = @{
        customSubDomainName = $FOUNDRY_RESOURCE_NAME
        publicNetworkAccess = "Enabled"
        allowProjectManagement = $true
    }
} | ConvertTo-Json -Depth 5 -Compress

# PowerShell が az.cmd に渡す際にダブルクォーテーションが消失するのを防ぐエスケープ処理
$hubBodyEscaped = $hubJson.Replace('"', '\"')

try {
    az rest `
        --method PUT `
        --headers 'Content-Type=application/json' `
        --url "https://management.azure.com/subscriptions/$SUBSCRIPTION_ID/resourceGroups/$RESOURCE_GROUP/providers/Microsoft.CognitiveServices/accounts/${FOUNDRY_RESOURCE_NAME}?api-version=2026-03-01" `
        --body $hubBodyEscaped `
        --output none
} catch {
    # エラー時はスルー
}


Write-Host ">>> Waiting for AIServices resource to reach Succeeded state..."
for ($retry = 1; $retry -le 36; $retry++) {
    $PROV_STATE = az cognitiveservices account show `
        --name $FOUNDRY_RESOURCE_NAME `
        --resource-group $RESOURCE_GROUP `
        --query "properties.provisioningState" -o tsv 2>$null

    if (-not $PROV_STATE) { $PROV_STATE = "Pending" }

    if ($PROV_STATE -eq "Succeeded") {
        Write-Host "    ✓ Provisioning complete."
        break
    } elseif ($PROV_STATE -eq "Failed") {
        Write-Error "❌ AIServices resource provisioning failed. Check the Azure portal for details."
        exit 1
    }
    Write-Host "    State: $PROV_STATE — retrying in 10s... ($retry/36)"
    Start-Sleep -Seconds 10
}

# Force-enable key auth and verify
$FOUNDRY_RESOURCE_ID = az cognitiveservices account show `
    --name $FOUNDRY_RESOURCE_NAME `
    --resource-group $RESOURCE_GROUP `
    --query id -o tsv

try {
    az resource update `
        --ids $FOUNDRY_RESOURCE_ID `
        --set properties.disableLocalAuth=false `
        --output none
} catch {}

az resource update `
    --ids $FOUNDRY_RESOURCE_ID `
    --set properties.allowProjectManagement=true `
    --output none

$DISABLE_LOCAL_AUTH = az cognitiveservices account show `
    --name $FOUNDRY_RESOURCE_NAME `
    --resource-group $RESOURCE_GROUP `
    --query properties.disableLocalAuth -o tsv

if ($DISABLE_LOCAL_AUTH -eq "true") {
    Write-Host "⚠️  API key authentication is disabled by Azure Policy on this tenant." -ForegroundColor Yellow
    Write-Host "   The deployment will continue — use DefaultAzureCredential (Entra ID) in your code." -ForegroundColor Yellow
}

Write-Host ">>> Creating Microsoft Foundry project..."
az cognitiveservices account project create `
    --name $FOUNDRY_RESOURCE_NAME `
    --resource-group $RESOURCE_GROUP `
    --project-name $PROJECT_NAME `
    --location $LOCATION `
    --output none

# --- Model Deployment --------------------------------------------------------
Write-Host ">>> Deploying model: $MODEL_NAME ($MODEL_VERSION)..."
az cognitiveservices account deployment create `
    --name $FOUNDRY_RESOURCE_NAME `
    --resource-group $RESOURCE_GROUP `
    --deployment-name $MODEL_DEPLOYMENT_NAME `
    --model-name $MODEL_NAME `
    --model-version $MODEL_VERSION `
    --model-format OpenAI `
    --sku-capacity 10 `
    --sku-name GlobalStandard `
    --output none

# --- Log Analytics Workspace -------------------------------------------------
Write-Host ">>> Creating Log Analytics workspace..."
az monitor log-analytics workspace create `
    --resource-group $RESOURCE_GROUP `
    --workspace-name $LOG_ANALYTICS_NAME `
    --location $LOCATION `
    --output none

$LOG_ANALYTICS_ID = az monitor log-analytics workspace show `
    --resource-group $RESOURCE_GROUP `
    --workspace-name $LOG_ANALYTICS_NAME `
    --query id -o tsv

# --- Application Insights ----------------------------------------------------
Write-Host ">>> Creating Application Insights (linked to Log Analytics)..."
az monitor app-insights component create `
    --app $APP_INSIGHTS_NAME `
    --resource-group $RESOURCE_GROUP `
    --location $LOCATION `
    --workspace $LOG_ANALYTICS_ID `
    --output none

$APP_INSIGHTS_CONN_STRING = az monitor app-insights component show `
    --app $APP_INSIGHTS_NAME `
    --resource-group $RESOURCE_GROUP `
    --query connectionString -o tsv

$APP_INSIGHTS_INSTRUMENTATION_KEY = az monitor app-insights component show `
    --app $APP_INSIGHTS_NAME `
    --resource-group $RESOURCE_GROUP `
    --query instrumentationKey -o tsv

$APP_INSIGHTS_RESOURCE_ID = az monitor app-insights component show `
    --app $APP_INSIGHTS_NAME `
    --resource-group $RESOURCE_GROUP `
    --query id -o tsv

# --- Connect App Insights to the Foundry account ----------------------------
Write-Host ">>> Connecting Application Insights to Foundry account..."
$connJson = @{
    properties = @{
        category = "AppInsights"
        target = $APP_INSIGHTS_RESOURCE_ID
        authType = "ApiKey"
        credentials = @{ key = $APP_INSIGHTS_CONN_STRING }
        isSharedToAll = $true
        metadata = @{
            ApiType = "Azure"
            ResourceId = $APP_INSIGHTS_RESOURCE_ID
        }
    }
} | ConvertTo-Json -Depth 5 -Compress

$connBodyEscaped = $connJson.Replace('"', '\"')

try {
    az rest `
        --method PUT `
        --headers 'Content-Type=application/json' `
        --url "https://management.azure.com/subscriptions/$SUBSCRIPTION_ID/resourceGroups/$RESOURCE_GROUP/providers/Microsoft.CognitiveServices/accounts/$FOUNDRY_RESOURCE_NAME/connections/appinsights-conn?api-version=2025-06-01" `
        --body $connBodyEscaped `
        --output none
} catch {
    Write-Host "⚠️  Could not link Application Insights to the account automatically." -ForegroundColor Yellow
    Write-Host "   Tracing (Challenge 2) can still be configured later from the Foundry portal." -ForegroundColor Yellow
}

# --- Retrieve endpoint and connection details -------------------------------
Write-Host ">>> Retrieving Foundry endpoint and keys..."
$FOUNDRY_ENDPOINT = az cognitiveservices account show `
    --name $FOUNDRY_RESOURCE_NAME `
    --resource-group $RESOURCE_GROUP `
    --query "properties.endpoint" -o tsv


$PROJECT_CONNECTION_STRING = az cognitiveservices account project show `
    --name $FOUNDRY_RESOURCE_NAME `
    --resource-group $RESOURCE_GROUP `
    --project-name $PROJECT_NAME `
    --query "properties.endpoints.\`"AI Foundry API\`"" -o tsv



# --- Write .env file ----------------------------------------------------------
$ROOT_DIR = Resolve-Path "$PSScriptRoot\.."
$ENV_FILE = Join-Path $ROOT_DIR ".env"

Write-Host ">>> Writing .env file to: $ENV_FILE"

$CurrentDate = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
$EnvContent = @"
# =============================================================================
# Foundry Hackathon — Environment Variables
# Auto-generated by deploy.ps1 on $CurrentDate
# =============================================================================

# Azure Subscription
AZURE_SUBSCRIPTION_ID=$SUBSCRIPTION_ID
RESOURCE_GROUP=$RESOURCE_GROUP

# AI Foundry
FOUNDRY_RESOURCE_NAME=$FOUNDRY_RESOURCE_NAME
PROJECT_NAME=$PROJECT_NAME
FOUNDRY_ENDPOINT=$FOUNDRY_ENDPOINT
PROJECT_CONNECTION_STRING=$PROJECT_CONNECTION_STRING
MODEL_DEPLOYMENT_NAME=$MODEL_DEPLOYMENT_NAME

# Application Insights & Monitoring
APPLICATIONINSIGHTS_CONNECTION_STRING=$APP_INSIGHTS_CONN_STRING
APPINSIGHTS_INSTRUMENTATION_KEY=$APP_INSIGHTS_INSTRUMENTATION_KEY

# Tracing (set to true to enable GenAI tracing)
AZURE_EXPERIMENTAL_ENABLE_GENAI_TRACING=true
OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true
"@

Set-Content -Path $ENV_FILE -Value $EnvContent -Encoding utf8

Write-Host ""
Write-Host "=============================================="
Write-Host "  ✅ DEPLOYMENT COMPLETE" -ForegroundColor Green
Write-Host "=============================================="
Write-Host ""
Write-Host "  .env file written to: $ENV_FILE"
Write-Host ""