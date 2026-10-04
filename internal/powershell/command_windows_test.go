package powershell

import (
	"os"
	"os/exec"
	"strings"
	"testing"
)

// Re-enter this native Go executable from each real shell. Calling 5.1 directly
// from pwsh would miss the environment inherited by an intermediary such as sbc.
func TestCommandUsesWindowsModules(t *testing.T) {
	if os.Getenv("SBC_MODULE_TEST_CHILD") == "1" {
		before := os.Getenv("PSModulePath")
		if before == "" {
			t.Fatal("the parent shell supplied no module path")
		}
		output, err := Command(t.Context(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Restricted", "-Command", `
$ErrorActionPreference = 'Stop'
Import-Module Microsoft.PowerShell.Security
Import-Module Microsoft.PowerShell.Utility
if ((Get-ExecutionPolicy) -ne 'Restricted') { throw 'Wrong execution policy' }
@{ value = $env:SBC_MODULE_TEST_VALUE } | ConvertTo-Json -Compress
`).CombinedOutput()
		if err != nil || !strings.Contains(string(output), `"value":"preserved"`) {
			t.Fatalf("Windows PowerShell modules: %v\n%s", err, output)
		}
		if os.Getenv("PSModulePath") != before {
			t.Fatal("changed the caller's module path")
		}
		return
	}
	for _, engine := range []string{"powershell.exe", "pwsh.exe"} {
		t.Run(engine, func(t *testing.T) {
			if _, err := exec.LookPath(engine); err != nil {
				t.Skipf("%s is not installed", engine)
			}
			binary, err := os.Executable()
			if err != nil {
				t.Fatal(err)
			}
			command := exec.CommandContext(t.Context(), engine, "-NoProfile", "-NonInteractive", "-Command",
				`& $env:SBC_MODULE_TEST_BINARY "-test.run=^TestCommandUsesWindowsModules$" "-test.v"; exit $LASTEXITCODE`)
			command.Env = append(os.Environ(), "SBC_MODULE_TEST_CHILD=1", "SBC_MODULE_TEST_VALUE=preserved", "SBC_MODULE_TEST_BINARY="+binary)
			if output, err := command.CombinedOutput(); err != nil {
				t.Fatalf("%s -> native process -> Windows PowerShell: %v\n%s", engine, err, output)
			}
		})
	}
}
