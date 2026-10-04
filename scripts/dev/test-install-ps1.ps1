#Requires -Version 5.1
# Fixture-only bootstrapper tests: no real registry, tasks or client installation.
$ErrorActionPreference = 'Stop'
$installer = Join-Path $PSScriptRoot '..\..\sing_box_manager\web\client\install.ps1'
$tokens = $null
$errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($installer, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
foreach ($statement in $ast.EndBlock.Statements) {
    if ($statement -is [Management.Automation.Language.FunctionDefinitionAst]) {
        . ([scriptblock]::Create($statement.Extent.Text))
    }
}

function Assert-Test {
    param([bool] $Condition, [string] $Message)
    if (-not $Condition) { throw $Message }
}
function Assert-Throws {
    param([scriptblock] $Body, [string] $Message)
    $failure = ''
    try { & $Body } catch { $failure = $_.Exception.Message }
    Assert-Test ($failure -like ('*' + $Message + '*')) "Expected failure '$Message', got '$failure'"
}
function Write-Fixture {
    param([string] $Path, [string] $Content)
    [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($Path))
    [IO.File]::WriteAllText($Path, $Content)
}

# Exercise the actual child-process wrapper, including pipe closure, exit status,
# stderr and redaction. The credential is never passed in a process argument.
$engine = (Get-Process -Id $PID).Path
$source = '$value = [Console]::In.ReadToEnd(); [Console]::Out.Write($value); [Console]::Error.Write("child stderr"); exit 7'
$encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($source))
$script:fixtureLink = 'https://vpn.example.com/sub/abcdefghijklmnopqrstuv'
$result = Invoke-SbcProgram $engine @('-NoProfile', '-NonInteractive', '-EncodedCommand', $encoded) $script:fixtureLink
Assert-Test ($result.Code -eq 7) 'Child exit status was lost'
Assert-Test ($result.Output.Contains('[subscription link]') -and $result.Output.Contains('child stderr')) 'Pipe output was lost'
Assert-Test (-not $result.Output.Contains('abcdefghijklmnopqrstuv')) 'Child output leaked a credential'

foreach ($value in @($script:fixtureLink, ($script:fixtureLink + '/'), 'http://127.0.0.1:8123/sub/abcdefghijklmnopqrstuv')) {
    [void](Get-SbcUri $value -Subscription)
}
foreach ($value in @('', 'http://vpn.example.com/sub/abcdefghijklmnopqrstuv', ($script:fixtureLink + '?x=1'), ($script:fixtureLink + '#x'),
        'https://user:pass@vpn.example.com/sub/abcdefghijklmnopqrstuv', 'file:///sub/abcdefghijklmnopqrstuv',
        'https://vpn.example.com/sub/short', 'https://vpn.example.com/sub/abcdefghijklmnopqrstuv/extra')) {
    Assert-Throws { Get-SbcUri $value -Subscription } 'not a subscription link'
}
# Help must work as a downloaded script block without changing execution policy.
$policies = Get-ExecutionPolicy -List | Out-String
& ([scriptblock]::Create([IO.File]::ReadAllText($installer))) -Help
Assert-Test ((Get-ExecutionPolicy -List | Out-String) -eq $policies) 'Help changed execution policy'

