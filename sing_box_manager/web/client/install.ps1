#Requires -Version 5.1
# Install sbc from a subscription link, or migrate the PowerShell client.
# Run as an in-memory script block so execution policy does not gate a file:
#   & ([scriptblock]::Create((irm 'https://<portal>/install.ps1')))
[CmdletBinding()]
param(
    [Alias('p')]
    [ValidateSet('', 'trojan', 'hysteria2', 'naive')]
    [string] $Protocol = '',
    [switch] $Help
)

function Get-SbcText {
    param([string] $Text)
    $language = $env:SBC_LANG
    if ([string]::IsNullOrEmpty($language)) {
        $language = [Globalization.CultureInfo]::CurrentUICulture.Name
        foreach ($locale in @($env:LC_ALL, $env:LC_MESSAGES, $env:LANG)) {
            if (-not [string]::IsNullOrEmpty($locale)) { $language = $locale; break }
        }
    }
    if ($language -notmatch '^(zh$|zh[-_](CN|SG))') { return $Text }
    $translations = @{
        'Subscription link' = '订阅链接'
        'Run this installer on 64-bit Windows, without administrator rights.' = '请在 64 位 Windows 上以普通用户身份运行安装程序。'
        'That is not a subscription link. Use HTTPS, or HTTP on loopback for a local test.' = '这不是订阅链接。请使用 HTTPS；本地测试可使用回环地址上的 HTTP。'
        'The request failed. Check the subscription link and network connection.' = '请求失败，请检查订阅链接和网络连接。'
        'The saved token did not get a subscription link. Paste the link from the portal.' = '保存的令牌未能取得订阅链接，请粘贴门户中的链接。'
        'The old client is missing its uninstaller or libraries. Repair it before migrating.' = '旧客户端缺少卸载程序或库，请先修复再迁移。'
        'The old client has unreadable port, route or protocol settings. Repair them before migrating.' = '旧客户端的端口、路由或协议设置无法读取，请先修复再迁移。'
        'Downloading sbc for Windows...' = '正在下载 Windows 版 sbc……'
        'sbc installation failed. The old client has not been removed.' = 'sbc 安装失败，旧客户端尚未移除。'
        'sbc installation failed. Check the output above before retrying.' = 'sbc 安装失败，请查看上面的输出后重试。'
        'Removing the old PowerShell client...' = '正在移除旧版 PowerShell 客户端……'
        'Legacy cleanup stopped. sbc is running; finish the old uninstaller, then run: sbc port {0}' = '旧客户端清理中断。sbc 正在运行；请完成旧客户端的卸载，再运行：sbc port {0}'
        'sbc is running on a spare port. When the old port is free, run: sbc port {0}' = 'sbc 正在备用端口运行。旧端口空出后，请运行：sbc port {0}'
        'sbc is installed. Restore the desktop proxy with: sbc desktop on' = 'sbc 已安装。要恢复桌面代理，请运行：sbc desktop on'
        'The old client has been replaced. Use sbc instead of proxy.' = '旧客户端已替换，请改用 sbc 命令。'
        'sbc is installed. Open a new terminal from Start to use its PATH and proxy variables.' = 'sbc 已安装。请从开始菜单打开新终端，以使用新的 PATH 和代理变量。'
        'The client command could not run.' = '客户端命令无法运行。'
    }
    if ($translations.ContainsKey($Text)) { return $translations[$Text] }
    return $Text
}

function Assert-SbcWindows {
    $architecture = $env:PROCESSOR_ARCHITEW6432
    if ([string]::IsNullOrEmpty($architecture)) { $architecture = $env:PROCESSOR_ARCHITECTURE }
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT -or $architecture -notin @('AMD64', 'ARM64')) {
        throw (Get-SbcText 'Run this installer on 64-bit Windows, without administrator rights.')
    }
    if ($env:SBC_ALLOW_ADMIN -eq '1') { return }
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    try {
        $principal = New-Object Security.Principal.WindowsPrincipal($identity)
        if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
            throw (Get-SbcText 'Run this installer on 64-bit Windows, without administrator rights.')
        }
    }
    finally { $identity.Dispose() }
}

function Read-SbcLine {
    param([string] $Path)
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        return ([IO.File]::ReadAllText($Path)).Trim()
    }
    return ''
}

