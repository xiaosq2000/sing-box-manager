# The caller substitutes only a validated process-user SID and on/off action.
# The policy table and metadata path are embedded, never read from arguments,
# files, stdin, environment variables, or another registry value.
$ErrorActionPreference = 'Stop'
$env:PSModulePath = $null
$sid = '@@SID@@'
$action = '@@ACTION@@'
$metadataPath = '@@METADATA@@'
$policies = @(
@@POLICIES@@
)
$hive = $null
$metadata = $null
$mutex = $null
$locked = $false

function Read-Policy($policy) {
    $key = $hive.OpenSubKey($policy.Key, $false)
    try {
        $present = $null -ne $key -and $key.GetValueNames() -contains $policy.Name
        $matches = $false
        if ($present) {
            $matches = $key.GetValueKind($policy.Name) -eq [Microsoft.Win32.RegistryValueKind]::String -and
                $key.GetValue($policy.Name, $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames) -ceq $policy.Value
        }
        return @{ Present = $present; Matches = $matches }
    } finally {
        if ($null -ne $key) { $key.Dispose() }
    }
}

try {
    # Parse the SID again in the elevated process. HKCU would target the UAC
    # administrator instead of the user who requested protection.
    $identity = [System.Security.Principal.SecurityIdentifier]::new($sid)
    if ($identity.Value -cne $sid -or @('on', 'off') -notcontains $action) { throw 'Invalid request' }
    $sections = [System.Security.AccessControl.AccessControlSections]::Owner -bor [System.Security.AccessControl.AccessControlSections]::Access

    # Serialize every registry read/write across helper processes, sessions
    # and UAC administrator accounts for this original user. Only admins and
    # SYSTEM can open the mutex. Refuse a squatted object; never fix its ACL.
    $mutexSecurity = [System.Security.AccessControl.MutexSecurity]::new()
    $mutexSecurity.SetSecurityDescriptorSddlForm('O:BAG:BAD:P(A;;0x1f0001;;;SY)(A;;0x1f0001;;;BA)')
    $created = $false
    $mutex = [System.Threading.Mutex]::new($false, 'Global\sbc.WebRTC.' + $sid, [ref]$created, $mutexSecurity)
    if ($mutex.GetAccessControl().GetSecurityDescriptorSddlForm($sections) -cne $mutexSecurity.GetSecurityDescriptorSddlForm($sections)) {
        throw 'Untrusted policy mutex'
    }
    try {
        $locked = $mutex.WaitOne(120000)
    } catch {
        $cause = $_.Exception
        while ($null -ne $cause.InnerException) { $cause = $cause.InnerException }
        # An abandoned mutex is acquired by this process. Durable intents
        # below recover the interrupted operation while we keep that lock.
        if ($cause -is [System.Threading.AbandonedMutexException]) { $locked = $true } else { throw }
    }
    if (-not $locked) { throw 'Policy helper is busy' }

    $hive = [Microsoft.Win32.Registry]::Users.OpenSubKey($sid, $true)
    if ($null -eq $hive) { throw 'User hive is not loaded' }

    # A non-administrator must not be able to forge ownership and make the
    # elevated helper delete a pre-existing policy. Supply a restrictive ACL
    # only when creating the new private ownership key. Never change an
    # existing key's ACL or any browser ACL; refuse an untrusted owner/DACL.
    $security = [System.Security.AccessControl.RegistrySecurity]::new()
    $security.SetSecurityDescriptorSddlForm('O:BAG:BAD:P(A;;KA;;;SY)(A;;KA;;;BA)(A;;KR;;;' + $sid + ')')
    $expectedSecurity = $security.GetSecurityDescriptorSddlForm($sections)
    $metadata = $hive.OpenSubKey($metadataPath, $true)
    if ($null -ne $metadata) {
        if ($metadata.GetAccessControl($sections).GetSecurityDescriptorSddlForm($sections) -cne $expectedSecurity) {
            throw 'Untrusted ownership key'
        }
        foreach ($policy in $policies) {
            if ($metadata.GetValueNames() -contains $policy.Owner) {
                if ($metadata.GetValueKind($policy.Owner) -ne [Microsoft.Win32.RegistryValueKind]::DWord -or
                    $metadata.GetValue($policy.Owner) -ne 1) { throw 'Invalid ownership intent' }
            }
        }
    }

    if ($action -eq 'on') {
        # Check every Chromium conflict before writing anything. Firefox
        # existing values, including foreign values, always remain untouched.
        $missing = $false
        foreach ($policy in $policies) {
            $current = Read-Policy $policy
            if ($policy.Chromium -and $current.Present -and -not $current.Matches) { throw 'Policy conflict' }
            if (-not $current.Present) { $missing = $true }
        }
        if ($missing -and $null -eq $metadata) {
            $metadata = $hive.CreateSubKey($metadataPath, [Microsoft.Win32.RegistryKeyPermissionCheck]::ReadWriteSubTree, $security)
            if ($metadata.GetAccessControl($sections).GetSecurityDescriptorSddlForm($sections) -cne $expectedSecurity) {
                throw 'Untrusted ownership key'
            }
        }
        foreach ($policy in $policies) {
            $current = Read-Policy $policy
            if ($current.Present) {
                if ($policy.Chromium -and -not $current.Matches) { throw 'Policy conflict' }
                continue
            }
            # Intent survives process interruption or a failed browser write.
            $metadata.SetValue($policy.Owner, 1, [Microsoft.Win32.RegistryValueKind]::DWord)
            $metadata.Flush()
            if ($metadata.GetValueKind($policy.Owner) -ne [Microsoft.Win32.RegistryValueKind]::DWord -or
                $metadata.GetValue($policy.Owner) -ne 1) { throw 'Ownership intent was not retained' }
            $key = $hive.CreateSubKey($policy.Key)
            try {
                # Recheck after opening writable: do not overwrite a policy
                # that appeared between the initial read and key creation.
                if ($key.GetValueNames() -contains $policy.Name) {
                    $current = Read-Policy $policy
                    if ($policy.Chromium -and -not $current.Matches) { throw 'Policy conflict' }
                    # This helper did not write the value; do not own it.
                    $metadata.DeleteValue($policy.Owner, $false)
                    $metadata.Flush()
                    continue
                }
                $key.SetValue($policy.Name, $policy.Value, [Microsoft.Win32.RegistryValueKind]::String)
                $key.Flush()
            } finally { $key.Dispose() }
            if (-not (Read-Policy $policy).Matches) { throw 'Policy write was not retained' }
        }
        foreach ($policy in $policies) {
            $current = Read-Policy $policy
            if (-not $current.Present -or ($policy.Chromium -and -not $current.Matches)) { throw 'Incomplete setup' }
        }
    } elseif ($null -ne $metadata) {
        $lastIntent = $null
        foreach ($policy in $policies) {
            if ($metadata.GetValueNames() -notcontains $policy.Owner) { continue }
            $current = Read-Policy $policy
            if ($current.Matches) {
                $key = $hive.OpenSubKey($policy.Key, $true)
                if ($null -eq $key) { throw 'Policy key disappeared' }
                try {
                    # Both type and value must still match immediately before
                    # deletion. A later external edit is not ours to remove.
                    if ((Read-Policy $policy).Matches) {
                        $key.DeleteValue($policy.Name, $false)
                        $key.Flush()
                    }
                } finally { $key.Dispose() }
            }
            if ((Read-Policy $policy).Matches) { throw 'Policy deletion was not retained' }
            # Keep the intent until deletion/preservation has been verified.
            # If this is the final value in our otherwise empty private key,
            # retain it until key deletion itself succeeds. A failed key
            # deletion must still leave a marker that triggers a retry.
            if ($metadata.GetValueNames().Length -eq 1 -and $metadata.GetSubKeyNames().Length -eq 0) {
                $lastIntent = $policy.Owner
                break
            }
            $metadata.DeleteValue($policy.Owner, $false)
            $metadata.Flush()
            if ($metadata.GetValueNames() -contains $policy.Owner) { throw 'Ownership deletion was not retained' }
        }
        # Delete only an empty key or one containing the final verified intent.
        # Never delete unrelated metadata, browser keys or browser values.
        if ($null -ne $lastIntent -or ($metadata.GetValueNames().Length -eq 0 -and $metadata.GetSubKeyNames().Length -eq 0)) {
            $metadata.Dispose()
            $metadata = $null
            $hive.DeleteSubKey($metadataPath, $false)
            $check = $hive.OpenSubKey($metadataPath, $false)
            if ($null -ne $check) {
                $check.Dispose()
                throw 'Ownership key deletion was not retained'
            }
        }
    }
    exit 0
} catch {
    # Do not print values or exception text. The unelevated caller reports a
    # fixed error and keeps recovery state for retry.
    exit 1
} finally {
    try {
        if ($null -ne $metadata) { $metadata.Dispose() }
        if ($null -ne $hive) { $hive.Dispose() }
    } finally {
        if ($null -ne $mutex) {
            try { if ($locked) { $mutex.ReleaseMutex() } } finally { $mutex.Dispose() }
        }
    }
}
