$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$request = [Console]::In.ReadToEnd() | ConvertFrom-Json
$key = $null
try {
    if ($null -eq $request.Changes) {
        $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey($request.Key, $false)
        $values = @{}
        foreach ($name in $request.Names) {
            if ($null -ne $key -and $key.GetValueNames() -contains $name) {
                $values[$name] = @{
                    Text = [string]$key.GetValue($name, $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
                    Kind = [string]$key.GetValueKind($name)
                }
            }
        }
        ConvertTo-Json -InputObject $values -Compress
    } else {
        # Compile notification calls before touching the registry.
        if ($request.Notification) {
            Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class SBCSettings {
    [DllImport("wininet.dll", SetLastError = true)]
    public static extern bool InternetSetOption(IntPtr h, int option, IntPtr buffer, int length);
    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    public static extern IntPtr SendMessageTimeout(IntPtr h, uint msg, UIntPtr w, string l, uint flags, uint timeout, out UIntPtr result);
}
'@
        }
        $key = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey($request.Key)
        $before = @{}
        $written = @()
        try {
            # Write the address and bypass list before enabling the desktop proxy.
            $changes = $request.Changes.PSObject.Properties | Sort-Object @{ Expression = { if ($_.Name -eq 'ProxyEnable') { 1 } else { 0 } } }, Name
            foreach ($change in $changes) {
                $name = $change.Name
                if ($key.GetValueNames() -contains $name) {
                    $before[$name] = @{
                        Value = $key.GetValue($name, $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
                        Kind = $key.GetValueKind($name)
                    }
                }
                $written += $name
                if ($change.Value.Kind) {
                    $kind = [Microsoft.Win32.RegistryValueKind]$change.Value.Kind
                    $value = $change.Value.Text
                    if ($kind -eq [Microsoft.Win32.RegistryValueKind]::DWord) { $value = [int]$value }
                    $key.SetValue($name, $value, $kind)
                } else {
                    $key.DeleteValue($name, $false)
                }
            }
            if ($request.Notification -eq 'desktop') {
                foreach ($option in @(39, 95)) {
                    if (-not [SBCSettings]::InternetSetOption([IntPtr]::Zero, $option, [IntPtr]::Zero, 0)) {
                        throw 'Could not refresh WinINet settings'
                    }
                }
            } elseif ($request.Notification -eq 'environment') {
                # An unresponsive window must not block an environment change.
                $result = [UIntPtr]::Zero
                [void][SBCSettings]::SendMessageTimeout([IntPtr]0xffff, 0x1a, [UIntPtr]::Zero, 'Environment', 2, 2000, [ref]$result)
            }
        } catch {
            foreach ($name in $written) {
                if ($before.ContainsKey($name)) {
                    $key.SetValue($name, $before[$name].Value, $before[$name].Kind)
                } else {
                    $key.DeleteValue($name, $false)
                }
            }
            if ($request.Notification -eq 'desktop') {
                foreach ($option in @(39, 95)) {
                    [void][SBCSettings]::InternetSetOption([IntPtr]::Zero, $option, [IntPtr]::Zero, 0)
                }
            }
            throw
        }
    }
} catch {
    # Do not print registry values, which can include credentials.
    exit 1
} finally {
    if ($null -ne $key) { $key.Dispose() }
}