$oldLocal = $env:LOCALAPPDATA
$oldLanguage = $env:SBC_LANG
$oldAllowAdmin = $env:SBC_ALLOW_ADMIN
$testRoot = Join-Path ([IO.Path]::GetTempPath()) ('sbc-bootstrap-test-' + [Guid]::NewGuid().ToString('N'))
$script:cases = 0
$script:events = New-Object 'Collections.Generic.List[string]'
$script:downloads = New-Object 'Collections.Generic.List[string]'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    try {
        $env:SBC_ALLOW_ADMIN = ''
        Assert-Throws { Assert-SbcWindows } 'without administrator rights'
        $env:SBC_ALLOW_ADMIN = '1'
        Assert-SbcWindows
    }
    finally {
        $env:SBC_ALLOW_ADMIN = $oldAllowAdmin
    }
}
$env:SBC_ALLOW_ADMIN = '1'
function Start-Sleep { param([int] $Seconds) }
function Read-SbcLink { $script:events.Add('prompt'); return $script:fixtureLink }
function Invoke-SbcRequest {
    param([Uri] $Uri, [string] $Token = '', [string] $Destination = '')
    if ($Destination) {
        Assert-Test ($Uri.AbsoluteUri -eq ($script:fixtureLink + '/files/sbc/windows-amd64/sbc')) 'Wrong binary URL'
        Assert-Test (-not $Token) 'Token forwarded to download'
        $script:downloads.Add($Destination)
        if ($script:downloadFailure) { throw 'fixture download failure' }
        Write-Fixture $Destination 'fixture executable'
        return
    }
    Assert-Test ($Uri.AbsoluteUri -eq 'https://vpn.example.com/api/sub') 'Wrong token exchange URL'
    Assert-Test ($Token -eq 'fixture-machine-token') 'Wrong saved token'
    $script:events.Add('exchange')
    if ($script:exchangeFailure) { throw 'fixture exchange failure' }
    return ('{"url":"' + $script:exchangeLink + '"}')
}
function Invoke-SbcProgram {
    param([string] $Binary, [string[]] $Arguments, [string] $InputText = '')
    Assert-Test (-not (($Arguments -join ' ').Contains('abcdefghijklmnopqrstuv'))) 'Credential in command arguments'
    if ($Arguments[0] -eq 'install') { Assert-Test ($InputText -eq $script:fixtureLink) 'Install did not receive link on stdin' }
    else { Assert-Test (-not $InputText) 'Unexpected credential on another command' }
    $command = $Arguments -join ' '
    $script:events.Add($command)
    $code = 0
    if ($Arguments[0] -eq $script:failedCommand) { $code = 1 }
    if ($Arguments[0] -eq 'port' -and $script:portFailures -gt 0) { $script:portFailures--; $code = 1 }
    return @{ Code = $code; Output = '' }
}
function Reset-Fixture {
    $script:cases++
    $env:LOCALAPPDATA = Join-Path $testRoot ([string]$script:cases)
    [void][IO.Directory]::CreateDirectory($env:LOCALAPPDATA)
    $script:events.Clear()
    $script:downloads.Clear()
    $script:downloadFailure = $false
    $script:exchangeFailure = $false
    $script:exchangeLink = $script:fixtureLink
    $script:failedCommand = ''
    $script:cleanupFailure = $false
    $script:desktop = 'ours'
    $script:portFailures = 0
}
function New-LegacyFixture {
    $root = Join-Path $env:LOCALAPPDATA 'sing-box'
    Write-Fixture (Join-Path $root 'config\selected-protocol') 'hysteria2'
    Write-Fixture (Join-Path $root 'config\selected-route') 'gfw'
    Write-Fixture (Join-Path $root 'config\selected-port') '1085'
    Write-Fixture (Join-Path $root 'config\portal-token') 'fixture-machine-token'
    Write-Fixture (Join-Path $root 'config\hysteria2\config.json') '{"inbounds":[{"type":"mixed","listen_port":1085}]}'
    Write-Fixture (Join-Path $root 'data\portal-base-url') 'https://vpn.example.com'
    Write-Fixture (Join-Path $root 'data\lib\ui.ps1') '# fixture'
    Write-Fixture (Join-Path $root 'data\lib\platform.ps1') '# fixture'
    Write-Fixture (Join-Path $root 'data\lib\system-proxy.ps1') 'function Get-SbmSystemProxyState { return $script:desktop }'
    Write-Fixture (Join-Path $root 'data\client-uninstall.ps1') @'
param([switch] $Yes, [switch] $Quiet, [string] $ScriptRoot, [ref] $Result)
Assert-Test ($Yes -and $Quiet) 'Legacy cleanup was interactive'
Assert-Test ($ScriptRoot -eq (Join-Path $env:LOCALAPPDATA 'sing-box\data')) 'Wrong legacy cleanup root'
$script:events.Add('legacy uninstall')
if ($script:cleanupFailure) { throw 'fixture cleanup failure' }
Remove-Item -LiteralPath (Split-Path $ScriptRoot -Parent) -Recurse -Force
$Result.Value = $true
'@
}
function Assert-CleanDownload {
    foreach ($path in $script:downloads) {
        Assert-Test (-not (Test-Path -LiteralPath (Split-Path $path -Parent))) 'Temporary download directory remains'
    }
}
function Assert-LegacyKept {
    Assert-Test (Test-Path -LiteralPath (Join-Path $env:LOCALAPPDATA 'sing-box\config\selected-protocol')) 'Old client removed after failure'
    Assert-Test (-not $script:events.Contains('legacy uninstall')) 'Old uninstaller ran after failure'
    Assert-CleanDownload
}