function Get-SbcUri {
    param([string] $Text, [switch] $Subscription)
    $uri = $null
    $valid = [Uri]::TryCreate($Text.Trim(), [UriKind]::Absolute, [ref]$uri)
    if (-not $valid -or $uri.UserInfo -or $uri.Query -or $uri.Fragment -or
        $Text.Trim() -match '[\x00-\x20\\]' -or
        ($uri.Scheme -ne 'https' -and -not ($uri.Scheme -eq 'http' -and $uri.IsLoopback))) {
        throw (Get-SbcText 'That is not a subscription link. Use HTTPS, or HTTP on loopback for a local test.')
    }
    if (($Subscription -and $uri.AbsolutePath -cnotmatch '^/sub/[A-Za-z0-9_-]{22}/?$') -or
        (-not $Subscription -and $uri.AbsolutePath -ne '/')) {
        throw (Get-SbcText 'That is not a subscription link. Use HTTPS, or HTTP on loopback for a local test.')
    }
    return $uri
}

function Invoke-SbcRequest {
    param([Uri] $Uri, [string] $Token = '', [string] $Destination = '')
    # No proxy dependency during bootstrap and no redirects carrying a token
    # to another host. The initial executable relies on the portal's TLS.
    $response = $null
    $stream = $null
    $file = $null
    try {
        $request = [Net.HttpWebRequest]::Create($Uri)
        $request.Proxy = $null
        $request.AllowAutoRedirect = $false
        $request.Timeout = 30000
        $request.ReadWriteTimeout = 30000
        if ($Token) {
            $request.Method = 'POST'
            $request.ContentLength = 0
            $request.Headers['Authorization'] = 'Bearer ' + $Token
        }
        $response = $request.GetResponse()
        if ([int]$response.StatusCode -ne 200) { throw 'HTTP request failed' }
        $stream = $response.GetResponseStream()
        if ($Destination) {
            $file = [IO.File]::Create($Destination)
            $stream.CopyTo($file)
            return
        }
        $reader = New-Object IO.StreamReader($stream, [Text.Encoding]::UTF8)
        try { return $reader.ReadToEnd() } finally { $reader.Dispose() }
    }
    catch {
        # WebException includes the URL, which contains the subscription token.
        throw (Get-SbcText 'The request failed. Check the subscription link and network connection.')
    }
    finally {
        if ($null -ne $file) { $file.Dispose() }
        if ($null -ne $stream) { $stream.Dispose() }
        if ($null -ne $response) { $response.Dispose() }
    }
}

function Read-SbcLink {
    $answer = Read-Host (Get-SbcText 'Subscription link') -AsSecureString
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($answer)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
}

function Get-SbcLegacyLink {
    param([string] $Root)
    try {
        $base = Get-SbcUri (Read-SbcLine (Join-Path $Root 'data\portal-base-url'))
        $token = Read-SbcLine (Join-Path $Root 'config\portal-token')
        if ($token -notmatch '^[A-Za-z0-9._@:+-]+$') { return '' }
        $response = Invoke-SbcRequest -Uri ([Uri]::new($base, '/api/sub')) -Token $token | ConvertFrom-Json
        $link = Get-SbcUri $response.url -Subscription
        if ($link.GetLeftPart([UriPartial]::Authority) -ne $base.GetLeftPart([UriPartial]::Authority)) { return '' }
        return $link.AbsoluteUri.TrimEnd('/')
    }
    catch { return '' }
}

