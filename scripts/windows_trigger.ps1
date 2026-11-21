<#
.SYNOPSIS
    Calls the SpotifyAddSongButton POST /add-current endpoint and displays a Windows toast notification.

.DESCRIPTION
    Designed to be triggered from a Windows Shortcut (.lnk) with an assigned Shortcut key
    (e.g., Ctrl+Alt+S). Reads LAMBDA_FUNCTION_URL and API_KEY from parameters, environment
    variables, or the repository's .env file (in that order).

.EXAMPLE
    powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "C:\path\to\scripts\windows_trigger.ps1"

.EXAMPLE
    .\scripts\windows_trigger.ps1 -TestToast
#>

[CmdletBinding()]
param(
    [string]$ApiUrl,
    [string]$ApiKey,
    [switch]$TestToast
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Read-DotEnv {
    param([string]$Path)

    $Values = @{}
    if (-not (Test-Path -LiteralPath $Path)) {
        return $Values
    }

    foreach ($Line in [System.IO.File]::ReadAllLines($Path, [System.Text.Encoding]::UTF8)) {
        $Trimmed = $Line.Trim()
        if ([string]::IsNullOrWhiteSpace($Trimmed) -or $Trimmed.StartsWith("#")) {
            continue
        }
        $EqIndex = $Trimmed.IndexOf("=")
        if ($EqIndex -lt 1) {
            continue
        }
        $Key = $Trimmed.Substring(0, $EqIndex).Trim()
        $Val = $Trimmed.Substring($EqIndex + 1).Trim()
        if (
            ($Val.StartsWith('"') -and $Val.EndsWith('"') -and $Val.Length -ge 2) -or
            ($Val.StartsWith("'") -and $Val.EndsWith("'") -and $Val.Length -ge 2)
        ) {
            $Val = $Val.Substring(1, $Val.Length - 2)
        }
        $Values[$Key] = $Val
    }
    return $Values
}

function Show-WindowsToast {
    param(
        [Parameter(Mandatory = $true)][string]$Title,
        [Parameter(Mandatory = $true)][string]$Message
    )

    try {
        [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
        [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null

        $EscapedTitle = [System.Security.SecurityElement]::Escape($Title)
        $EscapedMessage = [System.Security.SecurityElement]::Escape($Message)

        $ToastXmlString = @"
<toast duration="short">
  <visual>
    <binding template="ToastGeneric">
      <text>$EscapedTitle</text>
      <text>$EscapedMessage</text>
    </binding>
  </visual>
</toast>
"@

        $XmlDoc = New-Object Windows.Data.Xml.Dom.XmlDocument
        $XmlDoc.LoadXml($ToastXmlString)

        $Toast = New-Object Windows.UI.Notifications.ToastNotification $XmlDoc
        $AppId = "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
        $Notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($AppId)
        $Notifier.Show($Toast)
        return
    }
    catch {
        # Fallback for PowerShell 7+ or systems where WinRT toast invocation is unavailable.
        try {
            Add-Type -AssemblyName System.Windows.Forms
            Add-Type -AssemblyName System.Drawing
            $Notify = New-Object System.Windows.Forms.NotifyIcon
            $Notify.Icon = [System.Drawing.SystemIcons]::Information
            $Notify.Visible = $true
            $Notify.ShowBalloonTip(4000, $Title, $Message, [System.Windows.Forms.ToolTipIcon]::Info)
            Start-Sleep -Milliseconds 4200
            $Notify.Dispose()
        }
        catch {
            Write-Host "$Title`: $Message"
        }
    }
}

if ($TestToast) {
    Show-WindowsToast -Title "Spotify Add Song Button" -Message "Toast notifications are working!"
    exit 0
}

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$DotEnv = Read-DotEnv -Path (Join-Path $ProjectRoot ".env")

if ([string]::IsNullOrWhiteSpace($ApiUrl)) {
    if (-not [string]::IsNullOrWhiteSpace($env:LAMBDA_FUNCTION_URL)) {
        $ApiUrl = $env:LAMBDA_FUNCTION_URL
    }
    elseif ($DotEnv.ContainsKey("LAMBDA_FUNCTION_URL")) {
        $ApiUrl = $DotEnv["LAMBDA_FUNCTION_URL"]
    }
}

if ([string]::IsNullOrWhiteSpace($ApiKey)) {
    if (-not [string]::IsNullOrWhiteSpace($env:API_KEY)) {
        $ApiKey = $env:API_KEY
    }
    elseif ($DotEnv.ContainsKey("API_KEY")) {
        $ApiKey = $DotEnv["API_KEY"]
    }
}

if ([string]::IsNullOrWhiteSpace($ApiUrl) -or $ApiUrl -like "*your-function-url*") {
    Show-WindowsToast -Title "Spotify Add Failed" -Message "LAMBDA_FUNCTION_URL is not set in .env."
    exit 1
}

if ([string]::IsNullOrWhiteSpace($ApiKey) -or $ApiKey -eq "your_random_secret_key") {
    Show-WindowsToast -Title "Spotify Add Failed" -Message "API_KEY is not set in .env."
    exit 1
}

$NormalizedUrl = $ApiUrl.Trim().TrimEnd("/")
if (-not $NormalizedUrl.EndsWith("/add-current")) {
    $Endpoint = "$NormalizedUrl/add-current"
}
else {
    $Endpoint = $NormalizedUrl
}

try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
}
catch {
    # Ignore on runtimes that manage TLS automatically.
}

try {
    $Response = Invoke-RestMethod `
        -Method Post `
        -Uri $Endpoint `
        -Headers @{ "X-API-Key" = $ApiKey } `
        -TimeoutSec 20

    $Title = switch ($Response.status) {
        "added"           { "Added to Playlist" }
        "already_exists"  { "Already in Playlist" }
        "nothing_playing" { "Spotify" }
        "unsupported"     { "Cannot Add Track" }
        default           { "Spotify" }
    }

    $Body = if ($Response.message) { [string]$Response.message } else { "Request completed." }
    Show-WindowsToast -Title $Title -Message $Body
}
catch {
    $ErrorDetail = $_.Exception.Message
    try {
        if ($_.Exception.Response) {
            $StatusCode = [int]$_.Exception.Response.StatusCode
            $Stream = $_.Exception.Response.GetResponseStream()
            if ($Stream) {
                $Reader = New-Object System.IO.StreamReader($Stream, [System.Text.Encoding]::UTF8)
                $RawBody = $Reader.ReadToEnd()
                $Reader.Dispose()
                if (-not [string]::IsNullOrWhiteSpace($RawBody)) {
                    try {
                        $Parsed = $RawBody | ConvertFrom-Json
                        if ($Parsed.detail) {
                            $ErrorDetail = "HTTP $StatusCode`: $($Parsed.detail)"
                        }
                        else {
                            $ErrorDetail = "HTTP $StatusCode`: $RawBody"
                        }
                    }
                    catch {
                        $ErrorDetail = "HTTP $StatusCode`: $RawBody"
                    }
                }
                else {
                    $ErrorDetail = "HTTP $StatusCode"
                }
            }
        }
    }
    catch {
        # Keep default exception message.
    }

    Show-WindowsToast -Title "Spotify Add Failed" -Message $ErrorDetail
    exit 1
}