try {
    $env:SBC_LANG = 'en'
    Reset-Fixture
    Install-Sbc
    Assert-Test (($script:events -join ',') -eq 'prompt,install') 'Fresh install failed'
    Assert-CleanDownload

    Reset-Fixture
    Write-Fixture (Join-Path $env:LOCALAPPDATA 'sbc\subscription') $script:fixtureLink
    Install-Sbc -Protocol trojan
    Assert-Test (($script:events -join ',') -eq 'install --protocol trojan,protocol trojan,speed') 'Reinstall did not reuse its link'

    Reset-Fixture
    New-LegacyFixture
    $script:portFailures = 1
    Install-Sbc
    Assert-Test (($script:events -join ',') -eq 'exchange,install --route gfw --auth off --protocol hysteria2,protocol hysteria2,route gfw,speed,legacy uninstall,port 1085,port 1085,desktop on') 'Migration order or settings were lost'
    Assert-Test (-not (Test-Path -LiteralPath (Join-Path $env:LOCALAPPDATA 'sing-box'))) 'Legacy files remain'
    Assert-CleanDownload

    foreach ($state in @('other', 'off')) {
        Reset-Fixture
        New-LegacyFixture
        $script:desktop = $state
        Install-Sbc -Protocol trojan
        Assert-Test ($script:events.Contains('protocol trojan')) 'Explicit protocol ignored'
        Assert-Test (-not $script:events.Contains('desktop on')) 'Changed unowned desktop settings'
    }

    foreach ($failure in @('exchange', 'foreign-link')) {
        Reset-Fixture
        New-LegacyFixture
        if ($failure -eq 'exchange') { $script:exchangeFailure = $true }
        else { $script:exchangeLink = 'https://other.example.com/sub/abcdefghijklmnopqrstuv' }
        Install-Sbc
        Assert-Test ($script:events.Contains('prompt')) 'Invalid saved token response did not fall back to prompt'
    }

    foreach ($command in @('install', 'protocol', 'route', 'speed')) {
        Reset-Fixture
        New-LegacyFixture
        $script:failedCommand = $command
        Assert-Throws { Install-Sbc } 'old client has not been removed'
        Assert-LegacyKept
        Assert-Test ($script:events.Contains('uninstall --yes')) 'Partial new install was not cleaned up'
    }

    Reset-Fixture
    New-LegacyFixture
    Write-Fixture (Join-Path $env:LOCALAPPDATA 'sbc\subscription') $script:fixtureLink
    $script:failedCommand = 'install'
    Assert-Throws { Install-Sbc } 'old client has not been removed'
    Assert-LegacyKept
    Assert-Test (-not $script:events.Contains('uninstall --yes')) 'Removed a preexisting sbc installation'

    Reset-Fixture
    New-LegacyFixture
    $script:downloadFailure = $true
    Assert-Throws { Install-Sbc } 'fixture download failure'
    Assert-LegacyKept
    Assert-Test (($script:events -join ',') -eq 'exchange') 'Client ran after download failure'

    Reset-Fixture
    New-LegacyFixture
    Remove-Item -LiteralPath (Join-Path $env:LOCALAPPDATA 'sing-box\data\client-uninstall.ps1')
    Assert-Throws { Install-Sbc } 'missing its uninstaller'
    Assert-Test ($script:events.Count -eq 0) 'Incomplete legacy install was not rejected before download'

    Reset-Fixture
    New-LegacyFixture
    Write-Fixture (Join-Path $env:LOCALAPPDATA 'sing-box\config\selected-port') '0'
    Assert-Throws { Install-Sbc } 'unreadable port'
    Assert-LegacyKept

    Reset-Fixture
    New-LegacyFixture
    $script:cleanupFailure = $true
    Assert-Throws { Install-Sbc } 'Legacy cleanup stopped'
    Assert-Test (-not $script:events.Contains('port 1085')) 'Port handed off after incomplete legacy cleanup'
    Assert-Test (-not $script:events.Contains('uninstall --yes')) 'New client removed after legacy cleanup started'
    Assert-CleanDownload

    Reset-Fixture
    New-LegacyFixture
    $script:portFailures = 10
    Assert-Throws { Install-Sbc } 'sbc port 1085'
    Assert-Test (-not $script:events.Contains('desktop on')) 'Desktop changed before port handoff'
    Assert-CleanDownload

    Reset-Fixture
    New-LegacyFixture
    $script:failedCommand = 'desktop'
    Assert-Throws { Install-Sbc } 'sbc desktop on'
    Assert-CleanDownload

    Write-Host "Passed $script:cases bootstrapper fixture scenarios, URI validation, help and native stdin handling."
}
finally {
    $env:LOCALAPPDATA = $oldLocal
    $env:SBC_LANG = $oldLanguage
    $env:SBC_ALLOW_ADMIN = $oldAllowAdmin
    if (Test-Path -LiteralPath $testRoot) { Remove-Item -LiteralPath $testRoot -Recurse -Force }
}