function Read-SbcLegacy {
    param([string] $Root)
    $data = Join-Path $Root 'data'
    $config = Join-Path $Root 'config'
    if (-not (Test-Path -LiteralPath (Join-Path $config 'selected-protocol')) -and
        -not (Test-Path -LiteralPath (Join-Path $data 'client-uninstall.ps1'))) { return $null }
    foreach ($name in @('client-uninstall.ps1', 'lib\ui.ps1', 'lib\platform.ps1', 'lib\system-proxy.ps1')) {
        if (-not (Test-Path -LiteralPath (Join-Path $data $name) -PathType Leaf)) {
            throw (Get-SbcText 'The old client is missing its uninstaller or libraries. Repair it before migrating.')
        }
    }
    try {
        $protocol = Read-SbcLine (Join-Path $config 'selected-protocol')
        $route = Read-SbcLine (Join-Path $config 'selected-route')
        if (-not $route) { $route = 'china' }
        if ($protocol -notin @('trojan', 'hysteria2', 'naive') -or $route -notin @('china', 'gfw', 'ai', 'global')) { throw 'Invalid settings' }
        $profile = [IO.File]::ReadAllText((Join-Path $config ($protocol + '\config.json'))) | ConvertFrom-Json
        $mixed = @($profile.inbounds | Where-Object { $_.type -eq 'mixed' })
        if ($mixed.Count -ne 1) { throw 'No mixed inbound' }
        $port = Read-SbcLine (Join-Path $config 'selected-port')
        if (-not $port) { $port = [string]$mixed[0].listen_port }
        $number = 0
        if (-not [int]::TryParse($port, [ref]$number) -or $number -lt 1024 -or $number -gt 65535) { throw 'Invalid port' }
        $auth = 'off'
        if (@($mixed[0].users).Count -gt 0 -and $null -ne $mixed[0].users) { $auth = 'on' }
        $desktop = & {
            param($Directory)
            foreach ($name in @('platform.ps1', 'system-proxy.ps1')) {
                . ([scriptblock]::Create([IO.File]::ReadAllText((Join-Path $Directory ('lib\' + $name)))))
            }
            Get-SbmSystemProxyState
        } $data
        return @{ Root = $data; Port = $number; Route = $route; Protocol = $protocol; Auth = $auth; Desktop = $desktop }
    }
    catch {
        throw (Get-SbcText 'The old client has unreadable port, route or protocol settings. Repair them before migrating.')
    }
}

function Invoke-SbcProgram {
    param([string] $Binary, [string[]] $Arguments, [string] $InputText = '')
    $process = New-Object Diagnostics.Process
    try {
        $process.StartInfo.FileName = $Binary
        # Callers pass only fixed flags, validated names and numeric ports.
        $process.StartInfo.Arguments = $Arguments -join ' '
        $process.StartInfo.UseShellExecute = $false
        $process.StartInfo.CreateNoWindow = $true
        $process.StartInfo.RedirectStandardInput = $true
        $process.StartInfo.RedirectStandardOutput = $true
        $process.StartInfo.RedirectStandardError = $true
        [void]$process.Start()
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        if ($InputText) { $process.StandardInput.WriteLine($InputText) }
        $process.StandardInput.Close()
        $process.WaitForExit()
        $output = $stdout.Result + $stderr.Result
        if ($InputText) { $output = $output.Replace($InputText, '[subscription link]') }
        return @{ Code = $process.ExitCode; Output = $output.Trim() }
    }
    catch { throw (Get-SbcText 'The client command could not run.') }
    finally { $process.Dispose() }
}

function Show-SbcResult {
    param($Result)
    if ($Result.Output) { Write-Host $Result.Output }
    return ($Result.Code -eq 0)
}

function Remove-SbcLegacy {
    param([string] $Root)
    $removed = $false
    & ([scriptblock]::Create([IO.File]::ReadAllText((Join-Path $Root 'client-uninstall.ps1')))) `
        -Yes -Quiet -ScriptRoot $Root -Result ([ref]$removed)
    if (-not $removed) { throw 'Uninstallation incomplete' }
    Remove-Module sing-box-proxy -Force -ErrorAction SilentlyContinue
}

function Install-Sbc {
    param([string] $Protocol = '', [switch] $Help)
    if ($Help) {
        Write-Host 'Usage: & ([scriptblock]::Create((irm ''https://<portal>/install.ps1''))) [-Protocol trojan|hysteria2|naive]'
        return
    }
    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'
    Assert-SbcWindows
    $local = $env:LOCALAPPDATA
    if ([string]::IsNullOrEmpty($local)) { throw (Get-SbcText 'Run this installer on 64-bit Windows, without administrator rights.') }
    $legacyRoot = Join-Path $local 'sing-box'
    $sbcRoot = Join-Path $local 'sbc'
    $legacy = Read-SbcLegacy $legacyRoot
    $hadSbc = Test-Path -LiteralPath $sbcRoot
    $work = Join-Path ([IO.Path]::GetTempPath()) ('sbc-install-' + [Guid]::NewGuid().ToString('N'))
    $securityProtocol = [Net.ServicePointManager]::SecurityProtocol
    try {
        [Net.ServicePointManager]::SecurityProtocol = $securityProtocol -bor [Net.SecurityProtocolType]::Tls12
        $link = ''
        if ($null -ne $legacy) {
            $link = Get-SbcLegacyLink $legacyRoot
            if (-not $link) { Write-Host (Get-SbcText 'The saved token did not get a subscription link. Paste the link from the portal.') }
        }
        elseif ($hadSbc) { $link = Read-SbcLine (Join-Path $sbcRoot 'subscription') }
        if (-not $link) { $link = Read-SbcLink }
        $uri = Get-SbcUri $link -Subscription
        $link = $uri.AbsoluteUri.TrimEnd('/')
        [void][IO.Directory]::CreateDirectory($work)
        $binary = Join-Path $work 'sbc.exe'
        Write-Host (Get-SbcText 'Downloading sbc for Windows...')
        Invoke-SbcRequest -Uri ([Uri]($link + '/files/sbc/windows-amd64/sbc')) -Destination $binary
        $arguments = @('install')
        if ($null -ne $legacy) {
            $arguments += @('--route', $legacy.Route, '--auth', $legacy.Auth)
            if (-not $Protocol) { $Protocol = $legacy.Protocol }
        }
        if ($Protocol) { $arguments += @('--protocol', $Protocol) }
        $installed = $false
        try {
            $installed = Show-SbcResult (Invoke-SbcProgram $binary $arguments $link)
            # install can fall back to a subscription default; migration must
            # keep the requested choices before the old client is removed.
            if ($installed -and $Protocol) { $installed = Show-SbcResult (Invoke-SbcProgram $binary @('protocol', $Protocol)) }
            if ($installed -and $null -ne $legacy) { $installed = Show-SbcResult (Invoke-SbcProgram $binary @('route', $legacy.Route)) }
            # A second selection can succeed after install used its fallback.
            # Verify that the requested protocol loads a page before cleanup.
            if ($installed -and $Protocol) { $installed = Show-SbcResult (Invoke-SbcProgram $binary @('speed')) }
        }
        catch { $installed = $false }
        if (-not $installed) {
            if ($null -ne $legacy) {
                if (-not $hadSbc) { try { [void](Invoke-SbcProgram $binary @('uninstall', '--yes')) } catch {} }
                throw (Get-SbcText 'sbc installation failed. The old client has not been removed.')
            }
            throw (Get-SbcText 'sbc installation failed. Check the output above before retrying.')
        }
        if ($null -ne $legacy) {
            Write-Host (Get-SbcText 'Removing the old PowerShell client...')
            try { Remove-SbcLegacy $legacy.Root }
            catch { throw ((Get-SbcText 'Legacy cleanup stopped. sbc is running; finish the old uninstaller, then run: sbc port {0}') -f $legacy.Port) }
            $moved = $false
            for ($attempt = 0; $attempt -lt 10; $attempt++) {
                $result = Invoke-SbcProgram $binary @('port', [string]$legacy.Port)
                if ($result.Code -eq 0) { $moved = $true; break }
                Start-Sleep -Seconds 1
            }
            if (-not $moved) { throw ((Get-SbcText 'sbc is running on a spare port. When the old port is free, run: sbc port {0}') -f $legacy.Port) }
            if ($legacy.Desktop -eq 'ours') {
                if (-not (Show-SbcResult (Invoke-SbcProgram $binary @('desktop', 'on')))) {
                    throw (Get-SbcText 'sbc is installed. Restore the desktop proxy with: sbc desktop on')
                }
            }
            Write-Host (Get-SbcText 'The old client has been replaced. Use sbc instead of proxy.')
        }
        Write-Host (Get-SbcText 'sbc is installed. Open a new terminal from Start to use its PATH and proxy variables.')
    }
    finally {
        [Net.ServicePointManager]::SecurityProtocol = $securityProtocol
        if (Test-Path -LiteralPath $work) { Remove-Item -LiteralPath $work -Recurse -Force }
    }
}

Install-Sbc -Protocol $Protocol -Help:$Help
